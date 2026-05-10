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


# -------- constants --------

GOODREADS_RSS_URL = "https://www.goodreads.com/review/list_rss/{user_id}?shelf=read"
GOOGLE_BOOKS_URL = "https://www.googleapis.com/books/v1/volumes"
USER_AGENT = "Mozilla/5.0 (compatible; goodreads-to-storygraph/1.0)"

COLOR_PAGE_BG = "#1a1a1a"
COLOR_PANEL_BG = "#2b2b2b"
COLOR_BORDER = "#5fa8d3"
COLOR_TITLE = "#ffffff"
COLOR_BODY = "#e5e5e5"
COLOR_MUTED = "#9a9a9a"
COLOR_BOOKS_BAR = "#7fb3ff"
COLOR_PAGES_BAR = "#e85a3a"
COLOR_GRID = (1.0, 1.0, 1.0, 0.12)

GENRE_PALETTE = [
    "#7fb3ff", "#7ee08f", "#e85a3a", "#c499ff",
    "#ffd166", "#5fa8d3", "#bcbcbc",
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

    Per book: collect the set of distinct sub-genre buckets it contributes to
    (using BISAC second level when present, first level when specific enough,
    skipping generic top-levels like "Fiction" alone). A book contributes 1 to
    each unique bucket it has — never multiple counts for the same bucket per
    book. Books with no usable bucket count as uncategorized.
    """
    counter: dict = {}
    uncategorized = 0
    for b in books:
        cats = genres_by_isbn.get(b.isbn, []) if b.isbn else []
        book_buckets: set = set()
        for cat in cats:
            parts = [p.strip() for p in cat.split(" / ") if p.strip()]
            if len(parts) >= 2:
                book_buckets.add(parts[1])
            elif parts and parts[0] not in GENERIC_TOP_LEVELS:
                book_buckets.add(parts[0])
        if not book_buckets:
            uncategorized += 1
            continue
        for bucket in book_buckets:
            counter[bucket] = counter.get(bucket, 0) + 1

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
    fig = plt.figure(figsize=fmt["size_in"], facecolor=COLOR_PANEL_BG)

    gs = GridSpec(
        nrows=6, ncols=1,
        figure=fig,
        left=0.21, right=0.95, top=0.94, bottom=0.04,
        height_ratios=[0.45, 0.5, 1.3, 1.3, 1.3, 2.6],
        hspace=0.55,
    )

    _draw_header(fig.add_subplot(gs[0]), stats)
    _draw_summary(fig.add_subplot(gs[1]), stats)
    _draw_bar_chart(fig.add_subplot(gs[2]), stats.books_per_month, COLOR_BOOKS_BAR, "Books read per month")
    _draw_bar_chart(fig.add_subplot(gs[3]), stats.pages_per_month, COLOR_PAGES_BAR, "Pages read per month")
    _draw_genres(fig.add_subplot(gs[4]), genre_data, stats)
    _draw_book_list(fig.add_subplot(gs[5]), stats, list_max=fmt["list_max"])

    # Border patch drawn last (on top) so the rounded blue accent is always visible.
    border = FancyBboxPatch(
        (0.025, 0.02), 0.95, 0.96,
        boxstyle="round,pad=0,rounding_size=0.025",
        linewidth=3,
        edgecolor=COLOR_BORDER,
        facecolor="none",
        transform=fig.transFigure,
        zorder=1000,
        clip_on=False,
    )
    fig.add_artist(border)

    fig.savefig(path, dpi=fmt["dpi"], facecolor=COLOR_PANEL_BG)
    plt.close(fig)


def _strip_axes(ax):
    for spine in ax.spines.values():
        spine.set_visible(False)
    ax.tick_params(left=False, bottom=False, labelleft=False, labelbottom=False)
    ax.set_facecolor(COLOR_PANEL_BG)


def _draw_header(ax, stats: Stats):
    _strip_axes(ax)
    ax.text(0.5, 0.7, "Your Year in Books",
            ha="center", va="center",
            color=COLOR_TITLE, fontsize=22, fontweight="bold",
            transform=ax.transAxes)
    date_range = f"{stats.window_start.strftime('%b %d, %Y')} – {stats.window_end.strftime('%b %d, %Y')}"
    ax.text(0.5, 0.2, date_range,
            ha="center", va="center",
            color=COLOR_MUTED, fontsize=11,
            transform=ax.transAxes)


def _draw_summary(ax, stats: Stats):
    _strip_axes(ax)
    if stats.total_books == 0:
        ax.text(0.5, 0.5, "No books read in the last 12 months.",
                ha="center", va="center",
                color=COLOR_BODY, fontsize=14, fontweight="bold",
                transform=ax.transAxes)
        return
    main = f"You finished {stats.total_books:,} books and {stats.total_pages:,} pages"
    sub = "in the last 12 months"
    if stats.books_missing_pages > 0:
        plural = "s" if stats.books_missing_pages != 1 else ""
        sub += f"  ({stats.books_missing_pages} book{plural} had no page count)"
    ax.text(0.5, 0.7, main,
            ha="center", va="center",
            color=COLOR_BODY, fontsize=15, fontweight="bold",
            transform=ax.transAxes)
    ax.text(0.5, 0.3, sub,
            ha="center", va="center",
            color=COLOR_MUTED, fontsize=10,
            transform=ax.transAxes)


def _draw_bar_chart(ax, data: list, color: str, title: str):
    ax.set_facecolor(COLOR_PANEL_BG)
    labels = [_short_month_label(d[0]) for d in data]
    values = [d[1] for d in data]
    x = list(range(len(data)))
    ax.bar(x, values, color=color, width=0.7)
    ax.set_xticks(x, labels=labels)
    ax.set_title(title, color=COLOR_TITLE, fontsize=12, fontweight="bold", pad=8, loc="left")
    ax.tick_params(colors=COLOR_BODY, labelsize=8)
    for s in ("top", "right"):
        ax.spines[s].set_visible(False)
    for s in ("left", "bottom"):
        ax.spines[s].set_color(COLOR_MUTED)
    ax.yaxis.grid(True, color=COLOR_GRID, linewidth=0.5)
    ax.set_axisbelow(True)


def _short_month_label(full_label: str) -> str:
    """'Jun 2025' -> 'Jun '25'. Always include the year so the 12-month
    window's December-to-January transition is unambiguous."""
    parts = full_label.split(" ")
    if len(parts) == 2 and len(parts[1]) == 4:
        return f"{parts[0]} '{parts[1][2:]}"
    return full_label


def _draw_genres(ax, genre_data, stats: Stats):
    items, uncategorized = genre_data
    ax.set_facecolor(COLOR_PANEL_BG)
    ax.set_title("Genres", color=COLOR_TITLE, fontsize=12, fontweight="bold", pad=8, loc="left")

    if not items:
        for spine in ax.spines.values():
            spine.set_visible(False)
        ax.tick_params(left=False, bottom=False, labelleft=False, labelbottom=False)
        ax.text(0.5, 0.5, "Genre data unavailable.",
                ha="center", va="center",
                color=COLOR_MUTED, fontsize=10,
                transform=ax.transAxes)
        return

    labels = [_truncate_label(it[0], 18) for it in items]
    values = [it[1] for it in items]
    colors = [GENRE_PALETTE[i % len(GENRE_PALETTE)] for i in range(len(items))]
    y_positions = list(range(len(items)))
    ax.barh(y_positions, values, color=colors, height=0.7)
    ax.set_yticks(y_positions, labels=labels)
    ax.invert_yaxis()
    ax.tick_params(colors=COLOR_BODY, labelsize=9)
    for s in ("top", "right", "left"):
        ax.spines[s].set_visible(False)
    ax.spines["bottom"].set_color(COLOR_MUTED)
    ax.xaxis.grid(True, color=COLOR_GRID, linewidth=0.5)
    ax.set_axisbelow(True)

    if stats.total_books > 0 and uncategorized >= max(3, int(0.1 * stats.total_books)):
        plural = "s" if uncategorized != 1 else ""
        ax.text(1.0, -0.18, f"{uncategorized} book{plural} had no genre data.",
                ha="right", va="top",
                color=COLOR_MUTED, fontsize=8,
                transform=ax.transAxes)


def _draw_book_list(ax, stats: Stats, list_max: int = 50):
    _strip_axes(ax)
    ax.set_title("What you read", color=COLOR_TITLE, fontsize=12, fontweight="bold", pad=8, loc="left")
    titles = stats.book_titles
    if not titles:
        ax.text(0.0, 0.95, "(none)",
                ha="left", va="top",
                color=COLOR_MUTED, fontsize=10,
                transform=ax.transAxes)
        return

    truncated = len(titles) > list_max
    visible = titles[:list_max]
    n_lines = len(visible) + (1 if truncated else 0)

    if n_lines <= 10:
        font_size = 11
    elif n_lines <= 20:
        font_size = 10
    elif n_lines <= 30:
        font_size = 9
    else:
        font_size = 8

    # Reserve top 0.07 for the section title; distribute the rest.
    available = 0.90
    # Floor on line height so the list never feels cramped on a short list.
    line_height = max(0.055, available / max(n_lines, 1))
    start_y = 0.92

    # First pass: render the bullet + title in bold for every line. Capture
    # the artists so we can measure their widths and place the author/date
    # suffix to their right in regular weight.
    title_artists = []
    for i, (date, title, author) in enumerate(visible):
        y = start_y - (i + 1) * line_height
        if y < 0.02:
            break
        truncated_title = title if len(title) <= 55 else title[:54] + "…"
        t = ax.text(0.02, y, f"•  {truncated_title}",
                    ha="left", va="top",
                    color=COLOR_TITLE, fontsize=font_size, fontweight="bold",
                    transform=ax.transAxes)
        suffix = f"   — {author}   ({date.strftime('%b %Y')})"
        title_artists.append((t, y, suffix))

    if truncated:
        i = len(visible)
        y = start_y - (i + 1) * line_height
        if y >= 0.02:
            ax.text(0.02, y, f"…and {len(titles) - list_max} more",
                    ha="left", va="top",
                    color=COLOR_MUTED, fontsize=font_size, fontstyle="italic",
                    transform=ax.transAxes)

    # Second pass: now that we've drawn the bold titles, ask matplotlib for
    # their rendered widths so the suffix lines up flush after each one
    # instead of being column-aligned.
    ax.figure.canvas.draw()
    inv = ax.transAxes.inverted()
    for t, y, suffix in title_artists:
        bbox = t.get_window_extent()
        x_end_axes, _ = inv.transform((bbox.x1, bbox.y0))
        ax.text(x_end_axes, y, suffix,
                ha="left", va="top",
                color=COLOR_BODY, fontsize=font_size, fontweight="normal",
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
