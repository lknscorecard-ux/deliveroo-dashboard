"""
Deliveroo Partner Hub — Cloud Scraper (GitHub Actions edition)
Authenticates via cookies stored in DELIVEROO_COOKIES env var.
Saves output to data.json.

Set env var before running locally:
    set DELIVEROO_COOKIES=<paste JSON from extract_cookies.py>
    python scraper_cloud.py
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
        pass


def extract_site(popup_text):
    m = re.search(r"•\s+(.+?)'s first order", popup_text)
    return m.group(1).strip() if m else "Unknown"


def categorise(site):
    brands = ["Twisted London", "Kuro Smash", "Hot Chick", "Koreatown",
              "Lean Kitchen", "Bao Boys", "Dirty Bones"]
    for b in brands:
        if b.lower() in site.lower():
            return b
    return "Other"


async def click_row_by_timestamp(page, ts):
    return await page.evaluate("""(ts) => {
        const main = document.querySelector('main');
        if (!main) return false;
        const leaves = Array.from(main.querySelectorAll('*')).filter(e => e.childElementCount === 0);
        const el = leaves.find(e => e.textContent.trim() === ts);
        if (!el) return false;
        let row = el;
        for (let i = 0; i < 5; i++) { if (row.parentElement) row = row.parentElement; }
        row.click();
        return true;
    }""", ts)


async def get_popup_text(page):
    await page.wait_for_selector("text=Notification details", timeout=6000)
    return await page.evaluate("""() => {
        const h = Array.from(document.querySelectorAll('*'))
            .find(el => el.childElementCount === 0 && el.textContent.trim() === 'Notification details');
        if (!h) return '';
        let n = h;
        for (let i = 0; i < 5; i++) { if (n.parentElement) n = n.parentElement; }
        return n.innerText;
    }""")


async def get_branch_links(page):
    return await page.evaluate("""() =>
        Array.from(document.querySelectorAll('a[href*="branchId"]'))
            .map(a => {
                const m = a.href.match(/branchId=(\\d+)/);
                return m ? { name: a.innerText.trim(), branchId: m[1] } : null;
            }).filter(Boolean)
    """)


async def scrape_reviews_for_branch(page, branch_id, site_name):
    url = f"{BASE_URL}/reviews?orgId={ORG_ID}&branchId={branch_id}&dateRangePreset=last_7_days"
    await page.goto(url, wait_until="domcontentloaded")
    await page.wait_for_timeout(2000)
    await dismiss_cookies(page)

    # Expand truncated reviews
    for btn in await page.locator("text=expand_more").all():
        try:
            await btn.click(timeout=1000)
            await page.wait_for_timeout(300)
        except Exception:
            pass

    reviews = []
    lines = [l.strip() for l in (await page.inner_text("main")).splitlines() if l.strip()]
    for i, line in enumerate(lines):
        if re.match(r"^[1-5]$", line) and i + 1 < len(lines):
            reviews.append({
                "rating": int(line),
                "text": lines[i + 1],
                "site": site_name,
                "brand": categorise(site_name),
            })
    return reviews


# ── Main ───────────────────────────────────────────────────────────────────────
async def main():
    if not COOKIES_JSON:
        print("ERROR: DELIVEROO_COOKIES env var not set.")
        print("Run extract_cookies.py locally to get the cookie JSON.")
        raise SystemExit(1)

    cookies = json.loads(COOKIES_JSON)

    async with async_playwright() as pw:
        browser = await pw.chromium.launch(
            headless=True,
            args=["--no-sandbox", "--disable-dev-shm-usage"],
        )
        ctx = await browser.new_context(
            viewport={"width": 1400, "height": 800},
            user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/124 Safari/537.36",
        )
        await ctx.add_cookies(cookies)
        page = await ctx.new_page()

        # ── Load notifications ─────────────────────────────────────────────────
        print("Loading notifications…")
        await page.goto(NOTIF_URL, wait_until="domcontentloaded")
        await page.wait_for_timeout(3000)
        await dismiss_cookies(page)

        # Check still logged in
        if "login" in page.url or "sign-in" in page.url:
            print("ERROR: Cookies expired. Re-run extract_cookies.py and update the secret.")
            raise SystemExit(1)

        # ── Detect yesterday timestamps ────────────────────────────────────────
        timestamps = await page.evaluate("""() => {
            const main = document.querySelector('main');
            const leaves = Array.from(main.querySelectorAll('*')).filter(e => e.childElementCount === 0);
            return leaves
                .filter(e => e.textContent.trim().startsWith('Yesterday'))
                .map(e => {
                    const parent = e.parentElement;
                    const sibs = parent ? Array.from(parent.children) : [];
                    const title = sibs.map(s => s.innerText?.trim()).find(t => t && t !== e.textContent.trim()) || '';
                    return { time: e.textContent.trim(), title };
                });
        }""")

        cancelled_ts = [t["time"] for t in timestamps if "cancelled" in t["title"].lower() or "auto-rejected" in t["title"].lower()]
        review_ts    = [t["time"] for t in timestamps if "review" in t["title"].lower()]
        print(f"Found {len(cancelled_ts)} cancelled, {len(review_ts)} review notifications")

        # ── Scrape cancelled orders ────────────────────────────────────────────
        cancelled = []
        for ts in cancelled_ts:
            try:
                if not await click_row_by_timestamp(page, ts):
                    continue
                await page.wait_for_timeout(1500)
                await dismiss_cookies(page)
                text = await get_popup_text(page)
                site = extract_site(text)
                cancelled.append({"time": ts, "site": site, "brand": categorise(site)})
                print(f"  Cancelled: {ts} → {site}")
                await close_popup(page)
                await page.wait_for_timeout(500)
            except Exception as e:
                print(f"  Error {ts}: {e}")
                await close_popup(page)

        # ── Scrape reviews ─────────────────────────────────────────────────────
        reviews = []
        for ts in review_ts:
            try:
                if not await click_row_by_timestamp(page, ts):
                    continue
                await page.wait_for_timeout(1500)
                await dismiss_cookies(page)
                branches = await get_branch_links(page)
                await close_popup(page)

                for branch in branches:
                    print(f"  Review: {branch['name']}")
                    revs = await scrape_reviews_for_branch(page, branch["branchId"], branch["name"])
                    reviews.extend(revs)
                    await page.go_back(wait_until="domcontentloaded")
                    await page.wait_for_timeout(1000)
                    await dismiss_cookies(page)
            except Exception as e:
                print(f"  Error {ts}: {e}")
                await close_popup(page)

        await browser.close()

    # ── Save ───────────────────────────────────────────────────────────────────
    output = {
        "scraped_date": str(date.today()),
        "target_date":  str(date.today() - timedelta(days=1)),
        "cancelled_orders": cancelled,
        "reviews": reviews,
    }
    OUTPUT_FILE.write_text(json.dumps(output, indent=2))
    print(f"\nSaved {len(cancelled)} cancelled + {len(reviews)} reviews → {OUTPUT_FILE}")


if __name__ == "__main__":
    asyncio.run(main())
