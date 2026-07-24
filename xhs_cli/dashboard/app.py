"""FastAPI application and local-only dashboard routes."""

from __future__ import annotations

import html as _html_lib
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

from .analytics import AnalyticsCollector
from .api_health import ApiHealthMonitor
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


def _e(value: Any) -> str:
    """HTML-escape a value for safe inline rendering."""
    return _html_lib.escape("" if value is None else str(value))


def _render_api_health_card(check: dict) -> str:
    """Render one API health check row as an HTML card (avoids f-string
    escape sequences that break on Python 3.10/3.11)."""
    status = _e(check.get("status", "unknown"))
    endpoint = _e(check.get("endpoint", ""))
    latency = check.get("latency_ms")
    status_code = check.get("status_code")
    error = check.get("error_message")
    checked_at = _e(check.get("checked_at", ""))

    latency_html = ""
    if latency:
        latency_html = (
            '<span class="latency">' + _e(latency) + "ms</span>"
        )
    http_html = ""
    if status_code:
        http_html = "&nbsp;HTTP " + _e(status_code)
    error_html = ""
    if error:
        error_html = (
            '<p class="error">' + _e(error) + "</p>"
        )

    return (
        '<div class="card">'
        '<h3 style="margin:0 0 8px 0;font-size:15px;">'
        f'<span class="status-dot {status}"></span>'
        f"{endpoint}"
        '<span style="color:var(--muted);font-weight:400;'
        'font-size:13px;margin-left:8px;">'
        f"{latency_html}{http_html}"
        "</span></h3>"
        f"{error_html}"
        '<div style="font-size:12px;color:var(--muted);">'
        f"检测时间: {checked_at}</div>"
        "</div>"
    )


def _render_analytics_html(**ctx: Any) -> str:
    """Render analytics page HTML directly (bypasses Jinja2 for stability)."""
    from xml.sax.saxutils import escape as xml_escape

    e = xml_escape
    req = ctx["request"]
    data = ctx["data"]
    ranking = ctx.get("ranking", [])
    days = ctx["days"]
    account_id = ctx["account_id"]
    accounts = ctx.get("accounts", [])

    # Build account options
    acct_opts = "".join(
        f'<option value="{a["id"]}" {"selected" if account_id == a["id"] else ""}>{e(a["alias"])}</option>'
        for a in accounts
    )
    # Build day options
    day_opts = "".join(
        f'<option value="{d}" {"selected" if days == d else ""}>最近 {d} 天</option>'
        for d in [7, 30, 90]
    )

    # Stats
    if data.get("snapshot_count", 0) == 0:
        body = (
            '<section class="panel"><p class="muted">'
            "暂无数据。请确保 XHS_ANALYTICS_ENABLED=1 "
            "且至少发布过一篇笔记。</p></section>"
        )
        chart_js = ""
    else:
        notes_html = ""
        for i, note in enumerate(data.get("notes", [])[:10], start=1):
            rank_cls = " silver" if i == 2 else " bronze" if i == 3 else ""
            notes_html += (
                f'<tr><td><span class="rank-badge{rank_cls}">{i}</span></td>'
                f"<td>{e(str(note.get('title',''))[:24])}</td>"
                f"<td>{note.get('likes',0)}</td>"
                f"<td>{note.get('comments',0)}</td>"
                f"<td>{note.get('collects',0)}</td>"
                f"<td>{note.get('engagement_rate',0):.1f}</td></tr>"
            )

        ranking_html = ""
        if ranking:
            rank_rows = "".join(
                f'<tr><td>{e(r["alias"])}</td><td>{r["snapshots"]}</td>'
                f"<td>{(r.get('avg_er') or 0):.1f}</td></tr>"
                for r in ranking
            )
            ranking_html = (
                f'<section class="panel"><h2>账号对比</h2>'
                f'<div class="table-wrap"><table>'
                f'<tr><th>账号</th><th>快照数</th><th>平均互动率</th></tr>'
                f'{rank_rows}</table></div></section>'
            )

        trend_data = data.get("trend", [])
        chart_js = ""
        if trend_data:
            labels_js = ",".join(f"'{t['date']}'" for t in trend_data)
            values_js = ",".join(str(t.get("avg_likes", 0)) for t in trend_data)
            chart_js = f"""<script>
new Chart(document.getElementById('trendChart'),{{
  type:'line',
  data:{{labels:[{labels_js}],datasets:[{{label:'日均点赞',data:[{values_js}],
  borderColor:'#ff2442',backgroundColor:'rgba(255,36,66,0.1)',fill:true,tension:0.3}}]}},
  options:{{responsive:true,maintainAspectRatio:true}}
}});</script>"""

        body = (
            '<section class="panel">'
            '<div class="stats">'
            f'<article><b>{data["snapshot_count"]}</b><span>快照次数</span></article>'
            f'<article><b>{len(data.get("notes",[]))}</b><span>已发布笔记</span></article>'
            + (f'<article><b>{e(ranking[0]["alias"])}</b><span>最佳账号</span></article>' if ranking else "")
            + '</div>'
            '<div class="card"><canvas id="trendChart"></canvas></div></section>'
            '<section class="panel"><h2>笔记排行</h2>'
            '<div class="table-wrap"><table>'
            '<tr><th>#</th><th>笔记</th><th>点赞</th><th>评论</th><th>收藏</th><th>互动率</th></tr>'
            + notes_html
            + '</table></div></section>'
            + ranking_html
        )

    return f"""<!doctype html><html lang="zh-CN"><head>
<meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>效果度量 · 小红书运营后台</title>
<link rel="stylesheet" href="{req.url_for('static', path='/style.css')}">
<script src="https://cdn.jsdelivr.net/npm/chart.js@4.4.4/dist/chart.umd.min.js"></script>
<style>.rank-badge{{display:inline-block;width:22px;height:22px;line-height:22px;border-radius:50%;text-align:center;font-size:12px;font-weight:700;background:var(--red);color:#fff}}
.rank-badge.silver{{background:#bdc3c7}}.rank-badge.bronze{{background:#e67e22}}canvas{{max-height:300px}}</style>
</head><body>
<aside><div class="brand"><span>RED</span> 运营台</div><nav>
<a href="/">总览</a><a href="/accounts">账号管理</a><a href="/roles">角色库</a>
<a href="/searches">爆款搜索</a><a href="/library">素材库</a><a href="/publish">审核与发布</a>
<a href="/materials">授权素材与二创</a><a href="/personas">账号人设</a><a href="/research">AI 研究</a>
<a href="/engagement">互动工作台</a><a href="/rules">互动规则</a></nav>
<small>仅在本机运行<br>{ctx.get('data_dir','')}</small></aside>
<main><header><div><p class="eyebrow">PERFORMANCE</p><h1>效果度量</h1>
<p>发布后数据快照与账号对比趋势。</p></div></header>
<section class="panel compact"><form class="inline-form" method="get" action="/analytics">
<label>账号 <select name="account_id">{acct_opts}</select></label>
<label>周期 <select name="days">{day_opts}</select></label><button>查看</button></form></section>
{body}{chart_js}</main></body></html>"""


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
    analytics = AnalyticsCollector(db)
    health_monitor = ApiHealthMonitor(db)
    orchestrator = Orchestrator(db, config, store, queue, extension.store, extension.engagement, extension.ai)
    executor = ThreadPoolExecutor(max_workers=2, thread_name_prefix="xhs-interactive")
    root = Path(__file__).parent
    templates = Jinja2Templates(directory=root / "templates")
    templates.env.auto_reload = True
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
    def account_page(
        request: Request,
        group_filter: str = "",
        status_filter: str = "",
        role_filter: str = "",
        search: str = "",
    ):
        where_clauses: list[str] = []
        params: list[Any] = []

        if group_filter:
            where_clauses.append("a.group_name=?")
            params.append(group_filter)
        if status_filter == "ready":
            where_clauses.append("a.login_status='ready' AND a.enabled=1")
        elif status_filter == "unbound":
            where_clauses.append("a.login_status='unbound'")
        elif status_filter == "disabled":
            where_clauses.append("a.enabled=0")
        if role_filter:
            where_clauses.append("EXISTS (SELECT 1 FROM account_roles ar2 WHERE ar2.account_id=a.id AND ar2.role_id=?)")
            params.append(int(role_filter))
        if search:
            where_clauses.append("(a.alias LIKE ? OR a.nickname LIKE ? OR a.xhs_user_id LIKE ?)")
            like = f"%{search}%"
            params.extend([like, like, like])

        where = (" WHERE " + " AND ".join(where_clauses)) if where_clauses else ""
        accounts = db.fetchall(f"SELECT a.* FROM accounts a{where} ORDER BY a.id", tuple(params))

        role_bindings = db.fetchall(
            """SELECT ar.account_id, r.id role_id, r.name role_name, r.slug role_slug, ar.is_primary
            FROM account_roles ar JOIN roles r ON r.id=ar.role_id ORDER BY ar.id"""
        )
        roles_by_account: dict[int, list[dict[str, Any]]] = {}
        for rb in role_bindings:
            roles_by_account.setdefault(int(rb["account_id"]), []).append(rb)
        return render(
            request,
            "accounts.html",
            active="accounts",
            accounts=accounts,
            roles_by_account=roles_by_account,
            all_roles=db.fetchall("SELECT * FROM roles ORDER BY id"),
            groups=[r["group_name"] for r in db.fetchall(
                "SELECT DISTINCT group_name FROM accounts WHERE group_name!='' ORDER BY group_name"
            )],
            group_filter=group_filter,
            status_filter=status_filter,
            role_filter=role_filter,
            search=search,
            message=request.query_params.get("message"),
        )

    @app.post("/accounts")
    def create_account(alias: str = Form(...), group_name: str = Form("")):
        try:
            browsers.create_account(alias, group_name.strip())
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

    @app.post("/accounts/{account_id}/group")
    def set_account_group(account_id: int, group_name: str = Form("")):
        db.update("accounts", account_id, group_name=group_name.strip())
        return redirect("/accounts")

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

    @app.get("/api/account-groups")
    def api_account_groups():
        rows = db.fetchall("SELECT DISTINCT group_name FROM accounts WHERE group_name!='' ORDER BY group_name")
        return {"ok": True, "data": [r["group_name"] for r in rows]}

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

    @app.get("/analytics", response_class=HTMLResponse)
    def analytics_page(request: Request, days: int = 30, account_id: int = 0):
        data = analytics.summary(account_id=None if account_id == 0 else account_id, days=days)
        ranking = analytics.account_ranking(days=days)
        html = _render_analytics_html(
            request=request, data=data, ranking=ranking,
            days=days, account_id=account_id,
            accounts=db.fetchall("SELECT id,alias FROM accounts"),
            data_dir=str(config.data_dir),
        )
        return HTMLResponse(html)

    @app.get("/api-health", response_class=HTMLResponse)
    def api_health_page(request: Request):
        # Render this page directly (bypassing Jinja2 TemplateResponse)
        # because api_health.html is a standalone template and the shared
        # env's custom filter causes Jinja2 3.1's cache key to become
        # unhashable, which would break this route AND every subsequent
        # template render in the process.
        latest = health_monitor.latest() or []
        rows_html = "".join(
            _render_api_health_card(c) for c in latest
        )
        if latest:
            healthy = sum(1 for c in latest if c.get("status") == "healthy")
            total = len(latest)
            tag_cls = "ok" if healthy == total else ("warn" if healthy > 0 else "bad")
            summary_html = (
                f'<div class="summary">'
                f'<span class="tag {tag_cls}">{healthy}/{total} 正常</span>'
                f'</div>'
            )
            empty_html = ""
            probe_label = "重新检测"
        else:
            summary_html = ""
            empty_html = '<p style="color:var(--muted);">暂无检测记录。请确保 XHS_API_HEALTH_ENABLED=1。</p>'
            probe_label = "立即检测"
        html = f"""<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>API 健康</title>
<style>
  :root {{ --bg:#fff; --text:#1a1a1a; --muted:#666; --card:#f9f9f9; --border:#e0e0e0; }}
  body {{ margin:0; padding:24px; font:14px/1.6 system-ui; background:var(--bg); color:var(--text); }}
  h1 {{ font-size:20px; margin-bottom:16px; }}
  .card {{ background:var(--card); border:1px solid var(--border);
           border-radius:8px; padding:16px; margin-bottom:16px; }}
  .status-dot {{ display:inline-block; width:12px; height:12px;
                 border-radius:50%; margin-right:8px; }}
  .status-dot.healthy {{ background:#27ae60; }}
  .status-dot.degraded {{ background:#f39c12; }}
  .status-dot.timeout,.status-dot.unreachable {{ background:#e74c3c; }}
  .status-dot.unknown {{ background:#bdc3c7; }}
  table {{ width:100%; border-collapse:collapse; }}
  td,th {{ padding:10px 12px; text-align:left;
           border-bottom:1px solid var(--border); font-size:13px; }}
  th {{ font-weight:600; color:var(--muted); }}
  .latency {{ font-family:monospace; }}
  .error {{ color:#e74c3c; font-size:12px; max-width:300px;
            overflow:hidden; text-overflow:ellipsis; white-space:nowrap; }}
  .btn {{ display:inline-block; padding:8px 20px; background:#1a1a1a;
          color:#fff; border:none; border-radius:4px; cursor:pointer;
          text-decoration:none; font-size:14px; }}
  .btn:hover {{ opacity:0.8; }}
  .summary {{ display:flex; gap:16px; margin-bottom:20px; flex-wrap:wrap; }}
  .summary .tag {{ padding:6px 14px; border-radius:6px; font-size:13px; font-weight:600; }}
  .summary .tag.ok {{ background:#d4efdf; color:#1e8449; }}
  .summary .tag.warn {{ background:#fdebd0; color:#b9770e; }}
  .summary .tag.bad {{ background:#fadbd8; color:#c0392b; }}
</style>
</head>
<body>
<h1>API 健康监控</h1>
{empty_html}
{summary_html}
{rows_html}
<p><a href="#" onclick="probeNow();return false;" class="btn">{probe_label}</a></p>
<script>
async function probeNow() {{
  const btn = event.target;
  btn.textContent = "检测中…";
  btn.disabled = true;
  try {{ await fetch('/api/health/probe', {{method:'POST'}}); }} catch(e) {{}}
  location.reload();
}}
</script>
</body>
</html>"""
        return HTMLResponse(html)

    @app.get("/api/analytics/summary")
    def api_analytics_summary(days: int = 30, account_id: int = 0):
        return analytics.summary(account_id=None if account_id == 0 else account_id, days=days)

    @app.get("/api/health/status")
    def api_health_status():
        return {"ok": True, "data": health_monitor.latest()}

    @app.post("/api/health/probe")
    def api_health_probe():
        results = health_monitor.probe()
        return {"ok": True, "data": results}

    extension.install(app, templates)
    orchestrator.install(app)
    return app
