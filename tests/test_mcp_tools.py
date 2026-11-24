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
                    account = await sign_in(client, "mcp-fixture", add_research=False)
                    self.assertEqual((await client.get("/api/tools")).json(), [])
                    configured = (await client.post("/api/mcp/research")).raise_for_status().json()
                    self.assertEqual(configured["tools"], ["submit_job", "job_status", "read_log"])
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

                    methods = [request.get("method") for request in requests]
                    self.assertIn("tools/list", methods)
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
