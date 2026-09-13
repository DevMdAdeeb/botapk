import sqlite3
from contextlib import contextmanager
from typing import List, Optional, Tuple

from config import DB_PATH


@contextmanager
def get_conn():
    conn = sqlite3.connect(DB_PATH)
    try:
        yield conn
    finally:
        conn.close()


def init_db():
    """ينشئ الجداول إذا لم تكن موجودة ويثبت التحديثات الهيكلية."""
    with get_conn() as conn:
        # جدول الملفات
        conn.execute("""
            CREATE TABLE IF NOT EXISTS files (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                storage_message_id INTEGER NOT NULL,
                caption TEXT,
                added_by INTEGER,
                downloads INTEGER DEFAULT 0,
                created_at TEXT DEFAULT CURRENT_TIMESTAMP
            )
        """)

        # التأكد من وجود العمود downloads للمجموعات القديمة إن وجدت
        cursor = conn.execute("PRAGMA table_info(files)")
        columns = [row[1] for row in cursor.fetchall()]
        if "downloads" not in columns:
            conn.execute("ALTER TABLE files ADD COLUMN downloads INTEGER DEFAULT 0")

        # جدول المستخدمين
        conn.execute("""
            CREATE TABLE IF NOT EXISTS users (
                user_id INTEGER PRIMARY KEY,
                first_name TEXT,
                username TEXT,
                joined_at TEXT DEFAULT CURRENT_TIMESTAMP
            )
        """)

        # جدول الإعدادات
        conn.execute("""
            CREATE TABLE IF NOT EXISTS settings (
                key TEXT PRIMARY KEY,
                value TEXT
            )
        """)

        # جدول قنوات الاشتراك الإجباري
        conn.execute("""
            CREATE TABLE IF NOT EXISTS force_sub_channels (
                channel TEXT PRIMARY KEY
            )
        """)

        # تعيين القيم الافتراضية
        conn.execute("""
            INSERT OR IGNORE INTO settings (key, value) VALUES ('force_sub_enabled', '0')
        """)

        # إذا كانت هناك قناة سابقة مخزنة في الإعدادات القديمة، نحولها للجدول الجديد
        row = conn.execute("SELECT value FROM settings WHERE key = 'force_sub_channel'").fetchone()
        if row and row[0]:
            conn.execute("INSERT OR IGNORE INTO force_sub_channels (channel) VALUES (?)", (row[0],))
            conn.execute("DELETE FROM settings WHERE key = 'force_sub_channel'")

        conn.commit()


# ---------- المستخدمين ----------

def add_user(user_id: int, first_name: str = "", username: str = ""):
    """يضيف مستخدمًا جديدًا أو يحدّث بياناته."""
    with get_conn() as conn:
        conn.execute("""
            INSERT INTO users (user_id, first_name, username)
            VALUES (?, ?, ?)
            ON CONFLICT(user_id) DO UPDATE SET
                first_name = excluded.first_name,
                username = excluded.username
        """, (user_id, first_name, username))
        conn.commit()


def get_users_count() -> int:
    """يرجع عدد المستخدمين الإجمالي."""
    with get_conn() as conn:
        row = conn.execute("SELECT COUNT(*) FROM users").fetchone()
        return row[0] if row else 0


def get_all_user_ids() -> List[int]:
    """يرجع قائمة بجميع آيديهات المستخدمين للإذاعة."""
    with get_conn() as conn:
        rows = conn.execute("SELECT user_id FROM users").fetchall()
        return [row[0] for row in rows]


# ---------- الملفات ----------

def add_file(storage_message_id: int, caption: str, added_by: int) -> int:
    """يضيف ملفًا جديدًا ويرجع الـ ID الخاص به لاستخدامه في الرابط."""
    with get_conn() as conn:
        cur = conn.execute(
            "INSERT INTO files (storage_message_id, caption, added_by, downloads) VALUES (?, ?, ?, 0)",
            (storage_message_id, caption, added_by),
        )
        conn.commit()
        return cur.lastrowid


def get_file(file_id: int) -> Optional[Tuple[int, str, int, str]]:
    """يرجع (storage_message_id, caption, downloads, created_at) أو None إذا لم يوجد."""
    with get_conn() as conn:
        row = conn.execute(
            "SELECT storage_message_id, caption, downloads, created_at FROM files WHERE id = ?",
            (file_id,),
        ).fetchone()
        return row


def increment_downloads(file_id: int):
    """يزيد عدد التحميلات لملف معين بمقدار 1."""
    with get_conn() as conn:
        conn.execute(
            "UPDATE files SET downloads = downloads + 1 WHERE id = ?",
            (file_id,),
        )
        conn.commit()


def get_files_count() -> int:
    """يرجع عدد الملفات الإجمالي."""
    with get_conn() as conn:
        row = conn.execute("SELECT COUNT(*) FROM files").fetchone()
        return row[0] if row else 0


def get_total_downloads() -> int:
    """يرجع مجموع عدد التحميلات لجميع الملفات."""
    with get_conn() as conn:
        row = conn.execute("SELECT SUM(downloads) FROM files").fetchone()
        return row[0] if row and row[0] else 0


def get_recent_files(limit: int = 10, offset: int = 0) -> List[Tuple[int, int, str, int, str]]:
    """يرجع أحدث الملفات مع الترقيم الصفيحي (pagination)."""
    with get_conn() as conn:
        rows = conn.execute(
            "SELECT id, storage_message_id, caption, downloads, created_at FROM files ORDER BY id DESC LIMIT ? OFFSET ?",
            (limit, offset),
        ).fetchall()
        return rows


def delete_file(file_id: int) -> bool:
    """يحذف ملفًا من قاعدة البيانات ويرجع True إذا تم الحذف."""
    with get_conn() as conn:
        cur = conn.execute("DELETE FROM files WHERE id = ?", (file_id,))
        conn.commit()
        return cur.rowcount > 0


# ---------- الإعدادات والاشتراك الإجباري ----------

def is_force_sub_enabled() -> bool:
    with get_conn() as conn:
        row = conn.execute(
            "SELECT value FROM settings WHERE key = 'force_sub_enabled'"
        ).fetchone()
        return row is not None and row[0] == "1"


def set_force_sub_enabled(enabled: bool):
    with get_conn() as conn:
        conn.execute(
            "UPDATE settings SET value = ? WHERE key = 'force_sub_enabled'",
            ("1" if enabled else "0",),
        )
        conn.commit()


def get_force_sub_channels() -> List[str]:
    """يرجع قائمة بقنوات الاشتراك الإجباري."""
    with get_conn() as conn:
        rows = conn.execute("SELECT channel FROM force_sub_channels").fetchall()
        return [row[0] for row in rows]


def add_force_sub_channel(channel: str) -> bool:
    """يضيف قناة جديدة للاشتراك الإجباري."""
    with get_conn() as conn:
        try:
            conn.execute("INSERT INTO force_sub_channels (channel) VALUES (?)", (channel.strip(),))
            conn.commit()
            return True
        except sqlite3.IntegrityError:
            return False


def remove_force_sub_channel(channel: str) -> bool:
    """يحذف قناة من الاشتراك الإجباري."""
    with get_conn() as conn:
        cur = conn.execute("DELETE FROM force_sub_channels WHERE channel = ?", (channel.strip(),))
        conn.commit()
        return cur.rowcount > 0
