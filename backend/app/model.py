"""DeepSeek Chat Completions protocol. The agent owns the tool loop."""

import asyncio
from collections.abc import Callable
from dataclasses import dataclass, field
import json

import httpx

from .config import Settings


class ModelError(Exception):
    """An error safe to show in the local chat."""


@dataclass
class ToolCall:
    id: str = ""
    name: str = ""
    arguments: str = ""

    def protocol(self) -> dict:
        return {"id": self.id, "type": "function",
                "function": {"name": self.name, "arguments": self.arguments}}


@dataclass
class ModelReply:
    text: str = ""
    calls: list[ToolCall] = field(default_factory=list)


HTTP_ERRORS = {
    400: "DeepSeek 拒绝了请求，请核对模型 ID 和接口地址。",
    401: "DeepSeek 未通过鉴权。请检查 .env 中的 DEEPSEEK_API_KEY 并重启后端。",
    402: "DeepSeek 账户额度不足，请检查账户余额。",
    403: "当前凭证没有 DeepSeek 调用权限，请检查账户和 API Key。",
    404: "DeepSeek 接口或模型不存在，请检查 .env 中的地址和模型 ID。",
    429: "DeepSeek 请求过于频繁，请稍后重试。",
}


class DeepSeek:
    def __init__(self, settings: Settings, transport: httpx.AsyncBaseTransport | None = None):
        self.settings = settings
        self.client = httpx.AsyncClient(
            timeout=httpx.Timeout(30.0, connect=10.0),
            transport=transport,
            trust_env=False,
        )

    async def close(self) -> None:
        await self.client.aclose()

    async def reply(
        self,
        messages: list[dict],
        tools: list[dict],
        choice: str | dict,
        on_text: Callable[[str], None] | None = None,
    ) -> ModelReply:
        if not self.settings.api_key:
            raise ModelError("请在 .env 中填写 DEEPSEEK_API_KEY 并重启后端。")

        request = {
            "model": self.settings.model,
            "messages": messages,
            "tools": tools,
            "tool_choice": choice,
            "thinking": {"type": "disabled"},
            "max_tokens": 1500,
            "stream": True,
        }
        try:
            async with asyncio.timeout(55):
                async with self.client.stream(
                    "POST",
                    f"{self.settings.base_url}/chat/completions",
                    headers={"Authorization": f"Bearer {self.settings.api_key}", "Accept": "text/event-stream"},
                    json=request,
                ) as response:
                    if response.status_code != 200:
                        raise ModelError(HTTP_ERRORS.get(response.status_code, "DeepSeek 暂时无法响应，请稍后重试。"))
                    return await self._collect(response, on_text)
        except (httpx.TimeoutException, TimeoutError) as exc:
            raise ModelError("DeepSeek 响应超时，请稍后重试。") from exc
        except (httpx.HTTPError, httpx.InvalidURL) as exc:
            raise ModelError("无法连接 DeepSeek，请检查网络及 .env 中的接口地址。") from exc
        except (ValueError, KeyError, TypeError, AttributeError) as exc:
            raise ModelError("DeepSeek 返回的数据格式异常，请重试。") from exc

    @staticmethod
    async def _collect(response: httpx.Response, on_text: Callable[[str], None] | None) -> ModelReply:
        text: list[str] = []
        calls: dict[int, ToolCall] = {}
        data_lines: list[str] = []
        finish_reason = None

        async for line in response.aiter_lines():
            if line.startswith("data:"):
                data_lines.append(line[5:].removeprefix(" "))
                continue
            if line or not data_lines:
                continue

            event = "\n".join(data_lines)
            data_lines.clear()
            if event == "[DONE]":
                break
            chunk = json.loads(event)
            if "error" in chunk:
                raise ModelError("DeepSeek 在生成过程中返回错误，请重试。")
            choices = chunk.get("choices") or []
            if not choices:
                continue
            streamed = choices[0]
            delta = streamed.get("delta") or {}
            content = delta.get("content")
            if content:
                if not isinstance(content, str):
                    raise ModelError("DeepSeek 返回的正文格式异常。")
                text.append(content)
                if on_text:
                    on_text(content)
            for fragment in delta.get("tool_calls") or []:
                index = fragment["index"]
                if type(index) is not int or not 0 <= index < 16:
                    raise ModelError("DeepSeek 返回了超出范围的工具索引。")
                call = calls.setdefault(index, ToolCall())
                function = fragment.get("function") or {}
                if fragment.get("id"):
                    call.id = fragment["id"]
                if function.get("name"):
                    call.name = function["name"]
                call.arguments += function.get("arguments") or ""
                if len(call.arguments) > 8192:
                    raise ModelError("工具调用参数过长，未执行工具。")
            finish_reason = streamed.get("finish_reason") or finish_reason

        if finish_reason not in {"stop", "tool_calls"}:
            raise ModelError("DeepSeek 输出提前中断，请重试。")
        if (finish_reason == "tool_calls" and not calls
                or any(not call.id or not call.name for call in calls.values())
                or len({call.id for call in calls.values()}) != len(calls)):
            raise ModelError("DeepSeek 返回的工具调用信息不完整。")
        return ModelReply("".join(text), [calls[index] for index in sorted(calls)])
