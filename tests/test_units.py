"""Direct unit tests for the pure logic the agent and the analysis tools rely on.

Unlike the integration suites, nothing here starts a server, a subprocess, or a
Docker client: each test pins one decision the rest of the application assumes.
"""

import csv
import json
import os
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest import IsolatedAsyncioTestCase, TestCase
from unittest.mock import patch

import httpx

from backend.app.config import (
    ROOT,
    STDIO_CONFIG_ERROR,
    Settings,
    normalize_task_type,
    parse_stdio_servers,
)
from backend.app.docker_runner import PYTHON_IMAGE, container_args
from backend.app.failures import Failure, execution_failure, launch_failure
from backend.app.model import DeepSeek, ModelError
from scripts.fasta import MAX_COLUMN_RANGES, MAX_DIAGNOSTICS, InvalidFasta, Record, parse_fasta
from scripts.find_orf import find_orf
from scripts.seq_stats import seq_stats


class EventStream(httpx.AsyncByteStream):
    """Feed pre-built SSE bytes to the response parser, one chunk at a time."""

    def __init__(self, chunks: list[bytes]):
        self.chunks = chunks

    async def __aiter__(self):
        for chunk in self.chunks:
            yield chunk


def sse(*events: str | dict) -> EventStream:
    chunks = [b": keep-alive\n\n"]
    for event in events:
        payload = event if isinstance(event, str) else json.dumps(event, ensure_ascii=False)
        chunks.append(f"data: {payload}\n\n".encode())
    chunks.append(b"data: [DONE]\n\n")
    return EventStream(chunks)


def delta(content: str | None = None, finish: str | None = None, calls: list[dict] | None = None) -> dict:
    body: dict = {}
    if content is not None:
        body["content"] = content
    if calls is not None:
        body["tool_calls"] = calls
    return {"choices": [{"index": 0, "delta": body, "finish_reason": finish}]}


class ConfigTests(TestCase):
    def test_normalize_task_type_folds_case_and_unicode_width(self):
        self.assertEqual(normalize_task_type("  SEQ_Stats "), "seq_stats")
        self.assertEqual(normalize_task_type("记录数"), "记录数")
        # NFC composes the decomposed form so one type cannot be registered twice.
        self.assertEqual(normalize_task_type("e\u0301tude"), normalize_task_type("étude"))

    def test_stdio_profiles_accept_a_command_and_reject_unsafe_values(self):
        servers = parse_stdio_servers('{"calc": {"command": "python", "args": ["calc.py"]}}')
        self.assertEqual(servers["calc"].command, "python")
        self.assertEqual(servers["calc"].args, ["calc.py"])
        self.assertEqual(parse_stdio_servers(None), {})
        for value in ("not json", "[]", '{"bad name!": {"command": "python"}}',
                      '{"calc": {"command": "   "}}', '{"calc": {"args": []}}'):
            with self.subTest(value=value), self.assertRaises(ValueError) as raised:
                parse_stdio_servers(value)
            self.assertEqual(str(raised.exception), STDIO_CONFIG_ERROR)

    def test_load_prefers_environment_over_env_file_and_normalizes_urls(self):
        env = {
            "DEEPSEEK_MODEL": "environment-model",
            "DEEPSEEK_BASE_URL": "https://example.invalid/v1/",
            "DEEPSEEK_API_KEY": "environment-key",
            "APP_DATA_DIR": "var",
            "MCP_HTTP_ENDPOINTS": " http://one.invalid/mcp , ,http://two.invalid/mcp,http://one.invalid/mcp ",
            "MCP_STDIO_SERVERS": "{}",
        }
        file_values = {"DEEPSEEK_MODEL": "file-model", "DEEPSEEK_API_KEY": "file-key"}
        with patch.dict(os.environ, env, clear=True), \
                patch("backend.app.config.dotenv_values", return_value=file_values):
            settings = Settings.load()
            self.assertEqual(
                (settings.model, settings.api_key, settings.base_url),
                ("environment-model", "environment-key", "https://example.invalid/v1"),
            )
            self.assertEqual(settings.mcp_http_endpoints,
                             ("http://one.invalid/mcp", "http://two.invalid/mcp"))
            self.assertEqual(settings.data_dir, (ROOT / "var").resolve())
            self.assertEqual(settings.input_token_budget, 24_000)
            self.assertNotIn("environment-key", repr(settings))
            os.environ.pop("DEEPSEEK_MODEL")
            os.environ.pop("DEEPSEEK_API_KEY")
            file_loaded = Settings.load()
            self.assertEqual((file_loaded.model, file_loaded.api_key), ("file-model", "file-key"))

    def test_load_rejects_an_unusable_token_budget(self):
        for raw in ("1000", "not-a-number", " "):
            with self.subTest(raw=raw), patch.dict(os.environ, {"DEEPSEEK_INPUT_TOKEN_BUDGET": raw}, clear=True), \
                    patch("backend.app.config.dotenv_values", return_value={}):
                with self.assertRaises(ValueError) as raised:
                    Settings.load()
            self.assertIn("DEEPSEEK_INPUT_TOKEN_BUDGET", str(raised.exception))


class ModelStreamTests(IsolatedAsyncioTestCase):
    async def collect(self, *events, on_text=None):
        response = httpx.Response(200, headers={"Content-Type": "text/event-stream"}, stream=sse(*events))
        return await DeepSeek._collect(response, on_text)

    async def test_text_deltas_stream_in_order(self):
        seen: list[str] = []
        reply = await self.collect(
            delta("sample1 "), delta("共有 2 条"), delta("序列。", "stop"), on_text=seen.append,
        )
        self.assertEqual(reply.text, "sample1 共有 2 条序列。")
        self.assertEqual(reply.calls, [])
        self.assertEqual(seen, ["sample1 ", "共有 2 条", "序列。"])

    async def test_fragmented_tool_calls_join_by_index(self):
        reply = await self.collect(
            delta(calls=[{"index": 1, "id": "call_b",
                          "function": {"name": "job_status", "arguments": '{"job_'}},
                         {"index": 0, "id": "call_a",
                          "function": {"name": "submit_job", "arguments": '{"analysis'}}]),
            delta(calls=[{"index": 1, "function": {"arguments": 'id": "b"}'}},
                         {"index": 0, "function": {"arguments": '_type": "seq_stats"}'}}]),
            delta(finish="tool_calls"),
        )
        self.assertEqual([call.id for call in reply.calls], ["call_a", "call_b"])
        self.assertEqual([call.name for call in reply.calls], ["submit_job", "job_status"])
        self.assertEqual(json.loads(reply.calls[0].arguments), {"analysis_type": "seq_stats"})
        self.assertEqual(json.loads(reply.calls[1].arguments), {"job_id": "b"})
        self.assertEqual(reply.calls[0].protocol()["function"]["name"], "submit_job")

    async def test_multiline_data_event_is_rejoined(self):
        body = json.dumps(delta("多行事件", "stop"), ensure_ascii=False)
        split = body.index(",") + 1
        stream = EventStream([
            b": keep-alive\n\n",
            f"data: {body[:split]}\ndata: {body[split:]}\n\n".encode(),
            b"data: [DONE]\n\n",
        ])
        reply = await DeepSeek._collect(httpx.Response(200, stream=stream), None)
        self.assertEqual(reply.text, "多行事件")

    async def test_incomplete_or_malformed_streams_raise_model_errors(self):
        cases = {
            "no finish reason": (delta("半句"),),
            "tool_calls without calls": (delta(finish="tool_calls"),),
            "missing id": (delta(calls=[{"index": 0, "function": {"name": "read_log", "arguments": "{}"}}]),
                           delta(finish="tool_calls")),
            "missing name": (delta(calls=[{"index": 0, "id": "call_x", "function": {"arguments": "{}"}}]),
                             delta(finish="tool_calls")),
            "duplicate id": (delta(calls=[{"index": 0, "id": "call_x", "function": {"name": "a", "arguments": "{}"}},
                                          {"index": 1, "id": "call_x", "function": {"name": "b", "arguments": "{}"}}]),
                             delta(finish="tool_calls")),
            "index out of range": (delta(calls=[{"index": 16, "id": "call_x",
                                                 "function": {"name": "a", "arguments": "{}"}}]),
                                   delta(finish="tool_calls")),
            "non-integer index": (delta(calls=[{"index": "0", "id": "call_x",
                                                "function": {"name": "a", "arguments": "{}"}}]),
                                  delta(finish="tool_calls")),
            "oversized arguments": (delta(calls=[{"index": 0, "id": "call_x",
                                                  "function": {"name": "a", "arguments": "x" * 8193}}]),
                                    delta(finish="tool_calls")),
            "non-string content": (delta("ok"), {"choices": [{"delta": {"content": {"text": "no"}},
                                                              "finish_reason": "stop"}]}),
            "inline error": ({"error": {"message": "upstream failed"}},),
        }
        for name, events in cases.items():
            with self.subTest(case=name), self.assertRaises(ModelError):
                await self.collect(*events)

    async def test_missing_key_is_reported_without_a_request(self):
        model = DeepSeek(Settings(api_key=""))
        try:
            with self.assertRaises(ModelError) as raised:
                await model.reply([{"role": "user", "content": "统计 sample1"}], [], "auto")
        finally:
            await model.close()
        self.assertIn("DEEPSEEK_API_KEY", str(raised.exception))


class FailureClassificationTests(TestCase):
    def test_launch_errors_map_to_actionable_categories(self):
        cases = [
            (OSError(11, "try again"), ("temporary_resource", "PROCESS_TEMPORARILY_UNAVAILABLE")),
            (OSError(12, "out of memory"), ("resource_limit", "RESOURCE_LIMIT")),
            (OSError(28, "no space"), ("resource_limit", "RESOURCE_LIMIT")),
            (FileNotFoundError(2, "no interpreter"), ("configuration", "INTERPRETER_NOT_FOUND")),
            (PermissionError(13, "denied"), ("configuration", "LAUNCH_DENIED")),
            (OSError("unknown"), ("unknown", "LAUNCH_FAILED")),
        ]
        for error, expected in cases:
            with self.subTest(error=repr(error)):
                failure = launch_failure(error)
                self.assertIsInstance(failure, Failure)
                self.assertEqual((failure.category, failure.code), expected)
                self.assertEqual(failure.stage, "launch")
                self.assertTrue(failure.detail)

    def test_execution_logs_drive_the_reported_cause(self):
        with TemporaryDirectory() as directory:
            log = Path(directory) / "stderr.log"
            log.write_text(json.dumps({"code": "INVALID_BASE", "line": 2}) + "\n", encoding="utf-8")
            self.assertEqual(execution_failure(2, log), Failure("input", "INVALID_BASE", "input"))
            log.write_text(json.dumps({"code": "NOT_A_KNOWN_CODE"}) + "\n", encoding="utf-8")
            self.assertEqual(execution_failure(2, log).code, "UNCLASSIFIED_EXIT")
            log.write_text("Traceback (most recent call last):\n"
                           "ModuleNotFoundError: No module named 'Bio'\n", encoding="utf-8")
            self.assertEqual(execution_failure(1, log).code, "IMPORT_FAILED")
            # Job logs written on Windows end their lines with CRLF.
            log.write_bytes(b"MemoryError: out of memory\r\n")
            self.assertEqual(execution_failure(1, log).code, "MEMORY_LIMIT")
            log.write_bytes(b"Traceback (most recent call last):\r\nMemoryError\r\n")
            self.assertEqual(execution_failure(1, log).code, "MEMORY_LIMIT")
            self.assertEqual(execution_failure(1, Path(directory) / "missing.log").code, "UNCLASSIFIED_EXIT")


class DockerIsolationTests(TestCase):
    def test_container_arguments_keep_the_script_sandboxed(self):
        with TemporaryDirectory() as directory:
            inputs = Path(directory) / "staged, inputs with space"
            inputs.mkdir()
            args = container_args("research-python-abc123", inputs, 60)
            for flag, value in [("--network", "none"), ("--user", "65534:65534"), ("--cap-drop", "ALL"),
                                ("--memory", "256m"), ("--pids-limit", "32"), ("--log-driver", "none")]:
                with self.subTest(flag=flag):
                    self.assertEqual(args[args.index(flag) + 1], value)
            self.assertIn("--pull=never", args)
            self.assertIn("--read-only", args)
            self.assertEqual(args[args.index("--security-opt") + 1], "no-new-privileges=true")
            self.assertEqual(args[args.index("--tmpfs") + 1], "/tmp:rw,nosuid,nodev,noexec,size=16m")
            mount = next(csv.reader([args[args.index("--mount") + 1]]))
            self.assertEqual(mount, ["type=bind", f"source={inputs}", "destination=/inputs", "readonly"])
            self.assertIn(PYTHON_IMAGE, args)
            self.assertEqual(args[-11:-9], ["timeout", "--signal=KILL"])
            self.assertEqual((args[-9], args[-8]), ("60s", "python"))
            self.assertEqual(args[-7:-5], ["-I", "-B"])
            self.assertEqual(args[-2:], ["/inputs/analysis.py", "/inputs/input.fasta"])

    def test_fractional_timeout_is_formatted_for_the_container(self):
        args = container_args("job", Path("."), 0.5)
        self.assertEqual(args[args.index("--name") + 1], "job")
        self.assertIn("0.5s", args)


class FastaDiagnosticTests(TestCase):
    def test_diagnostics_are_capped_and_counted(self):
        source = [f"BAD{index}\n" for index in range(MAX_DIAGNOSTICS + 10)]
        with self.assertRaises(InvalidFasta) as raised:
            parse_fasta(source)
        diagnostics = raised.exception.diagnostics
        omitted = len(source) + 1 - MAX_DIAGNOSTICS
        self.assertEqual(len(diagnostics), MAX_DIAGNOSTICS + 1)
        self.assertEqual(diagnostics[-1].code, "MORE_ERRORS")
        self.assertIn(str(omitted), diagnostics[-1].message)
        self.assertEqual({item.code for item in diagnostics[:-1]}, {"MISSING_HEADER", "INVALID_BASE"})

    def test_column_ranges_stop_at_the_limit_and_report_truncation(self):
        sequence = "X" + "AX" * (MAX_COLUMN_RANGES + 4)
        with self.assertRaises(InvalidFasta) as raised:
            parse_fasta([">id\n", sequence + "\n"])
        issues = raised.exception.diagnostics
        self.assertEqual([item.code for item in issues], ["INVALID_BASE"])
        self.assertEqual(len(issues[0].columns), MAX_COLUMN_RANGES)
        self.assertTrue(issues[0].columns_truncated)
        self.assertEqual(issues[0].columns[0], (1, 1))

    def test_two_equally_long_orfs_keep_the_earlier_start(self):
        self.assertEqual(find_orf(Record("tie", "ATGAAATAACATGCCCTAA"))["orf"],
                         {"start": 1, "end": 9, "length_nt": 9})

    def test_ambiguous_codon_breaks_an_open_reading_frame(self):
        self.assertIsNone(find_orf(Record("gap", "ATGNNNTAA"))["orf"])
        self.assertIsNone(find_orf(Record("no-stop", "CCATGAAACCC"))["orf"])
        self.assertEqual(seq_stats(Record("gc", "GGNNAA")),
                         {"id": "gc", "length_nt": 6, "gc_pct": 50.0, "gc_count": 2, "known_bases": 4})
