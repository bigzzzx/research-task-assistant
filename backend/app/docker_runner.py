"""Run an uploaded Python script against one staged FASTA file."""

import asyncio
from contextlib import suppress
import csv
from io import StringIO
import os
from pathlib import Path
import shutil
import subprocess
from tempfile import TemporaryDirectory
from typing import BinaryIO

from .failures import Failure


PYTHON_IMAGE = "docker.io/library/python:3.12-slim"
MAX_LOG_BYTES = 1024 * 1024
LOG_END = b"\n[... custom script log truncated ...]\n"
CREATE_FLAGS = subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0
CLIENT_ENV = {"PATH", "SYSTEMROOT", "WINDIR", "HOME", "USERPROFILE", "TEMP", "TMP", "XDG_RUNTIME_DIR"}


def docker_command() -> list[str]:
    executable = shutil.which("docker")
    if executable is None and os.name == "nt":
        desktop_cli = Path(os.environ.get("ProgramFiles", "C:/Program Files")) / "Docker/Docker/resources/bin/docker.exe"
        if desktop_cli.is_file():
            executable = str(desktop_cli)
    if executable is None:
        raise FileNotFoundError

    local_socket = "npipe:////./pipe/docker_engine" if os.name == "nt" else "unix:///var/run/docker.sock"
    host = os.environ.get("DOCKER_HOST") or local_socket
    if not host.startswith(("unix:///", "npipe:////./pipe/")):
        raise ValueError("A local Docker daemon is required.")
    return [executable, "--host", host]


def container_args(name: str, inputs: Path, timeout: float) -> list[str]:
    mount = StringIO()
    csv.writer(mount, lineterminator="").writerow(
        ("type=bind", f"source={inputs}", "destination=/inputs", "readonly"),
    )
    return [
        "run", "--rm", "--init", "--pull=never", "--name", name,
        "--network", "none", "--read-only", "--cap-drop", "ALL",
        "--security-opt", "no-new-privileges=true", "--user", "65534:65534",
        "--cpus", "1", "--memory", "256m", "--memory-swap", "256m", "--pids-limit", "32",
        "--log-driver", "none", "--tmpfs", "/tmp:rw,nosuid,nodev,noexec,size=16m",
        "--mount", mount.getvalue(), "--workdir", "/inputs",
        PYTHON_IMAGE, "timeout", "--signal=KILL", f"{timeout:g}s",
        "python", "-I", "-B", "-u", "-X", "utf8", "/inputs/analysis.py", "/inputs/input.fasta",
    ]


async def write_log(stream: asyncio.StreamReader, output: BinaryIO) -> None:
    remaining = MAX_LOG_BYTES - len(LOG_END)
    truncated = False
    while chunk := await stream.read(65536):
        if truncated:
            continue
        output.write(chunk[:remaining])
        if len(chunk) > remaining:
            output.write(LOG_END)
            truncated = True
        remaining = max(0, remaining - len(chunk))
        output.flush()


async def remove_container(command: list[str], name: str, env: dict[str, str]) -> None:
    try:
        process = await asyncio.create_subprocess_exec(
            *command, "rm", "--force", name, stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, env=env, creationflags=CREATE_FLAGS,
        )
    except OSError:
        return
    try:
        await asyncio.wait_for(process.wait(), timeout=5)
    except (asyncio.TimeoutError, asyncio.CancelledError) as error:
        with suppress(ProcessLookupError):
            process.kill()
        await process.wait()
        if isinstance(error, asyncio.CancelledError):
            raise


async def run_python_job(
    job_id: str, directory: Path, source: bytes, fasta: Path, stdout: BinaryIO, stderr: BinaryIO, timeout: float,
) -> tuple[int | None, Failure | None]:
    try:
        command = docker_command()
    except FileNotFoundError:
        return None, Failure("configuration", "DOCKER_NOT_FOUND", "launch", "请安装并启动本机 Docker 后重试。")
    except ValueError:
        return None, Failure("configuration", "DOCKER_NOT_LOCAL", "launch", "自定义脚本需要连接本机 Docker。")

    name = f"research-python-{job_id}"
    with TemporaryDirectory(prefix="runtime-", dir=directory) as temporary:
        runtime = Path(temporary).resolve()
        inputs = runtime / "inputs"
        inputs.mkdir(mode=0o755)
        inputs.chmod(0o755)
        client_config = runtime / "docker-config"
        client_config.mkdir()
        env = {key: value for key, value in os.environ.items() if key.upper() in CLIENT_ENV}
        # Do not let the user's Docker config inject proxy credentials into the container.
        env["DOCKER_CONFIG"] = str(client_config)
        script = inputs / "analysis.py"
        script.write_bytes(source)
        script.chmod(0o644)
        staged_fasta = inputs / "input.fasta"
        shutil.copyfile(fasta, staged_fasta)
        staged_fasta.chmod(0o644)
        try:
            process = await asyncio.create_subprocess_exec(
                *command, *container_args(name, inputs, timeout), stdin=subprocess.DEVNULL,
                stdout=subprocess.PIPE, stderr=subprocess.PIPE, env=env, creationflags=CREATE_FLAGS,
            )
        except OSError:
            return None, Failure("configuration", "DOCKER_LAUNCH_FAILED", "launch", "无法启动 Docker 客户端。")
        tasks = [
            asyncio.create_task(process.wait()),
            asyncio.create_task(write_log(process.stdout, stdout)),
            asyncio.create_task(write_log(process.stderr, stderr)),
        ]
        completion = asyncio.gather(*tasks)
        try:
            exit_code, _, _ = await asyncio.wait_for(asyncio.shield(completion), timeout)
        except (asyncio.TimeoutError, asyncio.CancelledError) as error:
            with suppress(ProcessLookupError):
                process.kill()
            await remove_container(command, name, env)
            await asyncio.gather(*tasks, return_exceptions=True)
            with suppress(Exception):
                completion.result()
            if isinstance(error, asyncio.CancelledError):
                raise
            return process.returncode, Failure("timeout", "TIMEOUT", "execution",
                                               f"自定义脚本运行超过 {timeout:g} 秒，请核对脚本后重试。")
        except Exception:
            with suppress(ProcessLookupError):
                process.kill()
            await remove_container(command, name, env)
            await asyncio.gather(*tasks, return_exceptions=True)
            raise
    if exit_code == 125:
        return exit_code, Failure("configuration", "DOCKER_START_FAILED", "launch",
                                  f"Docker 无法启动分析容器。请检查 Docker、{PYTHON_IMAGE} 镜像和错误日志。")
    if exit_code:
        return exit_code, Failure("unknown", "CUSTOM_SCRIPT_FAILED", "execution", "自定义 Python 脚本执行失败，请查看错误日志。")
    return 0, None
