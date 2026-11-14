"""What is new for the user since they last looked: the lights on «Сканы» and «Агент» in the menu.

The browser remembers when each section was last opened and asks "what happened after that?". A scan counts when it has finished;
an agent session when its turn has ended, or — regardless of time — while the agent is waiting for the user's answer.
Only the user's OWN scans and sessions count (an admin does not get a light for everybody's scans).
"""

from datetime import datetime, timezone

from fastapi import APIRouter, Depends, Query
from pydantic import BaseModel
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.api.deps import get_current_user
from app.core.db import get_db
from app.models.agent import AgentEvent, AgentSession, AgentSessionStatus
from app.models.scan import FINAL_STATUSES, Scan, ScanStatus
from app.models.user import Role, User

router = APIRouter(prefix="/api", tags=["attention"])


class ScansAttention(BaseModel):
    finished: int = 0     # scans that reached a final state since `scans_since`
    failed: int = 0       # ... of them, failed
    findings: int = 0     # ... of them, ended with vulnerabilities found


class AgentAttention(BaseModel):
    finished: int = 0     # sessions whose turn ended since `agent_since` (and that are idle now)
    waiting: int = 0      # sessions where the agent is waiting for the user's answer right now


class Attention(BaseModel):
    now: datetime         # the server's clock: what the browser stores as "seen" (its own clock may be off)
    scans: ScansAttention
    agent: AgentAttention


def _utc(value: datetime | None) -> datetime | None:
    if value is None:
        return None
    return value if value.tzinfo else value.replace(tzinfo=timezone.utc)


def waiting_sessions(db: Session, user_id: int) -> int:
    """Running sessions of the user with a question of the agent that nobody has answered yet."""
    running = list(db.scalars(select(AgentSession.id).where(AgentSession.user_id == user_id, AgentSession.status == AgentSessionStatus.running)))
    if not running:
        return 0
    open_by_session: dict[int, set[str]] = {}
    rows = db.execute(
        select(AgentEvent.session_id, AgentEvent.kind, AgentEvent.payload)
        .where(AgentEvent.session_id.in_(running), AgentEvent.kind.in_(("approval.requested", "approval.resolved")))
        .order_by(AgentEvent.session_id, AgentEvent.seq)
    )
    for session_id, kind, payload in rows:
        request_id = str((payload or {}).get("request_id"))
        pending = open_by_session.setdefault(session_id, set())
        if kind == "approval.requested":
            pending.add(request_id)
        else:
            pending.discard(request_id)
    return sum(1 for pending in open_by_session.values() if pending)


@router.get("/attention", response_model=Attention)
def attention(
    scans_since: datetime | None = Query(default=None),
    agent_since: datetime | None = Query(default=None),
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> Attention:
    scans, agent = ScansAttention(), AgentAttention()
    since = _utc(scans_since)
    if since is not None:
        rows = db.execute(
            select(Scan.status, func.count())
            .where(Scan.user_id == user.id, Scan.status.in_(FINAL_STATUSES), Scan.finished_at > since)
            .group_by(Scan.status)
        ).all()
        counts = {status: n for status, n in rows}
        # a scan the user cancelled themselves is no news
        scans = ScansAttention(
            finished=sum(n for status, n in counts.items() if status != ScanStatus.cancelled),
            failed=counts.get(ScanStatus.failed, 0), findings=counts.get(ScanStatus.vulns_found, 0),
        )
    if user.role in (Role.admin, Role.operator):
        agent.waiting = waiting_sessions(db, user.id)
        since = _utc(agent_since)
        if since is not None:
            agent.finished = db.scalar(
                select(func.count(func.distinct(AgentEvent.session_id)))
                .join(AgentSession, AgentSession.id == AgentEvent.session_id)
                .where(
                    AgentSession.user_id == user.id, AgentSession.status == AgentSessionStatus.idle,
                    AgentEvent.kind.in_(("turn.completed", "turn.error")), AgentEvent.created_at > since,
                )
            ) or 0
    return Attention(now=datetime.now(timezone.utc), scans=scans, agent=agent)
