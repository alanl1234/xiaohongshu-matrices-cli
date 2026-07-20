from pathlib import Path

import pytest

from xhs_cli.dashboard.config import DashboardConfig
from xhs_cli.dashboard.db import Database
from xhs_cli.dashboard.importer import PublishImporter


def config(root: Path) -> DashboardConfig:
    return DashboardConfig.load(root)


def test_parse_and_import_directory(tmp_path):
    cfg = config(tmp_path / "data")
    db = Database(cfg.database_path)
    db.create_account("品牌主账号", str(cfg.profiles_dir / "brand"))
    folder = tmp_path / "batch" / "one"
    folder.mkdir(parents=True)
    (folder / "01.jpg").write_bytes(b"image")
    (folder / "post.md").write_text(
        "---\ntitle: 周末漫步\naccount: 品牌主账号\ntopics: [城市, 周末]\nimages: [01.jpg]\n---\n正文",
        encoding="utf-8",
    )
    task_ids = PublishImporter(db, cfg).import_directory(folder.parent)
    assert len(task_ids) == 1
    task = db.fetchone("SELECT * FROM publish_tasks WHERE id=?", (task_ids[0],))
    assert task["title"] == "周末漫步"
    assert task["status"] == "pending_review"


def test_import_rejects_image_outside_post_folder(tmp_path):
    cfg = config(tmp_path / "data")
    db = Database(cfg.database_path)
    db.create_account("a", str(cfg.profiles_dir / "a"))
    folder = tmp_path / "post"
    folder.mkdir()
    (tmp_path / "outside.jpg").write_bytes(b"image")
    (folder / "post.md").write_text(
        "---\ntitle: 标题\naccount: a\nimages: [../outside.jpg]\n---\n正文", encoding="utf-8"
    )
    with pytest.raises(ValueError, match="同一素材目录"):
        PublishImporter(db, cfg).import_directory(folder)
