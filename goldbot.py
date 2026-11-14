#!/usr/bin/env python3
"""WARDOGS gold-bar price watcher that alerts on Telegram when the price is below a threshold.

Data source (primary): https://metaforge.app/api/wardogs/market
  Public JSON: {"updatedAt": <ms>, "points": [{"t": <ms>, "price": <int>}], "stats": {...}}
  This is the live feed WARDOGS Hub (wardogshub.gg/gold-market) pulls its daily rate from.

Fallback source: https://wardogshub.gg/gold-market/
  The daily series is server-rendered into <script type="application/json" data-gold-series>.

The in-game rate is one fixed price per UTC day, so at most one alert is sent per day.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

DEFAULT_THRESHOLD = 400_000
DEFAULT_POLL_SECONDS = 900
METAFORGE_API = "https://metaforge.app/api/wardogs/market"
WARDOGSHUB_PAGE = "https://wardogshub.gg/gold-market/"
MARKET_PAGE = "https://wardogshub.gg/gold-market/"
USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/157.0.0.0 Safari/537.36 GoldBot/1.0"
)

BASE_DIR = Path(__file__).resolve().parent


# --------------------------------------------------------------------------- #
# Configuration
# --------------------------------------------------------------------------- #
def load_dotenv(path: Path) -> None:
    """Minimal .env loader. Real environment variables always win."""
    if not path.is_file():
        return
    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        os.environ.setdefault(key.strip(), value.strip().strip("'\""))


def env_int(name: str, default: int) -> int:
    try:
        return int(os.environ.get(name, default))
    except (TypeError, ValueError):
        return default


# --------------------------------------------------------------------------- #
# HTTP
# --------------------------------------------------------------------------- #
def http_get(url: str, *, timeout: float = 20.0, retries: int = 3) -> bytes:
    headers = {"User-Agent": USER_AGENT, "Accept": "*/*"}
    last_error: Exception | None = None
    for attempt in range(1, retries + 1):
        try:
            request = urllib.request.Request(url, headers=headers)
            with urllib.request.urlopen(request, timeout=timeout) as response:
                if response.status != 200:
                    raise RuntimeError(f"HTTP {response.status}")
                return response.read()
        except Exception as error:  # noqa: BLE001 - retried and re-raised below
            last_error = error
            if attempt < retries:
                time.sleep(2 ** attempt)
    raise RuntimeError(f"GET {url} failed after {retries} attempts: {last_error}")


# --------------------------------------------------------------------------- #
# Rate sources
# --------------------------------------------------------------------------- #
def _point_date(milliseconds: int) -> str:
    return datetime.fromtimestamp(milliseconds / 1000, timezone.utc).strftime("%Y-%m-%d")


def fetch_metaforge() -> dict:
    payload = json.loads(http_get(METAFORGE_API).decode("utf-8"))
    points = sorted(payload.get("points") or [], key=lambda point: point["t"])
    if not points:
        raise RuntimeError("MetaForge response contained no price points")

    latest = points[-1]
    previous = points[-2] if len(points) > 1 else None
    return {
        "source": "metaforge.app",
        "date": _point_date(latest["t"]),
        "price": int(latest["price"]),
        "prev_date": _point_date(previous["t"]) if previous else None,
        "prev_price": int(previous["price"]) if previous else None,
        "updated_at": payload.get("updatedAt"),
        "stats": payload.get("stats") or {},
        "series": [
            {"date": _point_date(point["t"]), "price": int(point["price"])}
            for point in points
        ],
    }


def fetch_wardogshub() -> dict:
    html = http_get(WARDOGSHUB_PAGE).decode("utf-8", errors="replace")
    match = re.search(
        r'<script type="application/json" data-gold-series>(.*?)</script>',
        html,
        re.DOTALL,
    )
    if not match:
        raise RuntimeError("wardogshub.gg page did not contain the gold series block")

    series = json.loads(match.group(1))
    if not series:
        raise RuntimeError("wardogshub.gg gold series was empty")

    latest = series[-1]
    previous = series[-2] if len(series) > 1 else None
    return {
        "source": "wardogshub.gg",
        "date": latest["date"],
        "price": int(latest["rate"]),
        "prev_date": previous["date"] if previous else None,
        "prev_price": int(previous["rate"]) if previous else None,
        "updated_at": None,
        "stats": {},
        "series": [
            {"date": point["date"], "price": int(point["rate"])} for point in series
        ],
    }


def fetch_rate(preferred: str = "auto") -> dict:
    fetchers = {
        "metaforge": fetch_metaforge,
        "wardogshub": fetch_wardogshub,
    }
    if preferred in fetchers:
        return fetchers[preferred]()

    errors = []
    for name in ("metaforge", "wardogshub"):
        try:
            return fetchers[name]()
        except Exception as error:  # noqa: BLE001 - try the next source
            errors.append(f"{name}: {error}")
    raise RuntimeError("all gold-rate sources failed -> " + " | ".join(errors))


# --------------------------------------------------------------------------- #
# State
# --------------------------------------------------------------------------- #
def load_state(path: Path) -> dict:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}


def save_state(path: Path, state: dict) -> None:
    temporary = path.with_name(path.name + ".tmp")
    temporary.write_text(json.dumps(state, indent=2), encoding="utf-8")
    os.replace(temporary, path)


# --------------------------------------------------------------------------- #
# Telegram / formatting
# --------------------------------------------------------------------------- #
def money(value: int) -> str:
    return f"${value:,.0f}"


def change_line(rate: dict) -> str | None:
    previous = rate.get("prev_price")
    if previous is None:
        return None
    delta = rate["price"] - previous
    direction = "+" if delta >= 0 else "-"
    percent = (delta / previous * 100) if previous else 0.0
    return f"{direction}{money(abs(delta))} ({direction}{abs(percent):.1f}%) vs {rate['prev_date']}"


def build_message(rate: dict, threshold: int, stale_days: int) -> str:
    lines = [
        "🥇 <b>WARDOGS gold below target</b>",
        "",
        f"Price: <b>{money(rate['price'])}</b> per bar",
        f"Day: <b>{rate['date']}</b> (UTC, rate holds all day)",
        f"Your threshold: {money(threshold)}",
    ]
    change = change_line(rate)
    if change:
        lines.append(f"Move: {change}")
    if stale_days > 1:
        lines.append(f"⚠️ Source is {stale_days} days old")
    lines.extend(["", f"Source: {rate['source']}", MARKET_PAGE])
    return "\n".join(lines)


HELP_TEXT = (
    "<b>GoldBot commands</b>\n"
    "/price — current gold bar price\n"
    "/history [days] — recent daily rates (default 7, max 30)\n"
    "/status — watch settings and last check\n"
    "/help — this list"
)


def price_card(rate: dict, threshold: int) -> str:
    lines = [
        "🥇 <b>WARDOGS gold price</b>",
        f"<b>{money(rate['price'])}</b> per bar",
        f"Day: <b>{rate['date']}</b> (UTC, fixed until midnight)",
    ]
    change = change_line(rate)
    if change:
        lines.append(f"Move: {change}")
    if rate["price"] < threshold:
        lines.append(f"✅ Below your {money(threshold)} target")
    else:
        over = rate["price"] - threshold
        lines.append(f"Above your {money(threshold)} target by {money(over)}")
    lines.extend(["", f"Source: {rate['source']}", MARKET_PAGE])
    return "\n".join(lines)


def history_text(rate: dict, days: int) -> str:
    series = rate.get("series") or []
    tail = series[-days:]
    lines = [f"<b>Last {len(tail)} days</b>"]
    for point in reversed(tail):
        lines.append(f"{point['date']}  {money(point['price'])}")
    return "\n".join(lines)


def status_text(rate: dict, threshold: int, poll: int, source: str, state: dict) -> str:
    below = rate["price"] < threshold
    return "\n".join(
        [
            "<b>GoldBot status</b>",
            f"Threshold: {money(threshold)}",
            f"Now: {money(rate['price'])} on {rate['date']} "
            f"({'BELOW' if below else 'above'})",
            f"Source: {rate['source']} (preference: {source})",
            f"Poll: every {poll}s",
            f"Last alert: {state.get('last_alert_key') or 'none'}",
            f"Last check: {state.get('last_checked') or 'not yet'}",
        ]
    )


def reply_for_command(
    command: str,
    arguments: list[str],
    *,
    rate: dict,
    threshold: int,
    poll: int,
    source: str,
    state: dict,
) -> str:
    if command in ("/start", "/help"):
        return HELP_TEXT
    if command in ("/price", "/gold"):
        return price_card(rate, threshold)
    if command == "/history":
        days = 7
        if arguments:
            try:
                days = max(1, min(30, int(arguments[0])))
            except ValueError:
                pass
        return history_text(rate, days)
    if command == "/status":
        return status_text(rate, threshold, poll, source, state)
    return "Unknown command. Send /help for the list."


def send_telegram(token: str, chat_id: str, text: str) -> None:
    url = f"https://api.telegram.org/bot{token}/sendMessage"
    body = urllib.parse.urlencode(
        {
            "chat_id": chat_id,
            "text": text,
            "parse_mode": "HTML",
            "disable_web_page_preview": "true",
        }
    ).encode("utf-8")
    request = urllib.request.Request(url, data=body, headers={"User-Agent": USER_AGENT})
    with urllib.request.urlopen(request, timeout=25) as response:
        result = json.loads(response.read().decode("utf-8"))
    if not result.get("ok"):
        raise RuntimeError(f"Telegram rejected the message: {result}")


def list_chat_ids(token: str) -> dict[str, str]:
    """Return {chat_id: label} for every chat that has messaged the bot."""
    payload = json.loads(
        http_get(f"https://api.telegram.org/bot{token}/getUpdates").decode("utf-8")
    )
    if not payload.get("ok"):
        raise RuntimeError(f"Telegram rejected getUpdates: {payload}")

    chats: dict[str, str] = {}
    for update in payload.get("result", []):
        message = (
            update.get("message")
            or update.get("edited_message")
            or update.get("channel_post")
            or update.get("edited_channel_post")
            or {}
        )
        chat = message.get("chat") or {}
        if chat.get("id") is None:
            continue
        label = (
            chat.get("username")
            or chat.get("title")
            or chat.get("first_name")
            or chat.get("type")
            or ""
        )
        chats[str(chat["id"])] = str(label)
    return chats


def telegram_updates(token: str, offset: int | None) -> list[dict]:
    query = urllib.parse.urlencode(
        {"timeout": 0, "allowed_updates": json.dumps(["message"])}
    )
    url = f"https://api.telegram.org/bot{token}/getUpdates?{query}"
    if offset is not None:
        url += f"&offset={offset}"
    payload = json.loads(http_get(url, timeout=15, retries=1).decode("utf-8"))
    if not payload.get("ok"):
        raise RuntimeError(f"Telegram rejected getUpdates: {payload}")
    return payload.get("result") or []


def handle_commands(
    *,
    token: str,
    chat_id: str,
    threshold: int,
    poll: int,
    source: str,
    state_path: Path,
    dry_run: bool,
) -> None:
    """Answer /price, /history, /status and /help on Telegram."""
    if dry_run or not token or not chat_id:
        return

    state = load_state(state_path)
    offset = state.get("update_offset")
    updates = telegram_updates(token, offset)
    if not updates:
        return

    rate: dict | None = None
    for update in updates:
        offset = max(offset or 0, int(update["update_id"]) + 1)
        message = update.get("message") or {}
        chat = message.get("chat") or {}
        if str(chat.get("id")) != str(chat_id):
            continue
        text = (message.get("text") or "").strip()
        if not text.startswith("/"):
            continue

        parts = text.split()
        command = parts[0].split("@", 1)[0].lower()
        if rate is None:
            rate = fetch_rate(source)
        answer = reply_for_command(
            command,
            parts[1:],
            rate=rate,
            threshold=threshold,
            poll=poll,
            source=source,
            state=state,
        )
        send_telegram(token, chat_id, answer)

    state["update_offset"] = offset
    save_state(state_path, state)


# --------------------------------------------------------------------------- #
# Core check
# --------------------------------------------------------------------------- #
def check_once(
    *,
    threshold: int,
    state_path: Path,
    preferred_source: str,
    dry_run: bool,
    token: str,
    chat_id: str,
    state_minimal: bool = False,
) -> bool:
    rate = fetch_rate(preferred_source)
    today = datetime.now(timezone.utc).date()
    rate_day = datetime.strptime(rate["date"], "%Y-%m-%d").date()
    stale_days = (today - rate_day).days

    state = load_state(state_path)
    alert_key = f"{rate['date']}:{rate['price']}"
    below = rate["price"] < threshold
    should_alert = below and state.get("last_alert_key") != alert_key

    print(
        f"[{datetime.now(timezone.utc).isoformat(timespec='seconds')}] "
        f"{money(rate['price'])} on {rate['date']} via {rate['source']} "
        f"| threshold {money(threshold)} | below={below} | stale={stale_days}d",
        flush=True,
    )

    if should_alert:
        message = build_message(rate, threshold, stale_days)
        if dry_run:
            print("--- ALERT (dry run) ---")
            print(message)
            print("-----------------------")
        else:
            if not token or not chat_id:
                raise RuntimeError(
                    "TELEGRAM_BOT_TOKEN and TELEGRAM_CHAT_ID must be set to send alerts"
                )
            send_telegram(token, chat_id, message)
            print("Alert sent to Telegram.", flush=True)
        state["last_alert_key"] = alert_key

    state.update(
        {
            "last_date": rate["date"],
            "last_price": rate["price"],
            "last_source": rate["source"],
            "last_checked": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        }
    )
    if state_minimal:
        # CI keeps only what must survive between runs, so the file changes
        # only when an alert fires or a command is answered.
        state = {
            key: state[key]
            for key in ("last_alert_key", "update_offset")
            if key in state
        }
        if not state:
            return should_alert
    save_state(state_path, state)
    return should_alert


# --------------------------------------------------------------------------- #
# Entry point
# --------------------------------------------------------------------------- #
def main() -> int:
    # Windows consoles default to cp1252; the alert text contains emoji.
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")
        except (AttributeError, ValueError):
            pass

    load_dotenv(BASE_DIR / ".env")

    parser = argparse.ArgumentParser(
        description="Alert on Telegram when the WARDOGS gold-bar price is below a threshold."
    )
    parser.add_argument("--once", action="store_true", help="run a single check and exit")
    parser.add_argument("--test", action="store_true", help="send a Telegram test message and exit")
    parser.add_argument(
        "--chat-id",
        action="store_true",
        help="show chat ids of messages sent to your bot (message the bot first)",
    )
    parser.add_argument(
        "--price", action="store_true", help="print the current gold price and exit"
    )
    parser.add_argument(
        "--state-minimal",
        action="store_true",
        help="persist only the alert key and update offset (for scheduled CI runs)",
    )
    parser.add_argument("--dry-run", action="store_true", help="print alerts instead of sending them")
    parser.add_argument(
        "--threshold",
        type=int,
        default=env_int("GOLD_THRESHOLD", DEFAULT_THRESHOLD),
        help=f"alert when one bar costs less than this (default {DEFAULT_THRESHOLD})",
    )
    parser.add_argument(
        "--poll",
        type=int,
        default=env_int("POLL_SECONDS", DEFAULT_POLL_SECONDS),
        help=f"seconds between checks when looping (default {DEFAULT_POLL_SECONDS})",
    )
    parser.add_argument(
        "--source",
        choices=("auto", "metaforge", "wardogshub"),
        default=os.environ.get("GOLD_SOURCE", "auto"),
        help="price source; auto tries MetaForge then the wardogshub.gg page",
    )
    parser.add_argument(
        "--state-file",
        type=Path,
        default=Path(os.environ.get("STATE_FILE", BASE_DIR / "goldbot_state.json")),
        help="where the last alerted price is remembered",
    )
    args = parser.parse_args()

    token = os.environ.get("TELEGRAM_BOT_TOKEN", "").strip()
    chat_id = os.environ.get("TELEGRAM_CHAT_ID", "").strip()

    if args.price:
        try:
            rate = fetch_rate(args.source)
        except Exception as error:  # noqa: BLE001 - CLI should report, not traceback
            print(f"ERROR: {error}", file=sys.stderr)
            return 1
        print(f"{money(rate['price'])} on {rate['date']} (UTC) via {rate['source']}")
        change = change_line(rate)
        if change:
            print(f"Move: {change}")
        if rate["price"] < args.threshold:
            print(f"Below your {money(args.threshold)} target")
        else:
            print(f"Above your {money(args.threshold)} target")
        return 0

    if args.chat_id:
        if not token:
            print("Add TELEGRAM_BOT_TOKEN to .env first, then run this again.", file=sys.stderr)
            return 2
        try:
            chats = list_chat_ids(token)
        except Exception as error:  # noqa: BLE001 - tell the user what failed
            print(f"ERROR: {error}", file=sys.stderr)
            return 1
        if not chats:
            print(
                "No messages yet.\n"
                "1. Open Telegram and find your bot.\n"
                "2. Press START (or send it 'hi').\n"
                "3. Run this command again."
            )
            return 0
        print("Found chat(s):")
        for found_id, label in chats.items():
            print(f"  {found_id}  {label}")
        print("\nPut the number in .env as TELEGRAM_CHAT_ID=<number>")
        return 0

    if args.test:
        if args.dry_run:
            print("Test message (dry run): GoldBot is configured correctly.")
            return 0
        if not token or not chat_id:
            print("Set TELEGRAM_BOT_TOKEN and TELEGRAM_CHAT_ID first.", file=sys.stderr)
            return 2
        send_telegram(
            token,
            chat_id,
            "✅ <b>GoldBot test</b>\nYour WARDOGS gold alerts are wired up.",
        )
        print("Test message sent.")
        return 0

    if args.once:
        try:
            check_once(
                threshold=args.threshold,
                state_path=args.state_file,
                preferred_source=args.source,
                dry_run=args.dry_run,
                token=token,
                chat_id=chat_id,
                state_minimal=args.state_minimal,
            )
            handle_commands(
                token=token,
                chat_id=chat_id,
                threshold=args.threshold,
                poll=args.poll,
                source=args.source,
                state_path=args.state_file,
                dry_run=args.dry_run,
            )
        except Exception as error:  # noqa: BLE001 - single-shot CLI should report, not traceback
            print(f"ERROR: {error}", file=sys.stderr)
            return 1
        return 0

    print(
        f"GoldBot running: threshold {money(args.threshold)}, "
        f"every {args.poll}s, source={args.source}. "
        f"Commands: /price /history /status /help. Ctrl+C to stop.",
        flush=True,
    )
    next_check = 0.0
    try:
        while True:
            if time.monotonic() >= next_check:
                try:
                    check_once(
                        threshold=args.threshold,
                        state_path=args.state_file,
                        preferred_source=args.source,
                        dry_run=args.dry_run,
                        token=token,
                        chat_id=chat_id,
                    )
                except Exception as error:  # noqa: BLE001 - keep watching after transient failures
                    print(
                        f"[{datetime.now(timezone.utc).isoformat(timespec='seconds')}] "
                        f"ERROR: {error}",
                        flush=True,
                    )
                next_check = time.monotonic() + max(30, args.poll)

            try:
                handle_commands(
                    token=token,
                    chat_id=chat_id,
                    threshold=args.threshold,
                    poll=args.poll,
                    source=args.source,
                    state_path=args.state_file,
                    dry_run=args.dry_run,
                )
            except Exception as error:  # noqa: BLE001 - a bad command must not stop alerts
                print(
                    f"[{datetime.now(timezone.utc).isoformat(timespec='seconds')}] "
                    f"command check failed: {error}",
                    flush=True,
                )
            time.sleep(5)
    except KeyboardInterrupt:
        print("\nStopped.")
        return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except KeyboardInterrupt:
        print("\nStopped.")
        raise SystemExit(0)
