"""One test file per claim. If a layer is marked done, a test here proves it."""

from __future__ import annotations

import json
import uuid

import pytest

from tests.conftest import booking_payload


def idem() -> dict[str, str]:
    return {"Idempotency-Key": uuid.uuid4().hex}


# ======================================================================
# Layer 02 — API: validation at the boundary, typed errors, idempotency
# ======================================================================
class TestLayer02Api:
    def test_happy_path_creates_and_delivers(self, client):
        r = client.post("/api/bookings", json=booking_payload(), headers=idem())
        assert r.status_code == 201, r.text
        body = r.json()
        assert body["delivered"] is True
        assert len(body["public_id"]) == 12

    def test_missing_idempotency_key_is_rejected(self, client):
        r = client.post("/api/bookings", json=booking_payload())
        assert r.status_code == 400
        assert r.json()["error"]["code"] == "bad_request"

    def test_replay_returns_same_booking(self, client):
        h = idem()
        a = client.post("/api/bookings", json=booking_payload(), headers=h)
        b = client.post("/api/bookings", json=booking_payload(), headers=h)
        assert a.json()["public_id"] == b.json()["public_id"]
        assert b.headers.get("Idempotent-Replay") == "true"
        from api import repo
        from api.policies import SYSTEM

        assert repo.count(SYSTEM, "bookings") == 1

    def test_same_key_different_body_is_a_conflict(self, client):
        h = idem()
        client.post("/api/bookings", json=booking_payload(), headers=h)
        r = client.post("/api/bookings", json=booking_payload(name="کس دیگر"), headers=h)
        assert r.status_code == 409
        assert r.json()["error"]["code"] == "idempotency_key_reuse"

    @pytest.mark.parametrize(
        "payload,field",
        [
            ({"name": "a", "phone": "09121234567"}, "name"),
            ({"name": "زهرا محمدی", "phone": "12345"}, "phone"),
            ({"name": "<script>x</script>", "phone": "09121234567"}, "name"),
        ],
    )
    def test_invalid_input_is_typed_and_names_the_field(self, client, payload, field):
        r = client.post("/api/bookings", json=payload, headers=idem())
        assert r.status_code == 422
        err = r.json()["error"]
        assert err["type"] == "validation_error"
        assert any(field in k for k in err.get("fields", {}))

    def test_error_envelope_always_carries_a_trace_id(self, client):
        r = client.post("/api/bookings", json={"name": "a"}, headers=idem())
        assert r.json()["error"]["trace_id"]
        assert r.headers["X-Trace-Id"]

    def test_unknown_field_is_refused(self, client):
        r = client.post("/api/bookings", json=booking_payload(is_admin=True), headers=idem())
        assert r.status_code == 422

    def test_phone_is_normalised(self, client):
        client.post("/api/bookings", json=booking_payload(phone="+98 912 ۱۲۳ ۴۵۶۷"), headers=idem())
        from api import repo
        from api.policies import SYSTEM

        assert repo.select(SYSTEM, "bookings")[0]["phone"] == "09121234567"

    def test_oversized_body_is_rejected(self, client):
        r = client.post("/api/bookings", json=booking_payload(note="x" * 40000), headers=idem())
        assert r.status_code in (413, 422)


# ======================================================================
# Layer 03 — database: constraints, forward-only migrations, no triggers
# ======================================================================
class TestLayer03Db:
    def test_every_migration_is_recorded(self):
        from api import db, repo
        from api.policies import SYSTEM

        files = len(list(db.MIGRATIONS_DIR.glob("[0-9][0-9][0-9][0-9]_*.sql")))
        assert repo.count(SYSTEM, "schema_migrations") == files

    def test_migrations_are_idempotent(self):
        from api import db

        assert db.migrate() == []

    def test_changing_an_applied_migration_is_refused(self, tmp_path):
        import hashlib

        from api import db

        conn = db.get_conn()
        row = conn.execute(
            "SELECT version, checksum FROM schema_migrations ORDER BY version LIMIT 1"
        ).fetchone()
        conn.execute(
            "UPDATE schema_migrations SET checksum = ? WHERE version = ?",
            (hashlib.sha256(b"tampered").hexdigest(), row["version"]),
        )
        with pytest.raises(RuntimeError, match="forward only"):
            db.migrate()
        conn.execute(
            "UPDATE schema_migrations SET checksum = ? WHERE version = ?",
            (row["checksum"], row["version"]),
        )

    def test_no_triggers_exist(self):
        from api import db

        assert db.assert_no_business_triggers() == []

    def test_constraints_are_enforced_by_the_database(self):
        import sqlite3

        from api import db

        conn = db.get_conn()
        with pytest.raises(sqlite3.IntegrityError):  # phone shape
            conn.execute(
                "INSERT INTO bookings(public_id,name,phone,service) "
                "VALUES ('aaaaaaaaaaaa','x y','123','laser')"
            )
        with pytest.raises(sqlite3.IntegrityError):  # service enum
            conn.execute(
                "INSERT INTO bookings(public_id,name,phone,service) "
                "VALUES ('bbbbbbbbbbbb','x y','09121234567','massage')"
            )
        with pytest.raises(sqlite3.IntegrityError):  # status enum
            conn.execute(
                "INSERT INTO bookings(public_id,name,phone,service,status) "
                "VALUES ('cccccccccccc','x y','09121234567','laser','weird')"
            )

    def test_foreign_keys_are_on(self):
        import sqlite3

        from api import db

        with pytest.raises(sqlite3.IntegrityError):
            db.get_conn().execute(
                "INSERT INTO deliveries(booking_id, provider) VALUES (999999,'file')"
            )


# ======================================================================
# Layer 04 — authorization is enforced in the data layer
# ======================================================================
class TestLayer04Authz:
    def test_every_table_has_an_explicit_policy(self):
        from api import db
        from api.policies import missing_policies

        assert missing_policies(db.tables()) == []

    def test_anonymous_cannot_read_bookings_through_the_repo(self):
        from api import repo
        from api.errors import Forbidden
        from api.policies import ANON

        with pytest.raises(Forbidden):
            repo.select(ANON, "bookings")

    def test_a_table_without_a_policy_is_unreachable(self):
        from api import db, repo
        from api.errors import PolicyMissing
        from api.policies import SYSTEM

        db.get_conn().execute("CREATE TABLE IF NOT EXISTS rogue (id INTEGER PRIMARY KEY)")
        try:
            with pytest.raises(PolicyMissing):
                repo.select(SYSTEM, "rogue")
        finally:
            db.get_conn().execute("DROP TABLE rogue")

    def test_password_hash_is_not_selectable_by_a_human_role(self):
        from api import auth, repo
        from api.policies import Actor

        auth.create_user("pol_owner", "another-long-password", role="owner")
        rows = repo.select(Actor("owner", 1), "admin_users", where="username='pol_owner'")
        assert rows and "password_hash" not in rows[0]

    def test_staff_only_sees_their_own_sessions(self):
        from api import auth, repo
        from api.policies import Actor

        uid_a = auth.create_user("sess_a", "password-aaaaaaaaaa")
        uid_b = auth.create_user("sess_b", "password-bbbbbbbbbb")
        auth.start_session(Actor("staff", uid_a), "1.1.1.1")
        auth.start_session(Actor("staff", uid_b), "2.2.2.2")
        mine = repo.select(Actor("staff", uid_a), "sessions")
        assert mine and all(s["user_id"] == uid_a for s in mine)

    def test_ui_hiding_is_not_the_control(self, client):
        # no cookie at all -> the API itself refuses, not the page
        assert client.get("/api/admin/bookings").status_code == 401
        assert client.patch("/api/admin/bookings/abc", json={"status": "done"}).status_code == 401

    def test_staff_cannot_reach_owner_endpoints(self, client):
        from api import auth

        auth.create_user("plain_staff", "staff-password-long", role="staff")
        r = client.post(
            "/api/admin/login", json={"username": "plain_staff", "password": "staff-password-long"}
        )
        assert r.status_code == 200
        assert client.get("/api/admin/costs").status_code == 403
        assert client.get("/api/admin/ops").status_code == 403

    def test_owner_can(self, owner_client):
        assert owner_client.get("/api/admin/costs").status_code == 200
        assert owner_client.get("/api/admin/ops").status_code == 200

    def test_write_requires_csrf(self, owner_client):
        client = owner_client
        client.post("/api/bookings", json=booking_payload(), headers=idem())
        pid = client.get("/api/admin/bookings").json()["items"][0]["public_id"]
        bad = client.patch(
            f"/api/admin/bookings/{pid}", json={"status": "done"}, headers={"X-CSRF-Token": "nope"}
        )
        assert bad.status_code == 403
        ok = client.patch(f"/api/admin/bookings/{pid}", json={"status": "done"})
        assert ok.status_code == 200


# ======================================================================
# Layer 08 — security
# ======================================================================
class TestLayer08Security:
    def test_security_headers_present(self, client):
        h = client.get("/api/healthz").headers
        assert "default-src 'self'" in h["Content-Security-Policy"]
        assert h["X-Frame-Options"] == "DENY"
        assert h["X-Content-Type-Options"] == "nosniff"
        assert "frame-ancestors 'none'" in h["Content-Security-Policy"]

    def test_passwords_are_argon2_hashed(self):
        from api import auth

        h = auth.hash_password("correct-horse-battery")
        assert h.startswith("$argon2id$") and "correct-horse" not in h

    def test_login_failure_is_generic(self, client):
        from api import auth

        auth.create_user("realuser", "the-real-password-x")
        a = client.post("/api/admin/login", json={"username": "realuser", "password": "wrong-pass"})
        b = client.post(
            "/api/admin/login", json={"username": "ghostuser", "password": "wrong-pass"}
        )
        assert a.status_code == b.status_code == 401
        assert a.json()["error"]["message"] == b.json()["error"]["message"]

    def test_session_cookie_is_httponly(self, client):
        from api import auth

        auth.create_user("cookieuser", "cookie-password-long")
        r = client.post(
            "/api/admin/login", json={"username": "cookieuser", "password": "cookie-password-long"}
        )
        assert "httponly" in r.headers["set-cookie"].lower()
        assert "samesite=strict" in r.headers["set-cookie"].lower()

    def test_sql_injection_attempt_is_stored_as_data_not_executed(self, client):
        evil = "'; DROP TABLE bookings;--"
        client.post("/api/bookings", json=booking_payload(note=evil), headers=idem())
        from api import db, repo
        from api.policies import SYSTEM

        assert "bookings" in db.tables()
        assert repo.select(SYSTEM, "bookings")[0]["note"] == evil

    def test_logs_never_contain_a_phone_or_a_secret(self):
        from api.logging_ import scrub

        out = json.dumps(
            scrub(
                {
                    "phone": "09121234567",
                    "password": "hunter2",
                    "msg": "call 09121234567 now",
                    "authorization": "Bearer abc.def",
                }
            ),
            ensure_ascii=False,
        )
        assert "09121234567" not in out
        assert "hunter2" not in out
        assert "abc.def" not in out

    def test_no_secret_is_committed(self):
        import pathlib
        import re

        root = pathlib.Path(__file__).resolve().parent.parent
        pat = re.compile(r"(sk-[A-Za-z0-9]{20,}|AKIA[0-9A-Z]{16}|-----BEGIN [A-Z ]*PRIVATE KEY)")
        skip = {"node_modules", ".git", "data", "public", "releases", "work", "site"}
        for p in root.rglob("*"):
            if p.is_dir() or any(s in p.parts for s in skip):
                continue
            if p.suffix in {".py", ".sh", ".yml", ".yaml", ".md", ".json", ".example"}:
                assert not pat.search(p.read_text(encoding="utf-8", errors="ignore")), p

    def test_honeypot_absorbs_bots_without_storing(self, client):
        r = client.post(
            "/api/bookings", json=booking_payload(website="http://spam.example"), headers=idem()
        )
        assert r.status_code == 201
        from api import repo
        from api.policies import SYSTEM

        assert repo.count(SYSTEM, "bookings") == 0


# ======================================================================
# Layer 09 — rate limiting
# ======================================================================
class TestLayer09RateLimit:
    def test_per_ip_booking_limit(self, client, monkeypatch):
        from api.config import settings

        monkeypatch.setattr(settings, "RL_BOOKING_IP_PER_HOUR", 2)
        codes = [
            client.post(
                "/api/bookings", json=booking_payload(phone=f"0912000000{i}"), headers=idem()
            ).status_code
            for i in range(4)
        ]
        assert codes[:2] == [201, 201]
        assert 429 in codes[2:]

    def test_per_identity_limit(self, client, monkeypatch):
        from api.config import settings

        monkeypatch.setattr(settings, "RL_BOOKING_PHONE_PER_DAY", 1)
        a = client.post("/api/bookings", json=booking_payload(), headers=idem())
        b = client.post("/api/bookings", json=booking_payload(note="again"), headers=idem())
        assert a.status_code == 201 and b.status_code == 429

    def test_429_carries_retry_after(self, client, monkeypatch):
        from api.config import settings

        monkeypatch.setattr(settings, "RL_BOOKING_PHONE_PER_DAY", 1)
        client.post("/api/bookings", json=booking_payload(), headers=idem())
        r = client.post("/api/bookings", json=booking_payload(note="x"), headers=idem())
        assert r.status_code == 429
        assert int(r.headers["Retry-After"]) > 0
        assert r.json()["error"]["retry_after"] > 0

    def test_login_is_rate_limited(self, client, monkeypatch):
        from api.config import settings

        monkeypatch.setattr(settings, "RL_LOGIN_IP_PER_15MIN", 3)
        codes = [
            client.post(
                "/api/admin/login", json={"username": "nobody", "password": "bad-password"}
            ).status_code
            for _ in range(5)
        ]
        assert 429 in codes

    def test_global_circuit_breaker(self, client):
        from api import ratelimit

        ratelimit.breaker_open("test", seconds=30)
        r = client.get("/api/admin/bookings")
        assert r.status_code == 503
        assert r.json()["error"]["code"] == "circuit_open"
        assert int(r.headers["Retry-After"]) > 0
        ratelimit.breaker_close()
        assert client.get("/api/admin/bookings").status_code == 401

    def test_ai_path_has_user_and_global_caps(self, owner_client, monkeypatch):
        from api.config import settings

        monkeypatch.setattr(settings, "RL_AI_USER_PER_HOUR", 2)
        owner_client.post("/api/bookings", json=booking_payload(), headers=idem())
        pid = owner_client.get("/api/admin/bookings").json()["items"][0]["public_id"]
        codes = [
            owner_client.post(f"/api/admin/bookings/{pid}/summary").status_code for _ in range(4)
        ]
        assert codes[:2] == [200, 200] and 429 in codes[2:]


# ======================================================================
# Layer 06 — cost control
# ======================================================================
class TestLayer06Cost:
    def test_every_paid_resource_declares_unit_cost_cap_and_alert(self):
        from api import budget

        for res, row in budget.summary().items():
            assert row["unit_cost_usd"] >= 0
            assert row["cap_usd"] > 0, res
            assert row["alert_at_pct"] > 0

    def test_spend_is_recorded_and_capped(self, monkeypatch):
        from api import budget
        from api.errors import BudgetExceeded

        monkeypatch.setitem(budget.CATALOG, "ai_summary", (1.0, 2.0, "تست"))
        budget.record("ai_summary", 1)
        budget.record("ai_summary", 1)
        assert budget.spent("ai_summary") == 2.0
        with pytest.raises(BudgetExceeded):
            budget.guard("ai_summary", 1)

    def test_alert_fires_before_the_cap(self, monkeypatch):
        from api import budget, tracking

        seen = []
        monkeypatch.setattr(tracking, "notify", lambda k, m, **kw: seen.append(k))
        monkeypatch.setitem(budget.CATALOG, "ai_summary", (1.0, 10.0, "تست"))
        for _ in range(8):
            budget.record("ai_summary", 1)
        assert "budget_alert" in seen
        assert budget.spent("ai_summary") < 10.0  # alerted *before* the cap


# ======================================================================
# Layer 10 — caching
# ======================================================================
class TestLayer10Cache:
    def test_identical_request_is_not_recomputed(self, owner_client):
        from api import cache

        before = cache.stats()["hits"]
        owner_client.get("/api/admin/bookings")
        owner_client.get("/api/admin/bookings")
        assert cache.stats()["hits"] > before

    def test_etag_round_trip_returns_304(self, owner_client):
        r1 = owner_client.get("/api/admin/bookings")
        tag = r1.headers["ETag"]
        r2 = owner_client.get("/api/admin/bookings", headers={"If-None-Match": tag})
        assert r2.status_code == 304

    def test_write_purges_the_cached_read(self, owner_client):
        r1 = owner_client.get("/api/admin/bookings")
        owner_client.post("/api/bookings", json=booking_payload(), headers=idem())
        r2 = owner_client.get("/api/admin/bookings")
        assert r1.headers["ETag"] != r2.headers["ETag"]
        assert r2.json()["total"] == 1

    def test_fingerprinted_assets_are_immutable(self):
        from api.cache import policy_for_path

        assert "immutable" in policy_for_path("style.abc1234567.css", True, 31536000, 0)
        assert "must-revalidate" in policy_for_path("index.html", False, 31536000, 0)


# ======================================================================
# Layer 12 — error tracking and logs
# ======================================================================
class TestLayer12Observability:
    def test_unhandled_exception_alerts_with_a_trace_id(self, client, monkeypatch):
        from api import main, tracking

        seen = {}

        def boom(*a, **k):
            raise RuntimeError("synthetic failure")

        monkeypatch.setattr(main.metrics, "record", boom)
        monkeypatch.setattr(tracking, "_sink", lambda payload: seen.update(payload))
        monkeypatch.setattr(tracking, "_should_send", lambda key: True)
        r = client.post("/api/bookings", json=booking_payload(), headers=idem())
        assert r.status_code == 500
        body = r.json()["error"]
        assert body["trace_id"] and body["message"] == "خطای داخلی سرور"
        assert "synthetic failure" not in json.dumps(body)  # no internals leak
        import time

        time.sleep(0.3)
        assert seen.get("trace_id") == body["trace_id"]
        assert seen.get("kind") == "unhandled_error"

    def test_logs_are_structured_json(self, capsys):
        from api.logging_ import info, setup_logging

        setup_logging()
        info("test.event", booking_id=7)
        line = capsys.readouterr().out.strip().splitlines()[-1]
        rec = json.loads(line)
        assert rec["msg"] == "test.event" and rec["booking_id"] == 7 and "trace_id" in rec

    def test_trace_id_is_returned_on_every_response(self, client):
        assert client.get("/api/healthz").headers["X-Trace-Id"]


# ======================================================================
# Layer 13 — backup and restore
# ======================================================================
class TestLayer13Backup:
    def test_backup_and_restore_round_trip(self, client, tmp_path):
        import gzip
        import sqlite3

        from api import db

        client.post("/api/bookings", json=booking_payload(), headers=idem())
        src = db.get_conn()
        snap = tmp_path / "snap.db"
        dst = sqlite3.connect(snap)
        with dst:
            src.backup(dst)
        assert dst.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
        n = dst.execute("SELECT COUNT(*) FROM bookings").fetchone()[0]
        dst.close()
        gz = tmp_path / "snap.db.gz"
        gz.write_bytes(gzip.compress(snap.read_bytes()))
        restored = tmp_path / "restored.db"
        restored.write_bytes(gzip.decompress(gz.read_bytes()))
        rc = sqlite3.connect(restored)
        assert rc.execute("SELECT COUNT(*) FROM bookings").fetchone()[0] == n == 1
        assert rc.execute("PRAGMA foreign_key_check").fetchall() == []


# ======================================================================
# Layer 14 — the outcome metric
# ======================================================================
class TestLayer14Outcome:
    def test_delivery_is_counted_not_just_submission(self, client):
        from api import metrics

        client.post("/api/bookings", json=booking_payload(), headers=idem())
        o = metrics.outcome(30)
        # the north star is now the attended session; delivery is a supporting number
        assert o["north_star"]["key"] == "session_attended"
        assert o["supporting"]["booking_delivered"] == 1
        assert o["supporting"]["booking_submitted"] == 1
        assert o["supporting"]["delivery_success_rate"] == 100.0

    def test_failed_delivery_does_not_count_as_a_result(self, client, monkeypatch):
        from api import metrics, notify

        monkeypatch.setitem(
            notify.PROVIDERS, "file", lambda b: (_ for _ in ()).throw(RuntimeError("down"))
        )
        r = client.post("/api/bookings", json=booking_payload(), headers=idem())
        assert r.status_code == 202 and r.json()["delivered"] is False
        o = metrics.outcome(30)
        assert o["supporting"]["booking_delivered"] == 0
        assert o["supporting"]["booking_delivery_failed"] == 1

    def test_vanity_metrics_are_labelled_separately(self, client):
        from api import metrics

        client.post("/api/outcome", json={"kind": "call_click"})
        o = metrics.outcome(30)
        assert o["vanity"]["call_click"] == 1
        assert "call_click" not in o["north_star"]

    def test_redeliver_turns_a_failure_into_a_result(self, client, owner_client, monkeypatch):
        from api import metrics, notify

        real = notify.PROVIDERS["file"]
        monkeypatch.setitem(
            notify.PROVIDERS, "file", lambda b: (_ for _ in ()).throw(RuntimeError("down"))
        )
        owner_client.post("/api/bookings", json=booking_payload(), headers=idem())
        pid = owner_client.get("/api/admin/bookings").json()["items"][0]["public_id"]
        monkeypatch.setitem(notify.PROVIDERS, "file", real)
        r = owner_client.post(f"/api/admin/bookings/{pid}/redeliver")
        assert r.json()["delivered"] is True
        assert metrics.outcome(30)["supporting"]["booking_delivered"] == 1


# ======================================================================
# Delivery honesty — the bug the audit found
# ======================================================================
class TestDeliveryHonesty:
    def test_success_response_means_the_clinic_really_got_it(self, client):
        from api.config import settings

        r = client.post("/api/bookings", json=booking_payload(), headers=idem())
        assert r.json()["delivered"] is True
        lines = settings.NOTIFY_FILE.read_text(encoding="utf-8").strip().splitlines()
        assert json.loads(lines[-1])["booking"]["public_id"] == r.json()["public_id"]

    def test_when_delivery_fails_the_response_says_so(self, client, monkeypatch):
        from api import notify

        monkeypatch.setitem(
            notify.PROVIDERS, "file", lambda b: (_ for _ in ()).throw(RuntimeError("down"))
        )
        r = client.post("/api/bookings", json=booking_payload(), headers=idem())
        assert r.status_code == 202
        assert r.json()["delivered"] is False

    def test_delivery_is_idempotent(self, client):
        from api import notify, repo
        from api.policies import SYSTEM

        client.post("/api/bookings", json=booking_payload(), headers=idem())
        row = repo.select(SYSTEM, "bookings")[0]
        notify.deliver(row)
        notify.deliver(row)
        assert repo.count(SYSTEM, "deliveries", where="booking_id = ?", params=[row["id"]]) == 1


# ======================================================================
# Test-suite safety — the suite wipes rows, so it must never reach a real database
# ======================================================================
class TestSuiteIsolation:
    def test_suite_cannot_touch_a_real_db(self):
        import tempfile

        from api.config import settings

        assert str(settings.DB_PATH).startswith(tempfile.gettempdir())
        assert "asa-test-" in str(settings.DB_PATH)
        assert settings.ENV == "development"
        assert not settings.AI_API_KEY and not settings.ALERT_WEBHOOK


# ======================================================================
# Layer 05 / 07 — release plumbing exists and is runnable
# ======================================================================
class TestLayer0507Release:
    def test_ops_scripts_exist_and_are_executable(self):
        import os
        import pathlib

        ops = pathlib.Path(__file__).resolve().parent.parent / "ops"
        for name in ("deploy.sh", "rollback.sh", "backup.sh", "restore.sh", "serve.sh"):
            p = ops / name
            assert p.exists(), name
            assert os.access(p, os.X_OK), name

    def test_ci_runs_checks_before_merge(self):
        import pathlib

        ci = (
            pathlib.Path(__file__).resolve().parent.parent / ".github" / "workflows" / "ci.yml"
        ).read_text()
        assert "pull_request" in ci and "pytest" in ci and "ruff" in ci

    def test_production_refuses_a_fake_notify_provider(self, monkeypatch):
        import importlib

        monkeypatch.setenv("APP_ENV", "production")
        monkeypatch.setenv("SECRET_KEY", "x" * 40)
        monkeypatch.setenv("NOTIFY_PROVIDER", "file")
        import api.config as cfg

        with pytest.raises(RuntimeError, match="not allowed in production"):
            importlib.reload(cfg)
        monkeypatch.setenv("APP_ENV", "development")
        importlib.reload(cfg)

    def test_production_requires_a_secret_key(self, monkeypatch):
        import importlib

        monkeypatch.setenv("APP_ENV", "production")
        monkeypatch.setenv("SECRET_KEY", "")
        monkeypatch.setenv("NOTIFY_PROVIDER", "telegram")
        import api.config as cfg

        with pytest.raises(RuntimeError, match="SECRET_KEY"):
            importlib.reload(cfg)
        monkeypatch.setenv("APP_ENV", "development")
        monkeypatch.setenv("NOTIFY_PROVIDER", "file")
        importlib.reload(cfg)
