import asyncio
from hashlib import sha256
from io import BytesIO
import json
from pathlib import Path
import re
import sys
from tempfile import TemporaryDirectory
from unittest import IsolatedAsyncioTestCase
from unittest.mock import patch

import httpx

from backend.app.config import Settings
from backend.app.docker_runner import LOG_END, MAX_LOG_BYTES, run_python_job
from tests.model_fixture import FixtureStream, ModelFixture, call_events
from tests.test_app import application, client_for, create_room, send, sign_in, terminal, tool_results, until


# Stand in for Docker at the process boundary. The uploaded Python code is never run by this fixture.
DOCKER_FIXTURE = r"""
import csv
import os
from pathlib import Path
import sys
import time

args = sys.argv[1:]
if args[0] == "rm":
    assert args[1] == "--force"
    Path(__file__).with_name("removed.txt").write_text(args[2], encoding="utf-8")
    sys.exit(0)

assert args[0] == "run" and "--rm" in args and "--read-only" in args
assert not list(Path(os.environ["DOCKER_CONFIG"]).iterdir())
assert "DEEPSEEK_API_KEY" not in os.environ
assert args[args.index("--network") + 1] == "none"
assert args[args.index("--user") + 1] == "65534:65534"
assert args[args.index("--cap-drop") + 1] == "ALL"
assert args[args.index("--memory") + 1] == "256m"
mount = next(csv.reader([args[args.index("--mount") + 1]]))
assert mount[0] == "type=bind" and mount[2:] == ["destination=/inputs", "readonly"]
directory = Path(mount[1].removeprefix("source="))
assert sorted(path.name for path in directory.iterdir()) == ["analysis.py", "input.fasta"]
assert args[-2:] == ["/inputs/analysis.py", "/inputs/input.fasta"]
source = (directory / "analysis.py").read_bytes()
if source == b"# slow":
    print("started", flush=True)
    time.sleep(30)
elif source == b"# big log":
    sys.stdout.buffer.write(b"x" * (1024 * 1024 + 8192))
else:
    records = sum(line.startswith(">") for line in (directory / "input.fasta").read_text().splitlines())
    print(f"{source.decode().strip()} has {records} FASTA records")
"""


class PythonModelFixture(ModelFixture):
    def __call__(self, request):
        body = json.loads(request.content)
        messages = body["messages"]
        question = next(row["content"] for row in reversed(messages) if row["role"] == "user")
        if body.get("tool_choice") == "auto" and messages[-1]["role"] != "tool":
            match = re.fullmatch(r"用 ([^ ]+) 分析 ([^ ]+)", question)
            if match:
                self.requests.append(body)
                types, filename = match.groups()
                calls = [("submit_job", {"analysis_type": task_type, "filename": filename})
                         for task_type in types.split(",")]
                return httpx.Response(200, headers={"Content-Type": "text/event-stream"},
                                      stream=FixtureStream(call_events(calls), self.delay))
        return super().__call__(request)

    @staticmethod
    def _explain(observations):
        if observations and observations[-1].get("script_name") and "stdout" in observations[-1]:
            result = observations[-1]
            return result.get("runner_error") or result["stdout"] or "任务已完成。"
        return ModelFixture._explain(observations)


class PythonJobTests(IsolatedAsyncioTestCase):
    def setUp(self):
        temporary = TemporaryDirectory(prefix="python-jobs,")
        self.addCleanup(temporary.cleanup)
        self.directory = Path(temporary.name)
        self.settings = Settings(data_dir=self.directory / "data", api_key="fixture-key")
        fixture = self.directory / "docker_fixture.py"
        fixture.write_text(DOCKER_FIXTURE, encoding="utf-8")
        self.command = [sys.executable, "-X", "utf8", str(fixture)]
        self.model = PythonModelFixture()

    async def upload(self, client, version, task_type="count_records", filename="count.py"):
        response = await client.post("/api/analysis-scripts", params={
            "filename": filename, "task_type": task_type,
        }, content=version)
        self.assertEqual(response.status_code, 201, response.text)
        return response.json()

    async def advertised(self, client):
        tools = (await client.get("/api/tools")).json()
        submit = next(tool for tool in tools if tool["name"] == "submit_job")
        return submit["parameters"]["properties"]["analysis_type"]["enum"]

    async def test_register_dynamically_and_submit_via_chat_mcp(self):
        with patch("backend.app.docker_runner.docker_command", return_value=self.command), patch.dict(
            "os.environ", {"DEEPSEEK_API_KEY": "a-fixture-only-secret", "DOCKER_CONFIG": str(self.directory)},
        ):
            async with application(self.settings, self.model) as app:
                async with client_for(app) as owner, client_for(app) as other:
                    self.assertEqual((await owner.get("/api/analysis-scripts")).status_code, 401)
                    await sign_in(owner, "python-owner")
                    await sign_in(other, "other-owner")
                    self.assertEqual(await self.advertised(owner), ["seq_stats", "find_orf"])
                    first = await self.upload(owner, b"# original", "Count_Records")
                    second = await self.upload(owner, b"# alternate", "记录数")
                    other_script = await self.upload(other, b"# private", "private_type")
                    self.assertEqual(await self.advertised(owner), ["seq_stats", "find_orf", "count_records", "记录数"])
                    self.assertEqual(await self.advertised(other), ["seq_stats", "find_orf", "private_type"])
                    self.assertEqual(first["sha256"], sha256(b"# original").hexdigest())
                    self.assertEqual((await other.get("/api/analysis-scripts")).json(), [other_script])
                    self.assertNotIn("source", first)
                    for name, task_type in (("notes.txt", "wrong_extension"), ("count.py", "SEQ_STATS")):
                        response = await owner.post("/api/analysis-scripts", params={
                            "filename": name, "task_type": task_type,
                        }, content=b"hello")
                        self.assertEqual(response.status_code, 422)
                    self.assertEqual((await owner.get("/api/jobs")).json(), [])

                    uploaded = await owner.post("/api/files", params={"filename": "fixture.fasta"}, content=b">one\nACGT\n")
                    self.assertEqual(uploaded.status_code, 201, uploaded.text)
                    room = await create_room(owner)
                    foreign_room = await create_room(other)
                    await send(other, foreign_room, "race", "用 count_records 分析 sample1.fasta")
                    refused = await until(other, foreign_room, lambda value: bool(tool_results(value)))
                    self.assertEqual(tool_results(refused)[0]["error"]["code"], "UNKNOWN_ANALYSIS")
                    await send(other, foreign_room, "private-file", "用 private_type 分析 fixture.fasta")
                    missing = await until(other, foreign_room, lambda value: bool(value["input_requests"]))
                    self.assertIsNone(missing["input_requests"][0]["original_file_id"])

                    question = "用 count_records,记录数,count_records 分析 fixture.fasta"
                    sent = await send(owner, room, "submit", question)
                    replay = await send(owner, room, "submit", question)
                    self.assertEqual(sent["id"], replay["id"])
                    snapshot = await until(owner, room, lambda value: terminal(value, 2))
                    jobs = {job["task_type"]: job for job in snapshot["jobs"]}
                    for script, output in ((first, "# original"), (second, "# alternate")):
                        job = jobs[script["task_type"]]
                        self.assertEqual((job["state"], job["script_id"]), ("succeeded", script["id"]))
                        job_id = job["id"]
                        events = [result for result in tool_results(snapshot, job_id) if result.get("ok")]
                        self.assertEqual(len(events), 4 if script == first else 3)
                        self.assertTrue(all(row["analysis_type"] == script["task_type"] for row in events))
                        self.assertIn("stdout", events[-1])
                        url = room + f"/jobs/{job_id}/logs/stdout"
                        self.assertIn(output + " has 1 FASTA records", (await owner.get(url)).text)
                        self.assertEqual((await other.get(url)).status_code, 404)
                        self.assertTrue(any(row["job_id"] == job_id and row["kind"] == "summary" and output in row["content"]
                                            for row in snapshot["messages"]))
                    request = next(row for row in self.model.requests if row["messages"][-1]["content"] == question)
                    schema = next(tool["function"]["parameters"] for tool in request["tools"]
                                  if tool["function"]["name"] == "submit_job")
                    self.assertEqual(schema["properties"]["analysis_type"]["enum"], await self.advertised(owner))
                    self.assertNotIn(first["sha256"], json.dumps(request))
                    self.assertEqual((await other.get("/api/jobs")).json(), [])

    async def test_missing_input_and_follow_up_preserve_version(self):
        with patch("backend.app.docker_runner.docker_command", return_value=self.command):
            async with application(self.settings, self.model) as app:
                async with client_for(app) as client:
                    await sign_in(client, "python-owner")
                    first = await self.upload(client, b"# original")
                    room = await create_room(client)
                    await send(client, room, "missing", "用 count_records 分析 new_file.fasta")
                    snapshot = await until(client, room, lambda value: bool(value["input_requests"]))
                    pending, = snapshot["input_requests"]
                    self.assertEqual((pending["task_type"], pending["script_id"]), ("count_records", first["id"]))
                    second = await self.upload(client, b"# newer")
                    self.assertEqual(await self.advertised(client), ["seq_stats", "find_orf", "count_records"])
                    response = await client.post(f"/api/input-requests/{pending['id']}/resolve",
                                                 json={"file_id": "sample1.fasta"})
                    self.assertEqual(response.status_code, 202, response.text)
                    snapshot = await until(client, room, lambda value: terminal(value, 1))
                    self.assertEqual(snapshot["jobs"][0]["script_id"], first["id"])
                    await send(client, room, "repeat", "再对 sample2 做一次")
                    snapshot = await until(client, room, lambda value: terminal(value, 2))
                    latest, original = snapshot["jobs"]
                    self.assertEqual((latest["task_type"], latest["script_id"], latest["filename"]),
                                     ("count_records", second["id"], "sample2.fasta"))
                    self.assertEqual(original["script_sha256"], first["sha256"])

    async def test_missing_docker_and_manual_retry_keep_attempts(self):
        with patch("backend.app.docker_runner.docker_command", side_effect=FileNotFoundError):
            async with application(self.settings, self.model) as app:
                async with client_for(app) as client:
                    await sign_in(client, "python-owner")
                    script = await self.upload(client, b"# retry fixture")
                    room = await create_room(client)
                    await send(client, room, "missing-docker", "用 count_records 分析 sample1.fasta")
                    first = (await until(client, room, lambda value: terminal(value, 1)))["jobs"][0]
                    self.assertEqual((first["state"], first["failure_code"]), ("failed", "DOCKER_NOT_FOUND"))
                    self.assertIsNone(first["input_request_id"])
                    with patch("backend.app.docker_runner.docker_command", return_value=self.command):
                        response = await client.post(f"/api/jobs/{first['id']}/retry")
                        self.assertEqual(response.status_code, 202, response.text)
                        snapshot = await until(client, room, lambda value: terminal(value, 2))
                    retry, original = snapshot["jobs"]
                    self.assertEqual((original["id"], original["state"]), (first["id"], "failed"))
                    self.assertEqual((retry["retry_of_job_id"], retry["script_id"], retry["state"]),
                                     (first["id"], script["id"], "succeeded"))

    async def test_timeout_removes_container_and_logs_are_bounded(self):
        file = self.directory / "fixture.fasta"
        file.write_text(">test\nACGT\n", encoding="utf-8")
        with patch("backend.app.docker_runner.docker_command", return_value=self.command):
            out, error = BytesIO(), BytesIO()
            _, failure = await asyncio.wait_for(
                run_python_job("timeout", self.directory, b"# slow", file, out, error, 0.5), timeout=10,
            )
            self.assertEqual(failure.code, "TIMEOUT")
            self.assertEqual((self.directory / "removed.txt").read_text(), "research-python-timeout")
            out, error = BytesIO(), BytesIO()
            code, failure = await run_python_job("large-log", self.directory, b"# big log", file, out, error, 10)
            self.assertEqual((code, failure), (0, None), error.getvalue())
            self.assertEqual(len(out.getvalue()), MAX_LOG_BYTES)
            self.assertTrue(out.getvalue().endswith(LOG_END))
        self.assertFalse(list(self.directory.glob("runtime-*")))
