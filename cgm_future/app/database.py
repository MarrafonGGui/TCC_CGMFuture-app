from __future__ import annotations
import os
import json
import time
import hmac
import base64
import sqlite3
import hashlib
import secrets
from pathlib import Path
from datetime import datetime, timedelta
from typing import Any
import pandas as pd
from dotenv import load_dotenv

BASE_DIR = Path(__file__).resolve().parent.parent
load_dotenv(BASE_DIR / ".env")

SQLITE_PATH = BASE_DIR / "cgm.db"
DB_PATH = SQLITE_PATH

# Perfis de acesso
ROLE_ADM = "adm"
ROLE_MEDICO = "medico"
ROLE_PACIENTE = "paciente"
VALID_ROLES = (ROLE_ADM, ROLE_MEDICO, ROLE_PACIENTE)

JWT_SECRET = os.getenv("JWT_SECRET", "tcc-cgm-health-rbac-secret-key-2026")

# Driver opcional do PostgreSQL (usa SQLite caso não esteja instalado)
try:
    import psycopg2
    from psycopg2 import sql, extras
    HAS_PSYCOPG2 = True
except ImportError:
    HAS_PSYCOPG2 = False


class DatabaseState:
    active_engine: str = "sqlite"  # "postgres" ou "sqlite"
    postgres_error: str | None = None
    last_checked: datetime = datetime.now()


STATE = DatabaseState()


def normalize_role(role: str | None) -> str:
    r = str(role or "").strip().lower()
    if r in ("adm", "admin", "administrador"):
        return ROLE_ADM
    if r in ("medico", "médico", "doctor"):
        return ROLE_MEDICO
    return ROLE_PACIENTE


def get_pg_config() -> dict:
    db_url = os.getenv("DATABASE_URL") or os.getenv("POSTGRES_URL")
    if db_url:
        return {"dsn": db_url}
    return {
        "host": os.getenv("POSTGRES_HOST", "localhost"),
        "port": int(os.getenv("POSTGRES_PORT", 5432)),
        "dbname": os.getenv("POSTGRES_DB", "cgm_db"),
        "user": os.getenv("POSTGRES_USER", "postgres"),
        "password": os.getenv("POSTGRES_PASSWORD", "postgres"),
        "connect_timeout": 3
    }


def try_get_pg_connection():
    if not HAS_PSYCOPG2:
        raise RuntimeError("Driver psycopg2-binary não instalado.")
    cfg = get_pg_config()
    if "dsn" in cfg:
        return psycopg2.connect(cfg["dsn"], connect_timeout=3)
    return psycopg2.connect(**cfg)


def get_sqlite_connection(db_path: Path | str | None = None) -> sqlite3.Connection:
    target_path = Path(db_path) if db_path else SQLITE_PATH
    conn = sqlite3.connect(str(target_path))
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON;")
    return conn


def determine_engine(force_recheck: bool = False) -> str:
    """Testa a conexão com o PostgreSQL e cai para SQLite se estiver indisponível."""
    target_db = os.getenv("DB_TYPE", "postgres").lower()
    if target_db == "sqlite":
        STATE.active_engine = "sqlite"
        STATE.postgres_error = None
        return "sqlite"

    if not HAS_PSYCOPG2:
        STATE.active_engine = "sqlite"
        STATE.postgres_error = "Driver psycopg2-binary não encontrado."
        return "sqlite"

    if not force_recheck and STATE.active_engine in ("postgres", "sqlite") and (datetime.now() - STATE.last_checked).total_seconds() < 10:
        return STATE.active_engine

    STATE.last_checked = datetime.now()
    try:
        conn = try_get_pg_connection()
        conn.close()
        STATE.active_engine = "postgres"
        STATE.postgres_error = None
        return "postgres"
    except Exception as e:
        STATE.active_engine = "sqlite"
        STATE.postgres_error = str(e)
        return "sqlite"


def get_db_info() -> dict:
    cfg = get_pg_config()
    host_info = cfg.get("dsn") or f"{cfg.get('host')}:{cfg.get('port')}/{cfg.get('dbname')}"
    return {
        "active_engine": STATE.active_engine,
        "is_postgres": (STATE.active_engine == "postgres"),
        "postgres_target": host_info,
        "postgres_error": STATE.postgres_error,
        "sqlite_path": str(SQLITE_PATH.name)
    }


# Hashing de senhas (PBKDF2) e tokens JWT (HS256)

def hash_password(password: str, salt: str | None = None) -> str:
    if not salt:
        salt = secrets.token_hex(16)
    key = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), salt.encode("utf-8"), 100_000)
    return f"{salt}${key.hex()}"


def verify_password(password: str, password_hash: str) -> bool:
    try:
        salt, key_hex = password_hash.split("$", 1)
        expected = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), salt.encode("utf-8"), 100_000).hex()
        return secrets.compare_digest(key_hex, expected)
    except Exception:
        return False


def _b64url_encode(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).rstrip(b"=").decode("ascii")


def _b64url_decode(val: str) -> bytes:
    pad = "=" * (-len(val) % 4)
    return base64.urlsafe_b64decode((val + pad).encode("ascii"))


def create_jwt_token(user_id: int, username: str, role: str, sid: str, days_valid: int = 7) -> str:
    now_ts = int(time.time())
    header = {"alg": "HS256", "typ": "JWT"}
    payload = {
        "sub": str(user_id),
        "username": username,
        "role": normalize_role(role),
        "sid": sid,
        "iat": now_ts,
        "exp": now_ts + int(days_valid * 86400)
    }
    h_b64 = _b64url_encode(json.dumps(header, separators=(",", ":")).encode("utf-8"))
    p_b64 = _b64url_encode(json.dumps(payload, separators=(",", ":")).encode("utf-8"))
    signing_input = f"{h_b64}.{p_b64}".encode("ascii")
    sig = hmac.new(JWT_SECRET.encode("utf-8"), signing_input, hashlib.sha256).digest()
    return f"{h_b64}.{p_b64}.{_b64url_encode(sig)}"


def decode_jwt_token(token: str) -> dict | None:
    try:
        parts = token.split(".")
        if len(parts) != 3:
            return None
        h_b64, p_b64, s_b64 = parts
        signing_input = f"{h_b64}.{p_b64}".encode("ascii")
        expected_sig = hmac.new(JWT_SECRET.encode("utf-8"), signing_input, hashlib.sha256).digest()
        actual_sig = _b64url_decode(s_b64)
        if not hmac.compare_digest(expected_sig, actual_sig):
            return None
        payload = json.loads(_b64url_decode(p_b64).decode("utf-8"))
        if int(payload.get("exp", 0)) < int(time.time()):
            return None
        return payload
    except Exception:
        return None


# Criação e migração das tabelas (PostgreSQL e SQLite)

def init_postgres():
    with try_get_pg_connection() as conn:
        with conn.cursor() as cur:
            # Usuários e vínculo médico-paciente
            cur.execute("""
                CREATE TABLE IF NOT EXISTS users (
                    id SERIAL PRIMARY KEY,
                    username VARCHAR(100) UNIQUE NOT NULL,
                    password_hash VARCHAR(255) NOT NULL,
                    role VARCHAR(50) NOT NULL DEFAULT 'paciente',
                    full_name VARCHAR(150),
                    doctor_id INTEGER REFERENCES users(id) ON DELETE SET NULL,
                    crm VARCHAR(60),
                    created_at TIMESTAMP WITHOUT TIME ZONE DEFAULT CURRENT_TIMESTAMP
                );
            """)

            # Colunas novas para bancos já existentes
            cur.execute("""
                SELECT column_name FROM information_schema.columns
                WHERE table_name = 'users' AND column_name = 'doctor_id';
            """)
            if not cur.fetchone():
                cur.execute("ALTER TABLE users ADD COLUMN doctor_id INTEGER REFERENCES users(id) ON DELETE SET NULL;")

            cur.execute("""
                SELECT column_name FROM information_schema.columns
                WHERE table_name = 'users' AND column_name = 'crm';
            """)
            if not cur.fetchone():
                cur.execute("ALTER TABLE users ADD COLUMN crm VARCHAR(60);")

            # Ajusta nomes antigos de perfil
            cur.execute("UPDATE users SET role = 'paciente' WHERE role = 'user';")
            cur.execute("UPDATE users SET role = 'adm' WHERE role = 'admin';")

            # Sessões ativas
            cur.execute("""
                CREATE TABLE IF NOT EXISTS user_sessions (
                    token VARCHAR(128) PRIMARY KEY,
                    user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
                    created_at TIMESTAMP WITHOUT TIME ZONE DEFAULT CURRENT_TIMESTAMP,
                    expires_at TIMESTAMP WITHOUT TIME ZONE NOT NULL
                );
            """)

            # Garante o administrador inicial
            cur.execute("SELECT id FROM users WHERE username = 'adm';")
            admin_row = cur.fetchone()
            if not admin_row:
                admin_pwd = hash_password("adm")
                cur.execute("""
                    INSERT INTO users (username, password_hash, role, full_name)
                    VALUES ('adm', %s, 'adm', 'Administrador do Sistema')
                    RETURNING id;
                """, (admin_pwd,))
                admin_id = cur.fetchone()[0]
            else:
                admin_id = admin_row[0]

            # Leituras de glicose
            cur.execute("""
                CREATE TABLE IF NOT EXISTS glucose_readings (
                    id SERIAL PRIMARY KEY,
                    user_id INTEGER NOT NULL DEFAULT 1 REFERENCES users(id) ON DELETE CASCADE,
                    timestamp TIMESTAMP WITHOUT TIME ZONE NOT NULL,
                    glucose NUMERIC(5, 1) NOT NULL,
                    source VARCHAR(100) NOT NULL DEFAULT 'manual',
                    created_at TIMESTAMP WITHOUT TIME ZONE DEFAULT CURRENT_TIMESTAMP
                );
            """)

            cur.execute("""
                SELECT column_name FROM information_schema.columns
                WHERE table_name = 'glucose_readings' AND column_name = 'user_id';
            """)
            if not cur.fetchone():
                cur.execute("ALTER TABLE glucose_readings ADD COLUMN user_id INTEGER DEFAULT 1 REFERENCES users(id) ON DELETE CASCADE;")
                cur.execute(f"UPDATE glucose_readings SET user_id = {admin_id} WHERE user_id IS NULL;")
                cur.execute("ALTER TABLE glucose_readings ALTER COLUMN user_id SET NOT NULL;")

            cur.execute("""
                DO $$
                BEGIN
                    IF EXISTS (
                        SELECT 1 FROM pg_constraint WHERE conname = 'glucose_readings_timestamp_key'
                    ) THEN
                        ALTER TABLE glucose_readings DROP CONSTRAINT glucose_readings_timestamp_key;
                    END IF;
                    IF NOT EXISTS (
                        SELECT 1 FROM pg_constraint WHERE conname = 'glucose_readings_user_timestamp_key'
                    ) THEN
                        ALTER TABLE glucose_readings ADD CONSTRAINT glucose_readings_user_timestamp_key UNIQUE (user_id, timestamp);
                    END IF;
                END $$;
            """)

            cur.execute("CREATE INDEX IF NOT EXISTS idx_readings_user_timestamp ON glucose_readings(user_id, timestamp);")
            cur.execute("CREATE INDEX IF NOT EXISTS idx_readings_user ON glucose_readings(user_id);")
            cur.execute("CREATE INDEX IF NOT EXISTS idx_readings_source ON glucose_readings(source);")

            # Arquivos importados
            cur.execute("""
                CREATE TABLE IF NOT EXISTS sources (
                    id SERIAL PRIMARY KEY,
                    user_id INTEGER NOT NULL DEFAULT 1 REFERENCES users(id) ON DELETE CASCADE,
                    filename VARCHAR(255) NOT NULL,
                    rows_count INTEGER NOT NULL,
                    imported_at TIMESTAMP WITHOUT TIME ZONE DEFAULT CURRENT_TIMESTAMP
                );
            """)
            cur.execute("""
                SELECT column_name FROM information_schema.columns
                WHERE table_name = 'sources' AND column_name = 'user_id';
            """)
            if not cur.fetchone():
                cur.execute("ALTER TABLE sources ADD COLUMN user_id INTEGER DEFAULT 1 REFERENCES users(id) ON DELETE CASCADE;")
                cur.execute(f"UPDATE sources SET user_id = {admin_id} WHERE user_id IS NULL;")
                cur.execute("ALTER TABLE sources ALTER COLUMN user_id SET NOT NULL;")

            cur.execute("""
                DO $$
                BEGIN
                    IF EXISTS (
                        SELECT 1 FROM pg_constraint WHERE conname = 'sources_filename_key'
                    ) THEN
                        ALTER TABLE sources DROP CONSTRAINT sources_filename_key;
                    END IF;
                    IF NOT EXISTS (
                        SELECT 1 FROM pg_constraint WHERE conname = 'sources_user_filename_key'
                    ) THEN
                        ALTER TABLE sources ADD CONSTRAINT sources_user_filename_key UNIQUE (user_id, filename);
                    END IF;
                END $$;
            """)

            # Histórico e validação das previsões
            cur.execute("""
                CREATE TABLE IF NOT EXISTS predictions_log (
                    id SERIAL PRIMARY KEY,
                    user_id INTEGER NOT NULL DEFAULT 1 REFERENCES users(id) ON DELETE CASCADE,
                    timestamp TIMESTAMP WITHOUT TIME ZONE NOT NULL,
                    current_glucose NUMERIC(5, 1) NOT NULL,
                    pred_15m NUMERIC(5, 1),
                    pred_30m NUMERIC(5, 1),
                    actual_15m NUMERIC(5, 1),
                    actual_30m NUMERIC(5, 1),
                    error_15m NUMERIC(5, 1),
                    error_30m NUMERIC(5, 1),
                    model_15m VARCHAR(100),
                    model_30m VARCHAR(100),
                    confirmed_15m BOOLEAN NOT NULL DEFAULT FALSE,
                    confirmed_30m BOOLEAN NOT NULL DEFAULT FALSE,
                    confirmed_by VARCHAR(100),
                    confirmed_at TIMESTAMP WITHOUT TIME ZONE,
                    created_at TIMESTAMP WITHOUT TIME ZONE DEFAULT CURRENT_TIMESTAMP
                );
            """)
            for col_name, col_def in [
                ("user_id", f"INTEGER DEFAULT {admin_id} REFERENCES users(id) ON DELETE CASCADE"),
                ("confirmed_15m", "BOOLEAN NOT NULL DEFAULT FALSE"),
                ("confirmed_30m", "BOOLEAN NOT NULL DEFAULT FALSE"),
                ("confirmed_by", "VARCHAR(100)"),
                ("confirmed_at", "TIMESTAMP WITHOUT TIME ZONE"),
            ]:
                cur.execute("""
                    SELECT column_name FROM information_schema.columns
                    WHERE table_name = 'predictions_log' AND column_name = %s;
                """, (col_name,))
                if not cur.fetchone():
                    cur.execute(f"ALTER TABLE predictions_log ADD COLUMN {col_name} {col_def};")

            cur.execute("CREATE INDEX IF NOT EXISTS idx_predictions_user ON predictions_log(user_id);")

            # Configurações gerais
            cur.execute("""
                CREATE TABLE IF NOT EXISTS settings (
                    key VARCHAR(100) PRIMARY KEY,
                    value TEXT
                );
            """)

            # Parâmetros metabólicos do paciente
            cur.execute("""
                CREATE TABLE IF NOT EXISTS patient_metabolic_profiles (
                    user_id INTEGER PRIMARY KEY REFERENCES users(id) ON DELETE CASCADE,
                    icr NUMERIC(5, 2) NOT NULL DEFAULT 8.0,
                    isf NUMERIC(5, 2) NOT NULL DEFAULT 40.0,
                    target_glucose NUMERIC(5, 1) NOT NULL DEFAULT 100.0,
                    clinical_notes TEXT,
                    updated_by VARCHAR(100),
                    updated_at TIMESTAMP WITHOUT TIME ZONE DEFAULT CURRENT_TIMESTAMP
                );
            """)
            for col_name, col_def in [
                ("clinical_notes", "TEXT"),
                ("updated_by", "VARCHAR(100)")
            ]:
                cur.execute("""
                    SELECT column_name FROM information_schema.columns
                    WHERE table_name = 'patient_metabolic_profiles' AND column_name = %s;
                """, (col_name,))
                if not cur.fetchone():
                    cur.execute(f"ALTER TABLE patient_metabolic_profiles ADD COLUMN {col_name} {col_def};")

            # Registros alimentares e revisão médica
            cur.execute("""
                CREATE TABLE IF NOT EXISTS diet_logs (
                    id SERIAL PRIMARY KEY,
                    user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
                    horario VARCHAR(20),
                    meal_summary TEXT NOT NULL,
                    carbs_g NUMERIC(6, 1) NOT NULL DEFAULT 0.0,
                    glycemic_index VARCHAR(30) DEFAULT 'Médio',
                    current_glucose NUMERIC(5, 1),
                    bolus_total NUMERIC(5, 2),
                    doctor_notes TEXT,
                    reviewed_by VARCHAR(100),
                    created_at TIMESTAMP WITHOUT TIME ZONE DEFAULT CURRENT_TIMESTAMP
                );
            """)
            cur.execute("CREATE INDEX IF NOT EXISTS idx_diet_logs_user ON diet_logs(user_id, created_at DESC);")

            # Chat entre médico e paciente
            cur.execute("""
                CREATE TABLE IF NOT EXISTS messages (
                    id SERIAL PRIMARY KEY,
                    sender_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
                    receiver_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
                    message TEXT NOT NULL,
                    timestamp TIMESTAMP WITHOUT TIME ZONE DEFAULT CURRENT_TIMESTAMP,
                    is_read BOOLEAN NOT NULL DEFAULT FALSE
                );
            """)
            cur.execute("CREATE INDEX IF NOT EXISTS idx_messages_pair ON messages(sender_id, receiver_id, timestamp);")
            cur.execute("CREATE INDEX IF NOT EXISTS idx_messages_unread ON messages(receiver_id, is_read);")

            # Cria contas de demonstração apenas na primeira execução
            cur.execute("SELECT value FROM settings WHERE key = 'rbac_demo_seeded';")
            if not cur.fetchone():
                cur.execute("SELECT id FROM users WHERE role = 'medico' LIMIT 1;")
                med_row = cur.fetchone()
                if not med_row:
                    cur.execute("""
                        INSERT INTO users (username, password_hash, role, full_name, crm)
                        VALUES ('medico', %s, 'medico', 'Dr. Carlos Eduardo Mendes', 'CRM-SP 145890')
                        ON CONFLICT (username) DO NOTHING
                        RETURNING id;
                    """, (hash_password("medico"),))
                    res_m = cur.fetchone()
                    med_id = res_m[0] if res_m else None
                else:
                    med_id = med_row[0]

                if med_id:
                    cur.execute("SELECT id FROM users WHERE username = 'paciente';")
                    if not cur.fetchone():
                        cur.execute("""
                            INSERT INTO users (username, password_hash, role, full_name, doctor_id)
                            VALUES ('paciente', %s, 'paciente', 'Ana Beatriz Silva', %s)
                            ON CONFLICT (username) DO NOTHING;
                        """, (hash_password("paciente"), med_id))
                    # Associa pacientes existentes sem médico ao médico inicial
                    cur.execute("UPDATE users SET doctor_id = %s WHERE role = 'paciente' AND doctor_id IS NULL;", (med_id,))

                cur.execute("""
                    INSERT INTO settings (key, value) VALUES ('rbac_demo_seeded', '1')
                    ON CONFLICT (key) DO UPDATE SET value = '1';
                """)

            conn.commit()


def init_sqlite(db_path: Path | str | None = None):
    with get_sqlite_connection(db_path) as conn:
        # Usuários
        conn.execute("""
            CREATE TABLE IF NOT EXISTS users (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                username TEXT UNIQUE NOT NULL,
                password_hash TEXT NOT NULL,
                role TEXT NOT NULL DEFAULT 'paciente',
                full_name TEXT,
                doctor_id INTEGER REFERENCES users(id) ON DELETE SET NULL,
                crm TEXT,
                created_at TEXT DEFAULT (datetime('now', 'localtime'))
            );
        """)
        u_cols = [r[1] for r in conn.execute("PRAGMA table_info(users);").fetchall()]
        if "doctor_id" not in u_cols:
            conn.execute("ALTER TABLE users ADD COLUMN doctor_id INTEGER REFERENCES users(id) ON DELETE SET NULL;")
        if "crm" not in u_cols:
            conn.execute("ALTER TABLE users ADD COLUMN crm TEXT;")

        conn.execute("UPDATE users SET role = 'paciente' WHERE role = 'user';")
        conn.execute("UPDATE users SET role = 'adm' WHERE role = 'admin';")

        # Sessões
        conn.execute("""
            CREATE TABLE IF NOT EXISTS user_sessions (
                token TEXT PRIMARY KEY,
                user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
                created_at TEXT DEFAULT (datetime('now', 'localtime')),
                expires_at TEXT NOT NULL
            );
        """)

        # Garante o administrador inicial
        admin_row = conn.execute("SELECT id FROM users WHERE username = 'adm';").fetchone()
        if not admin_row:
            admin_pwd = hash_password("adm")
            cur = conn.execute(
                "INSERT INTO users (username, password_hash, role, full_name) VALUES (?, ?, ?, ?);",
                ("adm", admin_pwd, "adm", "Administrador do Sistema")
            )
            admin_id = cur.lastrowid
        else:
            admin_id = admin_row["id"]

        # Leituras de glicose
        cur_cols = [r[1] for r in conn.execute("PRAGMA table_info(glucose_readings);").fetchall()]
        if not cur_cols:
            conn.execute("""
                CREATE TABLE glucose_readings (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
                    timestamp TEXT NOT NULL,
                    glucose REAL NOT NULL,
                    source TEXT NOT NULL DEFAULT 'manual',
                    created_at TEXT DEFAULT (datetime('now', 'localtime')),
                    UNIQUE(user_id, timestamp)
                );
            """)
        elif "user_id" not in cur_cols:
            conn.execute("PRAGMA foreign_keys = OFF;")
            conn.execute("""
                CREATE TABLE glucose_readings_migrated (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
                    timestamp TEXT NOT NULL,
                    glucose REAL NOT NULL,
                    source TEXT NOT NULL DEFAULT 'manual',
                    created_at TEXT DEFAULT (datetime('now', 'localtime')),
                    UNIQUE(user_id, timestamp)
                );
            """)
            conn.execute(f"""
                INSERT INTO glucose_readings_migrated (id, user_id, timestamp, glucose, source, created_at)
                SELECT id, {admin_id}, timestamp, glucose, source, created_at FROM glucose_readings;
            """)
            conn.execute("DROP TABLE glucose_readings;")
            conn.execute("ALTER TABLE glucose_readings_migrated RENAME TO glucose_readings;")
            conn.execute("PRAGMA foreign_keys = ON;")

        conn.execute("CREATE INDEX IF NOT EXISTS idx_readings_user_timestamp ON glucose_readings(user_id, timestamp);")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_readings_user ON glucose_readings(user_id);")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_readings_source ON glucose_readings(source);")

        # Arquivos importados
        s_cols = [r[1] for r in conn.execute("PRAGMA table_info(sources);").fetchall()]
        if not s_cols:
            conn.execute("""
                CREATE TABLE sources (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
                    filename TEXT NOT NULL,
                    rows_count INTEGER NOT NULL,
                    imported_at TEXT DEFAULT (datetime('now', 'localtime')),
                    UNIQUE(user_id, filename)
                );
            """)
        elif "user_id" not in s_cols:
            conn.execute("PRAGMA foreign_keys = OFF;")
            conn.execute("""
                CREATE TABLE sources_migrated (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
                    filename TEXT NOT NULL,
                    rows_count INTEGER NOT NULL,
                    imported_at TEXT DEFAULT (datetime('now', 'localtime')),
                    UNIQUE(user_id, filename)
                );
            """)
            conn.execute(f"""
                INSERT INTO sources_migrated (id, user_id, filename, rows_count, imported_at)
                SELECT id, {admin_id}, filename, rows_count, imported_at FROM sources;
            """)
            conn.execute("DROP TABLE sources;")
            conn.execute("ALTER TABLE sources_migrated RENAME TO sources;")
            conn.execute("PRAGMA foreign_keys = ON;")

        # Histórico de previsões
        p_cols = [r[1] for r in conn.execute("PRAGMA table_info(predictions_log);").fetchall()]
        if not p_cols:
            conn.execute("""
                CREATE TABLE predictions_log (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
                    timestamp TEXT NOT NULL,
                    current_glucose REAL NOT NULL,
                    pred_15m REAL,
                    pred_30m REAL,
                    actual_15m REAL,
                    actual_30m REAL,
                    error_15m REAL,
                    error_30m REAL,
                    model_15m TEXT,
                    model_30m TEXT,
                    confirmed_15m INTEGER NOT NULL DEFAULT 0,
                    confirmed_30m INTEGER NOT NULL DEFAULT 0,
                    confirmed_by TEXT,
                    confirmed_at TEXT,
                    created_at TEXT DEFAULT (datetime('now', 'localtime'))
                );
            """)
        else:
            if "user_id" not in p_cols:
                conn.execute("PRAGMA foreign_keys = OFF;")
                conn.execute("""
                    CREATE TABLE predictions_log_migrated (
                        id INTEGER PRIMARY KEY AUTOINCREMENT,
                        user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
                        timestamp TEXT NOT NULL,
                        current_glucose REAL NOT NULL,
                        pred_15m REAL,
                        pred_30m REAL,
                        actual_15m REAL,
                        actual_30m REAL,
                        error_15m REAL,
                        error_30m REAL,
                        model_15m TEXT,
                        model_30m TEXT,
                        created_at TEXT DEFAULT (datetime('now', 'localtime'))
                    );
                """)
                conn.execute(f"""
                    INSERT INTO predictions_log_migrated (id, user_id, timestamp, current_glucose, pred_15m, pred_30m, actual_15m, actual_30m, error_15m, error_30m, model_15m, model_30m, created_at)
                    SELECT id, {admin_id}, timestamp, current_glucose, pred_15m, pred_30m, actual_15m, actual_30m, error_15m, error_30m, model_15m, model_30m, created_at FROM predictions_log;
                """)
                conn.execute("DROP TABLE predictions_log;")
                conn.execute("ALTER TABLE predictions_log_migrated RENAME TO predictions_log;")
                conn.execute("PRAGMA foreign_keys = ON;")
                p_cols = [r[1] for r in conn.execute("PRAGMA table_info(predictions_log);").fetchall()]

            if "confirmed_15m" not in p_cols:
                conn.execute("ALTER TABLE predictions_log ADD COLUMN confirmed_15m INTEGER NOT NULL DEFAULT 0;")
            if "confirmed_30m" not in p_cols:
                conn.execute("ALTER TABLE predictions_log ADD COLUMN confirmed_30m INTEGER NOT NULL DEFAULT 0;")
            if "confirmed_by" not in p_cols:
                conn.execute("ALTER TABLE predictions_log ADD COLUMN confirmed_by TEXT;")
            if "confirmed_at" not in p_cols:
                conn.execute("ALTER TABLE predictions_log ADD COLUMN confirmed_at TEXT;")

        conn.execute("CREATE INDEX IF NOT EXISTS idx_predictions_user ON predictions_log(user_id);")

        # Configurações gerais
        conn.execute("""
            CREATE TABLE IF NOT EXISTS settings (
                key TEXT PRIMARY KEY,
                value TEXT
            );
        """)

        # Parâmetros metabólicos
        conn.execute("""
            CREATE TABLE IF NOT EXISTS patient_metabolic_profiles (
                user_id INTEGER PRIMARY KEY REFERENCES users(id) ON DELETE CASCADE,
                icr REAL NOT NULL DEFAULT 8.0,
                isf REAL NOT NULL DEFAULT 40.0,
                target_glucose REAL NOT NULL DEFAULT 100.0,
                clinical_notes TEXT,
                updated_by TEXT,
                updated_at TEXT DEFAULT (datetime('now', 'localtime'))
            );
        """)
        pm_cols = [r[1] for r in conn.execute("PRAGMA table_info(patient_metabolic_profiles);").fetchall()]
        if "clinical_notes" not in pm_cols:
            conn.execute("ALTER TABLE patient_metabolic_profiles ADD COLUMN clinical_notes TEXT;")
        if "updated_by" not in pm_cols:
            conn.execute("ALTER TABLE patient_metabolic_profiles ADD COLUMN updated_by TEXT;")

        # Registros alimentares
        conn.execute("""
            CREATE TABLE IF NOT EXISTS diet_logs (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
                horario TEXT,
                meal_summary TEXT NOT NULL,
                carbs_g REAL NOT NULL DEFAULT 0.0,
                glycemic_index TEXT DEFAULT 'Médio',
                current_glucose REAL,
                bolus_total REAL,
                doctor_notes TEXT,
                reviewed_by TEXT,
                created_at TEXT DEFAULT (datetime('now', 'localtime'))
            );
        """)
        conn.execute("CREATE INDEX IF NOT EXISTS idx_diet_logs_user ON diet_logs(user_id, created_at DESC);")

        # Mensagens diretas
        conn.execute("""
            CREATE TABLE IF NOT EXISTS messages (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                sender_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
                receiver_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
                message TEXT NOT NULL,
                timestamp TEXT NOT NULL DEFAULT (datetime('now', 'localtime')),
                is_read INTEGER NOT NULL DEFAULT 0
            );
        """)
        conn.execute("CREATE INDEX IF NOT EXISTS idx_messages_pair ON messages(sender_id, receiver_id, timestamp);")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_messages_unread ON messages(receiver_id, is_read);")

        # Cria contas de demonstração apenas na primeira execução
        seeded_flag = conn.execute("SELECT value FROM settings WHERE key = 'rbac_demo_seeded';").fetchone()
        if not seeded_flag:
            med_row = conn.execute("SELECT id FROM users WHERE role = 'medico' LIMIT 1;").fetchone()
            if not med_row:
                cur_m = conn.execute("""
                    INSERT OR IGNORE INTO users (username, password_hash, role, full_name, crm)
                    VALUES (?, ?, 'medico', 'Dr. Carlos Eduardo Mendes', 'CRM-SP 145890');
                """, ("medico", hash_password("medico")))
                med_id = cur_m.lastrowid
                if not med_id:
                    r_m = conn.execute("SELECT id FROM users WHERE username = 'medico';").fetchone()
                    med_id = r_m["id"] if r_m else None
            else:
                med_id = med_row["id"]

            if med_id:
                pac_row = conn.execute("SELECT id FROM users WHERE username = 'paciente';").fetchone()
                if not pac_row:
                    conn.execute("""
                        INSERT OR IGNORE INTO users (username, password_hash, role, full_name, doctor_id)
                        VALUES (?, ?, 'paciente', 'Ana Beatriz Silva', ?);
                    """, ("paciente", hash_password("paciente"), med_id))
                conn.execute("UPDATE users SET doctor_id = ? WHERE role = 'paciente' AND doctor_id IS NULL;", (med_id,))

            conn.execute("""
                INSERT INTO settings (key, value) VALUES ('rbac_demo_seeded', '1')
                ON CONFLICT(key) DO UPDATE SET value = '1';
            """)

        conn.commit()


def init_db(force_recheck: bool = True):
    engine = determine_engine(force_recheck=force_recheck)
    if engine == "postgres":
        try:
            init_postgres()
            return
        except Exception as e:
            print(f"[AVISO] Falha ao inicializar PostgreSQL: {e}. Usando SQLite...")
            STATE.active_engine = "sqlite"
            STATE.postgres_error = str(e)

    init_sqlite()


# Usuários, carteiras médicas e sessões

def _enrich_user_dict(u: dict | None) -> dict | None:
    if not u:
        return None
    d = dict(u)
    d["role"] = normalize_role(d.get("role"))
    if d.get("created_at") is not None:
        d["created_at"] = str(d["created_at"])
    return d


def get_user_by_id(user_id: int) -> dict | None:
    determine_engine()
    if STATE.active_engine == "postgres":
        try:
            with try_get_pg_connection() as conn:
                with conn.cursor(cursor_factory=extras.RealDictCursor) as cur:
                    cur.execute("""
                        SELECT u.id, u.username, u.role, u.full_name, u.doctor_id, u.crm, u.created_at,
                               d.full_name AS doctor_name, d.crm AS doctor_crm
                        FROM users u
                        LEFT JOIN users d ON d.id = u.doctor_id
                        WHERE u.id = %s;
                    """, (user_id,))
                    return _enrich_user_dict(cur.fetchone())
        except Exception as e:
            print(f"[ERRO POSTGRES get_user_by_id]: {e}")

    with get_sqlite_connection() as conn:
        row = conn.execute("""
            SELECT u.id, u.username, u.role, u.full_name, u.doctor_id, u.crm, u.created_at,
                   d.full_name AS doctor_name, d.crm AS doctor_crm
            FROM users u
            LEFT JOIN users d ON d.id = u.doctor_id
            WHERE u.id = ?;
        """, (user_id,)).fetchone()
        return _enrich_user_dict(row)


def get_user_by_username(username: str, include_password: bool = False) -> dict | None:
    clean_user = username.strip().lower()
    determine_engine()
    pwd_col = "u.password_hash," if include_password else ""

    if STATE.active_engine == "postgres":
        try:
            with try_get_pg_connection() as conn:
                with conn.cursor(cursor_factory=extras.RealDictCursor) as cur:
                    cur.execute(f"""
                        SELECT u.id, u.username, {pwd_col} u.role, u.full_name, u.doctor_id, u.crm, u.created_at,
                               d.full_name AS doctor_name, d.crm AS doctor_crm
                        FROM users u
                        LEFT JOIN users d ON d.id = u.doctor_id
                        WHERE LOWER(u.username) = %s;
                    """, (clean_user,))
                    return _enrich_user_dict(cur.fetchone())
        except Exception as e:
            print(f"[ERRO POSTGRES get_user_by_username]: {e}")

    with get_sqlite_connection() as conn:
        row = conn.execute(f"""
            SELECT u.id, u.username, {pwd_col} u.role, u.full_name, u.doctor_id, u.crm, u.created_at,
                   d.full_name AS doctor_name, d.crm AS doctor_crm
            FROM users u
            LEFT JOIN users d ON d.id = u.doctor_id
            WHERE LOWER(u.username) = ?;
        """, (clean_user,)).fetchone()
        return _enrich_user_dict(row)


def create_user(
    username: str,
    password: str,
    role: str = ROLE_PACIENTE,
    full_name: str | None = None,
    doctor_id: int | None = None,
    crm: str | None = None
) -> dict:
    clean_user = username.strip().lower()
    if len(clean_user) < 3:
        raise ValueError("O nome de usuário deve ter pelo menos 3 caracteres.")
    if len(password) < 3:
        raise ValueError("A senha deve ter pelo menos 3 caracteres.")

    norm_role = normalize_role(role)
    existing = get_user_by_username(clean_user)
    if existing:
        raise ValueError(f"O usuário '{clean_user}' já está cadastrado.")

    # Vínculo de médico só se aplica a pacientes
    valid_doc_id: int | None = None
    if norm_role == ROLE_PACIENTE and doctor_id is not None and str(doctor_id).strip() != "":
        doc = get_user_by_id(int(doctor_id))
        if not doc or doc["role"] != ROLE_MEDICO:
            raise ValueError("O médico selecionado para vínculo é inválido.")
        valid_doc_id = int(doctor_id)

    clean_crm = (crm or "").strip() or None if norm_role == ROLE_MEDICO else None
    pwd_hash = hash_password(password)
    clean_name = (full_name or clean_user).strip()

    determine_engine()
    if STATE.active_engine == "postgres":
        try:
            with try_get_pg_connection() as conn:
                with conn.cursor() as cur:
                    cur.execute("""
                        INSERT INTO users (username, password_hash, role, full_name, doctor_id, crm)
                        VALUES (%s, %s, %s, %s, %s, %s)
                        RETURNING id;
                    """, (clean_user, pwd_hash, norm_role, clean_name, valid_doc_id, clean_crm))
                    new_id = cur.fetchone()[0]
                    conn.commit()
            return get_user_by_id(new_id)  # type: ignore
        except Exception as e:
            print(f"[ERRO POSTGRES create_user]: {e}")

    with get_sqlite_connection() as conn:
        cur = conn.execute("""
            INSERT INTO users (username, password_hash, role, full_name, doctor_id, crm)
            VALUES (?, ?, ?, ?, ?, ?);
        """, (clean_user, pwd_hash, norm_role, clean_name, valid_doc_id, clean_crm))
        new_id = cur.lastrowid
        conn.commit()
    return get_user_by_id(int(new_id))  # type: ignore


_UNSET = object()


def update_user(
    user_id: int,
    username: str | None = None,
    password: str | None = None,
    full_name: str | None = None,
    role: str | None = None,
    doctor_id: Any = _UNSET,
    crm: Any = _UNSET
) -> dict:
    target = get_user_by_id(user_id)
    if not target:
        raise ValueError("Usuário não encontrado.")

    new_username = target["username"]
    if username is not None and username.strip():
        cand = username.strip().lower()
        if cand != target["username"]:
            if target["username"] == "adm":
                raise ValueError("O login 'adm' principal não pode ser renomeado.")
            if len(cand) < 3:
                raise ValueError("O nome de usuário deve ter pelo menos 3 caracteres.")
            other = get_user_by_username(cand)
            if other and other["id"] != user_id:
                raise ValueError(f"O login '{cand}' já está em uso por outra conta.")
            new_username = cand

    new_role = normalize_role(role) if role is not None else target["role"]
    if target["username"] == "adm" and new_role != ROLE_ADM:
        raise ValueError("O administrador principal 'adm' não pode ter seu perfil alterado.")

    new_full_name = full_name.strip() if full_name is not None and full_name.strip() else target["full_name"]

    if doctor_id is _UNSET:
        new_doc_id = target.get("doctor_id")
    elif doctor_id in (None, "", 0, "0", "none", "null"):
        new_doc_id = None
    else:
        doc = get_user_by_id(int(doctor_id))
        if not doc or doc["role"] != ROLE_MEDICO:
            raise ValueError("Médico informado para vinculação não foi encontrado.")
        new_doc_id = int(doctor_id)

    if new_role != ROLE_PACIENTE:
        new_doc_id = None

    if crm is _UNSET:
        new_crm = target.get("crm")
    else:
        new_crm = str(crm).strip() if crm else None
    if new_role != ROLE_MEDICO:
        new_crm = None

    pwd_hash: str | None = None
    if password is not None and str(password).strip() != "":
        if len(str(password).strip()) < 3:
            raise ValueError("A nova senha deve ter pelo menos 3 caracteres.")
        pwd_hash = hash_password(str(password).strip())

    determine_engine()
    if STATE.active_engine == "postgres":
        try:
            with try_get_pg_connection() as conn:
                with conn.cursor() as cur:
                    if pwd_hash:
                        cur.execute("""
                            UPDATE users
                            SET username = %s, password_hash = %s, role = %s, full_name = %s, doctor_id = %s, crm = %s
                            WHERE id = %s;
                        """, (new_username, pwd_hash, new_role, new_full_name, new_doc_id, new_crm, user_id))
                    else:
                        cur.execute("""
                            UPDATE users
                            SET username = %s, role = %s, full_name = %s, doctor_id = %s, crm = %s
                            WHERE id = %s;
                        """, (new_username, new_role, new_full_name, new_doc_id, new_crm, user_id))
                    conn.commit()
            return get_user_by_id(user_id)  # type: ignore
        except Exception as e:
            print(f"[ERRO POSTGRES update_user]: {e}")

    with get_sqlite_connection() as conn:
        if pwd_hash:
            conn.execute("""
                UPDATE users
                SET username = ?, password_hash = ?, role = ?, full_name = ?, doctor_id = ?, crm = ?
                WHERE id = ?;
            """, (new_username, pwd_hash, new_role, new_full_name, new_doc_id, new_crm, user_id))
        else:
            conn.execute("""
                UPDATE users
                SET username = ?, role = ?, full_name = ?, doctor_id = ?, crm = ?
                WHERE id = ?;
            """, (new_username, new_role, new_full_name, new_doc_id, new_crm, user_id))
        conn.commit()
    return get_user_by_id(user_id)  # type: ignore


def assign_patient_doctor(patient_id: int, doctor_id: int | None) -> dict:
    patient = get_user_by_id(patient_id)
    if not patient or patient["role"] != ROLE_PACIENTE:
        raise ValueError("Paciente não encontrado.")
    return update_user(patient_id, doctor_id=doctor_id)


def list_all_users(role_filter: str | None = None, doctor_id_filter: int | None = None) -> list[dict]:
    determine_engine()
    where_clauses_pg = []
    params_pg: list[Any] = []
    where_clauses_sq = []
    params_sq: list[Any] = []

    if role_filter:
        nr = normalize_role(role_filter)
        where_clauses_pg.append("u.role = %s")
        params_pg.append(nr)
        where_clauses_sq.append("u.role = ?")
        params_sq.append(nr)

    if doctor_id_filter is not None:
        where_clauses_pg.append("u.doctor_id = %s")
        params_pg.append(int(doctor_id_filter))
        where_clauses_sq.append("u.doctor_id = ?")
        params_sq.append(int(doctor_id_filter))

    where_sql_pg = ("WHERE " + " AND ".join(where_clauses_pg)) if where_clauses_pg else ""
    where_sql_sq = ("WHERE " + " AND ".join(where_clauses_sq)) if where_clauses_sq else ""

    if STATE.active_engine == "postgres":
        try:
            with try_get_pg_connection() as conn:
                with conn.cursor(cursor_factory=extras.RealDictCursor) as cur:
                    cur.execute(f"""
                        SELECT u.id, u.username, u.role, u.full_name, u.doctor_id, u.crm, u.created_at,
                               d.full_name AS doctor_name, d.username AS doctor_username,
                               COALESCE(r.readings_count, 0) AS readings_count,
                               r.last_glucose, r.last_reading_at,
                               COALESCE(p.predictions_count, 0) AS predictions_count,
                               COALESCE(mp.icr, 8.0) AS icr,
                               COALESCE(mp.isf, 40.0) AS isf,
                               COALESCE(mp.target_glucose, 100.0) AS target_glucose
                        FROM users u
                        LEFT JOIN users d ON d.id = u.doctor_id
                        LEFT JOIN (
                            SELECT g1.user_id,
                                   COUNT(*) AS readings_count,
                                   MAX(g1.timestamp) AS last_reading_at,
                                   (SELECT g2.glucose FROM glucose_readings g2
                                    WHERE g2.user_id = g1.user_id
                                    ORDER BY g2.timestamp DESC LIMIT 1) AS last_glucose
                            FROM glucose_readings g1
                            GROUP BY g1.user_id
                        ) r ON r.user_id = u.id
                        LEFT JOIN (
                            SELECT user_id, COUNT(*) AS predictions_count
                            FROM predictions_log GROUP BY user_id
                        ) p ON p.user_id = u.id
                        LEFT JOIN patient_metabolic_profiles mp ON mp.user_id = u.id
                        {where_sql_pg}
                        ORDER BY u.id ASC;
                    """, tuple(params_pg))
                    rows = []
                    for row in cur.fetchall():
                        item = _enrich_user_dict(dict(row))
                        if item:
                            if item.get("last_glucose") is not None:
                                item["last_glucose"] = float(item["last_glucose"])
                            if item.get("last_reading_at") is not None:
                                item["last_reading_at"] = str(item["last_reading_at"])
                            item["icr"] = float(item.get("icr") or 8.0)
                            item["isf"] = float(item.get("isf") or 40.0)
                            item["target_glucose"] = float(item.get("target_glucose") or 100.0)
                            rows.append(item)
                    return rows
        except Exception as e:
            print(f"[ERRO POSTGRES list_all_users]: {e}")

    with get_sqlite_connection() as conn:
        raw_rows = conn.execute(f"""
            SELECT u.id, u.username, u.role, u.full_name, u.doctor_id, u.crm, u.created_at,
                   d.full_name AS doctor_name, d.username AS doctor_username,
                   COALESCE(r.readings_count, 0) AS readings_count,
                   r.last_glucose, r.last_reading_at,
                   COALESCE(p.predictions_count, 0) AS predictions_count,
                   COALESCE(mp.icr, 8.0) AS icr,
                   COALESCE(mp.isf, 40.0) AS isf,
                   COALESCE(mp.target_glucose, 100.0) AS target_glucose
            FROM users u
            LEFT JOIN users d ON d.id = u.doctor_id
            LEFT JOIN (
                SELECT g1.user_id,
                       COUNT(*) AS readings_count,
                       MAX(g1.timestamp) AS last_reading_at,
                       (SELECT g2.glucose FROM glucose_readings g2
                        WHERE g2.user_id = g1.user_id
                        ORDER BY g2.timestamp DESC LIMIT 1) AS last_glucose
                FROM glucose_readings g1
                GROUP BY g1.user_id
            ) r ON r.user_id = u.id
            LEFT JOIN (
                SELECT user_id, COUNT(*) AS predictions_count
                FROM predictions_log GROUP BY user_id
            ) p ON p.user_id = u.id
            LEFT JOIN patient_metabolic_profiles mp ON mp.user_id = u.id
            {where_sql_sq}
            ORDER BY u.id ASC;
        """, tuple(params_sq)).fetchall()
        out = []
        for r in raw_rows:
            item = _enrich_user_dict(dict(r))
            if item:
                if item.get("last_glucose") is not None:
                    item["last_glucose"] = float(item["last_glucose"])
                if item.get("last_reading_at") is not None:
                    item["last_reading_at"] = str(item["last_reading_at"])
                item["icr"] = float(item.get("icr") or 8.0)
                item["isf"] = float(item.get("isf") or 40.0)
                item["target_glucose"] = float(item.get("target_glucose") or 100.0)
                out.append(item)
        return out


def get_doctors_with_portfolios() -> list[dict]:
    """Agrupa os pacientes vinculados a cada médico com totais de leituras e previsões."""
    all_users = list_all_users()
    doctors = [u for u in all_users if u["role"] == ROLE_MEDICO]
    patients = [u for u in all_users if u["role"] == ROLE_PACIENTE]

    portfolios = []
    for doc in doctors:
        linked = [p for p in patients if p.get("doctor_id") == doc["id"]]
        total_readings = sum(int(p.get("readings_count") or 0) for p in linked)
        total_preds = sum(int(p.get("predictions_count") or 0) for p in linked)
        portfolios.append({
            "doctor": doc,
            "patients": linked,
            "patients_count": len(linked),
            "total_readings": total_readings,
            "total_predictions": total_preds
        })
    return portfolios


def delete_user(user_id: int) -> bool:
    user = get_user_by_id(user_id)
    if not user:
        return False
    if user["username"] == "adm":
        raise ValueError("O usuário administrador principal 'adm' não pode ser excluído.")

    determine_engine()
    if STATE.active_engine == "postgres":
        try:
            with try_get_pg_connection() as conn:
                with conn.cursor() as cur:
                    cur.execute("UPDATE users SET doctor_id = NULL WHERE doctor_id = %s;", (user_id,))
                    cur.execute("DELETE FROM users WHERE id = %s;", (user_id,))
                    conn.commit()
                    return cur.rowcount > 0
        except Exception as e:
            print(f"[ERRO POSTGRES delete_user]: {e}")

    with get_sqlite_connection() as conn:
        conn.execute("UPDATE users SET doctor_id = NULL WHERE doctor_id = ?;", (user_id,))
        cur = conn.execute("DELETE FROM users WHERE id = ?;", (user_id,))
        conn.commit()
        return cur.rowcount > 0


def create_session(user_id: int, days_valid: int = 7) -> str:
    sid = secrets.token_hex(32)
    expires_at = (datetime.now() + timedelta(days=days_valid)).strftime("%Y-%m-%d %H:%M:%S")
    user = get_user_by_id(user_id)
    username = user["username"] if user else str(user_id)
    role = user["role"] if user else ROLE_PACIENTE

    determine_engine()
    if STATE.active_engine == "postgres":
        try:
            with try_get_pg_connection() as conn:
                with conn.cursor() as cur:
                    cur.execute("""
                        INSERT INTO user_sessions (token, user_id, expires_at)
                        VALUES (%s, %s, %s::timestamp);
                    """, (sid, user_id, expires_at))
                    conn.commit()
            return create_jwt_token(user_id, username, role, sid, days_valid=days_valid)
        except Exception as e:
            print(f"[ERRO POSTGRES create_session]: {e}")

    with get_sqlite_connection() as conn:
        conn.execute("""
            INSERT INTO user_sessions (token, user_id, expires_at)
            VALUES (?, ?, ?);
        """, (sid, user_id, expires_at))
        conn.commit()
    return create_jwt_token(user_id, username, role, sid, days_valid=days_valid)


def get_session_user(token: str) -> dict | None:
    if not token:
        return None

    # Aceita tanto JWT quanto o token de sessão direto
    sid = token
    jwt_data = decode_jwt_token(token)
    if jwt_data:
        sid = str(jwt_data.get("sid") or "")
        if not sid:
            uid = int(jwt_data["sub"])
            return get_user_by_id(uid)
    elif "." in token:
        # Token malformado ou expirado
        return None

    now_str = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    determine_engine()
    if STATE.active_engine == "postgres":
        try:
            with try_get_pg_connection() as conn:
                with conn.cursor(cursor_factory=extras.RealDictCursor) as cur:
                    cur.execute("""
                        SELECT s.user_id
                        FROM user_sessions s
                        WHERE s.token = %s AND s.expires_at > %s::timestamp;
                    """, (sid, now_str))
                    row = cur.fetchone()
                    if row:
                        return get_user_by_id(int(row["user_id"]))
                    return None
        except Exception as e:
            print(f"[ERRO POSTGRES get_session_user]: {e}")

    with get_sqlite_connection() as conn:
        row = conn.execute("""
            SELECT s.user_id
            FROM user_sessions s
            WHERE s.token = ? AND s.expires_at > ?;
        """, (sid, now_str)).fetchone()
        if row:
            return get_user_by_id(int(row["user_id"]))
        return None


def delete_session(token: str) -> None:
    if not token:
        return
    jwt_data = decode_jwt_token(token)
    sid = str(jwt_data.get("sid")) if (jwt_data and jwt_data.get("sid")) else token

    determine_engine()
    if STATE.active_engine == "postgres":
        try:
            with try_get_pg_connection() as conn:
                with conn.cursor() as cur:
                    cur.execute("DELETE FROM user_sessions WHERE token = %s;", (sid,))
                    conn.commit()
                    return
        except Exception as e:
            print(f"[ERRO POSTGRES delete_session]: {e}")

    with get_sqlite_connection() as conn:
        conn.execute("DELETE FROM user_sessions WHERE token = ?;", (sid,))
        conn.commit()


# Carga inicial a partir do CSV de exemplo

def seed_initial_data_if_empty(csv_path: Path | str | None = None, user_id: int | None = None) -> bool:
    determine_engine()
    seed_file = Path(csv_path) if csv_path else (BASE_DIR / "dados_glucose_segmentados.csv")
    if not seed_file.exists():
        return False

    if user_id is None:
        adm = get_user_by_username("adm")
        target_uid = adm["id"] if adm else 1
    else:
        target_uid = user_id

    if STATE.active_engine == "postgres":
        try:
            with try_get_pg_connection() as conn:
                with conn.cursor() as cur:
                    cur.execute("SELECT COUNT(*) FROM glucose_readings WHERE user_id = %s;", (target_uid,))
                    if cur.fetchone()[0] > 0:
                        return False

            df = pd.read_csv(seed_file)
            if "timestamp" in df.columns and "glucose" in df.columns:
                insert_readings_df(df[["timestamp", "glucose"]], user_id=target_uid, source="dados_iniciais_libre", filename=seed_file.name)
                # Popula uma amostra para o paciente de demonstração se estiver vazio
                pac = get_user_by_username("paciente")
                if pac:
                    with try_get_pg_connection() as conn2:
                        with conn2.cursor() as cur2:
                            cur2.execute("SELECT COUNT(*) FROM glucose_readings WHERE user_id = %s;", (pac["id"],))
                            if cur2.fetchone()[0] == 0:
                                sample_df = df[["timestamp", "glucose"]].tail(600).copy()
                                insert_readings_df(sample_df, user_id=pac["id"], source="libre_csv", filename="libre_paciente_inicial.csv")
                return True
        except Exception as e:
            print(f"[AVISO] Não foi possível semear no PostgreSQL: {e}")

    with get_sqlite_connection() as conn:
        cur = conn.execute("SELECT COUNT(*) FROM glucose_readings WHERE user_id = ?;", (target_uid,))
        has_adm_data = cur.fetchone()[0] > 0

    seeded_any = False
    try:
        df = pd.read_csv(seed_file)
        if "timestamp" in df.columns and "glucose" in df.columns:
            if not has_adm_data:
                insert_readings_df(df[["timestamp", "glucose"]], user_id=target_uid, source="dados_iniciais_libre", filename=seed_file.name)
                seeded_any = True
            pac = get_user_by_username("paciente")
            if pac and user_id is None:
                with get_sqlite_connection() as conn2:
                    cnt_pac = conn2.execute("SELECT COUNT(*) FROM glucose_readings WHERE user_id = ?;", (pac["id"],)).fetchone()[0]
                if cnt_pac == 0:
                    sample_df = df[["timestamp", "glucose"]].tail(600).copy()
                    insert_readings_df(sample_df, user_id=pac["id"], source="libre_csv", filename="libre_paciente_inicial.csv")
                    seeded_any = True
            return seeded_any
    except Exception as e:
        print(f"[AVISO] Não foi possível semear no SQLite: {e}")
    return False


# Leitura, gravação e edição de glicemias

def insert_readings_df(
    df: pd.DataFrame,
    user_id: int,
    source: str = "libre_csv",
    filename: str | None = None
) -> int:
    if df.empty or "timestamp" not in df.columns or "glucose" not in df.columns:
        return 0

    clean_df = df.copy()
    clean_df["timestamp"] = pd.to_datetime(clean_df["timestamp"], errors="coerce")
    clean_df["glucose"] = pd.to_numeric(clean_df["glucose"], errors="coerce")
    clean_df = clean_df.dropna(subset=["timestamp", "glucose"])
    clean_df = clean_df[(clean_df["glucose"] >= 20) & (clean_df["glucose"] <= 600)]
    clean_df["timestamp_str"] = clean_df["timestamp"].dt.strftime("%Y-%m-%d %H:%M:%S")
    clean_df["glucose"] = clean_df["glucose"].round(1)
    clean_df = clean_df.drop_duplicates(subset=["timestamp_str"], keep="last")

    records = [
        (user_id, row[0], float(row[1]), source)
        for row in clean_df[["timestamp_str", "glucose"]].itertuples(index=False, name=None)
    ]

    determine_engine()
    if STATE.active_engine == "postgres":
        try:
            with try_get_pg_connection() as conn:
                with conn.cursor() as cur:
                    query = """
                        INSERT INTO glucose_readings (user_id, timestamp, glucose, source)
                        VALUES %s
                        ON CONFLICT (user_id, timestamp) DO UPDATE SET
                            glucose = EXCLUDED.glucose,
                            source = EXCLUDED.source;
                    """
                    extras.execute_values(cur, query, records, template="(%s, %s::timestamp, %s, %s)", page_size=2000)
                    if filename:
                        cur.execute("""
                            INSERT INTO sources (user_id, filename, rows_count, imported_at)
                            VALUES (%s, %s, %s, CURRENT_TIMESTAMP)
                            ON CONFLICT (user_id, filename) DO UPDATE SET
                                rows_count = EXCLUDED.rows_count,
                                imported_at = EXCLUDED.imported_at;
                        """, (user_id, filename, len(records)))
                    conn.commit()
            update_prediction_actuals(user_id=user_id)
            return len(records)
        except Exception as e:
            print(f"[ERRO POSTGRES em insert_readings_df]: {e}. Usando SQLite...")
            STATE.active_engine = "sqlite"
            STATE.postgres_error = str(e)

    with get_sqlite_connection() as conn:
        conn.executemany("""
            INSERT INTO glucose_readings (user_id, timestamp, glucose, source)
            VALUES (?, ?, ?, ?)
            ON CONFLICT (user_id, timestamp) DO UPDATE SET
                glucose = excluded.glucose,
                source = excluded.source;
        """, records)
        if filename:
            conn.execute("""
                INSERT INTO sources (user_id, filename, rows_count, imported_at)
                VALUES (?, ?, ?, datetime('now', 'localtime'))
                ON CONFLICT(user_id, filename) DO UPDATE SET
                    rows_count = excluded.rows_count,
                    imported_at = excluded.imported_at;
            """, (user_id, filename, len(records)))
        conn.commit()

    update_prediction_actuals(user_id=user_id)
    return len(records)


def insert_single_reading(
    timestamp_val: str | datetime,
    glucose_val: float,
    user_id: int,
    source: str = "manual"
) -> int:
    dt = pd.to_datetime(timestamp_val)
    ts_str = dt.strftime("%Y-%m-%d %H:%M:%S")
    glucose_float = round(float(glucose_val), 1)

    determine_engine()
    if STATE.active_engine == "postgres":
        try:
            with try_get_pg_connection() as conn:
                with conn.cursor() as cur:
                    cur.execute("""
                        INSERT INTO glucose_readings (user_id, timestamp, glucose, source)
                        VALUES (%s, %s::timestamp, %s, %s)
                        ON CONFLICT (user_id, timestamp) DO UPDATE SET
                            glucose = EXCLUDED.glucose,
                            source = EXCLUDED.source
                        RETURNING id;
                    """, (user_id, ts_str, glucose_float, source))
                    last_id = cur.fetchone()[0]
                    conn.commit()
            update_prediction_actuals(user_id=user_id)
            return last_id
        except Exception as e:
            print(f"[ERRO POSTGRES em insert_single_reading]: {e}")
            STATE.active_engine = "sqlite"
            STATE.postgres_error = str(e)

    with get_sqlite_connection() as conn:
        conn.execute("""
            INSERT INTO glucose_readings (user_id, timestamp, glucose, source)
            VALUES (?, ?, ?, ?)
            ON CONFLICT (user_id, timestamp) DO UPDATE SET
                glucose = excluded.glucose,
                source = excluded.source;
        """, (user_id, ts_str, glucose_float, source))
        row = conn.execute("SELECT id FROM glucose_readings WHERE user_id = ? AND timestamp = ?;", (user_id, ts_str)).fetchone()
        last_id = row["id"] if row else 0
        conn.commit()

    update_prediction_actuals(user_id=user_id)
    return last_id


def get_reading_by_id(reading_id: int) -> dict | None:
    determine_engine()
    if STATE.active_engine == "postgres":
        try:
            with try_get_pg_connection() as conn:
                with conn.cursor(cursor_factory=extras.RealDictCursor) as cur:
                    cur.execute(
                        "SELECT id, user_id, timestamp, glucose, source, created_at FROM glucose_readings WHERE id = %s;",
                        (reading_id,)
                    )
                    r = cur.fetchone()
                    if r:
                        return {
                            "id": r["id"],
                            "user_id": r["user_id"],
                            "timestamp": str(r["timestamp"]),
                            "glucose": float(r["glucose"]),
                            "source": r["source"],
                            "created_at": str(r["created_at"])
                        }
                    return None
        except Exception as e:
            print(f"[ERRO POSTGRES get_reading_by_id]: {e}")

    with get_sqlite_connection() as conn:
        r = conn.execute(
            "SELECT id, user_id, timestamp, glucose, source, created_at FROM glucose_readings WHERE id = ?;",
            (reading_id,)
        ).fetchone()
        if r:
            return {
                "id": r["id"],
                "user_id": r["user_id"],
                "timestamp": str(r["timestamp"]),
                "glucose": float(r["glucose"]),
                "source": r["source"],
                "created_at": str(r["created_at"])
            }
        return None


def update_manual_reading(
    reading_id: int,
    new_glucose: float,
    new_timestamp: str | datetime,
    user_id: int | None = None
) -> dict | None:
    """Atualiza valor e horário de uma leitura manual."""
    existing = get_reading_by_id(reading_id)
    if not existing:
        return None
    if user_id is not None and existing["user_id"] != user_id:
        return None

    target_uid = existing["user_id"]
    glucose_float = round(float(new_glucose), 1)
    if glucose_float < 20 or glucose_float > 600:
        raise ValueError("A glicose deve estar entre 20 e 600 mg/dL.")

    dt = pd.to_datetime(new_timestamp)
    ts_str = dt.strftime("%Y-%m-%d %H:%M:%S")

    determine_engine()
    if STATE.active_engine == "postgres":
        try:
            with try_get_pg_connection() as conn:
                with conn.cursor() as cur:
                    # Evita conflito de unicidade caso já exista leitura no novo horário
                    cur.execute(
                        "DELETE FROM glucose_readings WHERE user_id = %s AND timestamp = %s::timestamp AND id <> %s;",
                        (target_uid, ts_str, reading_id)
                    )
                    cur.execute("""
                        UPDATE glucose_readings
                        SET glucose = %s, timestamp = %s::timestamp
                        WHERE id = %s;
                    """, (glucose_float, ts_str, reading_id))
                    conn.commit()
            update_prediction_actuals(user_id=target_uid)
            return get_reading_by_id(reading_id)
        except Exception as e:
            print(f"[ERRO POSTGRES update_manual_reading]: {e}")

    with get_sqlite_connection() as conn:
        conn.execute(
            "DELETE FROM glucose_readings WHERE user_id = ? AND timestamp = ? AND id <> ?;",
            (target_uid, ts_str, reading_id)
        )
        conn.execute("""
            UPDATE glucose_readings
            SET glucose = ?, timestamp = ?
            WHERE id = ?;
        """, (glucose_float, ts_str, reading_id))
        conn.commit()

    update_prediction_actuals(user_id=target_uid)
    return get_reading_by_id(reading_id)


def get_readings_df(user_id: int | None = None) -> pd.DataFrame:
    determine_engine()
    if STATE.active_engine == "postgres":
        try:
            with try_get_pg_connection() as conn:
                if user_id is not None:
                    query = "SELECT timestamp, glucose, source, user_id FROM glucose_readings WHERE user_id = %s ORDER BY timestamp ASC;"
                    params: tuple[Any, ...] = (user_id,)
                else:
                    query = "SELECT timestamp, glucose, source, user_id FROM glucose_readings ORDER BY user_id, timestamp ASC;"
                    params = ()
                df = pd.read_sql_query(query, conn, params=params)
            if not df.empty:
                df["timestamp"] = pd.to_datetime(df["timestamp"])
                df["glucose"] = pd.to_numeric(df["glucose"])
                return df
            return pd.DataFrame(columns=["timestamp", "glucose", "source", "user_id"])
        except Exception as e:
            print(f"[ERRO POSTGRES em get_readings_df]: {e}")
            STATE.active_engine = "sqlite"
            STATE.postgres_error = str(e)

    with get_sqlite_connection() as conn:
        if user_id is not None:
            query = "SELECT timestamp, glucose, source, user_id FROM glucose_readings WHERE user_id = ? ORDER BY timestamp ASC;"
            params = (user_id,)
        else:
            query = "SELECT timestamp, glucose, source, user_id FROM glucose_readings ORDER BY user_id, timestamp ASC;"
            params = ()
        df = pd.read_sql_query(query, conn, params=params)

    if df.empty:
        return pd.DataFrame(columns=["timestamp", "glucose", "source", "user_id"])
    df["timestamp"] = pd.to_datetime(df["timestamp"])
    df["glucose"] = pd.to_numeric(df["glucose"])
    return df


def get_latest_readings(user_id: int, n: int = 5) -> list[dict]:
    determine_engine()
    if STATE.active_engine == "postgres":
        try:
            with try_get_pg_connection() as conn:
                with conn.cursor(cursor_factory=extras.RealDictCursor) as cur:
                    cur.execute(
                        "SELECT id, timestamp, glucose, source FROM glucose_readings WHERE user_id = %s ORDER BY timestamp DESC LIMIT %s;",
                        (user_id, n)
                    )
                    rows = cur.fetchall()
            return [{"id": r["id"], "timestamp": str(r["timestamp"]), "glucose": float(r["glucose"]), "source": r["source"]} for r in rows]
        except Exception as e:
            print(f"[ERRO POSTGRES em get_latest_readings]: {e}")

    with get_sqlite_connection() as conn:
        rows = conn.execute(
            "SELECT id, timestamp, glucose, source FROM glucose_readings WHERE user_id = ? ORDER BY timestamp DESC LIMIT ?;",
            (user_id, n)
        ).fetchall()
    return [{"id": r["id"], "timestamp": str(r["timestamp"]), "glucose": float(r["glucose"]), "source": r["source"]} for r in rows]


def get_manual_readings(user_id: int, limit: int = 50) -> list[dict]:
    determine_engine()
    if STATE.active_engine == "postgres":
        try:
            with try_get_pg_connection() as conn:
                with conn.cursor(cursor_factory=extras.RealDictCursor) as cur:
                    cur.execute(
                        "SELECT id, timestamp, glucose, source, created_at FROM glucose_readings WHERE user_id = %s AND source = 'manual' ORDER BY timestamp DESC LIMIT %s;",
                        (user_id, limit)
                    )
                    rows = cur.fetchall()
            return [{
                "id": r["id"],
                "timestamp": str(r["timestamp"]),
                "glucose": float(r["glucose"]),
                "source": r["source"],
                "created_at": str(r["created_at"])
            } for r in rows]
        except Exception as e:
            print(f"[ERRO POSTGRES em get_manual_readings]: {e}")

    with get_sqlite_connection() as conn:
        rows = conn.execute(
            "SELECT id, timestamp, glucose, source, created_at FROM glucose_readings WHERE user_id = ? AND source = 'manual' ORDER BY timestamp DESC LIMIT ?;",
            (user_id, limit)
        ).fetchall()
    return [{
        "id": r["id"],
        "timestamp": str(r["timestamp"]),
        "glucose": float(r["glucose"]),
        "source": r["source"],
        "created_at": str(r["created_at"])
    } for r in rows]


def delete_reading_by_id(reading_id: int, user_id: int | None = None) -> bool:
    existing = get_reading_by_id(reading_id)
    if not existing:
        return False
    if user_id is not None and existing["user_id"] != user_id:
        return False
    target_uid = existing["user_id"]

    determine_engine()
    if STATE.active_engine == "postgres":
        try:
            with try_get_pg_connection() as conn:
                with conn.cursor() as cur:
                    cur.execute("DELETE FROM glucose_readings WHERE id = %s;", (reading_id,))
                    conn.commit()
                    deleted = cur.rowcount > 0
            if deleted:
                update_prediction_actuals(user_id=target_uid)
            return deleted
        except Exception as e:
            print(f"[ERRO POSTGRES em delete_reading_by_id]: {e}")

    with get_sqlite_connection() as conn:
        cur = conn.execute("DELETE FROM glucose_readings WHERE id = ?;", (reading_id,))
        conn.commit()
        deleted = cur.rowcount > 0
    if deleted:
        update_prediction_actuals(user_id=target_uid)
    return deleted


def clear_manual_readings(user_id: int) -> int:
    determine_engine()
    if STATE.active_engine == "postgres":
        try:
            with try_get_pg_connection() as conn:
                with conn.cursor() as cur:
                    cur.execute("DELETE FROM glucose_readings WHERE user_id = %s AND source = 'manual';", (user_id,))
                    conn.commit()
                    return cur.rowcount
        except Exception as e:
            print(f"[ERRO POSTGRES em clear_manual_readings]: {e}")

    with get_sqlite_connection() as conn:
        cur = conn.execute("DELETE FROM glucose_readings WHERE user_id = ? AND source = 'manual';", (user_id,))
        conn.commit()
        return cur.rowcount


def clear_all_data(user_id: int | None = None) -> None:
    determine_engine()
    if STATE.active_engine == "postgres":
        try:
            with try_get_pg_connection() as conn:
                with conn.cursor() as cur:
                    if user_id is not None:
                        cur.execute("DELETE FROM glucose_readings WHERE user_id = %s;", (user_id,))
                        cur.execute("DELETE FROM sources WHERE user_id = %s;", (user_id,))
                        cur.execute("DELETE FROM predictions_log WHERE user_id = %s;", (user_id,))
                    else:
                        cur.execute("TRUNCATE TABLE glucose_readings, sources, predictions_log RESTART IDENTITY;")
                    conn.commit()
                    return
        except Exception as e:
            print(f"[ERRO POSTGRES em clear_all_data]: {e}")

    with get_sqlite_connection() as conn:
        if user_id is not None:
            conn.execute("DELETE FROM glucose_readings WHERE user_id = ?;", (user_id,))
            conn.execute("DELETE FROM sources WHERE user_id = ?;", (user_id,))
            conn.execute("DELETE FROM predictions_log WHERE user_id = ?;", (user_id,))
        else:
            conn.execute("DELETE FROM glucose_readings;")
            conn.execute("DELETE FROM sources;")
            conn.execute("DELETE FROM predictions_log;")
        conn.commit()


def get_database_stats(user_id: int | None = None) -> dict:
    determine_engine()
    if STATE.active_engine == "postgres":
        try:
            with try_get_pg_connection() as conn:
                with conn.cursor(cursor_factory=extras.RealDictCursor) as cur:
                    if user_id is not None:
                        cur.execute("SELECT COUNT(*) as total FROM glucose_readings WHERE user_id = %s;", (user_id,))
                        total = cur.fetchone()["total"]
                        cur.execute("SELECT COUNT(*) as manual FROM glucose_readings WHERE user_id = %s AND source = 'manual';", (user_id,))
                        manual = cur.fetchone()["manual"]
                        sensor = total - manual
                        cur.execute("SELECT MIN(timestamp) as min_ts, MAX(timestamp) as max_ts FROM glucose_readings WHERE user_id = %s;", (user_id,))
                        min_max = cur.fetchone()
                        cur.execute("SELECT filename, rows_count, imported_at FROM sources WHERE user_id = %s ORDER BY id ASC;", (user_id,))
                        sources_rows = cur.fetchall()
                        cur.execute("SELECT COUNT(*) as cnt FROM predictions_log WHERE user_id = %s;", (user_id,))
                        preds_cnt = cur.fetchone()["cnt"]
                        cur.execute("""
                            SELECT COUNT(*) as cnt FROM predictions_log
                            WHERE user_id = %s AND (actual_15m IS NOT NULL OR actual_30m IS NOT NULL OR confirmed_15m = TRUE OR confirmed_30m = TRUE);
                        """, (user_id,))
                        eval_cnt = cur.fetchone()["cnt"]
                        cur.execute("""
                            SELECT COUNT(*) as cnt FROM predictions_log
                            WHERE user_id = %s AND (confirmed_15m = TRUE OR confirmed_30m = TRUE);
                        """, (user_id,))
                        confirmed_cnt = cur.fetchone()["cnt"]
                    else:
                        cur.execute("SELECT COUNT(*) as total FROM glucose_readings;")
                        total = cur.fetchone()["total"]
                        cur.execute("SELECT COUNT(*) as manual FROM glucose_readings WHERE source = 'manual';")
                        manual = cur.fetchone()["manual"]
                        sensor = total - manual
                        cur.execute("SELECT MIN(timestamp) as min_ts, MAX(timestamp) as max_ts FROM glucose_readings;")
                        min_max = cur.fetchone()
                        cur.execute("SELECT filename, rows_count, imported_at FROM sources ORDER BY id ASC;")
                        sources_rows = cur.fetchall()
                        cur.execute("SELECT COUNT(*) as cnt FROM predictions_log;")
                        preds_cnt = cur.fetchone()["cnt"]
                        cur.execute("""
                            SELECT COUNT(*) as cnt FROM predictions_log
                            WHERE actual_15m IS NOT NULL OR actual_30m IS NOT NULL OR confirmed_15m = TRUE OR confirmed_30m = TRUE;
                        """)
                        eval_cnt = cur.fetchone()["cnt"]
                        cur.execute("""
                            SELECT COUNT(*) as cnt FROM predictions_log
                            WHERE confirmed_15m = TRUE OR confirmed_30m = TRUE;
                        """)
                        confirmed_cnt = cur.fetchone()["cnt"]

                    cur.execute("SELECT COUNT(*) as total_users FROM users;")
                    total_users = cur.fetchone()["total_users"]

            first_ts = str(min_max["min_ts"]) if min_max and min_max["min_ts"] else None
            last_ts = str(min_max["max_ts"]) if min_max and min_max["max_ts"] else None
            span_days = 0.0
            if min_max and min_max["min_ts"] and min_max["max_ts"]:
                span_days = round((min_max["max_ts"] - min_max["min_ts"]).total_seconds() / 86400, 1)

            return {
                "engine": "PostgreSQL",
                "total_readings": total,
                "manual_readings": manual,
                "sensor_readings": sensor,
                "first_reading": first_ts,
                "last_reading": last_ts,
                "span_days": span_days,
                "sources": [{"filename": r["filename"], "rows_count": r["rows_count"], "imported_at": str(r["imported_at"])} for r in sources_rows],
                "total_predictions": preds_cnt,
                "evaluated_predictions": eval_cnt,
                "confirmed_predictions": confirmed_cnt,
                "total_users": total_users,
                "db_info": get_db_info()
            }
        except Exception as e:
            print(f"[ERRO POSTGRES em get_database_stats]: {e}")

    with get_sqlite_connection() as conn:
        if user_id is not None:
            total = conn.execute("SELECT COUNT(*) FROM glucose_readings WHERE user_id = ?;", (user_id,)).fetchone()[0]
            manual = conn.execute("SELECT COUNT(*) FROM glucose_readings WHERE user_id = ? AND source = 'manual';", (user_id,)).fetchone()[0]
            sensor = total - manual
            min_max = conn.execute("SELECT MIN(timestamp), MAX(timestamp) FROM glucose_readings WHERE user_id = ?;", (user_id,)).fetchone()
            sources_rows = conn.execute("SELECT filename, rows_count, imported_at FROM sources WHERE user_id = ? ORDER BY id ASC;", (user_id,)).fetchall()
            preds_cnt = conn.execute("SELECT COUNT(*) FROM predictions_log WHERE user_id = ?;", (user_id,)).fetchone()[0]
            eval_cnt = conn.execute("""
                SELECT COUNT(*) FROM predictions_log
                WHERE user_id = ? AND (actual_15m IS NOT NULL OR actual_30m IS NOT NULL OR confirmed_15m = 1 OR confirmed_30m = 1);
            """, (user_id,)).fetchone()[0]
            confirmed_cnt = conn.execute("""
                SELECT COUNT(*) FROM predictions_log
                WHERE user_id = ? AND (confirmed_15m = 1 OR confirmed_30m = 1);
            """, (user_id,)).fetchone()[0]
        else:
            total = conn.execute("SELECT COUNT(*) FROM glucose_readings;").fetchone()[0]
            manual = conn.execute("SELECT COUNT(*) FROM glucose_readings WHERE source = 'manual';").fetchone()[0]
            sensor = total - manual
            min_max = conn.execute("SELECT MIN(timestamp), MAX(timestamp) FROM glucose_readings;").fetchone()
            sources_rows = conn.execute("SELECT filename, rows_count, imported_at FROM sources ORDER BY id ASC;").fetchall()
            preds_cnt = conn.execute("SELECT COUNT(*) FROM predictions_log;").fetchone()[0]
            eval_cnt = conn.execute("""
                SELECT COUNT(*) FROM predictions_log
                WHERE actual_15m IS NOT NULL OR actual_30m IS NOT NULL OR confirmed_15m = 1 OR confirmed_30m = 1;
            """).fetchone()[0]
            confirmed_cnt = conn.execute("""
                SELECT COUNT(*) FROM predictions_log
                WHERE confirmed_15m = 1 OR confirmed_30m = 1;
            """).fetchone()[0]

        total_users = conn.execute("SELECT COUNT(*) FROM users;").fetchone()[0]

    first_ts = min_max[0] if min_max and min_max[0] else None
    last_ts = min_max[1] if min_max and min_max[1] else None
    span_days = 0.0
    if first_ts and last_ts:
        try:
            d0 = datetime.fromisoformat(first_ts)
            d1 = datetime.fromisoformat(last_ts)
            span_days = round((d1 - d0).total_seconds() / 86400, 1)
        except Exception:
            pass

    return {
        "engine": "SQLite",
        "total_readings": total,
        "manual_readings": manual,
        "sensor_readings": sensor,
        "first_reading": first_ts,
        "last_reading": last_ts,
        "span_days": span_days,
        "sources": [dict(r) for r in sources_rows],
        "total_predictions": preds_cnt,
        "evaluated_predictions": eval_cnt,
        "confirmed_predictions": confirmed_cnt,
        "total_users": total_users,
        "db_info": get_db_info()
    }


# Registro de previsões e cruzamento com leituras reais

def log_prediction(
    timestamp_val: str | datetime,
    current_glucose: float,
    pred_15m: float,
    pred_30m: float,
    user_id: int,
    model_15m: str = "",
    model_30m: str = ""
) -> int:
    dt = pd.to_datetime(timestamp_val)
    ts_str = dt.strftime("%Y-%m-%d %H:%M:%S")

    determine_engine()
    if STATE.active_engine == "postgres":
        try:
            with try_get_pg_connection() as conn:
                with conn.cursor() as cur:
                    cur.execute("""
                        INSERT INTO predictions_log (user_id, timestamp, current_glucose, pred_15m, pred_30m, model_15m, model_30m)
                        VALUES (%s, %s::timestamp, %s, %s, %s, %s, %s)
                        RETURNING id;
                    """, (user_id, ts_str, round(float(current_glucose), 1), round(float(pred_15m), 1), round(float(pred_30m), 1), model_15m, model_30m))
                    last_id = cur.fetchone()[0]
                    conn.commit()
                    return last_id
        except Exception as e:
            print(f"[ERRO POSTGRES em log_prediction]: {e}")

    with get_sqlite_connection() as conn:
        cur = conn.execute("""
            INSERT INTO predictions_log (user_id, timestamp, current_glucose, pred_15m, pred_30m, model_15m, model_30m)
            VALUES (?, ?, ?, ?, ?, ?, ?);
        """, (user_id, ts_str, round(float(current_glucose), 1), round(float(pred_15m), 1), round(float(pred_30m), 1), model_15m, model_30m))
        conn.commit()
        return int(cur.lastrowid)


def get_prediction_by_id(prediction_id: int) -> dict | None:
    determine_engine()
    if STATE.active_engine == "postgres":
        try:
            with try_get_pg_connection() as conn:
                with conn.cursor(cursor_factory=extras.RealDictCursor) as cur:
                    cur.execute("SELECT * FROM predictions_log WHERE id = %s;", (prediction_id,))
                    r = cur.fetchone()
                    if r:
                        d = dict(r)
                        d["timestamp"] = str(d["timestamp"])
                        d["confirmed_15m"] = bool(d.get("confirmed_15m"))
                        d["confirmed_30m"] = bool(d.get("confirmed_30m"))
                        return d
                    return None
        except Exception as e:
            print(f"[ERRO POSTGRES get_prediction_by_id]: {e}")

    with get_sqlite_connection() as conn:
        r = conn.execute("SELECT * FROM predictions_log WHERE id = ?;", (prediction_id,)).fetchone()
        if r:
            d = dict(r)
            d["timestamp"] = str(d["timestamp"])
            d["confirmed_15m"] = bool(d.get("confirmed_15m"))
            d["confirmed_30m"] = bool(d.get("confirmed_30m"))
            return d
        return None


def confirm_prediction_feedback(
    prediction_id: int,
    horizon: int,
    confirmed: bool,
    actor_label: str,
    user_id: int | None = None
) -> dict | None:
    """Marca ou desmarca uma previsão como confirmada manualmente."""
    if horizon not in (15, 30):
        raise ValueError("Horizonte inválido: escolha 15 ou 30 minutos.")

    row = get_prediction_by_id(prediction_id)
    if not row:
        return None
    if user_id is not None and row["user_id"] != user_id:
        return None

    col_conf = "confirmed_15m" if horizon == 15 else "confirmed_30m"
    col_act = "actual_15m" if horizon == 15 else "actual_30m"
    col_err = "error_15m" if horizon == 15 else "error_30m"
    pred_val = float(row["pred_15m"] if horizon == 15 else row["pred_30m"])
    now_str = datetime.now().strftime("%Y-%m-%d %H:%M:%S")

    determine_engine()
    if STATE.active_engine == "postgres":
        try:
            with try_get_pg_connection() as conn:
                with conn.cursor() as cur:
                    if confirmed:
                        # Se ainda não houver leitura real no horário, usa o valor previsto como referência
                        cur.execute(f"""
                            UPDATE predictions_log
                            SET {col_conf} = TRUE,
                                {col_act} = COALESCE({col_act}, %s),
                                {col_err} = COALESCE({col_err}, 0.0),
                                confirmed_by = %s,
                                confirmed_at = %s::timestamp
                            WHERE id = %s;
                        """, (pred_val, actor_label, now_str, prediction_id))
                    else:
                        cur.execute(f"""
                            UPDATE predictions_log
                            SET {col_conf} = FALSE
                            WHERE id = %s;
                        """, (prediction_id,))
                    conn.commit()
            return get_prediction_by_id(prediction_id)
        except Exception as e:
            print(f"[ERRO POSTGRES confirm_prediction_feedback]: {e}")

    with get_sqlite_connection() as conn:
        if confirmed:
            conn.execute(f"""
                UPDATE predictions_log
                SET {col_conf} = 1,
                    {col_act} = COALESCE({col_act}, ?),
                    {col_err} = COALESCE({col_err}, 0.0),
                    confirmed_by = ?,
                    confirmed_at = ?
                WHERE id = ?;
            """, (pred_val, actor_label, now_str, prediction_id))
        else:
            conn.execute(f"""
                UPDATE predictions_log
                SET {col_conf} = 0
                WHERE id = ?;
            """, (prediction_id,))
        conn.commit()
    return get_prediction_by_id(prediction_id)


def update_prediction_actuals(user_id: int | None = None) -> None:
    determine_engine()
    if STATE.active_engine == "postgres":
        try:
            with try_get_pg_connection() as conn:
                with conn.cursor(cursor_factory=extras.RealDictCursor) as cur:
                    if user_id is not None:
                        cur.execute("""
                            SELECT id, user_id, timestamp, pred_15m, pred_30m, actual_15m, actual_30m, confirmed_15m, confirmed_30m
                            FROM predictions_log
                            WHERE user_id = %s;
                        """, (user_id,))
                    else:
                        cur.execute("""
                            SELECT id, user_id, timestamp, pred_15m, pred_30m, actual_15m, actual_30m, confirmed_15m, confirmed_30m
                            FROM predictions_log;
                        """)
                    rows = cur.fetchall()
                    for row in rows:
                        pred_id = row["id"]
                        p_uid = row["user_id"]
                        base_dt = pd.to_datetime(row["timestamp"])

                        if row["pred_15m"] is not None:
                            t15 = base_dt + timedelta(minutes=15)
                            t15_min = (t15 - timedelta(minutes=7, seconds=30)).strftime("%Y-%m-%d %H:%M:%S")
                            t15_max = (t15 + timedelta(minutes=7, seconds=30)).strftime("%Y-%m-%d %H:%M:%S")
                            cur.execute("""
                                SELECT glucose FROM glucose_readings
                                WHERE user_id = %s AND timestamp >= %s::timestamp AND timestamp <= %s::timestamp
                                ORDER BY ABS(EXTRACT(EPOCH FROM timestamp) - EXTRACT(EPOCH FROM %s::timestamp)) ASC
                                LIMIT 1;
                            """, (p_uid, t15_min, t15_max, t15.strftime("%Y-%m-%d %H:%M:%S")))
                            cand15 = cur.fetchone()
                            if cand15:
                                act15 = float(cand15["glucose"])
                                err15 = round(abs(float(row["pred_15m"]) - act15), 1)
                                cur.execute("UPDATE predictions_log SET actual_15m = %s, error_15m = %s WHERE id = %s;", (act15, err15, pred_id))

                        if row["pred_30m"] is not None:
                            t30 = base_dt + timedelta(minutes=30)
                            t30_min = (t30 - timedelta(minutes=7, seconds=30)).strftime("%Y-%m-%d %H:%M:%S")
                            t30_max = (t30 + timedelta(minutes=7, seconds=30)).strftime("%Y-%m-%d %H:%M:%S")
                            cur.execute("""
                                SELECT glucose FROM glucose_readings
                                WHERE user_id = %s AND timestamp >= %s::timestamp AND timestamp <= %s::timestamp
                                ORDER BY ABS(EXTRACT(EPOCH FROM timestamp) - EXTRACT(EPOCH FROM %s::timestamp)) ASC
                                LIMIT 1;
                            """, (p_uid, t30_min, t30_max, t30.strftime("%Y-%m-%d %H:%M:%S")))
                            cand30 = cur.fetchone()
                            if cand30:
                                act30 = float(cand30["glucose"])
                                err30 = round(abs(float(row["pred_30m"]) - act30), 1)
                                cur.execute("UPDATE predictions_log SET actual_30m = %s, error_30m = %s WHERE id = %s;", (act30, err30, pred_id))
                    conn.commit()
            return
        except Exception as e:
            print(f"[ERRO POSTGRES em update_prediction_actuals]: {e}")

    with get_sqlite_connection() as conn:
        if user_id is not None:
            rows = conn.execute("""
                SELECT id, user_id, timestamp, pred_15m, pred_30m, actual_15m, actual_30m, confirmed_15m, confirmed_30m
                FROM predictions_log
                WHERE user_id = ?;
            """, (user_id,)).fetchall()
        else:
            rows = conn.execute("""
                SELECT id, user_id, timestamp, pred_15m, pred_30m, actual_15m, actual_30m, confirmed_15m, confirmed_30m
                FROM predictions_log;
            """).fetchall()

        for row in rows:
            pred_id = row["id"]
            p_uid = row["user_id"]
            try:
                base_dt = pd.to_datetime(row["timestamp"])
            except Exception:
                continue

            if row["pred_15m"] is not None:
                t15 = base_dt + timedelta(minutes=15)
                t15_min = (t15 - timedelta(minutes=7, seconds=30)).strftime("%Y-%m-%d %H:%M:%S")
                t15_max = (t15 + timedelta(minutes=7, seconds=30)).strftime("%Y-%m-%d %H:%M:%S")
                cand15 = conn.execute("""
                    SELECT glucose FROM glucose_readings
                    WHERE user_id = ? AND timestamp >= ? AND timestamp <= ?
                    ORDER BY ABS(strftime('%s', timestamp) - strftime('%s', ?)) ASC
                    LIMIT 1;
                """, (p_uid, t15_min, t15_max, t15.strftime("%Y-%m-%d %H:%M:%S"))).fetchone()
                if cand15:
                    actual15 = float(cand15["glucose"])
                    err15 = round(abs(float(row["pred_15m"]) - actual15), 1)
                    conn.execute("UPDATE predictions_log SET actual_15m = ?, error_15m = ? WHERE id = ?;", (actual15, err15, pred_id))

            if row["pred_30m"] is not None:
                t30 = base_dt + timedelta(minutes=30)
                t30_min = (t30 - timedelta(minutes=7, seconds=30)).strftime("%Y-%m-%d %H:%M:%S")
                t30_max = (t30 + timedelta(minutes=7, seconds=30)).strftime("%Y-%m-%d %H:%M:%S")
                cand30 = conn.execute("""
                    SELECT glucose FROM glucose_readings
                    WHERE user_id = ? AND timestamp >= ? AND timestamp <= ?
                    ORDER BY ABS(strftime('%s', timestamp) - strftime('%s', ?)) ASC
                    LIMIT 1;
                """, (p_uid, t30_min, t30_max, t30.strftime("%Y-%m-%d %H:%M:%S"))).fetchone()
                if cand30:
                    actual30 = float(cand30["glucose"])
                    err30 = round(abs(float(row["pred_30m"]) - actual30), 1)
                    conn.execute("UPDATE predictions_log SET actual_30m = ?, error_30m = ? WHERE id = ?;", (actual30, err30, pred_id))

        conn.commit()


def get_prediction_logs(user_id: int, limit: int = 20) -> list[dict]:
    determine_engine()
    if STATE.active_engine == "postgres":
        try:
            with try_get_pg_connection() as conn:
                with conn.cursor(cursor_factory=extras.RealDictCursor) as cur:
                    cur.execute("""
                        SELECT id, timestamp, current_glucose, pred_15m, pred_30m,
                               actual_15m, actual_30m, error_15m, error_30m,
                               model_15m, model_30m,
                               confirmed_15m, confirmed_30m, confirmed_by, confirmed_at,
                               created_at
                        FROM predictions_log
                        WHERE user_id = %s
                        ORDER BY id DESC
                        LIMIT %s;
                    """, (user_id, limit))
                    rows = cur.fetchall()
            out = []
            for r in rows:
                d = dict(r)
                for k in ("current_glucose", "pred_15m", "pred_30m", "actual_15m", "actual_30m", "error_15m", "error_30m"):
                    if d.get(k) is not None:
                        d[k] = float(d[k])
                d["timestamp"] = str(d["timestamp"])
                d["created_at"] = str(d["created_at"])
                d["confirmed_15m"] = bool(d.get("confirmed_15m"))
                d["confirmed_30m"] = bool(d.get("confirmed_30m"))
                if d.get("confirmed_at"):
                    d["confirmed_at"] = str(d["confirmed_at"])
                out.append(d)
            return out
        except Exception as e:
            print(f"[ERRO POSTGRES em get_prediction_logs]: {e}")

    with get_sqlite_connection() as conn:
        rows = conn.execute("""
            SELECT id, timestamp, current_glucose, pred_15m, pred_30m,
                   actual_15m, actual_30m, error_15m, error_30m,
                   model_15m, model_30m,
                   confirmed_15m, confirmed_30m, confirmed_by, confirmed_at,
                   created_at
            FROM predictions_log
            WHERE user_id = ?
            ORDER BY id DESC
            LIMIT ?;
        """, (user_id, limit)).fetchall()
    out = []
    for r in rows:
        d = dict(r)
        d["confirmed_15m"] = bool(d.get("confirmed_15m"))
        d["confirmed_30m"] = bool(d.get("confirmed_30m"))
        out.append(d)
    return out


def get_sources(user_id: int | None = None) -> list[str]:
    stats = get_database_stats(user_id=user_id)
    return [s["filename"] for s in stats.get("sources", [])]


def get_setting(key: str, default: str | None = None) -> str | None:
    determine_engine()
    if STATE.active_engine == "postgres":
        try:
            with try_get_pg_connection() as conn:
                with conn.cursor() as cur:
                    cur.execute("SELECT value FROM settings WHERE key = %s;", (key,))
                    row = cur.fetchone()
                    return row[0] if row else default
        except Exception:
            pass

    with get_sqlite_connection() as conn:
        row = conn.execute("SELECT value FROM settings WHERE key = ?;", (key,)).fetchone()
        return row["value"] if row else default


def set_setting(key: str, value: str) -> None:
    determine_engine()
    if STATE.active_engine == "postgres":
        try:
            with try_get_pg_connection() as conn:
                with conn.cursor() as cur:
                    cur.execute("""
                        INSERT INTO settings (key, value) VALUES (%s, %s)
                        ON CONFLICT (key) DO UPDATE SET value = EXCLUDED.value;
                    """, (key, str(value)))
                    conn.commit()
            return
        except Exception:
            pass

    with get_sqlite_connection() as conn:
        conn.execute("""
            INSERT INTO settings (key, value) VALUES (?, ?)
            ON CONFLICT(key) DO UPDATE SET value = excluded.value;
        """, (key, str(value)))
        conn.commit()


# Parâmetros metabólicos (ICR, ISF e glicose alvo)

def get_patient_profile(user_id: int) -> dict:
    determine_engine()
    default_profile = {
        "user_id": user_id,
        "icr": 8.0,
        "isf": 40.0,
        "target_glucose": 100.0,
        "clinical_notes": "",
        "updated_by": None,
        "updated_at": datetime.now().isoformat()
    }

    if STATE.active_engine == "postgres":
        try:
            with try_get_pg_connection() as conn:
                with conn.cursor(cursor_factory=extras.RealDictCursor) as cur:
                    cur.execute("""
                        SELECT user_id, icr, isf, target_glucose, clinical_notes, updated_by, updated_at
                        FROM patient_metabolic_profiles
                        WHERE user_id = %s;
                    """, (user_id,))
                    row = cur.fetchone()
                    if row:
                        return {
                            "user_id": row["user_id"],
                            "icr": float(row["icr"]),
                            "isf": float(row["isf"]),
                            "target_glucose": float(row["target_glucose"]),
                            "clinical_notes": row.get("clinical_notes") or "",
                            "updated_by": row.get("updated_by"),
                            "updated_at": str(row["updated_at"])
                        }
                    cur.execute("""
                        INSERT INTO patient_metabolic_profiles (user_id, icr, isf, target_glucose)
                        VALUES (%s, 8.0, 40.0, 100.0)
                        ON CONFLICT (user_id) DO NOTHING;
                    """, (user_id,))
                    conn.commit()
                    return default_profile
        except Exception as e:
            print(f"[ERRO POSTGRES get_patient_profile]: {e}")

    with get_sqlite_connection() as conn:
        row = conn.execute("""
            SELECT user_id, icr, isf, target_glucose, clinical_notes, updated_by, updated_at
            FROM patient_metabolic_profiles
            WHERE user_id = ?;
        """, (user_id,)).fetchone()
        if row:
            return {
                "user_id": row["user_id"],
                "icr": float(row["icr"]),
                "isf": float(row["isf"]),
                "target_glucose": float(row["target_glucose"]),
                "clinical_notes": row["clinical_notes"] or "",
                "updated_by": row["updated_by"],
                "updated_at": str(row["updated_at"])
            }
        conn.execute("""
            INSERT OR IGNORE INTO patient_metabolic_profiles (user_id, icr, isf, target_glucose)
            VALUES (?, 8.0, 40.0, 100.0);
        """, (user_id,))
        conn.commit()
        return default_profile


def save_patient_profile(
    user_id: int,
    icr: float,
    isf: float,
    target_glucose: float,
    clinical_notes: str | None = None,
    updated_by: str | None = None
) -> dict:
    if icr <= 0:
        raise ValueError("A Razão Carboidrato/Insulina (ICR) deve ser maior que zero.")
    if isf <= 0:
        raise ValueError("O Fator de Sensibilidade à Insulina (ISF) deve ser maior que zero.")
    if target_glucose < 50 or target_glucose > 300:
        raise ValueError("A Glicose Alvo deve estar entre 50 e 300 mg/dL.")

    existing = get_patient_profile(user_id)
    notes_val = clinical_notes if clinical_notes is not None else existing.get("clinical_notes", "")

    determine_engine()
    if STATE.active_engine == "postgres":
        try:
            with try_get_pg_connection() as conn:
                with conn.cursor(cursor_factory=extras.RealDictCursor) as cur:
                    cur.execute("""
                        INSERT INTO patient_metabolic_profiles (user_id, icr, isf, target_glucose, clinical_notes, updated_by, updated_at)
                        VALUES (%s, %s, %s, %s, %s, %s, CURRENT_TIMESTAMP)
                        ON CONFLICT (user_id) DO UPDATE SET
                            icr = EXCLUDED.icr,
                            isf = EXCLUDED.isf,
                            target_glucose = EXCLUDED.target_glucose,
                            clinical_notes = EXCLUDED.clinical_notes,
                            updated_by = EXCLUDED.updated_by,
                            updated_at = CURRENT_TIMESTAMP
                        RETURNING user_id, icr, isf, target_glucose, clinical_notes, updated_by, updated_at;
                    """, (user_id, float(icr), float(isf), float(target_glucose), notes_val, updated_by))
                    row = cur.fetchone()
                    conn.commit()
                    return {
                        "user_id": row["user_id"],
                        "icr": float(row["icr"]),
                        "isf": float(row["isf"]),
                        "target_glucose": float(row["target_glucose"]),
                        "clinical_notes": row.get("clinical_notes") or "",
                        "updated_by": row.get("updated_by"),
                        "updated_at": str(row["updated_at"])
                    }
        except Exception as e:
            print(f"[ERRO POSTGRES save_patient_profile]: {e}")

    with get_sqlite_connection() as conn:
        conn.execute("""
            INSERT INTO patient_metabolic_profiles (user_id, icr, isf, target_glucose, clinical_notes, updated_by, updated_at)
            VALUES (?, ?, ?, ?, ?, ?, datetime('now', 'localtime'))
            ON CONFLICT(user_id) DO UPDATE SET
                icr = excluded.icr,
                isf = excluded.isf,
                target_glucose = excluded.target_glucose,
                clinical_notes = excluded.clinical_notes,
                updated_by = excluded.updated_by,
                updated_at = datetime('now', 'localtime');
        """, (user_id, float(icr), float(isf), float(target_glucose), notes_val, updated_by))
        conn.commit()

    return get_patient_profile(user_id)


# Histórico alimentar e parecer médico

def log_diet_entry(
    user_id: int,
    horario: str,
    meal_summary: str,
    carbs_g: float,
    glycemic_index: str = "Médio",
    current_glucose: float | None = None,
    bolus_total: float | None = None
) -> int:
    determine_engine()
    if STATE.active_engine == "postgres":
        try:
            with try_get_pg_connection() as conn:
                with conn.cursor() as cur:
                    cur.execute("""
                        INSERT INTO diet_logs (user_id, horario, meal_summary, carbs_g, glycemic_index, current_glucose, bolus_total)
                        VALUES (%s, %s, %s, %s, %s, %s, %s)
                        RETURNING id;
                    """, (user_id, horario, meal_summary, float(carbs_g), glycemic_index, current_glucose, bolus_total))
                    new_id = cur.fetchone()[0]
                    conn.commit()
                    return new_id
        except Exception as e:
            print(f"[ERRO POSTGRES log_diet_entry]: {e}")

    with get_sqlite_connection() as conn:
        cur = conn.execute("""
            INSERT INTO diet_logs (user_id, horario, meal_summary, carbs_g, glycemic_index, current_glucose, bolus_total)
            VALUES (?, ?, ?, ?, ?, ?, ?);
        """, (user_id, horario, meal_summary, float(carbs_g), glycemic_index, current_glucose, bolus_total))
        conn.commit()
        return int(cur.lastrowid)


def get_diet_logs(user_id: int, limit: int = 20) -> list[dict]:
    determine_engine()
    if STATE.active_engine == "postgres":
        try:
            with try_get_pg_connection() as conn:
                with conn.cursor(cursor_factory=extras.RealDictCursor) as cur:
                    cur.execute("""
                        SELECT id, user_id, horario, meal_summary, carbs_g, glycemic_index,
                               current_glucose, bolus_total, doctor_notes, reviewed_by, created_at
                        FROM diet_logs
                        WHERE user_id = %s
                        ORDER BY id DESC LIMIT %s;
                    """, (user_id, limit))
                    rows = cur.fetchall()
            out = []
            for r in rows:
                d = dict(r)
                d["carbs_g"] = float(d["carbs_g"]) if d.get("carbs_g") is not None else 0.0
                if d.get("current_glucose") is not None:
                    d["current_glucose"] = float(d["current_glucose"])
                if d.get("bolus_total") is not None:
                    d["bolus_total"] = float(d["bolus_total"])
                d["created_at"] = str(d["created_at"])
                out.append(d)
            return out
        except Exception as e:
            print(f"[ERRO POSTGRES get_diet_logs]: {e}")

    with get_sqlite_connection() as conn:
        rows = conn.execute("""
            SELECT id, user_id, horario, meal_summary, carbs_g, glycemic_index,
                   current_glucose, bolus_total, doctor_notes, reviewed_by, created_at
            FROM diet_logs
            WHERE user_id = ?
            ORDER BY id DESC LIMIT ?;
        """, (user_id, limit)).fetchall()
    return [dict(r) for r in rows]


def review_diet_entry(diet_id: int, doctor_notes: str, reviewed_by: str) -> dict | None:
    determine_engine()
    if STATE.active_engine == "postgres":
        try:
            with try_get_pg_connection() as conn:
                with conn.cursor(cursor_factory=extras.RealDictCursor) as cur:
                    cur.execute("""
                        UPDATE diet_logs
                        SET doctor_notes = %s, reviewed_by = %s
                        WHERE id = %s
                        RETURNING *;
                    """, (doctor_notes.strip(), reviewed_by, diet_id))
                    r = cur.fetchone()
                    conn.commit()
                    if r:
                        d = dict(r)
                        d["carbs_g"] = float(d["carbs_g"])
                        d["created_at"] = str(d["created_at"])
                        return d
                    return None
        except Exception as e:
            print(f"[ERRO POSTGRES review_diet_entry]: {e}")

    with get_sqlite_connection() as conn:
        conn.execute("""
            UPDATE diet_logs SET doctor_notes = ?, reviewed_by = ? WHERE id = ?;
        """, (doctor_notes.strip(), reviewed_by, diet_id))
        row = conn.execute("SELECT * FROM diet_logs WHERE id = ?;", (diet_id,)).fetchone()
        conn.commit()
        return dict(row) if row else None


def get_diet_entry_by_id(diet_id: int) -> dict | None:
    determine_engine()
    if STATE.active_engine == "postgres":
        try:
            with try_get_pg_connection() as conn:
                with conn.cursor(cursor_factory=extras.RealDictCursor) as cur:
                    cur.execute("SELECT * FROM diet_logs WHERE id = %s;", (diet_id,))
                    r = cur.fetchone()
                    return dict(r) if r else None
        except Exception:
            pass
    with get_sqlite_connection() as conn:
        r = conn.execute("SELECT * FROM diet_logs WHERE id = ?;", (diet_id,)).fetchone()
        return dict(r) if r else None


# Mensagens diretas entre médico e paciente

def send_direct_message(sender_id: int, receiver_id: int, message: str) -> dict:
    clean_msg = str(message or "").strip()
    if not clean_msg:
        raise ValueError("A mensagem não pode estar vazia.")
    now_str = datetime.now().strftime("%Y-%m-%d %H:%M:%S")

    determine_engine()
    if STATE.active_engine == "postgres":
        try:
            with try_get_pg_connection() as conn:
                with conn.cursor(cursor_factory=extras.RealDictCursor) as cur:
                    cur.execute("""
                        INSERT INTO messages (sender_id, receiver_id, message, timestamp, is_read)
                        VALUES (%s, %s, %s, %s::timestamp, FALSE)
                        RETURNING id, sender_id, receiver_id, message, timestamp, is_read;
                    """, (sender_id, receiver_id, clean_msg, now_str))
                    r = cur.fetchone()
                    conn.commit()
                    return {
                        "id": r["id"],
                        "sender_id": r["sender_id"],
                        "receiver_id": r["receiver_id"],
                        "message": r["message"],
                        "timestamp": str(r["timestamp"]),
                        "is_read": bool(r["is_read"])
                    }
        except Exception as e:
            print(f"[ERRO POSTGRES send_direct_message]: {e}")

    with get_sqlite_connection() as conn:
        cur = conn.execute("""
            INSERT INTO messages (sender_id, receiver_id, message, timestamp, is_read)
            VALUES (?, ?, ?, ?, 0);
        """, (sender_id, receiver_id, clean_msg, now_str))
        msg_id = int(cur.lastrowid)
        conn.commit()

    return {
        "id": msg_id,
        "sender_id": sender_id,
        "receiver_id": receiver_id,
        "message": clean_msg,
        "timestamp": now_str,
        "is_read": False
    }


def mark_messages_as_read(receiver_id: int, sender_id: int) -> int:
    determine_engine()
    if STATE.active_engine == "postgres":
        try:
            with try_get_pg_connection() as conn:
                with conn.cursor() as cur:
                    cur.execute("""
                        UPDATE messages
                        SET is_read = TRUE
                        WHERE receiver_id = %s AND sender_id = %s AND is_read = FALSE;
                    """, (receiver_id, sender_id))
                    conn.commit()
                    return cur.rowcount
        except Exception as e:
            print(f"[ERRO POSTGRES mark_messages_as_read]: {e}")

    with get_sqlite_connection() as conn:
        cur = conn.execute("""
            UPDATE messages
            SET is_read = 1
            WHERE receiver_id = ? AND sender_id = ? AND is_read = 0;
        """, (receiver_id, sender_id))
        conn.commit()
        return cur.rowcount


def get_messages_between(
    user_a: int,
    user_b: int,
    limit: int = 100,
    mark_read_for: int | None = None
) -> list[dict]:
    if mark_read_for is not None:
        other = user_b if mark_read_for == user_a else user_a
        mark_messages_as_read(receiver_id=mark_read_for, sender_id=other)

    determine_engine()
    if STATE.active_engine == "postgres":
        try:
            with try_get_pg_connection() as conn:
                with conn.cursor(cursor_factory=extras.RealDictCursor) as cur:
                    cur.execute("""
                        SELECT m.id, m.sender_id, m.receiver_id, m.message, m.timestamp, m.is_read,
                               u.full_name AS sender_name, u.role AS sender_role
                        FROM messages m
                        JOIN users u ON u.id = m.sender_id
                        WHERE (m.sender_id = %s AND m.receiver_id = %s)
                           OR (m.sender_id = %s AND m.receiver_id = %s)
                        ORDER BY m.id DESC
                        LIMIT %s;
                    """, (user_a, user_b, user_b, user_a, limit))
                    rows = list(reversed(cur.fetchall()))
            return [{
                "id": r["id"],
                "sender_id": r["sender_id"],
                "receiver_id": r["receiver_id"],
                "sender_name": r["sender_name"],
                "sender_role": normalize_role(r["sender_role"]),
                "message": r["message"],
                "timestamp": str(r["timestamp"]),
                "is_read": bool(r["is_read"])
            } for r in rows]
        except Exception as e:
            print(f"[ERRO POSTGRES get_messages_between]: {e}")

    with get_sqlite_connection() as conn:
        rows = conn.execute("""
            SELECT m.id, m.sender_id, m.receiver_id, m.message, m.timestamp, m.is_read,
                   u.full_name AS sender_name, u.role AS sender_role
            FROM messages m
            JOIN users u ON u.id = m.sender_id
            WHERE (m.sender_id = ? AND m.receiver_id = ?)
               OR (m.sender_id = ? AND m.receiver_id = ?)
            ORDER BY m.id DESC
            LIMIT ?;
        """, (user_a, user_b, user_b, user_a, limit)).fetchall()
    rows = list(reversed(rows))
    return [{
        "id": r["id"],
        "sender_id": r["sender_id"],
        "receiver_id": r["receiver_id"],
        "sender_name": r["sender_name"],
        "sender_role": normalize_role(r["sender_role"]),
        "message": r["message"],
        "timestamp": str(r["timestamp"]),
        "is_read": bool(r["is_read"])
    } for r in rows]


def get_unread_counts_for_user(user_id: int) -> dict[int, int]:
    """Retorna a contagem de mensagens não lidas agrupada por remetente."""
    determine_engine()
    if STATE.active_engine == "postgres":
        try:
            with try_get_pg_connection() as conn:
                with conn.cursor() as cur:
                    cur.execute("""
                        SELECT sender_id, COUNT(*)
                        FROM messages
                        WHERE receiver_id = %s AND is_read = FALSE
                        GROUP BY sender_id;
                    """, (user_id,))
                    return {int(r[0]): int(r[1]) for r in cur.fetchall()}
        except Exception as e:
            print(f"[ERRO POSTGRES get_unread_counts_for_user]: {e}")

    with get_sqlite_connection() as conn:
        rows = conn.execute("""
            SELECT sender_id, COUNT(*)
            FROM messages
            WHERE receiver_id = ? AND is_read = 0
            GROUP BY sender_id;
        """, (user_id,)).fetchall()
        return {int(r[0]): int(r[1]) for r in rows}
