from datetime import datetime

from pydantic import BaseModel, ConfigDict, Field, computed_field, field_validator

from app.models.llm_profile import Provider
from app.services.llm import strix_model_id


def _clean_base_url(value: str | None) -> str | None:
    if value is None:
        return None
    value = value.strip()
    if not value:
        return None
    if not value.startswith(("http://", "https://")) or len(value) > 500:
        raise ValueError("base_url must be an http(s) URL")
    return value.rstrip("/")


class LLMProfileCreate(BaseModel):
    name: str = Field(min_length=1, max_length=100)
    provider: Provider
    model: str = Field(min_length=1, max_length=200)
    base_url: str | None = None
    api_key: str = Field(min_length=1, max_length=500)

    _url = field_validator("base_url")(_clean_base_url)

    @field_validator("name", "model", "api_key")
    @classmethod
    def _strip(cls, v: str) -> str:
        v = v.strip()
        if not v:
            raise ValueError("must not be blank")
        return v


class LLMProfileUpdate(BaseModel):
    """Partial update. `api_key` is only replaced when provided and non-empty;
    `base_url` is cleared when explicitly sent as null/empty."""

    name: str | None = Field(default=None, min_length=1, max_length=100)
    provider: Provider | None = None
    model: str | None = Field(default=None, min_length=1, max_length=200)
    base_url: str | None = None
    api_key: str | None = Field(default=None, max_length=500)

    _url = field_validator("base_url")(_clean_base_url)

    @field_validator("name", "model")
    @classmethod
    def _strip(cls, v: str | None) -> str | None:
        if v is None:
            return v
        v = v.strip()
        if not v:
            raise ValueError("must not be blank")
        return v


class LLMProfileOut(BaseModel):
    """The API key is never returned — only a short hint such as '…a1b2'."""

    model_config = ConfigDict(from_attributes=True)

    id: int
    name: str
    provider: Provider
    model: str
    base_url: str | None
    api_key_hint: str
    created_at: datetime
    updated_at: datetime

    @computed_field  # type: ignore[prop-decorator]
    @property
    def strix_model(self) -> str:
        return strix_model_id(self.provider, self.model)


class ProviderPreset(BaseModel):
    provider: Provider
    label: str
    hint: str
    model_placeholder: str
    suggestions: list[str]
    base_url_required: bool
