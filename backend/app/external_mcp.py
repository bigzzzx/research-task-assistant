"""Connect to external MCP services configured by workspace accounts."""

import asyncio
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from ipaddress import IPv6Address, ip_address
import json
import socket
from typing import Literal

from httpx2 import URL, AsyncClient, AsyncHTTPTransport, InvalidURL, Request, Response
from mcp.client import Client
from mcp.client.sse import sse_client
from mcp.client.stdio import stdio_client
from mcp.client.streamable_http import streamable_http_client
from mcp.types import CallToolResult, Tool

from .config import Settings


Transport = Literal["streamable-http", "sse", "stdio"]


class EndpointNotAllowed(ValueError):
    pass


def http_url(endpoint: str) -> URL | None:
    try:
        url = URL(endpoint)
    except (InvalidURL, ValueError):
        return None
    if url.scheme in {"https", "http"} and url.host and not url.userinfo and not url.fragment:
        return url
    return None


def allowed_connection(transport: Transport, endpoint: str, settings: Settings) -> bool:
    if transport == "stdio":
        return endpoint in settings.mcp_stdio_servers
    url = http_url(endpoint)
    return url is not None and (url.scheme == "https" or endpoint in settings.mcp_http_endpoints)


async def public_address(url: URL) -> str:
    targets = await asyncio.get_running_loop().getaddrinfo(
        url.raw_host.decode("ascii"), url.port or 443, type=socket.SOCK_STREAM,
    )
    addresses = [ip_address(target[4][0]) for target in targets]
    for address in sorted(addresses, key=lambda item: item.version):
        if isinstance(address, IPv6Address) and (address.ipv4_mapped or address.sixtofour or address.teredo):
            continue
        if address.is_global and not (address.is_multicast or address.is_reserved):
            return str(address)
    raise EndpointNotAllowed("内网或本机 MCP 地址需先在后端登记。")


class EndpointTransport(AsyncHTTPTransport):
    def __init__(self, endpoint: URL, address: str | None):
        super().__init__(trust_env=False)
        self.endpoint = endpoint
        self.address = address

    async def handle_async_request(self, request: Request) -> Response:
        if (request.url.scheme, request.url.netloc) != (self.endpoint.scheme, self.endpoint.netloc):
            raise EndpointNotAllowed("MCP 请求转向了其他地址。")
        if self.address is not None:
            # Connect to the verified IP without resolving the hostname a second time.
            # Preserve the original Host header and TLS certificate verification.
            request = Request(
                request.method, request.url.copy_with(host=self.address), headers=request.headers,
                stream=request.stream, extensions={**request.extensions, "sni_hostname": self.endpoint.host},
            )
        return await super().handle_async_request(request)


@asynccontextmanager
async def open_external_client(transport: Transport, endpoint: str, settings: Settings) -> AsyncIterator[Client]:
    if transport == "stdio":
        parameters = settings.mcp_stdio_servers.get(endpoint)
        if parameters is None:
            raise EndpointNotAllowed("请先在后端登记这个本机 MCP 服务。")
        async with Client(stdio_client(parameters), read_timeout_seconds=12) as client:
            yield client
        return

    url = http_url(endpoint)
    trusted = endpoint in settings.mcp_http_endpoints
    if url is None or (url.scheme != "https" and not trusted):
        raise EndpointNotAllowed("请填写公开 HTTPS 地址，或使用后端登记的地址。")
    address = None if trusted else await public_address(url)

    def http_client(**kwargs) -> AsyncClient:
        return AsyncClient(transport=EndpointTransport(url, address), trust_env=False, **kwargs)

    if transport == "sse":
        connection = sse_client(endpoint, timeout=12, sse_read_timeout=12, httpx_client_factory=http_client)
        async with Client(connection, read_timeout_seconds=12) as client:
            yield client
    else:
        async with http_client(timeout=12) as http:
            connection = streamable_http_client(endpoint, http_client=http)
            async with Client(connection, read_timeout_seconds=12) as client:
                yield client


async def discover_tools(transport: Transport, endpoint: str, settings: Settings) -> list[Tool]:
    tools: dict[str, Tool] = {}
    cursor = None
    seen_cursors = set()
    async with asyncio.timeout(18), open_external_client(transport, endpoint, settings) as client:
        while True:
            page = await client.list_tools(cursor=cursor)
            tools.update((tool.name, tool) for tool in page.tools)
            cursor = page.next_cursor
            if cursor is None:
                return list(tools.values())
            if cursor in seen_cursors:
                raise ValueError("MCP returned a repeated tools cursor.")
            seen_cursors.add(cursor)


async def call_external_tool(
    transport: Transport, endpoint: str, settings: Settings, name: str, arguments: dict,
) -> CallToolResult:
    async with asyncio.timeout(20), open_external_client(transport, endpoint, settings) as client:
        return await client.call_tool(name, arguments)


def result_text(result: CallToolResult) -> str:
    data = result.structured_content
    if data is None:
        data = [
            block.text if block.type == "text" else {"type": block.type}
            for block in result.content
        ]
    return json.dumps(data, ensure_ascii=False, separators=(",", ":"))
