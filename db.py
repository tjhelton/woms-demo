"""Simulated IssueTracker database backed by SQLite."""

from __future__ import annotations

import aiosqlite
import hashlib
import json
import os
from datetime import datetime, timezone
from pathlib import Path

_default_db = Path(__file__).parent / "issuetracker.db"
DB_PATH = Path(os.getenv("DB_PATH", str(_default_db)))

# --- Status mappings ---
# SC API uses UUIDs for status on create/update
SC_STATUS_UUID = {
    "todo":        "17e793a1-26a3-4ecd-99ca-f38ecc6eaa2e",
    "in_progress": "20ce0cb1-387a-47d4-8c34-bc6fd3be0e27",
    "complete":    "7223d809-553e-4714-a038-62dc98f3fbf3",
    "cant_do":     "06308884-41c2-4ee0-9da7-5676647d3d75",
}

# Feed returns uppercase strings: "TODO", "IN PROGRESS", "DONE", "CAN'T DO"
# Action detail returns UUIDs in status_id
# Map both forms → IssueTracker status
SC_STATUS_TO_ISSUETRACKER = {
    # UUID form (from action detail / list) — most reliable, preferred lookup
    "17e793a1-26a3-4ecd-99ca-f38ecc6eaa2e": "Open",
    "20ce0cb1-387a-47d4-8c34-bc6fd3be0e27": "In Progress",
    "7223d809-553e-4714-a038-62dc98f3fbf3": "Completed",
    "06308884-41c2-4ee0-9da7-5676647d3d75": "Cancelled",
    # Feed string form — SC feed uses underscore-separated uppercase keys
    "TODO":        "Open",
    "TO_DO":       "Open",
    "IN PROGRESS": "In Progress",
    "IN_PROGRESS": "In Progress",
    "DONE":        "Completed",
    "COMPLETE":    "Completed",
    "CANT_DO":     "Cancelled",
    "CAN'T DO":    "Cancelled",
}

ISSUETRACKER_STATUS_TO_SC = {
    "Open":        "17e793a1-26a3-4ecd-99ca-f38ecc6eaa2e",
    "In Progress": "20ce0cb1-387a-47d4-8c34-bc6fd3be0e27",
    "Completed":   "7223d809-553e-4714-a038-62dc98f3fbf3",
    "Cancelled":   "06308884-41c2-4ee0-9da7-5676647d3d75",
}

# --- Priority mappings ---
# SC API uses UUIDs for priority on create/update
SC_PRIORITY_UUID = {
    "none":   "58941717-817f-4c7c-a6f6-5cd05e2bbfde",
    "low":    "16ba4717-adc9-4d48-bf7c-044cfe0d2727",
    "medium": "ce87c58a-eeb2-4fde-9dc4-c6e85f1f4055",
    "high":   "02eb40c1-4f46-40c5-be16-d32941c96ec9",
}

# Map both UUID and feed-string forms → IssueTracker priority
SC_PRIORITY_TO_ISSUETRACKER = {
    # UUID form
    "58941717-817f-4c7c-a6f6-5cd05e2bbfde": "None",
    "16ba4717-adc9-4d48-bf7c-044cfe0d2727": "Low",
    "ce87c58a-eeb2-4fde-9dc4-c6e85f1f4055": "Medium",
    "02eb40c1-4f46-40c5-be16-d32941c96ec9": "High",
    # Feed string form
    "NONE":   "None",
    "LOW":    "Low",
    "MEDIUM": "Medium",
    "HIGH":   "High",
}

ISSUETRACKER_PRIORITY_TO_SC = {
    "None":   "58941717-817f-4c7c-a6f6-5cd05e2bbfde",
    "Low":    "16ba4717-adc9-4d48-bf7c-044cfe0d2727",
    "Medium": "ce87c58a-eeb2-4fde-9dc4-c6e85f1f4055",
    "High":   "02eb40c1-4f46-40c5-be16-d32941c96ec9",
}

ISSUETRACKER_STATUSES = ["Open", "In Progress", "Completed", "Cancelled"]
ISSUETRACKER_PRIORITIES = ["None", "Low", "Medium", "High"]


_pool: list[aiosqlite.Connection] = []
_pool_size = 3


async def get_db() -> aiosqlite.Connection:
    if _pool:
        return _pool.pop()
    conn = await aiosqlite.connect(str(DB_PATH))
    conn.row_factory = aiosqlite.Row
    await conn.execute("PRAGMA journal_mode=WAL")
    await conn.execute("PRAGMA busy_timeout=5000")
    return conn


async def release_db(conn: aiosqlite.Connection):
    """Return a connection to the pool instead of closing it."""
    if len(_pool) < _pool_size:
        _pool.append(conn)
    else:
        await conn.close()


async def init_db():
    db = await get_db()
    try:
        await db.executescript("""
            CREATE TABLE IF NOT EXISTS work_orders (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                wo_number TEXT UNIQUE NOT NULL,
                title TEXT NOT NULL,
                description TEXT DEFAULT '',
                status TEXT DEFAULT 'Open',
                priority TEXT DEFAULT 'None',
                assignee TEXT DEFAULT '',
                location TEXT DEFAULT '',
                asset TEXT DEFAULT '',
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                due_date TEXT,
                sc_action_id TEXT,
                sc_last_synced TEXT,
                metadata TEXT DEFAULT '{}'
            );

            CREATE TABLE IF NOT EXISTS sync_log (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                timestamp TEXT NOT NULL,
                direction TEXT NOT NULL,
                event_type TEXT NOT NULL,
                sc_action_id TEXT,
                wo_number TEXT,
                details TEXT DEFAULT '',
                status TEXT DEFAULT 'success'
            );

            CREATE TABLE IF NOT EXISTS comments (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                wo_id INTEGER NOT NULL,
                author TEXT DEFAULT '',
                body TEXT NOT NULL,
                sc_item_id TEXT,
                source TEXT DEFAULT 'issuetracker',
                created_at TEXT NOT NULL,
                FOREIGN KEY (wo_id) REFERENCES work_orders(id)
            );

            CREATE TABLE IF NOT EXISTS sc_sites (
                id TEXT PRIMARY KEY,
                name TEXT NOT NULL,
                parent_id TEXT,
                deleted INTEGER DEFAULT 0
            );

            CREATE TABLE IF NOT EXISTS sc_users (
                id TEXT PRIMARY KEY,
                email TEXT,
                firstname TEXT,
                lastname TEXT,
                active INTEGER DEFAULT 1
            );

            CREATE TABLE IF NOT EXISTS sc_groups (
                id TEXT PRIMARY KEY,
                name TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS sc_assets (
                id TEXT PRIMARY KEY,
                code TEXT,
                type_name TEXT,
                site_id TEXT,
                site_name TEXT
            );

            CREATE TABLE IF NOT EXISTS deleted_sc_actions (
                sc_action_id TEXT PRIMARY KEY,
                deleted_at TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS media (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                wo_id INTEGER NOT NULL,
                label TEXT NOT NULL,
                source TEXT NOT NULL,
                sc_media_id TEXT,
                sc_media_token TEXT,
                sc_inspection_id TEXT,
                local_filename TEXT,
                media_type TEXT DEFAULT 'image',
                created_at TEXT NOT NULL,
                FOREIGN KEY (wo_id) REFERENCES work_orders(id)
            );

            CREATE TABLE IF NOT EXISTS config (
                key TEXT PRIMARY KEY,
                value TEXT NOT NULL
            );

            CREATE INDEX IF NOT EXISTS idx_wo_sc_action ON work_orders(sc_action_id);
            CREATE INDEX IF NOT EXISTS idx_sync_log_ts ON sync_log(timestamp DESC);
            CREATE INDEX IF NOT EXISTS idx_comments_wo ON comments(wo_id);
            CREATE UNIQUE INDEX IF NOT EXISTS idx_comments_sc_item ON comments(sc_item_id) WHERE sc_item_id IS NOT NULL;
            CREATE UNIQUE INDEX IF NOT EXISTS idx_media_sc_id ON media(sc_media_id) WHERE sc_media_id IS NOT NULL;
            CREATE INDEX IF NOT EXISTS idx_media_wo ON media(wo_id);
        """)
        await db.commit()
    finally:
        await release_db(db)


def _token_hash(token: str) -> str:
    """Return a SHA-256 hex digest of the token (never store the raw token)."""
    return hashlib.sha256(token.encode()).hexdigest()


async def check_token_and_reset(token: str) -> bool:
    """Compare the current SC API token against the one stored in the database.

    If the token has changed (i.e. a different SC account), wipe all data so the
    sync engine starts fresh.  Returns True if a reset occurred.
    """
    if not token:
        return False

    current_hash = _token_hash(token)
    conn = await get_db()
    try:
        cursor = await conn.execute(
            "SELECT value FROM config WHERE key = 'sc_token_hash'"
        )
        row = await cursor.fetchone()
        stored_hash = row["value"] if row else None

        if stored_hash == current_hash:
            return False

        # Token changed (or first run with a real token) — reset data
        if stored_hash is not None:
            # Only log when switching, not on first-ever run
            import logging
            logging.getLogger("db").info(
                "SC API token changed — resetting database for new account"
            )
            await conn.executescript("""
                DELETE FROM media;
                DELETE FROM comments;
                DELETE FROM work_orders;
                DELETE FROM sync_log;
                DELETE FROM deleted_sc_actions;
                DELETE FROM sc_sites;
                DELETE FROM sc_users;
                DELETE FROM sc_groups;
                DELETE FROM sc_assets;
            """)

            # Clean up local upload files
            uploads_dir = Path(__file__).parent / "static" / "uploads"
            if uploads_dir.exists():
                for f in uploads_dir.iterdir():
                    if f.name != ".gitkeep":
                        f.unlink(missing_ok=True)

        # Store the new token hash
        await conn.execute(
            """INSERT INTO config (key, value) VALUES ('sc_token_hash', ?)
               ON CONFLICT(key) DO UPDATE SET value = excluded.value""",
            (current_hash,),
        )
        await conn.commit()
        return stored_hash is not None
    finally:
        await release_db(conn)


def _generate_wo_number(row_id: int) -> str:
    return f"RCL-WO-{row_id:05d}"


async def create_work_order(
    title: str,
    description: str = "",
    status: str = "Open",
    priority: str = "None",
    assignee: str = "",
    location: str = "",
    asset: str = "",
    due_date: str | None = None,
    sc_action_id: str | None = None,
    metadata: dict | None = None,
) -> dict:
    now = datetime.now(timezone.utc).isoformat()
    db = await get_db()
    try:
        cursor = await db.execute(
            """INSERT INTO work_orders
               (wo_number, title, description, status, priority, assignee,
                location, asset, created_at, updated_at, due_date,
                sc_action_id, sc_last_synced, metadata)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                "TEMP", title, description, status, priority, assignee,
                location, asset, now, now, due_date,
                sc_action_id, now if sc_action_id else None,
                json.dumps(metadata or {}),
            ),
        )
        row_id = cursor.lastrowid
        wo_number = _generate_wo_number(row_id)
        await db.execute(
            "UPDATE work_orders SET wo_number = ? WHERE id = ?",
            (wo_number, row_id),
        )
        await db.commit()
        return await get_work_order(row_id, db)
    finally:
        await release_db(db)


async def get_work_order(wo_id: int, db: aiosqlite.Connection | None = None) -> dict | None:
    close = db is None
    if db is None:
        db = await get_db()
    try:
        cursor = await db.execute("SELECT * FROM work_orders WHERE id = ?", (wo_id,))
        row = await cursor.fetchone()
        return dict(row) if row else None
    finally:
        if close:
            await release_db(db)


async def get_work_order_by_sc_id(sc_action_id: str) -> dict | None:
    db = await get_db()
    try:
        cursor = await db.execute(
            "SELECT * FROM work_orders WHERE sc_action_id = ?", (sc_action_id,)
        )
        row = await cursor.fetchone()
        return dict(row) if row else None
    finally:
        await release_db(db)


async def get_work_order_by_number(wo_number: str) -> dict | None:
    db = await get_db()
    try:
        cursor = await db.execute(
            "SELECT * FROM work_orders WHERE wo_number = ?", (wo_number,)
        )
        row = await cursor.fetchone()
        return dict(row) if row else None
    finally:
        await release_db(db)


async def list_work_orders() -> list[dict]:
    db = await get_db()
    try:
        cursor = await db.execute(
            "SELECT * FROM work_orders ORDER BY updated_at DESC"
        )
        rows = await cursor.fetchall()
        return [dict(r) for r in rows]
    finally:
        await release_db(db)


async def update_work_order(wo_id: int, **fields) -> dict | None:
    if not fields:
        return await get_work_order(wo_id)

    now = datetime.now(timezone.utc).isoformat()
    fields["updated_at"] = now

    set_clause = ", ".join(f"{k} = ?" for k in fields)
    values = list(fields.values()) + [wo_id]

    db = await get_db()
    try:
        await db.execute(
            f"UPDATE work_orders SET {set_clause} WHERE id = ?", values
        )
        await db.commit()
        return await get_work_order(wo_id, db)
    finally:
        await release_db(db)


async def delete_work_order(wo_id: int):
    db = await get_db()
    try:
        await db.execute("DELETE FROM comments WHERE wo_id = ?", (wo_id,))
        await db.execute("DELETE FROM media WHERE wo_id = ?", (wo_id,))
        await db.execute("DELETE FROM work_orders WHERE id = ?", (wo_id,))
        await db.commit()
    finally:
        await release_db(db)


async def bulk_delete_work_orders(wo_ids: list[int]) -> list[dict]:
    """Delete multiple work orders. Returns the list of deleted WOs (with sc_action_id) before removal."""
    if not wo_ids:
        return []
    db = await get_db()
    try:
        placeholders = ",".join("?" for _ in wo_ids)
        cursor = await db.execute(
            f"SELECT id, wo_number, sc_action_id FROM work_orders WHERE id IN ({placeholders})",
            wo_ids,
        )
        rows = [dict(r) for r in await cursor.fetchall()]
        await db.execute(f"DELETE FROM comments WHERE wo_id IN ({placeholders})", wo_ids)
        await db.execute(f"DELETE FROM media WHERE wo_id IN ({placeholders})", wo_ids)
        await db.execute(f"DELETE FROM work_orders WHERE id IN ({placeholders})", wo_ids)
        await db.commit()
        return rows
    finally:
        await release_db(db)


async def add_sync_log(
    direction: str,
    event_type: str,
    sc_action_id: str | None = None,
    wo_number: str | None = None,
    details: str = "",
    status: str = "success",
):
    now = datetime.now(timezone.utc).isoformat()
    db = await get_db()
    try:
        await db.execute(
            """INSERT INTO sync_log (timestamp, direction, event_type,
               sc_action_id, wo_number, details, status)
               VALUES (?, ?, ?, ?, ?, ?, ?)""",
            (now, direction, event_type, sc_action_id, wo_number, details, status),
        )
        await db.commit()
    finally:
        await release_db(db)


async def get_sync_logs(limit: int = 50) -> list[dict]:
    db = await get_db()
    try:
        cursor = await db.execute(
            "SELECT * FROM sync_log ORDER BY timestamp DESC LIMIT ?", (limit,)
        )
        rows = await cursor.fetchall()
        return [dict(r) for r in rows]
    finally:
        await release_db(db)


async def add_comment(
    wo_id: int,
    author: str = "",
    body: str = "",
    sc_item_id: str | None = None,
    source: str = "issuetracker",
    created_at: str | None = None,
) -> dict:
    now = datetime.now(timezone.utc).isoformat()
    ts = created_at or now
    db = await get_db()
    try:
        cursor = await db.execute(
            """INSERT INTO comments (wo_id, author, body, sc_item_id, source, created_at)
               VALUES (?, ?, ?, ?, ?, ?)""",
            (wo_id, author, body, sc_item_id, source, ts),
        )
        comment_id = cursor.lastrowid
        await db.commit()
        cursor2 = await db.execute("SELECT * FROM comments WHERE id = ?", (comment_id,))
        row = await cursor2.fetchone()
        return dict(row) if row else {}
    finally:
        await release_db(db)


async def get_comments(wo_id: int) -> list[dict]:
    db = await get_db()
    try:
        cursor = await db.execute(
            "SELECT * FROM comments WHERE wo_id = ? ORDER BY created_at ASC",
            (wo_id,),
        )
        rows = await cursor.fetchall()
        return [dict(r) for r in rows]
    finally:
        await release_db(db)


# ---------------------------------------------------------------------------
# SC Objects (sites, users, groups, assets) — cached reference data
# ---------------------------------------------------------------------------

async def upsert_sc_sites(sites: list[dict]):
    db = await get_db()
    try:
        for s in sites:
            await db.execute(
                """INSERT INTO sc_sites (id, name, parent_id, deleted)
                   VALUES (?, ?, ?, ?)
                   ON CONFLICT(id) DO UPDATE SET name=excluded.name,
                   parent_id=excluded.parent_id, deleted=excluded.deleted""",
                (s.get("id") or s.get("site_uuid", ""), s.get("name", ""),
                 s.get("parent_id"), 1 if s.get("deleted") else 0),
            )
        await db.commit()
    finally:
        await release_db(db)


async def upsert_sc_users(users: list[dict]):
    db = await get_db()
    try:
        for u in users:
            await db.execute(
                """INSERT INTO sc_users (id, email, firstname, lastname, active)
                   VALUES (?, ?, ?, ?, ?)
                   ON CONFLICT(id) DO UPDATE SET email=excluded.email,
                   firstname=excluded.firstname, lastname=excluded.lastname,
                   active=excluded.active""",
                (u.get("id", ""), u.get("email", ""),
                 u.get("firstname", ""), u.get("lastname", ""),
                 1 if u.get("active", True) else 0),
            )
        await db.commit()
    finally:
        await release_db(db)


async def upsert_sc_groups(groups: list[dict]):
    db = await get_db()
    try:
        for g in groups:
            await db.execute(
                """INSERT INTO sc_groups (id, name)
                   VALUES (?, ?)
                   ON CONFLICT(id) DO UPDATE SET name=excluded.name""",
                (g.get("id", ""), g.get("name", "")),
            )
        await db.commit()
    finally:
        await release_db(db)


async def upsert_sc_assets(assets: list[dict]):
    """Replace the entire sc_assets table with the provided list.

    Using a full replace (DELETE + INSERT) ensures that assets removed from
    SafetyCulture are also removed from the local cache.  Passing an empty
    list clears the table, which is the correct behaviour when SC has no assets.
    """
    db = await get_db()
    try:
        await db.execute("DELETE FROM sc_assets")
        for a in assets:
            type_obj = a.get("type") or {}
            site_obj = a.get("site") or {}
            await db.execute(
                """INSERT INTO sc_assets (id, code, type_name, site_id, site_name)
                   VALUES (?, ?, ?, ?, ?)""",
                (a.get("id", ""), a.get("code", ""),
                 type_obj.get("name", ""), site_obj.get("id", ""),
                 site_obj.get("name", "")),
            )
        await db.commit()
    finally:
        await release_db(db)


async def get_sc_objects() -> dict:
    """Return all cached SC reference objects."""
    db = await get_db()
    try:
        sites  = [dict(r) for r in await (await db.execute(
            "SELECT * FROM sc_sites WHERE deleted=0 ORDER BY name")).fetchall()]
        users  = [dict(r) for r in await (await db.execute(
            "SELECT * FROM sc_users WHERE active=1 ORDER BY firstname, lastname")).fetchall()]
        groups = [dict(r) for r in await (await db.execute(
            "SELECT * FROM sc_groups ORDER BY name")).fetchall()]
        assets = [dict(r) for r in await (await db.execute(
            "SELECT * FROM sc_assets ORDER BY code, type_name")).fetchall()]
        return {"sites": sites, "users": users, "groups": groups, "assets": assets}
    finally:
        await release_db(db)


async def get_sc_site_name(site_id: str) -> str | None:
    """Resolve a SC site UUID to its cached name."""
    db = await get_db()
    try:
        cursor = await db.execute("SELECT name FROM sc_sites WHERE id = ?", (site_id,))
        row = await cursor.fetchone()
        return row["name"] if row else None
    finally:
        await release_db(db)


async def get_sc_asset_code(asset_id: str) -> str | None:
    """Resolve a SC asset UUID to its cached code."""
    db = await get_db()
    try:
        cursor = await db.execute("SELECT code FROM sc_assets WHERE id = ?", (asset_id,))
        row = await cursor.fetchone()
        return row["code"] if row else None
    finally:
        await release_db(db)


async def get_sc_user_id_by_name(name: str) -> str | None:
    """Resolve a display name (e.g. 'Jane Smith') to a SC user UUID."""
    db = await get_db()
    try:
        cursor = await db.execute(
            "SELECT id FROM sc_users WHERE TRIM(firstname || ' ' || lastname) = ? COLLATE NOCASE",
            (name,),
        )
        row = await cursor.fetchone()
        if row:
            return row["id"]
        # Fallback: match by email or partial name
        cursor = await db.execute(
            "SELECT id FROM sc_users WHERE email = ? COLLATE NOCASE OR firstname = ? COLLATE NOCASE",
            (name, name),
        )
        row = await cursor.fetchone()
        return row["id"] if row else None
    finally:
        await release_db(db)


async def get_sc_site_id_by_name(name: str) -> str | None:
    """Resolve a site name to a SC site UUID."""
    db = await get_db()
    try:
        cursor = await db.execute(
            "SELECT id FROM sc_sites WHERE name = ? COLLATE NOCASE AND deleted = 0",
            (name,),
        )
        row = await cursor.fetchone()
        return row["id"] if row else None
    finally:
        await release_db(db)


async def get_sc_asset_id_by_code(code: str) -> str | None:
    """Resolve an asset code to a SC asset UUID."""
    db = await get_db()
    try:
        cursor = await db.execute(
            "SELECT id FROM sc_assets WHERE code = ? COLLATE NOCASE",
            (code,),
        )
        row = await cursor.fetchone()
        return row["id"] if row else None
    finally:
        await release_db(db)


async def comment_exists_by_sc_id(sc_item_id: str) -> bool:
    db = await get_db()
    try:
        cursor = await db.execute(
            "SELECT id FROM comments WHERE sc_item_id = ?", (sc_item_id,)
        )
        row = await cursor.fetchone()
        return row is not None
    finally:
        await release_db(db)


# ---------------------------------------------------------------------------
# Deleted SC actions — prevent the sync engine from re-creating deleted WOs
# ---------------------------------------------------------------------------

async def mark_sc_action_deleted(sc_action_id: str):
    """Record that a SC action was intentionally deleted so the sync engine
    won't recreate the corresponding work order."""
    now = datetime.now(timezone.utc).isoformat()
    db = await get_db()
    try:
        await db.execute(
            """INSERT INTO deleted_sc_actions (sc_action_id, deleted_at)
               VALUES (?, ?)
               ON CONFLICT(sc_action_id) DO UPDATE SET deleted_at=excluded.deleted_at""",
            (sc_action_id, now),
        )
        await db.commit()
    finally:
        await release_db(db)


async def get_tracked_sc_actions() -> list[dict]:
    """Return all work orders that have a linked SC action ID."""
    db = await get_db()
    try:
        cursor = await db.execute(
            "SELECT id, wo_number, sc_action_id FROM work_orders WHERE sc_action_id IS NOT NULL"
        )
        rows = await cursor.fetchall()
        return [dict(r) for r in rows]
    finally:
        await release_db(db)


async def is_sc_action_deleted(sc_action_id: str) -> bool:
    """Check if a SC action ID was previously deleted by the user."""
    db = await get_db()
    try:
        cursor = await db.execute(
            "SELECT sc_action_id FROM deleted_sc_actions WHERE sc_action_id = ?",
            (sc_action_id,),
        )
        row = await cursor.fetchone()
        return row is not None
    finally:
        await release_db(db)


# ---------------------------------------------------------------------------
# Media — attachments from SC and uploads from IssueTracker
# ---------------------------------------------------------------------------

async def add_media(
    wo_id: int,
    label: str,
    source: str,
    sc_media_id: str | None = None,
    sc_media_token: str | None = None,
    sc_inspection_id: str | None = None,
    local_filename: str | None = None,
    media_type: str = "image",
) -> dict:
    now = datetime.now(timezone.utc).isoformat()
    db = await get_db()
    try:
        cursor = await db.execute(
            """INSERT INTO media
               (wo_id, label, source, sc_media_id, sc_media_token,
                sc_inspection_id, local_filename, media_type, created_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (wo_id, label, source, sc_media_id, sc_media_token,
             sc_inspection_id, local_filename, media_type, now),
        )
        media_id = cursor.lastrowid
        await db.commit()
        cursor2 = await db.execute("SELECT * FROM media WHERE id = ?", (media_id,))
        row = await cursor2.fetchone()
        return dict(row) if row else {}
    finally:
        await release_db(db)


async def get_media(wo_id: int) -> list[dict]:
    db = await get_db()
    try:
        cursor = await db.execute(
            "SELECT * FROM media WHERE wo_id = ? ORDER BY created_at ASC",
            (wo_id,),
        )
        rows = await cursor.fetchall()
        return [dict(r) for r in rows]
    finally:
        await release_db(db)


async def get_all_media() -> list[dict]:
    """Return all media records joined with work order info."""
    db = await get_db()
    try:
        cursor = await db.execute(
            """SELECT m.*, w.wo_number, w.title AS wo_title
               FROM media m
               JOIN work_orders w ON w.id = m.wo_id
               ORDER BY w.updated_at DESC, m.created_at ASC"""
        )
        rows = await cursor.fetchall()
        return [dict(r) for r in rows]
    finally:
        await release_db(db)


async def delete_media(media_id: int) -> dict | None:
    db = await get_db()
    try:
        cursor = await db.execute("SELECT * FROM media WHERE id = ?", (media_id,))
        row = await cursor.fetchone()
        if not row:
            return None
        record = dict(row)
        await db.execute("DELETE FROM media WHERE id = ?", (media_id,))
        await db.commit()
        return record
    finally:
        await release_db(db)


async def media_exists_by_sc_id(sc_media_id: str) -> bool:
    db = await get_db()
    try:
        cursor = await db.execute(
            "SELECT id FROM media WHERE sc_media_id = ?", (sc_media_id,)
        )
        row = await cursor.fetchone()
        return row is not None
    finally:
        await release_db(db)
