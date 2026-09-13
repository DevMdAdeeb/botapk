import sqlite3
import json
from contextlib import contextmanager
from typing import List, Optional, Tuple, Dict, Any

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

        # جدول قنوات النشر العامة
        conn.execute("""
            CREATE TABLE IF NOT EXISTS public_channels (
                channel TEXT PRIMARY KEY
            )
        """)

        # جدول المنشورات المحفوظة بالأرقام
        conn.execute("""
            CREATE TABLE IF NOT EXISTS posts (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                post_chat_id INTEGER NOT NULL,
                post_message_id INTEGER NOT NULL,
                file_id INTEGER NOT NULL,
                extra_buttons TEXT, -- مخزنة كـ JSON string
                created_at TEXT DEFAULT CURRENT_TIMESTAMP
            )
        """)

        # تعيين القيم الافتراضية
        conn.execute("""
            INSERT OR IGNORE INTO settings (key, value) VALUES ('force_sub_enabled', '0')
        """)
        conn.execute("""
            INSERT OR IGNORE INTO settings (key, value) VALUES ('post_signature_text', '')
        """)
        conn.execute("""
            INSERT OR IGNORE INTO settings (key, value) VALUES ('post_signature_url', '')
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


def get_top_downloaded_files(min_downloads: int = 10, limit: int = 20) -> List[Tuple[int, int, str, int, str]]:
    """يرجع قائمة بأكثر التطبيقات تحميلاً التي بلغت أكثر من min_downloads تنزيل."""
    with get_conn() as conn:
        rows = conn.execute(
            "SELECT id, storage_message_id, caption, downloads, created_at FROM files WHERE downloads > ? ORDER BY downloads DESC LIMIT ?",
            (min_downloads, limit),
        ).fetchall()
        return rows


def delete_file(file_id: int) -> bool:
    """يحذف ملفًا من قاعدة البيانات ويرجع True إذا تم الحذف."""
    with get_conn() as conn:
        cur = conn.execute("DELETE FROM files WHERE id = ?", (file_id,))
        conn.commit()
        return cur.rowcount > 0


# ---------- الإعدادات والاشتراك الإجباري والتوقيع ----------

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


def get_post_signature() -> Tuple[str, str]:
    """يرجع (text, url) التوقيع المخزن أو ("", "")."""
    with get_conn() as conn:
        row_text = conn.execute("SELECT value FROM settings WHERE key = 'post_signature_text'").fetchone()
        row_url = conn.execute("SELECT value FROM settings WHERE key = 'post_signature_url'").fetchone()
        text = row_text[0] if row_text and row_text[0] else ""
        url = row_url[0] if row_url and row_url[0] else ""
        return text, url


def set_post_signature(text: str, url: str):
    """يحدد نص ورابط التوقيع الخاص بالمنشورات."""
    with get_conn() as conn:
        conn.execute("INSERT OR REPLACE INTO settings (key, value) VALUES ('post_signature_text', ?)", (text.strip(),))
        conn.execute("INSERT OR REPLACE INTO settings (key, value) VALUES ('post_signature_url', ?)", (url.strip(),))
        conn.commit()


def delete_post_signature():
    """يحذف توقيع المنشورات."""
    with get_conn() as conn:
        conn.execute("UPDATE settings SET value = '' WHERE key IN ('post_signature_text', 'post_signature_url')")
        conn.commit()


# ---------- قنوات النشر العامة ----------

def get_public_channels() -> List[str]:
    """يرجع قائمة بقنوات النشر العامة."""
    with get_conn() as conn:
        rows = conn.execute("SELECT channel FROM public_channels").fetchall()
        return [row[0] for row in rows]


def add_public_channel(channel: str) -> bool:
    """يضيف قناة جديدة لقنوات النشر العامة."""
    with get_conn() as conn:
        try:
            conn.execute("INSERT INTO public_channels (channel) VALUES (?)", (channel.strip(),))
            conn.commit()
            return True
        except sqlite3.IntegrityError:
            return False


def remove_public_channel(channel: str) -> bool:
    """يحذف قناة من قنوات النشر العامة."""
    with get_conn() as conn:
        cur = conn.execute("DELETE FROM public_channels WHERE channel = ?", (channel.strip(),))
        conn.commit()
        return cur.rowcount > 0


# ---------- المنشورات المحفوظة ----------

def add_post(post_chat_id: int, post_message_id: int, file_id: int, extra_buttons: Optional[List[Dict[str, str]]] = None) -> int:
    """يحفظ منشورًا تفاعليًا جديدًا ويرجع الـ ID الخاص بالمنشور."""
    buttons_json = json.dumps(extra_buttons or [], ensure_ascii=False)
    with get_conn() as conn:
        cur = conn.execute(
            "INSERT INTO posts (post_chat_id, post_message_id, file_id, extra_buttons) VALUES (?, ?, ?, ?)",
            (post_chat_id, post_message_id, file_id, buttons_json),
        )
        conn.commit()
        return cur.lastrowid


def get_post(post_id: int) -> Optional[Tuple[int, int, int, List[Dict[str, str]], str]]:
    """يرجع (post_chat_id, post_message_id, file_id, extra_buttons, created_at) أو None."""
    with get_conn() as conn:
        row = conn.execute(
            "SELECT post_chat_id, post_message_id, file_id, extra_buttons, created_at FROM posts WHERE id = ?",
            (post_id,),
        ).fetchone()
        if not row:
            return None
        post_chat_id, post_message_id, file_id, extra_buttons_json, created_at = row
        try:
            extra_buttons = json.loads(extra_buttons_json) if extra_buttons_json else []
        except Exception:
            extra_buttons = []
        return post_chat_id, post_message_id, file_id, extra_buttons, created_at


def get_recent_posts(limit: int = 10, offset: int = 0) -> List[Tuple[int, int, int, int, str]]:
    """يرجع قائمة بأحدث المنشورات."""
    with get_conn() as conn:
        rows = conn.execute(
            "SELECT id, post_chat_id, post_message_id, file_id, created_at FROM posts ORDER BY id DESC LIMIT ? OFFSET ?",
            (limit, offset),
        ).fetchall()
        return rows


def get_posts_count() -> int:
    """يرجع عدد المنشورات المحفوظة."""
    with get_conn() as conn:
        row = conn.execute("SELECT COUNT(*) FROM posts").fetchone()
        return row[0] if row else 0


def delete_post(post_id: int) -> bool:
    """يحذف منشورًا من قاعدة البيانات."""
    with get_conn() as conn:
        cur = conn.execute("DELETE FROM posts WHERE id = ?", (post_id,))
        conn.commit()
        return cur.rowcount > 0
