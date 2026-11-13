"""Mapping of a user's LLM profile to the environment Strix expects.

Strix reads STRIX_LLM (a LiteLLM model id), LLM_API_KEY and, optionally,
LLM_API_BASE. The scan engine (Phase 2) calls `build_strix_env` right before
launching a run, so decrypted keys only ever live in the worker's process env.
"""

import json
import logging

import httpx

from app.core.crypto import decrypt
from app.models.llm_profile import LLMProfile, Provider

log = logging.getLogger("oreshnik.llm")

# LiteLLM provider prefixes for the three fixed, real providers. Each of these
# routes through LiteLLM to that provider's own API — never through a
# caller-supplied base_url — so a model the admin already typed fully
# qualified ("anthropic/claude-x") must not be prefixed again.
_PREFIX = {
    Provider.claude: "anthropic",
    Provider.openai: "openai",
    Provider.grok: "xai",
}


def strix_model_id(provider: Provider, model: str) -> str:
    if provider == Provider.custom:
        # A custom endpoint's model id is whatever that endpoint calls it — it does not
        # have to look like "<something>/<id>", and when it does, that "/" is part of the
        # real id (e.g. "customprovider/modelid", or even a literal "openai/..." name),
        # never a hint to add or skip a prefix. Strix (via the OpenAI Agents SDK's
        # MultiProvider) always strips exactly the first "openai/" segment to pick the one
        # client that actually honours LLM_API_BASE; every other prefix is instead handed to
        # LiteLLM, which rejects any prefix it doesn't recognise as a real provider name. So
        # "openai/" is prepended here unconditionally (never skipped, however the model
        # already looks) — that single guaranteed strip is what makes the remainder Strix
        # forwards exactly byte-for-byte what was typed, whatever it contains.
        return f"openai/{model}"
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


# ── fetching the provider's own model list ("Загрузить модели") ───────────
#
# This is the panel's own outbound request, not Strix's: an operator/admin who is about to
# save a profile can list what the provider actually serves instead of guessing an id. For
# "custom" the target is the admin's own base_url — exactly the host their key already goes
# to on every real scan, so this adds no reach beyond what the profile already grants.

_MODELS_TIMEOUT_SECONDS = 10.0
_MODELS_MAX_BYTES = 2_000_000  # a model listing is a short JSON array; anything past this is not one

_LIST_URL = {
    Provider.claude: "https://api.anthropic.com/v1/models",
    Provider.openai: "https://api.openai.com/v1/models",
    Provider.grok: "https://api.x.ai/v1/models",
}


class ModelListError(Exception):
    """A human-readable reason the provider's model list could not be fetched."""


def _models_request(provider: Provider, base_url: str | None, api_key: str) -> tuple[str, dict[str, str]]:
    if provider == Provider.custom:
        assert base_url, "custom requires base_url (enforced by the caller)"
        return f"{base_url}/models", {"Authorization": f"Bearer {api_key}"}
    if provider == Provider.claude:
        return _LIST_URL[provider], {"x-api-key": api_key, "anthropic-version": "2023-06-01"}
    return _LIST_URL[provider], {"Authorization": f"Bearer {api_key}"}


def fetch_provider_models(
    provider: Provider,
    base_url: str | None,
    api_key: str,
    *,
    transport: httpx.BaseTransport | None = None,
) -> list[str]:
    """The model ids a provider's own `/models`-style endpoint reports, sorted.

    `transport` lets tests inject an `httpx.MockTransport` instead of reaching the network.
    """
    url, headers = _models_request(provider, base_url, api_key)
    try:
        with httpx.Client(
            timeout=_MODELS_TIMEOUT_SECONDS, follow_redirects=False, transport=transport
        ) as client, client.stream("GET", url, headers=headers) as resp:
            if resp.status_code in (401, 403):
                raise ModelListError("Провайдер отверг ключ (401/403)")
            if resp.status_code >= 400:
                raise ModelListError(f"Провайдер вернул ошибку {resp.status_code}")
            body = bytearray()
            for chunk in resp.iter_bytes():
                body += chunk
                if len(body) > _MODELS_MAX_BYTES:
                    raise ModelListError("Ответ провайдера слишком большой")
    except ModelListError:
        raise
    except httpx.TimeoutException as exc:
        raise ModelListError("Провайдер не ответил за отведённое время") from exc
    except httpx.HTTPError as exc:
        log.warning("model list fetch failed for %s: %s", provider, exc)
        raise ModelListError("Не удалось подключиться к провайдеру") from exc

    try:
        payload = json.loads(bytes(body))
    except ValueError as exc:
        raise ModelListError("Провайдер вернул не JSON") from exc

    ids = sorted(_extract_model_ids(payload))
    if not ids:
        raise ModelListError("Провайдер не вернул ни одной модели")
    return ids


def _extract_model_ids(payload: object) -> list[str]:
    items: list[object]
    if isinstance(payload, dict) and isinstance(payload.get("data"), list):
        items = payload["data"]
    elif isinstance(payload, dict) and isinstance(payload.get("models"), list):
        items = payload["models"]
    elif isinstance(payload, list):
        items = payload
    else:
        return []
    ids: list[str] = []
    for item in items:
        if isinstance(item, str):
            ids.append(item)
        elif isinstance(item, dict):
            value = item.get("id") or item.get("name")
            if isinstance(value, str):
                ids.append(value)
    return ids


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
