"""Routes that connect AI search briefs and authorized derivative assets to P0 workflows."""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, FastAPI, Form, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates

from .config import DashboardConfig
from .db import Database
from .importer import PublishImporter
from .materials import MaterialWorkflow
from .operations import OperationsStore
from .utils import json_loads


class ResearchWorkflowRoutes:
    def __init__(
        self,
        db: Database,
        config: DashboardConfig,
        store: OperationsStore,
        search_queue: Any,
    ):
        self.db, self.config, self.store, self.search_queue = db, config, store, search_queue
        self.materials = MaterialWorkflow(db, store, PublishImporter(db, config))

    def install(self, app: FastAPI, templates: Jinja2Templates) -> None:
        router = APIRouter()

        def redirect(path: str, message: str) -> RedirectResponse:
            return RedirectResponse(f"{path}?message={message}", status_code=303)

        @router.post("/research/runs/{run_id}/create-search")
        def create_search_from_brief(run_id: int, account_id: str = Form(""), request: Request = None):
            run = self.db.fetchone("SELECT * FROM agent_runs WHERE id=?", (run_id,))
            if not run or run["kind"] != "search_brief" or run["status"] != "complete":
                return redirect("/research", "只有已完成的搜索理解任务可以转换")
            plan = json_loads(run["output_json"], {})
            payload = json_loads(run["input_json"], {})
            max_candidates = int(payload.get("max_candidates") or 8)
            objective = str(payload.get("objective", ""))
            target_audience = str(payload.get("target_audience", ""))
            orch = request.app.state.orchestrator if request is not None else None
            if orch is None:
                return redirect("/research", "编排器未初始化，无法创建检索任务")
            job_ids = orch.create_search_jobs_from_plan(
                plan, objective, target_audience, max_candidates, int(account_id) if account_id else None
            )
            orch._mark(f"brief_done:{run_id}")
            if not job_ids:
                return redirect("/research", "没有可用的已登录账号")
            return redirect("/searches", f"已根据检索方案创建 {len(job_ids)} 个检索任务")

        @router.get("/materials", response_class=HTMLResponse)
        def materials_page(request: Request):
            context = {
                "request": request,
                "active": "materials",
                "data_dir": self.config.data_dir,
                "message": request.query_params.get("message"),
                "sources": self.db.fetchall("SELECT * FROM knowledge_sources ORDER BY id DESC"),
                "candidates": self.db.fetchall(
                    """SELECT c.*,s.name source_name,n.note_id platform_note_id FROM material_candidates c
                    JOIN knowledge_sources s ON s.id=c.source_id LEFT JOIN notes n ON n.id=c.note_id
                    ORDER BY c.id DESC"""
                ),
                "derivatives": self.db.fetchall(
                    """SELECT d.*,c.authorization_status FROM derivative_tasks d
                    JOIN material_candidates c ON c.id=d.candidate_id ORDER BY d.id DESC"""
                ),
                "notes": self.db.fetchall("SELECT id,note_id,title FROM notes ORDER BY id DESC LIMIT 500"),
                "personas": self.db.fetchall("SELECT id,name,version FROM personas WHERE enabled=1 ORDER BY id DESC"),
            }
            return templates.TemplateResponse(request, "materials.html", context)

        @router.post("/materials/sources")
        def register_source(
            name: str = Form(...),
            source_type: str = Form("owned"),
            authorization_status: str = Form(...),
            source_url: str = Form(""),
            local_path: str = Form(""),
            usage_restrictions: str = Form(""),
        ):
            self.materials.register_source(
                name,
                authorization_status,
                source_type=source_type,
                source_url=source_url,
                local_path=local_path,
                usage_restrictions=usage_restrictions,
            )
            return redirect("/materials", "素材来源已登记")

        @router.post("/materials/candidates")
        def create_candidate(source_id: int = Form(...), note_id: str = Form("")):
            try:
                self.materials.create_candidate(note_id=int(note_id) if note_id else None, source_id=source_id)
                return redirect("/materials", "已授权素材已进入候选区")
            except ValueError as exc:
                return redirect("/materials", str(exc))

        @router.post("/materials/derivatives")
        def create_derivative(candidate_id: int = Form(...), title: str = Form(...), persona_id: str = Form("")):
            try:
                self.materials.create_derivative_task(
                    candidate_id, title, persona_id=int(persona_id) if persona_id else None
                )
                return redirect("/materials", "线下二创任务已创建")
            except ValueError as exc:
                return redirect("/materials", str(exc))

        @router.post("/materials/derivatives/{task_id}/import")
        def import_derivative(task_id: int, directory: str = Form(...), rights_declared: bool = Form(False)):
            try:
                publish_ids = self.materials.import_finished_assets(task_id, directory, rights_declared=rights_declared)
                return redirect("/publish", f"二创成品已生成待审核发布任务：{publish_ids}")
            except ValueError as exc:
                return redirect("/materials", str(exc))

        app.include_router(router)
