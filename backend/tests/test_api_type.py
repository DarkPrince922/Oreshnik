"""STRIX_API_TYPE: which OpenAI API route Strix's agent loop takes. A `custom` gateway gets chat completions, because on the
Responses route Strix sends native "custom" tools and the gateway answers 400 "This endpoint supports function tools only"."""
import pytest
from pydantic import ValidationError

from app.core.config import Settings, settings
from app.engine import LocalRunner
from app.engine.ssh import build_script, remote_dir
from app.models import LLMProfile, Provider
from app.services.llm import build_strix_env
from tests.test_strix_privacy import spec
from app.core.crypto import encrypt


def profile(provider: Provider, base_url: str | None = None) -> LLMProfile:
    return LLMProfile(
        user_id=1, name="p", provider=provider, model="m", base_url=base_url, api_key_enc=encrypt("k" * 20), api_key_hint="…kkkk",
    )


def test_a_custom_profile_gets_chat_completions_by_default():
    env = build_strix_env(profile(Provider.custom, "http://gw.internal/v1"))
    assert env["STRIX_API_TYPE"] == "chat_completions"


@pytest.mark.parametrize("provider", [Provider.claude, Provider.openai, Provider.grok])
def test_real_providers_are_left_to_strix_by_default(provider):
    assert "STRIX_API_TYPE" not in build_strix_env(profile(provider))


@pytest.mark.parametrize("value", ["responses", "chat_completions"])
def test_the_admin_setting_applies_to_every_profile(monkeypatch, value):
    monkeypatch.setattr(settings, "strix_api_type", value)
    for provider in (Provider.claude, Provider.openai, Provider.grok, Provider.custom):
        assert build_strix_env(profile(provider, "http://x/v1" if provider == Provider.custom else None))["STRIX_API_TYPE"] == value


def test_the_default_setting_is_auto_and_values_are_validated_like_strixs_own():
    assert Settings(_env_file=None).strix_api_type == "auto"
    assert Settings(_env_file=None, strix_api_type=" Chat_Completions ").strix_api_type == "chat_completions"
    assert Settings(_env_file=None, strix_api_type="").strix_api_type == "auto"
    with pytest.raises(ValidationError, match="STRIX_API_TYPE"):
        Settings(_env_file=None, strix_api_type="chat")


def test_the_local_runner_passes_it_to_strix(tmp_path):
    env = build_strix_env(profile(Provider.custom, "http://gw.internal/v1"))
    assert LocalRunner(strix_bin="strix").build_env(spec(tmp_path, env=env))["STRIX_API_TYPE"] == "chat_completions"


def test_the_remote_script_exports_it(tmp_path):
    env = build_strix_env(profile(Provider.custom, "http://gw.internal/v1"))
    script = build_script(spec(tmp_path, env=env), ["strix", "-n"], remote_dir(1))
    assert "export STRIX_API_TYPE=chat_completions" in script.splitlines()
