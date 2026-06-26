"""
Deliveroo Partner Hub — Cloud Scraper v4
Cookie-based auth. Navigates to /home first to trigger server-side JWT refresh,
then scrapes yesterday's notifications.
Secret: DELIVEROO_COOKIES (extracted via extract_cookies.py)
"""

import asyncio
import json
import os
import re
from datetime import date, timedelta
from pathlib import Path

from playwright.async_api import async_playwright, TimeoutError as PWTimeout
from playwright_stealth import stealth_async

ORG_ID    = "513610"
BASE_URL  = "https://partner-hub.deliveroo.com"
HOME_URL  = f"{BASE_URL}/home?orgId={ORG_ID}"
NOTIF_URL = f"{BASE_URL}/notifications?back_url=%2Fhome&orgId={ORG_ID}"
OUT_FILE  = Path(__file__).parent / "data.json"
COOKIES_JSON = os.environ.get("DELIVEROO_COOKIES", "")

_api_responses = []


async def capture_response(response):
    url = response.url
    if any(k in url.lower() for k in ["notification", "review", "activity", "feed", "alert"]):
        try:
            if "json" in response.headers.get("content-type", ""):
                body = await response.json()
                print(f"[NET] {response.status} {url[:120]}")
                _api_responses.append({"url": url, "status": response.status, "body": body})
        except Exception:
            pass


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
    for selector in [
        "button:has-text('Close')",
        "button:has-text('Cancel')",
        "button:has-text('Continue without accepting')",
        "[aria-label='Close']",
    ]:
        try:
            btn = page.locator(selector).first
            if await btn.is_visible(timeout=1000):
                await btn.click()
                await page.wait_for_timeout(400)
        except Exception:
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


def parse_api_notifications():
    yesterday = str(date.today() - timedelta(days=1))
    cancelled = []
    review_branches = []
    for item in _api_responses:
        body = item["body"]
        notifications = []
        if isinstance(body, list):
            notifications = body
        elif isinstance(body, dict):
            for key in ["notifications", "items", "data", "results", "feed", "alerts"]:
                if key in body and isinstance(body[key], list):
                    notifications = body[key]
                    break
        for n in notifications:
            if not isinstance(n, dict):
                continue
            created = str(n.get("created_at", n.get("date", n.get("timestamp", ""))))
            if yesterday not in created:
                continue
            ntype = str(n.get("type", n.get("category", ""))).lower()
            text  = str(n.get("body", n.get("message", n.get("text", "")))).lower()
            title = str(n.get("title", n.get("subject", ""))).lower()
            if any(k in ntype or k in text or k in title for k in ["cancel", "reject", "first order"]):
                site = (n.get("site_name") or n.get("restaurant_name") or
                        n.get("branch_name") or extract_site(text) or "Unknown")
                cancelled.append({"time": created, "site": site, "brand": categorise(site)})
                print(f"[API] Cancelled: {site}")
            elif any(k in ntype or k in text or k in title for k in ["review", "rating", "feedback"]):
                branch_id = str(n.get("branch_id") or n.get("branchId") or "")
                site = (n.get("site_name") or n.get("restaurant_name") or n.get("branch_name") or "Unknown")
                review_branches.append({"branchId": branch_id, "name": site})
                print(f"[API] Review: {site}")
    return cancelled, review_branches


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
                seen.add(t); results.push(t);
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
    await dismiss_popups(page)
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
                        "rating": int(line), "text": nxt,
                        "site": site_name, "brand": categorise(site_name),
                    })
    except Exception as e:
        print(f"  Review parse error: {e}")
    return reviews


async def main():
    if not COOKIES_JSON:
        print("ERROR: DELIVEROO_COOKIES env var not set.")
        raise SystemExit(1)

    cookies = json.loads(COOKIES_JSON)
    print(f"Loaded {len(cookies)} cookies.")

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
            permissions=[],
        )
        await ctx.add_cookies(cookies)
        page = await ctx.new_page()
        await stealth_async(page)
        page.on("response", capture_response)

        # ── Step 1: Hit /home first — triggers server-side JWT refresh ─────────
        print(f"Loading home page to refresh JWT…")
        try:
            await page.goto(HOME_URL, wait_until="networkidle", timeout=20000)
        except Exception:
            await page.goto(HOME_URL, wait_until="domcontentloaded", timeout=15000)
        await page.wait_for_timeout(4000)
        await dismiss_popups(page)

        print(f"Home URL: {page.url}")
        print(f"Title   : {await page.title()}")

        if "login" in page.url:
            print("ERROR: Redirected to login after home load.")
            print("Re-run extract_cookies.py locally and update DELIVEROO_COOKIES secret.")
            await page.screenshot(path="debug.png", full_page=True)
            raise SystemExit(1)

        print("✓ Logged in via cookies.")

        # ── Step 2: Navigate to notifications ─────────────────────────────────
        print(f"\nNavigating to notifications…")
        await page.goto(NOTIF_URL, wait_until="domcontentloaded")
        await page.wait_for_timeout(3000)
        await dismiss_popups(page)

        # Wait for notification list to actually render
        print("Waiting for notification list to render…")
        try:
            await page.wait_for_selector("main", timeout=10000)
            # Wait for at least one notification row (has a timestamp-like text)
            await page.wait_for_function(
                """() => {
                    const main = document.querySelector('main');
                    if (!main) return false;
                    const text = main.innerText || '';
                    return text.includes('Yesterday') || text.includes('ago') || text.includes('/2026');
                }""",
                timeout=15000
            )
            print("Notification list rendered.")
        except PWTimeout:
            print("Timed out waiting for notifications — list may be empty.")

        await page.wait_for_timeout(2000)
        print(f"Notifications URL: {page.url}")
        body_text = await page.inner_text("body")
        print(f"Page text (first 800):\n{body_text[:800]}")
        await page.screenshot(path="debug.png", full_page=True)

        print(f"\nAPI responses captured: {len(_api_responses)}")

        # ── Step 3: API-first parse ────────────────────────────────────────────
        cancelled, review_branches = parse_api_notifications()
        print(f"API: {len(cancelled)} cancelled, {len(review_branches)} review notifications")

        # ── Step 4: DOM fallback ───────────────────────────────────────────────
        reviews = []
        if not cancelled and not review_branches:
            print("\nDOM fallback…")
            all_ts = await get_yesterday_timestamps(page)
            print(f"'Yesterday' timestamps: {all_ts}")

            processed_review_ts = set()
            for ts in all_ts:
                print(f"\n→ {ts}")
                try:
                    if not await click_row_by_ts(page, ts):
                        print("  Could not click")
                        continue
                    await page.wait_for_timeout(2500)
                    await dismiss_popups(page)
                    popup = await get_popup_text(page)
                    print(f"  Popup ({len(popup)} chars): {popup[:200]}")
                    if not popup:
                        await close_popup(page)
                        continue
                    pl = popup.lower()
                    if any(k in pl for k in ["first order", "cancel", "rejected"]):
                        site = extract_site(popup)
                        print(f"  Cancelled: {site}")
                        cancelled.append({"time": ts, "site": site, "brand": categorise(site)})
                        await close_popup(page)
                    elif any(k in pl for k in ["review", "rating", "feedback"]):
                        if ts not in processed_review_ts:
                            branches = await get_branch_links(page)
                            print(f"  Reviews: {len(branches)} branches")
                            await close_popup(page)
                            processed_review_ts.add(ts)
                            for b in branches:
                                revs = await scrape_reviews_for_branch(page, b["branchId"], b["name"])
                                print(f"  {b['name']}: {len(revs)} reviews")
                                reviews.extend(revs)
                                await page.goto(NOTIF_URL, wait_until="domcontentloaded")
                                await page.wait_for_timeout(2000)
                                await dismiss_popups(page)
                        else:
                            await close_popup(page)
                    else:
                        await close_popup(page)
                    await page.wait_for_timeout(800)
                except Exception as e:
                    print(f"  Error: {e}")
                    await close_popup(page)

        if review_branches:
            for b in review_branches:
                revs = await scrape_reviews_for_branch(page, b["branchId"], b["name"])
                print(f"Reviews {b['name']}: {len(revs)}")
                reviews.extend(revs)

        await browser.close()

    output = {
        "scraped_date": str(date.today()),
        "target_date":  str(date.today() - timedelta(days=1)),
        "cancelled_orders": cancelled,
        "reviews": reviews,
    }
    OUT_FILE.write_text(json.dumps(output, indent=2))
    print(f"\n✓ Done: {len(cancelled)} cancelled, {len(reviews)} reviews")


if __name__ == "__main__":
    asyncio.run(main())
