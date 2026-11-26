# SMS.ir + reminders — production activation checklist

This document describes how to enable **real** patient/clinic SMS and the
automatic confirmation reminder. It does **not** contain secrets.

## Architecture (current)

| Path | Kind | Mechanism |
|------|------|-----------|
| Patient books | `booked` | `sms.send_now` → queue → SMS.ir Verify (template booked) |
| Clinic alert on book | `admin_appointment` | `notify.alert_new_appointment` → Verify |
| ~2h before slot | `confirm_request` | `reminders.run_once` / background loop / cron |
| Staff cancel + notify | `cancelled` | Doctor status patch with `notify=true` |
| Patient self-cancel | `cancelled` | Portal cancel (best-effort; cancel never rolls back) |
| Staff reschedule + notify | `rescheduled` | Doctor move with notify; Verify reuses **booked** template `371089` (DATE/TIME/CODE) because status template `730888` only has FULLNAME |
| Staff next session + notify | `booked` | Doctor followup reuses allowlisted `booked` + template `371089` (kind `followup` is not hard-allowed and has no panel template) |
| Registration welcome | `registered` | Queued but **not** live on SMS.ir (hard ceiling) |

Live SMS.ir traffic is allowed only for kinds in:

`SMS_IR_ALLOWED_KINDS ∩ SMSIR_HARD_ALLOWED_KINDS`

Hard ceiling (code): `admin_appointment`, `booked`, `confirm_request`, `cancelled`, `rescheduled`.

Empty `SMS_IR_ALLOWED_KINDS` ⇒ **fail-closed** (kinds are written to the local file
sink as `status=logged`; booking/register still succeed).

## Required ENV (names only)

Provide these in production (`.env.production` and/or a root-owned overrides file).
Never commit real values.

| Variable | Purpose |
|----------|---------|
| `APP_ENV=production` | Enables production safety checks |
| `SECRET_KEY` | Sessions / CSRF |
| `SMS_PROVIDER=smsir` | Live patient/clinic SMS path |
| `SMS_API_KEY` | SMS.ir panel API key |
| `SMS_SENDER` | Dedicated numeric line number |
| `SMS_IR_ALLOWED_KINDS` | Comma-separated allowlist (see below) |
| `NOTIFY_PROVIDER=smsir` | Clinic lead / admin booking alerts via SMS.ir |
| `ADMIN_NOTIFICATION_PHONE` | Destination for admin Verify / notify |
| `SMS_INBOUND_SECRET` | Shared secret for inbound reply webhook |
| `REMINDER_BACKGROUND=1` | Start `reminders.loop` in app lifespan |
| `REMINDER_LEAD_MIN=120` | Minutes before slot to send `confirm_request` |
| `REMINDER_TICK_S=60` | Background poll interval |

Optional but recommended: `SMS_BUDGET_MONTHLY_USD`, `SMS_UNIT_COST_USD`, `ALERT_WEBHOOK`.

### Recommended allowlist for go-live

```text
SMS_IR_ALLOWED_KINDS=admin_appointment,booked,confirm_request,cancelled,rescheduled
```

## Runtime overrides on the VPS

Production historically used a **webhook sink** so SMS.ir was never called:

- `SMS_PROVIDER=webhook` + dead `SMS_WEBHOOK`
- `NOTIFY_PROVIDER=webhook` + dead `NOTIFY_WEBHOOK`
- `REMINDER_BACKGROUND=0`

Those may live in systemd `Environment=` and/or `/etc/asa-physio/runtime-overrides.env`.
To go live, remove the sink overrides (or set the live values there) so the process
environment matches the table above, then restart with:

```bash
sudo systemctl restart asa-physio.service
```

Confirm with `curl -fsS http://127.0.0.1:8080/api/readyz` (`notify_provider` should
reflect the live notify provider, not a dead webhook).

## Inbound replies (1 / 2)

1. Configure SMS.ir (or gateway) divert to `POST https://<host>/api/sms/inbound`.
2. Send header `X-SMS-Secret: <SMS_INBOUND_SECRET>`.
3. Normalize phones to `09xxxxxxxxx`.

Without inbound, `confirm_request` still sends; attendance will not auto-update.

## Dedup & failure behaviour

- Automatic reminders **claim** `confirm_sent_at` before send; on SMS failure the claim
  is **released** so a later tick can retry.
- Manual `/api/admin/appointments/{id}/remind` uses the same claim; duplicate calls get
  HTTP 409 once a successful reminder was recorded.
- Booking / register / cancel **never fail** solely because SMS delivery failed
  (`sms_sent` may be `false`).

## What the operator must supply

1. Valid SMS.ir API key and line number (panel).
2. Verify templates already mapped in code (booked / confirm / status / admin).
   Note: live **reschedule** and **next-session** patient SMS both use the booked
   Verify template (`371089`) so DATE/TIME reach the patient; a dedicated
   multi-param “changed/followup” panel template is not configured in code.
3. Admin notification mobile number.
4. Inbound divert URL + shared secret.
5. Explicit approval to leave the webhook sink and enable `REMINDER_BACKGROUND=1`.
6. A short live test plan using a clinic-owned test phone (not a real patient).

## Safety notes

- Do **not** set `SMS_PROVIDER=file` or `NOTIFY_PROVIDER=file` in production (boot fails).
- `registered` / promo-like kinds cannot be force-enabled via ENV alone (hard ceiling).
- Test SMS with mocks in CI; never use production patient numbers in automated tests.
