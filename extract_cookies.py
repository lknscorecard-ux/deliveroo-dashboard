"""
One-time cookie extractor — run this LOCALLY after logging in.
Visits partner-hub AND identity.deliveroo.com to capture ALL session cookies,
including the identity session that allows the server to refresh the JWT.

Usage:
    pip install playwright pyperclip
    python -m playwright install chromium
    python extract_cookies.py
"""

import asyncio
import json
from pathlib import Path

try:
    import pyperclip
    HAS_CLIPBOARD = True
except ImportError:
    HAS_CLIPBOARD = False

from playwright.async_api import async_playwright

SESSION_DIR = str(Path(__file__).parent / "deliveroo_session")


async def main():
    async with async_playwright() as pw:
        ctx = await pw.chromium.launch_persistent_context(
            SESSION_DIR,
            headless=False,
            viewport={"width": 1400, "height": 800},
        )
        page = ctx.pages[0] if ctx.pages else await ctx.new_page()

        # Step 1: Visit partner hub home (triggers server-side JWT refresh)
        print("Opening Deliveroo Partner Hub…")
        await page.goto("https://partner-hub.deliveroo.com/home", wait_until="domcontentloaded")
        await page.wait_for_timeout(3000)

        if "login" in page.url or "sign-in" in page.url or "auth" in page.url:
            print("\n  Please log in manually in the browser window.")
            print("  Press Enter here once you are fully logged in and on the Home page.")
            input()

        # Step 2: Also visit identity.deliveroo.com to capture identity session cookies
        print("Capturing identity session cookies…")
        await page.goto("https://identity.deliveroo.com/", wait_until="domcontentloaded")
        await page.wait_for_timeout(2000)

        # Step 3: Go back to partner hub to trigger a fresh JWT issuance
        print("Refreshing JWT via partner hub…")
        await page.goto("https://partner-hub.deliveroo.com/home", wait_until="networkidle", timeout=15000)
        await page.wait_for_timeout(3000)

        # Step 4: Extract ALL cookies from all domains
        print("Extracting cookies…")
        cookies = await ctx.cookies([
            "https://partner-hub.deliveroo.com",
            "https://identity.deliveroo.com",
            "https://deliveroo.com",
        ])
        await ctx.close()

    cookie_json = json.dumps(cookies)

    print(f"\n  Extracted {len(cookies)} cookies.\n")
    print("=" * 60)
    print("DELIVEROO_COOKIES (copy everything between the lines):")
    print("=" * 60)
    print(cookie_json)
    print("=" * 60)

    if HAS_CLIPBOARD:
        pyperclip.copy(cookie_json)
        print("\n  Copied to clipboard automatically!")

    print("\nNext steps:")
    print("  1. GitHub repo -> Settings -> Secrets and variables -> Actions")
    print("  2. Click DELIVEROO_COOKIES secret -> Update")
    print("  3. Paste the JSON above -> Save")
    print("  4. Run the workflow immediately — do NOT wait!")


if __name__ == "__main__":
    asyncio.run(main())
