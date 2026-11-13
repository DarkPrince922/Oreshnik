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

## 12. Agent activity — "what are the agents doing?"

**Why the log looks empty.** In headless mode (`-n`) Strix prints only its banner and the findings to stdout. The agents' actual
work — shell commands (`exec_command`), their output, reasoning, spawning of sub-agents (`create_agent`) — never reaches stdout. It is
persisted in the run directory instead (verified against strix-agent 1.7.0, including a database written by its real session class):

| File | Content |
|------|---------|
| `<run>/.state/agents.json` | `{statuses, names, parent_of, errors}` — the agent tree. Statuses: `running`, `waiting`, `budget_paused`, `completed`, `stopped`, `crashed`, `failed` |
| `<run>/.state/agents.db` | SQLite (WAL), `agent_messages(id, session_id = agent id, message_data, created_at)`; `message_data` is an Agents-SDK item: message (`role`), `function_call` (`name`, JSON `arguments`, `call_id`), `function_call_output`, `reasoning` |

**Pipeline.** The worker's `ActivityTailer` (a thread next to the Strix process, 1 s period) reads new `agent_messages` rows through a
short-lived **read-only** SQLite connection — verified to work while Strix is writing — and appends normalised events to
`scan-<id>/activity.jsonl`; the roster goes to `scan-<id>/agents.json` (atomic replace). The API never touches Strix's database
(except the backfill below), so the panel is independent of Strix's internal format and of SQLite locking. `stop()` does a final flush
**before** the scan is reported finished, so the last steps are never lost.

- Event kinds: `task` (an agent's first user turn = the task it was launched with), `message`, `agent_message` (between agents),
  `system` (budget notices), `tool_call` (+ one-line `summary`, e.g. the shell command), `tool_output` (paired by `call_id`).
  System noise (inherited-context dumps, "you ended a turn without a tool call" nudges, reasoning items) is dropped.
- **Bounded:** text 6 000 chars (head+tail kept), tool args 3 000 per string, images never inlined, 20 000 events / 50 MB per scan
  (a final `system` event says when recording stopped). ANSI/control characters are stripped. The injected LLM key is redacted.
- `GET /api/scans/{id}/activity?cursor=<byte offset>` → `{events, next_cursor, agents, finished}`; the cursor is a byte offset, so each
  poll reads only new data and page boundaries cannot lose or repeat events.
- **Access: admin and the owning operator only — not viewers.** Raw transcripts contain every command, including credentials the
  instruction handed to the agents; findings are visible to viewers, transcripts are not (API 403, tab hidden).
- **Backfill:** a *finished* scan that has no `activity.jsonl` (run before this feature, or by an older worker) is rebuilt from
  `agents.db` on first request, under a file lock. Running scans are never read by the API process.

**UI (`Агенты` tab, the default for a running scan).** Agent tree (who spawned whom, status, command count; click to filter) and a live
feed: shell commands with their output, the agents' messages, "Запущен агент X — задача …", vulnerability reports. Filters
Всё/Команды/Сообщения, auto-follow with a "↓ к новым шагам" hint, windowed rendering. All text is rendered as plain text — commands and
output quote the scanned target and may be hostile (verified in a browser with `<img onerror>` in targets).

**MockRunner** writes the same `.state/agents.json` + `agents.db` (the DDL is taken from a database produced by the real library):
root agent → nmap → spawns "Recon Specialist" → spawns a "Validator N" per finding → vulnerability reports → finish. `MOCK:slow` makes the
recon phase 60 commands long, `MOCK:fail`/cancel leave agents `failed`/`stopped`.

**Not verified:** a real multi-agent scan. Real Strix uses more tools than the mock (browser, proxy/Caido, notes, coverage, …); they are
shown generically (tool name + the first meaningful argument) and may need nicer rendering once seen on real data.

### Layout on wide screens
The page content uses the full width next to the sidebar (no centred max-width column): measured at 1366/1920/2560 px, the gap
between the sidebar and the content and between the content and the right edge is just the page padding (40–48 px); before, a
1024 px column left 320 px (1920) / 640 px (2560) empty on each side. Cards (LLM profiles, runners) share the row evenly and are
capped at 44 rem each; reading-width content keeps a sensible measure (report 56 rem, finding text 64 rem). The log and the
agent feed are sized from the viewport height (`100vh − 29rem`, at least 28–32 rem) so a large monitor shows more of the process.

## 13. Continuing a scan with new instructions ("Продолжить…")

Strix has no way to talk to a **running** headless scan (`-n` reads its instruction only at start; the interactive TUI can message
agents, but it talks to the process over a private socket). What it does have is **`--resume <run>`**, and that is what the panel
uses — for scans that are *finished or stopped* (verified against strix-agent 1.7.0):

| Fact (source) | Consequence |
|---|---|
| `--resume NAME` loads `./strix_runs/NAME/{run.json, .state/agents.json, .state/agents.db}` and **refuses `--target`** | the panel passes `--resume=NAME` and no targets; the targets come from the run |
| a new `--instruction` on resume is injected into the root agent's session as a plain, high-priority *user* message, and the agent loop restarts (`core/runner.py`: "otherwise … silently ignored") | the instruction is what makes a *finished* scan do more work |
| findings, usage and agent histories are restored from the run directory | results and cost of a continuation are **cumulative** |
| the budget limit is compared with the **whole** run's cost (`recomputed_budget_flags`: `cost >= max_budget` → stop) | a limit at or below what was already spent stops the continuation at once → refused up front |
| sandbox containers are named after the run; repo clones live in `/tmp/strix_repos/<run>` | one run must never be active twice |

**Model.** A continuation is a **new scan** (`POST /api/scans/{id}/continue`, columns `parent_scan_id`, `resume_run_name`,
`cost_base`). The parent's run directory is **copied** into the new scan's own working directory (`services/resume.py`, symlinks
copied as links, never followed), so the parent keeps its results exactly as they were, and one scan can be continued in several
directions. `cost - cost_base` is what the continuation itself cost.

**Rules.**
- Only finished scans (succeeded / vulns_found / failed / cancelled) that still have their run files can be continued; admin or the
  owning operator, never a viewer. The child belongs to whoever started it and needs **that** user's LLM profile (an admin does not
  inherit an operator's key). Mock runs need none.
- A scan that finished on its own (`succeeded` / `vulns_found`) needs a new instruction; a stopped or failed one can simply resume.
- At most one active scan per Strix run (409 "уже продолжается в скане #N"); the parent cannot be deleted while a continuation
  still needs it. The per-user active-scan limit applies.
- **Exit code 2 safeguard.** A continued run starts out holding the parent's findings, and argparse also exits with 2. So
  "vulnerabilities found" is only believed if Strix really touched the run (`run.json` changed); otherwise it is a failure.
- The activity feed of a continuation **starts after the parent's history** (`ActivityTailer.skip_existing`): you see the injected
  instruction first, labelled "Инструкция пользователя", then what the agents did about it; the roster still lists every agent.

**UI.** A "Продолжить…" button on finished scans (owner/admin): a dialog explaining that a new scan is created, the instruction
(required or optional as above), LLM profile (real runners), and a *total* budget limit with the amount already spent. The new scan
links back to its parent ("↻ Продолжение скана #N"), the parent lists its continuations, and the scan list marks them ("↻ из #N").

**Not verified:** a real `--resume` of a **completed** run on a machine with Docker and an LLM. The argument handling was checked
against the real CLI, the resume code path was read, and the whole flow was exercised end to end with a fake `strix` that reproduces
the CLI contract and with the simulated runner — but what the root agent actually *does* with the injected instruction after it had
already finished is up to the model.
**Known limit:** repository targets are cloned to `/tmp/strix_repos/<run>` (outside the run directory); if the worker container was
recreated since the parent ran, Strix itself refuses the resume with a clear message ("cloned repo … is missing").
