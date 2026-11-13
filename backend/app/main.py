from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from app import __version__
from app.api import health
from app.core.config import settings

app = FastAPI(
    title="Oreshnik API",
    version=__version__,
    description="Web control-plane around the Strix pentesting CLI.",
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=settings.cors_origins,
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

app.include_router(health.router)


@app.get("/")
def root() -> dict:
    return {"name": "Oreshnik", "version": __version__, "docs": "/docs"}
