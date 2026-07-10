"""One-time local scraper: detect a kids'/children's menu on each restaurant's
website and tag menus-latest.json with a `kids_menu` boolean.

This is *not* part of the Summerlicious prix-fixe deal — it's a best-effort
signal for parents choosing where to take a kid. Most restaurant sites render
their menus with JavaScript, so we drive a headless Chromium (Playwright),
render each page, and look for phrases like "kids menu", "children's menu",
"bambini", "menu enfant" — in the page text or in a link to a kids menu
(including PDFs). Sites that word it unusually or gate the menu behind a form
will be missed (false negatives) — a missing tag means "not found", not a
definitive "no".

Results are cached by website URL in kids-menu-cache.json so re-runs are cheap.

Setup (one time):
    pip install playwright && python3 -m playwright install chromium

Usage:
    python3 kids_menu.py               # scrape all, use cache
    python3 kids_menu.py --limit 15    # first N (handy for a smoke test)
    python3 kids_menu.py --force       # ignore cache
    python3 kids_menu.py --workers 6
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urlparse

from playwright.async_api import async_playwright

PDFTOTEXT = shutil.which("pdftotext")  # poppler; used to read PDF menus

HERE = Path(__file__).parent
MENUS_PATH = HERE / "menus-latest.json"
CACHE_PATH = HERE / "kids-menu-cache.json"

UA = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
    "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
)

# Phrases that indicate a dedicated kids/children's offering.
KID_PATTERNS = [
    r"kid'?s?\s+menu", r"children'?s?\s+menu", r"child'?s?\s+menu",
    r"kiddie\s+menu", r"kids?\s+meals?", r"menu\s+for\s+(?:the\s+)?kids",
    r"little\s+(?:diners|ones|guests)", r"kids?\s+eat\s+free",
    r"bambini", r"menu\s+enfants?", r"per\s+i\s+bambini",
]
KID_RE = re.compile("|".join(KID_PATTERNS), re.I)
# Which internal links are worth navigating to (menu-ish pages).
FOLLOW_HINT_RE = re.compile(r"kid|child|bambini|enfant|menu|dining|food", re.I)

NAV_TIMEOUT = 25000       # ms
SETTLE_MS = 1800          # let client-rendered menus populate
MAX_FOLLOW = 4            # menu/kids sub-pages to visit if homepage misses
MAX_PDF = 6               # menu PDFs to download + read per site


def _norm_link(href: str, text: str) -> str:
    return re.sub(r"[-_/]", " ", href or "") + " " + (text or "")


def _extract_pdf_text(data: bytes) -> str:
    """Run pdftotext on PDF bytes and return the text (empty on failure)."""
    if not PDFTOTEXT:
        return ""
    with tempfile.NamedTemporaryFile(suffix=".pdf", delete=False) as f:
        f.write(data)
        path = f.name
    try:
        out = subprocess.run(
            [PDFTOTEXT, "-q", "-nopgbrk", path, "-"],
            capture_output=True, timeout=30,
        )
        return out.stdout.decode("utf-8", "ignore")
    except (subprocess.SubprocessError, OSError):
        return ""
    finally:
        try:
            os.unlink(path)
        except OSError:
            pass


async def pdf_has_kids(context, url: str):
    """Download a PDF (via the browser session) and look for a kids-menu phrase.
    Returns the matched phrase, or None."""
    if not PDFTOTEXT:
        return None
    try:
        resp = await context.request.get(url, timeout=20000)
    except Exception:  # noqa: BLE001
        return None
    if not resp.ok:
        return None
    try:
        body = await resp.body()
    except Exception:  # noqa: BLE001
        return None
    if not body or not body[:5].startswith(b"%PDF"):
        return None
    text = await asyncio.to_thread(_extract_pdf_text, body)
    m = KID_RE.search(text or "")
    return m.group(0).strip() if m else None


async def scan_site(browser, url: str) -> dict:
    """Render the site (and a few menu links) and look for a kids menu."""
    page = await browser.new_page(user_agent=UA)
    page.set_default_timeout(8000)
    try:
        try:
            await page.goto(url, wait_until="domcontentloaded", timeout=NAV_TIMEOUT)
        except Exception as e:  # noqa: BLE001
            return {"found": False, "phrase": "", "source": url, "status": f"nav-err {type(e).__name__}"}
        await page.wait_for_timeout(SETTLE_MS)
        try:
            await page.evaluate("window.scrollTo(0, document.body.scrollHeight)")
            await page.wait_for_timeout(500)
        except Exception:  # noqa: BLE001
            pass

        base = page.url
        base_host = urlparse(base).netloc

        text = (await page.inner_text("body")) if await page.query_selector("body") else ""
        m = KID_RE.search(text or "")
        if m:
            return {"found": True, "phrase": m.group(0).strip(), "source": base, "status": "ok"}

        links = await page.eval_on_selector_all(
            "a[href]", "els => els.map(e => ({href: e.href, text: (e.innerText||'').trim()}))"
        )
        follow: list[str] = []
        pdfs: list[str] = []
        seen_pdfs: set[str] = set()
        for l in links:
            href, ltext = l.get("href") or "", l.get("text") or ""
            if not href or href.startswith(("mailto:", "tel:", "javascript:")):
                continue
            norm = _norm_link(href, ltext)
            if KID_RE.search(norm):
                return {"found": True, "phrase": (ltext or href)[:60], "source": base, "status": "ok-link"}
            if urlparse(href).path.lower().endswith(".pdf"):
                if FOLLOW_HINT_RE.search(norm) and href not in seen_pdfs:
                    seen_pdfs.add(href)
                    pdfs.append(href)
            elif urlparse(href).netloc == base_host and FOLLOW_HINT_RE.search(norm):
                if href != base and href not in follow:
                    follow.append(href)

        checked_pdfs = 0
        # Menu PDFs are a strong signal — read them first.
        for pdf in pdfs:
            if checked_pdfs >= MAX_PDF:
                break
            phrase = await pdf_has_kids(page.context, pdf)
            checked_pdfs += 1
            if phrase:
                return {"found": True, "phrase": phrase, "source": pdf, "status": "ok-pdf"}

        # Visit the most promising HTML links: kid/child pages, then menu pages.
        def _rank(h: str) -> int:
            if re.search(r"kid|child|bambini|enfant", h, re.I):
                return 0
            if re.search(r"menu", h, re.I):
                return 1
            return 2
        follow.sort(key=_rank)

        for link in follow[:MAX_FOLLOW]:
            try:
                await page.goto(link, wait_until="domcontentloaded", timeout=NAV_TIMEOUT)
                await page.wait_for_timeout(SETTLE_MS)
                ptext = (await page.inner_text("body")) if await page.query_selector("body") else ""
                m2 = KID_RE.search(ptext or "")
                if m2:
                    return {"found": True, "phrase": m2.group(0).strip(), "source": link, "status": "ok-page"}
                # Menu pages often link a PDF menu — grab any new ones.
                sublinks = await page.eval_on_selector_all(
                    "a[href*='.pdf']", "els => els.map(e => ({href: e.href, text: (e.innerText||'').trim()}))"
                )
                for sl in sublinks:
                    if checked_pdfs >= MAX_PDF:
                        break
                    h, tx = sl.get("href") or "", sl.get("text") or ""
                    if not urlparse(h).path.lower().endswith(".pdf") or h in seen_pdfs:
                        continue
                    if not FOLLOW_HINT_RE.search(_norm_link(h, tx)):
                        continue
                    seen_pdfs.add(h)
                    if KID_RE.search(_norm_link(h, tx)):
                        return {"found": True, "phrase": (tx or h)[:60], "source": link, "status": "ok-link-pdf"}
                    phrase = await pdf_has_kids(page.context, h)
                    checked_pdfs += 1
                    if phrase:
                        return {"found": True, "phrase": phrase, "source": h, "status": "ok-pdf-page"}
            except Exception:  # noqa: BLE001
                continue

        return {"found": False, "phrase": "", "source": base, "status": "not-found"}
    finally:
        await page.close()


def load_json(path: Path, default):
    if not path.exists():
        return default
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return default


def save_cache(cache: dict) -> None:
    CACHE_PATH.write_text(json.dumps(cache, indent=2, ensure_ascii=False), encoding="utf-8")


async def run(todo: list, cache: dict, workers: int) -> None:
    total = len(todo)
    done = 0
    queue: asyncio.Queue = asyncio.Queue()
    for r in todo:
        queue.put_nowait(r)

    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=True)

        async def worker():
            nonlocal done
            while not queue.empty():
                try:
                    r = queue.get_nowait()
                except asyncio.QueueEmpty:
                    return
                site = (r.get("website") or "").strip()
                try:
                    result = await scan_site(browser, site)
                except Exception as e:  # noqa: BLE001
                    result = {"found": False, "phrase": "", "source": site, "status": f"err {type(e).__name__}"}
                cache[site] = {"name": r.get("restaurant_name", "?"), **result,
                               "checked_at": datetime.now(timezone.utc).isoformat()}
                done += 1
                flag = "KIDS" if result["found"] else "----"
                print(f"[{done}/{total}] {flag} {r.get('restaurant_name','?')[:40]}  ({result['status']})", flush=True)
                if done % 15 == 0:
                    save_cache(cache)

        await asyncio.gather(*[worker() for _ in range(min(workers, total or 1))])
        await browser.close()
    save_cache(cache)


def main() -> None:
    ap = argparse.ArgumentParser(description="Tag restaurants with a kids-menu flag by rendering their websites.")
    ap.add_argument("--limit", type=int, default=None, help="Only process the first N restaurants with a website.")
    ap.add_argument("--force", action="store_true", help="Ignore the cache and re-scrape every site.")
    ap.add_argument("--workers", type=int, default=6, help="Concurrent headless pages (default 6).")
    args = ap.parse_args()

    restaurants = load_json(MENUS_PATH, None)
    if not restaurants:
        sys.exit(f"Could not load {MENUS_PATH}")
    cache = load_json(CACHE_PATH, {})

    targets = [r for r in restaurants if (r.get("website") or "").strip()]
    if args.limit is not None:
        targets = targets[: args.limit]
    todo = [r for r in targets if args.force or (r.get("website") or "").strip() not in cache]
    print(f"{len(targets)} restaurants with a website; {len(todo)} to scrape ({len(targets) - len(todo)} cached).")

    if todo:
        asyncio.run(run(todo, cache, args.workers))

    found = 0
    for r in restaurants:
        site = (r.get("website") or "").strip()
        hit = cache.get(site)
        if hit and hit.get("found"):
            r["kids_menu"] = True
            found += 1
        else:
            r.pop("kids_menu", None)

    MENUS_PATH.write_text(json.dumps(restaurants, indent=2, ensure_ascii=False), encoding="utf-8")
    season = load_json(HERE / "season.json", {})
    archive = HERE / f"menus-{season.get('season')}-{season.get('year')}.json"
    if archive.exists():
        archive.write_bytes(MENUS_PATH.read_bytes())

    print(f"\nDone. Tagged {found} restaurants with a kids menu (of {len(targets)} checked).")


if __name__ == "__main__":
    main()
