"""Central structured judgments and conservative semantic model routing."""
from __future__ import annotations

import asyncio
import logging
import math
import time
from collections import Counter, deque
from fnmatch import fnmatch
from typing import Any

from app.config import BrokerConfig
from app.mcp import MCPError, MCPRegistry
from app.providers.base import ProviderError
from app.providers.system1 import laya_judgment, ollama_judgment
from app.schemas import (
    InferenceKind,
    System1Attempt,
    System1ModelJudgment,
    System1Request,
    System1Response,
    TaskCreateRequest,
)

logger = logging.getLogger("ai_broker.system1")
TIERS = ("simple", "medium", "complex")
DEFAULT_PROFILE = "default"
UNCERTAIN = {"LOW_CONFIDENCE", "INSUFFICIENT_MARGIN"}


def validate_judgment(judgment: System1ModelJudgment, request: System1Request) -> None:
    def valid(value: Any) -> bool:
        if request.decision_type == "binary":
            return type(value) is bool
        if request.decision_type == "choice":
            return isinstance(value, str) and value in request.options
        return (type(value) in (int, float) and math.isfinite(value)
                and value == int(value) and 0 <= value < len(request.rubric))

    values = [item.value for item in judgment.alternatives]
    if not valid(judgment.decision) or any(not valid(value) for value in values):
        raise ValueError("decision or alternative outside contract")
    if judgment.decision in values or len(set(values)) != len(values):
        raise ValueError("duplicate alternative")
    if any(item.confidence > judgment.confidence for item in judgment.alternatives):
        raise ValueError("selected decision is not top1")
    # Binary included: a lone `true` at 1.0 with no score for `false` is a
    # claim, not a distribution, and it was accepted as one.
    if request.decision_type == "binary":
        expected: set[Any] = {True, False}
    elif request.decision_type == "choice":
        expected = set(request.options)
    else:
        expected = set(range(len(request.rubric)))
    if {judgment.decision, *values} != expected:
        raise ValueError("all alternative scores are required for a trustworthy margin")


class System1Service:
    def __init__(self, config: BrokerConfig, mcp: MCPRegistry, ollama: Any = None, inference_slot: Any = None):
        self.config = config
        self.mcp = mcp
        self.ollama = ollama
        self.inference_slot = inference_slot
        self.counts: Counter[str] = Counter()
        self.samples: deque[dict[str, Any]] = deque(maxlen=1000)

    def _record(self, event: str, **fields: Any) -> None:
        self.counts[event] += 1
        if event == "system1.attempt":
            self.counts[f"provider:{fields['provider']}"] += 1
            self.counts[f"use_case:{fields['use_case']}"] += 1
            if fields.get("reason_code"):
                self.counts[f"error:{fields['reason_code']}"] += 1
        self.samples.append({"event": event, **fields})
        logger.info(event, extra={"event": event, **fields})

    def metrics(self) -> dict[str, Any]:
        return {"counts": dict(self.counts), "recent": list(self.samples), "confidence_is_calibrated": False}

    async def judge(self, request: System1Request) -> System1Response:
        started = time.perf_counter()
        settings = self.config.system1
        profile_name = request.threshold_profile or request.use_case
        profile = settings.thresholds.get(profile_name, settings.default_threshold)
        use_case_profile = settings.thresholds.get(request.use_case, settings.default_threshold)
        instructions = request.instructions or use_case_profile.instructions or profile.instructions
        self._record("system1.call", use_case=request.use_case)
        if not settings.enabled:
            return self._fallback(request, started, [], "SYSTEM1_DISABLED")
        # The default threshold must be asked for by name. Falling back to it
        # silently let a misspelt use_case ("goal_completio") pass at 0.85 what
        # its real profile demands at 0.97.
        if profile_name not in settings.thresholds and request.threshold_profile != DEFAULT_PROFILE:
            return self._fallback(request, started, [], "UNKNOWN_THRESHOLD_PROFILE"
                                  if request.threshold_profile else "UNKNOWN_USE_CASE")
        if not instructions:
            return self._fallback(request, started, [], "MISSING_INSTRUCTIONS")
        attempts: list[System1Attempt] = []
        priority = settings.provider_priority if settings.fallback_policy == "provider_then_current" else settings.provider_priority[:1]
        target_model = None
        if request.target is not None:
            # Evaluation pins one judge: substituting another provider would
            # attribute its answer to the model under test.
            priority = [request.target.provider]
            target_model = request.target.model
        for provider in priority:
            attempt_started = time.perf_counter()
            model = target_model or (settings.laya.model if provider == "laya_mcp" else settings.ollama.model)
            judgment = None
            scored = None
            source = "native"
            usage: dict[str, Any] = {}
            reason = None
            try:
                invocation = await asyncio.wait_for(
                    self._invoke(provider, request, instructions, target_model), timeout=settings.timeout_seconds,
                )
                judgment, model = invocation[:2]
                usage = invocation[2] if len(invocation) > 2 else {}
                source = invocation[3] if len(invocation) > 3 else "native"
                if not isinstance(judgment, System1ModelJudgment) or not isinstance(usage, dict):
                    raise ValueError("unexpected provider contract")
                if any(type(value) is not int or value < 0 for key, value in usage.items()
                       if key in {"input_tokens", "output_tokens"}):
                    raise ValueError("invalid token usage")
                validate_judgment(judgment, request)
                # A valid judgment keeps its scores even when the threshold
                # rejects it: calibrating needs the low ones too.
                scored = judgment
                if source == "self_reported":
                    # A System-2 teacher label: evidence for distillation, never
                    # a decision the broker vouches for, whatever it claims.
                    reason = "SELF_REPORTED_SCORE"
                elif judgment.confidence < profile.confidence:
                    reason = "LOW_CONFIDENCE"
                elif request.decision_type != "binary" and judgment.confidence - max(
                    item.confidence for item in judgment.alternatives
                ) < profile.min_margin:
                    reason = "INSUFFICIENT_MARGIN"
            except asyncio.TimeoutError:
                reason = "TIMEOUT"
            except ProviderError as error:
                reason = error.code
            except MCPError:
                reason = "MCP_ERROR"
            except (ValueError, TypeError, KeyError, AttributeError):
                reason = "INVALID_OUTPUT"
            except Exception:
                # Optional provider infrastructure must not break the old router.
                # Cancellation derives from BaseException and deliberately propagates.
                reason = "INTERNAL_PROVIDER_ERROR"
            attempt = System1Attempt(provider=provider, model=model,
                                     latency_ms=round((time.perf_counter() - attempt_started) * 1000, 3),
                                     reason_code=reason,
                                     tokens_input=usage.get("input_tokens") if reason != "INVALID_OUTPUT" else None,
                                     tokens_output=usage.get("output_tokens") if reason != "INVALID_OUTPUT" else None,
                                     **({**scored.model_dump(), "score_source": source} if scored is not None else {}))
            attempts.append(attempt)
            self._record("system1.attempt", use_case=request.use_case,
                         **attempt.model_dump(exclude={"decision", "confidence", "alternatives"}),
                         confidence=attempt.confidence, fallback_used=len(attempts) > 1,
                         targeted=request.target is not None)
            if reason is None and judgment is not None:
                result = System1Response(
                    use_case=request.use_case, **judgment.model_dump(), provider=provider, model=model,
                    latency_ms=round((time.perf_counter() - started) * 1000, 3),
                    accepted=True, fallback_used=len(attempts) > 1,
                    reason_code=attempts[0].reason_code if len(attempts) > 1 else None, attempts=attempts,
                )
                self._record("system1.decision", use_case=result.use_case, provider=provider, model=model,
                             latency_ms=result.latency_ms, confidence=result.confidence,
                             decision=result.decision, fallback_used=result.fallback_used, reason_code=result.reason_code)
                return result
            if reason in UNCERTAIN:
                # The provider worked and was unsure. The next one exists for
                # providers that fail, not as a second opinion: asking a weaker
                # classifier until one sounds confident defeats the threshold.
                break
        return self._fallback(request, started, attempts, attempts[-1].reason_code or "PROVIDER_UNAVAILABLE")

    async def _invoke(self, provider: str, request: System1Request, instructions: str, model: str | None = None):
        if provider == "laya_mcp":
            return await laya_judgment(self.config, self.mcp, request, instructions, model)
        return await ollama_judgment(self.config, self.ollama, request, instructions, self.inference_slot, model)

    def _fallback(self, request, started, attempts, reason) -> System1Response:
        result = System1Response(
            use_case=request.use_case, latency_ms=round((time.perf_counter() - started) * 1000, 3),
            fallback_used=True, reason_code=reason, attempts=attempts,
            provider=attempts[-1].provider if attempts else None, model=attempts[-1].model if attempts else None,
        )
        self._record("system1.fallback", use_case=result.use_case, latency_ms=result.latency_ms,
                     provider=result.provider, model=result.model, fallback_used=True, reason_code=reason)
        return result

    def _tier(self, entry: dict[str, Any]) -> str | None:
        identity = f"{entry['provider']}/{entry['deployment']}/{entry['name']}"
        matches = {tier for pattern, tier in self.config.system1.routing.model_tiers.items()
                   if fnmatch(identity.lower(), pattern.lower())}
        return next(iter(matches)) if len(matches) == 1 else None

    async def route(self, request: TaskCreateRequest, candidates: list[dict[str, Any]]) -> list[dict[str, Any]]:
        settings = self.config.system1.routing
        if not self.config.system1.enabled or not settings.enabled or len(candidates) < 2:
            return candidates
        if request.inference_kind != InferenceKind.chat:
            return candidates
        if not request.auxiliary_invocations:
            return candidates
        tiers = {id(item): self._tier(item) for item in candidates}
        mapped = [item for item in candidates if tiers[id(item)] is not None]
        # Without explicit capability evidence retain the existing router.
        if len({tiers[id(item)] for item in mapped}) < 2:
            self._record("system1.routing_skipped", use_case="semantic_routing", reason_code="UNKNOWN_MODEL_TIER")
            return candidates
        judgment = await self.judge(System1Request(
            use_case="semantic_routing", decision_type="choice", options=list(TIERS),
            criteria={"simple": "lookup, short translation, formatting or one-step answer",
                      "medium": "several ordinary steps, routine coding or summarization",
                      "complex": "proofs, specialist knowledge, architecture, security or long multi-step reasoning"},
            input={"request": request.content.prompt}, instructions=settings.instructions,
            cloud_allowed=bool(request.model_requirements.cloud_allowed),
        ))
        proposed = candidates
        reason = judgment.reason_code
        if judgment.accepted:
            minimum = TIERS.index(str(judgment.decision))
            capable = [item for item in mapped if TIERS.index(str(tiers[id(item)])) >= minimum]
            if capable:
                # Preserve the broker's evidence ranking inside each tier and
                # keep only routes capable of the accepted semantic level.
                proposed = sorted(capable, key=lambda item: TIERS.index(str(tiers[id(item)])))
            else:
                reason = "NO_MODEL_AT_REQUIRED_TIER"
        chosen = candidates if settings.shadow_mode else proposed
        self._record("system1.routing", use_case="semantic_routing", provider=judgment.provider,
                     latency_ms=judgment.latency_ms, confidence=judgment.confidence, decision=judgment.decision,
                     model=chosen[0]["name"], proposed_model=proposed[0]["name"],
                     previous_model=candidates[0]["name"], shadow_mode=settings.shadow_mode,
                     fallback_used=judgment.fallback_used or proposed is candidates, reason_code=reason)
        return chosen
