"""P1 data store and durable operation queue for AI and engagement work."""

from __future__ import annotations

import json
import threading
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any

from .db import Database
from .governance import contains_sensitive_information
from .utils import json_dumps, now_iso

OPERATION_KINDS = {
    "search_brief",
    "screen_results",
    "material_research",
    "agent_draft",
    "comment",
    "comment_reply",
    "dm_sync",
    "dm_reply",
    "dm_outbound",
    "image_decompose",
}

SCHEMA = """
CREATE TABLE IF NOT EXISTS personas (
 id INTEGER PRIMARY KEY AUTOINCREMENT, account_id INTEGER NOT NULL, name TEXT NOT NULL,
 version INTEGER NOT NULL DEFAULT 1, brand_identity TEXT NOT NULL DEFAULT '', tone TEXT NOT NULL DEFAULT '',
 expertise_json TEXT NOT NULL DEFAULT '[]', common_phrases_json TEXT NOT NULL DEFAULT '[]',
 allowed_cta_json TEXT NOT NULL DEFAULT '[]', prohibited_claims_json TEXT NOT NULL DEFAULT '[]',
 examples_json TEXT NOT NULL DEFAULT '[]', enabled INTEGER NOT NULL DEFAULT 1,
 created_at TEXT NOT NULL, updated_at TEXT NOT NULL,
 UNIQUE(account_id,name,version), FOREIGN KEY(account_id) REFERENCES accounts(id) ON DELETE CASCADE);
CREATE TABLE IF NOT EXISTS knowledge_sources (
 id INTEGER PRIMARY KEY AUTOINCREMENT, name TEXT NOT NULL, source_type TEXT NOT NULL DEFAULT 'owned',
 source_url TEXT NOT NULL DEFAULT '', local_path TEXT NOT NULL DEFAULT '',
 authorization_status TEXT NOT NULL DEFAULT 'unverified', usage_restrictions TEXT NOT NULL DEFAULT '',
 checksum TEXT NOT NULL DEFAULT '', created_at TEXT NOT NULL, updated_at TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS material_candidates (
 id INTEGER PRIMARY KEY AUTOINCREMENT, note_id INTEGER, source_id INTEGER,
 relevance_score REAL NOT NULL DEFAULT 0, cluster_name TEXT NOT NULL DEFAULT '',
 insights_json TEXT NOT NULL DEFAULT '{}', authorization_status TEXT NOT NULL DEFAULT 'unverified',
 status TEXT NOT NULL DEFAULT 'candidate', created_at TEXT NOT NULL, updated_at TEXT NOT NULL,
 FOREIGN KEY(note_id) REFERENCES notes(id) ON DELETE SET NULL,
 FOREIGN KEY(source_id) REFERENCES knowledge_sources(id) ON DELETE SET NULL);
CREATE TABLE IF NOT EXISTS derivative_tasks (
 id INTEGER PRIMARY KEY AUTOINCREMENT, candidate_id INTEGER, persona_id INTEGER, title TEXT NOT NULL,
 brief_json TEXT NOT NULL DEFAULT '{}', export_dir TEXT NOT NULL DEFAULT '', final_asset_dir TEXT NOT NULL DEFAULT '',
 rights_declared INTEGER NOT NULL DEFAULT 0, status TEXT NOT NULL DEFAULT 'draft',
 created_at TEXT NOT NULL, updated_at TEXT NOT NULL,
 FOREIGN KEY(candidate_id) REFERENCES material_candidates(id) ON DELETE SET NULL,
 FOREIGN KEY(persona_id) REFERENCES personas(id) ON DELETE SET NULL);
CREATE TABLE IF NOT EXISTS agent_runs (
 id INTEGER PRIMARY KEY AUTOINCREMENT, kind TEXT NOT NULL, provider TEXT NOT NULL DEFAULT '',
 model TEXT NOT NULL DEFAULT '', status TEXT NOT NULL DEFAULT 'pending', input_json TEXT NOT NULL DEFAULT '{}',
 output_json TEXT NOT NULL DEFAULT '{}', error TEXT, created_at TEXT NOT NULL, finished_at TEXT);
CREATE TABLE IF NOT EXISTS policy_rules (
 id INTEGER PRIMARY KEY AUTOINCREMENT, version INTEGER NOT NULL UNIQUE, name TEXT NOT NULL,
 rules_json TEXT NOT NULL, active INTEGER NOT NULL DEFAULT 0, created_at TEXT NOT NULL, activated_at TEXT);
CREATE TABLE IF NOT EXISTS drafts (
 id INTEGER PRIMARY KEY AUTOINCREMENT, kind TEXT NOT NULL, account_id INTEGER, persona_id INTEGER,
 agent_run_id INTEGER, title TEXT NOT NULL DEFAULT '', content TEXT NOT NULL,
 context_json TEXT NOT NULL DEFAULT '{}', sources_json TEXT NOT NULL DEFAULT '[]', model TEXT NOT NULL DEFAULT '',
 prompt_version TEXT NOT NULL DEFAULT '', persona_version INTEGER, policy_rule_id INTEGER,
 status TEXT NOT NULL DEFAULT 'pending_review', created_at TEXT NOT NULL, updated_at TEXT NOT NULL,
 FOREIGN KEY(account_id) REFERENCES accounts(id) ON DELETE SET NULL,
 FOREIGN KEY(persona_id) REFERENCES personas(id) ON DELETE SET NULL,
 FOREIGN KEY(agent_run_id) REFERENCES agent_runs(id) ON DELETE SET NULL,
 FOREIGN KEY(policy_rule_id) REFERENCES policy_rules(id) ON DELETE SET NULL);
CREATE TABLE IF NOT EXISTS engagement_threads (
 id INTEGER PRIMARY KEY AUTOINCREMENT, account_id INTEGER NOT NULL, channel TEXT NOT NULL,
 external_user_id TEXT NOT NULL, display_name TEXT NOT NULL DEFAULT '', platform_thread_ref TEXT NOT NULL DEFAULT '',
 lead_reason TEXT NOT NULL DEFAULT '', warm_lead INTEGER NOT NULL DEFAULT 0, status TEXT NOT NULL DEFAULT 'active',
 last_activity_at TEXT, created_at TEXT NOT NULL, updated_at TEXT NOT NULL,
 UNIQUE(account_id,channel,external_user_id), FOREIGN KEY(account_id) REFERENCES accounts(id) ON DELETE CASCADE);
CREATE TABLE IF NOT EXISTS engagement_tasks (
 id INTEGER PRIMARY KEY AUTOINCREMENT, thread_id INTEGER, account_id INTEGER NOT NULL, draft_id INTEGER,
 kind TEXT NOT NULL, target_note_id TEXT NOT NULL DEFAULT '', target_comment_id TEXT NOT NULL DEFAULT '',
 target_user_id TEXT NOT NULL DEFAULT '', content TEXT NOT NULL, status TEXT NOT NULL DEFAULT 'pending_review',
 approved_at TEXT, policy_rule_id INTEGER, idempotency_key TEXT NOT NULL UNIQUE, error TEXT,
 created_at TEXT NOT NULL, updated_at TEXT NOT NULL,
 FOREIGN KEY(thread_id) REFERENCES engagement_threads(id) ON DELETE SET NULL,
 FOREIGN KEY(account_id) REFERENCES accounts(id) ON DELETE CASCADE,
 FOREIGN KEY(draft_id) REFERENCES drafts(id) ON DELETE SET NULL,
 FOREIGN KEY(policy_rule_id) REFERENCES policy_rules(id) ON DELETE SET NULL);
CREATE TABLE IF NOT EXISTS engagement_attempts (
 id INTEGER PRIMARY KEY AUTOINCREMENT, task_id INTEGER NOT NULL, status TEXT NOT NULL DEFAULT 'running',
 stage TEXT NOT NULL DEFAULT 'starting', error_category TEXT, message TEXT, screenshot_path TEXT,
 platform_result_ref TEXT, started_at TEXT NOT NULL, finished_at TEXT,
 FOREIGN KEY(task_id) REFERENCES engagement_tasks(id) ON DELETE CASCADE);
CREATE TABLE IF NOT EXISTS policy_decisions (
 id INTEGER PRIMARY KEY AUTOINCREMENT, task_id INTEGER, draft_id INTEGER, rule_id INTEGER NOT NULL,
 decision TEXT NOT NULL, reasons_json TEXT NOT NULL DEFAULT '[]', signals_json TEXT NOT NULL DEFAULT '{}',
 created_at TEXT NOT NULL, FOREIGN KEY(task_id) REFERENCES engagement_tasks(id) ON DELETE CASCADE,
 FOREIGN KEY(draft_id) REFERENCES drafts(id) ON DELETE CASCADE, FOREIGN KEY(rule_id) REFERENCES policy_rules(id));
CREATE TABLE IF NOT EXISTS operation_queue (
 id INTEGER PRIMARY KEY AUTOINCREMENT, kind TEXT NOT NULL, resource_id INTEGER NOT NULL, account_id INTEGER,
 status TEXT NOT NULL DEFAULT 'queued', available_at TEXT NOT NULL, lease_until TEXT,
 attempts INTEGER NOT NULL DEFAULT 0,
 max_attempts INTEGER NOT NULL DEFAULT 1, idempotency_key TEXT NOT NULL UNIQUE, last_error TEXT,
 created_at TEXT NOT NULL, updated_at TEXT NOT NULL, FOREIGN KEY(account_id) REFERENCES accounts(id));
CREATE TABLE IF NOT EXISTS operation_actions (
 id INTEGER PRIMARY KEY AUTOINCREMENT, account_id INTEGER NOT NULL, thread_id INTEGER,
 external_user_id TEXT NOT NULL DEFAULT '',
 action TEXT NOT NULL, content TEXT NOT NULL DEFAULT '', created_at TEXT NOT NULL,
 FOREIGN KEY(account_id) REFERENCES accounts(id) ON DELETE CASCADE);
CREATE TABLE IF NOT EXISTS target_contacts (
 external_user_id TEXT PRIMARY KEY, last_account_id INTEGER, last_contact_at TEXT,
 blocked INTEGER NOT NULL DEFAULT 0, block_reason TEXT NOT NULL DEFAULT '', updated_at TEXT NOT NULL,
 FOREIGN KEY(last_account_id) REFERENCES accounts(id) ON DELETE SET NULL);
CREATE TABLE IF NOT EXISTS sensitive_handoff_events (
 id INTEGER PRIMARY KEY AUTOINCREMENT, thread_id INTEGER NOT NULL, account_id INTEGER NOT NULL,
 event_type TEXT NOT NULL DEFAULT 'sensitive_information_detected', created_at TEXT NOT NULL,
 FOREIGN KEY(thread_id) REFERENCES engagement_threads(id) ON DELETE CASCADE,
 FOREIGN KEY(account_id) REFERENCES accounts(id) ON DELETE CASCADE);
CREATE TABLE IF NOT EXISTS roles (
 id INTEGER PRIMARY KEY AUTOINCREMENT, name TEXT NOT NULL, slug TEXT NOT NULL UNIQUE,
 description TEXT NOT NULL DEFAULT '', style_ref TEXT NOT NULL DEFAULT '', config_json TEXT NOT NULL DEFAULT '{}',
 created_at TEXT NOT NULL, updated_at TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS account_roles (
 id INTEGER PRIMARY KEY AUTOINCREMENT, account_id INTEGER NOT NULL, role_id INTEGER NOT NULL,
 is_primary INTEGER NOT NULL DEFAULT 0, created_at TEXT NOT NULL,
 UNIQUE(account_id, role_id),
 FOREIGN KEY(account_id) REFERENCES accounts(id) ON DELETE CASCADE,
 FOREIGN KEY(role_id) REFERENCES roles(id) ON DELETE CASCADE);
CREATE TABLE IF NOT EXISTS source_account_mappings (
 id INTEGER PRIMARY KEY AUTOINCREMENT, source_xhs_user_id TEXT NOT NULL, derivative_account_id INTEGER NOT NULL,
 role_id INTEGER, note TEXT NOT NULL DEFAULT '', created_at TEXT NOT NULL,
 UNIQUE(source_xhs_user_id, derivative_account_id),
 FOREIGN KEY(derivative_account_id) REFERENCES accounts(id) ON DELETE CASCADE,
 FOREIGN KEY(role_id) REFERENCES roles(id) ON DELETE SET NULL);
CREATE TABLE IF NOT EXISTS image_prompts (
 id INTEGER PRIMARY KEY AUTOINCREMENT, source_xhs_user_id TEXT NOT NULL, note_id TEXT NOT NULL DEFAULT '',
 image_index INTEGER NOT NULL DEFAULT 0, image_url TEXT NOT NULL DEFAULT '', local_path TEXT NOT NULL DEFAULT '',
 prompt_words TEXT NOT NULL DEFAULT '{}', decomposed_by TEXT NOT NULL DEFAULT '',
 authorization_status TEXT NOT NULL DEFAULT 'pending', created_at TEXT NOT NULL);
CREATE INDEX IF NOT EXISTS idx_operation_queue_claim ON operation_queue(status,available_at,id);
CREATE INDEX IF NOT EXISTS idx_engagement_tasks_status ON engagement_tasks(status);
CREATE INDEX IF NOT EXISTS idx_drafts_status ON drafts(status);
"""

DEFAULT_RULES = {
    "pilot_days": 14,
    "comment_reply_daily": 8,
    "external_comment_daily": 8,
    "comment_hourly_combined": 2,
    "dm_outbound_daily": 5,
    "dm_outbound_interval_seconds": 3600,
    "dm_inbound_daily": 30,
    "dm_thread_daily": 10,
    "dm_thread_interval_seconds": 300,
    "target_cooldown_days": 30,
    "similarity_threshold": 0.85,
}


@dataclass(frozen=True)
class OperationItem:
    id: int
    kind: str
    resource_id: int
    account_id: int | None
    attempts: int
    max_attempts: int


class OperationsStore:
    def __init__(self, db: Database):
        self.db = db
        self._lock = threading.RLock()
        with self.db.connect() as con:
            con.executescript(SCHEMA)
            if not con.execute("SELECT 1 FROM policy_rules LIMIT 1").fetchone():
                now = now_iso()
                con.execute(
                    "INSERT INTO policy_rules(version,name,rules_json,active,created_at,activated_at) "
                    "VALUES(1,?,?,?,?,?)",
                    ("14天保守试运行", json_dumps(DEFAULT_RULES), 1, now, now),
                )
            con.commit()

    def active_rule(self) -> dict[str, Any]:
        row = self.db.fetchone("SELECT * FROM policy_rules WHERE active=1 ORDER BY version DESC LIMIT 1")
        if not row:
            raise RuntimeError("没有启用的互动规则")
        row["rules"] = json.loads(row["rules_json"])
        return row

    def create_persona(self, account_id: int, name: str, **values: Any) -> int:
        if contains_sensitive_information(json_dumps({"name": name, **values})):
            raise ValueError("人设配置不得包含手机号、微信号或地址")
        now = now_iso()
        previous = self.db.fetchone(
            "SELECT MAX(version) version FROM personas WHERE account_id=? AND name=?", (account_id, name)
        )
        version = int(previous["version"] or 0) + 1
        return self.db.execute(
            """INSERT INTO personas(account_id,name,version,brand_identity,tone,expertise_json,
            common_phrases_json,allowed_cta_json,prohibited_claims_json,examples_json,created_at,updated_at)
            VALUES(?,?,?,?,?,?,?,?,?,?,?,?)""",
            (
                account_id,
                name.strip(),
                version,
                values.get("brand_identity", ""),
                values.get("tone", ""),
                json_dumps(values.get("expertise", [])),
                json_dumps(values.get("common_phrases", [])),
                json_dumps(values.get("allowed_cta", [])),
                json_dumps(values.get("prohibited_claims", [])),
                json_dumps(values.get("examples", [])),
                now,
                now,
            ),
        )

    def create_agent_run(self, kind: str, payload: dict[str, Any]) -> int:
        if kind not in {"search_brief", "screen_results", "material_research", "agent_draft", "image_decompose"}:
            raise ValueError("不支持的 AI 任务类型")
        if contains_sensitive_information(json_dumps(payload)):
            raise ValueError("AI 任务输入包含敏感信息，禁止保存或发送到模型")
        return self.db.execute(
            "INSERT INTO agent_runs(kind,input_json,created_at) VALUES(?,?,?)",
            (kind, json_dumps(payload), now_iso()),
        )

    def create_draft(self, kind: str, content: str, **values: Any) -> int:
        if contains_sensitive_information(json_dumps({"content": content, **values})):
            raise ValueError("草稿包含手机号、微信号或地址，禁止保存")
        now = now_iso()
        return self.db.execute(
            """INSERT INTO drafts(kind,account_id,persona_id,agent_run_id,title,content,context_json,
            sources_json,model,prompt_version,persona_version,policy_rule_id,status,created_at,updated_at)
            VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (
                kind,
                values.get("account_id"),
                values.get("persona_id"),
                values.get("agent_run_id"),
                values.get("title", ""),
                content,
                json_dumps(values.get("context", {})),
                json_dumps(values.get("sources", [])),
                values.get("model", ""),
                values.get("prompt_version", ""),
                values.get("persona_version"),
                values.get("policy_rule_id"),
                "pending_review",
                now,
                now,
            ),
        )

    def upsert_thread(self, account_id: int, channel: str, external_user_id: str, **values: Any) -> int:
        if contains_sensitive_information(json_dumps({"external_user_id": external_user_id, **values})):
            raise ValueError("会话索引不得保存手机号、微信号或地址")
        now = now_iso()
        row = self.db.fetchone(
            "SELECT id FROM engagement_threads WHERE account_id=? AND channel=? AND external_user_id=?",
            (account_id, channel, external_user_id),
        )
        if row:
            self.db.execute(
                """UPDATE engagement_threads SET display_name=?,platform_thread_ref=?,lead_reason=?,warm_lead=?,
                updated_at=? WHERE id=?""",
                (
                    values.get("display_name", ""),
                    values.get("platform_thread_ref", ""),
                    values.get("lead_reason", ""),
                    int(values.get("warm_lead", False)),
                    now,
                    row["id"],
                ),
            )
            return int(row["id"])
        return self.db.execute(
            """INSERT INTO engagement_threads(account_id,channel,external_user_id,display_name,
            platform_thread_ref,lead_reason,warm_lead,created_at,updated_at) VALUES(?,?,?,?,?,?,?,?,?)""",
            (
                account_id,
                channel,
                external_user_id,
                values.get("display_name", ""),
                values.get("platform_thread_ref", ""),
                values.get("lead_reason", ""),
                int(values.get("warm_lead", False)),
                now,
                now,
            ),
        )

    def create_engagement_task(self, kind: str, account_id: int, content: str, **values: Any) -> int:
        if kind not in {"comment", "comment_reply", "dm_reply", "dm_outbound"}:
            raise ValueError("不支持的互动任务类型")
        if contains_sensitive_information(json_dumps({"content": content, **values})):
            raise ValueError("互动任务包含手机号、微信号或地址，禁止保存")
        rule = self.active_rule()
        now = now_iso()
        key = values.get("idempotency_key") or f"{kind}:{uuid.uuid4().hex}"
        return self.db.execute(
            """INSERT INTO engagement_tasks(thread_id,account_id,draft_id,kind,target_note_id,target_comment_id,
            target_user_id,content,policy_rule_id,idempotency_key,created_at,updated_at)
            VALUES(?,?,?,?,?,?,?,?,?,?,?,?)""",
            (
                values.get("thread_id"),
                account_id,
                values.get("draft_id"),
                kind,
                values.get("target_note_id", ""),
                values.get("target_comment_id", ""),
                values.get("target_user_id", ""),
                content,
                rule["id"],
                key,
                now,
                now,
            ),
        )

    def approve_task(self, task_id: int) -> None:
        changed = self.db.execute(
            "UPDATE engagement_tasks SET status='approved',approved_at=?,updated_at=? "
            "WHERE id=? AND status='pending_review'",
            (now_iso(), now_iso(), task_id),
        )
        if not changed and not self.db.fetchone("SELECT id FROM engagement_tasks WHERE id=?", (task_id,)):
            raise ValueError("互动任务不存在")

    def enqueue(self, kind: str, resource_id: int, account_id: int | None, max_attempts: int = 1) -> int:
        if kind not in OPERATION_KINDS:
            raise ValueError("不支持的操作队列类型")
        key = f"{kind}:{resource_id}"
        now = now_iso()
        with self._lock, self.db.connect() as con:
            con.execute("BEGIN IMMEDIATE")
            row = con.execute("SELECT id,status FROM operation_queue WHERE idempotency_key=?", (key,)).fetchone()
            if row and row["status"] in {"queued", "running"}:
                con.commit()
                return int(row["id"])
            if row:
                con.execute(
                    "UPDATE operation_queue SET status='queued',available_at=?,attempts=0,last_error=NULL,"
                    "updated_at=? WHERE id=?",
                    (now, now, row["id"]),
                )
                queue_id = int(row["id"])
            else:
                cursor = con.execute(
                    """INSERT INTO operation_queue(kind,resource_id,account_id,available_at,max_attempts,
                    idempotency_key,created_at,updated_at) VALUES(?,?,?,?,?,?,?,?)""",
                    (kind, resource_id, account_id, now, max_attempts, key, now, now),
                )
                queue_id = int(cursor.lastrowid)
            con.commit()
            return queue_id

    def claim(self, lease_seconds: int = 180) -> OperationItem | None:
        now_dt = datetime.now(UTC)
        now, lease = now_dt.isoformat(), (now_dt + timedelta(seconds=lease_seconds)).isoformat()
        with self._lock, self.db.connect() as con:
            con.execute("BEGIN IMMEDIATE")
            con.execute(
                "UPDATE operation_queue SET status='manual',last_error='执行中断，需人工核验',updated_at=? "
                "WHERE status='running' AND lease_until<? "
                "AND kind NOT IN ('search_brief','screen_results','material_research','agent_draft','image_decompose')",
                (now, now),
            )
            con.execute(
                "UPDATE operation_queue SET status='queued',lease_until=NULL,updated_at=? "
                "WHERE status='running' AND lease_until<? "
                "AND kind IN ('search_brief','screen_results','material_research','agent_draft','image_decompose')",
                (now, now),
            )
            row = con.execute(
                """SELECT q.* FROM operation_queue q WHERE q.status='queued' AND q.available_at<=?
                AND (q.account_id IS NULL OR NOT EXISTS(SELECT 1 FROM operation_queue a
                WHERE a.account_id=q.account_id AND a.status='running')) ORDER BY q.id LIMIT 1""",
                (now,),
            ).fetchone()
            if not row:
                con.commit()
                return None
            con.execute(
                "UPDATE operation_queue SET status='running',lease_until=?,attempts=attempts+1,updated_at=? WHERE id=?",
                (lease, now, row["id"]),
            )
            con.commit()
            return OperationItem(
                int(row["id"]),
                row["kind"],
                int(row["resource_id"]),
                int(row["account_id"]) if row["account_id"] is not None else None,
                int(row["attempts"]) + 1,
                int(row["max_attempts"]),
            )

    def finish(self, item: OperationItem, status: str, error: str | None = None) -> None:
        self.db.execute(
            "UPDATE operation_queue SET status=?,lease_until=NULL,last_error=?,updated_at=? WHERE id=?",
            (status, error, now_iso(), item.id),
        )

    # ── 角色库：共享人设定义，与具体账号解耦，支持一风格绑多账号 ──
    def create_role(self, name: str, slug: str, **values: Any) -> int:
        if contains_sensitive_information(json_dumps({"name": name, "slug": slug, **values})):
            raise ValueError("角色配置不得包含手机号、微信号或地址")
        slug = slug.strip()
        existing = self.db.fetchone("SELECT id FROM roles WHERE slug=?", (slug,))
        if existing:
            return int(existing["id"])
        now = now_iso()
        return self.db.execute(
            """INSERT INTO roles(name,slug,description,style_ref,config_json,created_at,updated_at)
            VALUES(?,?,?,?,?,?,?)""",
            (
                name.strip(),
                slug,
                values.get("description", ""),
                values.get("style_ref", ""),
                json_dumps(values.get("config", {})),
                now,
                now,
            ),
        )

    def get_role(self, role_id: int) -> dict[str, Any] | None:
        return self.db.fetchone("SELECT * FROM roles WHERE id=?", (role_id,))

    def list_roles(self) -> list[dict[str, Any]]:
        return self.db.fetchall("SELECT * FROM roles ORDER BY id")

    def bind_account_role(self, account_id: int, role_id: int, is_primary: bool = False) -> int:
        existing = self.db.fetchone(
            "SELECT id FROM account_roles WHERE account_id=? AND role_id=?", (account_id, role_id)
        )
        if existing:
            if is_primary:
                self.db.execute("UPDATE account_roles SET is_primary=0 WHERE account_id=?", (account_id,))
                self.db.execute("UPDATE account_roles SET is_primary=1 WHERE id=?", (existing["id"],))
            return int(existing["id"])
        if is_primary:
            self.db.execute("UPDATE account_roles SET is_primary=0 WHERE account_id=?", (account_id,))
        return self.db.execute(
            "INSERT INTO account_roles(account_id,role_id,is_primary,created_at) VALUES(?,?,?,?)",
            (account_id, role_id, int(is_primary), now_iso()),
        )

    def unbind_account_role(self, account_id: int, role_id: int) -> None:
        self.db.execute(
            "DELETE FROM account_roles WHERE account_id=? AND role_id=?", (account_id, role_id)
        )

    def list_account_roles(self, account_id: int) -> list[dict[str, Any]]:
        return self.db.fetchall(
            "SELECT r.*, ar.is_primary FROM account_roles ar JOIN roles r ON r.id=ar.role_id "
            "WHERE ar.account_id=? ORDER BY ar.id",
            (account_id,),
        )

    def create_source_mapping(
        self, source_xhs_user_id: str, derivative_account_id: int, role_id: int | None = None, note: str = ""
    ) -> int:
        if contains_sensitive_information(json_dumps({"source": source_xhs_user_id, "note": note})):
            raise ValueError("源账号映射不得包含手机号、微信号或地址")
        src = source_xhs_user_id.strip()
        existing = self.db.fetchone(
            "SELECT id FROM source_account_mappings WHERE source_xhs_user_id=? AND derivative_account_id=?",
            (src, derivative_account_id),
        )
        if existing:
            return int(existing["id"])
        return self.db.execute(
            """INSERT INTO source_account_mappings(
            source_xhs_user_id,derivative_account_id,role_id,note,created_at) VALUES(?,?,?,?,?)""",
            (src, derivative_account_id, role_id, note, now_iso()),
        )

    def list_source_mappings(self, source_xhs_user_id: str | None = None) -> list[dict[str, Any]]:
        if source_xhs_user_id:
            return self.db.fetchall(
                "SELECT * FROM source_account_mappings WHERE source_xhs_user_id=? ORDER BY id",
                (source_xhs_user_id,),
            )
        return self.db.fetchall("SELECT * FROM source_account_mappings ORDER BY id")

    def create_image_prompt(
        self,
        source_xhs_user_id: str,
        note_id: str,
        image_index: int,
        prompt_words: dict[str, Any],
        image_url: str = "",
        local_path: str = "",
        decomposed_by: str = "",
        authorization_status: str = "pending",
    ) -> int:
        serialized = json_dumps(prompt_words)
        if contains_sensitive_information(serialized):
            authorization_status = "needs_review"
        return self.db.execute(
            """INSERT INTO image_prompts(
            source_xhs_user_id,note_id,image_index,image_url,local_path,prompt_words,
            decomposed_by,authorization_status,created_at) VALUES(?,?,?,?,?,?,?,?,?)""",
            (
                source_xhs_user_id.strip(),
                note_id,
                image_index,
                image_url,
                local_path,
                serialized,
                decomposed_by,
                authorization_status,
                now_iso(),
            ),
        )

    def list_image_prompts(
        self, source_xhs_user_id: str | None = None, note_id: str | None = None
    ) -> list[dict[str, Any]]:
        clauses: list[str] = []
        params: list[Any] = []
        if source_xhs_user_id:
            clauses.append("source_xhs_user_id=?")
            params.append(source_xhs_user_id)
        if note_id:
            clauses.append("note_id=?")
            params.append(note_id)
        where = (" WHERE " + " AND ".join(clauses)) if clauses else ""
        return self.db.fetchall(f"SELECT * FROM image_prompts{where} ORDER BY id", tuple(params))

    def recent_content(self, account_id: int, action: str, days: int = 30) -> list[str]:
        since = (datetime.now(UTC) - timedelta(days=days)).isoformat()
        rows = self.db.fetchall(
            "SELECT content FROM operation_actions WHERE account_id=? AND action=? "
            "AND datetime(created_at)>=datetime(?)",
            (account_id, action, since),
        )
        return [str(row["content"]) for row in rows if row["content"]]
