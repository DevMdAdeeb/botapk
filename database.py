import sqlite3
from contextlib import contextmanager

from config import DB_PATH


def init_db():
    """ينشئ الجداول إذا لم تكن موجودة."""
    with get_conn() as conn:
        conn.execute("""
            CREATE TABLE IF NOT EXISTS files (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                storage_message_id INTEGER NOT NULL,
                caption TEXT,
                added_by INTEGER,
                created_at TEXT DEFAULT CURRENT_TIMESTAMP
            )
        """)
        conn.execute("""
            CREATE TABLE IF NOT EXISTS settings (
                key TEXT PRIMARY KEY,
                value TEXT
            )
        """)
        # قيم افتراضية
        conn.execute("""
            INSERT OR IGNORE INTO settings (key, value) VALUES ('force_sub_enabled', '0')
        """)
        conn.execute("""
            INSERT OR IGNORE INTO settings (key, value) VALUES ('force_sub_channel', '')
        """)
        conn.commit()


@contextmanager
def get_conn():
    conn = sqlite3.connect(DB_PATH)
    try:
        yield conn
    finally:
        conn.close()


# ---------- الملفات ----------

def add_file(storage_message_id: int, caption: str, added_by: int) -> int:
    """يضيف ملفًا جديدًا ويرجع الـ ID الخاص به لاستخدامه في الرابط."""
    with get_conn() as conn:
        cur = conn.execute(
            "INSERT INTO files (storage_message_id, caption, added_by) VALUES (?, ?, ?)",
            (storage_message_id, caption, added_by),
        )
        conn.commit()
        return cur.lastrowid


def get_file(file_id: int):
    """يرجع (storage_message_id, caption) أو None إذا لم يوجد."""
    with get_conn() as conn:
        row = conn.execute(
            "SELECT storage_message_id, caption FROM files WHERE id = ?",
            (file_id,),
        ).fetchone()
        return row


# ---------- الإعدادات ----------

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


def get_force_sub_channel():
    """يرجع آيدي القناة (كنص) أو يوزرها المخزّن، أو None."""
    with get_conn() as conn:
        row = conn.execute(
            "SELECT value FROM settings WHERE key = 'force_sub_channel'"
        ).fetchone()
        return row[0] if row and row[0] else None


def set_force_sub_channel(channel_id_or_username: str):
    with get_conn() as conn:
        conn.execute(
            "UPDATE settings SET value = ? WHERE key = 'force_sub_channel'",
            (channel_id_or_username,),
        )
        conn.commit()
