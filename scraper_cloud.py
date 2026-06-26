"""
Deliveroo Partner Hub — Cloud Scraper (GitHub Actions edition)
Authenticates via cookies stored in DELIVEROO_COOKIES env var.
Saves output to data.json.
"""

import asyncio
import json
import os
import re
from datetime import date, timedelta
from pathlib import Path

from playwright.async_api import async_playwright, TimeoutError as PWTimeout

# ── Config ─────────────────────────────────────────────────────────────────────
ORG_ID       = "513610"
BASE_URL     = "https://partner-hub.deliveroo.com"
NOTIF_URL    = f"{BASE_URL}/notifications?back_url=%2Fhome&orgId={ORG_ID}"
OUTPUT_FILE  = Path(__file__).parent / "data.json"
COOKIES_JSON = os.environ.get("DELIVEROO_COOKIES", "")

# ── Helpers ────────────────────────────────────────────────────────────────────
async def dismiss_cookies(page):
    try:
        btn = page.locator("button", has_text="Continue without accepting")
        await btn.click(timeout=3000)
    except PWTimeout:
        pass


async def close_popup(page):
    try:
        await page.locator("button[aria-label='Close']").click(timeout=3000)
        await page.wait_for_timeout(600)
    except PWTimeout:
        # fallback: navigate away from notification param
        url = page.url
        if "notification=" in url:
            clean = re.sub(r"&?notification=[^&]+", "", url)
            await page.goto(clean, wait_until="domcontentloaded")
            await page.wait_for_timeout(1000)


def extract_site(popup_text):
    m = re.search(r"[•·]\s*(.+?)'s first order", popup_text)
    return m.group(1).strip() if m else "Unknown"


def categorise(site):
    brands = ["Twisted London", "Kuro Smash", "Hot Chick", "Koreatown",
              "Lean Kitchen", "Bao Boys", "Dirty Bones"]
    for b in brands:
        if b.lower() in site.lower():
            return b
    return "Other"


async def get_all_yesterday_timestamps(page):
    """Return all 'Yesterday at XX:XX' timestamps found in the notifications list."""
    return await page.evaluate("""() => {
        const main = document.querySelector('main');
        if (!main) return [];
        const all = Array.from(main.querySelectorAll('*'));
        const leaves = all.filter(e => e.childElementCount === 0);
        return leaves
            .map(e => e.textContent?.trim() || '')
            .filter(t => /^Yesterday at \\d{2}:\\d{2}$/.test(t));
    }""")


async def click_row_by_timestamp(page, ts):
    """Click the notification row matching the given timestamp."""
    clicked = await page.evaluate("""(ts) => {
        const main = document.querySelector('main');
        if (!main) return false;
        const all = Array.from(main.querySelectorAll('*')).filter(e => e.childElementCount === 0);
        const el = all.find(e => e.textContent.trim() === ts);
        if (!el) return false;
        let row = el;
        for (let i = 0; i < 6; i++) { if (row.parentElement && row.parentElement !== document.body) row = row.parentElement; }
        row.click();
        return true;
    }""", ts)
    return clicked


async def get_popup_text(page):
    """Wait for popup and return its full text."""
    try:
        await page.wait_for_selector("text=Notification details", timeout=6000)
    except PWTimeout:
        return ""
    return await page.evaluate("""() => {
        const h = Array.from(document.querySelectorAll('*'))
            .find(el => el.childElementCount === 0 && el.textContent.trim() === 'Notification details');
        if (!h) return '';
        let n = h;
        for (let i = 0; i < 6; i++) { if (n.parentElement) n = n.parentElement; }
        return n.innerText || n.textContent || '';
    }""")


async def get_branch_links(page):
    """Extract branchId links from an open review notification popup."""
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
    await page.wait_for_timeout(2500)
    await dismiss_cookies(page)

    # Expand truncated reviews
    for btn in await page.locator("text=expand_more").all():
        try:
            await btn.click(timeout=1000)
            await page.wait_for_timeout(300)
        except Exception:
            pass

    reviews = []
    try:
        text = await page.inner_text("main")
        lines = [l.strip() for l in text.splitlines() if l.strip()]
        for i, line in enumerate(lines):
            if re.match(r"^[1-5]$", line) and i + 1 < len(lines):
                next_line = lines[i + 1]
                # Skip lines that look like UI labels
                if len(next_line) > 5 and not re.match(r"^\d+$", next_line):
                    reviews.append({
                        "rating": int(line),
                        "text": next_line,
                        "site": site_name,
                        "brand": categorise(site_name),
                    })
    except Exception as e:
        print(f"    Error parsing reviews for {site_name}: {e}")

    return reviews


# ── Main ───────────────────────────────────────────────────────────────────────
async def main():
    if not COOKIES_JSON:
        print("ERROR: DELIVEROO_COOKIES env var not set.")
        raise SystemExit(1)

    cookies = json.loads(COOKIES_JSON)

    async with async_playwright() as pw:
        browser = await pw.chromium.launch(
            headless=True,
            args=["--no-sandbox", "--disable-dev-shm-usage", "--disable-gpu"],
        )
        ctx = await browser.new_context(
            viewport={"width": 1440, "height": 900},
            user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
        )
        await ctx.add_cookies(cookies)
        page = await ctx.new_page()

        # ── Load notifications ─────────────────────────────────────────────────
        print("Loading notifications page…")
        await page.goto(NOTIF_URL, wait_until="domcontentloaded")
        await page.wait_for_timeout(4000)
        await dismiss_cookies(page)

        # Check still logged in
        if "login" in page.url or "sign-in" in page.url or "auth" in page.url:
            print("ERROR: Cookies expired. Re-run extract_cookies.py and update the secret.")
            raise SystemExit(1)

        print(f"Page loaded: {page.url}")

        # ── Get all Yesterday timestamps ────────────────────────────────────────
        all_ts = await get_all_yesterday_timestamps(page)
        print(f"Found {len(all_ts)} 'Yesterday' notifications: {all_ts}")

        # ── Click each one, detect type from popup, scrape accordingly ─────────
        cancelled = []
        reviews = []
        processed_review_ts = set()  # avoid double-processing same review notification

        for ts in all_ts:
            print(f"\nProcessing: {ts}")
            try:
                if not await click_row_by_timestamp(page, ts):
                    print(f"  Could not click row for {ts}")
                    continue

                await page.wait_for_timeout(2000)
                await dismiss_cookies(page)

                popup_text = await get_popup_text(page)
                if not popup_text:
                    print(f"  No popup text found for {ts}")
                    await close_popup(page)
                    continue

                print(f"  Popup type detected from text length: {len(popup_text)} chars")

                # ── Determine notification type ────────────────────────────────
                if "first order" in popup_text.lower() or "cancelled" in popup_text.lower() or "auto-rejected" in popup_text.lower():
                    # Cancelled order
                    site = extract_site(popup_text)
                    print(f"  → Cancelled order: {site}")
                    cancelled.append({"time": ts, "site": site, "brand": categorise(site)})
                    await close_popup(page)

                elif "review" in popup_text.lower() or "branchId" in page.url or await page.locator("a[href*='branchId']").count() > 0:
                    # Customer reviews — get branch links before closing
                    if ts not in processed_review_ts:
                        branches = await get_branch_links(page)
                        print(f"  → Reviews notification: {len(branches)} branches")
                        await close_popup(page)
                        processed_review_ts.add(ts)

                        for branch in branches:
                            print(f"    Scraping reviews: {branch['name']}")
                            revs = await scrape_reviews_for_branch(page, branch["branchId"], branch["name"])
                            print(f"    Found {len(revs)} reviews")
                            reviews.extend(revs)
                            # Go back to notifications
                            await page.goto(NOTIF_URL, wait_until="domcontentloaded")
                            await page.wait_for_timeout(2000)
                            await dismiss_cookies(page)
                    else:
                        await close_popup(page)
                else:
                    print(f"  → Unknown type, skipping")
                    await close_popup(page)

                await page.wait_for_timeout(800)

            except Exception as e:
                print(f"  Error processing {ts}: {e}")
                await close_popup(page)
                await page.wait_for_timeout(500)

        await browser.close()

    # ── Save ───────────────────────────────────────────────────────────────────
    output = {
        "scraped_date": str(date.today()),
        "target_date":  str(date.today() - timedelta(days=1)),
        "cancelled_orders": cancelled,
        "reviews": reviews,
    }
    OUTPUT_FILE.write_text(json.dumps(output, indent=2))
    print(f"\n✓ Saved: {len(cancelled)} cancelled orders, {len(reviews)} reviews → {OUTPUT_FILE}")


if __name__ == "__main__":
    asyncio.run(main())
