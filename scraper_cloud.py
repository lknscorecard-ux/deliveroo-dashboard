"""
Deliveroo Partner Hub — Cloud Scraper v3 (GitHub Actions edition)
Logs in fresh with email + password every run — no cookie expiry issues.
Secrets: DELIVEROO_EMAIL, DELIVEROO_PASSWORD
"""

import asyncio
import json
import os
import re
from datetime import date, timedelta
from pathlib import Path

from playwright.async_api import async_playwright, TimeoutError as PWTimeout
from playwright_stealth import stealth_async

# ── Config ─────────────────────────────────────────────────────────────────────
ORG_ID    = "513610"
BASE_URL  = "https://partner-hub.deliveroo.com"
LOGIN_URL = f"{BASE_URL}/login"
NOTIF_URL = f"{BASE_URL}/notifications?back_url=%2Fhome&orgId={ORG_ID}"
OUT_FILE  = Path(__file__).parent / "data.json"

EMAIL    = os.environ.get("DELIVEROO_EMAIL", "")
PASSWORD = os.environ.get("DELIVEROO_PASSWORD", "")

_api_responses = []


# ── Network interceptor ────────────────────────────────────────────────────────
async def capture_response(response):
    url = response.url
    if any(k in url.lower() for k in ["notification", "review", "dashapi", "activity", "feed", "alert"]):
        try:
            if "json" in response.headers.get("content-type", ""):
                body = await response.json()
                print(f"[NET] {response.status} {url[:120]}")
                _api_responses.append({"url": url, "status": response.status, "body": body})
        except Exception:
            pass


# ── Helpers ────────────────────────────────────────────────────────────────────
def categorise(site):
    brands = ["Twisted London", "Kuro Smash", "Hot Chick", "Koreatown",
              "Lean Kitchen", "Bao Boys", "Dirty Bones"]
    for b in brands:
        if b.lower() in (site or "").lower():
            return b
    return "Other"


def extract_site(text):
    m = re.search(r"[•·]\s*(.+?)'s first order", text)
    if m:
        return m.group(1).strip()
    m = re.search(r"(\w[\w\s]+?)\s+first order", text, re.IGNORECASE)
    return m.group(1).strip() if m else "Unknown"


async def dismiss_popups(page):
    """Dismiss all post-login popups: Medallia survey, cookie banner, notification prompts."""
    # Medallia survey "Close" button
    for selector in [
        "button:has-text('Close')",
        "button:has-text('Cancel')",
        "[aria-label='Close']",
        ".medallia-survey button",
        "button.close",
    ]:
        try:
            btn = page.locator(selector).first
            if await btn.is_visible(timeout=1500):
                await btn.click()
                await page.wait_for_timeout(500)
                print(f"Dismissed popup: {selector}")
        except Exception:
            pass

    # Cookie banner
    await dismiss_cookies(page)


async def dismiss_cookies(page):
    try:
        await page.locator("button", has_text="Continue without accepting").click(timeout=2000)
        await page.wait_for_timeout(500)
    except PWTimeout:
        pass


async def close_popup(page):
    try:
        await page.locator("button[aria-label='Close']").click(timeout=2000)
        await page.wait_for_timeout(500)
    except PWTimeout:
        url = page.url
        if "notification=" in url:
            clean = re.sub(r"&?notification=[^&]+", "", url)
            await page.goto(clean, wait_until="domcontentloaded")
            await page.wait_for_timeout(1000)


# ── Login ──────────────────────────────────────────────────────────────────────
async def login(page):
    print("Logging in…")
    await page.goto(LOGIN_URL, wait_until="domcontentloaded")
    await page.wait_for_timeout(3000)
    await dismiss_cookies(page)

    # Wait for email field
    email_loc = page.locator('[data-testid="login-email"]')
    await email_loc.wait_for(timeout=10000)

    # Click → triple-click to select all → type (React needs real key events)
    await email_loc.click(click_count=3)
    await page.wait_for_timeout(300)
    await page.keyboard.type(EMAIL, delay=50)
    await page.wait_for_timeout(400)

    pw_loc = page.locator('[data-testid="login-password"]')
    await pw_loc.click(click_count=3)
    await page.wait_for_timeout(300)
    await page.keyboard.type(PASSWORD, delay=50)
    await page.wait_for_timeout(500)

    # Debug: print what's in the fields
    email_val = await email_loc.input_value()
    pw_val    = await pw_loc.input_value()
    print(f"Email field value: {email_val}")
    print(f"Password field filled: {'yes' if pw_val else 'no'}")

    await page.locator('[data-testid="login-submit"]').click()
    await page.wait_for_timeout(4000)

    print(f"After submit — URL: {page.url}")
    try:
        body = await page.inner_text("body")
        print(f"Page text after submit: {body[:600]}")
    except Exception:
        pass

    # Check for error messages
    if "login" in page.url:
        print("ERROR: Still on login page — wrong credentials or bot detection.")
        raise SystemExit(1)

    print(f"Logged in! URL: {page.url}")


# ── Parse API responses ────────────────────────────────────────────────────────
def parse_api_notifications():
    yesterday = str(date.today() - timedelta(days=1))
    cancelled = []
    review_branches = []

    for item in _api_responses:
        body = item["body"]
        url  = item["url"]
        print(f"[PARSE] {url[:100]} — keys: {list(body.keys()) if isinstance(body, dict) else type(body).__name__}")

        notifications = []
        if isinstance(body, list):
            notifications = body
        elif isinstance(body, dict):
            for key in ["notifications", "items", "data", "results", "feed", "alerts"]:
                if key in body and isinstance(body[key], list):
                    notifications = body[key]
                    print(f"[PARSE] {len(notifications)} items under '{key}'")
                    break

        for n in notifications:
            if not isinstance(n, dict):
                continue
            created = str(n.get("created_at", n.get("date", n.get("timestamp", ""))))
            if yesterday not in created:
                continue

            ntype = str(n.get("type", n.get("category", n.get("kind", "")))).lower()
            text  = str(n.get("body", n.get("message", n.get("text", n.get("description", ""))))).lower()
            title = str(n.get("title", n.get("subject", ""))).lower()

            if any(k in ntype or k in text or k in title for k in ["cancel", "reject", "first order"]):
                site = (n.get("site_name") or n.get("restaurant_name") or
                        n.get("branch_name") or extract_site(text) or "Unknown")
                cancelled.append({"time": created, "site": site, "brand": categorise(site)})
                print(f"[PARSE] → Cancelled: {site}")

            elif any(k in ntype or k in text or k in title for k in ["review", "rating", "feedback"]):
                branch_id = str(n.get("branch_id") or n.get("branchId") or n.get("location_id") or "")
                site = (n.get("site_name") or n.get("restaurant_name") or n.get("branch_name") or "Unknown")
                review_branches.append({"branchId": branch_id, "name": site})
                print(f"[PARSE] → Review: {site}")

    return cancelled, review_branches


# ── DOM fallback ───────────────────────────────────────────────────────────────
async def get_yesterday_timestamps(page):
    return await page.evaluate(r"""() => {
        const main = document.querySelector('main');
        if (!main) return [];
        const seen = new Set();
        const results = [];
        for (const el of main.querySelectorAll('*')) {
            if (el.childElementCount > 0) continue;
            const t = (el.textContent || '').trim();
            if (/^Yesterday at \d{2}:\d{2}$/.test(t) && !seen.has(t)) {
                seen.add(t);
                results.push(t);
            }
        }
        return results;
    }""")


async def click_row_by_ts(page, ts):
    return await page.evaluate("""(ts) => {
        const main = document.querySelector('main');
        if (!main) return false;
        const el = Array.from(main.querySelectorAll('*'))
            .filter(e => e.childElementCount === 0)
            .find(e => e.textContent.trim() === ts);
        if (!el) return false;
        let row = el;
        for (let i = 0; i < 6; i++) {
            if (row.parentElement && row.parentElement !== document.body)
                row = row.parentElement;
        }
        row.click();
        return true;
    }""", ts)


async def get_popup_text(page):
    try:
        await page.wait_for_selector("text=Notification details", timeout=6000)
    except PWTimeout:
        return ""
    return await page.evaluate("""() => {
        const h = Array.from(document.querySelectorAll('*'))
            .find(el => el.childElementCount === 0 &&
                        el.textContent.trim() === 'Notification details');
        if (!h) return '';
        let n = h;
        for (let i = 0; i < 6; i++) { if (n.parentElement) n = n.parentElement; }
        return n.innerText || n.textContent || '';
    }""")


async def get_branch_links(page):
    return await page.evaluate("""() =>
        Array.from(document.querySelectorAll('a[href*="branchId"]'))
            .map(a => {
                const m = a.href.match(/branchId=(\\d+)/);
                return m ? { name: (a.innerText || a.textContent || '').trim(), branchId: m[1] } : null;
            }).filter(Boolean)
    """)


async def scrape_reviews_for_branch(page, branch_id, site_name):
    url = f"{BASE_URL}/reviews?orgId={ORG_ID}&branchId={branch_id}&dateRangePreset=last_7_days"
    await page.goto(url, wait_until="domcontentloaded")
    await page.wait_for_timeout(3000)
    await dismiss_cookies(page)

    for btn in await page.locator("text=expand_more").all():
        try:
            await btn.click(timeout=1000)
            await page.wait_for_timeout(200)
        except Exception:
            pass

    reviews = []
    try:
        text = await page.inner_text("main")
        lines = [l.strip() for l in text.splitlines() if l.strip()]
        for i, line in enumerate(lines):
            if re.match(r"^[1-5]$", line) and i + 1 < len(lines):
                nxt = lines[i + 1]
                if len(nxt) > 5 and not re.match(r"^\d+$", nxt):
                    reviews.append({
                        "rating": int(line),
                        "text": nxt,
                        "site": site_name,
                        "brand": categorise(site_name),
                    })
    except Exception as e:
        print(f"  Error parsing reviews: {e}")
    return reviews


# ── Main ───────────────────────────────────────────────────────────────────────
async def main():
    if not EMAIL or not PASSWORD:
        print("ERROR: Set DELIVEROO_EMAIL and DELIVEROO_PASSWORD secrets.")
        raise SystemExit(1)

    async with async_playwright() as pw:
        browser = await pw.chromium.launch(
            headless=True,
            args=["--no-sandbox", "--disable-dev-shm-usage", "--disable-gpu",
                  "--disable-blink-features=AutomationControlled"],
        )
        ctx = await browser.new_context(
            viewport={"width": 1440, "height": 900},
            user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                       "AppleWebKit/537.36 (KHTML, like Gecko) "
                       "Chrome/124.0.0.0 Safari/537.36",
            permissions=[],   # blocks browser-level notification permission prompts
        )
        page = await ctx.new_page()
        await stealth_async(page)
        page.on("response", capture_response)

        # ── Login ──────────────────────────────────────────────────────────────
        await login(page)
        await page.wait_for_timeout(2000)
        await dismiss_popups(page)   # close Medallia survey + any other blockers

        # ── Navigate to notifications ──────────────────────────────────────────
        print(f"\nNavigating to notifications…")
        await page.goto(NOTIF_URL, wait_until="domcontentloaded")
        await page.wait_for_timeout(5000)
        await dismiss_cookies(page)

        print(f"URL : {page.url}")
        print(f"Title: {await page.title()}")

        try:
            body_text = await page.inner_text("body")
            print(f"Body (first 800 chars):\n{body_text[:800]}")
        except Exception as e:
            print(f"Could not read body: {e}")

        await page.screenshot(path="debug.png", full_page=True)
        print("Screenshot → debug.png")

        print(f"\nCaptured {len(_api_responses)} API responses.")

        # ── API-first approach ─────────────────────────────────────────────────
        cancelled, review_branches = parse_api_notifications()
        print(f"API parse: {len(cancelled)} cancelled, {len(review_branches)} review notifications")

        # ── DOM fallback ───────────────────────────────────────────────────────
        reviews = []
        if not cancelled and not review_branches:
            print("\nFalling back to DOM scraping…")
            all_ts = await get_yesterday_timestamps(page)
            print(f"Found {len(all_ts)} 'Yesterday' timestamps: {all_ts}")

            processed_review_ts = set()
            for ts in all_ts:
                print(f"\nProcessing: {ts}")
                try:
                    if not await click_row_by_ts(page, ts):
                        print("  Could not click row")
                        continue
                    await page.wait_for_timeout(2500)
                    await dismiss_cookies(page)

                    popup = await get_popup_text(page)
                    print(f"  Popup ({len(popup)} chars): {popup[:200]}")
                    if not popup:
                        await close_popup(page)
                        continue

                    pl = popup.lower()
                    if any(k in pl for k in ["first order", "cancel", "rejected"]):
                        site = extract_site(popup)
                        print(f"  → Cancelled: {site}")
                        cancelled.append({"time": ts, "site": site, "brand": categorise(site)})
                        await close_popup(page)

                    elif any(k in pl for k in ["review", "rating", "feedback"]):
                        if ts not in processed_review_ts:
                            branches = await get_branch_links(page)
                            print(f"  → Reviews: {len(branches)} branches")
                            await close_popup(page)
                            processed_review_ts.add(ts)
                            for b in branches:
                                revs = await scrape_reviews_for_branch(page, b["branchId"], b["name"])
                                print(f"    {b['name']}: {len(revs)} reviews")
                                reviews.extend(revs)
                                await page.goto(NOTIF_URL, wait_until="domcontentloaded")
                                await page.wait_for_timeout(2000)
                                await dismiss_cookies(page)
                        else:
                            await close_popup(page)
                    else:
                        print("  → Unknown, skipping")
                        await close_popup(page)

                    await page.wait_for_timeout(800)

                except Exception as e:
                    print(f"  Error: {e}")
                    await close_popup(page)

        # ── If API found review branches, scrape them ──────────────────────────
        if review_branches:
            for b in review_branches:
                print(f"\nScraping reviews: {b['name']} (branchId={b['branchId']})")
                revs = await scrape_reviews_for_branch(page, b["branchId"], b["name"])
                print(f"  {len(revs)} reviews found")
                reviews.extend(revs)

        await browser.close()

    # ── Save ───────────────────────────────────────────────────────────────────
    output = {
        "scraped_date": str(date.today()),
        "target_date":  str(date.today() - timedelta(days=1)),
        "cancelled_orders": cancelled,
        "reviews": reviews,
    }
    OUT_FILE.write_text(json.dumps(output, indent=2))
    print(f"\n✓ Done: {len(cancelled)} cancelled, {len(reviews)} reviews → {OUT_FILE}")


if __name__ == "__main__":
    asyncio.run(main())
