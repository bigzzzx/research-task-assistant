"""The three callable tools and their validation boundary."""

from collections.abc import Collection
import json
from pathlib import PurePath
import re

from scripts.seq_stats import gc_percentage

from .config import ANALYSES
from .jobs import Jobs, status_view
from .store import Store

JOB_ID = re.compile(r"[0-9a-fA-F]{8,32}")
JOB_PARAM = {"job_id": {"type": "string", "description": "当前会话的任务 ID。优先使用工具返回的完整 ID。"}}
DEFINITIONS = {
    "submit_job": (
        "把指定 FASTA 文件提交给本机后台分析。立即返回任务 ID，任务完成会自动跟进。",
        {
            "analysis_type": {
                "type": "string",
                "enum": list(ANALYSES),
                "description": "seq_stats 为长度和 GC 统计，find_orf 为正链最长完整 ORF。",
            },
            "filename": {"type": "string", "description": "用户指定的 FASTA 文件名，如 sample1.fasta。缺失时可提示补传。"},
        },
    ),
    "job_status": ("按任务 ID 查询当前会话任务的状态、时间、退出码与恢复信息。", JOB_PARAM),
    "read_log": ("读取指定任务的标准输出和错误日志。两路日志含各自的截断标记。", JOB_PARAM),
}


def tool_error(code: str, message: str) -> dict:
    return {"ok": False, "error": {"code": code, "message": message}}


def argument_error(arguments: dict, expected: Collection[str]) -> dict | None:
    if (arguments.keys() != set(expected)
            or any(not isinstance(value, str) or not value or len(value) > 256 for value in arguments.values())):
        return tool_error("INVALID_ARGUMENTS", f"请提供有效的字段：{', '.join(expected)}。")
    return None


def find_job(store: Store, conversation_id: str, job_id: str) -> dict | None:
    if not JOB_ID.fullmatch(job_id):
        return None
    if len(job_id) == 32:
        job = store.job(job_id.lower())
        return job if job and job["conversation_id"] == conversation_id else None
    matches = store.jobs_with_prefix(conversation_id, job_id.lower())
    return matches[0] if len(matches) == 1 else None


def format_gc_log(log: dict, decimal_places: int) -> dict:
    """Add a display preview and keep the original log available for inspection."""
    lines = []
    legacy_precision = False
    for line in log["stdout"].splitlines():
        try:
            row = json.loads(line)
        except ValueError:
            lines.append(line)
            continue
        if not isinstance(row, dict) or "gc_pct" not in row:
            lines.append(line)
            continue

        saved_percentage = row.pop("gc_pct")
        gc_count, known_bases = row.get("gc_count"), row.get("known_bases")
        if type(gc_count) is int and type(known_bases) is int and 0 <= gc_count <= known_bases:
            percentage = gc_percentage(gc_count, known_bases, decimal_places)
            row["gc_display"] = f"{percentage}%" if percentage is not None else None
        elif isinstance(saved_percentage, (int, float)):
            row["gc_display"] = f"{saved_percentage:.2f}%"
            legacy_precision |= decimal_places != 2
        else:
            row["gc_display"] = None
        lines.append(json.dumps(row, ensure_ascii=False, separators=(",", ":")))
    result = {**log, "display_stdout": "\n".join(lines)}
    if legacy_precision:
        result["gc_precision_note"] = "这份历史日志只保存了两位小数，按记录中的精度显示。"
    return result


class Tools:
    def __init__(self, store: Store, jobs: Jobs):
        self.store = store
        self.jobs = jobs

    def execute(
        self, owner_id: str, conversation_id: str, turn_id: int, name: str, arguments: dict,
        *, only_job: str | None = None,
    ) -> dict:
        if not self.store.owned_conversation(owner_id, conversation_id):
            return tool_error("CONVERSATION_NOT_FOUND", "当前账号找不到此会话。")
        if name not in DEFINITIONS or (only_job and name == "submit_job"):
            return tool_error("UNKNOWN_TOOL", "这一轮无法使用这个工具。")
        if error := argument_error(arguments, DEFINITIONS[name][1]):
            return error

        if name == "submit_job":
            return self._submit(owner_id, conversation_id, turn_id, arguments["analysis_type"], arguments["filename"])

        job_id = arguments["job_id"]
        job = find_job(self.store, conversation_id, job_id)
        if not job or (only_job and job["id"] != only_job):
            return tool_error("JOB_NOT_FOUND", "当前会话找不到该任务。请从工具记录中复制任务 ID。")
        return status_view(job) if name == "job_status" else self.jobs.log_preview(job)

    def _submit(self, owner_id: str, conversation_id: str, turn_id: int, analysis_type: str, filename: str) -> dict:
        if analysis_type not in ANALYSES:
            return tool_error("UNKNOWN_ANALYSIS", "可用的分析是 seq_stats 和 find_orf。")
        if any(char in filename for char in "/\\:"):
            return tool_error("INVALID_FILENAME", "请使用已有样例或本账号上传的文件名。")

        key = filename.strip().casefold()
        files = self.store.files(owner_id)
        matches = [item for item in files if item["name_key"] == key]
        if not matches and not PurePath(key).suffix:
            matches = [item for item in files if PurePath(item["name_key"]).stem == key]
        if not matches:
            pending, created = self.store.request_input(conversation_id, turn_id, analysis_type, filename.strip())
            if created:
                self.jobs.events.send(owner_id, "input.updated", pending)
            return {**tool_error("FILE_NOT_FOUND", "未找到该文件。请在待补充文件卡片选择或上传文件，再确认继续。"),
                    "input_request_id": pending["id"]}
        if len(matches) > 1:
            return tool_error("AMBIGUOUS_FILE", "有多个同名文件，请选择带扩展名的完整文件名。")

        job, created = self.store.submit(conversation_id, turn_id, matches[0]["id"], analysis_type)
        if created:
            self.jobs.enqueue(job)
        return {
            "ok": True,
            "job_id": job["id"],
            "state": job["state"],
            "analysis_type": analysis_type,
            "filename": job["filename"],
        }
