"""FastAPI application and local-only dashboard routes."""

from __future__ import annotations

import shutil
import uuid
from concurrent.futures import ThreadPoolExecutor
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Annotated, Any
from urllib.parse import urlsplit

from fastapi import FastAPI, File, Form, HTTPException, Request, UploadFile
from fastapi.responses import HTMLResponse, PlainTextResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from starlette.middleware.trustedhost import TrustedHostMiddleware

from .browser import AccountBrowserService
from .collector import CollectorService
from .config import DashboardConfig
from .db import Database
from .exporter import MarkdownExporter
from .extension import DashboardExtension
from .importer import IMAGE_SUFFIXES, PublishImporter
from .orchestrator import Orchestrator
from .persistence import P0Store
from .publisher import BrowserPublisher
from .queue import DurableTaskQueue
from .rate_limit import AccountRateLimiter
from .utils import json_dumps, json_loads, now_iso, safe_name, split_terms


def create_app(data_dir: str | Path | None = None) -> FastAPI:
    config = DashboardConfig.load(data_dir)
    db = Database(config.database_path)
    browsers = AccountBrowserService(db, config)
    store = P0Store(db)
    limiter = AccountRateLimiter(
        store, interval_seconds=config.request_interval_seconds, daily_limit=config.daily_request_limit
    )
    exporter = MarkdownExporter(db, config)
    collector = CollectorService(db, browsers, exporter, limiter, store)
    importer = PublishImporter(db, config)
    publisher = BrowserPublisher(db, config, browsers)
    queue = DurableTaskQueue(
        db,
        store,
        collector,
        publisher,
        workers=config.worker_threads,
        lease_seconds=config.queue_lease_seconds,
        poll_seconds=config.queue_poll_seconds,
    )
    extension = DashboardExtension(db, config, browsers, queue)
    orchestrator = Orchestrator(db, config, store, queue, extension.store, extension.engagement, extension.ai)
    executor = ThreadPoolExecutor(max_workers=2, thread_name_prefix="xhs-interactive")
    root = Path(__file__).parent
    templates = Jinja2Templates(directory=root / "templates")
    templates.env.filters["fromjson"] = lambda value: json_loads(value, [])

    @asynccontextmanager
    async def lifespan(_app: FastAPI):
        queue.start()
        extension.start()
        orchestrator.start()
        try:
            yield
        finally:
            orchestrator.stop()
            extension.stop()
            queue.stop()
            executor.shutdown(wait=False, cancel_futures=False)

    app = FastAPI(title="XHS Operations Dashboard", docs_url="/api/docs", redoc_url=None, lifespan=lifespan)
    app.add_middleware(TrustedHostMiddleware, allowed_hosts=["127.0.0.1", "localhost", "testserver", "[::1]"])
    app.mount("/static", StaticFiles(directory=root / "static"), name="static")
    app.state.config, app.state.db, app.state.executor = config, db, executor
    app.state.store, app.state.queue = store, queue
    app.state.extension = extension
    app.state.orchestrator = orchestrator

    @app.middleware("http")
    async def local_origin_guard(request: Request, call_next):
        if request.method in {"POST", "PUT", "PATCH", "DELETE"}:
            origin = request.headers.get("origin")
            if origin:
                parsed = urlsplit(origin)
                if parsed.hostname not in {"127.0.0.1", "localhost", "::1"}:
                    return PlainTextResponse("Cross-origin request rejected", status_code=403)
        return await call_next(request)

    def render(request: Request, template: str, **context: Any) -> HTMLResponse:
        context.update({"request": request, "active": context.get("active", ""), "data_dir": config.data_dir})
        return templates.TemplateResponse(request, template, context)

    def redirect(path: str, message: str | None = None) -> RedirectResponse:
        return RedirectResponse(path + (f"?message={message}" if message else ""), status_code=303)

    @app.get("/", response_class=HTMLResponse)
    def dashboard(request: Request):
        queue_counts = store.queue_counts()
        stats = {
            "accounts": db.fetchone("SELECT COUNT(*) count FROM accounts")["count"],
            "ready_accounts": db.fetchone(
                "SELECT COUNT(*) count FROM accounts WHERE login_status='ready' AND enabled=1"
            )["count"],
            "notes": db.fetchone("SELECT COUNT(*) count FROM notes")["count"],
            "pending": db.fetchone("SELECT COUNT(*) count FROM publish_tasks WHERE status='pending_review'")["count"],
            "queued": sum(queue_counts.get(status, 0) for status in ("queued", "running", "retry_wait")),
        }
        jobs = db.fetchall("SELECT * FROM search_jobs ORDER BY id DESC LIMIT 8")
        tasks = db.fetchall(
            """SELECT t.*,a.alias account_alias FROM publish_tasks t
            JOIN accounts a ON a.id=t.account_id ORDER BY t.id DESC LIMIT 8"""
        )
        return render(request, "dashboard.html", active="dashboard", stats=stats, jobs=jobs, tasks=tasks)

    @app.get("/accounts", response_class=HTMLResponse)
    def account_page(request: Request):
        return render(
            request,
            "accounts.html",
            active="accounts",
            accounts=db.fetchall("SELECT * FROM accounts ORDER BY id"),
            message=request.query_params.get("message"),
        )

    @app.post("/accounts")
    def create_account(alias: str = Form(...)):
        try:
            browsers.create_account(alias)
            return redirect("/accounts", "Profile created; select Bind to scan the QR code")
        except Exception as exc:
            return redirect("/accounts", str(exc))

    @app.post("/accounts/{account_id}/bind")
    def bind_account(account_id: int):
        executor.submit(browsers.bind, account_id)
        return redirect("/accounts", "Login window started")

    @app.post("/accounts/{account_id}/repair-profile")
    def repair_account_profile(account_id: int):
        try:
            browsers.repair_profile_lock(account_id)
            return redirect("/accounts", "Browser profile lock checked and repaired")
        except Exception as exc:
            return redirect("/accounts", str(exc))

    @app.post("/accounts/{account_id}/verify")
    def verify_account(account_id: int):
        executor.submit(browsers.verify, account_id)
        return redirect("/accounts", "Verifying account session")

    @app.post("/accounts/{account_id}/delete")
    def delete_account(account_id: int):
        try:
            browsers.delete_account(account_id)
            return redirect("/accounts", "Account profile deleted")
        except Exception as exc:
            return redirect("/accounts", str(exc))

    @app.post("/accounts/{account_id}/toggle")
    def toggle_account(account_id: int):
        account = db.fetchone("SELECT * FROM accounts WHERE id=?", (account_id,))
        if account:
            db.update("accounts", account_id, enabled=0 if account["enabled"] else 1)
        return redirect("/accounts")

    @app.get("/searches", response_class=HTMLResponse)
    def search_page(request: Request):
        jobs = db.fetchall(
            """SELECT j.*,a.alias account_alias FROM search_jobs j
            LEFT JOIN accounts a ON a.id=j.account_id ORDER BY j.id DESC"""
        )
        return render(
            request,
            "searches.html",
            active="searches",
            jobs=jobs,
            accounts=db.fetchall("SELECT * FROM accounts WHERE enabled=1 ORDER BY id"),
            message=request.query_params.get("message"),
        )

    @app.post("/searches")
    def create_search(
        name: str = Form(...),
        account_id: str = Form(""),
        keywords: str = Form(""),
        topics: str = Form(""),
        author_ids: str = Form(""),
        start_date: str = Form(""),
        end_date: str = Form(""),
        media_type: str = Form("all"),
        include_comments: bool = Form(False),
        comment_limit: int = Form(100),
        max_pages: int = Form(3),
        min_score: int = Form(1000),
        min_likes: int = Form(0),
        min_collects: int = Form(0),
        min_comments: int = Form(0),
        weight_likes: float = Form(1),
        weight_collects: float = Form(2),
        weight_comments: float = Form(3),
        weight_shares: float = Form(1),
    ):
        if not split_terms(keywords) and not split_terms(topics) and not split_terms(author_ids):
            return redirect("/searches", "Provide a keyword, topic, or author ID")
        selected_account = (
            db.fetchone("SELECT id FROM accounts WHERE id=? AND enabled=1 AND login_status='ready'", (int(account_id),))
            if account_id
            else db.fetchone("SELECT id FROM accounts WHERE enabled=1 AND login_status='ready' ORDER BY id LIMIT 1")
        )
        if not selected_account:
            return redirect("/searches", "没有可用的已登录账号")
        selected_account_id = int(selected_account["id"])
        values = {
            "name": name.strip(),
            "account_id": selected_account_id,
            "keywords_json": json_dumps(split_terms(keywords)),
            "topics_json": json_dumps(split_terms(topics)),
            "author_ids_json": json_dumps(split_terms(author_ids)),
            "start_date": start_date or None,
            "end_date": end_date or None,
            "media_type": media_type,
            "include_comments": int(include_comments),
            "comment_limit": max(0, min(comment_limit, 100)),
            "max_pages": max(1, min(max_pages, 10)),
            "min_score": max(0, min_score),
            "min_likes": max(0, min_likes),
            "min_collects": max(0, min_collects),
            "min_comments": max(0, min_comments),
            "weights_json": json_dumps(
                {
                    "likes": weight_likes,
                    "collects": weight_collects,
                    "comments": weight_comments,
                    "shares": weight_shares,
                }
            ),
        }
        job_id = db.create_search_job(values)
        queue.enqueue_search(job_id, selected_account_id)
        return redirect("/searches", f"Search job {job_id} entered the durable queue")

    @app.post("/searches/{job_id}/pause")
    def pause_search(job_id: int):
        store.cancel("search", job_id)
        db.update("search_jobs", job_id, status="paused")
        return redirect("/searches")

    @app.post("/searches/{job_id}/resume")
    def resume_search(job_id: int):
        job = db.fetchone("SELECT status FROM search_jobs WHERE id=?", (job_id,))
        if job and job["status"] in {"paused", "failed"}:
            db.update("search_jobs", job_id, status="pending", error=None)
            account = db.fetchone("SELECT account_id FROM search_jobs WHERE id=?", (job_id,))
            if account and account["account_id"]:
                queue.enqueue_search(job_id, int(account["account_id"]))
        return redirect("/searches")

    @app.get("/library", response_class=HTMLResponse)
    def library(request: Request):
        notes = db.fetchall(
            """SELECT n.*,e.directory,e.status export_status,j.name job_name FROM notes n
            JOIN search_job_notes jn ON jn.note_id=n.id JOIN search_jobs j ON j.id=jn.job_id
            LEFT JOIN export_bundles e ON e.note_id=n.id AND e.job_id=j.id
            ORDER BY n.viral_score DESC,n.id DESC"""
        )
        return render(request, "library.html", active="library", notes=notes)

    @app.get("/publish", response_class=HTMLResponse)
    def publish_page(request: Request):
        tasks = db.fetchall(
            """SELECT t.*,a.alias account_alias,a.nickname FROM publish_tasks t
            JOIN accounts a ON a.id=t.account_id ORDER BY t.id DESC"""
        )
        return render(
            request,
            "publish.html",
            active="publish",
            tasks=tasks,
            accounts=db.fetchall("SELECT * FROM accounts WHERE enabled=1 ORDER BY id"),
            message=request.query_params.get("message"),
        )

    @app.post("/publish")
    async def create_publish(
        images: Annotated[list[UploadFile], File()],
        account_id: int = Form(...),
        title: str = Form(...),
        body: str = Form(...),
        topics: str = Form(""),
    ):
        staging = config.uploads_dir / "incoming" / uuid.uuid4().hex
        staging.mkdir(parents=True)
        paths: list[Path] = []
        try:
            for index, upload in enumerate(images, 1):
                suffix = Path(upload.filename or "").suffix.lower()
                if suffix not in IMAGE_SUFFIXES:
                    raise ValueError(f"Unsupported image type: {upload.filename}")
                filename = safe_name(upload.filename or "image")
                if not filename.lower().endswith(suffix):
                    filename += suffix
                target = staging / f"{index:03d}-{filename}"
                with target.open("wb") as handle:
                    while chunk := await upload.read(1024 * 1024):
                        handle.write(chunk)
                paths.append(target)
            task_id = importer.create(account_id, title, body, split_terms(topics), paths)
            return redirect("/publish", f"Task {task_id} is awaiting review")
        except Exception as exc:
            return redirect("/publish", str(exc))
        finally:
            shutil.rmtree(staging, ignore_errors=True)

    @app.post("/publish/import")
    def import_publish(directory: str = Form(...)):
        try:
            ids = importer.import_directory(directory)
            return redirect("/publish", f"Imported {len(ids)} review tasks")
        except Exception as exc:
            return redirect("/publish", str(exc))

    @app.post("/publish/{task_id}/approve")
    def approve_publish(task_id: int):
        task = db.fetchone("SELECT status FROM publish_tasks WHERE id=?", (task_id,))
        if task and task["status"] in {"pending_review", "failed"}:
            db.update("publish_tasks", task_id, status="approved", approved_at=now_iso())
        return redirect("/publish")

    @app.post("/publish/{task_id}/run")
    def run_publish(task_id: int):
        task = db.fetchone("SELECT status FROM publish_tasks WHERE id=?", (task_id,))
        if not task or task["status"] != "approved":
            return redirect("/publish", "Only approved tasks can be published")
        full_task = db.fetchone("SELECT account_id FROM publish_tasks WHERE id=?", (task_id,))
        db.update("publish_tasks", task_id, status="queued")
        queue.enqueue_publish(task_id, int(full_task["account_id"]))
        return redirect("/publish", "Task entered the durable publish queue")

    @app.post("/publish/{task_id}/cancel")
    def cancel_publish(task_id: int):
        task = db.fetchone("SELECT status FROM publish_tasks WHERE id=?", (task_id,))
        if task and task["status"] in {"pending_review", "approved", "queued", "failed"}:
            queue.store.cancel("publish", task_id)
            db.update("publish_tasks", task_id, status="cancelled")
        return redirect("/publish")

    @app.post("/publish/{task_id}/confirm")
    def confirm_publish(task_id: int, final_url: str = Form(...)):
        task = db.fetchone("SELECT status FROM publish_tasks WHERE id=?", (task_id,))
        if task and task["status"] == "verification_pending" and "xiaohongshu.com" in final_url:
            db.update("publish_tasks", task_id, status="published", final_url=final_url, error=None)
        return redirect("/publish")

    @app.get("/api/health")
    def health():
        return {
            "ok": True,
            "database": str(config.database_path),
            "data_dir": str(config.data_dir),
            "schema_version": 2,
            "queue": store.queue_counts(),
        }

    @app.get("/api/queue")
    def api_queue():
        fields = (
            "id,kind,resource_id,account_id,status,available_at,lease_until,attempts,max_attempts,last_error,updated_at"
        )
        return {"ok": True, "data": db.fetchall(f"SELECT {fields} FROM task_queue ORDER BY id DESC")}

    @app.get("/api/accounts")
    def api_accounts():
        fields = "id,alias,xhs_user_id,nickname,login_status,enabled,last_verified_at"
        return {"ok": True, "data": db.fetchall(f"SELECT {fields} FROM accounts")}

    @app.get("/api/search-jobs/{job_id}")
    def api_search_job(job_id: int):
        job = db.fetchone("SELECT * FROM search_jobs WHERE id=?", (job_id,))
        if not job:
            raise HTTPException(404, "search job not found")
        return {"ok": True, "data": job}

    @app.get("/api/publish-tasks/{task_id}")
    def api_publish_task(task_id: int):
        task = db.fetchone("SELECT * FROM publish_tasks WHERE id=?", (task_id,))
        if not task:
            raise HTTPException(404, "publish task not found")
        attempts = db.fetchall("SELECT * FROM publish_attempts WHERE task_id=? ORDER BY id DESC", (task_id,))
        return {"ok": True, "data": {"task": task, "attempts": attempts}}

    extension.install(app, templates)
    orchestrator.install(app)
    return app
