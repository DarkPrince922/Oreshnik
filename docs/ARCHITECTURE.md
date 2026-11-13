# Oreshnik — Architecture

Web control-plane around the open-source Strix pentesting CLI. This document is the
source of truth for the design agreed during planning.

> ⚠️ Authorized testing only. Oreshnik orchestrates Strix, which must only be pointed
> at systems you own or are explicitly permitted to test.

## 1. Why a control-plane (not a library wrapper)

Strix's open-source distribution exposes **no Python or REST API**. The only
integration surface is:

- the **headless CLI**: `strix -n -t <target> --scan-mode quick|deep --instruction "..." --max-budget N`
- the **artifacts** it writes to `strix_runs/<run-name>/`:
  `run.json` (status, `llm_usage.cost`), `vulnerabilities.json`, `findings.sarif`,
  `penetration_test_report.md`, `vulnerabilities/*.md`
- **stdout**: streams findings in real time; exit codes `0` clean / `1` fatal / `2` vulns found
- **Docker is required** — each run spins up a sandbox container.

So Oreshnik runs the CLI as a job, streams its stdout, and parses its artifacts.

## 2. Topology

```
┌───────────────────────────────────────────────────────────┐
│ Browser — panel "Oreshnik" (React/Vite/TS)                 │
│  Dashboard · New scan · Scan detail (live log + report) ·  │
│  Findings · Runners · LLM profiles · Users                 │
└───────────────┬───────────────────────────────────────────┘
                │ REST + WebSocket (live log)
┌───────────────▼───────────────────────────────────────────┐
│ FastAPI — auth/roles · scans · runners · llm profiles ·    │
│           findings · schedules · settings                   │
│   secrets (LLM/SSH keys) encrypted at rest (Fernet)         │
├───────────────┬───────────────────────────────────────────┤
│ Postgres      │ Redis (Celery broker + log pub/sub)        │
└───────────────┴───────────────────────────────────────────┘
                │ enqueue
┌───────────────▼───────────────────────────────────────────┐
│ Celery worker ── picks a Runner:                           │
│   • MockRunner  — fakes a Strix run (no tokens burned)     │
│   • LocalRunner — subprocess `strix -n ...`, docker.sock   │
│   • SSHRunner   — provisions + runs strix on a remote host │
│  streams stdout → Redis → WS → UI; parses artifacts → DB   │
├────────────────────────────────────────────────────────────┤
│ Celery beat ── cron schedules → periodic scans             │
└────────────────────────────────────────────────────────────┘
```

Single VM hosts panel + backend + Strix + Docker. Additional **SSH runners** can be
attached: the panel connects over SSH, installs everything required (Docker, Strix,
deps), and health-checks that the runner is online.

## 3. Decisions (locked)

| # | Topic | Decision |
|---|-------|----------|
| 1 | Docker access | worker mounts `docker.sock` (host-root level) — accepted risk |
| 2 | Targets | same forms Strix accepts (URL/domain/IP/repo/OpenAPI/Postman); no ownership verification |
| 3 | Cancel / budget | as Strix: cancel = kill process + container; limits via `--max-budget` and scan-mode |
| 4 | LLM | **per-user** profiles: Claude / OpenAI / Grok / custom (`model` + `base_url` + `api_key`); keys encrypted |
| 5 | Findings | as Strix: each run is an independent snapshot, no dedup; export md / JSON / SARIF |
| 6 | Notifications | panel only |
| 7 | Audit log | not yet |
| 8 | SSH runners | panel SSHes in, installs deps, marks online via health-check |
| 9 | Deploy | plain HTTP during development |
| 10 | Target allowlist | **deferred** — not enforced yet (added later as an option) |
| 11 | Dev safety | MockRunner default, so building/testing burns no LLM tokens |

### Roles
- `admin` — manage users, runners; see everything.
- `operator` — create/run scans, manage own LLM profiles.
- `viewer` — read-only.

LLM keys are **personal**: each user manages their own profiles and picks one per scan.

## 4. Data model (draft)

- `users` — id, email, password_hash, role, created_at
- `llm_profiles` — id, **user_id**, name, provider(claude|openai|grok|custom), model, base_url?, api_key_enc, created_at
- `runners` — id, name, kind(mock|local|ssh), status(online|offline|provisioning|error), ssh_host?, ssh_user?, ssh_key_enc?, last_seen_at
- `scans` — id, user_id, runner_id, targets[], scan_mode, instruction?, max_budget?, llm_profile_id, status, cost, exit_code?, run_dir, created_at, started_at?, finished_at?
- `findings` — id, scan_id, severity, title, description, poc?, recommendation?, raw(json)
- `schedules` — id, user_id, name, cron, scan_template(json), enabled, last_run_at?
- `scan_logs` — persisted stdout per scan (file on disk + tail in DB)

### Scan lifecycle
`queued → running → (succeeded | vulns_found | failed | cancelled)`
stdout → Redis pub/sub → WebSocket; on finish parse
`run.json` / `vulnerabilities.json` / `findings.sarif` / `penetration_test_report.md`.

## 5. Runner abstraction

```python
class Runner(Protocol):
    async def prepare(self) -> None: ...          # provision/verify (SSH installs deps)
    async def health(self) -> RunnerStatus: ...   # online?
    async def run(self, spec: ScanSpec, on_log) -> RunResult: ...  # execute, stream stdout
    async def cancel(self, handle) -> None: ...    # kill process + container
    async def collect(self, handle) -> RunArtifacts: ...  # fetch strix_runs/<id>
```

- **MockRunner** — emits scripted stdout + writes a fake `run.json`/`vulnerabilities.json`.
- **LocalRunner** — `subprocess` + `docker.sock`.
- **SSHRunner** — `asyncssh`: provision deps, run remotely, `sftp` back artifacts.

## 6. Security of the panel itself

- Secrets (LLM api keys, SSH keys) encrypted with Fernet (`SECRET_ENCRYPTION_KEY`), never returned to UI in plaintext.
- JWT auth; role checks on every mutating route.
- Plain HTTP only in dev; put behind a TLS reverse proxy for prod.

## 7. Phased build plan

- **Phase 0 — Skeleton** *(done)*: repo layout, docker-compose (postgres/redis/backend/worker/beat/frontend), config, Alembic, health endpoints, branded frontend shell + login page.
- **Phase 1 — Domain + auth** *(done)*: users/roles, JWT login, per-user LLM profiles (encrypted), runners CRUD with
  worker-side health checks, first-run bootstrap, login throttling, panel UI for all of it. See §9.
- **Phase 2 — Scan engine**: Runner abstraction (Mock + Local), Strix command builder, Celery task, stdout → WS, artifact parser → DB.
- **Phase 3 — UI**: dashboard, new scan, scan detail (live log + findings + report), severity views, SARIF/JSON download.
- **Phase 4 — Scheduling + cost**: beat schedules, cost from `run.json`.
- **Phase 5 — SSH runners**: remote provisioning + execution + artifact fetch.

## 8. UI direction

Strict **black**, but "alive": dark glass panels, smooth transitions, micro-interactions
(Framer Motion), animated background, live status indicators. Brand: Oreshnik.

## 9. Phase 1 — what exists

### API
| Area | Endpoints | Access |
|------|-----------|--------|
| Auth | `POST /api/auth/login`, `GET /api/auth/me`, `POST /api/auth/change-password` | login: public; others: any user |
| Users | `GET/POST /api/users`, `PATCH/DELETE /api/users/{id}` | admin |
| LLM profiles | `GET/POST /api/llm-profiles`, `PATCH/DELETE /api/llm-profiles/{id}`, `GET /api/llm-profiles/presets` | admin, operator — **own profiles only** |
| Runners | `GET /api/runners` | any user (SSH details admin-only) |
| | `POST /api/runners`, `PATCH/DELETE /api/runners/{id}`, `POST /api/runners/{id}/check` | admin |

### Behaviours worth knowing
- **First run:** if the `users` table is empty an admin is created from `ADMIN_EMAIL` / `ADMIN_PASSWORD`. With an empty
  `ADMIN_PASSWORD` a random one is logged **once** (`docker compose logs backend | grep "initial admin"`). A `mock` runner is
  created too. Both steps are idempotent.
- **Tokens** are JWTs, but the user is re-read from the DB on every request, so deactivating an account or changing its role
  takes effect immediately.
- **Login throttling:** 10 failed attempts per email+IP per 15 min (Redis; fails open if Redis is down). Unknown emails and wrong
  passwords return the same response and take the same time.
- **Secrets:** LLM API keys and SSH keys are Fernet-encrypted; responses only carry a hint (`…1234`) / `has_ssh_key`. A profile owned
  by someone else is a 404 — admins can't read other users' keys either.
- **LLM → Strix mapping** (`app/services/llm.py::build_strix_env`): `claude→anthropic/<model>`, `openai→openai/<model>`,
  `grok→xai/<model>`, `custom→openai/<model>` + `LLM_API_BASE` (custom requires a base URL). The Phase 2 scan engine calls this
  right before launching Strix.
- **Runner checks run on the Celery worker**, not in the API, because the worker is where scans run (`local` = strix CLI +
  docker.sock present). If the worker doesn't answer within 15 s the runner is marked offline. `ssh` runners are stored but can't be
  checked until Phase 5.
- **Startup safety:** outside `ORESHNIK_ENV=dev` the API refuses to start with placeholder/weak `SECRET_ENCRYPTION_KEY` / `JWT_SECRET`.

### Tests
`backend/tests` — 37 tests against a real Postgres (schema built by the real Alembic migrations). Run:

```bash
cd backend && pip install -r requirements.txt -r requirements-dev.txt
POSTGRES_HOST=localhost POSTGRES_PORT=5432 pytest      # needs a Postgres user that can CREATE DATABASE
```
