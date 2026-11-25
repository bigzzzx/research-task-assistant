"""Workspace management endpoints for MCP and text Skills."""

from base64 import b64decode
from io import BytesIO
from sqlite3 import IntegrityError
from typing import Annotated
from zipfile import ZIP_DEFLATED, ZipFile

from fastapi import APIRouter, Depends, HTTPException, Request, Response
from pydantic import BaseModel, ConfigDict, Field, StrictBool, ValidationError

from .auth import Account, current_account
from .extensions import RESEARCH_SERVER_NAME
from .external_mcp import EndpointNotAllowed, Transport, allowed_connection, discover_tools
from .skills import MAX_BUNDLE, MAX_DOCUMENT, MAX_FILES, SKILL_FILE, bundle_files, parse_skill

router = APIRouter(prefix="/api")
SignedIn = Annotated[Account, Depends(current_account)]


class ServerInput(BaseModel):
    model_config = ConfigDict(str_strip_whitespace=True, extra="forbid")

    name: str = Field(min_length=1, max_length=80)
    endpoint: str = Field(min_length=1, max_length=512)
    transport: Transport = "streamable-http"


class EnabledInput(BaseModel):
    enabled: StrictBool


class SkillInput(BaseModel):
    model_config = ConfigDict(extra="forbid")

    document: str = Field(min_length=1, max_length=MAX_DOCUMENT)
    enabled: StrictBool = True


class SkillImport(SkillInput):
    folder: str = Field(min_length=1, max_length=64)
    files: dict[str, str] = Field(max_length=MAX_FILES - 1)


def reload_stdio_settings(request: Request) -> str | None:
    tools = request.app.state.tools
    try:
        tools.settings = tools.settings.reload_stdio_servers()
    except (OSError, ValueError):
        return "本机 MCP 配置读取失败，请检查 .env 中的 MCP_STDIO_SERVERS。"
    return None


@router.get("/mcp/servers")
async def mcp_servers(request: Request, account: SignedIn):
    extensions = request.app.state.extensions
    tools = request.app.state.tools
    stdio_config_error = reload_stdio_settings(request)
    settings = tools.settings
    return {
        "research": {"name": RESEARCH_SERVER_NAME, "tool_count": tools.research_tool_count},
        "stdio_servers": sorted(settings.mcp_stdio_servers),
        "stdio_config_error": stdio_config_error,
        "trusted_endpoints": list(settings.mcp_http_endpoints),
        "servers": [
            {**server, "available": allowed_connection(server["transport"], server["endpoint"], settings)}
            for server in extensions.servers(account.id)
        ],
    }


async def discover(request: Request, transport: Transport, endpoint: str):
    if transport == "stdio" and (error := reload_stdio_settings(request)):
        raise HTTPException(422, error)
    try:
        return await discover_tools(transport, endpoint, request.app.state.tools.settings)
    except EndpointNotAllowed as error:
        raise HTTPException(422, str(error)) from None
    except Exception:
        raise HTTPException(502, "无法连接 MCP 服务。请核对传输方式、地址或本机命令。") from None


@router.post("/mcp/servers", status_code=201)
async def add_server(request: Request, body: ServerInput, account: SignedIn):
    extensions = request.app.state.extensions
    tools = await discover(request, body.transport, body.endpoint)
    try:
        server_id = extensions.add_server(account.id, body.name, body.endpoint, body.transport, tools)
    except IntegrityError:
        raise HTTPException(409, "当前账号已经添加了这个 MCP 服务。") from None
    return {"id": server_id, "tool_count": len(tools)}


@router.post("/mcp/servers/{server_id}/refresh")
async def refresh_server(request: Request, server_id: str, account: SignedIn):
    extensions = request.app.state.extensions
    server = extensions.server(account.id, server_id)
    if not server:
        raise HTTPException(404, "找不到此 MCP 服务。")
    tools = await discover(request, server["transport"], server["endpoint"])
    if not extensions.refresh_server(account.id, server_id, tools):
        raise HTTPException(404, "找不到此 MCP 服务。")
    return {"tool_count": len(tools)}


@router.post("/mcp/servers/{server_id}/enabled")
async def enable_server(request: Request, server_id: str, body: EnabledInput, account: SignedIn):
    if not request.app.state.extensions.set_server_enabled(account.id, server_id, body.enabled):
        raise HTTPException(404, "找不到此 MCP 服务。")
    return {"enabled": body.enabled}


@router.post("/mcp/tools/{name}/enabled")
async def enable_tool(request: Request, name: str, body: EnabledInput, account: SignedIn):
    if not request.app.state.tools.set_tool_enabled(account.id, name, body.enabled):
        raise HTTPException(404, "找不到可配置的 MCP 工具。")
    return {"enabled": body.enabled}


@router.get("/skills")
async def list_skills(request: Request, account: SignedIn):
    return request.app.state.extensions.skills(account.id)


@router.post("/skills", status_code=201)
@router.post("/skills/{skill_id}")
async def save_skill(request: Request, body: SkillInput, account: SignedIn, skill_id: str | None = None):
    return persist_skill(request, account, body, skill_id=skill_id)


@router.get("/skills/{skill_id}")
async def get_skill(request: Request, skill_id: str, account: SignedIn):
    skill = request.app.state.extensions.skill(account.id, skill_id=skill_id)
    if skill is None:
        raise HTTPException(404, "找不到此 Skill。")
    return {**skill, "files": sorted(skill["files"])}


@router.post("/skills/{skill_id}/enabled")
async def enable_skill(request: Request, skill_id: str, body: EnabledInput, account: SignedIn):
    if not request.app.state.extensions.set_skill_enabled(account.id, skill_id, body.enabled):
        raise HTTPException(404, "找不到此 Skill。")
    return {"enabled": body.enabled}


@router.post("/skill-bundles", status_code=201)
async def import_skill(request: Request, account: SignedIn):
    raw = bytearray()
    async for chunk in request.stream():
        if len(raw) + len(chunk) > 4 * MAX_BUNDLE // 3 + 12 * MAX_DOCUMENT:
            raise HTTPException(413, "技能目录超过上传大小限制。")
        raw.extend(chunk)
    try:
        body = SkillImport.model_validate_json(raw)
    except ValidationError:
        raise HTTPException(422, "请选择包含 SKILL.md 且文件总数不超过 64 个的技能目录。") from None
    return persist_skill(request, account, body, folder=body.folder, files=body.files)


@router.get("/skills/{skill_id}/bundle")
async def export_skill(request: Request, skill_id: str, account: SignedIn):
    skill = request.app.state.extensions.skill(account.id, skill_id=skill_id)
    if skill is None:
        raise HTTPException(404, "找不到此 Skill。")
    folder = skill["name"]
    archive = BytesIO()
    with ZipFile(archive, "w", ZIP_DEFLATED) as bundle:
        bundle.writestr(f"{folder}/{SKILL_FILE}", skill["document"].encode("utf-8"))
        for path, content in skill["files"].items():
            bundle.writestr(f"{folder}/{path}", b64decode(content))
    return Response(archive.getvalue(), media_type="application/zip", headers={
        "Content-Disposition": f'attachment; filename="{folder}.zip"',
        "Cache-Control": "no-store",
    })


def persist_skill(
    request: Request, account: Account, body: SkillInput,
    *, skill_id: str | None = None, folder: str | None = None, files: dict[str, str] | None = None,
):
    try:
        document = parse_skill(body.document)
        if folder is not None and folder != document.name:
            raise ValueError("目录名称需与 SKILL.md 的 name 一致。")
        saved = request.app.state.extensions.save_skill(
            account.id, document, body.enabled, skill_id=skill_id,
            files=bundle_files(files) if files is not None else None,
        )
    except ValueError as error:
        raise HTTPException(422, str(error)) from None
    except IntegrityError:
        raise HTTPException(409, "当前账号已使用这个技能标识。") from None
    if saved is None:
        raise HTTPException(404, "找不到此 Skill。")
    return {"id": saved}
