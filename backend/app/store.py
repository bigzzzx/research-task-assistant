"""Short SQLite transactions on the application event loop."""

from datetime import datetime, timezone
from pathlib import Path, PurePath
import json
import sqlite3
from uuid import uuid4

from .config import SAMPLES
from .context import HistoryTurn
from .failures import Failure


def now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="microseconds").replace("+00:00", "Z")


JOB_TABLE = """
CREATE TABLE IF NOT EXISTS {table} (
    id TEXT PRIMARY KEY,
    conversation_id TEXT NOT NULL REFERENCES conversations(id),
    origin_message_id INTEGER NOT NULL REFERENCES messages(id),
    file_id TEXT NOT NULL REFERENCES files(id),
    analysis_type TEXT NOT NULL CHECK(analysis_type IN ('seq_stats', 'find_orf')),
    state TEXT NOT NULL DEFAULT 'queued',
    created_at TEXT NOT NULL,
    started_at TEXT,
    finished_at TEXT,
    exit_code INTEGER,
    runner_error TEXT,
    summary_state TEXT NOT NULL DEFAULT 'pending',
    revision INTEGER NOT NULL DEFAULT 0,
    input_name TEXT,
    failure_category TEXT,
    failure_code TEXT,
    failure_stage TEXT,
    retry_of_job_id TEXT REFERENCES {table}(id),
    root_job_id TEXT REFERENCES {table}(id),
    attempt_number INTEGER NOT NULL DEFAULT 1,
    retry_kind TEXT CHECK(retry_kind IN ('manual', 'automatic')),
    automatic_retries INTEGER NOT NULL DEFAULT 0,
    available_at TEXT
);
"""

SCHEMA = """
PRAGMA foreign_keys = ON;
PRAGMA journal_mode = WAL;

CREATE TABLE IF NOT EXISTS users (
    id TEXT PRIMARY KEY,
    username TEXT NOT NULL,
    username_key TEXT NOT NULL UNIQUE,
    password_hash TEXT NOT NULL,
    created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS account_output_preferences (
    owner_id TEXT PRIMARY KEY REFERENCES users(id) ON DELETE CASCADE,
    result_order TEXT CHECK(result_order IN ('file', 'sequence_id')),
    gc_decimal_places INTEGER CHECK(gc_decimal_places BETWEEN 0 AND 2)
);
CREATE TABLE IF NOT EXISTS sessions (
    token_hash TEXT PRIMARY KEY,
    user_id TEXT NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    expires_at INTEGER NOT NULL
);
CREATE TABLE IF NOT EXISTS conversations (
    id TEXT PRIMARY KEY,
    title TEXT NOT NULL,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    owner_id TEXT REFERENCES users(id)
);
CREATE TABLE IF NOT EXISTS files (
    id TEXT PRIMARY KEY,
    conversation_id TEXT REFERENCES conversations(id),
    display_name TEXT NOT NULL,
    name_key TEXT NOT NULL,
    storage_name TEXT NOT NULL,
    source TEXT NOT NULL CHECK(source IN ('sample', 'upload')),
    size_bytes INTEGER NOT NULL,
    owner_id TEXT REFERENCES users(id),
    created_at TEXT,
    UNIQUE(conversation_id, name_key)
);
CREATE TABLE IF NOT EXISTS messages (
    id INTEGER PRIMARY KEY,
    conversation_id TEXT NOT NULL REFERENCES conversations(id),
    turn_id INTEGER REFERENCES messages(id),
    kind TEXT NOT NULL,
    status TEXT NOT NULL,
    content TEXT NOT NULL DEFAULT '',
    client_message_id TEXT,
    job_id TEXT,
    tool_calls TEXT,
    tool_call_id TEXT,
    invocation_id INTEGER REFERENCES messages(id),
    source TEXT,
    created_at TEXT NOT NULL,
    revision INTEGER NOT NULL DEFAULT 0,
    UNIQUE(conversation_id, client_message_id)
);
CREATE INDEX IF NOT EXISTS messages_conversation ON messages(conversation_id, id);
CREATE INDEX IF NOT EXISTS messages_by_turn ON messages(conversation_id, COALESCE(turn_id, id));
CREATE UNIQUE INDEX IF NOT EXISTS one_job_summary ON messages(job_id) WHERE kind = 'summary';

CREATE TABLE IF NOT EXISTS conversation_summaries (
    conversation_id TEXT PRIMARY KEY REFERENCES conversations(id) ON DELETE CASCADE,
    through_turn_id INTEGER NOT NULL,
    content TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

""" + JOB_TABLE.format(table="jobs")

INPUT_REQUEST_TABLE = """
CREATE TABLE IF NOT EXISTS input_requests (
    id TEXT PRIMARY KEY,
    conversation_id TEXT NOT NULL REFERENCES conversations(id),
    origin_message_id INTEGER NOT NULL REFERENCES messages(id),
    analysis_type TEXT NOT NULL CHECK(analysis_type IN ('seq_stats', 'find_orf')),
    filename TEXT NOT NULL,
    failed_job_id TEXT UNIQUE REFERENCES jobs(id),
    resolved_job_id TEXT REFERENCES jobs(id),
    created_at TEXT NOT NULL,
    revision INTEGER NOT NULL DEFAULT 0
)
"""

JOB_SELECT = """
SELECT j.*, COALESCE(j.input_name, f.display_name) AS filename, c.owner_id,
       request.id AS input_request_id, next.id AS next_attempt_id, next.available_at AS next_attempt_at,
       COALESCE(j.finished_at, strftime('%Y-%m-%dT%H:%M:%fZ', 'now')) AS status_at,
       MAX(0, ROUND((julianday(COALESCE(j.started_at, j.finished_at, 'now')) - julianday(j.created_at)) * 86400, 3))
           AS queue_wait_seconds,
       MAX(0, ROUND((julianday(COALESCE(j.finished_at, 'now')) - julianday(j.started_at)) * 86400, 3))
           AS duration_seconds
FROM jobs j
JOIN files f ON f.id = j.file_id
JOIN conversations c ON c.id = j.conversation_id
LEFT JOIN input_requests request ON request.failed_job_id = j.id
LEFT JOIN jobs next ON next.retry_of_job_id = j.id
"""

INPUT_REQUEST_SELECT = """
SELECT request.*, failed.file_id AS original_file_id, failed.failure_code
FROM input_requests request
JOIN conversations c ON c.id = request.conversation_id
LEFT JOIN jobs failed ON failed.id = request.failed_job_id
"""


class Store:
    def __init__(self, db_path: Path, samples_dir: Path):
        self.db = sqlite3.connect(db_path)
        self.db.row_factory = sqlite3.Row
        self.db.executescript(SCHEMA)
        self._upgrade()
        with self.db:
            for name in SAMPLES:
                self.db.execute(
                    """INSERT OR IGNORE INTO files
                       (id, display_name, name_key, storage_name, source, size_bytes)
                       VALUES (?, ?, ?, ?, 'sample', ?)""",
                    (name, name, name.casefold(), name, (samples_dir / name).stat().st_size),
                )

    def close(self) -> None:
        self.db.close()

    def _upgrade(self) -> None:
        with self.db:
            self.db.execute("BEGIN IMMEDIATE")
            for table, name, declaration in (
                ("conversations", "owner_id", "TEXT REFERENCES users(id)"),
                ("files", "owner_id", "TEXT REFERENCES users(id)"),
                ("files", "created_at", "TEXT"),
                ("jobs", "input_name", "TEXT"),
            ):
                columns = {row["name"] for row in self.db.execute(f"PRAGMA table_info({table})")}
                if name not in columns:
                    self.db.execute(f"ALTER TABLE {table} ADD COLUMN {name} {declaration}")
            self._upgrade_jobs()
            self.db.execute("CREATE INDEX IF NOT EXISTS conversations_owner ON conversations(owner_id, updated_at)")
            self.db.execute(
                "CREATE UNIQUE INDEX IF NOT EXISTS files_owner_name ON files(owner_id, name_key) WHERE owner_id IS NOT NULL"
            )
            self.db.execute("""
                UPDATE jobs SET input_name=(SELECT display_name FROM files WHERE id=jobs.file_id)
                WHERE input_name IS NULL
            """)

    def _upgrade_jobs(self) -> None:
        columns = {row["name"] for row in self.db.execute("PRAGMA table_info(jobs)")}
        if "retry_of_job_id" not in columns:
            old_fields = ("id, conversation_id, origin_message_id, file_id, analysis_type, state, created_at, "
                          "started_at, finished_at, exit_code, runner_error, summary_state, revision, input_name")
            self.db.execute(JOB_TABLE.format(table="jobs_upgrade"))
            self.db.execute(f"INSERT INTO jobs_upgrade ({old_fields}) SELECT {old_fields} FROM jobs")
            self.db.execute("DROP TABLE jobs")
            self.db.execute("ALTER TABLE jobs_upgrade RENAME TO jobs")
        self.db.execute("CREATE INDEX IF NOT EXISTS jobs_conversation ON jobs(conversation_id, created_at)")
        self.db.execute("""
            CREATE UNIQUE INDEX IF NOT EXISTS jobs_initial_submission ON jobs(origin_message_id, file_id, analysis_type)
            WHERE retry_of_job_id IS NULL
        """)
        self.db.execute("CREATE UNIQUE INDEX IF NOT EXISTS job_next_attempt ON jobs(retry_of_job_id)")
        self.db.execute(INPUT_REQUEST_TABLE)
        self.db.execute("""
            CREATE UNIQUE INDEX IF NOT EXISTS input_request_missing_name
            ON input_requests(origin_message_id, analysis_type, filename) WHERE failed_job_id IS NULL
        """)
        self.db.execute("CREATE INDEX IF NOT EXISTS input_requests_conversation ON input_requests(conversation_id)")

    def _one(self, sql: str, params: tuple = ()) -> dict | None:
        row = self.db.execute(sql, params).fetchone()
        return dict(row) if row else None

    def _all(self, sql: str, params: tuple = ()) -> list[dict]:
        return [dict(row) for row in self.db.execute(sql, params)]

    def create_user(self, username: str, key: str, password_hash: str) -> dict:
        user_id = uuid4().hex
        with self.db:
            self.db.execute(
                "INSERT INTO users (id, username, username_key, password_hash, created_at) VALUES (?,?,?,?,?)",
                (user_id, username, key, password_hash, now()),
            )
        return {"id": user_id, "username": username}

    def user_by_name(self, key: str) -> dict | None:
        return self._one("SELECT * FROM users WHERE username_key=?", (key,))

    def output_preferences(self, owner_id: str) -> dict:
        return self._one(
            "SELECT result_order, gc_decimal_places FROM account_output_preferences WHERE owner_id=?",
            (owner_id,),
        ) or {"result_order": None, "gc_decimal_places": None}

    def save_output_preferences(
        self, owner_id: str, order: str | None, decimals: int | None, *, keep_unspecified: bool = False,
    ) -> dict:
        with self.db:
            self.db.execute(
                """INSERT INTO account_output_preferences (owner_id, result_order, gc_decimal_places)
                   VALUES (?, ?, ?) ON CONFLICT(owner_id) DO UPDATE
                   SET result_order=CASE WHEN ? THEN COALESCE(excluded.result_order, result_order)
                                         ELSE excluded.result_order END,
                       gc_decimal_places=CASE WHEN ? THEN COALESCE(excluded.gc_decimal_places, gc_decimal_places)
                                              ELSE excluded.gc_decimal_places END""",
                (owner_id, order, decimals, keep_unspecified, keep_unspecified),
            )
        return self.output_preferences(owner_id)

    def create_session(self, user_id: str, token_hash: str, expires_at: int) -> None:
        with self.db:
            self.db.execute("DELETE FROM sessions WHERE expires_at<=unixepoch()")
            self.db.execute(
                "INSERT INTO sessions (token_hash, user_id, expires_at) VALUES (?,?,?)",
                (token_hash, user_id, expires_at),
            )

    def session(self, token_hash: str) -> dict | None:
        return self._one("""
            SELECT u.id, u.username, s.expires_at FROM sessions s
            JOIN users u ON u.id=s.user_id WHERE s.token_hash=? AND s.expires_at>unixepoch()
        """, (token_hash,))

    def delete_session(self, token_hash: str) -> None:
        with self.db:
            self.db.execute("DELETE FROM sessions WHERE token_hash=?", (token_hash,))

    def new_conversation(self, owner_id: str) -> dict:
        conversation_id, timestamp = uuid4().hex, now()
        with self.db:
            self.db.execute(
                "INSERT INTO conversations (id, title, created_at, updated_at, owner_id) VALUES (?,?,?,?,?)",
                (conversation_id, "新会话", timestamp, timestamp, owner_id),
            )
        return self.conversation(conversation_id)

    def conversation(self, conversation_id: str) -> dict | None:
        return self._one("SELECT * FROM conversations WHERE id=?", (conversation_id,))

    def owned_conversation(self, owner_id: str, conversation_id: str) -> dict | None:
        return self._one("SELECT * FROM conversations WHERE owner_id=? AND id=?", (owner_id, conversation_id))

    def conversations(self, owner_id: str) -> list[dict]:
        return self._all("""
            SELECT c.*, a.last_finished_at, COALESCE(a.active_jobs, 0) AS active_jobs
            FROM conversations c
            LEFT JOIN (
                SELECT conversation_id, MAX(finished_at) AS last_finished_at,
                    SUM(state IN ('queued', 'running')) AS active_jobs
                FROM jobs GROUP BY conversation_id
            ) a ON a.conversation_id = c.id
            WHERE c.owner_id=?
            ORDER BY c.updated_at DESC
        """, (owner_id,))

    def files(self, owner_id: str) -> list[dict]:
        return self._all(
            "SELECT * FROM files WHERE source='sample' OR owner_id=? ORDER BY source, rowid",
            (owner_id,),
        )

    def file(self, file_id: str) -> dict | None:
        return self._one("SELECT * FROM files WHERE id=?", (file_id,))

    def owned_file(self, owner_id: str, file_id: str) -> dict | None:
        return self._one(
            "SELECT * FROM files WHERE id=? AND (owner_id=? OR source='sample')", (file_id, owner_id),
        )

    def add_file(self, owner_id: str, file_id: str, name: str, size: int, conversation_id: str | None = None) -> dict:
        with self.db:
            self._insert_file(owner_id, file_id, name, size, conversation_id)
        return self.file(file_id)

    def _insert_file(self, owner_id: str, file_id: str, name: str, size: int, conversation_id: str | None) -> None:
        self.db.execute(
            """INSERT INTO files
               (id, conversation_id, display_name, name_key, storage_name, source, size_bytes, owner_id, created_at)
               VALUES (?, ?, ?, ?, ?, 'upload', ?, ?, ?)""",
            (file_id, conversation_id, name, name.casefold(), file_id, size, owner_id, now()),
        )

    @staticmethod
    def public_file(row: dict) -> dict:
        return {key: row[key] for key in ("id", "display_name", "source", "size_bytes", "created_at")}

    @staticmethod
    def public_message(row: dict) -> dict:
        return {**row, "tool_calls": json.loads(row["tool_calls"]) if row["tool_calls"] else []}

    def message(self, message_id: int) -> dict:
        return self.public_message(self._one("SELECT * FROM messages WHERE id=?", (message_id,)))

    def user_message(self, conversation_id: str, client_id: str, text: str) -> tuple[dict, bool]:
        with self.db:
            cursor = self.db.execute(
                """INSERT INTO messages
                   (conversation_id, kind, status, content, client_message_id, created_at)
                   VALUES (?, 'user', 'queued', ?, ?, ?)
                   ON CONFLICT(conversation_id, client_message_id) DO NOTHING""",
                (conversation_id, text, client_id, now()),
            )
            if not cursor.rowcount:
                row = self._one(
                    "SELECT * FROM messages WHERE conversation_id=? AND client_message_id=?",
                    (conversation_id, client_id),
                )
                return self.public_message(row), False
            title = " ".join(text.split())[:48]
            self.db.execute(
                """UPDATE conversations SET updated_at=?,
                   title=CASE WHEN (SELECT COUNT(*) FROM messages
                                    WHERE conversation_id=? AND kind='user') = 1
                         THEN ? ELSE title END WHERE id=?""",
                (now(), conversation_id, title, conversation_id),
            )
        return self.message(cursor.lastrowid), True

    def add_message(
        self,
        conversation_id: str,
        turn_id: int,
        kind: str,
        *,
        content: str = "",
        status: str = "done",
        job_id: str | None = None,
        tool_calls: list[dict] | None = None,
        tool_call_id: str | None = None,
        invocation_id: int | None = None,
        source: str | None = None,
    ) -> dict:
        with self.db:
            if kind == "summary":
                existing = self._one("SELECT id FROM messages WHERE kind='summary' AND job_id=?", (job_id,))
                if existing:
                    return self.change_message(existing["id"], content=content, status=status)
            cursor = self.db.execute(
                """INSERT INTO messages
                   (conversation_id, turn_id, kind, status, content, job_id,
                    tool_calls, tool_call_id, invocation_id, source, created_at)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (conversation_id, turn_id, kind, status, content, job_id,
                 json.dumps(tool_calls, ensure_ascii=False) if tool_calls else None,
                 tool_call_id, invocation_id, source, now()),
            )
        return self.message(cursor.lastrowid)

    def change_message(self, message_id: int, *, status: str | None = None, content: str | None = None) -> dict:
        with self.db:
            self.db.execute(
                """UPDATE messages SET status=COALESCE(?, status), content=COALESCE(?, content),
                   revision=revision+1 WHERE id=?""",
                (status, content, message_id),
            )
        return self.message(message_id)

    def append_delta(self, message_id: int, delta: str) -> int:
        with self.db:
            return self.db.execute(
                "UPDATE messages SET content=content||?, revision=revision+1 WHERE id=? RETURNING revision",
                (delta, message_id),
            ).fetchone()[0]

    def fail_drafts(self, conversation_id: str, turn_id: int, job_id: str | None) -> list[dict]:
        with self.db:
            rows = self.db.execute(
                """UPDATE messages SET status='interrupted', revision=revision+1
                   WHERE conversation_id=? AND turn_id=? AND job_id IS ? AND status='running'
                   RETURNING id""",
                (conversation_id, turn_id, job_id),
            ).fetchall()
        return [self.message(row["id"]) for row in rows]

    def history_turns(
        self, conversation_id: str, *, before: int, after: int, limit: int,
        newest: bool = True, include_task_summaries: bool = True,
    ) -> list[HistoryTurn]:
        direction = "DESC" if newest else "ASC"
        rows = self._all(
            f"""WITH turns AS (
                    SELECT id FROM messages WHERE conversation_id=? AND kind='user'
                    AND status IN ('done', 'error', 'interrupted') AND id>? AND id<?
                    ORDER BY id {direction} LIMIT ?
                )
                SELECT t.id AS origin, m.kind, m.content
                FROM turns t JOIN messages m ON COALESCE(m.turn_id, m.id)=t.id
                WHERE m.conversation_id=? AND (
                    m.kind='user' OR (m.status='done' AND
                        (m.kind='assistant' OR (? AND m.kind='summary')))
                )
                ORDER BY t.id, m.id""",
            (conversation_id, after, before, limit, conversation_id, include_task_summaries),
        )
        groups: dict[int, list[dict]] = {}
        for row in rows:
            if row["content"]:
                groups.setdefault(row["origin"], []).append({
                    "role": "user" if row["kind"] == "user" else "assistant",
                    "content": row["content"][:2500],
                })
        return [HistoryTurn(turn_id, messages) for turn_id, messages in groups.items()]

    def conversation_summary(self, conversation_id: str) -> dict | None:
        return self._one("SELECT * FROM conversation_summaries WHERE conversation_id=?", (conversation_id,))

    def save_conversation_summary(self, conversation_id: str, through_turn_id: int, content: str) -> None:
        with self.db:
            self.db.execute("""
                INSERT INTO conversation_summaries (conversation_id, through_turn_id, content, updated_at)
                VALUES (?,?,?,?)
                ON CONFLICT(conversation_id) DO UPDATE SET through_turn_id=excluded.through_turn_id,
                    content=excluded.content, updated_at=excluded.updated_at
                WHERE excluded.through_turn_id>conversation_summaries.through_turn_id
            """, (conversation_id, through_turn_id, content, now()))

    def snapshot(self, owner_id: str, conversation_id: str) -> dict | None:
        conversation = self.owned_conversation(owner_id, conversation_id)
        if not conversation:
            return None
        return {
            "conversation": conversation,
            "messages": [self.public_message(row) for row in self._all(
                "SELECT * FROM messages WHERE conversation_id=? ORDER BY id", (conversation_id,)
            )],
            "jobs": self._all(JOB_SELECT + " WHERE j.conversation_id=? ORDER BY j.created_at DESC", (conversation_id,)),
            "files": [self.public_file(row) for row in self.files(owner_id)],
            "input_requests": self._all(INPUT_REQUEST_SELECT + " WHERE request.conversation_id=? ORDER BY request.created_at",
                                        (conversation_id,)),
        }

    def recent_jobs(self, conversation_id: str, before_user_id: int) -> list[dict]:
        return self._all(
            JOB_SELECT + " WHERE j.conversation_id=? AND j.origin_message_id<? ORDER BY j.created_at DESC LIMIT 8",
            (conversation_id, before_user_id),
        )

    def job_filenames(self, conversation_id: str, before_user_id: int) -> list[str]:
        rows = self._all("""
            SELECT DISTINCT COALESCE(j.input_name, f.display_name) AS filename
            FROM jobs j JOIN files f ON f.id=j.file_id
            WHERE j.conversation_id=? AND j.origin_message_id<?
            ORDER BY filename
        """, (conversation_id, before_user_id))
        return [row["filename"] for row in rows]

    def jobs_for_filename(self, conversation_id: str, before_user_id: int, filename: str) -> list[dict]:
        return self._all(JOB_SELECT + """
            WHERE j.conversation_id=? AND j.origin_message_id<?
            AND COALESCE(j.input_name, f.display_name)=?
            ORDER BY j.created_at DESC LIMIT 3
        """, (conversation_id, before_user_id, filename))

    def job(self, job_id: str) -> dict | None:
        return self._one(JOB_SELECT + " WHERE j.id=?", (job_id,))

    def owned_job(self, owner_id: str, job_id: str) -> dict | None:
        return self._one(JOB_SELECT + " WHERE c.owner_id=? AND j.id=?", (owner_id, job_id))

    def jobs(self, owner_id: str) -> list[dict]:
        return self._all(JOB_SELECT + " WHERE c.owner_id=? ORDER BY j.created_at DESC", (owner_id,))

    def jobs_with_prefix(self, conversation_id: str, prefix: str) -> list[dict]:
        return self._all(
            JOB_SELECT + " WHERE j.conversation_id=? AND j.id LIKE ? LIMIT 2", (conversation_id, prefix + "%")
        )

    def submit(self, conversation_id: str, turn_id: int, file_id: str, analysis_type: str) -> tuple[dict, bool]:
        with self.db:
            return self._submit(conversation_id, turn_id, file_id, analysis_type)

    def _submit(self, conversation_id: str, turn_id: int, file_id: str, analysis_type: str) -> tuple[dict, bool]:
        job_id = uuid4().hex
        cursor = self.db.execute(
            """INSERT INTO jobs (id, conversation_id, origin_message_id, file_id, analysis_type, created_at, input_name)
               VALUES (?, ?, ?, ?, ?, ?, (SELECT display_name FROM files WHERE id=?))
               ON CONFLICT(origin_message_id, file_id, analysis_type) WHERE retry_of_job_id IS NULL DO NOTHING""",
            (job_id, conversation_id, turn_id, file_id, analysis_type, now(), file_id),
        )
        if cursor.rowcount:
            return self.job(job_id), True
        row = self._one(JOB_SELECT + """
            WHERE j.origin_message_id=? AND j.file_id=? AND j.analysis_type=? AND j.retry_of_job_id IS NULL
        """, (turn_id, file_id, analysis_type))
        return row, False

    def retry_job(self, job: dict) -> tuple[dict, bool]:
        with self.db:
            self.db.execute("BEGIN IMMEDIATE")
            return self._retry(job, job["file_id"])

    def _retry(self, job: dict, file_id: str, retry_at: str | None = None) -> tuple[dict, bool]:
        existing = self._one(JOB_SELECT + " WHERE j.retry_of_job_id=?", (job["id"],))
        if existing:
            return existing, False
        job_id = uuid4().hex
        self.db.execute(
            """INSERT INTO jobs (id, conversation_id, origin_message_id, file_id, analysis_type, created_at, input_name,
                                 retry_of_job_id, root_job_id, attempt_number, retry_kind, automatic_retries, available_at)
               VALUES (?, ?, ?, ?, ?, ?, (SELECT display_name FROM files WHERE id=?), ?, ?, ?, ?, ?, ?)""",
            (job_id, job["conversation_id"], job["origin_message_id"], file_id, job["analysis_type"], now(), file_id,
             job["id"], job["root_job_id"] or job["id"], job["attempt_number"] + 1,
             "automatic" if retry_at else "manual", job["automatic_retries"] + 1 if retry_at else 0, retry_at),
        )
        self.db.execute("UPDATE jobs SET revision=revision+1 WHERE id=?", (job["id"],))
        return self.job(job_id), True

    def queued_jobs(self) -> list[dict]:
        return self._all(JOB_SELECT + " WHERE j.state='queued' AND c.owner_id IS NOT NULL ORDER BY j.created_at")

    def request_input(self, conversation_id: str, turn_id: int, analysis_type: str, filename: str) -> tuple[dict, bool]:
        with self.db:
            return self._request_input(conversation_id, turn_id, analysis_type, filename)

    def _request_input(
        self, conversation_id: str, turn_id: int, analysis_type: str, filename: str, failed_job_id: str | None = None,
    ) -> tuple[dict, bool]:
        request_id = uuid4().hex
        cursor = self.db.execute("""
            INSERT INTO input_requests (id, conversation_id, origin_message_id, analysis_type, filename, failed_job_id, created_at)
            VALUES (?, ?, ?, ?, ?, ?, ?) ON CONFLICT DO NOTHING
        """, (request_id, conversation_id, turn_id, analysis_type, filename, failed_job_id, now()))
        if cursor.rowcount:
            return self.input_request(request_id), True
        row = self._one(INPUT_REQUEST_SELECT + """
            WHERE request.origin_message_id=? AND request.analysis_type=? AND request.filename=? AND request.failed_job_id IS ?
        """, (turn_id, analysis_type, filename, failed_job_id))
        return row, False

    def input_request(self, request_id: str) -> dict | None:
        return self._one(INPUT_REQUEST_SELECT + " WHERE request.id=?", (request_id,))

    def owned_input_request(self, owner_id: str, request_id: str) -> dict | None:
        return self._one(INPUT_REQUEST_SELECT + " WHERE c.owner_id=? AND request.id=?", (owner_id, request_id))

    def resolve_input(
        self, owner_id: str, request_id: str, file_id: str, *, repaired_file: tuple[str, int] | None = None,
    ) -> tuple[dict, dict, bool]:
        with self.db:
            self.db.execute("BEGIN IMMEDIATE")
            request = self.owned_input_request(owner_id, request_id)
            if request["resolved_job_id"]:
                return request, self.job(request["resolved_job_id"]), False
            if repaired_file:
                name, size = repaired_file
                self._insert_file(owner_id, file_id, name, size, request["conversation_id"])
            if request["failed_job_id"]:
                job, created = self._retry(self.job(request["failed_job_id"]), file_id)
            else:
                job, created = self._submit(request["conversation_id"], request["origin_message_id"],
                                            file_id, request["analysis_type"])
            self.db.execute("UPDATE input_requests SET resolved_job_id=?, revision=revision+1 WHERE id=?",
                            (job["id"], request_id))
            return self.input_request(request_id), job, created

    def claim_job(self, job_id: str) -> dict | None:
        timestamp = now()
        with self.db:
            cursor = self.db.execute(
                """UPDATE jobs SET state='running', started_at=?, revision=revision+1
                   WHERE id=? AND state='queued' AND (available_at IS NULL OR available_at<=?)""",
                (timestamp, job_id, timestamp),
            )
        return self.job(job_id) if cursor.rowcount else None

    def finish_job(
        self, job_id: str, exit_code: int | None, runner_error: str | None = None, *, failure: Failure | None = None,
        retry_at: str | None = None,
    ) -> dict:
        if failure:
            runner_error = failure.detail
        state = "succeeded" if exit_code == 0 and not (failure or runner_error) else "failed"
        with self.db:
            changed = self.db.execute(
                """UPDATE jobs SET state=?, finished_at=?, exit_code=?, runner_error=?, failure_category=?,
                                   failure_code=?, failure_stage=?, revision=revision+1
                   WHERE id=? AND state IN ('queued', 'running')""",
                (state, now(), exit_code, runner_error, failure.category if failure else None,
                 failure.code if failure else None, failure.stage if failure else None, job_id),
            )
            if changed.rowcount and (retry_at or (failure and failure.category == "input")):
                job = self.job(job_id)
                if retry_at:
                    self._retry(job, job["file_id"], retry_at)
                else:
                    self._request_input(job["conversation_id"], job["origin_message_id"], job["analysis_type"],
                                        job["filename"], job_id)
        return self.job(job_id)

    def change_summary(self, job_id: str, before: str, after: str) -> dict | None:
        with self.db:
            cursor = self.db.execute(
                """UPDATE jobs SET summary_state=?, revision=revision+1
                   WHERE id=? AND summary_state=? AND state IN ('succeeded', 'failed')""",
                (after, job_id, before),
            )
        return self.job(job_id) if cursor.rowcount else None

    def finish_summary(self, job_id: str, message_id: int, outcome: str) -> tuple[dict, dict]:
        with self.db:
            self.db.execute("UPDATE messages SET status='done', revision=revision+1 WHERE id=?", (message_id,))
            self.db.execute("UPDATE jobs SET summary_state=?, revision=revision+1 WHERE id=?", (outcome, job_id))
        return self.message(message_id), self.job(job_id)

    def recover(self) -> list[dict]:
        with self.db:
            self.db.execute(
                """UPDATE jobs SET state='failed', finished_at=?, runner_error=?, failure_category='interrupted',
                                   failure_code='INTERRUPTED', failure_stage='execution', revision=revision+1
                   WHERE state='running'""",
                (now(), "后端重启时任务尚未确认完成，请重新提交计算。"),
            )
            self.db.execute(
                "UPDATE jobs SET summary_state='pending', revision=revision+1 WHERE summary_state='running'"
            )
            self.db.execute(
                "UPDATE messages SET status='interrupted', revision=revision+1 WHERE status IN ('running', 'queued')"
            )
            self.db.execute("""
                UPDATE messages SET status='interrupted', revision=revision+1
                WHERE kind='invocation' AND status='done' AND json_array_length(tool_calls) >
                    (SELECT COUNT(*) FROM messages result WHERE result.invocation_id=messages.id)
            """)
        return self._all(JOB_SELECT + """
            WHERE c.owner_id IS NOT NULL AND j.summary_state='pending' AND j.state IN ('succeeded', 'failed')
        """)

    def assign_legacy(self, owner_id: str) -> tuple[int, int]:
        """Associate a previous single-user workspace with an explicit local account."""
        with self.db:
            self.db.execute("BEGIN IMMEDIATE")
            used = {row["name_key"] for row in self.files(owner_id)}
            uploads = self._all("SELECT * FROM files WHERE owner_id IS NULL AND source='upload' ORDER BY rowid")
            for file in uploads:
                name = file["display_name"]
                path = PurePath(name)
                index = 1
                while name.casefold() in used:
                    index += 1
                    suffix = f"-{index}{path.suffix}"
                    name = path.stem[:120 - len(suffix)] + suffix
                used.add(name.casefold())
                self.db.execute(
                    "UPDATE files SET owner_id=?, display_name=?, name_key=? WHERE id=?",
                    (owner_id, name, name.casefold(), file["id"]),
                )
            conversations = self.db.execute("UPDATE conversations SET owner_id=? WHERE owner_id IS NULL", (owner_id,))
        return conversations.rowcount, len(uploads)
