"""Proveedores de inferencia del Broker.

Paquete con un modulo por adapter; este __init__ reexporta la API publica para
mantener estables los imports `from app.providers import ...`.
"""

from app.providers.base import (
    MIN_BYTES_PER_TOKEN,
    PROBE_HARD_MAX_MODELS,
    ROLE_SYSTEM_PROMPTS,
    CredentialResolver,
    ModelOutput,
    ProviderError,
    context_fits_with_capped_output,
    enforce_context_limit,
    estimate_required_context,
    estimate_tokens_upper_bound,
    infer_openai_compatible_capabilities,
    provider_http_error_message,
    request_with_context_capped_output,
    role_system_prompt,
)
from app.providers.bootstrap import BootstrapModelProvider
from app.providers.deepseek import DeepSeekProvider
from app.providers.huggingface import HuggingFaceLocalProvider
from app.providers.ollama import OllamaLifecycleManager, OllamaProvider
from app.providers.openai_compatible import OpenAICompatibleProvider
from app.providers.routing import RoutedModelProvider, build_provider

__all__ = [
    "MIN_BYTES_PER_TOKEN",
    "PROBE_HARD_MAX_MODELS",
    "ROLE_SYSTEM_PROMPTS",
    "BootstrapModelProvider",
    "CredentialResolver",
    "DeepSeekProvider",
    "HuggingFaceLocalProvider",
    "ModelOutput",
    "OllamaLifecycleManager",
    "OllamaProvider",
    "OpenAICompatibleProvider",
    "ProviderError",
    "RoutedModelProvider",
    "build_provider",
    "context_fits_with_capped_output",
    "enforce_context_limit",
    "estimate_required_context",
    "estimate_tokens_upper_bound",
    "infer_openai_compatible_capabilities",
    "provider_http_error_message",
    "request_with_context_capped_output",
    "role_system_prompt",
]
