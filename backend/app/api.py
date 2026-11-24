import asyncio
from contextlib import asynccontextmanager
from pathlib import Path, PurePath
import sqlite3
from typing import Annotated, Literal
import unicodedata
from uuid import uuid4

from fastapi import Depends, FastAPI, HTTPException, Query, Request
from fastapi.exception_handlers import request_validation_exception_handler
from fastapi.exceptions import RequestValidationError
from fastapi.responses import FileResponse, JSONResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from httpx import AsyncBaseTransport
from pydantic import BaseModel, Field
from starlette.middleware.trustedhost import TrustedHostMiddleware

from .agent import Agent
from .auth import Account, current_account, router as auth_router
from .config import ROOT, SAMPLES, UPLOAD_LIMIT, Settings
from .events import Events
from .input_repair import FormatRepair, input_diagnostics, preview_repair
from .jobs import Jobs, TERMINAL, log_page, registered_input
from .mcp_tools import open_tool_client
from .model import DeepSeek
from .store import Store
from .tools import Tools, find_job

SUFFIXES = {".fa", ".fasta", ".fna"}
SignedIn = Annotated[Account, Depends(current_account)]
LogStream = Literal["stdout", "stderr"]


class SendMessage(BaseModel):
    client_message_id: str = Field(min_length=1, max_length=80)
    text: str = Field(min_length=1, max_length=4000)


class OutputPreferences(BaseModel):
    result_order: Literal["file", "sequence_id"] | None = None
    gc_decimal_places: int | None = Field(default=None, ge=0, le=2, strict=True)


class ResolveInput(BaseModel):
    file_id: str | None = Field(default=None, min_length=1, max_length=120)
    repair_token: str | None = Field(default=None, pattern=r"^[0-9a-f]{64}$")


def create_app(
    settings: Settings | None = None,
    *,
    model_transport: AsyncBaseTransport | None = None,
    frontend_dir: Path | None = None,
) -> FastAPI:
    @asynccontextmanager
    async def lifespan(app: FastAPI):
        resolved = settings or Settings.load()
        (resolved.data_dir / "uploads").mkdir(parents=True, exist_ok=True)
        store = Store(resolved.data_dir / "assistant.sqlite3", ROOT / "data")
        events = Events()
        model = DeepSeek(resolved, model_transport)
        jobs = Jobs(resolved, store, events, lambda job: agent.complete(job))
        try:
            async with open_tool_client(Tools(store, jobs)) as tools:
                agent = Agent(store, events, model, tools)
                app.state.settings, app.state.events, app.state.store = resolved, events, store
                app.state.agent, app.state.jobs, app.state.tools = agent, jobs, tools
                app.state.password_slots = asyncio.Semaphore(2)
                try:
                    for pending in store.recover():
                        agent.complete(pending)
                    jobs.start()
                    yield
                finally:
                    await jobs.close()
                    await agent.close()
        finally:
            events.close()
            await model.close()
            store.close()

    app = FastAPI(title="科研计算任务助手", lifespan=lifespan)
    app.add_middleware(TrustedHostMiddleware, allowed_hosts=["localhost", "127.0.0.1"])
    app.include_router(auth_router)

    @app.exception_handler(RequestValidationError)
    async def validation_error(request: Request, exc: RequestValidationError):
        if request.url.path.startswith("/api/auth/"):
            return JSONResponse({"detail": "请检查用户名和密码的长度与格式。"}, status_code=422)
        return await request_validation_exception_handler(request, exc)

    @app.middleware("http")
    async def require_local_origin(request: Request, call_next):
        origin = request.headers.get("origin")
        expected_origin = f"{request.url.scheme}://{request.headers.get('host')}"
        if request.method == "POST" and origin and origin != expected_origin:
            return JSONResponse({"detail": "请从同一个本机应用地址提交请求。"}, status_code=403)
        response = await call_next(request)
        if request.url.path.startswith("/api/"):
            response.headers.setdefault("Cache-Control", "no-store")
            response.headers["X-Content-Type-Options"] = "nosniff"
        return response

    def conversation_or_404(account: Account, conversation_id: str) -> dict:
        conversation = app.state.store.owned_conversation(account.id, conversation_id)
        if not conversation:
            raise HTTPException(404, "会话不存在。")
        return conversation

    @app.get("/api/config")
    async def config(account: SignedIn):
        resolved = app.state.settings
        return {"model": resolved.model, "key_configured": bool(resolved.api_key),
                "samples": list(SAMPLES), "upload_limit": UPLOAD_LIMIT}

    @app.get("/api/preferences", response_model=OutputPreferences)
    async def output_preferences(account: SignedIn):
        return app.state.store.output_preferences(account.id)

    @app.post("/api/preferences", response_model=OutputPreferences)
    async def save_output_preferences(body: OutputPreferences, account: SignedIn):
        return app.state.store.save_output_preferences(account.id, body.result_order, body.gc_decimal_places)

    @app.get("/api/conversations")
    async def conversations(account: SignedIn):
        return app.state.store.conversations(account.id)

    @app.post("/api/conversations", status_code=201)
    async def new_conversation(account: SignedIn):
        return app.state.store.new_conversation(account.id)

    @app.get("/api/conversations/{conversation_id}")
    async def conversation(conversation_id: str, account: SignedIn):
        snapshot = app.state.store.snapshot(account.id, conversation_id)
        if not snapshot:
            raise HTTPException(404, "会话不存在。")
        return snapshot

    @app.post("/api/conversations/{conversation_id}/messages", status_code=202)
    async def send_message(conversation_id: str, body: SendMessage, account: SignedIn):
        conversation_or_404(account, conversation_id)
        if not app.state.settings.api_key:
            raise HTTPException(503, "请在 .env 中填写 DEEPSEEK_API_KEY 并重启后端。")
        text = body.text.strip()
        if not text:
            raise HTTPException(422, "请输入要发送的消息。")
        message, created = app.state.store.user_message(conversation_id, body.client_message_id, text)
        if created:
            app.state.events.send(account.id, "message.updated", message)
            app.state.agent.chat(account.id, message)
        return message

    @app.get("/api/events")
    async def events(account: SignedIn):
        async def data():
            if app.state.store.session(account.token_hash):
                async for event in app.state.events.listen(account.id, account.token_hash, account.expires_at):
                    yield event

        return StreamingResponse(
            data(),
            media_type="text/event-stream",
            headers={"Cache-Control": "no-store, no-transform", "X-Accel-Buffering": "no"},
        )

    @app.post("/api/conversations/{conversation_id}/files", status_code=201)
    async def upload_to_conversation(conversation_id: str, filename: str, request: Request, account: SignedIn):
        conversation_or_404(account, conversation_id)
        return await save_upload(account, filename, request, conversation_id)

    @app.get("/api/files")
    async def files(account: SignedIn):
        store = app.state.store
        return [store.public_file(row) for row in store.files(account.id)]

    @app.post("/api/files", status_code=201)
    async def upload(filename: str, request: Request, account: SignedIn):
        return await save_upload(account, filename, request)

    async def save_upload(account: Account, filename: str, request: Request, conversation_id: str | None = None):
        name = validated_filename(filename)
        store = app.state.store
        if any(row["name_key"] == name.casefold() for row in store.files(account.id)):
            raise HTTPException(409, "当前账号已有同名文件，请改名后再上传。")

        storage_id = uuid4().hex
        target = app.state.settings.data_dir / "uploads" / storage_id
        created = registered = False
        size = 0
        try:
            with target.open("xb") as output:
                created = True
                async for part in request.stream():
                    size += len(part)
                    if size > UPLOAD_LIMIT:
                        raise HTTPException(413, "文件超过 5 MiB，请使用较小的 FASTA。")
                    output.write(part)
            if not store.session(account.token_hash):
                raise HTTPException(401, "登录已失效，请重新登录后上传。")
            row = store.add_file(account.id, storage_id, name, size, conversation_id)
            registered = True
        except sqlite3.IntegrityError:
            raise HTTPException(409, "当前账号已有同名文件，请改名后再上传。") from None
        finally:
            if created and not registered:
                target.unlink(missing_ok=True)
        public = store.public_file(row)
        app.state.events.send(account.id, "file.added", {"file": public})
        return public

    @app.get("/api/files/{file_id}/download", response_class=FileResponse)
    async def download_file(file_id: str, account: SignedIn):
        file = app.state.store.owned_file(account.id, file_id)
        if not file:
            raise HTTPException(404, "文件不存在。")
        try:
            path = registered_input(app.state.settings.data_dir, file)
        except OSError:
            raise HTTPException(404, "文件不可读取。") from None
        return FileResponse(path, filename=file["display_name"], media_type="application/octet-stream")

    @app.get("/api/jobs")
    async def all_jobs(account: SignedIn):
        return app.state.store.jobs(account.id)

    def job_or_404(account: Account, job_id: str) -> dict:
        job = app.state.store.owned_job(account.id, job_id)
        if not job:
            raise HTTPException(404, "任务不存在。")
        return job

    @app.get("/api/jobs/{job_id}")
    async def job_detail(job_id: str, account: SignedIn):
        return job_or_404(account, job_id)

    @app.post("/api/jobs/{job_id}/retry", status_code=202)
    async def retry_job(job_id: str, account: SignedIn):
        original = job_or_404(account, job_id)
        if original["state"] != "failed":
            raise HTTPException(409, "只有已失败的任务可以重新运行。")
        if original["input_request_id"] and not original["next_attempt_id"]:
            raise HTTPException(409, "请在原会话选择修正后的文件，再继续分析。")
        job, created = app.state.store.retry_job(original)
        if created:
            app.state.events.send(account.id, "job.updated", app.state.store.job(job_id))
            app.state.jobs.enqueue(job)
        return job

    def input_or_404(account: Account, input_id: str) -> dict:
        pending = app.state.store.owned_input_request(account.id, input_id)
        if not pending:
            raise HTTPException(404, "找不到待补充文件的记录。")
        return pending

    def original_input(account: Account, pending: dict) -> Path | None:
        file = app.state.store.owned_file(account.id, pending["original_file_id"])
        if not file:
            return None
        try:
            return registered_input(app.state.settings.data_dir, file)
        except OSError:
            return None

    async def repair_for(account: Account, pending: dict) -> FormatRepair | None:
        if pending["resolved_job_id"] or pending["failure_code"] != "MISSING_HEADER":
            return None
        source = original_input(account, pending)
        return await asyncio.to_thread(preview_repair, source) if source else None

    def repair_response(repair: FormatRepair | None) -> dict | None:
        return {"token": repair.token, "line": repair.line, "before": repair.before, "after": repair.after} if repair else None

    @app.get("/api/input-requests/{input_id}/diagnostics")
    async def failure_diagnostics(input_id: str, account: SignedIn):
        pending = input_or_404(account, input_id)
        if not pending["failed_job_id"]:
            return {"issues": [], "truncated": False, "repair": None}
        log_path = app.state.jobs.log_path(pending["failed_job_id"], "stderr")
        diagnostics = await asyncio.to_thread(input_diagnostics, log_path, original_input(account, pending))
        repair = await repair_for(account, pending)
        return {**diagnostics, "repair": repair_response(repair)}

    @app.get("/api/input-requests/{input_id}/repair")
    async def input_repair(input_id: str, account: SignedIn):
        return repair_response(await repair_for(account, input_or_404(account, input_id)))

    @app.post("/api/input-requests/{input_id}/resolve", status_code=202)
    async def resolve_input(input_id: str, body: ResolveInput, account: SignedIn):
        pending = input_or_404(account, input_id)
        store = app.state.store
        if pending["resolved_job_id"]:
            return {"input_request": pending, "job": store.job(pending["resolved_job_id"]), "file": None}
        if bool(body.file_id) == bool(body.repair_token):
            raise HTTPException(422, "请选择一份文件，或确认已预览的修复。")
        if body.repair_token:
            repair = await repair_for(account, pending)
            if not repair or repair.token != body.repair_token:
                raise HTTPException(409, "修复预览已失效，请重新预览或上传修正版。")
            if not store.session(account.token_hash):
                raise HTTPException(401, "登录已失效，请重新登录后确认修复。")
            file_id = uuid4().hex
            filename = PurePath(pending["filename"])
            name = f"{filename.stem[:90]}-修复-{file_id[:10]}{filename.suffix}"
            target = app.state.settings.data_dir / "uploads" / file_id
            wrote = False
            try:
                with target.open("xb") as output:
                    wrote = True
                    output.write(repair.content)
                updated, job, created = store.resolve_input(account.id, input_id, file_id,
                                                            repaired_file=(name, len(repair.content)))
            except OSError:
                raise HTTPException(503, "无法保存格式修复副本。") from None
            finally:
                if wrote and not store.file(file_id):
                    target.unlink(missing_ok=True)
            saved = store.file(file_id)
            public = store.public_file(saved) if saved else None
            if public:
                app.state.events.send(account.id, "file.added", {"file": public})
        else:
            file = store.owned_file(account.id, body.file_id)
            if not file:
                raise HTTPException(404, "文件不存在。")
            if file["id"] == pending["original_file_id"]:
                raise HTTPException(409, "请选择修正后的文件，也可以上传一份副本。")
            try:
                registered_input(app.state.settings.data_dir, file)
            except OSError:
                raise HTTPException(409, "所选文件无法读取，请重新上传。") from None
            updated, job, created = store.resolve_input(account.id, input_id, file["id"])
            public = None

        app.state.events.send(account.id, "input.updated", updated)
        if created:
            if updated["failed_job_id"]:
                app.state.events.send(account.id, "job.updated", store.job(updated["failed_job_id"]))
            app.state.jobs.enqueue(job)
        return {"input_request": updated, "job": job, "file": public}

    @app.get("/api/jobs/{job_id}/logs/{stream}/content")
    async def job_log_page(job_id: str, stream: LogStream, account: SignedIn, offset: Annotated[int, Query(ge=0)] = 0):
        job = job_or_404(account, job_id)
        try:
            return log_page(app.state.jobs.log_path(job["id"], stream), offset, finished=job["state"] in TERMINAL)
        except OSError:
            raise HTTPException(503, "日志暂时无法读取。") from None

    @app.get("/api/jobs/{job_id}/logs/{stream}", response_class=FileResponse)
    async def download_job_log(job_id: str, stream: LogStream, account: SignedIn):
        return log_download(job_or_404(account, job_id), stream)

    @app.get("/api/conversations/{conversation_id}/jobs/{job_id}/logs/{stream}", response_class=FileResponse)
    async def download_log(conversation_id: str, job_id: str, stream: LogStream, account: SignedIn):
        conversation_or_404(account, conversation_id)
        return log_download(find_job(app.state.store, conversation_id, job_id), stream)

    def log_download(job: dict | None, stream: LogStream) -> FileResponse:
        if not job or job["state"] not in TERMINAL:
            raise HTTPException(404, "任务日志暂不可下载。")
        path = app.state.jobs.log_path(job["id"], stream)
        if not path.is_file():
            raise HTTPException(404, "此任务没有保存该路日志。")
        return FileResponse(path, filename=f"{job['id']}-{stream}.log", media_type="text/plain")

    @app.post("/api/conversations/{conversation_id}/jobs/{job_id}/retry-summary", status_code=202)
    async def retry_summary(conversation_id: str, job_id: str, account: SignedIn):
        conversation_or_404(account, conversation_id)
        job = find_job(app.state.store, conversation_id, job_id)
        if not job:
            raise HTTPException(404, "任务不存在。")
        if not app.state.settings.api_key:
            raise HTTPException(503, "请先在 .env 中配置 DeepSeek API Key 并重启后端。")
        pending = app.state.store.change_summary(job["id"], "error", "pending")
        if not pending:
            raise HTTPException(409, "只有解释异常的任务可以重试解释。")
        app.state.events.send(account.id, "job.updated", pending)
        app.state.agent.complete(pending)
        return pending

    @app.get("/api/tools")
    async def tool_catalog(account: SignedIn):
        return [tool["function"] for tool in app.state.tools.model_schemas()]

    dist = frontend_dir or ROOT / "frontend" / "dist"
    if (dist / "assets").is_dir():
        app.mount("/assets", StaticFiles(directory=dist / "assets"), name="assets")

    @app.get("/", include_in_schema=False, response_class=FileResponse)
    async def index():
        if not (dist / "index.html").is_file():
            raise HTTPException(503, "请先按 README 构建前端，或通过 Vite 的地址打开应用。")
        return FileResponse(dist / "index.html", headers={"Cache-Control": "no-cache"})

    return app


def validated_filename(filename: str) -> str:
    name = unicodedata.normalize("NFC", filename.strip())
    if (not name or len(name) > 120 or not name.isprintable() or any(char in name for char in '/\\:<>|"?*')
            or PurePath(name).suffix.lower() not in SUFFIXES):
        raise HTTPException(422, "请使用长度不超过 120 字符的 .fasta、.fa 或 .fna 文件名。")
    return name


app = create_app()
