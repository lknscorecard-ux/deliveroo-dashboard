"""
Deliveroo Partner Hub — Cloud Scraper v8
100% API-based. No DOM scraping.

Flow:
  1. Load /home?orgId=513610              → refresh JWT cookie
  2. GET /api-gw/notifications/...        → yesterday's notifications (Bearer token)
  3. Cancelled: GET notification detail   → extract site name from body text
  4. Reviews:  GET notification detail    → resources[] each has title + link
               Extract branchId from link
               GET /api/restaurants/{branchId}/reviews  → rating_stars + rating_comment
"""

import asyncio
import json
import os
import re
from datetime import date, timedelta
from pathlib import Path

from playwright.async_api import async_playwright
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
    site_norm = (site or "").replace(" ", "").lower()
    for b in brands:
        if b.lower() in (site or "").lower() or b.replace(" ", "").lower() in site_norm:
            return b
    return "Other"


def extract_site_from_body(body_text):
    m = re.search(r"[•·]\s*(.+?)'s first order", body_text)
    if m:
        return m.group(1).strip()
    m = re.search(r"([\w][\w\s\-\!\(\)]+?)\s+first order", body_text, re.IGNORECASE)
    return m.group(1).strip() if m else "Unknown"


def new_review_count(subtitle_text):
    m = re.match(r"(\d+)\s+new review", subtitle_text or "")
    return int(m.group(1)) if m else 1


def branch_id_from_link(link):
    m = re.search(r"branchId=(\d+)", link or "")
    return m.group(1) if m else None


async def dismiss_popups(page):
    for selector in [
        "button:has-text('Continue without accepting')",
        "button:has-text('Close')",
        "[aria-label='Close']",
    ]:
        try:
            btn = page.locator(selector).first
            if await btn.is_visible(timeout=600):
                await btn.click()
                await page.wait_for_timeout(200)
        except Exception:
            pass


async def api_get(page, url, token):
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
        )
        await ctx.add_cookies(cookies)
        page = await ctx.new_page()
        await stealth_async(page)

        # ── 1. Load /home → refresh JWT ───────────────────────────────────────
        print("Loading /home to refresh JWT…")
        try:
            await page.goto(HOME_URL, wait_until="networkidle", timeout=20000)
        except Exception:
            await page.goto(HOME_URL, wait_until="domcontentloaded", timeout=15000)
        await page.wait_for_timeout(3000)
        await dismiss_popups(page)

        if "login" in page.url:
            print("ERROR: Redirected to login. Re-run extract_cookies.py.")
            await page.screenshot(path="debug.png", full_page=True)
            raise SystemExit(1)
        print("✓ Logged in.")

        # ── 2. Get Bearer token ───────────────────────────────────────────────
        all_cookies = await ctx.cookies(["https://partner-hub.deliveroo.com"])
        token = next((c["value"] for c in all_cookies if c["name"] == "token"), None)
        if not token:
            print("ERROR: No token cookie found.")
            raise SystemExit(1)
        print(f"✓ Token ({len(token)} chars).")

        # ── 3. Fetch ALL notifications (paginate until yesterday is fully covered) ──
        print("\nFetching notifications…")
        all_notifs = []
        url = f"{NOTIF_API}?limit=200"
        page_num = 0
        while True:
            page_num += 1
            result = await api_get(page, url, token)
            if not result.get("ok"):
                print(f"ERROR fetching notifications page {page_num}: {result}")
                break
            batch = result["data"] if isinstance(result["data"], list) else []
            if not batch:
                break
            all_notifs.extend(batch)
            print(f"  Page {page_num}: {len(batch)} notifications (total so far: {len(all_notifs)})")

            # Stop if oldest notification in this batch is before yesterday
            last_ts = batch[-1].get("timestamp", "")
            if last_ts and last_ts[:10] < yesterday:
                break
            # Stop if no yesterday notifications found in this batch
            yesterday_in_batch = [n for n in batch if yesterday in n.get("timestamp", "")]
            if not yesterday_in_batch and page_num > 1:
                break
            # Paginate using last notification ID
            last_id = batch[-1].get("id", "")
            if not last_id or len(batch) < 200:
                break
            url = f"{NOTIF_API}?limit=200&starting_after={last_id}"

        yesterday_notifs = [n for n in all_notifs if yesterday in n.get("timestamp", "")]
        print(f"Total fetched: {len(all_notifs)} | Yesterday: {len(yesterday_notifs)}")
        for n in yesterday_notifs:
            print(f"  [{n['timestamp']}] {n['title']}")

        # ── 4. Process notifications ──────────────────────────────────────────
        cancelled = []
        review_tasks = []       # {site, branch_id, url}
        seen_branch_ids = set() # deduplicate branches across multiple review notifications

        # Cancelled: only yesterday's notifications
        for n in yesterday_notifs:
            title = n.get("title", "")
            if "cancelled" in title.lower() or "auto-rejected" in title.lower():
                dr = await api_get(page, f"{NOTIF_API}/{n['id']}", token)
                if dr.get("ok"):
                    site = extract_site_from_body(dr["data"].get("body", ""))
                    print(f"  ✓ Cancelled: {site}")
                    cancelled.append({
                        "time": n["timestamp"],
                        "site": site,
                        "brand": categorise(site),
                    })

        # Reviews: look at "Customer reviews" notifications from yesterday OR today
        # (multiple orgs send separate batches; both days covers timezone edge cases)
        today = str(date.today())
        all_review_notifs = [
            n for n in all_notifs
            if "review" in n.get("title", "").lower()
            and (yesterday in n.get("timestamp", "") or today in n.get("timestamp", ""))
        ]
        print(f"\n  Found {len(all_review_notifs)} review notification(s) from {yesterday}/{today}")

        for n in all_review_notifs:
            dr = await api_get(page, f"{NOTIF_API}/{n['id']}", token)
            if not dr.get("ok"):
                continue
            resources = dr["data"].get("resources", [])
            ts = n.get("timestamp", "")
            print(f"  [{ts}] Review notification: {len(resources)} site(s)")
            for r in resources:
                link = r.get("link", "")
                branch_id = branch_id_from_link(link)
                site_name = r.get("title", "Unknown")
                if branch_id and branch_id not in seen_branch_ids:
                    seen_branch_ids.add(branch_id)
                    full_link = link if link.startswith("http") else f"{BASE_URL}{link}"
                    review_tasks.append({
                        "site": site_name,
                        "branch_id": branch_id,
                        "url": full_link,
                    })
                    print(f"    + {site_name} (branchId={branch_id})")

        print(f"\nCancelled: {len(cancelled)} | Review sites: {len(review_tasks)}")

        # ── 5. Fetch reviews via API ──────────────────────────────────────────
        # Warm up the /api/restaurants/ session by navigating to the first review page
        if review_tasks:
            warmup_url = review_tasks[0].get("url",
                f"{BASE_URL}/reviews?orgId=188047&branchId={review_tasks[0]['branch_id']}&dateRangePreset=last_7_days")
            print(f"\nWarming up session for reviews API…")
            try:
                await page.goto(warmup_url, wait_until="domcontentloaded", timeout=15000)
            except Exception:
                pass
            await page.wait_for_timeout(4000)
            await dismiss_popups(page)
            # Refresh token after navigation
            all_cookies = await ctx.cookies(["https://partner-hub.deliveroo.com"])
            token = next((c["value"] for c in all_cookies if c["name"] == "token"), token)
            print(f"  Session warmed up. Token: {len(token)} chars.")

        reviews = []
        for task in review_tasks:
            branch_id = task["branch_id"]
            site_name = task["site"]
            print(f"\nFetching reviews: {site_name} (branchId={branch_id})…")

            api_url = f"{BASE_URL}/api/restaurants/{branch_id}/reviews?stars=&sort_date=&starting_after="
            result = await api_get(page, api_url, token)

            # If HTML returned (session dropped), navigate to review page and retry
            if not result.get("ok") and "DOCTYPE" in str(result.get("error", "")):
                print(f"  Session lost — re-navigating and retrying…")
                review_url = task.get("url", f"{BASE_URL}/reviews?orgId=188047&branchId={branch_id}&dateRangePreset=last_7_days")
                try:
                    await page.goto(review_url, wait_until="domcontentloaded", timeout=15000)
                except Exception:
                    pass
                await page.wait_for_timeout(4000)
                await dismiss_popups(page)
                all_cookies = await ctx.cookies(["https://partner-hub.deliveroo.com"])
                token = next((c["value"] for c in all_cookies if c["name"] == "token"), token)
                result = await api_get(page, api_url, token)

            if not result.get("ok"):
                print(f"  ERROR {result.get('status')}: {result}")
                continue

            raw_reviews = result["data"].get("reviews", [])
            # Only keep reviews created on target date (yesterday)
            new_reviews = [r for r in raw_reviews if r.get("created_at", "")[:10] == yesterday]
            print(f"  API: {len(raw_reviews)} total, {len(new_reviews)} from {yesterday}")

            for rev in new_reviews:
                rating  = rev.get("rating_stars")
                comment = (rev.get("rating_comment") or "").strip()
                created = rev.get("created_at", "")
                print(f"  ★{rating}  [{created}]  {comment[:60]}")
                reviews.append({
                    "site":    site_name,
                    "brand":   categorise(site_name),
                    "rating":  rating,
                    "text":    comment,
                    "created": created,
                })

        await page.screenshot(path="debug.png", full_page=True)
        await browser.close()

    # Load existing history and append today's data
    history = {}
    if OUT_FILE.exists():
        try:
            history = json.loads(OUT_FILE.read_text())
            # Handle old single-day format: migrate it
            if "target_date" in history:
                old_date = history.get("target_date", "unknown")
                history = {old_date: history}
        except Exception:
            history = {}

    history[yesterday] = {
        "scraped_date":     str(date.today()),
        "cancelled_orders": cancelled,
        "reviews":          reviews,
    }
    OUT_FILE.write_text(json.dumps(history, indent=2))
    print(f"\n✓ Done — {len(cancelled)} cancelled, {len(reviews)} reviews → data.json")


if __name__ == "__main__":
    asyncio.run(main())
