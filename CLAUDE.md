# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Project Overview

Bilibili membership store (show.bilibili.com) ticket-grabbing tool — a local FastAPI web app for automated ticket purchasing. Chinese-language UI and codebase.

## Commands

```bash
uv sync                    # Install dependencies (requires Python 3.13+)
uv run main.py             # Start local server at http://127.0.0.1:8000
uv run python -m unittest tests/test_regressions.py
```

There is no linter configuration in this project.

## Architecture

**Request flow**: Browser → FastAPI (`app/server.py`) → `Grabber` state machine (`app/grabber.py`) → Bilibili API (`app/bili/ticket.py`)

**Core components**:

- `app/server.py` — FastAPI app with REST endpoints + WebSocket for real-time log streaming. Holds global `AppState` (config, grabber instance, connected WS clients). Static files served from `web/` directory.
- `app/grabber.py` — `Grabber` class is the ticket-grabbing state machine. Runs as an asyncio task. Loop: wait for start_time → resolve buyers/price → retry `prepare_order` → `create_order` cycle. Classifies each response via `ResultKind` enum to decide next action (success/stop, risk handling, retry, fatal).
- `app/config.py` — Pydantic `AppConfig` model. Persisted to `config.json` (path overridable via `TICKET_BUY_CONFIG` env var). File permissions set to 0o600 after write.
- `app/bili/client.py` — `BiliClient` wraps `httpx.AsyncClient` with Bilibili-specific headers, cookie parsing, and `bili_ticket` HMAC generation. Mobile User-Agent used for better compatibility.
- `app/bili/ticket.py` — Bilibili ticket API calls: `get_project` (shows/screens/SKUs), `get_buyers`, `prepare_order` (get token), `create_order`.
- `app/bili/risk.py` — `RiskHandler` orchestrates the `-352` risk-control flow: gaia-vgate register → captcha solve → validate → set `x-bili-gaia-vtoken` cookie.
- `app/bili/captcha.py` — `CaptchaSolver` Protocol with two implementations: `ManualSolver` (asyncio Future-based, user completes geetest in browser) and `RrocrSolver` (third-party API). `build_solver()` factory selects by config.
- `app/bili/errors.py` — Error code → `ResultKind` classification (SUCCESS/RISK/RETRY/SOLD_OUT/FATAL).
- `app/notify.py` — Push notifications (Bark / ServerChan) on success.
- `web/` — Static frontend: `index.html`, `app.js`, `style.css`. Communicates with backend via fetch API and WebSocket.

**Key design patterns**:

- `Grabber.on_event` callback broadcasts JSON events (logs + status) to all WebSocket clients via `AppState.broadcast`. Events have `type` field: `"log"` or `"status"`.
- `ManualSolver` uses an `asyncio.Future` to bridge the async grabber loop with user interaction from the web frontend (poll `/api/captcha` GET → render geetest → submit via POST).
- `BiliClient` is created per-request for API endpoints, but the grabber reuses one instance across its lifetime (closed in `finally`).
- `ConfigUpdate` model uses `exclude_none=True` to implement partial-update merge semantics.

## Configuration

Config file: `config.json` in project root (gitignored). Example: `config.example.json`. Key fields: `cookie`, `project_id`, `screen_id`, `sku_id`, `buyer_ids`, `count`, `start_time` (ISO format, empty = immediate), `interval_ms` (min 100), `max_attempts`, `captcha_mode` (`manual` | `rrocr`).

## Language

Code comments, docstrings, log messages, and UI text are all in Chinese. Maintain this convention.
