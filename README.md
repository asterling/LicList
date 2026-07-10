# Summerlicious 2026 Menu Explorer

A fast, filterable, mobile-friendly browser for every restaurant participating in **Toronto's Summerlicious 2026** prix-fixe festival (Jul 3–16, 2026). The same site auto-flips between Winterlicious and Summerlicious as the City rotates campaigns.

Live: **https://asterling.github.io/WinterLicList/**

![Screenshot](og-image.png)

## Why this exists

The official Winterlicious site is slow, paginated, and hard to filter. This one loads every menu at once and lets you slice the list however you like:

- 🔎 **Search** restaurant names and dishes
- 🍱 **Filter** by cuisine, neighbourhood, lunch/dinner price
- 🌱 **Veg / Vegan only** toggle (only restaurants with qualifying items)
- ⭐ **Michelin** highlight
- ❤️ **Favorites** saved locally
- 🗺️ **Map view** powered by Leaflet + OpenStreetMap
- 🔗 **Deep links** — share any restaurant: `/#r=canoe`
- 📱 **Mobile friendly**

## How it works

- `winterlic.py` — Python scraper hitting Toronto's open-data endpoint. Auto-detects whether the active campaign is Winter- or Summerlicious based on the calendar (override with `LICIOUS_SEASON=Winter|Summer`).
- `menus-latest.json` — the freshest snapshot, always the file the site loads first.
- `menus-{Winterlicious,Summerlicious}-YYYY.json` — per-campaign archives.
- `season.json` — `{season, year, label, fetched_at}`; drives the page title.
- `index.html` / `styles.css` / `app.js` — static frontend, no build step, no framework.

Open `index.html` over `http://` (not `file://`) so the JSON fetch succeeds. The quickest local server:

```bash
python3 -m http.server 8000
# then open http://localhost:8000
```

## Refreshing the data

```bash
python3 -m pip install curl_cffi
python3 winterlic.py
```

> The City's endpoint sits behind Akamai Bot Manager, which blocks plain
> `requests`/`curl` by TLS fingerprint. `winterlic.py` uses `curl_cffi` to
> impersonate Chrome's TLS handshake — that's the only reason the fetch works.

This rewrites `menus-latest.json` (and the per-campaign archive) from the city's live endpoint.

A GitHub Action runs this on a schedule — see `.github/workflows/refresh-data.yml`.

## AI enrichment (optional, local LLM)

Each restaurant in the JSON can be augmented with four AI-generated fields:

- `ai.standouts` — 3–5 menu items the LLM thinks are worth flagging, taken verbatim from the menu
- `ai.vibe_tags` — 3–5 short tags from a controlled list ("date night", "casual", "upscale", "groups", "trendy", "cozy", "celebration", "hidden gem", "neighbourhood favourite", …)
- `ai.one_liner` — a single-sentence pitch that replaces boilerplate city descriptions on the card and modal
- `ai.dietary_summary` — *deterministic*, not LLM: counts veg / vegan / GF items from the city's flags

The enrichment runs entirely **locally** via [Ollama](https://ollama.com). No API keys, no per-query cost. Results are cached by content hash in `enrichment-cache.json`, so unchanged menus skip the LLM on subsequent runs.

### One-time setup

1. Install Ollama — either the prebuilt installer from <https://ollama.com/download>, or build from source:

   ```bash
   git clone --depth=1 https://github.com/ollama/ollama
   cd ollama
   # macOS users on Xcode 15+ may need: -ldflags="-extldflags=-Wl,-ld_classic"
   go build -o ollama .
   ```

2. Start the Ollama server (leave it running in another terminal):

   ```bash
   ollama serve
   ```

3. Pull the default model (~4.7 GB on disk, ~5 GB RAM at runtime):

   ```bash
   ollama pull qwen2.5:7b
   ```

   Smaller alternatives if you're tight on RAM (lower quality):

   - `qwen2.5:3b` — ~2 GB
   - `qwen2.5:1.5b` — ~1 GB (often hallucinates dish names; not recommended)

### Running it

```bash
# Fetch + enrich in one shot
python3 winterlic.py --enrich

# Just enrich the JSON you already have
python3 winterlic.py --no-fetch --enrich

# Pick a different model
python3 winterlic.py --enrich --model qwen2.5:3b

# First N restaurants only (handy when iterating on prompts)
python3 winterlic.py --enrich --limit 10

# Force re-enrichment ignoring the cache
python3 enrich.py menus-latest.json --force
```

Runtimes on Apple Silicon for the full 240-restaurant sweep:

| Model | Steady-state | First-run total | Cached re-run |
|---|---|---|---|
| `qwen2.5:7b` | ~5 s/restaurant | ~20 min | seconds |
| `qwen2.5:3b` | ~3 s/restaurant | ~12 min | seconds |
| `qwen2.5:1.5b` | ~3 s/restaurant | ~12 min | seconds |

The frontend reads `ai.*` fields if present and falls back gracefully if they're missing, so enrichment is always optional.

## Kids-menu tags (optional, local scrape)

`kids_menu.py` flags restaurants that appear to offer a kids'/children's menu — **not** part of the Summerlicious deal, but handy for parents. It drives a headless Chromium (Playwright) to render each restaurant's website (most menus are JavaScript-rendered), follows a couple of menu/kids links, and **downloads any linked menu PDFs and reads them with `pdftotext`** (many independents publish PDF menus). Image-only PDFs and standalone menu images (jpg/png) are **OCR'd with Tesseract** (PDF pages are rendered to images via poppler's `pdftoppm` first). It looks for phrases like "kids menu", "children's menu", "bambini", "menu enfant". Matches set `kids_menu: true` on the record; the frontend shows a 🧒 badge, a "Kids menu" filter, and a clickable stat. Results cache by URL in `kids-menu-cache.json` (gitignored), including per-site menu-format diagnostics (PDFs parsed/OCR'd, image menus seen), summarized at the end of a run.

Detection is high-precision but low-recall: a tag means we found clear evidence; a missing tag just means we didn't (the menu may be gated behind an ordering widget, or an image too stylized/low-res for OCR). Tags are **sticky** — a confirmed tag survives a later run that can't re-reach the site (scraping isn't deterministic), and `winterlic.py` preserves the tag across data refreshes.

```bash
pip install playwright && python3 -m playwright install chromium
brew install poppler tesseract   # pdftotext/pdftoppm + OCR
python3 kids_menu.py            # scrape all, cache-aware
python3 kids_menu.py --limit 15 # smoke test
python3 kids_menu.py --force    # ignore cache
```

## Data source

`https://secure.toronto.ca/c3api_data/v2/DataAccess.svc/Licious/map_data` — City of Toronto open data, no API key required.

## License

MIT. Not affiliated with the City of Toronto or Winterlicious.
