"""Database layer for storing user preferences and notification history."""

import os

import aiosqlite

DB_PATH = os.path.join(os.environ.get("DATA_DIR", os.path.dirname(__file__)), "bot.db")


async def get_db() -> aiosqlite.Connection:
    db = await aiosqlite.connect(DB_PATH)
    db.row_factory = aiosqlite.Row
    await db.execute("PRAGMA journal_mode=WAL")
    return db


async def init_db() -> None:
    """Create tables if they don't exist."""
    db = await get_db()
    try:
        await db.executescript("""
            CREATE TABLE IF NOT EXISTS users (
                chat_id INTEGER PRIMARY KEY,
                lat REAL,
                lon REAL,
                fuel_type TEXT,
                is_active INTEGER DEFAULT 1,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            );

            CREATE TABLE IF NOT EXISTS notifications (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                chat_id INTEGER NOT NULL,
                osm_id TEXT NOT NULL,
                message_id INTEGER NOT NULL,
                notified_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                UNIQUE(chat_id, osm_id)
            );

            CREATE INDEX IF NOT EXISTS idx_notifications_chat
                ON notifications(chat_id);
        """)
        await db.commit()
    finally:
        await db.close()


async def upsert_user(chat_id: int, lat: float, lon: float, fuel_type: str) -> None:
    """Insert or update user preferences."""
    db = await get_db()
    try:
        await db.execute(
            """
            INSERT INTO users (chat_id, lat, lon, fuel_type, is_active, updated_at)
            VALUES (?, ?, ?, ?, 1, CURRENT_TIMESTAMP)
            ON CONFLICT(chat_id) DO UPDATE SET
                lat = excluded.lat,
                lon = excluded.lon,
                fuel_type = excluded.fuel_type,
                is_active = 1,
                updated_at = CURRENT_TIMESTAMP
            """,
            (chat_id, lat, lon, fuel_type),
        )
        await db.commit()
    finally:
        await db.close()


async def set_user_active(chat_id: int, active: bool) -> None:
    db = await get_db()
    try:
        await db.execute(
            "UPDATE users SET is_active = ?, updated_at = CURRENT_TIMESTAMP WHERE chat_id = ?",
            (1 if active else 0, chat_id),
        )
        await db.commit()
    finally:
        await db.close()


async def get_active_users() -> list[dict]:
    """Return all users with active monitoring."""
    db = await get_db()
    try:
        cursor = await db.execute(
            "SELECT chat_id, lat, lon, fuel_type FROM users WHERE is_active = 1"
        )
        rows = await cursor.fetchall()
        return [dict(row) for row in rows]
    finally:
        await db.close()


async def get_user(chat_id: int) -> dict | None:
    db = await get_db()
    try:
        cursor = await db.execute(
            "SELECT chat_id, lat, lon, fuel_type, is_active FROM users WHERE chat_id = ?",
            (chat_id,),
        )
        row = await cursor.fetchone()
        return dict(row) if row else None
    finally:
        await db.close()


async def add_notification(chat_id: int, osm_id: str, message_id: int) -> bool:
    """Add a notification record with message_id. Returns True if it was new."""
    db = await get_db()
    try:
        await db.execute(
            "INSERT OR IGNORE INTO notifications (chat_id, osm_id, message_id) VALUES (?, ?, ?)",
            (chat_id, osm_id, message_id),
        )
        await db.commit()
        return db.total_changes > 0
    finally:
        await db.close()


async def update_notification_message(
    chat_id: int, osm_id: str, message_id: int
) -> None:
    """Update the message_id for an existing notification."""
    db = await get_db()
    try:
        await db.execute(
            "UPDATE notifications SET message_id = ? WHERE chat_id = ? AND osm_id = ?",
            (message_id, chat_id, osm_id),
        )
        await db.commit()
    finally:
        await db.close()


async def remove_notification(chat_id: int, osm_id: str) -> None:
    """Remove a notification record."""
    db = await get_db()
    try:
        await db.execute(
            "DELETE FROM notifications WHERE chat_id = ? AND osm_id = ?",
            (chat_id, osm_id),
        )
        await db.commit()
    finally:
        await db.close()


async def get_notifications_for_user(chat_id: int) -> dict[str, int]:
    """Get dict of {osm_id: message_id} for a user."""
    db = await get_db()
    try:
        cursor = await db.execute(
            "SELECT osm_id, message_id FROM notifications WHERE chat_id = ?",
            (chat_id,),
        )
        rows = await cursor.fetchall()
        return {row["osm_id"]: row["message_id"] for row in rows}
    finally:
        await db.close()


async def delete_all_notifications_for_user(chat_id: int) -> list[int]:
    """Delete all notifications for a user, return list of message_ids to clean up."""
    db = await get_db()
    try:
        cursor = await db.execute(
            "SELECT message_id FROM notifications WHERE chat_id = ?",
            (chat_id,),
        )
        rows = await cursor.fetchall()
        message_ids = [row["message_id"] for row in rows]
        await db.execute("DELETE FROM notifications WHERE chat_id = ?", (chat_id,))
        await db.commit()
        return message_ids
    finally:
        await db.close()
