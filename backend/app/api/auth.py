from functools import lru_cache

from fastapi import APIRouter, Depends, HTTPException, Request, status
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.api.deps import get_current_user
from app.core.db import get_db
from app.core.security import create_access_token, hash_password, verify_password
from app.models.user import User
from app.schemas.user import LoginIn, PasswordChange, TokenOut, UserOut
from app.services import throttle

router = APIRouter(prefix="/api/auth", tags=["auth"])


@lru_cache
def _dummy_hash() -> str:
    # Verified against when the email is unknown, so response time doesn't reveal
    # whether an account exists.
    return hash_password("oreshnik-dummy-password")


@router.post("/login", response_model=TokenOut)
def login(body: LoginIn, request: Request, db: Session = Depends(get_db)) -> TokenOut:
    ip = request.client.host if request.client else "unknown"
    if throttle.is_blocked(body.email, ip):
        raise HTTPException(
            status.HTTP_429_TOO_MANY_REQUESTS,
            "Слишком много неудачных попыток. Попробуйте позже.",
        )

    user = db.scalar(select(User).where(User.email == body.email))
    password_ok = verify_password(body.password, user.password_hash if user else _dummy_hash())
    if user is None or not password_ok or not user.is_active:
        throttle.record_failure(body.email, ip)
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "Неверный email или пароль")

    throttle.reset(body.email, ip)
    return TokenOut(access_token=create_access_token(str(user.id)), user=UserOut.model_validate(user))


@router.get("/me", response_model=UserOut)
def me(user: User = Depends(get_current_user)) -> User:
    return user


@router.post("/change-password", status_code=status.HTTP_204_NO_CONTENT)
def change_password(
    body: PasswordChange,
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> None:
    if not verify_password(body.current_password, user.password_hash):
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "Текущий пароль указан неверно")
    user.password_hash = hash_password(body.new_password)
    db.commit()
