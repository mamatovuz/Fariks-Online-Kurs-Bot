from __future__ import annotations

import hmac
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
CLIENT_DIR = ROOT_DIR / "client"


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

BOT_TOKEN = os.getenv("BOT_TOKEN", "").strip()
API_HOST = os.getenv("API_HOST", "127.0.0.1").strip()
API_PORT = int(os.getenv("API_PORT", "8080"))
PUBLIC_CLIENT_URL = os.getenv("PUBLIC_CLIENT_URL", f"http://localhost:{API_PORT}").rstrip("/")
ADMIN_TOKEN = os.getenv("ADMIN_TOKEN", "change-me")
DB_PATH = Path(os.getenv("DB_PATH", str(BASE_DIR / "fariks_lms.sqlite3")))
if not DB_PATH.is_absolute():
    DB_PATH = BASE_DIR / DB_PATH


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

        token = uuid.uuid4().hex
        expires_at = (datetime.now(timezone.utc) + timedelta(hours=2)).isoformat()
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
            raise ValueError("Test token topilmadi")

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
            is_correct = selected == question["correct_option"]
            correct_count += 1 if is_correct else 0
            details.append(
                {
                    "id": question["id"],
                    "selected": selected,
                    "correct": question["correct_option"],
                    "is_correct": is_correct,
                    "explanation": question["explanation"],
                }
            )

        total_count = len(questions)
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

        if text == "/start":
            self.start(chat_id, user_id)
            return

        state, payload = self.db.get_state(user_id)
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
        with self.db.connection() as conn:
            module = conn.execute("SELECT * FROM modules WHERE id = ?", (module_id,)).fetchone()
            lessons = rows_to_dicts(
                conn.execute(
                    "SELECT * FROM lessons WHERE module_id = ? ORDER BY position, id",
                    (module_id,),
                ).fetchall()
            )
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
        self.telegram.send_message(
            chat_id,
            f"🎥 <b>{lesson['title']}</b>\n\n{video_line}",
            {"inline_keyboard": [[{"text": "📝 Testni boshlash", "callback_data": f"test:{lesson_id}"}]]},
        )

    def send_test_link(self, chat_id: int, user_id: int, lesson_id: int) -> None:
        try:
            token = self.db.create_test_token(user_id, lesson_id)
            lesson = self.db.get_lesson(lesson_id)
        except ValueError as error:
            self.telegram.send_message(chat_id, str(error))
            return
        link = f"{PUBLIC_CLIENT_URL}/test/{lesson['slug']}?token={token}"
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


def profile_lesson_progress(self: Database, user_id: int, lesson_id: int) -> dict | None:
    with self.connection() as conn:
        row = conn.execute(
            "SELECT * FROM progress WHERE user_id = ? AND lesson_id = ?",
            (user_id, lesson_id),
        ).fetchone()
    return dict(row) if row else None


setattr(Database, "profile_lesson_progress", profile_lesson_progress)


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

        def is_admin(self, query: dict[str, list[str]]) -> bool:
            token = self.headers.get("X-Admin-Token") or query.get("admin_token", [""])[0]
            return hmac.compare_digest(token, ADMIN_TOKEN)

        def handle_admin_get(self, path: str) -> None:
            if path == "/api/admin/summary":
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
