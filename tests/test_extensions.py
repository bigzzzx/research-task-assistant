import asyncio
from base64 import b64encode
from io import BytesIO
from contextlib import asynccontextmanager, closing, nullcontext
import json
import os
from pathlib import Path
import socket
import sys
from tempfile import TemporaryDirectory
from unittest import IsolatedAsyncioTestCase, TestCase
from zipfile import ZipFile
from unittest.mock import patch

import httpx
from mcp.client.stdio import StdioServerParameters
from mcp.server.mcpserver import MCPServer
import uvicorn

from backend.app.config import ROOT, Settings
from backend.app.extensions import ExtensionStore
from backend.app.store import Store
from tests.model_fixture import FixtureStream, ModelFixture, call_events, text_events
from tests.test_app import application, client_for, create_room, send, sign_in, until


def calculation_server(calls):
    async def calculate_total(values: list[int], multiplier: int = 1) -> dict[str, int]:
        calls.append((values, multiplier))
        return {"total": sum(values) * multiplier}

    server = MCPServer("test-remote-server", log_level="WARNING")
    server.add_tool(calculate_total)
    return server


@asynccontextmanager
async def remote_server(transport="streamable-http"):
    calls = []
    server = calculation_server(calls)
    sse = transport == "sse"
    app = server.sse_app() if sse else server.streamable_http_app(stateless_http=True, json_response=True)
    uvicorn_server = uvicorn.Server(uvicorn.Config(app, lifespan="off", log_level="warning"))
    with socket.socket() as listener:
        listener.bind(("127.0.0.1", 0))
        path = "sse" if sse else "mcp"
        address = f"http://127.0.0.1:{listener.getsockname()[1]}/{path}"
        async with nullcontext() if sse else server.session_manager.run():
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
                    account = await sign_in(owner, "remote-owner")
                    await sign_in(other, "remote-other")
                    add = "/api/mcp/servers"
                    self.assertEqual((await owner.post(add, json={
                        "name": "未知地址", "endpoint": "http://localhost:9999/mcp",
                    })).status_code, 422)
                    self.assertEqual((await owner.post(add, json={
                        "name": "未登记的内网服务", "endpoint": "https://127.0.0.1:9999/mcp",
                    })).status_code, 422)
                    self.assertEqual((await owner.get(add)).json()["servers"], [])
                    server_id = (await owner.post(add, json={
                        "name": "本机外部服务", "endpoint": endpoint,
                    })).raise_for_status().json()["id"]
                    server = f"{add}/{server_id}"
                    self.assertEqual((await other.post(server + "/refresh")).status_code, 404)
                    tool = next(tool for tool in (await owner.get("/api/tools")).json() if tool["server_id"] == server_id)
                    self.assertFalse(tool["enabled"])
                    self.assertEqual(tool["display_name"], "calculate_total")
                    name = tool["name"]
                    toggle = f"/api/mcp/tools/{name}/enabled"
                    self.assertTrue(all(tool["server_id"] is None for tool in (await other.get("/api/tools")).json()))
                    self.assertEqual((await other.get(add)).json()["servers"], [])
                    self.assertEqual((await other.post(toggle, json={"enabled": True})).status_code, 404)
                    await owner.post(toggle, json={"enabled": True})
                    await owner.post(server + "/refresh")
                    self.assertTrue(next(tool for tool in (await owner.get("/api/tools")).json()
                                         if tool["name"] == name)["enabled"])

                    room = await create_room(owner)
                    message = await answer(owner, room, "remote", "测试外部工具")
                    self.assertEqual(calls, [([2, 3, 7], 2)])
                    history = (await owner.get(room)).json()["messages"]
                    self.assertTrue(any("结果为 24" in item["content"] for item in history))
                    self.assertIn(name, {tool["function"]["name"] for tool in model.requests[0]["tools"]})

                    async def execute(arguments):
                        return await app.state.tools.execute(
                            account["id"], room.rsplit("/", 1)[1], message["id"], name, json.dumps(arguments),
                        )

                    wrong = await execute({"values": "1,2,3"})
                    self.assertEqual(wrong["error"]["code"], "INVALID_ARGUMENTS")
                    await owner.post(server + "/enabled", json={"enabled": False})
                    self.assertEqual((await execute({"values": [1]}))["error"]["code"], "UNKNOWN_TOOL")
                    await answer(owner, room, "disabled", "测试外部工具")
                    self.assertNotIn(name, {tool["function"]["name"] for tool in model.requests[-1]["tools"]})
                    await owner.post(server + "/enabled", json={"enabled": True})
                    await owner.post(toggle, json={"enabled": False})
                    self.assertEqual((await execute({"values": [1]}))["error"]["code"], "UNKNOWN_TOOL")
                    self.assertEqual(len(calls), 1)

    async def assert_transport_can_call_tool(self, settings, endpoint, transport):
        async with application(settings, RemoteModel()) as app:
            async with client_for(app) as owner:
                await sign_in(owner, f"{transport}-owner")
                added = (await owner.post("/api/mcp/servers", json={
                    "name": "外部计算器", "transport": transport, "endpoint": endpoint,
                })).raise_for_status().json()
                self.assertEqual(added["tool_count"], 1)
                settings_view = (await owner.get("/api/mcp/servers")).json()
                self.assertEqual(settings_view["research"]["tool_count"], 3)
                self.assertEqual(settings_view["servers"][0]["transport"], transport)
                tool = next(tool for tool in (await owner.get("/api/tools")).json() if tool["server_id"] == added["id"])
                self.assertEqual(tool["transport"], transport)
                await owner.post(f"/api/mcp/tools/{tool['name']}/enabled", json={"enabled": True})
                room = await create_room(owner)
                await answer(owner, room, "external-call", "测试外部工具")
                self.assertTrue(any("结果为 24" in item["content"]
                                    for item in (await owner.get(room)).json()["messages"]))

    async def test_legacy_sse_discovery_and_call(self):
        async with remote_server("sse") as (endpoint, calls):
            settings = Settings(data_dir=self.data_dir, api_key="fixture-key", mcp_http_endpoints=(endpoint,))
            await self.assert_transport_can_call_tool(settings, endpoint, "sse")
            self.assertEqual(calls, [([2, 3, 7], 2)])

    async def test_registered_external_stdio_discovery_and_call(self):
        parameters = StdioServerParameters(
            command=sys.executable, args=["-X", "utf8", "-m", "tests.test_extensions"], cwd=ROOT,
        )
        settings = Settings(data_dir=self.data_dir, api_key="fixture-key", mcp_stdio_servers={"calculator": parameters})
        await self.assert_transport_can_call_tool(settings, "calculator", "stdio")

    async def test_stdio_profiles_reload_without_a_backend_restart(self):
        env_file = self.data_dir / ".env"
        profiles = {"calculator": {
            "command": Path(sys.executable).as_posix(),
            "args": ["-X", "utf8", "-m", "tests.test_extensions"],
            "cwd": ROOT.as_posix(),
        }}
        with (
            patch("backend.app.config.ROOT", self.data_dir),
            patch.dict(os.environ, {"APP_DATA_DIR": "workspace", "DEEPSEEK_API_KEY": "fixture-key"}),
        ):
            os.environ.pop("MCP_STDIO_SERVERS", None)
            async with application(None, RemoteModel()) as app:
                async with client_for(app) as owner:
                    await sign_in(owner, "stdio-reload-owner")
                    path = "/api/mcp/servers"
                    self.assertEqual((await owner.get(path)).raise_for_status().json()["stdio_servers"], [])

                    env_file.write_text(f"MCP_STDIO_SERVERS='{json.dumps(profiles)}'\n", encoding="utf-8")
                    refreshed = (await owner.get(path)).raise_for_status().json()
                    self.assertEqual(refreshed["stdio_servers"], ["calculator"])
                    self.assertIsNone(refreshed["stdio_config_error"])
                    added = (await owner.post(path, json={
                        "name": "计算器", "transport": "stdio", "endpoint": "calculator",
                    })).raise_for_status().json()
                    tool = next(tool for tool in (await owner.get("/api/tools")).json() if tool["server_id"] == added["id"])
                    toggle = f"/api/mcp/tools/{tool['name']}/enabled"
                    (await owner.post(toggle, json={"enabled": True})).raise_for_status()
                    room = await create_room(owner)
                    await answer(owner, room, "hot-stdio", "测试外部工具")
                    history = (await owner.get(room)).raise_for_status().json()["messages"]
                    self.assertTrue(any("结果为 24" in message["content"] for message in history))

                    env_file.write_text("MCP_STDIO_SERVERS='{\n", encoding="utf-8")
                    malformed = (await owner.get(path)).raise_for_status().json()
                    self.assertEqual(malformed["stdio_servers"], ["calculator"])
                    self.assertIn("MCP_STDIO_SERVERS", malformed["stdio_config_error"])

                    env_file.write_text("MCP_STDIO_SERVERS='{}'\n", encoding="utf-8")
                    refresh = await owner.post(f"{path}/{added['id']}/refresh")
                    self.assertEqual(refresh.status_code, 422)
                    removed = (await owner.get(path)).raise_for_status().json()
                    self.assertEqual(removed["stdio_servers"], [])
                    self.assertIsNone(removed["stdio_config_error"])
                    self.assertFalse(removed["servers"][0]["available"])

    async def test_skill_import_activation_resources_and_account_isolation(self):
        name = "research-result-report"
        marker = "SKILL_BODY_ONLY"
        document = f"---\nname: {name}\ndescription: 整理科研任务的实验记录或计算结果汇总\n---\n\n{marker}"
        resource = "references/outline.md"
        encoded = b64encode(b"RESOURCE_ONLY").decode()

        class SkillModel(ModelFixture):
            def __call__(self, request):
                body = json.loads(request.content)
                self.requests.append(body)
                tools = {item["function"]["name"]: item["function"] for item in body.get("tools", [])}
                question = next(m["content"] for m in reversed(body["messages"]) if m["role"] == "user")
                observations = [json.loads(m["content"]) for m in body["messages"] if m["role"] == "tool"]
                if observations and observations[-1].get("skill_name"):
                    events = call_events([("read_skill_resource", {"name": name, "path": resource})])
                elif observations and observations[-1].get("output") == "RESOURCE_ONLY":
                    events = text_events("已按技能和参考资料整理。")
                elif "activate_skill" in tools and question in ("把刚才的分析整理成一份实验记录", "汇总本次计算结果"):
                    events = call_events([("activate_skill", {"name": name})])
                else:
                    events = text_events("普通回复。")
                return httpx.Response(200, headers={"Content-Type": "text/event-stream"}, stream=FixtureStream(events, 0))

        model = SkillModel()
        async with application(Settings(data_dir=self.data_dir, api_key="fixture-key"), model) as app:
            async with client_for(app) as owner, client_for(app) as other:
                await sign_in(owner, "skill-owner")
                await sign_in(other, "skill-other")
                payload = {"folder": name, "document": document, "files": {resource: encoded}, "enabled": True}
                for invalid in ({**payload, "folder": "wrong-name"}, {**payload, "files": {"../escape": encoded}}):
                    self.assertEqual((await owner.post("/api/skill-bundles", json=invalid)).status_code, 422)
                skill_id = (await owner.post("/api/skill-bundles", json=payload)).raise_for_status().json()["id"]
                skill_path = "/api/skills/" + skill_id
                self.assertEqual((await other.get("/api/skills")).json(), [])
                self.assertEqual((await other.get(skill_path)).status_code, 404)
                self.assertEqual((await other.get(skill_path + "/bundle")).status_code, 404)
                self.assertEqual((await other.post(skill_path + "/enabled", json={"enabled": False})).status_code, 404)
                self.assertEqual((await owner.post("/api/skills",
                                                   json={"document": document, "enabled": True})).status_code, 409)
                detail = (await owner.get(skill_path)).json()
                self.assertEqual(detail["files"], [resource])
                self.assertEqual(detail["document"], document)
                with ZipFile(BytesIO((await owner.get(skill_path + "/bundle")).raise_for_status().content)) as archive:
                    self.assertEqual(archive.read(f"{name}/SKILL.md").decode(), document)
                    self.assertEqual(archive.read(f"{name}/{resource}"), b"RESOURCE_ONLY")

                for index, question in enumerate(("把刚才的分析整理成一份实验记录", "汇总本次计算结果")):
                    room = await create_room(owner)
                    first = len(model.requests)
                    message = await answer(owner, room, f"skill-{index}", question)
                    at_discovery, at_activation, at_resource = model.requests[first:]
                    self.assertIn(name, json.dumps(at_discovery["tools"]))
                    self.assertNotIn(marker, json.dumps(at_discovery))
                    self.assertIn(marker, json.dumps(at_activation))
                    self.assertNotIn("RESOURCE_ONLY", json.dumps(at_activation))
                    self.assertIn("RESOURCE_ONLY", json.dumps(at_resource))
                    self.assertEqual(app.state.store.activated_skill_ids(room.rsplit("/", 1)[1], message["id"]), [skill_id])
                    self.assertEqual((await owner.get(room)).json()["messages"][0]["content"], question)

                await answer(owner, await create_room(owner), "unrelated", "你好")
                self.assertNotIn(marker, json.dumps(model.requests[-1]))
                await answer(other, await create_room(other), "foreign", "汇总本次计算结果")
                self.assertNotIn("activate_skill", json.dumps(model.requests[-1].get("tools")))
                await answer(owner, await create_room(owner), "manual", f"/skill {name} 帮我整理一下")
                self.assertIn(marker, model.requests[-1]["messages"][0]["content"])
                (await owner.post(skill_path + "/enabled", json={"enabled": False})).raise_for_status()
                for index, question in enumerate(("把刚才的分析整理成一份实验记录", "汇总本次计算结果")):
                    await answer(owner, await create_room(owner), f"disabled-{index}", question)
                    self.assertNotIn("activate_skill", json.dumps(model.requests[-1].get("tools")))
                    self.assertNotIn(marker, json.dumps(model.requests[-1]))


class ExtensionMigrationTests(TestCase):
    def test_legacy_skill_preserves_content_and_enabled_state(self):
        with TemporaryDirectory() as temp, closing(Store(Path(temp) / "test.sqlite3", ROOT / "data")) as store:
            owner = store.create_user("legacy", "legacy", "test-only")["id"]
            skill_id = "f" * 32
            with store.db:
                store.db.execute("""
                    CREATE TABLE skills (
                        id TEXT PRIMARY KEY, owner_id TEXT, name TEXT, trigger TEXT,
                        trigger_key TEXT, instructions TEXT, enabled INTEGER
                    )
                """)
                store.db.execute("INSERT INTO skills VALUES (?, ?, ?, ?, ?, ?, ?)",
                                 (skill_id, owner, "科研报告", "生成科研报告", "生成科研报告", "按四节输出。", 0))
            extensions = ExtensionStore(store.db)
            skill = extensions.skill(owner, skill_id=skill_id)
            self.assertFalse(skill["enabled"])
            self.assertIn("生成科研报告", skill["description"])
            self.assertIn("按四节输出。", skill["document"])
            extensions.set_skill_enabled(owner, skill_id, True)
            self.assertTrue(ExtensionStore(store.db).skill(owner, skill_id=skill_id)["enabled"])

    def test_existing_http_connections_keep_their_transport_after_upgrade(self):
        with TemporaryDirectory() as temp, closing(Store(Path(temp) / "test.sqlite3", ROOT / "data")) as store:
            old = store.create_user("old", "old", "test-only")["id"]
            with store.db:
                store.db.execute("""
                    CREATE TABLE mcp_servers (
                        id TEXT PRIMARY KEY, owner_id TEXT NOT NULL REFERENCES users(id),
                        name TEXT NOT NULL, endpoint TEXT NOT NULL, enabled INTEGER NOT NULL,
                        synced_at TEXT NOT NULL, UNIQUE(owner_id, endpoint)
                    )
                """)
                store.db.execute(
                    "INSERT INTO mcp_servers VALUES (?, ?, ?, ?, ?, ?)",
                    ("saved", old, "已配置的服务", "https://example.com/mcp", 0, "2026-11-23"),
                )
            extensions = ExtensionStore(store.db)
            saved = extensions.servers(old)[0]
            self.assertEqual(saved["id"], "saved")
            self.assertEqual(saved["transport"], "streamable-http")
            self.assertFalse(saved["enabled"])


if __name__ == "__main__":
    calculation_server([]).run(transport="stdio")
