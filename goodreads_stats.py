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
from matplotlib.patches import FancyBboxPatch


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

# Refined dark palette. Page sits darkest; section "cards" sit slightly
# elevated above it for delineation. Axes facecolors match the card color.
COLOR_PAGE_BG = "#0d1016"
COLOR_CARD_BG = "#1a1f2c"
COLOR_PANEL_BG = COLOR_CARD_BG  # axes facecolors should match cards
COLOR_BORDER = "#6ba8e8"

COLOR_TEXT_HIGH = "#f4f5f9"   # large/bold/heading text
COLOR_TEXT_BODY = "#cfd3df"   # default body
COLOR_TEXT_MUTED = "#7c8497"  # captions, dates, secondary metadata
COLOR_DIVIDER = "#262d3a"
COLOR_GRID = (1.0, 1.0, 1.0, 0.05)

# Two accent colors used consistently across the figure: blue for "books"
# everywhere it appears, amber for "pages." Genre palette uses the same
# blue first so the visual hierarchy stays coherent.
COLOR_BOOKS = "#7fb3ff"
COLOR_PAGES = "#f3a657"

GENRE_PALETTE = [
    "#7fb3ff", "#9bdb87", "#f3a657", "#c499ff",
    "#ffd166", "#5fa8d3", "#a0a8b9",
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

OUTPUT_FORMATS = [
    {"name": "pdf",    "filename": "year_in_books.pdf",        "size_in": (8.5, 11.0),       "dpi": 100, "list_max": 50},
    {"name": "web",    "filename": "year_in_books_web.png",    "size_in": (8.0, 12.0),       "dpi": 150, "list_max": 50},
    {"name": "social", "filename": "year_in_books_social.png", "size_in": (6.0, 10.6667),    "dpi": 180, "list_max": 20},
]


# -------- data classes --------

@dataclass
class Book:
    title: str
    author: str
    isbn: Optional[str]
    num_pages: Optional[int]
    user_read_at: datetime
    user_rating: Optional[int]


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


# -------- fetch --------

def fetch_read_shelf(user_id: str, timeout: int = 30) -> list:
    url = GOODREADS_RSS_URL.format(user_id=user_id)
    response = requests.get(url, headers={"User-Agent": USER_AGENT}, timeout=timeout)
    response.raise_for_status()
    soup = BeautifulSoup(response.text, "lxml-xml")
    items = soup.find_all("item")
    books = []
    for item in items:
        try:
            books.append(_parse_item(item))
        except Exception as e:
            logging.warning("Skipping unparseable RSS item: %s", e)
    return books


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

    return Book(
        title=title,
        author=author,
        isbn=isbn,
        num_pages=num_pages,
        user_read_at=user_read_at,
        user_rating=user_rating,
    )


# -------- aggregate --------

def aggregate_last_12_months(books: list, today: Optional[datetime] = None) -> Stats:
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
    cache = _load_cache(cache_path)
    isbns_seen_this_run = set()
    for b in books:
        if not b.isbn or b.isbn in isbns_seen_this_run:
            continue
        isbns_seen_this_run.add(b.isbn)
        if b.isbn in cache:
            continue
        try:
            cache[b.isbn] = _query_google_books(b.isbn, timeout=timeout)
        except Exception as e:
            logging.warning("Genre lookup failed for ISBN %s: %s", b.isbn, e)
            cache[b.isbn] = []
    _save_cache(cache_path, cache)
    return cache


def _query_google_books(isbn: str, timeout: int = 10) -> list:
    response = requests.get(
        GOOGLE_BOOKS_URL,
        params={"q": f"isbn:{isbn}"},
        headers={"User-Agent": USER_AGENT},
        timeout=timeout,
    )
    if response.status_code != 200:
        return []
    data = response.json()
    if not data.get("items"):
        return []
    info = data["items"][0].get("volumeInfo", {})
    return list(info.get("categories") or [])


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


def aggregate_genres(books: list, genres_by_isbn: dict) -> tuple:
    """Return (top_items, uncategorized_count).

    Per book: collect distinct sub-genre buckets (BISAC second level when
    present, otherwise first level). A book contributes 1 to each unique
    bucket it has — never multiple counts for the same bucket per book.
    Books that returned no categories from Google Books at all count as
    uncategorized.

    First pass tries to use only specific buckets (skipping generic
    top-levels like "Fiction" alone). If that yields nothing, falls back
    to using the generic top-levels too — better to show "Fiction" than
    to show "Genre data unavailable" when Google Books only returned
    coarse categories for every book.
    """
    counter, uncategorized = _bucket_genres(books, genres_by_isbn, allow_generic=False)
    if not counter:
        # Try again allowing generic top-levels — last-resort fallback.
        counter, uncategorized = _bucket_genres(books, genres_by_isbn, allow_generic=True)

    if not counter:
        return ([], uncategorized)

    items = sorted(counter.items(), key=lambda kv: (-kv[1], kv[0]))
    if len(items) > 7:
        top = items[:6]
        rest = items[6:]
        other_count = sum(c for _, c in rest)
        if other_count > 0:
            items = top + [("Other", other_count)]
        else:
            items = top
    return (items, uncategorized)


def _bucket_genres(books: list, genres_by_isbn: dict, *, allow_generic: bool) -> tuple:
    counter: dict = {}
    uncategorized = 0
    for b in books:
        cats = genres_by_isbn.get(b.isbn, []) if b.isbn else []
        book_buckets: set = set()
        for cat in cats:
            parts = [p.strip() for p in cat.split(" / ") if p.strip()]
            if len(parts) >= 2:
                book_buckets.add(parts[1])
            elif parts:
                if allow_generic or parts[0] not in GENERIC_TOP_LEVELS:
                    book_buckets.add(parts[0])
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
    results = {}
    for fmt in OUTPUT_FORMATS:
        path = output_dir / fmt["filename"]
        _render_one(stats, genre_data, fmt, path)
        results[fmt["name"]] = path
    return results


def _render_one(stats: Stats, genre_data, fmt: dict, path: Path) -> None:
    fig = plt.figure(figsize=fmt["size_in"], facecolor=COLOR_PAGE_BG)

    gs = GridSpec(
        nrows=5, ncols=1,
        figure=fig,
        left=0.09, right=0.91, top=0.93, bottom=0.06,
        height_ratios=[0.85, 1.20, 1.80, 1.50, 2.80],
        hspace=0.65,
    )

    # Card backgrounds drawn first so they sit behind every chart artist.
    _draw_section_cards(fig, gs)

    _draw_title(fig.add_subplot(gs[0]), stats)
    _draw_hero(fig.add_subplot(gs[1]), stats)
    _draw_combined_chart(fig.add_subplot(gs[2]), stats)
    genre_ax = fig.add_subplot(gs[3])
    _draw_genres(genre_ax, genre_data, stats)
    _set_subplot_left(genre_ax, 0.22)
    _draw_book_list(fig.add_subplot(gs[4]), stats, list_max=fmt["list_max"])

    _draw_footer(fig, stats)

    # Rounded accent border, drawn last so it sits cleanly on top of any
    # other figure content.
    border = FancyBboxPatch(
        (0.025, 0.02), 0.95, 0.96,
        boxstyle="round,pad=0,rounding_size=0.025",
        linewidth=2,
        edgecolor=COLOR_BORDER,
        facecolor="none",
        transform=fig.transFigure,
        zorder=1000,
        clip_on=False,
    )
    fig.add_artist(border)

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
            linewidth=0,
            facecolor=COLOR_CARD_BG,
            transform=fig.transFigure,
            zorder=0,
        )
        fig.add_artist(card)


def _draw_footer(fig, stats: Stats) -> None:
    fig.text(
        0.5, 0.038,
        f"Source: Goodreads  ·  Generated {stats.window_end.strftime('%b %d, %Y')}",
        ha="center", va="center",
        color=COLOR_TEXT_MUTED, fontsize=8,
    )


def _strip_axes(ax):
    for spine in ax.spines.values():
        spine.set_visible(False)
    ax.tick_params(left=False, bottom=False, labelleft=False, labelbottom=False)
    ax.set_facecolor(COLOR_PANEL_BG)


def _draw_title(ax, stats: Stats):
    """Headline + deck. Tried a kicker-headline-deck stack but it crowded
    in the available section height; kept it to two lines for clarity."""
    _strip_axes(ax)
    ax.text(0.5, 0.62, "Your Year in Books",
            ha="center", va="center",
            color=COLOR_TEXT_HIGH, fontsize=24, fontweight="bold",
            transform=ax.transAxes)
    date_range = f"{stats.window_start.strftime('%B %Y')} – {stats.window_end.strftime('%B %Y')}"
    ax.text(0.5, 0.22, date_range,
            ha="center", va="center",
            color=COLOR_TEXT_MUTED, fontsize=11,
            transform=ax.transAxes)


def _draw_hero(ax, stats: Stats):
    """Big stat number, label below, secondary stat in a smaller line."""
    _strip_axes(ax)

    if stats.total_books == 0:
        ax.text(0.5, 0.5, "No books read in the last 12 months.",
                ha="center", va="center",
                color=COLOR_TEXT_BODY, fontsize=14, fontweight="bold",
                transform=ax.transAxes)
        return

    # Hero stat: bigger than body type but not dominating; the charts are
    # the focus. Color is reserved for charts; size carries the emphasis.
    ax.text(0.5, 0.72, f"{stats.total_books:,}",
            ha="center", va="center",
            color=COLOR_TEXT_HIGH, fontsize=48, fontweight="bold",
            transform=ax.transAxes)
    ax.text(0.5, 0.30, "books finished",
            ha="center", va="center",
            color=COLOR_TEXT_BODY, fontsize=12, fontweight="semibold",
            transform=ax.transAxes)


def _draw_combined_chart(ax, stats: Stats):
    """Books-per-month line chart with a subtle filled area under the line.
    Highlights the peak month with an inline annotation."""
    months = [_short_month_label(m[0]) for m in stats.books_per_month]
    books_values = [m[1] for m in stats.books_per_month]
    x = list(range(len(months)))

    ax.set_facecolor(COLOR_CARD_BG)

    ax.fill_between(x, books_values, color=COLOR_BOOKS, alpha=0.14, zorder=1)
    ax.plot(x, books_values, color=COLOR_BOOKS, linewidth=2.4,
            marker="o", markersize=5.5, markerfacecolor=COLOR_BOOKS,
            markeredgecolor=COLOR_CARD_BG, markeredgewidth=1.5,
            zorder=3)

    # Annotate the peak — the single highlighted data point in NYT/Bloomberg
    # tradition.
    if any(books_values):
        peak_i = max(range(len(books_values)), key=lambda i: books_values[i])
        peak_v = books_values[peak_i]
        if peak_v > 0:
            ax.annotate(
                f"{peak_v} {'book' if peak_v == 1 else 'books'}",
                xy=(peak_i, peak_v),
                xytext=(0, 12), textcoords="offset points",
                ha="center", va="bottom",
                color=COLOR_TEXT_HIGH, fontsize=10, fontweight="semibold",
                zorder=4,
            )

    ax.set_title("Books read by month",
                 color=COLOR_TEXT_HIGH, fontsize=13, fontweight="semibold",
                 pad=14, loc="left")

    # X axis
    ax.set_xticks(x, labels=months)
    ax.tick_params(axis="x", colors=COLOR_TEXT_MUTED, labelsize=8.5,
                   length=0, pad=6)

    # Left y axis: integer ticks for books
    ax.tick_params(axis="y", colors=COLOR_TEXT_MUTED, labelsize=8.5,
                   length=0, pad=4)
    max_books = max(books_values) if books_values else 0
    if max_books > 0:
        step = max(1, max_books // 4)
        ax.set_yticks(list(range(0, max_books + step, step)))
    ax.set_ylim(bottom=0)

    # Spines off; subtle bottom rule and gridlines
    for s in ("top", "left", "right"):
        ax.spines[s].set_visible(False)
    ax.spines["bottom"].set_color(COLOR_DIVIDER)
    ax.spines["bottom"].set_linewidth(0.8)
    ax.yaxis.grid(True, color=COLOR_GRID, linewidth=0.5)
    ax.set_axisbelow(True)


def _short_month_label(full_label: str) -> str:
    """'Jun 2025' -> 'Jun '25'. Year is always included so the 12-month
    window's December-to-January transition is unambiguous."""
    parts = full_label.split(" ")
    if len(parts) == 2 and len(parts[1]) == 4:
        return f"{parts[0]} '{parts[1][2:]}"
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
    bars = ax.barh(y_positions, values, color=colors, height=0.55)
    ax.set_yticks(y_positions, labels=labels)
    ax.invert_yaxis()
    ax.tick_params(axis="y", colors=COLOR_TEXT_BODY, labelsize=10, length=0, pad=8)

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
    ax.set_title("What you read",
                 color=COLOR_TEXT_HIGH, fontsize=13, fontweight="semibold",
                 pad=6, loc="left")
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

    # Larger type than before — the list is the longest section, it should
    # read comfortably. The longest title+author should reach close to the
    # right-hand date column.
    if n_lines <= 6:
        font_size = 16
    elif n_lines <= 12:
        font_size = 15
    elif n_lines <= 18:
        font_size = 14
    elif n_lines <= 28:
        font_size = 13
    else:
        font_size = 11

    available = 0.96
    line_height = max(0.060, available / max(n_lines, 1))
    start_y = 0.97

    # Three text artists per book: bold title (left), author (regular,
    # positioned flush after title via bbox measurement), date (right-aligned
    # in muted color, far right column). The right-aligned date column gives
    # the list an editorial table feel rather than a wall of bullet text.
    title_artists = []
    for i, (date, title, author) in enumerate(visible):
        y = start_y - (i + 1) * line_height
        if y < 0.0:
            break
        clipped_title = title if len(title) <= 70 else title[:69] + "…"
        t_title = ax.text(0.0, y, clipped_title,
                          ha="left", va="top",
                          color=COLOR_TEXT_HIGH, fontsize=font_size, fontweight="bold",
                          transform=ax.transAxes)
        suffix = f"   {author}"
        ax.text(1.0, y, date.strftime("%b %Y"),
                ha="right", va="top",
                color=COLOR_TEXT_MUTED, fontsize=font_size, fontweight="normal",
                transform=ax.transAxes)
        title_artists.append((t_title, y, suffix))

    if truncated:
        i = len(visible)
        y = start_y - (i + 1) * line_height
        if y >= 0.02:
            ax.text(0.0, y, f"…and {len(titles) - list_max} more",
                    ha="left", va="top",
                    color=COLOR_TEXT_MUTED, fontsize=font_size, fontstyle="italic",
                    transform=ax.transAxes)

    # Second pass: position the author flush after each bold title using the
    # rendered bbox of the title artist.
    ax.figure.canvas.draw()
    inv = ax.transAxes.inverted()
    for t, y, suffix in title_artists:
        bbox = t.get_window_extent()
        x_end_axes, _ = inv.transform((bbox.x1, bbox.y0))
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

    books = fetch_read_shelf(user_id)
    stats = aggregate_last_12_months(books, today=today)

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
