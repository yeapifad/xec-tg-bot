"""
Configuration — loaded from environment variables.
Copy .env.example → .env and fill in values.
"""

import os
from dataclasses import dataclass, field
from typing import Optional


@dataclass
class Config:
    bot_token:    str
    admin_ids:    list[int]          # Telegram user IDs that can use admin commands
    engage_group: int                # The engage group chat ID (negative for supergroups)
    db_path:      str = "xec.db"
    webhook_url:  Optional[str] = None   # e.g. https://yourapp.railway.app
    webhook_port: int = 8000
    webhook_secret: Optional[str] = None

    @classmethod
    def from_env(cls) -> "Config":
        token = os.environ.get("BOT_TOKEN", "").strip()
        if not token:
            raise ValueError("BOT_TOKEN env var is required")

        raw_admins = os.environ.get("ADMIN_IDS", "").strip()
        admin_ids  = [int(x.strip()) for x in raw_admins.split(",") if x.strip().isdigit()]
        if not admin_ids:
            raise ValueError("ADMIN_IDS env var is required (comma-separated Telegram user IDs)")

        raw_group = os.environ.get("ENGAGE_GROUP_ID", "").strip()
        if not raw_group:
            raise ValueError("ENGAGE_GROUP_ID env var is required")

        return cls(
            bot_token     = token,
            admin_ids     = admin_ids,
            engage_group  = int(raw_group),
            db_path       = os.environ.get("DB_PATH", "xec.db"),
            webhook_url   = os.environ.get("WEBHOOK_URL", "").strip() or None,
            webhook_port  = int(os.environ.get("PORT", "8000")),
            webhook_secret= os.environ.get("WEBHOOK_SECRET", "").strip() or None,
        )
