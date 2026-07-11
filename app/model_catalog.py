"""Presentación del catálogo: disponibilidad operativa y matriz de features.

Traduce las entradas crudas de los providers a los modelos de respuesta de
/api/v1/models/availability y /api/v1/models/context.
"""
from __future__ import annotations

from typing import Any, Literal, cast

from app.schemas import ModelAvailabilityItem


def model_availability_item(entry: dict[str, Any], health: dict[str, dict[str, Any]]) -> ModelAvailabilityItem:
    provider_name = str(entry.get("provider") or "unknown")
    deployment_name = str(entry.get("deployment") or "unknown")
    raw_provider_status = str(
        (
            health.get(provider_name.lower())
            or health.get(provider_name)
            or health.get(deployment_name.lower())
            or health.get(deployment_name)
            or {}
        ).get("status")
        or "unknown"
    )
    provider_status = cast(
        'Literal["healthy", "degraded", "unavailable", "unknown"]',
        raw_provider_status if raw_provider_status in {"healthy", "degraded", "unavailable"} else "unknown",
    )
    model_status = str(entry.get("status") or "unknown")
    compatibility = str(entry.get("compatibility") or "unknown")
    capabilities = [str(item) for item in entry.get("capabilities") or []]
    context_window = entry.get("context_window")
    try:
        context_window = int(context_window) if context_window is not None else None
    except (TypeError, ValueError):
        context_window = None

    availability: Literal["online", "offline", "unknown", "incompatible"]
    if provider_status == "unavailable":
        availability = "offline"
        dispatchable = False
        reason = "Proveedor no disponible en este momento."
    elif compatibility == "incompatible":
        availability = "incompatible"
        dispatchable = False
        reason = entry.get("compatibility_error") or "Modelo marcado como incompatible con el endpoint de inferencia."
    elif compatibility == "compatible":
        available_capabilities = {item.lower() for item in capabilities}
        availability = "online"
        dispatchable = bool({"completion", "embedding"}.intersection(available_capabilities))
        reason = (
            "Modelo compatible y proveedor disponible."
            if dispatchable
            else "Modelo disponible, pero no declara una capacidad ejecutable por el Broker."
        )
    elif model_status in {"available", "online"} and provider_status in {"healthy", "degraded"}:
        availability = "unknown"
        dispatchable = False
        reason = "Proveedor disponible, pero compatibilidad del modelo no comprobada."
    else:
        availability = "unknown"
        dispatchable = False
        reason = "No hay suficiente informacion para confirmar disponibilidad operativa."

    return ModelAvailabilityItem(
        provider=provider_name,
        deployment=deployment_name,
        model=str(entry.get("name") or entry.get("model") or "unknown"),
        availability=availability,
        dispatchable=dispatchable,
        reason=str(reason),
        provider_status=provider_status,
        model_status=model_status,
        compatibility=compatibility,
        capabilities=capabilities,
        context_window=context_window,
        compatibility_error=entry.get("compatibility_error"),
    )


def model_feature_profile(entry: dict[str, Any]) -> dict[str, Any]:
    raw_capabilities = {str(item).lower() for item in entry.get("capabilities") or []}
    model_name = str(entry.get("name") or "").lower()
    provider_name = str(entry.get("provider") or "").lower()
    compatibility = str(entry.get("compatibility") or "unknown").lower()
    notes: list[str] = []

    def status(*names: str, default: str = "unknown") -> str:
        return "supported" if raw_capabilities.intersection(names) else default

    def name_hints(*hints: str) -> bool:
        return any(item in model_name for item in hints)

    features: dict[str, dict[str, str]] = {
        "modalities": {
            "text_input": status("completion", "chat", "text", default="supported"),
            "text_output": status("completion", "chat", "text", default="supported"),
            "image_input": status("vision", "image", "multimodal", "visual"),
            "image_output": status("image_generation", "image-output", "text-to-image"),
            "audio_input": status("audio", "speech", "transcription", "asr"),
            "audio_output": status("tts", "speech_output", "audio-output"),
            "video_input": status("video", "video_input"),
            "video_output": status("video_generation", "video-output", "text-to-video"),
            "embedding_output": status("embedding", "embeddings"),
            "multimodal_input": status("multimodal", "omni"),
        },
        "files": {
            "file_upload": status("file", "files", "document", "pdf", "attachment"),
            "pdf_input": status("pdf", "document"),
            "document_input": status("document", "file", "files"),
            "spreadsheet_input": status("spreadsheet", "csv", "xlsx"),
            "presentation_input": status("ppt", "pptx", "presentation"),
            "archive_input": status("zip", "archive"),
            "image_file_input": status("image", "vision", "multimodal"),
            "audio_file_input": status("audio", "speech", "asr"),
            "video_file_input": status("video"),
        },
        "tools": {
            "function_calling": status("tools", "tool_calling", "function_calling", "functions"),
            "parallel_tool_calls": status("parallel_tool_calls"),
            "tool_choice": status("tool_choice", "tools"),
            "web_search": status("web_search", "search", "browser"),
            "deep_research": status("deep_research", "deep_search", "research"),
            "code_execution": status("code", "code_execution", "python"),
            "retrieval": status("retrieval", "rag", "vector_search"),
            "computer_use": status("computer_use", "desktop", "browser_control"),
            "mcp_tools": status("mcp", "tools"),
        },
        "understanding": {
            "ocr": status("ocr", "vision", "image", "multimodal"),
            "chart_understanding": status("chart", "vision", "image", "multimodal"),
            "table_understanding": status("table", "spreadsheet", "document"),
            "diagram_understanding": status("diagram", "vision", "image", "multimodal"),
            "math": status("math", "reasoning"),
            "coding": status("code", "coding", "programming"),
            "scientific_reasoning": status("science", "reasoning"),
            "legal_reasoning": status("legal"),
            "medical_reasoning": status("medical"),
            "financial_reasoning": status("finance", "financial"),
            "multilingual": status("multilingual", "translation"),
            "translation": status("translation", "multilingual"),
        },
        "reasoning": {
            "reasoning_optimized": status("reasoning", "thinking"),
            "chain_of_thought_private": status("reasoning", "thinking"),
            "planning": status("planning", "agent", "agentic"),
            "self_reflection": status("reflection", "critique", "reasoning"),
            "agentic": status("agent", "agentic", "tool_calling", "tools"),
            "mixture_compatible": "supported" if compatibility == "compatible" else "unsupported" if compatibility == "incompatible" else "unknown",
        },
        "generation": {
            "chat_completions": "supported" if compatibility == "compatible" else "unsupported" if compatibility == "incompatible" else status("completion", "chat"),
            "json_mode": status("json", "structured_output", "response_format"),
            "json_schema": status("json_schema", "structured_output"),
            "structured_outputs": status("structured_output", "json_schema"),
            "streaming": status("streaming", "stream"),
            "text_classification": status("classification"),
            "summarization": status("summarization", "summary", "completion"),
            "reranking": status("rerank", "reranking"),
            "moderation": status("moderation", "safety"),
        },
        "memory_and_state": {
            "conversation_state": status("stateful", "conversation_state"),
            "long_term_memory": status("memory", "long_term_memory"),
            "prompt_caching": status("prompt_cache", "caching", "cache"),
        },
        "operations": {
            "batch_inference": status("batch", "batch_inference"),
            "fine_tuning": status("fine_tuning", "finetune"),
            "distillation": status("distillation"),
            "quantized": status("quantized", "quantization"),
            "deterministic_seed": status("seed", "deterministic"),
            "logprobs": status("logprobs"),
            "token_counting": status("token_counting", "tokenizer"),
        },
        "deployment": {
            "local_execution": "supported" if str(entry.get("deployment") or "").lower() == "local" else "unsupported",
            "cloud_execution": "supported" if str(entry.get("deployment") or "").lower() in {"cloud", "api"} else "unsupported",
            "offline_capable": "supported" if str(entry.get("deployment") or "").lower() == "local" else "unknown",
            "privacy_boundary_local": "supported" if str(entry.get("deployment") or "").lower() == "local" else "unsupported",
        },
        "safety": {
            "safety_tuned": status("safety", "moderation", "guardrails"),
            "policy_guardrails": status("guardrails", "moderation", "safety"),
            "citation_grounding": status("citations", "grounding"),
        },
        "broker_support": {
            "single_prompt": "supported" if "completion" in raw_capabilities or compatibility == "compatible" else "unknown",
            "mixture_proposer": "supported" if compatibility == "compatible" else "unsupported" if compatibility == "incompatible" else "unknown",
            "mixture_arbiter": "supported" if compatibility == "compatible" else "unsupported" if compatibility == "incompatible" else "unknown",
            "embedding_task": "supported" if "embedding" in raw_capabilities else "unsupported",
        },
    }

    if name_hints("vision", "vl", "visual", "multimodal", "omni", "phi-4-multimodal", "fuyu", "kosmos"):
        features["modalities"]["image_input"] = "supported"
        features["modalities"]["multimodal_input"] = "supported"
        features["files"]["image_file_input"] = "supported"
        features["understanding"]["ocr"] = "supported"
        features["understanding"]["chart_understanding"] = "supported"
        features["understanding"]["diagram_understanding"] = "supported"
        notes.append("image_input inferido por el nombre del modelo.")
    if name_hints("audio", "speech", "whisper", "asr", "tts"):
        features["modalities"]["audio_input"] = "supported"
        features["files"]["audio_file_input"] = "supported"
        notes.append("audio_input inferido por el nombre del modelo.")
    if name_hints("video"):
        features["modalities"]["video_input"] = "supported"
        features["files"]["video_file_input"] = "supported"
        notes.append("video_input inferido por el nombre del modelo.")
    if name_hints("embed", "embedding", "bge", "e5"):
        features["modalities"]["embedding_output"] = "supported"
        notes.append("embedding_output inferido por el nombre del modelo.")
    if name_hints("reason", "r1", "qwq", "thinking"):
        features["reasoning"]["reasoning_optimized"] = "supported"
        features["reasoning"]["chain_of_thought_private"] = "supported"
        notes.append("reasoning_optimized inferido por el nombre del modelo.")
    if name_hints("coder", "code", "starcoder", "codestral", "deepseek-coder", "devstral"):
        features["understanding"]["coding"] = "supported"
        notes.append("coding inferido por el nombre del modelo.")
    if name_hints("math", "qwq", "qwen"):
        features["understanding"]["math"] = "supported"
        notes.append("math inferido por el nombre del modelo.")
    if name_hints("translate", "nllb", "seamless", "multilingual", "aya", "sea-lion"):
        features["understanding"]["translation"] = "supported"
        features["understanding"]["multilingual"] = "supported"
        notes.append("multilingual inferido por el nombre del modelo.")
    if name_hints("guard", "safety", "moderation", "shield"):
        features["safety"]["safety_tuned"] = "supported"
        features["safety"]["policy_guardrails"] = "supported"
        notes.append("safety_tuned inferido por el nombre del modelo.")
    if name_hints("rerank", "reranker"):
        features["generation"]["reranking"] = "supported"
        notes.append("reranking inferido por el nombre del modelo.")
    if provider_name in {"ollama", "deepseek"} or compatibility in {"compatible", "incompatible"}:
        notes.append("Las capacidades no declaradas por el proveedor se devuelven como unknown.")

    return {"features": features, "feature_notes": notes}
