from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.api.deps import require_operator
from app.core.crypto import decrypt, encrypt
from app.core.db import get_db
from app.models.llm_profile import LLMProfile, Provider
from app.models.user import User
from app.schemas.llm import (
    LLMProfileCreate,
    LLMProfileOut,
    LLMProfileUpdate,
    ModelListRequest,
    ModelListResponse,
    ProviderPreset,
)
from app.services.llm import PRESETS, ModelListError, fetch_provider_models, key_hint

router = APIRouter(prefix="/api/llm-profiles", tags=["llm-profiles"])

# Keys are personal: every query below is scoped to the requesting user, and a
# profile belonging to someone else is indistinguishable from a missing one.


def _own_or_404(db: Session, user: User, profile_id: int) -> LLMProfile:
    profile = db.scalar(
        select(LLMProfile).where(LLMProfile.id == profile_id, LLMProfile.user_id == user.id)
    )
    if profile is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Профиль не найден")
    return profile


def _require_base_url_for_custom(provider: Provider, base_url: str | None) -> None:
    if provider == Provider.custom and not base_url:
        raise HTTPException(
            422,
            "Для custom-провайдера нужен base_url (OpenAI-совместимый endpoint)",
        )


@router.get("/presets", response_model=list[ProviderPreset])
def presets(_: User = Depends(require_operator)) -> list[dict]:
    return PRESETS


@router.post("/models", response_model=ModelListResponse)
def fetch_models(
    body: ModelListRequest,
    user: User = Depends(require_operator),
    db: Session = Depends(get_db),
) -> ModelListResponse:
    """«Загрузить модели»: ask the provider itself what it serves.

    Used both while creating a profile (a freshly typed, not-yet-saved key) and while
    editing one (`profile_id`, so the stored key need not be retyped just to refresh the
    list) — `api_key`/`base_url` in the body win over the profile's stored values so an
    edited-but-unsaved endpoint can be probed too.
    """
    api_key = body.api_key
    base_url = body.base_url
    if body.profile_id is not None:
        profile = _own_or_404(db, user, body.profile_id)
        if profile.provider != body.provider:
            raise HTTPException(422, "profile_id относится к другому провайдеру")
        if api_key is None:
            api_key = decrypt(profile.api_key_enc)
        if base_url is None:
            base_url = profile.base_url
    if not api_key:
        raise HTTPException(422, "Нужен api_key (свой или уже сохранённого профиля через profile_id)")
    _require_base_url_for_custom(body.provider, base_url)
    try:
        models = fetch_provider_models(body.provider, base_url, api_key)
    except ModelListError as exc:
        raise HTTPException(status.HTTP_502_BAD_GATEWAY, str(exc)) from exc
    return ModelListResponse(models=models)


@router.get("", response_model=list[LLMProfileOut])
def list_profiles(
    user: User = Depends(require_operator), db: Session = Depends(get_db)
) -> list[LLMProfile]:
    return list(
        db.scalars(
            select(LLMProfile).where(LLMProfile.user_id == user.id).order_by(LLMProfile.id)
        )
    )


@router.post("", response_model=LLMProfileOut, status_code=status.HTTP_201_CREATED)
def create_profile(
    body: LLMProfileCreate,
    user: User = Depends(require_operator),
    db: Session = Depends(get_db),
) -> LLMProfile:
    _require_base_url_for_custom(body.provider, body.base_url)
    profile = LLMProfile(
        user_id=user.id,
        name=body.name,
        provider=body.provider,
        model=body.model,
        base_url=body.base_url,
        api_key_enc=encrypt(body.api_key),
        api_key_hint=key_hint(body.api_key),
    )
    db.add(profile)
    try:
        db.commit()
    except IntegrityError:
        db.rollback()
        raise HTTPException(status.HTTP_409_CONFLICT, "Профиль с таким именем уже существует")
    db.refresh(profile)
    return profile


@router.patch("/{profile_id}", response_model=LLMProfileOut)
def update_profile(
    profile_id: int,
    body: LLMProfileUpdate,
    user: User = Depends(require_operator),
    db: Session = Depends(get_db),
) -> LLMProfile:
    profile = _own_or_404(db, user, profile_id)
    sent = body.model_fields_set

    if body.name is not None:
        profile.name = body.name
    if body.provider is not None:
        profile.provider = body.provider
    if body.model is not None:
        profile.model = body.model
    if "base_url" in sent:  # explicit null/empty clears it
        profile.base_url = body.base_url
    if body.api_key is not None and body.api_key.strip():
        key = body.api_key.strip()
        profile.api_key_enc = encrypt(key)
        profile.api_key_hint = key_hint(key)

    _require_base_url_for_custom(profile.provider, profile.base_url)
    try:
        db.commit()
    except IntegrityError:
        db.rollback()
        raise HTTPException(status.HTTP_409_CONFLICT, "Профиль с таким именем уже существует")
    db.refresh(profile)
    return profile


@router.delete("/{profile_id}", status_code=status.HTTP_204_NO_CONTENT)
def delete_profile(
    profile_id: int, user: User = Depends(require_operator), db: Session = Depends(get_db)
) -> None:
    db.delete(_own_or_404(db, user, profile_id))
    db.commit()
