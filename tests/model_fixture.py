"""Deterministic Chat Completions stream used in local tests."""

import asyncio
import json
import re
from uuid import uuid4

import httpx


class FixtureStream(httpx.AsyncByteStream):
    def __init__(self, events: list[dict], delay: float):
        self.events = events
        self.delay = delay

    async def __aiter__(self):
        yield b": keep-alive\n\n"
        for chunk in self.events:
            if self.delay:
                await asyncio.sleep(self.delay)
            serialized = ("data: " + json.dumps(chunk, ensure_ascii=False) + "\n\n").encode()
            for index in range(0, len(serialized), 13):
                yield serialized[index:index + 13]
        yield b'data: {"choices": [], "usage": {"total_tokens": 1}}\n\n'
        yield b"data: [DONE]\n\n"


def chunk(delta: dict, finish: str | None = None) -> dict:
    return {"choices": [{"index": 0, "delta": delta, "finish_reason": finish}]}


def call_events(calls: list[tuple[str, dict | str]]) -> list[dict]:
    starts, ends = [], []
    for index, (name, args) in enumerate(calls):
        raw = args if isinstance(args, str) else json.dumps(args, ensure_ascii=False)
        split = len(raw) // 2
        starts.append({
            "id": "call_" + uuid4().hex,
            "index": index,
            "type": "function",
            "function": {"name": name, "arguments": raw[:split]},
        })
        ends.append({"index": index, "id": "", "function": {"arguments": raw[split:]}})
    return [chunk({"tool_calls": starts}), chunk({"tool_calls": ends}), chunk({}, "tool_calls")]


def text_events(text: str) -> list[dict]:
    return [*(chunk({"content": text[i:i + 12]}) for i in range(0, len(text), 12)), chunk({}, "stop")]


class ModelFixture:
    def __init__(self, delay=0.0):
        self.requests: list[dict] = []
        self.delay = delay
        self.fail_summaries = False

    def __call__(self, request: httpx.Request) -> httpx.Response:
        assert request.url.path.endswith("/chat/completions")
        assert request.headers["authorization"] == "Bearer fixture-key"
        body = json.loads(request.content)
        assert body["stream"] is True
        assert body["thinking"] == {"type": "disabled"}
        self.requests.append(body)
        messages = body["messages"]
        choice = body["tool_choice"]
        question = next(message["content"] for message in reversed(messages) if message["role"] == "user")
        observations = [json.loads(message["content"]) for message in messages if message["role"] == "tool"]

        if self.fail_summaries and question.startswith("系统事件"):
            return httpx.Response(401, json={"error": "fixture-auth-rejected"})

        if isinstance(choice, dict):
            name = choice["function"]["name"]
            job_id = next((result["job_id"] for result in reversed(observations) if result.get("ok")), None)
            if not job_id:
                job_id = re.search(r"[a-f0-9]{32}", question).group()
            events = call_events([(name, {"job_id": job_id})])
        elif choice == "none":
            events = text_events(self._explain(observations))
        elif observations:
            events = text_events(self._explain(observations))
        elif "两个任务" in question:
            events = call_events([
                ("submit_job", {"analysis_type": "seq_stats", "filename": "sample1"}),
                ("submit_job", {"analysis_type": "find_orf", "filename": "sample2"}),
            ])
        elif "非法参数测试" in question:
            events = call_events([("submit_job", {"wrong": "argument"})])
        elif "不完整片段测试" in question:
            events = call_events([("submit_job", {"analysis_type": "seq_stats", "filename": "sample1"})])[:-1]
        elif "查状态" in question and re.search(r"[a-f0-9]{8,32}", question):
            job_id = re.search(r"[a-f0-9]{8,32}", question).group()
            events = call_events([("job_status", {"job_id": job_id})])
        else:
            context = json.loads(messages[0]["content"].split("\n会话数据 ", 1)[1])
            filename = self._filename(question, context["available_files"])
            if filename:
                analysis = "find_orf" if "orf" in question.lower() else "seq_stats"
                if "再对" in question and context["recent_jobs"]:
                    analysis = context["recent_jobs"][0]["analysis_type"]
                events = call_events([("submit_job", {"analysis_type": analysis, "filename": filename})])
            else:
                events = text_events("请提供想要分析的 FASTA 文件名。")

        return httpx.Response(200, headers={"Content-Type": "text/event-stream"}, stream=FixtureStream(events, self.delay))

    @staticmethod
    def _filename(question: str, available: list[str]) -> str | None:
        for name in available:
            if name in question or (name.startswith("sample") and name.split(".")[0] in question):
                return name
        return None

    @staticmethod
    def _explain(observations: list[dict]) -> str:
        if not observations:
            return "请提供分析类型和文件名。"
        result = observations[-1]
        if not result["ok"]:
            return result["error"]["message"]
        logs = "stdout" in result
        if not logs:
            state = result["state"]
            if state in {"queued", "running"}:
                return f"已提交 {result['filename']}。任务 ID 为 {result['job_id']}，完成后会更新。"
            return f"{result['filename']} 当前状态为 {state}。"
        if result["state"] == "failed":
            issues = [json.loads(line) for line in result["stderr"].splitlines() if line.startswith("{")]
            entries = [
                (f"第 {issue['line']} 行，" if issue.get("line") else "") + issue["message"]
                + (" 涉及字符 " + "、".join(issue["characters"]) + "。" if issue.get("characters") else "")
                for issue in issues
            ]
            if any(issue["code"] == "MISSING_HEADER" for issue in issues):
                entries.append("请给标题补上 >，核对非法字符后重新提交。")
            if result.get("runner_error"):
                entries.append(result["runner_error"])
            return result["filename"] + " 分析失败。\n\n" + "\n".join("- " + entry for entry in entries)
        records = [json.loads(line) for line in result["stdout"].splitlines() if line.startswith("{")]
        result_lines = []
        for record in records:
            if "record_count" in record:
                continue
            if "gc_pct" in record:
                gc = "无法计算" if record["gc_pct"] is None else f"{record['gc_pct']:.2f}%"
                result_lines.append(f"{record['id']} 为 {record['length_nt']} nt，GC 含量 {gc}。")
            elif record["orf"]:
                orf = record["orf"]
                result_lines.append(f"{record['id']} 为 {orf['start']}–{orf['end']}，{orf['length_nt']} nt。")
            else:
                result_lines.append(f"{record['id']} 没有完整 ORF。")
        return result["filename"] + " 分析完成。\n\n" + "\n".join("- " + line for line in result_lines)
