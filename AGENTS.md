# AGENTS.md — Base44 dev environment notes

## Stack
- Python 3.12 / FastAPI app (`api/main.py` → `api.main:app`).
- SQLite database at `data/asa.db` (no external DB service needed).
- Server-rendered Jinja templates for the booking portal + admin panel; static site in `public/`.

## Running
- `docker compose -f docker-compose.base44.yml up -d --build` brings up the app on host port 3000 (container 8080).
- Uvicorn runs with `--reload --reload-dir /srv/api` so edits to `api/` hot-reload.
- Dependencies install from `requirements.txt` on every container start (bind-mounted source, no baked image).

## Boot behavior
- `APP_ENV=development` → `SECRET_KEY` is auto-generated (ephemeral), `NOTIFY_PROVIDER=file` and `SMS_PROVIDER=file` are allowed. No external credentials required to boot.
- Migrations run automatically on startup via the FastAPI lifespan (`api.db.migrate()`).
- The first admin user can be seeded with `ADMIN_USER=owner ADMIN_PASSWORD=... python -m ops.seed` (optional).

## Health
- Liveness: `GET /api/healthz` (200, simple).
- Readiness: `GET /api/readyz` (checks DB connectivity). Used by the compose healthcheck.

## Key routes
- `/` — static marketing site (from `public/`).
- `/booking` — patient booking portal (server-rendered).
- `/admin` — staff/doctor panel (requires login).
- `/api/docs` — OpenAPI Swagger UI (dev only).

## Tests
- `make test` → `python3 -m pytest -q` (168 tests).
- `make lint` → `ruff check && ruff format --check`.

## Notes
- The repo's own `docker-compose.yml` builds a production image (bakes source via `COPY`) and is NOT used for dev.
- SQLite WAL mode is enabled; the `app_data` named volume persists the DB across restarts.
