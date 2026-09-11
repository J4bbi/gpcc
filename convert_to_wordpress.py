#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""
Convert scraped Wix content to WordPress WXR import format.
Includes automatic image attachment import.

Usage:
    python convert_to_wordpress.py [input_dir] [output_file]

    Defaults:
        input_dir: gpcc_export
        output_file: wordpress_import.xml
"""
import os
import sys
import json
import html
import re
import glob
import mimetypes
from datetime import datetime, timedelta
from urllib.parse import urlparse, unquote
from xml.sax.saxutils import escape

from content_cleaning import is_boilerplate_paragraph, is_boilerplate_heading, is_boilerplate_list


def parse_published_date(published_date, scraped_at_iso):
    """Parse Wix's 'published_date' byline (e.g. "Aug 26, 2025", "Apr 4" with
    no year, or a relative "3 days ago") into a datetime. Year-less and
    relative values are anchored to scraped_at rather than "now", so a
    reclean of already-scraped data doesn't silently reinterpret them
    against today's date. Returns None if missing/unparseable."""
    if not published_date:
        return None
    try:
        anchor = datetime.fromisoformat(scraped_at_iso)
    except (ValueError, TypeError):
        anchor = datetime.now()

    text = published_date.strip()

    # Already an unambiguous ISO date (e.g. from parse_rolling_update.py's
    # own output, which resolves its "~~"-separated headers to real dates
    # itself) - no anchoring needed.
    try:
        return datetime.strptime(text, '%Y-%m-%d')
    except ValueError:
        pass

    m = re.match(r'^(\d+)\s+(minute|hour|day|week)s?\s+ago$', text, re.I)
    if m:
        n, unit = int(m.group(1)), m.group(2).lower()
        delta = {
            'minute': timedelta(minutes=n), 'hour': timedelta(hours=n),
            'day': timedelta(days=n), 'week': timedelta(weeks=n),
        }[unit]
        return anchor - delta
    if re.match(r'^yesterday$', text, re.I):
        return anchor - timedelta(days=1)
    if re.match(r'^today$', text, re.I):
        return anchor

    for fmt in ('%b %d, %Y', '%B %d, %Y'):
        try:
            return datetime.strptime(text, fmt)
        except ValueError:
            pass

    # No year given - anchor to scraped_at's year, rolling back a year if
    # that would otherwise land in the future (matches sync_meetings.py's
    # same problem for meeting dates).
    for fmt in ('%b %d', '%B %d'):
        try:
            dt = datetime.strptime(text, fmt)
            candidate = dt.replace(year=anchor.year)
            if candidate > anchor:
                candidate = dt.replace(year=anchor.year - 1)
            return candidate
        except ValueError:
            pass

    return None


def cdata(text):
    """Wrap text in CDATA section."""
    if text is None:
        return "<![CDATA[]]>"
    text = str(text).replace("]]>", "]]]]><![CDATA[>")
    return f"<![CDATA[{text}]]>"


def clean_title(title):
    """Strip a leading "** " (Wix bold-markdown artifact) from a scraped
    title, then convert an ALL-CAPS title (e.g. "** LOCAL MEDIA LINKS") to
    first-letter-per-word capitalization. Titles that aren't all-caps are
    left as-is, so mixed-case titles keep any deliberate styling."""
    title = title.strip()
    if title.startswith('** '):
        title = title[2:].strip()
    if title.isupper():
        title = re.sub(
            r'\S+',
            lambda m: m.group(0)[:1].upper() + m.group(0)[1:].lower(),
            title,
        )
    return title


def clean_slug(text):
    """Create URL-friendly slug from text."""
    slug = text.lower()
    slug = re.sub(r'[^a-z0-9\s-]', '', slug)
    slug = re.sub(r'[\s_]+', '-', slug)
    slug = re.sub(r'-+', '-', slug)
    return slug.strip('-')[:200]


def get_filename_from_url(url):
    """Extract filename from URL."""
    parsed = urlparse(url)
    path = unquote(parsed.path)
    filename = os.path.basename(path).split('?')[0]
    if not filename or filename == '/':
        filename = f"image_{hash(url) % 100000}.jpg"
    return re.sub(r'[^\w\-_\.]', '_', filename)


def get_mime_type(filename):
    """Get MIME type from filename."""
    mime_type, _ = mimetypes.guess_type(filename)
    return mime_type or 'application/octet-stream'


def is_valid_image_url(url):
    """Check if URL is a valid image (not a tracking pixel)."""
    if not url:
        return False
    url_lower = url.lower()
    if '1x1' in url_lower or 'pixel' in url_lower or 'spacer' in url_lower:
        return False
    if 'data:image' in url_lower:
        return False
    # Check for image extensions or Wix image URLs
    image_indicators = ['.jpg', '.jpeg', '.png', '.gif', '.webp', '.svg', 'wixstatic.com/media']
    return any(ind in url_lower for ind in image_indicators)


def normalize_image_url(url):
    """Normalize image URL to full URL."""
    if url.startswith('//'):
        return 'https:' + url
    return url


def collect_images(pages):
    """Collect all unique images from pages."""
    images = {}
    for page in pages:
        for img in page.get('images', []):
            src = img.get('src', '')
            if is_valid_image_url(src):
                src = normalize_image_url(src)
                if src not in images:
                    images[src] = {
                        'url': src,
                        'alt': img.get('alt', ''),
                        'filename': get_filename_from_url(src)
                    }
    return images


def json_to_html_content(data, image_id_map, include_images=True):
    """Convert JSON structured content to HTML for WordPress."""
    html_parts = []

    # Add headings and paragraphs interleaved more naturally
    for heading in data.get('headings', []):
        if is_boilerplate_heading(heading.get('text', '')):
            continue
        level = heading.get('level', 2)
        text = html.escape(heading.get('text', ''))
        html_parts.append(f"<h{level}>{text}</h{level}>")

    for para in data.get('paragraphs', []):
        if para.strip() and not is_boilerplate_paragraph(para):
            # Paragraphs are pre-built, already-safe HTML fragments (see
            # content_cleaning.extract_paragraph_html) - text nodes are
            # already entity-escaped by BeautifulSoup's own serialization,
            # so html.escape() here would double-escape and break the <a>
            # tags deliberately preserved for inline links.
            html_parts.append(f"<p>{para}</p>")

    # Add lists (skipping the nav menu / category filter / post-byline widgets
    # that render as <ul><li> on every page - see content_cleaning.py)
    for lst in data.get('lists', []):
        if lst and not is_boilerplate_list(lst):
            items = ''.join(f"<li>{html.escape(item)}</li>" for item in lst)
            html_parts.append(f"<ul>{items}</ul>")

    # Add tables
    for table in data.get('tables', []):
        if table:
            rows_html = []
            for i, row in enumerate(table):
                tag = 'th' if i == 0 else 'td'
                cells = ''.join(f"<{tag}>{html.escape(str(cell))}</{tag}>" for cell in row)
                rows_html.append(f"<tr>{cells}</tr>")
            html_parts.append(f"<table>{chr(10).join(rows_html)}</table>")

    # Add images with WordPress attachment references
    if include_images:
        for img in data.get('images', []):
            src = img.get('src', '')
            if is_valid_image_url(src):
                src = normalize_image_url(src)
                alt = html.escape(img.get('alt', ''))
                # Use the original URL - WordPress will remap after import
                html_parts.append(f'<img src="{html.escape(src)}" alt="{alt}" class="aligncenter" />')

    return "\n\n".join(html_parts)


def load_scraped_content(input_dir):
    """Load all JSON files from the scraped content directory."""
    pages = []
    by_category_dir = os.path.join(input_dir, 'by_category')

    if not os.path.exists(by_category_dir):
        print(f"Error: Directory not found: {by_category_dir}")
        return pages

    for category in os.listdir(by_category_dir):
        category_path = os.path.join(by_category_dir, category)
        if not os.path.isdir(category_path):
            continue

        for filename in os.listdir(category_path):
            if not filename.endswith('.json'):
                continue

            filepath = os.path.join(category_path, filename)
            try:
                with open(filepath, 'r', encoding='utf-8') as f:
                    data = json.load(f)
                    data['_category'] = category
                    data['_filename'] = filename.replace('.json', '')
                    pages.append(data)
            except Exception as e:
                print(f"Warning: Could not load {filepath}: {e}")

    return pages


def generate_attachment_item(img_id, img_data, post_date, pub_date):
    """Generate WXR item for an image attachment."""
    url = img_data['url']
    filename = img_data['filename']
    alt = img_data.get('alt', '')
    title = alt if alt else os.path.splitext(filename)[0].replace('_', ' ').replace('-', ' ')
    title = clean_title(title)
    mime_type = get_mime_type(filename)

    return f'''  <item>
    <title>{cdata(title)}</title>
    <link>{escape(url)}</link>
    <pubDate>{pub_date}</pubDate>
    <dc:creator>{cdata("admin")}</dc:creator>
    <guid isPermaLink="false">{escape(url)}</guid>
    <description></description>
    <content:encoded>{cdata("")}</content:encoded>
    <excerpt:encoded>{cdata(alt)}</excerpt:encoded>
    <wp:post_id>{img_id}</wp:post_id>
    <wp:post_date>{post_date}</wp:post_date>
    <wp:post_date_gmt>{post_date}</wp:post_date_gmt>
    <wp:post_modified>{post_date}</wp:post_modified>
    <wp:post_modified_gmt>{post_date}</wp:post_modified_gmt>
    <wp:comment_status>closed</wp:comment_status>
    <wp:ping_status>closed</wp:ping_status>
    <wp:post_name>{cdata(clean_slug(title))}</wp:post_name>
    <wp:status>inherit</wp:status>
    <wp:post_parent>0</wp:post_parent>
    <wp:menu_order>0</wp:menu_order>
    <wp:post_type>attachment</wp:post_type>
    <wp:post_password></wp:post_password>
    <wp:is_sticky>0</wp:is_sticky>
    <wp:attachment_url>{escape(url)}</wp:attachment_url>
    <wp:postmeta>
      <wp:meta_key>{cdata("_wp_attached_file")}</wp:meta_key>
      <wp:meta_value>{cdata(filename)}</wp:meta_value>
    </wp:postmeta>
    <wp:postmeta>
      <wp:meta_key>{cdata("_wp_attachment_image_alt")}</wp:meta_key>
      <wp:meta_value>{cdata(alt)}</wp:meta_value>
    </wp:postmeta>
  </item>'''


def partition_pages(pages):
    """Split scraped pages into (about_pages, meeting_pages, news_pages).

    - about_pages: the static "who we are" / "history" reference pages -> get
      merged into a single About page. Excludes /post/ URLs even if scraped
      under the 'about' category (those read as articles, not reference
      pages, so they go to news instead).
    - meeting_pages: excluded entirely from WXR - these are synced into
      Events Manager separately via sync_meetings.py (see that script for why:
      Events Manager's calendar data lives in a custom DB table that a plain
      WXR/WordPress-Importer run never touches).
    - news_pages: everything else - becomes tagged blog posts (or, for
      non-/post/ utility pages like /contact or /map, plain draft pages).
    """
    about_pages, meeting_pages, news_pages = [], [], []
    for page in pages:
        url = page.get('url', '').rstrip('/')
        if '/_files/ugd/' in url:
            continue  # handled as attachments, not content items
        if url.endswith('/post/rolling-update'):
            continue  # superseded by load_rolling_update_pages()'s per-entry split
        category = page.get('_category', 'uncategorized')
        if category == 'meetings':
            meeting_pages.append(page)
        elif category == 'about' and '/post/' not in url:
            about_pages.append(page)
        else:
            news_pages.append(page)
    return about_pages, meeting_pages, news_pages


def load_rolling_update_pages(input_dir):
    """Load parse_rolling_update.py's already-dated, already-split entries
    and adapt them into the same page-dict shape news_pages items use, so
    they flow through the ordinary News item generation. Each entry becomes
    its own tagged post instead of one giant mashed-together rolling-update
    page."""
    pages = []
    entry_dir = os.path.join(input_dir, 'by_category', 'news', 'rolling_update')
    for path in sorted(glob.glob(os.path.join(entry_dir, '*.json'))):
        if os.path.basename(path) == 'index.json':
            continue
        with open(path, encoding='utf-8') as f:
            entry = json.load(f)
        filename = os.path.splitext(os.path.basename(path))[0]
        pages.append({
            'title': entry['title'],
            'url': f"{entry['source_url']}#{filename}",
            'paragraphs': entry.get('body', []),
            'headings': [], 'lists': [], 'images': [], 'tables': [], 'links': [],
            '_category': 'rolling_update',
            '_filename': filename,
            'published_date': entry['date'],
            'scraped_at': '',
        })
    return pages


def build_about_page_content(about_pages, image_id_map):
    """Merge the about-category reference pages into one page body."""
    def sort_key(p):
        url = p.get('url', '').rstrip('/')
        if url.endswith('/who-we-are'):
            return 0
        if url.endswith('/history'):
            return 1
        return 2

    section_titles = {'history': 'Our History'}
    parts = []
    for page in sorted(about_pages, key=sort_key):
        url = page.get('url', '').rstrip('/')
        slug = url.rsplit('/', 1)[-1]
        if slug in section_titles:
            parts.append(f"<h2>{html.escape(section_titles[slug])}</h2>")
        # Reuse json_to_html_content's paragraph/list/table rendering, but
        # skip its own scraped headings (e.g. "HISTORY | GPCC") - those were
        # page titles on the source site, not meaningful section breaks once
        # merged into one page - and skip images: no media on the About page.
        content_only = dict(page)
        content_only['headings'] = []
        parts.append(json_to_html_content(content_only, image_id_map, include_images=False))
    return "\n\n".join(parts)


def generate_wxr(pages, site_url="https://example.com", input_dir="gpcc_export"):
    """Generate WordPress WXR XML from scraped pages.

    Only produces the About page and News posts/pages - meeting content is
    synced separately via sync_meetings.py (see partition_pages docstring).
    """

    now = datetime.now()
    pub_date = now.strftime("%a, %d %b %Y %H:%M:%S +0000")
    post_date = now.strftime("%Y-%m-%d %H:%M:%S")

    about_pages, meeting_pages, news_pages = partition_pages(pages)
    news_pages = news_pages + load_rolling_update_pages(input_dir)

    # Collect unique images from News only - the About page renders no media
    # (see build_about_page_content), so its images would just be unused
    # attachments cluttering the media library.
    images = collect_images(news_pages)

    # Create image ID map (start IDs at 10000 to avoid conflicts)
    image_id_map = {}
    for i, url in enumerate(images.keys()):
        image_id_map[url] = 10000 + i

    # Single "News" category for every blog post; original scrape categories
    # (planning, local_info, general, documents, contact, about, events)
    # become tags instead, so they're still filterable without fragmenting
    # the category taxonomy.
    category_xml = ['''  <wp:category>
    <wp:term_id>1</wp:term_id>
    <wp:category_nicename>news</wp:category_nicename>
    <wp:category_parent></wp:category_parent>
    <wp:cat_name>{}</wp:cat_name>
  </wp:category>'''.format(cdata('News'))]

    tag_names = sorted({page.get('_category', 'uncategorized') for page in news_pages})
    tag_xml = []
    for i, tag in enumerate(tag_names, start=1):
        tag_slug = clean_slug(tag)
        tag_name = tag.replace('_', ' ').title()
        tag_xml.append(f'''  <wp:tag>
    <wp:term_id>{100 + i}</wp:term_id>
    <wp:tag_slug>{cdata(tag_slug)}</wp:tag_slug>
    <wp:tag_name>{cdata(tag_name)}</wp:tag_name>
  </wp:tag>''')

    # Collect document attachments referenced by the pages actually being imported here
    documents = {}
    for page in about_pages + news_pages:
        for link in page.get('links', []):
            if link.get('type') == 'document':
                doc_url = link['url']
                if doc_url.startswith('//'):
                    doc_url = 'https:' + doc_url
                doc_url = doc_url.split('?')[0]  # strip tracking/query params
                if doc_url not in documents:
                    documents[doc_url] = {
                        'url': doc_url,
                        'filename': get_filename_from_url(doc_url),
                        'title': link.get('text', '') or get_filename_from_url(doc_url),
                    }


    # Build attachment items, deduplicating images and documents by slug
    attachment_xml = []
    seen_slugs = set()
    all_attachments = (
        list(images.values()) +
        list(documents.values())
    )
    for att_id, att_data in enumerate(all_attachments, start=10000):
        title = att_data.get('alt', '') or att_data.get('title', '') or att_data.get('filename', '')
        slug = clean_slug(title)
        if slug in seen_slugs:
            continue
        seen_slugs.add(slug)
        attachment_xml.append(generate_attachment_item(att_id, att_data, post_date, pub_date))

    items_xml = []
    post_count = 0
    page_count = 0

    # About page: one consolidated static Page merging who-we-are + history
    if about_pages:
        about_html = build_about_page_content(about_pages, image_id_map)
        item = f'''  <item>
    <title>{cdata("About")}</title>
    <link>{escape("https://www.grangeprestonfieldcc.org.uk/about")}</link>
    <pubDate>{pub_date}</pubDate>
    <dc:creator>{cdata("admin")}</dc:creator>
    <guid isPermaLink="false">{cdata("about-page")}</guid>
    <description></description>
    <content:encoded>{cdata(about_html)}</content:encoded>
    <excerpt:encoded>{cdata("")}</excerpt:encoded>
    <wp:post_id>1</wp:post_id>
    <wp:post_date>{post_date}</wp:post_date>
    <wp:post_date_gmt>{post_date}</wp:post_date_gmt>
    <wp:post_modified>{post_date}</wp:post_modified>
    <wp:post_modified_gmt>{post_date}</wp:post_modified_gmt>
    <wp:comment_status>closed</wp:comment_status>
    <wp:ping_status>closed</wp:ping_status>
    <wp:post_name>{cdata("about")}</wp:post_name>
    <wp:status>publish</wp:status>
    <wp:post_parent>0</wp:post_parent>
    <wp:menu_order>0</wp:menu_order>
    <wp:post_type>page</wp:post_type>
    <wp:post_password></wp:post_password>
    <wp:is_sticky>0</wp:is_sticky>
  </item>'''
        items_xml.append(item)
        page_count += 1

    # News: /post/ articles become tagged posts in the News category; the
    # handful of non-/post/ utility pages (contact, map, planning landing,
    # etc.) become plain draft pages, same as before.
    for i, page in enumerate(news_pages, start=2):
        content_html = json_to_html_content(page, image_id_map)
        # Wix "pointer" stub pages (e.g. "Scroll down from here for other
        # items >>") have no paragraphs/headings/lists/images of their own -
        # only sitewide footer links, which get filtered as boilerplate - and
        # would otherwise become a blank post. Skip them rather than
        # importing empty content.
        if not content_html.strip():
            continue

        raw_title = page.get('title', 'Untitled')
        if raw_title.endswith(' | GPCC'):
            raw_title = raw_title[:-len(' | GPCC')]
        title = clean_title(raw_title)
        url = page.get('url', '')
        category = page.get('_category', 'uncategorized')
        filename = page.get('_filename', f'page-{i}')
        slug = clean_slug(filename) if filename != 'index' else clean_slug(title)
        post_type = "post" if "/post/" in url else "page"

        item_date = parse_published_date(page.get('published_date', ''), page.get('scraped_at', ''))
        item_post_date = item_date.strftime("%Y-%m-%d %H:%M:%S") if item_date else post_date
        item_pub_date = item_date.strftime("%a, %d %b %Y %H:%M:%S +0000") if item_date else pub_date

        terms_xml = ""
        if post_type == "post":
            post_count += 1
            tag_slug = clean_slug(category)
            tag_name = category.replace('_', ' ').title()
            terms_xml = (
                f'    <category domain="category" nicename="news">{cdata("News")}</category>\n'
                f'    <category domain="post_tag" nicename="{tag_slug}">{cdata(tag_name)}</category>\n'
            )
        else:
            page_count += 1

        item = f'''  <item>
    <title>{cdata(title)}</title>
    <link>{escape(url)}</link>
    <pubDate>{item_pub_date}</pubDate>
    <dc:creator>{cdata("admin")}</dc:creator>
    <guid isPermaLink="false">{escape(url)}</guid>
    <description></description>
    <content:encoded>{cdata(content_html)}</content:encoded>
    <excerpt:encoded>{cdata("")}</excerpt:encoded>
    <wp:post_id>{i}</wp:post_id>
    <wp:post_date>{item_post_date}</wp:post_date>
    <wp:post_date_gmt>{item_post_date}</wp:post_date_gmt>
    <wp:post_modified>{item_post_date}</wp:post_modified>
    <wp:post_modified_gmt>{item_post_date}</wp:post_modified_gmt>
    <wp:comment_status>closed</wp:comment_status>
    <wp:ping_status>closed</wp:ping_status>
    <wp:post_name>{cdata(slug)}</wp:post_name>
    <wp:status>publish</wp:status>
    <wp:post_parent>0</wp:post_parent>
    <wp:menu_order>0</wp:menu_order>
    <wp:post_type>{post_type}</wp:post_type>
    <wp:post_password></wp:post_password>
    <wp:is_sticky>0</wp:is_sticky>
{terms_xml}  </item>'''
        items_xml.append(item)

    stats = {
        'posts': post_count,
        'pages': page_count,
        'attachments': len(attachment_xml),
        'meetings_excluded': len(meeting_pages),
        'tags': len(tag_names),
    }

    # Assemble full WXR document
    wxr = f'''<?xml version="1.0" encoding="UTF-8"?>
<rss version="2.0"
  xmlns:excerpt="http://wordpress.org/export/1.2/excerpt/"
  xmlns:content="http://purl.org/rss/1.0/modules/content/"
  xmlns:wfw="http://wellformedweb.org/CommentAPI/"
  xmlns:dc="http://purl.org/dc/elements/1.1/"
  xmlns:wp="http://wordpress.org/export/1.2/"
>
<channel>
  <title>Imported Site</title>
  <link>{escape(site_url)}</link>
  <description>Content imported from Wix site</description>
  <pubDate>{pub_date}</pubDate>
  <language>en-GB</language>
  <wp:wxr_version>1.2</wp:wxr_version>
  <wp:base_site_url>{escape(site_url)}</wp:base_site_url>
  <wp:base_blog_url>{escape(site_url)}</wp:base_blog_url>

  <wp:author>
    <wp:author_id>1</wp:author_id>
    <wp:author_login>{cdata("admin")}</wp:author_login>
    <wp:author_email>{cdata("admin@example.com")}</wp:author_email>
    <wp:author_display_name>{cdata("Admin")}</wp:author_display_name>
    <wp:author_first_name>{cdata("")}</wp:author_first_name>
    <wp:author_last_name>{cdata("")}</wp:author_last_name>
  </wp:author>

{chr(10).join(category_xml)}

{chr(10).join(tag_xml)}

{chr(10).join(attachment_xml)}

{chr(10).join(items_xml)}

</channel>
</rss>'''

    return wxr, stats


def main():
    input_dir = sys.argv[1] if len(sys.argv) > 1 else "gpcc_export"
    output_file = sys.argv[2] if len(sys.argv) > 2 else "wordpress_import.xml"

    print(f"Loading content from: {input_dir}/")
    pages = load_scraped_content(input_dir)

    if not pages:
        print("No content found to convert.")
        return 1

    print(f"Found {len(pages)} pages")

    # Count by category
    categories = {}
    for page in pages:
        cat = page.get('_category', 'uncategorized')
        categories[cat] = categories.get(cat, 0) + 1

    print("\nCategories:")
    for cat, count in sorted(categories.items()):
        print(f"  {cat}: {count} pages")

    print(f"\nGenerating WXR file: {output_file}")
    wxr_content, stats = generate_wxr(pages, input_dir=input_dir)

    with open(output_file, 'w', encoding='utf-8') as f:
        f.write(wxr_content)

    print(f"\nOutput:")
    print(f"  News posts:        {stats['posts']} (tagged by original category)")
    print(f"  Pages:             {stats['pages']} (includes the merged About page)")
    print(f"  Attachments:       {stats['attachments']} (images + documents)")
    print(f"  Tags:              {stats['tags']}")
    print(f"  Meetings excluded: {stats['meetings_excluded']} (run sync_meetings.py separately - see that script)")
    print(f"\nDone! Import {output_file} via WordPress Dashboard:")
    print("  Tools > Import > WordPress > Run Importer")
    print("\nIMPORTANT: Check 'Download and import file attachments' during import!")
    print("\nNote: All posts and pages are imported as published.")

    return 0


if __name__ == "__main__":
    sys.exit(main())
