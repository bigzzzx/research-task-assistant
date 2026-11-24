"""Private IPC from the stdio server to the application's task manager."""

import asyncio
from collections.abc import AsyncIterator
from contextlib import ExitStack, asynccontextmanager
import json
import logging
from multiprocessing import AuthenticationError
from multiprocessing.connection import Client, Connection, Listener
import os
from pathlib import Path
from secrets import token_bytes
from tempfile import TemporaryDirectory
from uuid import uuid4

from pydantic import BaseModel, ConfigDict

from .tools import Tools, tool_error

ADDRESS_ENV = "RESEARCH_MCP_PIPE"
TOKEN_ENV = "RESEARCH_MCP_TOKEN"
SCOPE_KEY = "research-assistant.local/tool-scope"
MAX_FRAME_BYTES = 64 * 1024
logger = logging.getLogger(__name__)


class ToolScope(BaseModel):
    model_config = ConfigDict(strict=True, extra="forbid")

    owner_id: str
    conversation_id: str
    turn_id: int
    only_job: str | None = None


def receive(connection: Connection) -> bytes:
    if not connection.poll(8):
        raise TimeoutError("Timed out waiting for the local tool bridge.")
    return connection.recv_bytes(MAX_FRAME_BYTES)


def exchange(address: str, token: bytes, frame: bytes) -> dict | None:
    with Client(address, authkey=token) as connection:
        connection.send_bytes(frame)
        return json.loads(receive(connection)) if frame else None


async def execute_in_app(scope: ToolScope, name: str, arguments: dict) -> dict:
    frame = json.dumps(
        {"scope": scope.model_dump(), "name": name, "arguments": arguments}, ensure_ascii=False,
    ).encode()
    try:
        return await asyncio.to_thread(
            exchange, os.environ[ADDRESS_ENV], bytes.fromhex(os.environ[TOKEN_ENV]), frame,
        )
    except (OSError, EOFError, AuthenticationError, KeyError, ValueError):
        return tool_error("LOCAL_TOOL_UNAVAILABLE", "暂时无法连接本机任务管理器。")


async def serve_bridge(listener: Listener, tools: Tools) -> None:
    while True:
        try:
            with await asyncio.to_thread(listener.accept) as connection:
                frame = await asyncio.to_thread(receive, connection)
                if not frame:
                    return
                request = json.loads(frame)
                scope = ToolScope.model_validate(request["scope"])
                try:
                    result = tools.execute(
                        scope.owner_id, scope.conversation_id, scope.turn_id, request["name"], request["arguments"],
                        only_job=scope.only_job,
                    )
                except Exception:
                    logger.exception("Local task tool failed")
                    result = tool_error("LOCAL_TOOL_FAILED", "执行本机工具时发生错误。")
                await asyncio.to_thread(connection.send_bytes, json.dumps(result, ensure_ascii=False).encode())
        except (OSError, EOFError, AuthenticationError, KeyError, TypeError, ValueError) as error:
            logger.debug("Local tool bridge request ended: %s", type(error).__name__)


@asynccontextmanager
async def open_tool_bridge(tools: Tools) -> AsyncIterator[dict[str, str]]:
    with ExitStack() as cleanup:
        if os.name == "nt":
            address = r"\\.\pipe\research-mcp-" + uuid4().hex
            family = "AF_PIPE"
        else:
            directory = cleanup.enter_context(TemporaryDirectory(prefix="research-mcp-"))
            address = str(Path(directory) / "tools.sock")
            family = "AF_UNIX"
        token = token_bytes(32)
        listener = cleanup.enter_context(Listener(address, family=family, authkey=token))
        worker = asyncio.create_task(serve_bridge(listener, tools))
        try:
            yield {ADDRESS_ENV: address, TOKEN_ENV: token.hex()}
        finally:
            if not worker.done():
                await asyncio.to_thread(exchange, address, token, b"")
            await worker
