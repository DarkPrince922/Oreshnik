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
- **Strix does not phone home.** By itself the Strix CLI (1.7.0) reports to PostHog and Scarf (model name, finding severities and CWEs,
  duration, cost, LLM providers, loaded skills; never targets, instructions or finding texts) and asks PyPI once a day whether a
  newer version exists. Both are **off by default** in the panel: every runner — local and SSH — passes `STRIX_TELEMETRY=0` and
  `STRIX_NO_UPDATE_CHECK=1` explicitly, *after* the scan's own variables, so neither the worker's environment nor a scan can switch
  them back on. Turn them on with the settings `STRIX_TELEMETRY` / `STRIX_UPDATE_CHECK`. Checked with the real Strix through the
  panel's local runner and a proxy that records the hosts it tries to reach: with the defaults **none**; with both switched on
  `us.i.posthog.com`, `strix.gateway.scarf.sh` and `pypi.org`. (Not covered: the LLM provider you chose, which is the point of the
  scan, and the sandbox image pull from GHCR.)

## 7. Phased build plan

Status legend: *(done)* shipped and tested · *(in progress)* being built · *(planned)* not started.

- **Phase 0 — Skeleton** *(done)*: repo layout, docker-compose (postgres/redis/backend/worker/beat/frontend), config, Alembic, health endpoints, branded frontend shell + login page.
- **Phase 1 — Domain + auth** *(done)*: users/roles, JWT login, per-user LLM profiles (encrypted), runners CRUD with
  worker-side health checks, first-run bootstrap, login throttling, panel UI for all of it. See §9.
- **Phase 2 — Scan engine** *(done)*: Runner abstraction (Mock + Local), Strix command builder, Celery task, live log over WebSocket, artifact parser → DB, cancel, reaper. See §10.
- **Phase 3 — UI** *(done)*: dashboard, scan list, new scan, scan detail (live log + findings + report), severity views, SARIF/JSON download. See §11.
  Three things were added to phase 3 **beyond the original plan**, each at the owner's request after seeing a real scan run:
  - *live agent activity* — the "Агенты" tab (commands, messages, sub-agents) with a worker-side tailer and API. See §12.
  - *continuing a scan with new instructions* (`strix --resume`) — first as a new scan on a copy of the run (migration 0004), later changed at the owner's request to continue **in the same scan** by default (migration 0006). See §13.
  - *full-width layout* on large screens (UI only).
- **Phase 4 — Scheduling + cost** *(done)*: see §14.
  - *Scheduling:* recurring scans by cron (with time zone), run-now, overlap protection, safe defaults for real (paid) runners.
  - *Live cost:* the spend of a running scan from `run.json` (Strix rewrites it after every model cycle) and a budget bar with warnings.
  - *Cost accounting:* spend by user, day and scan, counting only what each scan itself cost (continuations are cumulative in Strix).
- **Phase 5 — SSH runners** *(done)*: the panel connects over SSH with a **pinned host key** (confirmed by fingerprint), health-checks the host, installs Docker + Strix when asked, runs scans remotely (live log, cost and agent feed mirrored, cancel of the whole process group, results fetched back) and cleans up. See §15.

**Deferred by decision** (not part of any phase unless the owner asks): target allow-list / ownership checks (decision #10), audit log (#7), notifications outside the panel (#6), automatic retention/pruning of run directories.

**Open technical debt:** `Dockerfile.worker` and the compose wiring have never been built or run (no Docker daemon in the dev environment, and none on any host the SSH runner was tried against); no real LLM-backed scan has been verified end to end through the panel; sending messages to a *running* headless scan is impossible in Strix 1.7.0 (only the interactive TUI can), so "continue" works on finished or stopped scans only.

## 8. UI direction

Strict **black**, but "alive": dark glass panels, smooth transitions, micro-interactions
(Framer Motion), animated background, live status indicators. Brand: Oreshnik.

## 9. Phase 1 — what exists

### API
| Area | Endpoints | Access |
|------|-----------|--------|
| Auth | `POST /api/auth/login`, `GET /api/auth/me`, `POST /api/auth/change-password` | login: public; others: any user |
| Users | `GET/POST /api/users`, `PATCH/DELETE /api/users/{id}` | admin |
| LLM profiles | `GET/POST /api/llm-profiles`, `PATCH/DELETE /api/llm-profiles/{id}`, `GET /api/llm-profiles/presets`, `POST /api/llm-profiles/models` (fetch the provider's model list) | admin, operator — **own profiles only** |
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
  `grok→xai/<model>` (prefix skipped if the admin already typed it fully qualified) + `LLM_API_BASE` (custom requires a base
  URL). The Phase 2 scan engine calls this right before launching Strix.
  For `custom`, `<model>` is **unconditionally** prefixed with `openai/`, never skipped even if it already looks
  prefixed — verified against the real `strix-agent` 1.7.0 / `openai-agents` SDK: any *other* prefix is handed to LiteLLM as
  a provider name and rejected if it isn't one LiteLLM recognises (so a model id like `customprovider/modelid` would fail
  outright), while `openai/` is the one tag the SDK's own `MultiProvider` always strips exactly once before using the client
  that actually honours `LLM_API_BASE` — so prepending it unconditionally is what makes the remainder reach the endpoint
  byte-for-byte as typed, whatever it contains (including a model id that happens to itself start with `openai/`, e.g. a
  namespaced id like `openai/gpt-oss-20b` served by a self-hosted gateway).
- **"Загрузить модели"** (`POST /api/llm-profiles/models`, `app/services/llm.py::fetch_provider_models`): lets the form ask
  the provider what it actually serves instead of the admin guessing an id — Anthropic/OpenAI/xAI's own `/v1/models`, or
  `{base_url}/models` for `custom`. Either a freshly typed (not yet saved) `api_key`, or `profile_id` of an owned profile
  whose stored key is reused server-side (so refreshing the list while editing never needs the key retyped), must resolve
  to a key. For `custom` this is the panel's own outbound request to the admin's own `base_url` — the same host the key
  already goes to on every real scan, so it opens no reach beyond what the profile already grants. Bounded timeout (10 s)
  and response size (2 MB, enforced while streaming, not after buffering); 401/403 and other HTTP errors, a timeout, non-JSON
  and an empty model list are all reported as a readable `422`/`502`, never a stack trace. Model ids are returned exactly as
  the provider spelled them (`{"data":[...]}` or `{"models":[...]}` or a bare list, item either a string or `{"id"|"name":
  ...}`) — the frontend offers them as `<datalist>` options next to the preset's own suggestions, but the field stays free
  text either way.
- **Reasoning effort** (`STRIX_REASONING_EFFORT`, `engine/base.py::strix_reasoning_env`, applied like the privacy env above —
  explicit, after the scan's own variables, same for both runners): how hard each model call thinks before answering
  (none…max; Strix's own default is `high`, which is this setting's default too, so leaving it alone changes nothing).
  Checked against the real `strix-agent` 1.7.0: it only has an effect for a model LiteLLM's own catalog marks
  `supports_reasoning` for — true for the Claude and GPT-5 presets above, **false today for `xai/grok-4` and for a
  "custom" `base_url` model** (LiteLLM's catalog doesn't know them), so the setting silently does nothing for those
  profiles. Strix never sends a `max_tokens`/output-length cap or a context-window size to the model API itself; both
  exist only inside its own history-compaction bookkeeping (`context_window()`/`output_limit()` in `llm/context_budget.py`,
  again from LiteLLM's catalog, falling back to `STRIX_CONTEXT_FALLBACK_TOKENS` — Strix's own env var, 200 000 by
  default, not currently set by the panel — for a model LiteLLM doesn't recognise), and a real provider "context window
  exceeded" error still forces a compaction retry regardless of that estimate.
- **Runner checks run on the Celery worker**, not in the API, because the worker is where scans run (`local` = strix CLI +
  docker.sock present). If the worker doesn't answer within 15 s (60 s for SSH hosts, which have to be reached) the runner is marked
  offline. `ssh` runners are checked over a real connection with a pinned host key (§15).
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

**Two ways, one endpoint** (`POST /api/scans/{id}/continue`):

- **In place (default, requested by the owner).** The *same* scan — same id, log, agent feed, findings — goes back to the queue, and
  Strix picks the run up again from the scan's own directory. No new scan appears: a scan stopped as #10 and continued is still #10.
- **As a new scan** (`as_new_scan: true`, the "Создать новый скан" box). A new scan on a **copy** of the run (`services/resume.py`,
  symlinks copied as links, never followed): the original stays exactly as it was, and one scan can be continued in several
  directions. Columns `parent_scan_id`, `resume_run_name`, `cost_base` (migration 0004); `cost - cost_base` is what the new scan cost.

An admin continuing **someone else's** scan always gets the second kind: the key is personal, so continuing in place would run the
owner's scan on the admin's key and bill the owner (the API answers 403 for an in-place attempt).

**How an in-place continuation works** (migration `0006`: `resume_count`, `elapsed_base`, `resume_history`):

| Part | Behaviour |
|---|---|
| Queueing | one atomic `UPDATE … WHERE status IN (finished)` takes the scan from finished to `queued`; a second click or a competing request loses with 409 and writes nothing. It resets `started_at`/`finished_at`/`exit_code`/`status_detail`, bumps `resume_count`, appends `{at, instruction, max_budget}` to `resume_history`, and replaces `max_budget` (the new limit is for the whole run) |
| Instruction | the worker passes Strix only the instruction **of this resume** (none if the user just picked the scan up again) — never the original one a second time; the original stays in `instruction` |
| Log | appended to the same `oreshnik.log` after a marker line "Продолжение скана (раз: N)"; the WebSocket replays from where the browser stopped, so the page keeps what it has and nothing is duplicated |
| Agent feed | `ActivityTailer.continue_in_place` skips what is already in `agents.db`, **continues the sequence numbers** after the existing `activity.jsonl` (the API and the browser address events by `seq`), and labels the new instruction as the user's |
| Findings and cost | read back from the run at the end (cumulative, like Strix). They are replaced **only if the run directory could really be read**; if it vanished or cannot be parsed the scan fails but keeps its findings, cost and run name |
| Time | `started_at` restarts with each run, so the seconds of the earlier runs are banked in `elapsed_base` and the duration shown is *active* time — the hours the scan sat finished do not count |

**Rules.**
- Only finished scans (succeeded / vulns_found / failed / cancelled) that still have their run files can be continued; admin or the
  owning operator, never a viewer. The scan needs **the continuing user's own** LLM profile (an admin does not inherit an operator's
  key); in place that user is always the owner. Mock runs need none.
- A scan that finished on its own (`succeeded` / `vulns_found`) needs a new instruction; a stopped or failed one can simply resume.
- At most one active scan per Strix run (409 "уже продолжается в скане #N", also across the two kinds); a parent cannot be deleted
  while a *copy* still needs it. The runner's capacity rules apply (§17), and a refused request changes nothing about the scan.
- A scan continued in place that was started by a schedule counts as active, so the schedule's next firing is skipped meanwhile.
- **Exit code 2 safeguard.** A continued run starts out holding the parent's findings, and argparse also exits with 2. So
  "vulnerabilities found" is only believed if Strix really touched the run (`run.json` changed); otherwise it is a failure.
- The activity feed of a **copy** starts after the parent's history (`ActivityTailer.skip_existing`); an in-place continuation simply
  carries on the same feed. Either way you see the injected instruction first, labelled "Инструкция пользователя", then what the
  agents did about it; the roster still lists every agent.

**UI.** A "Продолжить…" button on finished scans (owner/admin): a dialog explaining what will happen, the instruction (required or
optional as above), LLM profile (real runners), a *total* budget limit with the amount already spent, and a box "Создать новый
скан" (ticked and locked for somebody else's scan). In place, the page stays where it is: the status turns to running, the live log
reconnects, the agents tab opens, the header says "↻ Продолжен в этом же скане: N раз", and the Параметры tab lists every
continuation (time, what was asked, the limit). While a continued scan runs, its previous findings and report stay visible with a
note, and are replaced when it finishes. Viewers see that a scan was continued but not what was asked. The scan list marks it
"↻ продолжен ×N"; a copy shows "↻ из #N" and links back to its parent.

**Accounting note.** Spend is booked on the day a scan last finished (§14), so a scan stopped yesterday and continued today moves
its whole cost to today. Money is never counted twice (an in-place scan has no `cost_base`; a copy counts only its own part).

**Tests.** 37 backend tests for the in-place flow (same id, no new row, log/feed continuity, banked time, atomic queueing — including a
competitor winning at the last instant —, instruction handling, budget, profile, ownership, viewer redaction, cancel and resume
again, broker down, unreadable run keeps the results, schedules, cost accounting), each safeguard broken on purpose to see a test
fail; 63 frontend unit tests; a 34-check browser scenario on a real stack (the browser found a bug: the dialog stayed blocked after
the first in-place submit because the page no longer navigated away).

**Not verified:** a real `--resume` of a **completed** run on a machine with Docker and an LLM. The argument handling was checked
against the real CLI, the resume code path was read, and the whole flow was exercised end to end with a fake `strix` that reproduces
the CLI contract and with the simulated runner — but what the root agent actually *does* with the injected instruction after it had
already finished is up to the model.
**Known limit:** repository targets are cloned to `/tmp/strix_repos/<run>` (outside the run directory); if the worker container was
recreated since the parent ran, Strix itself refuses the resume with a clear message ("cloned repo … is missing").

## 14. Scheduling, live cost and cost accounting (phase 4)

### Scheduling
A **schedule** is a scan template (targets, mode, instruction, budget, runner, LLM profile — the same fields as `POST /api/scans`)
plus a 5-field **cron** expression and an IANA **time zone**. Migration `0005_schedules` adds the `schedules` table and
`scans.schedule_id` (a scan remembers which schedule started it; deleting the schedule keeps its scans and clears the link).

**Who fires it.** Celery beat runs `oreshnik.run_due_schedules` every 30 s (so a schedule fires within ~30 s of its time). For each
due schedule the task first *claims* the firing with `UPDATE … WHERE next_run_at = <expected>` — a compare-and-swap, so two beat
processes or overlapping ticks can never start the same firing twice — and only then starts the scan, as its **owner**, with the
owner's LLM profile, through the same code as `POST /api/scans` (`services/scan_factory.py`), so every rule of a normal scan
(targets, filesystem paths admin-only, the runner's capacity rules §17, profile ownership) applies unchanged.

**What it does when things go wrong** (the outcome is stored on the schedule and shown on its card):

| Situation | Result |
|---|---|
| the previous scan of the same schedule is still running | *Пропущен* — no pile-up |
| the owner already has the maximum of active scans, or the task queue is down | *Пропущен* (transient, tries again at the next time) |
| owner deactivated, runner deleted, LLM profile deleted/invalid | *Ошибка*, and the schedule is **switched off** with `disabled_reason` ("Отключено автоматически: …") — a configuration problem does not fix itself |
| beat or the whole worker was down at the scheduled time | the missed firing is **dropped**; the next time is computed from *now*, so recovery never causes a burst of scans |

A schedule whose runner was deleted never falls back to "the default runner" — that could silently turn a real scan into a
simulated one (or the other way round). Editing it requires picking a runner again.

**Safe defaults for real (paid) runners**: a `max_budget` is **mandatory**, and the schedule may not fire more often than every
`SCHEDULE_MIN_INTERVAL_MINUTES` (15). The simulated runner is exempt from both. Other limits: 20 schedules per user
(`MAX_SCHEDULES_PER_USER`), unique name per user; SSH runners work for schedules like any other real runner (§15).

**Time zones and DST.** The expression is evaluated on the *wall clock* of the schedule's zone, then converted to UTC, so "03:00
Europe/Berlin" stays 03:00 local across DST changes: a time skipped by spring-forward fires once (at the first valid instant), and a
time repeated by fall-back fires once (the first occurrence) instead of twice. *Residual edge, accepted:* if the scheduler was down
across the first occurrence and comes back inside the repeated hour, one extra scan can start; overlap protection and the budget
limit bound the damage.

**API** (`/api/schedules`, admin = all, operator = own, **viewer = 403**): `GET` list (with the next 5 firing times), `POST`,
`GET/PATCH/DELETE /{id}`, `GET /preview?cron=&timezone=&count=` (validation + next times + the shortest interval, used by the form),
`POST /{id}/run` (start now; same overlap rule → 409; does not move the schedule's own next time). PATCH recomputes `next_run_at`
only when the timing changed or the schedule was re-enabled, and clears `disabled_reason` when re-enabled.

**UI** — «Расписания» (admin/operator): cards with next/last run in the schedule's own zone, result badge with the reason, a link
to the last scan, an on/off switch, «Запустить сейчас», «Сканы расписания» (the scan list filtered with `?schedule=<id>`), edit,
delete. The form offers presets (hourly, daily 03:00, weekdays 09:00, …), a free cron field with **live preview of the next five
runs** in the chosen zone, and shows the budget/interval rules before submitting. Scans started by a schedule say so on their page
and in the list ("⏱ <name>").

### Live cost and the budget bar
Strix rewrites `run.json` (with `llm_usage.cost` and `total_tokens`) after every model cycle. The worker's existing cancel probe —
which already polls every 2 s for a cancel request and writes the heartbeat — now also reads those two numbers and
writes them in the same `UPDATE`, so `scans.cost` grows **while the scan runs** instead of appearing at the end. A half-written or
odd file is ignored (never fatal); the final value from the artifacts still wins at the end.

On the scan page a **budget bar** shows spend against the scan's limit: normal below 80 %, amber from 80 % ("подходит к лимиту…"),
red at 100 % ("Лимит бюджета достигнут — Strix сворачивает работу"). It can legitimately pass 100 %: Strix stops after the model
cycle that crossed the limit. For a continuation the bar uses the **cumulative** cost, because that is what Strix compares with the
limit (§13). The dashboard shows a warning when an active scan among the latest ones is at ≥ 80 %, and «Расходы» lists the budgets of running scans.

### Cost accounting («Расходы», `GET /api/costs?days=1..365&user_id=`)
- A scan's **own cost** is `greatest(cost − cost_base, 0)`: a continuation's `cost` is cumulative in Strix, so only its own part is
  booked and money is never counted twice.
- It is booked on the UTC date of `coalesce(finished_at, started_at, created_at)` (so a scan that is still running already counts). A scan
  continued in place (§13) is booked on the day it last finished — its earlier spend moves with it.
- Response: today / last 7 days / month-to-date / the chosen window / all time, what the *currently running* scans have cost so
  far, tokens, a gap-free per-day series, per-user totals, and the ten most expensive scans.
- Operators see **only their own** spend (`user_id` is ignored for them); admins see everyone and may filter; viewers get 403.
- UI: stat cards, a bar chart per day (7/30/90 days), per-user table (admin), top scans, a "now" panel with live budget bars.

**Caveat worth knowing.** The numbers are what Strix reports from its pricing tables (LiteLLM), i.e. an **estimate, not an
invoice**. For models whose prices Strix does not know (custom `base_url` models in particular) the figure can be zero or far too
low — a real scan seen earlier showed ≈ $0.07 for 1.8 M tokens. Since `--max-budget` uses the same number, such a limit may never
trigger; for those models the wall-clock limit (`SCAN_TIMEOUT_MINUTES`) and the provider's own spend cap are the real protection.

### Tests
Backend **386 tests** (277 before; 109 new): `test_schedule_service` (cron/zone/DST maths, claim and firing rules, auto-disable),
`test_schedules` (API, roles, validation, ownership), `test_costs` (accounting, windows, roles), `test_live_cost` (a real
subprocess rewriting `run.json` while the scan runs; the database must show the growing value *during* the run). The safeguards
were checked by **deliberately breaking each one** and watching a test fail: non-atomic claim, no overlap protection, falling back to
the default runner, replaying missed firings, double-counting a continuation, no budget requirement, no minimum interval, live cost
not written, viewers seeing money, a new schedule without `next_run_at`. Frontend: **56 vitest tests** (budget states, chart scale,
cron presets, time formatting). Browser end-to-end on a real stack (uvicorn + prefork Celery with beat + Vite): **65 checks**,
including a schedule that beat really fired into a scan, plus the earlier suites rerun (77 + 28) as regression.

### Not verified / limits
- **Live cost on a real Strix scan.** Read from the source (`record_sdk_usage` → `save_run_data`) and reproduced by a fake `strix` and
  the simulated runner, but not observed on a real LLM-backed scan.
- A real (non-simulated) schedule has never fired end to end — only the simulated runner did (no Docker/LLM here).
- Schedules are not evaluated while **beat** is not running; compose runs exactly one beat (a second one would be harmless thanks to
  the claim, merely redundant).
- The DST edge above; precision is the 30 s beat tick; no calendar exclusions (holidays) and no per-schedule retention of old scans.

## 15. SSH runners (phase 5)

An SSH runner moves **only Strix itself** to another machine. Everything the panel builds on stays where it was: the scan's directory
under `STRIX_RUNS_DIR` is still the source of truth, the log is still `oreshnik.log`, results are still read from
`scan-<id>/strix_runs/<name>/`, and the live cost, the agent feed, the budget bar, cancel, schedules, cost accounting and
"Продолжить" work unchanged. Migration `0007` adds `ssh_host_key`, `ssh_host_key_pending`, `provision_log`, `provision_started_at`;
the host, port, user and the (Fernet-encrypted) private key were already in the model; migration `0008` adds the encrypted password
(`ssh_password_enc`).

### Trust: the host key is pinned, never learned silently
- **First contact looks, it does not log in.** The first check fetches the host's public key (no credentials are sent — the host
  sees a handshake and nothing else) and stores it as *pending*. The card shows the fingerprint; the administrator compares it with
  `ssh-keygen -lf /etc/ssh/ssh_host_ed25519_key.pub` **on the server** and confirms in a dialog (a box "я сверил(а)" must be ticked).
  The fingerprint is sent back with the confirmation; if the host presented another key in the meantime, nothing is pinned.
  Checked on a real OpenSSH server: the fingerprint the panel shows is identical to `ssh-keygen -lf`'s.
- **Every connection is checked against the pinned key** (`known_hosts=([trusted], [], [])`). A different key is an *error*, never a
  prompt and never an automatic update: the panel stops **before any credential is sent**, shows both fingerprints, and offers no
  "trust" button — only an explicit "Сбросить старый ключ", after which the new key goes through the same confirmation. Changing the
  host or port forgets the pinned key as well.
- **Logging in: a key, a password, or both** (an administrator chooses per runner; at least one must exist, and the API refuses to
  remove the last one). With both, the key is tried first and the password is only offered if the key is rejected. No SSH agent, and
  none of the panel host's `~/.ssh/config`. A **key is the better choice**: a password can be guessed, and the form says so.
  - A password is only ever offered to a host whose key is **pinned** — sent to an impostor it would be handed over in the clear.
    Verified on a real `sshd`: on first contact, and when the key has changed, the host sees no login attempt at all.
  - Stored encrypted (Fernet), kept exactly as typed, never returned by the API (`has_ssh_password` only), never in an error message,
    a log, a command line or the remote disk.
  - **A wrong password is not repeated.** Each connection makes one attempt; a runner that rejected its login (or whose host key
    changed) is put in the `error` state and the periodic check leaves it alone — retrying a wrong password every two minutes is
    exactly what gets a client locked out by fail2ban. An administrator's "Проверить", or saving corrected credentials (which puts the
    runner back in the rotation), starts it again. A saved change of credentials keeps the confirmed host key (it is about the host,
    not the login); changing the address forgets it.
  - The password is **not** used for `sudo`: installing system packages still needs root or passwordless sudo (feeding a password to
    `sudo -S` would put it on the installer's stdin next to the script, and an askpass helper would write it to the remote disk).
- The private key is never part of an error message or a log, and operators/viewers never see connection details, passwords or
  fingerprints.

### Is the host ready?
One script over SSH (`ssh_runner.probe`) reports OS, user, root/sudo, Docker (present, and usable by this user), Strix on the
non-interactive `PATH` (`~/.local/bin` is added: SSH sessions do not read the profile), python3, setsid, curl, uv and free space. The
verdict is a status plus a reason a person can act on ("нет Strix — нажмите «Установить»", "доступ к Docker — добавьте
пользователя в группу docker…"). `online` means: Docker usable, Strix, python3 and setsid present. Statuses: `error` for a rejected
key or a changed host key (needs attention), `offline` for unreachable or incomplete. A periodic task re-checks every trusted host
(`SSH_CHECK_INTERVAL_SECONDS`, default 120) so "online" is never a week-old fact; hosts that are not confirmed are not contacted.

### Running a scan (`engine/ssh.py`)
1. Connect (key pinned), stop and remove whatever an earlier attempt left in `~/oreshnik/scan-<id>`, create it.
2. For a continuation, **upload the run directory** — the panel's copy is the master, so the remote copy is disposable.
3. Start Strix as `setsid -w sh -s`, with the script and the LLM environment on **stdin**. The command line (visible to every user of
   the host through `ps`) holds no secrets and neither does the remote disk. Every dynamic word (targets, run name, values) is
   shell-quoted; a hostile target arrives at Strix exactly as typed (tested with `$(…)`, backticks and quotes).
4. Stream the output into the scan log (redacted like a local scan). Meanwhile a mirror thread copies `run.json`, `vulnerabilities.json`
   and the agent roster every 2 s, and a **consistent snapshot** of the agents' SQLite database (the host's python3 takes it with
   the backup API — the live database is in WAL mode, so copying the file alone would give a stale or torn picture). That is what
   keeps the live cost, the agent feed and the budget bar working for remote scans.
5. **Cancel and time limit** act on Strix's whole process group: SIGINT, then SIGTERM, then SIGKILL (`STRIX_CANCEL_GRACE_SECONDS`
   apart). Checked on a real sshd with a Strix that ignores SIGINT and one that spawns an immortal child: nothing is left behind.
6. At the end: remove the scan's sandbox containers (by label), **fetch the run directory** and delete the remote directory. The fetch
   treats the host as untrusted: names are validated, symlinks and special files are skipped, and the size/number of files are capped
   (`SSH_FETCH_MAX_MB` 500, `SSH_FETCH_MAX_FILES` 20000). If fetching fails the remote directory is **kept**, the log says where it is
   and the scan fails with the reason — nothing is lost silently.
7. **A lost connection loses the run:** Strix is stopped on the host over a fresh connection (best effort) and the scan fails with
   "Потеряно соединение…" — the same outcome as a worker dying under a local scan. If the *worker* is lost, the reaper does the same
   cleanup on the host (`cleanup_remote`).

Only administrators create or edit runners, so only they can make the panel connect to an address; operators and viewers see
status only. Rules: creating a scan (or a schedule firing) on a host whose key is not confirmed, or that is being installed, is refused up front
(a schedule is then switched off with that reason); **local filesystem paths are refused on an SSH runner** (they would point at the
runner's disk, not the panel's); real runners still need an LLM profile, and the profile's key is sent to the host, into the Strix
process environment — choose hosts you trust with it. "Strix touched `run.json`" is now judged by content (hash), not file time: a
run fetched back from a host is a new file even when nothing changed, and that would have made a startup error (exit code 2) look
like "vulnerabilities found".

### Installing what is missing (`services/provision.py`)
"Установить" (admin, confirmed hosts only) looks at the host first and builds a **plan** of only the missing steps, then runs them one
by one, each in a fresh login (so a new `docker` group membership applies to the next step), streaming everything into a log the UI
polls. A failing step stops the installation; nothing is retried behind the user's back.

| Step | How | Needs root |
|---|---|---|
| Docker | official `get.docker.com` script, downloaded first and then run (never `curl \| sh` as root); service enabled; user added to `docker` | yes |
| python3, util-linux (`setsid`) | apt | yes |
| uv, then Strix | uv installer; `uv tool install --python 3.12 --force strix-agent==<STRIX_VERSION>` for the user | no |
| sandbox image | `docker pull <STRIX_SANDBOX_IMAGE>` (large; otherwise the first scan waits for it) | no |

System packages are installed only on Debian/Ubuntu and only with root or passwordless sudo; otherwise the panel says exactly what is
missing and what to do. Versions are pinned to what the panel was verified against (`STRIX_VERSION=1.7.0`) and quoted, so a hostile
value cannot become a command. One installation at a time; an installation lost with its worker stops blocking the runner after
90 minutes; the log is cleaned of terminal escapes and capped at 200 kB.

### Tests
**Backend: 138 new tests (561 in total)** — 36 for the SSH layer, 18 for host-key trust and checks, 33 for scans on a host, 30 for
installation, 21 for password login — run against a **real SSH server** (asyncssh, no external binary) playing a hermetic remote machine with a fake
`strix`/`docker` on it: the whole scan path, secrets (never in a command line or on the remote disk, redacted in the log and the
feed), cancel of the process group with and without SIGINT, connection loss, host key changes, fetch limits and symlinks, live
mirroring of a WAL database, continuation in place and as a copy, leftovers of an earlier attempt, the reaper, schedules. Every
safeguard was **broken on purpose** (50 mutations: key in the command line, group not signalled, quoting removed, host key not pinned,
symlinks followed, no size cap, remote deletion unrestricted, unconfirmed hosts accepted, one-click trust of a changed key, no
cleanup after a lost connection, freshness by file time, …) and a test failed each time. Frontend: 9 new unit tests (72 in total) for the host key states, fingerprints and login methods. Browser end-to-end against a **real
OpenSSH `sshd`**: 37 checks for keys and 15 for passwords (see below); the earlier browser suites (77, 28, 65, 34 checks) were rerun as
regression.

### Verified for real, and what is not
Verified against a real `sshd` (OpenSSH, as a non-root user): the fingerprint equals `ssh-keygen -lf`; pinned-key login; the probe;
SFTP; exec exit codes and separate stderr; scans through the UI with cancel (SIGINT ignored, child process), live log/feed/cost,
continuation in place; a changed host key blocks scans before any login; **password login** (a runner created with a password only,
nothing offered before the fingerprint is confirmed, a scan over the password with the password in no log or record, a wrong password
tried exactly once per check and not retried by the periodic check, recovery by saving the corrected password, swapping the password
for a key); and the **real Strix installer step** — `uv tool install
--python 3.12 --force strix-agent==1.7.0` run from the UI through SSH installed a working `strix 1.7.0` into `~/.local/bin`.

**Not verified:** the Docker installer (`get.docker.com`) and the uv installer script — those hosts are not reachable from the
development sandbox, so they run only against stand-ins in the tests; a **real Docker daemon** on a remote host (sandbox containers,
`docker pull` of the image) — none is available here; a **real LLM-backed scan** on a host; non-Debian systems (by design not
touched); rootless Docker / `DOCKER_HOST` on the host; SSH through jump hosts; SSH agent authentication and keys protected by a passphrase (not supported); keyboard-interactive logins that ask for more than one
password (a second factor); `sudo` with a password.
Known limits: Strix's repository clones in `/tmp/strix_repos/<run>` on the host are not removed by the panel; the host's own load is
not throttled beyond the runner's own limits (§17) and the worker's concurrency.

## 16. Custom-provider model ids, and fetching a provider's model list

Two related fixes/additions to LLM profiles, both in `app/services/llm.py`:

- **`strix_model_id` no longer special-cases an already-prefixed `custom` model.** A `custom` endpoint's model id can be
  anything that endpoint calls it — it does not have to look like `<vendor>/<id>`, and when it does, that `/` is part of
  the real id (e.g. `customprovider/modelid`), not a hint to add or skip a prefix. Traced through the installed
  `strix-agent` 1.7.0 and the `openai-agents` SDK it builds on (`StrixProvider`/`MultiProvider`): `openai/` is the *one*
  prefix the SDK always strips exactly once to pick the client that honours `LLM_API_BASE`; every other prefix is instead
  handed to LiteLLM as a `custom_llm_provider` name and rejected if LiteLLM doesn't recognise it — so an unrecognised
  prefix like `customproviders/modelid` would previously have reached Strix unprefixed (model has no "/" at the very
  start matching "openai/") only when the typed id had no slash at all; whenever it did, the fix now always prepends
  `openai/`, even if the typed id already starts with `openai/` (e.g. a literal namespaced id such as
  `openai/gpt-oss-20b` served by a self-hosted gateway) — so the remainder after Strix's single guaranteed strip is
  always exactly, byte-for-byte, what the admin typed. Verified for real: started the panel's own API and a tiny
  OpenAI-compatible stand-in server, created `custom` profiles with both a nested id (`customprovider/mymodel` →
  `strix_model: "openai/customprovider/mymodel"`) and the literal-prefix edge case
  (`openai/gpt-oss-20b` → `"openai/openai/gpt-oss-20b"`, which the SDK strips back down to the original `openai/gpt-oss-20b`).
- **"Загрузить модели"** (`POST /api/llm-profiles/models`): the profile form can ask the provider what it actually
  serves — Claude/OpenAI/xAI's own `/v1/models`, or `{base_url}/models` for `custom` — instead of the admin guessing an
  id. Either a freshly typed (not yet saved) `api_key`, or an owned profile's `profile_id` (its stored key is decrypted
  and reused server-side, so refreshing the list while editing never needs the key retyped), must resolve to a key; for
  `custom`, `base_url` is required the same way it is for creating a profile. This is the panel's own outbound request —
  for `custom` it reaches the admin's own `base_url`, exactly the host the key already goes to on every real scan, so it
  opens no reach beyond what the profile already grants. Bounded timeout (10 s) and response size (2 MB, enforced while
  streaming the body, not after buffering it); 401/403, any other HTTP status, a timeout, non-JSON and an empty list are
  all reported as a readable `422`/`502`, never a stack trace. Accepts the common response shapes (`{"data":[...]}`,
  `{"models":[...]}`, or a bare list; each item either a plain string or `{"id"|"name": ...}`) and returns the ids
  exactly as the provider spelled them, sorted. The frontend (`LLMProfiles.tsx`) offers them as `<datalist>` options next
  to the preset's own suggestions — the model field stays free text either way, so an id the fetch can't discover can
  still be typed by hand. The button's enablement and the suggestion-merging are pure functions
  (`frontend/src/lib/llmProfile.ts`) so they are unit-tested the way the rest of `src/lib` is, rather than through a
  component-test harness this project doesn't otherwise use.

### Tests
Backend: 18 new tests (598 in total) — `strix_model_id`'s verbatim pass-through (plain id, nested slashes, the literal
`openai/`-prefix edge case), `fetch_provider_models` against an `httpx.MockTransport` (every response shape, 401, other
HTTP errors, non-JSON, the size cap enforced mid-stream, an empty list, the right auth header per provider), and the
endpoint (role check, needs a key or a `profile_id`, `base_url` required for `custom`, reusing another user's profile is a
404, a `profile_id` for the wrong provider is a 422, provider errors surface as 502). Frontend: 7 new unit tests (79 in
total) for `canFetchModels`/`mergeModelOptions`. Verified for real, beyond the mocked tests above: the panel's own API
making a genuine outbound HTTP call (real `httpx`, real sockets) to a throwaway OpenAI-compatible stand-in server, both
directly (`curl`) and driven through a real headless browser — opening the LLM-profiles page, picking `custom`, filling
`base_url`/key, clicking **Загрузить модели**, seeing the datalist fill with both ids above, and saving the profile.

## 17. Per-runner limits: scans per account, targets at once

Two numbers on every runner (`runners.max_scans_per_user`, `runners.max_targets`; migration 0009), set by an administrator
when the runner is created and editable later. Both are checked in one place, `services/scan_factory.py::check_runner_capacity`,
which `POST /api/scans`, schedules and `POST /api/scans/{id}/continue` (in place or as a copy) all go through.

| | meaning | default |
|---|---|---|
| `max_scans_per_user` | active scans (`queued`, `running`, `cancelling`) one **account** may have **on this runner** | empty = `MAX_ACTIVE_SCANS_PER_USER` (3), read at check time, so changing it needs no edit of any runner |
| `max_targets` | **distinct targets** (domains / hosts) being scanned on this runner at once, **all accounts together** | empty = no limit; the form prefills 3 for a new SSH host, nothing for a local runner |

Behaviour worth knowing:
- **Per runner, not per account in total.** The earlier single per-account cap across all runners is gone: with a local runner and an
  SSH host an account can have up to its number on *each*. `MAX_ACTIVE_SCANS_PER_USER` is now only the default for a runner that has
  no number of its own. (The worker still decides how many scans *run* at once — `SCAN_CONCURRENCY`; the rest wait as `queued`.)
- **What counts as one target** (`services/targets.py::target_key`): the host. `https://App.example.com:8443/login`,
  `app.example.com/x` and `APP.example.com` are one; an IP is its own address; a `git@…` / `git://` repository or a local path is
  counted as the whole string. A repository served over https looks like a web application and counts as its host, so several
  repositories on one forge are one target here — the per-account number still bounds how many such scans there can be.
  `www.example.com` and `example.com` are two hosts. Two accounts scanning the same host share one place.
- **Queued scans hold their place** (they are active), like they already did for the per-account cap. Places come back as soon as
  a scan finishes, fails or is cancelled — there is no counter to drift: both numbers are counted from the scans table each time.
- **Refusals.** Over the per-account number or the target limit: `429` (transient — a schedule is *skipped*, not switched off). A
  single scan with more distinct targets than the runner can *ever* hold: `422` ("Разбейте скан") — waiting would never help. A refusal
  says how many places are taken, **never whose targets** (another account's domains are not for you to read).
- **No race.** The check takes `SELECT … FOR UPDATE` on the runner row and the caller commits straight after, so two simultaneous
  requests for the last place are handled one after the other (before, the count and the insert were two separate steps and a script
  could overshoot the per-account number). The lock is per runner: other runners are not slowed down.

What this does **not** do: it does not limit what a scan costs on the host (CPU, memory, a sandbox's processes) — Strix has its own
`STRIX_SANDBOX_MEM_LIMIT` / `_CPUS` / `_PIDS_LIMIT`, which the panel still does not set — and it does not isolate the sandboxes of
different scans on one host from each other (they share Docker's default bridge network).

### Checked against the installed Strix (1.7.0) before building it
Strix has no concurrency or "targets" limit of its own, so this had to live in the panel; and nothing in Strix stands in the way
of several scans on one host: one sandbox container per run (cached per process by run id, so sub-agents share it), Docker names it
itself (no name clashes), the label `strix-run-id` is per run, Caido's port is published as a random free host port, the per-run
directories (`strix_repos/<run>`, `strix_api_specs/<run>`) are keyed by run name, and the only cross-process file lock is for Codex
subscription tokens, which the panel does not use. A run takes any number of `--target`s into that one sandbox.

### Tests
Backend: 39 new tests (637 in total) in `tests/test_runner_limits.py` — what counts as a target; the per-account number per runner (own number, panel default,
other runners and accounts unaffected, places freed by a cancel); the target limit across accounts (same host not counted twice,
nobody's targets in a refusal, freed by a cancel, a scan that can never fit, per-runner, no limit); continuation in place and as a copy;
a schedule that does not fit is skipped; **simultaneous requests** (8 for 3 places → exactly 3; 6 hosts for 2 places → exactly 2); and
setting/clearing the limits through the API. The checks were broken on purpose (no row lock, count ignoring the runner, the runner's
own number ignored, raw strings instead of hosts, only own scans counted, 429 instead of 422, a refusal naming the busy targets, the
continue path unchecked, "no limit" not honoured) and a test failed each time. Frontend: 6 new unit tests (85 in total) for the
limit fields. Browser end-to-end on the real stack: a local runner shows the defaults, an SSH host starts with 3 targets and keeps an
edited value, `0` is refused with a message, an empty field clears the limit; the refusals were also read off the live API.

## 18. A model that does not answer: the preflight, its message and its timeout

Before a scan starts Strix sends the model one tiny request ("Reply with just 'OK'", `interface/scan_setup.py::preflight_request`) and
gives up after `LLM_PREFLIGHT_TIMEOUT` seconds (30 by default). Its message names the model and the variable, and says nothing about *why* —
the SDK retries 429/5xx/network errors with back-off inside that window, so repeated gateway errors, a blocked route and a slow first token
all end as "did not answer within 30s".

What the panel does about it:
- **The status line shows the whole reason.** Strix prints failures in a bordered box wrapped at the terminal width, so the last *line* is
  often a tail (scan #19 read "Strix завершился с кодом 1: reachable."). `LogWriter` now also keeps the last *paragraph* (lines between blank
  lines / box borders, at most 5), and the worker uses that for the status. For this one failure the status adds what to check: that the
  **runner** (not only the panel) reaches the profile's Base URL, and the timeout setting.
- **`LLM_PREFLIGHT_TIMEOUT`** is a panel setting (`.env`, 1-600, default 30 = Strix's own, so nothing changes until it is set), passed to Strix
  like the privacy and reasoning settings: explicitly, after the scan's own variables, for both runners.

Worth knowing: "Загрузить модели" runs on the **panel's** host and a scan runs on the **runner's** (a different machine for SSH runners), so a
model list that loads says nothing about whether the runner can reach the endpoint.

**What the preflight actually sends** (the real `strix-agent` 1.7.0 run against a local endpoint that logs requests, for `STRIX_LLM`
= `kimi-k3.1-pro`, `openai/kimi-k3.1-pro`, `openai/openai/gpt-oss-20b` and `openai/customprovider/mymodel`): `POST <LLM_API_BASE>/chat/completions`,
non-streaming, with a body of exactly `{"messages": [system "You are a helpful assistant.", user "Reply with just 'OK'."], "model": "<id>"}` —
no `max_tokens`, no `stream`, no reasoning parameters. The `model` field is the id **without** Strix's route prefix: `openai/kimi-k3.1-pro` and
plain `kimi-k3.1-pro` produce identical requests, so the "openai/…" in the failure message and in "Для Strix: …" is Strix's internal name and
never reaches the provider (the form's hint now says both: «Для Strix: openai/X · провайдеру уйдёт: X»). Two consequences: a model id is not the
cause of a preflight timeout, and a thinking model with no `max_tokens` cap, asked non-streaming, may take longer than 30 s to answer even "OK".

To see what the runner sees, on the runner, with the same body (no `max_tokens` — a cap would make a thinking model look fast):
`curl -sS -m 180 -o /dev/null -w "%{http_code} connect=%{time_connect}s first_byte=%{time_starttransfer}s total=%{time_total}s\n" <BASE_URL>/chat/completions
-H "Authorization: Bearer <KEY>" -H "Content-Type: application/json" -d '{"model":"<MODEL>","messages":[{"role":"user","content":"Reply with just OK"}]}'`
— no connection or `000` = the route is blocked; `4xx`/`5xx` = the gateway; `200` with a total over 30 s = a slow model (raise the setting).

Tests: scan #19's log reproduced exactly (unit and end to end through the worker with a real subprocess), the hint, the setting and
both runners (a real local subprocess and a real SSH host receive it; a scan's own env cannot override it). Every piece was broken on
purpose (7 mutations) and a test failed each time.

## 19. «Операции»: a plain-words goal, an LLM-written script, run as root — no approval step

Added at the owner's explicit request and explicit choice, after the trade-off was laid out: an admin describes an
operational task in plain words ("обнови пакеты и перезапусти nginx"); the panel asks the chosen LLM profile to
write a POSIX `sh` script for it; the script runs **immediately**, as root over SSH, on every selected runner — with
**no human review before it runs**. This is categorically different from every other feature in this panel (a scan
is sandboxed and scoped to declared targets; a schedule and a continuation still go through the same checks as a
manual scan): there is no sandbox here, no target-scope check, no approval queue. Admin-only (`require_admin`), not
`operator` — the one role boundary already used for every other irreversible, host-level action (runner CRUD, host
key trust, provisioning).

### What is still enforced, independent of that choice
- **The script is never invisible.** It is stored verbatim on the run (`OpsRun.script`) and shown in the UI before,
  during and after execution — "no approval" does not mean "no audit trail".
- **A short, high-precision safety guard** (`app/services/ops.py::looks_catastrophic`) refuses a handful of
  one-liners that have no legitimate "oops, meant to do that" reading on a real host: a fork bomb, `rm -rf /` (and
  `--no-preserve-root`, any flag spelling/order, via a small token-based check, not a single regex — a regex alone
  missed `rm -rf --no-preserve-root /`), `mkfs`, `dd`/`>` onto a whole raw disk, `wipefs`. It is **not** a review
  step: ordinary admin commands (reinstalling a package, restarting a service, `rm -rf` under `/var/log` or `/opt`)
  are deliberately left alone — false positives on real scripts should be ~zero. When it refuses, **no host is
  touched at all** (every `OpsRunHost` becomes `blocked`, not attempted).
- **Each host's outcome is independent and visible on its own** (`OpsRunHost`): one host's SSH failure, script
  failure or disk-destroying refusal never silently hides another's result, and a refusal never names *whose*
  targets another account is using (mirrors the wording rule already used for the per-runner target limit, §17).

### Design, reusing what already exists
- **Targets are the existing SSH runner pool** (the owner's explicit choice over a separate "hosts" list) — same
  pinned host key, same encrypted credentials, same `check_runner_usable` gate (host configured, key confirmed, not
  mid-installation) as a scan already requires, checked up front for every selected runner before anything is
  created.
- **The LLM call is a new, independent one-shot path** (`app/services/llm.py::chat_once`), not Strix: it hits the
  profile's provider **natively** (Claude's `/v1/messages`, OpenAI/xAI/`custom`'s `/chat/completions`) with the
  profile's own id verbatim — never a LiteLLM/Strix routing prefix, confirmed against the real wire format with a
  logging stub server, same as `strix_model_id` (§16): `claude-sonnet-4-6` and a hypothetical `anthropic/claude-sonnet-4-6`
  would send identically. A non-streaming request, bounded time (120 s) and response size (500 KB).
- **Execution reuses the plain SSH primitive** (`SshConnection.run("sh -s", input=script, ...)`, the same `sh -s`
  stdin convention `services/ssh_runner.py::probe` already uses) — the script travels over stdin, never through a
  shell-interpolated command line, so no extra quoting layer can corrupt it. One host at a time, in the order
  submitted; `OpsRun.cancel_requested` is checked **between** hosts (an admin can stop the rest of the fleet; the
  host already running is not interrupted mid-script) — a cooperative flag, not a hard kill, consistent with every
  other cancel in this panel.
- **A deleted runner keeps its audit record**: `OpsRunHost.runner_name` is snapshotted at creation, so the fleet a
  task touched is still legible after a runner is renamed or removed (`runner_id` goes `NULL`, the name stays).
- **A gone LLM profile, a drafting failure, or the safety guard all fail the whole run explicitly** (`OpsRun.status
  = failed`, every still-queued host marked `failed`/`blocked` with a reason) — nothing is left silently "queued"
  forever.

### What this does not do
No target-scope restriction (unlike a scan, a task can run on any selected runner's whole filesystem); no resource
limit on the script itself; no retry of a failed host; no partial-script review (it is all-or-nothing per host); no
non-admin role can use it at all.

### Tests
Backend: `tests/test_ops.py` (33 tests) — permissions, validation before anything is created (profile ownership,
runner kind/readiness, empty goal, dedup, too many runners), the real end-to-end path on an **asyncssh-backed fake
host** (script drafted once, executed, exit code and merged stdout/stderr captured, a failing script vs. an
unreachable one, a deleted runner keeps its name), the safety guard (catches every pattern above including
`--no-preserve-root` with a non-root path, leaves ordinary admin scripts alone, blocks before touching the host — a
sentinel file proves the script never ran), a missing LLM profile failing the run instead of being silently skipped,
cancel (refused once a run is over; a worker-level test proves `cancel_requested` is actually honoured between two
hosts), and `chat_once` sending the bare model id to a mocked endpoint. Ten deliberate mutations of every safeguard
above, all caught. Frontend: `src/lib/ops.ts` + 6 vitest tests for the pure submit/cancel/label logic.

**Verified for real, end to end, not just in the test suite**: the full live stack (API + a real Celery worker, not
eager-mode) against a **real OpenSSH server** and an OpenAI-compatible stub endpoint standing in for the LLM — a
task created through the live API drafted a script from the stub, ran it over a real SSH connection, and returned
the real host's own `uname -a` and `id -un` output; a second task with a stub that returns `rm -rf /` was blocked
before touching the host (a sentinel `touch` in the same script never ran); the real browser UI showed both runs,
created a third through the form, and displayed its generated script; and an `operator` account was refused by the
live API (403), not just by a unit test.

### 18a. `STRIX_API_TYPE`: a custom gateway must not get the Responses route (HTTP 400 "This endpoint supports function tools only")

Seen in a real scan: the pre-scan check passed (it always posts to `/chat/completions`) and the agent loop then failed with
`openai.BadRequestError: 400 … This endpoint supports function tools only` from `agents/models/openai_responses.py`, i.e. from
`POST /responses`. Read in `strix-agent` 1.7.0 (not run against the user's gateway): Strix chooses the route in `resolve_api_type` —
`STRIX_API_TYPE` if set, otherwise **Responses when its LiteLLM catalog lists `/v1/responses` for the model name, chat completions
for everything else**. On the Responses route it sends its file tools as native `custom`-type tools; on the chat-completions route
`_custom_tool_as_function_tool` turns every tool into a plain function. A gateway that implements only function tools refuses the former.

The panel never set `STRIX_API_TYPE` (and the runner's environment is a whitelist, so the worker's own value never reached Strix). Now
`build_strix_env` (`services/llm.py::strix_api_type`) sets it for **both runners**: a `custom` profile gets `chat_completions`; other
profiles are left to Strix. The admin setting `STRIX_API_TYPE` = `auto` (default) | `responses` | `chat_completions` overrides it for every profile —
`responses` is for a gateway that really implements the whole Responses API. Tests: `tests/test_api_type.py`.

## 20. «Агент»: Codex CLI in an isolated container, driven from the panel

> **Since §23 the agent runs on `codex app-server`, not on `codex exec --json`.** What is said below about one `exec` process per turn, the
> `exec` event format and a process that lingers after a turn is superseded by §23; the isolation, the key handling, the gateway handling
> (function tools only, the compat proxy) and the image/health notes still apply as written.

A chat with an open-source coding agent ([Codex CLI](https://github.com/openai/codex), Apache-2.0) inside the panel: the operator
writes a task in plain words and sees every command, file edit and answer as a feed of steps, can add messages and stop it.
(Claude Code is not open source, so it is not an option; goose, opencode and Kimi CLI were considered — Codex was chosen.)

```
Browser ──REST + WS──> FastAPI ──> Celery (queue `agent`) ──HTTP, token──> agentd (container `agent`) ──> codex exec --json
                         │                │                                   own volume, own network, no secrets
                      Postgres  <── events stored one by one ──┘
```

### Why a separate container (the decision that shapes everything)

Codex is started with `--dangerously-bypass-approvals-and-sandbox`: the agent runs any command it likes, with no confirmation.
Codex's own sandbox (bubblewrap) cannot run inside a container, so **the container is the sandbox**. `agent` therefore gets:
no `docker.sock`, no `.env` (only `AGENT_TOKEN`), no route to Postgres and Redis (its own network `agentnet`; only the backend and
`agent-worker` are on both), a read-only root filesystem, no capabilities, `no-new-privileges`, a pid and memory limit, and a non-root user.
What it *can* do: write in its volume (`/data/agent/<session key>/work`), reach the internet (the model API, `git`, `pip`…).
The worker running the panel's own code never executes what the agent produces.

- The model's API key travels in the body of each turn (`panel → agent` over the internal network, not TLS) into the environment
  of the Codex process only; it is never written to disk by agentd. The key is also **redacted from every stored event** should the
  agent print it, and from agentd's stderr tail. Codex's own shell policy already hides variables named `*KEY*` from the commands.
- agentd authenticates every request with `AGENT_TOKEN` (constant-time compare; an empty token refuses everything).
  `AGENT_TOKEN` empty in the panel = the feature is off (`503`, nothing is sent anywhere).
- Session keys, thread ids and model names are validated against strict patterns before they reach a path or an argv, the prompt is
  passed after `--`, and `base_url` is written with TOML-safe quoting — none of the user's strings can become an option or a config key.

### Turns, threads and history

A *session* has a Codex thread id (`thread.started`). Every message is one *turn*: the first is `codex exec`, later ones
`codex exec resume <thread>` — Codex carries the conversation itself (checked: the second request to the model contained the first
turn's items). The session's `CODEX_HOME` and working directory live in the container's volume, so a thread survives a restart of agentd.
`agent_events` stores what Codex printed (long strings cut at 20 000 characters, at most 20 000 events per session) plus the panel's own
`user`, `turn.error`, `turn.stopped`. One turn at a time per session: the claim is a single conditional `UPDATE … WHERE status='idle'`.

- **Live view** — `WS /api/agent/sessions/{id}/ws` (first frame `{token, from}`) replays from a sequence number and then tails the table;
  a reconnect resumes from the next unseen event. Unlike a scan the socket stays open between turns.
- **Stop** — `SIGINT` to the agent's process group, `SIGKILL` after a grace period. The same happens when the worker hangs up mid-turn
  or a turn exceeds `AGENT_TURN_TIMEOUT_SECONDS` (default 30 minutes).
- **Liveness** — a command that prints nothing for minutes must not look like a lost worker: agentd sends `agentd.ping` every 15 s and the
  worker moves the session's heartbeat; `reap_stale_agent_turns` (beat, every minute) only ends a turn that stopped beating
  for `AGENT_STALE_SECONDS` (300) and stops its process.
- Turns run on their own Celery queue (`agent`, service `agent-worker`, `AGENT_CONCURRENCY`) so long chats cannot occupy scan slots.

### Who can use it

Admin and operator; viewers cannot. Sessions are private to their owner (an admin does not see other people's): the agent works with the
owner's LLM key and its history may contain anything the owner pasted. Profiles: OpenAI, Grok (xAI has a Responses API) and custom
(an OpenAI-compatible endpoint **that implements the Responses API**). **Claude profiles are refused with an explanation**: Codex speaks only the
Responses API (`wire_api = "responses"`), and Anthropic's native API is a different protocol.

### Checked against the real Codex (0.169.0) — and what was not

Run for real against a fake Responses endpoint: provider and model via `-c model_providers.<id>={…}`, the key read from the env var named in
`env_key` and sent as `Authorization: Bearer`, JSONL events (`thread.started`, `item.completed` with `agent_message` / `error`, `turn.started`,
`turn.completed` with usage, `turn.failed`), `exec resume <thread_id>`, and that **`CODEX_HOME` must exist** (agentd creates it).
**Not run:** a real model, and Codex with `--dangerously-bypass-approvals-and-sandbox` (blocked in the environment this was built in); the
exact shape of `command_execution` / `file_change` items comes from Codex's documented JSONL format, and the page keeps any item it does not
recognise as a generic step instead of dropping it. **Not built or run:** the `agent` image and the compose changes (no Docker daemon
there) — try `docker compose up --build`, check `GET /api/agent/health` in the panel, and send one message.

### Installing Codex in the image, and the health check (found on first real use)

`@openai/codex` is a JS launcher; the real binary is the separate package `@openai/codex-linux-x64` (or `-arm64`), an **optional** dependency that npm
silently skips when it cannot fetch it. The first image built that way started fine and then failed on the first message with
`Missing optional dependency @openai/codex-linux-x64. Reinstall Codex`. `agentd/Dockerfile` now installs the platform package by name with
`--include=optional` (checked here also with `NPM_CONFIG_OMIT=optional` set) and runs `codex --version`, so an unusable Codex fails the **build**.
`GET /health` of agentd also really starts it (`codex --version`, remembered for a minute) instead of only looking for a file on the PATH, and the
panel's badge on the «Агент» page shows the reason («Codex в контейнере агента не запускается: …»).

### Gateways that accept only function tools (`AGENT_TOOLS`)

A custom OpenAI-compatible gateway answered the agent with `400 This endpoint supports function tools only` (the same message Strix got, §18a). Run for real, Codex 0.169
against a request-logging endpoint sends, by default, `function` tools **plus** a built-in `web_search` tool and a `namespace` tool (`multi_agent_v1`). With
`-c web_search="disabled" --disable multi_agent` only `function` tools remain (`exec_command`, `write_stdin`, `request_user_input`, `view_image`, and the goal tools — switched off too with
`--disable goals`, like `image_generation`, to keep the list short for the simpler models behind gateways); the same flags work for `exec resume` and the history still carries over.
The panel sends `function_tools_only` with each turn: **on for `custom` and Grok profiles, off for OpenAI itself** (its API takes every tool type, and web search is useful there).
The setting `AGENT_TOOLS` = `auto` (default) | `functions` | `full` overrides this for all profiles (also editable in «Настройки»). Not verified: that a particular gateway then works end to
end — the request is function-only, but a model behind it still has to call tools through its Responses API.

### A gateway whose final stream event Codex cannot parse (the compat proxy)

Second real-gateway failure: the model answered, then `Reconnecting… 1/5 (stream disconnected before completion: failed to parse ResponseCompleted: missing field
`cached_tokens`)` — the answer appeared again and again and the turn finally failed. Reproduced with the real Codex 0.169 against an endpoint whose `response.completed`
carries `input_tokens_details` without `cached_tokens`: Codex cannot parse the final event, treats the finished stream as dropped and repeats the whole request (and the tokens are paid each time).
For gateways (every profile but OpenAI's own) agentd now gives Codex the address of a small **compat proxy** inside its own process (`/compat/<per-turn token>/…`, loopback only,
the token valid for that turn and then dropped) which forwards to the real endpoint and completes only the `usage` numbers of the final events (`cached_tokens`, `reasoning_tokens`,
`total_tokens`; a chat-style `prompt_tokens` is mapped); every other line passes through unchanged, and the key is whatever Codex sends — nothing is stored.
Checked with the real Codex through the proxy: one answer, `turn.completed`, no retries. The setting `AGENT_RESPONSE_FIX` («Исправлять ответы шлюза», on by default) switches it off.
Other gateway quirks can be handled in the same place.

### A Codex that reports the turn over and does not exit

Seen in a chat: the usage line («токенов: … на входе») was shown and the spinner kept turning — the turn stays `running` until the Codex process exits, and it did not exit at once.
agentd now treats Codex's own `turn.completed` / `turn.failed` as the end: it gives the process `AGENT_EXIT_GRACE_SECONDS` (10) to leave by itself, then ends it (SIGINT, then SIGKILL after the usual grace)
and reports exit code 0 with `lingered: true` — ending a finished process is not a failure. The page shows no spinner once the usage line closes the feed and says «завершается…» while it waits.

### Limits

The agent can do anything inside its container, including reading what you paste into the chat and using the model key it was given
for that turn; it cannot reach the panel's data. It has no access to your hosts unless you hand it SSH keys in the chat. There is no
per-session spending limit besides the turn time limit (Codex reports token counts, not cost, so the budget bar of scans does not apply).
The container's volume grows with the sessions' working directories: deleting a session removes its directory.

### Tests

`backend/tests/test_agent.py` (28): the API (roles, ownership, disabled without a token, Claude profile refused, one turn at a time,
history order and `resume`, what Codex is given — argv, key, no panel secrets —, failed and stopped turns, redaction, truncation, an unreachable
container, the stale-turn reaper, delete, WebSocket replay/resume/auth) against `tests/fake_codex.py` through the real agentd app; a real uvicorn
for the tests that need streaming (stop; a silent long command stays alive); agentd on its own (token, unsafe identifiers, timeout, TOML quoting).
Frontend: `src/lib/agentModel.test.ts` — how events become the feed.

## 21. «Настройки»: operating parameters in the panel, and a live limit of scans that run at once

Until now every number below lived in `.env` and needed a restart. They are now edited by an administrator on the **«Настройки»** page
(`GET/PUT /api/settings`, admin only) and take effect without restarting anything.

### Where a value comes from

`.env` (and the default in `app/core/config.py`) is the **baseline**. A change made in the panel is a row in `app_settings` (migration 0012)
and **wins over the baseline**; "сбросить" deletes the row. Setting a value equal to the baseline stores no row (so a later edit of `.env`
still counts for a parameter nobody really changed). Each row keeps who changed it and when.

The panel code already read `settings.<name>` at the moment it needed a value, so applying a change is setting that attribute
(`app/core/runtime_settings.py::apply`). Every process — the API, each worker child, beat — runs a small background thread that reloads the
table every 5 seconds (`start_refresher`: API lifespan, Celery `worker_process_init` and `beat_init`); the API process that took the `PUT`
applies it at once. So a change reaches all of them within seconds. A database that is away, or not yet migrated, leaves the current values
alone; a stored value that no longer passes validation, or for a parameter that no longer exists, is ignored.

### What is editable (27 parameters, `runtime_settings.SPECS`) and what is not

Scans (parallelism, timeout, grace on stop, stale threshold, log size, per-account limit), schedules, Strix (reasoning effort, API route,
preflight timeout, telemetry, update check, version and sandbox image to install), SSH, Операции, Агент, login throttling and session length.
Each has bounds checked on the server (and mirrored in the page), a unit, a hint, and its `.env` name.

**Not here, deliberately:** secrets and what the panel needs before it can read the database (`JWT_SECRET`, `SECRET_ENCRYPTION_KEY`, Postgres
and Redis addresses, `ADMIN_*`, `ORESHNIK_ENV`, `CORS_ORIGINS`), `AGENT_TOKEN` / `AGENT_URL`, paths and binaries, and the worker's process
counts (`SCAN_WORKER_SLOTS`, `AGENT_CONCURRENCY`). `strix_version` and `strix_sandbox_image` end up in shell commands on SSH hosts, so they
must match a strict pattern (no spaces, quotes, `;`, `$`, backticks). `ssh_check_interval_seconds` sets beat's schedule at startup, so it is
saved but marked "нужен перезапуск" (`docker compose restart beat`).

### The limit of scans that run at once (`SCAN_CONCURRENCY`)

It used to be the worker's `--concurrency`, i.e. fixed at start. Now the **panel** holds it: the worker starts with up to
`SCAN_WORKER_SLOTS` processes (default 16, `--autoscale`, so idle ones are not kept) as a ceiling, and `tasks._claim` — under a PostgreSQL
advisory lock, so two workers cannot take the last place — counts scans in `running` + `cancelling` and moves a queued scan to `running` only
while that count is below `scan_concurrency`. Otherwise the scan **stays `queued`** and its task is scheduled again in 5 s; cancelling it makes the
next try a no-op. Raising the limit lets waiting scans start within seconds; lowering it does **not** stop scans that are already running — it only
stops new ones from starting. The setting cannot exceed the ceiling (refused with a reason). Places are not handed out strictly in order of arrival.

### Tests and limits

`tests/test_app_settings.py` (34): every parameter exists on `settings` and its default is valid; secrets are not editable; shell metacharacters are
refused; the API (roles, listing, apply at once, reset, "equal to default stores nothing", all-or-nothing, round trips); another process picking a
change up by refreshing, invalid and unknown rows ignored, a broken database; the scan gate (waits at the limit, starts with a free place, raising
the limit without restart, lowering does not kill, cancelling still holds its place, finished ones do not, cancelled is skipped, re-scheduled in a real
worker). Frontend: `src/lib/settingsForm.test.ts` (11). **Not run:** the Celery signal hooks and the autoscaling worker themselves (no broker or Docker
in the environment where this was written) — after updating, change «Сканов одновременно» with scans queued and watch them start.

## 22. Saved model lists, and choosing any model of any provider for a scan

A profile used to hold one model; «Загрузить модели» only suggested ids while typing it. Now the list **is kept** and a scan picks from all of it.

- **Storage.** `llm_profiles.models` (JSON list, migration 0013) holds what the provider served at the last load (at most 1000 ids, trimmed, no repeats,
  each ≤ 200 characters). `model` stays the profile's default and is always selectable, first, even if it is not in the list (`services/llm.py::profile_models`).
- **Loading.** `POST /api/llm-profiles/models` with `profile_id` and neither a typed key nor a changed endpoint uses the stored ones **and stores the list at once**
  (`saved: true`) — it is not lost if the form is closed without «Сохранить». A list loaded with a typed key or another endpoint is only handed back; the form sends it
  with «Сохранить» (`models` on create/update). An untouched form sends no list, so editing a profile and retyping its key cannot wipe what it already has.
  `POST /api/llm-profiles/{id}/refresh-models` (the card's «Обновить модели») asks again with the stored key and replaces the list.
- **Choosing.** `ScanCreate`, `ScanContinue` and schedules take `llm_model` next to `llm_profile_id` (`scans.llm_model`, `schedules.llm_model`; null = follow the profile's default).
  The server checks the model is one of that profile's (default included) — otherwise `422` with a hint to load the list — and that a model comes with a profile. `ScanOut.llm_model`
  is what the scan runs with (the chosen one, else the profile's default). The worker passes it to `build_strix_env(profile, model)` → `STRIX_LLM`, with the provider's usual prefix.
- **The page.** One select, «Модель», lists every model of every profile the user has, grouped by profile («DeepSeek Flash · свой endpoint»), the profile's default marked; a search box
  appears once there are more than 12. The same control is in «Новый скан», the schedule form and «Продолжить». It sends no `llm_model` for a profile's own default, so such a scan keeps following the profile.
- **Continuing / schedules.** A continuation keeps the scan's model unless a model or another profile is chosen; a model that has left the profile's list (the provider stopped serving it and the list was refreshed)
  falls back to the profile's default. Changing a schedule's profile without choosing a model means that profile's default. A schedule whose pinned model is later removed from the list is refused when it fires, with the reason
  (the usual skipped-with-reason), rather than silently running something else.
- **«Агент» and «Операции» too.** An agent session (`agent_sessions.llm_model`) and an ops run (`ops_runs.llm_model`, migration 0014) take `llm_model` next to `llm_profile_id`, checked the same way
  (`services/llm.py::validate_model`, `422` with the hint). The session keeps its model for every turn: Codex gets it through `-c model=…` with the profile's endpoint (`endpoint_for(profile, model)`), and an ops script is
  drafted with it. The agent's choice is limited to the profiles Codex can use (OpenAI, Grok, custom — not Claude). The agent's model is checked only when the session is created, not on every message, so a model that
  left the list later is tried anyway and the provider's own error is shown in the chat.

Tests: `tests/test_llm_models.py` (30: list cleaning and storage, load/refresh, choosing for scans of different providers, refusals, what Strix is given, continuing, schedules), `src/lib/modelChoice.test.ts` (14).


## 23. «Агент» on `codex app-server`: a live chat, control of the turn, files, questions, scans

The agent used to be one `codex exec --json` process per message: only finished steps came back, nothing could be changed during a turn. It now talks to
**`codex app-server`** (the protocol Codex's own IDE extension uses; JSON-RPC, one JSON object per line over the process's stdio; `agentd/rpc.py`).
Everything below was **run against the real Codex 0.169.0** (a stand-in model endpoint that streams like a real one), not only read from its schema:
`thread/start|resume|fork|revert`, `turn/start|steer|interrupt`, `thread/compact/start`, `thread/shellCommand`, `review/start`, the deltas
(`item/agentMessage/delta`, `item/commandExecution/outputDelta`, …), `turn/diff/updated`, `turn/plan/updated`, `thread/tokenUsage/updated` and the server
requests `item/commandExecution|fileChange/requestApproval`, `item/tool/requestUserInput`. A scripted run of 18 such checks through the final agentd passes.

### Processes and sessions

One `codex app-server` per session, started with the first message, ended after `AGENT_IDLE_SECONDS` (900) without work or when
`AGENT_MAX_PROCESSES` (8) is reached (the longest idle one makes room; if none is idle the new turn is refused with a reason). The conversation lives in the session's
`CODEX_HOME`, so the next message `thread/resume`s it; a thread whose files are gone is replaced by a new one instead of refusing to answer. A changed endpoint, key or tool set
restarts the process; a changed effort, model or approval mode does not (they go with `turn/start`). The old process's last words never reach a new turn
(callbacks are bound to the process that made them — a bug found by a test).

### What the page gets (the events)

agentd turns Codex's notifications into a small stream of its own, and keeps the old `codex exec` item shapes so one feed renders both:

| | stored (`agent_events`, in order, never changes) | live (`agent_live`, one row per unfinished item, dropped when it finishes) |
|---|---|---|
| messages, reasoning | the finished item | the text so far (throttled to ~7 per second) |
| commands | the finished item: command as typed, output, exit code, status | the output so far |
| file changes | each file's own diff | — |
| the turn | `turn.started`, `turn.diff` (unified `git diff`), `turn.plan`, `turn.completed` (+ token usage of the turn) | the diff and plan so far |
| questions | `approval.requested`, `approval.resolved` | — |

The WebSocket sends `event` frames (numbered, a reconnect resumes from the next unseen one) and `live` / `live_end` frames. A live snapshot never replaces a finished
item on the page (the finished one arrives first). Streaming output of a command, not only its end, is visible while it runs.

### Controlling a turn

**Stop** (`turn/interrupt`; also when the panel hangs up or the time limit passes), **add an instruction** to the running turn (`turn/steer`; shown in the feed as "добавлено по ходу"),
**compact** the conversation, **review** the changes (`review/start`; needs a git repository, so every working folder is `git init`ed), **run a command yourself**
(`!ls`, `thread/shellCommand`, no model involved), **undo the last turn** (`thread/revert`) and **fork** the session. Slash commands in the box: `/compact`, `/review`, `/undo`, `/fork`.
Two of these are easy to misread: *undo removes the turn from the **conversation**, not from the **files*** (checked: Codex's revert is history only; the page says so), and a fork is a copy of the folder
and the conversation under a new key (the source session must be idle).

### Questions to the user («Подтверждения»)

Per session: **«Без вопросов»** (`approvalPolicy never`, as before) or **«Спрашивать»** (`untrusted`, with the sandbox mode `danger-full-access` because the container is the sandbox:
Codex then asks before commands it does not know to be safe and before every file change). The page shows a card with the command (as typed) or a pointer to the change; **Разрешить / Разрешить на сессию / Отклонить**.
The agent can also ask a question (`request_user_input`): options and/or a free answer, secret answers as a password field. An unanswered question is declined after `AGENT_APPROVAL_TIMEOUT_SECONDS`;
**Stop** cancels pending questions first (an interrupt would never reach a turn that waits). Requests the panel cannot answer (MCP elicitation, dynamic tools) are refused with an error; extra
**permissions are never granted**.

### Settings and the system prompt

Per session and editable: title, **its own instructions**, **reasoning effort** (minimal…xhigh), approval mode. *Found on the real Codex:* on `thread/resume` the new `developerInstructions` are
**ignored** (the old text stays in the thread; `baseInstructions` would replace Codex's whole built-in prompt). So instructions are given when a thread is created, and when you change them later agentd
puts "the instructions changed, ONLY these apply now (the previous are cancelled): …" in front of the next message — once; the panel remembers what the thread has (`thread_instructions`).

### The session's files

Folder browser, text preview (first 1 MB; binary files are described), download, upload (raw body, `AGENT_MAX_UPLOAD_MB`), `git clone --depth 1` of a plain **https** link (protocols are closed
on purpose: no `file://`, `ext::`, ssh; credentials in the URL are refused), the whole folder as a zip (without `.git`, `AGENT_MAX_ARCHIVE_MB`). Every path is resolved first and refused unless its **real** location
is inside the session's folder (the agent can make symbolic links; links are tested). The panel only checks that the session is yours and passes the request on (streams, not buffers).

### «Разобрать в агенте» (from a scan)

The button on a finished scan creates a session whose folder already holds `scan/README.md` (targets, mode, status, the scan's own instruction), `scan/findings.json` (the findings the panel stored),
and, if Strix left them, `report.md`, `vulnerabilities.json`, `findings.sarif`, `vulnerabilities/*.md` (limits: 5 MB per file, 30 MB, 200 files), plus a proposed first message that the user reads and sends.
An operator can do this only for their own scans, an admin for any; the model is chosen from the user's profiles as everywhere. The target's source code is not fetched automatically: clone it in «Файлы».

### Limits, honestly

- `app-server` is marked **experimental** by Codex; only 0.169.0 was checked, and the version is pinned in the image. A newer Codex may change the protocol.
- **Sessions of different users share one container and one Unix user.** The container hides the panel's secrets, but an agent could read another session's folder (and its parent process's environment, `AGENT_TOKEN` included).
  Treat the agent container as one trust domain: do not give «Агент» to people who must not see each other's work. (Separate containers per user would fix it; not built.)
- No disk quota per session besides the upload and archive limits; a runaway agent can fill the volume.
- Not run for real: the `agent` image and compose (no Docker daemon where this was written), a real model, and a real gateway with the full flow (the gateway cases are covered by the earlier checks in this document).

### Tests

`tests/test_agentd.py` (87) runs agentd as a real uvicorn against `tests/fake_appserver.py` (it speaks the recorded protocol): input checking, live text, what Codex is started and told, process reuse and restart,
approvals (command, file change, question; decline; late and wrong answers; timeout), steer, stop, a hung-up client, timeout, compact/shell/review/revert, fork, every file call incl. symlink escapes, limits, clone validation, health, the compat proxy.
`tests/test_agentd_normalize.py` (14) — the pure translation. `tests/test_agent.py` (57) — the panel's API through the worker to agentd: ownership of everything, history and live snapshots, the socket (live, late client, `live_end`),
questions, steering, stopping, settings and the instructions hand-over, actions, undo one after another, fork, files, scans, gateways. Frontend: `agentModel.test.ts` (28; feed, live overlay, questions, undo, composer commands),
`diff.test.ts` (11), `files.test.ts`.


## 24. «Агент»: context and spend, goals, web search

Three additions to the chat, each checked on the real Codex 0.169 first.

### How full the context is, and what the session has spent

`thread/tokenUsage/updated` carries the thread's running total, the last model call and `modelContextWindow`. After every turn agentd reports `context = {used: tokens of the last
call, window, window_known}` and the turn's own usage; the panel keeps `context_used`, `context_window` and the session's totals (`tokens_in`, `tokens_out`, summed turn by turn, so
they survive a restarted thread). The header shows a bar («контекст 76% · 196k из 258k»), the totals, and offers «сжать контекст» from 70%. **Caveat shown in the page:** for a model Codex does
not know (its warning "Model metadata … not found. Defaulting to fallback metadata" — every gateway model so far) the window is Codex's default (258 400), not the model's real one; the
percentage is then marked «≈». There is no cost in money: the panel has no prices for arbitrary gateway models.

### Goals — the agent working by itself

`thread/goal/set` gives the thread an objective (and a token budget). **Found by running it:** an *active* goal makes Codex start turns **by itself** — right after it is set and after every turn —
until the model marks it complete (`update_goal`), it is paused, or the budget is spent (`budgetLimited`; with a budget of 1000 tokens the run ended after 17 turns). Unattended, that is an agent spending tokens
with nobody watching. So:

- Codex's goal feature is **off** (`--disable goals`) unless the session has a goal. This also closes a hole that existed for OpenAI profiles: with the feature on by default the model was offered `create_goal` and could have started such a loop on its own.
- A goal is started only by the user («Поставить цель…» or `/goal текст`) and **must have a token budget** (10 000 … `AGENT_GOAL_MAX_TOKENS`, default ceiling 5M, editable in «Настройки»; the form proposes 500k). The turn's time limit applies to the whole run as well.
- The run is one stream: agentd keeps reading across Codex's self-started turns (each is stored and counted on its own), and ends when the goal stops being active, when no next turn starts within 20 s, on Stop, on the time limit, or when the panel hangs up.
- **Nobody reading = goal paused.** At the end of every such stream an active goal is set to `paused`; Stop pauses first (between two turns there is nothing to interrupt); a goal found active when a thread is resumed (the container died mid-run) is paused before anything else;
  a turn that starts with no reader is interrupted. Checked on the real Codex: a paused goal does not start turns, an ordinary message with a paused goal is one turn, and with the feature off the model is not offered the goal tools.
- The header shows the goal, its status and tokens used of the budget, with «продолжить» (sets the same goal going again) and «снять». The feed gets a line when the goal's status changes, not after every turn.

### Web search

Per session: **auto** (as before: on for OpenAI, off for gateways, which refuse the tool), **on**, **off**. "On" for a gateway is the user's call — a gateway that does not implement it answers 400, as documented above. A change restarts the session's process on the same thread.

### Also fixed here

The feed keyed a step by (user action, item id); a goal runs many Codex turns inside one action and their item ids repeat, so later steps replaced earlier ones. Steps are now keyed by Codex turn as well.

### Tests

`test_agentd.py` +18 (context, the guessed window, web search choices, a goal to completion, to its budget, Stop, resume, a hung-up reader, no next turn, clear, an active goal left behind, refusals), `test_agent.py` +11 (the same through the panel, totals, the budget ceiling, fork),
`agentModel.test.ts` +8. Real Codex through the final agentd: budget run to `budgetLimited`, nothing calling the model afterwards, Stop → paused, a paused goal stays quiet, resume, completion by the model, clear, goal tools absent when off.

## 25. Lights in the menu: something new in «Сканы» and «Агент»

A small light next to «Сканы» and «Агент» in the menu, lit when something happened since the user last opened that section and off once it is opened
(asked for instead of browser notifications: nothing to permit, works on plain HTTP).

- `GET /api/attention?scans_since=&agent_since=` counts, **for the user's own things only**: scans that reached a final state after `scans_since` (a scan the user cancelled is no news; `failed` and
  `findings` colour the light red / amber, otherwise green), agent sessions whose turn ended after `agent_since` and that are idle now, and — regardless of time — sessions where the agent is **waiting
  for the user's answer** (amber, pulsing; it stays lit until the question is answered, also after the page was opened and left).
- "Last opened" lives in the browser's `localStorage`, per account and section, as the **server's** time (the reply's `now`), so a wrong clock on the computer does not matter. The first time nothing is remembered,
  everything counts as seen: old scans do not light up. It is per browser, not per account: another device has its own lights.
- The page polls every 8 s (every 32 s in a background tab); the section being looked at never has a light. A tooltip says what is new («2 скана завершено, с находками: 1»).

Tests: `tests/test_attention.py` (8), `src/lib/attention.test.ts` (7); the off-on-open behaviour was also driven in a real browser against a stubbed API.

## 26. Tools for the agent: baked into the image, and self-installed at runtime

The agent container (§20) starts with nothing but Codex, Python and Node — enough to read and patch code, not enough for most pentesting tasks. Two layers add
to that, both inside the same sandbox the agent already runs in (no Docker socket, `read_only: true`, `cap_drop: [ALL]`, no DB/Redis/secrets, outbound internet
only): everything here is for **authorized** testing — the agent's own targets (environments the operator owns or has written permission to test), the same rule
that already governs Strix scans.

### Layer 1 — baked into the image (`agentd/Dockerfile`)

An apt-get install, Debian bookworm's main repo only, no backports, chosen for dependable availability rather than completeness:

- **dev**: `jq`, `ripgrep` (`rg`), `httpie` (`http`), `zip`/`unzip`, `sqlite3`, `dnsutils` (`dig`), `openssl`, `less`
- **network / recon**: `nmap`, `netcat-openbsd` (`nc`), `whois`, `traceroute`, `nikto`
- `sqlmap`, from PyPI, in its own venv at `/opt/agent-tools` (never the system Python — Debian's PEP 668 refuses a bare `pip install` there anyway — and never
  `agentd`'s own venv, so neither gets a dependency conflict from the other's needs)

An admin extends this list without touching the Dockerfile: `AGENT_EXTRA_PACKAGES` in `.env` (space-separated apt package names) flows straight into the same
`apt-get install` via a build arg (`docker-compose.yml`'s `agent.build.args`). It needs `docker compose build agent` to take effect — the image is immutable at
runtime, which is the point: nothing baked in can be tampered with from inside a session.

`cap_drop: [ALL]` means tools that want raw sockets degrade rather than fail outright — `nmap` without `CAP_NET_RAW` falls back to its unprivileged TCP-connect
scan (`-sT`) automatically; this is normal, documented `nmap` behaviour, not a bug here, and is enough for port discovery and most version/NSE checks.

### Layer 2 — the agent installs more at runtime, no root

Baked-in coverage will always be incomplete; the agent decides for itself, mid-session, what else a task needs and installs it — without root, since the
container never grants any. Three mechanisms, each already present in a stock Debian/Node image or added as one static binary, and each configured (in
`spawn_env()`, `agentd/main.py`) to write only inside the session's own writable home (`agent_data` volume — the one place the read-only-rootfs container can
write), never a system path uid 1000 cannot touch:

- `pip install --user ...` → `~/.local/bin` (pip's own default; no extra configuration needed)
- `npm install -g ...` → `~/.npm-global/bin`, via `NPM_CONFIG_PREFIX`
- `micromamba create -n tools -c conda-forge ...` / `micromamba install -n tools ...` → `~/.micromamba/envs/tools/bin`, via `MAMBA_ROOT_PREFIX`. `micromamba`
  itself is one static binary baked into the image (`/usr/local/bin/micromamba`, mamba-org's own bootstrap URL) — no conda, no root, no system package
  manager involved in fetching it.

All four self-install/baked bin directories are prepended to `PATH`, so anything placed there is callable by bare name right away — no second step, no shell
restart. Two places set this, for a reason that only showed up once real commands were traced: `codex`'s shell tool runs every command as `/bin/bash -lc
"..."` — a **login** shell — and Debian's stock `/etc/profile` unconditionally **overwrites** `PATH` for login shells before it sources `/etc/profile.d/*.sh`.
So `spawn_env()` sets `PATH`/`MAMBA_ROOT_PREFIX`/`NPM_CONFIG_PREFIX` as the spawned process's own environment (covers anything that is not a login shell), and
`agentd/Dockerfile` also bakes a fixed `/etc/profile.d/agent-tools.sh` that re-adds the same directories after that reset (covers the login shell, which is
where it actually matters in practice). A PATH entry for a directory that does not exist yet is harmless — it is only resolved at the moment a command is
looked up — so this works correctly before the agent has installed anything, and immediately after.

### What this does not do

No listening sockets, no inbound ports, no reverse-shell catchers: the agent container has no published ports and sits on the isolated `agentnet` network, so
nothing outside Docker could reach a socket it opened anyway — raising that would need its own, separate decision about exposing a port, which has not been
made. This section is tools the agent runs against things *it* connects out to, not things that connect in to it.

### Tests

`tests/test_agentd.py::test_self_installed_tools_land_in_the_sessions_own_folder_and_are_on_the_path` (the fake `app-server` reports its own `PATH`,
`MAMBA_ROOT_PREFIX` and `NPM_CONFIG_PREFIX`, checked against the session's actual home directory). The apt/`sqlmap`/`micromamba` install steps themselves are
not covered by this suite — there is no Docker daemon in the environment this was written in — and should be sanity-checked with a real `docker compose build
agent` before this ships.

## 27. A listener for the agent: a port the panel proxies in, on request

§20 made `agent` deliberately unreachable from anywhere but the panel: no published port, no route but `agentnet`. That is right for commands the
agent *initiates* (recon, `curl`, `git`), but it also means a capability some pentesting tasks genuinely need — catching an inbound connection,
e.g. a reverse shell from an authorized target, or a webhook callback — had nowhere to land. This section is that one exception, made as
narrow as the rest of the design allows it to be: **authorized testing only**, same as everywhere else in this panel.

```
outside world ──TCP:4444──> backend (published, host network) ──TCP:4444──> agent container (agentnet, no published port)
                                    ↑ opened only by an explicit API call, for one session, one port, at a time
```

- **A fixed range, not a free-for-all.** `AGENT_LISTENER_PORTS` (`.env`, e.g. `4444-4450`) is both the range `docker-compose.yml` publishes on
  `backend`'s own host ports *and* the range the API itself will open — the same variable, so they cannot drift apart. Empty = the feature is off
  (`503` from every listener call); the ports stay published either way (nothing is bound to them until opened), so turning the feature off again
  needs only the `.env` change, no rebuild.
- **Opened per session, not per deploy.** `POST /api/agent/sessions/{id}/listener {port}` picks one port out of the range and starts an
  `asyncio` TCP proxy in the **backend** process (`app/services/agent_listener.py`) between `0.0.0.0:<port>` (the host-published side) and
  `agent:<port>` (the same port, reached over `agentnet` by the container's own Docker-DNS name — the one host this module will ever proxy to).
  It is a dumb byte pipe both ways; it does not look inside the connection. `DELETE` closes it; `GET` reports it (also folded into
  `AgentSessionOut.listener_port`, shown in the session's settings in the panel). One port per session at a time — opening a second closes the first.
- **The agent, not the panel, listens.** The proxy does not create a socket inside the container: the agent itself has to bind that port
  (`nc -lp 4444`, a short Python server, whatever the task calls for) with an ordinary shell command, same as any other command it runs. A
  connection to the published port before that happens is accepted and closed at once (nothing to proxy to yet) — not left hanging.
- **Ephemeral and self-closing.** All state is an in-memory `dict` in the one `uvicorn` process (`docker-compose.yml` runs no `--workers`); it does
  not survive a restart, which is intentional — a listener is a short-lived, explicit action, not standing infrastructure. A listener nobody has
  sent a byte through for `AGENT_LISTENER_IDLE_SECONDS` (default 1800) closes itself (`sweep_loop`, started in `app/main.py`'s lifespan); the
  session's own deletion also closes its listener; shutdown closes all of them.
- **What this still does not do.** The agent container itself remains exactly as isolated as §20 describes — this adds one proxied port on the
  *panel's* host, not a published port on `agent`. Raw sockets, SYN scans and the like remain unavailable regardless (`cap_drop: [ALL]`, §26);
  a listener only ever receives ordinary TCP, same as any service.

### Tests

`tests/test_agent_listener.py` (24): `port_range()` parsing, the actual byte-for-byte TCP pipe (against a stand-in "inside" server — two
different loopback addresses stand in for the panel's host and the container, since one address cannot bind the same port twice), the
nothing-listening-yet case, range/conflict/idempotency rules, the idle sweep, and the HTTP surface (ownership, 404 for someone else's session,
422 outside the range, closing on session delete).
