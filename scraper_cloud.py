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
import base64
import json
import os
import re
import urllib.request
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

from playwright.async_api import async_playwright
from playwright_stealth import stealth_async

ORG_ID       = "513610"
BASE_URL     = "https://partner-hub.deliveroo.com"
HOME_URL     = f"{BASE_URL}/home?orgId={ORG_ID}"
NOTIF_API    = f"{BASE_URL}/api-gw/notifications/employee/self/alerts"
OUT_FILE           = Path(__file__).parent / "data.json"
DELIVEROO_TOKEN    = os.environ.get("DELIVEROO_TOKEN", "")
GITHUB_PAT         = os.environ.get("GITHUB_PAT", "")
GITHUB_REPO        = "lknscorecard-ux/deliveroo-dashboard"
DELIVEROO_EMAIL    = os.environ.get("DELIVEROO_EMAIL", "")
DELIVEROO_PASSWORD = os.environ.get("DELIVEROO_PASSWORD", "")


def push_token_to_github(new_token: str):
    """Update DELIVEROO_TOKEN secret in GitHub so next run uses the fresh JWT."""
    if not GITHUB_PAT:
        print("  (GITHUB_PAT not set — skipping secret update)")
        return
    try:
        from nacl import encoding, public
    except ImportError:
        print("  (pynacl not installed — skipping secret update)")
        return

    owner, repo = GITHUB_REPO.split("/")
    headers = {
        "Authorization": f"Bearer {GITHUB_PAT}",
        "Accept": "application/vnd.github+json",
        "X-GitHub-Api-Version": "2022-11-28",
        "Content-Type": "application/json",
    }
    try:
        req = urllib.request.Request(
            f"https://api.github.com/repos/{owner}/{repo}/actions/secrets/public-key",
            headers=headers,
        )
        with urllib.request.urlopen(req) as r:
            key_data = json.loads(r.read())

        pub_key   = public.PublicKey(key_data["key"].encode(), encoding.Base64Encoder)
        encrypted = base64.b64encode(
            public.SealedBox(pub_key).encrypt(new_token.encode())
        ).decode()

        payload = json.dumps({"encrypted_value": encrypted, "key_id": key_data["key_id"]}).encode()
        req = urllib.request.Request(
            f"https://api.github.com/repos/{owner}/{repo}/actions/secrets/DELIVEROO_TOKEN",
            data=payload, headers=headers, method="PUT",
        )
        with urllib.request.urlopen(req) as r:
            print(f"  ✓ DELIVEROO_TOKEN secret refreshed in GitHub (HTTP {r.status}).")
    except Exception as e:
        print(f"  WARNING: Could not update GitHub secret: {e}")


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

    today     = str(date.today())
    yesterday = str(date.today() - timedelta(days=1))
    print(f"Target date: {today} (real-time — scraping today's data)")

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

        # ── 0. Capture ALL api-gw URLs the page loads (helps discover endpoints) ─
        intercepted_api_urls = set()
        async def on_response(response):
            url = response.url
            if "api-gw" in url or "api/" in url:
                intercepted_api_urls.add(url)
        page.on("response", on_response)

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

        # ── 2. Get Bearer token (and try refresh endpoints) ───────────────────
        all_cookies = await ctx.cookies(["https://partner-hub.deliveroo.com"])
        token = next((c["value"] for c in all_cookies if c["name"] == "token"), None)
        if not token:
            print("ERROR: No token cookie found.")
            raise SystemExit(1)
        print(f"✓ Token ({len(token)} chars).")

        # Try to get a refreshed token from the server
        refreshed_token = token
        for refresh_url in [
            f"{BASE_URL}/api-gw/auth/token/refresh",
            f"{BASE_URL}/api-gw/auth/refresh",
            f"{BASE_URL}/api-gw/employee/auth/refresh",
            f"{BASE_URL}/api/auth/refresh",
        ]:
            result = await page.evaluate("""async ([url, tok]) => {
                try {
                    const r = await fetch(url, {
                        method: 'POST',
                        credentials: 'include',
                        headers: { 'Authorization': 'Bearer ' + tok,
                                   'Content-Type': 'application/json' }
                    });
                    const data = await r.json().catch(() => ({}));
                    return { ok: r.ok, status: r.status, data };
                } catch(e) { return { ok: false, error: e.message }; }
            }""", [refresh_url, token])
            if result.get("ok"):
                print(f"  ✓ Refresh endpoint found: {refresh_url}")
                # Token may be in response body or in updated cookie
                data = result.get("data", {})
                new_tok = (data.get("token") or data.get("access_token")
                           or data.get("jwt") or "")
                if new_tok:
                    refreshed_token = new_tok
                    print(f"  ✓ Got refreshed token from response ({len(new_tok)} chars).")
                else:
                    # Check if cookie was updated
                    new_cookies = await ctx.cookies(["https://partner-hub.deliveroo.com"])
                    new_tok = next((c["value"] for c in new_cookies if c["name"] == "token"), "")
                    if new_tok and new_tok != token:
                        refreshed_token = new_tok
                        print(f"  ✓ Got refreshed token from cookie ({len(new_tok)} chars).")
                break
            elif result.get("status") not in [0, 404, 405]:
                print(f"  Refresh {refresh_url}: HTTP {result.get('status')}")

        if refreshed_token != token:
            push_token_to_github(refreshed_token)
            token = refreshed_token
        else:
            # Even if no refresh endpoint found, push the current valid token
            # so the secret stays fresh in GitHub (resets the expiry window)
            push_token_to_github(token)

        # ── 2b. Print ALL API URLs the page called (helps discover endpoints) ───
        if intercepted_api_urls:
            print("API URLs called by Partner Hub on load:")
            for u in sorted(intercepted_api_urls):
                print(f"  {u}")


        # ── 3. Discover all accessible org IDs ───────────────────────────────
        # The UI shows notifications from ALL orgs; the API is scoped per-org.
        # Find every org this account has access to, then fetch from each.
        print("\nDiscovering accessible orgs…")
        org_ids = set([ORG_ID])

        # Try common org-list endpoints
        for org_endpoint in [
            f"{BASE_URL}/api-gw/employee/self/orgs",
            f"{BASE_URL}/api-gw/auth/employee/self",
            f"{BASE_URL}/api-gw/orgs",
            f"{BASE_URL}/api/v1/employee/orgs",
        ]:
            r = await api_get(page, org_endpoint, token)
            if r.get("ok"):
                data = r.get("data", {})
                # Try to extract org IDs from various response shapes
                if isinstance(data, list):
                    for item in data:
                        oid = str(item.get("id") or item.get("orgId") or item.get("org_id") or "")
                        if oid.isdigit():
                            org_ids.add(oid)
                elif isinstance(data, dict):
                    for key in ["orgs", "organisations", "organizations", "accounts"]:
                        for item in (data.get(key) or []):
                            oid = str(item.get("id") or item.get("orgId") or "")
                            if oid.isdigit():
                                org_ids.add(oid)
                    oid = str(data.get("orgId") or data.get("org_id") or "")
                    if oid.isdigit():
                        org_ids.add(oid)
                if len(data if isinstance(data, list) else []) > 0 or (isinstance(data, dict) and data):
                    print(f"  {org_endpoint} → {str(data)[:200]}")

        # Also intercept what the page itself loads (org switcher dropdown)
        captured_orgs = set()
        async def capture_org_response(response):
            if "org" in response.url.lower() and "partner-hub" in response.url:
                try:
                    d = await response.json()
                    if isinstance(d, list):
                        for item in d:
                            oid = str(item.get("id") or item.get("orgId") or "")
                            if oid.isdigit():
                                captured_orgs.add(oid)
                except Exception:
                    pass
        page.on("response", capture_org_response)
        # Navigate to root (org selector) to trigger org list API call
        try:
            await page.goto(f"{BASE_URL}/home", wait_until="networkidle", timeout=15000)
        except Exception:
            pass
        await page.wait_for_timeout(3000)
        org_ids.update(captured_orgs)
        print(f"  Orgs discovered: {sorted(org_ids)}")

        # ── 4. Fetch notifications from ALL orgs ──────────────────────────────
        print("\nFetching notifications from all orgs…")
        all_notifs = []
        seen_ids = set()

        for oid in sorted(org_ids):
            try:
                await page.goto(f"{BASE_URL}/home?orgId={oid}",
                                wait_until="domcontentloaded", timeout=15000)
            except Exception:
                pass
            await page.wait_for_timeout(2000)
            r = await api_get(page, f"{NOTIF_API}?limit=200", token)
            batch = r["data"] if isinstance(r.get("data"), list) else []
            added = 0
            for n in batch:
                key = n.get("id") or f"{n.get('timestamp')}-{n.get('title')}"
                if key not in seen_ids:
                    seen_ids.add(key)
                    all_notifs.append(n)
                    added += 1
            print(f"  org {oid}: {len(batch)} notifs, {added} new unique")

        # Return to main org
        try:
            await page.goto(HOME_URL, wait_until="domcontentloaded", timeout=15000)
        except Exception:
            pass
        await page.wait_for_timeout(2000)
        all_cookies = await ctx.cookies([BASE_URL])
        token = next((c["value"] for c in all_cookies if c["name"] == "token"), token)

        print(f"  Total unique notifications: {len(all_notifs)}")

        # Print sample notification keys to reveal orgId/org fields
        if all_notifs:
            print(f"  Sample notification keys: {sorted(all_notifs[0].keys())}")
            # Extract any org IDs found inside notifications themselves
            for n in all_notifs:
                for key in ["orgId", "org_id", "organisationId", "organization_id"]:
                    val = str(n.get(key) or "")
                    if val.isdigit():
                        org_ids.add(val)

        today_notifs = [n for n in all_notifs if today in n.get("timestamp", "")]
        print(f"  Today ({today}): {len(today_notifs)} notifications")
        for n in today_notifs:
            print(f"    [{n['timestamp']}] {n['title']}")

        # ── 5. Cancelled orders (today only) ─────────────────────────────────
        cancelled = []
        for n in today_notifs:
            title = n.get("title", "")
            if "cancelled" in title.lower() or "auto-rejected" in title.lower():
                dr = await api_get(page, f"{NOTIF_API}/{n['id']}", token)
                if dr.get("ok"):
                    site = extract_site_from_body(dr["data"].get("body", ""))
                    print(f"  ✓ Cancelled: {site}")
                    cancelled.append({"time": n["timestamp"], "site": site, "brand": categorise(site)})

        # ── 6. Discover ALL branch IDs from historical review notifications ───
        # The API only returns 200 notifications. Yesterday's notification for some
        # branches may be missing, but those branches WILL appear in older notifications.
        # Collect every branch ID seen across ALL review notifications in the 200.
        print("\nDiscovering branches from all historical review notifications…")
        all_review_notifs = [n for n in all_notifs if "review" in n.get("title", "").lower()]
        print(f"  {len(all_review_notifs)} review notifications in history")

        known_branches = {}   # branch_id → {site, url}
        for n in all_review_notifs:
            dr = await api_get(page, f"{NOTIF_API}/{n['id']}", token)
            if not dr.get("ok"):
                continue
            for r in dr["data"].get("resources", []):
                link  = r.get("link", "")
                bid   = branch_id_from_link(link)
                sname = r.get("title", "Unknown")
                if bid and bid not in known_branches:
                    full_link = link if link.startswith("http") else f"{BASE_URL}{link}"
                    known_branches[bid] = {"site": sname, "url": full_link}
                    print(f"    + {sname} (branchId={bid})")

        print(f"  Total known branches: {len(known_branches)}")

        # ── 7. Warm up session then check every branch for yesterday's reviews ─
        if known_branches:
            first_bid = next(iter(known_branches))
            warmup_url = known_branches[first_bid]["url"]
            print(f"\nWarming up session…")
            try:
                await page.goto(warmup_url, wait_until="domcontentloaded", timeout=15000)
            except Exception:
                pass
            await page.wait_for_timeout(4000)
            await dismiss_popups(page)
            all_cookies = await ctx.cookies(["https://partner-hub.deliveroo.com"])
            token = next((c["value"] for c in all_cookies if c["name"] == "token"), token)
            print(f"  Session warmed up. Token: {len(token)} chars.")

        reviews = []
        for branch_id, info in known_branches.items():
            site_name = info["site"]
            api_url = (f"{BASE_URL}/api/restaurants/{branch_id}/reviews"
                       f"?stars=&sort_date=&starting_after=")
            result = await api_get(page, api_url, token)

            if not result.get("ok") and "DOCTYPE" in str(result.get("error", "")):
                # Session drop — re-navigate and retry
                try:
                    await page.goto(info["url"], wait_until="domcontentloaded", timeout=15000)
                except Exception:
                    pass
                await page.wait_for_timeout(3000)
                all_cookies = await ctx.cookies(["https://partner-hub.deliveroo.com"])
                token = next((c["value"] for c in all_cookies if c["name"] == "token"), token)
                result = await api_get(page, api_url, token)

            if not result.get("ok"):
                continue

            page_reviews = result["data"].get("reviews", [])
            # Keep reviews from today UTC.
            # BST midnight edge: review at 00:xx BST = 23:xx UTC on previous day (yesterday),
            # so also include yesterday's reviews where UTC hour is 23.
            today_reviews = [
                r for r in page_reviews
                if r.get("created_at", "")[:10] == today
                or (r.get("created_at", "")[:10] == yesterday
                    and r.get("created_at", "T")[11:13] == "23")
            ]
            if not today_reviews:
                continue

            print(f"  {site_name} (branchId={branch_id}): {len(today_reviews)} review(s)")
            for rev in today_reviews:
                rating  = rev.get("rating_stars")
                comment = (rev.get("rating_comment") or "").strip()
                created = rev.get("created_at", "")
                print(f"    ★{rating}  [{created}]  {comment[:60]}")
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

    history[today] = {
        "scraped_date":     str(date.today()),
        "last_updated":     datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "cancelled_orders": cancelled,
        "reviews":          reviews,
    }
    OUT_FILE.write_text(json.dumps(history, indent=2))
    print(f"\n✓ Done — {len(cancelled)} cancelled, {len(reviews)} reviews → data.json[{today}]")


if __name__ == "__main__":
    asyncio.run(main())
