# AGENTS.md

This file provides guidance to Codex (Codex.ai/code) when working with code in this repository.

## Project overview

This is a local Bilibili 会员购 (show.bilibili.com) ticket-grabbing tool. It is a FastAPI web application with a vanilla HTML/JS/CSS frontend. Users configure a show, ticket tier, buyers, and a start time; the backend then polls Bilibili's order APIs until an order is created or limits are hit.

## Common commands

This project uses [uv](https://docs.astral.sh/uv/) for Python dependency and environment management (Python 3.13+ required).

- Install dependencies: `uv sync`
- Run the application: `uv run main.py`
  - Prints the local URL and admin token on startup.
  - Default address: `http://127.0.0.1:8000`.
- Run all tests: `uv run pytest`
- Run a single test: `uv run pytest tests/test_regressions.py::ClassName::method_name`
- Run tests verbosely: `uv run pytest -v`

There is no project-local lint command. The GitHub Actions workflow in `.github/workflows/python-app.yml` runs `uv sync --locked`, `uv run --with flake8 flake8 .`, and `uv run pytest` on Python 3.13.

## Configuration

- Runtime configuration is stored in `config.json` at the repository root (gitignored).
- `config.example.json` documents the schema.
- The config path can be overridden with the environment variable `TICKET_BUY_CONFIG`.
- Admin token priority: `TICKET_BUY_ADMIN_TOKEN` environment variable > `server.admin_token` in config > auto-generated temporary token printed at startup.

## Architecture

### Backend (`app/`)

- `app/server.py` — FastAPI app, route layer, and WebSocket log broadcaster.
  - Holds global mutable state in `server.state` (config, active `Grabber`, connected WebSocket clients).
  - Delegates admin-token and origin checks to `AdminAuth`; `server.state.config.server.allowed_origins` can whitelist additional origins.
  - Serves the frontend from `web/`.
- `app/admin_auth.py` — Admin token authentication via cookie, `X-Admin-Token`, or `Authorization: Bearer`, origin checks, and failed-login throttling. Reads the current config through a provider on each check.
- `app/api_schemas.py` — API request models, the config-update field whitelist, and the `SENSITIVE_PLACEHOLDER` sentinel shared with `config_service.py`.
- `app/config_service.py` — Public config redaction (using that sentinel), sensitive update merging, and grab-start validation. Functions receive config explicitly and do not import the server.
- `app/grabber.py` — Core ticket-grabbing state machine (`Grabber`).
  - Separates per-run reset, prewarm preparation, and the order loop; `_run` owns cancellation, error handling, and client cleanup.
  - Lifecycle: prewarm wait → refresh `bili_ticket` → resolve buyers/price → wait for start time → attempt order loop (with optional return-ticket monitoring).
  - Each attempt calls `ticket.prepare_order` then `ticket.create_order`.
  - Result codes are classified by `app/bili/errors.py` into `SUCCESS`, `RETRY`, `RATE_LIMIT`, `SOLD_OUT`, `RISK`, or `FATAL`.
  - On `RISK`, delegates to `RiskHandler` and retries with the returned `gaia_vtoken`.
  - Emits events through a callback; `server.py` broadcasts these events over WebSocket.
- `app/grab_models.py` — Grab phases, status, targets, and attempt/burst result dataclasses; original imports from `app.grabber` remain supported.
- `app/grab_strategy.py` — `BalancedStrategyController`, burst limits, and burst-phase queries such as `in_return_burst()`, independent of the server and HTTP client.
- `app/grab_reporting.py` — `RunRecorder` owns JSONL output and elapsed time. `build_report` provides the same schema for empty and active reports; the exposed fields are the explicit `_REPORT_FIELDS` whitelist, so `GrabberStatus` fields not listed there stay internal. File write failures do not interrupt grabbing.
- `app/time_utils.py` — Shared local-time parsing; callers retain their own error messages and exception types.
- `app/config.py` — Pydantic v2 models (`AppConfig`, `NotifyConfig`, `ServerConfig`) and JSON persistence.
- `app/notify.py` — Sends payment reminders through Bark, Server酱, or macOS Messages.

### Bilibili integration (`app/bili/`)

- `app/bili/client.py` — `BiliClient`, a thin `httpx.AsyncClient` wrapper that parses the raw cookie string, sets mobile UA / Referer headers, and can generate `bili_ticket`.
- `app/bili/auth.py` — QR-code login flow and cookie validation via the nav endpoint.
- `app/bili/ticket.py` — Dataclasses and API calls for project info, buyer list, `prepare_order`, and `create_order`.
- `app/bili/risk.py` — `RiskHandler` for the `gaia-vgate` flow: register → solve Geetest → validate → obtain `grisk_id` / `gaia_vtoken`.
- `app/bili/captcha.py` — `CaptchaSolver` protocol with two implementations:
  - `ManualSolver`: exposes `gt`/`challenge` to the web UI and waits for the user to submit the result.
  - `RrocrSolver`: third-party service integration (requires `rrocr_token`).
- `app/bili/errors.py` — Error-code descriptions and classification.

### Frontend (`web/`)

Static HTML/JS/CSS files served by FastAPI. The frontend communicates with the backend via REST and WebSocket (`/ws`) for real-time logs.

### Entry point

- `main.py` loads config, ensures/validates the admin token, and starts uvicorn with `app.server:app`.

## Testing

Tests live in `tests/test_regressions.py` and `tests/test_backend_boundaries.py` and use `unittest` (run by pytest). They heavily mock Bilibili API calls, `BiliClient`, and `asyncio.sleep` to test the grabber state machine and server routes without network access. Boundary tests cover report consistency, dynamic auth config, cancellation, repeated runs, telemetry failures, and shared time parsing.

When modifying the grabber or server, prefer adding regression tests that patch `app.grabber.ticket.*`, `app.grabber.BiliClient`, and `app.grabber.notify.send_all` rather than relying on live Bilibili calls.

## Security notes

- `config.json` is written with `0o600` permissions and excluded from the hatch wheel build.
- `rrocr_token`, cookie, and notify URLs/keys are redacted from public config responses and from error logs in `notify.py`.
- The admin token is required for all `/api/*` routes and `/ws`; configure a strong token in production rather than relying on the generated temporary one.
