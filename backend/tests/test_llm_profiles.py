from app.core.db import SessionLocal
from app.models import LLMProfile
from app.services.llm import build_strix_env, strix_model_id
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
