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

Early development — phases 0–5 done (skeleton, auth/roles, per-user LLM profiles, runners, scan engine, scan UI, live agent activity, continuing a scan with new instructions in the same scan, cron schedules, live scan cost and cost accounting, SSH runners (key or password login) with a pinned host key and one-click installation of Docker and Strix). Operating parameters (limits, timeouts, Strix/SSH/agent tuning) are edited in the panel under «Настройки» — no restart; `.env` holds the defaults. Also: «Агент» — a chat with the open-source Codex CLI in an isolated container, with a live feed of its steps (set `AGENT_TOKEN` to enable it). See [`docs/ARCHITECTURE.md`](docs/ARCHITECTURE.md) for the full
design and the phased build plan.

## Quick start (dev)

```bash
cp .env.example .env         # then edit secrets
docker compose up --build
# frontend: http://localhost:5173
# api:      http://localhost:8000/api/health  (docs: /docs)
```

**First login.** An admin is created on first start (`ADMIN_EMAIL`, default `admin@oreshnik.dev`). If `ADMIN_PASSWORD`
is empty a random password is printed once:

```bash
docker compose logs backend | grep "initial admin"
```

Before using it for real, replace the `CHANGE_ME` secrets in `.env` (outside `ORESHNIK_ENV=dev` the API refuses to start with them).

The default runner is `mock` — it simulates Strix runs without calling any LLM, so you
can build and test the whole pipeline without burning tokens. Switch a runner to
`local` to execute real scans.
