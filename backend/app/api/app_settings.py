"""«Настройки»: the operating parameters that used to live only in `.env`, editable by an administrator.

See `app/core/runtime_settings.py` for what is here, what is not, and how a change reaches every process.
"""

import json

from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.api.deps import require_admin
from app.core import runtime_settings as rs
from app.core.db import get_db
from app.models.app_setting import AppSetting
from app.models.user import User
from app.schemas.app_settings import SettingOut, SettingsGroupOut, SettingsOut, SettingsUpdate

router = APIRouter(prefix="/api/settings", tags=["settings"])


def _snapshot(db: Session) -> SettingsOut:
    stored = {row.key: row for row in db.scalars(select(AppSetting))}
    valid = rs.load_overrides(db)
    groups: dict[str, list[SettingOut]] = {name: [] for name in rs.GROUPS}
    for spec in rs.SPECS:
        changed = spec.key in valid
        groups[spec.group].append(
            SettingOut(
                key=spec.key, env=spec.env, label=spec.label, hint=spec.hint, kind=spec.kind, minimum=spec.minimum,
                maximum=spec.maximum, choices=list(spec.choices), unit=spec.unit, restart=spec.restart,
                value=valid.get(spec.key, rs.BASELINE[spec.key]), default=rs.BASELINE[spec.key], changed=changed,
                updated_at=stored[spec.key].updated_at if changed else None,
            )
        )
    return SettingsOut(groups=[SettingsGroupOut(name=name, items=items) for name, items in groups.items()])


@router.get("", response_model=SettingsOut)
def get_settings(_: User = Depends(require_admin), db: Session = Depends(get_db)) -> SettingsOut:
    return _snapshot(db)


@router.put("", response_model=SettingsOut)
def update_settings(body: SettingsUpdate, admin: User = Depends(require_admin), db: Session = Depends(get_db)) -> SettingsOut:
    """Set or reset several parameters at once. All or nothing: one invalid value changes nothing."""
    problems: list[str] = []
    parsed: dict[str, object] = {}
    for key, raw in body.values.items():
        spec = rs.BY_KEY.get(key)
        if spec is None:
            problems.append(f"{key}: такого параметра нет")
            continue
        if raw is None:
            parsed[key] = None
            continue
        try:
            parsed[key] = rs.parse(spec, raw)
        except ValueError as exc:
            problems.append(f"{spec.label}: {exc}")
    if problems:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, "; ".join(problems))

    for key, value in parsed.items():
        row = db.get(AppSetting, key)
        if value is None or value == rs.BASELINE[key]:
            # A value equal to the default is no change at all: keeping no row means a later change of `.env` still takes effect.
            if row is not None:
                db.delete(row)
        elif row is None:
            db.add(AppSetting(key=key, value=json.dumps(value), updated_by=admin.id))
        else:
            row.value = json.dumps(value)
            row.updated_by = admin.id
    db.commit()
    rs.apply(rs.load_overrides(db))  # this process at once; the others pick it up within seconds
    return _snapshot(db)
