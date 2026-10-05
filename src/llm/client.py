"""
LiteLLM Client Wrapper with Cost Tracking.

Abstracts Ollama (local SLM), OpenAI, and Anthropic behind a single
interface. Tracks cost, latency, and token usage per invocation.

Features:
    - Automatic Tier 1 → fallback switching on latency threshold
    - Per-invocation cost tracking via LiteLLM metadata
    - Structured output support (JSON mode)
    - Retry with exponential backoff on transient failures

Usage:
    from src.llm.client import LLMClient, LLMResponse

    client = LLMClient(settings)
    response = client.generate(
        prompt="Generate SQL for: show total revenue",
        system="You are a SQL expert.",
        model_tier="tier1",
    )
    print(response.content, response.cost, response.latency_ms)
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass, field
from typing import Any, Optional

import litellm
from openai import OpenAI

from src.config import Settings

logger = logging.getLogger(__name__)

# Suppress litellm's verbose logging
litellm.suppress_debug_info = True


@dataclass
class LLMResponse:
    """Structured response from an LLM invocation."""

    content: str
    model_used: str
    tier: str  # "tier1", "tier1_fallback", "tier2"
    prompt_tokens: int
    completion_tokens: int
    total_tokens: int
    cost_usd: float
    latency_ms: float
    raw_response: Any = field(default=None, repr=False)
    reasoning: Optional[str] = None
    reasoning_details: Optional[Any] = None


class LLMClient:
    """
    Unified LLM client wrapping OpenRouter (via OpenAI SDK) and LiteLLM.

    Supports OpenRouter reasoning models (e.g. nvidia/nemotron-3-ultra-550b-a55b:free)
    with extra_body={"reasoning": {"enabled": True}} and multi-turn reasoning_details.
    """

    def __init__(self, settings: Settings) -> None:
        self._settings = settings
        self._cumulative_cost: float = 0.0
        self._invocation_count: int = 0

        # Initialize OpenRouter / OpenAI client if key is configured
        self._openrouter_client: Optional[OpenAI] = None
        openrouter_key = self._settings.effective_openrouter_key
        if openrouter_key:
            self._openrouter_client = OpenAI(
                base_url=self._settings.openrouter_base_url,
                api_key=openrouter_key,
            )
            logger.info("Initialized OpenRouter client at %s", self._settings.openrouter_base_url)

    def generate(
        self,
        prompt: str = "",
        system: str = "",
        messages: Optional[list[dict[str, Any]]] = None,
        model_tier: str = "tier1",
        temperature: float = 0.0,
        max_tokens: int = 2048,
        json_mode: bool = False,
        timeout: float = 30.0,
        reasoning: Optional[bool] = None,
    ) -> LLMResponse:
        """
        Generate a completion from the appropriate model.

        Supports single prompt/system or pre-assembled messages with reasoning_details.
        """
        model = self._resolve_model(model_tier)
        tier_label = model_tier

        # Build messages if not provided
        if messages is not None:
            conv_messages = list(messages)
        else:
            conv_messages = []
            if system:
                conv_messages.append({"role": "system", "content": system})
            if prompt:
                conv_messages.append({"role": "user", "content": prompt})

        # ── Pathway A: OpenRouter via OpenAI client ────────────────────
        if self._openrouter_client is not None:
            max_attempts = 3
            backoff = 1.0

            should_reason = self._settings.enable_reasoning if reasoning is None else reasoning

            for attempt in range(1, max_attempts + 1):
                start = time.perf_counter()
                try:
                    extra_body = {}
                    if should_reason:
                        extra_body["reasoning"] = {"enabled": True}

                    api_kwargs: dict[str, Any] = {
                        "model": model,
                        "messages": conv_messages,
                        "temperature": temperature,
                        "max_tokens": max_tokens,
                        "timeout": timeout,
                    }
                    if extra_body:
                        api_kwargs["extra_body"] = extra_body
                    if json_mode:
                        api_kwargs["response_format"] = {"type": "json_object"}

                    response = self._openrouter_client.chat.completions.create(**api_kwargs)
                    elapsed_ms = (time.perf_counter() - start) * 1000

                    # Check for upstream provider errors returned in 200 payload
                    if getattr(response, "error", None) or not getattr(response, "choices", None):
                        err_payload = getattr(response, "error", {})
                        raise RuntimeError(f"OpenRouter upstream error: {err_payload}")

                    choice = response.choices[0]
                    message = choice.message
                    content = message.content or ""
                    reasoning = getattr(message, "reasoning", None)
                    reasoning_details = getattr(message, "reasoning_details", None)

                    usage = response.usage
                    prompt_tokens = usage.prompt_tokens if usage else 0
                    completion_tokens = usage.completion_tokens if usage else 0
                    total_tokens = prompt_tokens + completion_tokens
                    cost = float(getattr(usage, "cost", 0.0) or 0.0)

                    self._cumulative_cost += cost
                    self._invocation_count += 1

                    res = LLMResponse(
                        content=content.strip(),
                        model_used=model,
                        tier=tier_label,
                        prompt_tokens=prompt_tokens,
                        completion_tokens=completion_tokens,
                        total_tokens=total_tokens,
                        cost_usd=cost,
                        latency_ms=round(elapsed_ms, 1),
                        raw_response=response,
                        reasoning=reasoning,
                        reasoning_details=reasoning_details,
                    )

                    logger.info(
                        "OpenRouter response: model=%s tokens=%d cost=$%.5f latency=%.0fms",
                        res.model_used, res.total_tokens, res.cost_usd, res.latency_ms,
                    )
                    return res

                except Exception as e:
                    if attempt < max_attempts:
                        logger.warning(
                            "OpenRouter call failed (attempt %d/%d): %s. Retrying in %.1fs...",
                            attempt, max_attempts, e, backoff
                        )
                        time.sleep(backoff)
                        backoff *= 2.0
                    else:
                        logger.warning("OpenRouter call failed after %d attempts: %s", max_attempts, e)

        # ── Pathway B: LiteLLM fallback ────────────────────────────────
        litellm_model = model
        openrouter_key = self._settings.effective_openrouter_key

        kwargs: dict[str, Any] = {
            "messages": conv_messages,
            "temperature": temperature,
            "max_tokens": max_tokens,
            "timeout": timeout,
        }

        # If OpenRouter is configured and model isn't prefixed, route via openrouter/
        if openrouter_key and not litellm_model.startswith("ollama/") and not litellm_model.startswith("azure/"):
            if not litellm_model.startswith("openrouter/"):
                litellm_model = f"openrouter/{litellm_model}"
            kwargs["api_key"] = openrouter_key
            kwargs["api_base"] = self._settings.openrouter_base_url

        kwargs["model"] = litellm_model

        if json_mode:
            kwargs["response_format"] = {"type": "json_object"}

        start = time.perf_counter()
        try:
            response = litellm.completion(**kwargs)
        except litellm.exceptions.Timeout:
            if model_tier == "tier1":
                fallback_model = self._settings.tier1_fallback_model
                if openrouter_key and not fallback_model.startswith("openrouter/"):
                    fallback_model = f"openrouter/{fallback_model}"
                logger.warning(
                    "Tier 1 timeout (%.1fs) — falling back to %s",
                    timeout,
                    fallback_model,
                )
                kwargs["model"] = fallback_model
                tier_label = "tier1_fallback"
                start = time.perf_counter()
                response = litellm.completion(**kwargs)
            else:
                raise

        elapsed_ms = (time.perf_counter() - start) * 1000

        choice = response.choices[0]
        content = choice.message.content or ""

        usage = response.usage
        prompt_tokens = usage.prompt_tokens if usage else 0
        completion_tokens = usage.completion_tokens if usage else 0
        total_tokens = prompt_tokens + completion_tokens

        cost = self._calculate_cost(response)
        self._cumulative_cost += cost
        self._invocation_count += 1

        result = LLMResponse(
            content=content.strip(),
            model_used=kwargs["model"],
            tier=tier_label,
            prompt_tokens=prompt_tokens,
            completion_tokens=completion_tokens,
            total_tokens=total_tokens,
            cost_usd=cost,
            latency_ms=round(elapsed_ms, 1),
            raw_response=response,
        )

        logger.info(
            "LLM response: model=%s tier=%s tokens=%d cost=$%.5f latency=%.0fms",
            result.model_used,
            result.tier,
            result.total_tokens,
            result.cost_usd,
            result.latency_ms,
        )

        return result

    def _resolve_model(self, model_tier: str) -> str:
        """Resolve a tier name to a concrete model identifier."""
        if model_tier == "tier1":
            return self._settings.tier1_model
        elif model_tier == "tier2":
            return self._settings.tier2_model
        elif model_tier == "tier1_fallback":
            return self._settings.tier1_fallback_model
        else:
            # Treat as explicit model name
            return model_tier

    @staticmethod
    def _calculate_cost(response: Any) -> float:
        """Extract cost from LiteLLM response metadata."""
        try:
            # LiteLLM provides cost calculation via completion_cost
            cost = litellm.completion_cost(completion_response=response)
            return float(cost) if cost else 0.0
        except Exception:
            return 0.0

    @property
    def cumulative_cost(self) -> float:
        """Total cost across all invocations in this session."""
        return self._cumulative_cost

    @property
    def invocation_count(self) -> int:
        """Number of LLM invocations in this session."""
        return self._invocation_count
