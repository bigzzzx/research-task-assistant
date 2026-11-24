"""The local research MCP server. Its protocol runs on stdin and stdout."""

import json
from typing import Annotated

from mcp.server.mcpserver import Context, MCPServer
from mcp.types import CallToolResult, TextContent
from pydantic import Field

from .tool_bridge import SCOPE_KEY, ToolScope, execute_in_app
from .tools import DEFINITIONS, JOB_PARAM, tool_error

AnalysisType = Annotated[str, Field(json_schema_extra=DEFINITIONS["submit_job"][1]["analysis_type"])]
FastaFilename = Annotated[str, Field(json_schema_extra=DEFINITIONS["submit_job"][1]["filename"])]
JobId = Annotated[str, Field(json_schema_extra=JOB_PARAM["job_id"])]


async def execute_scoped_tool(ctx: Context, name: str, arguments: dict) -> CallToolResult:
    try:
        scope = ToolScope.model_validate((ctx.request_context.meta or {}).get(SCOPE_KEY))
    except ValueError:
        outcome = tool_error("MISSING_TOOL_SCOPE", "工具调用缺少有效的工作区作用域。")
    else:
        outcome = await execute_in_app(scope, name, arguments)
    return CallToolResult(
        content=[TextContent(type="text", text=json.dumps(outcome, ensure_ascii=False))],
        structured_content=outcome,
        is_error=not outcome["ok"],
    )


async def submit_job(analysis_type: AnalysisType, filename: FastaFilename, ctx: Context) -> CallToolResult:
    return await execute_scoped_tool(ctx, "submit_job", {"analysis_type": analysis_type, "filename": filename})


async def job_status(job_id: JobId, ctx: Context) -> CallToolResult:
    return await execute_scoped_tool(ctx, "job_status", {"job_id": job_id})


async def read_log(job_id: JobId, ctx: Context) -> CallToolResult:
    return await execute_scoped_tool(ctx, "read_log", {"job_id": job_id})


def create_tool_server() -> MCPServer:
    server = MCPServer("research-calculation-tools", log_level="WARNING")
    server.add_tool(submit_job, description=DEFINITIONS["submit_job"][0], structured_output=False)
    server.add_tool(job_status, description=DEFINITIONS["job_status"][0], structured_output=False)
    server.add_tool(read_log, description=DEFINITIONS["read_log"][0], structured_output=False)
    return server


if __name__ == "__main__":
    create_tool_server().run(transport="stdio")
