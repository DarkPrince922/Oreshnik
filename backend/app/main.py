import logging
from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from app import __version__
from app.api import agent, auth, costs, health, llm_profiles, ops, runners, scans, schedules, users
from app.bootstrap import run_bootstrap
from app.core.config import settings
from app.core.db import SessionLocal

log = logging.getLogger("oreshnik")


def check_config() -> None:
    problems = settings.insecure_secrets()
    if not problems:
        return
    if settings.is_dev:
        log.warning("Using placeholder/weak secrets (dev only): %s", ", ".join(problems))
    else:
        raise RuntimeError(
            f"Refusing to start with placeholder/weak secrets in ORESHNIK_ENV="
            f"{settings.oreshnik_env}: {', '.join(problems)}"
        )


@asynccontextmanager
async def lifespan(_: FastAPI):
    check_config()
    try:
        with SessionLocal() as db:
            run_bootstrap(db)
    except Exception:
        # Typically: migrations have not run yet. Keep serving /api/health so the
        # problem is visible instead of crash-looping.
        log.exception("Bootstrap failed (have migrations been applied?)")
    yield


app = FastAPI(
    title="Oreshnik API",
    version=__version__,
    description="Web control-plane around the Strix pentesting CLI.",
    lifespan=lifespan,
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=settings.cors_origins,
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

app.include_router(health.router)
app.include_router(auth.router)
app.include_router(users.router)
app.include_router(llm_profiles.router)
app.include_router(runners.router)
app.include_router(scans.router)
app.include_router(schedules.router)
app.include_router(costs.router)
app.include_router(ops.router)
app.include_router(agent.router)


@app.get("/")
def root() -> dict:
    return {"name": "Oreshnik", "version": __version__, "docs": "/docs"}
