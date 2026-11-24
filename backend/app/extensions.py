"""Account-owned MCP connections and text Skills."""

import json
import re
import sqlite3
import unicodedata
from uuid import NAMESPACE_URL, uuid4, uuid5

from mcp.types import Tool

from .store import now

RESEARCH_SERVER_NAME = "本机科研任务服务"


class ExtensionStore:
    def __init__(self, db: sqlite3.Connection):
        self.db = db
        with db:
            db.execute("BEGIN IMMEDIATE")
            columns = {row["name"] for row in db.execute("PRAGMA table_info(users)")}
            if "research_mcp_added" not in columns:
                db.execute("ALTER TABLE users ADD COLUMN research_mcp_added INTEGER NOT NULL DEFAULT 0")
                db.execute("UPDATE users SET research_mcp_added=1")
            db.execute("""
                CREATE TABLE IF NOT EXISTS mcp_servers (
                    id TEXT PRIMARY KEY,
                    owner_id TEXT NOT NULL REFERENCES users(id),
                    name TEXT NOT NULL,
                    endpoint TEXT NOT NULL,
                    enabled INTEGER NOT NULL DEFAULT 1,
                    synced_at TEXT NOT NULL,
                    UNIQUE(owner_id, endpoint)
                )
            """)
            db.execute("""
                CREATE TABLE IF NOT EXISTS mcp_tools (
                    server_id TEXT NOT NULL REFERENCES mcp_servers(id) ON DELETE CASCADE,
                    name TEXT NOT NULL,
                    function_name TEXT NOT NULL UNIQUE,
                    description TEXT NOT NULL,
                    input_schema TEXT NOT NULL,
                    enabled INTEGER NOT NULL DEFAULT 0,
                    available INTEGER NOT NULL DEFAULT 1,
                    PRIMARY KEY(server_id, name)
                )
            """)
            db.execute("""
                CREATE TABLE IF NOT EXISTS skills (
                    id TEXT PRIMARY KEY,
                    owner_id TEXT NOT NULL REFERENCES users(id),
                    name TEXT NOT NULL,
                    trigger TEXT NOT NULL,
                    trigger_key TEXT NOT NULL,
                    instructions TEXT NOT NULL,
                    enabled INTEGER NOT NULL DEFAULT 1,
                    UNIQUE(owner_id, trigger_key)
                )
            """)

    def has_research_server(self, owner_id: str) -> bool:
        row = self.db.execute("SELECT research_mcp_added FROM users WHERE id=?", (owner_id,)).fetchone()
        return bool(row and row["research_mcp_added"])

    def add_research_server(self, owner_id: str) -> None:
        with self.db:
            self.db.execute("UPDATE users SET research_mcp_added=1 WHERE id=?", (owner_id,))

    def servers(self, owner_id: str) -> list[dict]:
        rows = self.db.execute("""
            SELECT s.*, (SELECT count(*) FROM mcp_tools t WHERE t.server_id=s.id AND t.available=1) AS tool_count
            FROM mcp_servers s WHERE s.owner_id=? ORDER BY s.name, s.id
        """, (owner_id,))
        return [{**dict(row), "enabled": bool(row["enabled"])} for row in rows]

    def server(self, owner_id: str, server_id: str) -> dict | None:
        row = self.db.execute("SELECT * FROM mcp_servers WHERE id=? AND owner_id=?", (server_id, owner_id)).fetchone()
        return dict(row) if row else None

    def add_server(self, owner_id: str, name: str, endpoint: str, tools: list[Tool]) -> str:
        server_id = uuid4().hex
        with self.db:
            self.db.execute("""
                INSERT INTO mcp_servers (id, owner_id, name, endpoint, synced_at) VALUES (?, ?, ?, ?, ?)
            """, (server_id, owner_id, name, endpoint, now()))
            self._save_catalog(server_id, tools)
        return server_id

    def refresh_server(self, owner_id: str, server_id: str, tools: list[Tool]) -> bool:
        with self.db:
            updated = self.db.execute(
                "UPDATE mcp_servers SET synced_at=? WHERE id=? AND owner_id=?", (now(), server_id, owner_id),
            ).rowcount
            if updated:
                self._save_catalog(server_id, tools)
        return bool(updated)

    def _save_catalog(self, server_id: str, tools: list[Tool]) -> None:
        self.db.execute("UPDATE mcp_tools SET available=0 WHERE server_id=?", (server_id,))
        for tool in tools:
            safe_name = re.sub(r"[^a-zA-Z0-9_-]", "_", tool.name)[:42]
            suffix = uuid5(NAMESPACE_URL, f"{server_id}/{tool.name}").hex[:16]
            function_name = f"mcp_{safe_name}_{suffix}"
            self.db.execute("""
                INSERT INTO mcp_tools (server_id, name, function_name, description, input_schema)
                VALUES (?, ?, ?, ?, ?)
                ON CONFLICT(server_id, name) DO UPDATE SET
                    description=excluded.description, input_schema=excluded.input_schema, available=1
            """, (server_id, tool.name, function_name, tool.description or "",
                  json.dumps(tool.input_schema, ensure_ascii=False)))

    def set_server_enabled(self, owner_id: str, server_id: str, enabled: bool) -> bool:
        with self.db:
            return bool(self.db.execute(
                "UPDATE mcp_servers SET enabled=? WHERE id=? AND owner_id=?", (enabled, server_id, owner_id),
            ).rowcount)

    def set_tool_enabled(self, owner_id: str, function_name: str, enabled: bool) -> bool:
        with self.db:
            return bool(self.db.execute("""
                UPDATE mcp_tools SET enabled=? WHERE function_name=? AND
                server_id IN (SELECT id FROM mcp_servers WHERE owner_id=?)
            """, (enabled, function_name, owner_id)).rowcount)

    def remote_tools(self, owner_id: str, function_name: str | None = None) -> list[dict]:
        rows = self.db.execute("""
            SELECT t.*, s.name AS server_name, s.enabled AS server_enabled, s.endpoint
            FROM mcp_tools t JOIN mcp_servers s ON s.id=t.server_id
            WHERE s.owner_id=? AND (? IS NULL OR t.function_name=?) ORDER BY s.name, t.name, t.function_name
        """, (owner_id, function_name, function_name))
        return [{**dict(row), "input_schema": json.loads(row["input_schema"])} for row in rows]

    def skills(self, owner_id: str) -> list[dict]:
        rows = self.db.execute("""
            SELECT id, name, trigger, instructions, enabled FROM skills WHERE owner_id=? ORDER BY name, id
        """, (owner_id,))
        return [{**dict(row), "enabled": bool(row["enabled"])} for row in rows]

    def save_skill(
        self, owner_id: str, name: str, trigger: str, instructions: str, enabled: bool, skill_id: str | None = None,
    ) -> str | None:
        trigger_key = unicodedata.normalize("NFKC", trigger).casefold()
        with self.db:
            if skill_id is None:
                skill_id = uuid4().hex
                self.db.execute("""
                    INSERT INTO skills (id, owner_id, name, trigger, trigger_key, instructions, enabled)
                    VALUES (?, ?, ?, ?, ?, ?, ?)
                """, (skill_id, owner_id, name, trigger, trigger_key, instructions, enabled))
            elif not self.db.execute("""
                UPDATE skills SET name=?, trigger=?, trigger_key=?, instructions=?, enabled=?
                WHERE id=? AND owner_id=?
            """, (name, trigger, trigger_key, instructions, enabled, skill_id, owner_id)).rowcount:
                return None
        return skill_id

    def matching_skill(self, owner_id: str, text: str) -> dict | None:
        query = unicodedata.normalize("NFKC", text).casefold()
        row = self.db.execute("""
            SELECT name, instructions FROM skills WHERE owner_id=? AND enabled=1 AND instr(?, trigger_key)>0
            ORDER BY length(trigger_key) DESC, id LIMIT 1
        """, (owner_id, query)).fetchone()
        return dict(row) if row else None
