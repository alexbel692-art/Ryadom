#!/usr/bin/env python3
"""
«Рядом» — веб-панель и API-сервер для клиентов удалённого доступа (совместим с RustDesk).

Только стандартная библиотека Python 3.10+ и SQLite. Запуск:  python3 app.py
Настройки — переменные окружения (см. README и server/.env.example).
"""
import base64
import hashlib
import hmac
import json
import logging
import mimetypes
import os
import re
import secrets
import sqlite3
import threading
import time
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, quote, urlparse

# ---------------------------------------------------------------- настройки
ENV = os.environ.get
HOST = ENV("RYADOM_LISTEN", "0.0.0.0")
PORT = int(ENV("RYADOM_PORT", "21114"))
DATA_DIR = ENV("RYADOM_DATA", os.path.join(os.path.dirname(os.path.abspath(__file__)), "data"))
DOWNLOADS_DIR = ENV("RYADOM_DOWNLOADS", os.path.join(DATA_DIR, "downloads"))
STATIC_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "static")
KEY_FILE = ENV("RYADOM_KEY_FILE", "/rustdesk/id_ed25519.pub")
DOMAIN = ENV("RYADOM_DOMAIN", "localhost")
ID_SERVER = ENV("RYADOM_ID_SERVER", DOMAIN)
RELAY_SERVER = ENV("RYADOM_RELAY_SERVER", DOMAIN)
API_URL = ENV("RYADOM_API_URL", f"https://{DOMAIN}")
APP_NAME = ENV("RYADOM_APP_NAME", "Рядом")
URI_SCHEME = ENV("RYADOM_URI_SCHEME", "ryadom")
ADMIN_USER = ENV("RYADOM_ADMIN_USER", "admin")
ADMIN_PASSWORD = ENV("RYADOM_ADMIN_PASSWORD", "")
ONLINE_WINDOW = int(ENV("RYADOM_ONLINE_WINDOW", "60"))      # сек. без heartbeat — офлайн
TOKEN_TTL = int(ENV("RYADOM_TOKEN_TTL_DAYS", "30")) * 86400
AUDIT_KEEP_DAYS = int(ENV("RYADOM_AUDIT_KEEP_DAYS", "365"))
TRUST_PROXY = ENV("RYADOM_TRUST_PROXY", "1") == "1"         # брать IP из X-Forwarded-For (за Caddy)
MAX_BODY = 4 * 1024 * 1024

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("ryadom")

CONN_TYPES = {0: "Удалённый рабочий стол", 1: "Передача файлов", 2: "Проброс портов",
              3: "Просмотр камеры", 4: "Терминал"}

# ---------------------------------------------------------------- база данных
SCHEMA = """
CREATE TABLE IF NOT EXISTS users (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    name TEXT UNIQUE NOT NULL COLLATE NOCASE,
    display_name TEXT NOT NULL DEFAULT '',
    email TEXT NOT NULL DEFAULT '',
    note TEXT NOT NULL DEFAULT '',
    pw_hash TEXT NOT NULL,
    is_admin INTEGER NOT NULL DEFAULT 0,
    status INTEGER NOT NULL DEFAULT 1,
    created_at INTEGER NOT NULL,
    last_login INTEGER
);
CREATE TABLE IF NOT EXISTS tokens (
    token_hash TEXT PRIMARY KEY,
    user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    kind TEXT NOT NULL,              -- 'web' | 'client'
    device_id TEXT NOT NULL DEFAULT '',
    device_uuid TEXT NOT NULL DEFAULT '',
    created_at INTEGER NOT NULL,
    last_seen INTEGER NOT NULL
);
CREATE TABLE IF NOT EXISTS devices (
    id TEXT PRIMARY KEY,
    uuid TEXT NOT NULL DEFAULT '',
    hostname TEXT NOT NULL DEFAULT '',
    username TEXT NOT NULL DEFAULT '',
    os TEXT NOT NULL DEFAULT '',
    version TEXT NOT NULL DEFAULT '',
    cpu TEXT NOT NULL DEFAULT '',
    memory TEXT NOT NULL DEFAULT '',
    sysinfo TEXT NOT NULL DEFAULT '{}',
    alias TEXT NOT NULL DEFAULT '',
    note TEXT NOT NULL DEFAULT '',
    user_id INTEGER REFERENCES users(id) ON DELETE SET NULL,
    ip TEXT NOT NULL DEFAULT '',
    conns TEXT NOT NULL DEFAULT '[]',
    pending_disconnect TEXT NOT NULL DEFAULT '[]',
    has_sysinfo INTEGER NOT NULL DEFAULT 0,
    first_seen INTEGER NOT NULL,
    last_seen INTEGER NOT NULL
);
CREATE TABLE IF NOT EXISTS conn_audit (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    device_id TEXT NOT NULL,
    conn_id TEXT NOT NULL,
    session_id TEXT NOT NULL DEFAULT '',
    ip TEXT NOT NULL DEFAULT '',
    peer_id TEXT NOT NULL DEFAULT '',
    peer_name TEXT NOT NULL DEFAULT '',
    conn_type INTEGER,
    started_at INTEGER NOT NULL,
    authorized_at INTEGER,
    closed_at INTEGER
);
CREATE INDEX IF NOT EXISTS conn_audit_dev ON conn_audit(device_id, conn_id);
CREATE INDEX IF NOT EXISTS conn_audit_time ON conn_audit(started_at);
CREATE TABLE IF NOT EXISTS file_audit (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    device_id TEXT NOT NULL,
    peer_id TEXT NOT NULL DEFAULT '',
    conn_id TEXT NOT NULL DEFAULT '',
    direction INTEGER,
    path TEXT NOT NULL DEFAULT '',
    is_file INTEGER NOT NULL DEFAULT 0,
    info TEXT NOT NULL DEFAULT '{}',
    created_at INTEGER NOT NULL
);
CREATE INDEX IF NOT EXISTS file_audit_time ON file_audit(created_at);
CREATE TABLE IF NOT EXISTS alarm_audit (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    device_id TEXT NOT NULL,
    typ INTEGER,
    info TEXT NOT NULL DEFAULT '{}',
    created_at INTEGER NOT NULL
);
CREATE TABLE IF NOT EXISTS address_books (
    user_id INTEGER PRIMARY KEY REFERENCES users(id) ON DELETE CASCADE,
    data TEXT NOT NULL,
    updated_at INTEGER NOT NULL
);
"""

_db_lock = threading.RLock()
_db = None


def db():
    global _db
    if _db is None:
        os.makedirs(DATA_DIR, exist_ok=True)
        _db = sqlite3.connect(os.path.join(DATA_DIR, "ryadom.db"), check_same_thread=False,
                              isolation_level=None)
        _db.row_factory = sqlite3.Row
        _db.execute("PRAGMA journal_mode=WAL")
        _db.execute("PRAGMA foreign_keys=ON")
        _db.executescript(SCHEMA)
    return _db


def q(sql, args=(), one=False):
    with _db_lock:
        cur = db().execute(sql, args)
        rows = cur.fetchall()
    if one:
        return rows[0] if rows else None
    return rows


def ex(sql, args=()):
    with _db_lock:
        cur = db().execute(sql, args)
        return cur.lastrowid, cur.rowcount


def now():
    return int(time.time())


# ---------------------------------------------------------------- пароли/токены
def hash_password(pw: str) -> str:
    salt = secrets.token_bytes(16)
    it = 240_000
    dk = hashlib.pbkdf2_hmac("sha256", pw.encode(), salt, it)
    return f"pbkdf2_sha256${it}${base64.b64encode(salt).decode()}${base64.b64encode(dk).decode()}"


def check_password(pw: str, stored: str) -> bool:
    try:
        _, it, salt, dk = stored.split("$")
        calc = hashlib.pbkdf2_hmac("sha256", pw.encode(), base64.b64decode(salt), int(it))
        return hmac.compare_digest(calc, base64.b64decode(dk))
    except Exception:
        return False


def token_hash(tok: str) -> str:
    return hashlib.sha256(tok.encode()).hexdigest()


def issue_token(user_id, kind, device_id="", device_uuid=""):
    tok = secrets.token_urlsafe(32)
    t = now()
    if kind == "client" and device_uuid:
        # один токен на устройство+пользователя
        ex("DELETE FROM tokens WHERE user_id=? AND kind='client' AND device_uuid=?", (user_id, device_uuid))
    ex("INSERT INTO tokens(token_hash,user_id,kind,device_id,device_uuid,created_at,last_seen) "
       "VALUES(?,?,?,?,?,?,?)", (token_hash(tok), user_id, kind, device_id, device_uuid, t, t))
    return tok


def user_by_token(tok):
    if not tok:
        return None
    row = q("SELECT u.*, t.kind AS token_kind, t.last_seen AS token_seen FROM tokens t "
            "JOIN users u ON u.id=t.user_id WHERE t.token_hash=?", (token_hash(tok),), one=True)
    if not row:
        return None
    if row["status"] != 1 or now() - row["token_seen"] > TOKEN_TTL:
        return None
    if now() - row["token_seen"] > 60:
        ex("UPDATE tokens SET last_seen=? WHERE token_hash=?", (now(), token_hash(tok)))
    return row


def user_payload(u):
    return {"name": u["name"], "display_name": u["display_name"], "email": u["email"],
            "note": u["note"], "status": u["status"], "is_admin": bool(u["is_admin"])}


# ---------------------------------------------------------------- защита от перебора
_fail_lock = threading.Lock()
_failures = {}  # ip -> [timestamps]


def too_many_failures(ip):
    with _fail_lock:
        lst = [t for t in _failures.get(ip, []) if time.time() - t < 600]
        _failures[ip] = lst
        return len(lst) >= 10


def note_failure(ip):
    with _fail_lock:
        _failures.setdefault(ip, []).append(time.time())


# ---------------------------------------------------------------- вспомогательное
def public_key():
    try:
        with open(KEY_FILE, encoding="utf-8") as f:
            return f.read().strip()
    except OSError:
        return ""


def config_string():
    """Строка конфигурации сервера в формате клиента (Настройки → Сеть → Импорт)."""
    cfg = {"host": ID_SERVER, "relay": RELAY_SERVER, "api": API_URL, "key": public_key()}
    raw = json.dumps(cfg, separators=(",", ":")).encode()
    return base64.urlsafe_b64encode(raw).decode()[::-1]


def is_online(dev):
    return now() - dev["last_seen"] <= ONLINE_WINDOW


def device_json(d, users_by_id=None):
    conns = json.loads(d["conns"] or "[]")
    online = is_online(d)
    owner = ""
    if d["user_id"] and users_by_id is not None:
        owner = users_by_id.get(d["user_id"], "")
    return {
        "id": d["id"], "hostname": d["hostname"], "username": d["username"], "os": d["os"],
        "version": d["version"], "cpu": d["cpu"], "memory": d["memory"], "alias": d["alias"],
        "note": d["note"], "ip": d["ip"], "owner": owner, "online": online,
        "active_sessions": len(conns) if online else 0,
        "first_seen": d["first_seen"], "last_seen": d["last_seen"],
    }


def list_downloads():
    out = []
    try:
        for name in sorted(os.listdir(DOWNLOADS_DIR)):
            p = os.path.join(DOWNLOADS_DIR, name)
            if os.path.isfile(p) and not name.startswith("."):
                low = name.lower()
                if low.endswith((".exe", ".msi")):
                    plat = "Windows"
                elif low.endswith((".dmg", ".pkg")):
                    plat = "macOS"
                elif low.endswith(".apk"):
                    plat = "Android"
                elif low.endswith((".deb", ".rpm", ".appimage", ".flatpak", ".tar.gz")):
                    plat = "Linux"
                else:
                    plat = "Другое"
                out.append({"name": name, "platform": plat, "size": os.path.getsize(p),
                            "url": "/downloads/" + quote(name)})
    except OSError:
        pass
    return out


def cleanup_loop():
    while True:
        try:
            cut = now() - AUDIT_KEEP_DAYS * 86400
            ex("DELETE FROM conn_audit WHERE started_at<?", (cut,))
            ex("DELETE FROM file_audit WHERE created_at<?", (cut,))
            ex("DELETE FROM alarm_audit WHERE created_at<?", (cut,))
            ex("DELETE FROM tokens WHERE last_seen<?", (now() - TOKEN_TTL,))
        except Exception as e:  # pragma: no cover
            log.warning("cleanup: %s", e)
        time.sleep(3600)


def ensure_admin():
    if q("SELECT 1 FROM users WHERE is_admin=1 LIMIT 1", one=True):
        return
    pw = ADMIN_PASSWORD or secrets.token_urlsafe(9)
    ex("INSERT INTO users(name,display_name,pw_hash,is_admin,status,created_at) VALUES(?,?,?,1,1,?)",
       (ADMIN_USER, "Администратор", hash_password(pw), now()))
    if ADMIN_PASSWORD:
        log.info("Создан администратор «%s» (пароль из RYADOM_ADMIN_PASSWORD)", ADMIN_USER)
    else:
        log.warning("Создан администратор «%s» с паролем: %s  — смените его в панели!", ADMIN_USER, pw)


class HttpError(Exception):
    def __init__(self, status, msg):
        super().__init__(msg)
        self.status = status
        self.msg = msg


ID_RE = re.compile(r"^[A-Za-z0-9_\-]{1,64}$")


def s(v, n=512):
    """Безопасно привести к строке ограниченной длины."""
    if v is None:
        return ""
    if not isinstance(v, str):
        v = json.dumps(v, ensure_ascii=False) if isinstance(v, (dict, list)) else str(v)
    return v[:n]


# ---------------------------------------------------------------- HTTP
class Handler(BaseHTTPRequestHandler):
    server_version = "Ryadom"
    sys_version = ""
    protocol_version = "HTTP/1.1"

    # --- утилиты ответа
    def log_message(self, fmt, *args):
        if "/api/heartbeat" in (self.path or ""):
            return
        log.info("%s %s", self.client_ip(), fmt % args)

    def client_ip(self):
        if TRUST_PROXY:
            xff = self.headers.get("X-Forwarded-For", "")
            if xff:
                return xff.split(",")[0].strip()
            xr = self.headers.get("X-Real-IP")
            if xr:
                return xr.strip()
        return self.client_address[0]

    def send_body(self, status, body: bytes, ctype, extra=None):
        self.send_response(status)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Referrer-Policy", "no-referrer")
        for k, v in (extra or {}).items():
            self.send_header(k, v)
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(body)

    def send_json(self, obj, status=200):
        self.send_body(status, json.dumps(obj, ensure_ascii=False).encode(), "application/json; charset=utf-8",
                       {"Cache-Control": "no-store"})

    def send_text(self, text, status=200):
        self.send_body(status, text.encode(), "text/plain; charset=utf-8", {"Cache-Control": "no-store"})

    def read_json(self):
        n = int(self.headers.get("Content-Length") or 0)
        if n > MAX_BODY:
            raise HttpError(413, "Слишком большой запрос")
        raw = self.rfile.read(n) if n else b""
        if not raw.strip():
            return {}
        try:
            data = json.loads(raw.decode("utf-8"))
        except Exception:
            raise HttpError(400, "Некорректный JSON")
        return data if isinstance(data, dict) else {"_": data}

    def bearer(self):
        h = self.headers.get("Authorization", "")
        return h[7:].strip() if h.lower().startswith("bearer ") else ""

    def require_user(self, admin=False):
        u = user_by_token(self.bearer())
        if not u:
            raise HttpError(401, "Invalid token")
        if admin and not u["is_admin"]:
            raise HttpError(403, "Admin required!")
        return u

    # --- маршрутизация
    def do_GET(self):
        self.dispatch("GET")

    def do_HEAD(self):
        self.dispatch("GET")

    def do_POST(self):
        self.dispatch("POST")

    def do_PUT(self):
        self.dispatch("PUT")

    def do_DELETE(self):
        self.dispatch("DELETE")

    def dispatch(self, method):
        url = urlparse(self.path)
        path = url.path.rstrip("/") or "/"
        self.query = {k: v[-1] for k, v in parse_qs(url.query).items()}
        try:
            if path.startswith("/api/"):
                return self.client_api(method, path)
            if path.startswith("/panel/api/"):
                return self.panel_api(method, path)
            if method != "GET":
                raise HttpError(405, "Method not allowed")
            return self.static(path)
        except HttpError as e:
            # клиент RustDesk ждёт {"error": "..."}
            self.send_json({"error": e.msg}, e.status)
        except BrokenPipeError:
            pass
        except Exception:
            log.exception("Ошибка обработки %s %s", method, self.path)
            self.send_json({"error": "Внутренняя ошибка сервера"}, 500)

    # ------------------------------------------------------------ статика
    def static(self, path):
        if path == "/":
            path = "/download.html"
        elif path in ("/admin", "/panel"):
            path = "/index.html"
        if path.startswith("/downloads/"):
            name = os.path.basename(path[len("/downloads/"):])
            from urllib.parse import unquote
            name = unquote(name)
            full = os.path.join(DOWNLOADS_DIR, name)
            if not name or name.startswith(".") or not os.path.isfile(full):
                raise HttpError(404, "Файл не найден")
            return self.send_file(full, attachment=name)
        rel = os.path.normpath(path.lstrip("/"))
        if rel.startswith(".."):
            raise HttpError(404, "Not found")
        full = os.path.join(STATIC_DIR, rel)
        if not os.path.isfile(full):
            raise HttpError(404, "Not found")
        self.send_file(full)

    def send_file(self, full, attachment=None):
        ctype = mimetypes.guess_type(full)[0] or "application/octet-stream"
        if ctype.startswith("text/") or ctype in ("application/javascript", "image/svg+xml"):
            ctype += "; charset=utf-8"
        size = os.path.getsize(full)
        self.send_response(200)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(size))
        self.send_header("X-Content-Type-Options", "nosniff")
        if attachment:
            self.send_header("Content-Disposition", "attachment; filename*=UTF-8''" + quote(attachment))
        else:
            self.send_header("Cache-Control", "no-cache")
            if full.endswith(".html"):
                self.send_header("Content-Security-Policy",
                                 "default-src 'self'; img-src 'self' data:; style-src 'self' 'unsafe-inline'; "
                                 "script-src 'self' 'unsafe-inline'; frame-ancestors 'none'")
        self.end_headers()
        if self.command == "HEAD":
            return
        with open(full, "rb") as f:
            while True:
                chunk = f.read(256 * 1024)
                if not chunk:
                    break
                self.wfile.write(chunk)

    # ------------------------------------------------------------ API для клиента
    def client_api(self, method, path):
        ip = self.client_ip()

        if path == "/api/login-options" and method == "GET":
            return self.send_json([])

        if path == "/api/login" and method == "POST":
            body = self.read_json()
            if too_many_failures(ip):
                raise HttpError(429, "Слишком много попыток входа, подождите 10 минут")
            if body.get("type") not in (None, "", "account"):
                raise HttpError(400, "Поддерживается только вход по логину и паролю")
            name = s(body.get("username"), 128).strip()
            u = q("SELECT * FROM users WHERE name=?", (name,), one=True)
            if not u or not check_password(s(body.get("password"), 256), u["pw_hash"]):
                note_failure(ip)
                raise HttpError(401, "Неверный логин или пароль")
            if u["status"] != 1:
                raise HttpError(403, "Пользователь отключён")
            dev_id, dev_uuid = s(body.get("id"), 64), s(body.get("uuid"), 128)
            tok = issue_token(u["id"], "client", dev_id, dev_uuid)
            ex("UPDATE users SET last_login=? WHERE id=?", (now(), u["id"]))
            if dev_id and ID_RE.match(dev_id):
                # устройство закрепляется за пользователем, который вошёл на нём
                ex("UPDATE devices SET user_id=? WHERE id=?", (u["id"], dev_id))
            log.info("Вход в клиенте: %s (устройство %s)", u["name"], dev_id)
            return self.send_json({"access_token": tok, "type": "access_token", "user": user_payload(u)})

        if path == "/api/logout" and method == "POST":
            tok = self.bearer()
            if tok:
                ex("DELETE FROM tokens WHERE token_hash=?", (token_hash(tok),))
            return self.send_json({})

        if path == "/api/currentUser" and method == "POST":
            u = self.require_user()
            return self.send_json(user_payload(u))

        # --- heartbeat / sysinfo (без авторизации, как у сервера RustDesk)
        if path == "/api/heartbeat" and method == "POST":
            return self.heartbeat(self.read_json(), ip)
        if path == "/api/sysinfo" and method == "POST":
            return self.sysinfo(self.read_json(), ip)
        if path == "/api/sysinfo_ver" and method == "POST":
            return self.send_text("1")

        # --- аудит
        if path.startswith("/api/audit") and method == "POST":
            return self.audit(path, self.read_json(), ip)

        # --- адресная книга (классический режим)
        if path == "/api/ab/settings" or path == "/api/ab/personal" or path.startswith("/api/ab/shared"):
            raise HttpError(404, "Not found")
        if path in ("/api/ab", "/api/ab/get"):
            u = self.require_user()
            if method == "GET" or path == "/api/ab/get":
                row = q("SELECT data,updated_at FROM address_books WHERE user_id=?", (u["id"],), one=True)
                if not row:
                    return self.send_body(200, b"null", "application/json")
                return self.send_json({"data": row["data"], "updated_at": row["updated_at"]})
            if method == "POST":
                body = self.read_json()
                data = body.get("data")
                if not isinstance(data, str):
                    raise HttpError(400, "Нет поля data")
                try:
                    json.loads(data)
                except Exception:
                    raise HttpError(400, "Некорректные данные адресной книги")
                ex("INSERT INTO address_books(user_id,data,updated_at) VALUES(?,?,?) "
                   "ON CONFLICT(user_id) DO UPDATE SET data=excluded.data, updated_at=excluded.updated_at",
                   (u["id"], data, now()))
                return self.send_body(200, b"", "application/json")

        # --- «Доступные устройства» и пользователи в клиенте
        if path == "/api/users" and method == "GET":
            u = self.require_user()
            rows = q("SELECT * FROM users WHERE status=1 ORDER BY name") if u["is_admin"] else [u]
            return self.paged([user_payload(r) for r in rows])
        if path == "/api/peers" and method == "GET":
            u = self.require_user()
            users = {r["id"]: r["name"] for r in q("SELECT id,name FROM users")}
            if u["is_admin"]:
                rows = q("SELECT * FROM devices ORDER BY last_seen DESC")
            else:
                rows = q("SELECT * FROM devices WHERE user_id=? ORDER BY last_seen DESC", (u["id"],))
            data = []
            for d in rows:
                data.append({
                    "id": d["id"], "status": 1, "note": d["note"] or d["alias"],
                    "user": users.get(d["user_id"], ""), "user_name": users.get(d["user_id"], ""),
                    "info": {"username": d["username"], "os": d["os"],
                             "device_name": d["alias"] or d["hostname"]},
                })
            return self.paged(data)
        if path == "/api/device-group/accessible" and method == "GET":
            self.require_user()
            return self.paged([])

        raise HttpError(404, "Not found")

    def paged(self, items):
        try:
            cur = max(1, int(self.query.get("current", 1)))
            size = min(1000, max(1, int(self.query.get("pageSize", 100))))
        except ValueError:
            cur, size = 1, 100
        return self.send_json({"total": len(items), "data": items[(cur - 1) * size: cur * size]})

    def heartbeat(self, body, ip):
        dev_id = s(body.get("id"), 64)
        if not ID_RE.match(dev_id):
            raise HttpError(400, "Некорректный ID")
        uuid = s(body.get("uuid"), 128)
        conns = body.get("conns") or []
        conns = [int(c) for c in conns if isinstance(c, (int, float))][:64]
        t = now()
        d = q("SELECT * FROM devices WHERE id=?", (dev_id,), one=True)
        resp = {}
        if not d:
            ex("INSERT INTO devices(id,uuid,ip,conns,first_seen,last_seen) VALUES(?,?,?,?,?,?)",
               (dev_id, uuid, ip, json.dumps(conns), t, t))
            resp["sysinfo"] = 1
        else:
            pending = json.loads(d["pending_disconnect"] or "[]")
            if pending:
                resp["disconnect"] = pending
            ex("UPDATE devices SET uuid=?, ip=?, conns=?, last_seen=?, pending_disconnect='[]' WHERE id=?",
               (uuid or d["uuid"], ip, json.dumps(conns), t, dev_id))
            if not d["has_sysinfo"]:
                resp["sysinfo"] = 1
        if "modified_at" in body:
            resp["modified_at"] = body["modified_at"]
        return self.send_json(resp)

    def sysinfo(self, body, ip):
        dev_id = s(body.get("id"), 64)
        if not ID_RE.match(dev_id):
            raise HttpError(400, "Некорректный ID")
        t = now()
        fields = {
            "uuid": s(body.get("uuid"), 128), "hostname": s(body.get("hostname"), 128),
            "username": s(body.get("username"), 128), "os": s(body.get("os"), 256),
            "version": s(body.get("version"), 32), "cpu": s(body.get("cpu"), 256),
            "memory": s(body.get("memory"), 32),
        }
        info = json.dumps({k: v for k, v in body.items() if k != "uuid"}, ensure_ascii=False)[:8192]
        d = q("SELECT id FROM devices WHERE id=?", (dev_id,), one=True)
        if d:
            ex("UPDATE devices SET uuid=?,hostname=?,username=?,os=?,version=?,cpu=?,memory=?,sysinfo=?,"
               "has_sysinfo=1,ip=?,last_seen=? WHERE id=?",
               (*fields.values(), info, ip, t, dev_id))
        else:
            ex("INSERT INTO devices(id,uuid,hostname,username,os,version,cpu,memory,sysinfo,has_sysinfo,ip,"
               "first_seen,last_seen) VALUES(?,?,?,?,?,?,?,?,?,1,?,?,?)",
               (dev_id, *fields.values(), info, ip, t, t))
        # предустановленный пользователь/заметка из настроек сборки клиента
        preset_user = s(body.get("preset-username"), 128)
        if preset_user:
            u = q("SELECT id FROM users WHERE name=?", (preset_user,), one=True)
            if u:
                ex("UPDATE devices SET user_id=? WHERE id=? AND user_id IS NULL", (u["id"], dev_id))
        preset_note = s(body.get("preset-note"), 512)
        if preset_note:
            ex("UPDATE devices SET note=? WHERE id=? AND note=''", (preset_note, dev_id))
        return self.send_text("SYSINFO_UPDATED")

    def audit(self, path, body, ip):
        typ = path[len("/api/audit"):].strip("/")
        dev_id = s(body.get("id"), 64)
        if not ID_RE.match(dev_id):
            raise HttpError(400, "Некорректный ID")
        t = now()
        conn_id = s(body.get("conn_id"), 32)
        if typ == "conn":
            action = body.get("action")
            row = q("SELECT id FROM conn_audit WHERE device_id=? AND conn_id=? ORDER BY id DESC LIMIT 1",
                    (dev_id, conn_id), one=True)
            if action == "new" or not row:
                ex("INSERT INTO conn_audit(device_id,conn_id,session_id,ip,started_at) VALUES(?,?,?,?,?)",
                   (dev_id, conn_id, s(body.get("session_id"), 32), s(body.get("ip"), 64) or ip, t))
                row = q("SELECT id FROM conn_audit WHERE device_id=? AND conn_id=? ORDER BY id DESC LIMIT 1",
                        (dev_id, conn_id), one=True)
            if "peer" in body:
                peer = body.get("peer") or ["", ""]
                if not isinstance(peer, list):
                    peer = [peer, ""]
                peer = (peer + ["", ""])[:2]
                ctype = body.get("type")
                ex("UPDATE conn_audit SET peer_id=?, peer_name=?, conn_type=?, authorized_at=? WHERE id=?",
                   (s(peer[0], 64), s(peer[1], 128), ctype if isinstance(ctype, int) else None, t, row["id"]))
            if action == "close":
                ex("UPDATE conn_audit SET closed_at=? WHERE id=?", (t, row["id"]))
            return self.send_json({})
        if typ == "file":
            info = body.get("info")
            ex("INSERT INTO file_audit(device_id,peer_id,conn_id,direction,path,is_file,info,created_at) "
               "VALUES(?,?,?,?,?,?,?,?)",
               (dev_id, s(body.get("peer_id"), 64), conn_id,
                body.get("type") if isinstance(body.get("type"), int) else None,
                s(body.get("path"), 2048), 1 if body.get("is_file") else 0, s(info, 16384), t))
            return self.send_json({})
        if typ == "alarm":
            ex("INSERT INTO alarm_audit(device_id,typ,info,created_at) VALUES(?,?,?,?)",
               (dev_id, body.get("typ") if isinstance(body.get("typ"), int) else None,
                s(body.get("info"), 8192), t))
            return self.send_json({})
        raise HttpError(404, "Not found")

    # ------------------------------------------------------------ API веб-панели
    def panel_api(self, method, path):
        ip = self.client_ip()
        p = path[len("/panel/api"):]

        # публичная информация для страницы загрузки
        if p == "/public" and method == "GET":
            return self.send_json({
                "app_name": APP_NAME, "id_server": ID_SERVER, "relay_server": RELAY_SERVER,
                "api_server": API_URL, "key": public_key(), "config": config_string(),
                "downloads": list_downloads(),
            })

        if p == "/login" and method == "POST":
            body = self.read_json()
            if too_many_failures(ip):
                raise HttpError(429, "Слишком много попыток входа, подождите 10 минут")
            u = q("SELECT * FROM users WHERE name=?", (s(body.get("username"), 128).strip(),), one=True)
            if not u or not check_password(s(body.get("password"), 256), u["pw_hash"]):
                note_failure(ip)
                raise HttpError(401, "Неверный логин или пароль")
            if u["status"] != 1 or not u["is_admin"]:
                raise HttpError(403, "В панель могут входить только администраторы")
            ex("UPDATE users SET last_login=? WHERE id=?", (now(), u["id"]))
            return self.send_json({"token": issue_token(u["id"], "web"), "user": user_payload(u)})

        me = self.require_user(admin=True)

        if p == "/logout" and method == "POST":
            ex("DELETE FROM tokens WHERE token_hash=?", (token_hash(self.bearer()),))
            return self.send_json({})

        if p == "/me" and method == "GET":
            return self.send_json({**user_payload(me), "uri_scheme": URI_SCHEME, "app_name": APP_NAME})

        if p == "/me/password" and method == "POST":
            body = self.read_json()
            if not check_password(s(body.get("old"), 256), me["pw_hash"]):
                raise HttpError(400, "Текущий пароль неверен")
            new = s(body.get("new"), 256)
            if len(new) < 8:
                raise HttpError(400, "Пароль должен быть не короче 8 символов")
            ex("UPDATE users SET pw_hash=? WHERE id=?", (hash_password(new), me["id"]))
            return self.send_json({})

        if p == "/stats" and method == "GET":
            devs = q("SELECT last_seen, conns FROM devices")
            online = [d for d in devs if is_online(d)]
            day = now() - 86400
            return self.send_json({
                "devices": len(devs), "online": len(online),
                "active_sessions": sum(len(json.loads(d["conns"] or "[]")) for d in online),
                "sessions_24h": q("SELECT COUNT(*) c FROM conn_audit WHERE started_at>? AND authorized_at IS NOT NULL",
                                  (day,), one=True)["c"],
                "users": q("SELECT COUNT(*) c FROM users", one=True)["c"],
            })

        # --- устройства
        if p == "/devices" and method == "GET":
            users = {r["id"]: r["name"] for r in q("SELECT id,name FROM users")}
            rows = q("SELECT * FROM devices ORDER BY last_seen DESC")
            return self.send_json([device_json(d, users) for d in rows])
        m = re.match(r"^/devices/([A-Za-z0-9_\-]{1,64})(/disconnect)?$", p)
        if m:
            dev_id = m.group(1)
            d = q("SELECT * FROM devices WHERE id=?", (dev_id,), one=True)
            if not d:
                raise HttpError(404, "Устройство не найдено")
            if m.group(2) and method == "POST":
                conns = json.loads(d["conns"] or "[]")
                if not conns:
                    raise HttpError(400, "На устройстве нет активных сеансов")
                ex("UPDATE devices SET pending_disconnect=? WHERE id=?", (json.dumps(conns), dev_id))
                log.info("%s отключает сеансы %s на %s", me["name"], conns, dev_id)
                return self.send_json({"ok": True})
            if method == "GET":
                users = {r["id"]: r["name"] for r in q("SELECT id,name FROM users")}
                out = device_json(d, users)
                out["sysinfo"] = json.loads(d["sysinfo"] or "{}")
                out["sessions"] = [dict(r) for r in q(
                    "SELECT * FROM conn_audit WHERE device_id=? ORDER BY id DESC LIMIT 50", (dev_id,))]
                return self.send_json(out)
            if method == "PUT":
                body = self.read_json()
                sets, args = [], []
                for k in ("alias", "note"):
                    if k in body:
                        sets.append(f"{k}=?")
                        args.append(s(body[k], 512))
                if "owner" in body:
                    owner = s(body["owner"], 128)
                    uid = None
                    if owner:
                        r = q("SELECT id FROM users WHERE name=?", (owner,), one=True)
                        if not r:
                            raise HttpError(400, "Нет такого пользователя")
                        uid = r["id"]
                    sets.append("user_id=?")
                    args.append(uid)
                if sets:
                    ex(f"UPDATE devices SET {', '.join(sets)} WHERE id=?", (*args, dev_id))
                return self.send_json({"ok": True})
            if method == "DELETE":
                ex("DELETE FROM devices WHERE id=?", (dev_id,))
                return self.send_json({"ok": True})

        # --- журнал сеансов
        if p == "/audit/conn" and method == "GET":
            limit = min(1000, int(self.query.get("limit", 300) or 300))
            where, args = ["1=1"], []
            dev = self.query.get("device")
            if dev:
                where.append("(c.device_id LIKE ? OR c.peer_id LIKE ? OR c.peer_name LIKE ? OR d.hostname LIKE ?)")
                args += [f"%{dev}%"] * 4
            if self.query.get("authorized") == "1":
                where.append("c.authorized_at IS NOT NULL")
            rows = q(f"SELECT c.*, d.hostname, d.alias FROM conn_audit c LEFT JOIN devices d ON d.id=c.device_id "
                     f"WHERE {' AND '.join(where)} ORDER BY c.id DESC LIMIT ?", (*args, limit))
            out = []
            for r in rows:
                x = dict(r)
                x["conn_type_name"] = CONN_TYPES.get(r["conn_type"], "—" if r["authorized_at"] is None else "Другое")
                out.append(x)
            return self.send_json(out)
        if p == "/audit/file" and method == "GET":
            rows = q("SELECT f.*, d.hostname, d.alias FROM file_audit f LEFT JOIN devices d ON d.id=f.device_id "
                     "ORDER BY f.id DESC LIMIT 500")
            return self.send_json([dict(r) for r in rows])
        if p == "/audit/alarm" and method == "GET":
            rows = q("SELECT a.*, d.hostname FROM alarm_audit a LEFT JOIN devices d ON d.id=a.device_id "
                     "ORDER BY a.id DESC LIMIT 500")
            return self.send_json([dict(r) for r in rows])

        # --- пользователи
        if p == "/users" and method == "GET":
            rows = q("SELECT u.*, (SELECT COUNT(*) FROM devices d WHERE d.user_id=u.id) AS devices "
                     "FROM users u ORDER BY u.name")
            return self.send_json([{**user_payload(r), "id": r["id"], "devices": r["devices"],
                                    "created_at": r["created_at"], "last_login": r["last_login"]} for r in rows])
        if p == "/users" and method == "POST":
            body = self.read_json()
            name = s(body.get("name"), 64).strip()
            if not re.match(r"^[\w.\-@]{2,64}$", name):
                raise HttpError(400, "Логин: 2–64 символа, буквы, цифры, . - _ @")
            pw = s(body.get("password"), 256)
            if len(pw) < 8:
                raise HttpError(400, "Пароль должен быть не короче 8 символов")
            if q("SELECT 1 FROM users WHERE name=?", (name,), one=True):
                raise HttpError(400, "Такой логин уже есть")
            ex("INSERT INTO users(name,display_name,email,note,pw_hash,is_admin,status,created_at) "
               "VALUES(?,?,?,?,?,?,1,?)",
               (name, s(body.get("display_name"), 128), s(body.get("email"), 128), s(body.get("note"), 512),
                hash_password(pw), 1 if body.get("is_admin") else 0, now()))
            return self.send_json({"ok": True})
        m = re.match(r"^/users/(\d+)$", p)
        if m:
            uid = int(m.group(1))
            u = q("SELECT * FROM users WHERE id=?", (uid,), one=True)
            if not u:
                raise HttpError(404, "Пользователь не найден")
            admins = q("SELECT COUNT(*) c FROM users WHERE is_admin=1 AND status=1", one=True)["c"]
            if method == "PUT":
                body = self.read_json()
                sets, args = [], []
                for k, n in (("display_name", 128), ("email", 128), ("note", 512)):
                    if k in body:
                        sets.append(f"{k}=?")
                        args.append(s(body[k], n))
                if "is_admin" in body or "status" in body:
                    new_admin = 1 if body.get("is_admin", bool(u["is_admin"])) else 0
                    new_status = 1 if int(body.get("status", u["status"])) == 1 else 0
                    if u["is_admin"] and u["status"] == 1 and (not new_admin or not new_status) and admins <= 1:
                        raise HttpError(400, "Нельзя убрать последнего администратора")
                    sets += ["is_admin=?", "status=?"]
                    args += [new_admin, new_status]
                    if not new_status:
                        ex("DELETE FROM tokens WHERE user_id=?", (uid,))
                if body.get("password"):
                    pw = s(body["password"], 256)
                    if len(pw) < 8:
                        raise HttpError(400, "Пароль должен быть не короче 8 символов")
                    sets.append("pw_hash=?")
                    args.append(hash_password(pw))
                    ex("DELETE FROM tokens WHERE user_id=? AND kind='client'", (uid,))
                if sets:
                    ex(f"UPDATE users SET {', '.join(sets)} WHERE id=?", (*args, uid))
                return self.send_json({"ok": True})
            if method == "DELETE":
                if uid == me["id"]:
                    raise HttpError(400, "Нельзя удалить самого себя")
                if u["is_admin"] and admins <= 1:
                    raise HttpError(400, "Нельзя удалить последнего администратора")
                ex("DELETE FROM users WHERE id=?", (uid,))
                return self.send_json({"ok": True})

        # --- настройки подключения клиентов
        if p == "/server" and method == "GET":
            return self.send_json({
                "app_name": APP_NAME, "id_server": ID_SERVER, "relay_server": RELAY_SERVER,
                "api_server": API_URL, "key": public_key(), "config": config_string(),
                "uri_scheme": URI_SCHEME, "downloads": list_downloads(),
            })

        raise HttpError(404, "Not found")


def main():
    db()
    ensure_admin()
    os.makedirs(DOWNLOADS_DIR, exist_ok=True)
    threading.Thread(target=cleanup_loop, daemon=True).start()
    srv = ThreadingHTTPServer((HOST, PORT), Handler)
    srv.daemon_threads = True
    log.info("«%s» слушает http://%s:%d  (ID-сервер %s, API %s)", APP_NAME, HOST, PORT, ID_SERVER, API_URL)
    if not public_key():
        log.warning("Публичный ключ не найден в %s — проверьте том с данными hbbs", KEY_FILE)
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
