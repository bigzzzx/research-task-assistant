"""Workspace management endpoints for MCP and text Skills."""

from sqlite3 import IntegrityError
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel, ConfigDict, Field, StrictBool

from .auth import Account, current_account
from .external_mcp import allowed_endpoint, discover_tools
from .mcp_tools import research_command

router = APIRouter(prefix="/api")
SignedIn = Annotated[Account, Depends(current_account)]


class ServerInput(BaseModel):
    model_config = ConfigDict(str_strip_whitespace=True, extra="forbid")

    name: str = Field(min_length=1, max_length=80)
    endpoint: str = Field(min_length=1, max_length=512)


class EnabledInput(BaseModel):
    enabled: StrictBool


class SkillInput(BaseModel):
    model_config = ConfigDict(str_strip_whitespace=True, extra="forbid")

    name: str = Field(min_length=1, max_length=80)
    trigger: str = Field(min_length=2, max_length=120)
    instructions: str = Field(min_length=1, max_length=1600)
    enabled: StrictBool = True


@router.get("/mcp/servers")
async def mcp_servers(request: Request, account: SignedIn):
    extensions = request.app.state.extensions
    endpoints = request.app.state.settings.mcp_http_endpoints
    return {
        "research_added": extensions.has_research_server(account.id),
        "research_command": research_command(),
        "endpoints": [endpoint for endpoint in endpoints if allowed_endpoint(endpoint, endpoints)],
        "servers": [
            {**server, "available": allowed_endpoint(server["endpoint"], endpoints)}
            for server in extensions.servers(account.id)
        ],
    }


@router.post("/mcp/research", status_code=201)
async def add_research_server(request: Request, account: SignedIn):
    try:
        names = await request.app.state.tools.refresh_research_tools()
    except Exception:
        raise HTTPException(502, "暂时无法发现本机 stdio 服务的工具。") from None
    request.app.state.extensions.add_research_server(account.id)
    request.app.state.events.send(account.id, "config.updated", {})
    return {"tools": names}


async def discover(request: Request, endpoint: str):
    if not allowed_endpoint(endpoint, request.app.state.settings.mcp_http_endpoints):
        raise HTTPException(422, "请从本机配置的可信 MCP 地址中选择。")
    try:
        return await discover_tools(endpoint)
    except Exception:
        raise HTTPException(502, "无法连接 Streamable HTTP 服务或读取工具清单。") from None


@router.post("/mcp/servers", status_code=201)
async def add_server(request: Request, body: ServerInput, account: SignedIn):
    extensions = request.app.state.extensions
    tools = await discover(request, body.endpoint)
    try:
        server_id = extensions.add_server(account.id, body.name, body.endpoint, tools)
    except IntegrityError:
        raise HTTPException(409, "当前账号已经添加了这个地址。") from None
    return {"id": server_id, "tool_count": len(tools)}


@router.post("/mcp/servers/{server_id}/refresh")
async def refresh_server(request: Request, server_id: str, account: SignedIn):
    extensions = request.app.state.extensions
    server = extensions.server(account.id, server_id)
    if not server:
        raise HTTPException(404, "找不到此 MCP 服务。")
    tools = await discover(request, server["endpoint"])
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
    if not request.app.state.extensions.set_tool_enabled(account.id, name, body.enabled):
        raise HTTPException(404, "找不到可配置的 MCP 工具。")
    return {"enabled": body.enabled}


@router.get("/skills")
async def list_skills(request: Request, account: SignedIn):
    return request.app.state.extensions.skills(account.id)


@router.post("/skills", status_code=201)
@router.post("/skills/{skill_id}")
async def save_skill(request: Request, body: SkillInput, account: SignedIn, skill_id: str | None = None):
    try:
        saved = request.app.state.extensions.save_skill(account.id, **body.model_dump(), skill_id=skill_id)
    except IntegrityError:
        raise HTTPException(409, "当前账号已使用这个触发短语。") from None
    if saved is None:
        raise HTTPException(404, "找不到此 Skill。")
    return {"id": saved}
