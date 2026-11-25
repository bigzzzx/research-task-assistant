"""Discover MCP tools and expose the enabled catalog to the Agent."""

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
import json
import logging
import sys

from jsonschema import SchemaError, ValidationError
from jsonschema.validators import validator_for
from mcp.client import Client
from mcp.client.stdio import StdioServerParameters, stdio_client
from mcp.types import RequestParamsMeta, Tool
from referencing import Registry
from referencing.exceptions import Unresolvable

from .config import ANALYSES, LOG_LIMIT, ROOT, Settings
from .context import clip_text
from .extensions import ExtensionStore, RESEARCH_SERVER_NAME
from .external_mcp import allowed_connection, call_external_tool, result_text
from .tool_bridge import SCOPE_KEY, ToolScope, open_tool_bridge
from .tools import DEFINITIONS, Tools, argument_error, tool_error

logger = logging.getLogger(__name__)


def research_command() -> list[str]:
    return [sys.executable, "-E", "-X", "utf8", "-m", "backend.app.research_mcp"]


class MCPToolClient:
    def __init__(
        self, client: Client, catalog: list[Tool], tools: Tools,
        extensions: ExtensionStore, settings: Settings,
    ):
        self._client = client
        self._tools = {tool.name: tool for tool in catalog}
        self._store = tools.store
        self.extensions = extensions
        self.settings = settings

    @property
    def research_tool_count(self) -> int:
        return len(self._tools)

    def _external_available(self, row: dict) -> bool:
        return bool(row["available"] and allowed_connection(row["transport"], row["endpoint"], self.settings))

    def _research_parameters(self, owner_id: str, tool: Tool) -> dict:
        parameters = {**tool.input_schema, "additionalProperties": False}
        if tool.name == "submit_job":
            properties = parameters["properties"]
            parameters["properties"] = {
                **properties, "analysis_type": {
                    **properties["analysis_type"],
                    "enum": [*ANALYSES, *self._store.registered_analysis_types(owner_id)],
                },
            }
        return parameters

    def catalog(self, owner_id: str) -> list[dict]:
        disabled = self.extensions.disabled_research_tools(owner_id)
        entries = [{
            "name": name,
            "display_name": name,
            "description": tool.description or "",
            "parameters": self._research_parameters(owner_id, tool),
            "server_name": RESEARCH_SERVER_NAME,
            "transport": "stdio",
            "server_id": None,
            "required": False,
            "enabled": name not in disabled,
            "available": True,
            "server_enabled": True,
        } for name, tool in self._tools.items()]
        for row in self.extensions.external_tools(owner_id):
            entries.append({
                "name": row["function_name"],
                "display_name": row["name"],
                "description": row["description"],
                "parameters": row["input_schema"],
                "server_name": row["server_name"],
                "server_id": row["server_id"],
                "transport": row["transport"],
                "required": False,
                "enabled": bool(row["enabled"]),
                "available": self._external_available(row),
                "server_enabled": bool(row["server_enabled"]),
            })
        return entries

    def set_tool_enabled(self, owner_id: str, name: str, enabled: bool) -> bool:
        if name in self._tools:
            self.extensions.set_research_tool_enabled(owner_id, name, enabled)
            return True
        return self.extensions.set_tool_enabled(owner_id, name, enabled)

    def model_schemas(self, owner_id: str, names: tuple[str, ...] | None = None) -> list[dict]:
        return [
            {"type": "function", "function": {
                "name": tool["name"],
                "description": tool["description"],
                "parameters": tool["parameters"],
            }}
            for tool in self.catalog(owner_id)
            if tool["available"] and tool["enabled"] and tool["server_enabled"]
            and (names is None or tool["name"] in names)
        ]

    async def execute(
        self, owner_id: str, conversation_id: str, turn_id: int, name: str, raw: str,
        *, only_job: str | None = None,
    ) -> dict:
        if not self._store.owned_conversation(owner_id, conversation_id):
            return tool_error("CONVERSATION_NOT_FOUND", "当前账号找不到此会话。")
        try:
            arguments = json.loads(raw)
        except (ValueError, RecursionError):
            arguments = None
        if not isinstance(arguments, dict):
            return tool_error("INVALID_ARGUMENTS", "工具参数不是有效的 JSON 对象。")

        if name in self._tools:
            if name in self.extensions.disabled_research_tools(owner_id):
                return tool_error("TOOL_DISABLED", f"{name} 已停用，可在工具列表中重新启用。")
            scope = ToolScope(owner_id=owner_id, conversation_id=conversation_id, turn_id=turn_id, only_job=only_job)
            return await self._execute_research(name, arguments, scope)
        if not only_job:
            matches = self.extensions.external_tools(owner_id, name)
            if matches:
                tool = matches[0]
                if tool["enabled"] and tool["server_enabled"] and self._external_available(tool):
                    return await self._execute_external(tool, arguments)
        return tool_error("UNKNOWN_TOOL", "当前账号暂不可调用这个工具。")

    async def _execute_research(self, name: str, arguments: dict, scope: ToolScope) -> dict:
        tool = self._tools[name]
        if error := argument_error(arguments, tool.input_schema.get("required", [])):
            return error
        metadata = RequestParamsMeta(**{SCOPE_KEY: scope.model_dump()})
        try:
            response = await self._client.call_tool(name, arguments, meta=metadata)
            outcome = response.structured_content
            if isinstance(outcome, dict) and outcome.get("ok") is (not response.is_error):
                return outcome
            logger.warning("Research MCP tool %s returned an unexpected result", name)
        except Exception as error:
            logger.warning("Research MCP tool %s failed: %s", name, type(error).__name__)
        return tool_error("MCP_TOOL_UNAVAILABLE", "暂时无法取得本机 MCP 工具结果，请稍后重试。")

    async def _execute_external(self, row: dict, arguments: dict) -> dict:
        schema = row["input_schema"]
        try:
            validator = validator_for(schema)
            validator.check_schema(schema)
            validator(schema, registry=Registry()).validate(arguments)
        except ValidationError:
            return tool_error("INVALID_ARGUMENTS", "工具参数与该 MCP 工具的输入定义不匹配。")
        except (SchemaError, Unresolvable, RecursionError):
            return tool_error("INVALID_TOOL_SCHEMA", "该 MCP 工具的参数定义无法验证。")

        try:
            result = await call_external_tool(row["transport"], row["endpoint"], self.settings, row["name"], arguments)
            output = result_text(result)
            clipped = clip_text(output, LOG_LIMIT)
            return {
                "ok": not result.is_error,
                "server_name": row["server_name"],
                "tool": row["name"],
                "output": clipped,
                "output_truncated": output != clipped,
            }
        except Exception as error:
            logger.warning("External MCP tool %s failed: %s", row["function_name"], type(error).__name__)
            return tool_error("MCP_TOOL_UNAVAILABLE", "外部 MCP 工具暂时无法调用。")


@asynccontextmanager
async def open_tool_client(
    tools: Tools, extensions: ExtensionStore, settings: Settings,
) -> AsyncIterator[MCPToolClient]:
    async with open_tool_bridge(tools) as environment:
        command, *arguments = research_command()
        parameters = StdioServerParameters(command=command, args=arguments, cwd=ROOT, env=environment)
        async with Client(stdio_client(parameters), read_timeout_seconds=12) as client:
            discovered = await client.list_tools()
            if not DEFINITIONS.keys() <= {tool.name for tool in discovered.tools}:
                raise ValueError("The research server did not list the required tools.")
            yield MCPToolClient(client, discovered.tools, tools, extensions, settings)
