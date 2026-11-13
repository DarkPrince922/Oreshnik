# Oreshnik

Web control-plane around the open-source [Strix](https://github.com/usestrix/strix)
AI pentesting CLI. Launch scans from a browser, watch them live, store findings,
read branded reports.

> ⚠️ For **authorized** security testing only — scan systems you own or have
> explicit written permission to test.

## What it is

Strix has no programmatic API in its open-source form: the integration path is the
headless CLI (`strix -n ...`) plus the artifacts it writes to disk. Oreshnik is an
orchestrator around that: it runs the CLI, streams its output, parses its artifacts,
and presents everything in a multi-user web panel.

```
Browser (React/Vite) ──REST+WS──> FastAPI ──> Celery worker ──> strix -n ... (Docker)
                                      │                              │
                                   Postgres   <── artifacts ── strix_runs/<id>/
                                   Redis (queue + live-log pub/sub)
```

## Stack

FastAPI · Postgres · Celery + Redis (queue + beat scheduler) · React/Vite/TS/Tailwind.

## Status

Early development. See [`docs/ARCHITECTURE.md`](docs/ARCHITECTURE.md) for the full
design and the phased build plan.

## Quick start (dev)

```bash
cp .env.example .env         # then edit secrets
docker compose up --build
# frontend: http://localhost:5173
# api:      http://localhost:8000/api/health
```

The default runner is `mock` — it simulates Strix runs without calling any LLM, so you
can build and test the whole pipeline without burning tokens. Switch a runner to
`local` to execute real scans.
