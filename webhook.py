"""
webhook.py — aiohttp web server for:
  1. Telegram webhook  POST /webhook
  2. Extension report  POST /report  { round_id, missing: ["@x1","@x2"] }
  3. Health check      GET  /healthz
"""

import hashlib
import hmac
import json
import logging
import os

from aiohttp import web
from aiogram import Bot, Dispatcher
from aiogram.types import Update

from config import Config
from database import Database

logger = logging.getLogger(__name__)


async def start_webhook(bot: Bot, dp: Dispatcher, config: Config):
    app = web.Application()

    # ── Store shared objects ─────────────────────────────────
    app["bot"]    = bot
    app["dp"]     = dp
    app["config"] = config
    # Database is injected from dp's workflow_data later; grab from dp storage
    # (we stored db on dp during registration via dp["db"] = db in bot.py)
    app["db"] = dp["db"]

    # ── Routes ──────────────────────────────────────────────
    app.router.add_get("/healthz",  handle_health)
    app.router.add_post("/webhook", handle_tg_webhook)
    app.router.add_post("/report",  handle_extension_report)

    # ── Set Telegram webhook ─────────────────────────────────
    webhook_url    = config.webhook_url.rstrip("/") + "/webhook"
    webhook_secret = config.webhook_secret or ""

    await bot.set_webhook(
        url           = webhook_url,
        secret_token  = webhook_secret or None,
        drop_pending_updates = True,
    )
    logger.info("Webhook set: %s", webhook_url)

    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, "0.0.0.0", config.webhook_port)
    await site.start()
    logger.info("Web server listening on port %s", config.webhook_port)

    # Keep running forever
    import asyncio
    await asyncio.Event().wait()


# ── Health ────────────────────────────────────────────────────
async def handle_health(request: web.Request) -> web.Response:
    return web.json_response({"status": "ok"})


# ── Telegram webhook ──────────────────────────────────────────
async def handle_tg_webhook(request: web.Request) -> web.Response:
    bot: Bot       = request.app["bot"]
    dp: Dispatcher = request.app["dp"]
    config: Config = request.app["config"]

    # Verify secret token if configured
    if config.webhook_secret:
        token_header = request.headers.get("X-Telegram-Bot-Api-Secret-Token", "")
        if token_header != config.webhook_secret:
            logger.warning("Webhook: invalid secret token")
            return web.Response(status=403, text="Forbidden")

    try:
        data   = await request.json()
        update = Update(**data)
        await dp.feed_update(bot, update)
    except Exception as e:
        logger.exception("Webhook processing error: %s", e)

    return web.Response(text="ok")


# ── Extension report endpoint ─────────────────────────────────
async def handle_extension_report(request: web.Request) -> web.Response:
    """
    POST /report
    Headers: X-Api-Key: <REPORT_API_KEY>
    Body JSON:
    {
      "round_id": 3,
      "missing":  ["@naval", "@jack", "elonmusk"]
    }
    Returns: { "saved": 3 }
    """
    config: Config  = request.app["config"]
    db: Database    = request.app["db"]
    bot: Bot        = request.app["bot"]

    # Simple API key check
    api_key = os.environ.get("REPORT_API_KEY", "")
    if api_key:
        sent_key = request.headers.get("X-Api-Key", "")
        if not hmac.compare_digest(sent_key, api_key):
            return web.json_response({"error": "Unauthorized"}, status=401)

    try:
        body = await request.json()
    except Exception:
        return web.json_response({"error": "Invalid JSON"}, status=400)

    round_id = body.get("round_id")
    missing  = body.get("missing", [])

    if not isinstance(round_id, int) or not isinstance(missing, list):
        return web.json_response({"error": "round_id (int) and missing (list) required"}, status=400)

    clean = [u.strip().lstrip("@").lower() for u in missing if isinstance(u, str) and u.strip()]
    if not clean:
        return web.json_response({"saved": 0})

    await db.save_missing_report(round_id, clean)
    logger.info("Extension report: round=%s missing=%s", round_id, len(clean))

    # Notify admins via DM
    summary = (
        f"📩 <b>Extension Report — Round #{round_id}</b>\n"
        f"Missing: <b>{len(clean)}</b> users\n"
        f"Use /missing {round_id} to see details."
    )
    for admin_id in config.admin_ids:
        try:
            await bot.send_message(admin_id, summary)
        except Exception as e:
            logger.warning("Could not notify admin %s: %s", admin_id, e)

    return web.json_response({"saved": len(clean)})
