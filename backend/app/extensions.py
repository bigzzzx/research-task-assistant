"""Account-owned MCP connections and text Skills."""

import json
import re
import sqlite3
from uuid import NAMESPACE_URL, uuid4, uuid5

from mcp.types import Tool

from .skills import SkillDocument, render_skill
from .store import now

RESEARCH_SERVER_NAME = "本机科研任务服务"


class ExtensionStore:
    def __init__(self, db: sqlite3.Connection):
        self.db = db
        with db:
            db.execute("BEGIN IMMEDIATE")
            db.execute("""
                CREATE TABLE IF NOT EXISTS mcp_servers (
                    id TEXT PRIMARY KEY,
                    owner_id TEXT NOT NULL REFERENCES users(id),
                    name TEXT NOT NULL,
                    endpoint TEXT NOT NULL,
                    transport TEXT NOT NULL DEFAULT 'streamable-http',
                    enabled INTEGER NOT NULL DEFAULT 1,
                    synced_at TEXT NOT NULL,
                    UNIQUE(owner_id, endpoint)
                )
            """)
            columns = {row["name"] for row in db.execute("PRAGMA table_info(mcp_servers)")}
            if "transport" not in columns:
                db.execute("ALTER TABLE mcp_servers ADD COLUMN transport TEXT NOT NULL DEFAULT 'streamable-http'")
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
                CREATE TABLE IF NOT EXISTS disabled_research_tools (
                    owner_id TEXT NOT NULL REFERENCES users(id) ON DELETE CASCADE,
                    name TEXT NOT NULL,
                    PRIMARY KEY(owner_id, name)
                )
            """)
            initialized = db.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='agent_skills'").fetchone()
            db.execute("""
                CREATE TABLE IF NOT EXISTS agent_skills (
                    id TEXT PRIMARY KEY,
                    owner_id TEXT NOT NULL REFERENCES users(id),
                    name TEXT NOT NULL,
                    description TEXT NOT NULL,
                    document TEXT NOT NULL,
                    files TEXT NOT NULL DEFAULT '{}',
                    enabled INTEGER NOT NULL DEFAULT 1,
                    UNIQUE(owner_id, name)
                )
            """)
            if not initialized and db.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='skills'").fetchone():
                rows = db.execute("SELECT id, owner_id, name, trigger, instructions, enabled FROM skills").fetchall()
                for row in rows:
                    name = "legacy-" + row["id"]
                    description = f'{row["name"]}。当用户希望完成相关任务时使用。原触发短语：{row["trigger"]}'
                    document = render_skill(name, description, row["instructions"])
                    db.execute("""
                        INSERT INTO agent_skills (id, owner_id, name, description, document, enabled)
                        VALUES (?, ?, ?, ?, ?, ?)
                    """, (row["id"], row["owner_id"], name, description, document, row["enabled"]))

    def servers(self, owner_id: str) -> list[dict]:
        rows = self.db.execute("""
            SELECT s.*, (SELECT count(*) FROM mcp_tools t WHERE t.server_id=s.id AND t.available=1) AS tool_count
            FROM mcp_servers s WHERE s.owner_id=? ORDER BY s.name, s.id
        """, (owner_id,))
        return [{**dict(row), "enabled": bool(row["enabled"])} for row in rows]

    def server(self, owner_id: str, server_id: str) -> dict | None:
        row = self.db.execute("SELECT * FROM mcp_servers WHERE id=? AND owner_id=?", (server_id, owner_id)).fetchone()
        return dict(row) if row else None

    def add_server(self, owner_id: str, name: str, endpoint: str, transport: str, tools: list[Tool]) -> str:
        server_id = uuid4().hex
        with self.db:
            self.db.execute("""
                INSERT INTO mcp_servers (id, owner_id, name, endpoint, transport, synced_at) VALUES (?, ?, ?, ?, ?, ?)
            """, (server_id, owner_id, name, endpoint, transport, now()))
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

    def disabled_research_tools(self, owner_id: str) -> set[str]:
        rows = self.db.execute("SELECT name FROM disabled_research_tools WHERE owner_id=?", (owner_id,))
        return {row["name"] for row in rows}

    def set_research_tool_enabled(self, owner_id: str, name: str, enabled: bool) -> None:
        with self.db:
            if enabled:
                self.db.execute("DELETE FROM disabled_research_tools WHERE owner_id=? AND name=?", (owner_id, name))
            else:
                self.db.execute("""
                    INSERT INTO disabled_research_tools (owner_id, name) VALUES (?, ?)
                    ON CONFLICT(owner_id, name) DO NOTHING
                """, (owner_id, name))

    def external_tools(self, owner_id: str, function_name: str | None = None) -> list[dict]:
        rows = self.db.execute("""
            SELECT t.*, s.name AS server_name, s.enabled AS server_enabled, s.endpoint, s.transport
            FROM mcp_tools t JOIN mcp_servers s ON s.id=t.server_id
            WHERE s.owner_id=? AND (? IS NULL OR t.function_name=?) ORDER BY s.name, t.name, t.function_name
        """, (owner_id, function_name, function_name))
        return [{**dict(row), "input_schema": json.loads(row["input_schema"])} for row in rows]

    def skills(self, owner_id: str) -> list[dict]:
        rows = self.db.execute("""
            SELECT id, name, description, enabled FROM agent_skills WHERE owner_id=? ORDER BY name
        """, (owner_id,))
        return [{**dict(row), "enabled": bool(row["enabled"])} for row in rows]

    def save_skill(
        self, owner_id: str, document: SkillDocument, enabled: bool,
        *, skill_id: str | None = None, files: dict[str, str] | None = None,
    ) -> str | None:
        with self.db:
            if skill_id is not None:
                updated = self.db.execute("""
                    UPDATE agent_skills SET name=?, description=?, document=?, enabled=?
                    WHERE id=? AND owner_id=?
                """, (document.name, document.description, document.source, enabled, skill_id, owner_id))
                return skill_id if updated.rowcount else None
            skill_id = uuid4().hex
            self.db.execute("""
                INSERT INTO agent_skills (id, owner_id, name, description, document, enabled, files)
                VALUES (?, ?, ?, ?, ?, ?, ?)
            """, (skill_id, owner_id, document.name, document.description, document.source, enabled, json.dumps(files or {})))
            return skill_id

    def set_skill_enabled(self, owner_id: str, skill_id: str, enabled: bool) -> bool:
        with self.db:
            return bool(self.db.execute(
                "UPDATE agent_skills SET enabled=? WHERE id=? AND owner_id=?", (enabled, skill_id, owner_id),
            ).rowcount)

    def skill(self, owner_id: str, *, skill_id: str | None = None, name: str | None = None) -> dict | None:
        row = self.db.execute("""
            SELECT id, name, description, document, files, enabled FROM agent_skills
            WHERE owner_id=? AND (id=? OR (name=? AND enabled=1))
        """, (owner_id, skill_id, name)).fetchone()
        return {**dict(row), "files": json.loads(row["files"]), "enabled": bool(row["enabled"])} if row else None
