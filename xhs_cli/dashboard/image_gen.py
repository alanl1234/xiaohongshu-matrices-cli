"""Optional AI image generation for draft publishing.

This module is *opt-in*: set XHS_AUTO_IMAGE_GEN=1 to enable auto-generation
inside the agent pipeline. Otherwise drafts are published as-is (text only,
pending manual image attachment).

Provider: OpenAI DALL-E compatible endpoint. Set XHS_IMAGE_GEN_API_KEY
and optionally XHS_IMAGE_GEN_BASE_URL to point at any compatible service.
"""

from __future__ import annotations

import base64
import hashlib
import os
import time
from pathlib import Path
from typing import Any, Protocol

import httpx

IMAGE_SUFFIXES = {".jpg", ".jpeg", ".png", ".webp"}


class ImageGenProvider(Protocol):
    name: str

    def generate(self, prompt: str, *, size: str = "1024x1024", quality: str = "standard") -> list[dict[str, Any]]: ...


class OpenAIImageGenProvider:
    """DALL-E 2/3 compatible provider. Falls back gracefully."""

    name = "openai"

    def __init__(self) -> None:
        self.key = os.getenv("XHS_IMAGE_GEN_API_KEY", "")
        self.base = os.getenv("XHS_IMAGE_GEN_BASE_URL", "https://api.openai.com/v1").rstrip("/")
        self.model = os.getenv("XHS_IMAGE_GEN_MODEL", "dall-e-3")
        self.size = os.getenv("XHS_IMAGE_GEN_SIZE", "1024x1024")
        self.quality = os.getenv("XHS_IMAGE_GEN_QUALITY", "standard")

    @property
    def configured(self) -> bool:
        return bool(self.key)

    def generate(self, prompt: str, *, size: str = "", quality: str = "") -> list[dict[str, Any]]:
        if not self.configured:
            raise RuntimeError("XHS_IMAGE_GEN_API_KEY is not set")
        body = {
            "model": self.model,
            "prompt": prompt,
            "n": 1,
            "size": size or self.size,
            "quality": quality or self.quality,
        }
        headers = {
            "Authorization": f"Bearer {self.key}",
            "Content-Type": "application/json",
        }
        endpoint = f"{self.base}/images/generations"
        try:
            resp = httpx.post(endpoint, json=body, headers=headers, timeout=120)
            resp.raise_for_status()
            data = resp.json()
            results = data.get("data") or []
            enriched: list[dict[str, Any]] = []
            for item in results:
                item["_model"] = self.model
                item["_prompt_hash"] = hashlib.sha256(prompt.encode()).hexdigest()[:12]
                enriched.append(item)
            return enriched
        except httpx.HTTPError as exc:
            raise RuntimeError(f"Image generation failed: {exc}") from exc


class ImageGenService:
    """Generates images for a draft and saves them as local files."""

    def __init__(self, provider: ImageGenProvider | None = None, output_dir: Path | str = ""):
        self.provider = provider or OpenAIImageGenProvider()
        self.out = Path(output_dir) if output_dir else Path.home() / ".xiaohongshu-cli" / "generated_images"
        self.out.mkdir(parents=True, exist_ok=True)

    @property
    def enabled(self) -> bool:
        return self.provider.configured

    def _save_image(self, b64_data: str, prompt: str, index: int) -> Path:
        raw = base64.b64decode(b64_data)
        path = self.out / f"gen-{hashlib.sha256(prompt.encode()).hexdigest()[:16]}-{index}.png"
        path.write_bytes(raw)
        return path.resolve()

    def generate_for_draft(self, title: str, body: str, count: int = 3) -> list[Path]:
        """Generate images matching a draft's title + first sentence as visual prompt.

        Args:
            title: Post title
            body: Post body (first ~200 chars used)
            count: How many images to request (1–4, default 3)

        Returns:
            List of saved file paths
        """
        if not self.enabled:
            return []
        snippet = body[:200].strip()
        base_prompt = f"小红书配图：{title}. {snippet}".strip()
        # prepend safe prefix so the model avoids faces / wechat QR / phone numbers
        safe_prefix = "clean editorial illustration, no faces, no text overlay, no QR codes, no phone numbers: "
        full = safe_prefix + base_prompt
        count = max(1, min(count, 4))
        paths: list[Path] = []
        for i in range(count):
            try:
                results = self.provider.generate(full)
                for item in results:
                    b64 = item.get("b64_json")
                    url = item.get("url")
                    if b64:
                        paths.append(self._save_image(b64, full, i))
                    elif url:
                        raw = httpx.get(url, timeout=30).content
                        path = self.out / f"gen-{hashlib.sha256(full.encode()).hexdigest()[:16]}-{i}.png"
                        path.write_bytes(raw)
                        paths.append(path.resolve())
                    else:
                        continue
            except Exception:
                continue
            if paths and i < count - 1:
                time.sleep(0.5)
        return paths


# ── gate for auto-gen inside the agent pipeline ──

def auto_image_gen_enabled() -> bool:
    return os.getenv("XHS_AUTO_IMAGE_GEN", "0").strip() in {"1", "true", "yes"}
