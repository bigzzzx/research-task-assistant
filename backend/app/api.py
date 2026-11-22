from contextlib import asynccontextmanager
from pathlib import Path, PurePath
import sqlite3
from typing import Literal
import unicodedata
from uuid import uuid4

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import FileResponse, JSONResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from httpx import AsyncBaseTransport
from pydantic import BaseModel, Field
from starlette.middleware.trustedhost import TrustedHostMiddleware

from .agent import Agent
from .config import ROOT, SAMPLES, UPLOAD_LIMIT, Settings
from .events import Events
from .jobs import Jobs, TERMINAL
from .model import DeepSeek
from .store import Store
from .tools import Tools

SUFFIXES = {".fa", ".fasta", ".fna"}


class SendMessage(BaseModel):
    client_message_id: str = Field(min_length=1, max_length=80)
    text: str = Field(min_length=1, max_length=4000)


def create_app(settings: Settings | None = None, *, model_transport: AsyncBaseTransport | None = None) -> FastAPI:
    @asynccontextmanager
    async def lifespan(app: FastAPI):
        resolved = settings or Settings.load()
        (resolved.data_dir / "uploads").mkdir(parents=True, exist_ok=True)
        store = Store(resolved.data_dir / "assistant.sqlite3", ROOT / "data")
        events = Events()
        model = DeepSeek(resolved, model_transport)
        agent = Agent(store, events, model)
        jobs = Jobs(resolved, store, events, lambda job: agent.complete(job, tools))
        tools = Tools(store, jobs)
        app.state.settings, app.state.events, app.state.store = resolved, events, store
        app.state.agent, app.state.jobs, app.state.tools = agent, jobs, tools
        try:
            for pending in store.recover():
                agent.complete(pending, tools)
            jobs.start()
            yield
        finally:
            await jobs.close()
            await agent.close()
            events.close()
            await model.close()
            store.close()

    app = FastAPI(title="科研计算任务助手", lifespan=lifespan)
    app.add_middleware(TrustedHostMiddleware, allowed_hosts=["localhost", "127.0.0.1"])

    @app.middleware("http")
    async def require_local_origin(request: Request, call_next):
        origin = request.headers.get("origin")
        if request.method == "POST" and origin and origin != f"http://{request.headers.get('host')}":
            return JSONResponse({"detail": "请从同一个本机应用地址提交请求。"}, status_code=403)
        return await call_next(request)

    def conversation_or_404(conversation_id: str) -> dict:
        conversation = app.state.store.conversation(conversation_id)
        if not conversation:
            raise HTTPException(404, "会话不存在。")
        return conversation

    @app.get("/api/config")
    async def config():
        resolved = app.state.settings
        return {"model": resolved.model, "key_configured": bool(resolved.api_key),
                "samples": list(SAMPLES), "upload_limit": UPLOAD_LIMIT}

    @app.get("/api/conversations")
    async def conversations():
        return app.state.store.conversations()

    @app.post("/api/conversations", status_code=201)
    async def new_conversation():
        return app.state.store.new_conversation()

    @app.get("/api/conversations/{conversation_id}")
    async def conversation(conversation_id: str):
        conversation_or_404(conversation_id)
        return app.state.store.snapshot(conversation_id)

    @app.post("/api/conversations/{conversation_id}/messages", status_code=202)
    async def send_message(conversation_id: str, body: SendMessage):
        conversation_or_404(conversation_id)
        if not app.state.settings.api_key:
            raise HTTPException(503, "请在 .env 中填写 DEEPSEEK_API_KEY 并重启后端。")
        text = body.text.strip()
        if not text:
            raise HTTPException(422, "请输入要发送的消息。")
        message, created = app.state.store.user_message(conversation_id, body.client_message_id, text)
        if created:
            app.state.events.send("message.updated", message)
            app.state.agent.chat(message, app.state.tools)
        return message

    @app.get("/api/events")
    async def events():
        return StreamingResponse(
            app.state.events.listen(),
            media_type="text/event-stream",
            headers={"Cache-Control": "no-cache, no-transform", "X-Accel-Buffering": "no"},
        )

    @app.post("/api/conversations/{conversation_id}/files", status_code=201)
    async def upload(conversation_id: str, filename: str, request: Request):
        conversation_or_404(conversation_id)
        name = validated_filename(filename)
        store = app.state.store
        if any(row["name_key"] == name.casefold() for row in store.files(conversation_id)):
            raise HTTPException(409, "已有同名文件，请改名后再上传。")

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
            row = store.add_file(conversation_id, storage_id, name, size)
            registered = True
        except sqlite3.IntegrityError:
            raise HTTPException(409, "已有同名文件，请改名后再上传。") from None
        finally:
            if created and not registered:
                target.unlink(missing_ok=True)
        public = store.public_file(row)
        app.state.events.send("file.added", {"conversation_id": conversation_id, "file": public})
        return public

    @app.get("/api/conversations/{conversation_id}/jobs/{job_id}/logs/{stream}", response_class=FileResponse)
    async def download_log(conversation_id: str, job_id: str, stream: Literal["stdout", "stderr"]):
        job = app.state.tools.find_job(conversation_id, job_id)
        if not job or job["state"] not in TERMINAL:
            raise HTTPException(404, "任务日志暂不可下载。")
        path = app.state.jobs.log_path(job["id"], stream)
        if not path.is_file():
            raise HTTPException(404, "此任务没有保存该路日志。")
        return FileResponse(path, filename=f"{job['id'][:8]}-{stream}.log", media_type="text/plain")

    @app.post("/api/conversations/{conversation_id}/jobs/{job_id}/retry-summary", status_code=202)
    async def retry_summary(conversation_id: str, job_id: str):
        job = app.state.tools.find_job(conversation_id, job_id)
        if not job:
            raise HTTPException(404, "任务不存在。")
        if not app.state.settings.api_key:
            raise HTTPException(503, "请先在 .env 中配置 DeepSeek API Key 并重启后端。")
        pending = app.state.store.change_summary(job["id"], "error", "pending")
        if not pending:
            raise HTTPException(409, "只有解释异常的任务可以重试解释。")
        app.state.events.send("job.updated", pending)
        app.state.agent.complete(pending, app.state.tools)
        return pending

    dist = ROOT / "frontend" / "dist"
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
