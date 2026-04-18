#!/usr/bin/env python3
"""
Run this script ONCE and leave it running.
It opens a real browser — log in via France Connect → impôts.gouv.fr.
Once logged in, press Enter. The browser stays open and bot.py connects to it.
"""

import asyncio
import base64
import json
from playwright.async_api import async_playwright

RESCHEDULE_URL   = "https://rdv.anct.gouv.fr/users/rdvs/779995/creneaux"
SESSION_FILE     = "session.json"
ENDPOINT_FILE    = "browser_endpoint.txt"


async def main() -> None:
    async with async_playwright() as pw:
        # launch_server() exposes a WS endpoint that bot.py can connect to
        browser_server = await pw.chromium.launch_server(headless=False)
        ws_endpoint    = browser_server.ws_endpoint

        # Connect a client to that server to do the actual browsing
        browser = await pw.chromium.connect(ws_endpoint)
        ctx     = await browser.new_context(locale="fr-FR")
        page    = await ctx.new_page()

        print("\n=== Login helper ===")
        print("Browser opening... Log in via France Connect → impôts.gouv.fr.")
        print("Once you can see your RDV page, come back here.\n")

        await page.goto(RESCHEDULE_URL)
        input("Press Enter once you are fully logged in... ")

        # Save cookies for HTTP requests (keep-alive, /session command)
        full_state   = await ctx.storage_state()
        cookies_only = {"cookies": full_state["cookies"]}

        with open(SESSION_FILE, "w", encoding="utf-8") as f:
            json.dump(cookies_only, f)

        b64 = base64.b64encode(json.dumps(cookies_only).encode()).decode()

        # Save the WS endpoint so bot.py can connect to THIS browser server
        with open(ENDPOINT_FILE, "w", encoding="utf-8") as f:
            f.write(ws_endpoint)

        print(f"\n✅ Session saved to {SESSION_FILE}")
        print(f"   Cookies: {len(cookies_only['cookies'])}")
        print(f"   Browser endpoint saved to {ENDPOINT_FILE}")
        print(f"\n--- Copy this as SESSION_STATE on Railway (for alarm-only mode) ---")
        print(f"SESSION_STATE={b64}")
        print(f"---------------------------------------------------------------------\n")
        print("✅ Browser is staying open. Start bot.py in another terminal.")
        print("   Keep THIS terminal running — closing it kills the browser and session.\n")
        print("   Press Ctrl+C to stop.\n")

        # Keep the browser server alive forever
        try:
            await asyncio.Event().wait()
        except (asyncio.CancelledError, KeyboardInterrupt):
            print("\nShutting down browser...")

        await browser_server.close()


if __name__ == "__main__":
    asyncio.run(main())
