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
from zoneinfo import ZoneInfo

UK_TZ = ZoneInfo("Europe/London")
from pathlib import Path

from playwright.async_api import async_playwright
from playwright_stealth import stealth_async

ORG_ID       = "513610"
BASE_URL     = "https://partner-hub.deliveroo.com"
HOME_URL     = f"{BASE_URL}/home?orgId={ORG_ID}"
NOTIF_API    = f"{BASE_URL}/api-gw/notifications/employee/self/alerts"
OUT_FILE           = Path(__file__).parent / "data.json"
DELIVEROO_TOKEN    = os.environ.get("DELIVEROO_TOKEN", "")
GITHUB_PAT         = os.environ.get("GH_PAT", "")
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
    # "Order disruption" body: "• SiteName is currently encountering..."
    m = re.search(r"[•·]\s*(.+?)\s+is currently encountering", body_text)
    if m:
        return m.group(1).strip()
    # "First order" body: "• SiteName's first order"
    m = re.search(r"[•·]\s*(.+?)'s first order", body_text)
    if m:
        return m.group(1).strip()
    # Fallback: anything before "first order"
    m = re.search(r"([\w][\w\s\-\!\(\)]+?)\s+first order", body_text, re.IGNORECASE)
    return m.group(1).strip() if m else "Unknown"


def new_review_count(subtitle_text):
    m = re.match(r"(\d+)\s+new review", subtitle_text or "")
    return int(m.group(1)) if m else 1


def branch_id_from_link(link):
    link = link or ""
    # branchId= or branch_id= query param
    m = re.search(r"[?&]branch[_i]?[dI][dD]?=(\d+)", link, re.IGNORECASE)
    if m: return m.group(1)
    m = re.search(r"branchId=(\d+)", link, re.IGNORECASE)
    if m: return m.group(1)
    # /restaurants/{id}
    m = re.search(r"/restaurants?/(\d+)", link)
    if m: return m.group(1)
    # /site/{id} or /sites/{id}
    m = re.search(r"/sites?/(\d+)", link)
    if m: return m.group(1)
    # /branch/{id}
    m = re.search(r"/branches?/(\d+)", link)
    if m: return m.group(1)
    # id= as last resort
    m = re.search(r"[?&]id=(\d+)", link)
    if m: return m.group(1)
    return None


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

    # Use UK timezone — business operates in London time (BST/GMT)
    now_uk       = datetime.now(UK_TZ)
    today        = now_uk.strftime("%Y-%m-%d")
    yesterday    = (now_uk - timedelta(days=1)).strftime("%Y-%m-%d")
    two_days_ago = (now_uk - timedelta(days=2)).strftime("%Y-%m-%d")
    print(f"UK time now:             {now_uk.strftime('%Y-%m-%d %H:%M %Z')}"
          f"\nCancelled orders target: {yesterday} (notification API — yesterday UK)"
          f"\nReviews target:          {today} (branch reviews API — today UK)")

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

        # ── 4. Fetch notifications — UI interception + API fallback ──────────
        # The UI always loads newest-first (critical: review notification
        # arrives once at ~6:30 PM UK, so the 8 PM run must see it at the top).
        # Strategy: intercept the network response the UI fires when we navigate
        # to /notifications — this gives exactly what the UI sees, newest-first.
        # Then also call the API with pagination for historical branch discovery.
        print("\nFetching notifications…")
        all_notifs = []
        seen_ids = set()

        for oid in sorted(org_ids):
            intercepted_batches = []

            async def _capture_notif(response, _batches=intercepted_batches):
                if ("/notifications/employee/self/alerts" in response.url
                        and response.status == 200):
                    try:
                        data = await response.json()
                        if isinstance(data, list) and data:
                            _batches.append(data)
                            print(f"    [intercept] {len(data)} notifs from {response.url[-60:]}")
                    except Exception:
                        pass

            page.on("response", _capture_notif)

            # Navigate to the notifications page — the UI fires its own API call
            notif_page_url = (f"{BASE_URL}/notifications"
                              f"?back_url=%2Fhome&orgId={oid}")
            try:
                await page.goto(notif_page_url, wait_until="networkidle", timeout=20000)
            except Exception:
                try:
                    await page.goto(notif_page_url, wait_until="domcontentloaded", timeout=15000)
                except Exception:
                    pass
            await page.wait_for_timeout(3000)

            # Scroll to trigger lazy-load of more notifications
            for _ in range(5):
                await page.evaluate("window.scrollTo(0, document.body.scrollHeight)")
                await page.wait_for_timeout(1200)
            await page.wait_for_timeout(800)

            page.remove_listener("response", _capture_notif)

            # Add intercepted (UI) notifications first — these are newest-first
            ui_added = 0
            for batch in intercepted_batches:
                for n in batch:
                    key = n.get("id") or f"{n.get('timestamp')}-{n.get('title')}"
                    if key not in seen_ids:
                        seen_ids.add(key)
                        all_notifs.append(n)
                        ui_added += 1
            print(f"  org {oid}: {ui_added} notifications from UI interception")

            # Also paginate the API directly for historical branch discovery
            # (older notifications not loaded by the UI scroll)
            try:
                await page.goto(f"{BASE_URL}/home?orgId={oid}",
                                wait_until="domcontentloaded", timeout=15000)
            except Exception:
                pass
            await page.wait_for_timeout(1500)

            for page_num in range(5):
                offset = page_num * 200
                r = await api_get(page, f"{NOTIF_API}?limit=200&offset={offset}&sort=desc", token)
                if not r.get("ok") or not isinstance(r.get("data"), list):
                    r = await api_get(page, f"{NOTIF_API}?limit=200&offset={offset}", token)
                batch = r["data"] if isinstance(r.get("data"), list) else []
                added = 0
                for n in batch:
                    key = n.get("id") or f"{n.get('timestamp')}-{n.get('title')}"
                    if key not in seen_ids:
                        seen_ids.add(key)
                        all_notifs.append(n)
                        added += 1
                print(f"  org {oid} API page {page_num+1}: {len(batch)} fetched, {added} new unique")
                if len(batch) < 200:
                    break  # last page

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

        def notif_uk_date(ts):
            try:
                dt = datetime.fromisoformat(ts.replace("Z", "+00:00"))
                return dt.astimezone(UK_TZ).strftime("%Y-%m-%d")
            except Exception:
                return ts[:10]

        # Only fetch today's notifications — yesterday's data is already stored
        today_notifs = [n for n in all_notifs if notif_uk_date(n.get("timestamp","")) == today]
        print(f"  Today ({today}): {len(today_notifs)} notifications")

        # Debug: print all unique notification titles to diagnose missing records
        all_titles = {}
        for n in all_notifs:
            t = n.get("title", "(no title)")
            all_titles[t] = all_titles.get(t, 0) + 1
        print("  Notification title breakdown:")
        for title, count in sorted(all_titles.items(), key=lambda x: -x[1]):
            print(f"    [{count}x] {title}")
        print("  Today's notification titles:")
        for n in today_notifs:
            print(f"    {notif_uk_date(n.get('timestamp','?'))} {n.get('timestamp','')[:16]} | {n.get('title','')}")

        # ── 5. Cancelled orders — today only ─────────────────────────────────
        CANCEL_KEYWORDS = ["cancelled", "auto-rejected", "order disruption", "disruption detected"]
        cancelled_today = []
        for n in today_notifs:
            title = n.get("title", "")
            if any(kw in title.lower() for kw in CANCEL_KEYWORDS):
                dr = await api_get(page, f"{NOTIF_API}/{n['id']}", token)
                if dr.get("ok"):
                    site = extract_site_from_body(dr["data"].get("body", ""))
                    uk_time = datetime.fromisoformat(
                        n["timestamp"].replace("Z","+00:00")
                    ).astimezone(UK_TZ).strftime("%Y-%m-%dT%H:%M:%S")
                    print(f"  ✓ Cancelled: {site}")
                    cancelled_today.append({"time": uk_time, "site": site, "brand": categorise(site)})

        # ── 6. Discover branch IDs ────────────────────────────────────────────
        # Strategy A: Check EVERY today's notification for resources (title-agnostic,
        #   catches reviews even if title wording is unexpected).
        # Strategy B: Check historical notifications where title suggests reviews.
        # Strategy C: Directly query restaurant list API endpoints.
        known_branches = {}   # branch_id → {site, url, from_today}

        def _add_branch(res, label="", from_today=False):
            """Extract branch from a resource dict and add to known_branches."""
            link = (res.get("link") or res.get("url") or res.get("href") or "")
            bid  = branch_id_from_link(link)
            sname = (res.get("title") or res.get("name") or res.get("siteName") or "Unknown")
            if bid and bid not in known_branches:
                full_link = link if link.startswith("http") else f"{BASE_URL}{link}"
                known_branches[bid] = {"site": sname, "url": full_link, "from_today": from_today}
                print(f"    + {sname} (branchId={bid}){label}")
                return True
            elif bid and from_today and not known_branches[bid].get("from_today"):
                # Mark existing branch as today if found in today's notification
                known_branches[bid]["from_today"] = True
            return False

        # ── A. Today's notifications (ALL, regardless of title) ───────────────
        print(f"\n[A] Checking all {len(today_notifs)} today's notifications for resources…")
        for n in today_notifs:
            nid = n.get("id")
            if not nid:
                continue
            dr = await api_get(page, f"{NOTIF_API}/{nid}", token)
            if not dr.get("ok"):
                continue
            data = dr.get("data") or {}
            resources = data.get("resources", []) if isinstance(data, dict) else []
            if not resources:
                continue
            print(f"  '{n.get('title','')}' → {len(resources)} resource(s)")
            for res in resources:
                added = _add_branch(res, " ← TODAY", from_today=True)
                if not added:
                    # Print full resource so we can debug link format
                    print(f"    [skip] keys={list(res.keys())} link={str(res.get('link') or res.get('url') or '')[:80]}")

        # ── B. Historical review-type notifications ────────────────────────────
        REVIEW_KEYWORDS = ["review", "feedback", "rating", "customer", "star"]
        all_review_notifs = [
            n for n in all_notifs
            if n not in today_notifs
            and any(kw in (n.get("title","") + " " + n.get("subtitle","")).lower()
                    for kw in REVIEW_KEYWORDS)
        ]
        print(f"\n[B] {len(all_review_notifs)} historical review-type notifications")
        for n in all_review_notifs:
            nid = n.get("id")
            if not nid:
                continue
            dr = await api_get(page, f"{NOTIF_API}/{nid}", token)
            if not dr.get("ok"):
                continue
            data = dr.get("data") or {}
            resources = data.get("resources", []) if isinstance(data, dict) else []
            for res in resources:
                _add_branch(res)

        # ── C. Direct restaurant list endpoints ───────────────────────────────
        print(f"\n[C] Trying direct restaurant list endpoints…")
        for rest_url in [
            f"{BASE_URL}/api-gw/restaurants?orgId={ORG_ID}",
            f"{BASE_URL}/api-gw/org/{ORG_ID}/restaurants",
            f"{BASE_URL}/api-gw/employee/self/sites",
            f"{BASE_URL}/api-gw/sites?orgId={ORG_ID}",
            f"{BASE_URL}/api/restaurants?org_id={ORG_ID}",
            f"{BASE_URL}/api/v1/restaurants?orgId={ORG_ID}",
        ]:
            r = await api_get(page, rest_url, token)
            if r.get("ok"):
                data = r.get("data", {})
                print(f"  ✓ {rest_url}")
                items = (data if isinstance(data, list)
                         else data.get("restaurants", data.get("sites",
                              data.get("branches", data.get("data", [])))))
                for item in (items if isinstance(items, list) else []):
                    bid = str(item.get("id") or item.get("branchId") or
                              item.get("branch_id") or "")
                    sname = (item.get("name") or item.get("title") or
                             item.get("restaurantName") or "Unknown")
                    if bid.isdigit() and bid not in known_branches:
                        known_branches[bid] = {
                            "site": sname, "url": f"{BASE_URL}/home?orgId={ORG_ID}"
                        }
                        print(f"    + {sname} (branchId={bid}) [restaurant API]")

        # ── D. Navigate to review notification detail + intercept API calls ────
        # This is the most reliable method: the UI's own network calls reveal
        # the exact review API URLs (with correct branchIds) the backend uses.
        # It bypasses all link-format guessing.
        REVIEW_KEYWORDS_TITLE = ["review", "feedback", "rating", "customer", "star"]
        today_review_notif_ids = [
            n.get("id") for n in today_notifs
            if n.get("id") and any(
                kw in n.get("title", "").lower() for kw in REVIEW_KEYWORDS_TITLE
            )
        ]
        print(f"\n[D] Navigating to {len(today_review_notif_ids)} review notification detail page(s)…")

        # Cache: branchId → list of raw review dicts captured from interception
        intercepted_review_data = {}   # bid → [rev, ...]

        for nid in today_review_notif_ids:
            captured_urls = {}  # bid → response data

            async def _cap_rev(response, _c=captured_urls):
                url = response.url
                if response.status != 200:
                    return
                if ("restaurants" in url or "sites" in url) and "review" in url:
                    bid = branch_id_from_link(url)
                    if bid and bid not in _c:
                        try:
                            data = await response.json()
                            _c[bid] = {"url": url, "data": data}
                            print(f"    [D] Intercepted reviews bid={bid}: …{url[-50:]}")
                        except Exception:
                            _c[bid] = {"url": url, "data": None}

            page.on("response", _cap_rev)
            detail_url = (f"{BASE_URL}/notifications/{nid}"
                          f"?back_url=%2Fnotifications&orgId={ORG_ID}")
            print(f"  Navigating to notification detail…")
            try:
                await page.goto(detail_url, wait_until="networkidle", timeout=20000)
            except Exception:
                try:
                    await page.goto(detail_url, wait_until="domcontentloaded", timeout=15000)
                except Exception:
                    pass
            await page.wait_for_timeout(4000)
            await page.evaluate("window.scrollTo(0, document.body.scrollHeight)")
            await page.wait_for_timeout(2000)
            page.remove_listener("response", _cap_rev)

            for bid, item in captured_urls.items():
                # Add to known_branches so step 7 will also poll it
                if bid not in known_branches:
                    known_branches[bid] = {
                        "site": f"Branch-{bid}", "url": item["url"]
                    }
                    print(f"    + Branch {bid} discovered via page interception [D]")
                # Cache raw data so step 7 can use it without an extra API call
                if item.get("data"):
                    raw = item["data"]
                    rev_list = (raw.get("reviews") or raw.get("data") or
                                (raw if isinstance(raw, list) else []))
                    intercepted_review_data[bid] = (
                        rev_list if isinstance(rev_list, list) else []
                    )

        # Update site names from notification resources where possible
        for nid in today_review_notif_ids:
            dr = await api_get(page, f"{NOTIF_API}/{nid}", token)
            if not dr.get("ok"):
                continue
            data = dr.get("data") or {}
            for res in (data.get("resources", []) if isinstance(data, dict) else []):
                link  = (res.get("link") or res.get("url") or res.get("href") or "")
                bid   = branch_id_from_link(link)
                sname = (res.get("title") or res.get("name") or res.get("siteName") or "")
                if bid and sname and bid in known_branches:
                    known_branches[bid]["site"] = sname  # use real name

        print(f"\n  Total known branches: {len(known_branches)}")

        # ── 7. Fetch reviews for all known branches ───────────────────────────
        # Branches from today's notification are priority — navigate to each
        # to establish the correct session context, then call the reviews API.
        today_notif_branch_ids = {
            bid for bid, info in known_branches.items()
            if info.get("from_today") or bid in intercepted_review_data
        }
        print(f"  Today-notification branches: {len(today_notif_branch_ids)} → {sorted(today_notif_branch_ids)}")

        def to_uk_date(ts):
            try:
                dt = datetime.fromisoformat(ts.replace("Z", "+00:00"))
                return dt.astimezone(UK_TZ).strftime("%Y-%m-%d")
            except Exception:
                return ts[:10]

        def extract_reviews_from_response(result, branch_id, site_name, debug=False):
            """Parse the reviews API response — handles multiple response shapes."""
            if not result.get("ok"):
                if debug:
                    print(f"    API failed: status={result.get('status')} err={str(result.get('error',''))[:80]}")
                return None
            rd = result.get("data")
            if debug:
                print(f"    API ok, data type={type(rd).__name__} sample={str(rd)[:200]}")
            if rd is None:
                return None
            # Multiple possible shapes
            if isinstance(rd, list):
                return rd
            if isinstance(rd, dict):
                for key in ["reviews", "data", "items", "results", "content"]:
                    val = rd.get(key)
                    if isinstance(val, list):
                        return val
                    if isinstance(val, dict):
                        # Nested: {"data": {"reviews": [...]}}
                        for inner_key in ["reviews", "items", "results"]:
                            inner = val.get(inner_key)
                            if isinstance(inner, list):
                                return inner
            return []

        # Return to home before reviews to reset session cleanly
        try:
            await page.goto(HOME_URL, wait_until="domcontentloaded", timeout=15000)
        except Exception:
            pass
        await page.wait_for_timeout(2000)
        all_cookies = await ctx.cookies([BASE_URL])
        token = next((c["value"] for c in all_cookies if c["name"] == "token"), token)

        reviews = []
        debug_count = 0  # print full response for first 3 branches

        for branch_id, info in known_branches.items():
            site_name = info["site"]
            is_today_branch = branch_id in today_notif_branch_ids
            debug = debug_count < 3  # detailed debug for first 3 branches

            # Try intercepted data first (from Strategy D)
            raw_reviews = intercepted_review_data.get(branch_id)

            if raw_reviews is None:
                # For today's notification branches: navigate to the branch URL.
                # This establishes session context AND may let us intercept the
                # reviews API call the page fires automatically.
                if is_today_branch and info.get("url"):
                    branch_url = info["url"]
                    if not branch_url.startswith("http"):
                        branch_url = f"{BASE_URL}{branch_url}"
                    captured_on_nav = {}  # bid → reviews list captured during navigation

                    async def _cap_nav(response, _c=captured_on_nav, _bid=branch_id):
                        if response.status != 200:
                            return
                        url = response.url
                        if ("restaurants" in url or "sites" in url) and "review" in url:
                            try:
                                data = await response.json()
                                rev_list = (data.get("reviews") or data.get("data") or
                                            (data if isinstance(data, list) else []))
                                if isinstance(rev_list, list) and rev_list:
                                    _c[_bid] = rev_list
                                    print(f"    [nav-intercept] bid={_bid}: {len(rev_list)} reviews from {url[-50:]}")
                            except Exception:
                                pass

                    page.on("response", _cap_nav)
                    try:
                        await page.goto(branch_url, wait_until="domcontentloaded", timeout=15000)
                        await page.wait_for_timeout(3000)
                    except Exception as e:
                        if debug:
                            print(f"  Navigate to branch failed: {e}")
                    page.remove_listener("response", _cap_nav)

                    if branch_id in captured_on_nav:
                        raw_reviews = captured_on_nav[branch_id]
                    else:
                        all_cookies = await ctx.cookies([BASE_URL])
                        token = next((c["value"] for c in all_cookies if c["name"] == "token"), token)

                # Try multiple API URL formats
                if not raw_reviews:
                    api_urls = [
                        f"{BASE_URL}/api/restaurants/{branch_id}/reviews?stars=&sort_date=&starting_after=",
                        f"{BASE_URL}/api/restaurants/{branch_id}/reviews",
                        f"{BASE_URL}/api-gw/restaurants/{branch_id}/reviews",
                        f"{BASE_URL}/api-gw/partner/restaurants/{branch_id}/reviews",
                    ]
                    for api_url in api_urls:
                        result = await api_get(page, api_url, token)
                        raw_reviews = extract_reviews_from_response(result, branch_id, site_name, debug=debug)
                        if debug:
                            print(f"  [{branch_id}] {api_url[-55:]}: ok={result.get('ok')} status={result.get('status')} → {len(raw_reviews) if raw_reviews is not None else 'None'} reviews")
                        if raw_reviews:  # non-empty list
                            print(f"  {site_name}: {len(raw_reviews)} review(s) via {api_url[-50:]}")
                            break
                        if raw_reviews is None and result.get("status") not in [401, 403, 404]:
                            # Session drop — refresh once and retry this URL
                            try:
                                await page.goto(HOME_URL, wait_until="domcontentloaded", timeout=15000)
                            except Exception:
                                pass
                            await page.wait_for_timeout(2000)
                            all_cookies = await ctx.cookies([BASE_URL])
                            token = next((c["value"] for c in all_cookies if c["name"] == "token"), token)
                            result = await api_get(page, api_url, token)
                            raw_reviews = extract_reviews_from_response(result, branch_id, site_name, debug=debug)
                            if raw_reviews:
                                break

                if debug:
                    debug_count += 1

            if not raw_reviews:
                if is_today_branch:
                    print(f"  {site_name} (bid={branch_id}): no reviews returned [TODAY branch]")
                continue

            # Date filter: for today's notification branches accept last 3 days
            # (notification may be delayed; reviews might be from yesterday)
            cutoff = (now_uk - timedelta(days=2)).strftime("%Y-%m-%d")
            if is_today_branch:
                filtered = [
                    r for r in raw_reviews
                    if to_uk_date(r.get("created_at", "") or r.get("date", "")) >= cutoff
                ]
            else:
                filtered = [
                    r for r in raw_reviews
                    if to_uk_date(r.get("created_at", "") or r.get("date", "")) == today
                ]

            if not filtered:
                print(f"  {site_name} (bid={branch_id}): {len(raw_reviews)} reviews but none in date range. "
                      f"Sample dates: {[to_uk_date(r.get('created_at','')) for r in raw_reviews[:3]]}")
                continue

            print(f"  {site_name} (bid={branch_id}): {len(filtered)} review(s) in range")
            for rev in filtered:
                rating  = rev.get("rating_stars") or rev.get("rating") or rev.get("stars")
                comment = (rev.get("rating_comment") or rev.get("comment") or rev.get("text") or "").strip()
                created = rev.get("created_at", "") or rev.get("date", "")
                rev_date = to_uk_date(created)
                print(f"    ★{rating}  [{created}]  {comment[:60]}")
                reviews.append({
                    "site":    site_name,
                    "brand":   categorise(site_name),
                    "rating":  rating,
                    "text":    comment,
                    "created": created,
                    "date":    rev_date,
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

    now_utc = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")

    # Store everything under today's UK date — each run accumulates the day's data
    history[today] = {
        "scraped_date":     today,
        "last_updated":     now_utc,
        "cancelled_orders": cancelled_today,
        "reviews":          reviews,
    }

    OUT_FILE.write_text(json.dumps(history, indent=2))
    print(f"\n✓ Done — {len(cancelled_today)} cancellations, "
          f"{len(reviews)} reviews → data.json[{today}]")


if __name__ == "__main__":
    asyncio.run(main())
