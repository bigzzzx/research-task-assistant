from contextlib import asynccontextmanager
import json
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest import IsolatedAsyncioTestCase
from unittest.mock import patch

from mcp.client.stdio import stdio_client

from backend.app.config import Settings
from backend.app.mcp_tools import SCOPE_KEY
from tests.model_fixture import ModelFixture
from tests.test_app import application, client_for, create_room, send, sign_in, terminal, until


class MCPToolsTests(IsolatedAsyncioTestCase):
    async def test_chat_discovers_and_calls_tools_over_mcp(self):
        requests = []

        class RecordingWriter:
            def __init__(self, stream):
                self.stream = stream

            def __getattr__(self, name):
                return getattr(self.stream, name)

            async def send(self, message):
                requests.append(message.message.model_dump(by_alias=True, exclude_none=True))
                await self.stream.send(message)

            async def __aenter__(self):
                await self.stream.__aenter__()
                return self

            async def __aexit__(self, *args):
                return await self.stream.__aexit__(*args)

        @asynccontextmanager
        async def recording_stdio(parameters):
            self.assertEqual(parameters.args[-2:], ["-m", "backend.app.research_mcp"])
            async with stdio_client(parameters) as (read, write):
                yield read, RecordingWriter(write)

        model = ModelFixture()
        with TemporaryDirectory() as directory, patch("backend.app.mcp_tools.stdio_client", recording_stdio):
            settings = Settings(data_dir=Path(directory), api_key="fixture-key")
            async with application(settings, model) as app:
                async with client_for(app) as client:
                    self.assertIn("tools/list", [request.get("method") for request in requests])
                    account = await sign_in(client, "mcp-fixture")
                    catalog = (await client.get("/api/tools")).raise_for_status().json()
                    self.assertEqual([tool["name"] for tool in catalog], ["submit_job", "job_status", "read_log"])
                    self.assertTrue(all(tool["enabled"] and not tool["required"] and tool["server_id"] is None
                                        for tool in catalog))
                    room = await create_room(client)
                    message = await send(client, room, "mcp-run", "统计 sample1.fasta 的 GC 含量")
                    snapshot = await until(client, room, lambda result: terminal(result, 1))
                    job = snapshot["jobs"][0]
                    self.assertEqual(job["state"], "succeeded")
                    self.assertEqual(job["summary_state"], "done")
                    self.assertEqual((await client.get("/mcp")).status_code, 404)
                    self.assertEqual([set(tool["parameters"]["properties"]) for tool in catalog],
                                     [{"analysis_type", "filename"}, {"job_id"}, {"job_id"}])

                    rejected = await app.state.tools.execute(
                        account["id"], room.rsplit("/", 1)[1], message["id"], "read_log",
                        json.dumps({"job_id": job["id"], "owner_id": account["id"]}),
                    )
                    self.assertEqual(rejected["error"]["code"], "INVALID_ARGUMENTS")

                    methods = [request.get("method") for request in requests]
                    self.assertEqual(methods.count("tools/list"), 1)
                    calls = [request["params"] for request in requests if request.get("method") == "tools/call"]
                    self.assertEqual([call["name"] for call in calls], ["submit_job", "job_status", "read_log"])
                    for index, call in enumerate(calls):
                        self.assertEqual(call["_meta"][SCOPE_KEY], {
                            "owner_id": account["id"], "conversation_id": room.rsplit("/", 1)[1],
                            "turn_id": message["id"], "only_job": job["id"] if index else None,
                        })
                    self.assertEqual(calls[0]["arguments"], {"analysis_type": "seq_stats", "filename": "sample1.fasta"})
                    self.assertEqual(calls[2]["arguments"], {"job_id": job["id"]})
                    self.assertNotIn(SCOPE_KEY, json.dumps(model.requests))

    async def test_paused_tools_are_account_scoped_persistent_and_block_stale_calls(self):
        with TemporaryDirectory() as directory:
            settings = Settings(data_dir=Path(directory), api_key="fixture-key")
            model = ModelFixture()
            async with application(settings, model) as app:
                async with client_for(app) as owner, client_for(app) as other:
                    account = await sign_in(owner, "tool-owner")
                    await sign_in(other, "other-tool-owner")
                    room = await create_room(owner)
                    toggle = "/api/mcp/tools/submit_job/enabled"
                    for _ in range(2):
                        (await owner.post(toggle, json={"enabled": False})).raise_for_status()
                    self.assertTrue(all(tool["enabled"] for tool in (await other.get("/api/tools")).json()))
                    message = await send(owner, room, "paused-submit", "统计 sample1.fasta")
                    # The fixture requests submit_job even though this account no longer advertises it.
                    snapshot = await until(owner, room, lambda s: any(
                        m["id"] == message["id"] and m["status"] == "done" for m in s["messages"]
                    ))
                    self.assertEqual(snapshot["jobs"], [])
                    observed = [json.loads(m["content"]) for m in snapshot["messages"] if m["kind"] == "tool"]
                    self.assertEqual([r["error"]["code"] for r in observed], ["TOOL_DISABLED"])
                    self.assertNotIn("submit_job", [s["function"]["name"] for s in model.requests[0]["tools"]])
                    for name in ("job_status", "read_log"):
                        (await owner.post(f"/api/mcp/tools/{name}/enabled", json={"enabled": False})).raise_for_status()
                    self.assertEqual(app.state.tools.model_schemas(account["id"]), [])
                    self.assertEqual((await owner.post("/api/mcp/tools/missing/enabled",
                                                       json={"enabled": True})).status_code, 404)

            async with application(settings, model) as app:
                async with client_for(app) as owner:
                    await sign_in(owner, "tool-owner")
                    self.assertFalse(any(tool["enabled"] for tool in (await owner.get("/api/tools")).json()))
                    hello = await send(owner, room, "all-paused", "你好")
                    await until(owner, room, lambda s: any(
                        m["id"] == hello["id"] and m["status"] == "done" for m in s["messages"]
                    ))
                    self.assertEqual({tool["function"]["name"] for tool in model.requests[-1]["tools"]},
                                     {"get_output_preferences", "update_output_preferences"})
                    (await owner.post(toggle, json={"enabled": True})).raise_for_status()
                    self.assertEqual([tool["name"] for tool in (await owner.get("/api/tools")).json() if tool["enabled"]],
                                     ["submit_job"])

    async def test_log_pause_and_resume_on_completed_job(self):
        with TemporaryDirectory() as directory:
            model = ModelFixture()
            async with application(Settings(data_dir=Path(directory), api_key="fixture-key"), model) as app:
                async with client_for(app) as owner:
                    await sign_in(owner, "pause-summary")
                    room = await create_room(owner)
                    for name in ("job_status", "read_log"):
                        (await owner.post(f"/api/mcp/tools/{name}/enabled", json={"enabled": False})).raise_for_status()
                    await send(owner, room, "without-logs", "统计 sample1.fasta")
                    snapshot = await until(owner, room, lambda s: terminal(s, 1))
                    job = snapshot["jobs"][0]
                    self.assertEqual((job["state"], job["summary_state"]), ("succeeded", "error"))
                    self.assertIn("read_log", next(m["content"] for m in snapshot["messages"] if m["kind"] == "summary"))
                    self.assertFalse(any(m["kind"] == "invocation" and m["job_id"] == job["id"]
                                         for m in snapshot["messages"]))

                    (await owner.post("/api/mcp/tools/read_log/enabled", json={"enabled": True})).raise_for_status()
                    (await owner.post(f"{room}/jobs/{job['id']}/retry-summary")).raise_for_status()
                    resumed = await until(owner, room, lambda s: s["jobs"][0]["summary_state"] == "done")
                    self.assertEqual([j["id"] for j in resumed["jobs"]], [job["id"]])
                    calls = [call["function"]["name"] for m in resumed["messages"]
                             if m["kind"] == "invocation" and m["job_id"] == job["id"] for call in m["tool_calls"]]
                    self.assertEqual(calls, ["read_log"])
