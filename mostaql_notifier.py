#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Mostaql Project Monitor -> Telegram Notifier
============================================

A background scraper that watches the Mostaql "Development" projects feed
(https://mostaql.com/projects?category=development) and pushes instant,
richly-formatted Telegram alerts for projects matching a Backend-Engineer
keyword profile.

Pipeline
--------
    1. Fetch the listing page (newest first).
    2. Drop projects already present in seen_ids.json  -> "new" projects.
    3. Match a Backend-Engineer keyword profile against title + brief.
    4. For each match, fetch the project detail page and extract client
       quality signals (hire rate, budget, duration, bid count).
    5. Classify the opportunity (green / yellow / red) and push a Telegram
       alert.

Operational safety
------------------
    * Exponential backoff + jitter on transient network failures.
    * Atomic, corruption-tolerant state persistence (seen_ids.json).
    * Empty-DOM watchdog: N consecutive zero-result cycles trigger an admin
      alert, because a silent selector break is otherwise indistinguishable
      from "no new projects".
    * First run "primes" the seen-set instead of replaying the whole back
      catalogue (configurable).

Requires: BOT_TOKEN and CHAT_ID in a .env file next to this script.
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import random
import re
import signal
import sys
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable, Optional
from urllib.parse import urljoin

import requests
from bs4 import BeautifulSoup
from dotenv import load_dotenv

# ---------------------------------------------------------------------------
# Paths & environment
# ---------------------------------------------------------------------------

BASE_DIR = Path(__file__).resolve().parent
ENV_PATH = BASE_DIR / ".env"

# Load .env before logging is configured so LOG_LEVEL from the file is honoured.
load_dotenv(ENV_PATH)

# ---------------------------------------------------------------------------
# Logging
# ---------------------------------------------------------------------------

LOG_FORMAT = "%(asctime)s | %(levelname)-7s | %(name)-10s | %(message)s"

logging.basicConfig(
    level=os.getenv("LOG_LEVEL", "INFO").upper(),
    format=LOG_FORMAT,
    datefmt="%Y-%m-%d %H:%M:%S",
    stream=sys.stdout,
)
log = logging.getLogger("monitor")

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

STATE_PATH = BASE_DIR / "seen_ids.json"

LISTING_URL = "https://mostaql.com/projects?category=development"

# Loop pacing. The spec calls for 75-90s; we pick uniformly inside that window
# so the request pattern is not a perfectly regular metronome.
LOOP_INTERVAL_MIN = 75.0
LOOP_INTERVAL_MAX = 90.0

# Networking
REQUEST_TIMEOUT = 30.0          # (connect, read) budget in seconds
DETAIL_FETCH_DELAY = 1.5        # politeness pause between detail-page hits
MAX_ATTEMPTS = 4                # per-request attempt ceiling
BACKOFF_BASE = 2.0              # exponential base (seconds)
BACKOFF_CAP = 60.0              # max single sleep

# State
MAX_SEEN_IDS = 1000             # hard cap on persisted IDs

# Watchdog
EMPTY_CYCLE_ALERT_THRESHOLD = 5  # consecutive empty cycles before alerting
EMPTY_ALERT_REPEAT_EVERY = 30    # re-alert at most once per N empty cycles

# Telegram
TELEGRAM_API = "https://api.telegram.org/bot{token}/{method}"
TG_MAX_LEN = 4096               # Telegram hard message limit
BRIEF_MAX_CHARS = 600           # brief truncation in the alert body

# Heuristics used by the quality classifier
HIGH_HIRE_RATE = 50.0           # >= 50% hire rate  -> green
LOW_COMPETITION_BIDS = 5        # <= 5 bids + verified -> green
LOW_BUDGET_CEILING = 100.0      # "$" ceiling considered "low"
SCOPE_WORD_COUNT = 120          # brief word count that counts as "large scope"

# Browser-like header set. Mostaql sits behind Cloudflare; a coherent desktop
# header profile avoids the most common bot heuristics. Note: we deliberately
# advertise only gzip/deflate, because requests cannot decode Brotli unless the
# optional `brotli` package is installed.
DEFAULT_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/121.0.0.0 Safari/537.36"
    ),
    "Accept": (
        "text/html,application/xhtml+xml,application/xml;q=0.9,"
        "image/avif,image/webp,*/*;q=0.8"
    ),
    "Accept-Language": "ar,en-US;q=0.9,en;q=0.8",
    "Accept-Encoding": "gzip, deflate",
    "Upgrade-Insecure-Requests": "1",
    "Sec-Fetch-Dest": "document",
    "Sec-Fetch-Mode": "navigate",
    "Sec-Fetch-Site": "none",
    "Sec-Fetch-User": "?1",
    "Cache-Control": "max-age=0",
    "Connection": "keep-alive",
}

# ---------------------------------------------------------------------------
# Keyword profile
# ---------------------------------------------------------------------------

# English terms are matched with Unicode-aware regex word boundaries so that
# "bot" does not fire on "robot"/"about" and "rest" does not fire on
# "restaurant". Each entry is a literal term; the regex is built from them.
ENGLISH_KEYWORDS: tuple[str, ...] = (
    "python",
    "django",
    "fastapi",
    "flask",
    "backend",
    "api",
    "apis",
    "rest",
    "restful",
    "graphql",
    "websocket",
    "websockets",
    "sql",
    "postgresql",
    "postgres",
    "mysql",
    "database",
    "redis",
    "scraping",
    "scraper",
    "bot",
    "docker",
    "webhooks",
)

# Arabic terms are matched as normalized substrings. Word boundaries are the
# wrong tool here: Arabic attaches clitics directly to the stem, so a project
# saying "بالبايثون" or "والدوكر" would be missed by a \b-anchored pattern.
ARABIC_KEYWORDS: tuple[str, ...] = (
    "بايثون",
    "جانغو",
    "باك اند",
    "باكاند",
    "باك إند",
    "قاعدة بيانات",
    "سحب بيانات",
    "كشط",
    "بوت",
    "دوكر",
    "ربط api",
    "بوابة دفع",
)

# ---------------------------------------------------------------------------
# Text utilities
# ---------------------------------------------------------------------------

# Arabic-Indic and Extended Arabic-Indic digits, in case the site localises
# numerals. Normalising up front keeps every downstream int()/float() simple.
_DIGIT_TRANSLATION = str.maketrans(
    "٠١٢٣٤٥٦٧٨٩" "۰۱۲۳۴۵۶۷۸۹",
    "0123456789" "0123456789",
)

# Arabic orthographic variants folded to a canonical form so that a keyword
# written with one alef/yaa shape still matches text written with another.
_ARABIC_FOLD = str.maketrans(
    {
        "أ": "ا", "إ": "ا", "آ": "ا", "ٱ": "ا",
        "ى": "ي", "ئ": "ي",
        "ؤ": "و",
        "ة": "ه",
        "گ": "ك",
    }
)

# Combining marks (harakat/tatweel) that would otherwise break substring hits.
_DIACRITICS_RE = re.compile(r"[ً-ٰٟـ]")

_WS_RE = re.compile(r"\s+")


def normalize_arabic(text: str) -> str:
    """Fold Arabic orthographic variants and strip diacritics."""
    if not text:
        return ""
    text = _DIACRITICS_RE.sub("", text)
    return text.translate(_ARABIC_FOLD)


def normalize_text(text: str) -> str:
    """Normalise any user-facing text: digits + Arabic folding + whitespace."""
    if not text:
        return ""
    text = text.translate(_DIGIT_TRANSLATION)
    text = normalize_arabic(text)
    return _WS_RE.sub(" ", text).strip()


def clean_text(node: Any) -> str:
    """Extract collapsed, unescaped text from a BeautifulSoup node."""
    if node is None:
        return ""
    return _WS_RE.sub(" ", node.get_text(" ", strip=True)).strip()


def parse_number(text: str) -> Optional[float]:
    """Pull the first numeric token out of a string ('57.14%' -> 57.14)."""
    if not text:
        return None
    match = re.search(r"\d+(?:[.,]\d+)?", text.translate(_DIGIT_TRANSLATION))
    if not match:
        return None
    try:
        return float(match.group(0).replace(",", "."))
    except ValueError:
        return None


def parse_int(text: str) -> Optional[int]:
    """Parse an integer, tolerating Arabic number words ('عرض واحد' -> 1)."""
    if not text:
        return None
    normalized = text.translate(_DIGIT_TRANSLATION)
    match = re.search(r"\d+", normalized)
    if match:
        return int(match.group(0))

    # Fall back to spelled-out numerals.
    words = {
        "واحد": 1, "واحدا": 1, "واحدة": 1,
        "اثنان": 2, "اثنين": 2, "عرضان": 2, "عرضين": 2,
        "ثلاثة": 3, "ثلاث": 3,
        "اربعة": 4, "أربعة": 4,
        "خمسة": 5,
    }
    folded = normalize_arabic(normalized)
    for word, value in words.items():
        if normalize_arabic(word) in folded:
            return value
    return None


# ---- Telegram MarkdownV2 escaping -----------------------------------------
# MarkdownV2 requires these characters to be backslash-escaped anywhere they
# appear as literal text. Getting this wrong makes the Bot API reject the whole
# message with "can't parse entities", so it is centralised here.
#
# The reserved set is exactly Telegram's documented list:
#     _ * [ ] ( ) ~ ` > # + - = | { } . !
# plus the backslash itself.
_MDV2_SPECIAL_RE = re.compile(r"([_*\[\]()~`>#+\-=|{}.!\\])")

# Same set as a plain set, for the pre-flight validator below.
_MDV2_RESERVED = frozenset("_*[]()~`>#+-=|{}.!\\")
# The only characters allowed to appear bare, and only as paired delimiters.
_MDV2_DELIMITERS = frozenset("*_`")

# A complete [label](url) construct; the url may contain escaped parens.
_MDV2_LINK_RE = re.compile(r"\[([^\]]*)\]\(((?:\\.|[^)\\])*)\)")


def escape_md(text: Any) -> str:
    """Escape dynamic text for safe interpolation into a MarkdownV2 message."""
    if text is None:
        return ""
    return _MDV2_SPECIAL_RE.sub(r"\\\1", str(text))


def escape_md_url(url: Any) -> str:
    """Inside a MarkdownV2 inline-URL only ')' and '\\' need escaping."""
    if not url:
        return ""
    return str(url).replace("\\", "\\\\").replace(")", "\\)")


# ---- MarkdownV2 composition helpers ---------------------------------------
# Every literal that reaches Telegram goes through one of these, so escaping is
# structural rather than something the author has to remember. Hand-escaping
# static strings is what produced the "unescaped '.'" 400 in the first place.

def md_bold(text: Any) -> str:
    """Render `text` as bold literal text."""
    return f"*{escape_md(text)}*"


def md_italic(text: Any) -> str:
    """Render `text` as italic literal text."""
    return f"_{escape_md(text)}_"


def md_link(label: Any, url: Any) -> str:
    """Render an inline link; the label is escaped, the URL in its own way."""
    return f"[{escape_md(label)}]({escape_md_url(url)})"


def md_field(emoji: str, label: Any, value: Any, *, italic: bool = False) -> str:
    """
    One ``emoji *Label:* value`` line, with every literal escaped.

    `italic` is used for placeholder values ("not disclosed") so they read as
    secondary to real data.
    """
    rendered = md_italic(value) if italic else escape_md(value)
    return f"{emoji} {md_bold(label)} {rendered}"


def _validate_mdv2_body(text: str) -> tuple[bool, str]:
    """
    Walk `text` honouring backslash escapes and report the first problem.

    Bare occurrences of the delimiter characters are tracked as a stack so an
    unbalanced '*' or '_' is reported too.
    """
    stack: list[str] = []
    index, length = 0, len(text)

    while index < length:
        char = text[index]

        if char == "\\":
            if index + 1 >= length:
                return False, "dangling backslash at end of message"
            index += 2                      # skip the escaped character
            continue

        if char in _MDV2_DELIMITERS:
            if stack and stack[-1] == char:
                stack.pop()
            else:
                stack.append(char)
            index += 1
            continue

        if char in _MDV2_RESERVED:
            context = text[max(0, index - 20): index + 20]
            return False, (
                f"unescaped {char!r} at offset {index} (context: {context!r})"
            )

        index += 1

    if stack:
        return False, f"unbalanced delimiter(s): {stack}"
    return True, ""


def validate_markdownv2(text: str) -> tuple[bool, str]:
    """
    Pre-flight check for an outgoing MarkdownV2 message.

    Returns ``(ok, problem)``. Link constructs are validated separately (their
    label is checked as body text, their parens are part of the syntax) and
    then removed before the surrounding text is checked.
    """
    if not text:
        return True, ""

    remainder: list[str] = []
    cursor = 0

    for match in _MDV2_LINK_RE.finditer(text):
        remainder.append(text[cursor:match.start()])
        ok, problem = _validate_mdv2_body(match.group(1))
        if not ok:
            return False, f"in link label: {problem}"
        cursor = match.end()

    remainder.append(text[cursor:])
    return _validate_mdv2_body("".join(remainder))


def strip_markdown(text: str) -> str:
    """Best-effort plain-text rendering, used by the notifier's fallback path."""
    if not text:
        return ""
    # Drop emphasis markers that are NOT backslash-escaped, then undo the
    # remaining escapes. The order matters: unescaping first would turn a
    # literal "\_" into "_" which the marker-stripper would then delete.
    text = re.sub(r"(?<!\\)[*_`]", "", text)
    text = re.sub(r"\\(.)", r"\1", text)
    return text


# ---------------------------------------------------------------------------
# HTTP layer
# ---------------------------------------------------------------------------

class FetchError(RuntimeError):
    """Raised when a URL could not be retrieved after all retries."""


class CloudflareChallenge(FetchError):
    """Raised when a Cloudflare interstitial/deny page is detected."""


# Markers that indicate a Cloudflare block rather than real content.
_CF_MARKERS = (
    "cf-browser-verification",
    "cf_chl_opt",
    "checking your browser",
    "just a moment",
    "attention required!",
    "enable javascript and cookies to continue",
)

# Status codes worth retrying.
_RETRY_STATUS = frozenset({408, 425, 429, 500, 502, 503, 504, 522, 524})


class HttpClient:
    """A requests.Session wrapper with coherent headers and retry/backoff."""

    def __init__(self, headers: Optional[dict[str, str]] = None) -> None:
        self.session = requests.Session()
        self.session.headers.update(headers or DEFAULT_HEADERS)

    def close(self) -> None:
        try:
            self.session.close()
        except Exception:  # pragma: no cover - defensive
            pass

    def get(self, url: str, *, referer: Optional[str] = None) -> str:
        """
        GET `url` and return decoded HTML.

        Retries transient failures with exponential backoff + jitter. Raises
        FetchError (or CloudflareChallenge) when the content is unusable.
        """
        headers = {"Referer": referer} if referer else None
        last_error: Optional[Exception] = None

        for attempt in range(1, MAX_ATTEMPTS + 1):
            try:
                response = self.session.get(
                    url, timeout=REQUEST_TIMEOUT, headers=headers
                )
            except (requests.ConnectionError, requests.Timeout) as exc:
                last_error = exc
                delay = self._backoff_delay(attempt)
                log.warning(
                    "Network error on %s (attempt %d/%d): %s -- retrying in %.1fs",
                    url, attempt, MAX_ATTEMPTS, exc.__class__.__name__, delay,
                )
                if attempt < MAX_ATTEMPTS:
                    time.sleep(delay)
                continue
            except requests.RequestException as exc:
                # Malformed URL / too many redirects etc. -- not retryable.
                raise FetchError(f"Request failed for {url}: {exc}") from exc

            if response.status_code in _RETRY_STATUS:
                last_error = FetchError(f"HTTP {response.status_code}")
                # Honour Retry-After when the server sends it (429/503).
                retry_after = response.headers.get("Retry-After")
                delay = (
                    min(float(retry_after), BACKOFF_CAP)
                    if (retry_after or "").strip().replace(".", "", 1).isdigit()
                    else self._backoff_delay(attempt)
                )
                log.warning(
                    "HTTP %d on %s (attempt %d/%d) -- retrying in %.1fs",
                    response.status_code, url, attempt, MAX_ATTEMPTS, delay,
                )
                if attempt < MAX_ATTEMPTS:
                    time.sleep(delay)
                continue

            if response.status_code >= 400:
                raise FetchError(f"HTTP {response.status_code} for {url}")

            body = response.text

            # A 200 can still be a Cloudflare challenge page. Detect it so the
            # failure is loud instead of silently parsing to zero projects.
            lowered = body[:4000].lower()
            if any(marker in lowered for marker in _CF_MARKERS):
                raise CloudflareChallenge(
                    f"Cloudflare challenge detected for {url}. "
                    "Consider a residential proxy or a headless browser."
                )

            return body

        raise FetchError(
            f"Exhausted {MAX_ATTEMPTS} attempts for {url}: {last_error}"
        )

    @staticmethod
    def _backoff_delay(attempt: int) -> float:
        """Exponential backoff with full jitter, capped."""
        exponential = min(BACKOFF_BASE ** attempt, BACKOFF_CAP)
        return random.uniform(0.0, exponential)


# ---------------------------------------------------------------------------
# Data models
# ---------------------------------------------------------------------------

@dataclass
class ProjectStub:
    """A project as it appears in the listing feed."""

    project_id: str
    title: str
    url: str
    brief: str = ""
    posted_at: Optional[str] = None
    client_name: Optional[str] = None
    listing_bids: Optional[int] = None
    matched_keywords: list[str] = field(default_factory=list)

    @property
    def searchable_text(self) -> str:
        """The text corpus used for keyword matching."""
        return f"{self.title}\n{self.brief}"


@dataclass
class ProjectDetail:
    """Client-quality signals scraped from a single project page."""

    budget_raw: Optional[str] = None
    budget_min: Optional[float] = None
    budget_max: Optional[float] = None
    duration: Optional[str] = None
    hire_rate: Optional[float] = None
    hire_rate_raw: Optional[str] = None
    hire_rate_pending: bool = False     # "لم يحسب بعد" -> no history yet
    open_projects: Optional[int] = None
    in_progress: Optional[int] = None
    active_communications: Optional[int] = None
    registration_date: Optional[str] = None
    client_name: Optional[str] = None
    is_verified: bool = False
    bids: Optional[int] = None
    bids_capped: bool = False       # True when the page's 50-bid cap was hit
    bids_raw: Optional[str] = None
    description: str = ""
    skills: list[str] = field(default_factory=list)
    fetch_ok: bool = True
    error: Optional[str] = None


# ---------------------------------------------------------------------------
# Scraper
# ---------------------------------------------------------------------------

# Mostaql renders the employer card twice (once per responsive column); the
# first match is the one we want. Selectors below were verified against a live
# page -- see README for the field-by-field mapping.
_SEL_PROJECT_ROW = "tr.project-row"
_SEL_TITLE_LINK = ".card--title h2 a[href], a.details-url[href]"
_SEL_BRIEF = "p.project__brief"
_SEL_TIME = "time[datetime]"
_SEL_META_LI = "ul.project__meta li"

_SEL_EMPLOYER_WIDGET = '[data-type="employer_widget"]'
_SEL_META_TABLE_ROW = "table.table-meta tr"
_SEL_PROJECT_META_ROW = "#project-meta-panel .meta-row"
_SEL_BIDS_PANEL = "#bidsCollection-panel"
_SEL_BID_ITEM = "div.bid[data-bid-item], div.bid"
# Scope to the body, not the whole #project-brief panel -- the panel's header
# ("تفاصيل المشروع") would otherwise be prepended to every brief.
_SEL_DESCRIPTION = (
    "#projectDetailsTab .text-wrapper-div, "
    "#projectDetailsTab, "
    "#project-brief .text-wrapper-div"
)

# Mostaql caps the rendered bid list at 50 items regardless of the true total,
# so a count of exactly 50 means "50 or more", not "50".
BIDS_PAGE_CAP = 50

_PROJECT_ID_RE = re.compile(r"/project/(\d+)")

# Arabic labels as they appear in the DOM, keyed to our field names.
# NOTE: the labels are folded through normalize_arabic() *before* being used as
# lookup keys -- the DOM writes "الميزانية" (taa marbuta) while our matcher sees
# the folded "الميزانيه", so raw keys would silently never match.
_CLIENT_LABELS_RAW = {
    "معدل التوظيف": "hire_rate",
    "المشاريع المفتوحة": "open_projects",
    "مشاريع قيد التنفيذ": "in_progress",
    "التواصلات الجارية": "active_communications",
    "تاريخ التسجيل": "registration_date",
}
_PROJECT_LABELS_RAW = {
    "الميزانية": "budget",
    "مدة التنفيذ": "duration",
}

_CLIENT_LABELS = {
    normalize_arabic(k): v for k, v in _CLIENT_LABELS_RAW.items()
}
_PROJECT_LABELS = {
    normalize_arabic(k): v for k, v in _PROJECT_LABELS_RAW.items()
}

# Mostaql renders this placeholder when a client has no hire history yet
# (a genuinely new client) -- distinct from "0%", which means "never hired".
HIRE_RATE_PENDING_MARKERS = ("لم يحسب بعد", "لم تحسب بعد")


class MostaqlScraper:
    """Parses the listing feed and individual project pages."""

    def __init__(self, client: HttpClient) -> None:
        self.client = client

    # -- listing -----------------------------------------------------------

    def fetch_listing(self) -> list[ProjectStub]:
        """Fetch and parse the newest page of the development category."""
        html = self.client.get(LISTING_URL)
        return self.parse_listing(html)

    @staticmethod
    def parse_listing(html: str) -> list[ProjectStub]:
        """
        Parse project rows out of a listing page.

        Returns an empty list both for a genuinely empty feed and for a broken
        layout; the caller's empty-cycle watchdog is what tells them apart.
        """
        soup = BeautifulSoup(html, "html.parser")
        rows = soup.select(_SEL_PROJECT_ROW)
        projects: list[ProjectStub] = []

        for row in rows:
            link = row.select_one(_SEL_TITLE_LINK)
            if link is None or not link.get("href"):
                continue

            url = urljoin(LISTING_URL, link["href"].strip())
            id_match = _PROJECT_ID_RE.search(url)
            if not id_match:
                continue

            title = clean_text(link) or clean_text(row.select_one(".card--title"))

            time_node = row.select_one(_SEL_TIME)
            posted_at = (time_node.get("datetime") or "").strip() or None

            projects.append(
                ProjectStub(
                    project_id=id_match.group(1),
                    title=title,
                    url=url,
                    brief=clean_text(row.select_one(_SEL_BRIEF)),
                    posted_at=posted_at,
                    client_name=MostaqlScraper._parse_listing_client(row),
                    listing_bids=MostaqlScraper._parse_listing_bids(row),
                )
            )

        log.debug("Parsed %d project rows from listing", len(projects))
        return projects

    @staticmethod
    def _parse_listing_client(row: Any) -> Optional[str]:
        """The client name is the first meta <li> carrying a <bdi> name."""
        for li in row.select(_SEL_META_LI):
            name_node = li.find("bdi")
            if name_node:
                name = clean_text(name_node)
                if name:
                    return name
        return None

    @staticmethod
    def _parse_listing_bids(row: Any) -> Optional[int]:
        """Bid count from the listing meta row, e.g. '18 عرض' / 'عرض واحد'."""
        for li in row.select(_SEL_META_LI):
            # The bid row is the one using the file-signature icon.
            if li.select_one(".hsoub-file-signature-icon") is None:
                continue
            return parse_int(clean_text(li))
        return None

    # -- detail ------------------------------------------------------------

    def fetch_detail(self, url: str) -> ProjectDetail:
        """Fetch and parse a project page; never raises, degrades to fetch_ok=False."""
        try:
            html = self.client.get(url, referer=LISTING_URL)
        except FetchError as exc:
            log.warning("Detail fetch failed for %s: %s", url, exc)
            return ProjectDetail(fetch_ok=False, error=str(exc))
        return self.parse_detail(html)

    @staticmethod
    def parse_detail(html: str) -> ProjectDetail:
        """Extract client-quality signals from a project detail page."""
        soup = BeautifulSoup(html, "html.parser")
        detail = ProjectDetail()

        # --- budget / duration / skills (project meta panel) --------------
        for row in soup.select(_SEL_PROJECT_META_ROW):
            label = normalize_arabic(clean_text(row.select_one(".meta-label")))
            value_node = row.select_one(".meta-value")
            if not label or value_node is None:
                continue

            field_name = _PROJECT_LABELS.get(label)
            if field_name == "budget":
                detail.budget_raw = clean_text(value_node)
                detail.budget_min, detail.budget_max = (
                    MostaqlScraper._parse_budget(detail.budget_raw)
                )
            elif field_name == "duration":
                detail.duration = clean_text(value_node)

        # Skills are NOT reachable through the label/value pair: on the live
        # layout the "المهارات" row renders an empty .meta-value, while the
        # actual tags sit in a sibling `ul.skills.list-tags` under a row that
        # has no .meta-value at all. So they are pulled by their own class.
        # The list is rendered twice (responsive columns) -> de-duplicate.
        skill_nodes = soup.select(
            "#project-meta-panel ul.skills li"
        ) or soup.select("li.skills__item")
        seen_skills: set[str] = set()
        for node in skill_nodes:
            skill = clean_text(node)
            if skill and skill not in seen_skills:
                seen_skills.add(skill)
                detail.skills.append(skill)

        # --- client signals (employer widget) -----------------------------
        widget = soup.select_one(_SEL_EMPLOYER_WIDGET)
        if widget is not None:
            detail.is_verified = (
                widget.select_one(".profile-verification-badge") is not None
            )
            if not detail.client_name:
                detail.client_name = clean_text(
                    widget.select_one(".profile__name")
                ) or None

            for row in widget.select(_SEL_META_TABLE_ROW):
                cells = row.find_all("td")
                if len(cells) < 2:
                    continue
                label = normalize_arabic(clean_text(cells[0]))
                value_text = clean_text(cells[1])
                field_name = _CLIENT_LABELS.get(label)

                if field_name == "hire_rate":
                    detail.hire_rate_raw = value_text or None
                    detail.hire_rate = parse_number(value_text)
                    if detail.hire_rate is None and any(
                        marker in normalize_arabic(value_text)
                        for marker in HIRE_RATE_PENDING_MARKERS
                    ):
                        detail.hire_rate_pending = True
                elif field_name == "open_projects":
                    detail.open_projects = parse_int(value_text)
                elif field_name == "in_progress":
                    detail.in_progress = parse_int(value_text)
                elif field_name == "active_communications":
                    detail.active_communications = parse_int(value_text)
                elif field_name == "registration_date":
                    detail.registration_date = value_text or None

        # --- bid count ----------------------------------------------------
        panel = soup.select_one(_SEL_BIDS_PANEL)
        if panel is not None:
            bid_items = panel.select(_SEL_BID_ITEM)
            detail.bids = len(bid_items)
            detail.bids_capped = detail.bids >= BIDS_PAGE_CAP
            if detail.bids_capped:
                detail.bids_raw = f"{BIDS_PAGE_CAP}+"

        # --- full description --------------------------------------------
        detail.description = clean_text(soup.select_one(_SEL_DESCRIPTION))

        return detail

    @staticmethod
    def _parse_budget(raw: str) -> tuple[Optional[float], Optional[float]]:
        """Parse '$25.00 - $50.00' into (25.0, 50.0); tolerates single values."""
        if not raw:
            return None, None
        numbers = [
            float(n.replace(",", ""))
            for n in re.findall(r"\d[\d,]*(?:\.\d+)?", raw.translate(_DIGIT_TRANSLATION))
        ]
        if not numbers:
            return None, None
        if len(numbers) == 1:
            return numbers[0], numbers[0]
        return min(numbers), max(numbers)


# ---------------------------------------------------------------------------
# Keyword matching
# ---------------------------------------------------------------------------

# Word-boundary pattern for the English set. re.IGNORECASE + \b gives us
# "bot" != "robot" and "rest" != "restaurant".
_EN_PATTERN = re.compile(
    r"\b(?:" + "|".join(re.escape(k) for k in ENGLISH_KEYWORDS) + r")\b",
    re.IGNORECASE,
)

# Normalized Arabic keywords (folded once at import time).
_AR_KEYWORDS_NORMALIZED = tuple(normalize_arabic(k) for k in ARABIC_KEYWORDS)


def match_keywords(text: str) -> list[str]:
    """
    Return the keywords matched in `text`.

    English keywords use word boundaries; Arabic keywords use normalized
    substring matching (see ARABIC_KEYWORDS for the rationale).
    """
    if not text:
        return []

    hits: list[str] = []

    for match in _EN_PATTERN.finditer(text):
        hits.append(match.group(0).lower())

    folded = normalize_arabic(text).lower()
    for keyword, normalized in zip(ARABIC_KEYWORDS, _AR_KEYWORDS_NORMALIZED):
        if normalized and normalized in folded:
            hits.append(keyword)

    # De-duplicate while preserving order.
    seen: set[str] = set()
    unique: list[str] = []
    for hit in hits:
        if hit not in seen:
            seen.add(hit)
            unique.append(hit)
    return unique


# ---------------------------------------------------------------------------
# Quality classification
# ---------------------------------------------------------------------------

@dataclass
class Assessment:
    """The classifier's verdict for one project."""

    badge: str          # 🟢 / 🟡 / 🔴
    label: str          # human-readable tier name
    reason: str         # why it got this tier


def assess(stub: ProjectStub, detail: ProjectDetail) -> Assessment:
    """
    Classify an opportunity by client quality.

    Risk signals are evaluated first: a project that looks genuinely bad should
    not be upgraded to green merely because it also has a healthy hire rate.
    """
    brief_text = detail.description or stub.brief or ""
    word_count = len(brief_text.split())

    hire_rate = detail.hire_rate
    bids = detail.bids if detail.bids is not None else stub.listing_bids
    open_projects = detail.open_projects

    # ---- 🔴 Risky ------------------------------------------------------
    if open_projects is not None and open_projects >= 2 and (hire_rate or 0) == 0:
        return Assessment(
            "🔴", "Risky",
            f"Client has {open_projects} open projects and a 0% hire rate",
        )

    if (
        detail.budget_max is not None
        and detail.budget_max <= LOW_BUDGET_CEILING
        and word_count >= SCOPE_WORD_COUNT
    ):
        return Assessment(
            "🔴", "Risky",
            f"Scope-to-budget mismatch: {word_count}-word brief against a "
            f"${detail.budget_max:.0f} ceiling",
        )

    # ---- 🟢 High / Promising -------------------------------------------
    if hire_rate is not None and hire_rate >= HIGH_HIRE_RATE:
        return Assessment(
            "🟢", "High / Promising",
            f"Client hire rate {hire_rate:.0f}% (≥ {HIGH_HIRE_RATE:.0f}%)",
        )

    if detail.is_verified and bids is not None and bids <= LOW_COMPETITION_BIDS:
        return Assessment(
            "🟢", "High / Promising",
            f"Verified client with low competition ({bids} bid"
            f"{'s' if bids != 1 else ''})",
        )

    # ---- 🟡 Average / Neutral ------------------------------------------
    if detail.hire_rate_pending:
        reason = "New client -- hire rate not calculated yet"
    elif hire_rate is None:
        reason = "New client with no published hire history"
    elif hire_rate == 0:
        reason = "Client has not hired yet (0% hire rate)"
    else:
        reason = f"Moderate hire rate ({hire_rate:.0f}%)"

    if detail.budget_raw:
        reason += f"; budget {detail.budget_raw}"
    if bids is not None:
        reason += f"; {bids} bid{'s' if bids != 1 else ''}"

    return Assessment("🟡", "Average / Neutral", reason)


# ---------------------------------------------------------------------------
# State persistence
# ---------------------------------------------------------------------------

class SeenStore:
    """
    Persists seen project IDs so restarts do not replay old notifications.

    The file (JSON) is written atomically and the ID list is capped at
    `cap` entries, oldest evicted first. Corrupt or unreadable state is moved
    aside rather than crashing the monitor.
    """

    def __init__(self, path: Path, cap: int = MAX_SEEN_IDS) -> None:
        self.path = path
        self.cap = cap
        self._ids: list[str] = []           # insertion-ordered, oldest first
        self._set: set[str] = set()
        self._dirty = False                 # True when IDs changed since last save

    # -- I/O ---------------------------------------------------------------

    def load(self) -> None:
        """Load state, tolerating a missing or corrupted file."""
        if not self.path.exists():
            log.info("No state file at %s -- starting fresh", self.path)
            return

        try:
            with self.path.open("r", encoding="utf-8") as handle:
                payload = json.load(handle)
        except (json.JSONDecodeError, UnicodeDecodeError, OSError) as exc:
            quarantine = self.path.with_suffix(
                f".corrupt-{datetime.now(timezone.utc):%Y%m%d%H%M%S}.json"
            )
            log.error(
                "State file unreadable (%s) -- moving to %s and starting fresh",
                exc, quarantine.name,
            )
            try:
                self.path.replace(quarantine)
            except OSError:
                pass
            return

        # Accept either {"ids": [...]} or a bare list, for forward/backward
        # compatibility with hand-edited state files.
        raw_ids: Any = payload.get("ids") if isinstance(payload, dict) else payload
        if not isinstance(raw_ids, list):
            log.error("State file has unexpected shape -- ignoring its contents")
            return

        for item in raw_ids:
            if isinstance(item, (str, int)):
                self._add_in_memory(str(item), mark_dirty=False)

        self._dirty = False
        log.info("Loaded %d seen project IDs", len(self._ids))

    def save(self) -> bool:
        """
        Atomically persist state only when the ID set has changed.

        Returns True when the file was rewritten, False for a no-op or a write
        failure. Keeping no-op saves off disk prevents empty GitHub Actions
        state commits.
        """
        if not self._dirty:
            log.debug("State unchanged -- skipping save")
            return False

        payload = {
            "updated_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
            "count": len(self._ids),
            "ids": self._ids,
        }
        temp_path = self.path.with_suffix(".json.tmp")
        try:
            with temp_path.open("w", encoding="utf-8") as handle:
                json.dump(payload, handle, ensure_ascii=False, indent=0)
            os.replace(temp_path, self.path)   # atomic on the same filesystem
        except OSError as exc:
            log.error("Failed to persist state to %s: %s", self.path, exc)
            try:
                temp_path.unlink(missing_ok=True)
            except OSError:
                pass
            return False

        self._dirty = False
        return True

    # -- queries -----------------------------------------------------------

    def __contains__(self, project_id: str) -> bool:
        return project_id in self._set

    def __len__(self) -> int:
        return len(self._ids)

    @property
    def is_empty(self) -> bool:
        return not self._ids

    # -- mutation ----------------------------------------------------------

    def add(self, project_id: str) -> None:
        self._add_in_memory(project_id)

    def add_many(self, project_ids: Iterable[str]) -> None:
        for project_id in project_ids:
            self._add_in_memory(project_id)

    def _add_in_memory(self, project_id: str, *, mark_dirty: bool = True) -> bool:
        """Insert an ID, evicting the oldest entries beyond the cap."""
        if not project_id or project_id in self._set:
            return False
        self._ids.append(project_id)
        self._set.add(project_id)

        overflow = len(self._ids) - self.cap
        if overflow > 0:
            for evicted in self._ids[:overflow]:
                self._set.discard(evicted)
            del self._ids[:overflow]

        if mark_dirty:
            self._dirty = True
        return True


# ---------------------------------------------------------------------------
# Telegram
# ---------------------------------------------------------------------------

REDACTED_TOKEN = "<REDACTED>"


def redact_token(value: Any, token: str) -> str:
    """Replace a bot token in exception/URL text before it reaches a log."""
    text = str(value)
    if token:
        text = text.replace(token, REDACTED_TOKEN)
    return text


class DryRunNotifier:
    """
    Prints alerts to stdout instead of calling Telegram.

    Used by ``--dry-run`` so the scraping/matching/classification pipeline can
    be validated end-to-end without credentials or side effects.
    """

    def __init__(self) -> None:
        self.sent: list[str] = []

    def send(self, text: str, *, admin: bool = False) -> bool:
        self.sent.append(text)
        tag = "ADMIN" if admin else "ALERT"
        print(f"\n{'=' * 62}\n[{tag}] (dry run -- not sent)\n{'-' * 62}")
        print(strip_markdown(text))
        print("=" * 62)
        return True

    def verify(self) -> bool:
        print("[dry-run] Skipping Telegram authentication check.")
        return True


class TelegramNotifier:
    """Thin Bot API client with MarkdownV2 escaping and a plain-text fallback."""

    def __init__(self, token: str, chat_id: str, admin_chat_id: Optional[str] = None):
        self.token = token
        self.chat_id = chat_id
        self.admin_chat_id = admin_chat_id or chat_id
        self.session = requests.Session()

    def send(self, text: str, *, admin: bool = False) -> bool:
        target = self.admin_chat_id if admin else self.chat_id

        # Pre-flight: catch malformed MarkdownV2 locally rather than letting
        # Telegram reject it. A 400 costs a round-trip and only says which
        # character offended -- this reports the offending offset and context.
        ok, problem = validate_markdownv2(text)
        if not ok:
            log.error(
                "Outgoing message is not valid MarkdownV2 (%s) -- "
                "falling back to plain text. This is a bug in the message "
                "builder, not in the scraped data.",
                problem,
            )
            return self._post(target, strip_markdown(text), parse_mode=None)

        if self._post(target, text, parse_mode="MarkdownV2"):
            return True

        log.warning("MarkdownV2 rejected by Telegram -- retrying as plain text")
        return self._post(target, strip_markdown(text), parse_mode=None)

    def _post(self, chat_id: str, text: str, *, parse_mode: Optional[str]) -> bool:
        url = TELEGRAM_API.format(token=self.token, method="sendMessage")
        payload: dict[str, Any] = {
            "chat_id": chat_id,
            "text": text[:TG_MAX_LEN],
            "disable_web_page_preview": True,
        }
        if parse_mode:
            payload["parse_mode"] = parse_mode

        for attempt in range(1, MAX_ATTEMPTS + 1):
            try:
                response = self.session.post(url, data=payload, timeout=REQUEST_TIMEOUT)
            except (requests.ConnectionError, requests.Timeout) as exc:
                delay = HttpClient._backoff_delay(attempt)
                log.warning(
                    "Telegram network error (attempt %d/%d): %s -- retrying in %.1fs",
                    attempt, MAX_ATTEMPTS, exc.__class__.__name__, delay,
                )
                if attempt < MAX_ATTEMPTS:
                    time.sleep(delay)
                continue
            except requests.RequestException as exc:
                log.error(
                    "Telegram request failed: %s",
                    redact_token(exc, self.token),
                )
                return False

            if response.status_code == 200:
                message_id = (
                    response.json().get("result", {}).get("message_id")
                )
                log.debug(
                    "Telegram HTTP 200 | parse_mode=%s | message_id=%s | "
                    "%d chars",
                    parse_mode or "none", message_id, len(text),
                )
                return True

            # 429 = rate limited; the body carries retry_after.
            if response.status_code == 429 and attempt < MAX_ATTEMPTS:
                try:
                    retry_after = response.json().get("parameters", {}).get(
                        "retry_after", 5
                    )
                except ValueError:
                    retry_after = 5
                log.warning("Telegram rate limit -- sleeping %ss", retry_after)
                time.sleep(min(float(retry_after), BACKOFF_CAP))
                continue

            # Entity-parse failures are handled by the caller's fallback.
            if response.status_code == 400 and "parse" in response.text.lower():
                log.warning("Telegram parse error: %s", response.text[:200])
                return False

            log.error(
                "Telegram HTTP %d: %s", response.status_code, response.text[:200]
            )
            if response.status_code < 500:
                return False
            if attempt < MAX_ATTEMPTS:
                time.sleep(HttpClient._backoff_delay(attempt))

        return False

    def verify(self) -> bool:
        """Confirm the token/chat pair works before entering the loop."""
        url = TELEGRAM_API.format(token=self.token, method="getMe")
        try:
            response = self.session.get(url, timeout=REQUEST_TIMEOUT)
        except requests.RequestException as exc:
            log.error(
                "Telegram unreachable: %s",
                redact_token(exc, self.token),
            )
            return False
        if response.status_code != 200:
            log.error("Telegram rejected the bot token: HTTP %d", response.status_code)
            return False
        username = response.json().get("result", {}).get("username", "?")
        log.info("Telegram bot authenticated as @%s", username)
        return True


# ---------------------------------------------------------------------------
# Alert formatting
# ---------------------------------------------------------------------------

def format_alert(
    stub: ProjectStub, detail: ProjectDetail, verdict: Assessment
) -> str:
    """Build the MarkdownV2 alert body for one matched project."""
    lines: list[str] = []

    # Header: badge + tier + the reason behind the classification.
    lines.append(f"{verdict.badge} *{escape_md(verdict.label)}*")
    lines.append(md_italic(verdict.reason))
    lines.append("")

    # Title.
    lines.append(md_bold(stub.title))
    lines.append("")

    # Budget & duration.
    lines.append(
        md_field("💰", "Budget:", detail.budget_raw)
        if detail.budget_raw
        else md_field("💰", "Budget:", "not disclosed", italic=True)
    )

    lines.append(
        md_field("⏱", "Duration:", detail.duration)
        if detail.duration
        else md_field("⏱", "Duration:", "not specified", italic=True)
    )

    # Client reputation.
    if detail.hire_rate is not None:
        lines.append(md_field("📊", "Hire rate:", f"{detail.hire_rate:g}%"))
    elif detail.hire_rate_pending:
        lines.append(
            md_field("📊", "Hire rate:", "new client (not yet calculated)",
                     italic=True)
        )
    else:
        lines.append(md_field("📊", "Hire rate:", "no history", italic=True))

    # Bids (with the page-cap caveat made explicit).
    bid_value = detail.bids if detail.bids is not None else stub.listing_bids
    if bid_value is not None:
        suffix = "+ (page cap)" if detail.bids_capped else ""
        lines.append(md_field("🙋", "Bids:", f"{bid_value}{suffix}"))

    if detail.open_projects is not None:
        lines.append(md_field("📂", "Open projects:", detail.open_projects))

    if detail.client_name or stub.client_name:
        client = detail.client_name or stub.client_name or ""
        verified = " ✅" if detail.is_verified else ""
        lines.append(md_field("👤", "Client:", client) + verified)

    # Matched keywords, so it is obvious why this project surfaced.
    if stub.matched_keywords:
        lines.append(
            md_field("🔑", "Matched:", ", ".join(stub.matched_keywords))
        )

    # Brief.
    brief = detail.description or stub.brief
    if brief:
        lines.append("")
        if len(brief) > BRIEF_MAX_CHARS:
            brief = brief[:BRIEF_MAX_CHARS].rstrip() + "…"
        lines.append(f"{md_bold('📝 Brief:')}\n{escape_md(brief)}")

    # Link.
    lines.append("")
    lines.append(f"🔗 {md_link('Open project on Mostaql', stub.url)}")

    return "\n".join(lines)


def format_dom_alert(empty_cycles: int) -> str:
    """
    Admin alert raised when the listing parses to zero for too long.

    Literal punctuation is escaped here just as carefully as scraped text --
    MarkdownV2 rejects the entire message over a single unescaped '.'.
    """
    return "\n".join(
        [
            f"⚠️ {md_bold('Mostaql monitor: no projects parsed')}",
            "",
            (
                f"The listing page returned {md_bold('0 projects')} for "
                # The trailing period is literal punctuation and must be
                # escaped -- MarkdownV2 rejects the message over a bare '.'.
                f"{md_bold(f'{empty_cycles} consecutive cycles')}\\."
            ),
            "",
            escape_md(
                "This usually means the site's DOM structure changed, "
                "or the request is being blocked."
            ),
            "",
            f"🔗 {md_link('Check the listing', LISTING_URL)}",
        ]
    )


def format_test_message() -> str:
    """
    The ``--test-telegram`` handshake message.

    Built through the same helpers as the real alerts so its literals cannot
    drift out of sync with the escaping rules. (This exact message was the
    source of the original "Character '.' is reserved" 400.)
    """
    return "\n".join(
        [
            f"✅ {md_bold('Mostaql monitor connected')}",
            md_italic("You will receive project alerts in this chat."),
            "",
            f"🔗 {md_link('Open the monitored feed', LISTING_URL)}",
        ]
    )


# ---------------------------------------------------------------------------
# Monitor loop
# ---------------------------------------------------------------------------

class MostaqlMonitor:
    """Wires the scraper, matcher, classifier and notifier into a poll loop."""

    def __init__(
        self,
        scraper: MostaqlScraper,
        notifier: TelegramNotifier,
        store: SeenStore,
        *,
        notify_on_first_run: bool = False,
        persist: bool = True,
        prime: bool = True,
    ) -> None:
        self.scraper = scraper
        self.notifier = notifier
        self.store = store
        self.notify_on_first_run = notify_on_first_run
        self.persist = persist
        self.prime = prime
        self._stopping = False

    # -- lifecycle ---------------------------------------------------------

    def install_signal_handlers(self) -> None:
        """Exit cleanly on SIGINT/SIGTERM so state is flushed."""
        def handler(signum: int, _frame: Any) -> None:
            log.info("Signal %s received -- shutting down after this cycle", signum)
            self._stopping = True

        for sig in (signal.SIGINT, signal.SIGTERM):
            try:
                signal.signal(sig, handler)
            except (ValueError, AttributeError):
                # Not available on every platform/thread context.
                pass

    def run(self, max_cycles: Optional[int] = None) -> int:
        """
        Poll until signalled (or until `max_cycles` completes, for --once).

        Notifies on new projects matching the keyword profile. Returns 0 when
        the final requested cycle completed healthily, 1 otherwise (used by
        --once to fail the GitHub Actions job on fetch/DOM/delivery errors).
        """
        empty_cycles = 0
        cycle = 0
        last_cycle_ok = True
        primed = (not self.store.is_empty) or (not self.prime)

        log.info(
            "Monitor starting | seen=%d | notify_on_first_run=%s | prime=%s",
            len(self.store), self.notify_on_first_run, self.prime,
        )

        while not self._stopping:
            if max_cycles is not None and cycle >= max_cycles:
                break

            cycle += 1
            started = time.monotonic()
            log.info("--- cycle %d ---", cycle)

            try:
                projects = self.scraper.fetch_listing()
            except FetchError as exc:
                log.error("Listing fetch failed: %s", exc)
                last_cycle_ok = False
                if max_cycles is not None and cycle >= max_cycles:
                    break
                self._sleep()
                continue

            # ---- empty-DOM watchdog -------------------------------------
            if not projects:
                empty_cycles += 1
                log.warning(
                    "Listing returned 0 projects (streak: %d/%d)",
                    empty_cycles, EMPTY_CYCLE_ALERT_THRESHOLD,
                )
                last_cycle_ok = False
                if empty_cycles >= EMPTY_CYCLE_ALERT_THRESHOLD and (
                    (empty_cycles - EMPTY_CYCLE_ALERT_THRESHOLD)
                    % EMPTY_ALERT_REPEAT_EVERY
                    == 0
                ):
                    self.notifier.send(format_dom_alert(empty_cycles), admin=True)
                if max_cycles is not None and cycle >= max_cycles:
                    break
                self._sleep()
                continue

            empty_cycles = 0
            last_cycle_ok = True

            # ---- first-run priming --------------------------------------
            # Without this, a cold start would replay every project currently
            # on page 1 as if it were brand new.
            if not primed:
                primed = True
                if self.notify_on_first_run:
                    log.info(
                        "First run: notifying on %d existing projects",
                        len(projects),
                    )
                else:
                    self.store.add_many(p.project_id for p in projects)
                    self._save()
                    log.info(
                        "Primed state with %d existing projects (no alerts sent)",
                        len(projects),
                    )
                    if max_cycles is not None and cycle >= max_cycles:
                        break
                    self._sleep()
                    continue

            # ---- new projects, oldest first -----------------------------
            new_projects = [p for p in projects if p.project_id not in self.store]
            log.info(
                "Fetched %d projects | %d new", len(projects), len(new_projects)
            )

            delivery_failures = 0
            for stub in reversed(new_projects):     # listing is newest-first
                if self._stopping:
                    break
                try:
                    if not self._process(stub):
                        delivery_failures += 1
                except Exception:
                    # One bad project must never kill the loop.
                    log.exception("Unhandled error processing %s", stub.url)
                    delivery_failures += 1

            if delivery_failures:
                last_cycle_ok = False
                log.warning(
                    "%d project(s) were not marked seen and will be retried",
                    delivery_failures,
                )

            # Persist only IDs that were actually recorded by _process().
            self._save()

            log.info(
                "Cycle %d done in %.1fs | seen=%d",
                cycle, time.monotonic() - started, len(self.store),
            )

            # Do not idle when this was the final requested cycle (--once).
            if max_cycles is not None and cycle >= max_cycles:
                break
            self._sleep()

        self._save()
        log.info("Monitor stopped cleanly | seen=%d", len(self.store))
        return 0 if last_cycle_ok else 1

    def _save(self) -> None:
        """Persist state, unless running in a non-persisting (dry-run) mode."""
        if self.persist:
            self.store.save()

    # -- per-project handling ---------------------------------------------

    def _process(self, stub: ProjectStub) -> bool:
        """
        Match, enrich and notify for a single new project.

        Returns True when the project can be marked seen. A project is only
        recorded after a successful Telegram delivery, so failed alerts are
        retried on the next cycle instead of being silently dropped.
        """
        matched = match_keywords(stub.searchable_text)
        if not matched:
            log.debug("Skip (no keyword match): %s", stub.title)
            self.store.add(stub.project_id)
            return True

        stub.matched_keywords = matched
        log.info("MATCH [%s] %s", ", ".join(matched), stub.title)

        detail = self.scraper.fetch_detail(stub.url)
        if not detail.fetch_ok:
            log.warning("Alerting without detail data: %s", detail.error)

        verdict = assess(stub, detail)
        log.info("  -> %s %s (%s)", verdict.badge, verdict.label, verdict.reason)

        delivered = self.notifier.send(format_alert(stub, detail, verdict))
        if delivered:
            self.store.add(stub.project_id)
            self._save()
        else:
            log.warning(
                "Delivery failed for %s -- keeping it unseen for retry",
                stub.project_id,
            )

        # Politeness pause between detail-page hits.
        if DETAIL_FETCH_DELAY > 0:
            time.sleep(DETAIL_FETCH_DELAY)

        return delivered

    def _sleep(self) -> None:
        """Sleep a jittered interval, waking early if shutdown is requested."""
        delay = random.uniform(LOOP_INTERVAL_MIN, LOOP_INTERVAL_MAX)
        log.debug("Sleeping %.1fs", delay)
        deadline = time.monotonic() + delay
        while not self._stopping and time.monotonic() < deadline:
            time.sleep(min(1.0, deadline - time.monotonic()))


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

def load_config() -> tuple[str, str, Optional[str], bool]:
    """
    Load and validate secrets from the already-loaded environment.

    Exits with a clear message rather than starting a bot that cannot notify.
    """
    token = (os.getenv("BOT_TOKEN") or "").strip()
    chat_id = (os.getenv("CHAT_ID") or "").strip()
    admin_chat_id = (os.getenv("ADMIN_CHAT_ID") or "").strip() or None
    notify_on_first_run = (
        os.getenv("NOTIFY_ON_FIRST_RUN", "false").strip().lower()
        in {"1", "true", "yes", "on"}
    )

    missing = [
        name
        for name, value in (("BOT_TOKEN", token), ("CHAT_ID", chat_id))
        if not value
    ]
    if missing:
        log.critical(
            "Missing required environment variable(s): %s. "
            "Copy .env.example to .env and fill them in.",
            ", ".join(missing),
        )
        raise SystemExit(2)

    return token, chat_id, admin_chat_id, notify_on_first_run


def parse_args(argv: Optional[list[str]] = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        prog="mostaql_notifier",
        description=(
            "Monitor Mostaql's development projects feed and push matching "
            "projects to Telegram."
        ),
    )
    parser.add_argument(
        "--once",
        action="store_true",
        help="run a single polling cycle and exit (useful for cron/Task Scheduler)",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help=(
            "print alerts to stdout instead of sending them; ignores the "
            "seen-state so every current match is shown; needs no .env"
        ),
    )
    parser.add_argument(
        "--test-telegram",
        action="store_true",
        help="send a test message to CHAT_ID and exit",
    )
    parser.add_argument(
        "--notify-on-first-run",
        action="store_true",
        help="also alert on the projects already listed at startup",
    )
    return parser.parse_args(argv)


def main(argv: Optional[list[str]] = None) -> int:
    args = parse_args(argv)

    client = HttpClient()
    scraper = MostaqlScraper(client)

    try:
        # ---- dry run: no credentials, no state, no side effects ----------
        if args.dry_run:
            log.info("DRY RUN -- nothing will be sent or persisted")
            monitor = MostaqlMonitor(
                scraper,
                DryRunNotifier(),
                # Path is never read or written: load() is not called and
                # persist=False, so the store stays empty for the whole run.
                SeenStore(BASE_DIR / ".dry-run-unused.json"),
                notify_on_first_run=True,
                persist=False,
                prime=False,                   # treat everything as new
            )
            return monitor.run(max_cycles=1)

        token, chat_id, admin_chat_id, notify_on_first_run = load_config()
        if args.notify_on_first_run:
            notify_on_first_run = True

        notifier = TelegramNotifier(token, chat_id, admin_chat_id)
        if not notifier.verify():
            log.critical("Telegram credentials are invalid -- aborting.")
            return 2

        if args.test_telegram:
            ok = notifier.send(format_test_message())
            log.info("Test message %s", "sent" if ok else "FAILED")
            return 0 if ok else 1

        store = SeenStore(STATE_PATH, cap=MAX_SEEN_IDS)
        store.load()

        monitor = MostaqlMonitor(
            scraper, notifier, store, notify_on_first_run=notify_on_first_run
        )
        monitor.install_signal_handlers()
        status = monitor.run(max_cycles=1 if args.once else None)
        if args.once:
            if status:
                log.error("Single-cycle run failed -- exiting non-zero for CI")
            return status
        return 0
    finally:
        client.close()


if __name__ == "__main__":
    try:
        sys.exit(main())
    except KeyboardInterrupt:
        log.info("Interrupted by user")
        sys.exit(0)
