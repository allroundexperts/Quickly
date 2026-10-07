"""Heuristics for telling automated open/click hits apart from real recipients.

Corporate mail filters (Microsoft Safe Links, Proofpoint, Mimecast, …) fetch
every link and image in a message at delivery time to check it for phishing.
Without filtering, each of those fetches is counted as an open or a click.

This module is intentionally free of database or async dependencies; the
burst check that needs click history lives in ``app.tracking_events``.
"""
from __future__ import annotations

# // BEACON SYNC: This file handles tracking logic that Beacon mirrors.
# // Any changes to tracking behavior, event types, metadata collected, or URL structure
# // must also be reflected in the Beacon service (/beacon or the separate Beacon repo).
# // If you are an AI agent, add a TODO at the end of your response listing what needs to be updated in Beacon.

import re
from datetime import datetime

# Hits this soon after the email was sent are almost always a scanner
# inspecting the message on delivery, not a person reading it.
CLICK_MIN_SECONDS_AFTER_SEND = 60
OPEN_MIN_SECONDS_AFTER_SEND = 15

# Clicks on two or more *different* links of the same email within this many
# seconds are a scanner walking every link, not a person.
CLICK_BURST_WINDOW_SECONDS = 2

USER_AGENT_MAX_LEN = 512

_BOT_UA_RE = re.compile(
    r"bot\b|crawl|spider|slurp|preview|scan|headless|phantomjs"
    r"|python|curl|wget|go-http-client|java/|okhttp|axios|node-fetch|libwww|httpclient"
    r"|barracuda|mimecast|proofpoint|symantec|messagelabs|trendmicro|sophos"
    r"|fortinet|fortiguard|bitdefender|ironport|fireeye|zscaler|forcepoint|cloudmark",
    re.IGNORECASE,
)

# Apple Mail Privacy Protection pre-fetches every image through a proxy whose
# User-Agent is exactly this string, whether or not the message is ever read.
_APPLE_MPP_UA = "Mozilla/5.0"


def bot_user_agent_reason(user_agent: str | None, *, is_open: bool = False) -> str | None:
    """Return why *user_agent* looks automated, or ``None`` if it looks human.

    ``None`` means the User-Agent is unknown (e.g. a Beacon event that did not
    forward it) and is never treated as a bot.  An empty string means the
    request genuinely sent no User-Agent, which real browsers never do.

    Image proxies that fetch on behalf of a reading user (``GoogleImageProxy``,
    ``YahooMailProxy``) are deliberately not matched.
    """
    if user_agent is None:
        return None
    ua = user_agent.strip()
    if not ua:
        return "empty_user_agent"
    if is_open and ua == _APPLE_MPP_UA:
        return "apple_mpp_prefetch"
    if _BOT_UA_RE.search(ua):
        return "bot_user_agent"
    return None


def is_too_soon_after_send(sent_at: datetime | None, now: datetime, min_seconds: int) -> bool:
    """True when *now* is less than *min_seconds* after *sent_at*."""
    if sent_at is None:
        return False
    return (now - sent_at).total_seconds() < min_seconds
