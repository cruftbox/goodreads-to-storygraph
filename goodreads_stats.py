"""Year-in-books stats pipeline.

Fetches the Goodreads read-shelf RSS, windows to the last 12 months,
looks up genres via the Google Books API (cached on disk), and renders
a single one-pager visual in three formats: PDF (Letter portrait),
Web PNG (1200x1800), and Social PNG (1080x1920, IG Story aspect).
"""

from __future__ import annotations

import argparse
import json
import logging
import sys
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from pathlib import Path
from typing import Optional

import requests
from bs4 import BeautifulSoup

import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
from matplotlib.gridspec import GridSpec
from matplotlib.patches import FancyBboxPatch, Rectangle


# -------- typography --------

# Try modern humanist sans-serifs in priority order. matplotlib falls through
# the list silently when a font isn't installed. DejaVu Sans is the matplotlib
# default and always present.
plt.rcParams["font.family"] = [
    "Inter", "Segoe UI Variable", "Segoe UI", "Arial", "DejaVu Sans",
]
plt.rcParams["font.size"] = 10
plt.rcParams["axes.unicode_minus"] = False
# Quiet the per-character findfont log spam when fallbacks aren't installed.
import logging as _logging
_logging.getLogger("matplotlib.font_manager").setLevel(_logging.ERROR)


# -------- constants --------

GOODREADS_RSS_URL = "https://www.goodreads.com/review/list_rss/{user_id}?shelf=read"
GOOGLE_BOOKS_URL = "https://www.googleapis.com/books/v1/volumes"
USER_AGENT = "Mozilla/5.0 (compatible; goodreads-to-storygraph/1.0)"

# Light editorial palette. Magazine-for-readers feel: warm cream page,
# clean white cards, deep saturated text and accents.
COLOR_PAGE_BG = "#f4efe4"
COLOR_CARD_BG = "#ffffff"
COLOR_PANEL_BG = COLOR_CARD_BG  # axes facecolors should match cards
COLOR_CARD_EDGE = "#e2dccc"     # subtle paper-edge stroke around cards
COLOR_BORDER = "#3a78c9"        # accent for the optional outer border

COLOR_TEXT_HIGH = "#1c2030"   # near-black for headlines & big numbers
COLOR_TEXT_BODY = "#3a3f50"   # body text on light cards
COLOR_TEXT_MUTED = "#7d8294"  # captions, dates, secondary metadata
COLOR_DIVIDER = "#e3ddcd"
COLOR_GRID = (0.0, 0.0, 0.0, 0.06)

# Saturated editorial accents that read on light backgrounds. Books blue,
# pages amber. Used consistently wherever those series appear.
COLOR_BOOKS = "#2c5fb8"
COLOR_PAGES = "#c2691f"

GENRE_PALETTE = [
    "#2c5fb8", "#3f8a52", "#c2691f", "#7e4ea8",
    "#c8961d", "#3a8aa6", "#8a8f9f",
]

# BISAC top-level categories that are too generic to be useful as a genre bucket.
# When Google Books returns ONLY one of these for a book (no subcategory), we
# treat that book as having no genre data rather than dumping it into a giant
# "Fiction" bar that swamps the chart.
GENERIC_TOP_LEVELS = frozenset({
    "Fiction", "Nonfiction", "Non-Fiction",
    "Juvenile Fiction", "Juvenile Nonfiction",
    "Young Adult Fiction", "Young Adult Nonfiction",
})

# Tags that show up in Goodreads' shelf data but aren't genres — they're
# format, age category, or shelf-management labels. Compared lowercase.
GENRE_STOPWORDS = frozenset({
    # Format / medium
    "audiobook", "audio book", "audio", "audiobooks",
    "ebook", "e-book", "e book", "ebooks", "kindle", "print", "books",
    # Age category
    "adult", "adults", "young adult", "ya", "new adult",
    "middle grade", "middle-grade", "middle grades",
    "children", "childrens", "children's", "kids", "kid", "picture books",
    # Status / shelf-management
    "read", "reading", "currently reading", "currently-reading",
    "want to read", "want-to-read", "to-read", "to read",
    "owned", "owned-books", "favorites", "favourites", "default",
    "reread", "re-read", "rereads", "did not finish", "dnf",
    "library", "borrowed", "wishlist", "to-buy",
    # Generic top-levels (also filtered by GENERIC_TOP_LEVELS)
    "fiction", "nonfiction", "non-fiction", "non fiction",
})

def _compute_formats(stats: "Stats") -> list:
    """Return per-format render configs sized to the actual data.

    PDF and web grow vertically with book count so the full 12-month list
    fits without cramping. Social stays fixed at the Instagram Story 9:16
    aspect and continues to truncate the list at 20 entries.
    """
    n = max(0, stats.total_books)
    # Tunables: each extra book adds this many vertical inches at the
    # PDF/web's native DPI scale. Tuned so a typical 30-book year reads
    # comfortably without dwarfing the charts above it.
    extra_per_book = 0.18
    extra_books = max(0, n - 8)
    web_h = 10.0 + extra_books * extra_per_book
    pdf_h = 10.5 + extra_books * extra_per_book
    return [
        {"name": "pdf",    "filename": "year_in_books.pdf",
         "size_in": (8.5, max(11.0, pdf_h)), "dpi": 100, "list_max": 200,
         "list_lines_estimate": n + 1},
        {"name": "web",    "filename": "year_in_books_web.png",
         "size_in": (8.0, max(10.5, web_h)), "dpi": 150, "list_max": 200,
         "list_lines_estimate": n + 1},
        {"name": "social", "filename": "year_in_books_social.png",
         "size_in": (6.0, 10.6667), "dpi": 180, "list_max": 20,
         "list_lines_estimate": min(20, n) + 1},
    ]


def _compute_height_ratios(n_list_lines: int) -> list:
    """Section ratios for the four-section layout: combined title+hero,
    monthly chart, genres, book list. Title and hero merged into one card
    so the top of the page doesn't feel like two empty boxes."""
    masthead = 2.10   # 3 lines of equal-size type with generous spacing
    chart = 2.80
    genres = 2.55
    # Bumped per-line allocation again (0.30 → 0.36) plus a smaller
    # baseline so the section title sits closer to the first entry.
    list_h = 0.45 + n_list_lines * 0.36
    return [masthead, chart, genres, list_h]


# -------- data classes --------

@dataclass
class Book:
    title: str
    author: str
    isbn: Optional[str]
    num_pages: Optional[int]
    user_read_at: datetime
    user_rating: Optional[int]
    goodreads_book_id: Optional[str] = None


@dataclass
class Stats:
    today: datetime
    window_start: datetime
    window_end: datetime
    total_books: int
    total_pages: int
    books_missing_pages: int
    books_per_month: list  # [(label, count), ...] oldest first
    pages_per_month: list
    book_titles: list      # [(date, title, author), ...] reverse-chronological
    books: list            # in-window Book objects
    first_name: Optional[str] = None  # for personalizing the title line


# -------- fetch --------

def fetch_read_shelf(user_id: str, timeout: int = 30) -> tuple:
    """Return (books, first_name). first_name is parsed from the RSS channel
    title (e.g., 'Michael\\'s bookshelf: read' -> 'Michael') and is None if
    the channel title doesn't match the expected pattern."""
    url = GOODREADS_RSS_URL.format(user_id=user_id)
    response = requests.get(url, headers={"User-Agent": USER_AGENT}, timeout=timeout)
    response.raise_for_status()
    soup = BeautifulSoup(response.text, "lxml-xml")

    first_name = None
    channel = soup.find("channel")
    if channel is not None:
        # Direct child <title> only — items also have <title> elements.
        for t in channel.find_all("title", recursive=False):
            first_name = _parse_first_name(t.text or "")
            break

    items = soup.find_all("item")
    books = []
    for item in items:
        try:
            books.append(_parse_item(item))
        except Exception as e:
            logging.warning("Skipping unparseable RSS item: %s", e)
    return books, first_name


def _parse_first_name(channel_title: str) -> Optional[str]:
    """Goodreads channel titles look like 'Michael's bookshelf: read'.
    Pull everything before \"'s bookshelf\" and take the first whitespace-
    separated token as the first name. Returns None if the pattern doesn't
    match."""
    if not channel_title:
        return None
    idx = channel_title.find("'s bookshelf")
    if idx < 0:
        # Some locales use a unicode right-single-quote; try that too.
        idx = channel_title.find("’s bookshelf")
    if idx < 0:
        return None
    name_blob = channel_title[:idx].strip()
    if not name_blob:
        return None
    return name_blob.split()[0]


def _text(elem) -> str:
    return elem.text.strip() if elem and elem.text else ""


def _parse_item(item) -> Book:
    title = _text(item.find("title"))
    author = _text(item.find("author_name"))

    isbn = _text(item.find("isbn")) or _text(item.find("isbn13")) or None

    num_pages_text = _text(item.find("num_pages"))
    num_pages = int(num_pages_text) if num_pages_text.isdigit() else None

    rating_text = _text(item.find("user_rating"))
    user_rating = int(rating_text) if rating_text.isdigit() and int(rating_text) > 0 else None

    read_at_text = _text(item.find("user_read_at")) or _text(item.find("user_date_added"))
    if not read_at_text:
        raise ValueError("no usable date")
    user_read_at = datetime.strptime(read_at_text, "%a, %d %b %Y %H:%M:%S %z").astimezone()

    # Pull the Goodreads book ID for the optional page-scrape genre fallback.
    book_id = _text(item.find("book_id")) or None
    if not book_id:
        book_elem = item.find("book")
        if book_elem is not None:
            attr_id = book_elem.get("id")
            if attr_id and attr_id.strip():
                book_id = attr_id.strip()

    return Book(
        title=title,
        author=author,
        isbn=isbn,
        num_pages=num_pages,
        user_read_at=user_read_at,
        user_rating=user_rating,
        goodreads_book_id=book_id,
    )


# -------- aggregate --------

def aggregate_last_12_months(
    books: list,
    today: Optional[datetime] = None,
    first_name: Optional[str] = None,
) -> Stats:
    if today is None:
        today = datetime.now().astimezone()
    window_start = today - timedelta(days=365)

    in_window = [b for b in books if window_start <= b.user_read_at <= today]

    months = _month_buckets_ending_at(today, 12)
    bpm = {label: 0 for label in months}
    ppm = {label: 0 for label in months}

    total_pages = 0
    books_missing_pages = 0
    for b in in_window:
        label = b.user_read_at.strftime("%b %Y")
        if label not in bpm:
            continue
        bpm[label] += 1
        if b.num_pages:
            ppm[label] += b.num_pages
            total_pages += b.num_pages
        else:
            books_missing_pages += 1

    titles = sorted(
        [(b.user_read_at, b.title, b.author) for b in in_window],
        key=lambda x: x[0],
        reverse=True,
    )

    return Stats(
        today=today,
        window_start=window_start,
        window_end=today,
        total_books=len(in_window),
        total_pages=total_pages,
        books_missing_pages=books_missing_pages,
        books_per_month=[(label, bpm[label]) for label in months],
        pages_per_month=[(label, ppm[label]) for label in months],
        book_titles=titles,
        books=in_window,
        first_name=first_name,
    )


def _month_buckets_ending_at(today: datetime, n: int) -> list:
    labels = []
    year, month = today.year, today.month
    for _ in range(n):
        labels.append(datetime(year, month, 1).strftime("%b %Y"))
        if month == 1:
            month = 12
            year -= 1
        else:
            month -= 1
    return list(reversed(labels))


# -------- genres --------

def lookup_genres(books: list, cache_path: Path, timeout: int = 10) -> dict:
    """Look up genre/category data for each book. Strategy:

    1. If the book has an ISBN, query Google Books by ISBN.
    2. If that returned nothing or only generic top-levels, try Google
       Books with a title+author query.
    3. If still nothing useful and we have a Goodreads book ID from the
       RSS, scrape the Goodreads book page for crowd-sourced genres.

    A cached entry that's empty or only-generic is treated as stale so a
    re-run benefits from the new fallback chain even if older Google-only
    cache data is on disk.
    """
    cache = _load_cache(cache_path)
    seen = set()
    for b in books:
        key = _book_key(b)
        if not key or key in seen:
            continue
        seen.add(key)
        if key in cache and cache[key] and not _is_only_generic(cache[key]):
            continue
        try:
            cats: list = []
            if b.isbn:
                cats = _query_google_books_isbn(b.isbn, timeout=timeout)
            if (not cats) or _is_only_generic(cats):
                ta_cats = _query_google_books_title_author(
                    b.title, b.author, timeout=timeout
                )
                if ta_cats and not _is_only_generic(ta_cats):
                    cats = ta_cats
                elif not cats:
                    cats = ta_cats
            if (not cats or _is_only_generic(cats)) and b.goodreads_book_id:
                gr_cats = _query_goodreads_genres(b.goodreads_book_id, timeout=timeout)
                if gr_cats:
                    cats = gr_cats
            cache[key] = cats
        except Exception as e:
            logging.warning("Genre lookup failed for %s: %s", key, e)
            cache[key] = []
    _save_cache(cache_path, cache)
    return cache


def _is_only_generic(cats: list) -> bool:
    """True when every category in the list is a generic top-level like
    "Fiction" (no sub-tag, no informative non-fiction top). Empty list
    returns False so callers handle the "no data" case separately."""
    if not cats:
        return False
    for cat in cats:
        parts = [p.strip() for p in cat.split(" / ") if p.strip()]
        if len(parts) >= 2:
            return False
        if parts and parts[0] not in GENERIC_TOP_LEVELS:
            return False
    return True


def _book_key(book) -> str:
    if book.isbn:
        return book.isbn
    title = (book.title or "").strip().lower()
    author = (book.author or "").strip().lower()
    if not (title or author):
        return ""
    return f"ta:{title}|{author}"


def _query_google_books_isbn(isbn: str, timeout: int = 10) -> list:
    response = requests.get(
        GOOGLE_BOOKS_URL,
        params={"q": f"isbn:{isbn}"},
        headers={"User-Agent": USER_AGENT},
        timeout=timeout,
    )
    return _extract_categories_from_response(response)


def _query_google_books_title_author(title: str, author: str, timeout: int = 10) -> list:
    parts = []
    if title and title.strip():
        parts.append(f'intitle:"{title.strip()}"')
    if author and author.strip():
        parts.append(f'inauthor:"{author.strip()}"')
    if not parts:
        return []
    response = requests.get(
        GOOGLE_BOOKS_URL,
        params={"q": "+".join(parts), "maxResults": 1},
        headers={"User-Agent": USER_AGENT},
        timeout=timeout,
    )
    return _extract_categories_from_response(response)


def _extract_categories_from_response(response) -> list:
    if response.status_code != 200:
        return []
    data = response.json()
    if not data.get("items"):
        return []
    info = data["items"][0].get("volumeInfo", {})
    return list(info.get("categories") or [])


# Module-level rate gate so we don't hammer Goodreads with parallel requests
# from a fast-running render. Single-threaded today, but this keeps us
# polite if anything ever calls into here concurrently.
_GR_REQUEST_DELAY_SEC = 0.4
_GR_LAST_REQUEST_AT = 0.0


def _query_goodreads_genres(book_id: str, timeout: int = 15) -> list:
    """Scrape the Goodreads book page for crowd-sourced genre tags. Returns
    a list of genre name strings (e.g., ["Fantasy", "Epic Fantasy"]) or an
    empty list if the page can't be parsed."""
    import time as _time

    global _GR_LAST_REQUEST_AT
    delta = _time.time() - _GR_LAST_REQUEST_AT
    if delta < _GR_REQUEST_DELAY_SEC:
        _time.sleep(_GR_REQUEST_DELAY_SEC - delta)
    _GR_LAST_REQUEST_AT = _time.time()

    url = f"https://www.goodreads.com/book/show/{book_id}"
    response = requests.get(
        url,
        headers={"User-Agent": USER_AGENT},
        timeout=timeout,
        allow_redirects=True,
    )
    if response.status_code != 200:
        return []
    return _extract_goodreads_genres(response.text)


def _extract_goodreads_genres(html: str) -> list:
    """Two-pattern extractor for Goodreads' genre tags. Tries the classic
    HTML pattern first, then walks the React __NEXT_DATA__ JSON blob for
    Genre objects. Returns deduped names in insertion order."""
    import re as _re

    soup = BeautifulSoup(html, "html.parser")

    found: list = []
    seen: set = set()

    def add(name: str) -> None:
        n = (name or "").strip()
        if not n or n.lower() in {"...more", "more"} or n in seen:
            return
        seen.add(n)
        found.append(n)

    # Pattern 1: anchor links to /genres/* — works on classic and many
    # current Goodreads pages.
    for a in soup.find_all("a", href=_re.compile(r"^/genres/")):
        text = a.get_text(strip=True)
        if text and len(text) <= 40:
            add(text)

    if found:
        return found[:15]

    # Pattern 2: React __NEXT_DATA__ JSON blob. Genre objects look like
    # {"__typename": "Genre", "name": "Fantasy", ...} nested in the apollo
    # cache. Walk the whole blob and harvest names.
    next_script = soup.find("script", id="__NEXT_DATA__")
    if next_script and next_script.string:
        try:
            blob = json.loads(next_script.string)
        except json.JSONDecodeError:
            blob = None
        if blob is not None:
            def visit(obj):
                if isinstance(obj, dict):
                    if obj.get("__typename") == "Genre" and obj.get("name"):
                        add(obj["name"])
                    for v in obj.values():
                        visit(v)
                elif isinstance(obj, list):
                    for v in obj:
                        visit(v)
            visit(blob)

    return found[:15]


def _load_cache(path: Path) -> dict:
    if not path.exists():
        return {}
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return {}


def _save_cache(path: Path, cache: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(cache, indent=2, sort_keys=True), encoding="utf-8")


def aggregate_genres(books: list, genres_by_key: dict) -> tuple:
    """Return (top_items, uncategorized_count) for the genre chart.

    Bucketing rules:
    - Per book, walk its category list in order. Take the first 3 distinct
      genre tags (after filtering format/age/shelf-management stop-words
      and generic top-levels like "Fiction" alone). This stops Goodreads'
      noisier 10-15-tag results from inflating an "Other" bar.
    - Each kept tag contributes 1 to that bucket's count.
    - Show the top 10 buckets, sorted by count (ties alphabetical). No
      "Other" rollup — readers care about what they read, not the long
      tail.
    - If the strict pass returns nothing, retry once allowing generic
      top-levels so a book tagged only "Fiction" still appears.
    """
    counter, uncategorized = _bucket_genres(books, genres_by_key, allow_generic=False)
    if not counter:
        counter, uncategorized = _bucket_genres(books, genres_by_key, allow_generic=True)

    if not counter:
        return ([], uncategorized)

    items = sorted(counter.items(), key=lambda kv: (-kv[1], kv[0]))
    return (items[:8], uncategorized)


def _bucket_genres(
    books: list,
    genres_by_key: dict,
    *,
    allow_generic: bool,
    max_per_book: int = 3,
) -> tuple:
    counter: dict = {}
    uncategorized = 0
    for b in books:
        cats = genres_by_key.get(_book_key(b), [])
        book_buckets: list = []
        for cat in cats:
            parts = [p.strip() for p in cat.split(" / ") if p.strip()]
            if len(parts) >= 2:
                bucket = parts[1]
            elif parts:
                if allow_generic or parts[0] not in GENERIC_TOP_LEVELS:
                    bucket = parts[0]
                else:
                    continue
            else:
                continue
            if bucket.lower() in GENRE_STOPWORDS:
                continue
            if bucket in book_buckets:
                continue
            book_buckets.append(bucket)
            if len(book_buckets) >= max_per_book:
                break
        if not book_buckets:
            uncategorized += 1
            continue
        for bucket in book_buckets:
            counter[bucket] = counter.get(bucket, 0) + 1
    return counter, uncategorized


# -------- render --------

def render_visual(stats: Stats, genre_data: tuple, output_dir: Path) -> dict:
    output_dir = Path(output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)
    formats = _compute_formats(stats)
    results = {}
    for fmt in formats:
        path = output_dir / fmt["filename"]
        _render_one(stats, genre_data, fmt, path)
        results[fmt["name"]] = path
    return results


def _render_one(stats: Stats, genre_data, fmt: dict, path: Path) -> None:
    fig = plt.figure(figsize=fmt["size_in"], facecolor=COLOR_PAGE_BG)

    n_list_lines = fmt.get("list_lines_estimate", stats.total_books + 1)
    gs = GridSpec(
        nrows=4, ncols=1,
        figure=fig,
        left=0.07, right=0.93, top=0.965, bottom=0.055,
        height_ratios=_compute_height_ratios(n_list_lines),
        hspace=0.45,
    )

    # Card backgrounds drawn first so they sit behind every chart artist.
    _draw_section_cards(fig, gs)

    _draw_masthead(fig.add_subplot(gs[0]), stats)
    _draw_books_per_month(fig.add_subplot(gs[1]), stats)
    genre_ax = fig.add_subplot(gs[2])
    _draw_genres(genre_ax, genre_data, stats)
    _set_subplot_left(genre_ax, 0.25)
    _draw_book_list(fig.add_subplot(gs[3]), stats, list_max=fmt["list_max"])

    _draw_footer(fig, stats)

    fig.savefig(path, dpi=fmt["dpi"], facecolor=COLOR_PAGE_BG)
    plt.close(fig)


def _set_subplot_left(ax, new_left: float) -> None:
    """Override one subplot's left edge while preserving its other bounds.
    Used for charts whose y-tick labels need more room than the global
    GridSpec left margin allows."""
    pos = ax.get_position()
    ax.set_position([new_left, pos.y0, pos.x1 - new_left, pos.height])


def _draw_section_cards(fig, gs) -> None:
    """Draw a slightly elevated 'card' behind each gridspec row so the
    sections are visually delineated against the darker page background."""
    n = gs.get_geometry()[0]
    for i in range(n):
        pos = gs[i].get_position(fig)
        # Generous bleed around each section, especially upward to cover the
        # section title area that lives in the gridspec's title pad.
        margin_x = 0.02
        margin_y_top = 0.030
        margin_y_bottom = 0.012
        x0 = pos.x0 - margin_x
        y0 = pos.y0 - margin_y_bottom
        w = (pos.x1 + margin_x) - x0
        h = (pos.y1 + margin_y_top) - y0
        card = FancyBboxPatch(
            (x0, y0), w, h,
            boxstyle="round,pad=0,rounding_size=0.012",
            linewidth=0.7,
            edgecolor=COLOR_CARD_EDGE,
            facecolor=COLOR_CARD_BG,
            transform=fig.transFigure,
            zorder=0,
        )
        fig.add_artist(card)


def _draw_footer(fig, stats: Stats) -> None:
    fig.text(
        0.5, 0.018,
        f"Source: Goodreads  ·  Generated {stats.window_end.strftime('%b %d, %Y')}",
        ha="center", va="center",
        color=COLOR_TEXT_MUTED, fontsize=8,
    )


def _strip_axes(ax):
    for spine in ax.spines.values():
        spine.set_visible(False)
    ax.tick_params(left=False, bottom=False, labelleft=False, labelbottom=False)
    ax.set_facecolor(COLOR_PANEL_BG)


def _draw_masthead(ax, stats: Stats):
    """Combined title + hero in a single card. Headline → date deck →
    big stat number → label, all in one section so we don't get two
    half-empty cards stacked at the top of the page."""
    _strip_axes(ax)

    if stats.first_name:
        headline = f"{stats.first_name}’s Year in Books"
    else:
        headline = "Your Year in Books"

    # All three lines at the same point size; visual hierarchy comes from
    # weight and color, not size variation.
    masthead_size = 20

    ax.text(0.5, 0.82, headline,
            ha="center", va="center",
            color=COLOR_TEXT_HIGH, fontsize=masthead_size, fontweight="bold",
            transform=ax.transAxes)

    date_range = f"{stats.window_start.strftime('%B %Y')} – {stats.window_end.strftime('%B %Y')}"
    ax.text(0.5, 0.50, date_range,
            ha="center", va="center",
            color=COLOR_TEXT_MUTED, fontsize=masthead_size, fontweight="regular",
            transform=ax.transAxes)

    if stats.total_books == 0:
        ax.text(0.5, 0.18, "No books read in the last 12 months.",
                ha="center", va="center",
                color=COLOR_TEXT_BODY, fontsize=masthead_size, fontweight="bold",
                transform=ax.transAxes)
        return

    summary = f"{stats.total_books:,} books finished in the last 12 months"
    ax.text(0.5, 0.18, summary,
            ha="center", va="center",
            color=COLOR_TEXT_HIGH, fontsize=masthead_size, fontweight="bold",
            transform=ax.transAxes)


def _draw_books_per_month(ax, stats: Stats):
    """Books per month as columns made of stacked book-spine rectangles —
    one rectangle per book read that month, slight color variation so the
    stack reads as separate volumes rather than a solid bar."""
    months = [_short_month_label(m[0]) for m in stats.books_per_month]
    values = [m[1] for m in stats.books_per_month]
    x = list(range(len(months)))

    ax.set_facecolor(COLOR_CARD_BG)

    # Editorial palette of book-spine colors — subtle variation, no clown
    # rainbow. The column reads as a stack of books on a shelf.
    spine_palette = [
        "#2c5fb8", "#3a78c9", "#4a87d6", "#1f4e9a",
        "#5a93de", "#274f9a", "#3d6ec0", "#5180c8",
    ]

    book_width = 0.62
    book_height = 0.78  # leaves a thin gap so stacked spines stay distinct
    book_gap_top = 0.06

    for i, count in enumerate(values):
        for b in range(count):
            color = spine_palette[(i + b) % len(spine_palette)]
            rect = Rectangle(
                (i - book_width / 2, b + book_gap_top),
                book_width, book_height,
                facecolor=color,
                edgecolor=COLOR_CARD_BG,
                linewidth=1.1,
                zorder=2,
            )
            ax.add_patch(rect)

    # Highlight the peak month inline.
    if any(values):
        peak_i = max(range(len(values)), key=lambda i: values[i])
        peak_v = values[peak_i]
        if peak_v > 0:
            ax.annotate(
                f"{peak_v} {'book' if peak_v == 1 else 'books'}",
                xy=(peak_i, peak_v),
                xytext=(0, 14), textcoords="offset points",
                ha="center", va="bottom",
                color=COLOR_TEXT_HIGH, fontsize=10, fontweight="semibold",
                zorder=4,
            )

    ax.set_title("Books read by month",
                 color=COLOR_TEXT_HIGH, fontsize=13, fontweight="semibold",
                 pad=14, loc="left")

    # Set explicit limits — Rectangle patches don't auto-scale axes.
    max_books = max(values) if values else 1
    ax.set_xlim(-0.6, len(values) - 0.4)
    ax.set_ylim(0, max(1, max_books) + 0.7)

    ax.set_xticks(x, labels=months)
    ax.tick_params(axis="x", colors=COLOR_TEXT_HIGH, labelsize=10.5,
                   length=0, pad=8)

    ax.tick_params(axis="y", colors=COLOR_TEXT_HIGH, labelsize=10.5,
                   length=0, pad=4)
    if max_books > 0:
        step = max(1, max_books // 4)
        ax.set_yticks(list(range(0, max_books + step, step)))

    for label in ax.get_xticklabels():
        label.set_fontweight("semibold")
    for label in ax.get_yticklabels():
        label.set_fontweight("semibold")

    for s in ("top", "left", "right"):
        ax.spines[s].set_visible(False)
    ax.spines["bottom"].set_color(COLOR_DIVIDER)
    ax.spines["bottom"].set_linewidth(0.8)
    ax.yaxis.grid(True, color=COLOR_GRID, linewidth=0.5)
    ax.set_axisbelow(True)


def _short_month_label(full_label: str) -> str:
    """'Jun 2025' -> 'Jun 25'. Year is always included so the 12-month
    window's December-to-January transition is unambiguous."""
    parts = full_label.split(" ")
    if len(parts) == 2 and len(parts[1]) == 4:
        return f"{parts[0]} {parts[1][2:]}"
    return full_label


def _draw_genres(ax, genre_data, stats: Stats):
    items, uncategorized = genre_data
    ax.set_facecolor(COLOR_PANEL_BG)
    ax.set_title("Top genres",
                 color=COLOR_TEXT_HIGH, fontsize=13, fontweight="semibold",
                 pad=14, loc="left")

    if not items:
        for spine in ax.spines.values():
            spine.set_visible(False)
        ax.tick_params(left=False, bottom=False, labelleft=False, labelbottom=False)
        if stats.total_books > 0:
            msg = (
                f"Google Books returned no genre data for any of the "
                f"{stats.total_books} books in the window. This usually means "
                f"the books in your read shelf RSS lacked ISBNs."
            )
        else:
            msg = "No books in the window."
        ax.text(0.5, 0.5, msg,
                ha="center", va="center",
                color=COLOR_TEXT_MUTED, fontsize=10, fontstyle="italic",
                wrap=True,
                transform=ax.transAxes)
        return

    labels = [_truncate_label(it[0], 18) for it in items]
    values = [it[1] for it in items]
    # Top bar uses the warm accent (the editorial highlight); rest fall
    # through the muted palette.
    colors = [COLOR_PAGES if i == 0 else GENRE_PALETTE[(i + 1) % len(GENRE_PALETTE)]
              for i in range(len(items))]
    y_positions = list(range(len(items)))
    bars = ax.barh(y_positions, values, color=colors, height=0.42)
    ax.set_yticks(y_positions, labels=labels)
    ax.invert_yaxis()
    ax.tick_params(axis="y", colors=COLOR_TEXT_HIGH, labelsize=10.5, length=0, pad=8)
    for label in ax.get_yticklabels():
        label.set_fontweight("bold")

    # Hide x-axis entirely — the count labels at bar end carry the values.
    ax.tick_params(axis="x", length=0, labelbottom=False)

    # Numeric label at the end of each bar
    max_val = max(values) if values else 1
    for bar, v in zip(bars, values):
        ax.text(bar.get_width() + max_val * 0.02,
                bar.get_y() + bar.get_height() / 2,
                f"{v}",
                ha="left", va="center",
                color=COLOR_TEXT_BODY, fontsize=10, fontweight="semibold")

    # Extend x range so labels fit without clipping
    ax.set_xlim(0, max_val * 1.18)

    # No spines, no grid — clean horizontal bars only
    for s in ax.spines.values():
        s.set_visible(False)

    if stats.total_books > 0 and uncategorized >= max(3, int(0.1 * stats.total_books)):
        plural = "s" if uncategorized != 1 else ""
        ax.text(1.0, -0.20, f"{uncategorized} book{plural} without genre data",
                ha="right", va="top",
                color=COLOR_TEXT_MUTED, fontsize=9, fontstyle="italic",
                transform=ax.transAxes)


def _draw_book_list(ax, stats: Stats, list_max: int = 50):
    _strip_axes(ax)
    ax.set_title("What you read in the last 12 months",
                 color=COLOR_TEXT_HIGH, fontsize=13, fontweight="semibold",
                 pad=2, loc="left")
    titles = stats.book_titles
    if not titles:
        ax.text(0.0, 0.96, "(none)",
                ha="left", va="top",
                color=COLOR_TEXT_MUTED, fontsize=10,
                transform=ax.transAxes)
        return

    truncated = len(titles) > list_max
    visible = titles[:list_max]
    n_lines = len(visible) + (1 if truncated else 0)

    # With variable figure height (the section grows to fit), we don't
    # need to shrink type aggressively — the figure simply gets taller for
    # readers who finish more books.
    if n_lines <= 24:
        font_size = 13
    elif n_lines <= 50:
        font_size = 12
    else:
        font_size = 11

    # Pull the first entry up close to the section title and use the full
    # vertical space — the section's pixel height grows with the book
    # count, so this gives consistent inches-per-line at any list size.
    start_y = 0.98
    available = 0.96
    line_height = available / max(n_lines, 1)
    # Reserve the rightmost slice of the row for the date column. Author
    # text is truncated post-render if it would extend into this band.
    date_col_left = 0.84

    # Three text artists per book: bold title (left), author (regular,
    # positioned flush after title via bbox measurement), date (right-
    # aligned, muted, in its reserved right column).
    title_artists = []
    for i, (date, title, author) in enumerate(visible):
        y = start_y - (i + 1) * line_height
        if y < 0.0:
            break
        clipped_title = title if len(title) <= 50 else title[:49] + "…"
        t_title = ax.text(0.0, y, clipped_title,
                          ha="left", va="top",
                          color=COLOR_TEXT_HIGH, fontsize=font_size, fontweight="bold",
                          transform=ax.transAxes)
        date_str = _short_month_label(date.strftime("%b %Y"))
        ax.text(1.0, y, date_str,
                ha="right", va="top",
                color=COLOR_TEXT_MUTED, fontsize=font_size, fontweight="normal",
                transform=ax.transAxes)
        title_artists.append((t_title, y, author))

    if truncated:
        i = len(visible)
        y = start_y - (i + 1) * line_height
        if y >= 0.0:
            ax.text(0.0, y, f"…and {len(titles) - list_max} more",
                    ha="left", va="top",
                    color=COLOR_TEXT_MUTED, fontsize=font_size, fontstyle="italic",
                    transform=ax.transAxes)

    # Second pass: place the author flush after the bold title, dynamically
    # truncated if it would otherwise spill into the date column.
    ax.figure.canvas.draw()
    inv = ax.transAxes.inverted()
    for t, y, author in title_artists:
        bbox = t.get_window_extent()
        x_end_axes, _ = inv.transform((bbox.x1, bbox.y0))
        # Estimate available width in characters before the date column
        # (rough but fine in practice; we err toward truncating).
        gap_axes = max(0.0, date_col_left - x_end_axes - 0.012)
        # Approximate axes-fraction-per-char at this font size, derived
        # from the rendered title bbox.
        title_w_axes = x_end_axes  # title was drawn from x=0
        chars_in_title = max(1, len(t.get_text()))
        per_char = title_w_axes / chars_in_title if chars_in_title else 0.012
        per_char = max(per_char, 0.006)  # floor so author isn't over-truncated
        max_author_chars = max(0, int(gap_axes / per_char) - 2)  # 2-char buffer for the leading gap
        if max_author_chars <= 0:
            continue
        author_text = author if len(author) <= max_author_chars else author[: max(2, max_author_chars - 1)] + "…"
        suffix = "   " + author_text
        ax.text(x_end_axes, y, suffix,
                ha="left", va="top",
                color=COLOR_TEXT_BODY, fontsize=font_size, fontweight="normal",
                transform=ax.transAxes)


def _truncate_label(s: str, max_len: int) -> str:
    return s if len(s) <= max_len else s[: max_len - 1] + "…"


# -------- end-to-end --------

def generate(user_id: str, output_dir: Path, today: Optional[datetime] = None) -> dict:
    output_dir = Path(output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)
    cache_path = output_dir / "genres_cache.json"

    books, first_name = fetch_read_shelf(user_id)
    stats = aggregate_last_12_months(books, today=today, first_name=first_name)

    if stats.total_books == 0:
        genre_data = ([], 0)
    else:
        genres_by_isbn = lookup_genres(stats.books, cache_path)
        genre_data = aggregate_genres(stats.books, genres_by_isbn)

    paths = render_visual(stats, genre_data, output_dir)
    return {
        "total_books": stats.total_books,
        "total_pages": stats.total_pages,
        "books_missing_pages": stats.books_missing_pages,
        "outputs": {name: str(p) for name, p in paths.items()},
    }


def main(argv: Optional[list] = None) -> int:
    parser = argparse.ArgumentParser(
        description="Generate a 'year in books' visualization from a Goodreads read shelf."
    )
    parser.add_argument("--user-id", help="Goodreads user ID (else read from config.json).")
    parser.add_argument("--output-dir", default="output", help="Output directory (default: output).")
    parser.add_argument("--config", default="config.json", help="Path to config.json.")
    args = parser.parse_args(argv)

    user_id = args.user_id
    if not user_id:
        config_path = Path(args.config)
        if not config_path.exists():
            print(f"config not found at {config_path} and no --user-id supplied", file=sys.stderr)
            return 2
        config = json.loads(config_path.read_text(encoding="utf-8"))
        user_id = config.get("goodreads_user_id")
        if not user_id:
            print("goodreads_user_id missing from config.json", file=sys.stderr)
            return 2

    logging.basicConfig(level=logging.INFO, format="%(levelname)s: %(message)s")
    result = generate(user_id, Path(args.output_dir))
    print(json.dumps(result, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
