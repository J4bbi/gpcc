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
import copy
import re
from urllib.parse import urljoin

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
_TAG_RE = re.compile(r'<[^>]+>')

# Tags extract_paragraph_html() keeps as real markup; everything else gets
# unwrapped down to its text (Wix wraps nearly all body text in decorative
# <span style="..."> for font-size/color, which carries no real meaning here).
_ALLOWED_INLINE_TAGS = {'a', 'strong', 'em', 'b', 'i', 'br'}
_DANGEROUS_HREF_RE = re.compile(r'^\s*(javascript|data|vbscript):', re.I)


def is_boilerplate_heading(text):
    return text.strip().lower() in BOILERPLATE_HEADINGS


def is_boilerplate_paragraph(text):
    # Paragraphs may now be small HTML fragments (see extract_paragraph_html)
    # rather than plain text - match against the tag-stripped text so a
    # boilerplate line that happens to carry <strong>/<a> formatting is still
    # recognized.
    stripped = _TAG_RE.sub('', text).strip()
    if stripped.upper() == 'ACCESSIBILITY & PRIVACY':
        return True
    if re.match(r'^Updated:', stripped, re.I):
        return True
    if _COPYRIGHT_RE.match(stripped):
        return True
    if re.match(r'^[\w.+-]+@[\w-]+\.[\w.-]+$', stripped):  # a bare email address, on its own line
        return True
    return False


def extract_paragraph_html(tag, base_url=''):
    """Render a BeautifulSoup tag's inner content as a small, safe HTML
    fragment: keeps <a href> (made absolute, external links get
    target=_blank/rel=noopener) and basic emphasis tags, unwraps everything
    else (the <span style="...">/<u>/<font> wrappers Wix uses purely for
    inline styling) down to plain text. Without this, paragraphs that
    contain an inline link (e.g. "...on this Council site.") get flattened
    to dead, unlinked text - see the news-post accessibility review this was
    added for.

    Text nodes are HTML-escaped automatically by BeautifulSoup's own string
    serialization, so the result is safe to insert directly into WXR/HTML
    output without a further html.escape() pass - doing that pass anyway
    would double-escape the tags this function deliberately keeps.
    """
    working = copy.copy(tag)
    for el in working.find_all(True):
        if el.name == 'a':
            href = (el.get('href') or '').strip()
            if not href or _DANGEROUS_HREF_RE.match(href):
                el.unwrap()
                continue
            if base_url:
                href = urljoin(base_url, href)
            attrs = {'href': href}
            if href.startswith('http') and 'grangeprestonfieldcc' not in href:
                attrs['target'] = '_blank'
                attrs['rel'] = 'noopener noreferrer'
            el.attrs = attrs
        elif el.name not in _ALLOWED_INLINE_TAGS:
            el.unwrap()

    html_str = ''.join(str(c) for c in working.contents)
    return re.sub(r'\s+', ' ', html_str).strip()


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
