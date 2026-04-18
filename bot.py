#!/usr/bin/env python3
"""
Telegram bot that monitors rdv.anct.gouv.fr for available RDV slots
and automatically reschedules to the earliest available date.

Detection logic:
  - "tous les créneaux sont pris" in page text  → no slots
  - phrase absent on HTTP 200                   → slots available → try to book
  - HTTP 429 / 403 / CAPTCHA                    → blocked → back off
  - redirected to login page                    → session expired → notify user
"""

import asyncio
import base64
import json
import logging
import os
import random
import sys
import tempfile
from datetime import datetime, date
from typing import Optional

import requests
from bs4 import BeautifulSoup
from dotenv import load_dotenv
from playwright.async_api import async_playwright, TimeoutError as PWTimeout
from telegram import Update
from telegram.constants import ParseMode
from telegram.ext import Application, CommandHandler, ContextTypes

load_dotenv(dotenv_path=".env", override=False)

# ── Configuration ─────────────────────────────────────────────────────────────
BOT_TOKEN        = os.getenv("TELEGRAM_BOT_TOKEN", "")
CHAT_ID          = os.getenv("TELEGRAM_CHAT_ID", "")
NTFY_TOPIC       = os.getenv("NTFY_TOPIC", "")
CURRENT_RDV_DATE = os.getenv("CURRENT_RDV_DATE", "")  # YYYY-MM-DD — your current appointment

RESCHEDULE_URL = "https://rdv.anct.gouv.fr/users/rdvs/779995/creneaux"
VIEW_URL       = "https://rdv.anct.gouv.fr/users/rdvs/779995"
SESSION_FILE   = "session.json"

# Hard limit — NEVER book anything on or after this date no matter what
HARD_LIMIT_DATE = date(2026, 5, 19)

CHECK_INTERVAL      = 0.8  # seconds between checks
REQUEST_TIMEOUT     = 15   # seconds for HTTP request
BACKOFF_AFTER_BLOCK = 300  # seconds to pause after being blocked

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
    "monitoring":        False,
    "slots_available":   None,   # None = unknown, True = yes, False = no
    "blocked":           False,
    "check_count":       0,
    "last_check":        None,
    "extra_wait":        0,
    "error_streak":      0,
    "current_rdv_date":  None,   # updated in memory after each successful booking
    "booking_active":    False,  # prevents concurrent booking attempts
    "session_notified":  False,  # prevents spamming session-expired messages
}

FRENCH_MONTHS = {
    "janvier": 1, "février": 2, "mars": 3, "avril": 4,
    "mai": 5, "juin": 6, "juillet": 7, "août": 8,
    "septembre": 9, "octobre": 10, "novembre": 11, "décembre": 12,
}


# ── Session helpers ───────────────────────────────────────────────────────────

def _get_session_path() -> Optional[str]:
    """Return path to a valid session.json (from env var or local file)."""
    session_state = os.getenv("SESSION_STATE", "")
    if session_state:
        try:
            decoded = base64.b64decode(session_state.encode()).decode()
            json.loads(decoded)  # validate JSON
            tmp = tempfile.NamedTemporaryFile(
                mode="w", suffix=".json", delete=False, encoding="utf-8"
            )
            tmp.write(decoded)
            tmp.close()
            return tmp.name
        except Exception as exc:
            logger.error(f"Failed to decode SESSION_STATE: {exc}")
    if os.path.exists(SESSION_FILE):
        return SESSION_FILE
    return None


def _load_session_cookies() -> dict:
    """Extract cookies from session file for use with requests."""
    path = _get_session_path()
    if not path:
        return {}
    try:
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
        return {c["name"]: c["value"] for c in data.get("cookies", [])}
    except Exception:
        return {}


def _parse_french_date(text: str) -> Optional[date]:
    """Parse '5 mai 2026' or '2026-05-05' into a date object."""
    text = text.strip().lower()
    try:
        return datetime.strptime(text, "%Y-%m-%d").date()
    except ValueError:
        pass
    parts = text.split()
    if len(parts) >= 3:
        try:
            day   = int(parts[0])
            month = FRENCH_MONTHS.get(parts[1])
            year  = int(parts[2])
            if month:
                return date(year, month, day)
        except (ValueError, TypeError):
            pass
    return None


def _current_target_date() -> Optional[date]:
    """
    Return the date the bot is currently trying to beat.
    Uses in-memory value (updated after bookings) or falls back to env var.
    """
    if state["current_rdv_date"]:
        return state["current_rdv_date"]
    if CURRENT_RDV_DATE:
        try:
            return datetime.strptime(CURRENT_RDV_DATE, "%Y-%m-%d").date()
        except ValueError:
            pass
    return None


def _is_bookable(d: date) -> bool:
    """
    Return True only if d is strictly before both the current target
    and the hard limit (19 mai 2026). Never book on or after 19 mai.
    """
    target = _current_target_date()
    if target and d >= target:
        return False
    if d >= HARD_LIMIT_DATE:
        return False
    return True


# ── Website checker ───────────────────────────────────────────────────────────

def _check_slots_no_session() -> bool:
    """
    Fallback check without session cookies.
    Returns True if slots appear available, False otherwise.
    Used when session is expired so alerts still fire.
    """
    try:
        s = requests.Session()
        s.max_redirects = 5
        resp = s.get(
            RESCHEDULE_URL,
            headers={
                "User-Agent":      random.choice(USER_AGENTS),
                "Accept-Language": "fr-FR",
            },
            timeout=REQUEST_TIMEOUT,
        )
        if resp.status_code != 200:
            return False
        if any(x in resp.url for x in ("sign_in", "franceconnect", "impots.gouv")):
            return False
        page_text = BeautifulSoup(resp.text, "html.parser").get_text(separator=" ", strip=True).lower()
        return "tous les créneaux sont pris" not in page_text
    except Exception:
        return False


def check_slots() -> dict:
    """
    Fetch the reschedule page with session cookies.
    Returns: { "status": str, "detail": str, "http_code": int|None }
    Statuses: available | unavailable | session_expired | blocked |
              rate_limited | captcha | error
    """
    cookies = _load_session_cookies()
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
        session = requests.Session()
        session.max_redirects = 10
        resp = session.get(
            RESCHEDULE_URL, headers=headers, cookies=cookies, timeout=REQUEST_TIMEOUT,
        )
    except requests.exceptions.TooManyRedirects:
        return {"status": "session_expired",
                "detail": "Too many redirects — session expired (redirect loop)",
                "http_code": None}
    except requests.exceptions.Timeout:
        return {"status": "error", "detail": "Request timed out", "http_code": None}
    except requests.exceptions.ConnectionError as exc:
        return {"status": "error", "detail": f"Connection error: {exc}", "http_code": None}
    except requests.exceptions.RequestException as exc:
        return {"status": "error", "detail": f"Request failed: {exc}", "http_code": None}

    code = resp.status_code

    if code == 429:
        return {"status": "rate_limited",
                "detail": f"HTTP 429 – Retry-After: {resp.headers.get('Retry-After', '?')}s",
                "http_code": 429}
    if code == 403:
        return {"status": "blocked", "detail": "HTTP 403 Forbidden", "http_code": 403}
    if code == 503:
        return {"status": "error", "detail": "HTTP 503 – site may be down", "http_code": 503}
    if code != 200:
        return {"status": "error", "detail": f"Unexpected HTTP {code}", "http_code": code}

    # Session expired → redirected to login page
    if "sign_in" in resp.url or "franceconnect" in resp.url:
        return {"status": "session_expired",
                "detail": "Session expired — need to re-login",
                "http_code": 200}

    soup      = BeautifulSoup(resp.text, "html.parser")
    page_text = soup.get_text(separator=" ", strip=True).lower()

    if any(s in page_text for s in ["captcha", "i'm not a robot", "cloudflare"]):
        return {"status": "captcha", "detail": "CAPTCHA detected", "http_code": 200}

    if any(p in page_text for p in [
        "tous les créneaux sont pris",
        "aucun créneau", "aucun créneaux", "aucune disponibilité",
    ]):
        return {"status": "unavailable", "detail": "No slots available", "http_code": 200}

    return {"status": "available", "detail": "Slots found on reschedule page", "http_code": 200}


# ── Telegram helpers ──────────────────────────────────────────────────────────

async def send_notification(app: Application, text: str) -> None:
    if not CHAT_ID:
        return
    try:
        await app.bot.send_message(chat_id=CHAT_ID, text=text, parse_mode=ParseMode.HTML)
    except Exception as exc:
        logger.error(f"Telegram send failed: {exc}")


async def send_alarm(app: Application, text: str) -> None:
    for i in range(5):
        try:
            await app.bot.send_message(
                chat_id=CHAT_ID,
                text=("🚨🚨🚨 " if i > 0 else "") + text,
                parse_mode=ParseMode.HTML,
                disable_notification=False,
            )
        except Exception as exc:
            logger.error(f"Alarm send failed: {exc}")
        await asyncio.sleep(0.3)


def send_ntfy_alarm() -> None:
    if not NTFY_TOPIC:
        return
    try:
        requests.post(
            f"https://ntfy.sh/{NTFY_TOPIC}",
            headers={"Title": "RDV DISPONIBLE !!!", "Priority": "urgent", "Tags": "rotating_light"},
            data="Un créneau est disponible ! Réservez maintenant.".encode("utf-8"),
            timeout=10,
        )
        logger.info("ntfy alarm sent")
    except Exception as exc:
        logger.error(f"ntfy failed: {exc}")


# ── Auto-booking ──────────────────────────────────────────────────────────────

async def try_book_earlier_slot(app: Application) -> bool:
    """
    Launch a headless browser with the saved session, open the reschedule page,
    find the earliest slot that passes _is_bookable(), and confirm it.
    Returns True if a booking was successfully made.
    """
    if state["booking_active"]:
        logger.info("Auto-booking: already in progress, skipping")
        return False

    target = _current_target_date()
    if not target:
        logger.warning("Auto-booking skipped: CURRENT_RDV_DATE not set")
        return False

    session_path = _get_session_path()
    if not session_path:
        await send_notification(
            app,
            "⚠️ <b>Auto-booking:</b> no session found.\n"
            "Run <code>python login.py</code> on your PC."
        )
        return False

    state["booking_active"] = True
    logger.info(f"Auto-booking: launching browser (beat {target}, hard limit {HARD_LIMIT_DATE})…")

    page = None
    try:
        async with async_playwright() as pw:
            browser = await pw.chromium.launch(headless=True)
            ctx = await browser.new_context(
                storage_state=session_path,
                locale="fr-FR",
                user_agent=random.choice(USER_AGENTS),
            )
            page = await ctx.new_page()

            await page.goto(RESCHEDULE_URL, timeout=30_000)
            await page.wait_for_load_state("networkidle", timeout=20_000)

            # Session expired inside Playwright
            if any(x in page.url for x in ("sign_in", "franceconnect", "impots.gouv")):
                logger.warning("Auto-booking: session expired (Playwright)")
                state["session_notified"] = False  # force fresh notification
                return False

            page_text = (await page.inner_text("body")).lower()
            if "tous les créneaux sont pris" in page_text:
                logger.info("Auto-booking: no slots on page")
                return False

            # Find all slot elements
            slots = await page.query_selector_all(
                "a[href*='creneau'], a[href*='créneau'], "
                "button[data-date], button[data-time], "
                "[class*='creneau']:not([disabled]), "
                "[class*='slot']:not([disabled])"
            )
            if not slots:
                logger.info("Auto-booking: no slot elements found in DOM")
                return False

            # Keep only slots that pass the hard limit + current target check
            bookable: list[tuple[date, object, str]] = []
            for slot in slots:
                raw = (
                    await slot.get_attribute("aria-label")
                    or await slot.get_attribute("data-date")
                    or await slot.inner_text()
                ) or ""
                parsed = _parse_french_date(raw)
                if parsed and _is_bookable(parsed):
                    bookable.append((parsed, slot, raw.strip()))

            if not bookable:
                logger.info("Auto-booking: no bookable slots earlier than target/hard-limit")
                return False

            # Pick the earliest
            bookable.sort(key=lambda x: x[0])
            best_date, best_el, best_label = bookable[0]
            logger.info(f"Auto-booking: clicking '{best_label}' ({best_date})…")
            await best_el.click()
            await page.wait_for_load_state("networkidle", timeout=15_000)

            # Confirm
            confirm = await page.query_selector(
                "button:has-text('Confirmer'), button:has-text('Valider'), "
                "button:has-text('OK'), input[type='submit']"
            )
            if confirm:
                await confirm.click()
                await page.wait_for_load_state("networkidle", timeout=15_000)

            # Verify booking went through — page should no longer be on the slot picker
            current_url = page.url
            if "creneaux" in current_url:
                logger.warning("Auto-booking: still on creneaux page after confirm — may have failed")
                await send_notification(
                    app,
                    f"⚠️ <b>Booking uncertain.</b>\n"
                    f"Tried to book {best_date.strftime('%d/%m/%Y')} but couldn't confirm success.\n"
                    f"👉 <a href=\"{VIEW_URL}\">Check your appointment manually</a>"
                )
                return False

            await browser.close()

            booked_str = best_date.strftime("%d/%m/%Y")
            logger.info(f"Auto-booking: confirmed {booked_str}")

            # Update in-memory target so next booking must be even earlier
            state["current_rdv_date"] = best_date

            send_ntfy_alarm()
            await send_alarm(
                app,
                f"🎉 <b>Appointment rescheduled!</b>\n"
                f"New date : <b>{booked_str}</b>\n"
                f"Still monitoring for anything earlier than {booked_str}…\n\n"
                f"⚠️ Update <code>CURRENT_RDV_DATE={best_date.strftime('%Y-%m-%d')}</code> "
                f"on Railway so the bot remembers after a restart.\n"
                f"👉 <a href=\"{VIEW_URL}\">View your appointment</a>"
            )
            return True

    except PWTimeout:
        logger.error("Auto-booking: timed out waiting for page")
        await send_notification(app, "⚠️ <b>Auto-booking timed out.</b> Will retry on next slot detection.")
        if page:
            try:
                await page.screenshot(path="booking_error.png")
            except Exception:
                pass
        return False
    except Exception as exc:
        logger.error(f"Auto-booking error: {exc}")
        await send_notification(app, f"⚠️ <b>Auto-booking error:</b> {exc}")
        if page:
            try:
                await page.screenshot(path="booking_error.png")
            except Exception:
                pass
        return False
    finally:
        state["booking_active"] = False


# ── Monitoring loop ───────────────────────────────────────────────────────────

async def monitor_loop(app: Application) -> None:
    logger.info("Monitoring loop started")

    target    = _current_target_date()
    has_session = bool(_get_session_path())
    auto_book = bool(target and has_session)

    await send_notification(
        app,
        f"🔍 <b>Monitoring started</b>\n"
        f"Interval     : every {CHECK_INTERVAL}s\n"
        f"Target date  : before {target.strftime('%d/%m/%Y') if target else 'not set'}\n"
        f"Hard limit   : {HARD_LIMIT_DATE.strftime('%d/%m/%Y')} (never book on/after)\n"
        f"Auto-booking : {'✅ enabled' if auto_book else '❌ disabled'}"
    )

    while state["monitoring"]:
        if state["extra_wait"] > 0:
            wait = state["extra_wait"]
            state["extra_wait"] = 0
            logger.info(f"Back-off: waiting {wait}s")
            for _ in range(wait):
                if not state["monitoring"]:
                    break
                await asyncio.sleep(1)
            if not state["monitoring"]:
                break

        result       = check_slots()
        status       = result["status"]
        detail       = result["detail"]
        prev_slots   = state["slots_available"]
        prev_blocked = state["blocked"]

        state["check_count"] += 1
        state["last_check"]   = datetime.now()
        logger.info(f"Check #{state['check_count']}: [{status}] {detail}")

        # ── Session expired ───────────────────────────────────────────────────
        if status == "session_expired":
            if not state["session_notified"]:
                state["session_notified"] = True
                await send_notification(
                    app,
                    "🔒 <b>Session expired!</b>\n\n"
                    "1. Run <code>python login.py</code> on your PC\n"
                    "2. Copy the <code>SESSION_STATE=...</code> value\n"
                    "3. Update it in Railway Variables\n\n"
                    "⚠️ Auto-booking is paused but alerts will still fire if slots appear."
                )
            # Try a cookie-less check as fallback — site may still show slot info
            fallback = await asyncio.get_event_loop().run_in_executor(None, _check_slots_no_session)
            if fallback and state["slots_available"] is not True:
                state["slots_available"] = True
                send_ntfy_alarm()
                await send_alarm(
                    app,
                    f"🎉 <b>RDV SLOTS ARE AVAILABLE!</b>\n"
                    f"⚠️ Session expired — <b>auto-booking paused</b>. Book manually!\n\n"
                    f"👉 <a href=\"{RESCHEDULE_URL}\">Reschedule NOW</a>"
                )
            elif not fallback:
                state["slots_available"] = None
            await asyncio.sleep(CHECK_INTERVAL)
            continue

        # Session back — reset notification flag
        if state["session_notified"] and status != "session_expired":
            state["session_notified"] = False
            await send_notification(app, "✅ <b>Session restored.</b> Auto-booking is active again.")

        # ── Blocked / rate-limited / CAPTCHA ──────────────────────────────────
        if status in ("blocked", "rate_limited", "captcha"):
            state["error_streak"] += 1
            if not state["blocked"]:
                state["blocked"] = True
                await send_notification(
                    app,
                    f"⛔ <b>Monitoring blocked!</b>\n{detail}\n"
                    f"Pausing {BACKOFF_AFTER_BLOCK // 60} min…"
                )
            state["extra_wait"] = BACKOFF_AFTER_BLOCK
            await asyncio.sleep(CHECK_INTERVAL)
            continue

        if prev_blocked and status not in ("blocked", "rate_limited", "captcha"):
            state["blocked"]      = False
            state["error_streak"] = 0
            await send_notification(app, "✅ <b>Monitoring resumed.</b>")

        # ── Generic error ─────────────────────────────────────────────────────
        if status == "error":
            state["error_streak"] += 1
            if state["error_streak"] == 1:
                await send_notification(app, f"⚠️ <b>Check failed:</b> {detail}")
            state["slots_available"] = None
            await asyncio.sleep(CHECK_INTERVAL)
            continue

        state["error_streak"] = 0

        # ── Slots available ───────────────────────────────────────────────────
        if status == "available":
            if prev_slots is not True:
                state["slots_available"] = True
                send_ntfy_alarm()
                await send_alarm(
                    app,
                    f"🎉 <b>RDV SLOTS ARE AVAILABLE!</b>\n\n"
                    f"👉 <a href=\"{RESCHEDULE_URL}\">Reschedule NOW</a>"
                )
                # Re-evaluate auto_book dynamically each time
                current_auto_book = bool(_current_target_date() and _get_session_path())
                if current_auto_book:
                    await try_book_earlier_slot(app)

        # ── No slots ──────────────────────────────────────────────────────────
        elif status == "unavailable":
            if prev_slots is True:
                await send_notification(app, "ℹ️ <b>Slots are gone.</b> Continuing to monitor…")
            state["slots_available"] = False

        await asyncio.sleep(CHECK_INTERVAL)

    logger.info("Monitoring loop stopped")


# ── Command handlers ──────────────────────────────────────────────────────────

async def cmd_start(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    chat_id = update.effective_chat.id
    await update.message.reply_html(
        f"👋 <b>RDV Monitor Bot</b>\n\n"
        f"Your chat ID: <code>{chat_id}</code>\n\n"
        f"<b>Commands:</b>\n"
        f"/monitor – start monitoring\n"
        f"/stop    – stop monitoring\n"
        f"/check   – one-time check\n"
        f"/status  – show current status\n"
        f"/session – check if login session is valid\n"
        f"/test    – fire a test alarm"
    )


async def cmd_monitor(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    if state["monitoring"]:
        await update.message.reply_text("Already monitoring! Use /status to check.")
        return
    state["monitoring"]       = True
    state["blocked"]          = False
    state["extra_wait"]       = 0
    state["error_streak"]     = 0
    state["slots_available"]  = None
    state["session_notified"] = False
    ctx.application.create_task(monitor_loop(ctx.application))


async def cmd_stop(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    if not state["monitoring"]:
        await update.message.reply_text("Not currently monitoring.")
        return
    state["monitoring"] = False
    await update.message.reply_text("🛑 Monitoring stopped.")


async def cmd_check(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    await update.message.reply_text("🔄 Checking…")
    result = await asyncio.get_event_loop().run_in_executor(None, check_slots)
    status = result["status"]
    emoji  = {
        "available":      "✅",
        "unavailable":    "❌",
        "session_expired":"🔒",
        "blocked":        "⛔",
        "rate_limited":   "🚫",
        "captcha":        "🤖",
        "error":          "⚠️",
    }.get(status, "❓")
    msg = f"{emoji} <b>{status.upper()}</b>\n{result['detail']}\nHTTP: {result['http_code'] or 'N/A'}"
    if status == "available":
        msg += f'\n\n👉 <a href="{RESCHEDULE_URL}">Reschedule NOW</a>'
    await update.message.reply_html(msg)


async def cmd_test(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    await update.message.reply_text("🔔 Firing test alarm…")
    send_ntfy_alarm()
    await send_alarm(
        ctx.application,
        f"🎉 <b>[TEST] RDV SLOTS ARE AVAILABLE!</b>\n"
        f"This is a test.\n\n"
        f"👉 <a href=\"{RESCHEDULE_URL}\">Reschedule NOW</a>"
    )


async def cmd_session(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    has_env_var = bool(os.getenv("SESSION_STATE", ""))
    has_file    = os.path.exists(SESSION_FILE)
    cookies     = _load_session_cookies()

    if not cookies:
        await update.message.reply_html(
            f"❌ <b>No session found.</b>\n\n"
            f"SESSION_STATE env var : {'✅ set' if has_env_var else '❌ not set'}\n"
            f"session.json file     : {'✅ exists' if has_file else '❌ not found'}\n\n"
            f"Run <code>python login.py</code> on your PC and add "
            f"<code>SESSION_STATE</code> to Railway variables."
        )
        return

    await update.message.reply_text("🔄 Checking session…")
    try:
        def _do_session_check():
            s = requests.Session()
            s.max_redirects = 10
            return s.get(
                RESCHEDULE_URL,
                headers={"User-Agent": random.choice(USER_AGENTS), "Accept-Language": "fr-FR"},
                cookies=cookies,
                timeout=15,
                allow_redirects=True,
            )
        resp = await asyncio.get_event_loop().run_in_executor(None, _do_session_check)
        if any(x in resp.url for x in ("sign_in", "franceconnect", "impots.gouv")):
            await update.message.reply_html(
                "🔒 <b>Session expired.</b>\n"
                "Re-run <code>python login.py</code> and update <code>SESSION_STATE</code> on Railway."
            )
        else:
            page_text   = resp.text.lower()
            slot_status = (
                "❌ No slots right now"
                if "tous les créneaux sont pris" in page_text
                else "✅ Slots appear available!"
            )
            target = _current_target_date()
            await update.message.reply_html(
                f"✅ <b>Session is valid.</b>\n\n"
                f"Cookies     : {len(cookies)}\n"
                f"Slots now   : {slot_status}\n"
                f"Target      : before {target.strftime('%d/%m/%Y') if target else 'not set'}\n"
                f"Hard limit  : never book on/after {HARD_LIMIT_DATE.strftime('%d/%m/%Y')}"
            )
    except Exception as exc:
        await update.message.reply_html(f"⚠️ <b>Error:</b> {exc}")


async def cmd_status(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    s           = state
    slot_icon   = {True: "✅ Available", False: "❌ Unavailable", None: "❓ Unknown"}[s["slots_available"]]
    last        = s["last_check"].strftime("%d/%m %H:%M:%S") if s["last_check"] else "never"
    target      = _current_target_date()
    target_str  = target.strftime("%d/%m/%Y") if target else "not set"
    auto_book   = bool(target and _get_session_path())
    await update.message.reply_html(
        f"📊 <b>Bot Status</b>\n\n"
        f"Monitoring   : {'🟢 ON' if s['monitoring'] else '🔴 OFF'}\n"
        f"Slots now    : {slot_icon}\n"
        f"Blocked      : {'⛔ YES' if s['blocked'] else '✅ no'}\n"
        f"Session      : {'🔒 EXPIRED' if s['session_notified'] else '✅ ok'}\n"
        f"Booking lock : {'🔄 active' if s['booking_active'] else '✅ free'}\n"
        f"Checks done  : {s['check_count']}\n"
        f"Last check   : {last}\n"
        f"Interval     : {CHECK_INTERVAL}s\n"
        f"Target       : before {target_str}\n"
        f"Hard limit   : {HARD_LIMIT_DATE.strftime('%d/%m/%Y')}\n"
        f"Auto-booking : {'✅ ON' if auto_book else '❌ OFF'}"
    )


# ── Auto-start ────────────────────────────────────────────────────────────────

async def post_init(app: Application) -> None:
    state["monitoring"]   = True
    state["blocked"]      = False
    state["error_streak"] = 0

    async def _start(ctx: ContextTypes.DEFAULT_TYPE) -> None:
        await monitor_loop(ctx.application)

    app.job_queue.run_once(_start, when=0)


# ── Main ──────────────────────────────────────────────────────────────────────

def main() -> None:
    if not BOT_TOKEN:
        logger.error("TELEGRAM_BOT_TOKEN is not set.")
        sys.exit(1)

    app = Application.builder().token(BOT_TOKEN).post_init(post_init).build()

    app.add_handler(CommandHandler("start",   cmd_start))
    app.add_handler(CommandHandler("monitor", cmd_monitor))
    app.add_handler(CommandHandler("stop",    cmd_stop))
    app.add_handler(CommandHandler("check",   cmd_check))
    app.add_handler(CommandHandler("status",  cmd_status))
    app.add_handler(CommandHandler("test",    cmd_test))
    app.add_handler(CommandHandler("session", cmd_session))

    logger.info("Bot starting…")
    app.run_polling(allowed_updates=Update.ALL_TYPES)


if __name__ == "__main__":
    main()
