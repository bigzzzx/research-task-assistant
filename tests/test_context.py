import json
from dataclasses import replace
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest import IsolatedAsyncioTestCase, TestCase
from uuid import uuid4

import httpx

from backend.app.config import Settings
from backend.app.context import ConversationContext, HistoryTurn, SUMMARY_PROMPT, build_prompt, estimate_input_tokens
from backend.app.model import DeepSeek, ModelError
from tests.model_fixture import FixtureStream, ModelFixture, call_events, text_events
from tests.test_app import application, client_for, create_room, send, sign_in, until


def history_turn(number: int, text: str) -> HistoryTurn:
    return HistoryTurn(number, [
        {"role": "user", "content": text},
        {"role": "assistant", "content": f"第 {number} 轮确认。"},
    ])


def facts(messages: list[dict]) -> dict:
    return json.loads(messages[0]["content"].split("\n会话数据 ", 1)[1])


def saved_turn(store, conversation_id: str, text: str) -> int:
    row, _ = store.user_message(conversation_id, uuid4().hex, text)
    store.change_message(row["id"], status="done")
    store.add_message(conversation_id, row["id"], "assistant", content="已确认。")
    return row["id"]


def saved_job(store, conversation_id: str, turn_id: int, filename: str) -> dict:
    job, _ = store.submit(conversation_id, turn_id, filename, "seq_stats")
    store.finish_job(job["id"], 0)
    return store.change_summary(job["id"], "pending", "done")


class ContextFixture(ModelFixture):
    def __init__(self):
        super().__init__()
        self.fail_history_summary = False
        self.long_log_job = ""

    def __call__(self, request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        messages = body["messages"]
        if messages[0]["content"] == SUMMARY_PROMPT:
            self.requests.append(body)
            assert not body.get("tools")
            if self.fail_history_summary:
                return httpx.Response(503)
            data = json.loads(messages[-1]["content"])
            text = "早期决定包含青杉。" if not data["previous_summary"] else "早期决定包含青杉，另已确认配色。"
            return self._stream(text_events(text))
        if self.long_log_job:
            self.requests.append(body)
            if any(message["role"] == "tool" for message in messages):
                return self._stream(text_events("日志已读取，更多内容可从任务页面查看。"))
            return self._stream(call_events([("read_log", {"job_id": self.long_log_job})]))
        return super().__call__(request)

    @staticmethod
    def _stream(events) -> httpx.Response:
        return httpx.Response(200, headers={"Content-Type": "text/event-stream"}, stream=FixtureStream(events, 0))

    @property
    def summaries(self) -> list[dict]:
        return [body for body in self.requests if body["messages"][0]["content"] == SUMMARY_PROMPT]


class PromptTests(TestCase):
    def test_budget_keeps_recent_complete_turn_and_pinned_job(self):
        old = history_turn(1, "旧历史 " + "甲" * 1000)
        new = history_turn(2, "近期问题")
        current = [{"role": "user", "content": "当前问题"}]
        jobs = [{"job_id": "aa" * 16, "filename": "sample1.fasta"}]
        context = ConversationContext([old, new], "已确认保留原始记录。", ["sample1.fasta"], [], jobs, [])
        expected = build_prompt("规则", current, [], "none", 100_000, replace(context, history=[new]))
        budget = estimate_input_tokens(expected.messages, [], "none")
        selected = build_prompt("规则", current, [], "none", budget, context)
        self.assertEqual(selected.history, [new])
        self.assertEqual(selected.messages[1:-1], new.messages)
        self.assertEqual(selected.messages[-1], current[0])
        self.assertEqual(facts(selected.messages)["referenced_jobs"], jobs)
        self.assertEqual(context.history, [old, new])


class ContextIntegrationTests(IsolatedAsyncioTestCase):
    def setUp(self):
        temporary = TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.settings = Settings(data_dir=Path(temporary.name), api_key="fixture-key")
        self.model = ContextFixture()

    async def test_each_react_call_fits_and_preserves_call_result_pair(self):
        settings = replace(self.settings, input_token_budget=7800)
        async with application(settings, self.model) as app, client_for(app) as client:
            await sign_in(client, "context-user")
            room = await create_room(client)
            conversation_id = room.rsplit("/", 1)[1]
            store = app.state.store
            first_turn = saved_turn(store, conversation_id, "旧任务")
            job = saved_job(store, conversation_id, first_turn, "sample1.fasta")
            self.model.long_log_job = job["id"]
            folder = app.state.jobs.directory(job["id"])
            folder.mkdir(parents=True)
            raw_log = "开始\n" + "甲乙" * 2300 + "\n结束\n"
            app.state.jobs.log_path(job["id"], "stdout").write_bytes(raw_log.encode())
            for index in range(3):
                # Leave room for the fixed tool schemas and one prior turn before the log arrives.
                saved_turn(store, conversation_id, f"前文第 {index} 轮" + "事" * 375)
            sent = await send(client, room, "read", "读取任务 " + job["id"] + " 的日志。")
            snapshot = await until(client, room, lambda data: any(
                message["id"] == sent["id"] and message["status"] == "done" for message in data["messages"]
            ))
            first, second = self.model.requests
            for body in (first, second):
                estimate = estimate_input_tokens(body["messages"], body["tools"], body["tool_choice"])
                self.assertLessEqual(estimate, settings.input_token_budget)
            users = lambda body: [message for message in body["messages"] if message["role"] == "user"]
            self.assertGreater(len(users(first)), len(users(second)))
            self.assertEqual(users(first)[-1], users(second)[-1])
            call, result = second["messages"][-2:]
            self.assertEqual(call["tool_calls"][0]["id"], result["tool_call_id"])
            forwarded = json.loads(result["content"])
            self.assertEqual(forwarded["job_id"], job["id"])
            saved_result = next(message for message in snapshot["messages"] if message["kind"] == "tool")
            self.assertGreater(len(json.loads(saved_result["content"])["stdout"]), len(forwarded["stdout"]))
            self.assertEqual(app.state.jobs.log_path(job["id"], "stdout").read_bytes(), raw_log.encode())

    async def test_history_summary_persists_and_advances_after_restart(self):
        async with application(self.settings, self.model) as app, client_for(app) as client:
            await sign_in(client, "context-user")
            room = await create_room(client)
            conversation_id = room.rsplit("/", 1)[1]
            store = app.state.store
            older_ids = [saved_turn(store, conversation_id, f"第 {index} 轮项目代号青杉") for index in range(18)]
            sent = await send(client, room, "first", "接着讨论")
            await until(client, room, lambda data: any(
                message["id"] == sent["id"] and message["status"] == "done" for message in data["messages"]
            ))
            saved = store.conversation_summary(conversation_id)
            self.assertEqual(saved["through_turn_id"], older_ids[7])
            self.assertEqual(len(self.model.summaries), 1)
            self.assertIn("青杉", facts(self.model.requests[-1]["messages"])["history_summary"])
            self.assertEqual(store.message(older_ids[0])["content"], "第 0 轮项目代号青杉")

        async with application(self.settings, self.model) as app, client_for(app) as client:
            await sign_in(client, "context-user")
            store = app.state.store
            self.assertEqual(store.conversation_summary(conversation_id), saved)
            for index in range(5):
                saved_turn(store, conversation_id, f"后续讨论配色 {index}")
            sent = await send(client, room, "second", "接着讨论配色")
            await until(client, room, lambda data: any(
                message["id"] == sent["id"] and message["status"] == "done" for message in data["messages"]
            ))
            request = json.loads(self.model.summaries[-1]["messages"][-1]["content"])
            self.assertEqual(request["previous_summary"], saved["content"])
            self.assertEqual(request["turns"][0]["turn_id"], older_ids[8])
            advanced = store.conversation_summary(conversation_id)
            self.assertGreater(advanced["through_turn_id"], saved["through_turn_id"])
            self.assertIn("配色", facts(self.model.requests[-1]["messages"])["history_summary"])

    async def test_summary_failure_does_not_advance_watermark_or_prevent_reply(self):
        self.model.fail_history_summary = True
        async with application(self.settings, self.model) as app, client_for(app) as client:
            await sign_in(client, "context-user")
            room = await create_room(client)
            conversation_id = room.rsplit("/", 1)[1]
            for index in range(14):
                saved_turn(app.state.store, conversation_id, f"旧记录 {index}")
            sent = await send(client, room, "failing-summary", "继续")
            await until(client, room, lambda data: any(
                message["id"] == sent["id"] and message["status"] == "done" for message in data["messages"]
            ))
            self.assertEqual(len(self.model.summaries), 1)
            self.assertIsNone(app.state.store.conversation_summary(conversation_id))

    async def test_older_job_can_be_retrieved_by_filename_or_id_only_in_its_conversation(self):
        async with application(self.settings, self.model) as app, client_for(app) as client:
            owner = await sign_in(client, "context-user")
            room = await create_room(client)
            conversation_id = room.rsplit("/", 1)[1]
            store = app.state.store
            old_turn = saved_turn(store, conversation_id, "早期执行 sample1.fasta")
            old_job = saved_job(store, conversation_id, old_turn, "sample1.fasta")
            for index in range(9):
                turn = saved_turn(store, conversation_id, f"较新的 sample2 分析 {index}")
                saved_job(store, conversation_id, turn, "sample2.fasta")
            for question in ("查状态 sample1.fasta", "查状态 " + old_job["id"][:8]):
                sent = await send(client, room, uuid4().hex, question)
                await until(client, room, lambda data: any(
                    message["id"] == sent["id"] and message["status"] == "done" for message in data["messages"]
                ))
                context = facts(self.model.requests[-1]["messages"])
                self.assertNotIn(old_job["id"], {job["job_id"] for job in context["recent_jobs"]})
                self.assertEqual(context["referenced_jobs"][0]["job_id"], old_job["id"])
                results = json.loads(self.model.requests[-1]["messages"][-1]["content"])
                self.assertEqual((results["job_id"], results["state"]), (old_job["id"], "succeeded"))

            other = store.new_conversation(owner["id"])["id"]
            sent = await send(client, "/api/conversations/" + other, "other-room", "查状态 " + old_job["id"][:8])
            await until(client, "/api/conversations/" + other, lambda data: any(
                message["id"] == sent["id"] and message["status"] == "done" for message in data["messages"]
            ))
            self.assertEqual(facts(self.model.requests[-1]["messages"])["referenced_jobs"], [])
            result = json.loads(self.model.requests[-1]["messages"][-1]["content"])
            self.assertEqual(result["error"]["code"], "JOB_NOT_FOUND")

    async def test_transport_rejects_overflow_before_contacting_provider(self):
        settings = replace(self.settings, input_token_budget=4096)
        payload = [{"role": "user", "content": "a" * 5000}]
        provider = DeepSeek(settings, httpx.MockTransport(self.model))
        try:
            with self.assertRaisesRegex(ModelError, "超出输入预算"):
                await provider.reply(payload, [], "none")
        finally:
            await provider.close()
        self.assertEqual(self.model.requests, [])
