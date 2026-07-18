"""Install a catalogued Camoufox browser ZIP into the multi-version cache."""

from __future__ import annotations

import argparse
import hashlib
import shutil
import sys
from pathlib import Path
from zipfile import ZipFile

import orjson
from camoufox.multiversion import (
    BROWSERS_DIR,
    COMPAT_FLAG,
    load_config,
    load_repo_cache,
    save_config,
    set_active,
    version_folder_name,
)

# Camoufox ships a different executable name per platform:
# Windows uses "camoufox.exe"; macOS / Linux use "camoufox".
CAMOUFOX_BINARY = "camoufox.exe" if sys.platform == "win32" else "camoufox"


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def find_catalog_entry(repo: str, version_build: str, digest: str) -> dict:
    for repo_data in load_repo_cache().get("repos", []):
        if repo_data.get("name", "").lower() != repo.lower():
            continue
        for item in repo_data.get("versions", []):
            full = f"{item.get('version')}-{item.get('build')}"
            if full == version_build and item.get("sha256") == digest:
                return item
    raise SystemExit("ZIP is not the selected build in the synced Camoufox catalog.")


def safe_extract(source: Path, destination: Path) -> None:
    root = destination.resolve()
    with ZipFile(source) as archive:
        for member in archive.infolist():
            target = (root / member.filename).resolve()
            if target != root and root not in target.parents:
                raise SystemExit(f"Unsafe ZIP entry: {member.filename}")
        archive.extractall(root)


def install(source: Path, repo: str, version_build: str) -> Path:
    source = source.resolve(strict=True)
    digest = sha256(source)
    entry = find_catalog_entry(repo, version_build, digest)
    folder = version_folder_name(entry["version"], entry["build"], digest[:8])
    target = BROWSERS_DIR / repo.lower() / folder
    staging = target.with_name(f".{target.name}.installing")

    if (target / "version.json").exists() and (target / CAMOUFOX_BINARY).exists():
        set_active(f"browsers/{repo.lower()}/{folder}")
        return target

    if target.exists():
        shutil.rmtree(target)
    if staging.exists():
        shutil.rmtree(staging)
    staging.mkdir(parents=True)

    try:
        safe_extract(source, staging)
        if not (staging / CAMOUFOX_BINARY).is_file():
            raise SystemExit(f"The archive does not contain {CAMOUFOX_BINARY} at its root.")
        metadata = {
            "version": entry["version"],
            "build": entry["build"],
            "prerelease": entry.get("is_prerelease", False),
            "asset_id": entry.get("asset_id"),
            "asset_size": entry.get("asset_size"),
            "asset_updated_at": entry.get("asset_updated_at"),
            "sha256": digest,
            "created_at": entry.get("created_at"),
        }
        (staging / "version.json").write_bytes(orjson.dumps(metadata))
        staging.replace(target)
    except BaseException:
        if staging.exists():
            shutil.rmtree(staging)
        raise

    config = load_config()
    config.update(
        {
            "channel": f"{repo.lower()}/stable",
            "pinned": version_build,
            "active_version": f"browsers/{repo.lower()}/{folder}",
        }
    )
    save_config(config)
    COMPAT_FLAG.touch(exist_ok=True)
    return target


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("zip_path", type=Path)
    parser.add_argument("--repo", default="jwriter20")
    parser.add_argument("--version", default="150.0.2-beta.25")
    args = parser.parse_args()
    print(install(args.zip_path, args.repo, args.version))


if __name__ == "__main__":
    main()
