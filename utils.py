"""
utils.py — shared helpers
"""

import re
from typing import Optional

# Matches x.com and twitter.com tweet URLs
TWEET_RE = re.compile(
    r"https?://(?:www\.)?(?:x\.com|twitter\.com)/([A-Za-z0-9_]{1,50})/status/(\d+)[^\s]*",
    re.IGNORECASE,
)

RESERVED = {
    "i", "search", "explore", "notifications", "messages", "home",
    "settings", "hashtag", "intent", "who_to_follow", "lists",
    "bookmarks", "compose", "logout", "x",
}


def extract_tweet_url(text: str) -> Optional[str]:
    """Return the first clean tweet URL found in text, or None."""
    m = TWEET_RE.search(text or "")
    if not m:
        return None
    username = m.group(1).lower()
    if username in RESERVED:
        return None
    # Normalise to x.com
    full = m.group(0)
    full = re.sub(r"https?://(?:www\.)?twitter\.com", "https://x.com", full, flags=re.I)
    # Strip query params / fragments
    full = full.split("?")[0].split("#")[0]
    return full


def extract_x_username_from_url(url: str) -> Optional[str]:
    """Return the tweet author username from a tweet URL."""
    m = TWEET_RE.search(url or "")
    if not m:
        return None
    username = m.group(1).lower()
    return None if username in RESERVED else username


def is_tweet_url(text: str) -> bool:
    return bool(TWEET_RE.search(text or ""))


def chunk_text(text: str, limit: int = 3800) -> list[str]:
    """Split text into chunks ≤ limit characters, breaking on newlines."""
    if len(text) <= limit:
        return [text]
    chunks, current = [], []
    length = 0
    for line in text.split("\n"):
        if length + len(line) + 1 > limit:
            chunks.append("\n".join(current))
            current, length = [], 0
        current.append(line)
        length += len(line) + 1
    if current:
        chunks.append("\n".join(current))
    return chunks


def fmt_user(tg_username: Optional[str], user_id: Optional[int]) -> str:
    if tg_username:
        return f"@{tg_username}"
    if user_id:
        return f'<a href="tg://user?id={user_id}">User {user_id}</a>'
    return "Unknown"
