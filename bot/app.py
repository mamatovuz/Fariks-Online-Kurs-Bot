from __future__ import annotations

import hmac
import base64
import hashlib
import html
import json
import mimetypes
import os
import re
import sqlite3
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path


BASE_DIR = Path(__file__).resolve().parent
ROOT_DIR = BASE_DIR.parent


def find_client_dir() -> Path:
    candidates = [
        ROOT_DIR / "client",
        BASE_DIR / "client",
    ]
    for candidate in candidates:
        if (candidate / "index.html").exists():
            return candidate
    return ROOT_DIR / "client"


CLIENT_DIR = find_client_dir()


def load_env(path: Path) -> None:
    if not path.exists():
        return

    for raw_line in path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        key = key.strip()
        value = value.strip().strip('"').strip("'")
        if key and key not in os.environ:
            os.environ[key] = value


load_env(BASE_DIR / ".env")


def clean_env(name: str, default: str = "") -> str:
    value = os.getenv(name, default).strip()
    if len(value) >= 2 and value[0] == value[-1] and value[0] in {"'", '"'}:
        value = value[1:-1].strip()
    return value


def normalize_api_host(value: str) -> str:
    if value.startswith(("http://", "https://")):
        return "0.0.0.0"
    return value or "0.0.0.0"


def normalize_public_url(value: str) -> str:
    value = value.strip().rstrip("/")
    if not value:
        return value
    if "://" not in value:
        value = f"https://{value}"
    parsed = urllib.parse.urlparse(value)
    host = parsed.netloc.lower()
    if host.endswith(".railway.app") and not host.endswith(".up.railway.app"):
        subdomain = host[: -len(".railway.app")]
        host = f"{subdomain}.up.railway.app"
        value = urllib.parse.urlunparse(("https", host, parsed.path, "", "", ""))
    elif parsed.scheme == "http" and host.endswith(".up.railway.app"):
        value = urllib.parse.urlunparse(("https", parsed.netloc, parsed.path, "", "", ""))
    return value.rstrip("/")


BOT_TOKEN = clean_env("BOT_TOKEN")
RAILWAY_PUBLIC_DOMAIN = clean_env("RAILWAY_PUBLIC_DOMAIN")
API_HOST = normalize_api_host(clean_env("API_HOST", "0.0.0.0"))
API_PORT = int(clean_env("PORT") or clean_env("API_PORT") or "8080")
DEFAULT_PUBLIC_CLIENT_URL = f"https://{RAILWAY_PUBLIC_DOMAIN}" if RAILWAY_PUBLIC_DOMAIN else f"http://localhost:{API_PORT}"
PUBLIC_CLIENT_URL = normalize_public_url(clean_env("PUBLIC_CLIENT_URL", DEFAULT_PUBLIC_CLIENT_URL))
PUBLIC_API_URL = normalize_public_url(clean_env("PUBLIC_API_URL", DEFAULT_PUBLIC_CLIENT_URL))
ADMIN_TOKEN = clean_env("ADMIN_TOKEN", "change-me")
APP_SECRET = clean_env("APP_SECRET") or ADMIN_TOKEN or BOT_TOKEN or "fariks-lms-dev-secret"
ADMIN_TELEGRAM_IDS = {
    int(item)
    for item in re.split(r"[,\s]+", clean_env("ADMIN_TELEGRAM_IDS") or clean_env("ADMIN_TELEGRAM_ID") or ADMIN_TOKEN)
    if item.strip().lstrip("-").isdigit()
}
DB_PATH = Path(clean_env("DB_PATH", str(BASE_DIR / "fariks_lms.sqlite3")))
if not DB_PATH.is_absolute():
    DB_PATH = BASE_DIR / DB_PATH


def b64url_encode(value: bytes) -> str:
    return base64.urlsafe_b64encode(value).decode("ascii").rstrip("=")


def b64url_decode(value: str) -> bytes:
    padding = "=" * (-len(value) % 4)
    return base64.urlsafe_b64decode(value + padding)


def create_signed_token(kind: str, payload: dict, expires_at: datetime) -> str:
    body = {
        "kind": kind,
        "exp": int(expires_at.timestamp()),
        "payload": payload,
    }
    encoded_body = b64url_encode(json.dumps(body, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8"))
    signature = hmac.new(APP_SECRET.encode("utf-8"), encoded_body.encode("ascii"), hashlib.sha256).digest()
    return f"{kind}.{encoded_body}.{b64url_encode(signature)}"


def read_signed_token(token: str, expected_kind: str) -> dict:
    try:
        kind, encoded_body, encoded_signature = token.split(".", 2)
        if kind != expected_kind:
            raise ValueError
        expected_signature = hmac.new(APP_SECRET.encode("utf-8"), encoded_body.encode("ascii"), hashlib.sha256).digest()
        if not hmac.compare_digest(b64url_decode(encoded_signature), expected_signature):
            raise ValueError
        body = json.loads(b64url_decode(encoded_body).decode("utf-8"))
        if body.get("kind") != expected_kind:
            raise ValueError
        if int(body.get("exp", 0)) < int(datetime.now(timezone.utc).timestamp()):
            raise TimeoutError
        return body
    except TimeoutError:
        raise ValueError("Link muddati tugagan")
    except Exception as error:
        if isinstance(error, ValueError) and str(error):
            raise
        raise ValueError("Link noto'g'ri yoki eskirgan")


def create_admin_session_token(telegram_id: int, name: str, username: str = "") -> str:
    return create_signed_token(
        "admin",
        {"telegram_id": telegram_id, "name": name, "username": username, "login_method": "telegram"},
        datetime.now(timezone.utc) + timedelta(days=14),
    )


def read_admin_session_token(token: str) -> dict:
    body = read_signed_token(token, "admin")
    return dict(body.get("payload") or {})


def same_origin(left: str, right: str) -> bool:
    left_url = urllib.parse.urlparse(left)
    right_url = urllib.parse.urlparse(right)
    return (left_url.scheme, left_url.netloc) == (right_url.scheme, right_url.netloc)


def public_link(path: str, query: dict[str, str] | None = None) -> str:
    params = dict(query or {})
    if PUBLIC_API_URL and not same_origin(PUBLIC_CLIENT_URL, PUBLIC_API_URL):
        params.setdefault("api", PUBLIC_API_URL)
    encoded = urllib.parse.urlencode(params)
    return f"{PUBLIC_CLIENT_URL}{path}" + (f"?{encoded}" if encoded else "")


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def format_money(amount: int) -> str:
    return f"{amount:,}".replace(",", " ") + " so'm"


def slugify(value: str) -> str:
    text = value.lower()
    replacements = {
        "o'": "o",
        "g'": "g",
        "sh": "sh",
        "ch": "ch",
        "yo": "yo",
        "ya": "ya",
        "yu": "yu",
    }
    for source, target in replacements.items():
        text = text.replace(source, target)
    text = re.sub(r"[^a-z0-9]+", "-", text)
    text = text.strip("-")
    return text or f"item-{uuid.uuid4().hex[:8]}"


def rows_to_dicts(rows: list[sqlite3.Row]) -> list[dict]:
    return [dict(row) for row in rows]


class Database:
    def __init__(self, path: Path):
        self.path = path
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.lock = threading.RLock()

    @contextmanager
    def connection(self):
        with self.lock:
            conn = sqlite3.connect(self.path)
            conn.row_factory = sqlite3.Row
            conn.execute("PRAGMA foreign_keys = ON")
            try:
                yield conn
                conn.commit()
            except Exception:
                conn.rollback()
                raise
            finally:
                conn.close()

    def init_schema(self) -> None:
        with self.connection() as conn:
            conn.executescript(
                """
                CREATE TABLE IF NOT EXISTS users (
                    telegram_id INTEGER PRIMARY KEY,
                    full_name TEXT NOT NULL,
                    phone TEXT NOT NULL,
                    registered_at TEXT NOT NULL
                );

                CREATE TABLE IF NOT EXISTS user_states (
                    telegram_id INTEGER PRIMARY KEY,
                    state TEXT NOT NULL,
                    payload TEXT NOT NULL DEFAULT '{}',
                    updated_at TEXT NOT NULL
                );

                CREATE TABLE IF NOT EXISTS courses (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    slug TEXT NOT NULL UNIQUE,
                    title TEXT NOT NULL,
                    price INTEGER NOT NULL,
                    description TEXT NOT NULL DEFAULT '',
                    created_at TEXT NOT NULL
                );

                CREATE TABLE IF NOT EXISTS modules (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    course_id INTEGER NOT NULL,
                    title TEXT NOT NULL,
                    position INTEGER NOT NULL,
                    created_at TEXT NOT NULL,
                    FOREIGN KEY(course_id) REFERENCES courses(id) ON DELETE CASCADE
                );

                CREATE TABLE IF NOT EXISTS lessons (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    module_id INTEGER NOT NULL,
                    slug TEXT NOT NULL UNIQUE,
                    title TEXT NOT NULL,
                    position INTEGER NOT NULL,
                    video_url TEXT NOT NULL DEFAULT '',
                    duration_minutes INTEGER NOT NULL DEFAULT 30,
                    pass_percent INTEGER NOT NULL DEFAULT 80,
                    created_at TEXT NOT NULL,
                    FOREIGN KEY(module_id) REFERENCES modules(id) ON DELETE CASCADE
                );

                CREATE TABLE IF NOT EXISTS questions (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    lesson_id INTEGER NOT NULL,
                    text TEXT NOT NULL,
                    option_a TEXT NOT NULL,
                    option_b TEXT NOT NULL,
                    option_c TEXT NOT NULL,
                    option_d TEXT NOT NULL,
                    correct_option TEXT NOT NULL CHECK(correct_option IN ('A','B','C','D')),
                    explanation TEXT NOT NULL DEFAULT '',
                    position INTEGER NOT NULL,
                    created_at TEXT NOT NULL,
                    FOREIGN KEY(lesson_id) REFERENCES lessons(id) ON DELETE CASCADE
                );

                CREATE TABLE IF NOT EXISTS payments (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    user_id INTEGER NOT NULL,
                    course_id INTEGER NOT NULL,
                    method TEXT NOT NULL,
                    amount INTEGER NOT NULL,
                    status TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    FOREIGN KEY(user_id) REFERENCES users(telegram_id) ON DELETE CASCADE,
                    FOREIGN KEY(course_id) REFERENCES courses(id) ON DELETE CASCADE
                );

                CREATE TABLE IF NOT EXISTS enrollments (
                    user_id INTEGER NOT NULL,
                    course_id INTEGER NOT NULL,
                    status TEXT NOT NULL DEFAULT 'active',
                    created_at TEXT NOT NULL,
                    PRIMARY KEY(user_id, course_id),
                    FOREIGN KEY(user_id) REFERENCES users(telegram_id) ON DELETE CASCADE,
                    FOREIGN KEY(course_id) REFERENCES courses(id) ON DELETE CASCADE
                );

                CREATE TABLE IF NOT EXISTS progress (
                    user_id INTEGER NOT NULL,
                    lesson_id INTEGER NOT NULL,
                    status TEXT NOT NULL DEFAULT 'not_started',
                    best_percent INTEGER NOT NULL DEFAULT 0,
                    passed_at TEXT,
                    updated_at TEXT NOT NULL,
                    PRIMARY KEY(user_id, lesson_id),
                    FOREIGN KEY(user_id) REFERENCES users(telegram_id) ON DELETE CASCADE,
                    FOREIGN KEY(lesson_id) REFERENCES lessons(id) ON DELETE CASCADE
                );

                CREATE TABLE IF NOT EXISTS test_tokens (
                    token TEXT PRIMARY KEY,
                    user_id INTEGER NOT NULL,
                    lesson_id INTEGER NOT NULL,
                    expires_at TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    FOREIGN KEY(user_id) REFERENCES users(telegram_id) ON DELETE CASCADE,
                    FOREIGN KEY(lesson_id) REFERENCES lessons(id) ON DELETE CASCADE
                );

                CREATE TABLE IF NOT EXISTS results (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    user_id INTEGER NOT NULL,
                    lesson_id INTEGER NOT NULL,
                    correct_count INTEGER NOT NULL,
                    total_count INTEGER NOT NULL,
                    percent INTEGER NOT NULL,
                    passed INTEGER NOT NULL,
                    created_at TEXT NOT NULL,
                    FOREIGN KEY(user_id) REFERENCES users(telegram_id) ON DELETE CASCADE,
                    FOREIGN KEY(lesson_id) REFERENCES lessons(id) ON DELETE CASCADE
                );
                """
            )

    def seed(self) -> None:
        with self.connection() as conn:
            course_count = conn.execute("SELECT COUNT(*) FROM courses").fetchone()[0]
            if course_count:
                return

            def add_course(title: str, price: int, description: str) -> int:
                cursor = conn.execute(
                    """
                    INSERT INTO courses (slug, title, price, description, created_at)
                    VALUES (?, ?, ?, ?, ?)
                    """,
                    (slugify(title), title, price, description, now_iso()),
                )
                return int(cursor.lastrowid)

            def add_module(course_id: int, title: str, position: int) -> int:
                cursor = conn.execute(
                    """
                    INSERT INTO modules (course_id, title, position, created_at)
                    VALUES (?, ?, ?, ?)
                    """,
                    (course_id, title, position, now_iso()),
                )
                return int(cursor.lastrowid)

            def add_lesson(module_id: int, title: str, position: int, video_url: str = "") -> int:
                base_slug = slugify(title)
                slug = base_slug
                counter = 2
                while conn.execute("SELECT 1 FROM lessons WHERE slug = ?", (slug,)).fetchone():
                    slug = f"{base_slug}-{counter}"
                    counter += 1
                cursor = conn.execute(
                    """
                    INSERT INTO lessons
                        (module_id, slug, title, position, video_url, duration_minutes, pass_percent, created_at)
                    VALUES (?, ?, ?, ?, ?, 30, 80, ?)
                    """,
                    (module_id, slug, title, position, video_url, now_iso()),
                )
                return int(cursor.lastrowid)

            def add_questions(lesson_id: int, questions: list[tuple[str, str, str, str, str, str, str]]) -> None:
                for index, question in enumerate(questions, start=1):
                    conn.execute(
                        """
                        INSERT INTO questions
                            (lesson_id, text, option_a, option_b, option_c, option_d, correct_option, explanation, position, created_at)
                        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                        """,
                        (lesson_id, *question, index, now_iso()),
                    )

            national = add_course(
                "Milliy Sertifikat Matematika",
                300_000,
                "Milliy sertifikat imtihonlari uchun bosqichma-bosqich matematika kursi.",
            )
            attestation = add_course(
                "Attestatsiya Matematika",
                250_000,
                "Ustozlar attestatsiyasi uchun amaliy misollar va testlar.",
            )
            applicant = add_course(
                "Abituriyent Matematika",
                350_000,
                "DTM va oliy ta'lim kirish imtihonlari uchun matematika tayyorlov kursi.",
            )

            module_1 = add_module(national, "1-MODUL: Algebra asoslari", 1)
            module_2 = add_module(national, "2-MODUL: Trigonometriya", 2)
            module_3 = add_module(national, "3-MODUL: Geometriya", 3)

            lesson_1 = add_lesson(module_1, "1-Dars: Chiziqli tenglamalar", 1)
            lesson_2 = add_lesson(module_1, "2-Dars: Ildizli tenglamalar", 2)
            lesson_3 = add_lesson(module_1, "3-Dars: Logarifmlar", 3)
            add_lesson(module_2, "4-Dars: Trigonometrik ayniyatlar", 1)
            add_lesson(module_2, "5-Dars: Sinus va kosinus tenglamalar", 2)
            add_lesson(module_3, "6-Dars: Uchburchaklar", 1)

            add_questions(
                lesson_1,
                [
                    (r"$2x+3=11$ tenglamani yeching.", r"$4$", r"$5$", r"$3$", r"$7$", "A", r"$2x=8$, demak $x=4$."),
                    (r"$\frac{2x+3}{x-1}=5$ tenglamani yeching.", r"$2$", r"$3$", r"$\frac{8}{3}$", r"$-1$", "C", r"$2x+3=5x-5$, demak $x=\frac{8}{3}$."),
                    (r"$3(x-2)=2x+5$ bo'lsa, $x$ nechaga teng?", r"$9$", r"$11$", r"$7$", r"$13$", "B", r"$3x-6=2x+5$, demak $x=11$."),
                    (r"$5x-7=2x+14$ tenglama ildizini toping.", r"$7$", r"$5$", r"$6$", r"$9$", "A", r"$3x=21$, demak $x=7$."),
                    (r"$4-2x=10$ tenglamaning yechimi qaysi?", r"$3$", r"$-2$", r"$-3$", r"$7$", "C", r"$-2x=6$, demak $x=-3$."),
                    (r"$\frac{x+4}{3}=5$ tenglamada $x$ ni toping.", r"$10$", r"$11$", r"$12$", r"$9$", "B", r"$x+4=15$, demak $x=11$."),
                    (r"$7x+1=3x+17$ bo'lsa, $x$ nechaga teng?", r"$2$", r"$3$", r"$4$", r"$5$", "C", r"$4x=16$, demak $x=4$."),
                    (r"$2(x+5)-3=15$ tenglamani yeching.", r"$4$", r"$6$", r"$8$", r"$9$", "A", r"$2x+7=15$, demak $x=4$."),
                    (r"$\frac{x}{2}+\frac{x}{3}=10$ tenglama ildizini toping.", r"$10$", r"$12$", r"$14$", r"$16$", "B", r"$\frac{5x}{6}=10$, demak $x=12$."),
                    (r"$0.2x+3=7$ bo'lsa, $x$ nechaga teng?", r"$10$", r"$15$", r"$20$", r"$25$", "C", r"$0.2x=4$, demak $x=20$."),
                    (r"$|x-3|=5$ tenglama yechimlari qaysi?", r"$8$ va $-2$", r"$5$ va $-5$", r"$3$ va $5$", r"$2$ va $8$", "A", r"$x-3=5$ yoki $x-3=-5$."),
                    (r"$6-(x+1)=2x-4$ tenglamani yeching.", r"$2$", r"$3$", r"$4$", r"$5$", "B", r"$5-x=2x-4$, demak $3x=9$."),
                    (r"$\frac{2}{3}x-4=6$ bo'lsa, $x$ nechaga teng?", r"$12$", r"$15$", r"$18$", r"$21$", "B", r"$\frac{2}{3}x=10$, demak $x=15$."),
                    (r"$5(x-1)=2(2x+3)$ tenglama ildizini toping.", r"$9$", r"$10$", r"$11$", r"$12$", "C", r"$5x-5=4x+6$, demak $x=11$."),
                    (r"$\frac{3x-1}{2}=7$ bo'lsa, $x$ nechaga teng?", r"$4$", r"$5$", r"$6$", r"$7$", "B", r"$3x-1=14$, demak $x=5$."),
                    (r"$9x=3(x+8)$ tenglamani yeching.", r"$3$", r"$4$", r"$5$", r"$6$", "B", r"$9x=3x+24$, demak $x=4$."),
                    (r"$x-(2x-5)=1$ tenglama yechimi qaysi?", r"$2$", r"$3$", r"$4$", r"$5$", "C", r"$-x+5=1$, demak $x=4$."),
                    (r"$4(x+2)=2x+18$ tenglamani yeching.", r"$4$", r"$5$", r"$6$", r"$7$", "B", r"$4x+8=2x+18$, demak $x=5$."),
                    (r"$\frac{x-2}{x+1}=\frac{1}{2}$ tenglama ildizini toping.", r"$3$", r"$4$", r"$5$", r"$6$", "C", r"$2x-4=x+1$, demak $x=5$."),
                    (r"$3x+2=2(x+9)$ bo'lsa, $x$ nechaga teng?", r"$14$", r"$15$", r"$16$", r"$18$", "C", r"$3x+2=2x+18$, demak $x=16$."),
                ],
            )

            short_questions = [
                (r"$\sqrt{x+4}=5$ tenglamani yeching.", r"$19$", r"$20$", r"$21$", r"$22$", "C", r"$x+4=25$, demak $x=21$."),
                (r"$\sqrt{x-1}=4$ bo'lsa, $x$ nechaga teng?", r"$15$", r"$16$", r"$17$", r"$18$", "C", r"$x-1=16$, demak $x=17$."),
                (r"$\sqrt{x+4}+\sqrt{x-1}=5$ uchun mos yechimni toping.", r"$1$", r"$5$", r"$10$", r"$13$", "B", r"$x=5$ bo'lsa, $3+2=5$."),
                (r"$\sqrt{2x+1}=3$ tenglama ildizi qaysi?", r"$3$", r"$4$", r"$5$", r"$6$", "B", r"$2x+1=9$, demak $x=4$."),
                (r"$\sqrt{x}=7$ bo'lsa, $x$ nechaga teng?", r"$14$", r"$21$", r"$42$", r"$49$", "D", r"$x=49$."),
            ]
            add_questions(lesson_2, short_questions)

            add_questions(
                lesson_3,
                [
                    (r"$\log_2 x=5$ bo'lsa, $x$ nechaga teng?", r"$10$", r"$16$", r"$25$", r"$32$", "D", r"$x=2^5=32$."),
                    (r"$\log_3 81$ qiymatini toping.", r"$3$", r"$4$", r"$5$", r"$6$", "B", r"$3^4=81$."),
                    (r"$\log_{10} 1000$ qiymati qaysi?", r"$2$", r"$3$", r"$4$", r"$10$", "B", r"$10^3=1000$."),
                    (r"$\log_5 25+\log_2 8$ ni hisoblang.", r"$4$", r"$5$", r"$6$", r"$7$", "B", r"$2+3=5$."),
                    (r"$\log_4 16$ qiymatini toping.", r"$2$", r"$3$", r"$4$", r"$8$", "A", r"$4^2=16$."),
                ],
            )

            for course_id, label in [(attestation, "Attestatsiya"), (applicant, "Abituriyent")]:
                module = add_module(course_id, "1-MODUL: Boshlang'ich testlar", 1)
                lesson = add_lesson(module, f"1-Dars: {label} kirish testi", 1)
                add_questions(lesson, short_questions)

    def set_state(self, telegram_id: int, state: str, payload: dict | None = None) -> None:
        with self.connection() as conn:
            conn.execute(
                """
                INSERT INTO user_states (telegram_id, state, payload, updated_at)
                VALUES (?, ?, ?, ?)
                ON CONFLICT(telegram_id) DO UPDATE SET
                    state = excluded.state,
                    payload = excluded.payload,
                    updated_at = excluded.updated_at
                """,
                (telegram_id, state, json.dumps(payload or {}, ensure_ascii=False), now_iso()),
            )

    def get_state(self, telegram_id: int) -> tuple[str | None, dict]:
        with self.connection() as conn:
            row = conn.execute(
                "SELECT state, payload FROM user_states WHERE telegram_id = ?",
                (telegram_id,),
            ).fetchone()
        if not row:
            return None, {}
        return row["state"], json.loads(row["payload"] or "{}")

    def clear_state(self, telegram_id: int) -> None:
        with self.connection() as conn:
            conn.execute("DELETE FROM user_states WHERE telegram_id = ?", (telegram_id,))

    def register_user(self, telegram_id: int, full_name: str, phone: str) -> None:
        with self.connection() as conn:
            conn.execute(
                """
                INSERT INTO users (telegram_id, full_name, phone, registered_at)
                VALUES (?, ?, ?, ?)
                ON CONFLICT(telegram_id) DO UPDATE SET
                    full_name = excluded.full_name,
                    phone = excluded.phone
                """,
                (telegram_id, full_name, phone, now_iso()),
            )
            conn.execute("DELETE FROM user_states WHERE telegram_id = ?", (telegram_id,))

    def get_user(self, telegram_id: int) -> dict | None:
        with self.connection() as conn:
            row = conn.execute("SELECT * FROM users WHERE telegram_id = ?", (telegram_id,)).fetchone()
        return dict(row) if row else None

    def list_courses(self) -> list[dict]:
        with self.connection() as conn:
            rows = conn.execute("SELECT * FROM courses ORDER BY id").fetchall()
        return rows_to_dicts(rows)

    def get_course(self, course_id: int) -> dict | None:
        with self.connection() as conn:
            row = conn.execute("SELECT * FROM courses WHERE id = ?", (course_id,)).fetchone()
        return dict(row) if row else None

    def list_enrollments(self, user_id: int) -> list[dict]:
        with self.connection() as conn:
            rows = conn.execute(
                """
                SELECT e.*, c.title, c.price, c.description
                FROM enrollments e
                JOIN courses c ON c.id = e.course_id
                WHERE e.user_id = ?
                ORDER BY e.created_at DESC
                """,
                (user_id,),
            ).fetchall()
        return rows_to_dicts(rows)

    def is_enrolled(self, user_id: int, course_id: int) -> bool:
        with self.connection() as conn:
            row = conn.execute(
                "SELECT 1 FROM enrollments WHERE user_id = ? AND course_id = ? AND status = 'active'",
                (user_id, course_id),
            ).fetchone()
        return bool(row)

    def create_payment_and_enrollment(self, user_id: int, course_id: int, method: str) -> dict:
        course = self.get_course(course_id)
        if not course:
            raise ValueError("Kurs topilmadi")

        with self.connection() as conn:
            cursor = conn.execute(
                """
                INSERT INTO payments (user_id, course_id, method, amount, status, created_at)
                VALUES (?, ?, ?, ?, 'confirmed', ?)
                """,
                (user_id, course_id, method, course["price"], now_iso()),
            )
            conn.execute(
                """
                INSERT INTO enrollments (user_id, course_id, status, created_at)
                VALUES (?, ?, 'active', ?)
                ON CONFLICT(user_id, course_id) DO UPDATE SET status = 'active'
                """,
                (user_id, course_id, now_iso()),
            )
        return {"payment_id": cursor.lastrowid, "course": course}

    def list_payments(self, user_id: int) -> list[dict]:
        with self.connection() as conn:
            rows = conn.execute(
                """
                SELECT p.*, c.title AS course_title
                FROM payments p
                JOIN courses c ON c.id = p.course_id
                WHERE p.user_id = ?
                ORDER BY p.created_at DESC
                """,
                (user_id,),
            ).fetchall()
        return rows_to_dicts(rows)

    def get_lesson(self, lesson_id: int) -> dict | None:
        with self.connection() as conn:
            row = conn.execute(
                """
                SELECT l.*, m.course_id, m.title AS module_title, c.title AS course_title
                FROM lessons l
                JOIN modules m ON m.id = l.module_id
                JOIN courses c ON c.id = m.course_id
                WHERE l.id = ?
                """,
                (lesson_id,),
            ).fetchone()
        return dict(row) if row else None

    def get_lesson_by_slug(self, slug: str) -> dict | None:
        with self.connection() as conn:
            row = conn.execute(
                """
                SELECT l.*, m.course_id, m.title AS module_title, c.title AS course_title
                FROM lessons l
                JOIN modules m ON m.id = l.module_id
                JOIN courses c ON c.id = m.course_id
                WHERE l.slug = ?
                """,
                (slug,),
            ).fetchone()
        return dict(row) if row else None

    def course_lesson_order(self, course_id: int) -> list[dict]:
        with self.connection() as conn:
            rows = conn.execute(
                """
                SELECT l.*, m.course_id, m.title AS module_title, m.position AS module_position
                FROM lessons l
                JOIN modules m ON m.id = l.module_id
                WHERE m.course_id = ?
                ORDER BY m.position, l.position, l.id
                """,
                (course_id,),
            ).fetchall()
        return rows_to_dicts(rows)

    def is_lesson_unlocked(self, user_id: int, lesson_id: int) -> bool:
        lesson = self.get_lesson(lesson_id)
        if not lesson or not self.is_enrolled(user_id, lesson["course_id"]):
            return False

        ordered = self.course_lesson_order(lesson["course_id"])
        lesson_ids = [item["id"] for item in ordered]
        if not lesson_ids or lesson_id not in lesson_ids:
            return False
        index = lesson_ids.index(lesson_id)
        if index == 0:
            return True

        previous_id = lesson_ids[index - 1]
        with self.connection() as conn:
            row = conn.execute(
                """
                SELECT status FROM progress
                WHERE user_id = ? AND lesson_id = ? AND status = 'passed'
                """,
                (user_id, previous_id),
            ).fetchone()
        return bool(row)

    def get_course_structure(self, user_id: int, course_id: int) -> dict:
        course = self.get_course(course_id)
        if not course:
            raise ValueError("Kurs topilmadi")

        with self.connection() as conn:
            modules = rows_to_dicts(
                conn.execute(
                    "SELECT * FROM modules WHERE course_id = ? ORDER BY position, id",
                    (course_id,),
                ).fetchall()
            )
            progress_rows = rows_to_dicts(
                conn.execute(
                    """
                    SELECT p.*, l.module_id
                    FROM progress p
                    JOIN lessons l ON l.id = p.lesson_id
                    JOIN modules m ON m.id = l.module_id
                    WHERE p.user_id = ? AND m.course_id = ?
                    """,
                    (user_id, course_id),
                ).fetchall()
            )
            progress_map = {row["lesson_id"]: row for row in progress_rows}

            for module in modules:
                lessons = rows_to_dicts(
                    conn.execute(
                        "SELECT * FROM lessons WHERE module_id = ? ORDER BY position, id",
                        (module["id"],),
                    ).fetchall()
                )
                for lesson in lessons:
                    lesson["unlocked"] = self.is_lesson_unlocked(user_id, lesson["id"])
                    lesson["progress"] = progress_map.get(lesson["id"])
                module["lessons"] = lessons
                module["unlocked"] = any(lesson["unlocked"] for lesson in lessons)

        return {"course": course, "modules": modules}

    def get_questions(self, lesson_id: int, include_correct: bool = False) -> list[dict]:
        with self.connection() as conn:
            rows = conn.execute(
                """
                SELECT * FROM questions
                WHERE lesson_id = ?
                ORDER BY position, id
                """,
                (lesson_id,),
            ).fetchall()

        questions = []
        for row in rows:
            item = {
                "id": row["id"],
                "text": row["text"],
                "position": row["position"],
                "options": {
                    "A": row["option_a"],
                    "B": row["option_b"],
                    "C": row["option_c"],
                    "D": row["option_d"],
                },
            }
            if include_correct:
                item["correct_option"] = row["correct_option"]
                item["explanation"] = row["explanation"]
            questions.append(item)
        return questions

    def create_test_token(self, user_id: int, lesson_id: int) -> str:
        if not self.is_lesson_unlocked(user_id, lesson_id):
            raise ValueError("Bu dars hali ochilmagan")

        user = self.get_user(user_id) or {"full_name": "", "phone": ""}
        expires_at_dt = datetime.now(timezone.utc) + timedelta(hours=2)
        token = create_signed_token(
            "test",
            {
                "user_id": user_id,
                "lesson_id": lesson_id,
                "full_name": user.get("full_name", ""),
                "phone": user.get("phone", ""),
            },
            expires_at_dt,
        )
        expires_at = expires_at_dt.isoformat()
        with self.connection() as conn:
            conn.execute(
                """
                INSERT INTO test_tokens (token, user_id, lesson_id, expires_at, created_at)
                VALUES (?, ?, ?, ?, ?)
                """,
                (token, user_id, lesson_id, expires_at, now_iso()),
            )
        return token

    def validate_token(self, token: str, lesson_slug: str | None = None) -> dict:
        with self.connection() as conn:
            row = conn.execute(
                """
                SELECT t.*, l.slug, l.title AS lesson_title, l.duration_minutes, l.pass_percent,
                       m.title AS module_title, c.title AS course_title, c.id AS course_id,
                       u.full_name, u.phone
                FROM test_tokens t
                JOIN lessons l ON l.id = t.lesson_id
                JOIN modules m ON m.id = l.module_id
                JOIN courses c ON c.id = m.course_id
                JOIN users u ON u.telegram_id = t.user_id
                WHERE t.token = ?
                """,
                (token,),
            ).fetchone()

        if not row:
            body = read_signed_token(token, "test")
            claims = dict(body.get("payload") or {})
            user_id = int(claims.get("user_id") or 0)
            lesson_id = int(claims.get("lesson_id") or 0)
            lesson = self.get_lesson(lesson_id)
            if not user_id or not lesson:
                raise ValueError("Test token topilmadi")
            if lesson_slug and lesson["slug"] != lesson_slug:
                raise ValueError("Test link boshqa dars uchun berilgan")

            full_name = str(claims.get("full_name") or "Telegram foydalanuvchi").strip()
            phone = str(claims.get("phone") or "-").strip()
            with self.connection() as conn:
                existing_user = conn.execute("SELECT * FROM users WHERE telegram_id = ?", (user_id,)).fetchone()
                if not existing_user:
                    conn.execute(
                        """
                        INSERT INTO users (telegram_id, full_name, phone, registered_at)
                        VALUES (?, ?, ?, ?)
                        """,
                        (user_id, full_name, phone, now_iso()),
                    )
                else:
                    full_name = existing_user["full_name"]
                    phone = existing_user["phone"]
                conn.execute(
                    """
                    INSERT INTO enrollments (user_id, course_id, status, created_at)
                    VALUES (?, ?, 'active', ?)
                    ON CONFLICT(user_id, course_id) DO UPDATE SET status = 'active'
                    """,
                    (user_id, lesson["course_id"], now_iso()),
                )

            return {
                "token": token,
                "user_id": user_id,
                "lesson_id": lesson_id,
                "expires_at": datetime.fromtimestamp(int(body["exp"]), timezone.utc).isoformat(),
                "created_at": now_iso(),
                "slug": lesson["slug"],
                "lesson_title": lesson["title"],
                "duration_minutes": lesson["duration_minutes"],
                "pass_percent": lesson["pass_percent"],
                "module_title": lesson["module_title"],
                "course_title": lesson["course_title"],
                "course_id": lesson["course_id"],
                "full_name": full_name,
                "phone": phone,
            }

        data = dict(row)
        expires_at = datetime.fromisoformat(data["expires_at"])
        if expires_at < datetime.now(timezone.utc):
            raise ValueError("Test link muddati tugagan")
        if lesson_slug and data["slug"] != lesson_slug:
            raise ValueError("Test link boshqa dars uchun berilgan")
        if not self.is_lesson_unlocked(data["user_id"], data["lesson_id"]):
            raise ValueError("Bu dars hali ochilmagan")
        return data

    def get_test_payload(self, token: str, lesson_slug: str) -> dict:
        token_data = self.validate_token(token, lesson_slug)
        questions = self.get_questions(token_data["lesson_id"], include_correct=False)
        return {
            "user": {
                "telegram_id": token_data["user_id"],
                "full_name": token_data["full_name"],
                "phone": token_data["phone"],
            },
            "course": {"id": token_data["course_id"], "title": token_data["course_title"]},
            "module": {"title": token_data["module_title"]},
            "lesson": {
                "id": token_data["lesson_id"],
                "slug": token_data["slug"],
                "title": token_data["lesson_title"],
                "duration_minutes": token_data["duration_minutes"],
                "pass_percent": token_data["pass_percent"],
            },
            "questions": questions,
        }

    def grade_result(self, token: str, answers: dict[str, str]) -> dict:
        token_data = self.validate_token(token)
        lesson_id = int(token_data["lesson_id"])
        user_id = int(token_data["user_id"])
        questions = self.get_questions(lesson_id, include_correct=True)
        if not questions:
            raise ValueError("Bu dars uchun savollar topilmadi")

        answer_map = {str(key): str(value).upper() for key, value in answers.items()}
        correct_count = 0
        details = []
        for question in questions:
            selected = answer_map.get(str(question["id"]), "")
            if selected not in {"A", "B", "C", "D"}:
                selected = ""
            is_correct = selected == question["correct_option"]
            correct_count += 1 if is_correct else 0
            details.append(
                {
                    "id": question["id"],
                    "position": question["position"],
                    "text": question["text"],
                    "options": question["options"],
                    "selected": selected,
                    "selected_text": question["options"].get(selected, "") if selected else "",
                    "correct": question["correct_option"],
                    "correct_text": question["options"][question["correct_option"]],
                    "is_correct": is_correct,
                    "explanation": question["explanation"],
                }
            )

        total_count = len(questions)
        unanswered_count = sum(1 for detail in details if not detail["selected"])
        wrong_count = sum(1 for detail in details if detail["selected"] and not detail["is_correct"])
        percent = round(correct_count * 100 / total_count)
        passed = percent >= int(token_data["pass_percent"])
        timestamp = now_iso()

        with self.connection() as conn:
            conn.execute(
                """
                INSERT INTO results
                    (user_id, lesson_id, correct_count, total_count, percent, passed, created_at)
                VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                (user_id, lesson_id, correct_count, total_count, percent, int(passed), timestamp),
            )
            existing = conn.execute(
                "SELECT * FROM progress WHERE user_id = ? AND lesson_id = ?",
                (user_id, lesson_id),
            ).fetchone()
            best_percent = max(percent, existing["best_percent"] if existing else 0)
            status = "passed" if passed or (existing and existing["status"] == "passed") else "failed"
            passed_at = timestamp if passed else (existing["passed_at"] if existing else None)
            conn.execute(
                """
                INSERT INTO progress (user_id, lesson_id, status, best_percent, passed_at, updated_at)
                VALUES (?, ?, ?, ?, ?, ?)
                ON CONFLICT(user_id, lesson_id) DO UPDATE SET
                    status = excluded.status,
                    best_percent = excluded.best_percent,
                    passed_at = COALESCE(excluded.passed_at, progress.passed_at),
                    updated_at = excluded.updated_at
                """,
                (user_id, lesson_id, status, best_percent, passed_at, timestamp),
            )

        next_lesson = self.next_lesson_after(lesson_id) if passed else None
        course_completed = passed and next_lesson is None and self.is_course_completed(user_id, token_data["course_id"])
        return {
            "user_id": user_id,
            "lesson_id": lesson_id,
            "lesson_title": token_data["lesson_title"],
            "correct_count": correct_count,
            "wrong_count": wrong_count,
            "unanswered_count": unanswered_count,
            "total_count": total_count,
            "percent": percent,
            "passed": passed,
            "pass_percent": token_data["pass_percent"],
            "next_lesson": next_lesson,
            "course_completed": course_completed,
            "details": details,
        }

    def next_lesson_after(self, lesson_id: int) -> dict | None:
        lesson = self.get_lesson(lesson_id)
        if not lesson:
            return None
        ordered = self.course_lesson_order(lesson["course_id"])
        ids = [item["id"] for item in ordered]
        if lesson_id not in ids:
            return None
        index = ids.index(lesson_id)
        if index + 1 >= len(ordered):
            return None
        return ordered[index + 1]

    def is_course_completed(self, user_id: int, course_id: int) -> bool:
        ordered = self.course_lesson_order(course_id)
        if not ordered:
            return False
        with self.connection() as conn:
            rows = conn.execute(
                """
                SELECT lesson_id FROM progress
                WHERE user_id = ? AND status = 'passed'
                """,
                (user_id,),
            ).fetchall()
        passed_ids = {row["lesson_id"] for row in rows}
        return all(item["id"] in passed_ids for item in ordered)

    def list_results(self, user_id: int) -> list[dict]:
        with self.connection() as conn:
            rows = conn.execute(
                """
                SELECT r.*, l.title AS lesson_title, c.title AS course_title
                FROM results r
                JOIN lessons l ON l.id = r.lesson_id
                JOIN modules m ON m.id = l.module_id
                JOIN courses c ON c.id = m.course_id
                WHERE r.user_id = ?
                ORDER BY r.created_at DESC
                LIMIT 20
                """,
                (user_id,),
            ).fetchall()
        return rows_to_dicts(rows)

    def profile_stats(self, user_id: int) -> dict:
        with self.connection() as conn:
            enrollments = conn.execute(
                "SELECT COUNT(*) FROM enrollments WHERE user_id = ? AND status = 'active'",
                (user_id,),
            ).fetchone()[0]
            passed = conn.execute(
                "SELECT COUNT(*) FROM progress WHERE user_id = ? AND status = 'passed'",
                (user_id,),
            ).fetchone()[0]
            avg = conn.execute(
                "SELECT ROUND(AVG(percent)) FROM results WHERE user_id = ?",
                (user_id,),
            ).fetchone()[0]
        return {"enrollments": enrollments, "passed_lessons": passed, "average_percent": int(avg or 0)}

    def admin_summary(self) -> dict:
        with self.connection() as conn:
            return {
                "courses": conn.execute("SELECT COUNT(*) FROM courses").fetchone()[0],
                "modules": conn.execute("SELECT COUNT(*) FROM modules").fetchone()[0],
                "lessons": conn.execute("SELECT COUNT(*) FROM lessons").fetchone()[0],
                "questions": conn.execute("SELECT COUNT(*) FROM questions").fetchone()[0],
                "students": conn.execute("SELECT COUNT(*) FROM users").fetchone()[0],
                "payments": conn.execute("SELECT COUNT(*) FROM payments").fetchone()[0],
                "results": conn.execute("SELECT COUNT(*) FROM results").fetchone()[0],
            }

    def admin_courses(self) -> list[dict]:
        with self.connection() as conn:
            courses = rows_to_dicts(conn.execute("SELECT * FROM courses ORDER BY id").fetchall())
            modules = rows_to_dicts(conn.execute("SELECT * FROM modules ORDER BY position, id").fetchall())
            lessons = rows_to_dicts(conn.execute("SELECT * FROM lessons ORDER BY position, id").fetchall())
            question_counts = rows_to_dicts(
                conn.execute("SELECT lesson_id, COUNT(*) AS total FROM questions GROUP BY lesson_id").fetchall()
            )
        counts = {item["lesson_id"]: item["total"] for item in question_counts}
        lessons_by_module: dict[int, list[dict]] = {}
        for lesson in lessons:
            lesson["question_count"] = counts.get(lesson["id"], 0)
            lessons_by_module.setdefault(lesson["module_id"], []).append(lesson)
        modules_by_course: dict[int, list[dict]] = {}
        for module in modules:
            module["lessons"] = lessons_by_module.get(module["id"], [])
            modules_by_course.setdefault(module["course_id"], []).append(module)
        for course in courses:
            course["modules"] = modules_by_course.get(course["id"], [])
        return courses

    def admin_students(self) -> list[dict]:
        with self.connection() as conn:
            rows = conn.execute(
                """
                SELECT u.*,
                    COUNT(DISTINCT e.course_id) AS courses_count,
                    COUNT(DISTINCT CASE WHEN p.status = 'passed' THEN p.lesson_id END) AS passed_lessons
                FROM users u
                LEFT JOIN enrollments e ON e.user_id = u.telegram_id
                LEFT JOIN progress p ON p.user_id = u.telegram_id
                GROUP BY u.telegram_id
                ORDER BY u.registered_at DESC
                """
            ).fetchall()
        return rows_to_dicts(rows)

    def admin_payments(self) -> list[dict]:
        with self.connection() as conn:
            rows = conn.execute(
                """
                SELECT p.*, u.full_name, c.title AS course_title
                FROM payments p
                JOIN users u ON u.telegram_id = p.user_id
                JOIN courses c ON c.id = p.course_id
                ORDER BY p.created_at DESC
                LIMIT 100
                """
            ).fetchall()
        return rows_to_dicts(rows)

    def admin_results(self) -> list[dict]:
        with self.connection() as conn:
            rows = conn.execute(
                """
                SELECT r.*, u.full_name, l.title AS lesson_title, c.title AS course_title
                FROM results r
                JOIN users u ON u.telegram_id = r.user_id
                JOIN lessons l ON l.id = r.lesson_id
                JOIN modules m ON m.id = l.module_id
                JOIN courses c ON c.id = m.course_id
                ORDER BY r.created_at DESC
                LIMIT 100
                """
            ).fetchall()
        return rows_to_dicts(rows)

    def admin_create_course(self, data: dict) -> dict:
        title = str(data.get("title", "")).strip()
        price = int(data.get("price") or 0)
        description = str(data.get("description", "")).strip()
        if not title or price <= 0:
            raise ValueError("Kurs nomi va narxi to'g'ri kiritilishi kerak")
        base_slug = slugify(title)
        slug = base_slug
        with self.connection() as conn:
            counter = 2
            while conn.execute("SELECT 1 FROM courses WHERE slug = ?", (slug,)).fetchone():
                slug = f"{base_slug}-{counter}"
                counter += 1
            cursor = conn.execute(
                """
                INSERT INTO courses (slug, title, price, description, created_at)
                VALUES (?, ?, ?, ?, ?)
                """,
                (slug, title, price, description, now_iso()),
            )
        return self.get_course(int(cursor.lastrowid))

    def admin_create_module(self, data: dict) -> dict:
        course_id = int(data.get("course_id") or 0)
        title = str(data.get("title", "")).strip()
        position = int(data.get("position") or 1)
        if not course_id or not title:
            raise ValueError("Kurs va modul nomi kiritilishi kerak")
        with self.connection() as conn:
            cursor = conn.execute(
                """
                INSERT INTO modules (course_id, title, position, created_at)
                VALUES (?, ?, ?, ?)
                """,
                (course_id, title, position, now_iso()),
            )
            row = conn.execute("SELECT * FROM modules WHERE id = ?", (cursor.lastrowid,)).fetchone()
        return dict(row)

    def admin_create_lesson(self, data: dict) -> dict:
        module_id = int(data.get("module_id") or 0)
        title = str(data.get("title", "")).strip()
        position = int(data.get("position") or 1)
        duration = int(data.get("duration_minutes") or 30)
        pass_percent = int(data.get("pass_percent") or 80)
        video_url = str(data.get("video_url", "")).strip()
        if not module_id or not title:
            raise ValueError("Modul va dars nomi kiritilishi kerak")
        base_slug = slugify(title)
        slug = base_slug
        with self.connection() as conn:
            counter = 2
            while conn.execute("SELECT 1 FROM lessons WHERE slug = ?", (slug,)).fetchone():
                slug = f"{base_slug}-{counter}"
                counter += 1
            cursor = conn.execute(
                """
                INSERT INTO lessons
                    (module_id, slug, title, position, video_url, duration_minutes, pass_percent, created_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (module_id, slug, title, position, video_url, duration, pass_percent, now_iso()),
            )
            row = conn.execute("SELECT * FROM lessons WHERE id = ?", (cursor.lastrowid,)).fetchone()
        return dict(row)

    def admin_create_question(self, data: dict) -> dict:
        lesson_id = int(data.get("lesson_id") or 0)
        text = str(data.get("text", "")).strip()
        correct = str(data.get("correct_option", "")).strip().upper()
        options = data.get("options") or {}
        option_a = str(data.get("option_a") or options.get("A") or "").strip()
        option_b = str(data.get("option_b") or options.get("B") or "").strip()
        option_c = str(data.get("option_c") or options.get("C") or "").strip()
        option_d = str(data.get("option_d") or options.get("D") or "").strip()
        explanation = str(data.get("explanation", "")).strip()
        position = int(data.get("position") or 1)
        if not lesson_id or not text or correct not in {"A", "B", "C", "D"}:
            raise ValueError("Savol, dars va to'g'ri javob kiritilishi kerak")
        if not all([option_a, option_b, option_c, option_d]):
            raise ValueError("A, B, C, D variantlari to'liq kiritilishi kerak")
        with self.connection() as conn:
            cursor = conn.execute(
                """
                INSERT INTO questions
                    (lesson_id, text, option_a, option_b, option_c, option_d, correct_option, explanation, position, created_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (lesson_id, text, option_a, option_b, option_c, option_d, correct, explanation, position, now_iso()),
            )
            row = conn.execute("SELECT * FROM questions WHERE id = ?", (cursor.lastrowid,)).fetchone()
        return dict(row)


class MongoDatabase:
    def __init__(self, _path: Path | None = None):
        try:
            from pymongo import ASCENDING, MongoClient, ReturnDocument
            from pymongo.errors import OperationFailure
        except ImportError as error:
            raise RuntimeError("MongoDB uchun `pymongo[srv]` dependency o'rnatilishi kerak.") from error

        self.return_after = ReturnDocument.AFTER
        self.operation_failure = OperationFailure
        self.create_indexes = clean_env("MONGODB_CREATE_INDEXES", "1").lower() not in {"0", "false", "no"}
        database_url = clean_env("DATABASE_URL")
        self.uri = (
            clean_env("MONGODB_URI")
            or clean_env("MONGO_URI")
            or clean_env("MONGO_URL")
            or (database_url if database_url.startswith("mongodb") else "")
            or "mongodb://localhost:27017"
        )
        self.db_name = clean_env("MONGODB_DB", "fariks_lms")
        self.client = MongoClient(self.uri, serverSelectionTimeoutMS=8000)
        self.client.admin.command("ping")
        self.db = self.client[self.db_name]

        self.users = self.db.users
        self.user_states = self.db.user_states
        self.courses = self.db.courses
        self.modules = self.db.modules
        self.lessons = self.db.lessons
        self.questions = self.db.questions
        self.payments = self.db.payments
        self.enrollments = self.db.enrollments
        self.progress = self.db.progress
        self.test_tokens = self.db.test_tokens
        self.results = self.db.results
        self.counters = self.db.counters
        self.ASCENDING = ASCENDING

    def _clean(self, doc: dict | None) -> dict | None:
        if not doc:
            return None
        item = dict(doc)
        item.pop("_id", None)
        return item

    def _clean_list(self, docs) -> list[dict]:
        return [self._clean(doc) for doc in docs]

    def _next_id(self, name: str) -> int:
        row = self.counters.find_one_and_update(
            {"_id": name},
            {"$inc": {"seq": 1}},
            upsert=True,
            return_document=self.return_after,
        )
        return int(row["seq"])

    def _sync_counter(self, name: str, collection) -> None:
        row = collection.find_one(sort=[("id", -1)])
        max_id = int(row["id"]) if row else 0
        self.counters.update_one({"_id": name}, {"$max": {"seq": max_id}}, upsert=True)

    def _unique_slug(self, collection, title: str) -> str:
        base_slug = slugify(title)
        candidate = base_slug
        counter = 2
        while collection.find_one({"slug": candidate}):
            candidate = f"{base_slug}-{counter}"
            counter += 1
        return candidate

    def _create_index(self, collection, keys, **kwargs) -> bool:
        if not self.create_indexes:
            return False
        try:
            collection.create_index(keys, **kwargs)
            return True
        except self.operation_failure as error:
            message = str(error)
            is_disk_limit = (
                getattr(error, "code", None) == 14031
                or "OutOfDiskSpace" in message
                or "available disk space" in message
            )
            if not is_disk_limit:
                raise
            self.create_indexes = False
            print(
                "MongoDB index yaratish o'tkazib yuborildi: server disk joyi kam. "
                "Deploy davom etadi, lekin MONGODB_URI uchun Atlas yoki kattaroq storage ishlating."
            )
            return False

    def init_schema(self) -> None:
        indexes = [
            (self.users, [("telegram_id", self.ASCENDING)], {"unique": True}),
            (self.user_states, [("telegram_id", self.ASCENDING)], {"unique": True}),
            (self.courses, [("id", self.ASCENDING)], {"unique": True}),
            (self.courses, [("slug", self.ASCENDING)], {"unique": True}),
            (self.modules, [("id", self.ASCENDING)], {"unique": True}),
            (self.modules, [("course_id", self.ASCENDING), ("position", self.ASCENDING)], {}),
            (self.lessons, [("id", self.ASCENDING)], {"unique": True}),
            (self.lessons, [("slug", self.ASCENDING)], {"unique": True}),
            (self.lessons, [("module_id", self.ASCENDING), ("position", self.ASCENDING)], {}),
            (self.questions, [("id", self.ASCENDING)], {"unique": True}),
            (self.questions, [("lesson_id", self.ASCENDING), ("position", self.ASCENDING)], {}),
            (self.payments, [("id", self.ASCENDING)], {"unique": True}),
            (self.payments, [("user_id", self.ASCENDING), ("created_at", self.ASCENDING)], {}),
            (self.enrollments, [("user_id", self.ASCENDING), ("course_id", self.ASCENDING)], {"unique": True}),
            (self.progress, [("user_id", self.ASCENDING), ("lesson_id", self.ASCENDING)], {"unique": True}),
            (self.test_tokens, [("token", self.ASCENDING)], {"unique": True}),
            (self.results, [("id", self.ASCENDING)], {"unique": True}),
            (self.results, [("user_id", self.ASCENDING), ("created_at", self.ASCENDING)], {}),
        ]
        for collection, keys, options in indexes:
            if not self._create_index(collection, keys, **options):
                break
        for name, collection in [
            ("courses", self.courses),
            ("modules", self.modules),
            ("lessons", self.lessons),
            ("questions", self.questions),
            ("payments", self.payments),
            ("results", self.results),
        ]:
            self._sync_counter(name, collection)

    def seed(self) -> None:
        if self.courses.count_documents({}):
            return

        def add_course(title: str, price: int, description: str) -> int:
            course_id = self._next_id("courses")
            self.courses.insert_one(
                {
                    "_id": course_id,
                    "id": course_id,
                    "slug": self._unique_slug(self.courses, title),
                    "title": title,
                    "price": price,
                    "description": description,
                    "created_at": now_iso(),
                }
            )
            return course_id

        def add_module(course_id: int, title: str, position: int) -> int:
            module_id = self._next_id("modules")
            self.modules.insert_one(
                {
                    "_id": module_id,
                    "id": module_id,
                    "course_id": course_id,
                    "title": title,
                    "position": position,
                    "created_at": now_iso(),
                }
            )
            return module_id

        def add_lesson(module_id: int, title: str, position: int, video_url: str = "") -> int:
            lesson_id = self._next_id("lessons")
            self.lessons.insert_one(
                {
                    "_id": lesson_id,
                    "id": lesson_id,
                    "module_id": module_id,
                    "slug": self._unique_slug(self.lessons, title),
                    "title": title,
                    "position": position,
                    "video_url": video_url,
                    "duration_minutes": 30,
                    "pass_percent": 80,
                    "created_at": now_iso(),
                }
            )
            return lesson_id

        def add_questions(lesson_id: int, questions: list[tuple[str, str, str, str, str, str, str]]) -> None:
            docs = []
            for index, question in enumerate(questions, start=1):
                question_id = self._next_id("questions")
                text, option_a, option_b, option_c, option_d, correct_option, explanation = question
                docs.append(
                    {
                        "_id": question_id,
                        "id": question_id,
                        "lesson_id": lesson_id,
                        "text": text,
                        "option_a": option_a,
                        "option_b": option_b,
                        "option_c": option_c,
                        "option_d": option_d,
                        "correct_option": correct_option,
                        "explanation": explanation,
                        "position": index,
                        "created_at": now_iso(),
                    }
                )
            if docs:
                self.questions.insert_many(docs)

        national = add_course(
            "Milliy Sertifikat Matematika",
            300_000,
            "Milliy sertifikat imtihonlari uchun bosqichma-bosqich matematika kursi.",
        )
        attestation = add_course(
            "Attestatsiya Matematika",
            250_000,
            "Ustozlar attestatsiyasi uchun amaliy misollar va testlar.",
        )
        applicant = add_course(
            "Abituriyent Matematika",
            350_000,
            "DTM va oliy ta'lim kirish imtihonlari uchun matematika tayyorlov kursi.",
        )

        module_1 = add_module(national, "1-MODUL: Algebra asoslari", 1)
        module_2 = add_module(national, "2-MODUL: Trigonometriya", 2)
        module_3 = add_module(national, "3-MODUL: Geometriya", 3)

        lesson_1 = add_lesson(module_1, "1-Dars: Chiziqli tenglamalar", 1)
        lesson_2 = add_lesson(module_1, "2-Dars: Ildizli tenglamalar", 2)
        lesson_3 = add_lesson(module_1, "3-Dars: Logarifmlar", 3)
        add_lesson(module_2, "4-Dars: Trigonometrik ayniyatlar", 1)
        add_lesson(module_2, "5-Dars: Sinus va kosinus tenglamalar", 2)
        add_lesson(module_3, "6-Dars: Uchburchaklar", 1)

        add_questions(
            lesson_1,
            [
                (r"$2x+3=11$ tenglamani yeching.", r"$4$", r"$5$", r"$3$", r"$7$", "A", r"$2x=8$, demak $x=4$."),
                (r"$\frac{2x+3}{x-1}=5$ tenglamani yeching.", r"$2$", r"$3$", r"$\frac{8}{3}$", r"$-1$", "C", r"$2x+3=5x-5$, demak $x=\frac{8}{3}$."),
                (r"$3(x-2)=2x+5$ bo'lsa, $x$ nechaga teng?", r"$9$", r"$11$", r"$7$", r"$13$", "B", r"$3x-6=2x+5$, demak $x=11$."),
                (r"$5x-7=2x+14$ tenglama ildizini toping.", r"$7$", r"$5$", r"$6$", r"$9$", "A", r"$3x=21$, demak $x=7$."),
                (r"$4-2x=10$ tenglamaning yechimi qaysi?", r"$3$", r"$-2$", r"$-3$", r"$7$", "C", r"$-2x=6$, demak $x=-3$."),
                (r"$\frac{x+4}{3}=5$ tenglamada $x$ ni toping.", r"$10$", r"$11$", r"$12$", r"$9$", "B", r"$x+4=15$, demak $x=11$."),
                (r"$7x+1=3x+17$ bo'lsa, $x$ nechaga teng?", r"$2$", r"$3$", r"$4$", r"$5$", "C", r"$4x=16$, demak $x=4$."),
                (r"$2(x+5)-3=15$ tenglamani yeching.", r"$4$", r"$6$", r"$8$", r"$9$", "A", r"$2x+7=15$, demak $x=4$."),
                (r"$\frac{x}{2}+\frac{x}{3}=10$ tenglama ildizini toping.", r"$10$", r"$12$", r"$14$", r"$16$", "B", r"$\frac{5x}{6}=10$, demak $x=12$."),
                (r"$0.2x+3=7$ bo'lsa, $x$ nechaga teng?", r"$10$", r"$15$", r"$20$", r"$25$", "C", r"$0.2x=4$, demak $x=20$."),
                (r"$|x-3|=5$ tenglama yechimlari qaysi?", r"$8$ va $-2$", r"$5$ va $-5$", r"$3$ va $5$", r"$2$ va $8$", "A", r"$x-3=5$ yoki $x-3=-5$."),
                (r"$6-(x+1)=2x-4$ tenglamani yeching.", r"$2$", r"$3$", r"$4$", r"$5$", "B", r"$5-x=2x-4$, demak $3x=9$."),
                (r"$\frac{2}{3}x-4=6$ bo'lsa, $x$ nechaga teng?", r"$12$", r"$15$", r"$18$", r"$21$", "B", r"$\frac{2}{3}x=10$, demak $x=15$."),
                (r"$5(x-1)=2(2x+3)$ tenglama ildizini toping.", r"$9$", r"$10$", r"$11$", r"$12$", "C", r"$5x-5=4x+6$, demak $x=11$."),
                (r"$\frac{3x-1}{2}=7$ bo'lsa, $x$ nechaga teng?", r"$4$", r"$5$", r"$6$", r"$7$", "B", r"$3x-1=14$, demak $x=5$."),
                (r"$9x=3(x+8)$ tenglamani yeching.", r"$3$", r"$4$", r"$5$", r"$6$", "B", r"$9x=3x+24$, demak $x=4$."),
                (r"$x-(2x-5)=1$ tenglama yechimi qaysi?", r"$2$", r"$3$", r"$4$", r"$5$", "C", r"$-x+5=1$, demak $x=4$."),
                (r"$4(x+2)=2x+18$ tenglamani yeching.", r"$4$", r"$5$", r"$6$", r"$7$", "B", r"$4x+8=2x+18$, demak $x=5$."),
                (r"$\frac{x-2}{x+1}=\frac{1}{2}$ tenglama ildizini toping.", r"$3$", r"$4$", r"$5$", r"$6$", "C", r"$2x-4=x+1$, demak $x=5$."),
                (r"$3x+2=2(x+9)$ bo'lsa, $x$ nechaga teng?", r"$14$", r"$15$", r"$16$", r"$18$", "C", r"$3x+2=2x+18$, demak $x=16$."),
            ],
        )

        short_questions = [
            (r"$\sqrt{x+4}=5$ tenglamani yeching.", r"$19$", r"$20$", r"$21$", r"$22$", "C", r"$x+4=25$, demak $x=21$."),
            (r"$\sqrt{x-1}=4$ bo'lsa, $x$ nechaga teng?", r"$15$", r"$16$", r"$17$", r"$18$", "C", r"$x-1=16$, demak $x=17$."),
            (r"$\sqrt{x+4}+\sqrt{x-1}=5$ uchun mos yechimni toping.", r"$1$", r"$5$", r"$10$", r"$13$", "B", r"$x=5$ bo'lsa, $3+2=5$."),
            (r"$\sqrt{2x+1}=3$ tenglama ildizi qaysi?", r"$3$", r"$4$", r"$5$", r"$6$", "B", r"$2x+1=9$, demak $x=4$."),
            (r"$\sqrt{x}=7$ bo'lsa, $x$ nechaga teng?", r"$14$", r"$21$", r"$42$", r"$49$", "D", r"$x=49$."),
        ]
        add_questions(lesson_2, short_questions)
        add_questions(
            lesson_3,
            [
                (r"$\log_2 x=5$ bo'lsa, $x$ nechaga teng?", r"$10$", r"$16$", r"$25$", r"$32$", "D", r"$x=2^5=32$."),
                (r"$\log_3 81$ qiymatini toping.", r"$3$", r"$4$", r"$5$", r"$6$", "B", r"$3^4=81$."),
                (r"$\log_{10} 1000$ qiymati qaysi?", r"$2$", r"$3$", r"$4$", r"$10$", "B", r"$10^3=1000$."),
                (r"$\log_5 25+\log_2 8$ ni hisoblang.", r"$4$", r"$5$", r"$6$", r"$7$", "B", r"$2+3=5$."),
                (r"$\log_4 16$ qiymatini toping.", r"$2$", r"$3$", r"$4$", r"$8$", "A", r"$4^2=16$."),
            ],
        )

        for course_id, label in [(attestation, "Attestatsiya"), (applicant, "Abituriyent")]:
            module = add_module(course_id, "1-MODUL: Boshlang'ich testlar", 1)
            lesson = add_lesson(module, f"1-Dars: {label} kirish testi", 1)
            add_questions(lesson, short_questions)

    def set_state(self, telegram_id: int, state: str, payload: dict | None = None) -> None:
        self.user_states.update_one(
            {"_id": telegram_id},
            {"$set": {"telegram_id": telegram_id, "state": state, "payload": payload or {}, "updated_at": now_iso()}},
            upsert=True,
        )

    def get_state(self, telegram_id: int) -> tuple[str | None, dict]:
        row = self.user_states.find_one({"_id": telegram_id})
        if not row:
            return None, {}
        payload = row.get("payload") or {}
        if isinstance(payload, str):
            payload = json.loads(payload or "{}")
        return row.get("state"), payload

    def clear_state(self, telegram_id: int) -> None:
        self.user_states.delete_one({"_id": telegram_id})

    def register_user(self, telegram_id: int, full_name: str, phone: str) -> None:
        self.users.update_one(
            {"_id": telegram_id},
            {
                "$set": {"telegram_id": telegram_id, "full_name": full_name, "phone": phone},
                "$setOnInsert": {"registered_at": now_iso()},
            },
            upsert=True,
        )
        self.clear_state(telegram_id)

    def get_user(self, telegram_id: int) -> dict | None:
        return self._clean(self.users.find_one({"_id": telegram_id}))

    def list_courses(self) -> list[dict]:
        return self._clean_list(self.courses.find().sort("id", 1))

    def get_course(self, course_id: int) -> dict | None:
        return self._clean(self.courses.find_one({"id": int(course_id)}))

    def list_enrollments(self, user_id: int) -> list[dict]:
        items = self._clean_list(self.enrollments.find({"user_id": user_id, "status": "active"}).sort("created_at", -1))
        for item in items:
            course = self.get_course(item["course_id"]) or {}
            item.update({"title": course.get("title", ""), "price": course.get("price", 0), "description": course.get("description", "")})
        return items

    def is_enrolled(self, user_id: int, course_id: int) -> bool:
        return bool(self.enrollments.find_one({"user_id": user_id, "course_id": course_id, "status": "active"}))

    def create_payment_and_enrollment(self, user_id: int, course_id: int, method: str) -> dict:
        course = self.get_course(course_id)
        if not course:
            raise ValueError("Kurs topilmadi")
        payment_id = self._next_id("payments")
        self.payments.insert_one(
            {
                "_id": payment_id,
                "id": payment_id,
                "user_id": user_id,
                "course_id": course_id,
                "method": method,
                "amount": course["price"],
                "status": "confirmed",
                "created_at": now_iso(),
            }
        )
        self.enrollments.update_one(
            {"_id": f"{user_id}:{course_id}"},
            {"$set": {"user_id": user_id, "course_id": course_id, "status": "active"}, "$setOnInsert": {"created_at": now_iso()}},
            upsert=True,
        )
        return {"payment_id": payment_id, "course": course}

    def list_payments(self, user_id: int) -> list[dict]:
        items = self._clean_list(self.payments.find({"user_id": user_id}).sort("created_at", -1))
        for item in items:
            course = self.get_course(item["course_id"]) or {}
            item["course_title"] = course.get("title", "")
        return items

    def get_module(self, module_id: int) -> dict | None:
        return self._clean(self.modules.find_one({"id": int(module_id)}))

    def list_module_lessons(self, module_id: int) -> list[dict]:
        return self._clean_list(self.lessons.find({"module_id": int(module_id)}).sort([("position", 1), ("id", 1)]))

    def get_lesson(self, lesson_id: int) -> dict | None:
        lesson = self._clean(self.lessons.find_one({"id": int(lesson_id)}))
        if not lesson:
            return None
        module = self.get_module(lesson["module_id"]) or {}
        course = self.get_course(module.get("course_id", 0)) or {}
        lesson["course_id"] = module.get("course_id")
        lesson["module_title"] = module.get("title", "")
        lesson["course_title"] = course.get("title", "")
        return lesson

    def get_lesson_by_slug(self, slug: str) -> dict | None:
        lesson = self._clean(self.lessons.find_one({"slug": slug}))
        return self.get_lesson(lesson["id"]) if lesson else None

    def course_lesson_order(self, course_id: int) -> list[dict]:
        modules = self._clean_list(self.modules.find({"course_id": int(course_id)}).sort([("position", 1), ("id", 1)]))
        ordered = []
        for module in modules:
            lessons = self._clean_list(self.lessons.find({"module_id": module["id"]}).sort([("position", 1), ("id", 1)]))
            for lesson in lessons:
                lesson["course_id"] = course_id
                lesson["module_title"] = module["title"]
                lesson["module_position"] = module["position"]
                ordered.append(lesson)
        return ordered

    def profile_lesson_progress(self, user_id: int, lesson_id: int) -> dict | None:
        return self._clean(self.progress.find_one({"_id": f"{user_id}:{lesson_id}"}))

    def is_lesson_unlocked(self, user_id: int, lesson_id: int) -> bool:
        lesson = self.get_lesson(lesson_id)
        if not lesson or not self.is_enrolled(user_id, lesson["course_id"]):
            return False
        ordered = self.course_lesson_order(lesson["course_id"])
        lesson_ids = [item["id"] for item in ordered]
        if lesson_id not in lesson_ids:
            return False
        index = lesson_ids.index(lesson_id)
        if index == 0:
            return True
        previous_id = lesson_ids[index - 1]
        previous = self.profile_lesson_progress(user_id, previous_id)
        return bool(previous and previous.get("status") == "passed")

    def get_course_structure(self, user_id: int, course_id: int) -> dict:
        course = self.get_course(course_id)
        if not course:
            raise ValueError("Kurs topilmadi")
        modules = self._clean_list(self.modules.find({"course_id": course_id}).sort([("position", 1), ("id", 1)]))
        progress_rows = self._clean_list(self.progress.find({"user_id": user_id}))
        progress_map = {row["lesson_id"]: row for row in progress_rows}
        for module in modules:
            lessons = self.list_module_lessons(module["id"])
            for lesson in lessons:
                lesson["unlocked"] = self.is_lesson_unlocked(user_id, lesson["id"])
                lesson["progress"] = progress_map.get(lesson["id"])
            module["lessons"] = lessons
            module["unlocked"] = any(lesson["unlocked"] for lesson in lessons)
        return {"course": course, "modules": modules}

    def get_questions(self, lesson_id: int, include_correct: bool = False) -> list[dict]:
        rows = self._clean_list(self.questions.find({"lesson_id": int(lesson_id)}).sort([("position", 1), ("id", 1)]))
        questions = []
        for row in rows:
            item = {
                "id": row["id"],
                "text": row["text"],
                "image_data": row.get("image_data", ""),
                "position": row["position"],
                "options": {"A": row["option_a"], "B": row["option_b"], "C": row["option_c"], "D": row["option_d"]},
            }
            if include_correct:
                item["correct_option"] = row["correct_option"]
                item["explanation"] = row.get("explanation", "")
            questions.append(item)
        return questions

    def create_test_token(self, user_id: int, lesson_id: int) -> str:
        if not self.is_lesson_unlocked(user_id, lesson_id):
            raise ValueError("Bu dars hali ochilmagan")
        user = self.get_user(user_id) or {"full_name": "", "phone": ""}
        expires_at_dt = datetime.now(timezone.utc) + timedelta(hours=2)
        token = create_signed_token(
            "test",
            {"user_id": user_id, "lesson_id": lesson_id, "full_name": user.get("full_name", ""), "phone": user.get("phone", "")},
            expires_at_dt,
        )
        self.test_tokens.update_one(
            {"_id": token},
            {"$set": {"token": token, "user_id": user_id, "lesson_id": lesson_id, "expires_at": expires_at_dt.isoformat(), "created_at": now_iso()}},
            upsert=True,
        )
        return token

    def validate_token(self, token: str, lesson_slug: str | None = None) -> dict:
        row = self._clean(self.test_tokens.find_one({"_id": token}))
        if row:
            lesson = self.get_lesson(row["lesson_id"])
            user = self.get_user(row["user_id"])
        else:
            body = read_signed_token(token, "test")
            claims = dict(body.get("payload") or {})
            user_id = int(claims.get("user_id") or 0)
            lesson_id = int(claims.get("lesson_id") or 0)
            lesson = self.get_lesson(lesson_id)
            if not user_id or not lesson:
                raise ValueError("Test token topilmadi")
            full_name = str(claims.get("full_name") or "Telegram foydalanuvchi").strip()
            phone = str(claims.get("phone") or "-").strip()
            user = self.get_user(user_id)
            if not user:
                self.register_user(user_id, full_name, phone)
                user = self.get_user(user_id)
            self.enrollments.update_one(
                {"_id": f"{user_id}:{lesson['course_id']}"},
                {"$set": {"user_id": user_id, "course_id": lesson["course_id"], "status": "active"}, "$setOnInsert": {"created_at": now_iso()}},
                upsert=True,
            )
            row = {
                "token": token,
                "user_id": user_id,
                "lesson_id": lesson_id,
                "expires_at": datetime.fromtimestamp(int(body["exp"]), timezone.utc).isoformat(),
                "created_at": now_iso(),
            }

        if not lesson:
            raise ValueError("Test token topilmadi")
        expires_at = datetime.fromisoformat(row["expires_at"])
        if expires_at < datetime.now(timezone.utc):
            raise ValueError("Test link muddati tugagan")
        if lesson_slug and lesson["slug"] != lesson_slug:
            raise ValueError("Test link boshqa dars uchun berilgan")
        if not self.is_lesson_unlocked(row["user_id"], row["lesson_id"]):
            raise ValueError("Bu dars hali ochilmagan")
        user = user or {"full_name": "Telegram foydalanuvchi", "phone": "-"}
        return {
            **row,
            "slug": lesson["slug"],
            "lesson_title": lesson["title"],
            "duration_minutes": lesson["duration_minutes"],
            "pass_percent": lesson["pass_percent"],
            "module_title": lesson["module_title"],
            "course_title": lesson["course_title"],
            "course_id": lesson["course_id"],
            "full_name": user.get("full_name", ""),
            "phone": user.get("phone", ""),
        }

    def get_test_payload(self, token: str, lesson_slug: str) -> dict:
        token_data = self.validate_token(token, lesson_slug)
        questions = self.get_questions(token_data["lesson_id"], include_correct=False)
        return {
            "user": {"telegram_id": token_data["user_id"], "full_name": token_data["full_name"], "phone": token_data["phone"]},
            "course": {"id": token_data["course_id"], "title": token_data["course_title"]},
            "module": {"title": token_data["module_title"]},
            "lesson": {
                "id": token_data["lesson_id"],
                "slug": token_data["slug"],
                "title": token_data["lesson_title"],
                "duration_minutes": token_data["duration_minutes"],
                "pass_percent": token_data["pass_percent"],
            },
            "questions": questions,
        }

    def grade_result(self, token: str, answers: dict[str, str]) -> dict:
        token_data = self.validate_token(token)
        lesson_id = int(token_data["lesson_id"])
        user_id = int(token_data["user_id"])
        questions = self.get_questions(lesson_id, include_correct=True)
        if not questions:
            raise ValueError("Bu dars uchun savollar topilmadi")
        answer_map = {str(key): str(value).upper() for key, value in answers.items()}
        correct_count = 0
        details = []
        for question in questions:
            selected = answer_map.get(str(question["id"]), "")
            if selected not in {"A", "B", "C", "D"}:
                selected = ""
            is_correct = selected == question["correct_option"]
            correct_count += 1 if is_correct else 0
            details.append(
                {
                    "id": question["id"],
                    "position": question["position"],
                    "text": question["text"],
                    "image_data": question.get("image_data", ""),
                    "options": question["options"],
                    "selected": selected,
                    "selected_text": question["options"].get(selected, "") if selected else "",
                    "correct": question["correct_option"],
                    "correct_text": question["options"][question["correct_option"]],
                    "is_correct": is_correct,
                    "explanation": question["explanation"],
                }
            )
        total_count = len(questions)
        unanswered_count = sum(1 for detail in details if not detail["selected"])
        wrong_count = sum(1 for detail in details if detail["selected"] and not detail["is_correct"])
        percent = round(correct_count * 100 / total_count)
        passed = percent >= int(token_data["pass_percent"])
        timestamp = now_iso()
        result_id = self._next_id("results")
        self.results.insert_one(
            {
                "_id": result_id,
                "id": result_id,
                "user_id": user_id,
                "lesson_id": lesson_id,
                "correct_count": correct_count,
                "total_count": total_count,
                "percent": percent,
                "passed": int(passed),
                "created_at": timestamp,
            }
        )
        existing = self.profile_lesson_progress(user_id, lesson_id)
        best_percent = max(percent, existing["best_percent"] if existing else 0)
        status = "passed" if passed or (existing and existing.get("status") == "passed") else "failed"
        passed_at = timestamp if passed else (existing.get("passed_at") if existing else None)
        self.progress.update_one(
            {"_id": f"{user_id}:{lesson_id}"},
            {
                "$set": {
                    "user_id": user_id,
                    "lesson_id": lesson_id,
                    "status": status,
                    "best_percent": best_percent,
                    "passed_at": passed_at,
                    "updated_at": timestamp,
                }
            },
            upsert=True,
        )
        next_lesson = self.next_lesson_after(lesson_id) if passed else None
        course_completed = passed and next_lesson is None and self.is_course_completed(user_id, token_data["course_id"])
        return {
            "user_id": user_id,
            "lesson_id": lesson_id,
            "lesson_title": token_data["lesson_title"],
            "correct_count": correct_count,
            "wrong_count": wrong_count,
            "unanswered_count": unanswered_count,
            "total_count": total_count,
            "percent": percent,
            "passed": passed,
            "pass_percent": token_data["pass_percent"],
            "next_lesson": next_lesson,
            "course_completed": course_completed,
            "details": details,
        }

    def next_lesson_after(self, lesson_id: int) -> dict | None:
        lesson = self.get_lesson(lesson_id)
        if not lesson:
            return None
        ordered = self.course_lesson_order(lesson["course_id"])
        ids = [item["id"] for item in ordered]
        if lesson_id not in ids:
            return None
        index = ids.index(lesson_id)
        return ordered[index + 1] if index + 1 < len(ordered) else None

    def is_course_completed(self, user_id: int, course_id: int) -> bool:
        ordered = self.course_lesson_order(course_id)
        if not ordered:
            return False
        passed_ids = {row["lesson_id"] for row in self.progress.find({"user_id": user_id, "status": "passed"}, {"lesson_id": 1})}
        return all(item["id"] in passed_ids for item in ordered)

    def list_results(self, user_id: int) -> list[dict]:
        items = self._clean_list(self.results.find({"user_id": user_id}).sort("created_at", -1).limit(20))
        for item in items:
            lesson = self.get_lesson(item["lesson_id"]) or {}
            item["lesson_title"] = lesson.get("title", "")
            item["course_title"] = lesson.get("course_title", "")
        return items

    def profile_stats(self, user_id: int) -> dict:
        enrollments = self.enrollments.count_documents({"user_id": user_id, "status": "active"})
        passed = self.progress.count_documents({"user_id": user_id, "status": "passed"})
        rows = list(self.results.find({"user_id": user_id}, {"percent": 1}))
        avg = round(sum(row.get("percent", 0) for row in rows) / len(rows)) if rows else 0
        return {"enrollments": enrollments, "passed_lessons": passed, "average_percent": int(avg or 0)}

    def admin_summary(self) -> dict:
        return {
            "courses": self.courses.count_documents({}),
            "modules": self.modules.count_documents({}),
            "lessons": self.lessons.count_documents({}),
            "questions": self.questions.count_documents({}),
            "students": self.users.count_documents({}),
            "payments": self.payments.count_documents({}),
            "results": self.results.count_documents({}),
        }

    def admin_courses(self) -> list[dict]:
        courses = self.list_courses()
        modules = self._clean_list(self.modules.find().sort([("position", 1), ("id", 1)]))
        lessons = self._clean_list(self.lessons.find().sort([("position", 1), ("id", 1)]))
        counts = {row["_id"]: row["total"] for row in self.questions.aggregate([{"$group": {"_id": "$lesson_id", "total": {"$sum": 1}}}])}
        lessons_by_module: dict[int, list[dict]] = {}
        for lesson in lessons:
            lesson["question_count"] = counts.get(lesson["id"], 0)
            lessons_by_module.setdefault(lesson["module_id"], []).append(lesson)
        modules_by_course: dict[int, list[dict]] = {}
        for module in modules:
            module["lessons"] = lessons_by_module.get(module["id"], [])
            modules_by_course.setdefault(module["course_id"], []).append(module)
        for course in courses:
            course["modules"] = modules_by_course.get(course["id"], [])
        return courses

    def admin_students(self) -> list[dict]:
        students = self._clean_list(self.users.find().sort("registered_at", -1))
        for student in students:
            user_id = student["telegram_id"]
            student["courses_count"] = self.enrollments.count_documents({"user_id": user_id, "status": "active"})
            student["passed_lessons"] = self.progress.count_documents({"user_id": user_id, "status": "passed"})
        return students

    def admin_payments(self) -> list[dict]:
        items = self._clean_list(self.payments.find().sort("created_at", -1).limit(100))
        for item in items:
            user = self.get_user(item["user_id"]) or {}
            course = self.get_course(item["course_id"]) or {}
            item["full_name"] = user.get("full_name", "")
            item["course_title"] = course.get("title", "")
        return items

    def admin_results(self) -> list[dict]:
        items = self._clean_list(self.results.find().sort("created_at", -1).limit(100))
        for item in items:
            user = self.get_user(item["user_id"]) or {}
            lesson = self.get_lesson(item["lesson_id"]) or {}
            item["full_name"] = user.get("full_name", "")
            item["lesson_title"] = lesson.get("title", "")
            item["course_title"] = lesson.get("course_title", "")
        return items

    def admin_create_course(self, data: dict) -> dict:
        title = str(data.get("title", "")).strip()
        price = int(data.get("price") or 0)
        description = str(data.get("description", "")).strip()
        if not title or price <= 0:
            raise ValueError("Kurs nomi va narxi to'g'ri kiritilishi kerak")
        course_id = self._next_id("courses")
        doc = {
            "_id": course_id,
            "id": course_id,
            "slug": self._unique_slug(self.courses, title),
            "title": title,
            "price": price,
            "description": description,
            "created_at": now_iso(),
        }
        self.courses.insert_one(doc)
        return self.get_course(course_id)

    def admin_create_module(self, data: dict) -> dict:
        course_id = int(data.get("course_id") or 0)
        title = str(data.get("title", "")).strip()
        position = int(data.get("position") or 1)
        if not course_id or not title:
            raise ValueError("Kurs va modul nomi kiritilishi kerak")
        module_id = self._next_id("modules")
        self.modules.insert_one({"_id": module_id, "id": module_id, "course_id": course_id, "title": title, "position": position, "created_at": now_iso()})
        return self.get_module(module_id)

    def admin_create_lesson(self, data: dict) -> dict:
        module_id = int(data.get("module_id") or 0)
        title = str(data.get("title", "")).strip()
        position = int(data.get("position") or 1)
        duration = int(data.get("duration_minutes") or 30)
        pass_percent = int(data.get("pass_percent") or 80)
        video_url = str(data.get("video_url", "")).strip()
        if not module_id or not title:
            raise ValueError("Modul va dars nomi kiritilishi kerak")
        lesson_id = self._next_id("lessons")
        self.lessons.insert_one(
            {
                "_id": lesson_id,
                "id": lesson_id,
                "module_id": module_id,
                "slug": self._unique_slug(self.lessons, title),
                "title": title,
                "position": position,
                "video_url": video_url,
                "duration_minutes": duration,
                "pass_percent": pass_percent,
                "created_at": now_iso(),
            }
        )
        return self.get_lesson(lesson_id)

    def admin_update_lesson_video(self, lesson_id: int, video_url: str) -> dict:
        lesson_id = int(lesson_id or 0)
        video_url = str(video_url or "").strip()
        if not lesson_id or not video_url:
            raise ValueError("Dars va video kiritilishi kerak")
        result = self.lessons.update_one({"id": lesson_id}, {"$set": {"video_url": video_url}})
        if not result.matched_count:
            raise ValueError("Dars topilmadi")
        return self.get_lesson(lesson_id)

    def admin_next_question_position(self, lesson_id: int) -> int:
        row = self.questions.find_one({"lesson_id": int(lesson_id)}, sort=[("position", -1), ("id", -1)])
        return int(row.get("position") or 0) + 1 if row else 1

    def admin_create_question(self, data: dict) -> dict:
        lesson_id = int(data.get("lesson_id") or 0)
        text = str(data.get("text", "")).strip()
        image_data = str(data.get("image_data", "")).strip()
        correct = str(data.get("correct_option", "")).strip().upper()
        options = data.get("options") or {}
        option_a = str(data.get("option_a") or options.get("A") or "").strip()
        option_b = str(data.get("option_b") or options.get("B") or "").strip()
        option_c = str(data.get("option_c") or options.get("C") or "").strip()
        option_d = str(data.get("option_d") or options.get("D") or "").strip()
        explanation = str(data.get("explanation", "")).strip()
        position = int(data.get("position") or 1)
        if image_data and (not image_data.startswith("data:image/") or len(image_data) > 2_500_000):
            raise ValueError("Rasm hajmi katta yoki noto'g'ri formatda")
        if not lesson_id or (not text and not image_data) or correct not in {"A", "B", "C", "D"}:
            raise ValueError("Savol, dars va to'g'ri javob kiritilishi kerak")
        if not all([option_a, option_b, option_c, option_d]):
            raise ValueError("A, B, C, D variantlari to'liq kiritilishi kerak")
        question_id = self._next_id("questions")
        doc = {
            "_id": question_id,
            "id": question_id,
            "lesson_id": lesson_id,
            "text": text,
            "image_data": image_data,
            "option_a": option_a,
            "option_b": option_b,
            "option_c": option_c,
            "option_d": option_d,
            "correct_option": correct,
            "explanation": explanation,
            "position": position,
            "created_at": now_iso(),
        }
        self.questions.insert_one(doc)
        return self._clean(doc)


Database = MongoDatabase


class TelegramClient:
    def __init__(self, token: str):
        self.token = token
        self.api_url = f"https://api.telegram.org/bot{token}"

    def request(self, method: str, payload: dict | None = None, timeout: int = 35) -> dict:
        if not self.token:
            return {"ok": False, "description": "BOT_TOKEN is empty"}
        data = json.dumps(payload or {}, ensure_ascii=False).encode("utf-8")
        request = urllib.request.Request(
            f"{self.api_url}/{method}",
            data=data,
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        try:
            with urllib.request.urlopen(request, timeout=timeout) as response:
                return json.loads(response.read().decode("utf-8"))
        except urllib.error.HTTPError as error:
            body = error.read().decode("utf-8", errors="replace")
            print(f"Telegram HTTP error: {error.code} {body}")
        except Exception as error:
            print(f"Telegram request error: {error}")
        return {"ok": False}

    def send_message(self, chat_id: int, text: str, reply_markup: dict | None = None) -> None:
        payload = {"chat_id": chat_id, "text": text, "parse_mode": "HTML", "disable_web_page_preview": True}
        if reply_markup:
            payload["reply_markup"] = reply_markup
        self.request("sendMessage", payload)

    def send_video(self, chat_id: int, video: str, caption: str = "", reply_markup: dict | None = None) -> None:
        payload = {"chat_id": chat_id, "video": video, "caption": caption, "parse_mode": "HTML"}
        if reply_markup:
            payload["reply_markup"] = reply_markup
        result = self.request("sendVideo", payload)
        if not result.get("ok") and caption:
            self.send_message(chat_id, caption, reply_markup)

    def download_file_data_url(self, file_id: str, default_mime: str = "image/jpeg") -> str:
        result = self.request("getFile", {"file_id": file_id}, timeout=20)
        if not result.get("ok"):
            raise ValueError("Telegram faylini olishda xatolik")
        file_path = result.get("result", {}).get("file_path", "")
        if not file_path:
            raise ValueError("Telegram fayl yo'li topilmadi")
        mime_type = mimetypes.guess_type(file_path)[0] or default_mime
        url = f"https://api.telegram.org/file/bot{self.token}/{file_path}"
        with urllib.request.urlopen(url, timeout=40) as response:
            raw = response.read()
        if len(raw) > 2_200_000:
            raise ValueError("Rasm juda katta. Web admin panelda qirqib kichraytiring.")
        encoded = base64.b64encode(raw).decode("ascii")
        return f"data:{mime_type};base64,{encoded}"

    def answer_callback(self, callback_id: str, text: str = "", show_alert: bool = False) -> None:
        payload = {"callback_query_id": callback_id, "text": text, "show_alert": show_alert}
        self.request("answerCallbackQuery", payload)

    def get_updates(self, offset: int | None = None) -> list[dict]:
        payload = {"timeout": 30, "allowed_updates": ["message", "callback_query"]}
        if offset is not None:
            payload["offset"] = offset
        result = self.request("getUpdates", payload, timeout=40)
        if result.get("ok"):
            return result.get("result", [])
        return []


class FariksBot:
    def __init__(self, db: Database, telegram: TelegramClient):
        self.db = db
        self.telegram = telegram
        self.offset: int | None = None

    @staticmethod
    def main_keyboard() -> dict:
        return {
            "keyboard": [
                [{"text": "📚 Kurslar"}, {"text": "📖 Mening kurslarim"}],
                [{"text": "👤 Profilim"}, {"text": "💳 To'lovlarim"}],
                [{"text": "🏆 Natijalarim"}],
            ],
            "resize_keyboard": True,
        }

    @staticmethod
    def phone_keyboard() -> dict:
        return {
            "keyboard": [[{"text": "📱 Telefon raqamni yuborish", "request_contact": True}]],
            "resize_keyboard": True,
            "one_time_keyboard": True,
        }

    def is_admin_user(self, user_id: int) -> bool:
        return user_id in ADMIN_TELEGRAM_IDS

    def admin_web_link(self, user_id: int, from_user: dict) -> str:
        first_name = str(from_user.get("first_name") or "").strip()
        last_name = str(from_user.get("last_name") or "").strip()
        username = str(from_user.get("username") or "").strip()
        name = " ".join(part for part in [first_name, last_name] if part).strip() or username or str(user_id)
        token = create_admin_session_token(user_id, name, username)
        return public_link("/admin", {"token": token})

    def admin_keyboard(self, user_id: int, from_user: dict) -> dict:
        link = self.admin_web_link(user_id, from_user)
        return {
            "inline_keyboard": [
                [
                    {"text": "Statistika", "callback_data": "admin:stats"},
                    {"text": "Kurslar", "callback_data": "admin:courses"},
                ],
                [
                    {"text": "Yangi kurs", "callback_data": "admin:add_course"},
                    {"text": "Yangi modul", "callback_data": "admin:add_module"},
                ],
                [
                    {"text": "Yangi dars", "callback_data": "admin:add_lesson"},
                    {"text": "Darsga video", "callback_data": "admin:add_video"},
                ],
                [{"text": "Test savoli qo'shish", "callback_data": "admin:add_question"}],
                [{"text": "Web admin panel", "url": link}],
            ]
        }

    def show_admin_panel(self, chat_id: int, user_id: int, from_user: dict) -> None:
        summary = self.db.admin_summary()
        text = (
            "<b>FARIKS admin panel</b>\n\n"
            f"Kurslar: {summary['courses']}\n"
            f"Darslar: {summary['lessons']}\n"
            f"Savollar: {summary['questions']}\n"
            f"O'quvchilar: {summary['students']}\n\n"
            "Kerakli amalni tanlang."
        )
        self.telegram.send_message(chat_id, text, self.admin_keyboard(user_id, from_user))

    def show_admin_stats(self, chat_id: int, user_id: int, from_user: dict) -> None:
        summary = self.db.admin_summary()
        text = (
            "<b>Statistika</b>\n\n"
            f"Kurslar: {summary['courses']}\n"
            f"Modullar: {summary['modules']}\n"
            f"Darslar: {summary['lessons']}\n"
            f"Savollar: {summary['questions']}\n"
            f"O'quvchilar: {summary['students']}\n"
            f"To'lovlar: {summary['payments']}\n"
            f"Natijalar: {summary['results']}"
        )
        self.telegram.send_message(chat_id, text, self.admin_keyboard(user_id, from_user))

    def show_admin_courses(self, chat_id: int, user_id: int, from_user: dict) -> None:
        courses = self.db.admin_courses()
        if not courses:
            self.telegram.send_message(chat_id, "Hali kurs yo'q.", self.admin_keyboard(user_id, from_user))
            return
        lines = ["<b>Kurslar</b>\n"]
        for course in courses[:20]:
            lessons_count = sum(len(module.get("lessons", [])) for module in course.get("modules", []))
            question_count = sum(
                lesson.get("question_count", 0)
                for module in course.get("modules", [])
                for lesson in module.get("lessons", [])
            )
            lines.append(
                f"<b>{html.escape(course['title'])}</b>\n"
                f"Narxi: {format_money(course['price'])}\n"
                f"Modul: {len(course.get('modules', []))}, dars: {lessons_count}, savol: {question_count}\n"
            )
        self.telegram.send_message(chat_id, "\n".join(lines), self.admin_keyboard(user_id, from_user))

    def show_admin_course_picker(self, chat_id: int, prefix: str, empty_text: str) -> None:
        courses = self.db.list_courses()
        if not courses:
            self.telegram.send_message(chat_id, empty_text)
            return
        keyboard = [[{"text": course["title"][:55], "callback_data": f"{prefix}{course['id']}"}] for course in courses[:40]]
        keyboard.append([{"text": "Orqaga", "callback_data": "admin:home"}])
        self.telegram.send_message(chat_id, "Kursni tanlang:", {"inline_keyboard": keyboard})

    def show_admin_module_picker(self, chat_id: int, prefix: str, empty_text: str) -> None:
        modules = [(course, module) for course in self.db.admin_courses() for module in course.get("modules", [])]
        if not modules:
            self.telegram.send_message(chat_id, empty_text)
            return
        keyboard = [
            [
                {
                    "text": f"{course['title'][:22]} / {module['title'][:28]}",
                    "callback_data": f"{prefix}{module['id']}",
                }
            ]
            for course, module in modules[:40]
        ]
        keyboard.append([{"text": "Orqaga", "callback_data": "admin:home"}])
        self.telegram.send_message(chat_id, "Modulni tanlang:", {"inline_keyboard": keyboard})

    def show_admin_lesson_picker(self, chat_id: int, prefix: str, empty_text: str) -> None:
        lessons = [
            (course, module, lesson)
            for course in self.db.admin_courses()
            for module in course.get("modules", [])
            for lesson in module.get("lessons", [])
        ]
        if not lessons:
            self.telegram.send_message(chat_id, empty_text)
            return
        keyboard = [
            [
                {
                    "text": f"{course['title'][:18]} / {lesson['title'][:32]}",
                    "callback_data": f"{prefix}{lesson['id']}",
                }
            ]
            for course, module, lesson in lessons[:40]
        ]
        keyboard.append([{"text": "Orqaga", "callback_data": "admin:home"}])
        self.telegram.send_message(chat_id, "Darsni tanlang:", {"inline_keyboard": keyboard})

    @staticmethod
    def formula_block(latex: str) -> str:
        return f"\\[{latex}\\]"

    @staticmethod
    def combine_question_text(intro: str, latex: str = "") -> str:
        intro = intro.strip()
        if not latex:
            return intro
        return "\n".join(part for part in [intro, FariksBot.formula_block(latex)] if part)

    @staticmethod
    def extract_video_ref(message: dict, text: str) -> str:
        if message.get("video", {}).get("file_id"):
            return f"tgfile:{message['video']['file_id']}"
        document = message.get("document") or {}
        if document.get("file_id") and str(document.get("mime_type", "")).startswith("video/"):
            return f"tgfile:{document['file_id']}"
        return text.strip()

    def extract_photo_data(self, message: dict) -> str:
        photos = message.get("photo") or []
        if photos:
            return self.telegram.download_file_data_url(photos[-1]["file_id"], "image/jpeg")
        document = message.get("document") or {}
        if document.get("file_id") and str(document.get("mime_type", "")).startswith("image/"):
            return self.telegram.download_file_data_url(document["file_id"], document.get("mime_type") or "image/jpeg")
        return ""

    @staticmethod
    def normalize_trig(value: str) -> str:
        name = value.strip().lower()
        if name in {"tg", "tan"}:
            return "tan"
        if name in {"ctg", "cot"}:
            return "cot"
        if name == "cos":
            return "cos"
        return "sin"

    def start_admin_question_answers(self, chat_id: int, user_id: int, payload: dict, question_text: str) -> None:
        payload["text"] = question_text
        self.db.set_state(user_id, "admin_question_a", payload)
        self.telegram.send_message(chat_id, "A variantni yozing.")

    def finish_admin_question(self, chat_id: int, user_id: int, payload: dict) -> None:
        lesson_id = int(payload.get("lesson_id") or 0)
        position = (
            self.db.admin_next_question_position(lesson_id)
            if hasattr(self.db, "admin_next_question_position")
            else len(self.db.get_questions(lesson_id)) + 1
        )
        self.db.admin_create_question(
            {
                "lesson_id": lesson_id,
                "text": payload.get("text", ""),
                "option_a": payload.get("option_a", ""),
                "option_b": payload.get("option_b", ""),
                "option_c": payload.get("option_c", ""),
                "option_d": payload.get("option_d", ""),
                "correct_option": payload.get("correct_option", "A"),
                "explanation": payload.get("explanation", ""),
                "image_data": payload.get("image_data", ""),
                "position": position,
            }
        )
        self.db.clear_state(user_id)
        self.telegram.send_message(chat_id, "Savol saqlandi.", {"inline_keyboard": [[{"text": "Yana savol qo'shish", "callback_data": "admin:add_question"}, {"text": "Admin panel", "callback_data": "admin:home"}]]})

    def run(self) -> None:
        print("Telegram bot long-polling rejimida ishga tushdi.")
        while True:
            for update in self.telegram.get_updates(self.offset):
                self.offset = update["update_id"] + 1
                try:
                    self.handle_update(update)
                except Exception as error:
                    print(f"Update handling error: {error}")
            time.sleep(0.3)

    def handle_update(self, update: dict) -> None:
        if "callback_query" in update:
            self.handle_callback(update["callback_query"])
            return
        if "message" in update:
            self.handle_message(update["message"])

    def handle_message(self, message: dict) -> None:
        chat_id = int(message["chat"]["id"])
        user_id = int(message["from"]["id"])
        text = str(message.get("text", "")).strip()
        command = text.split()[0].split("@")[0] if text.startswith("/") else ""

        if command == "/admin":
            self.handle_admin_command(chat_id, user_id, message.get("from", {}))
            return

        if command == "/start":
            self.start(chat_id, user_id)
            return

        state, payload = self.db.get_state(user_id)
        if self.is_admin_user(user_id) and state and state.startswith("admin_"):
            if command == "/cancel":
                self.db.clear_state(user_id)
                self.telegram.send_message(chat_id, "Admin amal bekor qilindi.", self.admin_keyboard(user_id, message.get("from", {})))
                return
            self.handle_admin_state_message(message, state, payload)
            return

        if state == "awaiting_name":
            if len(text) < 3:
                self.telegram.send_message(chat_id, "Iltimos, ism familiyangizni to'liq kiriting.")
                return
            self.db.set_state(user_id, "awaiting_phone", {"full_name": text})
            self.telegram.send_message(chat_id, "Telefon raqamingizni yuboring.", self.phone_keyboard())
            return

        if state == "awaiting_phone":
            phone = message.get("contact", {}).get("phone_number") or text
            if len(phone) < 7:
                self.telegram.send_message(chat_id, "Telefon raqam noto'g'ri ko'rinyapti. Qayta yuboring.")
                return
            self.db.register_user(user_id, payload.get("full_name", ""), phone)
            self.telegram.send_message(
                chat_id,
                "✅ Ro'yxatdan o'tish muvaffaqiyatli yakunlandi.\n\nAsosiy menyu:",
                self.main_keyboard(),
            )
            return

        user = self.db.get_user(user_id)
        if not user:
            self.telegram.send_message(
                chat_id,
                "🎓 FARIKS O'quv Markaziga xush kelibsiz!\n\nOnline kurslardan foydalanish uchun ro'yxatdan o'ting.",
                {"inline_keyboard": [[{"text": "Ro'yxatdan o'tish", "callback_data": "register"}]]},
            )
            return

        if text == "📚 Kurslar":
            self.show_courses(chat_id)
        elif text == "📖 Mening kurslarim":
            self.show_my_courses(chat_id, user_id)
        elif text == "👤 Profilim":
            self.show_profile(chat_id, user_id)
        elif text == "💳 To'lovlarim":
            self.show_payments(chat_id, user_id)
        elif text == "🏆 Natijalarim":
            self.show_results(chat_id, user_id)
        else:
            self.telegram.send_message(chat_id, "Menyudan kerakli bo'limni tanlang.", self.main_keyboard())

    def handle_callback(self, callback: dict) -> None:
        callback_id = callback["id"]
        data = callback.get("data", "")
        user_id = int(callback["from"]["id"])
        chat_id = int(callback["message"]["chat"]["id"])
        self.telegram.answer_callback(callback_id)

        if data == "register":
            self.db.set_state(user_id, "awaiting_name")
            self.telegram.send_message(chat_id, "Ism familiyangizni kiriting.")
            return

        if data == "locked":
            self.telegram.answer_callback(callback_id, "Bu bo'lim hali ochilmagan.", show_alert=True)
            return

        if data.startswith("admin:"):
            self.handle_admin_callback(chat_id, user_id, data, callback.get("from", {}))
            return

        if not self.db.get_user(user_id):
            self.start(chat_id, user_id)
            return

        if data.startswith("buy:"):
            course_id = int(data.split(":", 1)[1])
            self.show_payment_methods(chat_id, course_id)
        elif data.startswith("pay:"):
            _, course_id, method = data.split(":", 2)
            self.confirm_payment(chat_id, user_id, int(course_id), method)
        elif data == "mycourses":
            self.show_my_courses(chat_id, user_id)
        elif data.startswith("open_course:"):
            course_id = int(data.split(":", 1)[1])
            self.show_course_structure(chat_id, user_id, course_id)
        elif data.startswith("module:"):
            module_id = int(data.split(":", 1)[1])
            self.show_module_lessons(chat_id, user_id, module_id)
        elif data.startswith("lesson:"):
            lesson_id = int(data.split(":", 1)[1])
            self.show_lesson(chat_id, user_id, lesson_id)
        elif data.startswith("test:"):
            lesson_id = int(data.split(":", 1)[1])
            self.send_test_link(chat_id, user_id, lesson_id)

    def start(self, chat_id: int, user_id: int) -> None:
        if self.db.get_user(user_id):
            self.telegram.send_message(chat_id, "Asosiy menyu:", self.main_keyboard())
            return
        self.telegram.send_message(
            chat_id,
            "🎓 FARIKS O'quv Markaziga xush kelibsiz!\n\nOnline kurslardan foydalanish uchun ro'yxatdan o'ting.",
            {"inline_keyboard": [[{"text": "Ro'yxatdan o'tish", "callback_data": "register"}]]},
        )

    def handle_admin_command(self, chat_id: int, user_id: int, from_user: dict) -> None:
        if not self.is_admin_user(user_id):
            self.telegram.send_message(chat_id, "Bu bo'lim faqat admin uchun.")
            return
        self.db.clear_state(user_id)
        self.show_admin_panel(chat_id, user_id, from_user)

    def handle_admin_callback(self, chat_id: int, user_id: int, data: str, from_user: dict) -> None:
        if not self.is_admin_user(user_id):
            self.telegram.send_message(chat_id, "Bu bo'lim faqat admin uchun.")
            return

        if data == "admin:home":
            self.db.clear_state(user_id)
            self.show_admin_panel(chat_id, user_id, from_user)
            return
        if data == "admin:stats":
            self.show_admin_stats(chat_id, user_id, from_user)
            return
        if data == "admin:courses":
            self.show_admin_courses(chat_id, user_id, from_user)
            return
        if data == "admin:add_course":
            self.db.set_state(user_id, "admin_course_title")
            self.telegram.send_message(chat_id, "Yangi kurs nomini yozing.\n\nBekor qilish: /cancel")
            return
        if data == "admin:add_module":
            self.show_admin_course_picker(chat_id, "admin:module_course:", "Avval kurs qo'shing.")
            return
        if data.startswith("admin:module_course:"):
            course_id = int(data.rsplit(":", 1)[1])
            self.db.set_state(user_id, "admin_module_title", {"course_id": course_id})
            self.telegram.send_message(chat_id, "Modul nomini yozing.\n\nMasalan: 1-MODUL Algebra")
            return
        if data == "admin:add_lesson":
            self.show_admin_module_picker(chat_id, "admin:lesson_module:", "Avval kurs va modul qo'shing.")
            return
        if data.startswith("admin:lesson_module:"):
            module_id = int(data.rsplit(":", 1)[1])
            self.db.set_state(user_id, "admin_lesson_title", {"module_id": module_id})
            self.telegram.send_message(chat_id, "Dars nomini yozing.\n\nMasalan: 1-Dars Chiziqli tenglamalar")
            return
        if data == "admin:add_video":
            self.show_admin_lesson_picker(chat_id, "admin:video_lesson:", "Avval dars qo'shing.")
            return
        if data.startswith("admin:video_lesson:"):
            lesson_id = int(data.rsplit(":", 1)[1])
            self.db.set_state(user_id, "admin_video_value", {"lesson_id": lesson_id})
            self.telegram.send_message(chat_id, "Video link yuboring yoki Telegramga video fayl tashlang.")
            return
        if data == "admin:add_question":
            self.show_admin_lesson_picker(chat_id, "admin:question_lesson:", "Avval dars qo'shing.")
            return
        if data.startswith("admin:question_lesson:"):
            lesson_id = int(data.rsplit(":", 1)[1])
            keyboard = {
                "inline_keyboard": [
                    [
                        {"text": "Oddiy matn", "callback_data": f"admin:qtype:text:{lesson_id}"},
                        {"text": "Kasr", "callback_data": f"admin:qtype:fraction:{lesson_id}"},
                    ],
                    [
                        {"text": "Ildiz", "callback_data": f"admin:qtype:sqrt:{lesson_id}"},
                        {"text": "Daraja", "callback_data": f"admin:qtype:power:{lesson_id}"},
                    ],
                    [
                        {"text": "Log", "callback_data": f"admin:qtype:log:{lesson_id}"},
                        {"text": "Trigonometria", "callback_data": f"admin:qtype:trig:{lesson_id}"},
                    ],
                    [{"text": "Rasmli savol", "callback_data": f"admin:qtype:image:{lesson_id}"}],
                ]
            }
            self.telegram.send_message(chat_id, "Savol turini tanlang:", keyboard)
            return
        if data.startswith("admin:qtype:"):
            _, _, kind, lesson_id = data.split(":", 3)
            if kind == "image":
                self.db.set_state(user_id, "admin_question_image", {"lesson_id": int(lesson_id), "kind": kind})
                self.telegram.send_message(chat_id, "Rasm yuboring. Qirqish kerak bo'lsa web admin paneldan foydalaning.")
                return
            self.db.set_state(user_id, "admin_question_prompt", {"lesson_id": int(lesson_id), "kind": kind})
            self.telegram.send_message(chat_id, "Savol matnini yozing.\n\nMasalan: Tenglamani yeching.")
            return

    def handle_admin_state_message(self, message: dict, state: str, payload: dict) -> None:
        chat_id = int(message["chat"]["id"])
        user_id = int(message["from"]["id"])
        text = str(message.get("text", "")).strip()

        if state == "admin_course_title":
            if len(text) < 3:
                self.telegram.send_message(chat_id, "Kurs nomini to'liq yozing.")
                return
            self.db.set_state(user_id, "admin_course_price", {"title": text})
            self.telegram.send_message(chat_id, "Kurs narxini yozing.\n\nMasalan: 300000")
            return

        if state == "admin_course_price":
            price = int(re.sub(r"\D+", "", text) or 0)
            if price <= 0:
                self.telegram.send_message(chat_id, "Narxni raqam bilan yozing. Masalan: 300000")
                return
            payload["price"] = price
            self.db.set_state(user_id, "admin_course_description", payload)
            self.telegram.send_message(chat_id, "Kurs tavsifini yozing. Agar kerak bo'lmasa - yuboring.")
            return

        if state == "admin_course_description":
            payload["description"] = "" if text == "-" else text
            course = self.db.admin_create_course(payload)
            self.db.clear_state(user_id)
            self.telegram.send_message(chat_id, f"Kurs qo'shildi: <b>{html.escape(course['title'])}</b>", self.admin_keyboard(user_id, message.get("from", {})))
            return

        if state == "admin_module_title":
            if len(text) < 2:
                self.telegram.send_message(chat_id, "Modul nomini yozing.")
                return
            payload["title"] = text
            self.db.set_state(user_id, "admin_module_position", payload)
            self.telegram.send_message(chat_id, "Modul tartib raqamini yozing. Masalan: 1")
            return

        if state == "admin_module_position":
            payload["position"] = int(re.sub(r"\D+", "", text) or 1)
            module = self.db.admin_create_module(payload)
            self.db.clear_state(user_id)
            self.telegram.send_message(chat_id, f"Modul qo'shildi: <b>{html.escape(module['title'])}</b>", self.admin_keyboard(user_id, message.get("from", {})))
            return

        if state == "admin_lesson_title":
            if len(text) < 2:
                self.telegram.send_message(chat_id, "Dars nomini yozing.")
                return
            payload["title"] = text
            self.db.set_state(user_id, "admin_lesson_video", payload)
            self.telegram.send_message(chat_id, "Dars videosini yuboring: video fayl, video link yoki -.")
            return

        if state == "admin_lesson_video":
            video_ref = self.extract_video_ref(message, text)
            payload["video_url"] = "" if video_ref == "-" else video_ref
            self.db.set_state(user_id, "admin_lesson_duration", payload)
            self.telegram.send_message(chat_id, "Test vaqti necha daqiqa bo'lsin? Masalan: 30")
            return

        if state == "admin_lesson_duration":
            payload["duration_minutes"] = int(re.sub(r"\D+", "", text) or 30)
            self.db.set_state(user_id, "admin_lesson_pass", payload)
            self.telegram.send_message(chat_id, "O'tish foizini yozing. Masalan: 80")
            return

        if state == "admin_lesson_pass":
            pass_percent = int(re.sub(r"\D+", "", text) or 80)
            payload["pass_percent"] = min(100, max(1, pass_percent))
            payload["position"] = len(self.db.list_module_lessons(int(payload["module_id"]))) + 1
            lesson = self.db.admin_create_lesson(payload)
            self.db.clear_state(user_id)
            self.telegram.send_message(chat_id, f"Dars qo'shildi: <b>{html.escape(lesson['title'])}</b>", self.admin_keyboard(user_id, message.get("from", {})))
            return

        if state == "admin_video_value":
            video_ref = self.extract_video_ref(message, text)
            if not video_ref:
                self.telegram.send_message(chat_id, "Video link yuboring yoki Telegramga video fayl tashlang.")
                return
            lesson = self.db.admin_update_lesson_video(int(payload["lesson_id"]), video_ref)
            self.db.clear_state(user_id)
            self.telegram.send_message(chat_id, f"Video saqlandi: <b>{html.escape(lesson['title'])}</b>", self.admin_keyboard(user_id, message.get("from", {})))
            return

        if state == "admin_question_image":
            try:
                image_data = self.extract_photo_data(message)
            except ValueError as error:
                self.telegram.send_message(chat_id, str(error))
                return
            if not image_data:
                self.telegram.send_message(chat_id, "Rasm yuboring yoki image fayl tashlang.")
                return
            payload["image_data"] = image_data
            self.db.set_state(user_id, "admin_question_image_intro", payload)
            self.telegram.send_message(chat_id, "Savol matnini yozing. Kerak bo'lmasa - yuboring.")
            return

        if state == "admin_question_image_intro":
            intro = "" if text == "-" else text
            self.start_admin_question_answers(chat_id, user_id, payload, intro)
            return

        if state == "admin_question_prompt":
            if len(text) < 2:
                self.telegram.send_message(chat_id, "Savol matnini yozing.")
                return
            payload["intro"] = text
            kind = payload.get("kind", "text")
            if kind == "text":
                self.start_admin_question_answers(chat_id, user_id, payload, text)
            elif kind == "fraction":
                self.db.set_state(user_id, "admin_question_fraction_top", payload)
                self.telegram.send_message(chat_id, "Kasr ustini yozing. Masalan: 2x+3")
            elif kind == "sqrt":
                self.db.set_state(user_id, "admin_question_sqrt_first", payload)
                self.telegram.send_message(chat_id, "1-ildiz ichini yozing. Masalan: x+4")
            elif kind == "power":
                self.db.set_state(user_id, "admin_question_power_base", payload)
                self.telegram.send_message(chat_id, "Asosni yozing. Masalan: x")
            elif kind == "log":
                self.db.set_state(user_id, "admin_question_log_base", payload)
                self.telegram.send_message(chat_id, "Log asosini yozing. Masalan: 2")
            else:
                self.db.set_state(user_id, "admin_question_trig_fn", payload)
                self.telegram.send_message(chat_id, "Funksiyani yozing: sin, cos, tg yoki ctg")
            return

        formula_steps = {
            "admin_question_fraction_top": ("top", "admin_question_fraction_bottom", "Kasr ostini yozing. Masalan: x-1"),
            "admin_question_fraction_bottom": ("bottom", "admin_question_fraction_right", "Tenglikdan keyingi sonni yozing. Masalan: 5"),
            "admin_question_sqrt_first": ("first", "admin_question_sqrt_second", "2-ildiz ichini yozing. Masalan: x-1"),
            "admin_question_sqrt_second": ("second", "admin_question_sqrt_right", "Tenglikdan keyingi sonni yozing. Masalan: 5"),
            "admin_question_power_base": ("base", "admin_question_power_degree", "Darajani yozing. Masalan: 2"),
            "admin_question_power_degree": ("degree", "admin_question_power_extra", "Davomini yozing. Masalan: +3x+2. Kerak bo'lmasa - yuboring."),
            "admin_question_power_extra": ("extra", "admin_question_power_right", "Tenglikdan keyingi sonni yozing. Masalan: 0"),
            "admin_question_log_base": ("base", "admin_question_log_inside", "Log ichidagi ifodani yozing. Masalan: x+1"),
            "admin_question_log_inside": ("inside", "admin_question_log_right", "Tenglikdan keyingi sonni yozing. Masalan: 3"),
            "admin_question_trig_fn": ("fn", "admin_question_trig_angle", "Burchak yoki ifodani yozing. Masalan: x"),
            "admin_question_trig_angle": ("angle", "admin_question_trig_right", "Tenglikdan keyingi qiymatni yozing. Masalan: 0"),
        }
        if state in formula_steps:
            key, next_state, prompt = formula_steps[state]
            payload[key] = "" if text == "-" else text
            self.db.set_state(user_id, next_state, payload)
            self.telegram.send_message(chat_id, prompt)
            return

        if state == "admin_question_fraction_right":
            latex = f"\\frac{{{payload.get('top', '')}}}{{{payload.get('bottom', '')}}}={text}"
            self.start_admin_question_answers(chat_id, user_id, payload, self.combine_question_text(payload.get("intro", ""), latex))
            return
        if state == "admin_question_sqrt_right":
            latex = f"\\sqrt{{{payload.get('first', '')}}}+\\sqrt{{{payload.get('second', '')}}}={text}"
            self.start_admin_question_answers(chat_id, user_id, payload, self.combine_question_text(payload.get("intro", ""), latex))
            return
        if state == "admin_question_power_right":
            extra = payload.get("extra", "")
            latex = f"{payload.get('base', '')}^{{{payload.get('degree', '')}}}{extra}={text}"
            self.start_admin_question_answers(chat_id, user_id, payload, self.combine_question_text(payload.get("intro", ""), latex))
            return
        if state == "admin_question_log_right":
            latex = f"\\log_{{{payload.get('base', '')}}}\\left({payload.get('inside', '')}\\right)={text}"
            self.start_admin_question_answers(chat_id, user_id, payload, self.combine_question_text(payload.get("intro", ""), latex))
            return
        if state == "admin_question_trig_right":
            fn = self.normalize_trig(payload.get("fn", "sin"))
            latex = f"\\{fn}\\left({payload.get('angle', '')}\\right)={text}"
            self.start_admin_question_answers(chat_id, user_id, payload, self.combine_question_text(payload.get("intro", ""), latex))
            return

        if state in {"admin_question_a", "admin_question_b", "admin_question_c", "admin_question_d"}:
            key = state[-1]
            payload[f"option_{key}"] = text
            next_map = {
                "admin_question_a": ("admin_question_b", "B variantni yozing."),
                "admin_question_b": ("admin_question_c", "C variantni yozing."),
                "admin_question_c": ("admin_question_d", "D variantni yozing."),
                "admin_question_d": ("admin_question_correct", "To'g'ri javobni yozing: A, B, C yoki D"),
            }
            next_state, prompt = next_map[state]
            self.db.set_state(user_id, next_state, payload)
            self.telegram.send_message(chat_id, prompt)
            return

        if state == "admin_question_correct":
            correct = text.upper()
            if correct not in {"A", "B", "C", "D"}:
                self.telegram.send_message(chat_id, "Faqat A, B, C yoki D yozing.")
                return
            payload["correct_option"] = correct
            self.db.set_state(user_id, "admin_question_explanation", payload)
            self.telegram.send_message(chat_id, "Izoh yozing. Kerak bo'lmasa - yuboring.")
            return

        if state == "admin_question_explanation":
            payload["explanation"] = "" if text == "-" else text
            self.finish_admin_question(chat_id, user_id, payload)
            return

    def show_courses(self, chat_id: int) -> None:
        courses = self.db.list_courses()
        lines = ["Mavjud kurslar:\n"]
        keyboard = []
        for course in courses:
            lines.append(f"📘 <b>{course['title']}</b>\nNarxi: {format_money(course['price'])}\n")
            keyboard.append([{"text": f"🛒 {course['title']}", "callback_data": f"buy:{course['id']}"}])
        self.telegram.send_message(chat_id, "\n".join(lines), {"inline_keyboard": keyboard})

    def show_payment_methods(self, chat_id: int, course_id: int) -> None:
        course = self.db.get_course(course_id)
        if not course:
            self.telegram.send_message(chat_id, "Kurs topilmadi.")
            return
        methods = [
            ("Click", "click"),
            ("Payme", "payme"),
            ("Uzum", "uzum"),
            ("Karta orqali", "card"),
        ]
        keyboard = [[{"text": name, "callback_data": f"pay:{course_id}:{code}"}] for name, code in methods]
        self.telegram.send_message(
            chat_id,
            f"<b>{course['title']}</b>\nNarxi: {format_money(course['price'])}\n\nTo'lov usulini tanlang.",
            {"inline_keyboard": keyboard},
        )

    def confirm_payment(self, chat_id: int, user_id: int, course_id: int, method: str) -> None:
        result = self.db.create_payment_and_enrollment(user_id, course_id, method)
        course = result["course"]
        self.telegram.send_message(
            chat_id,
            "✅ To'lov tasdiqlandi.\n\n"
            f"Siz <b>{course['title']}</b> kursiga muvaffaqiyatli qo'shildingiz.",
            {"inline_keyboard": [[{"text": "📚 Mening kurslarim", "callback_data": "mycourses"}]]},
        )

    def show_my_courses(self, chat_id: int, user_id: int) -> None:
        enrollments = self.db.list_enrollments(user_id)
        if not enrollments:
            self.telegram.send_message(chat_id, "Sizda hali sotib olingan kurslar yo'q.", self.main_keyboard())
            return
        keyboard = [
            [{"text": f"📘 {item['title']}", "callback_data": f"open_course:{item['course_id']}"}]
            for item in enrollments
        ]
        self.telegram.send_message(chat_id, "📚 Mening kurslarim:", {"inline_keyboard": keyboard})

    def show_course_structure(self, chat_id: int, user_id: int, course_id: int) -> None:
        structure = self.db.get_course_structure(user_id, course_id)
        course = structure["course"]
        lines = [f"📘 <b>{course['title']}</b>\n"]
        keyboard = []
        for module in structure["modules"]:
            icon = "" if module["unlocked"] else " 🔒"
            lines.append(f"{module['title']}{icon}")
            callback = f"module:{module['id']}" if module["unlocked"] else "locked"
            keyboard.append([{"text": f"{module['title']}{icon}", "callback_data": callback}])
        self.telegram.send_message(chat_id, "\n".join(lines), {"inline_keyboard": keyboard})

    def show_module_lessons(self, chat_id: int, user_id: int, module_id: int) -> None:
        module = self.db.get_module(module_id)
        lessons = self.db.list_module_lessons(module_id)
        if not module:
            self.telegram.send_message(chat_id, "Modul topilmadi.")
            return
        keyboard = []
        lines = [f"<b>{module['title']}</b>\n"]
        for lesson in lessons:
            unlocked = self.db.is_lesson_unlocked(user_id, lesson["id"])
            progress = self.db.profile_lesson_progress(user_id, lesson["id"]) if hasattr(self.db, "profile_lesson_progress") else None
            suffix = " ✅" if progress and progress.get("status") == "passed" else ("" if unlocked else " 🔒")
            lines.append(f"{lesson['title']}{suffix}")
            callback = f"lesson:{lesson['id']}" if unlocked else "locked"
            keyboard.append([{"text": f"{lesson['title']}{suffix}", "callback_data": callback}])
        self.telegram.send_message(chat_id, "\n".join(lines), {"inline_keyboard": keyboard})

    def show_lesson(self, chat_id: int, user_id: int, lesson_id: int) -> None:
        lesson = self.db.get_lesson(lesson_id)
        if not lesson or not self.db.is_lesson_unlocked(user_id, lesson_id):
            self.telegram.send_message(chat_id, "Bu dars hali ochilmagan.")
            return
        video_line = lesson["video_url"] or "Video dars fayli admin tomonidan qo'shiladi."
        keyboard = {"inline_keyboard": [[{"text": "Testni boshlash", "callback_data": f"test:{lesson_id}"}]]}
        title = html.escape(str(lesson["title"]))
        if str(video_line).startswith("tgfile:"):
            self.telegram.send_video(chat_id, video_line.replace("tgfile:", "", 1), f"<b>{title}</b>", keyboard)
            return
        self.telegram.send_message(chat_id, f"<b>{title}</b>\n\n{html.escape(str(video_line))}", keyboard)

    def send_test_link(self, chat_id: int, user_id: int, lesson_id: int) -> None:
        try:
            token = self.db.create_test_token(user_id, lesson_id)
            lesson = self.db.get_lesson(lesson_id)
        except ValueError as error:
            self.telegram.send_message(chat_id, str(error))
            return
        link = public_link(f"/test/{lesson['slug']}", {"token": token})
        self.telegram.send_message(chat_id, f"🔗 Testni boshlash:\n\n{link}")

    def show_profile(self, chat_id: int, user_id: int) -> None:
        user = self.db.get_user(user_id)
        stats = self.db.profile_stats(user_id)
        self.telegram.send_message(
            chat_id,
            "👤 <b>Profilim</b>\n\n"
            f"Ism familiya: {user['full_name']}\n"
            f"Telefon: {user['phone']}\n"
            f"Kurslar: {stats['enrollments']}\n"
            f"O'tilgan darslar: {stats['passed_lessons']}\n"
            f"O'rtacha natija: {stats['average_percent']}%",
            self.main_keyboard(),
        )

    def show_payments(self, chat_id: int, user_id: int) -> None:
        payments = self.db.list_payments(user_id)
        if not payments:
            self.telegram.send_message(chat_id, "Hozircha to'lovlar topilmadi.", self.main_keyboard())
            return
        lines = ["💳 <b>To'lovlarim</b>\n"]
        for payment in payments[:10]:
            lines.append(
                f"✅ {payment['course_title']}\n"
                f"Usul: {payment['method']}\n"
                f"Summa: {format_money(payment['amount'])}\n"
            )
        self.telegram.send_message(chat_id, "\n".join(lines), self.main_keyboard())

    def show_results(self, chat_id: int, user_id: int) -> None:
        results = self.db.list_results(user_id)
        if not results:
            self.telegram.send_message(chat_id, "Hozircha natijalar yo'q.", self.main_keyboard())
            return
        lines = ["🏆 <b>Natijalarim</b>\n"]
        for result in results[:10]:
            icon = "✅" if result["passed"] else "❌"
            lines.append(
                f"{icon} {result['lesson_title']}\n"
                f"Natija: {result['correct_count']}/{result['total_count']} - {result['percent']}%\n"
            )
        self.telegram.send_message(chat_id, "\n".join(lines), self.main_keyboard())


def make_handler(db: Database, telegram: TelegramClient):
    class FariksHandler(BaseHTTPRequestHandler):
        server_version = "FariksLMS/1.0"

        def log_message(self, format: str, *args) -> None:
            print(f"{self.address_string()} - {format % args}")

        def do_OPTIONS(self) -> None:
            self.send_response(204)
            self.send_cors_headers()
            self.end_headers()

        def do_GET(self) -> None:
            try:
                parsed = urllib.parse.urlparse(self.path)
                path = parsed.path
                query = urllib.parse.parse_qs(parsed.query)

                if path == "/api/health":
                    self.send_json({"ok": True, "service": "fariks-lms", "time": now_iso()})
                    return

                if path.startswith("/api/test/"):
                    lesson_slug = path.rsplit("/", 1)[-1]
                    token = query.get("token", [""])[0]
                    if not token:
                        self.send_json({"ok": False, "error": "Token kerak"}, status=400)
                        return
                    payload = db.get_test_payload(token, lesson_slug)
                    self.send_json({"ok": True, "data": payload})
                    return

                if path.startswith("/api/admin/"):
                    if not self.is_admin(query):
                        self.send_json({"ok": False, "error": "Admin token noto'g'ri"}, status=401)
                        return
                    self.handle_admin_get(path)
                    return

                self.serve_static(path)
            except ValueError as error:
                self.send_json({"ok": False, "error": str(error)}, status=400)
            except Exception as error:
                print(f"HTTP GET error: {error}")
                self.send_json({"ok": False, "error": "Server xatosi"}, status=500)

        def do_POST(self) -> None:
            try:
                parsed = urllib.parse.urlparse(self.path)
                path = parsed.path
                query = urllib.parse.parse_qs(parsed.query)
                body = self.read_json()

                if path == "/api/results":
                    token = str(body.get("token", ""))
                    answers = body.get("answers") or {}
                    result = db.grade_result(token, answers)
                    self.notify_result(result)
                    self.send_json({"ok": True, "data": result})
                    return

                if path.startswith("/api/admin/"):
                    if not self.is_admin(query):
                        self.send_json({"ok": False, "error": "Admin token noto'g'ri"}, status=401)
                        return
                    self.handle_admin_post(path, body)
                    return

                self.send_json({"ok": False, "error": "Endpoint topilmadi"}, status=404)
            except ValueError as error:
                self.send_json({"ok": False, "error": str(error)}, status=400)
            except Exception as error:
                print(f"HTTP POST error: {error}")
                self.send_json({"ok": False, "error": "Server xatosi"}, status=500)

        def read_json(self) -> dict:
            length = int(self.headers.get("Content-Length", "0") or 0)
            raw = self.rfile.read(length) if length else b"{}"
            return json.loads(raw.decode("utf-8") or "{}")

        def send_cors_headers(self) -> None:
            self.send_header("Access-Control-Allow-Origin", "*")
            self.send_header("Access-Control-Allow-Headers", "Content-Type, X-Admin-Token")
            self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")

        def send_json(self, payload: dict, status: int = 200) -> None:
            data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
            self.send_response(status)
            self.send_cors_headers()
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)

        def serve_static(self, path: str) -> None:
            if path in {"", "/"} or path.startswith("/test/") or path == "/admin":
                target = CLIENT_DIR / "index.html"
            else:
                relative = urllib.parse.unquote(path.lstrip("/"))
                target = (CLIENT_DIR / relative).resolve()
                if not target.is_relative_to(CLIENT_DIR.resolve()):
                    self.send_error(403)
                    return

            if not target.exists() or not target.is_file():
                self.send_error(404)
                return

            content = target.read_bytes()
            content_type = mimetypes.guess_type(str(target))[0] or "application/octet-stream"
            self.send_response(200)
            if content_type.startswith("text/") or target.suffix in {".js", ".css"}:
                content_type += "; charset=utf-8"
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(len(content)))
            self.end_headers()
            self.wfile.write(content)

        def get_admin_token(self, query: dict[str, list[str]]) -> str:
            token = self.headers.get("X-Admin-Token") or query.get("admin_token", [""])[0]
            return token.strip()

        def admin_claims(self, query: dict[str, list[str]]) -> dict | None:
            token = self.get_admin_token(query)
            if hmac.compare_digest(token, ADMIN_TOKEN):
                return {"login_method": "token", "name": "Admin", "telegram_id": None, "username": ""}
            try:
                claims = read_admin_session_token(token)
            except ValueError:
                return None
            telegram_id = int(claims.get("telegram_id") or 0)
            if telegram_id and telegram_id in ADMIN_TELEGRAM_IDS:
                return claims
            return None

        def is_admin(self, query: dict[str, list[str]]) -> bool:
            return self.admin_claims(query) is not None

        def handle_admin_get(self, path: str) -> None:
            query = urllib.parse.parse_qs(urllib.parse.urlparse(self.path).query)
            if path == "/api/admin/me":
                self.send_json({"ok": True, "data": self.admin_claims(query)})
            elif path == "/api/admin/summary":
                self.send_json({"ok": True, "data": db.admin_summary()})
            elif path == "/api/admin/courses":
                self.send_json({"ok": True, "data": db.admin_courses()})
            elif path == "/api/admin/students":
                self.send_json({"ok": True, "data": db.admin_students()})
            elif path == "/api/admin/payments":
                self.send_json({"ok": True, "data": db.admin_payments()})
            elif path == "/api/admin/results":
                self.send_json({"ok": True, "data": db.admin_results()})
            else:
                self.send_json({"ok": False, "error": "Admin endpoint topilmadi"}, status=404)

        def handle_admin_post(self, path: str, body: dict) -> None:
            if path == "/api/admin/courses":
                self.send_json({"ok": True, "data": db.admin_create_course(body)}, status=201)
            elif path == "/api/admin/modules":
                self.send_json({"ok": True, "data": db.admin_create_module(body)}, status=201)
            elif path == "/api/admin/lessons":
                self.send_json({"ok": True, "data": db.admin_create_lesson(body)}, status=201)
            elif path == "/api/admin/lessons/video":
                self.send_json({"ok": True, "data": db.admin_update_lesson_video(body.get("lesson_id"), body.get("video_url"))})
            elif path == "/api/admin/questions":
                self.send_json({"ok": True, "data": db.admin_create_question(body)}, status=201)
            else:
                self.send_json({"ok": False, "error": "Admin endpoint topilmadi"}, status=404)

        def notify_result(self, result: dict) -> None:
            if result["passed"]:
                if result["course_completed"]:
                    text = (
                        "🏆 Kurs muvaffaqiyatli yakunlandi.\n\n"
                        f"Oxirgi natija: {result['percent']}%\n\n"
                        "📄 Sertifikat olish uchun admin bilan bog'laning."
                    )
                else:
                    next_title = result["next_lesson"]["title"] if result["next_lesson"] else "Keyingi dars"
                    text = (
                        "🎉 Tabriklaymiz!\n\n"
                        f"{result['lesson_title']} testidan muvaffaqiyatli o'tdingiz.\n"
                        f"Natija: {result['percent']}%\n\n"
                        f"{next_title} ochildi."
                    )
            else:
                text = (
                    "❌ Testdan o'ta olmadingiz.\n\n"
                    f"Natija: {result['percent']}%\n"
                    f"{result['lesson_title']} darsini qayta ko'ring."
                )
            telegram.send_message(result["user_id"], text)

    return FariksHandler


def start_http_server(db: Database, telegram: TelegramClient) -> ThreadingHTTPServer:
    handler = make_handler(db, telegram)
    server = ThreadingHTTPServer((API_HOST, API_PORT), handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    print(f"FARIKS API va client sayt: http://{API_HOST}:{API_PORT}")
    print(f"Admin panel: http://localhost:{API_PORT}/admin")
    return server


def main() -> None:
    db = Database(DB_PATH)
    db.init_schema()
    db.seed()

    telegram = TelegramClient(BOT_TOKEN)
    start_http_server(db, telegram)

    if BOT_TOKEN:
        FariksBot(db, telegram).run()
    else:
        print("BOT_TOKEN bo'sh. Hozir faqat API/client server ishlayapti.")
        print("Telegram botni yoqish uchun bot/.env faylida BOT_TOKEN ni kiriting.")
        try:
            while True:
                time.sleep(3600)
        except KeyboardInterrupt:
            print("Server to'xtatildi.")


if __name__ == "__main__":
    main()
