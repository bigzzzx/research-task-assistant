import asyncio
from contextlib import asynccontextmanager
from dataclasses import replace
import json
from pathlib import Path
from tempfile import TemporaryDirectory
from time import time
from unittest import IsolatedAsyncioTestCase

import httpx

from backend.app.api import create_app
from backend.app.config import ROOT, UPLOAD_LIMIT, Settings
from backend.app.events import Events
from backend.app.store import Store
from tests.model_fixture import ModelFixture


TEST_PASSWORD = "fixture passphrase for local checks"


@asynccontextmanager
async def application(settings, model):
    app = create_app(settings, model_transport=httpx.MockTransport(model))
    async with app.router.lifespan_context(app):
        yield app


def client_for(app):
    return httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://127.0.0.1")


async def sign_in(client, username):
    credentials = {"username": username, "password": TEST_PASSWORD}
    response = await client.post("/api/auth/register", json=credentials)
    if response.status_code == 409:
        response = await client.post("/api/auth/login", json=credentials)
    assert response.status_code in {200, 201}, response.text
    return response.json()


@asynccontextmanager
async def service(settings, model):
    async with application(settings, model) as app:
        async with client_for(app) as client:
            await sign_in(client, "tester")
            yield client


async def create_room(client) -> str:
    response = await client.post("/api/conversations")
    assert response.status_code == 201, response.text
    return "/api/conversations/" + response.json()["id"]


async def send(client, room, key, text):
    response = await client.post(room + "/messages", json={"client_message_id": key, "text": text})
    assert response.status_code == 202, response.text
    return response.json()


async def until(client, room, predicate, timeout=14):
    async with asyncio.timeout(timeout):
        while True:
            snapshot = (await client.get(room)).json()
            if predicate(snapshot):
                return snapshot
            await asyncio.sleep(0.05)


def terminal(snapshot, count):
    jobs = snapshot["jobs"]
    return len(jobs) == count and all(job["summary_state"] in {"done", "error"} for job in jobs)


def tool_results(snapshot, job_id=None):
    return [
        json.loads(message["content"]) for message in snapshot["messages"]
        if message["kind"] == "tool" and (job_id is None or message["job_id"] == job_id)
    ]


class ApplicationTests(IsolatedAsyncioTestCase):
    def setUp(self):
        temp = TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.settings = Settings(data_dir=Path(temp.name), api_key="fixture-key")
        self.model = ModelFixture()

    async def test_success_then_anaphoric_failure_and_persistence(self):
        async with service(self.settings, self.model) as client:
            first_room = await create_room(client)
            await send(client, first_room, "stats", "帮我统计一下 sample1 里序列的 GC 含量。")
            running = await until(client, first_room, lambda value: any(j["state"] == "running" for j in value["jobs"]))
            self.assertTrue(any(row.get("state") == "queued" for row in tool_results(running)))

            other_room = await create_room(client)
            await send(client, other_room, "unrelated", "有哪些类型")
            await until(client, other_room, lambda value: any(m["kind"] == "assistant" for m in value["messages"]))
            first = await until(client, first_room, lambda value: terminal(value, 1))
            success_id = first["jobs"][0]["id"]
            self.assertEqual(first["jobs"][0]["state"], "succeeded")
            observations = tool_results(first, success_id)
            self.assertEqual([result["state"] for result in observations], ["queued", "succeeded", "succeeded"])
            stdout = await client.get(first_room + f"/jobs/{success_id}/logs/stdout")
            self.assertEqual(stdout.status_code, 200)
            self.assertIn("seq1", stdout.text)
            self.assertIn("48.89", stdout.text)
            self.assertEqual((await client.get(other_room + f"/jobs/{success_id}/logs/stdout")).status_code, 404)

            first_message = await send(client, first_room, "repeat", "再对 sample3 做一次。")
            replay = await send(client, first_room, "repeat", "再对 sample3 做一次。")
            self.assertEqual(first_message["id"], replay["id"])
            final = await until(client, first_room, lambda value: terminal(value, 2))
            bad, good = final["jobs"]
            self.assertNotEqual(bad["id"], good["id"])
            self.assertEqual((bad["filename"], bad["analysis_type"], bad["state"], bad["exit_code"]),
                             ("sample3.fasta", "seq_stats", "failed", 2))
            results = tool_results(final, bad["id"])
            self.assertEqual([result["state"] for result in results], ["queued", "failed", "failed"])
            issues = [json.loads(line)["code"] for line in results[-1]["stderr"].splitlines()]
            self.assertEqual(issues, ["MISSING_HEADER", "INVALID_BASE"])
            summaries = [message for message in final["messages"] if message["kind"] == "summary"]
            self.assertEqual(len(summaries), 2)
            self.assertIn("61.70%", summaries[0]["content"])
            self.assertIn("第 1 行", summaries[1]["content"])
            self.assertIn("第 2 行", summaries[1]["content"])
            self.assertTrue(any(row["tool_call_id"] for row in final["messages"] if row["kind"] == "tool"))

        async with service(self.settings, self.model) as client:
            restored = (await client.get(first_room)).json()
            self.assertEqual(restored["messages"], final["messages"])
            self.assertEqual(restored["jobs"], final["jobs"])
            self.assertEqual(len((await client.get("/api/conversations")).json()), 2)

    async def test_upload_validation_and_real_orf_subprocess(self):
        async with service(self.settings, self.model) as client:
            room = await create_room(client)
            response = await client.post(room + "/files?filename=植株 A.fa", content=b">test\nCATGCCC\nTAA\n")
            self.assertEqual(response.status_code, 201)
            self.assertEqual(response.json()["display_name"], "植株 A.fa")
            self.assertEqual((await client.post(room + "/files?filename=Sample1.fasta", content=b"")).status_code, 409)
            self.assertEqual((await client.post(room + "/files?filename=../p.fa", content=b"")).status_code, 422)
            self.assertEqual((await client.post(room + "/files?filename=test.txt", content=b"")).status_code, 422)
            self.assertEqual((await client.post(
                room + "/files?filename=large.fa", content=b"x" * (UPLOAD_LIMIT + 1)
            )).status_code, 413)
            self.assertEqual(len(list((self.settings.data_dir / "uploads").iterdir())), 1)

            await send(client, room, "orf", "找出植株 A.fa 的最长 ORF。")
            snapshot = await until(client, room, lambda value: terminal(value, 1))
            self.assertEqual(snapshot["jobs"][0]["analysis_type"], "find_orf")
            stdout = tool_results(snapshot)[-1]["stdout"]
            self.assertEqual(json.loads(stdout.splitlines()[0]),
                             {"id": "test", "orf": {"start": 2, "end": 10, "length_nt": 9}})

    async def test_multiple_fragmented_calls_and_invalid_arguments(self):
        async with service(self.settings, self.model) as client:
            room = await create_room(client)
            bad = await send(client, room, "bad", "非法参数测试")
            finished = await until(client, room, lambda value: any(
                row["id"] == bad["id"] and row["status"] == "done" for row in value["messages"]
            ))
            self.assertEqual(tool_results(finished)[-1]["error"]["code"], "INVALID_ARGUMENTS")
            self.assertEqual(finished["jobs"], [])

            incomplete = await send(client, room, "incomplete", "不完整片段测试")
            aborted = await until(client, room, lambda value: any(
                row["id"] == incomplete["id"] and row["status"] == "error" for row in value["messages"]
            ))
            self.assertEqual(aborted["jobs"], [])

            await send(client, room, "two", "提交两个任务")
            await until(client, room, lambda value: sum(j["state"] == "running" for j in value["jobs"]) == 2)
            snapshot = await until(client, room, lambda value: terminal(value, 2))
            self.assertEqual({j["analysis_type"] for j in snapshot["jobs"]}, {"seq_stats", "find_orf"})
            self.assertTrue(all(j["state"] == "succeeded" for j in snapshot["jobs"]))
            many = next(row for row in snapshot["messages"] if len(row["tool_calls"]) == 2)
            self.assertEqual(len([row for row in snapshot["messages"] if row["invocation_id"] == many["id"]]), 2)

    async def test_model_error_falls_back_to_local_log_and_can_retry(self):
        self.model.fail_summaries = True
        async with service(self.settings, self.model) as client:
            room = await create_room(client)
            await send(client, room, "bad-fasta", "对 sample3 做一次序列统计")
            snapshot = await until(client, room, lambda value: terminal(value, 1))
            job = snapshot["jobs"][0]
            summary = next(row for row in snapshot["messages"] if row["kind"] == "summary")
            self.assertEqual(job["summary_state"], "error")
            self.assertIn("第 1 行", summary["content"])
            self.assertIn("第 2 行", summary["content"])
            self.assertIn("鉴权", summary["content"])
            self.assertTrue(any(row["source"] == "system" for row in snapshot["messages"]))
            self.model.fail_summaries = False
            result = await client.post(room + f"/jobs/{job['id']}/retry-summary")
            self.assertEqual(result.status_code, 202)
            restored = await until(client, room, lambda value: (
                len(value["jobs"]) == 1 and value["jobs"][0]["summary_state"] == "done"
            ))
            summaries = [row for row in restored["messages"] if row["kind"] == "summary"]
            self.assertEqual([item["id"] for item in summaries], [summary["id"]])
            self.assertEqual(restored["jobs"][0]["id"], job["id"])

    async def test_restart_recovers_pending_job_and_rejects_other_origins(self):
        async with service(self.settings, self.model) as client:
            owner = (await client.get("/api/auth/me")).json()["id"]
        store = Store(self.settings.data_dir / "assistant.sqlite3", ROOT / "data")
        conversation = store.new_conversation(owner)["id"]
        message, _ = store.user_message(conversation, "queued-user", "统计 sample1")
        job, _ = store.submit(conversation, message["id"], "sample1.fasta", "seq_stats")
        store.claim_job(job["id"])
        store.close()

        async with service(self.settings, self.model) as client:
            self.assertEqual((await client.post("/api/conversations", headers={"Origin": "http://elsewhere"})).status_code, 403)
            restored = await until(client, "/api/conversations/" + conversation, lambda value: terminal(value, 1))
            self.assertEqual(restored["jobs"][0]["state"], "failed")
            self.assertIn("后端重启", restored["jobs"][0]["runner_error"])
            self.assertEqual(restored["messages"][0]["status"], "interrupted")

    async def test_script_timeout_is_a_task_failure(self):
        async with service(replace(self.settings, job_timeout=0.2), self.model) as client:
            room = await create_room(client)
            await send(client, room, "timeout", "统计 sample1 的 GC")
            snapshot = await until(client, room, lambda value: terminal(value, 1))
            self.assertEqual(snapshot["jobs"][0]["state"], "failed")
            self.assertIn("已结束子进程", snapshot["jobs"][0]["runner_error"])

    async def test_blank_key_does_not_start_an_agent(self):
        async with service(replace(self.settings, api_key=""), self.model) as client:
            self.assertEqual((await client.get("/api/config")).json()["key_configured"], False)
            room = await create_room(client)
            rejected = await client.post(room + "/messages", json={"client_message_id": "no-key", "text": "sample1"})
            self.assertEqual(rejected.status_code, 503)
            self.assertIn("DEEPSEEK_API_KEY", rejected.json()["detail"])
            self.assertEqual((await client.get(room)).json()["messages"], [])
            self.assertEqual(self.model.requests, [])

    async def test_sse_pushes_utf8_json_and_detaches_old_connections(self):
        events = Events()
        stream = events.listen("owner", "session", int(time()) + 30)
        self.assertIn("event: ready", await anext(stream))
        events.send("owner", "job.updated", {"state": "成功"})
        self.assertIn('data: {"state": "成功"}', await anext(stream))
        await stream.aclose()
        self.assertEqual(events._listeners, {})
