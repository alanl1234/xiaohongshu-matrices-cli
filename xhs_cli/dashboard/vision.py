"""Image-to-instruction decomposition via any multimodal model.

Provider-neutral by design: point it at any OpenAI-compatible multimodal
endpoint through XHS_VISION_API_KEY / XHS_VISION_BASE_URL / XHS_VISION_MODEL.
When those are unset it falls back to OPENAI_BASE_URL and the gpt-4o default,
so swapping providers is a configuration change, not a code change.
"""

from __future__ import annotations

import base64
import mimetypes
import os
from pathlib import Path
from typing import Any, TypeVar

import httpx
from pydantic import BaseModel, Field

from .utils import json_loads

T = TypeVar("T", bound=BaseModel)


class ImagePromptWords(BaseModel):
    """Structured instruction words decomposed from a single image."""

    subject: str = Field(default="", description="画面主体")
    composition: str = Field(default="", description="构图与布局")
    color_tone: str = Field(default="", description="色调与光影")
    style: str = Field(default="", description="视觉风格")
    text_overlay: str = Field(default="", description="图中文字/文案")
    reusable_prompt: str = Field(default="", description="可复用指令词，用于复现或二创")
    tags: list[str] = Field(default_factory=list, description="风格标签")


class VisionService:
    name = "vision"

    def __init__(self, api_key: str | None = None, base_url: str | None = None, model: str | None = None):
        self.api_key = api_key or os.getenv("XHS_VISION_API_KEY", "")
        self.base_url = (
            base_url or os.getenv("XHS_VISION_BASE_URL") or os.getenv("OPENAI_BASE_URL", "https://api.openai.com/v1")
        ).rstrip("/")
        self.model = model or os.getenv("XHS_VISION_MODEL", "gpt-4o")

    @property
    def configured(self) -> bool:
        return bool(self.api_key)

    @staticmethod
    def _mime_from_bytes(data: bytes) -> str:
        if data[:8] == b"\x89PNG\r\n\x1a\n":
            return "image/png"
        if data[:3] == b"\xff\xd8\xff":
            return "image/jpeg"
        if data[:6] in (b"GIF87a", b"GIF89a"):
            return "image/gif"
        if data[:4] == b"RIFF" and data[8:12] == b"WEBP":
            return "image/webp"
        return "image/png"

    @staticmethod
    def _encode_image(image: Path | str | bytes) -> dict[str, Any]:
        if isinstance(image, bytes):
            mime = VisionService._mime_from_bytes(image)
            data_url = f"data:{mime};base64,{base64.b64encode(image).decode()}"
        else:
            path = Path(image)
            if path.exists():
                mime = mimetypes.guess_type(path.name)[0] or "image/png"
                data_url = f"data:{mime};base64,{base64.b64encode(path.read_bytes()).decode()}"
            elif str(image).startswith(("http://", "https://", "data:")):
                data_url = str(image)
            else:
                raise ValueError(f"无法识别的图片输入：既不是本地文件也不是 URL：{image!r}")
        return {"type": "image_url", "image_url": {"url": data_url}}

    def analyze_image(
        self, image: Path | str | bytes, schema: type[T] = ImagePromptWords, *, instructions: str = ""
    ) -> T:
        """Decompose one image into structured instruction words.

        Args:
            image: local file path, http(s) URL, or raw bytes.
            schema: pydantic model the response is parsed into.
            instructions: optional override for the decomposition prompt.

        Returns:
            A validated instance of ``schema``.
        """
        if not self.configured:
            raise RuntimeError("未配置 XHS_VISION_API_KEY；图片拆解不会执行")
        system_prompt = instructions or (
            "分析这张图片，输出可用于复现或二创的结构化指令词，保持客观，"
            "不臆造图中没有的信息。只返回 JSON 对象。"
        )
        image_block = self._encode_image(image)
        messages = [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": [image_block, {"type": "text", "text": "请按要求的结构返回 JSON。"}]},
        ]
        body = {
            "model": self.model,
            "messages": messages,
            "response_format": {"type": "json_object"},
        }
        with httpx.Client(timeout=120) as client:
            resp = client.post(
                f"{self.base_url}/chat/completions",
                headers={"Authorization": f"Bearer {self.api_key}", "Content-Type": "application/json"},
                json=body,
            )
            resp.raise_for_status()
            data = resp.json()
        content = data["choices"][0]["message"]["content"]
        raw = content.strip()
        start, end = raw.find("{"), raw.rfind("}")
        if start != -1 and end != -1 and end > start:
            raw = raw[start : end + 1]
        try:
            return schema.model_validate_json(raw)
        except Exception:
            return schema.model_validate(json_loads(raw, {}))
