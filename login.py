#!/usr/bin/env python3
"""
Run this script ONCE on your PC to save your login session.
A real browser window opens — log in via France Connect → impôts.gouv.fr.
Once you can see the RDV page, press Enter here.
"""

import asyncio
import base64
import json
from playwright.async_api import async_playwright

RESCHEDULE_URL = "https://rdv.anct.gouv.fr/users/rdvs/779995/creneaux"
SESSION_FILE   = "session.json"


async def main() -> None:
    async with async_playwright() as pw:
        browser = await pw.chromium.launch(headless=False)
        ctx     = await browser.new_context(locale="fr-FR")
        page    = await ctx.new_page()

        print("\n=== Login helper ===")
        print("Browser opening... Log in via France Connect → impôts.gouv.fr.")
        print("Once you can see your RDV page, come back here.\n")

        await page.goto(RESCHEDULE_URL)
        input("Press Enter once you are fully logged in... ")

        # Save full storage state to file (used locally)
        full_state = await ctx.storage_state()

        # Extract only cookies (much smaller — safe for Railway env vars)
        cookies_only = {"cookies": full_state["cookies"]}

        with open(SESSION_FILE, "w", encoding="utf-8") as f:
            json.dump(cookies_only, f)

        b64 = base64.b64encode(json.dumps(cookies_only).encode()).decode()

        print(f"\n✅ Session saved to {SESSION_FILE}")
        print(f"   Cookies saved: {len(cookies_only['cookies'])}")
        print(f"   Base64 size:   {len(b64)} characters")
        print("\n--- Copy everything after the = and paste it as SESSION_STATE on Railway ---")
        print(f"SESSION_STATE={b64}")
        print("------------------------------------------------------------------------------\n")

        await browser.close()


if __name__ == "__main__":
    asyncio.run(main())
