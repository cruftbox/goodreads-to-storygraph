# Year in Books — Web UI and Visualization

**Date:** 2026-05-10
**Status:** Approved by user, ready for implementation plan
**Repo:** cruftbox/goodreads-tools

## Goal

Add a small local web frontend to the existing Goodreads → StoryGraph sync, with two buttons:

1. **Sync to StoryGraph** — launches the existing `book_sync.py` unchanged.
2. **Generate Year in Books** — fetches the last 12 months of reading from Goodreads and produces a single composite "year in books" visual in three formats (PDF, web PNG, social/mobile PNG).

The visual style follows the StoryGraph reference panels the user shared (dark background, rounded blue accent border, blue/orange chart colors).

## Out of scope

- No changes to `book_sync.py`. The sync script is fragile; the web UI wraps it as a subprocess.
- No login or auth on the web UI — runs on `127.0.0.1` only.
- No ratings histogram. Considered (RSS exposes `<user_rating>`) but excluded by user; can be added later as a small follow-up.
- No deployment story. This is a local tool launched with `python app.py`.
- No automated tests. Verification is a manual smoke test, matching the existing project posture.

## Architecture

**Flask** app in `app.py`, binding to `127.0.0.1:5000`. Single HTML page with two buttons, plus a status/log area and a downloads area. Two routes back the buttons:

- `POST /sync` → spawns `python book_sync.py` as a subprocess. Stdout/stderr piped to `output/sync_<timestamp>.log`. A module-level lock prevents concurrent syncs.
- `GET /sync-log/<id>?offset=N` → returns log bytes from offset; the page polls this every 2s while the sync is running.
- `POST /generate-stats` → runs the stats pipeline synchronously (~10-15 s incl. genre lookups). Returns JSON with paths to the three generated files.
- `GET /output/<filename>` → static-serves files from `output/`.

`book_sync.py`, `config.json`, and the existing CLI behavior are untouched. `python book_sync.py` keeps working standalone for users who don't want the web UI.

### File layout (new)

```
app.py                      Flask server, routes, log streaming, sync mutex
goodreads_stats.py          RSS fetch, 12-month windowing, Google Books lookup, render
templates/index.html        Single page: two buttons, log area, download links
static/style.css            Dark theme matching reference image aesthetic
output/                     Generated PDFs/PNGs and sync logs (gitignored)
docs/superpowers/specs/...  This spec
```

## Stats pipeline

`goodreads_stats.py` runs three steps end to end:

### 1. Fetch read shelf

```
GET https://www.goodreads.com/review/list_rss/{user_id}?shelf=read
```

Parsed with `BeautifulSoup(text, 'lxml-xml')` (same pattern as `book_sync.py`). Each `<item>` produces a `Book` dataclass with: `title`, `author`, `isbn`, `num_pages: int | None`, `user_read_at: datetime` (tz-aware, normalized via `.astimezone()`), `user_rating: int | None`.

Edge cases:
- `num_pages` empty → `None`. Excluded from the page-total *for that book only*; counted in the "books with no page count" disclosure.
- `isbn` empty → genre lookup skipped for that book; bucketed as `"Uncategorized"`.
- RSS fetch returns non-200 or zero items → return HTTP 502 with a friendly error to the page.

### 2. Window and aggregate

`aggregate_last_12_months(books, today) -> Stats`:

- Filter to `today - 365 days <= user_read_at <= today`.
- Bucket into 12 calendar months, oldest → newest. For `today=2026-05-10` that's `2025-06` through `2026-05`.
- Produce `Stats` with: `total_books`, `total_pages`, `books_per_month: list[(month_label, count)]`, `pages_per_month: list[(month_label, count)]`, `book_titles: list[(date, title, author)]` reverse-chronological.

### 3. Genre lookup (Google Books API)

For each in-window book with an ISBN:

```
GET https://www.googleapis.com/books/v1/volumes?q=isbn:{isbn}
```

Response field: `items[0].volumeInfo.categories` — a list of BISAC strings like `"Fiction / Science Fiction / Hard Science Fiction"`. Counting rule:

- Split each category string on `" / "`, take the first level (`"Fiction"`, `"Juvenile Fiction"`, etc.) and the second level if present (`"Science Fiction"`).
- For chart bars, use the **second level** when available (more interesting), falling back to the first.
- Count every category a book has — a book with two categories contributes to both bars.
- Aggregate top 5-7 by count. Ties broken by alphabetical order. Categories with count 1 are kept only if total distinct categories ≤ 7, otherwise rolled into `"Other"`.

Caching:
- Cache responses to `output/genres_cache.json` keyed by ISBN. Re-generates skip the HTTP call for any cached ISBN.
- Cache is gitignored.

Failures (no API key, network error, ISBN not in Google Books, missing `categories`):
- Counted under `"Uncategorized"`.
- If `"Uncategorized"` is non-trivial (≥10% of books in window), it's shown as a bar; otherwise hidden and disclosed in a small note under the chart: *"3 books had no genre data."*

No API key required for the volumes endpoint at this volume (~30-100 calls per generate, well under the 1000/day unauth quota). If we hit quota, the failure mode above kicks in cleanly.

### 4. Render

One matplotlib figure built with `gridspec`, six rows:

1. Title block — *"Your Year in Books"*, with date range subtitle.
2. Summary — large type, *"You finished N books and P pages in the last 12 months"*. Disclosure line if any books lacked page counts.
3. Books-per-month bar chart, 12 bars.
4. Pages-per-month bar chart, 12 bars.
5. Genres horizontal bar chart, top 5-7 buckets.
6. Book list, reverse-chronological — `Title — Author (Mon YYYY)` per line.

Three render passes write three files. Filenames are deterministic and overwritten on each generate:

| Output | Path | Size | Aspect | DPI |
|---|---|---|---|---|
| PDF | `output/year_in_books.pdf` | 8.5 × 11 in (Letter portrait) | 17:22 | vector |
| Web PNG | `output/year_in_books_web.png` | 1200 × 1800 px | 2:3 | 150 |
| Social PNG | `output/year_in_books_social.png` | 1080 × 1920 px | 9:16 (IG Story) | 150 |

Layout differs only in the book list:
- **PDF**: full list, single column, font scaled to fit (9pt floor; if still overflowing, paginate to a second page).
- **Web PNG**: full list; switches to two columns if single-column would overflow.
- **Social PNG**: top 20 most recent titles only, with `…and N more` if truncated.

## Visual design

**Colors** (sampled from reference images):

| Element | Hex |
|---|---|
| Page background (outside panel) | `#1a1a1a` |
| Panel background | `#2b2b2b` |
| Border accent | `#5fa8d3` (rounded ~16px, ~3px stroke) |
| Title text | `#ffffff` bold |
| Body text | `#e5e5e5` |
| Muted/subtitle | `#9a9a9a` |
| Books bar fill | `#7fb3ff` |
| Pages bar fill | `#e85a3a` |
| Genre bars | `#7fb3ff`, varied per row using a fixed accent palette |
| Grid lines | white at 12% opacity |

**Font.** matplotlib default `DejaVu Sans` is acceptable. If Inter or another humanist sans is installed, set it via `rcParams['font.family']`.

**Border / rounded corners.** matplotlib's figure patch doesn't natively round corners; we draw a rounded-rect path as the figure background to match the reference panels.

## Sync UX

- Click **Sync to StoryGraph** → button disables, spinner shows, server spawns `book_sync.py`. The user sees Chrome pop up exactly as today.
- Page polls `/sync-log/<id>` every 2s and appends new lines to a scrolling, monospace log area.
- A module-level `threading.Lock` prevents concurrent runs. A second click while one is running gets a "Sync already running" toast.
- On subprocess exit, status flips to ✅ Done or ❌ Failed (with link to the full log file). Button re-enables.

## Stats UX

- Click **Generate Year in Books** → button disables briefly. Stats pipeline runs synchronously.
- Three download links appear: **PDF**, **Web PNG**, **Social PNG**. Files served from `/output/`.
- Re-clicking regenerates from a fresh RSS fetch (genre cache still hit) and overwrites the three files. The links update.

## Page layout

```
┌─────────────────────────────────────────┐
│  Goodreads Tools                        │
│                                         │
│  [ Sync to StoryGraph ]                 │
│  [ Generate Year in Books ]             │
│                                         │
│  ─── Status ───                         │
│  (last sync log tail / stats result)    │
│                                         │
│  ─── Downloads ───                      │
│  [ PDF ]  [ Web PNG ]  [ Social PNG ]   │
└─────────────────────────────────────────┘
```

Same dark theme as the visuals, simpler styling.

## Config and credentials

Reuses the existing `config.json` `goodreads_user_id`. No new config needed. The Google Books volumes endpoint is unauthenticated for this volume.

## Repo changes summary

**New files:**
- `app.py`
- `goodreads_stats.py`
- `templates/index.html`
- `static/style.css`
- `docs/superpowers/specs/2026-05-10-year-in-books-design.md` (this file)

**Modified:**
- `requirements.txt` — add `flask==3.0.0`, `matplotlib==3.8.2`
- `.gitignore` — add `output/`
- `README.md` — new "Web UI" section with launch instructions

**Untouched:**
- `book_sync.py`
- `config.json`
- `LICENSE`

## Acceptance criteria

- `pip install -r requirements.txt` then `python app.py` opens a working page at `http://127.0.0.1:5000`.
- Clicking **Sync to StoryGraph** runs the existing sync end-to-end (Chrome opens, books sync, log streams to the page) without any change in observed behavior vs. running the CLI directly.
- A second click during an active sync is rejected with a visible message.
- Clicking **Generate Year in Books** produces three files in `output/` (PDF, web PNG, social PNG), each containing the panels listed in §Render in the order listed, with theming matching §Visual design.
- The generated visuals correctly cover the rolling 12-month window (e.g., on 2026-05-10 the buckets are 2025-06 through 2026-05).
- Books missing page counts don't crash the pages chart; missing genre lookups don't crash the genre chart; both surface a small disclosure line when relevant.
- Re-clicking **Generate Year in Books** uses the genre cache and completes faster on the second run.
- `python book_sync.py` continues to work standalone, unchanged.
