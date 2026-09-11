#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""
Splits the GPCC "Rolling Update" page into independent, dated news posts.

The page (https://www.grangeprestonfieldcc.org.uk/post/rolling-update) is a
single long article where entries are separated by a "~~" delimiter paragraph,
each followed by a "<date>: <title>" header (e.g. "21 Jan: Scottish Road Works
Register:") in bold, then body text.

This reuses the raw HTML already saved by scrape_wix_categorized.py
(gpcc_export/html/post_rolling-update.html) rather than re-fetching - re-run
that scraper first if you want the latest version of the page.

Usage:
    python parse_rolling_update.py [html_path] [--year YYYY]
"""
import argparse
import json
import os
import re
import sys
from datetime import date
from bs4 import BeautifulSoup

SOURCE_URL = "https://www.grangeprestonfieldcc.org.uk/post/rolling-update"
DEFAULT_HTML_PATH = "gpcc_export/html/post_rolling-update.html"
OUTPUT_DIR = "gpcc_export/by_category/news/rolling_update"

MONTHS = {
    'jan': 1, 'january': 1,
    'feb': 2, 'february': 2,
    'mar': 3, 'march': 3,
    'apr': 4, 'april': 4,
    'may': 5,
    'jun': 6, 'june': 6,
    'jul': 7, 'july': 7,
    'aug': 8, 'august': 8,
    'sep': 9, 'sept': 9, 'september': 9,
    'oct': 10, 'october': 10,
    'nov': 11, 'november': 11,
    'dec': 12, 'december': 12,
}

HEADER_RE = re.compile(
    r'^\s*(\d{1,2})\s+([A-Za-z]+)\.?\s*(\d{4})?\s*:\s*(.*)$'
)


def clean_text(text):
    """Collapse whitespace runs (newlines from nested spans, etc.) to single spaces."""
    return re.sub(r'\s+', ' ', text).strip()


def parse_header(first_p):
    """Extract (date_raw, day, month_name, year_or_None, title, leftover_body_text)
    from a segment's first paragraph, or None if it has no recognizable date header.
    """
    strong_text = clean_text(''.join(s.get_text() for s in first_p.find_all('strong')))
    full_text = clean_text(first_p.get_text())

    header_text = strong_text if strong_text else full_text
    match = HEADER_RE.match(header_text)
    if not match:
        return None

    day, month_word, year_str, title = match.groups()
    month_key = month_word.lower()
    if month_key not in MONTHS:
        return None

    title = title.strip().strip(':').strip()
    if not title:
        return None

    # Any text in the same <p> after the bold date/title header is the start of the body.
    # A leading ':' here is just the title/body separator carried outside the bold
    # span (formatting varies entry to entry) - strip it either way.
    leftover = ''
    if strong_text and full_text.startswith(strong_text):
        leftover = full_text[len(strong_text):].strip()
    elif strong_text:
        # Whitespace-normalization mismatch between strong_text and full_text;
        # fall back to locating the title within full_text.
        idx = full_text.find(title)
        if idx != -1:
            leftover = full_text[idx + len(title):].strip()
    leftover = leftover.lstrip(':').strip()

    date_raw = f"{day} {month_word}" + (f" {year_str}" if year_str else "")
    return {
        'date_raw': date_raw,
        'day': int(day),
        'month': MONTHS[month_key],
        'year': int(year_str) if year_str else None,
        'title': title,
        'leftover_body': leftover,
    }


def resolve_years(entries, anchor_year):
    """Fill in the year for entries lacking an explicit one.

    Entries are in reverse-chronological order (newest first). Walk top to
    bottom, decrementing the running year only on a genuine Dec/Jan-style
    wrap (previous entry in Q1, this one in Q4) - not on any month increase,
    since the page isn't perfectly chronological (an occasional entry is a
    month or two out of sequence) and a looser rule cascades one bad entry's
    year into every entry after it. Snaps to any explicit year found along
    the way.
    """
    running_year = anchor_year
    prev_month = None
    for entry in entries:
        if entry['year'] is not None:
            running_year = entry['year']
        else:
            if prev_month is not None and prev_month <= 3 and entry['month'] >= 10:
                running_year -= 1
            entry['year'] = running_year
        prev_month = entry['month']
    return entries


def parse_rolling_update(soup, anchor_year=None):
    """Parse the rolling-update article into a list of entry dicts (newest first,
    matching page order).

    The '~~' paragraphs are purely a visual separator on the page and aren't
    load-bearing here: a new entry starts at any paragraph matching the
    "<date>: <title>" header pattern, and every other paragraph (including a
    stray '~~' the author sometimes drops mid-list) is appended as body text
    to whichever entry is currently open. Paragraphs before the first
    recognized header (the page's intro/bio blurb) are discarded.
    """
    container = soup.find(attrs={'data-id': 'content-viewer'})
    if container is None:
        raise ValueError("Could not find the article's content-viewer container")

    if anchor_year is None:
        anchor_year = date.today().year

    entries = []
    for p in container.find_all('p'):
        text = clean_text(p.get_text())
        if text == '~~':
            continue

        header = parse_header(p)
        if header is not None:
            body = [header['leftover_body']] if header['leftover_body'] else []
            entries.append({
                'date_raw': header['date_raw'],
                'day': header['day'],
                'month': header['month'],
                'year': header['year'],
                'title': header['title'],
                'body': body,
            })
        elif entries and text:
            entries[-1]['body'].append(text)

    resolve_years(entries, anchor_year)
    for entry in entries:
        entry['date'] = date(entry['year'], entry['month'], entry['day']).isoformat()

    return entries


def slugify(text):
    slug = re.sub(r'[^\w\s-]', '', text.lower())
    slug = re.sub(r'[\s_-]+', '-', slug).strip('-')
    return slug[:80] or 'untitled'


def save_entries(entries, output_dir):
    os.makedirs(output_dir, exist_ok=True)

    index = []
    for entry in entries:
        filename_base = f"{entry['date']}_{slugify(entry['title'])}"
        json_path = os.path.join(output_dir, f"{filename_base}.json")
        txt_path = os.path.join(output_dir, f"{filename_base}.txt")

        post = {
            'source_url': SOURCE_URL,
            'date': entry['date'],
            'date_raw': entry['date_raw'],
            'title': entry['title'],
            'body': entry['body'],
        }
        with open(json_path, 'w', encoding='utf-8') as f:
            json.dump(post, f, indent=2, ensure_ascii=False)

        with open(txt_path, 'w', encoding='utf-8') as f:
            f.write(f"Date: {entry['date']} ({entry['date_raw']})\n")
            f.write(f"Title: {entry['title']}\n")
            f.write(f"Source: {SOURCE_URL}\n")
            f.write("=" * 60 + "\n\n")
            for para in entry['body']:
                f.write(f"{para}\n\n")

        index.append({
            'date': entry['date'],
            'title': entry['title'],
            'file': f"{filename_base}.json",
        })

    index_path = os.path.join(output_dir, 'index.json')
    with open(index_path, 'w', encoding='utf-8') as f:
        json.dump(sorted(index, key=lambda e: e['date'], reverse=True), f, indent=2, ensure_ascii=False)

    return index


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('html_path', nargs='?', default=DEFAULT_HTML_PATH,
                         help='Path to the saved rolling-update page HTML')
    parser.add_argument('--year', type=int, default=None,
                         help='Year to anchor the newest entry to (default: current year)')
    parser.add_argument('--output-dir', default=OUTPUT_DIR)
    args = parser.parse_args()

    if not os.path.exists(args.html_path):
        print(f"HTML file not found: {args.html_path}")
        print("Run scrape_wix_categorized.py first to fetch the page.")
        sys.exit(1)

    with open(args.html_path, encoding='utf-8') as f:
        soup = BeautifulSoup(f, 'html.parser')

    entries = parse_rolling_update(soup, anchor_year=args.year)
    index = save_entries(entries, args.output_dir)

    print(f"Parsed {len(entries)} entries from {args.html_path}")
    print(f"Saved to {args.output_dir}/")
    print()
    for e in index[:10]:
        print(f"  {e['date']}  {e['title']}")
    if len(index) > 10:
        print(f"  ... and {len(index) - 10} more")


if __name__ == '__main__':
    main()
