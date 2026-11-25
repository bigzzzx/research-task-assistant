"""Run analysis jobs and persist each observed transition."""

import asyncio
import codecs
from collections.abc import Callable
from contextlib import suppress
from datetime import datetime, timedelta, timezone
import errno
import logging
import os
from pathlib import Path
import subprocess
import sys

from .config import ANALYSES, CUSTOM_ANALYSIS, LOG_LIMIT, ROOT, Settings
from .docker_runner import run_python_job
from .events import Events
from .failures import Failure, INTERRUPTED, execution_failure, launch_failure
from .store import Store

logger = logging.getLogger(__name__)
SCRIPTS = {analysis: ROOT / "scripts" / f"{analysis}.py" for analysis in ANALYSES}
TERMINAL = {"succeeded", "failed"}
LOG_PAGE_BYTES = 64 * 1024


class Jobs:
    def __init__(self, settings: Settings, store: Store, events: Events, on_finish: Callable[[dict], None]):
        self.settings = settings
        self.store = store
        self.events = events
        self.on_finish = on_finish
        self._queue: asyncio.Queue[str] = asyncio.Queue()
        self._workers: list[asyncio.Task] = []
        self._delayed: dict[str, asyncio.TimerHandle] = {}

    def start(self) -> None:
        self._workers = [
            asyncio.create_task(self._worker(), name=f"analysis-worker-{index}")
            for index in range(self.settings.job_workers)
        ]
        for job in self.store.queued_jobs():
            self.enqueue(job)

    async def close(self) -> None:
        for timer in self._delayed.values():
            timer.cancel()
        self._delayed.clear()
        for worker in self._workers:
            worker.cancel()
        await asyncio.gather(*self._workers, return_exceptions=True)

    def enqueue(self, job: dict) -> None:
        job_id = job["id"]
        available_at = job["available_at"]
        delay = (datetime.fromisoformat(available_at) - datetime.now(timezone.utc)).total_seconds() if available_at else 0
        if delay > 0:
            if job_id not in self._delayed:
                self._delayed[job_id] = asyncio.get_running_loop().call_later(delay, self._ready, job_id)
        else:
            self._ready(job_id)
        self.events.send(job["owner_id"], "job.updated", job)

    def _ready(self, job_id: str) -> None:
        timer = self._delayed.pop(job_id, None)
        if timer:
            timer.cancel()
        self._queue.put_nowait(job_id)

    def directory(self, job_id: str) -> Path:
        return self.settings.data_dir / "jobs" / job_id

    def log_path(self, job_id: str, stream: str) -> Path:
        return self.directory(job_id) / f"{stream}.log"

    def log_preview(self, job: dict) -> dict:
        output = {}
        for stream in ("stdout", "stderr"):
            path = self.log_path(job["id"], stream)
            try:
                output[stream], output[f"{stream}_truncated"] = bounded_log(path)
            except FileNotFoundError:
                output[stream], output[f"{stream}_truncated"] = "", False
            except OSError:
                output[stream], output[f"{stream}_truncated"] = "当前无法读取此日志文件。", False
        return {**status_view(job), **output}

    async def _worker(self) -> None:
        while True:
            job_id = await self._queue.get()
            try:
                job = self.store.claim_job(job_id)
                if job:
                    self.events.send(job["owner_id"], "job.updated", job)
                    await self._run(job)
                elif (pending := self.store.job(job_id)) and pending["state"] == "queued":
                    self.enqueue(pending)
            finally:
                self._queue.task_done()

    async def _run(self, job: dict) -> None:
        job_id = job["id"]
        try:
            exit_code, failure = await self._execute(job)
        except asyncio.CancelledError:
            self.store.finish_job(job_id, None, failure=INTERRUPTED)
            raise
        except Exception:
            logger.exception("Unexpected error running job %s", job_id)
            exit_code = None
            failure = Failure("unknown", "RUNNER_ERROR", "execution", "后台执行出现异常，请核对日志。")

        retry_at = None
        attempts = job["automatic_retries"]
        if failure and failure.category == "temporary_resource" and attempts < self.settings.job_max_auto_retries:
            delay = timedelta(seconds=self.settings.job_retry_delay * 2 ** attempts)
            retry_at = (datetime.now(timezone.utc) + delay).isoformat(timespec="microseconds").replace("+00:00", "Z")
        finished = self.store.finish_job(job_id, exit_code, failure=failure, retry_at=retry_at)
        self.events.send(finished["owner_id"], "job.updated", finished)
        if finished["input_request_id"]:
            self.events.send(job["owner_id"], "input.updated", self.store.input_request(finished["input_request_id"]))
        if retry_at and finished["next_attempt_id"]:
            self.enqueue(self.store.job(finished["next_attempt_id"]))
        self.on_finish(finished)

    async def _execute(self, job: dict) -> tuple[int | None, Failure | None]:
        job_id = job["id"]
        custom = job["analysis_type"] == CUSTOM_ANALYSIS
        script = SCRIPTS.get(job["analysis_type"])
        source = None
        if custom:
            source = self.store.analysis_script_source(job["owner_id"], job["script_id"])
            if source is None:
                return None, Failure("configuration", "SCRIPT_NOT_FOUND", "preparation", "找不到此任务的脚本版本。")
        elif not script or not script.is_file():
            return None, Failure("configuration", "SCRIPT_NOT_FOUND", "preparation", "找不到此任务类型的分析脚本。")
        file = self.store.owned_file(job["owner_id"], job["file_id"])
        try:
            if not file:
                raise FileNotFoundError
            file_path = registered_input(self.settings.data_dir, file)
        except FileNotFoundError:
            return None, Failure("input", "INPUT_NOT_FOUND", "input", "原文件不存在，请补传可用文件。")
        except OSError:
            return None, Failure("input", "INPUT_UNREADABLE", "input", "原文件无法读取，请换用可读文件。")

        log_path = self.log_path(job_id, "stderr")
        try:
            directory = self.directory(job_id)
            directory.mkdir(parents=True, exist_ok=True)
            with self.log_path(job_id, "stdout").open("wb") as stdout, log_path.open("wb") as stderr:
                if custom:
                    return await run_python_job(job_id, directory, source, file_path, stdout, stderr,
                                                self.settings.job_timeout)
                exit_code, failure = await self._run_process(script, file_path, stdout, stderr)
        except OSError as exc:
            if exc.errno == errno.ENOSPC:
                return None, Failure("resource_limit", "LOG_STORAGE_FULL", "preparation", "磁盘空间不足，无法保存任务日志。")
            return None, Failure("configuration", "LOG_IO_FAILED", "preparation", "无法写入任务日志，请检查项目目录权限。")
        return exit_code, failure or (execution_failure(exit_code, log_path) if exit_code else None)

    async def _run_process(self, script: Path, file_path: Path, stdout, stderr) -> tuple[int | None, Failure | None]:
        # The analysis scripts only need the standard library. Do not inherit the model key.
        environment = {key: value for key, value in os.environ.items()
                       if key.upper() in {"SYSTEMROOT", "WINDIR", "TEMP", "TMP", "PATH"}}
        try:
            process = subprocess.Popen(
                [sys.executable, "-E", "-X", "utf8", "-u", str(script), "--", str(file_path)],
                stdout=stdout,
                stderr=stderr,
                stdin=subprocess.DEVNULL,
                shell=False,
                cwd=ROOT,
                env=environment,
                creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0,
            )
        except OSError as exc:
            return None, launch_failure(exc)
        waiter = asyncio.create_task(asyncio.to_thread(process.wait))
        try:
            return await asyncio.wait_for(asyncio.shield(waiter), timeout=self.settings.job_timeout), None
        except (TimeoutError, asyncio.CancelledError) as exc:
            with suppress(ProcessLookupError):
                process.kill()
            exit_code = await waiter
            if isinstance(exc, asyncio.CancelledError):
                raise
            return exit_code, Failure("timeout", "TIMEOUT", "execution",
                                      f"分析超过 {self.settings.job_timeout:g} 秒，已结束子进程。可核对输入后重试。")


def registered_input(data_dir: Path, file: dict) -> Path:
    folder = ROOT / "data" if file["source"] == "sample" else data_dir / "uploads"
    candidate = (folder / file["storage_name"]).resolve(strict=True)
    if candidate.parent != folder.resolve() or not candidate.is_file():
        raise OSError("Unregistered input location")
    return candidate


def log_page(path: Path, offset: int, *, finished: bool) -> dict:
    try:
        with path.open("rb") as source:
            size = os.fstat(source.fileno()).st_size
            start = min(offset, size)
            source.seek(start)
            data = source.read(min(LOG_PAGE_BYTES, size - start))
    except FileNotFoundError:
        return {"text": "", "next_offset": 0, "has_more": False, "available": False}

    decoder = codecs.getincrementaldecoder("utf-8")(errors="replace")
    text = decoder.decode(data, final=finished and start + len(data) == size)
    next_offset = start + len(data) - len(decoder.getstate()[0])
    return {
        "text": text, "next_offset": next_offset, "available": True,
        "has_more": start + len(data) < size,
    }


def bounded_log(path: Path) -> tuple[str, bool]:
    with path.open("rb") as source:
        first = source.read(LOG_LIMIT + 1)
        if len(first) <= LOG_LIMIT:
            return first.decode("utf-8", errors="replace"), False
        marker = b"\n[... log truncated ...]\n"
        side = (LOG_LIMIT - len(marker) - 12) // 2
        source.seek(-side, os.SEEK_END)
        head = first[:side].decode("utf-8", errors="ignore")
        tail = source.read(side).decode("utf-8", errors="ignore")
        return head + marker.decode() + tail, True


def status_view(job: dict) -> dict:
    keys = ("state", "filename", "analysis_type", "created_at", "started_at", "finished_at", "exit_code", "runner_error",
            "duration_seconds", "queue_wait_seconds", "status_at", "failure_category", "failure_code", "failure_stage",
            "retry_of_job_id", "attempt_number", "retry_kind", "available_at", "next_attempt_id", "next_attempt_at",
            "input_request_id")
    status = {"ok": True, "job_id": job["id"], **{key: job[key] for key in keys}}
    if job.get("script_id"):
        status.update(analysis_type=job["task_type"], script_name=job["script_name"], script_sha256=job["script_sha256"])
    return status
