"""One-time migration of the legacy single-account cookie file."""

from __future__ import annotations

import argparse
import json
from pathlib import Path

from .config import DashboardConfig
from .db import Database


def migrate_legacy_cookie(data_dir: str | Path | None = None) -> int:
    config = DashboardConfig.load(data_dir)
    db = Database(config.database_path)
    legacy = Path.home() / ".xiaohongshu-cli" / "cookies.json"
    if not legacy.is_file():
        raise ValueError("没有找到旧版 ~/.xiaohongshu-cli/cookies.json")
    payload = json.loads(legacy.read_text(encoding="utf-8"))
    cookies = [
        {"name": name, "value": value, "domain": ".xiaohongshu.com", "path": "/"}
        for name, value in payload.items()
        if name != "saved_at" and isinstance(value, str)
    ]
    if not any(item["name"] == "a1" for item in cookies):
        raise ValueError("旧版 Cookie 中缺少 a1")
    account = db.fetchone("SELECT * FROM accounts WHERE alias='default'")
    if account:
        account_id = int(account["id"])
    else:
        profile = config.profiles_dir / "default"
        profile.mkdir(parents=True, exist_ok=True)
        account_id = db.create_account("default", str(profile.resolve()))
        account = db.fetchone("SELECT * FROM accounts WHERE id=?", (account_id,))
    from camoufox.sync_api import Camoufox

    with Camoufox(
        headless=True, locale="zh-CN", persistent_context=True, user_data_dir=account["profile_dir"]
    ) as context:
        context.add_cookies(cookies)
    db.update("accounts", account_id, login_status="needs_verification", last_error=None)
    return account_id


def main() -> None:
    parser = argparse.ArgumentParser(description="迁移旧版单账号 Cookie")
    parser.add_argument("--data-dir", default=None)
    args = parser.parse_args()
    account_id = migrate_legacy_cookie(args.data_dir)
    print(f"Migrated legacy cookies to account #{account_id}; verify it in the dashboard.")


if __name__ == "__main__":
    main()
