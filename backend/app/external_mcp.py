"""Discovery and calls for operator-approved Streamable HTTP endpoints."""

import asyncio
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
import json
from urllib.parse import urlsplit

from httpx2 import AsyncClient
from mcp.client import Client
from mcp.client.streamable_http import streamable_http_client
from mcp.types import CallToolResult, Tool


def allowed_endpoint(endpoint: str, endpoints: tuple[str, ...]) -> bool:
    if endpoint not in endpoints:
        return False
    try:
        url = urlsplit(endpoint)
        return (
            url.scheme in {"https", "http"} and bool(url.hostname)
            and url.username is None and url.password is None and not url.fragment
        )
    except ValueError:
        return False


@asynccontextmanager
async def open_external_client(endpoint: str) -> AsyncIterator[Client]:
    async with AsyncClient(trust_env=False, timeout=12) as http:
        transport = streamable_http_client(endpoint, http_client=http)
        async with Client(transport, read_timeout_seconds=12) as client:
            yield client


async def discover_tools(endpoint: str) -> list[Tool]:
    tools: dict[str, Tool] = {}
    cursor = None
    seen_cursors = set()
    async with asyncio.timeout(18), open_external_client(endpoint) as client:
        while True:
            page = await client.list_tools(cursor=cursor)
            tools.update((tool.name, tool) for tool in page.tools)
            cursor = page.next_cursor
            if cursor is None:
                return list(tools.values())
            if cursor in seen_cursors:
                raise ValueError("MCP returned a repeated tools cursor.")
            seen_cursors.add(cursor)


async def call_external_tool(endpoint: str, name: str, arguments: dict) -> CallToolResult:
    async with asyncio.timeout(20), open_external_client(endpoint) as client:
        return await client.call_tool(name, arguments)


def result_text(result: CallToolResult) -> str:
    data = result.structured_content
    if data is None:
        data = [
            block.text if block.type == "text" else {"type": block.type}
            for block in result.content
        ]
    return json.dumps(data, ensure_ascii=False, separators=(",", ":"))
