from datetime import datetime
from typing import Any

from pydantic import BaseModel


class SettingOut(BaseModel):
    key: str
    env: str                      # the variable of .env this parameter's default comes from
    label: str
    hint: str
    kind: str                     # int | float | bool | choice | text
    minimum: float | None
    maximum: float | None
    choices: list[str]
    unit: str
    restart: str                  # not empty: what has to be restarted before a change takes effect
    value: Any                    # what applies right now
    default: Any                  # `.env` / the built-in default: what "сбросить" returns to
    changed: bool                 # a value set in the panel is in force
    updated_at: datetime | None


class SettingsGroupOut(BaseModel):
    name: str
    items: list[SettingOut]


class SettingsOut(BaseModel):
    groups: list[SettingsGroupOut]


class SettingsUpdate(BaseModel):
    # key -> new value; null = reset that parameter to its default
    values: dict[str, Any]
