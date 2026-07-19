"""Deterministic engagement governance. AI output never bypasses these checks."""

from __future__ import annotations

import logging
import re
from collections.abc import Iterable
from dataclasses import dataclass
from difflib import SequenceMatcher

logger = logging.getLogger(__name__)

PII_PATTERNS = (
    re.compile(r"(?<!\d)1[3-9]\d{9}(?!\d)"),                              # 手机号
    re.compile(r"(?:微信|微.?信|wx|wechat|v信|V信)\s*号?\s*[:：]?\s*[a-zA-Z][-_a-zA-Z0-9]{5,19}", re.I),  # 微信号
    re.compile(r"(?:地址|住址|收货地址)\s*[:：]\s*[^\n]{5,80}"),           # 地址
    re.compile(r"\b[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Z|a-z]{2,}\b"),  # 邮箱
    re.compile(r"[1-9]\d{5}(?:18|19|20)\d{2}(?:0[1-9]|1[0-2])(?:0[1-9]|[12]\d|3[01])\d{3}[\dXx]"),  # 身份证
    re.compile(r"(?:qq|Q{2})\s*[:：]?\s*[1-9]\d{4,11}", re.I),            # QQ号
)
OPT_OUT_PATTERNS = (re.compile(r"不要再联系|别联系|停止联系|不需要|别发了|退订|投诉|举报"),)
HIGH_RISK_PATTERNS = (re.compile(r"包治|治愈|保证收益|稳赚|无风险|未成年|绕过审核|规避风控"),)


def contains_sensitive_information(text: str) -> bool:
    return any(pattern.search(text or "") for pattern in PII_PATTERNS)


def contains_opt_out(text: str) -> bool:
    return any(pattern.search(text or "") for pattern in OPT_OUT_PATTERNS)


def contains_high_risk_claim(text: str) -> bool:
    return any(pattern.search(text or "") for pattern in HIGH_RISK_PATTERNS)


def normalized_similarity(left: str, right: str) -> float:
    def normalize(value):
        return re.sub(r"\s+|[^\w\u4e00-\u9fff]", "", value).lower()

    a, b = normalize(left), normalize(right)
    if not a or not b:
        return 0.0
    return SequenceMatcher(None, a, b).ratio()


def max_similarity(content: str, previous: Iterable[str]) -> float:
    return max((normalized_similarity(content, item) for item in previous), default=0.0)


@dataclass(frozen=True)
class PolicyResult:
    decision: str
    reasons: tuple[str, ...]
    sensitive: bool = False
    opt_out: bool = False
    similarity: float = 0.0


def evaluate_content(content: str, previous: Iterable[str] = (), threshold: float = 0.85) -> PolicyResult:
    reasons: list[str] = []
    sensitive = contains_sensitive_information(content)
    opt_out = contains_opt_out(content)
    risk = contains_high_risk_claim(content)
    similarity = max_similarity(content, previous)
    if sensitive:
        reasons.append("检测到手机号、微信号或地址；内容不得保存或发送，必须人工接管")
        logger.warning("敏感信息命中，操作已转人工: %s...", content[:80])
    if opt_out:
        reasons.append("检测到拒绝或投诉信号；目标必须停止触达")
        logger.warning("opt-out 命中，目标已停止触达")
    if risk:
        reasons.append("检测到高风险效果承诺或规避平台安全机制的表述")
    if similarity > threshold:
        reasons.append(f"与近期内容相似度 {similarity:.2f} 超过阈值 {threshold:.2f}")
    return PolicyResult("block" if reasons else "allow", tuple(reasons), sensitive, opt_out, similarity)


def is_warm_lead(reason: str) -> bool:
    return reason in {"inbound_dm", "owned_note_intent", "brand_mention", "opt_in_keyword"}
