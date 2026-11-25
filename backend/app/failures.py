"""Structured failures used by the runner, task history, and recovery actions."""

from dataclasses import dataclass
import errno
from io import SEEK_END
import json
from pathlib import Path
import re


@dataclass(frozen=True)
class Failure:
    category: str
    code: str
    stage: str
    detail: str | None = None


INPUT_CODES = frozenset({
    "EMPTY_FILE", "EMPTY_HEADER", "EMPTY_SEQUENCE", "INPUT_UNREADABLE", "INVALID_BASE",
    "INVALID_ENCODING", "INVALID_ID", "MISSING_HEADER",
})
INTERRUPTED = Failure("interrupted", "INTERRUPTED", "execution",
                      "后端关闭时任务被中断，请重新提交计算。")


def launch_failure(error: OSError) -> Failure:
    if error.errno == errno.EAGAIN:
        return Failure("temporary_resource", "PROCESS_TEMPORARILY_UNAVAILABLE", "launch",
                       "本机暂时无法分配进程资源。")
    if error.errno in {errno.ENOMEM, errno.ENOSPC} or getattr(error, "winerror", None) in {8, 14, 112, 1450}:
        return Failure("resource_limit", "RESOURCE_LIMIT", "launch", "本机资源不足，请检查内存和磁盘空间。")
    if isinstance(error, FileNotFoundError):
        return Failure("configuration", "INTERPRETER_NOT_FOUND", "launch", "找不到运行分析所需的 Python 解释器。")
    if isinstance(error, PermissionError):
        return Failure("configuration", "LAUNCH_DENIED", "launch", "没有权限启动分析进程。")
    return Failure("unknown", "LAUNCH_FAILED", "launch", "无法启动分析进程，请核对任务日志和运行环境。")


def execution_failure(exit_code: int, log_path: Path) -> Failure:
    try:
        with log_path.open("rb") as source:
            start = source.read(16384)
            length = source.seek(0, SEEK_END)
            source.seek(max(0, length - 4096))
            end = source.read().decode("utf-8", errors="replace")
    except OSError:
        start, end = b"", ""
    if exit_code == 2:
        for line in start.splitlines():
            try:
                issue = json.loads(line)
            except ValueError:
                continue
            if isinstance(issue, dict) and isinstance(issue.get("code"), str) and issue["code"] in INPUT_CODES:
                return Failure("input", issue["code"], "input")
    if re.search(r"^(?:ModuleNotFoundError|ImportError):", end, re.MULTILINE):
        return Failure("configuration", "IMPORT_FAILED", "execution", "分析脚本加载依赖失败，请检查错误日志。")
    # Windows job logs end lines with CRLF, so the anchor has to tolerate the carriage return.
    if re.search(r"^MemoryError(?::[^\r\n]*)?\r?$", end, re.MULTILINE):
        return Failure("resource_limit", "MEMORY_LIMIT", "execution", "分析进程内存不足，请调整资源或输入规模。")
    return Failure("unknown", "UNCLASSIFIED_EXIT", "execution")
