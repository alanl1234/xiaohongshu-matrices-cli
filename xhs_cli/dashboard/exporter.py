"""Atomic Markdown and image export for collected notes."""

from __future__ import annotations

import mimetypes
import os
import tempfile
from pathlib import Path
from typing import Any

import httpx

from .config import DashboardConfig
from .db import Database
from .utils import content_checksum, json_dumps, json_loads, now_iso, safe_name


def _yaml_text(value: Any) -> str:
    text = str(value if value is not None else "").replace("\\", "\\\\").replace('"', '\\"').replace("\n", " ")
    return f'"{text}"'


class MarkdownExporter:
    def __init__(self, db: Database, config: DashboardConfig):
        self.db = db
        self.config = config

    def _download_image(self, url: str, directory: Path, index: int) -> dict[str, Any]:
        suffix = Path(url.split("?", 1)[0]).suffix.lower()
        if suffix not in {".jpg", ".jpeg", ".png", ".webp", ".avif"}:
            suffix = ".jpg"
        target = directory / f"{index:03d}{suffix}"
        if target.exists() and target.stat().st_size:
            return {"url": url, "path": target.name, "status": "reused"}
        temp_name = None
        try:
            with httpx.stream("GET", url, timeout=30, follow_redirects=True) as response:
                response.raise_for_status()
                content_type = response.headers.get("content-type", "").split(";", 1)[0]
                guessed = mimetypes.guess_extension(content_type) if content_type.startswith("image/") else None
                if guessed and suffix == ".jpg" and guessed in {".png", ".webp", ".avif"}:
                    target = target.with_suffix(guessed)
                with tempfile.NamedTemporaryFile(delete=False, dir=directory, suffix=".part") as handle:
                    temp_name = handle.name
                    for chunk in response.iter_bytes():
                        handle.write(chunk)
            if not os.path.getsize(temp_name):
                raise ValueError("图片响应为空")
            os.replace(temp_name, target)
            return {"url": url, "path": target.name, "status": "downloaded"}
        except Exception as exc:
            if temp_name:
                Path(temp_name).unlink(missing_ok=True)
            return {"url": url, "path": "", "status": "failed", "error": str(exc)}

    def export(self, job: dict[str, Any], note: dict[str, Any]) -> Path:
        job_dir = self.config.library_dir / f"{job['id']}-{safe_name(job['name'])}"
        note_dir = job_dir / safe_name(note["note_id"])
        image_dir = note_dir / "images"
        image_dir.mkdir(parents=True, exist_ok=True)
        image_urls = json_loads(note.get("images_json"), [])
        results = [self._download_image(url, image_dir, index) for index, url in enumerate(image_urls, 1)]
        comments = json_loads(note.get("comments_json"), [])
        topics = json_loads(note.get("topics_json"), [])
        lines = ["---"]
        fields = {
            "note_id": note["note_id"],
            "title": note["title"],
            "author_id": note["author_id"],
            "author": note["author_name"],
            "source_url": note["original_url"],
            "published_at": note.get("published_at") or "",
            "media_type": note["media_type"],
            "likes": note["likes"],
            "collects": note["collects"],
            "comments": note["comments"],
            "shares": note["shares"],
            "viral_score": note["viral_score"],
            "collected_at": now_iso(),
        }
        for key, value in fields.items():
            lines.append(f"{key}: {_yaml_text(value)}")
        lines.append("topics: [" + ", ".join(_yaml_text(item) for item in topics) + "]")
        lines.extend(["---", "", f"# {note['title'] or '无标题'}", "", note["body"], "", "## 图片", ""])
        for result in results:
            if result["path"]:
                lines.append(f"![图片](images/{result['path']})")
            else:
                lines.append(f"- 下载失败：{result['url']}")
        lines.extend(["", f"## 评论（已保存 {len(comments)} 条）", ""])
        for item in comments:
            nickname = item.get("nickname") or item.get("user_name") or "匿名"
            content = str(item.get("content", "")).replace("\n", " ")
            likes = item.get("like_count", 0)
            lines.append(f"- **{nickname}**（赞 {likes}）：{content}")
        markdown = "\n".join(lines).rstrip() + "\n"
        temp = note_dir / "note.md.part"
        temp.write_text(markdown, encoding="utf-8")
        os.replace(temp, note_dir / "note.md")
        checksum = content_checksum({"note": note["note_id"], "markdown": markdown, "images": results})
        self.db.upsert_export(
            job["id"],
            note["id"],
            str(note_dir),
            "partial" if any(item["status"] == "failed" for item in results) else "complete",
            checksum,
            json_dumps(results),
        )
        self._write_manifest(job_dir, job["id"])
        return note_dir

    def _write_manifest(self, job_dir: Path, job_id: int) -> None:
        rows = self.db.fetchall(
            """SELECT n.note_id,n.title,n.author_name,n.viral_score,e.directory,e.status,e.checksum
        FROM export_bundles e JOIN notes n ON n.id=e.note_id WHERE e.job_id=? ORDER BY n.viral_score DESC""",
            (job_id,),
        )
        temp = job_dir / "manifest.json.part"
        temp.write_text(json_dumps({"job_id": job_id, "generated_at": now_iso(), "items": rows}), encoding="utf-8")
        os.replace(temp, job_dir / "manifest.json")
