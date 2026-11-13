import httpx
import pytest

from app.core.db import SessionLocal
from app.models import LLMProfile
from app.services.llm import ModelListError, build_strix_env, fetch_provider_models, strix_model_id
from app.models import Provider

KEY = "sk-ant-secret-1234567890"


def create(client, h, **over):
    body = {"name": "main", "provider": "claude", "model": "claude-sonnet-4-6", "api_key": KEY}
    body.update(over)
    return client.post("/api/llm-profiles", headers=h, json=body)


def test_api_key_is_never_returned_and_stored_encrypted(client, operator):
    r = create(client, operator)
    assert r.status_code == 201
    body = r.json()
    assert KEY not in r.text and "api_key" not in body
    assert body["api_key_hint"] == "…7890" and body["strix_model"] == "anthropic/claude-sonnet-4-6"
    assert KEY not in client.get("/api/llm-profiles", headers=operator).text
    with SessionLocal() as db:
        stored = db.query(LLMProfile).one()
        assert KEY not in stored.api_key_enc and stored.api_key_enc.startswith("gAAAA")
        assert build_strix_env(stored) == {"STRIX_LLM": "anthropic/claude-sonnet-4-6", "LLM_API_KEY": KEY}


def test_profiles_are_private_to_their_owner(client, admin, operator):
    pid = create(client, operator).json()["id"]
    assert client.get("/api/llm-profiles", headers=admin).json() == []  # even admins don't see others' keys
    assert client.patch(f"/api/llm-profiles/{pid}", headers=admin, json={"name": "x"}).status_code == 404
    assert client.delete(f"/api/llm-profiles/{pid}", headers=admin).status_code == 404
    assert len(client.get("/api/llm-profiles", headers=operator).json()) == 1


def test_viewer_cannot_use_profiles(client, viewer):
    assert client.get("/api/llm-profiles", headers=viewer).status_code == 403
    assert create(client, viewer).status_code == 403
    assert client.get("/api/llm-profiles").status_code == 401


def test_custom_provider_requires_base_url_and_maps_to_openai_prefix(client, operator):
    assert create(client, operator, provider="custom", model="llama3.1:70b").status_code == 422
    r = create(client, operator, provider="custom", model="llama3.1:70b", base_url="http://ollama:11434/v1/")
    assert r.status_code == 201
    assert r.json()["base_url"] == "http://ollama:11434/v1"  # trailing slash trimmed
    assert r.json()["strix_model"] == "openai/llama3.1:70b"
    with SessionLocal() as db:
        env = build_strix_env(db.query(LLMProfile).one())
    assert env["LLM_API_BASE"] == "http://ollama:11434/v1"


def test_base_url_must_be_http(client, operator):
    assert create(client, operator, base_url="file:///etc/passwd").status_code == 422
    assert create(client, operator, base_url="ftp://x").status_code == 422


def test_duplicate_names_conflict_per_user_only(client, admin, operator):
    assert create(client, operator).status_code == 201
    assert create(client, operator).status_code == 409
    assert create(client, admin).status_code == 201  # same name, different user — fine


def test_update_keeps_key_unless_replaced_and_can_clear_base_url(client, operator):
    pid = create(client, operator, base_url="https://proxy.example.com").json()["id"]
    r = client.patch(f"/api/llm-profiles/{pid}", headers=operator, json={"model": "claude-opus-5-5"})
    assert r.status_code == 200 and r.json()["api_key_hint"] == "…7890"
    assert r.json()["base_url"] == "https://proxy.example.com"  # untouched when not sent
    r = client.patch(f"/api/llm-profiles/{pid}", headers=operator, json={"base_url": None, "api_key": ""})
    assert r.json()["base_url"] is None and r.json()["api_key_hint"] == "…7890"  # blank key = keep
    r = client.patch(f"/api/llm-profiles/{pid}", headers=operator, json={"api_key": "sk-new-key-abcdefgh9999"})
    assert r.json()["api_key_hint"] == "…9999"
    with SessionLocal() as db:
        assert build_strix_env(db.query(LLMProfile).one())["LLM_API_KEY"] == "sk-new-key-abcdefgh9999"


def test_switching_to_custom_without_base_url_is_rejected(client, operator):
    pid = create(client, operator).json()["id"]
    assert client.patch(f"/api/llm-profiles/{pid}", headers=operator, json={"provider": "custom"}).status_code == 422


def test_delete(client, operator):
    pid = create(client, operator).json()["id"]
    assert client.delete(f"/api/llm-profiles/{pid}", headers=operator).status_code == 204
    assert client.get("/api/llm-profiles", headers=operator).json() == []


def test_presets(client, operator):
    presets = client.get("/api/llm-profiles/presets", headers=operator).json()
    assert [p["provider"] for p in presets] == ["claude", "openai", "grok", "custom"]
    assert next(p for p in presets if p["provider"] == "custom")["base_url_required"] is True


def test_strix_model_id_does_not_double_prefix():
    assert strix_model_id(Provider.claude, "anthropic/claude-x") == "anthropic/claude-x"
    assert strix_model_id(Provider.grok, "grok-4") == "xai/grok-4"
    assert strix_model_id(Provider.openai, "gpt-5.4") == "openai/gpt-5.4"


def test_custom_provider_model_id_is_passed_through_verbatim():
    """For "custom", "openai/" is never a provider hint, only Strix's own routing tag —
    it strips exactly one leading "openai/" and forwards the rest untouched. So whatever the
    admin typed, prefixed once, must come back out unprefixed (and otherwise unchanged) once
    Strix's own stripping runs — including a model id that happens to contain slashes of its
    own, or even one that literally starts with "openai/"."""

    def strips_one_leading_openai_layer(strix_model: str) -> str:
        assert strix_model.startswith("openai/")
        return strix_model[len("openai/") :]

    for typed in ("modelid", "customproviders/modelid", "openai/gpt-oss-20b", "a/b/c/d"):
        assert strips_one_leading_openai_layer(strix_model_id(Provider.custom, typed)) == typed


# ── fetching the provider's own model list ───────────────────────────────

def mock_transport(status_code: int = 200, json_body=None, raw: bytes | None = None) -> httpx.MockTransport:
    def handler(request: httpx.Request) -> httpx.Response:
        if raw is not None:
            return httpx.Response(status_code, content=raw)
        return httpx.Response(status_code, json=json_body)

    return httpx.MockTransport(handler)


def test_fetch_provider_models_parses_openai_style_data_list():
    models = fetch_provider_models(
        Provider.openai,
        None,
        "sk-x",
        transport=mock_transport(json_body={"data": [{"id": "gpt-5.4"}, {"id": "gpt-5.4-mini"}]}),
    )
    assert models == ["gpt-5.4", "gpt-5.4-mini"]


def test_fetch_provider_models_accepts_a_bare_list_of_strings():
    models = fetch_provider_models(
        Provider.custom, "http://ollama:11434/v1", "k", transport=mock_transport(json_body=["a", "b"])
    )
    assert models == ["a", "b"]


def test_fetch_provider_models_accepts_a_models_key():
    models = fetch_provider_models(
        Provider.custom,
        "http://host/v1",
        "k",
        transport=mock_transport(json_body={"models": [{"name": "llama3.1:70b"}]}),
    )
    assert models == ["llama3.1:70b"]


def test_fetch_provider_models_rejects_401():
    with pytest.raises(ModelListError, match="ключ"):
        fetch_provider_models(Provider.openai, None, "bad", transport=mock_transport(status_code=401))


def test_fetch_provider_models_rejects_other_http_errors():
    with pytest.raises(ModelListError, match="500"):
        fetch_provider_models(Provider.openai, None, "k", transport=mock_transport(status_code=500))


def test_fetch_provider_models_rejects_non_json():
    with pytest.raises(ModelListError, match="JSON"):
        fetch_provider_models(Provider.openai, None, "k", transport=mock_transport(raw=b"<html>not json</html>"))


def test_fetch_provider_models_rejects_an_oversized_response(monkeypatch):
    from app.services import llm as llm_service

    monkeypatch.setattr(llm_service, "_MODELS_MAX_BYTES", 10)
    huge = {"data": [{"id": "x" * 1000}]}
    with pytest.raises(ModelListError, match="большой"):
        fetch_provider_models(Provider.openai, None, "k", transport=mock_transport(json_body=huge))


def test_fetch_provider_models_rejects_an_empty_list():
    with pytest.raises(ModelListError, match="ни одной"):
        fetch_provider_models(Provider.openai, None, "k", transport=mock_transport(json_body={"data": []}))


def test_fetch_provider_models_uses_the_right_auth_header_per_provider():
    seen = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["url"] = str(request.url)
        seen["headers"] = dict(request.headers)
        return httpx.Response(200, json={"data": ["m"]})

    fetch_provider_models(Provider.claude, None, "ak", transport=httpx.MockTransport(handler))
    assert seen["url"] == "https://api.anthropic.com/v1/models"
    assert seen["headers"]["x-api-key"] == "ak" and "authorization" not in seen["headers"]

    fetch_provider_models(Provider.custom, "http://host/v1", "ck", transport=httpx.MockTransport(handler))
    assert seen["url"] == "http://host/v1/models"
    assert seen["headers"]["authorization"] == "Bearer ck"


# ── the "Загрузить модели" endpoint ───────────────────────────────────────

def test_fetch_models_endpoint_requires_operator(client, viewer):
    r = client.post("/api/llm-profiles/models", headers=viewer, json={"provider": "openai", "api_key": "k"})
    assert r.status_code == 403


def test_fetch_models_endpoint_needs_a_key_or_a_profile(client, operator):
    r = client.post("/api/llm-profiles/models", headers=operator, json={"provider": "openai"})
    assert r.status_code == 422


def test_fetch_models_endpoint_requires_base_url_for_custom(client, operator):
    r = client.post(
        "/api/llm-profiles/models", headers=operator, json={"provider": "custom", "api_key": "k"}
    )
    assert r.status_code == 422


def test_fetch_models_endpoint_proxies_the_provider(client, operator, monkeypatch):
    from app.api import llm_profiles

    monkeypatch.setattr(llm_profiles, "fetch_provider_models", lambda *a, **k: ["m1", "m2"])
    r = client.post(
        "/api/llm-profiles/models", headers=operator, json={"provider": "openai", "api_key": "sk-x"}
    )
    assert r.status_code == 200 and r.json() == {"models": ["m1", "m2"]}


def test_fetch_models_endpoint_surfaces_provider_errors_as_502(client, operator, monkeypatch):
    from app.api import llm_profiles

    def boom(*a, **k):
        raise llm_profiles.ModelListError("провайдер недоступен")

    monkeypatch.setattr(llm_profiles, "fetch_provider_models", boom)
    r = client.post(
        "/api/llm-profiles/models", headers=operator, json={"provider": "openai", "api_key": "sk-x"}
    )
    assert r.status_code == 502 and "недоступен" in r.json()["detail"]


def test_fetch_models_endpoint_reuses_a_saved_profiles_key_without_retyping_it(client, operator, monkeypatch):
    from app.api import llm_profiles

    pid = create(client, operator).json()["id"]  # provider=claude, key=KEY
    seen = {}

    def fake(provider, base_url, api_key, **_):
        seen.update(provider=provider, base_url=base_url, api_key=api_key)
        return ["claude-sonnet-4-6"]

    monkeypatch.setattr(llm_profiles, "fetch_provider_models", fake)
    r = client.post(
        "/api/llm-profiles/models", headers=operator, json={"provider": "claude", "profile_id": pid}
    )
    assert r.status_code == 200
    assert seen["api_key"] == KEY  # decrypted server-side; never retyped by the caller


def test_fetch_models_endpoint_rejects_someone_elses_profile_id(client, admin, operator):
    pid = create(client, operator).json()["id"]
    r = client.post(
        "/api/llm-profiles/models", headers=admin, json={"provider": "claude", "profile_id": pid}
    )
    assert r.status_code == 404


def test_fetch_models_endpoint_rejects_a_profile_id_of_a_different_provider(client, operator):
    pid = create(client, operator).json()["id"]  # provider=claude
    r = client.post(
        "/api/llm-profiles/models", headers=operator, json={"provider": "openai", "profile_id": pid}
    )
    assert r.status_code == 422
