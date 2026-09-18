# Mostaql Project Monitor → Telegram Notifier

Watches [Mostaql's development projects feed](https://mostaql.com/projects?category=development)
and pushes instant, richly formatted Telegram alerts for projects matching a
Backend-Engineer keyword profile — with a client-quality verdict on each one.

```
┌──────────────┐   ┌──────────────┐   ┌───────────────┐   ┌──────────────┐
│ listing page │ → │ dedupe vs    │ → │ keyword match │ → │ detail page  │
│ (newest 25)  │   │ seen_ids.json│   │ EN \b + AR    │   │ hire rate /  │
└──────────────┘   └──────────────┘   └───────────────┘   │ budget /bids │
                                                          └──────┬───────┘
                                                                 ▼
                                                     🟢 / 🟡 / 🔴  → Telegram
```

---

## Quick start

```bash
pip install -r requirements.txt
cp .env.example .env        # then fill in BOT_TOKEN and CHAT_ID
python mostaql_notifier.py --test-telegram   # confirm the bot can reach you
python mostaql_notifier.py                   # run the monitor
```

### Getting `BOT_TOKEN` and `CHAT_ID`

1. Message [@BotFather](https://t.me/BotFather) → `/newbot` → copy the token into `BOT_TOKEN`.
2. Send your new bot any message, then open
   `https://api.telegram.org/bot<TOKEN>/getUpdates` and read `result[0].message.chat.id`
   → that number goes in `CHAT_ID`.
3. For a group/channel: add the bot, then use the **negative** ID (e.g. `-1001234567890`).

> The bot cannot message you first — you must message it before `/getUpdates` returns anything.

---

## CLI

| Command | What it does |
|---|---|
| `python mostaql_notifier.py` | Poll forever (75–90 s jittered). Ctrl-C to stop cleanly. |
| `--dry-run` | Print alerts to stdout. Needs **no** `.env`, ignores saved state so every current match is shown, and never writes state. **Start here.** |
| `--once` | A single cycle, then exit — for cron / Task Scheduler. |
| `--test-telegram` | Send one test message and exit. |
| `--notify-on-first-run` | Also alert on the projects already listed at startup (see below). |

---

## Keyword matching

Matching runs over **title + brief only** — never the whole page, which would
otherwise match on nav links and category names.

**English uses regex word boundaries** (`\bword\b`, case-insensitive):

```
python django fastapi flask backend api apis rest restful graphql
websocket websockets sql postgresql postgres mysql database redis
scraping scraper bot docker webhooks
```

This is what stops the substring false positives:

| Text | Naive `in` | This matcher |
|---|---|---|
| `robot`, `about` | ❌ matches `bot` | ✅ no match |
| `restaurant` | ❌ matches `rest` | ✅ no match |
| `Rapid`, `Capital` | ❌ match `api` | ✅ no match |
| `REST API` | — | ✅ matches `rest` |

**Arabic uses normalized substring matching**, deliberately *not* word
boundaries: Arabic attaches clitics directly to the stem, so `\bبايثون\b` would
miss `بالبايثون` and `والدوكر`. Text and keywords are both folded first
(`أإآ→ا`, `ة→ه`, `ى→ي`, harakat stripped), so spelling variants still match.

```
بايثون  جانغو  باك اند  باكاند  باك إند  قاعدة بيانات  سحب بيانات
كشط  بوت  دوكر  ربط api  بوابة دفع
```

> **Tuning:** the generic `تكامل` ("integration") term was deliberately removed
> because it matched broad Shopify/domain setup work, not just backend/API
> projects. Keep the remaining list focused on backend vocabulary. Everything
> is a plain tuple at the top of the file — edit and restart.

---

## Quality classification

Every match triggers a follow-up GET of the project page, which supplies
budget, duration, hire rate and competition. Verdicts:

| Badge | Tier | Rule |
|---|---|---|
| 🟢 | High / Promising | Hire rate ≥ 50% **or** verified client with ≤ 5 bids |
| 🟡 | Average / Neutral | New client, or moderate hire rate/budget |
| 🔴 | Risky | ≥ 2 open projects with a 0% hire rate, **or** low budget vs. large scope |

Risk is evaluated **first**: a project that is genuinely bad is not upgraded to
green just because it also has a healthy hire rate.

Two client states are deliberately kept distinct, because they mean different
things:

* `لم يحسب بعد` → *"not yet calculated"* → reported as a **new client**, and
* `0%` → *"has never hired"* — a real track record, and a risk input.

> **Known limit:** Mostaql caps the rendered bid list at **50** items. A project
> showing "50+" is flagged as `50+ (page cap)` in the alert rather than being
> reported as exactly 50. Verified against a live 76-bid project.

---

## Resilience

| Concern | Handling |
|---|---|
| State survives restarts | `seen_ids.json`, written atomically (temp file + `os.replace`) |
| Unbounded file growth | Capped at the newest **1000** IDs, oldest evicted |
| Missing / corrupt state | Missing → starts fresh. Corrupt → quarantined to `seen_ids.json.corrupt-<ts>` and starts fresh; never crashes |
| Network failures | 4 attempts, exponential backoff with full jitter, capped at 60 s |
| `429`/`503` | Honours `Retry-After` when present |
| Cloudflare | Coherent desktop headers; challenge pages are **detected** and raised as `CloudflareChallenge` rather than silently parsing to zero projects |
| Layout breakage | Zero projects parsed for **5 consecutive cycles** → admin alert. Repeats at most every 30 cycles |
| One bad project | Caught per-project; can never kill the loop |
| Telegram parse errors | Every outgoing message is validated against Telegram's MarkdownV2 rules **before** the HTTP call; a malformed message is reported with the offending offset and sent as plain text instead |
| Telegram rate limits | Honours `parameters.retry_after` on 429 |
| Clean shutdown | `SIGINT`/`SIGTERM` flush state before exit |

### First-run priming

A cold start would otherwise replay every project currently on page 1 as if it
were new (~25 notifications). Instead the first cycle **marks them all as seen
without alerting**. Use `--notify-on-first-run` to get that initial batch anyway.

---

## Verified DOM selectors

Every selector below was confirmed against live pages, not guessed:

| Field | Selector |
|---|---|
| Listing rows | `tr.project-row` |
| Title / URL | `.card--title h2 a` (fallback `a.details-url`) |
| Brief | `p.project__brief` |
| Budget | `#project-meta-panel .meta-row` → label `الميزانية` |
| Duration | same panel → label `مدة التنفيذ` |
| Hire rate | `[data-type="employer_widget"] table.table-meta` → `معدل التوظيف` |
| Bids | `#bidsCollection-panel div.bid` (capped at 50 — see above) |
| Description | `#projectDetailsTab .text-wrapper-div` |
| Skills | `#project-meta-panel ul.skills li` |

Three layout quirks worth knowing if you ever re-tune this:

1. The employer card is rendered **twice** (responsive columns) — take the first match.
2. The skills list is **not** reachable through the label/value pair: the
   `المهارات` row has an empty `.meta-value`, and the tags live in a sibling
   `ul.skills.list-tags` under a row with no `.meta-value` at all. They're
   pulled by their own class and de-duplicated.
3. `#project-brief` is the whole panel *including* its "تفاصيل المشروع" heading —
   scope to `#projectDetailsTab` or the heading lands in every alert.

---

## Running it in the background

**Windows (Task Scheduler)** — trigger at logon, action:
```
pythonw.exe D:\Tech\freelnacing\mostaql_notifier_bot\mostaql_notifier.py
```
Use `pythonw.exe` (no console window) and set "Start in" to the project folder so
`.env` and `seen_ids.json` resolve.

**Windows (PowerShell, background):**
```powershell
Start-Process -WindowStyle Hidden pythonw `
  -ArgumentList "D:\Tech\freelnacing\mostaql_notifier_bot\mostaql_notifier.py"
```

**Linux/systemd:**
```ini
[Unit]
Description=Mostaql Telegram notifier
After=network-online.target

[Service]
WorkingDirectory=/opt/mostaql_notifier_bot
ExecStart=/usr/bin/python3 /opt/mostaql_notifier_bot/mostaql_notifier.py
Restart=always
RestartSec=30
User=mostaql

[Install]
WantedBy=multi-user.target
```

**Prefer `--once` on a scheduler?** It works, but you lose the in-process
backoff and the consecutive-empty-cycle watchdog is reset each run — the
long-running mode is better at detecting layout breakage.

**GitHub Actions (every 5 minutes):** `.github/workflows/mostaql_monitor.yml`
runs the offline test suite, executes `python mostaql_notifier.py --once`, and
commits the updated `seen_ids.json` back to the branch as
`github-actions[bot]`. Add `BOT_TOKEN` and `CHAT_ID` (and optionally
`ADMIN_CHAT_ID`) under **Settings → Secrets and variables → Actions**.
`seen_ids.json` is intentionally tracked so state survives between runs.

---

## Tests

```bash
python test_offline.py
```

223 assertions, no network and no credentials. Runs against the saved live
fixtures (`listing.html`, `detail.html`, `detail2.html`) plus synthetic HTML and
stub scrapers. Covers the parser, keyword false-positive guards (the `robot`/
`restaurant` cases), budget/number parsing, MarkdownV2 escaping **and a
structural MarkdownV2 validator exercised against every reserved character**,
the classifier, seen-store corruption and capping, and the full polling loop
(priming, restart dedupe, watchdog, fetch-failure survival, dry-run semantics).

The fixtures are real pages captured from Mostaql — keep them, they're what make
the DOM regressions catchable.

---

## Tuning reference

Constants at the top of `mostaql_notifier.py`:

| Constant | Default | Meaning |
|---|---|---|
| `LOOP_INTERVAL_MIN` / `MAX` | 75 / 90 | Jittered poll window (seconds) |
| `MAX_SEEN_IDS` | 1000 | Persisted ID cap |
| `EMPTY_CYCLE_ALERT_THRESHOLD` | 5 | Empty cycles before the admin alert |
| `HIGH_HIRE_RATE` | 50.0 | 🟢 hire-rate threshold |
| `LOW_COMPETITION_BIDS` | 5 | 🟢 "low competition" ceiling |
| `LOW_BUDGET_CEILING` | 100.0 | 🔴 low-budget ceiling ($) |
| `SCOPE_WORD_COUNT` | 120 | 🔴 "large scope" word count |
| `BRIEF_MAX_CHARS` | 600 | Brief truncation in alerts |
| `REQUEST_TIMEOUT` | 30.0 | Per-request timeout |
| `DETAIL_FETCH_DELAY` | 1.5 | Politeness pause between detail hits |

---

## MarkdownV2: why messages are built, not written

Telegram's MarkdownV2 rejects an **entire message** over a single unescaped
reserved character — `_ * [ ] ( ) ~ ` > # + - = | { } . !` and `\`. Since
alerts contain scraped Arabic, budgets like `$50.00`, durations and client names
like `نايف ا.`, hand-escaping is not survivable.

So message text is never written as a raw string. Every literal goes through a
composition helper, so escaping is structural rather than something the author
has to remember:

```python
md_bold("Budget:")          # *Budget\:*
md_italic("no history")     # _no history_
md_link("Open project", url)  # [Open project](https://...)
md_field("💰", "Budget:", "$50.00 - $100.00")
```

`validate_markdownv2()` then runs as a **pre-flight guard inside
`TelegramNotifier.send()`**. A malformed message never reaches the API — it is
logged with the exact offset and context, then sent as plain text:

```
Outgoing message is not valid MarkdownV2 (unescaped '.' at offset 107
(context: ' consecutive cycles*.\n\nThis usually means')) -- falling back
to plain text. This is a bug in the message builder, not in the scraped data.
```

That distinction matters: it tells you whether to fix code or expect bad data.

> **Every message builder must appear in `OUTGOING_MESSAGES` in
> `test_offline.py`.** The original `can't parse entities: Character '.'`
> failure came from `--test-telegram`'s inline string, which no test covered.
> That list is the regression guard — add to it when you add a message.

---

## Notes and limitations

* **Cloudflare.** Plain `requests` with coherent desktop headers currently
  works. If Mostaql tightens its bot rules you'll see `CloudflareChallenge` in
  the log — that needs a residential proxy or a headless browser, not a header
  tweak. The script deliberately fails loudly rather than reporting "no new
  projects".
* **Polling, not webhooks.** New projects are detected on the next cycle, so
  expect up to ~90 s of latency.
* **Listing page only.** Only page 1 (25 newest) is read. At a 75–90 s interval
  that is far more than the posting rate, but if the category ever exceeds ~25
  posts per cycle you'd want to paginate.
* **`is_verified`** is read from `.profile-verification-badge` inside the
  employer widget. Mostaql does not render this badge for every client, so a
  `False` here means "no badge shown", not "confirmed unverified".
* **Scrape responsibly.** ~1 request per 80 s plus one per matched project.
* **`LOG_LEVEL=DEBUG` logs your bot token.** `urllib3` prints full request URLs,
  and the Bot API puts the token in the path. Use `DEBUG` only when diagnosing,
  and don't ship those logs anywhere.
