#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""
Wix Site Scraper for https://www.grangeprestonfieldcc.org.uk/
With automatic content categorization.

Usage:
    pip install selenium beautifulsoup4 requests webdriver-manager
    python scrape_wix_categorized.py
"""
import os
import time
import re
import json
import requests
from datetime import datetime
from selenium import webdriver
from selenium.webdriver.chrome.options import Options
from bs4 import BeautifulSoup
from urllib.parse import urljoin, urlparse, unquote

from content_cleaning import extract_paragraph_html

BASE_URL = "https://www.grangeprestonfieldcc.org.uk/"
OUTPUT_DIR = "gpcc_export"

# Content categories based on URL patterns and keywords
CATEGORIES = {
    'meetings': {
        'url_patterns': ['meeting', 'minutes', 'agm', 'agenda'],
        'keywords': ['meeting', 'minutes', 'agenda', 'attendees', 'apologies'],
    },
    'planning': {
        'url_patterns': ['planning', 'development', 'application', 'licens'],
        'keywords': ['planning', 'application', 'development', 'building', 'licence'],
    },
    'news': {
        'url_patterns': ['news', 'update', 'announcement', 'blog'],
        'keywords': ['news', 'latest', 'announcement'],
    },
    'events': {
        'url_patterns': ['event', 'calendar', 'whats-on', 'activities'],
        'keywords': ['event', 'date', 'venue', 'registration', 'attend'],
    },
    'about': {
        'url_patterns': ['about', 'who-we-are', 'history', 'members', 'councillor'],
        'keywords': ['community council', 'councillor', 'member', 'history', 'founded'],
    },
    'contact': {
        'url_patterns': ['contact', 'get-in-touch', 'email', 'find-us'],
        'keywords': ['contact', 'email', 'phone', 'address', 'get in touch'],
    },
    'documents': {
        'url_patterns': ['document', 'download', 'file', 'report', 'pdf'],
        'keywords': ['download', 'document', 'report', 'pdf'],
    },
    'local_info': {
        'url_patterns': ['local', 'neighbourhood'],
        'keywords': ['neighbourhood', 'residents association'],
    },
}


def setup_driver():
    """Setup headless Chrome driver."""
    options = Options()
    options.add_argument('--headless=new')
    options.add_argument('--disable-gpu')
    options.add_argument('--no-sandbox')
    options.add_argument('--disable-dev-shm-usage')
    options.add_argument('--remote-debugging-port=9222')
    options.add_argument('--disable-extensions')
    options.add_argument('--disable-software-rasterizer')
    options.add_argument('--window-size=1920,1080')
    options.add_argument('--user-agent=Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36')

    driver = webdriver.Chrome(options=options)
    return driver


def scroll_page(driver):
    """Scroll page to trigger lazy loading."""
    last_height = driver.execute_script("return document.body.scrollHeight")

    for _ in range(5):
        driver.execute_script("window.scrollTo(0, document.body.scrollHeight);")
        time.sleep(1)
        new_height = driver.execute_script("return document.body.scrollHeight")
        if new_height == last_height:
            break
        last_height = new_height

    driver.execute_script("window.scrollTo(0, 0);")
    time.sleep(0.5)


def categorize_content(url, text_content):
    """Determine content category based on URL and content."""
    # Only match against the path, never the domain - the site's own name
    # ("grangeprestonfieldcc.org.uk") would otherwise match every URL.
    path_lower = urlparse(url).path.lower()
    text_lower = ' '.join(text_content).lower() if text_content else ''

    scores = {}

    for category, patterns in CATEGORIES.items():
        score = 0

        # Check URL patterns - a strong, deliberate signal (the page's own slug)
        for pattern in patterns['url_patterns']:
            if pattern in path_lower:
                score += 20

        # Check content keywords - a weaker signal, capped to limit noise
        # from words that appear incidentally in nav/footer on every page
        for keyword in patterns['keywords']:
            count = text_lower.count(keyword)
            score += min(count, 2)

        scores[category] = score

    # Return category with highest score, or 'general' if no strong match
    best_category = max(scores, key=scores.get)
    if scores[best_category] >= 10:
        return best_category

    return 'general'


def get_content_root(soup):
    """Wix blog/post pages render the actual article inside a
    data-id="content-viewer" div; everything else on the page (nav, "Recent
    Posts" sidebar teasers from OTHER pages, category filter widget, footer)
    lives outside it. Scoping extraction to this container when present is
    what keeps that site-wide chrome out of the scraped content - without it,
    every page's text gets polluted with duplicated snippets of other pages.
    Falls back to the whole page for templates that don't use this container
    (static pages like /who-we-are, /contact, blog listing pages, etc).
    """
    return soup.find(attrs={'data-id': 'content-viewer'}) or soup


def extract_text_content(soup):
    """Extract readable text content from page."""
    content = []

    # Remove script and style elements
    for element in soup(['script', 'style', 'noscript', 'iframe']):
        element.decompose()

    content_root = get_content_root(soup)

    # Get text from common content elements
    for tag in content_root.find_all(['h1', 'h2', 'h3', 'h4', 'h5', 'h6', 'p', 'li', 'td', 'th', 'span', 'div']):
        text = tag.get_text(strip=True)
        if text and len(text) > 3 and not text.startswith('{') and text not in content:
            content.append(text)

    return content


def extract_structured_content(soup, url):
    """Extract content in a structured format."""
    content = {
        'url': url,
        'scraped_at': datetime.now().isoformat(),
        'title': '',
        'published_date': '',
        'headings': [],
        'paragraphs': [],
        'links': [],
        'images': [],
        'lists': [],
        'tables': [],
    }

    # Title
    title_tag = soup.find('title')
    if title_tag:
        content['title'] = title_tag.get_text(strip=True)

    # Also check for h1
    h1 = soup.find('h1')
    if h1:
        content['title'] = h1.get_text(strip=True)

    # Wix's own "Updated:<date>" byline (e.g. <span data-hook="time-ago"
    # title="May 20, 2025">May 20, 2025</span>) - lives in the page header,
    # outside content_root, so it's captured here rather than as a paragraph.
    # The title attribute holds the real date even when the visible text is
    # a relative "X hours ago" that only makes sense at scrape time.
    time_tag = soup.find(attrs={'data-hook': 'time-ago'})
    if time_tag:
        content['published_date'] = time_tag.get('title', '').strip() or time_tag.get_text(strip=True)

    content_root = get_content_root(soup)

    # Headings
    for level in range(1, 7):
        for h in content_root.find_all(f'h{level}'):
            text = h.get_text(strip=True)
            if text:
                content['headings'].append({'level': level, 'text': text})

    # Paragraphs - stored as small HTML fragments (not plain text) so an
    # inline link inside a paragraph (e.g. "...on this Council site.")
    # survives instead of being flattened to dead text. See
    # content_cleaning.extract_paragraph_html().
    for p in content_root.find_all('p'):
        text = p.get_text(strip=True)
        if text and len(text) > 10:
            content['paragraphs'].append(extract_paragraph_html(p, base_url=url))

    # Links (external and documents) - left page-wide rather than scoped to
    # content_root, since document links are sometimes in a sidebar widget
    # rather than the article body and we don't want to lose those.
    for a in soup.find_all('a', href=True):
        href = a['href']
        text = a.get_text(strip=True)
        if href and text:
            # Check for document links
            if any(ext in href.lower() for ext in ['.pdf', '.doc', '.docx', '.xls', '.xlsx']):
                content['links'].append({'type': 'document', 'text': text, 'url': href})
            elif href.startswith('http') and 'grangeprestonfieldcc' not in href:
                content['links'].append({'type': 'external', 'text': text, 'url': href})

    # Images
    for img in content_root.find_all('img', src=True):
        src = img.get('src', '')
        alt = img.get('alt', '')
        if src and 'pixel' not in src.lower():
            content['images'].append({'src': src, 'alt': alt})

    # Lists
    for ul in content_root.find_all(['ul', 'ol']):
        items = []
        for li in ul.find_all('li', recursive=False):
            text = li.get_text(strip=True)
            if text:
                items.append(text)
        if items:
            content['lists'].append(items)

    # Tables
    for table in content_root.find_all('table'):
        rows = []
        for tr in table.find_all('tr'):
            cells = [td.get_text(strip=True) for td in tr.find_all(['td', 'th'])]
            if any(cells):
                rows.append(cells)
        if rows:
            content['tables'].append(rows)

    return content


def download_file(url, output_dir, subdir=''):
    """Download a file (image, PDF, etc.) and return local filename."""
    try:
        if url.startswith('//'):
            url = 'https:' + url
        elif url.startswith('/'):
            url = urljoin(BASE_URL, url)

        if '1x1' in url or 'pixel' in url.lower():
            return None

        response = requests.get(url, timeout=15)
        if response.status_code == 200 and len(response.content) > 500:
            parsed = urlparse(url)
            filename = os.path.basename(parsed.path).split('?')[0]
            filename = re.sub(r'[^\w\-_\.]', '_', unquote(filename))

            if not filename or filename == '_':
                ext = '.jpg' if 'image' in response.headers.get('content-type', '') else ''
                filename = f"file_{hash(url) % 100000}{ext}"

            save_dir = os.path.join(output_dir, subdir) if subdir else output_dir
            filepath = os.path.join(save_dir, filename)

            if not os.path.exists(filepath):
                with open(filepath, 'wb') as f:
                    f.write(response.content)
                return filename

    except Exception:
        pass

    return None


def extract_links(soup, base_url):
    """Extract internal links from page."""
    links = set()
    base_domain = urlparse(base_url).netloc

    for a in soup.find_all('a', href=True):
        href = a['href'].strip()

        if not href or href.startswith(('#', 'javascript:', 'mailto:', 'tel:')):
            continue

        # Always resolve through urljoin (not just for leading '/') so relative,
        # protocol-relative and empty hrefs all end up as a real absolute URL
        # instead of being hand-assembled into garbage like "://" -> ":".
        href = urljoin(base_url, href)
        parsed = urlparse(href)

        if parsed.scheme not in ('http', 'https') or not parsed.netloc:
            continue

        if parsed.netloc == base_domain:
            clean_url = f"{parsed.scheme}://{parsed.netloc}{parsed.path}"
            clean_url = clean_url.rstrip('/')
            if clean_url:
                links.add(clean_url)

    return links


def scrape_page(driver, url, output_dir):
    """Scrape a single page and return found links and page data."""
    print(f"  Loading: {url}")

    driver.get(url)
    time.sleep(3)
    scroll_page(driver)
    time.sleep(2)

    soup = BeautifulSoup(driver.page_source, 'html.parser')

    # Extract content
    text_content = extract_text_content(soup)
    structured_content = extract_structured_content(soup, url)

    # Categorize
    category = categorize_content(url, text_content)
    structured_content['category'] = category

    # Create category directory
    category_dir = os.path.join(output_dir, 'by_category', category)
    os.makedirs(category_dir, exist_ok=True)

    # Page filename
    path = urlparse(url).path.strip('/') or 'index'
    page_name = path.replace('/', '_')

    # Save structured JSON
    json_path = os.path.join(category_dir, f"{page_name}.json")
    with open(json_path, 'w', encoding='utf-8') as f:
        json.dump(structured_content, f, indent=2, ensure_ascii=False)

    # Save readable text
    text_path = os.path.join(category_dir, f"{page_name}.txt")
    with open(text_path, 'w', encoding='utf-8') as f:
        f.write(f"URL: {url}\n")
        f.write(f"Category: {category}\n")
        f.write(f"Title: {structured_content['title']}\n")
        f.write("=" * 60 + "\n\n")

        for heading in structured_content['headings']:
            prefix = '#' * heading['level']
            f.write(f"{prefix} {heading['text']}\n\n")

        for para in structured_content['paragraphs']:
            f.write(f"{para}\n\n")

    # Save raw HTML
    html_dir = os.path.join(output_dir, 'html')
    html_path = os.path.join(html_dir, f"{page_name}.html")
    with open(html_path, 'w', encoding='utf-8') as f:
        f.write(soup.prettify())

    # Download images
    images_dir = os.path.join(output_dir, 'images')
    for img in soup.find_all('img', src=True):
        download_file(img['src'], images_dir)

    # Download documents (PDFs, etc.)
    docs_dir = os.path.join(output_dir, 'documents')
    for a in soup.find_all('a', href=True):
        href = a['href']
        if any(ext in href.lower() for ext in ['.pdf', '.doc', '.docx', '.xls', '.xlsx']):
            if href.startswith('/'):
                href = urljoin(BASE_URL, href)
            downloaded = download_file(href, docs_dir)
            if downloaded:
                print(f"    Document: {downloaded}")

    # Find more links
    links = extract_links(soup, BASE_URL)

    return links, structured_content


def main():
    print(f"Scraping: {BASE_URL}")
    print(f"Output: {OUTPUT_DIR}/")
    print()

    # Create output directories
    for subdir in ['html', 'images', 'documents', 'by_category']:
        os.makedirs(os.path.join(OUTPUT_DIR, subdir), exist_ok=True)

    driver = setup_driver()

    visited = set()
    to_visit = {BASE_URL, BASE_URL.rstrip('/')}
    all_pages = []
    category_counts = {}

    try:
        while to_visit:
            url = to_visit.pop()
            url = url.rstrip('/')

            if url in visited:
                continue

            visited.add(url)

            if not url.startswith(('http://', 'https://')) or not urlparse(url).netloc:
                print(f"\nSkipping malformed URL: {url!r}")
                continue

            print(f"\n[{len(visited)}] Scraping: {url}")

            try:
                new_links, page_data = scrape_page(driver, url, OUTPUT_DIR)

                # Track category
                cat = page_data.get('category', 'general')
                category_counts[cat] = category_counts.get(cat, 0) + 1

                all_pages.append({
                    'url': url,
                    'title': page_data.get('title', ''),
                    'category': cat,
                })

                # Add new links
                for link in new_links:
                    link = link.rstrip('/')
                    if link not in visited:
                        to_visit.add(link)

            except Exception as e:
                print(f"  Error: {e}")

        # Create summary index
        summary = {
            'base_url': BASE_URL,
            'scraped_at': datetime.now().isoformat(),
            'total_pages': len(visited),
            'categories': category_counts,
            'pages': sorted(all_pages, key=lambda x: (x['category'], x['url'])),
        }

        index_path = os.path.join(OUTPUT_DIR, "index.json")
        with open(index_path, 'w', encoding='utf-8') as f:
            json.dump(summary, f, indent=2, ensure_ascii=False)

        # Create readable summary
        summary_path = os.path.join(OUTPUT_DIR, "SUMMARY.txt")
        with open(summary_path, 'w', encoding='utf-8') as f:
            f.write(f"GPCC Website Export Summary\n")
            f.write(f"{'=' * 60}\n\n")
            f.write(f"Source: {BASE_URL}\n")
            f.write(f"Exported: {datetime.now().strftime('%Y-%m-%d %H:%M')}\n")
            f.write(f"Total pages: {len(visited)}\n\n")

            f.write(f"Content by Category:\n")
            f.write(f"{'-' * 40}\n")
            for cat, count in sorted(category_counts.items(), key=lambda x: -x[1]):
                f.write(f"  {cat:20} : {count} pages\n")

            f.write(f"\n\nAll Pages by Category:\n")
            f.write(f"{'-' * 40}\n")
            current_cat = None
            for page in sorted(all_pages, key=lambda x: (x['category'], x['title'])):
                if page['category'] != current_cat:
                    current_cat = page['category']
                    f.write(f"\n## {current_cat.upper()}\n")
                f.write(f"  - {page['title'] or page['url']}\n")
                f.write(f"    {page['url']}\n")

        print(f"\n{'=' * 60}")
        print(f"Done! Scraped {len(visited)} pages")
        print(f"\nContent by category:")
        for cat, count in sorted(category_counts.items(), key=lambda x: -x[1]):
            print(f"  {cat:20} : {count} pages")

        print(f"\nOutput saved to: {OUTPUT_DIR}/")
        print(f"  - by_category/ : Content organized by type")
        print(f"  - html/        : Raw HTML pages")
        print(f"  - images/      : Downloaded images")
        print(f"  - documents/   : PDFs and other documents")
        print(f"  - SUMMARY.txt  : Human-readable summary")
        print(f"  - index.json   : Machine-readable index")

    finally:
        driver.quit()


if __name__ == '__main__':
    main()
