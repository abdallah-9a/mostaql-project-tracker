# Mostaql Project Monitor → Telegram Notifier

Polls Mostaql's development feed and sends Telegram alerts for backend/API projects.

## Features
- Atomic `seen_ids.json` deduplication with a 1,000-ID cap and no-op writes.
- Polls the newest 25 listings every 75–90 seconds; typical latency under 90 s.
- English word-boundary and normalized Arabic keyword matching.
- 🟢/🟡/🔴 client-quality verdict from hire rate, budget, and competition.
- Validated MarkdownV2 alerts with retries, rate limits, and plain-text fallback.
- Browser-profile WAF retries, Cloudflare detection, empty-DOM watchdog, clean shutdown.

## Setup & usage
```bash
pip install -r requirements.txt
cp .env.example .env  # BOT_TOKEN + CHAT_ID required; ADMIN_CHAT_ID optional
python mostaql_notifier.py --test-telegram
python mostaql_notifier.py                   # poll forever
python mostaql_notifier.py --once            # one cycle (cron/CI)
python mostaql_notifier.py --dry-run --once  # preview, no writes
python test_offline.py                       # 274 offline assertions
```

GitHub Actions runs every 15 minutes; add the same secrets under
**Settings → Secrets and variables → Actions**.
