"""
refresh_token_cloud.py — refresh DELIVEROO_TOKEN GitHub secret
Designed for GitHub Actions (headless, no interactive input).

Reads from environment:
  DELIVEROO_EMAIL     — set as GitHub Secret
  DELIVEROO_PASSWORD  — set as GitHub Secret
  GITHUB_PAT          — set as GitHub Secret (needs secrets:write)
  GITHUB_REPOSITORY   — auto-set by GitHub Actions (e.g. lknscorecard-ux/deliveroo-dashboard)
"""

import asyncio
import base64
import json
import os
import sys
import urllib.request
from playwright.async_api import async_playwright

EMAIL    = os.environ["DELIVEROO_EMAIL"]
PASSWORD = os.environ["DELIVEROO_PASSWORD"]
PAT      = os.environ["GITHUB_PAT"]
REPO     = os.environ.get("GITHUB_REPOSITORY", "lknscorecard-ux/deliveroo-dashboard")
SECRET   = "DELIVEROO_TOKEN"
BASE_URL = "https://partner-hub.deliveroo.com"


async def extract_token():
    async with async_playwright() as pw:
        browser = await pw.chromium.launch(
            headless=True,
            args=[
                "--disable-blink-features=AutomationControlled",
                "--no-sandbox",
                "--disable-setuid-sandbox",
            ],
        )
        ctx = await browser.new_context(
            user_agent=(
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/124.0.0.0 Safari/537.36"
            ),
            viewport={"width": 1280, "height": 800},
        )
        page = await ctx.new_page()

        print("Opening Partner Hub…")
        await page.goto(f"{BASE_URL}/home?orgId=513610",
                        wait_until="domcontentloaded", timeout=30000)
        await page.wait_for_timeout(3000)

        if "/login" in page.url:
            print("Not logged in — logging in…")

            # Dismiss cookie banner
            for txt in ["Continue without accepting", "Accept all", "Reject all"]:
                try:
                    btn = page.locator(f"button:has-text('{txt}')").first
                    if await btn.is_visible(timeout=2000):
                        await btn.click()
                        await page.wait_for_timeout(400)
                        break
                except Exception:
                    pass

            await page.wait_for_selector('input[type="email"]', timeout=20000)
            await page.locator('input[type="email"]').first.fill(EMAIL)
            await page.wait_for_timeout(400)
            await page.locator('input[type="password"]').first.fill(PASSWORD)
            await page.wait_for_timeout(400)
            await page.locator('form button[type="submit"]').first.click()

            try:
                await page.wait_for_url(lambda u: "/login" not in u, timeout=40000)
                print(f"Logged in. URL: {page.url}")
            except Exception:
                # Take a screenshot to debug
                await page.screenshot(path="login_debug.png")
                print("ERROR: Login may have failed or CAPTCHA appeared.")
                print("Screenshot saved as login_debug.png")
                sys.exit(1)

            await page.wait_for_timeout(3000)
        else:
            print("Session still active (no login required).")

        cookies = await ctx.cookies([BASE_URL])
        token = next((c["value"] for c in cookies if c["name"] == "token"), None)
        await browser.close()
        return token


def push_to_github(secret_value):
    from nacl import encoding, public

    owner, repo = REPO.split("/")
    headers = {
        "Authorization": f"Bearer {PAT}",
        "Accept": "application/vnd.github+json",
        "X-GitHub-Api-Version": "2022-11-28",
        "Content-Type": "application/json",
    }

    # Get repo public key
    req = urllib.request.Request(
        f"https://api.github.com/repos/{owner}/{repo}/actions/secrets/public-key",
        headers=headers,
    )
    with urllib.request.urlopen(req) as r:
        key_data = json.loads(r.read())

    # Encrypt secret
    pub_key = public.PublicKey(key_data["key"].encode(), encoding.Base64Encoder)
    encrypted = base64.b64encode(
        public.SealedBox(pub_key).encrypt(secret_value.encode())
    ).decode()

    # Update secret
    payload = json.dumps({
        "encrypted_value": encrypted,
        "key_id": key_data["key_id"],
    }).encode()
    req = urllib.request.Request(
        f"https://api.github.com/repos/{owner}/{repo}/actions/secrets/{SECRET}",
        data=payload, headers=headers, method="PUT",
    )
    with urllib.request.urlopen(req) as r:
        print(f"GitHub secret '{SECRET}' updated (HTTP {r.status}).")


async def main():
    print("=== Deliveroo Token Refresh (Cloud) ===")
    token = await extract_token()
    if not token:
        print("ERROR: token cookie not found after login.")
        sys.exit(1)
    print(f"Token extracted ({len(token)} chars).")
    push_to_github(token)
    print("=== Done ===")


asyncio.run(main())
