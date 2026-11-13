"""Mapping of a user's LLM profile to the environment Strix expects.

Strix reads STRIX_LLM (a LiteLLM model id), LLM_API_KEY and, optionally,
LLM_API_BASE. The scan engine (Phase 2) calls `build_strix_env` right before
launching a run, so decrypted keys only ever live in the worker's process env.
"""

from app.core.crypto import decrypt
from app.models.llm_profile import LLMProfile, Provider

# LiteLLM provider prefixes. "custom" means any OpenAI-compatible endpoint,
# which LiteLLM addresses through the "openai/" prefix plus LLM_API_BASE.
_PREFIX = {
    Provider.claude: "anthropic",
    Provider.openai: "openai",
    Provider.grok: "xai",
    Provider.custom: "openai",
}


def strix_model_id(provider: Provider, model: str) -> str:
    prefix = _PREFIX[provider]
    if model.startswith(prefix + "/"):
        return model  # already fully qualified
    return f"{prefix}/{model}"


def key_hint(api_key: str) -> str:
    return "…" + api_key[-4:] if len(api_key) >= 12 else "•••"


def build_strix_env(profile: LLMProfile) -> dict[str, str]:
    env = {
        "STRIX_LLM": strix_model_id(profile.provider, profile.model),
        "LLM_API_KEY": decrypt(profile.api_key_enc),
    }
    if profile.base_url:
        env["LLM_API_BASE"] = profile.base_url
    return env


PRESETS = [
    {
        "provider": Provider.claude,
        "label": "Claude",
        "hint": "Anthropic API",
        "model_placeholder": "claude-sonnet-4-6",
        "suggestions": ["claude-sonnet-4-6", "claude-opus-5-5"],
        "base_url_required": False,
    },
    {
        "provider": Provider.openai,
        "label": "OpenAI",
        "hint": "OpenAI API",
        "model_placeholder": "gpt-5.4",
        "suggestions": ["gpt-5.4"],
        "base_url_required": False,
    },
    {
        "provider": Provider.grok,
        "label": "Grok",
        "hint": "xAI API",
        "model_placeholder": "grok-4",
        "suggestions": [],
        "base_url_required": False,
    },
    {
        "provider": Provider.custom,
        "label": "Custom",
        "hint": "Любой OpenAI-совместимый endpoint (Ollama, LM Studio, vLLM, OpenRouter…)",
        "model_placeholder": "llama3.1:70b",
        "suggestions": [],
        "base_url_required": True,
    },
]
