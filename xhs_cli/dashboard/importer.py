"""Validated form uploads and batch post.md directory imports."""

from __future__ import annotations

import shutil
import uuid
from pathlib import Path
from typing import Any

import yaml

from .config import DashboardConfig
from .db import Database
from .utils import is_within, json_dumps, safe_name, split_terms

IMAGE_SUFFIXES = {".jpg", ".jpeg", ".png", ".webp"}


def parse_post_markdown(path: Path) -> tuple[dict[str, Any], str]:
    text = path.read_text(encoding="utf-8-sig")
    metadata: dict[str, Any] = {}
    body = text
    if text.startswith("---"):
        parts = text.split("---", 2)
        if len(parts) == 3:
            metadata = yaml.safe_load(parts[1]) or {}
            body = parts[2].strip()
    return metadata, body


class PublishImporter:
    def __init__(self, db: Database, config: DashboardConfig):
        self.db = db
        self.config = config

    @staticmethod
    def validate(title: str, body: str, images: list[Path]) -> None:
        if not title.strip() or len(title.strip()) > 20:
            raise ValueError("标题必须为 1–20 个字符")
        if not body.strip() or len(body.strip()) > 1000:
            raise ValueError("正文必须为 1–1000 个字符")
        if not 1 <= len(images) <= 18:
            raise ValueError("图文笔记必须包含 1–18 张图片")
        for image in images:
            if not image.is_file() or image.suffix.lower() not in IMAGE_SUFFIXES:
                raise ValueError(f"不支持的图片：{image}")
            if image.stat().st_size > 20 * 1024 * 1024:
                raise ValueError(f"单张图片不能超过 20MB：{image.name}")

    def create(
        self,
        account_id: int,
        title: str,
        body: str,
        topics: list[str],
        images: list[Path],
        source_dir: str | None = None,
        copy_images: bool = True,
    ) -> int:
        self.validate(title, body, images)
        account = self.db.fetchone("SELECT id FROM accounts WHERE id=? AND enabled=1", (account_id,))
        if not account:
            raise ValueError("目标账号不可用")
        if copy_images:
            target = self.config.uploads_dir / uuid.uuid4().hex
            target.mkdir(parents=True)
            copied = []
            for index, image in enumerate(images, 1):
                destination = target / f"{index:03d}-{safe_name(image.name)}"
                shutil.copy2(image, destination)
                copied.append(str(destination))
            image_paths = copied
        else:
            image_paths = [str(path.resolve()) for path in images]
        return self.db.create_publish_task(
            account_id,
            title.strip(),
            body.strip(),
            json_dumps(split_terms(topics)),
            json_dumps(image_paths),
            source_dir,
        )

    def import_directory(self, directory: str | Path) -> list[int]:
        root = Path(directory).expanduser().resolve()
        if not root.is_dir():
            raise ValueError("批量导入目录不存在")
        task_ids = []
        post_files = [root / "post.md"] if (root / "post.md").is_file() else sorted(root.glob("*/post.md"))
        for post_file in post_files:
            metadata, body = parse_post_markdown(post_file)
            account_value = metadata.get("account") or metadata.get("account_id")
            if account_value is None:
                raise ValueError(f"{post_file} 缺少 account")
            if str(account_value).isdigit():
                account = self.db.fetchone("SELECT * FROM accounts WHERE id=?", (int(account_value),))
            else:
                account = self.db.fetchone("SELECT * FROM accounts WHERE alias=?", (str(account_value),))
            if not account:
                raise ValueError(f"{post_file} 指定的账号不存在")
            image_values = metadata.get("images") or []
            if isinstance(image_values, str):
                image_values = [image_values]
            images = [(post_file.parent / str(value)).resolve() for value in image_values]
            if not images:
                images = sorted(path for path in post_file.parent.iterdir() if path.suffix.lower() in IMAGE_SUFFIXES)
            if any(not is_within(path, post_file.parent) for path in images):
                raise ValueError(f"{post_file} 的图片必须位于同一素材目录")
            topics = metadata.get("topics") or []
            task_ids.append(
                self.create(
                    int(account["id"]),
                    str(metadata.get("title", "")),
                    body,
                    split_terms(topics),
                    images,
                    str(post_file.parent),
                )
            )
        if not task_ids:
            raise ValueError("目录中没有找到 post.md")
        return task_ids
