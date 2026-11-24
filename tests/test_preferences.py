import json
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest import IsolatedAsyncioTestCase, TestCase

from backend.app.config import Settings
from backend.app.preferences import remembered_output_preferences, requested_gc_decimals
from tests.model_fixture import ModelFixture
from tests.test_app import application, client_for, create_room, send, sign_in, terminal, until

PREFERENCES = {"result_order": "sequence_id", "gc_decimal_places": 0}
DEFAULT_PREFERENCES = {"result_order": None, "gc_decimal_places": None}


class RememberedPreferenceTests(TestCase):
    def test_only_explicit_and_supported_defaults_are_saved(self):
        cases = {
            "以后统计 GC 保留一位小数，这次用两位小数。": {"gc_decimal_places": 1},
            "这次 GC 用两位小数，以后保留 0 位小数。": {"gc_decimal_places": 0},
            "记住，按原文件顺序展示结果。": {"result_order": "file"},
            "GC 保留０位小数设为默认。": {"gc_decimal_places": 0},
            "以后 GC 保留 1 位小数，执行时间保留 2 位小数。": {"gc_decimal_places": 1},
            "这次 GC 保留两位小数，结果按文件顺序展示。": {},
            "别记住这项，以后 GC 保留两位小数。": {},
            "你还记得我说过以后 GC 保留两位小数吗？": {},
            "请举一个‘以后 GC 保留一位小数’的例子。": {},
            "如果用户说以后 GC 保留一位小数，该怎么回？": {},
            "以后序列长度保留两位小数。": {},
            "以后 GC 保留二十一位小数。": {},
            "以后按序列 ID 降序展示结果。": {},
        }
        for question, expected in cases.items():
            with self.subTest(question=question):
                self.assertEqual(remembered_output_preferences(question), expected)

    def test_current_gc_precision_accepts_local_override(self):
        cases = {
            "统计 sample1 的 GC，保留一位小数。": 1,
            "以后用两位小数，这次保留 0 位小数。": 0,
            "这次先用两位小数，以后保留一位小数。": 2,
            "以后统计 GC 保留 1 位小数。": None,
            "下次保留一位小数。": None,
            "本轮 GC 保留一位小数，耗时保留两位小数。": 1,
            "请举一个 GC 保留一位小数的例子。": None,
        }
        for question, expected in cases.items():
            with self.subTest(question=question):
                self.assertEqual(requested_gc_decimals(question), expected)


class PreferenceTests(IsolatedAsyncioTestCase):
    def setUp(self):
        temporary = TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.settings = Settings(data_dir=Path(temporary.name), api_key="fixture-key")
        self.model = ModelFixture()

    async def test_chat_preferences_survive_restart_and_only_apply_when_relevant_to_the_owner(self):
        async with application(self.settings, self.model) as app:
            async with client_for(app) as first, client_for(app) as second:
                self.assertEqual((await first.get("/api/preferences")).status_code, 401)
                await sign_in(first, "memory-alice")
                await sign_in(second, "memory-bob")
                self.assertEqual((await first.get("/api/preferences")).json(), DEFAULT_PREFERENCES)

                prompt = await self._chat_prompt(first, "以后统计 GC 时保留 0 位小数，这次先用两位。")
                self.assertIn("本轮已保存为账号偏好", prompt)
                self.assertIn("保留 0 位小数", prompt)
                self.assertEqual((await first.get("/api/preferences")).json(), {
                    "result_order": None, "gc_decimal_places": 0,
                })

                prompt = await self._chat_prompt(first, "记住，以后按序列 ID 升序展示结果。")
                self.assertIn("本轮已保存为账号偏好", prompt)
                self.assertIn("按序列 ID 升序展示", prompt)
                self.assertEqual((await first.get("/api/preferences")).json(), PREFERENCES)

                prompt = await self._chat_prompt(first, "这次 GC 用两位小数，按文件顺序展示。")
                self.assertNotIn("本轮已保存为账号偏好", prompt)
                self.assertEqual((await first.get("/api/preferences")).json(), PREFERENCES)
                self.assertEqual((await second.get("/api/preferences")).json(), DEFAULT_PREFERENCES)
                self.assertEqual((await first.post("/api/preferences", json={
                    **PREFERENCES, "gc_decimal_places": 3,
                })).status_code, 422)
                self.assertEqual((await first.get("/api/preferences")).json(), PREFERENCES)

                prompt = await self._chat_prompt(second, "统计 GC 时怎么展示结果？")
                self.assertNotIn("账号偏好用于默认展示", prompt)

        async with application(self.settings, self.model) as app:
            async with client_for(app) as first:
                await sign_in(first, "memory-alice")
                self.assertEqual((await first.get("/api/preferences")).json(), PREFERENCES)
                prompt = await self._chat_prompt(first, "统计 GC 时怎么展示结果？")
                self.assertIn("按序列 ID 升序展示", prompt)
                self.assertIn("保留 0 位小数", prompt)

                prompt = await self._chat_prompt(first, "查找最长 ORF 时怎么展示结果？")
                self.assertIn("按序列 ID 升序展示", prompt)
                self.assertNotIn("保留 0 位小数", prompt)

                prompt = await self._chat_prompt(first, "你好")
                self.assertNotIn("账号偏好用于默认展示", prompt)

                self.assertEqual((await first.post("/api/preferences", json=DEFAULT_PREFERENCES)).json(), DEFAULT_PREFERENCES)
                prompt = await self._chat_prompt(first, "统计 GC 时怎么展示结果？")
                self.assertNotIn("账号偏好用于默认展示", prompt)

    async def _chat_prompt(self, client, question):
        conversation = await create_room(client)
        request_count = len(self.model.requests)
        await send(client, conversation, "preference-check", question)
        await until(client, conversation, lambda response: any(
            message["kind"] == "assistant" and message["status"] == "done" for message in response["messages"]
        ))
        return self.model.requests[request_count]["messages"][0]["content"]

    async def test_job_follow_up_receives_preferences_and_original_request(self):
        async with application(self.settings, self.model) as app:
            async with client_for(app) as client:
                await sign_in(client, "memory-task")
                self.assertEqual((await client.post("/api/preferences", json=PREFERENCES)).status_code, 200)
                conversation = await create_room(client)
                file = await client.post("/api/files?filename=precision.fa", content=b">precision\n" + b"G" * 15 + b"AAAA\n")
                self.assertEqual(file.status_code, 201)
                question = "统计 precision.fa 的 GC，这次保留一位小数。"
                await send(client, conversation, "task-preference-check", question)
                snapshot = await until(client, conversation, lambda response: terminal(response, 1))
                self.assertEqual(snapshot["jobs"][0]["state"], "succeeded")
                follow_up = next(
                    request for request in self.model.requests
                    if len(request["messages"]) >= 2 and request["messages"][1]["content"].startswith("系统事件")
                )
                self.assertIn("保留 0 位小数", follow_up["messages"][0]["content"])
                self.assertIn(question, follow_up["messages"][1]["content"])
                log = json.loads([item for item in snapshot["messages"] if item["kind"] == "tool"][-1]["content"])
                raw = json.loads(log["stdout"].splitlines()[0])
                self.assertEqual((raw["gc_count"], raw["known_bases"], raw["gc_pct"]), (15, 19, 78.95))
                self.assertEqual(json.loads(log["display_stdout"].splitlines()[0])["gc_display"], "78.9%")
                summary = next(item["content"] for item in snapshot["messages"] if item["kind"] == "summary")
                self.assertIn("78.9%", summary)
                self.assertEqual((await client.get("/api/preferences")).json(), PREFERENCES)
