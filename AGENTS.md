# AGENTS.md — Base44 development notes

## What this app is

Persian-language physiotherapy clinic website + booking system (FastAPI + SQLite).
Entry point: `api.main:app` (uvicorn). Port 8080 inside the container, mapped to host 3000.

## Running in the sandbox

`docker compose -f docker-compose.base44.yml up -d --build` starts the app from cloned
source with `uvicorn --reload`. Migrations run automatically on startup (the FastAPI
lifespan calls `db.migrate()`). To create an admin for a fresh data volume, run
`docker compose -f docker-compose.base44.yml run --rm -e ADMIN_USER=owner -e ADMIN_PASSWORD='<choose-a-password>' app python -m ops.seed`.

## No external credentials needed for dev

`APP_ENV=development` auto-generates `SECRET_KEY` and allows `NOTIFY_PROVIDER=file` and
`SMS_PROVIDER=file` (both rejected in production). No secrets are required to boot.
Telegram/SMS/AI integrations are optional and disabled by default in dev.

## Health checks

- `/api/readyz` — full readiness (migrations applied, policies checked)
- `/api/healthz` — liveness

## Key paths

- `api/` — all application code (FastAPI routes, logic, templates)
- `api/templates/` — Jinja2 templates for booking system + admin panel
- `migrations/` — forward-only SQL migrations (auto-applied on startup)
- `public/` — pre-built static site (served at `/`)
- `static/` — JS/CSS assets for the booking app
- `data/` — SQLite DB, uploads, notification outbox (persisted in `app_data` volume)
- `ops/seed.py` — creates the first admin user

## Admin panel

`/admin` — create a local owner account with the seeding command above when working from a fresh data volume.
