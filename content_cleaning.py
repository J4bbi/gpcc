#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""
Shared filters for site-wide Wix chrome that leaks into scraped page content
(nav menu, "Recent Posts"/category-filter sidebar widgets, footer, contact
widget) - used by convert_to_wordpress.py and sync_meetings.py so scraped
JSON renders as clean article content instead of duplicating boilerplate
across every page. See get_content_root() in scrape_wix_categorized.py for
the complementary fix at scrape time (scoping extraction to the actual
article container) - this module is the second line of defense for pages
that don't use that container, or any residual bleed.
"""
import re

BOILERPLATE_HEADINGS = {
    'recent posts', 'comments', 'contact us', 'follow us',
    'neighbourhood watch', 'join our mailing list',
}

# Known sidebar/nav widgets that render as <ul><li>...</li></ul> on every
# page. Matched as a set (order/subset varies between scrapes) against a
# small vocabulary, rather than an exact list, since the actual items seen
# have shifted between scrapes (e.g. category filter list gaining/losing
# entries as blog categories changed).
_NAV_MENU_ITEMS = {
    'home', 'about', 'reports', 'news', 'planning', 'map',
    'accessibility & privacy', 'history', 'contact', 'dropdown', 'more',
}
_CATEGORY_FILTER_ITEMS = {
    'all posts', 'news', 'event', 'planning', 'licensing', 'newsletter',
    'meetings', 'licencing',
}

_DATE_ITEM_RE = re.compile(r'^[A-Za-z]+ \d{1,2}, \d{4}$')
_READ_TIME_RE = re.compile(r'^\d+ min read$', re.I)
_COPYRIGHT_RE = re.compile(r'^\s*[\xa9©]\s*\d{4}\b', re.I)


def is_boilerplate_heading(text):
    return text.strip().lower() in BOILERPLATE_HEADINGS


def is_boilerplate_paragraph(text):
    stripped = text.strip()
    if stripped.upper() == 'ACCESSIBILITY & PRIVACY':
        return True
    if re.match(r'^Updated:', stripped, re.I):
        return True
    if _COPYRIGHT_RE.match(stripped):
        return True
    if re.match(r'^[\w.+-]+@[\w-]+\.[\w.-]+$', stripped):  # a bare email address, on its own line
        return True
    return False


def is_boilerplate_list(items):
    """True for the nav menu, blog category filter, or a post-byline widget
    (author name / "Mon D, YYYY" / "N min read")."""
    if not items:
        return True
    normalized = {i.strip().lower() for i in items}
    if normalized <= _NAV_MENU_ITEMS:
        return True
    if normalized <= _CATEGORY_FILTER_ITEMS:
        return True
    if len(items) == 3 and _DATE_ITEM_RE.match(items[1].strip()) and _READ_TIME_RE.match(items[2].strip()):
        return True
    return False


def clean_paragraphs(paragraphs):
    return [p for p in paragraphs if p.strip() and not is_boilerplate_paragraph(p)]


def clean_headings(headings):
    return [h for h in headings if not is_boilerplate_heading(h.get('text', ''))]


def clean_lists(lists):
    return [lst for lst in lists if not is_boilerplate_list(lst)]
