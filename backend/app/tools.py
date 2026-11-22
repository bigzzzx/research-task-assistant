"""The three callable tools and their validation boundary."""

import json
from pathlib import PurePath
import re

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
            "filename": {"type": "string", "description": "已登记的 FASTA 文件名，如 sample1.fasta。"},
        },
    ),
    "job_status": ("按任务 ID 查询当前会话任务的实际状态和退出码。", JOB_PARAM),
    "read_log": ("读取指定任务的标准输出和错误日志。两路日志含各自的截断标记。", JOB_PARAM),
}


def tool_schemas(names=tuple(DEFINITIONS)) -> list[dict]:
    return [
        {
            "type": "function",
            "function": {
                "name": name,
                "description": DEFINITIONS[name][0],
                "parameters": {
                    "type": "object",
                    "properties": DEFINITIONS[name][1],
                    "required": list(DEFINITIONS[name][1]),
                    "additionalProperties": False,
                },
            },
        }
        for name in names
    ]


def tool_error(code: str, message: str) -> dict:
    return {"ok": False, "error": {"code": code, "message": message}}


class Tools:
    def __init__(self, store: Store, jobs: Jobs):
        self.store = store
        self.jobs = jobs

    def execute(self, conversation_id: str, turn_id: int, name: str, raw: str, *, only_job: str | None = None) -> dict:
        if name not in DEFINITIONS or (only_job and name == "submit_job"):
            return tool_error("UNKNOWN_TOOL", "这一轮无法使用这个工具。")
        try:
            arguments = json.loads(raw)
        except (ValueError, RecursionError):
            return tool_error("INVALID_ARGUMENTS", "工具参数不是有效的 JSON 对象。")
        expected = DEFINITIONS[name][1]
        if (not isinstance(arguments, dict) or arguments.keys() != expected.keys()
                or any(not isinstance(value, str) or not value or len(value) > 256 for value in arguments.values())):
            return tool_error("INVALID_ARGUMENTS", f"请提供有效的字段：{', '.join(expected)}。")

        if name == "submit_job":
            return self.submit(conversation_id, turn_id, arguments["analysis_type"], arguments["filename"])

        job_id = arguments["job_id"]
        job = self.find_job(conversation_id, job_id)
        if not job or (only_job and job["id"] != only_job):
            return tool_error("JOB_NOT_FOUND", "当前会话找不到该任务。请从工具记录中复制任务 ID。")
        return status_view(job) if name == "job_status" else self.jobs.log_preview(job)

    def find_job(self, conversation_id: str, job_id: str) -> dict | None:
        if not JOB_ID.fullmatch(job_id):
            return None
        if len(job_id) == 32:
            job = self.store.job(job_id.lower())
            return job if job and job["conversation_id"] == conversation_id else None
        matches = self.store.jobs_with_prefix(conversation_id, job_id.lower())
        return matches[0] if len(matches) == 1 else None

    def submit(self, conversation_id: str, turn_id: int, analysis_type: str, filename: str) -> dict:
        if analysis_type not in ANALYSES:
            return tool_error("UNKNOWN_ANALYSIS", "可用的分析是 seq_stats 和 find_orf。")
        if any(char in filename for char in "/\\:"):
            return tool_error("INVALID_FILENAME", "请使用已有样例或本会话上传的文件名。")

        key = filename.strip().casefold()
        files = self.store.files(conversation_id)
        matches = [item for item in files if item["name_key"] == key]
        if not matches and not PurePath(key).suffix:
            matches = [item for item in files if PurePath(item["name_key"]).stem == key]
        if not matches:
            return tool_error("FILE_NOT_FOUND", "未找到该文件。请确认样例名或通过上传入口添加文件。")
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
