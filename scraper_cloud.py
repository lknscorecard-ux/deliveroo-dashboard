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
REVIEW_ORGS  = ["188047", "400890"]   # orgs that send review + some cancelled notifications
BASE_URL     = "https://partner-hub.deliveroo.com"
HOME_URL     = f"{BASE_URL}/home?orgId={ORG_ID}"
NOTIF_API    = f"{BASE_URL}/api-gw/notifications/employee/self/alerts"
OUT_FILE           = Path(__file__).parent / "data.json"
DELIVEROO_TOKEN    = os.environ.get("DELIVEROO_TOKEN", "")   # JWT from token cookie
DELIVEROO_EMAIL    = os.environ.get("DELIVEROO_EMAIL", "")
DELIVEROO_PASSWORD = os.environ.get("DELIVEROO_PASSWORD", "")


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


async def inject_token(ctx, token):
    """Inject JWT as the token cookie — no login flow needed."""
    await ctx.add_cookies([{
        "name":     "token",
        "value":    token,
        "domain":   "partner-hub.deliveroo.com",
        "path":     "/",
        "httpOnly": True,
        "secure":   True,
        "sameSite": "Lax",
    }])


async def login(page):
    """Log in to Deliveroo Partner Hub using email + password."""
    print("Navigating to login…")
    await page.goto("https://partner-hub.deliveroo.com/login", wait_until="domcontentloaded", timeout=20000)
    await page.wait_for_timeout(3000)

    # Dismiss cookie banner first
    await dismiss_popups(page)
    for btn_text in ["Continue without accepting", "Accept all", "Reject all"]:
        try:
            btn = page.locator(f"button:has-text('{btn_text}')").first
            if await btn.is_visible(timeout=1000):
                await btn.click()
                await page.wait_for_timeout(500)
                break
        except Exception:
            pass

    print(f"  URL: {page.url}")

    # Fill email — use press_sequentially to fire React onChange events
    try:
        email_input = page.locator('input[type="email"]').first
        await email_input.wait_for(timeout=8000)
        await email_input.click()
        await email_input.press_sequentially(DELIVEROO_EMAIL, delay=60)
        print(f"  ✓ Email typed")
        await page.wait_for_timeout(600)
    except Exception as e:
        print(f"  ERROR: Email field not found: {e}")
        await page.screenshot(path="debug.png", full_page=True)
        return False

    # Fill password — same approach
    try:
        pw_input = page.locator('input[type="password"]').first
        await pw_input.wait_for(timeout=5000)
        await pw_input.click()
        await pw_input.press_sequentially(DELIVEROO_PASSWORD, delay=60)
        print(f"  ✓ Password typed")
        await page.wait_for_timeout(600)
    except Exception as e:
        print(f"  ERROR: Password field not found: {e}")
        await page.screenshot(path="debug.png", full_page=True)
        return False

    # Screenshot before submit so we can see form state if login fails
    await page.screenshot(path="debug.png", full_page=True)
    print("  ✓ Pre-submit screenshot saved")

    # Click the Log in submit button (scoped to form, not cookie banner)
    clicked = False
    try:
        login_btn = page.locator('form button[type="submit"]').first
        if await login_btn.is_visible(timeout=2000):
            await login_btn.click()
            clicked = True
            print("  ✓ Login button clicked (form submit)")
    except Exception:
        pass

    if not clicked:
        try:
            login_btn = page.locator('button:has-text("Log in")').first
            if await login_btn.is_visible(timeout=2000):
                await login_btn.click()
                clicked = True
                print("  ✓ Login button clicked (text match)")
        except Exception:
            pass

    if not clicked:
        await pw_input.press("Enter")
        print("  ✓ Pressed Enter on password field")

    # Wait for navigation away from /login
    try:
        await page.wait_for_url(lambda u: "/login" not in u, timeout=25000)
    except Exception:
        pass
    await page.wait_for_timeout(4000)
    await dismiss_popups(page)
    await page.screenshot(path="debug.png", full_page=True)

    if "/login" in page.url:
        print(f"  ERROR: Login failed. Still on: {page.url}")
        return False

    print(f"✓ Logged in. URL: {page.url}")
    return True


async def main():
    if not DELIVEROO_TOKEN and not (DELIVEROO_EMAIL and DELIVEROO_PASSWORD):
        print("ERROR: Set DELIVEROO_TOKEN (preferred) or DELIVEROO_EMAIL + DELIVEROO_PASSWORD.")
        raise SystemExit(1)

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
        page = await ctx.new_page()
        await stealth_async(page)

        # ── 1. Auth ───────────────────────────────────────────────────────────
        if DELIVEROO_TOKEN:
            print("Using DELIVEROO_TOKEN (cookie injection)…")
            await inject_token(ctx, DELIVEROO_TOKEN)
            try:
                await page.goto(HOME_URL, wait_until="networkidle", timeout=20000)
            except Exception:
                await page.goto(HOME_URL, wait_until="domcontentloaded", timeout=15000)
            await page.wait_for_timeout(3000)
            await dismiss_popups(page)
            if "login" in page.url:
                print("ERROR: Token expired/invalid. Update DELIVEROO_TOKEN secret.")
                await page.screenshot(path="debug.png", full_page=True)
                raise SystemExit(1)
            print(f"✓ Authenticated via token. URL: {page.url}")
        else:
            print("Falling back to email/password login…")
            if not await login(page):
                raise SystemExit(1)
            try:
                await page.goto(HOME_URL, wait_until="networkidle", timeout=20000)
            except Exception:
                await page.goto(HOME_URL, wait_until="domcontentloaded", timeout=15000)
            await page.wait_for_timeout(3000)
            await dismiss_popups(page)
        print(f"✓ On home page.")

        # ── 2. Get Bearer token ───────────────────────────────────────────────
        all_cookies = await ctx.cookies(["https://partner-hub.deliveroo.com"])
        token = next((c["value"] for c in all_cookies if c["name"] == "token"), None)
        if not token:
            print("ERROR: No token cookie found.")
            raise SystemExit(1)
        print(f"✓ Token ({len(token)} chars).")

        # ── 3. Fetch ALL notifications from all org contexts ──────────────────
        async def fetch_notifs_for_org(org_id, pg, tok):
            """Navigate to org, fetch all notifications until 2 days ago."""
            org_home = f"{BASE_URL}/home?orgId={org_id}"
            try:
                await pg.goto(org_home, wait_until="domcontentloaded", timeout=15000)
            except Exception:
                pass
            await pg.wait_for_timeout(2000)
            await dismiss_popups(pg)

            two_days_ago = str(date.today() - timedelta(days=2))
            notifs = []
            fetch_url = f"{NOTIF_API}?limit=200"
            pg_num = 0
            while True:
                pg_num += 1
                r = await api_get(pg, fetch_url, tok)
                if not r.get("ok"):
                    print(f"    ERROR org {org_id} page {pg_num}: {r}")
                    break
                batch = r["data"] if isinstance(r["data"], list) else []
                if not batch:
                    break
                notifs.extend(batch)
                oldest_ts = batch[-1].get("timestamp", "")
                print(f"    org {org_id} page {pg_num}: {len(batch)} notifs, oldest: {oldest_ts}")
                if oldest_ts and oldest_ts[:10] < two_days_ago:
                    break
                last = batch[-1]
                cursor = (last.get("id") or last.get("_id") or last.get("notificationId")
                          or last.get("alertId") or last.get("notification_id") or "")
                if not cursor or len(batch) < 200:
                    break
                fetch_url = f"{NOTIF_API}?limit=200&starting_after={cursor}"
            return notifs

        print("\nFetching notifications across all orgs…")
        seen_notif_ids = set()
        all_notifs = []

        for org in [ORG_ID] + REVIEW_ORGS:
            print(f"  Fetching org {org}…")
            org_notifs = await fetch_notifs_for_org(org, page, token)
            added = 0
            for n in org_notifs:
                nid = n.get("id") or n.get("_id") or ""
                key = nid or f"{n.get('timestamp','')}-{n.get('title','')}"
                if key not in seen_notif_ids:
                    seen_notif_ids.add(key)
                    all_notifs.append(n)
                    added += 1
            print(f"  → {added} new unique notifications from org {org}")

        # Return to main org context and refresh token
        try:
            await page.goto(HOME_URL, wait_until="domcontentloaded", timeout=15000)
        except Exception:
            pass
        await page.wait_for_timeout(2000)
        all_cookies = await ctx.cookies(["https://partner-hub.deliveroo.com"])
        token = next((c["value"] for c in all_cookies if c["name"] == "token"), token)

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
                subtitle = r.get("subtitle", {})
                subtitle_text = subtitle.get("text", "") if isinstance(subtitle, dict) else str(subtitle)
                count = new_review_count(subtitle_text) or 1
                if branch_id and branch_id not in seen_branch_ids:
                    seen_branch_ids.add(branch_id)
                    full_link = link if link.startswith("http") else f"{BASE_URL}{link}"
                    review_tasks.append({
                        "site": site_name,
                        "branch_id": branch_id,
                        "url": full_link,
                        "count": count,
                    })
                    print(f"    + {site_name} (branchId={branch_id}, new={count})")

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
            needed   = task.get("count", 1)   # how many new reviews the notification reported
            print(f"\nFetching reviews: {site_name} (branchId={branch_id}, need={needed})…")

            # Collect exactly `needed` most-recent reviews, paginating if necessary.
            # No date filter — we trust the notification count over UTC date matching.
            collected = []
            starting_after = ""
            page_num = 0
            while len(collected) < needed:
                page_num += 1
                api_url = (f"{BASE_URL}/api/restaurants/{branch_id}/reviews"
                           f"?stars=&sort_date=&starting_after={starting_after}")
                result = await api_get(page, api_url, token)

                # Session-drop recovery
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
                    break

                page_reviews = result["data"].get("reviews", [])
                if not page_reviews:
                    break

                print(f"  Page {page_num}: {len(page_reviews)} reviews fetched")
                collected.extend(page_reviews)

                # No more pages
                if len(page_reviews) < 6:
                    break
                starting_after = page_reviews[-1].get("order_uuid", "")
                if not starting_after:
                    break

            new_reviews = collected[:needed]
            print(f"  → keeping {len(new_reviews)} review(s)")

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
