-- 0005_clinical_records.sql — forward only.
-- Clinical work in the doctor panel: the affected area/diagnosis, the treatment that
-- was actually performed, a per-patient treatment history, and an uploaded MRI file.
-- Both catalogues are extensible: staff type a missing entry once and it joins the list.
-- Still no triggers.

-- the patient can attach the MRI itself, not only a link
ALTER TABLE patients ADD COLUMN mri_file TEXT NOT NULL DEFAULT '';

-- ---------------------------------------------------------------- catalogues
CREATE TABLE body_parts (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    name       TEXT    NOT NULL UNIQUE CHECK (length(trim(name)) BETWEEN 2 AND 60),
    category   TEXT    NOT NULL DEFAULT 'other'
                       CHECK (category IN ('spine', 'upper', 'lower', 'neuro', 'other')),
    is_builtin INTEGER NOT NULL DEFAULT 0 CHECK (is_builtin IN (0, 1)),
    is_active  INTEGER NOT NULL DEFAULT 1 CHECK (is_active IN (0, 1)),
    created_by INTEGER NULL REFERENCES admin_users(id) ON DELETE SET NULL,
    created_at TEXT    NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ', 'now'))
);
CREATE INDEX idx_body_parts_cat ON body_parts(category, name);

CREATE TABLE treatments (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    name       TEXT    NOT NULL UNIQUE CHECK (length(trim(name)) BETWEEN 2 AND 60),
    is_builtin INTEGER NOT NULL DEFAULT 0 CHECK (is_builtin IN (0, 1)),
    is_active  INTEGER NOT NULL DEFAULT 1 CHECK (is_active IN (0, 1)),
    created_by INTEGER NULL REFERENCES admin_users(id) ON DELETE SET NULL,
    created_at TEXT    NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ', 'now'))
);

-- ---------------------------------------------------------------- history
CREATE TABLE treatment_sessions (
    id             INTEGER PRIMARY KEY AUTOINCREMENT,
    public_id      TEXT    NOT NULL UNIQUE CHECK (length(public_id) = 10),
    patient_id     INTEGER NOT NULL REFERENCES patients(id) ON DELETE CASCADE,
    appointment_id INTEGER NULL REFERENCES appointments(id) ON DELETE SET NULL,
    session_date   TEXT    NOT NULL CHECK (length(session_date) = 10),
    findings       TEXT    NOT NULL DEFAULT '' CHECK (length(findings) <= 2000),
    plan           TEXT    NOT NULL DEFAULT '' CHECK (length(plan) <= 2000),
    pain_before    INTEGER NULL CHECK (pain_before BETWEEN 0 AND 10),
    pain_after     INTEGER NULL CHECK (pain_after BETWEEN 0 AND 10),
    created_by     INTEGER NULL REFERENCES admin_users(id) ON DELETE SET NULL,
    created_at     TEXT    NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ', 'now')),
    updated_at     TEXT    NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ', 'now'))
);
CREATE INDEX idx_sessions_patient ON treatment_sessions(patient_id, session_date DESC);

CREATE TABLE session_body_parts (
    session_id   INTEGER NOT NULL REFERENCES treatment_sessions(id) ON DELETE CASCADE,
    body_part_id INTEGER NOT NULL REFERENCES body_parts(id) ON DELETE RESTRICT,
    PRIMARY KEY (session_id, body_part_id)
);

CREATE TABLE session_treatments (
    session_id   INTEGER NOT NULL REFERENCES treatment_sessions(id) ON DELETE CASCADE,
    treatment_id INTEGER NOT NULL REFERENCES treatments(id) ON DELETE RESTRICT,
    PRIMARY KEY (session_id, treatment_id)
);

-- ------------------------------------------------- widen the SMS kind enum
-- SQLite cannot alter a CHECK in place, so the table is rebuilt and copied.
CREATE TABLE sms_messages_new (
    id             INTEGER PRIMARY KEY AUTOINCREMENT,
    patient_id     INTEGER NULL REFERENCES patients(id) ON DELETE SET NULL,
    appointment_id INTEGER NULL REFERENCES appointments(id) ON DELETE SET NULL,
    phone          TEXT    NOT NULL
                           CHECK (phone GLOB '09[0-9][0-9][0-9][0-9][0-9][0-9][0-9][0-9][0-9]'),
    kind           TEXT    NOT NULL CHECK (kind IN (
                       'registered', 'booked', 'rescheduled', 'cancelled',
                       'followup', 'confirm_request', 'custom',
                       'welcome_back', 'session_logged')),
    body           TEXT    NOT NULL CHECK (length(body) BETWEEN 1 AND 600),
    status         TEXT    NOT NULL DEFAULT 'pending'
                           CHECK (status IN ('pending', 'sent', 'failed')),
    provider       TEXT    NOT NULL DEFAULT '',
    attempts       INTEGER NOT NULL DEFAULT 0 CHECK (attempts BETWEEN 0 AND 20),
    last_error     TEXT    NOT NULL DEFAULT '' CHECK (length(last_error) <= 400),
    sent_at        TEXT    NULL,
    created_at     TEXT    NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ', 'now'))
);
INSERT INTO sms_messages_new
SELECT id, patient_id, appointment_id, phone, kind, body, status, provider,
       attempts, last_error, sent_at, created_at FROM sms_messages;
DROP TABLE sms_messages;
ALTER TABLE sms_messages_new RENAME TO sms_messages;
CREATE INDEX idx_sms_patient ON sms_messages(patient_id, created_at DESC);
CREATE INDEX idx_sms_status  ON sms_messages(status, created_at);

-- ------------------------------------------------- seed the catalogues
-- Regions
INSERT INTO body_parts (name, category, is_builtin) VALUES
 ('گردن (مهره‌های گردنی)', 'spine', 1),
 ('پشت (مهره‌های سینه‌ای)', 'spine', 1),
 ('کمر (مهره‌های کمری)', 'spine', 1),
 ('مفصل ساکروایلیاک', 'spine', 1),
 ('دنبالچه', 'spine', 1),
 ('شانه', 'upper', 1),
 ('بازو', 'upper', 1),
 ('آرنج', 'upper', 1),
 ('ساعد', 'upper', 1),
 ('مچ دست', 'upper', 1),
 ('دست و انگشتان', 'upper', 1),
 ('لگن', 'lower', 1),
 ('مفصل ران', 'lower', 1),
 ('ران', 'lower', 1),
 ('زانو', 'lower', 1),
 ('ساق پا', 'lower', 1),
 ('مچ پا', 'lower', 1),
 ('پاشنه', 'lower', 1),
 ('کف پا و انگشتان', 'lower', 1),
 ('مفصل فک (TMJ)', 'other', 1),
 ('قفسه سینه', 'other', 1),
 ('عضلات کف لگن', 'other', 1);

-- Named diagnoses
INSERT INTO body_parts (name, category, is_builtin) VALUES
 ('دیسک کمر', 'spine', 1),
 ('دیسک گردن', 'spine', 1),
 ('تنگی کانال نخاعی', 'spine', 1),
 ('اسپوندیلولیستزیس', 'spine', 1),
 ('اسکولیوز', 'spine', 1),
 ('کایفوز (گردی پشت)', 'spine', 1),
 ('سیاتیک', 'neuro', 1),
 ('سندرم پیریفورمیس', 'neuro', 1),
 ('سندرم تونل کارپال', 'neuro', 1),
 ('فلج بل', 'neuro', 1),
 ('سکته مغزی', 'neuro', 1),
 ('ام‌اس (مولتیپل اسکلروزیس)', 'neuro', 1),
 ('پارکینسون', 'neuro', 1),
 ('نوروپاتی دیابتی', 'neuro', 1),
 ('آسیب نخاعی', 'neuro', 1),
 ('شانه منجمد', 'upper', 1),
 ('تاندونیت روتاتور کاف', 'upper', 1),
 ('گیرافتادگی شانه', 'upper', 1),
 ('آرنج تنیس‌بازان', 'upper', 1),
 ('آرنج گلف‌بازان', 'upper', 1),
 ('دکرون (تاندونیت مچ دست)', 'upper', 1),
 ('آرتروز زانو', 'lower', 1),
 ('آسیب منیسک', 'lower', 1),
 ('پارگی رباط صلیبی قدامی', 'lower', 1),
 ('سندرم درد کشککی-رانی', 'lower', 1),
 ('تعویض مفصل زانو', 'lower', 1),
 ('تعویض مفصل ران', 'lower', 1),
 ('خار پاشنه', 'lower', 1),
 ('فاشییت پلانتار', 'lower', 1),
 ('تاندونیت آشیل', 'lower', 1),
 ('پیچ‌خوردگی مچ پا', 'lower', 1),
 ('درد میوفاشیال', 'other', 1),
 ('فیبرومیالژیا', 'other', 1),
 ('آرتریت روماتوئید', 'other', 1),
 ('توانبخشی بعد از شکستگی', 'other', 1),
 ('توانبخشی بعد از جراحی', 'other', 1),
 ('آسیب ورزشی', 'other', 1),
 ('لنف ادم', 'other', 1);

INSERT INTO treatments (name, is_builtin) VALUES
 ('لیزر پرتوان', 1),
 ('شاک ویو', 1),
 ('تکار تراپی', 1),
 ('درمان‌های دستی (منوال تراپی)', 1),
 ('موبیلیزاسیون مفصلی', 1),
 ('طب سوزنی', 1),
 ('درای نیدلینگ', 1),
 ('اولتراسوند تراپی', 1),
 ('الکتروتراپی (TENS)', 1),
 ('جریان اینترفرنشیال', 1),
 ('مگنت تراپی', 1),
 ('تمرین درمانی', 1),
 ('ورزش اصلاحی', 1),
 ('کشش (تراکشن)', 1),
 ('ماساژ درمانی', 1),
 ('کینزیوتیپینگ', 1),
 ('PNF', 1),
 ('بیوفیدبک', 1),
 ('گرما درمانی', 1),
 ('سرما درمانی', 1),
 ('هیدروتراپی', 1),
 ('آموزش بیمار و اصلاح وضعیت', 1);
