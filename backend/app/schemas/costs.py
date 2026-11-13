from datetime import date

from pydantic import BaseModel


class CostPeriods(BaseModel):
    today: float
    week: float  # the last 7 days, today included
    month: float  # the current calendar month so far
    window: float  # the requested window
    all_time: float


class RunningNow(BaseModel):
    count: int
    cost: float  # what the scans that are still running have cost so far


class CostDay(BaseModel):
    date: date
    cost: float
    scans: int


class CostUser(BaseModel):
    user_id: int
    email: str
    cost: float
    scans: int


class CostScan(BaseModel):
    id: int
    target: str
    owner_email: str
    status: str
    cost: float


class CostSummary(BaseModel):
    days: int
    periods: CostPeriods
    running: RunningNow
    tokens: int
    per_day: list[CostDay]
    per_user: list[CostUser]
    top_scans: list[CostScan]
