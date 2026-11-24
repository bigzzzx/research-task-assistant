"""Serve the analysis tools through a private MCP Streamable HTTP transport."""

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
import json
import logging
from typing import Annotated

from httpx2 import ASGITransport, AsyncClient
from mcp.client import Client
from mcp.client.streamable_http import streamable_http_client
from mcp.server.mcpserver import Context, MCPServer
from mcp.types import CallToolResult, RequestParamsMeta, TextContent, Tool
from pydantic import BaseModel, ConfigDict, Field

from .tools import DEFINITIONS, JOB_PARAM, Tools, argument_error, tool_error

logger = logging.getLogger(__name__)
SCOPE_KEY = "research-assistant.local/tool-scope"
MCP_URL = "http://127.0.0.1:8000/mcp"
AnalysisType = Annotated[str, Field(json_schema_extra=DEFINITIONS["submit_job"][1]["analysis_type"])]
FastaFilename = Annotated[str, Field(json_schema_extra=DEFINITIONS["submit_job"][1]["filename"])]
JobId = Annotated[str, Field(json_schema_extra=JOB_PARAM["job_id"])]


class ToolScope(BaseModel):
    model_config = ConfigDict(strict=True, extra="forbid")

    owner_id: str
    conversation_id: str
    turn_id: int
    only_job: str | None = None


def create_tool_server(tools: Tools) -> MCPServer:
    def execute_scoped_tool(ctx: Context, name: str, arguments: dict) -> CallToolResult:
        try:
            scope = ToolScope.model_validate((ctx.request_context.meta or {}).get(SCOPE_KEY))
        except ValueError:
            outcome = tool_error("MISSING_TOOL_SCOPE", "工具调用缺少有效的工作区作用域。")
        else:
            outcome = tools.execute(
                scope.owner_id, scope.conversation_id, scope.turn_id, name, arguments, only_job=scope.only_job,
            )
        return CallToolResult(
            content=[TextContent(type="text", text=json.dumps(outcome, ensure_ascii=False))],
            structured_content=outcome,
            is_error=not outcome["ok"],
        )

    async def submit_job(analysis_type: AnalysisType, filename: FastaFilename, ctx: Context) -> CallToolResult:
        return execute_scoped_tool(ctx, "submit_job", {"analysis_type": analysis_type, "filename": filename})

    async def job_status(job_id: JobId, ctx: Context) -> CallToolResult:
        return execute_scoped_tool(ctx, "job_status", {"job_id": job_id})

    async def read_log(job_id: JobId, ctx: Context) -> CallToolResult:
        return execute_scoped_tool(ctx, "read_log", {"job_id": job_id})

    server = MCPServer("research-calculation-tools", log_level="WARNING")
    server.add_tool(submit_job, description=DEFINITIONS["submit_job"][0], structured_output=False)
    server.add_tool(job_status, description=DEFINITIONS["job_status"][0], structured_output=False)
    server.add_tool(read_log, description=DEFINITIONS["read_log"][0], structured_output=False)
    return server


class MCPToolClient:
    def __init__(self, client: Client, catalog: list[Tool]):
        self._client = client
        self._tools = {tool.name: tool for tool in catalog}

    def model_schemas(self, names: tuple[str, ...] | None = None) -> list[dict]:
        return [
            {"type": "function", "function": {
                "name": name,
                "description": self._tools[name].description,
                "parameters": {**self._tools[name].input_schema, "additionalProperties": False},
            }}
            for name in self._tools if names is None or name in names
        ]

    async def execute(
        self, owner_id: str, conversation_id: str, turn_id: int, name: str, raw: str,
        *, only_job: str | None = None,
    ) -> dict:
        tool = self._tools.get(name)
        if tool is None:
            return tool_error("UNKNOWN_TOOL", "这一轮无法使用这个工具。")
        try:
            arguments = json.loads(raw)
        except (ValueError, RecursionError):
            arguments = None
        if not isinstance(arguments, dict):
            return tool_error("INVALID_ARGUMENTS", "工具参数不是有效的 JSON 对象。")
        if error := argument_error(arguments, tool.input_schema["required"]):
            return error

        # Authenticated application context travels outside the model-supplied arguments.
        scope = ToolScope(owner_id=owner_id, conversation_id=conversation_id, turn_id=turn_id, only_job=only_job)
        metadata = RequestParamsMeta(**{SCOPE_KEY: scope.model_dump()})
        try:
            response = await self._client.call_tool(name, arguments, meta=metadata)
            outcome = response.structured_content
            succeeded = not response.is_error
            if isinstance(outcome, dict) and outcome.get("ok") is succeeded:
                return outcome
            logger.warning("MCP tool %s returned an unexpected result", name)
        except Exception as error:
            logger.warning("MCP tool %s failed: %s", name, type(error).__name__)
        return tool_error("MCP_TOOL_UNAVAILABLE", "暂时无法取得 MCP 工具结果，请稍后重试。")


@asynccontextmanager
async def open_tool_client(tools: Tools) -> AsyncIterator[MCPToolClient]:
    server = create_tool_server(tools)
    tool_app = server.streamable_http_app(stateless_http=True, json_response=True)
    async with server.session_manager.run():
        # Use HTTP framing with an ASGI transport inside the process and share the existing job queue.
        async with AsyncClient(transport=ASGITransport(app=tool_app), trust_env=False) as http_client:
            transport = streamable_http_client(MCP_URL, http_client=http_client)
            async with Client(transport, read_timeout_seconds=10) as client:
                discovered = await client.list_tools()
                yield MCPToolClient(client, discovered.tools)
