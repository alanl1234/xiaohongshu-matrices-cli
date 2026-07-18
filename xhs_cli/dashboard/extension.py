"""FastAPI routes and services for the AI/engagement extension."""

from __future__ import annotations

import json
import os
from typing import Any, Literal

from fastapi import APIRouter, Depends, FastAPI, Form, Header, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates
from pydantic import BaseModel, Field

from .ai import AIService
from .browser import AccountBrowserService
from .config import DashboardConfig
from .db import Database
from .engagement import EngagementExecutor
from .governance import contains_sensitive_information, evaluate_content, is_warm_lead
from .operation_queue import OperationQueue
from .operations import OperationsStore
from .research_extension import ResearchWorkflowRoutes
from .utils import json_loads, now_iso, split_terms


class AgentRunRequest(BaseModel):
    kind: Literal["search_brief", "material_research", "agent_draft"]
    payload: dict[str, Any]
    enqueue: bool = True


class AgentDraftRequest(BaseModel):
    kind: Literal["publish", "comment", "comment_reply", "dm_reply", "dm_outbound"]
    content: str = Field(min_length=1, max_length=5000)
    title: str = Field(default="", max_length=100)
    account_id: int | None = None
    persona_id: int | None = None
    context: dict[str, Any] = Field(default_factory=dict)
    sources: list[str] = Field(default_factory=list)


class PublishGateRequest(BaseModel):
    source_status: Literal["owned", "authorized"]
    source_refs: list[str] = Field(min_length=1)
    derivative_completed: bool
    images: list[str] = Field(min_length=1, max_length=18)
    final_asset_dir: str = ""
    rights_evidence: str = Field(min_length=1, max_length=1000)
    topics: list[str] = Field(default_factory=list)


class AgentEngagementRequest(BaseModel):
    kind: Literal["comment", "comment_reply", "dm_reply", "dm_outbound"]
    account_id: int
    content: str = Field(min_length=1, max_length=1000)
    thread_id: int | None = None
    target_note_id: str = ""
    target_comment_id: str = ""
    target_user_id: str = ""
    idempotency_key: str | None = None


class DashboardExtension:
    def __init__(self, db: Database, config: DashboardConfig, browsers: AccountBrowserService, search_queue: Any):
        self.db, self.config, self.browsers = db, config, browsers
        self.store = OperationsStore(db)
        self.ai = AIService(self.store)
        self.engagement = EngagementExecutor(self.store, config, browsers)
        self.queue = OperationQueue(self.store, self.ai, self.engagement, workers=max(1, config.worker_threads))
        self.agent_inbox = config.data_dir / "agent-inbox"
        self.research_routes = ResearchWorkflowRoutes(db, config, self.store, search_queue)
        self.agent_inbox.mkdir(parents=True, exist_ok=True)

    def start(self) -> None:
        self.queue.start()

    def stop(self) -> None:
        self.queue.stop()

    def install(self, app: FastAPI, templates: Jinja2Templates) -> None:
        router = APIRouter()

        def render(request: Request, template: str, **context: Any) -> HTMLResponse:
            context.update({"request": request, "data_dir": self.config.data_dir})
            return templates.TemplateResponse(request, template, context)

        def redirect(path: str, message: str | None = None) -> RedirectResponse:
            suffix = f"?message={message}" if message else ""
            return RedirectResponse(path + suffix, status_code=303)

        @router.get("/personas", response_class=HTMLResponse)
        def personas_page(request: Request):
            personas = self.db.fetchall(
                """SELECT p.*,a.alias account_alias FROM personas p JOIN accounts a ON a.id=p.account_id
                ORDER BY p.id DESC"""
            )
            return render(
                request,
                "personas.html",
                active="personas",
                personas=personas,
                accounts=self.db.fetchall("SELECT * FROM accounts WHERE enabled=1 ORDER BY id"),
                message=request.query_params.get("message"),
            )

        @router.post("/personas")
        def create_persona(
            account_id: int = Form(...),
            name: str = Form(...),
            brand_identity: str = Form(""),
            tone: str = Form(""),
            expertise: str = Form(""),
            allowed_cta: str = Form(""),
            prohibited_claims: str = Form(""),
        ):
            self.store.create_persona(
                account_id,
                name,
                brand_identity=brand_identity,
                tone=tone,
                expertise=split_terms(expertise),
                allowed_cta=split_terms(allowed_cta),
                prohibited_claims=split_terms(prohibited_claims),
            )
            return redirect("/personas", "人设新版本已保存")

        @router.get("/research", response_class=HTMLResponse)
        def research_page(request: Request):
            return render(
                request,
                "research.html",
                active="research",
                runs=self.db.fetchall("SELECT * FROM agent_runs ORDER BY id DESC LIMIT 100"),
                drafts=self.db.fetchall("SELECT * FROM drafts ORDER BY id DESC LIMIT 100"),
                inbox=self.agent_inbox,
                message=request.query_params.get("message"),
            )

        @router.post("/research")
        def create_research(kind: str = Form(...), objective: str = Form(...)):
            run_id = self.store.create_agent_run(kind, {"objective": objective})
            queue_kind = "material_research" if kind == "material_research" else "agent_draft"
            self.store.enqueue(queue_kind, run_id, None)
            return redirect("/research", f"AI 任务 #{run_id} 已排队")

        @router.post("/research/import-bundles")
        def import_bundles():
            imported = 0
            for path in sorted(self.agent_inbox.glob("*.json")):
                payload = json.loads(path.read_text(encoding="utf-8"))
                kind = str(payload.get("kind", ""))
                if kind in {"search_brief", "material_research", "agent_draft"}:
                    run_id = self.store.create_agent_run(kind, payload.get("payload", {}))
                    queue_kind = "material_research" if kind == "material_research" else "agent_draft"
                    self.store.enqueue(queue_kind, run_id, None)
                    path.rename(path.with_suffix(".imported"))
                    imported += 1
                elif kind in {"publish", "comment", "comment_reply", "dm_reply", "dm_outbound"}:
                    content = str(payload.get("content", ""))
                    if not content or contains_sensitive_information(content):
                        continue
                    self.store.create_draft(
                        kind,
                        content,
                        title=str(payload.get("title", "")),
                        account_id=payload.get("account_id"),
                        context=payload.get("context", {}),
                        sources=payload.get("sources", []),
                    )
                    path.rename(path.with_suffix(".imported"))
                    imported += 1
            return redirect("/research", f"已导入 {imported} 个任务包")

        @router.get("/engagement", response_class=HTMLResponse)
        def engagement_page(request: Request):
            tasks = self.db.fetchall(
                """SELECT t.*,a.alias account_alias,th.display_name,th.channel FROM engagement_tasks t
                JOIN accounts a ON a.id=t.account_id LEFT JOIN engagement_threads th ON th.id=t.thread_id
                ORDER BY t.id DESC"""
            )
            return render(
                request,
                "engagement.html",
                active="engagement",
                tasks=tasks,
                threads=self.db.fetchall("SELECT * FROM engagement_threads ORDER BY id DESC"),
                accounts=self.db.fetchall("SELECT * FROM accounts WHERE enabled=1 ORDER BY id"),
                message=request.query_params.get("message"),
            )

        @router.post("/engagement/threads")
        def create_thread(
            account_id: int = Form(...),
            channel: str = Form(...),
            external_user_id: str = Form(...),
            display_name: str = Form(""),
            platform_thread_ref: str = Form(""),
            lead_reason: str = Form(""),
        ):
            warm = is_warm_lead(lead_reason)
            self.store.upsert_thread(
                account_id,
                channel,
                external_user_id,
                display_name=display_name,
                platform_thread_ref=platform_thread_ref,
                lead_reason=lead_reason,
                warm_lead=warm,
            )
            return redirect("/engagement", "会话索引已保存；未保存消息正文")

        @router.post("/engagement")
        def create_engagement(
            account_id: int = Form(...),
            kind: str = Form(...),
            content: str = Form(...),
            thread_id: str = Form(""),
            target_note_id: str = Form(""),
            target_comment_id: str = Form(""),
            target_user_id: str = Form(""),
        ):
            result = evaluate_content(content)
            if result.decision == "block":
                return redirect("/engagement", "；".join(result.reasons))
            task_id = self.store.create_engagement_task(
                kind,
                account_id,
                content,
                thread_id=int(thread_id) if thread_id else None,
                target_note_id=target_note_id,
                target_comment_id=target_comment_id,
                target_user_id=target_user_id,
            )
            return redirect("/engagement", f"互动任务 #{task_id} 已进入逐条审核")

        @router.post("/engagement/{task_id}/approve")
        def approve_engagement(task_id: int):
            self.store.approve_task(task_id)
            return redirect("/engagement", f"任务 #{task_id} 已批准，仍需点击执行")

        @router.post("/engagement/{task_id}/run")
        def run_engagement(task_id: int):
            task = self.db.fetchone("SELECT * FROM engagement_tasks WHERE id=?", (task_id,))
            if not task or task["status"] != "approved":
                return redirect("/engagement", "只有已人工批准的任务可以执行")
            self.db.execute(
                "UPDATE engagement_tasks SET status='queued',updated_at=datetime('now') WHERE id=?", (task_id,)
            )
            self.store.enqueue(task["kind"], task_id, int(task["account_id"]))
            return redirect("/engagement", f"任务 #{task_id} 已进入执行队列")

        @router.post("/engagement/{task_id}/cancel")
        def cancel_engagement(task_id: int):
            self.db.execute(
                "UPDATE engagement_tasks SET status='cancelled',updated_at=datetime('now') "
                "WHERE id=? AND status IN ('pending_review','approved','queued')",
                (task_id,),
            )
            return redirect("/engagement")

        @router.post("/engagement/threads/{thread_id}/sync")
        def sync_thread(thread_id: int):
            thread = self.db.fetchone("SELECT account_id FROM engagement_threads WHERE id=?", (thread_id,))
            if thread:
                self.store.enqueue("dm_sync", thread_id, int(thread["account_id"]))
            return redirect("/engagement", "已排队同步；正文不会写入数据库")

        @router.get("/rules", response_class=HTMLResponse)
        def rules_page(request: Request):
            return render(
                request,
                "rules.html",
                active="rules",
                rule=self.store.active_rule(),
                message=request.query_params.get("message"),
            )

        def require_agent_token(
            authorization: str | None = Header(default=None), x_agent_token: str | None = Header(default=None)
        ) -> None:
            expected = os.getenv("XHS_AGENT_TOKEN", "")
            if not expected:
                raise HTTPException(503, "Agent Gateway 未启用；请设置 XHS_AGENT_TOKEN")
            supplied = x_agent_token or ((authorization or "").removeprefix("Bearer ").strip())
            if supplied != expected:
                raise HTTPException(401, "无效的 Agent Gateway token")

        agent = APIRouter(prefix="/api/agent", dependencies=[Depends(require_agent_token)], tags=["agent-gateway"])

        @agent.post("/runs", status_code=202)
        def agent_create_run(request: AgentRunRequest):
            run_id = self.store.create_agent_run(request.kind, request.payload)
            if request.enqueue:
                queue_kind = "material_research" if request.kind == "material_research" else "agent_draft"
                self.store.enqueue(queue_kind, run_id, None)
            return {"ok": True, "data": {"id": run_id, "status": "queued" if request.enqueue else "pending"}}

        @agent.get("/runs/{run_id}")
        def agent_get_run(run_id: int):
            row = self.db.fetchone(
                "SELECT id,kind,provider,model,status,output_json,error,created_at,finished_at "
                "FROM agent_runs WHERE id=?",
                (run_id,),
            )
            if not row:
                raise HTTPException(404, "AI 任务不存在")
            row["output"] = json_loads(row.pop("output_json"), {})
            return {"ok": True, "data": row}

        @agent.post("/drafts", status_code=201)
        def agent_create_draft(request: AgentDraftRequest):
            result = evaluate_content(request.content)
            if result.decision == "block":
                raise HTTPException(422, {"decision": "block", "reasons": result.reasons})
            draft_id = self.store.create_draft(
                request.kind,
                request.content,
                title=request.title,
                account_id=request.account_id,
                persona_id=request.persona_id,
                context=request.context,
                sources=request.sources,
                policy_rule_id=self.store.active_rule()["id"],
                prompt_version="external-agent-v1",
            )
            return {"ok": True, "data": {"id": draft_id, "status": "pending_review"}}

        @agent.put("/drafts/{draft_id}/publish-gate")
        def agent_fill_publish_gate(draft_id: int, request: PublishGateRequest):
            draft = self.db.fetchone("SELECT * FROM drafts WHERE id=?", (draft_id,))
            if not draft or draft["kind"] != "publish":
                raise HTTPException(404, "Publish draft not found")
            if draft["status"] != "pending_review":
                raise HTTPException(409, "Only pending-review publish drafts can receive gate data")
            context = json_loads(draft.get("context_json") or "{}", {})
            payload = request.model_dump()
            topics = payload.pop("topics")
            context["publish_gate"] = payload
            if topics:
                context["topics"] = topics
            self.db.execute(
                "UPDATE drafts SET context_json=?,updated_at=? WHERE id=?",
                (json.dumps(context, ensure_ascii=False), now_iso(), draft_id),
            )
            return {"ok": True, "data": {"id": draft_id, "status": "pending_review", "gate_complete": True}}

        @agent.post("/engagement-tasks", status_code=201)
        def agent_create_engagement(request: AgentEngagementRequest):
            result = evaluate_content(request.content)
            if result.decision == "block":
                raise HTTPException(422, {"decision": "block", "reasons": result.reasons})
            task_id = self.store.create_engagement_task(**request.model_dump(exclude_none=True))
            return {"ok": True, "data": {"id": task_id, "status": "pending_review"}}

        @agent.get("/engagement-tasks/{task_id}")
        def agent_get_engagement(task_id: int):
            row = self.db.fetchone(
                """SELECT id,kind,account_id,thread_id,status,target_note_id,target_comment_id,target_user_id,
                policy_rule_id,error,created_at,updated_at FROM engagement_tasks WHERE id=?""",
                (task_id,),
            )
            if not row:
                raise HTTPException(404, "互动任务不存在")
            return {"ok": True, "data": row}

        app.include_router(router)
        app.include_router(agent)
        self.research_routes.install(app, templates)
