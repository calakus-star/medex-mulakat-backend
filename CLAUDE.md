# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Project overview

MedeX Mülakat Sistemi ("MedeX Interview System") — an AI-driven interview platform for clinical-research / life-sciences / tech / business roles. Candidates are invited (or apply generally), take an AI-conducted interview (text or voice depending on level), and admins review scored reports and standardized CVs. UI text, prompts, and code comments are primarily in Turkish.

Two independent apps, deployed separately to Railway:
- `backend/` — single-file FastAPI app (`main.py`, ~2750 lines)
- `frontend/` — single-page Create React App (no TypeScript despite the devDependency)

There is no root package.json/build tool tying them together — work in each directory independently.

## Commands

### Backend (`backend/`)
```
pip install -r requirements.txt
uvicorn main:app --reload --port 8000       # dev server
uvicorn main:app --host 0.0.0.0 --port $PORT # production (Railway startCommand)
```
No test suite, linter, or formatter is configured for the backend. There is no `.env`/`.env.example` — all config comes from environment variables read via `os.getenv` at the top of `main.py` (see Config section below); for local dev, export them in the shell before running uvicorn.

One-off data migration: `python migrate_sqlite_to_postgres.py` (moves local `medex_mulakat.db` SQLite data into a Railway Postgres instance pointed at by `DATABASE_URL`).

### Frontend (`frontend/`)
```
npm install
npm start     # dev server, http://localhost:3000, expects backend at REACT_APP_API_URL or http://localhost:8000
npm run build # production build (CI=false so ESLint warnings don't fail the build)
npm run serve # serve the build directory, used as Railway startCommand
```
No test suite is configured (`react-scripts test` exists but is unused/no tests present). No `.eslintrc` beyond CRA defaults.

### Running a single test
Not applicable — neither package has tests set up.

## Architecture

### Database
SQLite (`medex_mulakat.db`, local dev) or PostgreSQL (Railway `DATABASE_URL`, production) chosen automatically by `USE_POSTGRES = bool(DATABASE_URL)`. `PostgresConnection` in `main.py` is a thin adapter that lets the rest of the code write SQLite-style queries (`?` placeholders, `sqlite3.Row`-like access) against Postgres too — `_pg_sql()` rewrites `?`→`%s` and a couple of SQLite-only SQL idioms. **When writing new queries, always use `?` placeholders and go through `get_db()`/`_pg_sql()` — don't write dialect-specific SQL directly.**

`init_db()` runs on import (module-level `init_db()` call), creates tables if missing, applies an ad-hoc list of `ALTER TABLE ... ADD COLUMN` migrations (the `migrations` list — this is the migration mechanism; there is no Alembic/versioned migration tool), and seeds/force-updates a large hardcoded catalog of default `positions` (name, category, role criteria with weights) on every startup. Admin-created custom positions are untouched by the forced-update block; only positions whose `name` matches an entry in the hardcoded `defaults` list get overwritten.

Core tables: `positions`, `candidates`, `interviews` (one row per candidate **per level**, keyed by `candidate_id + level`), `snapshots` (webcam captures), `ai_usage_logs` (per-call token/cost accounting).

### The three interview "levels"
A candidate's `level` (1, 2, or 3) determines both interview modality and depth, configured in `LEVEL_CONFIG`:
- **Level 1** — text chat, ~10 min, CV optional, uses Claude (`/api/interview/start`, `/api/interview/chat`), rendered by `frontend/src/pages/Interview.js`.
- **Level 2** — fully voice, ~20 min, CV required, uses **OpenAI Realtime API exclusively** (`/api/realtime/session`, `/api/realtime/sync`, `/api/realtime/report`), rendered by `frontend/src/pages/RealtimeInterview.js`. Claude/Anthropic must never be called for Level 2 — this is an explicit task requirement enforced throughout the backend (see `log_ai_provider()` calls tagged `"blocked"` at every point where a Level 2 code path would otherwise reach Claude, e.g. in `report_violation`). Don't add an Anthropic call into any Level 2 path.
- **Level 3** — adaptive, ~30+ min uncapped, CV required, still uses the Claude text-chat flow (same endpoints as Level 1) but with a senior/direct tone and no fixed time ceiling.

`DEPTH_TIER_CONFIG` (`kisa`/`standart`/`derin`) is an independent multiplier on top of a level's base minutes/question-count (`get_effective_level_config`), not a separate modality.

Both AI flows converge on the same shape of "end of interview" payload: a report block (`---RAPOR---...---RAPORSON---`), a standardized-CV block (`---STANDARTCV---...---STANDARTCVSON---`), a numeric score, and a hire recommendation, parsed by `finalize_interview()` (text flow) or the `/api/realtime/report` handler (voice flow). Recommendations below a 20% score threshold are coerced to `"Değerlendirilemedi"` (could not be evaluated) rather than an automatic reject — this is a deliberate business rule, not a bug.

### AI prompting
`get_system_prompt()` builds the Level 1/3 Claude system prompt per-candidate (position criteria, CV excerpt, admin's free-text `ai_note` treated as a binding instruction the model must act on, language instructions for interview vs. report language, level/depth-tier tone and pacing). It's wrapped via `cached_system()` using Anthropic prompt caching (`cache_control: ephemeral`) since the same system text is resent every turn. `build_l2_realtime_instructions()` builds the equivalent instructions for the OpenAI Realtime session. Position role criteria (name/weight/description triples) live in `criteria_json` on the `positions` table and are shared by both prompt builders via `get_position()`.

The "TAM FORMAT" report body (the section list from `**Aday:**` through `---STANDARTCVSON---`) is a single source, `REPORT_BODY_SECTIONS` (a `(key, levels, literal_text)` list) rendered by `build_report_body(level, ctx)`. Both `get_system_prompt()` (L1/L3) and `/api/realtime/report`'s `report_prompt` (L2) read from it — as of Faz D1, `/api/realtime/report` filters by the real `candidate_level` (2 or 3), so Level 3 now gets the same rich body Level 2 does. `get_system_prompt()`, however, pins its lookup to a **hardcoded `1`** regardless of the candidate's actual level — its `ctx` doesn't supply the L2/L3-shared keys (e.g. `ai_note_report_field`), and its only remaining level≠1 caller (`report_violation`'s Level 3 branch, now a rarely-hit edge case since Level 3 candidates use the voice/Realtime path exclusively) would otherwise `KeyError` on the now-`{2,3}` sections. **Caveat for future work:** if a Level-1-only report section is ever added, this hardcoded `1` needs re-examining — right now it silently means "L1 and L3-via-text-endpoint always render identically," which was already true before this pin, so nothing broke, but the pin makes that equivalence permanent rather than incidental.

Interview conversation state per (candidate, level) is stored as a JSON message array in `interviews.messages`; `build_compact_memory()` compresses this into a short Q/A digest reinjected each turn instead of resending full history, to bound token cost.

Both interview endpoints enforce a two-phase close: once time/question thresholds are met, the model is first told to ask a single closing question (`closing_asked` flag) rather than ending immediately, and only finalizes on the following turn. A candidate explicitly asking to stop (`[ADAY_CIKIS_TALEBI]` tag) short-circuits straight to `finalize_interview()`. The backend also strips any `[MÜLAKATBİTTİ]`/report block the model emits early if the minimum question count hasn't been satisfied yet — treat this as a hard safety net, not something to relax casually.

### Auth
Two separate JWT-based auth flows using the same `verify_token`/`create_token` helpers, distinguished by a `role` claim (`"admin"` vs `"candidate"`) — `verify_admin` is just `verify_token` plus a role check. No refresh tokens; `create_token(..., days=7)` default expiry. Candidate accounts are created by admins (or via `/api/apply` general application, auto-approved into a `candidates` row with generated username/password) and emailed their credentials via Resend (`send_invite_email`).

### Cost/usage tracking
Every AI call (Claude and OpenAI) should be logged through `record_ai_usage()` (writes `ai_usage_logs` + increments `interviews.total_*_tokens`) so the admin panel can show per-candidate token/cost breakdowns. `AI_PRICING_PER_1M` holds hand-maintained approximate per-model rates for the cost estimate shown in the panel, including separate cached-input rates (`input_cached`/`audio_input_cached`) — not a real invoice source; update it when models/pricing change.

`realtime_events` (Faz D1) stores raw Realtime events (`session.created`, speech start/stop, `response.done` usage, defensive truncation) for observability — no retention/cleanup policy exists yet. A long Level 3 session can generate hundreds of rows; once volume becomes a real concern, this table will need a retention policy (e.g. delete rows older than ~90 days).

### Frontend
Plain React (CRA), no state-management library, no CSS framework/component-library — global inline-style design tokens live in `frontend/src/components/Layout.js` (`colors`, `Header`, `Card`, `Input`, `Button`, `Alert`, etc.) and are reused across `AdminDashboard`, `PositionManager`, and `WalkinPanel`. Auth state is plain `localStorage` (`candidate_token`/`candidate_info`, admin equivalent) read directly in each page, no context provider. Routing (`App.js`) is flat — `PositionManager` and `WalkinPanel` are not routes; they're tab panels rendered conditionally inside `AdminDashboard.js`. `API_URL` (from `REACT_APP_API_URL`, exported from `App.js`) is the single source of truth for the backend base URL — import it rather than hardcoding `localhost:8000`.

### PDF / reporting
Admin-facing interview reports are rendered to PDF server-side with ReportLab (`_make_report_pdf`, `/api/admin/interviews/{candidate_id}/pdf`); `nixpacks.toml` installs `dejavu_fonts` specifically so Turkish characters render correctly in generated PDFs.

## Çalışma kuralları

- Ses/mikrofon/WebRTC ve Level 2 Realtime hattına ayrı ve açık onay olmadan dokunulmaz.
- "Yap" denmeden hiçbir değişiklik uygulanmaz; o ana kadar sadece not tutulur.
- "Yap" dendiğinde birikmiş tüm maddeler tek geçişte uygulanır, tek tek değil.
- Değişiklikler uygulanmadan önce diff gösterilir.
- Yanıtlar madde madde, kısa cümlelerle, Türkçe yazılır.
- Yanıt sonunda yönlendirici soru veya öneri yazılmaz.
