import json
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest import IsolatedAsyncioTestCase
from unittest.mock import patch

from httpx2 import ASGITransport

from backend.app.config import Settings
from backend.app.mcp_tools import SCOPE_KEY
from tests.model_fixture import ModelFixture
from tests.test_app import application, client_for, create_room, send, sign_in, terminal, until


class MCPToolsTests(IsolatedAsyncioTestCase):
    async def test_chat_discovers_and_calls_tools_over_mcp(self):
        requests = []

        class RecordingTransport(ASGITransport):
            async def handle_async_request(self, request):
                if request.method == "POST":
                    requests.append(json.loads(await request.aread()))
                return await super().handle_async_request(request)

        model = ModelFixture()
        with TemporaryDirectory() as directory, patch("backend.app.mcp_tools.ASGITransport", RecordingTransport):
            settings = Settings(data_dir=Path(directory), api_key="fixture-key")
            async with application(settings, model) as app:
                async with client_for(app) as client:
                    account = await sign_in(client, "mcp-fixture")
                    room = await create_room(client)
                    message = await send(client, room, "mcp-run", "统计 sample1.fasta 的 GC 含量")
                    snapshot = await until(client, room, lambda result: terminal(result, 1))
                    job = snapshot["jobs"][0]
                    self.assertEqual(job["state"], "succeeded")
                    self.assertEqual(job["summary_state"], "done")
                    self.assertEqual((await client.get("/mcp")).status_code, 404)
                    catalog = (await client.get("/api/tools")).raise_for_status().json()
                    self.assertEqual([tool["name"] for tool in catalog], ["submit_job", "job_status", "read_log"])
                    self.assertEqual([set(tool["parameters"]["properties"]) for tool in catalog],
                                     [{"analysis_type", "filename"}, {"job_id"}, {"job_id"}])

                    rejected = await app.state.tools.execute(
                        account["id"], room.rsplit("/", 1)[1], message["id"], "read_log",
                        json.dumps({"job_id": job["id"], "owner_id": account["id"]}),
                    )
                    self.assertEqual(rejected["error"]["code"], "INVALID_ARGUMENTS")

                    methods = [request["method"] for request in requests]
                    self.assertIn("tools/list", methods)
                    calls = [request["params"] for request in requests if request["method"] == "tools/call"]
                    self.assertEqual([call["name"] for call in calls], ["submit_job", "job_status", "read_log"])
                    for index, call in enumerate(calls):
                        self.assertEqual(call["_meta"][SCOPE_KEY], {
                            "owner_id": account["id"], "conversation_id": room.rsplit("/", 1)[1],
                            "turn_id": message["id"], "only_job": job["id"] if index else None,
                        })
                    self.assertEqual(calls[0]["arguments"], {"analysis_type": "seq_stats", "filename": "sample1.fasta"})
                    self.assertEqual(calls[2]["arguments"], {"job_id": job["id"]})
                    self.assertNotIn(SCOPE_KEY, json.dumps(model.requests))
