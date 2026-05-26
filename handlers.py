"""
handlers.py — All bot message & command handlers.

Silent tracking rules:
  • Any message in the engage group containing an x.com/twitter.com link
    → extract tweet URL + X username from URL
    → save TG username + X username silently (no reply, no reaction)

  • /join @xusername  (in group or DM)
    → register/update member's X username

Topic lifecycle:
  • /open  [topic name]  → open a new engage round for current topic
  • /close               → close current topic, send tweet list to admin

Admin commands (admin only):
  • /missing [round_id]  → show missing list with TG + X usernames
  • /members             → list all registered members
  • /rounds              → list recent rounds
  • /report <round_id> @x1 @x2 ...  → manually submit missing list
  • /setx @tguser @xuser            → admin manually maps a user

Webhook endpoint:
  • POST /report  { round_id, missing: ["@x1","@x2"] }
    → receives missing list from extension
"""

import logging
import re
from typing import Optional

from aiogram import Dispatcher, F, Bot
from aiogram.filters import Command, CommandObject
from aiogram.types import Message, ForumTopicClosed, ForumTopicCreated
from aiogram.types import InlineKeyboardMarkup, InlineKeyboardButton

from database import Database
from config import Config
from utils import (
    extract_tweet_url,
    extract_x_username_from_url,
    chunk_text,
    fmt_user,
    is_tweet_url,
)

logger = logging.getLogger(__name__)


def register_handlers(dp: Dispatcher, db: Database, config: Config):
    """Register all handlers onto the dispatcher."""

    def is_admin(user_id: int) -> bool:
        return user_id in config.admin_ids

    def is_engage_group(chat_id: int) -> bool:
        return chat_id == config.engage_group

    # ── /start & /help ────────────────────────────────────────

    @dp.message(Command("start", "help"))
    async def cmd_help(msg: Message):
        if msg.chat.type != "private":
            return  # ignore in groups
        await msg.answer(
            "<b>XEC Engage Bot</b>\n\n"
            "<b>Register your X account:</b>\n"
            "  /join @your_x_username\n\n"
            "<b>Admin commands:</b>\n"
            "  /open [name]    — open engage round\n"
            "  /close          — close round, get tweet list\n"
            "  /missing        — show missing users\n"
            "  /members        — list all members\n"
            "  /rounds         — recent rounds\n"
            "  /setx @tg @x   — manually map a user\n"
        )

    # ── /join — register X username ───────────────────────────

    @dp.message(Command("join"))
    async def cmd_join(msg: Message, command: CommandObject):
        user = msg.from_user
        if not user:
            return

        args = (command.args or "").strip()
        x_username = args.lstrip("@").lower() if args else None

        if not x_username:
            if msg.chat.type == "private":
                await msg.answer("Usage: /join @your_x_username")
            return

        tg_username = user.username or None
        await db.upsert_member(user.id, tg_username, x_username)
        logger.info("Member registered: TG=%s X=%s", tg_username, x_username)

        if msg.chat.type == "private":
            await msg.answer(f"✅ Registered: @{tg_username or user.id} → @{x_username}")
        else:
            # In group: delete command message silently if bot has permission
            try:
                await msg.delete()
            except Exception:
                pass

    # ── Silent tweet tracking in engage group ─────────────────

    @dp.message(F.chat.id == config.engage_group)
    async def track_engage_message(msg: Message):
        if not msg.from_user or not msg.text:
            return

        text = msg.text or msg.caption or ""
        tweet_url = extract_tweet_url(text)
        if not tweet_url:
            return

        user    = msg.from_user
        user_id = user.id
        tg_username = user.username or None

        # Extract X username from the tweet URL owner
        x_from_url = extract_x_username_from_url(tweet_url)

        # Update member record silently
        await db.upsert_member(user_id, tg_username, x_from_url)

        # Find the open round for this topic
        topic_id = msg.message_thread_id or 0
        round_row = await db.get_open_round(config.engage_group, topic_id)

        if round_row:
            await db.add_tweet(round_row["id"], user_id, tweet_url)
            logger.info(
                "Tweet saved: round=%s user=%s url=%s",
                round_row["id"], user_id, tweet_url
            )

    # ── /open — open engage round (admin) ─────────────────────

    @dp.message(Command("open"), F.chat.id == config.engage_group)
    async def cmd_open(msg: Message, command: CommandObject):
        if not is_admin(msg.from_user.id):
            return
        topic_id   = msg.message_thread_id or 0
        topic_name = command.args or f"Round {topic_id}"
        round_id   = await db.open_round(config.engage_group, topic_id, topic_name)
        logger.info("Opened round %s for topic %s", round_id, topic_id)
        try:
            await msg.delete()
        except Exception:
            pass

    # ── /close — close round + send tweet list (admin) ────────

    @dp.message(Command("close"), F.chat.id == config.engage_group)
    async def cmd_close(msg: Message, bot: Bot):
        if not is_admin(msg.from_user.id):
            return

        topic_id  = msg.message_thread_id or 0
        round_row = await db.close_round(config.engage_group, topic_id)

        if not round_row:
            try:
                await msg.delete()
            except Exception:
                pass
            return

        round_id = round_row["id"]
        tweets   = await db.get_round_tweets(round_id)

        # Build tweet URLs list for extension import
        tweet_lines = [t["tweet_url"] for t in tweets]
        header = (
            f"<b>📋 Round #{round_id} — {round_row.get('topic_name','')}</b>\n"
            f"<b>{len(tweet_lines)} tweets</b>\n\n"
            f"<code>Round ID: {round_id}</code>\n\n"
            "Copy URLs below into extension:\n"
        )

        chunks = chunk_text("\n".join(tweet_lines), 3500)
        # Send to admin via DM
        for admin_id in config.admin_ids:
            try:
                await bot.send_message(admin_id, header, parse_mode="HTML")
                for chunk in chunks:
                    await bot.send_message(admin_id, f"<code>{chunk}</code>", parse_mode="HTML")
            except Exception as e:
                logger.warning("Could not DM admin %s: %s", admin_id, e)

        logger.info("Round %s closed, %s tweets", round_id, len(tweet_lines))
        try:
            await msg.delete()
        except Exception:
            pass

    # ── /missing — show missing users (admin) ─────────────────

    @dp.message(Command("missing"))
    async def cmd_missing(msg: Message, command: CommandObject, bot: Bot):
        if not is_admin(msg.from_user.id):
            return

        # Parse optional round_id arg
        args     = (command.args or "").strip()
        round_id = int(args) if args.isdigit() else None

        if round_id is None:
            # Get latest round for the chat
            chat_id   = config.engage_group
            round_row = await db.get_latest_round(chat_id)
            if not round_row:
                await _reply_or_dm(msg, bot, "No rounds found.")
                return
            round_id = round_row["id"]

        missing = await db.get_missing_with_tg(round_id)

        if not missing:
            await _reply_or_dm(
                msg, bot,
                f"✅ No missing users for round #{round_id}. "
                "(Either all engaged or no report received yet.)"
            )
            return

        lines = [f"<b>❌ Missing — Round #{round_id} ({len(missing)} users)</b>\n"]
        for i, m in enumerate(missing, 1):
            x  = f"@{m['x_username']}"
            tg = f"@{m['tg_username']}" if m.get("tg_username") else f"[id:{m.get('user_id','?')}]"
            lines.append(f"{i}. {tg} → {x}")

        for chunk in chunk_text("\n".join(lines), 3800):
            await _reply_or_dm(msg, bot, chunk)

    # ── /members — list all registered members (admin) ────────

    @dp.message(Command("members"))
    async def cmd_members(msg: Message, bot: Bot):
        if not is_admin(msg.from_user.id):
            return
        members = await db.list_members()
        if not members:
            await _reply_or_dm(msg, bot, "No members registered yet.")
            return

        lines = [f"<b>👥 Members ({len(members)})</b>\n"]
        for m in members:
            tg = f"@{m['tg_username']}" if m.get("tg_username") else f"[id:{m['user_id']}]"
            x  = f"@{m['x_username']}" if m.get("x_username") else "—"
            lines.append(f"• {tg} → {x}")

        for chunk in chunk_text("\n".join(lines), 3800):
            await _reply_or_dm(msg, bot, chunk)

    # ── /rounds — recent rounds list (admin) ──────────────────

    @dp.message(Command("rounds"))
    async def cmd_rounds(msg: Message, bot: Bot):
        if not is_admin(msg.from_user.id):
            return
        async with __import__("aiosqlite").connect(db.path) as conn:
            conn.row_factory = __import__("aiosqlite").Row
            async with conn.execute("""
                SELECT r.*, COUNT(rt.id) as tweet_count
                FROM engage_rounds r
                LEFT JOIN round_tweets rt ON rt.round_id = r.id
                WHERE r.chat_id = ?
                GROUP BY r.id
                ORDER BY r.id DESC
                LIMIT 20
            """, (config.engage_group,)) as cur:
                rounds = [dict(r) for r in await cur.fetchall()]

        if not rounds:
            await _reply_or_dm(msg, bot, "No rounds yet.")
            return

        lines = ["<b>📅 Recent Rounds</b>\n"]
        for r in rounds:
            status = "✅" if r["status"] == "closed" else "🔵"
            lines.append(
                f"{status} <b>#{r['id']}</b> {r.get('topic_name','—')} "
                f"| {r['tweet_count']} tweets | {r['status']}"
            )

        await _reply_or_dm(msg, bot, "\n".join(lines))

    # ── /setx — admin manually maps TG → X (admin) ────────────

    @dp.message(Command("setx"))
    async def cmd_setx(msg: Message, command: CommandObject, bot: Bot):
        if not is_admin(msg.from_user.id):
            return
        args = (command.args or "").strip().split()
        if len(args) < 2:
            await _reply_or_dm(msg, bot, "Usage: /setx @tgusername @xusername")
            return
        tg_user = args[0].lstrip("@").lower()
        x_user  = args[1].lstrip("@").lower()

        # Look up by TG username
        async with __import__("aiosqlite").connect(db.path) as conn:
            conn.row_factory = __import__("aiosqlite").Row
            async with conn.execute(
                "SELECT user_id FROM members WHERE LOWER(tg_username)=?", (tg_user,)
            ) as cur:
                row = await cur.fetchone()

        if row:
            await db.upsert_member(row["user_id"], tg_user, x_user)
        else:
            # Create placeholder member (user_id=0 means unknown)
            await db.upsert_member(0, tg_user, x_user)

        await _reply_or_dm(msg, bot, f"✅ Mapped @{tg_user} → @{x_user}")

    # ── /report — admin or webhook submits missing list ───────

    @dp.message(Command("report"))
    async def cmd_report(msg: Message, command: CommandObject, bot: Bot):
        if not is_admin(msg.from_user.id):
            return
        args = (command.args or "").strip().split()
        if len(args) < 2 or not args[0].isdigit():
            await _reply_or_dm(
                msg, bot,
                "Usage: /report <round_id> @x1 @x2 …\n"
                "Or paste comma/newline separated usernames after round_id."
            )
            return

        round_id  = int(args[0])
        x_users   = [a.lstrip("@").lower() for a in args[1:] if a.strip()]

        await db.save_missing_report(round_id, x_users)
        await _reply_or_dm(
            msg, bot,
            f"✅ Saved {len(x_users)} missing user(s) for round #{round_id}."
        )

    # ── Helper: reply in chat or DM ───────────────────────────

    async def _reply_or_dm(msg: Message, bot: Bot, text: str):
        if msg.chat.type == "private":
            await msg.answer(text)
        else:
            # In group: send to DM of the admin who triggered
            try:
                await bot.send_message(msg.from_user.id, text)
                await msg.delete()
            except Exception:
                await msg.reply(text)
