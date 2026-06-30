"""
Deliveroo Partner Hub — Cloud Scraper v5
Direct API approach: hits /home for JWT refresh, then calls
/api-gw/notifications/employee/self/alerts with Bearer token.
No DOM scraping for notifications — bypasses React rendering entirely.
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

ORG_ID       = "513610"
BASE_URL     = "https://partner-hub.deliveroo.com"
HOME_URL     = f"{BASE_URL}/home?orgId={ORG_ID}"
NOTIF_API    = f"{BASE_URL}/api-gw/notifications/employee/self/alerts"
OUT_FILE     = Path(__file__).parent / "data.json"
COOKIES_JSON = os.environ.get("DELIVEROO_COOKIES", "")


def categorise(site):
    brands = ["Twisted London", "Kuro Smash", "Hot Chick", "Koreatown",
              "Lean Kitchen", "Bao Boys", "Dirty Bones"]
    for b in brands:
        if b.lower() in (site or "").lower():
            return b
    return "Other"


def extract_site_from_body(body_text):
    """Extract site name from cancelled-order notification body.
    Format: '• {SITE NAME}'s first order of the day was cancelled...'
    """
    m = re.search(r"[•·]\s*(.+?)'s first order", body_text)
    if m:
        return m.group(1).strip()
    m = re.search(r"([\w][\w\s\-]+?)\s+(?:first order|'s first)", body_text, re.IGNORECASE)
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


async def api_get(page, url, token):
    """Authenticated GET from within the browser context (inherits cookies)."""
    return await page.evaluate("""async ([url, token]) => {
        try {
            const resp = await fetch(url, {
                credentials: 'include',
                headers: { 'Authorization': 'Bearer ' + token }
            });
            const data = await resp.json();
            return { ok: resp.ok, status: resp.status, data };
        } catch(e) {
            return { ok: false, status: 0, error: e.message };
        }
    }""", [url, token])


async def scrape_reviews_for_branch(page, branch_id, site_name):
    url = f"{BASE_URL}/reviews?orgId={ORG_ID}&branchId={branch_id}&dateRangePreset=last_7_days"
    await page.goto(url, wait_until="domcontentloaded")
    await page.wait_for_timeout(4000)
    await dismiss_popups(page)

    # Expand all collapsed reviews
    for btn in await page.locator("text=expand_more").all():
        try:
            await btn.click(timeout=1000)
            await page.wait_for_timeout(200)
        except Exception:
            pass

    # Wait for star icons to appear (review cards loaded)
    try:
        await page.wait_for_selector('[data-testid="star-filled"]', timeout=8000)
    except PWTimeout:
        print(f"  No star ratings found — branch may have no reviews")
        return []

    # Extract reviews via JS using star icons + review text paragraph
    raw = await page.evaluate("""() => {
        const results = [];
        // Group star-filled icons by their parent element
        const allStars = Array.from(document.querySelectorAll('[data-testid="star-filled"]'));
        const groups = new Map();
        allStars.forEach(star => {
            const p = star.parentElement;
            if (!groups.has(p)) groups.set(p, 0);
            groups.set(p, groups.get(p) + 1);
        });

        groups.forEach((rating, starParent) => {
            // Walk up 4 levels to reach the card container
            let card = starParent;
            for (let i = 0; i < 4; i++) {
                if (card.parentElement) card = card.parentElement;
            }
            // Find review text: longest <p> that isn't a date
            const ps = Array.from(card.querySelectorAll('p'));
            const reviewP = ps.find(p => {
                const t = p.textContent.trim();
                return t.length > 20 && !/^\\d+(st|nd|rd|th)/.test(t);
            });
            const text = reviewP ? reviewP.textContent.trim() : '';
            if (rating >= 1 && rating <= 5 && text) {
                results.push({ rating, text });
            }
        });
        return results;
    }""")

    reviews = [
        {"rating": r["rating"], "text": r["text"],
         "site": site_name, "brand": categorise(site_name)}
        for r in raw
    ]
    print(f"  {len(reviews)} reviews extracted")
    return reviews


async def main():
    if not COOKIES_JSON:
        print("ERROR: DELIVEROO_COOKIES env var not set.")
        raise SystemExit(1)

    cookies = json.loads(COOKIES_JSON)
    print(f"Loaded {len(cookies)} cookies.")
    yesterday = str(date.today() - timedelta(days=1))
    print(f"Target date: {yesterday}")

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

        # ── Step 1: Load /home → triggers server-side JWT refresh ─────────────
        print("Loading /home to refresh JWT…")
        try:
            await page.goto(HOME_URL, wait_until="networkidle", timeout=20000)
        except Exception:
            await page.goto(HOME_URL, wait_until="domcontentloaded", timeout=15000)
        await page.wait_for_timeout(4000)
        await dismiss_popups(page)

        print(f"URL: {page.url}  |  Title: {await page.title()}")
        if "login" in page.url:
            print("ERROR: Redirected to login. Re-run extract_cookies.py and update secret.")
            await page.screenshot(path="debug.png", full_page=True)
            raise SystemExit(1)
        print("✓ Logged in.")

        # ── Step 2: Extract fresh token cookie (= Bearer JWT) ─────────────────
        all_cookies = await ctx.cookies(["https://partner-hub.deliveroo.com"])
        token = next((c["value"] for c in all_cookies if c["name"] == "token"), None)
        if not token:
            print("ERROR: No 'token' cookie found after home load.")
            raise SystemExit(1)
        print(f"✓ Token found ({len(token)} chars).")

        # ── Step 3: Fetch all notifications via direct API call ────────────────
        print("\nFetching notifications list via API…")
        result = await api_get(page, NOTIF_API, token)
        print(f"  Status: {result.get('status')}  ok={result.get('ok')}")

        if not result.get("ok"):
            print(f"  ERROR: {result}")
            await page.screenshot(path="debug.png", full_page=True)
            raise SystemExit(1)

        all_notifs = result["data"]
        if not isinstance(all_notifs, list):
            print(f"  Unexpected response shape: {str(all_notifs)[:200]}")
            all_notifs = []
        print(f"  Total notifications returned: {len(all_notifs)}")

        # Filter to yesterday only
        yesterday_notifs = [n for n in all_notifs if yesterday in n.get("timestamp", "")]
        print(f"  Notifications for {yesterday}: {len(yesterday_notifs)}")
        for n in yesterday_notifs:
            print(f"    [{n['timestamp']}] {n['title']}")

        # ── Step 4: Process each notification ──────────────────────────────────
        cancelled = []
        review_notif_ids = []

        for n in yesterday_notifs:
            title = n.get("title", "")
            if "cancelled" in title.lower() or "auto-rejected" in title.lower():
                # Fetch detail — site name is in the body field
                dr = await api_get(page, f"{NOTIF_API}/{n['id']}", token)
                if dr.get("ok"):
                    body_text = dr["data"].get("body", "")
                    site = extract_site_from_body(body_text)
                    print(f"  ✓ Cancelled: {site}")
                    cancelled.append({
                        "time": n["timestamp"],
                        "site": site,
                        "brand": categorise(site),
                    })
                else:
                    print(f"  Detail fetch failed for {n['id'][:8]}…: {dr}")
                    # Fallback: use body_short
                    site = extract_site_from_body(n.get("body_short", ""))
                    cancelled.append({
                        "time": n["timestamp"],
                        "site": site or "Unknown",
                        "brand": categorise(site or ""),
                    })
            elif "review" in title.lower():
                review_notif_ids.append(n["id"])

        print(f"\nCancelled orders: {len(cancelled)}")
        print(f"Review notifications: {len(review_notif_ids)}")

        # ── Step 5: Get branch IDs from review notification details ───────────
        review_branches = []
        seen_branches = set()

        for notif_id in review_notif_ids:
            dr = await api_get(page, f"{NOTIF_API}/{notif_id}", token)
            if not dr.get("ok"):
                continue
            detail = dr["data"]
            resources = detail.get("resources", [])
            actions   = detail.get("actions", [])

            # Each resource card = one branch, link contains branchId
            for r in resources:
                link = r.get("link", "")
                m = re.search(r"branchId[=\/](\d+)", link)
                if m and m.group(1) not in seen_branches:
                    seen_branches.add(m.group(1))
                    review_branches.append({"branchId": m.group(1), "name": r.get("title", "Unknown")})

            # Fallback: try actions
            if not resources:
                for a in actions:
                    link = a.get("link", "")
                    m = re.search(r"branchId[=\/](\d+)", link)
                    if m and m.group(1) not in seen_branches:
                        seen_branches.add(m.group(1))
                        review_branches.append({"branchId": m.group(1), "name": "Unknown"})

        print(f"Branch IDs for review scraping: {[b['branchId'] for b in review_branches]}")

        # ── Step 6: Scrape review text/ratings per branch ──────────────────────
        reviews = []
        for b in review_branches:
            print(f"\nScraping reviews: {b['name']} (branch {b['branchId']})…")
            revs = await scrape_reviews_for_branch(page, b["branchId"], b["name"])
            print(f"  {len(revs)} reviews found")
            reviews.extend(revs)

        await page.screenshot(path="debug.png", full_page=True)
        await browser.close()

    output = {
        "scraped_date": str(date.today()),
        "target_date":  yesterday,
        "cancelled_orders": cancelled,
        "reviews": reviews,
    }
    OUT_FILE.write_text(json.dumps(output, indent=2))
    print(f"\n✓ Done — {len(cancelled)} cancelled orders, {len(reviews)} reviews → data.json")


if __name__ == "__main__":
    asyncio.run(main())
