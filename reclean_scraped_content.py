#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""
Re-runs the (now content-viewer-scoped) extraction from scrape_wix_categorized.py
against the raw HTML already saved under gpcc_export/html/, and overwrites each
by_category/*/*.json's content fields (headings/paragraphs/links/images/lists/
tables) with the cleaned result. No browser/Selenium needed - just re-parses
what's already on disk, so this is safe to run any time without re-scraping
the live site.

Usage:
    python reclean_scraped_content.py [input_dir]
"""
import glob
import json
import os
import sys

from bs4 import BeautifulSoup

from scrape_wix_categorized import extract_structured_content

CONTENT_FIELDS = ['title', 'published_date', 'headings', 'paragraphs', 'links', 'images', 'lists', 'tables']


def main():
    input_dir = sys.argv[1] if len(sys.argv) > 1 else 'gpcc_export'
    html_dir = os.path.join(input_dir, 'html')
    by_category_dir = os.path.join(input_dir, 'by_category')

    json_paths = glob.glob(os.path.join(by_category_dir, '*', '*.json'))
    print(f"Found {len(json_paths)} scraped JSON files")

    updated, missing_html, unchanged = 0, 0, 0
    for json_path in sorted(json_paths):
        with open(json_path, encoding='utf-8') as f:
            data = json.load(f)

        filename = os.path.splitext(os.path.basename(json_path))[0]
        html_path = os.path.join(html_dir, f"{filename}.html")
        if not os.path.exists(html_path):
            missing_html += 1
            print(f"  SKIP (no saved HTML): {json_path}")
            continue

        with open(html_path, encoding='utf-8') as f:
            soup = BeautifulSoup(f, 'html.parser')

        cleaned = extract_structured_content(soup, data.get('url', ''))

        changed = any(data.get(field) != cleaned.get(field) for field in CONTENT_FIELDS)
        if not changed:
            unchanged += 1
            continue

        for field in CONTENT_FIELDS:
            data[field] = cleaned[field]

        with open(json_path, 'w', encoding='utf-8') as f:
            json.dump(data, f, indent=2, ensure_ascii=False)
        updated += 1

    print(f"\nUpdated:  {updated}")
    print(f"Unchanged: {unchanged}")
    print(f"Skipped:  {missing_html} (no saved HTML on disk)")
    return 0


if __name__ == '__main__':
    sys.exit(main())
