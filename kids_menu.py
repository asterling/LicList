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

PDFTOTEXT = shutil.which("pdftotext")  # poppler; reads text-layer PDF menus
PDFTOPPM = shutil.which("pdftoppm")    # poppler; renders PDF pages to images for OCR
TESSERACT = shutil.which("tesseract")  # OCR for image-only PDFs and menu images

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
MAX_IMAGES = 5            # menu images to OCR per site (fallback)


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


def _ocr_image_bytes(data: bytes, suffix: str = ".png") -> str:
    """OCR a single image with tesseract; returns recognized text (or '')."""
    if not TESSERACT:
        return ""
    with tempfile.NamedTemporaryFile(suffix=suffix, delete=False) as f:
        f.write(data)
        path = f.name
    try:
        out = subprocess.run([TESSERACT, path, "stdout", "-l", "eng"], capture_output=True, timeout=60)
        return out.stdout.decode("utf-8", "ignore")
    except (subprocess.SubprocessError, OSError):
        return ""
    finally:
        try:
            os.unlink(path)
        except OSError:
            pass


def _ocr_pdf_bytes(data: bytes, max_pages: int = 6) -> str:
    """Render an image-only PDF's pages to PNGs (pdftoppm) and OCR them."""
    if not (TESSERACT and PDFTOPPM):
        return ""
    import tempfile as _tf
    tmpdir = _tf.mkdtemp()
    try:
        pdf_path = os.path.join(tmpdir, "in.pdf")
        with open(pdf_path, "wb") as f:
            f.write(data)
        try:
            subprocess.run(
                [PDFTOPPM, "-png", "-r", "150", "-f", "1", "-l", str(max_pages),
                 pdf_path, os.path.join(tmpdir, "pg")],
                capture_output=True, timeout=120,
            )
        except (subprocess.SubprocessError, OSError):
            return ""
        parts = []
        for name in sorted(os.listdir(tmpdir)):
            if name.startswith("pg") and name.endswith(".png"):
                with open(os.path.join(tmpdir, name), "rb") as im:
                    parts.append(_ocr_image_bytes(im.read()))
        return "\n".join(parts)
    finally:
        shutil.rmtree(tmpdir, ignore_errors=True)


IMG_EXTS = (".jpg", ".jpeg", ".png", ".webp", ".gif", ".avif")
MENU_RE = re.compile(r"menu", re.I)


async def pdf_has_kids(context, url: str):
    """Download a PDF (via the browser session) and check for a kids-menu phrase.

    Returns (phrase_or_None, outcome) where outcome is:
      - 'parsed' : got a real text layer (whether or not kids matched)
      - 'empty'  : valid PDF but ~no extractable text (likely image-only/scanned)
      - 'failed' : download error, non-200, not a PDF, or pdftotext error
    """
    if not PDFTOTEXT:
        return (None, "failed")
    try:
        resp = await context.request.get(url, timeout=20000)
    except Exception:  # noqa: BLE001
        return (None, "failed")
    if not resp.ok:
        return (None, "failed")
    try:
        body = await resp.body()
    except Exception:  # noqa: BLE001
        return (None, "failed")
    if not body or not body[:5].startswith(b"%PDF"):
        return (None, "failed")
    text = await asyncio.to_thread(_extract_pdf_text, body)
    if len((text or "").strip()) < 30:
        # No text layer — likely a scanned/image PDF. OCR the pages.
        if TESSERACT and PDFTOPPM:
            ocr_text = await asyncio.to_thread(_ocr_pdf_bytes, body)
            m = KID_RE.search(ocr_text or "")
            return (m.group(0).strip() if m else None, "ocr")
        return (None, "empty")
    m = KID_RE.search(text)
    return (m.group(0).strip() if m else None, "parsed")


async def ocr_image_url(context, url: str):
    """Download a menu image and OCR it. Returns (phrase_or_None, outcome)."""
    if not TESSERACT:
        return (None, "failed")
    try:
        resp = await context.request.get(url, timeout=20000)
    except Exception:  # noqa: BLE001
        return (None, "failed")
    if not resp.ok:
        return (None, "failed")
    try:
        body = await resp.body()
    except Exception:  # noqa: BLE001
        return (None, "failed")
    if not body:
        return (None, "failed")
    ext = os.path.splitext(urlparse(url).path)[1].lower() or ".png"
    text = await asyncio.to_thread(_ocr_image_bytes, body, ext)
    m = KID_RE.search(text or "")
    return (m.group(0).strip() if m else None, "ocr")


async def scan_site(browser, url: str) -> dict:
    """Render the site (and a few menu links) and look for a kids menu.

    The returned dict also carries per-site diagnostics: how many menu PDFs we
    saw / parsed / found empty (image-only) / failed, and how many menu-ish
    image files the page references.
    """
    diag = {"pdfs_seen": 0, "pdfs_parsed": 0, "pdfs_empty": 0, "pdfs_failed": 0,
            "pdfs_ocr": 0, "image_menu_links": 0, "images_ocr": 0}
    image_menus: list[str] = []
    seen_imgs: set[str] = set()

    def add_image(u: str, hint: str):
        if not u:
            return
        if urlparse(u).path.lower().endswith(IMG_EXTS) and MENU_RE.search(hint):
            diag["image_menu_links"] += 1
            if u not in seen_imgs:
                seen_imgs.add(u)
                image_menus.append(u)

    def finish(found, phrase, source, status):
        return {"found": found, "phrase": phrase, "source": source, "status": status, **diag}

    async def check_pdf(context, href):
        """Download+parse a PDF, tallying diagnostics. Returns matched phrase or None."""
        diag["pdfs_seen"] += 1
        phrase, outcome = await pdf_has_kids(context, href)
        diag["pdfs_" + outcome] += 1
        return phrase

    page = await browser.new_page(user_agent=UA)
    page.set_default_timeout(8000)
    try:
        try:
            await page.goto(url, wait_until="domcontentloaded", timeout=NAV_TIMEOUT)
        except Exception as e:  # noqa: BLE001
            return finish(False, "", url, f"nav-err {type(e).__name__}")
        await page.wait_for_timeout(SETTLE_MS)
        try:
            await page.evaluate("window.scrollTo(0, document.body.scrollHeight)")
            await page.wait_for_timeout(500)
        except Exception:  # noqa: BLE001
            pass

        base = page.url
        base_host = urlparse(base).netloc

        # Count menu-ish image files referenced (heuristic for image-based menus).
        try:
            imgs = await page.eval_on_selector_all(
                "img", "els => els.map(e => ({u: e.currentSrc||e.src||'', a: (e.alt||'').trim()}))"
            )
        except Exception:  # noqa: BLE001
            imgs = []
        for im in imgs:
            u = im.get("u") or ""
            add_image(u, u + " " + (im.get("a") or ""))

        text = (await page.inner_text("body")) if await page.query_selector("body") else ""
        m = KID_RE.search(text or "")
        if m:
            return finish(True, m.group(0).strip(), base, "ok")

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
            path = urlparse(href).path.lower()
            add_image(href, norm)
            if KID_RE.search(norm):
                return finish(True, (ltext or href)[:60], base, "ok-link")
            if path.endswith(".pdf"):
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
            phrase = await check_pdf(page.context, pdf)
            checked_pdfs += 1
            if phrase:
                return finish(True, phrase, pdf, "ok-pdf")

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
                    return finish(True, m2.group(0).strip(), link, "ok-page")
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
                        diag["pdfs_seen"] += 1
                        return finish(True, (tx or h)[:60], link, "ok-link-pdf")
                    phrase = await check_pdf(page.context, h)
                    checked_pdfs += 1
                    if phrase:
                        return finish(True, phrase, h, "ok-pdf-page")
                # Menu pages are often just images — collect them for OCR.
                subimgs = await page.eval_on_selector_all(
                    "img", "els => els.map(e => ({u: e.currentSrc||e.src||'', a: (e.alt||'').trim()}))"
                )
                for im in subimgs:
                    u = im.get("u") or ""
                    add_image(u, u + " " + (im.get("a") or ""))
            except Exception:  # noqa: BLE001
                continue

        # Last resort: OCR menu images (kid/child-named first).
        if TESSERACT and image_menus:
            image_menus.sort(key=lambda u: 0 if re.search(r"kid|child", u, re.I) else 1)
            checked_imgs = 0
            for iu in image_menus:
                if checked_imgs >= MAX_IMAGES:
                    break
                diag["images_ocr"] += 1
                phrase, _ = await ocr_image_url(page.context, iu)
                checked_imgs += 1
                if phrase:
                    return finish(True, phrase, iu, "ok-image-ocr")

        return finish(False, "", base, "not-found")
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

    # Sticky: detection is high-precision, so a prior confirmed tag is trusted
    # even if this run came back "not found" (usually a transient timeout).
    # Scraping isn't perfectly deterministic, and kids menus don't disappear.
    found = 0
    for r in restaurants:
        site = (r.get("website") or "").strip()
        hit = cache.get(site)
        if (hit and hit.get("found")) or r.get("kids_menu") is True:
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

    # Aggregate diagnostics across the sites we just have cached.
    target_sites = {(r.get("website") or "").strip() for r in targets}
    rows = [v for k, v in cache.items() if k in target_sites]
    seen = sum(v.get("pdfs_seen", 0) for v in rows)
    parsed = sum(v.get("pdfs_parsed", 0) for v in rows)
    empty = sum(v.get("pdfs_empty", 0) for v in rows)
    failed = sum(v.get("pdfs_failed", 0) for v in rows)
    pdf_ocr = sum(v.get("pdfs_ocr", 0) for v in rows)
    img_ocr = sum(v.get("images_ocr", 0) for v in rows)
    ocr_status_hits = sum(1 for v in rows if v.get("status") == "ok-image-ocr")
    sites_with_img_menu = sum(1 for v in rows if v.get("image_menu_links", 0))
    print("\nMenu-format diagnostics:")
    print(f"  PDFs downloaded: {seen}  (text-parsed: {parsed}, image-only→OCR: {empty + pdf_ocr}, failed: {failed})")
    print(f"  OCR runs: {pdf_ocr} image-only PDFs, {img_ocr} menu images")
    print(f"  Tags from image OCR: {ocr_status_hits}")
    print(f"  Sites referencing menu image files (jpg/png/etc.): {sites_with_img_menu}")


if __name__ == "__main__":
    main()
