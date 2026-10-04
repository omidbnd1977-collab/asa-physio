"""Layer 08 — all configuration from the environment. No secret ever lives in the repo."""

from __future__ import annotations

import os
import pathlib
import secrets

ROOT = pathlib.Path(__file__).resolve().parent.parent


def _b(name: str, default: str = "0") -> bool:
    return os.getenv(name, default).strip().lower() in {"1", "true", "yes", "on"}


def _i(name: str, default: int) -> int:
    try:
        return int(os.getenv(name, str(default)))
    except ValueError:
        return default


def _f(name: str, default: float) -> float:
    try:
        return float(os.getenv(name, str(default)))
    except ValueError:
        return default


class Settings:
    # --- environment -------------------------------------------------------
    ENV: str = os.getenv("APP_ENV", "development")  # development | staging | production
    DEBUG: bool = _b(
        "APP_DEBUG", "1" if os.getenv("APP_ENV", "development") == "development" else "0"
    )
    VERSION: str = os.getenv("APP_VERSION", "dev")
    BASE_URL: str = os.getenv("APP_BASE_URL", "http://localhost:8080")

    # --- storage -----------------------------------------------------------
    DB_PATH: pathlib.Path = pathlib.Path(os.getenv("DB_PATH", str(ROOT / "data" / "asa.db")))
    BACKUP_DIR: pathlib.Path = pathlib.Path(os.getenv("BACKUP_DIR", str(ROOT / "data" / "backups")))
    STATIC_DIR: pathlib.Path = pathlib.Path(os.getenv("STATIC_DIR", str(ROOT / "public")))

    # --- security ----------------------------------------------------------
    # Dev gets an ephemeral secret so nothing is ever committed; prod MUST set it.
    SECRET_KEY: str = os.getenv("SECRET_KEY", "")
    SESSION_TTL_H: int = _i("SESSION_TTL_HOURS", 12)
    ALLOWED_ORIGINS: list[str] = [
        o.strip() for o in os.getenv("ALLOWED_ORIGINS", "").split(",") if o.strip()
    ]
    TRUST_PROXY: bool = _b("TRUST_PROXY", "0")
    MAX_BODY_BYTES: int = _i("MAX_BODY_BYTES", 16 * 1024)

    # --- layer 09: rate limits --------------------------------------------
    RL_IP_PER_MIN: int = _i("RL_IP_PER_MIN", 60)
    RL_IP_BURST: int = _i("RL_IP_BURST", 20)
    RL_BOOKING_IP_PER_HOUR: int = _i("RL_BOOKING_IP_PER_HOUR", 5)
    RL_BOOKING_PHONE_PER_DAY: int = _i("RL_BOOKING_PHONE_PER_DAY", 3)
    RL_LOGIN_IP_PER_15MIN: int = _i("RL_LOGIN_IP_PER_15MIN", 8)
    RL_AI_USER_PER_HOUR: int = _i("RL_AI_USER_PER_HOUR", 10)
    RL_AI_GLOBAL_PER_DAY: int = _i("RL_AI_GLOBAL_PER_DAY", 200)
    BREAKER_ERR_THRESHOLD: int = _i("BREAKER_ERR_THRESHOLD", 25)  # 5xx within window
    BREAKER_WINDOW_S: int = _i("BREAKER_WINDOW_S", 60)
    BREAKER_OPEN_S: int = _i("BREAKER_OPEN_S", 30)

    # --- layer 06: cost control -------------------------------------------
    AI_ENABLED: bool = _b("AI_ENABLED", "0")
    AI_API_KEY: str = os.getenv("AI_API_KEY", "")
    AI_UNIT_COST_USD: float = _f("AI_UNIT_COST_USD", 0.0012)  # per call
    BUDGET_MONTHLY_USD: float = _f("BUDGET_MONTHLY_USD", 5.0)
    BUDGET_ALERT_AT: float = _f("BUDGET_ALERT_AT", 0.8)  # alert at 80 % of cap

    # --- layer 12: alerting -------------------------------------------------
    ALERT_WEBHOOK: str = os.getenv("ALERT_WEBHOOK", "")
    ALERT_FILE: pathlib.Path = pathlib.Path(
        os.getenv("ALERT_FILE", str(ROOT / "data" / "alerts.log"))
    )

    # --- booking delivery (what makes the success toast true) ---------------
    NOTIFY_PROVIDER: str = os.getenv("NOTIFY_PROVIDER", "file")  # file | webhook | telegram
    NOTIFY_WEBHOOK: str = os.getenv("NOTIFY_WEBHOOK", "")
    TELEGRAM_BOT_TOKEN: str = os.getenv("TELEGRAM_BOT_TOKEN", "")
    TELEGRAM_CHAT_ID: str = os.getenv("TELEGRAM_CHAT_ID", "")
    NOTIFY_FILE: pathlib.Path = pathlib.Path(
        os.getenv("NOTIFY_FILE", str(ROOT / "data" / "outbox.jsonl"))
    )

    # --- SMS (patient notifications) ---------------------------------------
    SMS_PROVIDER: str = os.getenv("SMS_PROVIDER", "file")  # file|webhook|kavenegar|smsir
    SMS_API_KEY: str = os.getenv("SMS_API_KEY", "")
    SMS_SENDER: str = os.getenv("SMS_SENDER", "")
    SMS_WEBHOOK: str = os.getenv("SMS_WEBHOOK", "")
    SMS_INBOUND_SECRET: str = os.getenv("SMS_INBOUND_SECRET", "")
    SMS_UNIT_COST_USD: float = _f("SMS_UNIT_COST_USD", 0.0045)
    SMS_BUDGET_MONTHLY_USD: float = _f("SMS_BUDGET_MONTHLY_USD", 3.0)
    SMS_FILE: pathlib.Path = pathlib.Path(
        os.getenv("SMS_FILE", str(ROOT / "data" / "sms-outbox.txt"))
    )

    # --- patient portal -----------------------------------------------------
    PATIENT_SESSION_TTL_H: int = _i("PATIENT_SESSION_TTL_HOURS", 6)
    UPLOAD_DIR: pathlib.Path = pathlib.Path(os.getenv("UPLOAD_DIR", str(ROOT / "data" / "uploads")))
    MAX_UPLOAD_BYTES: int = _i("MAX_UPLOAD_BYTES", 50 * 1024 * 1024)  # 50 MB
    REMINDER_LEAD_MIN: int = _i("REMINDER_LEAD_MIN", 120)  # confirm SMS 2 h before
    REMINDER_TICK_S: int = _i("REMINDER_TICK_S", 60)
    # patients often share one IP (clinic wifi, a family, carrier NAT), so the
    # per-IP ceiling is loose and the real protection is the per-national-id limit
    RL_REGISTER_IP_PER_DAY: int = _i("RL_REGISTER_IP_PER_DAY", 25)
    RL_PATIENT_LOGIN_IP_PER_15MIN: int = _i("RL_PATIENT_LOGIN_IP_PER_15MIN", 10)
    RL_APPT_PATIENT_PER_DAY: int = _i("RL_APPT_PATIENT_PER_DAY", 4)

    # --- layer 10: caching --------------------------------------------------
    STATIC_IMMUTABLE_MAX_AGE: int = _i("STATIC_IMMUTABLE_MAX_AGE", 31536000)
    HTML_MAX_AGE: int = _i("HTML_MAX_AGE", 0)
    API_CACHE_TTL_S: int = _i("API_CACHE_TTL_S", 30)

    def __init__(self) -> None:
        if not self.SECRET_KEY:
            if self.ENV == "production":
                raise RuntimeError("SECRET_KEY is required in production (see .env.example)")
            self.SECRET_KEY = secrets.token_urlsafe(48)  # ephemeral, dev only
        if self.ENV == "production" and self.NOTIFY_PROVIDER == "file":
            # the file sink is a dev stub: refuse to pretend bookings are delivered in prod
            raise RuntimeError("NOTIFY_PROVIDER=file is not allowed in production")
        if self.ENV == "production" and self.SMS_PROVIDER == "file":
            # same rule for SMS: never tell a patient we texted them when we did not
            raise RuntimeError("SMS_PROVIDER=file is not allowed in production")
        self.DB_PATH.parent.mkdir(parents=True, exist_ok=True)
        self.BACKUP_DIR.mkdir(parents=True, exist_ok=True)
        self.UPLOAD_DIR.mkdir(parents=True, exist_ok=True)

    @property
    def is_prod(self) -> bool:
        return self.ENV == "production"


settings = Settings()
