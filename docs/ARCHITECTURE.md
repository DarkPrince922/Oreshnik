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
│ Postgres      │ Redis (Celery broker + results)        │
└───────────────┴───────────────────────────────────────────┘
                │ enqueue
┌───────────────▼───────────────────────────────────────────┐
│ Celery worker ── picks a Runner:                           │
│   • MockRunner  — fakes a Strix run (no tokens burned)     │
│   • LocalRunner — subprocess `strix -n ...`, docker.sock   │
│   • SSHRunner   — provisions + runs strix on a remote host │
│  appends stdout to a log file; API tails it → WS → UI;     │
│  parses artifacts → DB                                     │
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
- **Phase 2 — Scan engine** *(done)*: Runner abstraction (Mock + Local), Strix command builder, Celery task, live log over WebSocket, artifact parser → DB, cancel, reaper. See §10.
- **Phase 3 — UI** *(done)*: dashboard, scan list, new scan, scan detail (live log + findings + report), severity views, SARIF/JSON download. See §11.
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

## 10. Phase 2 — scan engine

### Verified against the real Strix (strix-agent 1.7.0, installed and inspected — not assumed)
| Fact | Consequence in Oreshnik |
|------|-------------------------|
| headless `-n`; mode `-m quick\|standard\|deep`, **default `deep`** | panel defaults to `quick`; `max_budget` → `--max-budget` |
| results in `./strix_runs/<name>/` of the cwd; name auto-generated | every scan gets a private cwd `STRIX_RUNS_DIR/scan-<id>/`; the run dir is discovered afterwards |
| exit `0` done, `1` error (Docker/model/preflight), `2` findings — **but argparse also exits 2** | "vulnerabilities found" is only believed when findings were actually parsed (`scan_outcome.decide`) |
| `SIGINT` → graceful stop (`run.json status=interrupted`, sandbox cleanup) | cancel = SIGINT → (grace) SIGTERM → (grace) SIGKILL on the whole process group |
| sandbox containers get label `strix-run-id=$STRIX_RUN_ID` | `STRIX_RUN_ID=oreshnik-scan-<id>`; leftovers are `docker rm -f`-ed after every run and by the reaper |
| keeps config in `~/.strix/` | each scan gets its own `HOME` — no key ever leaks between users' scans |
| **local directory targets are mounted writable into the sandbox** | filesystem-path targets are admin-only (see `services/targets.py`) |
| `--target=X` / `--instruction-file=F` forms parse correctly; `--target=-evil` is a value, not a flag | arguments always use the `=` form; instructions go through a file |

### Flow
```
POST /api/scans ─► validate targets/runner/profile/limits ─► scans row (queued) ─► Celery scan_run(id)
worker: atomic claim queued→running ─► engine (Mock|Local) ─► log file (cleaned, secrets redacted)
        ─► parse run dir ─► decide status ─► findings rows + cost/tokens ─► final status
API:    GET .../log (paged) · WS .../ws (live, replay+tail) · findings · report · export json|sarif|md
```
Statuses: `queued → running → succeeded | vulns_found | failed | cancelled` (+ `cancelling` while a stop is in flight).

### Engine pieces (`backend/app/engine`, `worker/tasks.py`, `services/`)
- **MockRunner** — writes artifacts in the exact Strix format, every finding labelled `[MOCK]`. Steerable from the instruction
  text: `MOCK:clean`, `MOCK:fail`, `MOCK:slow` (handy for trying every UI state without spending a token).
- **LocalRunner** — real subprocess. The Strix process gets an **allow-listed environment** (PATH, DOCKER_*, proxy vars +
  `STRIX_LLM`/`LLM_API_KEY`/`LLM_API_BASE`); the panel's own secrets (`JWT_SECRET`, `SECRET_ENCRYPTION_KEY`, DB password…) never reach it.
- **Log** — `STRIX_RUNS_DIR/scan-<id>/oreshnik.log`, ANSI/control characters stripped, the injected API key redacted, 20 MB cap.
  The API *tails the file* for the WebSocket instead of using Redis pub/sub: one fewer moving part, no replay/race logic, and the
  log survives a Redis restart. The worker closes the log **before** it publishes the final status, so the final drain is complete.
- **Cancel** — API does conditional `UPDATE`s (`queued→cancelled`, `running→cancelling`) so a scan being claimed at that instant
  cannot slip past; the worker polls the DB every 2 s (the same poll is its heartbeat).
- **Reaper** (`reap_stale_scans`, every 60 s via beat) — a running scan with no heartbeat for `SCAN_STALE_SECONDS` is failed and its
  sandbox containers removed. Covers a killed/OOM'd worker.
- **Guards** — `MAX_ACTIVE_SCANS_PER_USER` (429), `SCAN_TIMEOUT_MINUTES` wall clock, scan claimed at most once (a redelivered task
  never runs — and spends — twice).

### API additions
| Endpoint | Access |
|----------|--------|
| `POST /api/scans` | admin, operator |
| `GET /api/scans` (`status`, `limit`, `offset`), `GET /api/scans/{id}` | all roles (operators: own scans only) |
| `POST /api/scans/{id}/cancel`, `DELETE /api/scans/{id}` | admin, operator (own) |
| `GET /api/scans/{id}/log?offset&limit`, `.../findings`, `.../findings/{fid}`, `.../report`, `.../export/{json\|sarif\|md}` | same visibility |
| `WS /api/scans/{id}/ws` — first frame `{"token": "<jwt>", "from": 0}`; frames `status` / `log` / `end` | same visibility |

Viewers see all scans read-only but **not the instruction text** (it may contain test credentials).

### Known limits / not verified
- `Dockerfile.worker` and the compose wiring were **not built or run** (no Docker daemon in the dev environment). The Strix
  install mirrors a verified `pip install strix-agent==1.7.0`; the real Docker-out-of-Docker run (worker container → host Docker →
  sandbox) still needs a first run on a machine with Docker.
- A real LLM-backed scan has never been run (no key/Docker here). The `local` path was exercised with the real `strix` binary up to
  its Docker preflight, and end-to-end with a fake `strix` that reproduces its arguments, exit codes, signals and artifacts.
- Bind-mounting **admin-supplied local paths** from inside the worker container will refer to the container's filesystem, not the
  host's. Remote targets (URL/domain/IP/repo) are unaffected.
- Postman targets (`postman://`) are rejected for now (they need `POSTMAN_API_KEY`).
- Run directories are never pruned automatically (only on scan delete).

## 11. Phase 3 — scan UI

### Pages
| Route | Who | What |
|-------|-----|------|
| `/scans` | all | history with status filter, pagination, severity mini-bars; **polls every 3 s while anything is active** |
| `/scans/new` | admin, operator | targets (one per line), mode quick/standard/deep (deep carries a cost warning), runner, LLM profile, budget, instruction; `MOCK:*` helper chips on the mock runner |
| `/scans/:id` | all (operators: own) | status, live log, findings, report, exports, cancel/delete |
| `/` | all | active scans, recent findings, last 5 scans |

### Live log (`lib/useScanStream.ts`, `lib/logStream.ts`)
WebSocket with first-frame auth. On a dropped connection it reconnects with exponential backoff (0.4 s → 8 s) and resumes
with `from = nextSeq`; `LogBuffer` drops replayed frames, so a reconnect can neither lose nor duplicate lines. Memory is capped
at 50 000 lines and rendering is windowed to the last 1 000 (button to show all). Auto-scroll follows the tail unless the reader
scrolls up ("↓ к новым строкам"). Close code 4401 ends the session; 4404 means the scan is gone.

### Rendering untrusted text (the important part)
Everything a scan produces — log lines, finding fields, the report — is derived from the scanned target and may be hostile.
- Log lines and finding fields are rendered **only as React text nodes** (`whitespace-pre-wrap`), never as HTML.
- The report is markdown rendered by `react-markdown` (no raw HTML, `javascript:` URLs stripped) with **images removed** (an
  `<img>` would make the viewer's browser call an arbitrary host) and links opened with `noopener noreferrer nofollow`.
- Exports are downloaded through an authenticated `fetch` (a plain link cannot carry the bearer token) and served as attachments.
- Verified in a real browser with targets containing `<img onerror=…>`, a markdown image to an external host and a
  `javascript:` link: nothing executes, no request leaves for the external host, no `<img>`/`javascript:` link reaches the DOM.

### Wording that avoids false assurance
A scan with no findings is labelled **«Завершён, находок нет»** (never «Чисто»/«Безопасно») and its Findings tab says this is not
proof the target is safe. Runs that ended with an unexpected Strix status show a warning (e.g. possibly incomplete).

### Tests
- `frontend`: `npm test` (vitest) — `LogBuffer` (dedupe, gap marker, cap, resume), line classification, finding text helpers, target parsing.
- Browser end-to-end (Playwright, 55 checks) was run against the real stack (uvicorn + prefork Celery worker + Vite): the full
  create → live → findings → export → report flow, socket killed twice mid-scan, cancel, failed/clean scans, hostile targets,
  operator isolation, viewer restrictions, live list refresh, mobile overflow. The script is not part of the repo.
