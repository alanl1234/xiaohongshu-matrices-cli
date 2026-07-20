"""Authorized material and offline derivative-creation workflow."""

from __future__ import annotations

from pathlib import Path
from typing import Any

from .db import Database
from .importer import PublishImporter
from .operations import OperationsStore
from .utils import json_dumps, now_iso

AUTHORIZED = {"owned", "authorized"}


class MaterialWorkflow:
    def __init__(self, db: Database, store: OperationsStore, importer: PublishImporter):
        self.db, self.store, self.importer = db, store, importer

    def register_source(
        self,
        name: str,
        authorization_status: str,
        *,
        source_type: str = "owned",
        source_url: str = "",
        local_path: str = "",
        usage_restrictions: str = "",
    ) -> int:
        if authorization_status not in AUTHORIZED | {"unverified", "blocked"}:
            raise ValueError("无效的授权状态")
        now = now_iso()
        return self.db.execute(
            """INSERT INTO knowledge_sources(name,source_type,source_url,local_path,authorization_status,
            usage_restrictions,created_at,updated_at) VALUES(?,?,?,?,?,?,?,?)""",
            (
                name.strip(),
                source_type,
                source_url.strip(),
                local_path.strip(),
                authorization_status,
                usage_restrictions.strip(),
                now,
                now,
            ),
        )

    def create_candidate(self, *, note_id: int | None, source_id: int, insights: dict[str, Any] | None = None) -> int:
        source = self.db.fetchone("SELECT * FROM knowledge_sources WHERE id=?", (source_id,))
        if not source:
            raise ValueError("素材来源不存在")
        if source["authorization_status"] not in AUTHORIZED:
            raise ValueError("素材未确认自有或获得授权，不能进入二创流程")
        if note_id is not None and not self.db.fetchone("SELECT id FROM notes WHERE id=?", (note_id,)):
            raise ValueError("采集笔记不存在")
        now = now_iso()
        return self.db.execute(
            """INSERT INTO material_candidates(note_id,source_id,insights_json,authorization_status,
            created_at,updated_at) VALUES(?,?,?,?,?,?)""",
            (note_id, source_id, json_dumps(insights or {}), source["authorization_status"], now, now),
        )

    def create_derivative_task(
        self, candidate_id: int, title: str, *, persona_id: int | None = None, brief: dict[str, Any] | None = None
    ) -> int:
        candidate = self.db.fetchone("SELECT * FROM material_candidates WHERE id=?", (candidate_id,))
        if not candidate or candidate["authorization_status"] not in AUTHORIZED:
            raise ValueError("只有已授权候选素材可以创建二创任务")
        now = now_iso()
        return self.db.execute(
            """INSERT INTO derivative_tasks(candidate_id,persona_id,title,brief_json,status,created_at,updated_at)
            VALUES(?,?,?,?,?,?,?)""",
            (candidate_id, persona_id, title.strip(), json_dumps(brief or {}), "offline_creation", now, now),
        )

    def import_finished_assets(self, task_id: int, directory: str | Path, *, rights_declared: bool) -> list[int]:
        task = self.db.fetchone(
            """SELECT d.*,c.authorization_status FROM derivative_tasks d
            JOIN material_candidates c ON c.id=d.candidate_id WHERE d.id=?""",
            (task_id,),
        )
        if not task or task["authorization_status"] not in AUTHORIZED:
            raise ValueError("二创任务不存在或原始素材未授权")
        if not rights_declared:
            raise ValueError("必须由人工确认成品图片和文案拥有使用权")
        root = Path(directory).expanduser().resolve()
        if not root.is_dir() or not (root / "post.md").is_file():
            raise ValueError("成品目录必须存在并包含 post.md")
        publish_ids = self.importer.import_directory(root)
        self.db.execute(
            """UPDATE derivative_tasks SET final_asset_dir=?,rights_declared=1,status='imported_pending_review',
            updated_at=? WHERE id=?""",
            (str(root), now_iso(), task_id),
        )
        return publish_ids
