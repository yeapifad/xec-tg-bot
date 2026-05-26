"""
Database layer — SQLite via aiosqlite.

Tables:
  members       : TG user_id, tg_username, x_username (registered members)
  engage_rounds : one row per topic / engage round
  round_tweets  : tweet links submitted in a round
  missing_reports: extension-reported missing users per round
"""

import logging
from datetime import datetime
from typing import Optional
import aiosqlite

logger = logging.getLogger(__name__)

CREATE_TABLES = """
PRAGMA journal_mode=WAL;

CREATE TABLE IF NOT EXISTS members (
    user_id       INTEGER PRIMARY KEY,
    tg_username   TEXT,
    x_username    TEXT,
    registered_at TEXT DEFAULT (datetime('now')),
    updated_at    TEXT DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS engage_rounds (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    chat_id     INTEGER NOT NULL,
    topic_id    INTEGER NOT NULL,
    topic_name  TEXT,
    status      TEXT DEFAULT 'open',   -- open | closed
    opened_at   TEXT DEFAULT (datetime('now')),
    closed_at   TEXT
);

CREATE TABLE IF NOT EXISTS round_tweets (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    round_id    INTEGER NOT NULL REFERENCES engage_rounds(id),
    user_id     INTEGER NOT NULL,
    tweet_url   TEXT NOT NULL,
    submitted_at TEXT DEFAULT (datetime('now')),
    UNIQUE(round_id, user_id)          -- one tweet per member per round
);

CREATE TABLE IF NOT EXISTS missing_reports (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    round_id    INTEGER NOT NULL REFERENCES engage_rounds(id),
    x_username  TEXT NOT NULL,
    reported_at TEXT DEFAULT (datetime('now')),
    UNIQUE(round_id, x_username)
);

CREATE INDEX IF NOT EXISTS idx_members_tg ON members(tg_username);
CREATE INDEX IF NOT EXISTS idx_members_x  ON members(x_username);
CREATE INDEX IF NOT EXISTS idx_rounds_topic ON engage_rounds(chat_id, topic_id);
"""


class Database:
    def __init__(self, path: str = "xec.db"):
        self.path = path

    async def init(self):
        async with aiosqlite.connect(self.path) as db:
            await db.executescript(CREATE_TABLES)
            await db.commit()
        logger.info("Database initialised at %s", self.path)

    # ── Members ──────────────────────────────────────────────

    async def upsert_member(self, user_id: int, tg_username: Optional[str], x_username: Optional[str]):
        """Insert or update a member's record."""
        async with aiosqlite.connect(self.path) as db:
            await db.execute("""
                INSERT INTO members (user_id, tg_username, x_username, updated_at)
                VALUES (?, ?, ?, datetime('now'))
                ON CONFLICT(user_id) DO UPDATE SET
                    tg_username = COALESCE(excluded.tg_username, tg_username),
                    x_username  = COALESCE(excluded.x_username,  x_username),
                    updated_at  = excluded.updated_at
            """, (user_id, tg_username, x_username))
            await db.commit()

    async def get_member(self, user_id: int) -> Optional[dict]:
        async with aiosqlite.connect(self.path) as db:
            db.row_factory = aiosqlite.Row
            async with db.execute(
                "SELECT * FROM members WHERE user_id = ?", (user_id,)
            ) as cur:
                row = await cur.fetchone()
                return dict(row) if row else None

    async def get_member_by_x(self, x_username: str) -> Optional[dict]:
        async with aiosqlite.connect(self.path) as db:
            db.row_factory = aiosqlite.Row
            async with db.execute(
                "SELECT * FROM members WHERE LOWER(x_username) = LOWER(?)", (x_username,)
            ) as cur:
                row = await cur.fetchone()
                return dict(row) if row else None

    async def list_members(self) -> list[dict]:
        async with aiosqlite.connect(self.path) as db:
            db.row_factory = aiosqlite.Row
            async with db.execute("SELECT * FROM members ORDER BY registered_at") as cur:
                return [dict(r) for r in await cur.fetchall()]

    # ── Engage Rounds ────────────────────────────────────────

    async def open_round(self, chat_id: int, topic_id: int, topic_name: str = "") -> int:
        """Open a new engage round for a topic. Returns round_id."""
        async with aiosqlite.connect(self.path) as db:
            # Close any previously open round for this topic
            await db.execute("""
                UPDATE engage_rounds SET status='closed', closed_at=datetime('now')
                WHERE chat_id=? AND topic_id=? AND status='open'
            """, (chat_id, topic_id))

            cur = await db.execute("""
                INSERT INTO engage_rounds (chat_id, topic_id, topic_name)
                VALUES (?, ?, ?)
            """, (chat_id, topic_id, topic_name))
            await db.commit()
            return cur.lastrowid

    async def close_round(self, chat_id: int, topic_id: int) -> Optional[dict]:
        """Close the open round for a topic. Returns the round row."""
        async with aiosqlite.connect(self.path) as db:
            db.row_factory = aiosqlite.Row
            async with db.execute("""
                SELECT * FROM engage_rounds
                WHERE chat_id=? AND topic_id=? AND status='open'
                ORDER BY id DESC LIMIT 1
            """, (chat_id, topic_id)) as cur:
                row = await cur.fetchone()
            if not row:
                return None
            await db.execute("""
                UPDATE engage_rounds SET status='closed', closed_at=datetime('now')
                WHERE id=?
            """, (row["id"],))
            await db.commit()
            return dict(row)

    async def get_open_round(self, chat_id: int, topic_id: int) -> Optional[dict]:
        async with aiosqlite.connect(self.path) as db:
            db.row_factory = aiosqlite.Row
            async with db.execute("""
                SELECT * FROM engage_rounds
                WHERE chat_id=? AND topic_id=? AND status='open'
                ORDER BY id DESC LIMIT 1
            """, (chat_id, topic_id)) as cur:
                row = await cur.fetchone()
                return dict(row) if row else None

    async def get_latest_round(self, chat_id: int, topic_id: Optional[int] = None) -> Optional[dict]:
        async with aiosqlite.connect(self.path) as db:
            db.row_factory = aiosqlite.Row
            if topic_id:
                async with db.execute("""
                    SELECT * FROM engage_rounds
                    WHERE chat_id=? AND topic_id=?
                    ORDER BY id DESC LIMIT 1
                """, (chat_id, topic_id)) as cur:
                    row = await cur.fetchone()
            else:
                async with db.execute("""
                    SELECT * FROM engage_rounds
                    WHERE chat_id=?
                    ORDER BY id DESC LIMIT 1
                """, (chat_id,)) as cur:
                    row = await cur.fetchone()
            return dict(row) if row else None

    # ── Round Tweets ─────────────────────────────────────────

    async def add_tweet(self, round_id: int, user_id: int, tweet_url: str):
        async with aiosqlite.connect(self.path) as db:
            await db.execute("""
                INSERT INTO round_tweets (round_id, user_id, tweet_url)
                VALUES (?, ?, ?)
                ON CONFLICT(round_id, user_id) DO UPDATE SET
                    tweet_url    = excluded.tweet_url,
                    submitted_at = datetime('now')
            """, (round_id, user_id, tweet_url))
            await db.commit()

    async def get_round_tweets(self, round_id: int) -> list[dict]:
        async with aiosqlite.connect(self.path) as db:
            db.row_factory = aiosqlite.Row
            async with db.execute("""
                SELECT rt.*, m.tg_username, m.x_username
                FROM round_tweets rt
                LEFT JOIN members m ON m.user_id = rt.user_id
                WHERE rt.round_id = ?
                ORDER BY rt.submitted_at
            """, (round_id,)) as cur:
                return [dict(r) for r in await cur.fetchall()]

    # ── Missing Reports ───────────────────────────────────────

    async def save_missing_report(self, round_id: int, x_usernames: list[str]):
        async with aiosqlite.connect(self.path) as db:
            await db.executemany("""
                INSERT OR IGNORE INTO missing_reports (round_id, x_username)
                VALUES (?, ?)
            """, [(round_id, u.lower().lstrip("@")) for u in x_usernames])
            await db.commit()

    async def get_missing_with_tg(self, round_id: int) -> list[dict]:
        """Return missing users with their TG info (if registered)."""
        async with aiosqlite.connect(self.path) as db:
            db.row_factory = aiosqlite.Row
            async with db.execute("""
                SELECT mr.x_username,
                       m.tg_username,
                       m.user_id
                FROM missing_reports mr
                LEFT JOIN members m ON LOWER(m.x_username) = LOWER(mr.x_username)
                WHERE mr.round_id = ?
                ORDER BY mr.x_username
            """, (round_id,)) as cur:
                return [dict(r) for r in await cur.fetchall()]
