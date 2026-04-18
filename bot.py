#!/usr/bin/env python3
"""
Telegram bot that monitors rdv.anct.gouv.fr for available RDV slots.

Detection logic:
  - "aucun créneau" in page text  → no slots
  - phrase absent on HTTP 200     → slots available
  - HTTP 429 / 403 / CAPTCHA      → blocked/rate-limited → notify + back off
"""

import asyncio
import logging
import os
import random
import sys
from datetime import datetime
from typing import Optional

import requests
from bs4 import BeautifulSoup
from dotenv import load_dotenv
from telegram import Update
from telegram.constants import ParseMode
from telegram.ext import Application, CommandHandler, ContextTypes

load_dotenv(dotenv_path=".env", override=False)

# ── Configuration ─────────────────────────────────────────────────────────────
BOT_TOKEN  = os.getenv("TELEGRAM_BOT_TOKEN", "")
CHAT_ID    = os.getenv("TELEGRAM_CHAT_ID", "")
NTFY_TOPIC = os.getenv("NTFY_TOPIC", "")  

RDV_URL = (
    "https://rdv.anct.gouv.fr/prendre_rdv"
    "?departement="
    "&motif_name_with_location_type=renouvellement_de_recepisses_arrives_a_echeance_-public_office"
    "&public_link_organisation_id=2458"
)

CHECK_INTERVAL  = 0.1  # seconds between checks
REQUEST_TIMEOUT = 15   # seconds for each HTTP request
BACKOFF_AFTER_BLOCK = 300  # 5 min pause after getting blocked

# ── Logging ───────────────────────────────────────────────────────────────────
logging.basicConfig(
    format="%(asctime)s [%(levelname)s] %(message)s",
    level=logging.INFO,
    handlers=[
        logging.StreamHandler(sys.stdout),
        logging.FileHandler("bot.log", encoding="utf-8"),
    ],
)
logger = logging.getLogger(__name__)

# ── Rotating user-agent pool ──────────────────────────────────────────────────
USER_AGENTS = [
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
    "Mozilla/5.0 (X11; Linux x86_64; rv:125.0) Gecko/20100101 Firefox/125.0",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:125.0) Gecko/20100101 Firefox/125.0",
    "Mozilla/5.0 (iPhone; CPU iPhone OS 17_0 like Mac OS X) AppleWebKit/605.1.15 "
    "(KHTML, like Gecko) Version/17.0 Mobile/15E148 Safari/604.1",
]

# ── Shared state ──────────────────────────────────────────────────────────────
state: dict = {
    "monitoring":      False,
    "slots_available": None,   # None = unknown, True = yes, False = no
    "blocked":         False,
    "check_count":     0,
    "last_check":      None,   # datetime
    "extra_wait":      0,      # extra seconds to wait before next check
    "error_streak":    0,      # consecutive errors without a clean check
}


# ── Website checker ───────────────────────────────────────────────────────────

def check_slots() -> dict:
    """
    Fetch the RDV page and return:
      {
        "status":    "available" | "unavailable" | "blocked" | "rate_limited" | "captcha" | "error",
        "detail":    str,
        "http_code": int | None,
      }
    """
    headers = {
        "User-Agent":                random.choice(USER_AGENTS),
        "Accept":                    "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
        "Accept-Language":           "fr-FR,fr;q=0.9,en;q=0.8",
        "Accept-Encoding":           "gzip, deflate, br",
        "Connection":                "keep-alive",
        "Upgrade-Insecure-Requests": "1",
        "Cache-Control":             "no-cache",
    }

    try:
        resp = requests.get(RDV_URL, headers=headers, timeout=REQUEST_TIMEOUT)
    except requests.exceptions.Timeout:
        return {"status": "error", "detail": "Request timed out after 15s", "http_code": None}
    except requests.exceptions.ConnectionError as exc:
        return {"status": "error", "detail": f"Connection error: {exc}", "http_code": None}
    except requests.exceptions.RequestException as exc:
        return {"status": "error", "detail": f"Request failed: {exc}", "http_code": None}

    code = resp.status_code

    if code == 429:
        retry_after = resp.headers.get("Retry-After", "unknown")
        return {
            "status":    "rate_limited",
            "detail":    f"HTTP 429 Too Many Requests – Retry-After: {retry_after}s",
            "http_code": 429,
        }

    if code == 403:
        return {
            "status":    "blocked",
            "detail":    "HTTP 403 Forbidden – the server is refusing requests (IP blocked?)",
            "http_code": 403,
        }

    if code == 503:
        return {
            "status":    "error",
            "detail":    "HTTP 503 Service Unavailable – site may be down",
            "http_code": 503,
        }

    if code != 200:
        return {
            "status":    "error",
            "detail":    f"Unexpected HTTP {code}",
            "http_code": code,
        }

    # ── Parse HTML ────────────────────────────────────────────────────────────
    soup = BeautifulSoup(resp.text, "html.parser")
    page_text = soup.get_text(separator=" ", strip=True).lower()

    # CAPTCHA detection
    captcha_signals = ["captcha", "i'm not a robot", "je ne suis pas un robot", "cloudflare"]
    if any(sig in page_text for sig in captcha_signals):
        return {
            "status":    "captcha",
            "detail":    "CAPTCHA / anti-bot challenge detected",
            "http_code": 200,
        }

    # "No slots" detection – the phrase shown when nothing is available
    no_slot_phrases = [
        "aucun créneau",
        "aucun créneaux",
        "aucun slot",
        "no slot",
        "aucune disponibilité",
    ]
    if any(phrase in page_text for phrase in no_slot_phrases):
        return {"status": "unavailable", "detail": "No slots found on the page", "http_code": 200}

    # If the page loaded cleanly and the "no slots" message is absent → slots likely available
    # Look for additional positive indicators to increase confidence
    positive_indicators = []

    calendar_el = soup.find(
        attrs={"class": lambda c: c and any(
            kw in c.lower() for kw in ("calendar", "slot", "créneau", "booking", "rdv-slot", "disponib")
        ) if c else False}
    )
    if calendar_el:
        positive_indicators.append("calendar/slot widget found")

    # Look for <button> or <a> elements suggesting time selection
    booking_buttons = soup.find_all(
        lambda tag: tag.name in ("button", "a") and any(
            kw in (tag.get_text(strip=True) + " " + " ".join(tag.get("class", []))).lower()
            for kw in ("réserver", "choisir", "disponible", "book", "select", "créneau")
        )
    )
    if booking_buttons:
        positive_indicators.append(f"{len(booking_buttons)} booking button(s) found")

    detail = "Slots appear AVAILABLE"
    if positive_indicators:
        detail += " – " + ", ".join(positive_indicators)

    return {"status": "available", "detail": detail, "http_code": 200}


# ── Telegram helpers ──────────────────────────────────────────────────────────

async def send_notification(app: Application, text: str) -> None:
    """Send a message to the configured CHAT_ID."""
    if not CHAT_ID:
        logger.warning("TELEGRAM_CHAT_ID not set – cannot send notification")
        return
    try:
        await app.bot.send_message(chat_id=CHAT_ID, text=text, parse_mode=ParseMode.HTML)
    except Exception as exc:
        logger.error(f"Failed to send Telegram message: {exc}")


async def send_alarm(app: Application, text: str) -> None:
    """Spam 5 loud notifications so the user can't miss it."""
    for i in range(5):
        try:
            await app.bot.send_message(
                chat_id=CHAT_ID,
                text=("🚨🚨🚨 " if i > 0 else "") + text,
                parse_mode=ParseMode.HTML,
                disable_notification=False,
            )
        except Exception as exc:
            logger.error(f"Failed to send alarm message: {exc}")
        await asyncio.sleep(0.3)


def send_ntfy_alarm() -> None:
    """Send a max-priority push notification via ntfy.sh."""
    if not NTFY_TOPIC:
        return
    try:
        requests.post(
            f"https://ntfy.sh/{NTFY_TOPIC}",
            headers={
                "Title":    "RDV DISPONIBLE !!!",
                "Priority": "urgent",
                "Tags":     "rotating_light,calendar",
            },
            data="Un créneau est disponible ! Réservez maintenant.".encode("utf-8"),
            timeout=10,
        )
        logger.info("ntfy alarm sent")
    except Exception as exc:
        logger.error(f"Failed to send ntfy notification: {exc}")


# ── Monitoring loop ───────────────────────────────────────────────────────────

async def monitor_loop(app: Application) -> None:
    """Background coroutine: checks the site every CHECK_INTERVAL seconds."""
    logger.info("Monitoring loop started")
    await send_notification(
        app,
        f"🔍 <b>Monitoring started</b>\nChecking every {CHECK_INTERVAL}s for available RDV slots."
    )

    while state["monitoring"]:
        # Extra back-off after a block
        if state["extra_wait"] > 0:
            wait = state["extra_wait"]
            state["extra_wait"] = 0
            logger.info(f"Back-off: waiting {wait}s before next check")
            # Sleep in small chunks so we can respond to /stop quickly
            for _ in range(wait):
                if not state["monitoring"]:
                    break
                await asyncio.sleep(1)
            if not state["monitoring"]:
                break

        result      = check_slots()
        status      = result["status"]
        detail      = result["detail"]
        prev_slots  = state["slots_available"]
        prev_blocked = state["blocked"]

        state["check_count"] += 1
        state["last_check"]   = datetime.now()
        logger.info(f"Check #{state['check_count']}: [{status}] {detail}")

        # ── Blocked / rate-limited / CAPTCHA ─────────────────────────────────
        if status in ("blocked", "rate_limited", "captcha"):
            state["error_streak"] += 1
            if not state["blocked"]:
                state["blocked"] = True
                await send_notification(
                    app,
                    f"⛔ <b>Monitoring blocked!</b>\n"
                    f"Reason: {detail}\n\n"
                    f"Pausing for {BACKOFF_AFTER_BLOCK // 60} minutes before retrying…"
                )
            state["extra_wait"] = BACKOFF_AFTER_BLOCK
            await asyncio.sleep(CHECK_INTERVAL)
            continue

        # ── Recovered from block ──────────────────────────────────────────────
        if prev_blocked and status not in ("blocked", "rate_limited", "captcha"):
            state["blocked"]      = False
            state["error_streak"] = 0
            await send_notification(app, "✅ <b>Monitoring resumed</b> – requests are working again.")

        # ── Generic error ─────────────────────────────────────────────────────
        if status == "error":
            state["error_streak"] += 1
            # Only notify on first error in a streak to avoid spamming
            if state["error_streak"] == 1:
                await send_notification(
                    app,
                    f"⚠️ <b>Check failed</b>\n{detail}\nWill retry in {CHECK_INTERVAL}s."
                )
            state["slots_available"] = None
            await asyncio.sleep(CHECK_INTERVAL)
            continue

        # Clean check – reset error streak
        state["error_streak"] = 0

        # ── Slots available ───────────────────────────────────────────────────
        if status == "available":
            if prev_slots is not True:   # new transition → available
                state["slots_available"] = True
                send_ntfy_alarm()
                await send_alarm(
                    app,
                    f"🎉 <b>RDV SLOTS ARE AVAILABLE!</b>\n"
                    f"{detail}\n\n"
                    f"👉 <a href=\"{RDV_URL}\">Book your appointment NOW</a>"
                )
            # else: was already available, no need to spam

        # ── No slots ─────────────────────────────────────────────────────────
        elif status == "unavailable":
            if prev_slots is True:       # transition: was available → now gone
                await send_notification(
                    app,
                    "ℹ️ <b>Slots are gone again.</b>\nContinuing to monitor every minute…"
                )
            state["slots_available"] = False

        await asyncio.sleep(CHECK_INTERVAL)

    logger.info("Monitoring loop stopped")


# ── Command handlers ──────────────────────────────────────────────────────────

async def cmd_start(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    chat_id = update.effective_chat.id
    await update.message.reply_html(
        f"👋 <b>RDV Monitor Bot</b>\n\n"
        f"Your chat ID is: <code>{chat_id}</code>\n"
        f"Add <code>TELEGRAM_CHAT_ID={chat_id}</code> to your <code>.env</code> file.\n\n"
        f"<b>Commands:</b>\n"
        f"/monitor – start monitoring\n"
        f"/stop    – stop monitoring\n"
        f"/check   – run a single check right now\n"
        f"/status  – show current status\n"
        f"/test    – fire a test alarm"
    )


async def cmd_monitor(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    if state["monitoring"]:
        await update.message.reply_text("Already monitoring! Use /status to see the current state.")
        return
    state["monitoring"]      = True
    state["blocked"]         = False
    state["extra_wait"]      = 0
    state["error_streak"]    = 0
    state["slots_available"] = None
    ctx.application.create_task(monitor_loop(ctx.application))


async def cmd_stop(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    if not state["monitoring"]:
        await update.message.reply_text("Not currently monitoring.")
        return
    state["monitoring"] = False
    await update.message.reply_text("🛑 Monitoring stopped.")


async def cmd_check(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    """Run an immediate one-off check and report the result."""
    await update.message.reply_text("🔄 Checking right now…")
    result = check_slots()
    status = result["status"]
    detail = result["detail"]
    code   = result["http_code"]

    emoji = {
        "available":    "✅",
        "unavailable":  "❌",
        "blocked":      "⛔",
        "rate_limited": "🚫",
        "captcha":      "🤖",
        "error":        "⚠️",
    }.get(status, "❓")

    msg = (
        f"{emoji} <b>{status.upper()}</b>\n"
        f"{detail}\n"
        f"HTTP: {code if code else 'N/A'}"
    )
    if status == "available":
        msg += f'\n\n👉 <a href="{RDV_URL}">Book your appointment NOW</a>'

    await update.message.reply_html(msg)


async def cmd_test(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    """Fire the alarm as if a slot just appeared — for testing."""
    await update.message.reply_text("🔔 Firing test alarm…")
    send_ntfy_alarm()
    await send_alarm(
        ctx.application,
        f"🎉 <b>[TEST] RDV SLOTS ARE AVAILABLE!</b>\n"
        f"This is a test notification.\n\n"
        f"👉 <a href=\"{RDV_URL}\">Book your appointment NOW</a>"
    )


async def cmd_status(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    s = state
    slot_icon = {True: "✅ Available", False: "❌ Unavailable", None: "❓ Unknown"}[s["slots_available"]]
    last = s["last_check"].strftime("%d/%m %H:%M:%S") if s["last_check"] else "never"
    await update.message.reply_html(
        f"📊 <b>Bot Status</b>\n\n"
        f"Monitoring : {'🟢 ON' if s['monitoring'] else '🔴 OFF'}\n"
        f"Slots      : {slot_icon}\n"
        f"Blocked    : {'⛔ YES' if s['blocked'] else '✅ no'}\n"
        f"Checks done: {s['check_count']}\n"
        f"Last check : {last}\n"
        f"Interval   : every {CHECK_INTERVAL}s"
    )


# ── Auto-start on bot launch ──────────────────────────────────────────────────

async def post_init(app: Application) -> None:
    """Auto-start monitoring when the bot process starts."""
    state["monitoring"]   = True
    state["blocked"]      = False
    state["error_streak"] = 0

    async def _start(ctx: ContextTypes.DEFAULT_TYPE) -> None:
        await monitor_loop(ctx.application)

    app.job_queue.run_once(_start, when=0)


# ── Main ──────────────────────────────────────────────────────────────────────

def main() -> None:
    if not BOT_TOKEN:
        logger.error(
            "TELEGRAM_BOT_TOKEN is not set.\n"
            "Create a .env file with:\n"
            "  TELEGRAM_BOT_TOKEN=<your token>\n"
            "  TELEGRAM_CHAT_ID=<your chat id>"
        )
        sys.exit(1)

    app = (
        Application.builder()
        .token(BOT_TOKEN)
        .post_init(post_init)
        .build()
    )

    app.add_handler(CommandHandler("start",   cmd_start))
    app.add_handler(CommandHandler("monitor", cmd_monitor))
    app.add_handler(CommandHandler("stop",    cmd_stop))
    app.add_handler(CommandHandler("check",   cmd_check))
    app.add_handler(CommandHandler("status",  cmd_status))
    app.add_handler(CommandHandler("test",    cmd_test))

    logger.info("Bot starting…")
    app.run_polling(allowed_updates=Update.ALL_TYPES)


if __name__ == "__main__":
    main()
