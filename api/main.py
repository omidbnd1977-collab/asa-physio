"""Layer 02/05/08/09/10/12 — the HTTP surface.

Middleware order (outermost first):
    trace -> security headers -> body limit -> breaker + per-IP rate limit -> route
"""

from __future__ import annotations

import datetime as dt
import hashlib
import json
import mimetypes
import pathlib
import secrets
import time
from collections.abc import Callable
from contextlib import asynccontextmanager
from typing import Any

from fastapi import Body, FastAPI, Request, Response
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, RedirectResponse
from pydantic import ValidationError
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.middleware.cors import CORSMiddleware

from . import (
    ai,
    auth,
    budget,
    cache,
    db,
    metrics,
    notify,
    pages,
    ratelimit,
    reminders,
    repo,
    routes_booking,
    routes_doctor,
    schemas,
    tracking,
)
from .config import settings
from .errors import (
    AppError,
    BadRequest,
    Forbidden,
    IdempotencyMismatch,
    NotFound,
    PayloadTooLarge,
)
from .logging_ import fingerprint, info, new_trace_id, setup_logging, trace_id_var, warn
from .policies import SYSTEM, Actor

START = time.time()
TEMPLATES = pathlib.Path(__file__).resolve().parent / "templates"


# --------------------------------------------------------------------------
# lifespan
# --------------------------------------------------------------------------
def _bootstrap_admin() -> None:
    """Create the opt-in first-boot owner. Idempotent: a restart that finds the user
    already there does nothing."""
    user = settings.BOOTSTRAP_ADMIN_USER
    password = settings.BOOTSTRAP_ADMIN_PASSWORD
    if not user or not password:
        return
    if repo.count(SYSTEM, "admin_users", where="username = ?", params=[user]):
        return
    auth.create_user(user, password, role="owner")
    warn("bootstrap.admin_created", user=user)


@asynccontextmanager
async def lifespan(app: FastAPI):  # type: ignore[no-untyped-def]
    setup_logging()
    applied = db.migrate()
    triggers = db.assert_no_business_triggers()
    missing = __import__("api.policies", fromlist=["x"]).missing_policies(db.tables())
    if missing:
        raise RuntimeError(f"tables without an access policy: {missing}")
    _bootstrap_admin()
    info(
        "app.start",
        env=settings.ENV,
        version=settings.VERSION,
        migrations_applied=applied,
        triggers=triggers,
    )
    yield
    db.close_conn()
    info("app.stop")


app = FastAPI(
    title="ASA Physio API",
    version=settings.VERSION,
    lifespan=lifespan,
    docs_url="/api/docs" if not settings.is_prod else None,
    redoc_url=None,
    openapi_url="/api/openapi.json" if not settings.is_prod else None,
)


# --------------------------------------------------------------------------
# helpers
# --------------------------------------------------------------------------
def client_ip(request: Request) -> str:
    if settings.TRUST_PROXY:
        xff = request.headers.get("x-forwarded-for", "")
        if xff:
            return xff.split(",")[0].strip()[:45]
    return (request.client.host if request.client else "0.0.0.0")[:45]  # noqa: S104


def current_actor(request: Request) -> Actor:
    return auth.resolve_session(request.cookies.get(auth.SESSION_COOKIE))


def staff(request: Request) -> Actor:
    return auth.require(current_actor(request), "staff")


def owner(request: Request) -> Actor:
    return auth.require(current_actor(request), "owner")


def json_error(exc: AppError, trace_id: str) -> JSONResponse:
    headers = {"X-Trace-Id": trace_id}
    if exc.retry_after is not None:
        headers["Retry-After"] = str(exc.retry_after)
    return JSONResponse(exc.payload(trace_id), status_code=exc.status, headers=headers)


# --------------------------------------------------------------------------
# middleware
# --------------------------------------------------------------------------
class TraceMiddleware(BaseHTTPMiddleware):
    async def dispatch(self, request: Request, call_next: Callable):  # type: ignore[no-untyped-def]
        tid = request.headers.get("x-request-id", "")[:32] or new_trace_id()
        token = trace_id_var.set(tid)
        t0 = time.perf_counter()
        try:
            response = await call_next(request)
        except AppError as exc:
            if exc.status >= 500:
                ratelimit.record_server_error()
            response = json_error(exc, tid)
        except Exception as exc:  # noqa: BLE001 — layer 12: nothing escapes unseen
            tracking.capture(
                exc,
                trace_id=tid,
                where=f"{request.method} {request.url.path}",
                ip=client_ip(request),
            )
            ratelimit.record_server_error()
            response = json_error(AppError(), tid)
        dur = (time.perf_counter() - t0) * 1000
        response.headers["X-Trace-Id"] = tid
        response.headers["Server-Timing"] = f"app;dur={dur:.1f}"
        if request.url.path.startswith(("/api", "/admin")):
            info(
                "http.request",
                method=request.method,
                path=request.url.path,
                status=response.status_code,
                dur_ms=round(dur, 1),
                ip=fingerprint(client_ip(request)),
            )
        trace_id_var.reset(token)
        return response


class SecurityHeadersMiddleware(BaseHTTPMiddleware):
    CSP = (
        "default-src 'self'; "
        "img-src 'self' data:; "
        "font-src 'self' data:; "
        "style-src 'self' 'unsafe-inline'; "
        "script-src 'self' 'unsafe-inline'; "
        "connect-src 'self'; "
        "form-action 'self'; frame-ancestors 'none'; base-uri 'none'; object-src 'none'"
    )

    async def dispatch(self, request: Request, call_next: Callable):  # type: ignore[no-untyped-def]
        r = await call_next(request)
        r.headers.setdefault("Content-Security-Policy", self.CSP)
        r.headers.setdefault("X-Content-Type-Options", "nosniff")
        r.headers.setdefault("X-Frame-Options", "DENY")
        r.headers.setdefault("Referrer-Policy", "strict-origin-when-cross-origin")
        r.headers.setdefault(
            "Permissions-Policy", "geolocation=(), microphone=(), camera=(), payment=()"
        )
        r.headers.setdefault("Cross-Origin-Opener-Policy", "same-origin")
        if settings.is_prod:
            r.headers.setdefault(
                "Strict-Transport-Security", "max-age=31536000; includeSubDomains; preload"
            )
        return r


class BodyLimitMiddleware(BaseHTTPMiddleware):
    async def dispatch(self, request: Request, call_next: Callable):  # type: ignore[no-untyped-def]
        cl = request.headers.get("content-length")
        if cl and cl.isdigit() and int(cl) > settings.MAX_BODY_BYTES:
            raise PayloadTooLarge()
        return await call_next(request)


class GateMiddleware(BaseHTTPMiddleware):
    """Layer 09 — breaker + a coarse per-IP ceiling on every public path."""

    async def dispatch(self, request: Request, call_next: Callable):  # type: ignore[no-untyped-def]
        p = request.url.path
        if p.startswith("/api") and p not in ("/api/healthz", "/api/readyz"):
            ratelimit.breaker_check()
            ratelimit.enforce(
                ratelimit.ip_bucket("global", client_ip(request)), settings.RL_IP_PER_MIN, 60
            )
        return await call_next(request)


app.add_middleware(GateMiddleware)
app.add_middleware(BodyLimitMiddleware)
app.add_middleware(SecurityHeadersMiddleware)
app.add_middleware(TraceMiddleware)
if settings.ALLOWED_ORIGINS:
    app.add_middleware(
        CORSMiddleware,
        allow_origins=settings.ALLOWED_ORIGINS,
        allow_credentials=True,
        allow_methods=["GET", "POST", "PATCH"],
        allow_headers=["content-type", "idempotency-key", "x-csrf-token"],
    )


@app.exception_handler(AppError)
async def _app_error(request: Request, exc: AppError):  # type: ignore[no-untyped-def]
    if exc.status >= 500:
        ratelimit.record_server_error()
    return json_error(exc, trace_id_var.get())


@app.exception_handler(ValidationError)
async def _pydantic_error(request: Request, exc: ValidationError):  # type: ignore[no-untyped-def]
    fields = {
        ".".join(str(x) for x in e["loc"]): e["msg"].replace("Value error, ", "")
        for e in exc.errors()
    }
    from .errors import ValidationFailed

    return json_error(ValidationFailed(fields=fields), trace_id_var.get())


# --------------------------------------------------------------------------
# health
# --------------------------------------------------------------------------
@app.get("/api/healthz")
def healthz() -> dict[str, Any]:
    return {
        "ok": True,
        "version": settings.VERSION,
        "env": settings.ENV,
        "uptime_s": round(time.time() - START, 1),
    }


@app.get("/api/readyz")
def readyz() -> JSONResponse:
    checks: dict[str, Any] = {}
    ok = True
    try:
        n = repo.count(SYSTEM, "schema_migrations")
        checks["db"] = {"ok": True, "migrations": n}
    except Exception as exc:  # noqa: BLE001
        ok = False
        checks["db"] = {"ok": False, "error": type(exc).__name__}
    is_open, until, reason = ratelimit.breaker_state()
    checks["breaker"] = {
        "open": is_open,
        "reason": reason,
        "reopens_in_s": max(0, int(until - time.time())) if is_open else 0,
    }
    checks["notify_provider"] = settings.NOTIFY_PROVIDER
    return JSONResponse({"ok": ok, **checks}, status_code=200 if ok else 503)


# --------------------------------------------------------------------------
# public: bookings  (layer 02 — validated, typed errors, idempotent)
# --------------------------------------------------------------------------
def _idempotent(key: str, scope: str, body_hash: str) -> dict[str, Any] | None:
    rows = repo.select(
        SYSTEM, "idempotency_keys", where="key = ? AND scope = ?", params=[key, scope], limit=1
    )
    if not rows:
        return None
    if rows[0]["request_hash"] != body_hash:
        raise IdempotencyMismatch()
    return {"status": rows[0]["status_code"], "body": json.loads(rows[0]["response"])}


@app.post("/api/bookings", status_code=201)
async def create_booking(request: Request, payload: dict[str, Any] = Body(default={})) -> Response:
    ip = client_ip(request)
    raw = json.dumps(payload, ensure_ascii=False, sort_keys=True)
    body_hash = hashlib.sha256(raw.encode()).hexdigest()
    idem = (request.headers.get("idempotency-key") or "").strip()[:128]
    if not idem:
        raise BadRequest("هدر Idempotency-Key الزامی است.", fields={"Idempotency-Key": "missing"})
    if len(idem) < 8:
        raise BadRequest("Idempotency-Key نامعتبر است.")

    replay = _idempotent(idem, "bookings", body_hash)
    if replay:
        return JSONResponse(
            replay["body"], status_code=replay["status"], headers={"Idempotent-Replay": "true"}
        )

    data = schemas.BookingIn.model_validate(payload)

    # honeypot: accept and drop, so bots get no signal
    if data.website.strip():
        warn("booking.honeypot", ip=fingerprint(ip))
        return JSONResponse(
            {
                "public_id": "0" * 12,
                "status": "new",
                "delivered": True,
                "created_at": dt.datetime.now(dt.UTC).strftime("%Y-%m-%dT%H:%M:%SZ"),
            },
            status_code=201,
        )

    # layer 09 — per IP and per phone, on top of the global ceiling
    ratelimit.enforce(
        ratelimit.ip_bucket("booking", ip),
        settings.RL_BOOKING_IP_PER_HOUR,
        3600,
        message="تعداد درخواست‌های شما زیاد است. لطفاً بعداً تلاش کنید یا تلفنی تماس بگیرید.",
    )
    ratelimit.enforce(
        ratelimit.id_bucket("booking", data.phone),
        settings.RL_BOOKING_PHONE_PER_DAY,
        86400,
        message="برای این شماره امروز درخواست ثبت شده است. همکاران ما تماس می‌گیرند.",
    )

    public_id = secrets.token_hex(6)
    with repo.tx():
        bid = repo.insert(
            SYSTEM,
            "bookings",
            {
                "public_id": public_id,
                "name": data.name,
                "phone": data.phone,
                "service": data.service,
                "note": data.note,
                "ip_fp": fingerprint(ip),
                "ua_fp": fingerprint(request.headers.get("user-agent", "")),
            },
        )
    metrics.record("booking_submitted", booking_id=bid)

    row = repo.select(SYSTEM, "bookings", where="id = ?", params=[bid], limit=1)[0]
    delivered = notify.deliver(row)
    metrics.record("booking_delivered" if delivered else "booking_delivery_failed", booking_id=bid)
    cache.purge("admin:bookings")

    body = {
        "public_id": public_id,
        "status": "new",
        "delivered": delivered,
        "created_at": row["created_at"],
    }
    status = 201 if delivered else 202
    with repo.tx():
        repo.insert(
            SYSTEM,
            "idempotency_keys",
            {
                "key": idem,
                "scope": "bookings",
                "request_hash": body_hash,
                "status_code": status,
                "response": json.dumps(body, ensure_ascii=False),
            },
        )
    info("booking.created", booking_id=bid, delivered=delivered, service=data.service)
    return JSONResponse(body, status_code=status)


@app.post("/api/outcome", status_code=204)
async def track_outcome(request: Request, payload: dict[str, Any] = Body(default={})) -> Response:
    ratelimit.enforce(ratelimit.ip_bucket("outcome", client_ip(request)), 30, 3600)
    ev = schemas.OutcomeIn.model_validate(payload)
    metrics.record(ev.kind)
    return Response(status_code=204)


# --------------------------------------------------------------------------
# admin API
# --------------------------------------------------------------------------
@app.post("/api/admin/login")
async def login(
    request: Request, response: Response, payload: dict[str, Any] = Body(default={})
) -> dict[str, Any]:
    ip = client_ip(request)
    ratelimit.enforce(
        ratelimit.ip_bucket("login", ip),
        settings.RL_LOGIN_IP_PER_15MIN,
        900,
        message="تلاش‌های ناموفق زیاد بود. چند دقیقه صبر کنید.",
    )
    data = schemas.LoginIn.model_validate(payload)
    actor = auth.verify_login(data.username, data.password)
    sid, expires = auth.start_session(actor, ip)
    response.set_cookie(
        auth.SESSION_COOKIE,
        sid,
        httponly=True,
        samesite="strict",
        secure=settings.is_prod,
        max_age=settings.SESSION_TTL_H * 3600,
        path="/",
    )
    return {
        "role": actor.role,
        "csrf": auth.csrf_token(sid),
        "expires_at": expires.strftime("%Y-%m-%dT%H:%M:%SZ"),
    }


@app.post("/api/admin/logout", status_code=204)
async def logout(request: Request, response: Response) -> Response:
    sid = request.cookies.get(auth.SESSION_COOKIE)
    if sid:
        auth.revoke_session(sid)
    response.delete_cookie(auth.SESSION_COOKIE, path="/")
    return Response(status_code=204)


@app.get("/api/admin/me")
async def me(request: Request) -> dict[str, Any]:
    actor = staff(request)
    return {
        "role": actor.role,
        "user_id": actor.user_id,
        "csrf": auth.csrf_token(actor.session_id or ""),
    }


@app.get("/api/admin/bookings")
async def list_bookings(
    request: Request, response: Response, status: str = "", page: int = 1, per_page: int = 20
) -> Any:
    actor = staff(request)
    page = max(1, min(page, 500))
    per_page = max(1, min(per_page, 100))
    where, params = "1=1", []
    if status:
        if status not in ("new", "contacted", "scheduled", "done", "spam"):
            raise BadRequest("وضعیت نامعتبر است.")
        where, params = "status = ?", [status]

    key = f"admin:bookings:{status}:{page}:{per_page}"

    def build() -> dict[str, Any]:
        rows = repo.select(
            actor,
            "bookings",
            where=where,
            params=params,
            order_by="created_at desc",
            limit=per_page,
            offset=(page - 1) * per_page,
        )
        ids = [r["id"] for r in rows]
        dmap: dict[int, str] = {}
        if ids:
            marks = ",".join("?" for _ in ids)
            for d in repo.select(actor, "deliveries", where=f"booking_id IN ({marks})", params=ids):
                dmap[d["booking_id"]] = d["status"]
        for r in rows:
            r["delivery"] = dmap.get(r["id"], "pending")
            r.pop("ip_fp", None)
            r.pop("ua_fp", None)
        return {
            "items": rows,
            "page": page,
            "per_page": per_page,
            "total": repo.count(actor, "bookings", where=where, params=params),
        }

    data = cache.memo(key, settings.API_CACHE_TTL_S, build)
    tag = cache.etag_for(data)
    if request.headers.get("if-none-match") == tag:
        return Response(
            status_code=304, headers={"ETag": tag, "Cache-Control": "private, max-age=10"}
        )
    response.headers["ETag"] = tag
    response.headers["Cache-Control"] = "private, max-age=10"
    return data


@app.patch("/api/admin/bookings/{public_id}")
async def set_status(
    public_id: str, request: Request, payload: dict[str, Any] = Body(default={})
) -> dict[str, Any]:
    actor = staff(request)
    auth.check_csrf(actor.session_id or "", request.headers.get("x-csrf-token"))
    data = schemas.BookingStatusIn.model_validate(payload)
    now = dt.datetime.now(dt.UTC).strftime("%Y-%m-%dT%H:%M:%SZ")
    with repo.tx():
        n = repo.update(
            actor,
            "bookings",
            {"status": data.status, "updated_at": now},
            where="public_id = ?",
            params=[public_id[:12]],
        )
    if not n:
        raise NotFound("این درخواست پیدا نشد.")
    cache.purge("admin:bookings")
    return {"public_id": public_id, "status": data.status}


@app.post("/api/admin/bookings/{public_id}/redeliver")
async def redeliver(public_id: str, request: Request) -> dict[str, Any]:
    actor = staff(request)
    auth.check_csrf(actor.session_id or "", request.headers.get("x-csrf-token"))
    rows = repo.select(actor, "bookings", where="public_id = ?", params=[public_id[:12]], limit=1)
    if not rows:
        raise NotFound("این درخواست پیدا نشد.")
    ratelimit.enforce(
        ratelimit.id_bucket("redeliver", public_id),
        3,
        600,
        message="ارسال مجدد بیش از حد انجام شده است.",
    )
    ok = notify.deliver(rows[0], attempts=2)
    metrics.record(
        "booking_delivered" if ok else "booking_delivery_failed", booking_id=rows[0]["id"]
    )
    cache.purge("admin:bookings")
    return {"delivered": ok}


@app.post("/api/admin/bookings/{public_id}/summary")
async def summarize(public_id: str, request: Request) -> dict[str, Any]:
    actor = staff(request)
    auth.check_csrf(actor.session_id or "", request.headers.get("x-csrf-token"))
    rows = repo.select(actor, "bookings", where="public_id = ?", params=[public_id[:12]], limit=1)
    if not rows:
        raise NotFound("این درخواست پیدا نشد.")
    return ai.summarize_booking(actor, rows[0])  # type: ignore[return-value]


@app.get("/api/admin/metrics")
async def admin_metrics(request: Request, days: int = 30) -> dict[str, Any]:
    staff(request)
    days = max(1, min(days, 365))
    return cache.memo(
        f"metrics:{days}",
        20,
        lambda: {"outcome": metrics.outcome(days), "daily": metrics.daily(14)},
    )


@app.get("/api/admin/costs")
async def admin_costs(request: Request) -> dict[str, Any]:
    owner(request)
    return {"month": budget.month_key(), "resources": budget.summary()}


@app.get("/api/admin/ops")
async def admin_ops(request: Request) -> dict[str, Any]:
    owner(request)
    is_open, until, reason = ratelimit.breaker_state()
    bdir = settings.BACKUP_DIR
    backups = sorted(bdir.glob("asa-*.db.gz"), reverse=True) if bdir.exists() else []
    return {
        "version": settings.VERSION,
        "env": settings.ENV,
        "uptime_s": round(time.time() - START, 1),
        "breaker": {
            "open": is_open,
            "reason": reason,
            "reopens_in_s": max(0, int(until - time.time())) if is_open else 0,
        },
        "cache": cache.stats(),
        "db": {
            "path": str(settings.DB_PATH),
            "size_kb": round(settings.DB_PATH.stat().st_size / 1024, 1)
            if settings.DB_PATH.exists()
            else 0,
            "tables": len(db.tables()),
            "triggers": db.assert_no_business_triggers(),
        },
        "backups": {"count": len(backups), "latest": backups[0].name if backups else None},
        "notify_provider": settings.NOTIFY_PROVIDER,
        "limits": {
            "ip_per_min": settings.RL_IP_PER_MIN,
            "booking_ip_per_hour": settings.RL_BOOKING_IP_PER_HOUR,
            "booking_phone_per_day": settings.RL_BOOKING_PHONE_PER_DAY,
            "ai_user_per_hour": settings.RL_AI_USER_PER_HOUR,
            "ai_global_per_day": settings.RL_AI_GLOBAL_PER_DAY,
        },
    }


@app.post("/api/admin/breaker/close", status_code=204)
async def close_breaker(request: Request) -> Response:
    actor = owner(request)
    auth.check_csrf(actor.session_id or "", request.headers.get("x-csrf-token"))
    ratelimit.breaker_close()
    return Response(status_code=204)


app.include_router(routes_booking.router)
app.include_router(routes_doctor.router)


# --------------------------------------------------------------------------
# inbound SMS  (the patient replies "1" to confirm attendance)
# --------------------------------------------------------------------------
@app.post("/api/sms/inbound")
async def sms_inbound(
    request: Request, payload: dict[str, Any] = Body(default={})
) -> dict[str, Any]:
    """Called by the SMS gateway. Protected by a shared secret, not a session."""
    secret = settings.SMS_INBOUND_SECRET
    if secret:
        given = request.headers.get("x-sms-secret", "")
        if not secrets.compare_digest(given, secret):
            raise Forbidden("توکن نامعتبر است.")
    elif settings.is_prod:
        raise Forbidden("SMS_INBOUND_SECRET is not configured.")
    ratelimit.enforce(ratelimit.ip_bucket("smsin", client_ip(request)), 120, 60)
    data = schemas.SmsInboundIn.model_validate(payload)
    return reminders.handle_inbound(data.phone, data.body)  # type: ignore[return-value]


@app.post("/api/admin/reminders/run")
async def run_reminders(request: Request) -> dict[str, int]:
    """Manual trigger, so staff never have to wait for the ticker."""
    actor = auth.require(current_actor(request), "staff")
    auth.check_csrf(actor.session_id or "", request.headers.get("x-csrf-token"))
    return reminders.run_once()


# --------------------------------------------------------------------------
# patient portal pages
# --------------------------------------------------------------------------
PORTAL_ROUTES = {
    "/booking": "entry",
    "/booking/register": "register",
    "/booking/login": "login",
    "/booking/reserve": "reserve",
    "/booking/done": "done",
    "/booking/messages": "messages",
}


def _portal(name: str) -> HTMLResponse:
    return HTMLResponse(pages.page(name), headers={"Cache-Control": "no-store"})


@app.api_route("/booking", methods=["GET", "HEAD"], include_in_schema=False)
async def portal_entry() -> HTMLResponse:
    return _portal("entry")


@app.api_route("/booking/{step}", methods=["GET", "HEAD"], include_in_schema=False)
async def portal_step(step: str) -> Response:
    name = PORTAL_ROUTES.get(f"/booking/{step}")
    if name is None:
        return RedirectResponse("/booking", status_code=302)
    return _portal(name)


# --------------------------------------------------------------------------
# admin UI + static site (layer 10)
# --------------------------------------------------------------------------
@app.api_route("/admin", methods=["GET", "HEAD"], response_class=HTMLResponse)
async def admin_page() -> HTMLResponse:
    html = (TEMPLATES / "admin.html").read_text(encoding="utf-8")
    return HTMLResponse(html, headers={"Cache-Control": "no-store"})


def _serve_static(rel: str) -> Response:
    root = settings.STATIC_DIR.resolve()
    target = (root / rel).resolve()
    if not str(target).startswith(str(root)) or not target.is_file():
        raise NotFound()
    raw = target.read_bytes()
    etag = '"' + hashlib.sha256(raw).hexdigest()[:24] + '"'
    fingerprinted = bool(
        __import__("re").search(r"\.[0-9a-f]{10}\.(css|js|jpg|png|svg|woff2)$", target.name)
    )
    cc = cache.policy_for_path(
        rel, fingerprinted, settings.STATIC_IMMUTABLE_MAX_AGE, settings.HTML_MAX_AGE
    )
    ctype = mimetypes.guess_type(target.name)[0] or "application/octet-stream"
    return FileResponse(target, media_type=ctype, headers={"ETag": etag, "Cache-Control": cc})


@app.api_route("/{full_path:path}", methods=["GET", "HEAD"], include_in_schema=False)
async def static_site(full_path: str, request: Request) -> Response:
    if full_path.startswith("api/"):
        raise NotFound()
    if full_path in ("", "/"):
        full_path = "index.html"
    if not settings.STATIC_DIR.exists():
        return HTMLResponse(
            "<h1>static build missing — run: python3 build_static.py</h1>", status_code=503
        )
    candidate = settings.STATIC_DIR / full_path
    if candidate.is_dir():
        full_path = full_path.rstrip("/") + "/index.html"
    elif not candidate.exists() and "." not in full_path.rsplit("/", 1)[-1]:
        full_path = full_path + ".html"
    try:
        resp = _serve_static(full_path)
    except NotFound:
        if (settings.STATIC_DIR / "404.html").exists():
            return _serve_static("404.html")
        raise
    if request.headers.get("if-none-match") == resp.headers.get("ETag"):
        return Response(
            status_code=304,
            headers={"ETag": resp.headers["ETag"], "Cache-Control": resp.headers["Cache-Control"]},
        )
    return resp


@app.api_route("/", methods=["GET", "HEAD"], include_in_schema=False)
async def root(request: Request) -> Response:
    if not settings.STATIC_DIR.exists():
        return RedirectResponse("/admin")
    return await static_site("index.html", request)
