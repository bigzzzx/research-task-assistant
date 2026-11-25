from contextlib import closing
import json
from pathlib import Path
import sqlite3
from tempfile import TemporaryDirectory
from time import time
from unittest import IsolatedAsyncioTestCase, TestCase

from backend.app.auth import COOKIE
from backend.app.config import ROOT, Settings
from backend.app.events import Events
from backend.app.store import Store
from tests.model_fixture import ModelFixture
from tests.test_app import application, client_for, create_room, send, sign_in, terminal, until


class AccountTests(IsolatedAsyncioTestCase):
    def setUp(self):
        temporary = TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.settings = Settings(data_dir=Path(temporary.name), api_key="fixture-key")
        self.model = ModelFixture()

    async def test_register_login_and_revoke_browser_session(self):
        async with application(self.settings, self.model) as app:
            async with client_for(app) as client:
                for path in ("/api/conversations", "/api/config", "/api/events", "/api/tools", "/api/jobs", "/api/files"):
                    self.assertEqual((await client.get(path)).status_code, 401, path)
                short_password = "123456"
                short = await client.post("/api/auth/register", json={"username": "研究员甲", "password": short_password})
                self.assertEqual(short.status_code, 422)
                self.assertEqual(short.json()["detail"], "密码至少 7 位。")
                self.assertNotIn(short_password, short.text)

                credentials = {"username": "Alice-01", "password": "1234567"}
                registered = await client.post("/api/auth/register", json=credentials)
                self.assertEqual(registered.status_code, 201)
                self.assertEqual(set(registered.json()), {"id", "username"})
                self.assertIn("HttpOnly", registered.headers["set-cookie"])
                self.assertIn("SameSite=strict", registered.headers["set-cookie"])
                self.assertEqual(registered.headers["cache-control"], "no-store")
                saved = app.state.store.user_by_name("alice-01")["password_hash"]
                self.assertTrue(saved.startswith("scrypt1$"))
                self.assertNotEqual(saved, credentials["password"])
                old_token = client.cookies.get(COOKIE)
                self.assertEqual((await client.get("/api/auth/me")).json(), registered.json())
                self.assertEqual((await client.post("/api/auth/logout")).status_code, 204)
                client.cookies.set(COOKIE, old_token)
                self.assertEqual((await client.get("/api/auth/me")).status_code, 401)
                client.cookies.clear()

                wrong = await client.post("/api/auth/login", json={**credentials, "password": "incorrect password"})
                self.assertEqual(wrong.status_code, 401)
                login = await client.post("/api/auth/login", json={**credentials, "username": "alice-01"})
                self.assertEqual(login.json(), registered.json())
                self.assertNotEqual(client.cookies.get(COOKIE), old_token)
                with app.state.store.db:
                    app.state.store.db.execute("UPDATE sessions SET expires_at=0")
                self.assertEqual((await client.get("/api/auth/me")).status_code, 401)

    async def test_account_ownership_of_files_tasks_logs_and_tools(self):
        async with application(self.settings, self.model) as app:
            async with client_for(app) as alice, client_for(app) as bob:
                alice_user = await sign_in(alice, "研究员甲")
                bob_user = await sign_in(bob, "研究员乙")
                alice_room = await create_room(alice)
                bob_room = await create_room(bob)

                response = await alice.post("/api/files", params={"filename": "alpha.fa"}, content=b">alice\nAAACCC\n")
                self.assertEqual(response.status_code, 201)
                alice_file = response.json()["id"]
                self.assertEqual((await alice.get(f"/api/files/{alice_file}/download")).content, b">alice\nAAACCC\n")
                self.assertEqual((await bob.get(f"/api/files/{alice_file}/download")).status_code, 404)
                self.assertEqual({row["source"] for row in (await bob.get("/api/files")).json()}, {"sample"})
                self.assertEqual((await bob.get(alice_room)).status_code, 404)
                self.assertEqual((await bob.post(alice_room + "/files?filename=safe.fa", content=b">id\nACG")).status_code, 404)
                self.assertEqual((await bob.post(alice_room + "/messages", json={
                    "text": "测试", "client_message_id": "foreign-message",
                })).status_code, 404)
                self.assertEqual((await bob.get("/api/jobs", headers={"X-Workspace-Account": alice_user["id"]})).status_code, 401)

                bob_message = await send(bob, bob_room, "bob-turn", "帮我统计 alpha.fa")
                await until(bob, bob_room, lambda snapshot: any(message["kind"] == "assistant" for message in snapshot["messages"]))
                self.assertNotIn("alpha.fa", self.model.requests[-1]["messages"][0]["content"])
                refused = await app.state.tools.execute(
                    bob_user["id"], bob_room.rsplit("/", 1)[1], bob_message["id"], "submit_job",
                    json.dumps({"filename": "alpha.fa", "analysis_type": "seq_stats"}),
                )
                self.assertEqual(refused["error"]["code"], "FILE_NOT_FOUND")

                await send(alice, alice_room, "alice-run", "帮我统计 alpha.fa 的 GC")
                await until(alice, alice_room, lambda snapshot: any(row["state"] == "running" for row in snapshot["jobs"]))
                running = (await alice.get("/api/jobs")).json()
                self.assertEqual(len(running), 1)
                job_id = running[0]["id"]
                self.assertEqual((await bob.get("/api/jobs")).json(), [])
                self.assertEqual((await bob.get(f"/api/jobs/{job_id}/logs/stdout/content")).status_code, 404)
                snapshot = await until(alice, alice_room, lambda value: terminal(value, 1))
                self.assertEqual(snapshot["jobs"][0]["state"], "succeeded")

                stdout = (await alice.get(f"/api/jobs/{job_id}/logs/stdout")).raise_for_status()
                self.assertEqual(json.loads(stdout.text.splitlines()[0])["gc_pct"], 50.0)
                preview = (await alice.get(f"/api/jobs/{job_id}/logs/stdout/content")).raise_for_status().json()
                self.assertEqual(preview["text"].encode(), stdout.content)
                for path in (f"/api/jobs/{job_id}", f"/api/jobs/{job_id}/logs/stdout",
                             f"/api/jobs/{job_id}/logs/stderr/content", alice_room + f"/jobs/{job_id}/logs/stdout"):
                    self.assertEqual((await bob.get(path)).status_code, 404, path)
                self.assertEqual((await bob.post(alice_room + f"/jobs/{job_id}/retry-summary")).status_code, 404)

                same = await bob.post("/api/files?filename=alpha.fa", content=b">bob\nTTGG\n")
                self.assertEqual(same.status_code, 201)
                self.assertNotEqual(same.json()["id"], alice_file)
                self.assertEqual((await alice.post("/api/files?filename=ALPHA.FA", content=b"")).status_code, 409)
                catalog = (await bob.get("/api/tools")).raise_for_status().json()
                self.assertEqual({tool["name"] for tool in catalog}, {"submit_job", "job_status", "read_log"})

    async def test_event_delivery_is_account_scoped_and_logout_drops_buffered_events(self):
        events = Events()
        expires = int(time()) + 30
        first = events.listen("first", "token-one", expires)
        second = events.listen("second", "token-two", expires)
        self.assertIn("event: ready", await anext(first))
        self.assertIn("event: ready", await anext(second))
        events.send("first", "job.updated", {"private": "first"})
        events.send("second", "file.added", {"private": "second"})
        self.assertIn('"private": "first"', await anext(first))
        self.assertIn('"private": "second"', await anext(second))
        events.send("first", "message.updated", {"private": "queued"})
        events.disconnect("token-one")
        with self.assertRaises(StopAsyncIteration):
            await anext(first)
        events.send("second", "job.updated", {"active": True})
        self.assertIn('"active": true', await anext(second))
        await second.aclose()


class LegacyWorkspaceTests(TestCase):
    def test_history_lists_conversations_with_content(self):
        with TemporaryDirectory() as directory:
            with closing(Store(Path(directory) / "assistant.sqlite3", ROOT / "data")) as store:
                owner = store.create_user("researcher", "researcher", "unused-in-history-test")["id"]
                empty = store.new_conversation(owner)["id"]
                conversation = store.new_conversation(owner)["id"]
                with_file = store.new_conversation(owner)["id"]
                self.assertEqual(store.conversations(owner), [])

                store.user_message(conversation, "first-message", "新会话")
                store.add_file(owner, "local-file", "records.fa", 10, with_file)
                self.assertEqual({row["id"] for row in store.conversations(owner)}, {conversation, with_file})
                self.assertIsNotNone(store.owned_conversation(owner, empty))

    def test_pre_account_sqlite_records_require_explicit_assignment(self):
        with TemporaryDirectory() as directory:
            path = Path(directory) / "assistant.sqlite3"
            database = sqlite3.connect(path)
            with database:
                database.executescript("""
                    CREATE TABLE conversations (id TEXT PRIMARY KEY, title TEXT, created_at TEXT, updated_at TEXT);
                    CREATE TABLE files (id TEXT PRIMARY KEY, conversation_id TEXT, display_name TEXT, name_key TEXT,
                        storage_name TEXT, source TEXT, size_bytes INTEGER, UNIQUE(conversation_id, name_key));
                    INSERT INTO conversations VALUES ('before-a','旧会话甲','2026-01-01','2026-01-01');
                    INSERT INTO conversations VALUES ('before-b','旧会话乙','2026-01-01','2026-01-01');
                    INSERT INTO files VALUES ('one','before-a','old.fa','old.fa','one','upload',1);
                    INSERT INTO files VALUES ('two','before-b','old.fa','old.fa','two','upload',2);
                """)
            database.close()
            with closing(Store(path, ROOT / "data")) as store:
                first = store.create_user("old-owner", "old-owner", "unused-in-migration-test")["id"]
                second = store.create_user("other-user", "other-user", "unused-in-migration-test")["id"]
                self.assertEqual(store.conversations(first), [])
                self.assertEqual(store.conversations(second), [])
                self.assertEqual(store.assign_legacy(first), (2, 2))
                self.assertEqual(store.assign_legacy(first), (0, 0))
                self.assertEqual(len(store.conversations(first)), 2)
                self.assertEqual(store.conversations(second), [])
                uploads = [file for file in store.files(first) if file["source"] == "upload"]
                self.assertEqual([file["display_name"] for file in uploads], ["old.fa", "old-2.fa"])
                self.assertEqual({file["source"] for file in store.files(second)}, {"sample"})
