from __future__ import annotations

import logging
import time
from dataclasses import dataclass, field
from typing import Any, Optional

import litellm
from openai import OpenAI

from src.config import Settings

logger = logging.getLogger(__name__)

litellm.suppress_debug_info = True


@dataclass
class LLMResponse:
    content: str
    model_used: str
    tier: str
    prompt_tokens: int
    completion_tokens: int
    total_tokens: int
    cost_usd: float
    latency_ms: float
    raw_response: Any = field(default=None, repr=False)
    reasoning: Optional[str] = None
    reasoning_details: Optional[Any] = None


class LLMClient:
    def __init__(self, settings: Settings) -> None:
        self._settings = settings
        self._cumulative_cost: float = 0.0
        self._invocation_count: int = 0

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
        primary_model = self._resolve_model(model_tier)
        fallback_model = self._resolve_fallback_model(model_tier)
        pool_models = getattr(self._settings, "fallback_models_pool", [])

        candidate_models: list[str] = []
        for m in [primary_model, fallback_model] + pool_models:
            if m and m not in candidate_models:
                candidate_models.append(m)

        tier_label = model_tier

        if messages is not None:
            conv_messages = list(messages)
        else:
            conv_messages = []
            if system:
                conv_messages.append({"role": "system", "content": system})
            if prompt:
                conv_messages.append({"role": "user", "content": prompt})

        if self._openrouter_client is not None:
            should_reason = self._settings.enable_reasoning if reasoning is None else reasoning

            for model_candidate in candidate_models:
                max_attempts = 2
                backoff = 1.0

                for attempt in range(1, max_attempts + 1):
                    start = time.perf_counter()
                    try:
                        extra_body = {}
                        if should_reason:
                            extra_body["reasoning"] = {"enabled": True}

                        api_kwargs: dict[str, Any] = {
                            "model": model_candidate,
                            "messages": conv_messages,
                            "temperature": temperature,
                            "max_tokens": max_tokens,
                            "timeout": timeout,
                        }
                        if extra_body:
                            api_kwargs["extra_body"] = extra_body
                        if json_mode:
                            api_kwargs["response_format"] = {"type": "json_object"}

                        completion = self._openrouter_client.chat.completions.create(**api_kwargs)
                        elapsed_ms = (time.perf_counter() - start) * 1000

                        choice = completion.choices[0]
                        message = choice.message
                        content = message.content or ""

                        if not content.strip() and hasattr(choice, "error") and choice.error:
                            err_msg = str(choice.error)
                            logger.warning(
                                "OpenRouter provider payload error on %s: %s",
                                model_candidate,
                                err_msg,
                            )
                            raise RuntimeError(f"OpenRouter provider error: {err_msg}")

                        reasoning_content = None
                        if hasattr(message, "reasoning"):
                            reasoning_content = message.reasoning
                        elif hasattr(message, "reasoning_content"):
                            reasoning_content = message.reasoning_content

                        reasoning_details = getattr(message, "reasoning_details", None)

                        usage = completion.usage
                        prompt_tokens = usage.prompt_tokens if usage else 0
                        completion_tokens = usage.completion_tokens if usage else 0
                        total_tokens = usage.total_tokens if usage else (prompt_tokens + completion_tokens)

                        cost = 0.0

                        self._cumulative_cost += cost
                        self._invocation_count += 1

                        result = LLMResponse(
                            content=content.strip(),
                            model_used=model_candidate,
                            tier=tier_label,
                            prompt_tokens=prompt_tokens,
                            completion_tokens=completion_tokens,
                            total_tokens=total_tokens,
                            cost_usd=cost,
                            latency_ms=round(elapsed_ms, 1),
                            raw_response=completion,
                            reasoning=reasoning_content,
                            reasoning_details=reasoning_details,
                        )

                        logger.info(
                            "OpenRouter direct response: model=%s tier=%s tokens=%d cost=$%.5f latency=%.0fms (reasoning=%s)",
                            result.model_used,
                            result.tier,
                            result.total_tokens,
                            result.cost_usd,
                            result.latency_ms,
                            bool(reasoning_content),
                        )
                        return result

                    except Exception as e:
                        err_str = str(e).lower()
                        is_provider_failure = (
                            "503" in err_str
                            or "502" in err_str
                            or "429" in err_str
                            or "rate limit" in err_str
                            or "overloaded" in err_str
                            or "no available backend" in err_str
                        )

                        if is_provider_failure:
                            logger.warning(
                                "OpenRouter model %s is unavailable (%s). Immediate fallback to next candidate.",
                                model_candidate,
                                e,
                            )
                            break
                        if attempt < max_attempts:
                            logger.warning(
                                "OpenRouter candidate %s attempt %d/%d failed: %s. Retrying in %.1fs...",
                                model_candidate, attempt, max_attempts, e, backoff,
                            )
                            time.sleep(backoff)
                            backoff *= 2.0
                        else:
                            logger.warning(
                                "OpenRouter candidate %s failed after %d attempts: %s. Switching candidate...",
                                model_candidate, max_attempts, e,
                            )

        openrouter_key = self._settings.effective_openrouter_key
        for litellm_candidate in candidate_models:
            litellm_model = litellm_candidate
            kwargs: dict[str, Any] = {
                "messages": conv_messages,
                "temperature": temperature,
                "max_tokens": max_tokens,
                "timeout": timeout,
            }

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
                    "LiteLLM response: model=%s tier=%s tokens=%d cost=$%.5f latency=%.0fms",
                    result.model_used,
                    result.tier,
                    result.total_tokens,
                    result.cost_usd,
                    result.latency_ms,
                )
                return result
            except Exception as e:
                logger.warning("LiteLLM candidate %s failed: %s", litellm_candidate, e)
                continue

        raise RuntimeError(f"All LLM candidates failed for tier {model_tier}: {candidate_models}")

    def _resolve_model(self, model_tier: str) -> str:
        if model_tier == "tier1":
            return self._settings.tier1_model
        elif model_tier == "tier2":
            return self._settings.tier2_model
        elif model_tier == "tier1_fallback":
            return self._settings.tier1_fallback_model
        else:
            return model_tier

    def _resolve_fallback_model(self, model_tier: str) -> str:
        if model_tier == "tier1":
            return self._settings.tier1_fallback_model
        elif model_tier == "tier2":
            return getattr(self._settings, "tier2_fallback_model", self._settings.tier1_fallback_model)
        return ""

    @staticmethod
    def _calculate_cost(response: Any) -> float:
        try:
            cost = litellm.completion_cost(completion_response=response)
            return float(cost) if cost else 0.0
        except Exception:
            return 0.0

    @property
    def cumulative_cost(self) -> float:
        return self._cumulative_cost

    @property
    def invocation_count(self) -> int:
        return self._invocation_count
