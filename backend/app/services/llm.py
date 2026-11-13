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


def strix_api_type(profile: LLMProfile) -> str | None:
    """Which OpenAI API route Strix should use for this profile (`STRIX_API_TYPE`), or None to leave it to Strix.

    Strix's agent loop picks Responses or chat completions from its LiteLLM catalog by model name. On the Responses route it
    sends native "custom" tools (its file tools), which a self-hosted or third-party OpenAI-compatible gateway usually
    does not implement ("This endpoint supports function tools only", HTTP 400). A `custom` profile is by definition such
    a gateway, so it gets chat completions, where Strix turns every tool into a plain function. The admin setting
    `STRIX_API_TYPE` overrides this for all profiles.
    """
    from app.core.config import settings

    if settings.strix_api_type != "auto":
        return settings.strix_api_type
    return "chat_completions" if profile.provider == Provider.custom else None


MAX_SAVED_MODELS = 1000


def profile_models(profile: LLMProfile) -> list[str]:
    """Every model a scan may pick on this profile: its own default first, then the saved list; no repeats."""
    seen: set[str] = set()
    out: list[str] = []
    for name in [profile.model, *(profile.models or [])]:
        if isinstance(name, str) and name and name not in seen:
            seen.add(name)
            out.append(name)
    return out


def clean_model_list(names: list[str] | None) -> list[str]:
    """A model list as it is stored: trimmed, without blanks or repeats, each id and the whole list bounded."""
    out: list[str] = []
    seen: set[str] = set()
    for name in names or []:
        name = name.strip() if isinstance(name, str) else ""
        if name and len(name) <= 200 and name not in seen:
            seen.add(name)
            out.append(name)
        if len(out) >= MAX_SAVED_MODELS:
            break
    return out


def build_strix_env(profile: LLMProfile, model: str | None = None) -> dict[str, str]:
    """The environment Strix needs for `profile`; `model` (one of the profile's models) replaces the profile's default."""
    env = {
        "STRIX_LLM": strix_model_id(profile.provider, model or profile.model),
        "LLM_API_KEY": decrypt(profile.api_key_enc),
    }
    if profile.base_url:
        env["LLM_API_BASE"] = profile.base_url
    api_type = strix_api_type(profile)
    if api_type:
        env["STRIX_API_TYPE"] = api_type
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


class _TooLarge(Exception):
    """Internal signal from `_bounded_http`: the response exceeded its cap. Never escapes this module."""


def _bounded_http(
    method: str,
    url: str,
    headers: dict[str, str],
    *,
    json_body: object | None = None,
    timeout: float,
    max_bytes: int,
    transport: httpx.BaseTransport | None = None,
) -> tuple[int, bytes]:
    """One outbound call, bounded in time and in response size (enforced while streaming, not after buffering).

    Raises `httpx` exceptions verbatim and `_TooLarge` on an oversized response; every caller below turns those
    into its own user-facing error type, so this helper carries no wording of its own.
    """
    with httpx.Client(timeout=timeout, follow_redirects=False, transport=transport) as client, client.stream(
        method, url, headers=headers, json=json_body
    ) as resp:
        body = bytearray()
        for chunk in resp.iter_bytes():
            body += chunk
            if len(body) > max_bytes:
                raise _TooLarge
        return resp.status_code, bytes(body)


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
        status, raw = _bounded_http(
            "GET", url, headers, timeout=_MODELS_TIMEOUT_SECONDS, max_bytes=_MODELS_MAX_BYTES, transport=transport
        )
    except _TooLarge:
        raise ModelListError("Ответ провайдера слишком большой") from None
    except httpx.TimeoutException as exc:
        raise ModelListError("Провайдер не ответил за отведённое время") from exc
    except httpx.HTTPError as exc:
        log.warning("model list fetch failed for %s: %s", provider, exc)
        raise ModelListError("Не удалось подключиться к провайдеру") from exc

    if status in (401, 403):
        raise ModelListError("Провайдер отверг ключ (401/403)")
    if status >= 400:
        raise ModelListError(f"Провайдер вернул ошибку {status}")
    try:
        payload = json.loads(raw)
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


# ── one-shot chat completion ("операции": turning a goal into a script) ───
#
# This hits the provider's real, native chat endpoint directly — never through Strix/LiteLLM — so `model` is the
# provider's own id, exactly as stored on the profile: no litellm routing prefix is added or expected here (verified
# against the real strix-agent 1.7.0 earlier: that prefix never reaches the wire either, see strix_model_id's docstring).

_CHAT_TIMEOUT_SECONDS = 120.0
_CHAT_MAX_BYTES = 500_000  # a generated script plus the response envelope; anything past this is not one

_CHAT_URL = {
    Provider.claude: "https://api.anthropic.com/v1/messages",
    Provider.openai: "https://api.openai.com/v1/chat/completions",
    Provider.grok: "https://api.x.ai/v1/chat/completions",
}


class LlmCallError(Exception):
    """A human-readable reason a one-shot LLM call produced no answer."""


def chat_once(
    provider: Provider,
    base_url: str | None,
    api_key: str,
    model: str,
    *,
    system: str,
    user: str,
    max_tokens: int = 4000,
    timeout: float = _CHAT_TIMEOUT_SECONDS,
    transport: httpx.BaseTransport | None = None,
) -> str:
    """One non-streaming request; returns the assistant's reply text (never empty — raises instead)."""
    if provider == Provider.claude:
        url = _CHAT_URL[provider]
        headers = {"x-api-key": api_key, "anthropic-version": "2023-06-01"}
        body: dict[str, object] = {
            "model": model,
            "system": system,
            "max_tokens": max_tokens,
            "messages": [{"role": "user", "content": user}],
        }
    else:
        if provider == Provider.custom:
            assert base_url, "custom requires base_url (enforced by the caller)"
            url = f"{base_url}/chat/completions"
        else:
            url = _CHAT_URL[provider]
        headers = {"Authorization": f"Bearer {api_key}"}
        body = {
            "model": model,
            "max_tokens": max_tokens,
            "messages": [{"role": "system", "content": system}, {"role": "user", "content": user}],
        }

    try:
        status, raw = _bounded_http(
            "POST", url, headers, json_body=body, timeout=timeout, max_bytes=_CHAT_MAX_BYTES, transport=transport
        )
    except _TooLarge:
        raise LlmCallError("Ответ модели слишком большой") from None
    except httpx.TimeoutException as exc:
        raise LlmCallError("Модель не ответила за отведённое время") from exc
    except httpx.HTTPError as exc:
        log.warning("chat_once failed for %s: %s", provider, exc)
        raise LlmCallError("Не удалось подключиться к провайдеру") from exc

    if status in (401, 403):
        raise LlmCallError("Провайдер отверг ключ (401/403)")
    if status >= 400:
        raise LlmCallError(f"Провайдер вернул ошибку {status}")
    try:
        payload = json.loads(raw)
    except ValueError as exc:
        raise LlmCallError("Провайдер вернул не JSON") from exc

    text = _extract_chat_text(provider, payload)
    if not text.strip():
        raise LlmCallError("Провайдер вернул пустой ответ")
    return text


def _extract_chat_text(provider: Provider, payload: object) -> str:
    if not isinstance(payload, dict):
        return ""
    if provider == Provider.claude:
        blocks = payload.get("content")
        if not isinstance(blocks, list):
            return ""
        return "".join(b.get("text", "") for b in blocks if isinstance(b, dict) and b.get("type") == "text")
    choices = payload.get("choices")
    if not isinstance(choices, list) or not choices or not isinstance(choices[0], dict):
        return ""
    message = choices[0].get("message")
    content = message.get("content") if isinstance(message, dict) else None
    return content if isinstance(content, str) else ""


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
