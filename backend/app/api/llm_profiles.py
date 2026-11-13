from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.api.deps import require_operator
from app.core.crypto import encrypt
from app.core.db import get_db
from app.models.llm_profile import LLMProfile, Provider
from app.models.user import User
from app.schemas.llm import (
    LLMProfileCreate,
    LLMProfileOut,
    LLMProfileUpdate,
    ProviderPreset,
)
from app.services.llm import PRESETS, key_hint

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
