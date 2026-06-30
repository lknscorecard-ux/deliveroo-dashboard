"""
Deliveroo Partner Hub — Cloud Scraper v6
Flow:
  1. Load /home?orgId=513610  → JWT refresh
  2. Call /api-gw/notifications/employee/self/alerts  → yesterday's notifications
  3. Cancelled orders  → fetch detail → extract site name from body text
  4. Customer reviews  → fetch detail → iterate resources[] (each = one site)
                        each resource has: title, subtitle.text ("X new review"),
                        link (/reviews?orgId=188047&branchId=XXXXX&...)
  5. Navigate to each review link → wait for star icons → scrape rating + text
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
              "Lean Kitchen", "Bao Boys", "Dirty Bones", "WTF", "Wing Fest",
              "Protein Pizza"]
    for b in brands:
        if b.lower() in (site or "").lower():
            return b
    return "Other"


def extract_site_from_body(body_text):
    """Extract site name from: '• {SITE NAME}'s first order of the day was cancelled...'"""
    m = re.search(r"[•·]\s*(.+?)'s first order", body_text)
    if m:
        return m.group(1).strip()
    m = re.search(r"([\w][\w\s\-\!\(\)]+?)\s+first order", body_text, re.IGNORECASE)
    return m.group(1).strip() if m else "Unknown"


def new_review_count(subtitle_text):
    """Parse '2 new reviews' → 2, '1 new review' → 1"""
    m = re.match(r"(\d+)\s+new review", subtitle_text or "")
    return int(m.group(1)) if m else 1


async def dismiss_popups(page):
    for selector in [
        "button:has-text('Continue without accepting')",
        "button:has-text('Close')",
        "button:has-text('Cancel')",
        "[aria-label='Close']",
    ]:
        try:
            btn = page.locator(selector).first
            if await btn.is_visible(timeout=800):
                await btn.click()
                await page.wait_for_timeout(300)
        except Exception:
            pass


async def api_get(page, url, token):
    """Authenticated GET from within the browser context."""
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


async def scrape_reviews_page(page, review_url, site_name, count):
    """
    Navigate to a branch reviews page and scrape the `count` most recent reviews.
    Star rating = number of [data-testid="star-filled"] icons per review card.
    Review text = longest <p> in the card that isn't a date.
    """
    await page.goto(review_url, wait_until="domcontentloaded")
    await page.wait_for_timeout(5000)
    await dismiss_popups(page)

    # Wait for star icons to appear
    try:
        await page.wait_for_selector('[data-testid="star-filled"]', timeout=10000)
    except PWTimeout:
        print(f"  No stars found for {site_name} — page may not have rendered")
        # Debug: print page text snippet
        try:
            txt = await page.inner_text("main")
            print(f"  Page text (first 300): {txt[:300]}")
        except Exception:
            pass
        return []

    # Extract review cards via JS
    raw = await page.evaluate("""(count) => {
        const results = [];
        // Group star-filled icons by their direct parent element
        const allStars = Array.from(document.querySelectorAll('[data-testid="star-filled"]'));
        const groups = new Map();
        allStars.forEach(star => {
            const p = star.parentElement;
            if (!groups.has(p)) groups.set(p, 0);
            groups.set(p, groups.get(p) + 1);
        });

        // For each group, walk up to the card and get review text
        const entries = Array.from(groups.entries());
        // Take only the first `count` cards (most recent reviews)
        for (let i = 0; i < Math.min(count, entries.length); i++) {
            const [starParent, rating] = entries[i];
            let card = starParent;
            for (let j = 0; j < 4; j++) {
                if (card.parentElement) card = card.parentElement;
            }
            // Find the review text: longest <p> that isn't a date like "28th Jun 2026"
            const ps = Array.from(card.querySelectorAll('p'));
            const reviewP = ps.find(p => {
                const t = p.textContent.trim();
                return t.length > 5 && !/^\\d+(st|nd|rd|th)\\s+\\w+\\s+\\d{4}/.test(t);
            });
            const text = reviewP ? reviewP.textContent.trim() : '';
            if (rating >= 1 && rating <= 5) {
                results.push({ rating, text });
            }
        }
        return results;
    }""", count)

    reviews = [
        {
            "rating": r["rating"],
            "text": r["text"],
            "site": site_name,
            "brand": categorise(site_name),
        }
        for r in raw
    ]
    print(f"  {len(reviews)} review(s) scraped")
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

        # ── Step 1: Load /home → JWT refresh ──────────────────────────────────
        print("Loading /home to refresh JWT…")
        try:
            await page.goto(HOME_URL, wait_until="networkidle", timeout=20000)
        except Exception:
            await page.goto(HOME_URL, wait_until="domcontentloaded", timeout=15000)
        await page.wait_for_timeout(4000)
        await dismiss_popups(page)

        print(f"URL: {page.url}")
        if "login" in page.url:
            print("ERROR: Redirected to login. Re-run extract_cookies.py.")
            await page.screenshot(path="debug.png", full_page=True)
            raise SystemExit(1)
        print("✓ Logged in.")

        # ── Step 2: Get fresh token cookie ────────────────────────────────────
        all_cookies = await ctx.cookies(["https://partner-hub.deliveroo.com"])
        token = next((c["value"] for c in all_cookies if c["name"] == "token"), None)
        if not token:
            print("ERROR: No token cookie found.")
            raise SystemExit(1)
        print(f"✓ Token found ({len(token)} chars).")

        # ── Step 3: Fetch all notifications ───────────────────────────────────
        print("\nFetching notifications…")
        result = await api_get(page, NOTIF_API, token)
        if not result.get("ok"):
            print(f"ERROR: {result}")
            raise SystemExit(1)

        all_notifs = result["data"] if isinstance(result["data"], list) else []
        print(f"Total: {len(all_notifs)}  |  filtering to {yesterday}…")

        yesterday_notifs = [n for n in all_notifs if yesterday in n.get("timestamp", "")]
        print(f"Yesterday: {len(yesterday_notifs)} notification(s)")
        for n in yesterday_notifs:
            print(f"  [{n['timestamp']}] {n['title']}")

        # ── Step 4: Process notifications ─────────────────────────────────────
        cancelled = []
        review_sites = []   # list of {site_name, url, count}

        for n in yesterday_notifs:
            title = n.get("title", "")

            if "cancelled" in title.lower() or "auto-rejected" in title.lower():
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

            elif "review" in title.lower():
                dr = await api_get(page, f"{NOTIF_API}/{n['id']}", token)
                if not dr.get("ok"):
                    continue
                detail = dr["data"]
                resources = detail.get("resources", [])
                print(f"  Review notification: {len(resources)} site(s)")
                for r in resources:
                    link = r.get("link", "")
                    site_name = r.get("title", "Unknown")
                    count = new_review_count(r.get("subtitle", {}).get("text", "1 new review"))
                    if link:
                        full_link = link if link.startswith("http") else f"{BASE_URL}{link}"
                        review_sites.append({
                            "site": site_name,
                            "url": full_link,
                            "count": count,
                        })
                        print(f"    {site_name}: {count} new review(s)")

        print(f"\nCancelled orders: {len(cancelled)}")
        print(f"Sites to scrape reviews: {len(review_sites)}")

        # ── Step 5: Scrape reviews for each site ──────────────────────────────
        reviews = []
        for s in review_sites:
            print(f"\nScraping: {s['site']} ({s['count']} new review(s))…")
            print(f"  URL: {s['url']}")
            revs = await scrape_reviews_page(page, s["url"], s["site"], s["count"])
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
    print(f"\n✓ Done — {len(cancelled)} cancelled, {len(reviews)} reviews → data.json")


if __name__ == "__main__":
    asyncio.run(main())
