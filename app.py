"""
Clínica — servidor con base de datos SQLite (local) o PostgreSQL
(automático si existe la variable de entorno DATABASE_URL, por ejemplo en
Render) y login.

Cómo correr localmente:
    pip install -r requirements.txt
    python app.py

Luego abre http://localhost:5000 en tu navegador.
La primera vez te pedirá un código de activación y crear el usuario
administrador.
"""

import hashlib
import os
import re
import shutil
import sys
import smtplib
import sqlite3
import threading
import time
import uuid
import zipfile
import base64
import json
import platform
from datetime import datetime, timedelta
from email.mime.application import MIMEApplication
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from functools import wraps

from flask import (Flask, Response, abort, g, jsonify, make_response,
                    redirect, render_template, render_template_string,
                    request, send_from_directory, session, url_for)
from werkzeug.security import check_password_hash, generate_password_hash
from werkzeug.utils import secure_filename

# Interfaz de escritorio opcional. Si pywebview no está disponible,
# la aplicación continúa funcionando en el navegador del equipo.
try:
    import webview
except ImportError:
    webview = None

BASE_DIR = os.path.dirname(sys.executable) if getattr(sys, "frozen", False) else os.path.dirname(os.path.abspath(__file__))
RESOURCE_DIR = getattr(sys, "_MEIPASS", BASE_DIR)
DB_PATH = os.path.join(BASE_DIR, "clinica.db")
SECRET_KEY_PATH = os.path.join(BASE_DIR, ".secret_key")
UPLOAD_DIR = os.path.join(BASE_DIR, "uploads")
PHOTOS_DIR = os.path.join(UPLOAD_DIR, "photos")
DOCS_DIR = os.path.join(UPLOAD_DIR, "documents")
BRANDING_DIR = os.path.join(UPLOAD_DIR, "branding")
BACKUP_DIR = os.path.join(BASE_DIR, "backups")
MAX_BACKUPS = 60
CURRENT_TERMS_VERSION = "V6.4-2026-09"
LEGACY_TERMS_VERSION = "V6.1-legacy"
LICENSE_PRODUCT = "ClinicaSV Medical"

_PRINT_JOBS = {}
_PRINT_JOBS_LOCK = threading.Lock()
_PRINT_JOB_TTL = 15 * 60
for d in (UPLOAD_DIR, PHOTOS_DIR, DOCS_DIR, BRANDING_DIR, BACKUP_DIR):
    os.makedirs(d, exist_ok=True)

# ---------------------------------------------------------------------------
# Base de datos: SQLite en local, PostgreSQL automáticamente si existe
# DATABASE_URL (por ejemplo, en Render). El resto del código no necesita
# saber cuál de las dos está usando — ver la clase CompatConnection abajo.
# ---------------------------------------------------------------------------
DATABASE_URL = os.environ.get("DATABASE_URL", "").strip()
USE_POSTGRES = bool(DATABASE_URL)

if USE_POSTGRES:
    import psycopg2
    import psycopg2.extras
    # Render (y algunos otros) entregan la URL como "postgres://", pero
    # psycopg2 moderno exige el prefijo "postgresql://".
    if DATABASE_URL.startswith("postgres://"):
        DATABASE_URL = DATABASE_URL.replace("postgres://", "postgresql://", 1)

# ---------------------------------------------------------------------------
# Llave secreta persistente (se genera una sola vez, no se debe compartir).
# En Render, si defines la variable de entorno SECRET_KEY, se usa esa (así
# no cambia en cada reinicio del servidor, lo que invalidaría las sesiones).
# ---------------------------------------------------------------------------
_env_secret = os.environ.get("SECRET_KEY", "").strip()
if _env_secret:
    SECRET_KEY = _env_secret
elif os.path.exists(SECRET_KEY_PATH):
    with open(SECRET_KEY_PATH, "r") as f:
        SECRET_KEY = f.read().strip()
else:
    SECRET_KEY = uuid.uuid4().hex + uuid.uuid4().hex
    try:
        with open(SECRET_KEY_PATH, "w") as f:
            f.write(SECRET_KEY)
    except OSError:
        pass  # sistema de archivos de solo lectura o efímero — no es crítico

app = Flask(__name__, template_folder=os.path.join(RESOURCE_DIR, "templates"))
app.config["SECRET_KEY"] = SECRET_KEY
app.config["SESSION_COOKIE_HTTPONLY"] = True
app.config["SESSION_COOKIE_SAMESITE"] = "Lax"
app.config["MAX_CONTENT_LENGTH"] = 12 * 1024 * 1024  # 12 MB por archivo subido
app.permanent_session_lifetime = timedelta(hours=12)

IMAGE_EXTS = {"jpg", "jpeg", "png", "webp", "gif", "bmp", "tif", "tiff"}
DOCUMENT_EXTS = IMAGE_EXTS | {"pdf"}


def _ext_ok(filename, allowed):
    if "." not in filename:
        return False
    return filename.rsplit(".", 1)[1].lower() in allowed


# ---------------------------------------------------------------------------
# Capa de compatibilidad SQLite / PostgreSQL
# ---------------------------------------------------------------------------

def _raw_connection():
    if USE_POSTGRES:
        return psycopg2.connect(DATABASE_URL, cursor_factory=psycopg2.extras.RealDictCursor)
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn


class CompatConnection:
    """Envuelve la conexión real (SQLite o PostgreSQL) para que el resto del
    código pueda seguir escribiendo db.execute("... ? ...", (valor,)) y
    leyendo row["columna"] sin importar cuál motor esté activo."""

    def __init__(self, raw):
        self.raw = raw

    def execute(self, sql, params=()):
        cur = self.raw.cursor()
        if USE_POSTGRES:
            sql = sql.replace("?", "%s")
        cur.execute(sql, params)
        return cur

    def commit(self):
        self.raw.commit()

    def close(self):
        self.raw.close()


def _insert_ignore(table, columns):
    """Genera un INSERT que no falla si la fila ya existe (para los valores
    por defecto de settings). columns[0] debe ser la clave primaria."""
    cols_sql = ", ".join(columns)
    placeholders = ", ".join(["?"] * len(columns))
    if USE_POSTGRES:
        return f"INSERT INTO {table} ({cols_sql}) VALUES ({placeholders}) ON CONFLICT ({columns[0]}) DO NOTHING"
    return f"INSERT OR IGNORE INTO {table} ({cols_sql}) VALUES ({placeholders})"


def _column_exists(db, table, column):
    if USE_POSTGRES:
        row = db.execute(
            "SELECT column_name FROM information_schema.columns WHERE table_name = ? AND column_name = ?",
            (table, column),
        ).fetchone()
        return row is not None
    cols = [r[1] for r in db.execute(f"PRAGMA table_info({table})").fetchall()]
    return column in cols


# ---------------------------------------------------------------------------
# Base de datos: conexión por petición
# ---------------------------------------------------------------------------

def get_db():
    if "db" not in g:
        g.db = CompatConnection(_raw_connection())
        if not USE_POSTGRES:
            g.db.execute("PRAGMA foreign_keys = ON")
    return g.db


@app.teardown_appcontext
def close_db(exception=None):
    db = g.pop("db", None)
    if db is not None:
        db.close()


# ---------------------------------------------------------------------------
# Código de activación: se pide una sola vez, en la pantalla de "Configura el
# administrador" (primera instalación). Sin el código correcto, no se puede
# crear la cuenta de administrador ni usar la app.
#
# PARA CAMBIAR EL CÓDIGO (hazlo tú, antes de entregar el ZIP a alguien más):
# 1. Elige tu propio código, por ejemplo "ClinicaGarcia2026"
# 2. En una terminal, genera su hash con:
#      python3 -c "import hashlib; print(hashlib.sha256('TU_CODIGO_AQUI'.encode()).hexdigest())"
# 3. Copia el resultado (una cadena larga de letras y números) y pégalo abajo,
#    reemplazando el valorEATE TABLE  de ACTIVATION_CODE_HASH.
# 4. Guarda app.py. La próxima persona que instale este ZIP necesitará saber
#    "TU_CODIGO_AQUI" para poder crear el administrador.
#
# El código de ejemplo que viene puesto ahora es: ClinicaDemo2026
ACTIVATION_CODE_HASH = "626286af113f9d26817985694fa4c15927338e14488ac11ea593d396c214e611"


def _valid_activation_code(code):
    return hashlib.sha256((code or "").strip().encode()).hexdigest() == ACTIVATION_CODE_HASH


@app.route("/health", methods=["GET"])
def health():
    return jsonify({"ok": True, "database": "postgresql" if USE_POSTGRES else "sqlite"})


DEFAULT_SETTINGS = {
    "logo_size": "34",
    "disclaimer_text": "La información registrada en este sistema es de uso administrativo y clínico. Verifique los datos antes de emitir o entregar documentos. La información clínica no sustituye el criterio profesional del médico.",
    "clinic_name": "Clínica",
    "doctor_name": "",
    "doctor_specialty": "Medicina general",
    "doctor_registration": "",
    "doctor_phone": "",
    "doctor_email": "",
    "clinic_address": "",
    "document_header": "",
    "document_footer": "",
    "show_doctor_on_documents": "1",
    "show_clinic_on_documents": "1",
    "logo_path": "",
    "primary_color": "#2F6F62",
    "accent_color": "#E1734F",
    "bg_color": "#FAF8F4",
    "font_family": "Space Grotesk",
    "font_size": "15",
    "welcome_message": "",
    "backup_hour": "21:00",
    "backup_enabled": "1",
    "backup_folder": "",
    "installation_id": "",
    "license_id": "",
    "license_type": "Sin activar",
    "license_holder": "",
    "license_issued_at": "",
    "license_expires_at": "",
    "license_status": "sin_activar",
    "license_features": "",
    "license_token": "",
}

EMAIL_SETTINGS = {
    "smtp_host": "",
    "smtp_port": "587",
    "smtp_user": "",
    "smtp_password": "",
    "smtp_use_tls": "1",
    "smtp_from_name": "",
}

SYNC_SETTINGS = {
    "sync_remote_url": "",
    "sync_cron_secret": "",
    "sync_remote_username": "",
    "sync_remote_password": "",
}

VIEW_KEYS = ["pacientes", "agenda", "calendario", "tratamientos", "medicamentos", "presupuestos", "facturacion"]



def _normalize_security_answer(value):
    return " ".join((value or "").strip().lower().split())


def _generate_recovery_code():
    alphabet = "ABCDEFGHJKLMNPQRSTUVWXYZ23456789"
    import secrets
    raw = "".join(secrets.choice(alphabet) for _ in range(16))
    return "-".join(raw[i:i+4] for i in range(0, len(raw), 4))


def _normalize_recovery_code(value):
    return (value or "").replace("-", "").replace(" ", "").strip().upper()


def _machine_fingerprint():
    """Huella local no reversible usada para ligar una licencia a este equipo.
    Nunca se guarda el identificador bruto: solo un SHA-256."""
    pieces = [platform.system(), platform.machine(), platform.node()]
    try:
        import uuid as _uuid
        pieces.append(str(_uuid.getnode()))
    except Exception:
        pass
    try:
        if os.name == "nt":
            import winreg
            with winreg.OpenKey(winreg.HKEY_LOCAL_MACHINE, r"SOFTWARE\Microsoft\Cryptography") as key:
                pieces.append(str(winreg.QueryValueEx(key, "MachineGuid")[0]))
    except Exception:
        pass
    return hashlib.sha256("|".join(pieces).encode("utf-8")).hexdigest()


def _terms_accepted_for_current_user(user_id):
    if not user_id:
        return False
    db = get_db()
    row = db.execute(
        "SELECT 1 FROM legal_acceptances WHERE user_id = ? AND terms_version = ? ORDER BY accepted_at DESC LIMIT 1",
        (user_id, CURRENT_TERMS_VERSION),
    ).fetchone()
    return bool(row)


def _log_activity(action, target="", details="", user_id=None, username=None):
    """Registra una acción administrativa o de seguridad sin guardar secretos."""
    try:
        db = get_db()
        uid = user_id if user_id is not None else session.get("user_id")
        uname = username if username is not None else session.get("username", "")
        db.execute(
            "INSERT INTO activity_log (id, user_id, username, action, target, details, created_at) VALUES (?, ?, ?, ?, ?, ?, ?)",
            (uuid.uuid4().hex, uid, uname or "", _clean(action)[:80], _clean(target)[:120], _clean(details)[:500], _now()),
        )
        db.commit()
    except Exception as exc:
        print(f"[auditoria] No se pudo registrar la actividad: {exc}", flush=True)


def _backup_sha256(path):
    h = hashlib.sha256()
    with open(path, "rb") as fh:
        for chunk in iter(lambda: fh.read(1024 * 1024), b""):
            h.update(chunk)
    return h.hexdigest()


def _write_backup_checksum(path):
    checksum = _backup_sha256(path)
    with open(path + ".sha256", "w", encoding="utf-8") as fh:
        fh.write(checksum + "\n")
    return checksum


def _verify_sqlite_backup(path):
    """Comprueba integridad SQLite y, cuando existe sidecar, verifica hash."""
    try:
        conn = sqlite3.connect(path)
        row = conn.execute("PRAGMA integrity_check").fetchone()
        conn.close()
        sqlite_ok = bool(row and str(row[0]).lower() == "ok")
    except Exception as exc:
        return {"ok": False, "sqliteOk": False, "hashOk": False, "message": f"No se pudo abrir el respaldo: {exc}"}
    sidecar = path + ".sha256"
    current = _backup_sha256(path)
    if os.path.exists(sidecar):
        try:
            expected = open(sidecar, "r", encoding="utf-8").read().strip().split()[0]
            hash_ok = expected.lower() == current.lower()
            return {"ok": sqlite_ok and hash_ok, "sqliteOk": sqlite_ok, "hashOk": hash_ok, "sha256": current, "message": "Respaldo íntegro y hash coincidente." if sqlite_ok and hash_ok else "El hash no coincide o SQLite reportó un problema."}
        except Exception:
            pass
    return {"ok": sqlite_ok, "sqliteOk": sqlite_ok, "hashOk": None, "sha256": current, "message": "SQLite está íntegro. Este respaldo no tenía hash previo; se creó una referencia nueva."}


def _license_from_settings():
    db = get_db()
    rows = {r["key"]: r["value"] for r in db.execute("SELECT key, value FROM settings").fetchall()}
    installation_id = rows.get("installation_id", "")
    machine_fp = _machine_fingerprint()
    status = rows.get("license_status", "sin_activar") or "sin_activar"
    expires_at = rows.get("license_expires_at", "") or ""
    stored_token = rows.get("license_token", "") or ""
    if stored_token:
        try:
            payload = _decode_signed_license(stored_token)
            if payload.get("product") != LICENSE_PRODUCT:
                status = "invalida"
            elif payload.get("installation_id") and payload.get("installation_id") != installation_id:
                status = "invalida"
            elif payload.get("machine_binding") and payload.get("machine_binding") != machine_fp:
                status = "invalida"
        except Exception:
            status = "invalida"
    if expires_at and status != "invalida":
        try:
            exp = datetime.fromisoformat(expires_at)
            if exp < datetime.now():
                status = "vencida"
            elif exp <= datetime.now() + timedelta(days=30):
                status = "por_vencer" if status in ("activa", "por_vencer") else status
        except Exception:
            pass
    return {
        "product": LICENSE_PRODUCT,
        "status": status,
        "licenseId": rows.get("license_id", ""),
        "type": rows.get("license_type", "Sin activar"),
        "holder": rows.get("license_holder", ""),
        "issuedAt": rows.get("license_issued_at", ""),
        "expiresAt": expires_at,
        "installationId": installation_id,
        "machineBinding": machine_fp[:12].upper(),
        "signatureConfigured": bool(os.environ.get("LICENSE_PUBLIC_KEY", "").strip()) or any(os.path.exists(os.path.join(base, "license_public_key.pem")) for base in (BASE_DIR, RESOURCE_DIR)),
        "features": [x for x in (rows.get("license_features", "") or "").split(",") if x],
    }


def _decode_signed_license(token):
    """Formato V6.2: base64url(JSON payload).base64url(signature).
    La firma Ed25519 se verifica con LICENSE_PUBLIC_KEY (PEM) del proveedor.
    No contiene ni necesita una clave privada dentro de la app."""
    token = (token or "").strip()
    if "." not in token:
        raise ValueError("Formato de licencia inválido.")
    payload_b64, sig_b64 = token.split(".", 1)
    def b64decode(value):
        return base64.urlsafe_b64decode(value + "=" * (-len(value) % 4))
    payload_bytes = b64decode(payload_b64)
    signature = b64decode(sig_b64)
    payload = json.loads(payload_bytes.decode("utf-8"))
    if not isinstance(payload, dict):
        raise ValueError("Contenido de licencia inválido.")
    public_key_pem = os.environ.get("LICENSE_PUBLIC_KEY", "").strip()
    if not public_key_pem:
        # En modo normal la clave puede estar junto al proyecto.
        # En un EXE one-file de PyInstaller, los recursos --add-data se
        # extraen en sys._MEIPASS, por lo que también debemos buscar allí.
        key_candidates = [
            os.path.join(BASE_DIR, "license_public_key.pem"),
            os.path.join(RESOURCE_DIR, "license_public_key.pem"),
        ]
        for key_path in key_candidates:
            if os.path.exists(key_path):
                try:
                    with open(key_path, "r", encoding="utf-8") as fh:
                        public_key_pem = fh.read().strip()
                    if public_key_pem:
                        break
                except OSError:
                    public_key_pem = ""
    if not public_key_pem:
        raise RuntimeError("El proveedor todavía no ha configurado la clave pública de licencias en esta instalación.")
    try:
        from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey
        from cryptography.hazmat.primitives import serialization
        public_key = serialization.load_pem_public_key(public_key_pem.encode("utf-8"))
        if not isinstance(public_key, Ed25519PublicKey):
            raise ValueError("La clave pública no es Ed25519.")
        public_key.verify(signature, payload_bytes)
    except ImportError as exc:
        raise RuntimeError("Falta el componente criptográfico para verificar la licencia.") from exc
    return payload


def init_db():
    raw = _raw_connection()
    db = CompatConnection(raw)
    db.execute(
        """CREATE TABLE IF NOT EXISTS users (
                
            id TEXT PRIMARY KEY,
            username TEXT UNIQUE NOT NULL,
            password_hash TEXT NOT NULL,
            role TEXT NOT NULL DEFAULT 'admin',
            permissions TEXT NOT NULL DEFAULT '',
            display_name TEXT DEFAULT '',
            security_answer_hash TEXT DEFAULT '',
            recovery_code_hash TEXT DEFAULT '',
            active INTEGER NOT NULL DEFAULT 1,
            must_change_password INTEGER NOT NULL DEFAULT 0,
            last_login_at TEXT DEFAULT '',
            created_at TEXT NOT NULL
        )"""
    )
    db.execute(
        """CREATE TABLE IF NOT EXISTS legal_acceptances (
            id TEXT PRIMARY KEY,
            user_id TEXT NOT NULL REFERENCES users(id) ON DELETE CASCADE,
            username TEXT NOT NULL,
            terms_version TEXT NOT NULL,
            accepted_at TEXT NOT NULL
        )"""
    )
    db.execute(
        """CREATE TABLE IF NOT EXISTS activity_log (
            id TEXT PRIMARY KEY,
            user_id TEXT,
            username TEXT,
            action TEXT NOT NULL,
            target TEXT DEFAULT '',
            details TEXT DEFAULT '',
            created_at TEXT NOT NULL
        )"""
    )
    db.execute(
        """CREATE TABLE IF NOT EXISTS patients (
            id TEXT PRIMARY KEY,
            name TEXT NOT NULL,
            dob TEXT,
            phone TEXT,
            email TEXT,
            address TEXT,
            allergies TEXT,
            history TEXT,
            notes TEXT,
            photo TEXT,
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL
        )"""
    )
    db.execute(
        """CREATE TABLE IF NOT EXISTS appointments (
            id TEXT PRIMARY KEY,
            patient_id TEXT NOT NULL REFERENCES patients(id) ON DELETE CASCADE,
            date TEXT NOT NULL,
            time TEXT NOT NULL,
            duration INTEGER DEFAULT 30,
            status TEXT DEFAULT 'pendiente',
            reason TEXT,
            notes TEXT,
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL
        )"""
    )
    db.execute(
        """CREATE TABLE IF NOT EXISTS documents (
            id TEXT PRIMARY KEY,
            patient_id TEXT NOT NULL REFERENCES patients(id) ON DELETE CASCADE,
            filename TEXT NOT NULL,
            original_name TEXT NOT NULL,
            category TEXT DEFAULT 'otro',
            uploaded_at TEXT NOT NULL
        )"""
    )
    db.execute(
        """CREATE TABLE IF NOT EXISTS charges (
            id TEXT PRIMARY KEY,
            patient_id TEXT NOT NULL REFERENCES patients(id) ON DELETE CASCADE,
            date TEXT NOT NULL,
            treatment TEXT NOT NULL,
            cost REAL NOT NULL DEFAULT 0,
            paid REAL NOT NULL DEFAULT 0,
            notes TEXT,
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL
        )"""
    )
    db.execute(
        """CREATE TABLE IF NOT EXISTS settings (
            key TEXT PRIMARY KEY,
            value TEXT
        )"""
    )
    db.execute(
        """CREATE TABLE IF NOT EXISTS prescriptions (
            patient_id TEXT PRIMARY KEY REFERENCES patients(id) ON DELETE CASCADE,
            medications TEXT NOT NULL DEFAULT '',
            instructions TEXT NOT NULL DEFAULT '',
            updated_at TEXT NOT NULL
        )"""
    )
    db.execute(
        """CREATE TABLE IF NOT EXISTS clinical_records (
            id TEXT PRIMARY KEY,
            patient_id TEXT NOT NULL REFERENCES patients(id) ON DELETE CASCADE,
            fecha TEXT NOT NULL,
            especialidad TEXT NOT NULL DEFAULT '',
            motivo TEXT NOT NULL DEFAULT '',
            presion TEXT NOT NULL DEFAULT '',
            temperatura TEXT NOT NULL DEFAULT '',
            pulso TEXT NOT NULL DEFAULT '',
            peso TEXT NOT NULL DEFAULT '',
            talla TEXT NOT NULL DEFAULT '',
            saturacion TEXT NOT NULL DEFAULT '',
            examen TEXT NOT NULL DEFAULT '',
            diagnostico TEXT NOT NULL DEFAULT '',
            medicamentos TEXT NOT NULL DEFAULT '',
            indicaciones TEXT NOT NULL DEFAULT '',
            seguimiento TEXT NOT NULL DEFAULT '',
            created_at TEXT NOT NULL
        )"""
    )
    if not _column_exists(db, "clinical_records", "tratamiento"):
        db.execute("ALTER TABLE clinical_records ADD COLUMN tratamiento TEXT NOT NULL DEFAULT ''")
    if not _column_exists(db, "clinical_records", "costo"):
        db.execute("ALTER TABLE clinical_records ADD COLUMN costo REAL NOT NULL DEFAULT 0")
    if not _column_exists(db, "clinical_records", "especialidad_datos"):
        db.execute("ALTER TABLE clinical_records ADD COLUMN especialidad_datos TEXT NOT NULL DEFAULT '{}'")
    db.execute(
        """CREATE TABLE IF NOT EXISTS budgets (
            id TEXT PRIMARY KEY,
            patient_id TEXT NOT NULL REFERENCES patients(id) ON DELETE CASCADE,
            title TEXT NOT NULL,
            status TEXT NOT NULL DEFAULT 'pendiente',
            notes TEXT,
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL
        )"""
    )
    db.execute(
        """CREATE TABLE IF NOT EXISTS budget_items (
            id TEXT PRIMARY KEY,
            budget_id TEXT NOT NULL REFERENCES budgets(id) ON DELETE CASCADE,
            description TEXT NOT NULL,
            cost REAL NOT NULL DEFAULT 0
        )"""
    )
    db.execute(
        """CREATE TABLE IF NOT EXISTS budget_payments (
            id TEXT PRIMARY KEY,
            budget_id TEXT NOT NULL REFERENCES budgets(id) ON DELETE CASCADE,
            date TEXT NOT NULL,
            amount REAL NOT NULL DEFAULT 0,
            notes TEXT,
            created_at TEXT NOT NULL
        )"""
    )
    if USE_POSTGRES:
        # En Render (y hostings similares) el disco del servidor NO es persistente:
        # se borra en cada reinicio. Por eso, cuando se usa PostgreSQL, las fotos y
        # documentos subidos se guardan dentro de la propia base de datos en vez
        # de en el disco, para que sobrevivan los reinicios igual que el resto
        # de la información.
        db.execute(
            """CREATE TABLE IF NOT EXISTS file_blobs (
                id TEXT PRIMARY KEY,
                data BYTEA NOT NULL,
                content_type TEXT NOT NULL,
                created_at TEXT NOT NULL
            )"""
        )
    if not _column_exists(db, "patients", "photo"):
        db.execute("ALTER TABLE patients ADD COLUMN photo TEXT")
    if not _column_exists(db, "patients", "dui"):
        db.execute("ALTER TABLE patients ADD COLUMN dui TEXT")
    if not _column_exists(db, "users", "role"):
        db.execute("ALTER TABLE users ADD COLUMN role TEXT NOT NULL DEFAULT 'admin'")
    if not _column_exists(db, "users", "permissions"):
        db.execute("ALTER TABLE users ADD COLUMN permissions TEXT NOT NULL DEFAULT ''")
    if not _column_exists(db, "users", "display_name"):
        db.execute("ALTER TABLE users ADD COLUMN display_name TEXT DEFAULT ''")
    if not _column_exists(db, "users", "security_question"):
        db.execute("ALTER TABLE users ADD COLUMN security_question TEXT DEFAULT ''")
    if not _column_exists(db, "users", "security_answer_hash"):
        db.execute("ALTER TABLE users ADD COLUMN security_answer_hash TEXT DEFAULT ''")
    if not _column_exists(db, "users", "recovery_code_hash"):
        db.execute("ALTER TABLE users ADD COLUMN recovery_code_hash TEXT DEFAULT ''")
    if not _column_exists(db, "users", "active"):
        db.execute("ALTER TABLE users ADD COLUMN active INTEGER NOT NULL DEFAULT 1")
    if not _column_exists(db, "users", "must_change_password"):
        db.execute("ALTER TABLE users ADD COLUMN must_change_password INTEGER NOT NULL DEFAULT 0")
    if not _column_exists(db, "users", "last_login_at"):
        db.execute("ALTER TABLE users ADD COLUMN last_login_at TEXT DEFAULT ''")

    for k, v in DEFAULT_SETTINGS.items():
        db.execute(_insert_ignore("settings", ["key", "value"]), (k, v))
    for k, v in EMAIL_SETTINGS.items():
        db.execute(_insert_ignore("settings", ["key", "value"]), (k, v))
    for k, v in SYNC_SETTINGS.items():
        db.execute(_insert_ignore("settings", ["key", "value"]), (k, v))
    install_row = db.execute("SELECT value FROM settings WHERE key = 'installation_id'").fetchone()
    if not install_row or not (install_row["value"] or "").strip():
        db.execute("UPDATE settings SET value = ? WHERE key = 'installation_id'", (uuid.uuid4().hex,))
    # Las instalaciones V6.1 ya aceptaron un texto legal anterior; se registra
    # como versión histórica para que V6.2 exija una nueva aceptación por usuario.
    legacy_ts = db.execute("SELECT value FROM settings WHERE key = 'terms_accepted_at'").fetchone()
    if legacy_ts and legacy_ts["value"]:
        users = db.execute("SELECT id, username FROM users").fetchall()
        for u in users:
            exists = db.execute("SELECT 1 FROM legal_acceptances WHERE user_id = ? AND terms_version = ? LIMIT 1", (u["id"], LEGACY_TERMS_VERSION)).fetchone()
            if not exists:
                db.execute("INSERT INTO legal_acceptances (id, user_id, username, terms_version, accepted_at) VALUES (?, ?, ?, ?, ?)", (uuid.uuid4().hex, u["id"], u["username"], LEGACY_TERMS_VERSION, legacy_ts["value"]))
    db.commit()
    db.close()


init_db()


def _get_setting_value(key, default=""):
    try:
        raw = _raw_connection()
        db = CompatConnection(raw)
        row = db.execute("SELECT value FROM settings WHERE key = ?", (key,)).fetchone()
        db.close()
        return row["value"] if row and row["value"] else default
    except Exception:
        return default


def do_backup():
    """Copia clinica.db a backups/ (solo aplica con SQLite local; en
    PostgreSQL no hay un solo archivo que copiar — usa el respaldo de tu
    proveedor de base de datos, ej. Render Postgres)."""
    if USE_POSTGRES or not os.path.exists(DB_PATH):
        return None
    stamp = datetime.now().strftime("%Y-%m-%d_%H-%M-%S")
    dest_path = os.path.join(BACKUP_DIR, f"clinica_{stamp}.db")
    try:
        src = sqlite3.connect(DB_PATH)
        dst = sqlite3.connect(dest_path)
        with dst:
            src.backup(dst)
        src.close()
        dst.close()
        _write_backup_checksum(dest_path)
    except Exception as e:
        print(f"[respaldo] Error al respaldar: {e}")
        return None
    try:
        if os.path.isdir(UPLOAD_DIR) and os.listdir(UPLOAD_DIR):
            shutil.make_archive(os.path.join(BACKUP_DIR, f"uploads_{stamp}"), "zip", UPLOAD_DIR)
    except Exception as e:
        print(f"[respaldo] No se pudieron respaldar los archivos subidos: {e}")
    _prune_backups()
    _sync_to_folder([dest_path] + ([f"{os.path.join(BACKUP_DIR, f'uploads_{stamp}.zip')}"] if os.path.exists(os.path.join(BACKUP_DIR, f"uploads_{stamp}.zip")) else []))
    print(f"[respaldo] Copia de seguridad creada: {os.path.basename(dest_path)}")
    _log_activity("backup_create", os.path.basename(dest_path), "Respaldo local creado")
    return dest_path


def _sync_to_folder(paths):
    folder = _get_setting_value("backup_folder", "")
    if not folder:
        return False
    if not os.path.isdir(folder):
        print(f"[respaldo] La carpeta de sincronización no existe: {folder}")
        return False
    ok = True
    for p in paths:
        try:
            if os.path.exists(p):
                shutil.copy2(p, os.path.join(folder, os.path.basename(p)))
        except Exception as e:
            print(f"[respaldo] No se pudo copiar {p} a la carpeta sincronizada: {e}")
            ok = False
    return ok


def _prune_backups():
    files = sorted(
        [f for f in os.listdir(BACKUP_DIR) if f.startswith("clinica_") and f.endswith(".db")]
    )
    excess = len(files) - MAX_BACKUPS
    for old in files[:max(excess, 0)]:
        try:
            os.remove(os.path.join(BACKUP_DIR, old))
            stamp = old[len("clinica_"):-len(".db")]
            zpath = os.path.join(BACKUP_DIR, f"uploads_{stamp}.zip")
            if os.path.exists(zpath):
                os.remove(zpath)
        except Exception:
            pass


def _todays_backup_exists():
    if USE_POSTGRES:
        return True
    today = datetime.now().strftime("%Y-%m-%d")
    return any(f.startswith(f"clinica_{today}") for f in os.listdir(BACKUP_DIR))


def _backup_scheduler_loop():
    if _get_setting_value("backup_enabled", "1") == "1" and not _todays_backup_exists():
        do_backup()
    while True:
        try:
            hour_str = _get_setting_value("backup_hour", "21:00")
            hh, mm = (hour_str.split(":") + ["0", "0"])[:2]
            target_hour, target_min = int(hh), int(mm)
        except Exception:
            target_hour, target_min = 21, 0
        now = datetime.now()
        target = now.replace(hour=target_hour, minute=target_min, second=0, microsecond=0)
        if target <= now:
            target = target.replace(day=now.day) + timedelta(days=1)
        sleep_seconds = (target - now).total_seconds()
        time.sleep(min(sleep_seconds, 3600))
        if datetime.now() >= target and _get_setting_value("backup_enabled", "1") == "1":
            if not _todays_backup_exists():
                do_backup()


_backup_thread_started = False


def start_backup_scheduler():
    global _backup_thread_started
    if _backup_thread_started or USE_POSTGRES:
        return
    _backup_thread_started = True
    t = threading.Thread(target=_backup_scheduler_loop, daemon=True)
    t.start()


# ---------------------------------------------------------------------------
# Autenticación
# ---------------------------------------------------------------------------

_failed_attempts = {}
MAX_ATTEMPTS = 6
LOCKOUT_SECONDS = 60


def any_user_exists():
    db = get_db()
    row = db.execute("SELECT COUNT(*) AS c FROM users").fetchone()
    return row["c"] > 0


def login_required(view):
    @wraps(view)
    def wrapped(*args, **kwargs):
        if not session.get("user_id"):
            if request.path.startswith("/api/") or request.path.startswith("/uploads/"):
                return jsonify({"error": "No autenticado"}), 401
            return redirect(url_for("login"))
        if request.endpoint not in ("legal_acceptance", "logout") and not _terms_accepted_for_current_user(session.get("user_id")):
            if request.path.startswith("/api/"):
                return jsonify({"error": "Debes aceptar los términos de uso antes de continuar.", "requiresTerms": True}), 428
            return redirect(url_for("legal_acceptance"))
        if session.get("must_change_password") and request.endpoint not in ("change_password", "force_password_change", "logout", "legal_acceptance"):
            if request.path.startswith("/api/"):
                return jsonify({"error": "Debes cambiar tu contraseña antes de continuar.", "requiresPasswordChange": True}), 428
            return redirect(url_for("force_password_change"))
        return view(*args, **kwargs)

    return wrapped


def admin_required(view):
    @wraps(view)
    def wrapped(*args, **kwargs):
        if not session.get("user_id"):
            return jsonify({"error": "No autenticado"}), 401
        if session.get("role") != "admin":
            return jsonify({"error": "Solo el administrador puede hacer esto."}), 403
        return view(*args, **kwargs)

    return wrapped


def has_permission(view_key):
    if session.get("role") == "admin":
        return True
    perms = (session.get("permissions") or "")
    if perms == "all":
        return True
    return view_key in [p for p in perms.split(",") if p]


API_PERMISSION_MAP = [
    ("/api/users", None),
    ("/api/backups", None),
    ("/api/sync", None),
    ("/api/settings", None),
    ("/api/change-password", None),
    ("/api/me", None),
    ("/api/appointments", "agenda"),
    ("/api/patients", "pacientes"),
    ("/api/documents", "pacientes"),
    ("/api/budgets", "presupuestos"),
    ("/api/charges", "tratamientos"),
]


@app.before_request
def enforce_permissions():
    if not request.path.startswith("/api/"):
        return
    if not session.get("user_id"):
        return
    if session.get("role") == "admin":
        return
    for prefix, key in API_PERMISSION_MAP:
        if request.path.startswith(prefix):
            if key is None:
                return
            if not has_permission(key) and not (prefix == "/api/charges" and has_permission("facturacion")):
                return jsonify({"error": "No tienes permiso para acceder a esta sección."}), 403
            return


@app.before_request
def enforce_setup():
    if request.path in ("/setup", "/static") or request.path.startswith("/static/"):
        return
    if not any_user_exists() and request.path != "/setup":
        if request.path.startswith("/api/"):
            return jsonify({"error": "Configura el usuario administrador primero"}), 403
        return redirect(url_for("setup"))


@app.route("/setup", methods=["GET", "POST"])
def setup():
    if any_user_exists():
        return redirect(url_for("login"))
    error = None
    if request.method == "POST":
        username = request.form.get("username", "").strip()
        password = request.form.get("password", "")
        confirm = request.form.get("confirm", "")
        activation_code = request.form.get("activation_code", "").strip()
        accept_terms = request.form.get("accept_terms")
        license_file = request.files.get("license_file")
        license_token = ""
        license_payload = None

        rec = _failed_attempts.get("_setup_activation")
        if rec and rec["count"] >= MAX_ATTEMPTS and (time.time() - rec["ts"]) < LOCKOUT_SECONDS:
            error = "Demasiados intentos fallidos. Espera un minuto e intenta de nuevo."
        else:
            # Método recomendado: archivo de licencia .clsv. El código legacy
            # se conserva como compatibilidad para instalaciones antiguas.
            if license_file and license_file.filename:
                filename = secure_filename(license_file.filename)
                if not filename.lower().endswith(".clsv"):
                    error = "El archivo de licencia debe tener extensión .clsv."
                else:
                    try:
                        license_token = license_file.read().decode("utf-8").strip()
                        license_payload = _decode_signed_license(license_token)
                        expected_installation = _license_from_settings()["installationId"]
                        expected_machine = _machine_fingerprint()
                        if license_payload.get("product") != LICENSE_PRODUCT:
                            error = "La licencia no corresponde a ClínicaSV Medical."
                        elif license_payload.get("installation_id") and license_payload.get("installation_id") != expected_installation:
                            error = "La licencia corresponde a otra instalación."
                        elif license_payload.get("machine_binding") and license_payload.get("machine_binding") != expected_machine:
                            error = "La licencia corresponde a otro equipo."
                        else:
                            exp = license_payload.get("expires_at") or ""
                            if exp:
                                try:
                                    if datetime.fromisoformat(exp) < datetime.now():
                                        error = "La licencia ya está vencida."
                                except ValueError:
                                    error = "Fecha de vencimiento inválida."
                    except RuntimeError as exc:
                        error = str(exc)
                    except (ValueError, json.JSONDecodeError, UnicodeDecodeError) as exc:
                        error = f"No se pudo validar la licencia: {exc}"
            elif activation_code:
                if _valid_activation_code(activation_code):
                    license_payload = None
                else:
                    rec = _failed_attempts.get("_setup_activation", {"count": 0, "ts": 0})
                    rec["count"] += 1
                    rec["ts"] = time.time()
                    _failed_attempts["_setup_activation"] = rec
                    error = "Código de activación incorrecto."
            else:
                error = "Importa el archivo de licencia .clsv o introduce un código de activación."

            if not error and len(username) < 3:
                error = "El usuario debe tener al menos 3 caracteres."
            elif not error and len(password) < 8:
                error = "La contraseña debe tener al menos 8 caracteres."
            elif not error and password != confirm:
                error = "Las contraseñas no coinciden."
            elif not error and not accept_terms:
                error = "Debes aceptar los términos y condiciones para continuar."

        if not error:
            _failed_attempts.pop("_setup_activation", None)
            db = get_db()
            uid = uuid.uuid4().hex
            db.execute(
                "INSERT INTO users (id, username, password_hash, role, permissions, created_at) VALUES (?, ?, ?, 'admin', 'all', ?)",
                (uid, username, generate_password_hash(password), _now()),
            )
            accepted_at = _now()
            db.execute(
                "INSERT INTO legal_acceptances (id, user_id, username, terms_version, accepted_at) VALUES (?, ?, ?, ?, ?)",
                (uuid.uuid4().hex, uid, username, CURRENT_TERMS_VERSION, accepted_at),
            )
            db.execute(
                "INSERT INTO settings (key, value) VALUES (?, ?) ON CONFLICT(key) DO UPDATE SET value = excluded.value",
                ("terms_accepted_at", accepted_at),
            )
            # Si se activó mediante archivo .clsv, guardamos la licencia desde
            # este mismo paso para que el usuario no tenga que repetirla.
            if license_payload and license_token:
                updates = {
                    "license_id": license_payload.get("license_id", ""),
                    "license_type": license_payload.get("type", "Licencia"),
                    "license_holder": license_payload.get("holder", ""),
                    "license_issued_at": license_payload.get("issued_at", ""),
                    "license_expires_at": license_payload.get("expires_at", ""),
                    "license_status": "activa",
                    "license_features": ",".join(license_payload.get("features", []) or []),
                    "license_token": license_token,
                }
                for key, value in updates.items():
                    db.execute("UPDATE settings SET value = ? WHERE key = ?", (str(value), key))
            db.commit()
            return redirect(url_for("login"))
    return render_template("setup.html", error=error)


_FORGOT_PASSWORD_HTML = """
<!DOCTYPE html>
<html lang="es">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>Recuperar contraseña · Clínica</title>
<link href="https://fonts.googleapis.com/css2?family=Space+Grotesk:wght@600;700&family=Inter:wght@400;500;600&display=swap" rel="stylesheet">
<style>
  :root{--bg:#FAF8F4;--ink:#1E2A28;--primary:#2F6F62;--primary-dark:#204E45;--accent:#E1734F;--accent-dark:#C85E3B;--border:#DFE5E1;--muted:#7C8B87;--danger:#C0463C;--danger-light:#FBEAE8;--ok-light:#E7F3EE;}
  *{box-sizing:border-box;} body{margin:0;background:var(--primary-dark);font-family:'Inter',sans-serif;color:var(--ink);min-height:100vh;display:flex;align-items:center;justify-content:center;padding:20px;}
  .card{background:#fff;border-radius:20px;padding:34px 32px;width:100%;max-width:440px;box-shadow:0 20px 60px rgba(0,0,0,.25);} h1{font-family:'Space Grotesk',sans-serif;font-size:20px;margin:0 0 4px;} p.sub{color:var(--muted);font-size:13px;margin:0 0 18px;line-height:1.5;}
  label{display:block;font-size:12px;font-weight:600;color:var(--primary-dark);margin-bottom:5px;} input{width:100%;padding:10px 12px;border:1px solid var(--border);border-radius:14px;font-size:14px;margin-bottom:12px;} input:focus{outline:none;border-color:var(--primary);}
  button{width:100%;background:var(--accent);color:#fff;border:none;border-radius:14px;padding:11px;font-size:14px;font-weight:700;cursor:pointer;margin-bottom:9px;} button:hover{background:var(--accent-dark);} button.secondary{background:#fff;color:var(--primary-dark);border:1px solid var(--border);} button.secondary:hover{background:var(--bg);}
  .msg{font-size:12.5px;padding:9px 12px;border-radius:12px;margin-bottom:14px;background:var(--ok-light);color:var(--primary-dark);} .question-box{background:var(--bg);border-radius:14px;padding:12px 14px;margin-bottom:14px;font-size:13px;line-height:1.45;}
  .choice{border:1px solid var(--border);border-radius:14px;padding:12px;margin:10px 0;background:#FBFDFF;} .choice-title{font-weight:700;font-size:13px;margin-bottom:3px;} .small{font-size:11.5px;color:var(--muted);line-height:1.45;margin-bottom:10px;} a.back{display:block;text-align:center;margin-top:4px;color:var(--primary);font-size:12.5px;text-decoration:none;}
</style>
</head>
<body>
  <div class="card">
    <h1>🔑 Recuperar contraseña</h1>
    {% if message %}<div class="msg">{{ message }}</div>{% endif %}

    {% if question %}
      <div class="choice"><div class="choice-title">Pregunta de seguridad</div><div class="small">No requiere internet.</div><div class="question-box"><strong>{{ question }}</strong></div>
      <form method="POST"><input type="hidden" name="step" value="answer"><input type="hidden" name="username" value="{{ username_for_step2 }}">
        <label for="answer">Tu respuesta</label><input type="text" id="answer" name="answer" required autofocus>
        <label for="new_password">Nueva contraseña (mín. 8 caracteres)</label><input type="password" id="new_password" name="new_password" required minlength="8">
        <button type="submit">Cambiar contraseña</button>
      </form></div>
    {% endif %}

    {% if allow_code %}
      <div class="choice"><div class="choice-title">Código de recuperación local</div><div class="small">Funciona en la copia local sin internet. El código se invalida después de usarlo.</div>
      <form method="POST"><input type="hidden" name="step" value="code"><input type="hidden" name="username" value="{{ username_for_step2 }}">
        <label for="recovery_code">Código</label><input type="text" id="recovery_code" name="recovery_code" placeholder="Ej. ABCD-EFGH-IJKL-MNOP" required>
        <label for="code_new_password">Nueva contraseña (mín. 8 caracteres)</label><input type="password" id="code_new_password" name="new_password" required minlength="8">
        <button type="submit">Usar código y cambiar contraseña</button>
      </form></div>
    {% endif %}

    {% if not question and not allow_code and not message %}
      <p class="sub">Escribe tu usuario. El sistema usará la recuperación disponible para esa cuenta.</p>
      <form method="POST"><input type="hidden" name="step" value="username"><label for="username">Usuario</label><input type="text" id="username" name="username" required autofocus><button type="submit">Continuar</button></form>
    {% elif not question and allow_code %}
      <p class="sub">La cuenta tiene un código local de recuperación. Puedes usarlo arriba para recuperar el acceso sin internet.</p>
    {% endif %}
    {% if question or allow_code %}<p class="small">También puedes usar el correo configurado por el administrador como método de respaldo cuando no haya una recuperación local disponible.</p>{% endif %}
    <a class="back" href="/login">&larr; Volver a iniciar sesión</a>
  </div>
</body>
</html>
"""


_SECURITY_QUESTION_HTML = """
<!DOCTYPE html>
<html lang="es">
<head><meta charset="UTF-8"><meta name="viewport" content="width=device-width, initial-scale=1.0"><title>Seguridad de la cuenta · Clínica</title>
<link href="https://fonts.googleapis.com/css2?family=Space+Grotesk:wght@600;700&family=Inter:wght@400;500;600&display=swap" rel="stylesheet">
<style>
body{margin:0;background:#FAF8F4;font-family:Inter,sans-serif;color:#1E2A28;min-height:100vh;display:flex;align-items:center;justify-content:center;padding:20px}.wrap{max-width:720px;width:100%;display:grid;grid-template-columns:1fr 1fr;gap:14px}.card{background:#fff;border-radius:18px;padding:28px;box-shadow:0 10px 40px rgba(0,0,0,.12)}h1,h2{font-family:'Space Grotesk',sans-serif}h1{font-size:21px;margin:0 0 6px}h2{font-size:16px;margin:0 0 6px}p.sub{color:#7C8B87;font-size:12.5px;line-height:1.5}label{display:block;font-size:12px;font-weight:600;color:#204E45;margin:12px 0 5px}input{width:100%;box-sizing:border-box;padding:10px 12px;border:1px solid #DFE5E1;border-radius:12px;font-size:14px}button{margin-top:14px;width:100%;background:#E1734F;color:#fff;border:0;border-radius:999px;padding:11px;font-weight:700;cursor:pointer}.msg{background:#E7F3EE;color:#204E45;padding:10px 12px;border-radius:10px;font-size:12.5px;margin-bottom:12px}.code{font-family:'IBM Plex Mono',monospace;font-size:20px;font-weight:700;letter-spacing:.08em;background:#F8FAFC;border:1px dashed #94A3B8;padding:13px;border-radius:12px;word-break:break-all}.back{display:block;text-align:center;margin-top:14px;color:#2F6F62;text-decoration:none;font-size:12.5px}.danger{color:#A3342B;background:#FBEAE8;padding:10px;border-radius:10px;font-size:12px;margin-top:10px}@media(max-width:700px){.wrap{grid-template-columns:1fr}}
</style></head>
<body><div style="width:100%;max-width:720px"><h1>🛡️ Seguridad de la cuenta</h1><p class="sub">Configura una recuperación local para no depender de internet cuando olvides tu contraseña.</p><div class="wrap">
<div class="card"><h2>Pregunta de seguridad</h2><p class="sub">La respuesta se guarda protegida y no se muestra después de guardar.</p>{% if message %}<div class="msg">{{ message }}</div>{% endif %}<form method="POST">
<label for="security_question">Pregunta</label><input type="text" id="security_question" name="security_question" placeholder="Ej. ¿Nombre de mi primera mascota?" value="{{ current_question }}" required>
<label for="security_answer">Respuesta</label><input type="text" id="security_answer" name="security_answer" placeholder="No distingue mayúsculas ni espacios repetidos" required>
<label for="current_password">Contraseña actual</label><input type="password" id="current_password" name="current_password" required>
<button type="submit">Guardar pregunta</button></form></div>
<div class="card"><h2>Código de recuperación local</h2><p class="sub">Genera un código de un solo uso. Guárdalo o imprímelo ahora; la aplicación no volverá a mostrarlo.</p><button type="button" id="gen-code">Generar nuevo código</button><div id="code-result" style="display:none;margin-top:14px"><div class="code" id="code-value"></div><p class="sub">Generar otro código invalida el anterior.</p></div></div>
</div><a class="back" href="/">&larr; Volver a la app</a><script>document.getElementById('gen-code').addEventListener('click',async()=>{if(!confirm('¿Generar un nuevo código? El anterior dejará de funcionar.'))return;try{const r=await fetch('/api/account/recovery-code',{method:'POST'});const d=await r.json();if(!r.ok)throw new Error(d.error||'No se pudo generar');document.getElementById('code-value').textContent=d.code;document.getElementById('code-result').style.display='block';}catch(e){alert(e.message)}});</script></div></body></html>
"""


def _send_recovery_email(user, username):
    """Genera una contraseña temporal y la manda por correo (Brevo). Usado
    como respaldo cuando el usuario no configuró una pregunta de
    seguridad. Devuelve el mensaje a mostrar en pantalla.

    IMPORTANTE: solo cambia la contraseña en la base de datos DESPUÉS de
    confirmar que el correo se envió con éxito — así nunca se queda un
    usuario con una contraseña nueva que nunca le llegó."""
    db = get_db()
    rows = {r["key"]: r["value"] for r in db.execute("SELECT key, value FROM settings").fetchall()}
    recovery_email = rows.get("smtp_user", "")
    if not recovery_email:
        return (
            "No hay una pregunta de seguridad configurada para este usuario, ni "
            "un correo de recuperación configurado. Pide ayuda a quien administra "
            "el sistema, o usa el script de recuperación directa en la computadora."
        )

    import secrets

    new_password = secrets.token_urlsafe(9)
    try:
        send_email_with_attachment(
            recovery_email,
            f"Contraseña temporal - usuario {username}",
            f"Se solicitó restablecer la contraseña del usuario '{username}'.\n\n"
            f"Tu contraseña temporal es:\n\n{new_password}\n\n"
            "Inicia sesión con ella y cámbiala de inmediato desde tu perfil.\n\n"
            "Si tú no solicitaste este cambio, revisa quién tiene acceso a tu "
            "sistema — alguien más pudo haberlo pedido.",
        )
    except Exception as e:
        print(f"[recuperar contraseña] Error al enviar: {e}", flush=True)
        return (
            "No se pudo enviar el correo de recuperación en este momento "
            "(el servicio de correo puede no estar disponible todavía). Tu "
            "contraseña actual sigue siendo válida — no se hizo ningún cambio. "
            "Intenta de nuevo más tarde, o pide ayuda a quien administra el sistema."
        )

    # Solo llegamos aquí si el correo se mandó sin errores — ahora sí
    # actualizamos la contraseña.
    db.execute(
        "UPDATE users SET password_hash = ? WHERE id = ?",
        (generate_password_hash(new_password), user["id"]),
    )
    db.commit()
    return (
        "Se envió una contraseña temporal al correo de la clínica configurado. "
        "Revisa también la carpeta de spam."
    )


@app.route("/legal-acceptance", methods=["GET", "POST"])
@login_required
def legal_acceptance():
    db = get_db()
    user = db.execute("SELECT * FROM users WHERE id = ?", (session.get("user_id"),)).fetchone()
    if not user:
        session.clear()
        return redirect(url_for("login"))
    if _terms_accepted_for_current_user(user["id"]):
        return redirect(url_for("index"))
    error = None
    if request.method == "POST":
        if request.form.get("accept_terms") != "1":
            error = "Debes aceptar los términos para continuar usando el sistema."
        else:
            accepted_at = _now()
            db.execute("INSERT INTO legal_acceptances (id, user_id, username, terms_version, accepted_at) VALUES (?, ?, ?, ?, ?)", (uuid.uuid4().hex, user["id"], user["username"], CURRENT_TERMS_VERSION, accepted_at))
            db.commit()
            _log_activity("legal_acceptance", CURRENT_TERMS_VERSION, "Aceptación de términos", user_id=user["id"], username=user["username"])
            return redirect(url_for("index"))
    return render_template_string(_LEGAL_ACCEPTANCE_HTML, error=error, username=user["display_name"] or user["username"], terms_version=CURRENT_TERMS_VERSION)


_LEGAL_ACCEPTANCE_HTML = """
<!DOCTYPE html><html lang="es"><head><meta charset="UTF-8"><meta name="viewport" content="width=device-width, initial-scale=1.0"><title>Aceptación de términos · ClínicaSV</title>
<link href="https://fonts.googleapis.com/css2?family=Space+Grotesk:wght@600;700&family=Inter:wght@400;500;600&display=swap" rel="stylesheet">
<style>body{margin:0;background:#204E45;font-family:Inter,sans-serif;color:#1E2A28;min-height:100vh;display:flex;align-items:center;justify-content:center;padding:20px}.card{background:#fff;border-radius:18px;padding:28px;max-width:700px;width:100%;box-shadow:0 20px 60px rgba(0,0,0,.25)}h1{font-family:'Space Grotesk';margin:0 0 6px}p.sub{color:#6F7D79;font-size:13px;line-height:1.5}.legal{background:#FAF8F4;border:1px solid #DFE5E1;border-radius:12px;padding:16px;max-height:360px;overflow:auto;font-size:12.5px;line-height:1.55}.check{display:flex;gap:10px;align-items:flex-start;margin:18px 0}.check input{margin-top:3px}.error{background:#FBEAE8;color:#A3342B;padding:10px;border-radius:10px;font-size:12.5px;margin-bottom:12px}button{width:100%;border:0;border-radius:999px;background:#E1734F;color:#fff;padding:12px 16px;font-weight:700;cursor:pointer}</style></head>
<body><div class="card"><h1>Actualización de términos de uso</h1><p class="sub">Hola, {{ username }}. Para continuar utilizando ClínicaSV debes revisar y aceptar la versión <strong>{{ terms_version }}</strong>.</p>{% if error %}<div class="error">{{ error }}</div>{% endif %}
<div class="legal"><strong>1. Uso profesional.</strong> ClínicaSV es una herramienta de apoyo para la gestión administrativa y clínica. No sustituye el criterio profesional ni determina diagnósticos o tratamientos por sí misma.<br><br>
<strong>2. Responsabilidad del establecimiento.</strong> La clínica, consultorio y sus usuarios son responsables de la información que registran, las decisiones clínicas, el uso adecuado del sistema y el cumplimiento de la normativa aplicable.<br><br>
<strong>3. Custodia y respaldos.</strong> El establecimiento es responsable de proteger sus credenciales, equipos y copias de seguridad. El sistema ofrece herramientas de respaldo, pero el usuario debe verificar que sus respaldos sean correctos y recuperables.<br><br>
<strong>4. Datos clínicos.</strong> Los datos registrados pertenecen al establecimiento y/o a los responsables correspondientes. La licencia del software no supone cesión de esos datos al proveedor. La pérdida o indisponibilidad de un respaldo externo será responsabilidad de quien lo administre.<br><br>
<strong>5. Disponibilidad.</strong> El software se entrega como herramienta informática y puede presentar errores, mantenimientos o interrupciones. El proveedor no asume responsabilidad por decisiones médicas, administrativas o económicas tomadas con base en el uso del sistema.<br><br>
<strong>6. Aceptación.</strong> Al marcar la aceptación y continuar, el usuario confirma que ha leído estos términos y que utilizará el sistema bajo su propia responsabilidad y la de la clínica/consultorio.</div>
<form method="POST"><div class="check"><input type="checkbox" name="accept_terms" value="1" id="accept_terms" required><label for="accept_terms">He leído y acepto los términos de uso y el descargo de responsabilidad.</label></div><button type="submit">Aceptar y continuar</button></form></div></body></html>
"""

_TERMS_PUBLIC_HTML = """
<!DOCTYPE html><html lang="es"><head><meta charset="UTF-8"><meta name="viewport" content="width=device-width, initial-scale=1.0"><title>Términos y condiciones · ClínicaSV</title>
<link href="https://fonts.googleapis.com/css2?family=Space+Grotesk:wght@600;700&family=Inter:wght@400;500;600&display=swap" rel="stylesheet">
<style>body{margin:0;background:#F5F8FA;font-family:Inter,sans-serif;color:#17212B;padding:28px}.wrap{max-width:860px;margin:0 auto;background:#fff;border:1px solid #E2E8F0;border-radius:20px;padding:28px;box-shadow:0 10px 30px rgba(15,23,42,.06)}h1{font-family:'Space Grotesk';margin:0 0 6px;font-size:27px}p.sub{color:#64748B;font-size:13px}.legal{background:#FBFDFF;border:1px solid #E2E8F0;border-radius:14px;padding:20px;font-size:13px;line-height:1.65}.tag{display:inline-block;background:#E7F3EC;color:#166534;padding:5px 9px;border-radius:999px;font-size:11px;font-weight:700}.back{display:inline-block;margin-top:18px;color:#2563EB;text-decoration:none;font-weight:700;font-size:13px}</style></head>
<body><div class="wrap"><h1>Términos y condiciones</h1><p class="sub">ClínicaSV · Versión <strong>{{ terms_version }}</strong> <span class="tag">Vigente</span></p>
<div class="legal"><strong>1. Uso profesional.</strong><br>ClínicaSV es una herramienta de apoyo para la gestión administrativa y clínica. No sustituye el criterio profesional ni determina diagnósticos o tratamientos por sí misma.<br><br>
<strong>2. Responsabilidad del establecimiento.</strong><br>La clínica, consultorio y sus usuarios son responsables de la información que registran, las decisiones clínicas, el uso adecuado del sistema y el cumplimiento de la normativa aplicable.<br><br>
<strong>3. Custodia y respaldos.</strong><br>El establecimiento es responsable de proteger sus credenciales, equipos y copias de seguridad. El sistema ofrece herramientas de respaldo, pero el usuario debe verificar que sus respaldos sean correctos y recuperables.<br><br>
<strong>4. Datos clínicos.</strong><br>Los datos registrados pertenecen al establecimiento y/o a los responsables correspondientes. La licencia del software no supone cesión de esos datos al proveedor. La pérdida o indisponibilidad de un respaldo externo será responsabilidad de quien lo administre.<br><br>
<strong>5. Licencia de uso.</strong><br>La licencia habilita el uso del software conforme al tipo y vigencia contratados. El vencimiento de una licencia no autoriza al sistema a borrar ni destruir los datos clínicos o respaldos del establecimiento.<br><br>
<strong>6. Disponibilidad.</strong><br>El software puede requerir mantenimientos, actualizaciones o presentar interrupciones. El proveedor no asume responsabilidad por decisiones médicas, administrativas o económicas tomadas con base en el uso del sistema.<br><br>
<strong>7. Aceptación.</strong><br>El uso del sistema implica la aceptación de estos términos. Cuando se publique una nueva versión de los términos, se solicitará una nueva aceptación.</div>
<a class="back" href="/">← Volver a ClínicaSV</a></div></body></html>
"""

@app.route("/forgot-password", methods=["GET", "POST"])
def forgot_password():
    """Recuperación sin internet por pregunta o código local; correo como respaldo."""
    message = None
    question = None
    username_for_step2 = None
    allow_code = False
    if request.method == "POST":
        step = request.form.get("step", "username")
        db = get_db()
        if step == "username":
            username = request.form.get("username", "").strip()
            user = db.execute("SELECT * FROM users WHERE LOWER(username) = LOWER(?)", (username,)).fetchone()
            if user and (user["security_question"] or ""):
                question = user["security_question"]
                username_for_step2 = username
                allow_code = bool(user["recovery_code_hash"])
            elif user and (user["recovery_code_hash"] or ""):
                allow_code = True
                username_for_step2 = username
            elif user:
                message = _send_recovery_email(user, username)
            else:
                message = "Si el usuario existe, se mostrará una opción de recuperación disponible para esa cuenta."
        elif step == "answer":
            username = request.form.get("username", "").strip()
            answer = _normalize_security_answer(request.form.get("answer", ""))
            new_password = request.form.get("new_password", "").strip()
            user = db.execute("SELECT * FROM users WHERE LOWER(username) = LOWER(?)", (username,)).fetchone()
            valid = bool(user and user["security_answer_hash"] and check_password_hash(user["security_answer_hash"], answer))
            if valid and len(new_password) >= 8:
                db.execute("UPDATE users SET password_hash = ? WHERE id = ?", (generate_password_hash(new_password), user["id"]))
                db.commit()
                message = "Contraseña actualizada correctamente. Ya puedes iniciar sesión."
            else:
                message = "Respuesta incorrecta o contraseña muy corta (mínimo 8 caracteres). Intenta de nuevo."
                if user:
                    question = user["security_question"]
                    username_for_step2 = username
                    allow_code = bool(user["recovery_code_hash"])
        elif step == "code":
            username = request.form.get("username", "").strip()
            code = _normalize_recovery_code(request.form.get("recovery_code", ""))
            new_password = request.form.get("new_password", "").strip()
            user = db.execute("SELECT * FROM users WHERE LOWER(username) = LOWER(?)", (username,)).fetchone()
            valid = bool(user and user["recovery_code_hash"] and check_password_hash(user["recovery_code_hash"], code))
            if valid and len(new_password) >= 8:
                db.execute("UPDATE users SET password_hash = ?, recovery_code_hash = '' WHERE id = ?", (generate_password_hash(new_password), user["id"]))
                db.commit()
                message = "Contraseña actualizada correctamente. El código de recuperación usado fue invalidado. Ya puedes iniciar sesión."
            else:
                message = "Código de recuperación incorrecto o contraseña muy corta (mínimo 8 caracteres)."
                if user:
                    username_for_step2 = username
                    question = user["security_question"] or None
                    allow_code = bool(user["recovery_code_hash"])
    return render_template_string(_FORGOT_PASSWORD_HTML, message=message, question=question, username_for_step2=username_for_step2, allow_code=allow_code)


@app.route("/account/security-question", methods=["GET", "POST"])
@login_required
def account_security_question():
    """Permite a un usuario ya conectado configurar (o cambiar) su pregunta
    de seguridad, para poder recuperar su contraseña más adelante sin
    depender de internet ni correo."""
    db = get_db()
    user = db.execute("SELECT * FROM users WHERE username = ?", (session.get("username"),)).fetchone()
    message = None
    if request.method == "POST":
        current_password = request.form.get("current_password", "")
        if not user or not check_password_hash(user["password_hash"], current_password):
            message = "Contraseña actual incorrecta. No se guardaron los cambios."
        else:
            question = request.form.get("security_question", "").strip()[:200]
            answer = request.form.get("security_answer", "").strip().lower()[:200]
            if not question or not answer:
                message = "Completa la pregunta y la respuesta."
            else:
                db.execute(
                    "UPDATE users SET security_question = ?, security_answer_hash = ? WHERE id = ?",
                    (question, generate_password_hash(answer), user["id"]),
                )
                db.commit()
                message = "Pregunta de seguridad guardada correctamente."
                user = db.execute("SELECT * FROM users WHERE id = ?", (user["id"],)).fetchone()
    return render_template_string(
        _SECURITY_QUESTION_HTML, message=message, current_question=(user["security_question"] if user else "")
    )


@app.route("/api/account/recovery-code", methods=["POST"])
@login_required
def generate_recovery_code():
    user_id = session.get("user_id")
    code = _generate_recovery_code()
    db = get_db()
    db.execute("UPDATE users SET recovery_code_hash = ? WHERE id = ?", (generate_password_hash(_normalize_recovery_code(code)), user_id))
    db.commit()
    return jsonify({"ok": True, "code": code, "message": "Guárdalo o imprímelo ahora. Por seguridad no volverá a mostrarse."})


@app.route("/api/license", methods=["GET"])
@login_required
def get_license():
    return jsonify(_license_from_settings())


@app.route("/api/license/activate", methods=["POST"])
@login_required
@admin_required
def activate_license():
    data = request.get_json(force=True)
    token = (data.get("token") or "").strip()
    if not token:
        return jsonify({"error": "Pega el token de licencia firmado."}), 400
    try:
        payload = _decode_signed_license(token)
    except RuntimeError as exc:
        return jsonify({"error": str(exc)}), 503
    except (ValueError, json.JSONDecodeError, UnicodeDecodeError) as exc:
        return jsonify({"error": f"No se pudo validar la licencia: {exc}"}), 400
    expected_installation = _license_from_settings()["installationId"]
    expected_machine = _machine_fingerprint()
    if payload.get("product") != LICENSE_PRODUCT:
        return jsonify({"error": "La licencia no corresponde a ClínicaSV Medical."}), 400
    if payload.get("installation_id") and payload.get("installation_id") != expected_installation:
        return jsonify({"error": "La licencia corresponde a otra instalación."}), 400
    if payload.get("machine_binding") and payload.get("machine_binding") != expected_machine:
        return jsonify({"error": "La licencia corresponde a otro equipo."}), 400
    exp = payload.get("expires_at") or ""
    if exp:
        try:
            if datetime.fromisoformat(exp) < datetime.now():
                return jsonify({"error": "La licencia ya está vencida."}), 400
        except ValueError:
            return jsonify({"error": "Fecha de vencimiento inválida."}), 400
    db = get_db()
    updates = {
        "license_id": payload.get("license_id", ""),
        "license_type": payload.get("type", "Licencia"),
        "license_holder": payload.get("holder", ""),
        "license_issued_at": payload.get("issued_at", ""),
        "license_expires_at": exp,
        "license_status": "activa",
        "license_features": ",".join(payload.get("features", []) or []),
        "license_token": token,
    }
    for key, value in updates.items():
        db.execute("UPDATE settings SET value = ? WHERE key = ?", (str(value), key))
    db.commit()
    _log_activity("license_activation", payload.get("license_id", ""), "Licencia activada mediante token.")
    return jsonify(_license_from_settings())


@app.route("/api/license/activate-file", methods=["POST"])
@login_required
@admin_required
def activate_license_file():
    uploaded = request.files.get("license_file")
    if not uploaded or not uploaded.filename:
        return jsonify({"error": "Selecciona un archivo de licencia .clsv."}), 400
    filename = secure_filename(uploaded.filename)
    if not filename.lower().endswith(".clsv"):
        return jsonify({"error": "El archivo debe tener extensión .clsv."}), 400
    try:
        token = uploaded.read().decode("utf-8").strip()
    except Exception:
        return jsonify({"error": "No se pudo leer el archivo de licencia."}), 400
    try:
        payload = _decode_signed_license(token)
    except RuntimeError as exc:
        return jsonify({"error": str(exc)}), 503
    except (ValueError, json.JSONDecodeError, UnicodeDecodeError) as exc:
        return jsonify({"error": f"No se pudo validar la licencia: {exc}"}), 400
    expected_installation = _license_from_settings()["installationId"]
    expected_machine = _machine_fingerprint()
    if payload.get("product") != LICENSE_PRODUCT:
        return jsonify({"error": "La licencia no corresponde a ClínicaSV Medical."}), 400
    if payload.get("installation_id") and payload.get("installation_id") != expected_installation:
        return jsonify({"error": "La licencia corresponde a otra instalación."}), 400
    if payload.get("machine_binding") and payload.get("machine_binding") != expected_machine:
        return jsonify({"error": "La licencia corresponde a otro equipo."}), 400
    exp = payload.get("expires_at") or ""
    if exp:
        try:
            if datetime.fromisoformat(exp) < datetime.now():
                return jsonify({"error": "La licencia ya está vencida."}), 400
        except ValueError:
            return jsonify({"error": "Fecha de vencimiento inválida."}), 400
    db = get_db()
    updates = {
        "license_id": payload.get("license_id", ""),
        "license_type": payload.get("type", "Licencia"),
        "license_holder": payload.get("holder", ""),
        "license_issued_at": payload.get("issued_at", ""),
        "license_expires_at": exp,
        "license_status": "activa",
        "license_features": ",".join(payload.get("features", []) or []),
        "license_token": token,
    }
    for key, value in updates.items():
        db.execute("UPDATE settings SET value = ? WHERE key = ?", (str(value), key))
    db.commit()
    _log_activity("license_activation", payload.get("license_id", ""), f"Licencia importada desde {filename}.")
    return jsonify(_license_from_settings())


@app.route("/terms")
def public_terms():
    return render_template_string(_TERMS_PUBLIC_HTML, terms_version=CURRENT_TERMS_VERSION)


@app.route("/login", methods=["GET", "POST"])
def login():
    if not any_user_exists():
        return redirect(url_for("setup"))
    if session.get("user_id"):
        return redirect(url_for("index"))

    error = None
    if request.method == "POST":
        username = request.form.get("username", "").strip()
        password = request.form.get("password", "")

        key = username.lower()
        rec = _failed_attempts.get(key)
        if rec and rec["count"] >= MAX_ATTEMPTS and (time.time() - rec["ts"]) < LOCKOUT_SECONDS:
            error = "Demasiados intentos fallidos. Espera un minuto e intenta de nuevo."
        else:
            db = get_db()
            user = db.execute("SELECT * FROM users WHERE username = ?", (username,)).fetchone()
            if user and not bool(user["active"]):
                error = "Esta cuenta está desactivada. Contacta al administrador."
            elif user and check_password_hash(user["password_hash"], password):
                _failed_attempts.pop(key, None)
                now = _now()
                db.execute("UPDATE users SET last_login_at = ? WHERE id = ?", (now, user["id"]))
                db.commit()
                session.clear()
                session.permanent = True
                session["user_id"] = user["id"]
                session["username"] = user["username"]
                session["role"] = user["role"]
                session["permissions"] = user["permissions"]
                session["must_change_password"] = bool(user["must_change_password"])
                _log_activity("login", user["username"], "Inicio de sesión", user_id=user["id"], username=user["username"])
                if not _terms_accepted_for_current_user(user["id"]):
                    return redirect(url_for("legal_acceptance"))
                if bool(user["must_change_password"]):
                    return redirect(url_for("force_password_change"))
                return redirect(url_for("index"))
            else:
                rec = _failed_attempts.get(key, {"count": 0, "ts": 0})
                rec["count"] += 1
                rec["ts"] = time.time()
                _failed_attempts[key] = rec
                error = "Usuario o contraseña incorrectos."
    return render_template("login.html", error=error)


@app.route("/logout")
def logout():
    session.clear()
    return redirect(url_for("login"))


@app.route("/account/force-password", methods=["GET", "POST"])
@login_required
def force_password_change():
    error = None
    if request.method == "POST":
        current = request.form.get("current", "")
        new = request.form.get("new", "")
        confirm = request.form.get("confirm", "")
        db = get_db()
        user = db.execute("SELECT * FROM users WHERE id = ?", (session.get("user_id"),)).fetchone()
        if not user or not check_password_hash(user["password_hash"], current):
            error = "La contraseña actual no es correcta."
        elif len(new) < 8:
            error = "La nueva contraseña debe tener al menos 8 caracteres."
        elif new != confirm:
            error = "Las nuevas contraseñas no coinciden."
        else:
            db.execute("UPDATE users SET password_hash = ?, must_change_password = 0 WHERE id = ?", (generate_password_hash(new), user["id"]))
            db.commit()
            session["must_change_password"] = False
            _log_activity("password_change_required", user["username"], "Cambio de contraseña obligatorio completado", user_id=user["id"], username=user["username"])
            return redirect(url_for("index"))
    return render_template_string("""<!DOCTYPE html><html lang='es'><head><meta charset='utf-8'><meta name='viewport' content='width=device-width,initial-scale=1'><title>Cambiar contraseña · ClínicaSV</title><style>body{margin:0;background:#FAF8F4;font-family:Inter,Arial,sans-serif;color:#1E2A28;display:flex;align-items:center;justify-content:center;min-height:100vh;padding:20px}.card{background:#fff;border:1px solid #DFE5E1;border-radius:16px;padding:28px;max-width:420px;width:100%;box-shadow:0 16px 40px rgba(0,0,0,.08)}h1{margin:0 0 6px;font:700 22px Space Grotesk,Arial}.sub{color:#7C8B87;font-size:13px;line-height:1.5}.error{background:#FBEAE8;color:#C0463C;padding:10px;border-radius:8px;font-size:13px;margin:12px 0}label{display:block;font-size:12px;font-weight:600;margin:12px 0 5px;color:#204E45}input{width:100%;box-sizing:border-box;padding:11px;border:1px solid #DFE5E1;border-radius:9px;font-size:14px}button{margin-top:16px;width:100%;border:0;border-radius:9px;padding:11px;background:#2F6F62;color:#fff;font-weight:700;cursor:pointer}.terms{margin-top:12px;font-size:11.5px;color:#7C8B87}a{color:#2F6F62}</style></head><body><div class='card'><h1>Cambia tu contraseña</h1><p class='sub'>El administrador solicitó un cambio de contraseña antes de continuar.</p>{% if error %}<div class='error'>{{error}}</div>{% endif %}<form method='POST'><label>Contraseña actual</label><input type='password' name='current' required autofocus><label>Nueva contraseña (mínimo 8 caracteres)</label><input type='password' name='new' minlength='8' required><label>Repetir nueva contraseña</label><input type='password' name='confirm' minlength='8' required><button type='submit'>Guardar contraseña</button></form><div class='terms'><a href='/logout'>Cerrar sesión</a></div></div></body></html>""", error=error)

@app.route("/api/change-password", methods=["POST"])
@login_required
def change_password():
    data = request.get_json(force=True)
    current = data.get("current", "")
    new = data.get("new", "")
    if len(new) < 8:
        return jsonify({"error": "La nueva contraseña debe tener al menos 8 caracteres."}), 400
    db = get_db()
    user = db.execute("SELECT * FROM users WHERE id = ?", (session["user_id"],)).fetchone()
    if not user or not check_password_hash(user["password_hash"], current):
        return jsonify({"error": "La contraseña actual no es correcta."}), 400
    db.execute(
        "UPDATE users SET password_hash = ?, must_change_password = 0 WHERE id = ?",
        (generate_password_hash(new), user["id"]),
    )
    db.commit()
    session["must_change_password"] = False
    _log_activity("password_change", user["username"], "Contraseña cambiada", user_id=user["id"], username=user["username"])
    return jsonify({"ok": True})


@app.route("/")
@login_required
def index():
    return render_template("index.html", username=session.get("username", ""))


@app.route("/api/medications", methods=["GET"])
@login_required
def get_medications():
    path = os.path.join(BASE_DIR, "vademecum", "medicamentos.json")
    if not os.path.exists(path):
        return jsonify({"version":"", "fuente":"", "fecha_actualizacion":"", "medicamentos":[]})
    try:
        with open(path, "r", encoding="utf-8") as fh:
            data = json.load(fh)
        meds = data.get("medicamentos", []) if isinstance(data, dict) else []
        clean = []
        for m in meds:
            if isinstance(m, dict):
                clean.append({
                    "nombre": str(m.get("nombre", ""))[:200],
                    "principio_activo": str(m.get("principio_activo", ""))[:200],
                    "presentacion": str(m.get("presentacion", ""))[:200],
                    "via": str(m.get("via", ""))[:100],
                })
        return jsonify({
            "version": data.get("version", ""),
            "fuente": data.get("fuente", ""),
            "fecha_actualizacion": data.get("fecha_actualizacion", ""),
            "medicamentos": clean,
        })
    except (OSError, json.JSONDecodeError):
        return jsonify({"error":"No se pudo leer el catálogo de medicamentos."}), 500


@app.route("/uploads/<path:subpath>")
@login_required
def serve_upload(subpath):
    if USE_POSTGRES:
        db = get_db()
        row = db.execute("SELECT data, content_type FROM file_blobs WHERE id = ?", (subpath,)).fetchone()
        if not row:
            return jsonify({"error": "Archivo no encontrado."}), 404
        return Response(bytes(row["data"]), mimetype=row["content_type"])
    return send_from_directory(UPLOAD_DIR, subpath)


def _now():
    return time.strftime("%Y-%m-%dT%H:%M:%S")


EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")


def _clean(s):
    return (s or "").strip()


def patient_to_dict(row):
    d = dict(row)
    d["photoUrl"] = ("/uploads/" + d["photo"]) if d.get("photo") else None
    return d


def appt_to_dict(row):
    d = dict(row)
    d["patientId"] = d.pop("patient_id")
    return d


def doc_to_dict(row):
    d = dict(row)
    d["patientId"] = d.pop("patient_id")
    d["url"] = "/uploads/documents/" + d["filename"]
    return d


def charge_to_dict(row):
    d = dict(row)
    d["patientId"] = d.pop("patient_id")
    cost = d["cost"] or 0
    paid = d["paid"] or 0
    if paid <= 0:
        d["status"] = "pendiente"
    elif paid >= cost:
        d["status"] = "pagado"
    else:
        d["status"] = "parcial"
    d["balance"] = round(cost - paid, 2)
    return d


@app.route("/api/patients", methods=["GET"])
@login_required
def list_patients():
    db = get_db()
    order_sql = "SELECT * FROM patients ORDER BY name" if USE_POSTGRES else "SELECT * FROM patients ORDER BY name COLLATE NOCASE"
    rows = db.execute(order_sql).fetchall()
    return jsonify([patient_to_dict(r) for r in rows])


@app.route("/api/patients", methods=["POST"])
@login_required
def create_patient():
    data = request.get_json(force=True)
    name = _clean(data.get("name"))
    phone = _clean(data.get("phone"))
    if not name or not phone:
        return jsonify({"error": "Nombre y teléfono son obligatorios."}), 400
    email = _clean(data.get("email"))
    if email and not EMAIL_RE.match(email):
        return jsonify({"error": "El email no parece válido."}), 400

    pid = uuid.uuid4().hex
    now = _now()
    db = get_db()
    db.execute(
        """INSERT INTO patients (id, name, dob, phone, email, address, allergies, history, notes, dui, created_at, updated_at)
           VALUES (?,?,?,?,?,?,?,?,?,?,?,?)""",
        (pid, name, _clean(data.get("dob")), phone, email, _clean(data.get("address")),
         _clean(data.get("allergies")), _clean(data.get("history")), _clean(data.get("notes")),
         _clean(data.get("dui")), now, now),
    )
    db.commit()
    row = db.execute("SELECT * FROM patients WHERE id = ?", (pid,)).fetchone()
    return jsonify(patient_to_dict(row)), 201


@app.route("/api/patients/<pid>", methods=["PUT"])
@login_required
def update_patient(pid):
    db = get_db()
    existing = db.execute("SELECT * FROM patients WHERE id = ?", (pid,)).fetchone()
    if not existing:
        return jsonify({"error": "Paciente no encontrado."}), 404
    data = request.get_json(force=True)
    name = _clean(data.get("name"))
    phone = _clean(data.get("phone"))
    if not name or not phone:
        return jsonify({"error": "Nombre y teléfono son obligatorios."}), 400
    email = _clean(data.get("email"))
    if email and not EMAIL_RE.match(email):
        return jsonify({"error": "El email no parece válido."}), 400

    db.execute(
        """UPDATE patients SET name=?, dob=?, phone=?, email=?, address=?, allergies=?, history=?, notes=?, dui=?, updated_at=?
           WHERE id=?""",
        (name, _clean(data.get("dob")), phone, email, _clean(data.get("address")),
         _clean(data.get("allergies")), _clean(data.get("history")), _clean(data.get("notes")),
         _clean(data.get("dui")), _now(), pid),
    )
    db.commit()
    row = db.execute("SELECT * FROM patients WHERE id = ?", (pid,)).fetchone()
    return jsonify(patient_to_dict(row))


@app.route("/api/patients/<pid>", methods=["DELETE"])
@login_required
def delete_patient(pid):
    db = get_db()
    row = db.execute("SELECT photo FROM patients WHERE id = ?", (pid,)).fetchone()
    db.execute("DELETE FROM patients WHERE id = ?", (pid,))
    db.commit()
    if row and row["photo"]:
        _delete_uploaded_file(row["photo"])
    return jsonify({"ok": True})


def _safe_remove(path, base_dir=UPLOAD_DIR):
    """Borra un archivo, pero solo si está dentro de una carpeta permitida
    (uploads/ o backups/, según se indique) — por seguridad, para no borrar
    nada fuera de esas carpetas por accidente."""
    try:
        base_abs = os.path.abspath(base_dir)
        if os.path.commonpath([os.path.abspath(path), base_abs]) == base_abs and os.path.exists(path):
            os.remove(path)
            return True
    except (OSError, ValueError):
        pass
    return False


def _resize_and_compress_image(file_storage, max_dimension=800, quality=82):
    """Redimensiona (máximo 800px de lado) y comprime a JPEG una imagen antes
    de guardarla, para no ocupar espacio de más — importante sobre todo con
    el límite de 0.5 GB del plan gratuito de Neon. Devuelve
    (bytes, content_type, extensión)."""
    from PIL import Image
    import io

    img = Image.open(file_storage.stream)
    img = img.convert("RGB")  # unifica todo a JPEG (sin canal de transparencia)
    img.thumbnail((max_dimension, max_dimension), Image.LANCZOS)
    buf = io.BytesIO()
    img.save(buf, format="JPEG", quality=quality, optimize=True)
    buf.seek(0)
    return buf.read(), "image/jpeg", "jpg"


def _save_uploaded_bytes(rel_path, data, content_type="application/octet-stream"):
    """Como _save_uploaded_file, pero recibe bytes ya listos en vez de un
    FileStorage (para guardar una imagen ya redimensionada/comprimida)."""
    if USE_POSTGRES:
        db = get_db()
        db.execute(
            """INSERT INTO file_blobs (id, data, content_type, created_at) VALUES (?, ?, ?, ?)
               ON CONFLICT (id) DO UPDATE SET data = excluded.data, content_type = excluded.content_type""",
            (rel_path, psycopg2.Binary(data), content_type, _now()),
        )
        db.commit()
    else:
        with open(os.path.join(UPLOAD_DIR, rel_path), "wb") as f:
            f.write(data)


def _save_uploaded_file(rel_path, file_storage):
    """Guarda un archivo subido. En PostgreSQL (Render u otro hosting sin disco
    persistente) lo guarda dentro de la base de datos; en SQLite local, en el
    disco como siempre."""
    if USE_POSTGRES:
        data = file_storage.read()
        content_type = file_storage.mimetype or "application/octet-stream"
        db = get_db()
        db.execute(
            """INSERT INTO file_blobs (id, data, content_type, created_at) VALUES (?, ?, ?, ?)
               ON CONFLICT (id) DO UPDATE SET data = excluded.data, content_type = excluded.content_type""",
            (rel_path, psycopg2.Binary(data), content_type, _now()),
        )
        db.commit()
    else:
        file_storage.save(os.path.join(UPLOAD_DIR, rel_path))


def _delete_uploaded_file(rel_path):
    if not rel_path:
        return
    if USE_POSTGRES:
        db = get_db()
        db.execute("DELETE FROM file_blobs WHERE id = ?", (rel_path,))
        db.commit()
    else:
        _safe_remove(os.path.join(UPLOAD_DIR, rel_path))


def _read_uploaded_file(rel_path):
    """Devuelve los bytes de un archivo subido, sin importar si vive en disco
    (SQLite local) o en la base de datos (PostgreSQL). None si no existe."""
    if USE_POSTGRES:
        db = get_db()
        row = db.execute("SELECT data FROM file_blobs WHERE id = ?", (rel_path,)).fetchone()
        return bytes(row["data"]) if row else None
    full_path = os.path.join(UPLOAD_DIR, rel_path)
    if not os.path.exists(full_path):
        return None
    with open(full_path, "rb") as f:
        return f.read()


@app.route("/api/patients/<pid>/photo", methods=["POST"])
@login_required
def upload_patient_photo(pid):
    db = get_db()
    patient = db.execute("SELECT * FROM patients WHERE id = ?", (pid,)).fetchone()
    if not patient:
        return jsonify({"error": "Paciente no encontrado."}), 404
    file = request.files.get("photo")
    if not file or file.filename == "":
        return jsonify({"error": "No se recibió ninguna imagen."}), 400
    if not _ext_ok(file.filename, IMAGE_EXTS):
        return jsonify({"error": "Formato no permitido. Usa JPG, JPEG, PNG, WEBP, GIF, BMP o TIFF."}), 400

    if patient["photo"]:
        _delete_uploaded_file(patient["photo"])

    try:
        img_bytes, content_type, ext = _resize_and_compress_image(file)
    except Exception as e:
        return jsonify({"error": f"No se pudo procesar la imagen: {e}"}), 400

    rel_path = f"photos/{pid}_{uuid.uuid4().hex[:8]}.{ext}"
    _save_uploaded_bytes(rel_path, img_bytes, content_type)
    db.execute("UPDATE patients SET photo = ?, updated_at = ? WHERE id = ?", (rel_path, _now(), pid))
    db.commit()
    row = db.execute("SELECT * FROM patients WHERE id = ?", (pid,)).fetchone()
    return jsonify(patient_to_dict(row))


@app.route("/api/patients/<pid>/photo", methods=["DELETE"])
@login_required
def delete_patient_photo(pid):
    db = get_db()
    patient = db.execute("SELECT * FROM patients WHERE id = ?", (pid,)).fetchone()
    if not patient:
        return jsonify({"error": "Paciente no encontrado."}), 404
    if patient["photo"]:
        _delete_uploaded_file(patient["photo"])
    db.execute("UPDATE patients SET photo = NULL, updated_at = ? WHERE id = ?", (_now(), pid))
    db.commit()
    row = db.execute("SELECT * FROM patients WHERE id = ?", (pid,)).fetchone()
    return jsonify(patient_to_dict(row))


VALID_DOC_CATEGORIES = {"radiografia", "examen", "otro"}


@app.route("/api/patients/<pid>/documents", methods=["GET"])
@login_required
def list_documents(pid):
    db = get_db()
    rows = db.execute(
        "SELECT * FROM documents WHERE patient_id = ? ORDER BY uploaded_at DESC", (pid,)
    ).fetchall()
    return jsonify([doc_to_dict(r) for r in rows])


@app.route("/api/patients/<pid>/documents", methods=["POST"])
@login_required
def upload_document(pid):
    db = get_db()
    patient = db.execute("SELECT id FROM patients WHERE id = ?", (pid,)).fetchone()
    if not patient:
        return jsonify({"error": "Paciente no encontrado."}), 404
    file = request.files.get("file")
    if not file or file.filename == "":
        return jsonify({"error": "No se recibió ningún archivo."}), 400
    if not _ext_ok(file.filename, DOCUMENT_EXTS):
        return jsonify({"error": "Formato no permitido. Usa JPG, PNG, WEBP, GIF o PDF."}), 400
    category = request.form.get("category", "otro")
    if category not in VALID_DOC_CATEGORIES:
        category = "otro"

    original = secure_filename(file.filename)
    ext = original.rsplit(".", 1)[1].lower()
    did = uuid.uuid4().hex

    if ext in IMAGE_EXTS:
        # Igual que con la foto de perfil: comprimimos para ahorrar espacio.
        # Usamos un tamaño mayor (1600px) y más calidad que en la foto de
        # perfil, para que una radiografía siga siendo legible clínicamente.
        try:
            img_bytes, content_type, ext = _resize_and_compress_image(file, max_dimension=1600, quality=85)
        except Exception as e:
            return jsonify({"error": f"No se pudo procesar la imagen: {e}"}), 400
        stored_name = f"{did}.{ext}"
        _save_uploaded_bytes(f"documents/{stored_name}", img_bytes, content_type)
    else:
        # PDF: se guarda tal cual, sin comprimir.
        stored_name = f"{did}.{ext}"
        _save_uploaded_file(f"documents/{stored_name}", file)
    db.execute(
        """INSERT INTO documents (id, patient_id, filename, original_name, category, uploaded_at)
           VALUES (?,?,?,?,?,?)""",
        (did, pid, stored_name, original, category, _now()),
    )
    db.commit()
    row = db.execute("SELECT * FROM documents WHERE id = ?", (did,)).fetchone()
    return jsonify(doc_to_dict(row)), 201


@app.route("/api/documents/<did>", methods=["DELETE"])
@login_required
def delete_document(did):
    db = get_db()
    row = db.execute("SELECT * FROM documents WHERE id = ?", (did,)).fetchone()
    if row:
        _delete_uploaded_file(f"documents/{row['filename']}")
        db.execute("DELETE FROM documents WHERE id = ?", (did,))
        db.commit()
    return jsonify({"ok": True})


VALID_STATUS = {"confirmada", "pendiente", "cancelada"}


@app.route("/api/appointments", methods=["GET"])
@login_required
def list_appointments():
    db = get_db()
    date = request.args.get("date")
    if date:
        rows = db.execute("SELECT * FROM appointments WHERE date = ? ORDER BY time", (date,)).fetchall()
    else:
        rows = db.execute("SELECT * FROM appointments ORDER BY date, time").fetchall()
    return jsonify([appt_to_dict(r) for r in rows])


@app.route("/api/appointments", methods=["POST"])
@login_required
def create_appointment():
    data = request.get_json(force=True)
    patient_id = _clean(data.get("patientId"))
    date = _clean(data.get("date"))
    time_ = _clean(data.get("time"))
    if not patient_id or not date or not time_:
        return jsonify({"error": "Paciente, fecha y hora son obligatorios."}), 400
    status = data.get("status") if data.get("status") in VALID_STATUS else "pendiente"

    db = get_db()
    patient = db.execute("SELECT id FROM patients WHERE id = ?", (patient_id,)).fetchone()
    if not patient:
        return jsonify({"error": "El paciente no existe."}), 400

    aid = uuid.uuid4().hex
    now = _now()
    db.execute(
        """INSERT INTO appointments (id, patient_id, date, time, duration, status, reason, notes, created_at, updated_at)
           VALUES (?,?,?,?,?,?,?,?,?,?)""",
        (aid, patient_id, date, time_, int(data.get("duration") or 30), status,
         _clean(data.get("reason")), _clean(data.get("notes")), now, now),
    )
    db.commit()
    row = db.execute("SELECT * FROM appointments WHERE id = ?", (aid,)).fetchone()
    return jsonify(appt_to_dict(row)), 201


@app.route("/api/appointments/<aid>", methods=["PUT"])
@login_required
def update_appointment(aid):
    db = get_db()
    existing = db.execute("SELECT * FROM appointments WHERE id = ?", (aid,)).fetchone()
    if not existing:
        return jsonify({"error": "Cita no encontrada."}), 404
    data = request.get_json(force=True)
    patient_id = _clean(data.get("patientId"))
    date = _clean(data.get("date"))
    time_ = _clean(data.get("time"))
    if not patient_id or not date or not time_:
        return jsonify({"error": "Paciente, fecha y hora son obligatorios."}), 400
    status = data.get("status") if data.get("status") in VALID_STATUS else "pendiente"

    db.execute(
        """UPDATE appointments SET patient_id=?, date=?, time=?, duration=?, status=?, reason=?, notes=?, updated_at=?
           WHERE id=?""",
        (patient_id, date, time_, int(data.get("duration") or 30), status,
         _clean(data.get("reason")), _clean(data.get("notes")), _now(), aid),
    )
    db.commit()
    row = db.execute("SELECT * FROM appointments WHERE id = ?", (aid,)).fetchone()
    return jsonify(appt_to_dict(row))


@app.route("/api/appointments/<aid>", methods=["DELETE"])
@login_required
def delete_appointment(aid):
    db = get_db()
    db.execute("DELETE FROM appointments WHERE id = ?", (aid,))
    db.commit()
    return jsonify({"ok": True})


@app.route("/api/charges", methods=["GET"])
@login_required
def list_charges():
    db = get_db()
    patient_id = request.args.get("patient_id")
    if patient_id:
        rows = db.execute(
            "SELECT * FROM charges WHERE patient_id = ? ORDER BY date DESC", (patient_id,)
        ).fetchall()
    else:
        rows = db.execute("SELECT * FROM charges ORDER BY date DESC").fetchall()
    return jsonify([charge_to_dict(r) for r in rows])


@app.route("/api/charges", methods=["POST"])
@login_required
def create_charge():
    data = request.get_json(force=True)
    patient_id = _clean(data.get("patientId"))
    treatment = _clean(data.get("treatment"))
    date = _clean(data.get("date")) or time.strftime("%Y-%m-%d")
    if not patient_id or not treatment:
        return jsonify({"error": "Paciente y tratamiento son obligatorios."}), 400
    db = get_db()
    patient = db.execute("SELECT id FROM patients WHERE id = ?", (patient_id,)).fetchone()
    if not patient:
        return jsonify({"error": "El paciente no existe."}), 400
    try:
        cost = float(data.get("cost") or 0)
        paid = float(data.get("paid") or 0)
    except (TypeError, ValueError):
        return jsonify({"error": "Costo y monto pagado deben ser números."}), 400

    cid = uuid.uuid4().hex
    now = _now()
    db.execute(
        """INSERT INTO charges (id, patient_id, date, treatment, cost, paid, notes, created_at, updated_at)
           VALUES (?,?,?,?,?,?,?,?,?)""",
        (cid, patient_id, date, treatment, cost, paid, _clean(data.get("notes")), now, now),
    )
    db.commit()
    row = db.execute("SELECT * FROM charges WHERE id = ?", (cid,)).fetchone()
    return jsonify(charge_to_dict(row)), 201


@app.route("/api/charges/<cid>", methods=["PUT"])
@login_required
def update_charge(cid):
    db = get_db()
    existing = db.execute("SELECT * FROM charges WHERE id = ?", (cid,)).fetchone()
    if not existing:
        return jsonify({"error": "Registro no encontrado."}), 404
    data = request.get_json(force=True)
    treatment = _clean(data.get("treatment"))
    date = _clean(data.get("date"))
    if not treatment or not date:
        return jsonify({"error": "Tratamiento y fecha son obligatorios."}), 400
    try:
        cost = float(data.get("cost") or 0)
        paid = float(data.get("paid") or 0)
    except (TypeError, ValueError):
        return jsonify({"error": "Costo y monto pagado deben ser números."}), 400

    db.execute(
        """UPDATE charges SET date=?, treatment=?, cost=?, paid=?, notes=?, updated_at=? WHERE id=?""",
        (date, treatment, cost, paid, _clean(data.get("notes")), _now(), cid),
    )
    db.commit()
    row = db.execute("SELECT * FROM charges WHERE id = ?", (cid,)).fetchone()
    return jsonify(charge_to_dict(row))


@app.route("/api/charges/<cid>", methods=["DELETE"])
@login_required
def delete_charge(cid):
    db = get_db()
    db.execute("DELETE FROM charges WHERE id = ?", (cid,))
    db.commit()
    return jsonify({"ok": True})


ALLOWED_FONTS = {
    "Space Grotesk": "'Space Grotesk', sans-serif",
    "Inter": "'Inter', sans-serif",
    "Poppins": "'Poppins', sans-serif",
    "Merriweather": "'Merriweather', serif",
    "IBM Plex Mono": "'IBM Plex Mono', monospace",
}

HEX_RE = re.compile(r"^#[0-9a-fA-F]{6}$")


@app.route("/api/settings", methods=["GET"])
@login_required
def get_settings():
    db = get_db()
    rows = db.execute("SELECT key, value FROM settings").fetchall()
    result = dict(DEFAULT_SETTINGS)
    for r in rows:
        if r["key"] in EMAIL_SETTINGS or r["key"] in SYNC_SETTINGS:
            continue
        result[r["key"]] = r["value"]
    result["logoUrl"] = ("/uploads/" + result["logo_path"]) if result.get("logo_path") else None
    result["fonts"] = list(ALLOWED_FONTS.keys())
    return jsonify(result)


@app.route("/api/settings", methods=["POST"])
@login_required
@admin_required
def update_settings():
    data = request.get_json(force=True)
    db = get_db()

    clinic_name = _clean(data.get("clinic_name"))
    if clinic_name:
        db.execute("UPDATE settings SET value = ? WHERE key = 'clinic_name'", (clinic_name[:80],))

    if "doctor_name" in data:
        db.execute("UPDATE settings SET value = ? WHERE key = 'doctor_name'", (_clean(data.get("doctor_name"))[:80],))

    if "doctor_specialty" in data:
        specialty = _clean(data.get("doctor_specialty"))[:80]
        if specialty:
            db.execute("UPDATE settings SET value = ? WHERE key = 'doctor_specialty'", (specialty,))

    simple_doc_fields = {
        "doctor_registration": 100,
        "doctor_phone": 60,
        "doctor_email": 120,
        "clinic_address": 220,
        "document_header": 500,
        "document_footer": 500,
    }
    for key, max_len in simple_doc_fields.items():
        if key in data:
            db.execute("UPDATE settings SET value = ? WHERE key = ?", (_clean(data.get(key))[:max_len], key))
    for key in ("show_doctor_on_documents", "show_clinic_on_documents"):
        if key in data:
            db.execute("UPDATE settings SET value = ? WHERE key = ?", ("1" if data.get(key) else "0", key))

    for color_key in ("primary_color", "accent_color", "bg_color"):
        val = _clean(data.get(color_key))
        if val and HEX_RE.match(val):
            db.execute("UPDATE settings SET value = ? WHERE key = ?", (val, color_key))

    font = data.get("font_family")
    if font in ALLOWED_FONTS:
        db.execute("UPDATE settings SET value = ? WHERE key = 'font_family'", (font,))

    if "font_size" in data:
        try:
            size = int(data.get("font_size"))
            if 12 <= size <= 20:
                db.execute("UPDATE settings SET value = ? WHERE key = 'font_size'", (str(size),))
        except (TypeError, ValueError):
            pass

    if "welcome_message" in data:
        db.execute("UPDATE settings SET value = ? WHERE key = 'welcome_message'", (_clean(data.get("welcome_message"))[:200],))

    if "logo_size" in data:
        try:
            size = int(data.get("logo_size"))
            if 24 <= size <= 120:
                db.execute("UPDATE settings SET value = ? WHERE key = 'logo_size'", (str(size),))
        except (TypeError, ValueError):
            pass

    if "disclaimer_text" in data:
        db.execute("UPDATE settings SET value = ? WHERE key = 'disclaimer_text'", (_clean(data.get("disclaimer_text"))[:1000],))

    if "backup_hour" in data:
        val = _clean(data.get("backup_hour"))
        if re.match(r"^\d{1,2}:\d{2}$", val or ""):
            db.execute("UPDATE settings SET value = ? WHERE key = 'backup_hour'", (val,))
    if "backup_enabled" in data:
        db.execute("UPDATE settings SET value = ? WHERE key = 'backup_enabled'", ("1" if data.get("backup_enabled") else "0",))
    if "backup_folder" in data:
        db.execute("UPDATE settings SET value = ? WHERE key = 'backup_folder'", (_clean(data.get("backup_folder"))[:400],))

    db.commit()
    return get_settings()


@app.route("/api/settings/email", methods=["GET"])
@login_required
@admin_required
def get_email_settings():
    db = get_db()
    rows = {r["key"]: r["value"] for r in db.execute("SELECT key, value FROM settings").fetchall()}
    return jsonify({
        "smtp_host": rows.get("smtp_host", ""),
        "smtp_port": rows.get("smtp_port", "587"),
        "smtp_user": rows.get("smtp_user", ""),
        "smtp_use_tls": rows.get("smtp_use_tls", "1") == "1",
        "smtp_from_name": rows.get("smtp_from_name", ""),
        "hasPassword": bool(rows.get("smtp_password")),
    })


@app.route("/api/settings/email", methods=["POST"])
@login_required
@admin_required
def update_email_settings():
    data = request.get_json(force=True)
    db = get_db()
    db.execute("UPDATE settings SET value = ? WHERE key = 'smtp_host'", (_clean(data.get("smtp_host"))[:200],))
    port = _clean(data.get("smtp_port")) or "587"
    if port.isdigit():
        db.execute("UPDATE settings SET value = ? WHERE key = 'smtp_port'", (port,))
    db.execute("UPDATE settings SET value = ? WHERE key = 'smtp_user'", (_clean(data.get("smtp_user"))[:200],))
    db.execute("UPDATE settings SET value = ? WHERE key = 'smtp_use_tls'", ("1" if data.get("smtp_use_tls") else "0",))
    db.execute("UPDATE settings SET value = ? WHERE key = 'smtp_from_name'", (_clean(data.get("smtp_from_name"))[:120],))
    if data.get("smtp_password"):
        db.execute("UPDATE settings SET value = ? WHERE key = 'smtp_password'", (data["smtp_password"],))
    db.commit()
    return get_email_settings()


@app.route("/api/settings/sync", methods=["GET"])
@login_required
@admin_required
def get_sync_settings():
    db = get_db()
    rows = {r["key"]: r["value"] for r in db.execute("SELECT key, value FROM settings").fetchall()}
    return jsonify({
        "sync_remote_url": rows.get("sync_remote_url", ""),
        "sync_cron_secret": rows.get("sync_cron_secret", ""),
    })


@app.route("/api/settings/sync", methods=["POST"])
@login_required
@admin_required
def update_sync_settings():
    data = request.get_json(force=True)
    db = get_db()
    url = _clean(data.get("sync_remote_url")).rstrip("/")
    db.execute("UPDATE settings SET value = ? WHERE key = 'sync_remote_url'", (url[:300],))
    secret = _clean(data.get("sync_cron_secret", ""))
    db.execute("UPDATE settings SET value = ? WHERE key = 'sync_cron_secret'", (secret[:200],))
    db.commit()
    return get_sync_settings()


@app.route("/api/sync/push", methods=["POST"])
@login_required
@admin_required
def push_to_remote():
    """Sube los datos de esta copia local a la app en línea (Render + Neon),
    generando un respaldo .json completo y enviándoselo a la ruta de
    importación protegida con la clave secreta. Solo disponible en modo
    SQLite (copia local) — la versión en línea no necesita 'empujar' nada.

    Requiere que estén configurados en Configuración → Sincronización:
    - sync_remote_url: URL base de la app en línea
    - sync_cron_secret: la misma clave BACKUP_CRON_SECRET de Render
    """
    if USE_POSTGRES:
        return jsonify({"error": "Esta función solo está disponible en la copia local, no en la versión en línea."}), 400

    import urllib.error
    import urllib.request
    import json as json_lib

    db = get_db()
    rows = {r["key"]: r["value"] for r in db.execute("SELECT key, value FROM settings").fetchall()}
    remote_url = rows.get("sync_remote_url", "").rstrip("/")
    cron_secret = rows.get("sync_cron_secret", "").strip()

    if not remote_url:
        return jsonify({"error": "Falta configurar la URL de tu app en línea en Configuración → Sincronización."}), 400
    if not cron_secret:
        return jsonify({"error": "Falta configurar la clave secreta (BACKUP_CRON_SECRET) en Configuración → Sincronización."}), 400

    payload = generate_backup_payload(db)

    url = f"{remote_url}/api/sync/receive-json?key={cron_secret}"
    req = urllib.request.Request(
        url,
        data=payload,
        method="POST",
        headers={"Content-Type": "application/json", "User-Agent": "ClinicaLocal/1.0"},
    )
    try:
        with urllib.request.urlopen(req, timeout=120) as resp:
            result = json_lib.loads(resp.read())
    except urllib.error.HTTPError as e:
        detail = ""
        try:
            detail = json_lib.loads(e.read()).get("error", "")
        except Exception:
            pass
        return jsonify({"error": f"La app en línea rechazó el envío ({e.code}). {detail}".strip()}), 400
    except Exception as e:
        return jsonify({"error": f"No se pudo conectar con la app en línea: {e}"}), 500

    return jsonify({"ok": True, "message": result.get("message", "Versión en línea actualizada con los datos de esta PC.")})


@app.route("/api/sync/pull-from-cloud", methods=["POST"])
@login_required
@admin_required
def pull_from_cloud():
    """Descarga el respaldo .json completo desde la app en línea (usando el
    mismo endpoint protegido con BACKUP_CRON_SECRET que usa cron-job.org) y
    lo importa directamente en esta copia local, reemplazando todos los datos
    actuales. Solo disponible en modo SQLite (copia local), no en la versión
    en línea misma.

    Requiere que estén configurados en Configuración:
    - sync_remote_url: URL base de tu app en línea (ej. https://tu-clinica-en-render.onrender.com)
    - sync_cron_secret: la misma clave que BACKUP_CRON_SECRET en Render
    """
    if USE_POSTGRES:
        return jsonify({"error": "Esta función solo está disponible en la copia local, no en la versión en línea."}), 400

    import urllib.error
    import urllib.request
    import json as json_lib
    import base64

    db = get_db()
    rows = {r["key"]: r["value"] for r in db.execute("SELECT key, value FROM settings").fetchall()}
    remote_url = rows.get("sync_remote_url", "").rstrip("/")
    cron_secret = rows.get("sync_cron_secret", "").strip()

    if not remote_url:
        return jsonify({"error": "Falta configurar la URL de tu app en línea en Configuración → Sincronización."}), 400
    if not cron_secret:
        return jsonify({"error": "Falta configurar la clave secreta (BACKUP_CRON_SECRET) en Configuración → Sincronización."}), 400

    url = f"{remote_url}/api/cron/backup-download?key={cron_secret}"
    try:
        req = urllib.request.Request(url, headers={"User-Agent": "ClinicaLocal/1.0"})
        with urllib.request.urlopen(req, timeout=60) as resp:
            raw = resp.read()
    except urllib.error.HTTPError as e:
        return jsonify({"error": f"La app en línea rechazó la petición ({e.code}). Revisa la URL y la clave secreta."}), 400
    except Exception as e:
        return jsonify({"error": f"No se pudo conectar con la app en línea: {e}"}), 500

    try:
        data = json_lib.loads(raw)
    except Exception:
        return jsonify({"error": "La respuesta de la app en línea no es un archivo .json válido."}), 500

    # Reusar la misma lógica de importación que ya tiene la app
    try:
        for table in reversed(EXPORT_TABLES):
            db.execute(f"DELETE FROM {table}")
        for table in EXPORT_TABLES:
            for row in data.get("tables", {}).get(table, []):
                cols = ", ".join(row.keys())
                placeholders = ", ".join(["?"] * len(row))
                db.execute(f"INSERT OR REPLACE INTO {table} ({cols}) VALUES ({placeholders})", list(row.values()))
        for f in data.get("files", []):
            rel_path = f["id"]
            content = base64.b64decode(f["dataBase64"])
            _save_uploaded_bytes(rel_path, content, f.get("contentType", "application/octet-stream"))
        db.commit()
    except Exception as e:
        return jsonify({"error": f"Error al importar los datos: {e}"}), 500

    return jsonify({"ok": True, "message": "¡Listo! Tu copia local ya quedó actualizada con los datos de la app en línea."})


@app.route("/api/sync/receive-json", methods=["POST"])
def receive_json_sync():
    """Recibe un respaldo .json desde la copia local y lo importa en la base
    de datos en línea (PostgreSQL/Neon). Protegido con la misma clave
    BACKUP_CRON_SECRET — no requiere sesión iniciada, solo la clave.
    Permite sincronizar local → nube con un solo clic desde la copia local.
    """
    expected = os.environ.get("BACKUP_CRON_SECRET", "").strip()
    provided = request.args.get("key") or request.headers.get("X-Backup-Key", "")
    if not expected or provided != expected:
        return jsonify({"error": "No autorizado."}), 401

    import base64
    import json as json_lib

    try:
        data = request.get_json(force=True)
    except Exception:
        return jsonify({"error": "El cuerpo de la petición no es un .json válido."}), 400

    db = get_db()
    try:
        for table in reversed(EXPORT_TABLES):
            db.execute(f"DELETE FROM {table}")
        for table in EXPORT_TABLES:
            for row in data.get("tables", {}).get(table, []):
                cols = ", ".join(row.keys())
                placeholders = ", ".join(["?"] * len(row))
                if USE_POSTGRES:
                    set_clause = ", ".join(f"{k} = EXCLUDED.{k}" for k in row.keys())
                    db.execute(
                        f"INSERT INTO {table} ({cols}) VALUES ({placeholders}) "
                        f"ON CONFLICT (id) DO UPDATE SET {set_clause}",
                        list(row.values()),
                    )
                else:
                    db.execute(
                        f"INSERT OR REPLACE INTO {table} ({cols}) VALUES ({placeholders})",
                        list(row.values()),
                    )
        for f in data.get("files", []):
            rel_path = f["id"]
            content = base64.b64decode(f["dataBase64"])
            _save_uploaded_bytes(rel_path, content, f.get("contentType", "application/octet-stream"))
        db.commit()
    except Exception as e:
        return jsonify({"error": f"Error al importar los datos: {e}"}), 500

    return jsonify({"ok": True, "message": "Versión en línea actualizada con los datos de la copia local."})


@app.route("/api/sync/receive-backup", methods=["POST"])
@login_required
@admin_required
def receive_backup():
    if USE_POSTGRES:
        return jsonify({"error": "Esta versión usa PostgreSQL — no puede recibir un respaldo de archivo SQLite."}), 400
    file = request.files.get("backup")
    if not file:
        return jsonify({"error": "No se recibió ningún archivo de respaldo."}), 400

    do_backup()

    tmp_path = os.path.join(BACKUP_DIR, "_incoming_sync.db")
    file.save(tmp_path)
    try:
        src = sqlite3.connect(tmp_path)
        dst = sqlite3.connect(DB_PATH)
        with dst:
            src.backup(dst)
        src.close()
        dst.close()
    except Exception as e:
        return jsonify({"error": f"El archivo recibido no es una base de datos válida: {e}"}), 400
    finally:
        _safe_remove(tmp_path, BACKUP_DIR)

    uploads_file = request.files.get("uploads")
    if uploads_file:
        tmp_zip = os.path.join(BACKUP_DIR, "_incoming_sync_uploads.zip")
        uploads_file.save(tmp_zip)
        try:
            with zipfile.ZipFile(tmp_zip) as zf:
                zf.extractall(UPLOAD_DIR)
        except Exception as e:
            print(f"[sync] No se pudieron aplicar los archivos subidos recibidos: {e}")
        finally:
            _safe_remove(tmp_zip, BACKUP_DIR)

    session.clear()
    return jsonify({"ok": True})


def send_email_with_attachment(to_addr, subject, body, file_bytes=None, file_name=None):
    """Envía un correo (con o sin adjunto) usando la API de Brevo (HTTPS), en
    vez de SMTP directo. Render bloquea las conexiones salientes a los
    puertos SMTP (25, 465, 587) en el plan gratuito, así que usar SMTP
    directo (Gmail, etc.) no funciona ahí. La API de Brevo viaja por HTTPS
    normal, que sí está permitido.

    Requiere la variable de entorno BREVO_API_KEY configurada en Render, y
    un remitente verificado en Brevo (Senders). El correo de "para" y el
    nombre del remitente se siguen tomando de Configuración → Correo.
    file_bytes/file_name son opcionales — si no se pasan, se manda un
    correo de solo texto (ej. recuperación de contraseña).
    """
    import base64
    import json as json_lib
    import urllib.error
    import urllib.request

    db = get_db()
    rows = {r["key"]: r["value"] for r in db.execute("SELECT key, value FROM settings").fetchall()}
    sender_email = rows.get("smtp_user", "")
    from_name = rows.get("smtp_from_name") or rows.get("clinic_name", "Clínica")

    api_key = os.environ.get("BREVO_API_KEY", "").strip()
    if not api_key:
        raise RuntimeError("Falta configurar BREVO_API_KEY en las variables de entorno de Render.")
    if not sender_email:
        raise RuntimeError("El correo de la clínica no está configurado todavía (Configuración → Correo).")

    payload = {
        "sender": {"name": from_name, "email": sender_email},
        "to": [{"email": to_addr}],
        "subject": subject,
        "textContent": body,
    }
    if file_bytes is not None:
        payload["attachment"] = [
            {
                "content": base64.b64encode(file_bytes).decode("ascii"),
                "name": file_name,
            }
        ]

    req = urllib.request.Request(
        "https://api.brevo.com/v3/smtp/email",
        data=json_lib.dumps(payload).encode("utf-8"),
        method="POST",
        headers={
            "api-key": api_key,
            "Content-Type": "application/json",
            "Accept": "application/json",
        },
    )
    try:
        with urllib.request.urlopen(req, timeout=20) as resp:
            resp.read()
    except urllib.error.HTTPError as e:
        detalle = e.read().decode("utf-8", errors="replace")
        raise RuntimeError(f"Brevo rechazó el envío ({e.code}): {detalle}")


@app.route("/api/cron/email-backup", methods=["GET", "POST"])
def cron_email_backup():
    """Dispara el respaldo por correo desde afuera (sin necesitar sesión
    iniciada) — pensado para un servicio externo de cron (ej. cron-job.org)
    que visite esta URL una vez al día. Esto también sirve para 'despertar'
    la app en el plan gratuito de Render, donde el hilo interno no es
    confiable porque el servicio se duerme sin visitas.

    Protegido con una clave secreta: hay que definir la variable de entorno
    BACKUP_CRON_SECRET en Render (Environment) y visitar:
        https://tu-app.onrender.com/api/cron/email-backup?key=TU_CLAVE
    """
    expected = os.environ.get("BACKUP_CRON_SECRET", "").strip()
    provided = request.args.get("key") or request.headers.get("X-Backup-Key", "")
    if not expected or provided != expected:
        return jsonify({"error": "No autorizado."}), 401
    ok, detalle = do_email_backup()
    if ok:
        return jsonify({"ok": True, "message": "Respaldo enviado por correo."})
    return jsonify({"ok": False, "message": f"No se pudo enviar: {detalle}"}), 500


@app.route("/api/cron/backup-download", methods=["GET"])
def cron_backup_download():
    """Descarga el respaldo .json completo, protegido con la misma clave
    BACKUP_CRON_SECRET (sin necesitar sesión iniciada) — pensado para que
    un script en tu computadora (ej. una tarea programada de Windows) lo
    descargue automáticamente a una hora fija, guardándolo directo en tu
    PC sin que tengas que entrar a la app ni hacer nada manual.

        https://tu-app.onrender.com/api/cron/backup-download?key=TU_CLAVE
    """
    expected = os.environ.get("BACKUP_CRON_SECRET", "").strip()
    provided = request.args.get("key") or request.headers.get("X-Backup-Key", "")
    if not expected or provided != expected:
        return jsonify({"error": "No autorizado."}), 401
    db = get_db()
    payload = generate_backup_payload(db)
    stamp = datetime.now().strftime("%Y-%m-%d_%H-%M-%S")
    return Response(
        payload,
        mimetype="application/json",
        headers={"Content-Disposition": f'attachment; filename="respaldo_completo_{stamp}.json"'},
    )


def do_email_backup():
    """Genera el respaldo completo y lo envía por correo a la cuenta de la
    clínica configurada en Configuración → Correo. A diferencia de
    do_backup() (que solo funciona con SQLite local), esto sí funciona con
    PostgreSQL/Neon — por eso es el respaldo que corre en Render."""
    with app.app_context():
        try:
            db = get_db()
            rows = {r["key"]: r["value"] for r in db.execute("SELECT key, value FROM settings").fetchall()}
            to_addr = rows.get("smtp_user", "")
            if not to_addr:
                print("[respaldo por correo] Correo no configurado todavía, se omite el envío de hoy.", flush=True)
                return False, "No hay un correo de la clínica configurado en Configuración → Correo."
            payload = generate_backup_payload(db)
            clinic = rows.get("clinic_name") or "Clínica"
            stamp = datetime.now().strftime("%Y-%m-%d_%H-%M-%S")

            # Brevo no acepta .json como adjunto (solo permite un listado fijo
            # de extensiones: pdf, txt, zip, docx, csv, etc.), así que lo
            # comprimimos en un .zip — adentro sigue siendo el mismo .json.
            import io

            json_name = f"respaldo_completo_{stamp}.json"
            zip_buf = io.BytesIO()
            with zipfile.ZipFile(zip_buf, "w", zipfile.ZIP_DEFLATED) as zf:
                zf.writestr(json_name, payload)
            fname = f"respaldo_completo_{stamp}.zip"

            send_email_with_attachment(
                to_addr,
                f"Respaldo diario - {clinic} - {datetime.now().strftime('%d/%m/%Y')}",
                "Adjunto el respaldo automático diario de la base de datos de la clínica "
                "(pacientes, citas, cargos, presupuestos y documentos), comprimido en .zip. "
                "Descomprímelo para obtener el archivo .json — es el mismo que usas para "
                "Importar respaldo. Guarda este correo o el archivo en un lugar seguro "
                "fuera de Render y Neon.",
                zip_buf.getvalue(),
                fname,
            )
            print(f"[respaldo por correo] Enviado correctamente: {fname}", flush=True)
            return True, ""
        except Exception as e:
            print(f"[respaldo por correo] Error al enviar: {e}", flush=True)
            return False, str(e)
        finally:
            close_db()


def _email_backup_scheduler_loop():
    while True:
        target_hour, target_min, enabled = 21, 0, True
        try:
            with app.app_context():
                db = get_db()
                rows = {r["key"]: r["value"] for r in db.execute("SELECT key, value FROM settings").fetchall()}
                close_db()
            enabled = rows.get("backup_enabled", "1") == "1"
            hh, mm = (rows.get("backup_hour", "21:00").split(":") + ["0", "0"])[:2]
            target_hour, target_min = int(hh), int(mm)
        except Exception:
            pass
        now = datetime.now()
        target = now.replace(hour=target_hour, minute=target_min, second=0, microsecond=0)
        if target <= now:
            target = target + timedelta(days=1)
        sleep_seconds = (target - now).total_seconds()
        time.sleep(min(sleep_seconds, 3600))
        if datetime.now() >= target and enabled:
            do_email_backup()


_email_backup_thread_started = False


def start_email_backup_scheduler():
    """Inicia el hilo que manda el respaldo por correo una vez al día, a la
    misma hora configurada en Configuración → Copias de seguridad. Funciona
    igual con SQLite o con PostgreSQL/Neon."""
    global _email_backup_thread_started
    if _email_backup_thread_started:
        return
    _email_backup_thread_started = True
    t = threading.Thread(target=_email_backup_scheduler_loop, daemon=True)
    t.start()


@app.route("/api/documents/<did>/send-email", methods=["POST"])
@login_required
def send_document_email(did):
    data = request.get_json(force=True)
    to_addr = _clean(data.get("to"))
    if not to_addr or "@" not in to_addr:
        return jsonify({"error": "Escribe un correo válido."}), 400

    db = get_db()
    row = db.execute("SELECT * FROM documents WHERE id = ?", (did,)).fetchone()
    if not row:
        return jsonify({"error": "Documento no encontrado."}), 404
    patient = db.execute("SELECT * FROM patients WHERE id = ?", (row["patient_id"],)).fetchone()
    clinic_name = _get_setting_value("clinic_name", "Clínica")

    file_bytes = _read_uploaded_file(f"documents/{row['filename']}")
    if file_bytes is None:
        return jsonify({"error": "El archivo ya no está disponible en el servidor."}), 404

    subject = data.get("subject") or f"Tu examen de {clinic_name}"
    body = data.get("message") or (
        f"Hola {patient['name'] if patient else ''},\n\n"
        f"Te compartimos tu documento ({row['original_name']}) de {clinic_name}.\n\n"
        f"Saludos."
    )
    try:
        send_email_with_attachment(to_addr, subject, body, file_bytes, row["original_name"])
    except Exception as e:
        return jsonify({"error": str(e)}), 500
    return jsonify({"ok": True})


@app.route("/api/settings/logo", methods=["POST"])
@login_required
@admin_required
def upload_logo():
    file = request.files.get("logo")
    if not file or file.filename == "":
        return jsonify({"error": "No se recibió ninguna imagen."}), 400
    if not _ext_ok(file.filename, IMAGE_EXTS):
        return jsonify({"error": "Formato no permitido. Usa JPG, JPEG, PNG, WEBP, GIF, BMP o TIFF."}), 400

    db = get_db()
    old = db.execute("SELECT value FROM settings WHERE key = 'logo_path'").fetchone()
    if old and old["value"]:
        _delete_uploaded_file(old["value"])

    ext = secure_filename(file.filename).rsplit(".", 1)[1].lower()
    rel_path = f"branding/logo_{uuid.uuid4().hex[:8]}.{ext}"
    _save_uploaded_file(rel_path, file)
    db.execute("UPDATE settings SET value = ? WHERE key = 'logo_path'", (rel_path,))
    db.commit()
    return get_settings()


@app.route("/api/settings/logo", methods=["DELETE"])
@login_required
@admin_required
def delete_logo():
    db = get_db()
    old = db.execute("SELECT value FROM settings WHERE key = 'logo_path'").fetchone()
    if old and old["value"]:
        _delete_uploaded_file(old["value"])
    db.execute("UPDATE settings SET value = '' WHERE key = 'logo_path'")
    db.commit()
    return get_settings()


def backup_to_dict(filename):
    path = os.path.join(BACKUP_DIR, filename)
    stamp = filename[len("clinica_"):-len(".db")]
    zpath = os.path.join(BACKUP_DIR, f"uploads_{stamp}.zip")
    size = os.path.getsize(path)
    if os.path.exists(zpath):
        size += os.path.getsize(zpath)
    checksum_path = path + ".sha256"
    checksum = ""
    if os.path.exists(checksum_path):
        try:
            checksum = open(checksum_path, "r", encoding="utf-8").read().strip().split()[0]
        except Exception:
            checksum = ""
    return {
        "filename": filename,
        "createdAt": datetime.fromtimestamp(os.path.getmtime(path)).strftime("%Y-%m-%d %H:%M:%S"),
        "sizeKb": round(size / 1024, 1),
        "hasUploads": os.path.exists(zpath),
        "hasChecksum": bool(checksum),
        "checksum": checksum[:16],
    }


@app.route("/api/backups", methods=["GET"])
@login_required
@admin_required
def list_backups():
    if USE_POSTGRES:
        return jsonify([])
    files = sorted(
        [f for f in os.listdir(BACKUP_DIR) if f.startswith("clinica_") and f.endswith(".db")],
        reverse=True,
    )
    return jsonify([backup_to_dict(f) for f in files])


@app.route("/api/backups", methods=["POST"])
@login_required
@admin_required
def create_backup_now():
    if USE_POSTGRES:
        return jsonify({"error": "Esta versión usa PostgreSQL — los respaldos de archivo no aplican. Usa el sistema de respaldos de tu proveedor de base de datos."}), 400
    path = do_backup()
    if not path:
        return jsonify({"error": "No se pudo crear el respaldo."}), 500
    return list_backups()


@app.route("/api/backups/<path:filename>/download", methods=["GET"])
@login_required
@admin_required
def download_backup(filename):
    safe = secure_filename(filename)
    if safe != filename or not os.path.exists(os.path.join(BACKUP_DIR, safe)):
        return jsonify({"error": "Respaldo no encontrado."}), 404
    _log_activity("backup_download", safe, "Respaldo descargado")
    return send_from_directory(BACKUP_DIR, safe, as_attachment=True)


@app.route("/api/backups/<path:filename>/verify", methods=["POST"])
@login_required
@admin_required
def verify_backup(filename):
    safe = secure_filename(filename)
    path = os.path.join(BACKUP_DIR, safe)
    if safe != filename or not os.path.exists(path):
        return jsonify({"error": "Respaldo no encontrado."}), 404
    result = _verify_sqlite_backup(path)
    if result.get("hashOk") is None and result.get("sqliteOk"):
        try:
            _write_backup_checksum(path)
            result["hashOk"] = True
        except Exception:
            pass
    _log_activity("backup_verify", safe, result.get("message", "Verificación ejecutada"))
    return jsonify(result)


@app.route("/api/activity", methods=["GET"])
@login_required
@admin_required
def list_activity():
    db = get_db()
    rows = db.execute(
        "SELECT id, username, action, target, details, created_at FROM activity_log ORDER BY created_at DESC LIMIT 100"
    ).fetchall()
    return jsonify([dict(r) for r in rows])


# Todas las tablas que se incluyen en la exportación completa, en el orden
# correcto para poder reconstruirlas después (padres antes que hijos).
EXPORT_TABLES = [
    "users", "patients", "appointments", "documents", "charges", "settings",
    "prescriptions", "budgets", "budget_items", "budget_payments", "clinical_records",
]


def generate_backup_payload(db):
    """Genera los bytes del respaldo completo (pacientes, citas, cargos,
    presupuestos, usuarios y — en PostgreSQL/Neon — los archivos adjuntos
    en base64). Usado tanto por la descarga manual (/api/backups/export)
    como por el envío automático diario por correo."""
    import base64
    import json as json_lib

    data = {"exported_at": _now(), "tables": {}}
    for table in EXPORT_TABLES:
        rows = db.execute(f"SELECT * FROM {table}").fetchall()
        data["tables"][table] = [dict(r) for r in rows]

    if USE_POSTGRES:
        blobs = db.execute("SELECT id, data, content_type, created_at FROM file_blobs").fetchall()
        data["files"] = [
            {
                "id": b["id"],
                "contentType": b["content_type"],
                "createdAt": b["created_at"],
                "dataBase64": base64.b64encode(bytes(b["data"])).decode("ascii"),
            }
            for b in blobs
        ]
    else:
        # En SQLite local, los archivos viven como archivos sueltos dentro
        # de uploads/. Los incluimos igual en el respaldo (leyéndolos de
        # disco) para que este mismo .json sirva para restaurar fotos y
        # documentos también al importarlo en la versión en línea
        # (PostgreSQL/Neon), y no solo al revés.
        data["files"] = []
        for root, _dirs, filenames in os.walk(UPLOAD_DIR):
            for fname in filenames:
                full_path = os.path.join(root, fname)
                rel_path = os.path.relpath(full_path, UPLOAD_DIR).replace(os.sep, "/")
                try:
                    with open(full_path, "rb") as fh:
                        raw = fh.read()
                except OSError:
                    continue
                ext = fname.rsplit(".", 1)[-1].lower() if "." in fname else ""
                content_type = {
                    "jpg": "image/jpeg", "jpeg": "image/jpeg", "png": "image/png",
                    "webp": "image/webp", "gif": "image/gif", "pdf": "application/pdf",
                }.get(ext, "application/octet-stream")
                data["files"].append({
                    "id": rel_path,
                    "contentType": content_type,
                    "createdAt": datetime.fromtimestamp(os.path.getmtime(full_path)).strftime("%Y-%m-%dT%H:%M:%S"),
                    "dataBase64": base64.b64encode(raw).decode("ascii"),
                })

    return json_lib.dumps(data, default=str).encode("utf-8")


@app.route("/api/backups/export", methods=["GET"])
@login_required
@admin_required
def export_full_backup():
    """Descarga toda la información de la clínica en un solo archivo JSON,
    incluyendo las fotos/documentos guardados en la base de datos (en base64).
    Funciona igual con SQLite o PostgreSQL — pensado especialmente para cuando
    se usa PostgreSQL (Render, Neon, etc.), donde no hay un solo archivo de
    base de datos que descargar directamente como en SQLite. Guarda este
    archivo periódicamente fuera de tu hosting (en tu PC, un USB, Drive) como
    respaldo real e independiente de cualquier proveedor."""
    db = get_db()
    payload = generate_backup_payload(db)
    stamp = datetime.now().strftime("%Y-%m-%d_%H-%M-%S")
    return Response(
        payload,
        mimetype="application/json",
        headers={"Content-Disposition": f'attachment; filename="respaldo_completo_{stamp}.json"'},
    )


@app.route("/api/backups/import", methods=["POST"])
@login_required
@admin_required
def import_full_backup():
    """Restaura toda la información desde un archivo generado por
    /api/backups/export. Reemplaza TODO lo que haya actualmente."""
    import base64
    import json as json_lib

    file = request.files.get("file")
    if not file:
        return jsonify({"error": "No se recibió ningún archivo."}), 400
    try:
        data = json_lib.loads(file.read().decode("utf-8"))
    except Exception as e:
        return jsonify({"error": f"El archivo no es un respaldo válido: {e}"}), 400

    db = get_db()
    for table in reversed(EXPORT_TABLES):
        db.execute(f"DELETE FROM {table}")
    if USE_POSTGRES:
        db.execute("DELETE FROM file_blobs")

    for table in EXPORT_TABLES:
        for row in data.get("tables", {}).get(table, []):
            cols = list(row.keys())
            cols_sql = ", ".join(cols)
            placeholders = ", ".join(["?"] * len(cols))
            db.execute(f"INSERT INTO {table} ({cols_sql}) VALUES ({placeholders})", tuple(row[c] for c in cols))

    # Restaura los archivos (fotos, documentos) sin importar si el respaldo
    # viene de PostgreSQL/Neon y se importa en SQLite local, o al revés —
    # _save_uploaded_bytes ya sabe guardar en el lugar correcto según
    # dónde se esté ejecutando esta importación.
    for f in data.get("files", []):
        rel_path = f["id"]
        content = base64.b64decode(f["dataBase64"])
        _save_uploaded_bytes(rel_path, content, f.get("contentType", "application/octet-stream"))

    db.commit()
    session.clear()
    return jsonify({"ok": True, "message": "Respaldo restaurado. Vuelve a iniciar sesión."})


@app.route("/api/backups/<path:filename>", methods=["DELETE"])
@login_required
@admin_required
def delete_backup(filename):
    safe = secure_filename(filename)
    target = os.path.join(BACKUP_DIR, safe)
    if not os.path.exists(target):
        return jsonify({"error": "Respaldo no encontrado."}), 404
    if not _safe_remove(target, BACKUP_DIR):
        return jsonify({"error": "No se pudo eliminar el archivo (puede estar en uso)."}), 500
    if safe.startswith("clinica_") and safe.endswith(".db"):
        stamp = safe[len("clinica_"):-len(".db")]
        _safe_remove(os.path.join(BACKUP_DIR, f"uploads_{stamp}.zip"), BACKUP_DIR)
        _safe_remove(os.path.join(BACKUP_DIR, safe + ".sha256"), BACKUP_DIR)
    _log_activity("backup_delete", safe, "Respaldo eliminado")
    return jsonify({"ok": True})


@app.route("/api/backups/sync-folder", methods=["POST"])
@login_required
@admin_required
def sync_backup_folder_now():
    if USE_POSTGRES:
        return jsonify({"error": "Esta versión usa PostgreSQL — no aplica."}), 400
    folder = _get_setting_value("backup_folder", "")
    if not folder:
        return jsonify({"error": "Primero configura una carpeta de sincronización."}), 400
    if not os.path.isdir(folder):
        return jsonify({"error": f"La carpeta no existe en esta computadora: {folder}"}), 400
    files = sorted(
        [f for f in os.listdir(BACKUP_DIR) if f.startswith("clinica_") and f.endswith(".db")],
        reverse=True,
    )
    if not files:
        return jsonify({"error": "Todavía no hay ningún respaldo que copiar."}), 400
    latest = files[0]
    stamp = latest[len("clinica_"):-len(".db")]
    zpath = os.path.join(BACKUP_DIR, f"uploads_{stamp}.zip")
    paths = [os.path.join(BACKUP_DIR, latest)] + ([zpath] if os.path.exists(zpath) else [])
    ok = _sync_to_folder(paths)
    if not ok:
        return jsonify({"error": "No se pudo copiar a la carpeta. Revisa que la ruta sea correcta y tengas permiso de escritura."}), 500
    return jsonify({"ok": True, "folder": folder, "file": latest})


@app.route("/api/backups/<path:filename>/restore", methods=["POST"])
@login_required
@admin_required
def restore_backup(filename):
    if USE_POSTGRES:
        return jsonify({"error": "Esta versión usa PostgreSQL — no aplica restaurar un respaldo de archivo SQLite."}), 400
    safe = secure_filename(filename)
    backup_path = os.path.join(BACKUP_DIR, safe)
    if not os.path.exists(backup_path):
        return jsonify({"error": "Respaldo no encontrado."}), 404

    data = request.get_json(silent=True) or {}
    restore_uploads = bool(data.get("restoreUploads"))

    do_backup()

    try:
        src = sqlite3.connect(backup_path)
        dst = sqlite3.connect(DB_PATH)
        with dst:
            src.backup(dst)
        src.close()
        dst.close()
    except Exception as e:
        return jsonify({"error": f"No se pudo restaurar la base de datos: {e}"}), 500

    if restore_uploads and safe.startswith("clinica_") and safe.endswith(".db"):
        stamp = safe[len("clinica_"):-len(".db")]
        zpath = os.path.join(BACKUP_DIR, f"uploads_{stamp}.zip")
        if os.path.exists(zpath):
            try:
                with zipfile.ZipFile(zpath) as zf:
                    zf.extractall(UPLOAD_DIR)
            except Exception as e:
                print(f"[respaldo] No se pudieron restaurar los archivos subidos: {e}")

    session.clear()
    return jsonify({"ok": True, "message": "Respaldo restaurado. Vuelve a iniciar sesión."})


@app.route("/api/patients/<pid>/prescription", methods=["GET"])
@login_required
def get_prescription(pid):
    db = get_db()
    row = db.execute(
        "SELECT medications, instructions, updated_at FROM prescriptions WHERE patient_id = ?", (pid,)
    ).fetchone()
    return jsonify(dict(row) if row else {"medications": "", "instructions": "", "updated_at": ""})


@app.route("/api/patients/<pid>/prescription", methods=["POST"])
@login_required
def save_prescription(pid):
    data = request.get_json(force=True)
    medications = _clean(data.get("medications"))
    instructions = _clean(data.get("instructions"))
    db = get_db()
    if not db.execute("SELECT id FROM patients WHERE id = ?", (pid,)).fetchone():
        return jsonify({"error": "Paciente no encontrado."}), 404
    now = _now()
    db.execute(
        """INSERT INTO prescriptions (patient_id, medications, instructions, updated_at)
           VALUES (?, ?, ?, ?)
           ON CONFLICT(patient_id) DO UPDATE SET
             medications = excluded.medications, instructions = excluded.instructions, updated_at = excluded.updated_at""",
        (pid, medications, instructions, now),
    )
    db.commit()
    return jsonify({"medications": medications, "instructions": instructions, "updated_at": now})


# ---------------------------------------------------------------------------
# VENTANA INDEPENDIENTE DE IMPRESIÓN
# ---------------------------------------------------------------------------
def _cleanup_print_jobs():
    now = time.time()
    with _PRINT_JOBS_LOCK:
        for token, job in list(_PRINT_JOBS.items()):
            if now - job.get('created', now) > _PRINT_JOB_TTL:
                _PRINT_JOBS.pop(token, None)

@app.route('/api/print-jobs', methods=['POST'])
@login_required
def create_print_job():
    _cleanup_print_jobs()
    data = request.get_json(force=True) or {}
    title = _clean(data.get('title'))[:200] or 'Documento clínico'
    content = data.get('content') or ''
    settings = {}
    try:
        sdb = get_db()
        settings_rows = sdb.execute("SELECT key, value FROM settings").fetchall()
        settings = {r["key"]: r["value"] for r in settings_rows}
    except Exception:
        settings = {}
    clinic = _clean(settings.get('clinic_name') or data.get('clinic'))[:200] or 'Clínica Médica'
    logo = data.get('logo') or (("/uploads/" + settings.get('logo_path')) if settings.get('logo_path') else '')
    disclaimer = _clean(data.get('disclaimer') if data.get('disclaimer') is not None else settings.get('disclaimer_text'))[:1500]
    token = uuid.uuid4().hex
    with _PRINT_JOBS_LOCK:
        _PRINT_JOBS[token] = {
            'created': time.time(), 'title': title, 'content': content, 'clinic': clinic, 'logo': logo, 'disclaimer': disclaimer,
            'doctor_name': settings.get('doctor_name',''), 'doctor_specialty': settings.get('doctor_specialty',''),
            'doctor_registration': settings.get('doctor_registration',''), 'doctor_phone': settings.get('doctor_phone',''),
            'doctor_email': settings.get('doctor_email',''), 'clinic_address': settings.get('clinic_address',''),
            'document_header': settings.get('document_header',''), 'document_footer': settings.get('document_footer',''),
            'show_doctor_on_documents': settings.get('show_doctor_on_documents','1') != '0',
            'show_clinic_on_documents': settings.get('show_clinic_on_documents','1') != '0',
        }
    return jsonify({'token': token, 'url': url_for('print_job_view', token=token)})

@app.route('/print-window/<token>')
def print_job_view(token):
    _cleanup_print_jobs()
    with _PRINT_JOBS_LOCK:
        job = _PRINT_JOBS.get(token)
    if not job:
        abort(404)
    logo_html = ''
    if job.get('logo'):
        logo_html = f'<img src="{job["logo"]}" class="doc-logo" alt="Logo">'
    disclaimer = job.get('disclaimer') or ''
    doctor_name = job.get('doctor_name') or ''
    doctor_specialty = job.get('doctor_specialty') or ''
    doctor_registration = job.get('doctor_registration') or ''
    doctor_phone = job.get('doctor_phone') or ''
    doctor_email = job.get('doctor_email') or ''
    clinic_address = job.get('clinic_address') or ''
    document_header = job.get('document_header') or ''
    document_footer = job.get('document_footer') or ''
    doctor_line = ' · '.join([x for x in [doctor_name, doctor_specialty, ('Reg. '+doctor_registration) if doctor_registration else '', doctor_phone, doctor_email] if x])
    clinic_line = clinic_address
    return render_template_string('''<!doctype html>
<html lang="es"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>{{ title }}</title>
<style>
*{box-sizing:border-box}body{margin:0;background:#eef3f8;color:#17212b;font-family:Inter,Segoe UI,Arial,sans-serif}.toolbar{position:sticky;top:0;z-index:5;display:flex;align-items:center;justify-content:space-between;gap:10px;padding:12px 16px;background:#fff;border-bottom:1px solid #dce4ec;box-shadow:0 3px 12px rgba(15,23,42,.08)}.toolbar strong{font-size:14px}.actions{display:flex;gap:8px}.btn{border:0;border-radius:999px;padding:9px 15px;font-weight:700;cursor:pointer}.primary{background:#2563eb;color:#fff}.ghost{background:#f1f5f9;color:#17212b}.close{width:38px;height:38px;padding:0;font-size:24px;line-height:38px}.sheet{max-width:850px;margin:24px auto;padding:38px 42px;background:#fff;min-height:1100px;box-shadow:0 12px 35px rgba(15,23,42,.10);border-radius:16px}.head{text-align:center;border-bottom:2px solid #2563eb;padding-bottom:16px;margin-bottom:22px}.doc-logo{width:54px;height:54px;object-fit:contain;display:block;margin:0 auto 8px}.head h1{margin:0;font-size:22px}.kind{margin-top:6px;color:#2563eb;font-weight:800}.disclaimer{margin-top:28px;padding:10px 12px;border:1px solid #dbe3ec;border-radius:10px;font-size:10px;color:#64748b}.foot{margin-top:28px;padding-top:12px;border-top:1px solid #dbe3ec;font-size:10px;color:#64748b;display:flex;justify-content:space-between;gap:15px}.content p{line-height:1.5}.content table{width:100%;border-collapse:collapse}.content th,.content td{border:1px solid #dbe3ec;padding:7px;text-align:left}.content .summary{border:1px solid #dbe3ec;border-radius:10px;padding:12px;margin:10px 0}.content .meta{display:grid;grid-template-columns:repeat(auto-fit,minmax(170px,1fr));gap:10px;margin:10px 0}.content .meta>div{border:1px solid #dbe3ec;border-radius:9px;padding:9px}.content .meta strong{display:block;font-size:10px;text-transform:uppercase;color:#64748b;margin-bottom:3px}@media print{body{background:#fff}.toolbar{display:none}.sheet{box-shadow:none;border-radius:0;margin:0;max-width:none;padding:0;min-height:0}.disclaimer{page-break-inside:avoid}.foot{page-break-inside:avoid}}@media(max-width:700px){.sheet{margin:0;padding:24px 18px;border-radius:0}.toolbar{position:relative}}
</style></head><body>
<div class="toolbar"><strong>Vista previa de impresión</strong><div class="actions"><button class="btn primary" onclick="window.print()">Imprimir</button><button class="btn ghost close" onclick="window.close()" aria-label="Cerrar">×</button></div></div>
<main class="sheet"><header class="head">{{ logo_html|safe }}{% if show_clinic_on_documents %}<h1>{{ clinic }}</h1>{% endif %}{% if clinic_line %}<div style="color:#64748b;font-size:11px;margin-top:3px">{{ clinic_line }}</div>{% endif %}{% if show_doctor_on_documents and doctor_line %}<div style="margin-top:5px;font-size:12px;font-weight:700;color:#17212b">{{ doctor_line }}</div>{% endif %}{% if document_header %}<div style="margin-top:5px;font-size:11px;color:#64748b">{{ document_header }}</div>{% endif %}<div class="kind">{{ title }}</div></header><section class="content">{{ content|safe }}</section>{% if disclaimer %}<div class="disclaimer">{{ disclaimer }}</div>{% endif %}<footer class="foot"><span>{% if document_footer %}{{ document_footer }} · {% endif %}Documento generado el {{ generated }}</span><span>Firma / sello</span></footer></main>
</body></html>''', title=job['title'], clinic=job['clinic'], logo_html=logo_html, content=job['content'], disclaimer=disclaimer, doctor_line=doctor_line, clinic_line=clinic_line, document_header=document_header, document_footer=document_footer, show_doctor_on_documents=job.get('show_doctor_on_documents', True), show_clinic_on_documents=job.get('show_clinic_on_documents', True), generated=datetime.now().strftime('%d/%m/%Y %H:%M'))

# ---------------------------------------------------------------------------
# HISTORIA CLÍNICA MÉDICA
# ---------------------------------------------------------------------------

def _require_patient_access(pid):
    """Verifica que el paciente exista; si no, corta la petición con un
    error 404 en formato JSON (igual que el resto de rutas de la app)."""
    db = get_db()
    patient = db.execute("SELECT id FROM patients WHERE id = ?", (pid,)).fetchone()
    if not patient:
        abort(make_response(jsonify({"error": "Paciente no encontrado."}), 404))


@app.route("/api/patients/<pid>/historia-clinica", methods=["GET"])
@login_required
def get_historia_clinica(pid):
    _require_patient_access(pid)
    db = get_db()
    rows = db.execute(
        """SELECT id, patient_id, fecha, especialidad, motivo,
                  presion, temperatura, pulso, peso, talla, saturacion,
                  examen, diagnostico, medicamentos, indicaciones, seguimiento,
                  tratamiento, costo, especialidad_datos, created_at
           FROM clinical_records WHERE patient_id = ?
           ORDER BY fecha DESC""",
        (pid,)
    ).fetchall()
    return jsonify([dict(r) for r in rows])


@app.route("/api/patients/<pid>/historia-clinica", methods=["POST"])
@login_required
def save_historia_clinica(pid):
    _require_patient_access(pid)
    data = request.get_json(force=True)
    db = get_db()
    rid = str(uuid.uuid4())
    now = _now()
    db.execute(
        """INSERT INTO clinical_records
           (id, patient_id, fecha, especialidad, motivo,
            presion, temperatura, pulso, peso, talla, saturacion,
            examen, diagnostico, medicamentos, indicaciones, seguimiento,
            tratamiento, costo, especialidad_datos, created_at)
           VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
        (
            rid, pid, now[:10],
            _clean(data.get("especialidad"))[:100],
            _clean(data.get("motivo"))[:1000],
            _clean(data.get("presion"))[:50],
            _clean(data.get("temperatura"))[:50],
            _clean(data.get("pulso"))[:50],
            _clean(data.get("peso"))[:50],
            _clean(data.get("talla"))[:50],
            _clean(data.get("saturacion"))[:50],
            _clean(data.get("examen"))[:3000],
            _clean(data.get("diagnostico"))[:1000],
            _clean(data.get("medicamentos"))[:3000],
            _clean(data.get("indicaciones"))[:1000],
            _clean(data.get("seguimiento"))[:500],
            _clean(data.get("tratamiento"))[:300],
            round(float(data.get("costo") or 0), 2),
            _clean(data.get("especialidad_datos") or "{}")[:12000],
            now,
        )
    )
    db.commit()
    row = db.execute("SELECT * FROM clinical_records WHERE id = ?", (rid,)).fetchone()
    return jsonify(dict(row)), 201


@app.route("/api/patients/<pid>/historia-clinica/<rid>", methods=["PUT"])
@login_required
def update_historia_clinica(pid, rid):
    """Edita una consulta ya guardada. Solo actualiza el registro clínico
    en sí — NO vuelve a crear un presupuesto automático (eso ya pasó al
    guardar la consulta por primera vez)."""
    _require_patient_access(pid)
    db = get_db()
    existing = db.execute(
        "SELECT id FROM clinical_records WHERE id = ? AND patient_id = ?", (rid, pid)
    ).fetchone()
    if not existing:
        return jsonify({"error": "Consulta no encontrada."}), 404

    data = request.get_json(force=True)
    db.execute(
        """UPDATE clinical_records SET
             especialidad=?, motivo=?, presion=?, temperatura=?, pulso=?,
             peso=?, talla=?, saturacion=?, examen=?, diagnostico=?,
             medicamentos=?, indicaciones=?, seguimiento=?, tratamiento=?, costo=?, especialidad_datos=?
           WHERE id = ? AND patient_id = ?""",
        (
            _clean(data.get("especialidad"))[:100],
            _clean(data.get("motivo"))[:1000],
            _clean(data.get("presion"))[:50],
            _clean(data.get("temperatura"))[:50],
            _clean(data.get("pulso"))[:50],
            _clean(data.get("peso"))[:50],
            _clean(data.get("talla"))[:50],
            _clean(data.get("saturacion"))[:50],
            _clean(data.get("examen"))[:3000],
            _clean(data.get("diagnostico"))[:1000],
            _clean(data.get("medicamentos"))[:3000],
            _clean(data.get("indicaciones"))[:1000],
            _clean(data.get("seguimiento"))[:500],
            _clean(data.get("tratamiento"))[:300],
            round(float(data.get("costo") or 0), 2),
            _clean(data.get("especialidad_datos") or "{}")[:12000],
            rid, pid,
        )
    )
    db.commit()
    row = db.execute("SELECT * FROM clinical_records WHERE id = ?", (rid,)).fetchone()
    return jsonify(dict(row))


@app.route("/api/patients/<pid>/historia-clinica/<rid>", methods=["DELETE"])
@login_required
@admin_required
def delete_historia_clinica(pid, rid):
    _require_patient_access(pid)
    db = get_db()
    db.execute("DELETE FROM clinical_records WHERE id = ? AND patient_id = ?", (rid, pid))
    db.commit()
    return jsonify({"ok": True})


BUDGET_STATUSES = {"pendiente", "aprobado", "rechazado"}


def budget_to_dict(row, items):
    d = dict(row)
    d["items"] = [{"id": i["id"], "description": i["description"], "cost": i["cost"]} for i in items]
    d["total"] = round(sum(float(i["cost"] or 0) for i in items), 2)
    db = get_db()
    paid_row = db.execute("SELECT COALESCE(SUM(amount),0) AS paid FROM budget_payments WHERE budget_id = ?", (row["id"],)).fetchone()
    d["paid"] = round(float(paid_row["paid"] or 0), 2)
    d["balance"] = round(max(0, d["total"] - d["paid"]), 2)
    d["paymentStatus"] = "pagado" if d["balance"] <= 0 and d["total"] > 0 else ("parcial" if d["paid"] > 0 else "pendiente")
    return d


@app.route("/api/budgets", methods=["GET"])
@login_required
def list_budgets():
    db = get_db()
    pid = request.args.get("patient_id")
    if pid:
        rows = db.execute(
            "SELECT * FROM budgets WHERE patient_id = ? ORDER BY created_at DESC", (pid,)
        ).fetchall()
    else:
        rows = db.execute("SELECT * FROM budgets ORDER BY created_at DESC").fetchall()
    result = []
    for r in rows:
        items = db.execute("SELECT * FROM budget_items WHERE budget_id = ?", (r["id"],)).fetchall()
        result.append(budget_to_dict(r, items))
    return jsonify(result)


@app.route("/api/budgets", methods=["POST"])
@login_required
def create_budget():
    data = request.get_json(force=True)
    pid = data.get("patient_id")
    title = _clean(data.get("title"))
    items = data.get("items") or []
    if not pid or not title:
        return jsonify({"error": "Paciente y título son obligatorios."}), 400

    db = get_db()
    if not db.execute("SELECT id FROM patients WHERE id = ?", (pid,)).fetchone():
        return jsonify({"error": "Paciente no encontrado."}), 404

    bid = uuid.uuid4().hex
    now = _now()
    db.execute(
        """INSERT INTO budgets (id, patient_id, title, status, notes, created_at, updated_at)
           VALUES (?, ?, ?, 'pendiente', ?, ?, ?)""",
        (bid, pid, title[:120], _clean(data.get("notes")), now, now),
    )
    for it in items:
        desc = _clean(it.get("description"))
        try:
            cost = float(it.get("cost") or 0)
        except (TypeError, ValueError):
            cost = 0
        if desc:
            db.execute(
                "INSERT INTO budget_items (id, budget_id, description, cost) VALUES (?, ?, ?, ?)",
                (uuid.uuid4().hex, bid, desc[:200], cost),
            )
    db.commit()
    row = db.execute("SELECT * FROM budgets WHERE id = ?", (bid,)).fetchone()
    items_rows = db.execute("SELECT * FROM budget_items WHERE budget_id = ?", (bid,)).fetchall()
    return jsonify(budget_to_dict(row, items_rows)), 201


@app.route("/api/budgets/<bid>", methods=["PUT"])
@login_required
def update_budget(bid):
    data = request.get_json(force=True)
    db = get_db()
    row = db.execute("SELECT * FROM budgets WHERE id = ?", (bid,)).fetchone()
    if not row:
        return jsonify({"error": "Presupuesto no encontrado."}), 404

    title = _clean(data.get("title")) or row["title"]
    status = data.get("status") or row["status"]
    if status not in BUDGET_STATUSES:
        return jsonify({"error": "Estado inválido."}), 400
    notes = data.get("notes", row["notes"])

    db.execute(
        "UPDATE budgets SET title = ?, status = ?, notes = ?, updated_at = ? WHERE id = ?",
        (title[:120], status, _clean(notes), _now(), bid),
    )

    if data.get("items") is not None:
        db.execute("DELETE FROM budget_items WHERE budget_id = ?", (bid,))
        for it in data["items"]:
            desc = _clean(it.get("description"))
            try:
                cost = float(it.get("cost") or 0)
            except (TypeError, ValueError):
                cost = 0
            if desc:
                db.execute(
                    "INSERT INTO budget_items (id, budget_id, description, cost) VALUES (?, ?, ?, ?)",
                    (uuid.uuid4().hex, bid, desc[:200], cost),
                )
    db.commit()
    row = db.execute("SELECT * FROM budgets WHERE id = ?", (bid,)).fetchone()
    items_rows = db.execute("SELECT * FROM budget_items WHERE budget_id = ?", (bid,)).fetchall()
    return jsonify(budget_to_dict(row, items_rows))


@app.route("/api/budgets/<bid>/payments", methods=["GET"])
@login_required
def list_budget_payments(bid):
    db = get_db()
    if not db.execute("SELECT id FROM budgets WHERE id = ?", (bid,)).fetchone():
        return jsonify({"error": "Presupuesto no encontrado."}), 404
    rows = db.execute("SELECT * FROM budget_payments WHERE budget_id = ? ORDER BY date DESC, created_at DESC", (bid,)).fetchall()
    return jsonify([dict(r) for r in rows])


@app.route("/api/budgets/<bid>/payments", methods=["POST"])
@login_required
def create_budget_payment(bid):
    data = request.get_json(force=True)
    db = get_db()
    if not db.execute("SELECT id FROM budgets WHERE id = ?", (bid,)).fetchone():
        return jsonify({"error": "Presupuesto no encontrado."}), 404
    try:
        amount = float(data.get("amount") or 0)
    except (TypeError, ValueError):
        return jsonify({"error": "El monto debe ser un número."}), 400
    if amount <= 0:
        return jsonify({"error": "El monto debe ser mayor que cero."}), 400
    items = db.execute("SELECT cost FROM budget_items WHERE budget_id = ?", (bid,)).fetchall()
    total = sum(float(i["cost"] or 0) for i in items)
    paid = float(db.execute("SELECT COALESCE(SUM(amount),0) AS p FROM budget_payments WHERE budget_id = ?", (bid,)).fetchone()["p"] or 0)
    balance = max(0, total - paid)
    if amount > balance + 0.005:
        return jsonify({"error": f"El pago supera el saldo pendiente ({balance:.2f})."}), 400
    pid = uuid.uuid4().hex
    date = _clean(data.get("date")) or datetime.now().strftime("%Y-%m-%d")
    db.execute("INSERT INTO budget_payments (id,budget_id,date,amount,notes,created_at) VALUES (?,?,?,?,?,?)", (pid,bid,date,round(amount,2),_clean(data.get("notes")),_now()))
    db.commit()
    row = db.execute("SELECT * FROM budget_payments WHERE id = ?", (pid,)).fetchone()
    return jsonify(dict(row)), 201


@app.route("/api/budgets/<bid>/payments/<pid>", methods=["DELETE"])
@login_required
def delete_budget_payment(bid, pid):
    db = get_db()
    cur = db.execute("DELETE FROM budget_payments WHERE id = ? AND budget_id = ?", (pid, bid))
    if cur.rowcount == 0:
        return jsonify({"error": "Pago no encontrado."}), 404
    db.commit()
    return jsonify({"ok": True})


@app.route("/api/budgets/<bid>", methods=["DELETE"])
@login_required
def delete_budget(bid):
    db = get_db()
    db.execute("DELETE FROM budgets WHERE id = ?", (bid,))
    db.commit()
    return jsonify({"ok": True})


def user_to_dict(row):
    perms = row["permissions"] or ""
    db = get_db()
    accepted = db.execute(
        "SELECT terms_version, accepted_at FROM legal_acceptances WHERE user_id = ? ORDER BY accepted_at DESC LIMIT 1",
        (row["id"],),
    ).fetchone()
    return {
        "id": row["id"],
        "username": row["username"],
        "role": row["role"],
        "permissions": [] if row["role"] == "admin" else [p for p in perms.split(",") if p],
        "displayName": row["display_name"] or "",
        "createdAt": row["created_at"],
        "hasSecurityQuestion": bool(row["security_question"]),
        "hasRecoveryCode": bool(row["recovery_code_hash"]),
        "active": bool(row["active"]) if "active" in row.keys() else True,
        "mustChangePassword": bool(row["must_change_password"]) if "must_change_password" in row.keys() else False,
        "lastLoginAt": row["last_login_at"] if "last_login_at" in row.keys() else "",
        "termsVersion": accepted["terms_version"] if accepted else "",
        "termsAcceptedAt": accepted["accepted_at"] if accepted else "",
        "currentTermsVersion": CURRENT_TERMS_VERSION,
    }


@app.route("/api/me", methods=["GET"])
@login_required
def get_me():
    db = get_db()
    row = db.execute("SELECT * FROM users WHERE id = ?", (session["user_id"],)).fetchone()
    if not row:
        return jsonify({"error": "Usuario no encontrado"}), 404
    return jsonify(user_to_dict(row))


@app.route("/api/users", methods=["GET"])
@login_required
@admin_required
def list_users():
    db = get_db()
    rows = db.execute("SELECT * FROM users ORDER BY created_at ASC").fetchall()
    return jsonify([user_to_dict(r) for r in rows])


@app.route("/api/users", methods=["POST"])
@login_required
@admin_required
def create_user():
    data = request.get_json(force=True)
    username = _clean(data.get("username"))
    password = data.get("password") or ""
    role = data.get("role") if data.get("role") in ("admin", "secretaria") else "secretaria"
    perms = data.get("permissions") or []
    perms = [p for p in perms if p in VIEW_KEYS]
    display_name = _clean(data.get("displayName"))
    active = 1 if data.get("active", True) else 0
    must_change = 1 if data.get("mustChangePassword", False) else 0

    if len(username) < 3:
        return jsonify({"error": "El usuario debe tener al menos 3 caracteres."}), 400
    if len(password) < 8:
        return jsonify({"error": "La contraseña debe tener al menos 8 caracteres."}), 400

    db = get_db()
    if db.execute("SELECT id FROM users WHERE username = ?", (username,)).fetchone():
        return jsonify({"error": "Ese usuario ya existe."}), 400

    uid_ = uuid.uuid4().hex
    db.execute(
        """INSERT INTO users (id, username, password_hash, role, permissions, display_name, active, must_change_password, created_at)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
        (uid_, username, generate_password_hash(password), role,
         "all" if role == "admin" else ",".join(perms), display_name, active, must_change, _now()),
    )
    db.commit()
    row = db.execute("SELECT * FROM users WHERE id = ?", (uid_,)).fetchone()
    _log_activity("user_create", username, f"Rol={role}; activo={bool(active)}", user_id=session.get("user_id"), username=session.get("username"))
    return jsonify(user_to_dict(row)), 201


@app.route("/api/users/<uid>", methods=["PUT"])
@login_required
@admin_required
def update_user(uid):
    data = request.get_json(force=True)
    db = get_db()
    row = db.execute("SELECT * FROM users WHERE id = ?", (uid,)).fetchone()
    if not row:
        return jsonify({"error": "Usuario no encontrado."}), 404

    role = data.get("role") if data.get("role") in ("admin", "secretaria") else row["role"]
    perms = data.get("permissions")
    perms_val = row["permissions"]
    if role == "admin":
        perms_val = "all"
    elif perms is not None:
        perms_val = ",".join([p for p in perms if p in VIEW_KEYS])
    display_name = data.get("displayName", row["display_name"])
    active = 1 if data.get("active", bool(row["active"])) else 0
    must_change = 1 if data.get("mustChangePassword", bool(row["must_change_password"])) else 0
    if uid == session.get("user_id"):
        active = 1

    db.execute(
        "UPDATE users SET role = ?, permissions = ?, display_name = ?, active = ?, must_change_password = ? WHERE id = ?",
        (role, perms_val, _clean(display_name), active, must_change, uid),
    )
    if data.get("password"):
        if len(data["password"]) < 8:
            return jsonify({"error": "La contraseña debe tener al menos 8 caracteres."}), 400
        db.execute("UPDATE users SET password_hash = ?, must_change_password = 1 WHERE id = ?", (generate_password_hash(data["password"]), uid))
    db.commit()
    if uid == session.get("user_id"):
        session["role"] = role
        session["permissions"] = perms_val
        session["must_change_password"] = bool(must_change or data.get("password"))
    _log_activity("user_update", row["username"], f"Rol={role}; activo={bool(active)}; cambio_pw={bool(data.get('password'))}")
    row = db.execute("SELECT * FROM users WHERE id = ?", (uid,)).fetchone()
    return jsonify(user_to_dict(row))


@app.route("/api/users/<uid>", methods=["DELETE"])
@login_required
@admin_required
def delete_user(uid):
    db = get_db()
    if uid == session.get("user_id"):
        return jsonify({"error": "No puedes desactivar tu propia cuenta."}), 400
    target = db.execute("SELECT * FROM users WHERE id = ?", (uid,)).fetchone()
    if not target:
        return jsonify({"error": "Usuario no encontrado."}), 404
    admins_active = db.execute("SELECT COUNT(*) AS c FROM users WHERE role = 'admin' AND active = 1 AND id != ?", (uid,)).fetchone()
    if target["role"] == "admin" and admins_active["c"] == 0:
        return jsonify({"error": "Debe quedar al menos un administrador activo."}), 400
    db.execute("UPDATE users SET active = 0 WHERE id = ?", (uid,))
    db.commit()
    _log_activity("user_deactivate", target["username"], "Cuenta desactivada")
    return jsonify({"ok": True, "deactivated": True})


@app.route("/api/users/<uid>/activate", methods=["POST"])
@login_required
@admin_required
def activate_user(uid):
    db = get_db()
    row = db.execute("SELECT * FROM users WHERE id = ?", (uid,)).fetchone()
    if not row:
        return jsonify({"error": "Usuario no encontrado."}), 404
    db.execute("UPDATE users SET active = 1 WHERE id = ?", (uid,))
    db.commit()
    _log_activity("user_activate", row["username"], "Cuenta reactivada")
    row = db.execute("SELECT * FROM users WHERE id = ?", (uid,)).fetchone()
    return jsonify(user_to_dict(row))

import threading

# Puerto exclusivo de ClinicaSV — distinto al 5000 que usa la Clínica
# (u otras apps locales tuyas), para que nunca compitan por el mismo puerto
# ni se mezclen sus sesiones.
CLINICASV_PORT = int(os.environ.get("CLINICASV_PORT", "5050"))
CLINICA_NETWORK = os.environ.get("CLINICA_NETWORK", "0") == "1"
SERVER_HOST = "0.0.0.0" if CLINICA_NETWORK else "127.0.0.1"

def run_server():
    try:
        from waitress import serve
        serve(app, host=SERVER_HOST, port=CLINICASV_PORT, threads=8)
    except ImportError:
        app.run(host=SERVER_HOST, port=CLINICASV_PORT, debug=False)

if __name__ == "__main__":
    start_backup_scheduler()
    start_email_backup_scheduler()

    server_thread = threading.Thread(target=run_server, daemon=True)
    server_thread.start()

    app_url = f"http://127.0.0.1:{CLINICASV_PORT}"
    if webview is not None:
        try:
            webview.create_window("Clínica", app_url, width=1280, height=800, resizable=True, confirm_close=True)
            webview.start()
        except Exception as exc:
            # No dejar que un fallo de la capa de escritorio cierre el servidor.
            print(f"Aviso: no fue posible iniciar la ventana de escritorio: {exc}")
            try:
                import webbrowser
                webbrowser.open(app_url)
            except Exception:
                pass
            server_thread.join()
    else:
        import webbrowser
        webbrowser.open(app_url)
        server_thread.join()