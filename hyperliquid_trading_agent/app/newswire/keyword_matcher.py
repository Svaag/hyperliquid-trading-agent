from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any


@dataclass(frozen=True)
class ImportanceScoreDetails:
    score: float
    reasons: list[str] = field(default_factory=list)
    penalties: list[str] = field(default_factory=list)
    keyword_hits: list[str] = field(default_factory=list)


_HIGH_IMPORTANCE_PATTERNS: tuple[tuple[str, re.Pattern[str]], ...] = (
    ("fed", re.compile(r"\b(fed|federal reserve|fomc)\b", re.I)),
    ("cpi", re.compile(r"\bcpi\b|\binflation\b", re.I)),
    ("sec", re.compile(r"\bsec\b|\bu\.s\. securities and exchange commission\b|\bsecurities and exchange commission\b", re.I)),
    ("etf", re.compile(r"\betfs?\b", re.I)),
    ("hack", re.compile(r"\bhacks?\b|\bhacked\b", re.I)),
    ("exploit", re.compile(r"\bexploits?\b|\bexploited\b", re.I)),
    ("liquidation", re.compile(r"\bliquidat(?:e|ed|es|ing|ion|ions)\b", re.I)),
    ("hyperliquid", re.compile(r"\bhyperliquid\b", re.I)),
    ("outage", re.compile(r"\boutages?\b", re.I)),
    ("listing", re.compile(r"\blist(?:s|ed|ing)?\b|\blisting\b", re.I)),
)
_BREAKING_RE = re.compile(r"\b(breaking|urgent|alert|just in|developing)\b", re.I)
_LOW_VALUE_PATTERNS: tuple[tuple[str, re.Pattern[str]], ...] = (
    ("historical_investment_article", re.compile(r"\bif you invested\b|\bwould be worth this much\b", re.I)),
    ("price_prediction_article", re.compile(r"\bprice prediction\b|\bcould .* rally\b|\bmay be poised\b", re.I)),
    ("listicle_article", re.compile(r"\btop \d+ (coins|stocks|tokens)\b|\bhere'?s why\b", re.I)),
)
_SIZE_VALUE = r"\$?\d+(?:\.\d+)?\s?(?:m|mm|million|b|bn|billion)"
_MATERIAL_VERBS = r"hack|exploit|liquidat|fine|settlement|inflow|outflow|unlock|buy|sell|sold|purchase|raise|raised|seize|seized|loss|lost|acquir|merger"
_SIZE_EVENT_FORWARD_RE = re.compile(rf"(?i)({_SIZE_VALUE}).{{0,80}}({_MATERIAL_VERBS})")
_SIZE_EVENT_REVERSE_RE = re.compile(rf"(?i)({_MATERIAL_VERBS}).{{0,80}}({_SIZE_VALUE})")


def score_importance_details(title: str, text: str, query: str = "", public_metrics: Any = None) -> ImportanceScoreDetails:
    title_text = f"{query or ''} {title or ''}".strip()
    body_text = str(text or "")
    combined = f"{title_text} {body_text}".strip()
    score = 15.0
    reasons: list[str] = ["base_importance"]
    penalties: list[str] = []
    hits: list[str] = []

    for name, pattern in _HIGH_IMPORTANCE_PATTERNS:
        if pattern.search(title_text):
            score += 10.0
            reasons.append(f"title_keyword:{name}")
            hits.append(name)
        elif pattern.search(body_text):
            score += 2.5
            reasons.append(f"body_keyword:{name}")
            hits.append(name)

    if _BREAKING_RE.search(combined):
        score += 15.0
        reasons.append("breaking_language")

    if _SIZE_EVENT_FORWARD_RE.search(title_text) or _SIZE_EVENT_REVERSE_RE.search(title_text):
        score += 10.0
        reasons.append("material_size_language:title")
    elif _SIZE_EVENT_FORWARD_RE.search(body_text) or _SIZE_EVENT_REVERSE_RE.search(body_text):
        score += 2.5
        reasons.append("material_size_language:body")

    metric_score = _public_metric_score(public_metrics or {})
    if metric_score > 0:
        boost = min(20.0, metric_score / 50.0)
        score += boost
        reasons.append("public_metrics")

    for name, pattern in _LOW_VALUE_PATTERNS:
        if pattern.search(combined):
            score -= 15.0
            penalties.append(name)

    score = max(0.0, min(100.0, score))
    return ImportanceScoreDetails(score=score, reasons=reasons, penalties=penalties, keyword_hits=sorted(set(hits)))


def _public_metric_score(metrics: Any) -> float:
    if not isinstance(metrics, dict):
        return 0.0
    return (
        float(metrics.get("like_count", 0) or 0)
        + float(metrics.get("retweet_count", 0) or 0) * 2
        + float(metrics.get("reply_count", 0) or 0) * 1.5
        + float(metrics.get("quote_count", 0) or 0) * 2
    )
