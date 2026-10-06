from __future__ import annotations

import logging
import re
from dataclasses import dataclass
from enum import Enum

from src.config import Settings

logger = logging.getLogger(__name__)


class Tier(str, Enum):
    TIER_1_SLM = "TIER_1_SLM"
    TIER_2_FRONTIER = "TIER_2_FRONTIER"


@dataclass
class RoutingDecision:
    tier: Tier
    confidence: float
    reason: str
    features: dict
    ambiguous: bool


SIMPLE_PATTERNS: list[tuple[str, float]] = [
    (r"\bhow many\b", 0.2),
    (r"\bcount\b", 0.15),
    (r"\btotal\b", 0.1),
    (r"\blist\b", 0.2),
    (r"\bshow me\b", 0.15),
    (r"\btop \d+\b", 0.15),
    (r"\baverage\b", 0.1),
    (r"\bwhat is the\b", 0.1),
    (r"\bmost (popular|common|frequent)\b", 0.15),
]

COMPLEX_PATTERNS: list[tuple[str, float]] = [
    (r"\bcompare\b", 0.3),
    (r"\bcorrelat", 0.35),
    (r"\btrend\b", 0.25),
    (r"\bgrowth\b", 0.2),
    (r"\bover time\b", 0.25),
    (r"\bmonth.over.month\b", 0.3),
    (r"\byear.over.year\b", 0.3),
    (r"\bpercentage\b", 0.15),
    (r"\b(ratio|rate)\b", 0.2),
    (r"\bversus|vs\.?\b", 0.25),
    (r"\b(breakdown|decomposition)\b", 0.2),
    (r"\b(anomal|outlier|unusual)\b", 0.3),
    (r"\b(predict|forecast|project)\b", 0.35),
    (r"\bcohort\b", 0.35),
    (r"\bretention\b", 0.3),
    (r"\bfunnel\b", 0.3),
    (r"\bsegment\b", 0.2),
    (r"\bwindow\b", 0.2),
    (r"\branking|percentile\b", 0.2),
    (r"\bmoving average\b", 0.3),
    (r"\bcumulative\b", 0.25),
    (r"\bpivot\b", 0.25),
    (r"\bcross.tab\b", 0.3),
]

AMBIGUITY_PATTERNS: list[str] = [
    r"^(how|what|show|tell)\b.{0,15}$",
    r"\b(it|them|those|that|these)\b(?!.*\b(table|column|order|product)\b)",
    r"\bthe data\b",
    r"\beverything\b",
    r"\bsome\b.*\banalysis\b",
]


class QueryRouter:
    def __init__(self, settings: Settings) -> None:
        self._settings = settings

    def route(
        self,
        query: str,
        linked_tables: list[str] | None = None,
    ) -> RoutingDecision:
        features = self._extract_features(query, linked_tables or [])
        complexity = self._compute_complexity(features)

        if complexity >= 0.55:
            tier = Tier.TIER_2_FRONTIER
            confidence = min(complexity, 1.0)
            reason = "High complexity score: " + ", ".join(features["complex_signals"])
        else:
            tier = Tier.TIER_1_SLM
            confidence = 1.0 - complexity
            reason = "Low complexity score"
            if features["simple_signals"]:
                reason += ": " + ", ".join(features["simple_signals"][:3])

        ambiguous = (
            confidence < self._settings.router_confidence_threshold
            or features["ambiguity_score"] > 0.3
        )

        decision = RoutingDecision(
            tier=tier,
            confidence=round(confidence, 3),
            reason=reason,
            features=features,
            ambiguous=ambiguous,
        )

        logger.info(
            "Routing: '%s' → %s (confidence=%.2f, ambiguous=%s)",
            query[:60], tier.value, confidence, ambiguous,
        )

        return decision

    def _extract_features(
        self, query: str, linked_tables: list[str]
    ) -> dict:
        query_lower = query.lower()

        simple_score = 0.0
        simple_signals = []
        for pattern, weight in SIMPLE_PATTERNS:
            if re.search(pattern, query_lower, re.IGNORECASE):
                simple_score += weight
                simple_signals.append(pattern.strip("\\b"))

        complex_score = 0.0
        complex_signals = []
        for pattern, weight in COMPLEX_PATTERNS:
            if re.search(pattern, query_lower, re.IGNORECASE):
                complex_score += weight
                complex_signals.append(pattern.strip("\\b"))

        table_count = len(linked_tables)
        if table_count >= 4:
            complex_score += 0.3
            complex_signals.append(f"{table_count} tables linked")
        elif table_count >= 3:
            complex_score += 0.15
            complex_signals.append(f"{table_count} tables linked")

        word_count = len(query.split())
        if word_count > 25:
            complex_score += 0.15
            complex_signals.append("long query")
        elif word_count < 6:
            simple_score += 0.1
            simple_signals.append("short query")

        ambiguity_score = 0.0
        for pattern in AMBIGUITY_PATTERNS:
            if re.search(pattern, query_lower, re.IGNORECASE):
                ambiguity_score += 0.15

        return {
            "simple_score": round(simple_score, 3),
            "complex_score": round(complex_score, 3),
            "ambiguity_score": round(min(ambiguity_score, 1.0), 3),
            "table_count": table_count,
            "word_count": word_count,
            "simple_signals": simple_signals,
            "complex_signals": complex_signals,
        }

    @staticmethod
    def _compute_complexity(features: dict) -> float:
        simple = features["simple_score"]
        complex_ = features["complex_score"]

        total = simple + complex_ + 0.1
        complexity = complex_ / total

        if features["ambiguity_score"] > 0.3:
            complexity = min(complexity + 0.15, 1.0)

        return round(complexity, 3)
