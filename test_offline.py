#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Offline test suite for mostaql_notifier.

Runs against the saved live fixtures (listing.html / detail.html / detail2.html)
plus synthetic HTML for edge cases. No network, no Telegram, no credentials.

    python test_offline.py
"""

import json
import logging
import sys
import tempfile
from pathlib import Path

import mostaql_notifier as m

BASE = Path(__file__).resolve().parent
PASSED, FAILED = 0, 0


def check(label, condition, detail=""):
    global PASSED, FAILED
    if condition:
        PASSED += 1
        print(f"  PASS  {label}")
    else:
        FAILED += 1
        print(f"  FAIL  {label}  {detail}")


def section(title):
    print(f"\n=== {title} ===")


# ---------------------------------------------------------------------------
section("Listing parser (live fixture)")

listing_html = (BASE / "listing.html").read_text(encoding="utf-8")
projects = m.MostaqlScraper.parse_listing(listing_html)

check("parses 25 rows", len(projects) == 25, f"got {len(projects)}")
check("ids unique", len({p.project_id for p in projects}) == len(projects))
check("all ids numeric", all(p.project_id.isdigit() for p in projects))
check("all urls absolute https",
      all(p.url.startswith("https://mostaql.com/project/") for p in projects))
check("all titles non-empty", all(p.title.strip() for p in projects))
check("all briefs non-empty", all(p.brief.strip() for p in projects))
check("all have posted_at", all(p.posted_at for p in projects))
check("bids parsed for all", all(p.listing_bids is not None for p in projects),
      str([p.listing_bids for p in projects]))
check("client parsed for all", all(p.client_name for p in projects))

first = projects[0]
print(f"  sample: id={first.project_id} bids={first.listing_bids} "
      f"client={first.client_name!r}")
print(f"          title={first.title[:60]!r}")

# The listing reported '76 عرض' for this project -> Arabic numeral handling.
by_id = {p.project_id: p for p in projects}
check("'76 عرض' parsed as 76",
      any(p.listing_bids == 76 for p in projects),
      str(sorted(p.listing_bids for p in projects)))


# ---------------------------------------------------------------------------
section("Listing parser: Arabic spelled-out numerals")

synthetic = """
<tr class="project-row"><td class="row-td">
  <div class="card--title"><h2><a href="https://mostaql.com/project/999-x">عنوان</a></h2>
    <ul class="project__meta">
      <li class="text-muted"><bdi>Ali A.</bdi></li>
      <li class="text-muted"><time datetime="2026-01-01 00:00:00"></time></li>
      <li class="text-muted"><span class="hsoub-file-signature-icon"></span> عرض واحد </li>
    </ul>
  </div>
  <p class="project__brief"><a href="https://mostaql.com/project/999-x" class="details-url">نبذة</a></p>
</td></tr>
"""
syn = m.MostaqlScraper.parse_listing(synthetic)
check("synthetic row parsed", len(syn) == 1)
check("'عرض واحد' -> 1", syn and syn[0].listing_bids == 1,
      str(syn[0].listing_bids) if syn else "no row")
check("client name extracted", syn and syn[0].client_name == "Ali A.")
check("brief extracted", syn and syn[0].brief == "نبذة")


# ---------------------------------------------------------------------------
section("Detail parser (live fixture, 1 bid)")

detail_html = (BASE / "detail.html").read_text(encoding="utf-8")
d = m.MostaqlScraper.parse_detail(detail_html)

print(f"  budget={d.budget_raw!r} min={d.budget_min} max={d.budget_max}")
print(f"  duration={d.duration!r} hire_rate={d.hire_rate} bids={d.bids}")
print(f"  open={d.open_projects} in_progress={d.in_progress} "
      f"verified={d.is_verified}")

check("budget range parsed", d.budget_min == 25.0 and d.budget_max == 50.0,
      f"{d.budget_min}/{d.budget_max}")
check("hire rate parsed as 57.14", d.hire_rate == 57.14, str(d.hire_rate))
check("duration parsed", bool(d.duration), repr(d.duration))
check("bids == 1", d.bids == 1, str(d.bids))
check("bids not flagged capped", d.bids_capped is False)
check("open_projects == 1", d.open_projects == 1, str(d.open_projects))
check("registration date parsed", bool(d.registration_date))
check("description non-empty", len(d.description) > 50, str(len(d.description)))
# NB: this fixture's skills meta-row is genuinely empty on the live site, so we
# assert the field exists rather than that it is populated (detail2 has them).
check("skills field is a list", isinstance(d.skills, list))
check("duration value looks right", "يوم" in (d.duration or ""), repr(d.duration))
check("in_progress parsed", d.in_progress == 0, str(d.in_progress))
# Regression: the #project-brief panel header ("تفاصيل المشروع") must not be
# prepended to the description.
check("description has no panel heading",
      not d.description.startswith("تفاصيل المشروع"), repr(d.description[:30]))
check("description2 has no panel heading",
      not m.MostaqlScraper.parse_detail(
          (BASE / "detail2.html").read_text(encoding="utf-8")
      ).description.startswith("تفاصيل المشروع"))


# ---------------------------------------------------------------------------
section("Detail parser: bid-page cap (76-bid project renders 50)")

detail2_html = (BASE / "detail2.html").read_text(encoding="utf-8")
d2 = m.MostaqlScraper.parse_detail(detail2_html)

print(f"  bids={d2.bids} capped={d2.bids_capped} raw={d2.bids_raw!r}")
print(f"  hire_rate={d2.hire_rate} raw={d2.hire_rate_raw!r} "
      f"pending={d2.hire_rate_pending}")
check("bids capped at 50", d2.bids == 50, str(d2.bids))
check("cap flagged", d2.bids_capped is True)
check("raw shows '50+'", d2.bids_raw == "50+", repr(d2.bids_raw))
# This client's hire rate is the literal placeholder 'لم يحسب بعد' ("not yet
# calculated"), so None is the CORRECT parse -- but it must be flagged pending,
# not confused with "0% hire rate" (which means "never hired").
check("hire rate raw captured", d2.hire_rate_raw == "لم يحسب بعد",
      repr(d2.hire_rate_raw))
check("pending flag set", d2.hire_rate_pending is True)
check("hire rate is None (not 0.0)", d2.hire_rate is None, str(d2.hire_rate))
check("skills extracted on this fixture", len(d2.skills) > 0, str(d2.skills[:3]))
check("budget parsed on this fixture", d2.budget_max is not None,
      str(d2.budget_raw))


# ---------------------------------------------------------------------------
section("Classifier: pending vs zero hire rate are different")

pending_stub = m.ProjectStub(project_id="1", title="t", url="u")
pending_detail = m.ProjectDetail(hire_rate=None, hire_rate_pending=True, bids=1)
zero_detail = m.ProjectDetail(hire_rate=0.0, hire_rate_pending=False, bids=1)

pending_verdict = m.assess(pending_stub, pending_detail)
zero_verdict = m.assess(pending_stub, zero_detail)
print(f"  pending -> {pending_verdict.reason!r}")
print(f"  zero    -> {zero_verdict.reason!r}")

check("pending reason mentions 'not calculated'",
      "not calculated" in pending_verdict.reason, pending_verdict.reason)
check("zero reason mentions 'not hired'",
      "not hired" in zero_verdict.reason, zero_verdict.reason)
check("both are yellow", pending_verdict.badge == zero_verdict.badge == "🟡")


# ---------------------------------------------------------------------------
section("Budget parsing edge cases")

cases = [
    ("$25.00 - $50.00", (25.0, 50.0)),
    ("$500", (500.0, 500.0)),
    ("1,500 - 3,000 ريال", (1500.0, 3000.0)),
    ("غير محدد", (None, None)),
    ("", (None, None)),
    ("$1,200.50 - $2,400.75", (1200.50, 2400.75)),
]
for raw, expected in cases:
    got = m.MostaqlScraper._parse_budget(raw)
    check(f"budget {raw!r} -> {expected}", got == expected, f"got {got}")


# ---------------------------------------------------------------------------
section("Keyword matching: true positives")

TP = [
    ("Build a REST API in Django", ["api", "django", "rest"]),
    ("Python web scraping bot", ["python", "scraping", "bot"]),
    ("FastAPI backend with PostgreSQL and Redis", ["fastapi", "backend", "postgresql", "redis"]),
    ("Docker deployment with webhooks", ["docker", "webhooks"]),
    ("GraphQL + WebSockets realtime app", ["graphql", "websockets"]),
    ("مطلوب مبرمج بايثون لبناء API", ["بايثون", "api"]),
    ("أحتاج مطور باك اند محترف", ["باك اند"]),
    ("ربط API مع بوابة دفع", ["ربط api", "بوابة دفع"]),
    ("عمل قاعدة بيانات وسحب بيانات من موقع", ["قاعدة بيانات", "سحب بيانات"]),
    ("مشروع دوكر وبوت تيليجرام", ["دوكر", "بوت"]),
]
for text, expected in TP:
    got = m.match_keywords(text)
    missing = [k for k in expected if k not in got]
    check(f"TP {text[:44]!r}", not missing, f"missing={missing} got={got}")


# ---------------------------------------------------------------------------
section("Keyword matching: false-positive guards (the critical cases)")

FP = [
    # The spec's explicit examples: substring traps.
    ("Design a robot mascot", "bot"),
    ("Best restaurant website design", "rest"),
    ("About us page redesign", "bot"),
    ("I need a logo for my startup", "api"),
    # More substring traps for the rest of the list.
    ("Rapid prototyping in Figma", "api"),        # 'Rapid' contains 'api'
    ("Translate the following document", None),
    ("SQLite is not in the keyword list", "sql"),
    ("Capital letters layout", "api"),             # 'Capital'
    ("Therapist landing page", "api"),             # 'rapi'
    ("My portfolio needs animation", None),
    ("Bootstrap theme customization", "bot"),      # 'Boot' ~ not 'bot' (b-o-o-t)
    ("Restaurant menu app", "rest"),
    ("Directory listing site", None),
    ("Data entry and Excel work", "database"),
]
for text, must_not in FP:
    got = m.match_keywords(text)
    if must_not is None:
        # Assert no match at all for the purely-safe strings.
        check(f"FP {text[:44]!r} -> no match", got == [], f"got {got}")
    else:
        check(f"FP {text[:44]!r} does not match {must_not!r}",
              must_not not in got, f"got {got}")


# ---------------------------------------------------------------------------
section("Keyword matching: Arabic clitic handling")

# Arabic attaches prefixes/suffixes directly; substring matching must catch
# these where a word-boundary regex would fail.
CLITIC = [
    "اكتب لي كود بالبايثون",
    "المشروع بالدوكر",
    "عندي فكرة بوت واتساب",
    "أريد قاعدة بيانات كبيرة",
]
for text in CLITIC:
    got = m.match_keywords(text)
    check(f"clitic {text!r} matches", len(got) > 0, f"got {got}")


# ---------------------------------------------------------------------------
section("Word-boundary correctness vs naive substring")

check("'robot' does not match 'bot'", "bot" not in m.match_keywords("robot"))
check("'about' does not match 'bot'", "bot" not in m.match_keywords("about"))
check("'restaurant' does not match 'rest'",
      "rest" not in m.match_keywords("restaurant"))
check("'restful' DOES match 'restful'", "restful" in m.match_keywords("restful"))
check("'restful' also matches 'rest' via boundary",
      "rest" in m.match_keywords("REST API"),
      "REST uppercase should match 'rest'")
check("naive substring WOULD have failed this check",
      "bot" in "robot")  # documents why boundaries matter


# ---------------------------------------------------------------------------
section("Keyword profile: required backend vocabulary")

REQUIRED_EN = (
    "python", "django", "fastapi", "flask", "backend", "api", "rest",
    "sql", "postgresql", "redis", "docker", "webhooks",
)
REQUIRED_AR = ("بايثون", "جانغو", "باك اند", "ربط api")

for keyword in REQUIRED_EN:
    check(f"English keyword {keyword!r} configured",
          keyword in m.ENGLISH_KEYWORDS)
for keyword in REQUIRED_AR:
    check(f"Arabic keyword {keyword!r} configured",
          keyword in m.ARABIC_KEYWORDS)

check("generic 'تكامل' removed from Arabic keywords",
      "تكامل" not in m.ARABIC_KEYWORDS)
check("Shopify/domain integration no longer matches",
      m.match_keywords("مطلوب تكامل متجر شوبيفاي مع دومين") == [],
      str(m.match_keywords("مطلوب تكامل متجر شوبيفاي مع دومين")))


# ---------------------------------------------------------------------------
section("MarkdownV2 escaping")

check("escapes underscore", m.escape_md("a_b") == r"a\_b", m.escape_md("a_b"))
check("escapes asterisk", m.escape_md("*x*") == r"\*x\*", m.escape_md("*x*"))
check("escapes dot/bang", m.escape_md("57.14%!") == r"57\.14%\!",
      m.escape_md("57.14%!"))
check("escapes brackets", m.escape_md("[a]") == r"\[a\]", m.escape_md("[a]"))
check("escapes backslash", m.escape_md("a\\b") == r"a\\b", m.escape_md("a\\b"))
check("url escapes close-paren", m.escape_md_url("a)b") == r"a\)b",
      m.escape_md_url("a)b"))
check("arabic passes through", m.escape_md("بايثون") == "بايثون")
check("strip_markdown roundtrips", m.strip_markdown(r"a\_b") == "a_b",
      m.strip_markdown(r"a\_b"))


# ---------------------------------------------------------------------------
section("Alert formatting (real fixture)")

stub = projects[0]
stub.matched_keywords = ["python", "api"]
alert = m.format_alert(stub, d, m.assess(stub, d))

check("alert non-empty", len(alert) > 100)
check("contains badge", any(b in alert for b in ("🟢", "🟡", "🔴")))
check("contains markdown link", "](" in alert and stub.url[:40] in alert)
check("under telegram limit", len(alert) < m.TG_MAX_LEN, str(len(alert)))
print("\n" + "-" * 62)
print(alert)
print("-" * 62)


# ---------------------------------------------------------------------------
section("Classification rules")

def mk(hire=None, bids=None, openp=None, budget_max=None, verified=False,
       brief="", listing_bids=None):
    s = m.ProjectStub(project_id="1", title="t", url="u", brief=brief,
                      listing_bids=listing_bids)
    dd = m.ProjectDetail(hire_rate=hire, bids=bids, open_projects=openp,
                         is_verified=verified, budget_max=budget_max,
                         budget_raw=f"${budget_max}" if budget_max else None,
                         description=brief)
    return m.assess(s, dd)


check("hire 57% -> green", mk(hire=57.14, bids=1).badge == "🟢")
check("hire 50% -> green (boundary)", mk(hire=50.0, bids=9).badge == "🟢")
check("hire 49% -> not green", mk(hire=49.0, bids=9).badge != "🟢")
check("verified + <=5 bids -> green", mk(hire=None, bids=3, verified=True).badge == "🟢")
check("verified + 6 bids -> not green", mk(hire=None, bids=6, verified=True).badge != "🟢")
check("0% hire + 2 open -> red",
      mk(hire=0, bids=1, openp=2).badge == "🔴")
check("0% hire + 1 open -> not red",
      mk(hire=0, bids=1, openp=1).badge != "🔴")
check("low budget + huge scope -> red",
      mk(hire=None, bids=1, budget_max=50, brief="word " * 150).badge == "🔴")
check("low budget + small scope -> not red",
      mk(hire=None, bids=1, budget_max=50, brief="short brief").badge != "🔴")
check("new client -> yellow", mk(hire=None, bids=2).badge == "🟡")
check("0% hire + 1 open -> yellow", mk(hire=0, bids=2, openp=1).badge == "🟡")
check("reason is non-empty", bool(mk(hire=57.14, bids=1).reason))
check("listing bid fallback used",
      mk(hire=None, bids=None, listing_bids=4).badge == "🟡")


# ---------------------------------------------------------------------------
section("SeenStore persistence")

with tempfile.TemporaryDirectory() as td:
    path = Path(td) / "seen_ids.json"

    # -- missing file
    s = m.SeenStore(path)
    s.load()
    check("missing file -> empty", len(s) == 0)

    # -- basic add/save/reload roundtrip
    s.add_many(["1", "2", "3"])
    check("changed save reports a write", s.save() is True)
    check("saved file exists", path.exists())
    check("3 ids stored", len(s) == 3)

    s2 = m.SeenStore(path)
    s2.load()
    check("reload restores ids", len(s2) == 3 and "2" in s2)
    check("load leaves store clean", s2._dirty is False)

    # -- dedupe
    s2.add("1")
    check("duplicate add is a no-op", len(s2) == 3)

    # -- no-op save must not rewrite the file (or bump updated_at)
    before = path.read_text(encoding="utf-8")
    check("no-op save reports no write", s2.save() is False)
    check("no-op save leaves file untouched",
          path.read_text(encoding="utf-8") == before)

    # -- a genuinely new ID is persisted and clears the dirty flag
    s2.add("4")
    check("new id marks store dirty", s2._dirty is True)
    check("new id save reports a write", s2.save() is True)
    check("save clears dirty flag", s2._dirty is False)
    s2b = m.SeenStore(path)
    s2b.load()
    check("new id survives reload", "4" in s2b and len(s2b) == 4)
    check("loaded state is clean again", s2b.save() is False)

    # -- cap enforcement
    cap_store = m.SeenStore(path, cap=5)
    cap_store.add_many(str(i) for i in range(50))
    check("cap enforced", len(cap_store) == 5, str(len(cap_store)))
    check("keeps NEWEST ids", "49" in cap_store and "0" not in cap_store)
    check("repr of _ids is newest-last", cap_store._ids[-1] == "49")

    # -- corrupted JSON
    bad = Path(td) / "bad.json"
    bad.write_text("{not valid json at all", encoding="utf-8")
    s3 = m.SeenStore(bad)
    s3.load()
    check("corrupt file -> empty store", len(s3) == 0)
    check("corrupt file quarantined",
          any(p.name.startswith("bad.corrupt-") for p in Path(td).iterdir()),
          str([p.name for p in Path(td).iterdir()]))

    # -- wrong shape
    wrong = Path(td) / "wrong.json"
    wrong.write_text(json.dumps({"ids": "not-a-list"}), encoding="utf-8")
    s4 = m.SeenStore(wrong)
    s4.load()
    check("wrong shape -> empty store", len(s4) == 0)

    # -- bare-list backward compat
    bare = Path(td) / "bare.json"
    bare.write_text(json.dumps(["a", "b"]), encoding="utf-8")
    s5 = m.SeenStore(bare)
    s5.load()
    check("bare-list state accepted", len(s5) == 2)

    # -- int ids coerced
    ints = Path(td) / "ints.json"
    ints.write_text(json.dumps({"ids": [1, 2, 3]}), encoding="utf-8")
    s6 = m.SeenStore(ints)
    s6.load()
    check("int ids coerced to str", "1" in s6 and len(s6) == 3)


# ---------------------------------------------------------------------------
section("Utilities")

check("normalize folds alef", m.normalize_arabic("أحمد") == m.normalize_arabic("احمد"))
check("normalize folds taa marbuta",
      m.normalize_arabic("قاعدة") == m.normalize_arabic("قاعده"))
check("normalize strips harakat", m.normalize_arabic("بَايِثُون") == "بايثون",
      m.normalize_arabic("بَايِثُون"))
check("arabic-indic digits", m.parse_int("٤٢ عرض") == 42, str(m.parse_int("٤٢ عرض")))
check("parse_number percent", m.parse_number("57.14%") == 57.14)
check("parse_number none", m.parse_number("لا يوجد") is None)
check("parse_int word", m.parse_int("عرض واحد") == 1)
check("parse_int digits", m.parse_int("18 عرض") == 18)

# Verify the Arabic keyword list itself folds consistently with the matcher.
check("arabic keyword 'باك إند' folds to match 'باك اند' text",
      "باك إند" in m.match_keywords("اريد باك اند"),
      str(m.match_keywords("اريد باك اند")))


# ---------------------------------------------------------------------------
section("DOM-breakage watchdog alert")

watchdog_alert = m.format_dom_alert(5)
check("watchdog alert non-empty", len(watchdog_alert) > 50)
check("watchdog mentions count", "5" in watchdog_alert)
check("watchdog is markdown-safe",
      m.strip_markdown(watchdog_alert).count("\\") == 0,
      m.strip_markdown(watchdog_alert)[:80])
check("watchdog under limit", len(watchdog_alert) < m.TG_MAX_LEN)


# ---------------------------------------------------------------------------
section("Dry-run notifier")

dry = m.DryRunNotifier()
check("verify() is a no-op true", dry.verify() is True)
check("send() reports success", dry.send("*hello* _world_") is True)
check("send() records message", len(dry.sent) == 1)
check("send(admin=True) works", dry.send("x", admin=True) is True)
check("admin message recorded", len(dry.sent) == 2)


# ---------------------------------------------------------------------------
section("Telegram token redaction")

TOKEN = "123456:TEST-SECRET-TOKEN"


class FailingSession:
    def get(self, *args, **kwargs):
        raise m.requests.RequestException(
            f"GET https://api.telegram.org/bot{TOKEN}/getMe failed"
        )

    def post(self, *args, **kwargs):
        raise m.requests.RequestException(
            f"POST https://api.telegram.org/bot{TOKEN}/sendMessage failed"
        )


log_messages: list[str] = []


class CaptureLogs(logging.Handler):
    def emit(self, record):
        log_messages.append(record.getMessage())


bot = m.TelegramNotifier(TOKEN, "chat")
bot.session = FailingSession()
capture = CaptureLogs()
old_level = m.log.level
m.log.addHandler(capture)
m.log.setLevel(logging.DEBUG)
try:
    bot._post("chat", "hello", parse_mode=None)
    bot.verify()
finally:
    m.log.removeHandler(capture)
    m.log.setLevel(old_level)

joined_logs = "\n".join(log_messages)
check("raw bot token absent from logs", TOKEN not in joined_logs, joined_logs[:200])
check("redaction marker present", m.REDACTED_TOKEN in joined_logs)
check("redact_token helper replaces token",
      m.redact_token(f"url /bot{TOKEN}/x", TOKEN) == "url /bot<REDACTED>/x")


# ---------------------------------------------------------------------------
section("Alert renders for a detail-less project (fetch failure)")

failed_detail = m.ProjectDetail(fetch_ok=False, error="HTTP 503")
stub_f = m.ProjectStub(project_id="42", title="مشروع بايثون",
                       url="https://mostaql.com/project/42-x",
                       brief="مطلوب مبرمج بايثون", listing_bids=3,
                       matched_keywords=["python"])
alert_f = m.format_alert(stub_f, failed_detail, m.assess(stub_f, failed_detail))
check("degrades gracefully", "مشروع بايثون" in m.strip_markdown(alert_f))
check("fallback uses listing bids", "3" in m.strip_markdown(alert_f))
check("still has a link", "](" in alert_f)
check("under limit", len(alert_f) < m.TG_MAX_LEN)


# ---------------------------------------------------------------------------
section("Alert escaping with adversarial content")

nasty = m.ProjectStub(
    project_id="7",
    title="Fix [bug]_in *parser* `now` (v2.0)!",
    url="https://mostaql.com/project/7-a(b)",
    brief="Budget $50 - $100. Use C++ & _underscores_.",
    matched_keywords=["python"],
)
nasty_detail = m.ProjectDetail(budget_raw="$50 - $100", duration="3 days",
                               hire_rate=80.0, bids=2)
nasty_alert = m.format_alert(nasty, nasty_detail, m.assess(nasty, nasty_detail))
check("no unescaped specials in title",
      r"\[bug\]" in nasty_alert and r"\_in" in nasty_alert, nasty_alert[:120])
check("url close-paren escaped", r"a(b\)" in nasty_alert)
check("plain-text fallback is clean",
      "[" in m.strip_markdown(nasty_alert))
check("under limit", len(nasty_alert) < m.TG_MAX_LEN)


# ---------------------------------------------------------------------------
section("MarkdownV2 structural validation")

# Telegram's parser rejects the WHOLE message on any unescaped reserved
# character. `validate_markdownv2` now lives in the module itself and runs as a
# pre-flight guard before every send, so the tests exercise the real thing.
#
# The set below must cover EVERY message builder that can reach Telegram. The
# original bug was a builder (`--test-telegram`'s inline string) that no test
# validated -- so this list is the regression guard that matters most.
OUTGOING_MESSAGES = {
    "format_alert (live fixture)": alert,
    "format_alert (fetch failure)": alert_f,
    "format_alert (adversarial)": nasty_alert,
    "format_dom_alert": watchdog_alert,
    "format_test_message": m.format_test_message(),
}

for label, sample in OUTGOING_MESSAGES.items():
    ok, problem = m.validate_markdownv2(sample)
    check(f"valid MarkdownV2: {label}", ok, problem)

# Regression: the exact string that produced
#   "Bad Request: can't parse entities: Character '.' is reserved"
# must now be escaped at the source.
test_msg = m.format_test_message()
check("test message escapes its period", r"chat\." in test_msg, test_msg)

# Prove the validator actually catches breakage (guards against a no-op test).
check("validator detects unescaped '.'",
      m.validate_markdownv2("ends here.")[0] is False)
check("validator detects unescaped '-'",
      m.validate_markdownv2("a - b")[0] is False)
check("validator detects unescaped '!'",
      m.validate_markdownv2("wow!")[0] is False)
check("validator detects imbalance",
      m.validate_markdownv2("this _ is unbalanced")[0] is False)
check("validator detects dangling backslash",
      m.validate_markdownv2("trailing\\")[0] is False)
check("validator accepts escaped text",
      m.validate_markdownv2("ends here\\.")[0] is True)
check("validator accepts bold/italic",
      m.validate_markdownv2("*bold* _italic_")[0] is True)
check("validator accepts links",
      m.validate_markdownv2("[label](https://x\\.com/a\\)b)")[0] is True)

# Every reserved character, on its own, must be rejected bare and accepted
# escaped. This is the direct check for "is the full set covered".
for char in "_*[]()~`>#+-=|{}.!":
    bare_ok, _ = m.validate_markdownv2(f"x{char}y")
    esc_ok, _ = m.validate_markdownv2(f"x\\{char}y")
    # '*' and '_' and '`' are delimiters: bare-but-paired is legal, bare-single
    # is not, so `x*y` is flagged while `x*y*z` is fine. Everything else must
    # always be escaped.
    if char in "*_`":
        check(f"reserved {char!r}: escaped form accepted", esc_ok)
    else:
        check(f"reserved {char!r}: bare rejected, escaped accepted",
              bare_ok is False and esc_ok is True,
              f"bare={bare_ok} escaped={esc_ok}")


# ---------------------------------------------------------------------------
section("Monitor loop: priming, dedupe, watchdog")


class FakeScraper:
    """Stub scraper returning canned listings; counts detail fetches."""

    def __init__(self, listings):
        self.listings = listings          # list of pages, consumed per cycle
        self.detail_calls = []
        self._cycle = 0

    def fetch_listing(self):
        idx = min(self._cycle, len(self.listings) - 1)
        self._cycle += 1
        page = self.listings[idx]
        if isinstance(page, Exception):
            raise page
        return page

    def fetch_detail(self, url):
        self.detail_calls.append(url)
        return m.ProjectDetail(hire_rate=60.0, bids=2, open_projects=1,
                               budget_raw="$500 - $1000", budget_max=1000.0,
                               duration="5 أيام", description="python backend api")


class FailingNotifier:
    """Always fails delivery, simulating a Telegram outage."""

    def __init__(self):
        self.attempts = 0

    def send(self, text, *, admin=False):
        self.attempts += 1
        return False

    def verify(self):
        return True


class FlakyNotifier(FailingNotifier):
    """Fails the first delivery, then succeeds."""

    def __init__(self):
        super().__init__()
        self.sent = []

    def send(self, text, *, admin=False):
        self.attempts += 1
        if self.attempts == 1:
            return False
        self.sent.append(text)
        return True


def make_stub(pid, title="مشروع بايثون API"):
    return m.ProjectStub(project_id=pid, title=title,
                         url=f"https://mostaql.com/project/{pid}-x",
                         brief="python django backend api", listing_bids=2)


def run_monitor(store, listings, cycles, **kw):
    """Wire a monitor against fake data with sleeping disabled."""
    scraper = FakeScraper(listings)
    notifier = m.DryRunNotifier()
    mon = m.MostaqlMonitor(scraper, notifier, store, **kw)
    mon._sleep = lambda: None            # never actually idle
    mon.run(max_cycles=cycles)
    return scraper, notifier


with tempfile.TemporaryDirectory() as td:
    sp = Path(td) / "seen.json"

    # --- cold start primes instead of replaying the backlog ---------------
    store = m.SeenStore(sp)
    store.load()
    scraper, notifier = run_monitor(
        store, [[make_stub("1"), make_stub("2")]], cycles=1
    )
    check("first run sends nothing (priming)", len(notifier.sent) == 0,
          f"sent={len(notifier.sent)}")
    check("first run records ids", len(store) == 2, str(len(store)))
    check("state file written", sp.exists())
    check("no detail fetches while priming", scraper.detail_calls == [])

    # --- opt-in first-run notifications -----------------------------------
    store_n = m.SeenStore(Path(td) / "seen-notify-first.json")
    store_n.load()
    _, notifier_n = run_monitor(
        store_n, [[make_stub("1"), make_stub("2")]], cycles=1,
        notify_on_first_run=True,
    )
    check("notify-on-first-run alerts existing projects",
          len(notifier_n.sent) == 2, f"sent={len(notifier_n.sent)}")
    check("notify-on-first-run records ids", len(store_n) == 2,
          str(len(store_n)))

    # --- restart: same projects are not re-alerted ------------------------
    store2 = m.SeenStore(sp)
    store2.load()
    check("state survives restart", len(store2) == 2)
    scraper2, notifier2 = run_monitor(
        store2, [[make_stub("1"), make_stub("2")]], cycles=1
    )
    check("no re-alert after restart", len(notifier2.sent) == 0,
          f"sent={len(notifier2.sent)}")
    check("no detail fetch when nothing is new", scraper2.detail_calls == [])

    # --- a genuinely new project IS alerted -------------------------------
    store3 = m.SeenStore(sp)
    store3.load()
    scraper3, notifier3 = run_monitor(
        store3, [[make_stub("3"), make_stub("1"), make_stub("2")]], cycles=1
    )
    check("new project alerted exactly once", len(notifier3.sent) == 1,
          f"sent={len(notifier3.sent)}")
    check("detail fetched only for the new one",
          scraper3.detail_calls == ["https://mostaql.com/project/3-x"],
          str(scraper3.detail_calls))
    check("new id persisted", "3" in store3)

    # --- failed delivery keeps the ID unseen and marks the cycle unhealthy -
    fail_state = Path(td) / "seen-delivery-fail.json"
    store_fail = m.SeenStore(fail_state)
    store_fail.load()
    notifier_fail = FailingNotifier()
    mon_fail = m.MostaqlMonitor(
        FakeScraper([[make_stub("fail-1")]]), notifier_fail, store_fail,
        notify_on_first_run=True, prime=False,
    )
    mon_fail._sleep = lambda: None
    status_fail = mon_fail.run(max_cycles=1)
    check("failed delivery is not marked seen", "fail-1" not in store_fail)
    check("failed delivery leaves no state file", not fail_state.exists())
    check("failed delivery reports unhealthy", status_fail == 1, str(status_fail))
    check("failed delivery attempted once", notifier_fail.attempts == 1,
          str(notifier_fail.attempts))

    # --- the next cycle retries and persists only after delivery succeeds -
    retry_state = Path(td) / "seen-delivery-retry.json"
    store_retry = m.SeenStore(retry_state)
    store_retry.load()
    notifier_retry = FlakyNotifier()
    mon_retry = m.MostaqlMonitor(
        FakeScraper([[make_stub("retry-1")]]), notifier_retry, store_retry,
        notify_on_first_run=True, prime=False,
    )
    mon_retry._sleep = lambda: None
    status_retry = mon_retry.run(max_cycles=2)
    check("failed delivery is retried next cycle", notifier_retry.attempts == 2,
          str(notifier_retry.attempts))
    check("successful retry is marked seen", "retry-1" in store_retry)
    check("successful retry is persisted", retry_state.exists())
    check("retry cycle ends healthy", status_retry == 0, str(status_retry))

    # --- unmatched projects are still marked seen without an alert --------
    unmatched_state = Path(td) / "seen-unmatched.json"
    store_unmatched = m.SeenStore(unmatched_state)
    store_unmatched.load()
    notifier_unmatched = m.DryRunNotifier()
    no_match_stub = m.ProjectStub(
        project_id="unmatched-1", title="Logo design",
        url="https://mostaql.com/project/unmatched-1-x",
        brief="graphic design only", listing_bids=0,
    )
    mon_unmatched = m.MostaqlMonitor(
        FakeScraper([[no_match_stub]]), notifier_unmatched, store_unmatched,
        notify_on_first_run=True, prime=False,
    )
    mon_unmatched._sleep = lambda: None
    status_unmatched = mon_unmatched.run(max_cycles=1)
    check("unmatched project is marked seen", "unmatched-1" in store_unmatched)
    check("unmatched project sends no alert", len(notifier_unmatched.sent) == 0,
          str(len(notifier_unmatched.sent)))
    check("unmatched cycle ends healthy", status_unmatched == 0,
          str(status_unmatched))

    # --- already-seen NEWEST-first ordering: oldest alerted first ---------
    store4 = m.SeenStore(Path(td) / "seen4.json")
    store4.load()
    scraper4, notifier4 = run_monitor(
        store4,
        [[make_stub("30", "NEWEST"), make_stub("20", "MIDDLE"),
          make_stub("10", "OLDEST")]],
        cycles=1, notify_on_first_run=True, prime=False,
    )
    order = [s for s in notifier4.sent]
    check("all three alerted", len(order) == 3, str(len(order)))
    check("oldest is first (chronological)",
          "OLDEST" in order[0] and "NEWEST" in order[2],
          f"first={order[0][:40]!r} last={order[2][:40]!r}")

    # --- empty DOM watchdog ----------------------------------------------
    store5 = m.SeenStore(Path(td) / "seen5.json")
    store5.load()
    scraper5, notifier5 = run_monitor(store5, [[]], cycles=6)
    admin_alerts = [s for s in notifier5.sent if "no projects parsed" in s]
    check("watchdog fires at threshold", len(admin_alerts) == 1,
          f"admin alerts={len(admin_alerts)}")
    check("watchdog reports 5 cycles", "5 consecutive" in admin_alerts[0])

    # --- a transient empty cycle does NOT trip the watchdog ---------------
    store6 = m.SeenStore(Path(td) / "seen6.json")
    store6.load()
    _, notifier6 = run_monitor(
        store6, [[make_stub("1")], [], [make_stub("1")]], cycles=3
    )
    check("transient empty does not alert",
          not [s for s in notifier6.sent if "no projects parsed" in s])

    # --- fetch failure is survivable --------------------------------------
    store7 = m.SeenStore(Path(td) / "seen7.json")
    store7.load()
    scraper7, notifier7 = run_monitor(
        store7,
        [m.FetchError("boom"), [make_stub("9")]],
        cycles=2, notify_on_first_run=True, prime=False,
    )
    check("survives a fetch error and continues", len(notifier7.sent) == 1,
          f"sent={len(notifier7.sent)}")

    # --- dry-run semantics: prime=False + persist=False -------------------
    store8 = m.SeenStore(Path(td) / "never.json")
    store8.load()
    _, notifier8 = run_monitor(
        store8, [[make_stub("1"), make_stub("2")]], cycles=1,
        notify_on_first_run=True, prime=False, persist=False,
    )
    check("dry-run alerts on everything", len(notifier8.sent) == 2,
          f"sent={len(notifier8.sent)}")
    check("dry-run does not write state",
          not (Path(td) / "never.json").exists())

    # --- --once semantics: one cycle, persist, exit without idling --------
    args = m.parse_args(["--once"])
    check("--once parsed by CLI", args.once is True)

    store9 = m.SeenStore(Path(td) / "once.json")
    store9.load()
    scraper9 = FakeScraper([[make_stub("77")]])
    notifier9 = m.DryRunNotifier()
    mon9 = m.MostaqlMonitor(
        scraper9, notifier9, store9, notify_on_first_run=True, prime=False
    )

    def fail_if_sleeping():
        raise AssertionError("--once slept after its final cycle")

    mon9._sleep = fail_if_sleeping
    try:
        mon9.run(max_cycles=1)
        slept = False
    except AssertionError:
        slept = True

    check("--once does not idle after final cycle", not slept)
    check("--once processes exactly one listing fetch",
          scraper9._cycle == 1, str(scraper9._cycle))
    check("--once sends alerts for new matches", len(notifier9.sent) == 1,
          f"sent={len(notifier9.sent)}")
    check("--once persists seen id", "77" in store9)
    check("--once writes seen_ids state", (Path(td) / "once.json").exists())

    # A failed fetch must also exit promptly instead of idling.
    store10 = m.SeenStore(Path(td) / "once-error.json")
    store10.load()
    scraper10 = FakeScraper([m.FetchError("boom")])
    mon10 = m.MostaqlMonitor(
        scraper10, m.DryRunNotifier(), store10,
        notify_on_first_run=True, prime=False,
    )
    mon10._sleep = fail_if_sleeping
    try:
        mon10.run(max_cycles=1)
        slept = False
    except AssertionError:
        slept = True
    check("--once exits after a failed fetch without idling", not slept)

    # --- --once end-to-end through main(), with network/Telegram stubbed ---
    from unittest.mock import patch

    cli_state = Path(td) / "cli-once.json"
    seed = m.SeenStore(cli_state)
    seed.add("old-project")
    seed.save()

    with patch.object(m, "load_config",
                      return_value=("token", "chat", None, False)), \
            patch.object(m, "TelegramNotifier") as notifier_cls, \
            patch.object(m, "MostaqlScraper") as scraper_cls, \
            patch.object(m, "STATE_PATH", cli_state):
        notifier_cls.return_value.verify.return_value = True
        notifier_cls.return_value.send.return_value = True
        scraper_cls.return_value.fetch_listing.return_value = [make_stub("cli-77")]
        scraper_cls.return_value.fetch_detail.return_value = m.ProjectDetail(
            hire_rate=60.0, bids=2, open_projects=1,
            budget_raw="$500 - $1000", budget_max=1000.0,
            duration="5 أيام", description="python backend api",
        )

        code = m.main(["--once"])

        check("main(--once) exits with code 0", code == 0, str(code))
        check("main(--once) fetches the listing once",
              scraper_cls.return_value.fetch_listing.call_count == 1,
              str(scraper_cls.return_value.fetch_listing.call_count))
        check("main(--once) sends exactly one alert",
              notifier_cls.return_value.send.call_count == 1,
              str(notifier_cls.return_value.send.call_count))

    persisted = m.SeenStore(cli_state)
    persisted.load()
    check("main(--once) persists seen_ids.json", "cli-77" in persisted)

    # --- --once health checks: failures must surface as exit code 1 -------
    for label, failure in (
        ("fetch failure", m.FetchError("boom")),
        ("Cloudflare block", m.CloudflareChallenge("blocked")),
    ):
        failure_state = Path(td) / f"cli-{label.replace(' ', '-')}.json"
        with patch.object(m, "load_config",
                          return_value=("token", "chat", None, False)), \
                patch.object(m, "TelegramNotifier") as notifier_cls, \
                patch.object(m, "MostaqlScraper") as scraper_cls, \
                patch.object(m, "STATE_PATH", failure_state):
            notifier_cls.return_value.verify.return_value = True
            scraper_cls.return_value.fetch_listing.side_effect = failure
            code = m.main(["--once"])
            check(f"main(--once) exits 1 on {label}", code == 1, str(code))

    empty_state = Path(td) / "cli-empty-dom.json"
    with patch.object(m, "load_config",
                      return_value=("token", "chat", None, False)), \
            patch.object(m, "TelegramNotifier") as notifier_cls, \
            patch.object(m, "MostaqlScraper") as scraper_cls, \
            patch.object(m, "STATE_PATH", empty_state):
        notifier_cls.return_value.verify.return_value = True
        scraper_cls.return_value.fetch_listing.return_value = []
        code = m.main(["--once"])
        check("main(--once) exits 1 on empty DOM", code == 1, str(code))

    send_fail_state = Path(td) / "cli-send-failure.json"
    send_fail_seed = m.SeenStore(send_fail_state)
    send_fail_seed.add("old-project")
    send_fail_seed.save()

    with patch.object(m, "load_config",
                      return_value=("token", "chat", None, False)), \
            patch.object(m, "TelegramNotifier") as notifier_cls, \
            patch.object(m, "MostaqlScraper") as scraper_cls, \
            patch.object(m, "STATE_PATH", send_fail_state):
        notifier_cls.return_value.verify.return_value = True
        notifier_cls.return_value.send.return_value = False
        scraper_cls.return_value.fetch_listing.return_value = [
            make_stub("send-fail-1")
        ]
        scraper_cls.return_value.fetch_detail.return_value = m.ProjectDetail(
            hire_rate=60.0, bids=2, open_projects=1,
            budget_raw="$500 - $1000", budget_max=1000.0,
            duration="5 أيام", description="python backend api",
        )
        code = m.main(["--once"])
        check("main(--once) exits 1 on delivery failure", code == 1, str(code))

    send_fail_persisted = m.SeenStore(send_fail_state)
    send_fail_persisted.load()
    check("delivery failure is not persisted", "send-fail-1" not in send_fail_persisted)


# ---------------------------------------------------------------------------
section("Results")
print(f"\n  passed: {PASSED}")
print(f"  failed: {FAILED}")
sys.exit(1 if FAILED else 0)
