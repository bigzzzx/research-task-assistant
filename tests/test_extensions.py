import asyncio
from contextlib import asynccontextmanager, closing
import json
from pathlib import Path
import socket
from tempfile import TemporaryDirectory
from unittest import IsolatedAsyncioTestCase, TestCase

import httpx
from mcp.server.mcpserver import MCPServer
import uvicorn

from backend.app.config import ROOT, Settings
from backend.app.extensions import ExtensionStore
from backend.app.store import Store
from tests.model_fixture import FixtureStream, ModelFixture, call_events, text_events
from tests.test_app import application, client_for, create_room, send, sign_in, until


@asynccontextmanager
async def remote_server():
    calls = []

    async def calculate_total(values: list[int], multiplier: int = 1) -> dict[str, int]:
        calls.append((values, multiplier))
        return {"total": sum(values) * multiplier}

    server = MCPServer("test-remote-server", log_level="WARNING")
    server.add_tool(calculate_total)
    app = server.streamable_http_app(stateless_http=True, json_response=True)
    uvicorn_server = uvicorn.Server(uvicorn.Config(app, lifespan="off", log_level="warning"))
    with socket.socket() as listener:
        listener.bind(("127.0.0.1", 0))
        address = f"http://127.0.0.1:{listener.getsockname()[1]}/mcp"
        async with server.session_manager.run():
            worker = asyncio.create_task(uvicorn_server.serve(sockets=[listener]))
            try:
                async with asyncio.timeout(10):
                    while not uvicorn_server.started:
                        await asyncio.sleep(0.01)
                yield address, calls
            finally:
                uvicorn_server.should_exit = True
                await worker


class RemoteModel(ModelFixture):
    def __call__(self, request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        question = next(item["content"] for item in reversed(body["messages"]) if item["role"] == "user")
        if "测试外部工具" not in question:
            return super().__call__(request)
        self.requests.append(body)
        observations = [json.loads(item["content"]) for item in body["messages"] if item["role"] == "tool"]
        remote = next((item["function"]["name"] for item in body.get("tools", ())
                       if item["function"]["name"].startswith("mcp_")), None)
        if observations:
            result = json.loads(observations[-1]["output"])
            events = text_events(f"外部计算结果为 {result['total']}。")
        elif remote:
            events = call_events([(remote, {"values": [2, 3, 7], "multiplier": 2})])
        else:
            events = text_events("当前工具列表里没有该工具。")
        return httpx.Response(200, headers={"Content-Type": "text/event-stream"}, stream=FixtureStream(events, 0))


async def answer(client, room, key, question):
    message = await send(client, room, key, question)
    await until(client, room, lambda snapshot: any(item["id"] == message["id"] and item["status"] == "done"
                                                    for item in snapshot["messages"]))
    return message


class ExtensionTests(IsolatedAsyncioTestCase):
    def setUp(self):
        temp = TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.data_dir = Path(temp.name)

    async def test_real_http_discovery_tool_schema_and_account_switches(self):
        async with remote_server() as (endpoint, calls):
            model = RemoteModel()
            settings = Settings(data_dir=self.data_dir, api_key="fixture-key", mcp_http_endpoints=(endpoint,))
            async with application(settings, model) as app:
                async with client_for(app) as owner, client_for(app) as other:
                    account = await sign_in(owner, "remote-owner", add_research=False)
                    await sign_in(other, "remote-other", add_research=False)
                    add = "/api/mcp/servers"
                    self.assertEqual((await owner.post(add, json={
                        "name": "未知地址", "endpoint": "http://localhost:9999/mcp",
                    })).status_code, 422)
                    server_id = (await owner.post(add, json={
                        "name": "本机外部服务", "endpoint": endpoint,
                    })).raise_for_status().json()["id"]
                    server = f"{add}/{server_id}"
                    self.assertEqual((await other.post(server + "/refresh")).status_code, 404)
                    tool = (await owner.get("/api/tools")).json()[0]
                    self.assertFalse(tool["enabled"])
                    self.assertEqual(tool["display_name"], "calculate_total")
                    name = tool["name"]
                    toggle = f"/api/mcp/tools/{name}/enabled"
                    self.assertEqual((await other.get("/api/tools")).json(), [])
                    self.assertEqual((await other.post(toggle, json={"enabled": True})).status_code, 404)
                    await owner.post(toggle, json={"enabled": True})
                    await owner.post(server + "/refresh")
                    self.assertTrue((await owner.get("/api/tools")).json()[0]["enabled"])

                    room = await create_room(owner)
                    message = await answer(owner, room, "remote", "测试外部工具")
                    self.assertEqual(calls, [([2, 3, 7], 2)])
                    history = (await owner.get(room)).json()["messages"]
                    self.assertTrue(any("结果为 24" in item["content"] for item in history))
                    self.assertEqual(model.requests[0]["tools"][0]["function"]["name"], name)

                    async def execute(arguments):
                        return await app.state.tools.execute(
                            account["id"], room.rsplit("/", 1)[1], message["id"], name, json.dumps(arguments),
                        )

                    wrong = await execute({"values": "1,2,3"})
                    self.assertEqual(wrong["error"]["code"], "INVALID_ARGUMENTS")
                    await owner.post(server + "/enabled", json={"enabled": False})
                    self.assertEqual((await execute({"values": [1]}))["error"]["code"], "UNKNOWN_TOOL")
                    await answer(owner, room, "disabled", "测试外部工具")
                    self.assertEqual(model.requests[-1].get("tools", []), [])
                    await owner.post(server + "/enabled", json={"enabled": True})
                    await owner.post(toggle, json={"enabled": False})
                    self.assertEqual((await execute({"values": [1]}))["error"]["code"], "UNKNOWN_TOOL")
                    self.assertEqual(len(calls), 1)

    async def test_text_skill_only_enters_the_matching_accounts_turn(self):
        model = ModelFixture()
        marker = "测试模板要求四个小节。"
        async with application(Settings(data_dir=self.data_dir, api_key="fixture-key"), model) as app:
            async with client_for(app) as owner, client_for(app) as other:
                await sign_in(owner, "skill-owner")
                await sign_in(other, "skill-other")
                data = {"name": "科研报告", "trigger": "生成科研结果报告", "instructions": marker, "enabled": True}
                skill = (await owner.post("/api/skills", json=data)).raise_for_status().json()["id"]
                self.assertEqual((await other.get("/api/skills")).json(), [])
                self.assertEqual((await other.post("/api/skills/" + skill, json=data)).status_code, 404)
                room = await create_room(owner)
                await answer(owner, room, "match", "请生成科研结果报告")
                self.assertIn(marker, model.requests[-1]["messages"][-1]["content"])
                self.assertEqual((await owner.get(room)).json()["messages"][0]["content"], "请生成科研结果报告")
                await answer(owner, room, "mismatch", "你好")
                self.assertNotIn(marker, model.requests[-1]["messages"][-1]["content"])
                await owner.post("/api/skills/" + skill, json={**data, "enabled": False})
                await answer(owner, room, "inactive", "请生成科研结果报告")
                self.assertNotIn(marker, model.requests[-1]["messages"][-1]["content"])


class ExtensionMigrationTests(TestCase):
    def test_existing_accounts_retain_research_tools_after_upgrade(self):
        with TemporaryDirectory() as temp, closing(Store(Path(temp) / "test.sqlite3", ROOT / "data")) as store:
            old = store.create_user("old", "old", "test-only")["id"]
            extensions = ExtensionStore(store.db)
            new = store.create_user("new", "new", "test-only")["id"]
            self.assertTrue(extensions.has_research_server(old))
            self.assertFalse(ExtensionStore(store.db).has_research_server(new))
