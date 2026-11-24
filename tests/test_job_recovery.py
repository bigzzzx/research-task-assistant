from dataclasses import replace
import errno
import json
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest import IsolatedAsyncioTestCase
from unittest.mock import patch

from backend.app.config import ROOT, Settings
from backend.app.input_repair import preview_repair
from tests.model_fixture import ModelFixture
from tests.test_app import application, client_for, create_room, send, service, sign_in, terminal, until


class MissingInputModel(ModelFixture):
    @staticmethod
    def _filename(question: str, available: list[str]) -> str | None:
        if "missing-input.fa" in question:
            return "missing-input.fa"
        return ModelFixture._filename(question, available)


async def upload(client, name: str, content: bytes) -> dict:
    response = await client.post("/api/files", params={"filename": name}, content=content)
    assert response.status_code == 201, response.text
    return response.json()


class JobRecoveryTests(IsolatedAsyncioTestCase):
    def setUp(self):
        temporary = TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.settings = Settings(data_dir=Path(temporary.name), api_key="fixture-key")
        self.model = ModelFixture()

    async def test_transient_launch_retry_survives_restart_and_keeps_the_first_attempt(self):
        with patch("backend.app.jobs.subprocess.Popen", side_effect=BlockingIOError(errno.EAGAIN, "fixture busy")):
            async with service(self.settings, self.model) as client:
                room = await create_room(client)
                await send(client, room, "start", "统计 sample1.fasta 的 GC 含量")
                snapshot = await until(client, room, lambda row: len(row["jobs"]) == 2)
                child, original = snapshot["jobs"]
                self.assertEqual(original["failure_category"], "temporary_resource")
                self.assertEqual(original["failure_stage"], "launch")
                self.assertEqual(original["next_attempt_id"], child["id"])
                self.assertEqual(child["state"], "queued")
                self.assertIsNone(child["duration_seconds"])
                self.assertIsNotNone(child["available_at"])

        async with service(self.settings, self.model) as client:
            snapshot = await until(client, room, lambda value: terminal(value, 2))
            child, failed = snapshot["jobs"]
            self.assertEqual(failed["id"], original["id"])
            self.assertEqual(failed["state"], "failed")
            self.assertEqual(child["retry_kind"], "automatic")
            self.assertEqual(child["attempt_number"], 2)
            self.assertEqual(child["state"], "succeeded")
            self.assertEqual(child["retry_of_job_id"], failed["id"])
            self.assertGreaterEqual(child["started_at"], child["available_at"])
            self.assertGreater(child["duration_seconds"], 0)

    async def test_retry_budget_and_idempotent_manual_run(self):
        settings = replace(self.settings, job_retry_delay=0.01, job_max_auto_retries=2)
        with patch("backend.app.jobs.subprocess.Popen", side_effect=BlockingIOError(errno.EAGAIN, "fixture busy")):
            async with service(settings, self.model) as client:
                room = await create_room(client)
                await send(client, room, "start", "统计 sample1.fasta 的 GC 含量")
                snapshot = await until(client, room, lambda value: terminal(value, 3))
                self.assertEqual([job["automatic_retries"] for job in snapshot["jobs"]], [2, 1, 0])
                exhausted = snapshot["jobs"][0]
                self.assertIsNone(exhausted["next_attempt_id"])

        async with service(settings, self.model) as client:
            url = f"/api/jobs/{exhausted['id']}/retry"
            first = await client.post(url)
            second = await client.post(url)
            self.assertEqual(first.status_code, 202)
            child = first.json()
            self.assertEqual(second.json()["id"], child["id"])
            self.assertEqual(child["retry_kind"], "manual")
            self.assertEqual(child["automatic_retries"], 0)
            self.assertEqual(child["attempt_number"], 4)
            self.assertEqual(child["root_job_id"], snapshot["jobs"][-1]["id"])
            ready = await until(client, room, lambda value: terminal(value, 4))
            self.assertEqual(ready["jobs"][0]["state"], "succeeded")
            self.assertEqual([job["state"] for job in ready["jobs"][1:]], ["failed"] * 3)

    async def test_format_preview_and_approval_preserve_source_bytes_and_failed_log(self):
        payload = b"\xef\xbb\xbfseq_probe original title\r\nAACCGG\r\n"
        async with service(self.settings, self.model) as client:
            source = await upload(client, "repair-probe.fa", payload)
            room = await create_room(client)
            await send(client, room, "start", "统计 repair-probe.fa 的 GC 含量")
            failure = await until(client, room, lambda value: terminal(value, 1))
            original = failure["jobs"][0]
            before_log = await client.get(f"/api/jobs/{original['id']}/logs/stderr")
            pending = failure["input_requests"][0]
            self.assertEqual(original["failure_code"], "MISSING_HEADER")
            self.assertEqual(pending["original_file_id"], source["id"])
            self.assertIsNone(pending["resolved_job_id"])
            self.assertEqual((await client.post(f"/api/jobs/{original['id']}/retry")).status_code, 409)
            repair_url = f"/api/input-requests/{pending['id']}/repair"
            resolve_url = f"/api/input-requests/{pending['id']}/resolve"
            preview = (await client.get(repair_url)).json()
            diagnostics = (await client.get(repair_url.removesuffix("/repair") + "/diagnostics")).json()
            self.assertEqual(diagnostics["repair"], preview)
            self.assertEqual([issue["code"] for issue in diagnostics["issues"]], ["MISSING_HEADER"])
            self.assertEqual(preview["before"], "seq_probe original title")
            self.assertEqual(preview["after"], ">seq_probe original title")
            self.assertEqual(len((await client.get(room)).json()["jobs"]), 1)
            self.assertEqual((await client.post(resolve_url, json={"repair_token": "0" * 64})).status_code, 409)

            choice = {"repair_token": preview["token"]}
            accepted = await client.post(resolve_url, json=choice)
            self.assertEqual(accepted.status_code, 202, accepted.text)
            result = accepted.json()
            self.assertEqual(result["job"]["retry_of_job_id"], original["id"])
            self.assertEqual((await client.post(resolve_url, json=choice)).json()["job"]["id"], result["job"]["id"])
            self.assertEqual((await client.get(f"/api/files/{source['id']}/download")).content, payload)
            copy = await client.get(f"/api/files/{result['file']['id']}/download")
            self.assertEqual(copy.content, payload.replace(b"seq_probe", b">seq_probe", 1))

            completed = await until(client, room, lambda value: terminal(value, 2))
            self.assertEqual(completed["jobs"][0]["state"], "succeeded")
            self.assertEqual(completed["jobs"][1]["state"], "failed")
            after_log = await client.get(f"/api/jobs/{original['id']}/logs/stderr")
            self.assertEqual(before_log.content, after_log.content)
            self.assertEqual(json.loads(after_log.text.splitlines()[0])["code"], "MISSING_HEADER")
            self.assertIsNone(preview_repair(ROOT / "data" / "sample3.fasta"))

    async def test_missing_input_remains_pending_across_restart_and_is_account_scoped(self):
        async with service(self.settings, MissingInputModel()) as client:
            room = await create_room(client)
            await send(client, room, "start", "统计 missing-input.fa 的 GC 含量")
            snapshot = await until(client, room, lambda data: bool(data["input_requests"]))
            self.assertEqual(snapshot["jobs"], [])
            pending = snapshot["input_requests"][0]

        async with application(self.settings, self.model) as app:
            async with client_for(app) as owner, client_for(app) as other:
                await sign_in(owner, "tester")
                await sign_in(other, "other-tester")
                snapshot = (await owner.get(room)).json()
                self.assertIsNone(snapshot["input_requests"][0]["resolved_job_id"])
                url = f"/api/input-requests/{pending['id']}"
                other_file = await upload(other, "other.fa", b">other\nAAAA\n")
                self.assertEqual((await other.get(url + "/repair")).status_code, 404)
                self.assertEqual((await other.get(url + "/diagnostics")).status_code, 404)
                self.assertEqual((await other.post(url + "/resolve", json={"file_id": "sample1.fasta"})).status_code, 404)
                self.assertEqual((await owner.post(url + "/resolve", json={"file_id": other_file["id"]})).status_code, 404)
                supplied = await upload(owner, "missing-input.fa", b">uploaded\nACGT\n")
                accepted = await owner.post(url + "/resolve", json={"file_id": supplied["id"]})
                self.assertEqual(accepted.status_code, 202, accepted.text)
                job = accepted.json()["job"]
                self.assertEqual(job["filename"], supplied["display_name"])
                self.assertIsNone(job["retry_of_job_id"])
                self.assertEqual((await other.post(f"/api/jobs/{job['id']}/retry")).status_code, 404)
                finished = await until(owner, room, lambda value: terminal(value, 1))
                self.assertEqual(finished["jobs"][0]["state"], "succeeded")
                self.assertEqual(finished["input_requests"][0]["resolved_job_id"], job["id"])
