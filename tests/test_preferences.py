import asyncio
import json
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest import IsolatedAsyncioTestCase, TestCase

import httpx

from backend.app.config import Settings
from backend.app.preferences import GC_DECIMALS, temporary_output_preferences
from tests.model_fixture import FixtureStream, ModelFixture, call_events, text_events
from tests.test_app import application, client_for, create_room, send, sign_in, terminal, until

PREFERENCES = {"result_order": "sequence_id", "gc_decimal_places": 0}
DEFAULT_PREFERENCES = {"result_order": None, "gc_decimal_places": None}


class PreferenceModel(ModelFixture):
    def __init__(self):
        super().__init__()
        self.calls_by_question = {}

    def __call__(self, request):
        body = json.loads(request.content)
        messages = body["messages"]
        question = next(item["content"] for item in reversed(messages) if item["role"] == "user")
        calls = self.calls_by_question.get(question)
        observations = [json.loads(item["content"]) for item in messages if item["role"] == "tool"]
        if calls and not observations:
            self.requests.append(body)
            events = call_events(calls)
        elif observations and any("preferences" in item or item.get("error", {}).get("code") == "INVALID_PREFERENCES"
                                  for item in observations) and not any("job_id" in item for item in observations):
            self.requests.append(body)
            events = text_events("已读取工具返回的账号设置。")
        else:
            return super().__call__(request)
        return httpx.Response(200, headers={"Content-Type": "text/event-stream"}, stream=FixtureStream(events, 0))


class TemporaryPreferenceTests(TestCase):
    def test_current_gc_precision_accepts_local_override(self):
        cases = {
            "统计 sample1 的 GC，保留一位小数。": 1,
            "以后用两位小数，这次保留 0 位小数。": 0,
            "这次先用两位小数，以后保留一位小数。": 2,
            "以后统计 GC 保留 1 位小数。": None,
            "下次保留一位小数。": None,
            "本轮 GC 保留一位小数，耗时保留两位小数。": 1,
            "请举一个 GC 保留一位小数的例子。": None,
            "这次 GC 保留三位小数。": 3,
        }
        for question, expected in cases.items():
            with self.subTest(question=question):
                self.assertEqual(temporary_output_preferences(question, gc=True).get(GC_DECIMALS), expected)


class PreferenceTests(IsolatedAsyncioTestCase):
    def setUp(self):
        temporary = TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.settings = Settings(data_dir=Path(temporary.name), api_key="fixture-key")
        self.model = PreferenceModel()

    async def test_chat_preferences_survive_restart_and_only_apply_when_relevant_to_the_owner(self):
        async with application(self.settings, self.model) as app:
            async with client_for(app) as first, client_for(app) as second:
                self.assertEqual((await first.get("/api/preferences")).status_code, 401)
                await sign_in(first, "memory-alice")
                await sign_in(second, "memory-bob")
                self.assertEqual((await first.get("/api/preferences")).json(), DEFAULT_PREFERENCES)

                question = "小数点右边的数字，以后 GC 报告都省略吧。"
                self.model.calls_by_question[question] = [("update_output_preferences", {"gc_decimal_places": 0})]
                await self._chat_prompt(first, question)
                self.assertEqual((await first.get("/api/preferences")).json(), {
                    "result_order": None, "gc_decimal_places": 0,
                })

                question = "记住，以后按序列 ID 升序展示结果。"
                self.model.calls_by_question[question] = [("update_output_preferences", {"result_order": "sequence_id"})]
                await self._chat_prompt(first, question)
                self.assertEqual((await first.get("/api/preferences")).json(), PREFERENCES)

                for question in (
                    "这次 GC 用两位小数，按文件顺序展示。",
                    "能不能在报告里给我三位小数？",
                    "举例说明‘以后 GC 保留两位小数’的含义。",
                ):
                    await self._chat_prompt(first, question)
                    self.assertEqual((await first.get("/api/preferences")).json(), PREFERENCES)
                for args in ({"gc_decimal_places": True}, {"gc_decimal_places": 3, "result_order": "file"},
                             {"result_order": "file", "owner_id": "memory-bob"}):
                    question = "测试提交非法偏好工具参数"
                    self.model.calls_by_question[question] = [("update_output_preferences", args)]
                    await self._chat_prompt(first, question)
                    self.assertIn("INVALID_PREFERENCES", self.model.requests[-1]["messages"][-1]["content"])
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
                question = "我平常让你把 GC 百分比写到小数点后第几位？"
                self.model.calls_by_question[question] = [("get_output_preferences", {})]
                await self._chat_prompt(first, question)
                answer_context = json.loads(self.model.requests[-1]["messages"][-1]["content"])
                self.assertEqual(answer_context["preferences"], PREFERENCES)
                prompt = await self._chat_prompt(first, "统计 GC 时怎么展示结果？")
                self.assertIn("按序列 ID 升序展示", prompt)
                self.assertIn("保留 0 位小数", prompt)

                prompt = await self._chat_prompt(first, "查找最长 ORF 时怎么展示结果？")
                self.assertIn("按序列 ID 升序展示", prompt)
                self.assertNotIn("保留 0 位小数", prompt)

                prompt = await self._chat_prompt(first, "你好")
                self.assertNotIn("账号偏好用于默认展示", prompt)

                reset = "把账号的 GC 小数位恢复成系统默认。"
                self.model.calls_by_question[reset] = [("update_output_preferences", {"gc_decimal_places": None})]
                await self._chat_prompt(first, reset)
                self.assertEqual((await first.get("/api/preferences")).json(),
                                 {"result_order": "sequence_id", "gc_decimal_places": None})
                reset = "把排序和 GC 小数位都恢复成系统默认。"
                self.model.calls_by_question[reset] = [("update_output_preferences", DEFAULT_PREFERENCES)]
                await self._chat_prompt(first, reset)
                self.assertEqual((await first.get("/api/preferences")).json(), DEFAULT_PREFERENCES)
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
                completed = app.state.agent._complete
                release_report = asyncio.Event()

                async def wait_for_profile_edit(job):
                    await release_report.wait()
                    await completed(job)

                app.state.agent._complete = wait_for_profile_edit
                file = await client.post("/api/files?filename=precision.fa",
                                         content=b">zeta\n" + b"G" * 15 + b"AAAA\n>alpha\nACGT\n")
                self.assertEqual(file.status_code, 201)
                question = "以后 GC 保留一位小数，统计 precision.fa 的 GC，这次保留三位小数。"
                self.model.calls_by_question[question] = [
                    ("submit_job", {"analysis_type": "seq_stats", "filename": "precision.fa"}),
                    ("update_output_preferences", {"gc_decimal_places": 1}),
                ]
                await send(client, conversation, "task-preference-check", question)
                submitted = await until(client, conversation, lambda response: bool(response["jobs"]))
                job = submitted["jobs"][0]
                self.assertEqual((job["output_result_order"], job["output_gc_decimal_places"]), ("sequence_id", 3))
                outcomes = [json.loads(item["content"]) for item in submitted["messages"] if item["kind"] == "tool"]
                self.assertEqual(outcomes[0]["saved"], {"gc_decimal_places": 1})
                self.assertEqual((await client.get("/api/preferences")).json(), {**PREFERENCES, "gc_decimal_places": 1})
                changed = {"result_order": "file", "gc_decimal_places": 2}
                (await client.post("/api/preferences", json=changed)).raise_for_status()
                release_report.set()
                snapshot = await until(client, conversation, lambda response: terminal(response, 1))
                self.assertEqual(snapshot["jobs"][0]["state"], "succeeded")
                follow_up = next(
                    request for request in self.model.requests
                    if len(request["messages"]) >= 2 and request["messages"][1]["content"].startswith("系统事件")
                )
                self.assertIn("保留 3 位小数", follow_up["messages"][0]["content"])
                self.assertIn(question, follow_up["messages"][1]["content"])
                log = json.loads([item for item in snapshot["messages"] if item["kind"] == "tool"][-1]["content"])
                raw = json.loads(log["stdout"].splitlines()[0])
                self.assertEqual((raw["gc_count"], raw["known_bases"], raw["gc_pct"]), (15, 19, 78.95))
                rows = [json.loads(line) for line in log["display_stdout"].splitlines()]
                self.assertEqual([row["id"] for row in rows if "id" in row], ["alpha", "zeta"])
                self.assertEqual(rows[1]["gc_display"], "78.947%")
                summary = next(item["content"] for item in snapshot["messages"] if item["kind"] == "summary")
                self.assertIn("78.947%", summary)
                self.assertEqual((await client.get("/api/preferences")).json(), changed)

                next_room = await create_room(client)
                await send(client, next_room, "new-default", "统计 precision.fa 的 GC。")
                next_snapshot = await until(client, next_room, lambda response: terminal(response, 1))
                next_job = next_snapshot["jobs"][0]
                self.assertEqual((next_job["output_result_order"], next_job["output_gc_decimal_places"]), ("file", 2))
