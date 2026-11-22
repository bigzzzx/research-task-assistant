"""Start only the two analysis scripts and persist each observed transition."""

import asyncio
from collections.abc import Callable
from contextlib import suppress
import os
from pathlib import Path
import subprocess
import sys

from .config import LOG_LIMIT, ROOT, ANALYSES, Settings
from .events import Events
from .store import Store

SCRIPTS = {analysis: ROOT / "scripts" / f"{analysis}.py" for analysis in ANALYSES}
TERMINAL = {"succeeded", "failed"}


class Jobs:
    def __init__(self, settings: Settings, store: Store, events: Events, on_finish: Callable[[dict], None]):
        self.settings = settings
        self.store = store
        self.events = events
        self.on_finish = on_finish
        self._queue: asyncio.Queue[str] = asyncio.Queue()
        self._workers: list[asyncio.Task] = []

    def start(self) -> None:
        self._workers = [
            asyncio.create_task(self._worker(), name=f"analysis-worker-{index}")
            for index in range(self.settings.job_workers)
        ]

    async def close(self) -> None:
        for worker in self._workers:
            worker.cancel()
        await asyncio.gather(*self._workers, return_exceptions=True)

    def enqueue(self, job: dict) -> None:
        self._queue.put_nowait(job["id"])
        self.events.send("job.updated", job)

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

    def _input(self, file_id: str) -> Path:
        file = self.store.file(file_id)
        folder = ROOT / "data" if file["source"] == "sample" else self.settings.data_dir / "uploads"
        candidate = (folder / file["storage_name"]).resolve(strict=True)
        if candidate.parent != folder.resolve() or not candidate.is_file():
            raise OSError("Unregistered input location")
        return candidate

    async def _worker(self) -> None:
        while True:
            job_id = await self._queue.get()
            try:
                job = self.store.claim_job(job_id)
                if job:
                    self.events.send("job.updated", job)
                    await self._run(job)
            finally:
                self._queue.task_done()

    async def _run(self, job: dict) -> None:
        job_id = job["id"]
        exit_code = None
        runner_error = None
        try:
            self.directory(job_id).mkdir(parents=True, exist_ok=True)
            file_path = self._input(job["file_id"])
            with (
                self.log_path(job_id, "stdout").open("wb") as stdout,
                self.log_path(job_id, "stderr").open("wb") as stderr,
            ):
                exit_code, runner_error = await self._run_process(SCRIPTS[job["analysis_type"]], file_path, stdout, stderr)
        except asyncio.CancelledError:
            self.store.finish_job(job_id, None, "后端关闭时任务被中断，请重新提交计算。")
            raise
        except OSError:
            runner_error = "无法启动分析脚本或读取输入文件。请确认文件可读、项目目录可写。"

        finished = self.store.finish_job(job_id, exit_code, runner_error)
        self.events.send("job.updated", finished)
        self.on_finish(finished)

    async def _run_process(self, script: Path, file_path: Path, stdout, stderr) -> tuple[int, str | None]:
        # The analysis scripts only need the standard library. Do not inherit the model key.
        environment = {key: value for key, value in os.environ.items()
                       if key.upper() in {"SYSTEMROOT", "WINDIR", "TEMP", "TMP", "PATH"}}
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
        waiter = asyncio.create_task(asyncio.to_thread(process.wait))
        try:
            return await asyncio.wait_for(asyncio.shield(waiter), timeout=self.settings.job_timeout), None
        except (TimeoutError, asyncio.CancelledError) as exc:
            with suppress(ProcessLookupError):
                process.kill()
            exit_code = await waiter
            if isinstance(exc, asyncio.CancelledError):
                raise
            return exit_code, f"分析超过 {self.settings.job_timeout:g} 秒，已结束子进程。可核对输入后重试。"


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
    keys = ("state", "filename", "analysis_type", "created_at", "started_at", "finished_at", "exit_code", "runner_error")
    return {"ok": True, "job_id": job["id"], **{key: job[key] for key in keys}}
