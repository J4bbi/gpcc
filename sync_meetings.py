#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""
Sync GPCC's scraped meeting pages into Events Manager as real calendar events.

Why this is separate from convert_to_wordpress.py / the WordPress Importer:
Events Manager stores event data (date, time, etc.) in its own custom DB
table (wp_em_events), not just post meta, and it has no hooks into the
standard WordPress/WXR importer. A WXR item of post_type=event would create
a bare wp_posts row with none of that, so it wouldn't show up as a real
event anywhere. Instead this script groups the scraped agenda/report/summary
pages by meeting occurrence, synthesizes one page of content per meeting,
and creates each one properly via Events Manager's own REST API
(POST /wp-json/events-manager/v1/events), which is the only path that
correctly writes both the post and the custom table row.

Usage:
    python sync_meetings.py --site http://localhost:8080 \\
        --user admin --app-password "xxxx xxxx xxxx xxxx xxxx xxxx" \\
        [--input-dir gpcc_export] [--dry-run] [--publish]

    Generate an application password first:
        docker compose run --rm --entrypoint wp wp-cli \\
            user application-password create <username> sync-meetings
"""
import argparse
import calendar
import glob
import json
import os
import re
import sys
from datetime import date

import requests

from content_cleaning import is_boilerplate_paragraph

MONTHS = {
    'january': 1, 'february': 2, 'march': 3, 'april': 4, 'may': 5, 'june': 6,
    'july': 7, 'august': 8, 'september': 9, 'october': 10, 'november': 11, 'december': 12,
}
MONTH_NAMES_RE = '|'.join(MONTHS.keys())
WEEKDAY_RE = 'monday|tuesday|wednesday|thursday|friday|saturday|sunday'

TITLE_RE = re.compile(
    rf'^(?P<month>{MONTH_NAMES_RE})\s+(?P<year>\d{{4}})\s+(?P<kind>MEETING|AGM)\s*-\s*(?P<subtype>.+)$',
    re.IGNORECASE,
)
# Matches "Wednesday, 17 SEPTEMBER 2025" and glued variants like "WEDNESDAY15 OCTOBER 2025"
# (a scraping artifact where adjacent inline elements lose their whitespace).
DATE_RE = re.compile(
    rf'(?:{WEEKDAY_RE})[,\s]*(\d{{1,2}})\s+({MONTH_NAMES_RE})\s+(\d{{4}})',
    re.IGNORECASE,
)

LANDING_PAGE_TITLES = {'meetings'}


def load_meeting_pages(input_dir):
    pages = []
    for path in sorted(glob.glob(os.path.join(input_dir, 'by_category', 'meetings', '*.json'))):
        with open(path, encoding='utf-8') as f:
            data = json.load(f)
        if data.get('title', '').strip().lower() in LANDING_PAGE_TITLES:
            continue
        pages.append(data)
    return pages


def classify_page(page):
    """Return (year, month, kind, subtype_label) parsed from the page title,
    or None if the title doesn't match the expected "<Month> <Year> Meeting/AGM
    - <Subtype>" pattern."""
    title = page.get('title', '').strip()
    m = TITLE_RE.match(title)
    if not m:
        return None
    month = MONTHS[m.group('month').lower()]
    year = int(m.group('year'))
    subtype = m.group('subtype').strip()
    subtype_lower = subtype.lower()

    if 'inaugural' in subtype_lower:
        kind = 'INAUGURAL'
    elif m.group('kind').upper() == 'AGM':
        kind = 'AGM'
    else:
        kind = 'MEETING'

    if 'agenda' in subtype_lower:
        doc_type = 'agenda'
    else:
        doc_type = 'minutes'  # report / draft report / summary / inaugural-report

    return year, month, kind, doc_type, subtype


def extract_meeting_date(pages, year, month):
    """Search every page in the group for an explicit "<Weekday> <day> <Month>
    <year>" date matching the group's known month/year. Returns (date, True)
    if found, or (a computed 3rd-Wednesday fallback, False) if not - GPCC's
    ordinary meetings have historically landed on the 3rd Wednesday, but this
    is a best-effort default, not a confirmed fact for every month."""
    for page in pages:
        for para in page.get('paragraphs', []):
            for dm in DATE_RE.finditer(para):
                day, month_name, year_str = dm.groups()
                if MONTHS[month_name.lower()] == month and int(year_str) == year:
                    try:
                        return date(year, month, int(day)), True
                    except ValueError:
                        continue

    # Fallback: 3rd Wednesday of the month
    cal = calendar.Calendar()
    wednesdays = [d for d in cal.itermonthdates(year, month)
                  if d.month == month and d.weekday() == calendar.WEDNESDAY]
    return wednesdays[2], False


def page_to_html(page, heading):
    parts = [f"<h3>{heading}</h3>"]
    for para in page.get('paragraphs', []):
        if para.strip() and not is_boilerplate_paragraph(para):
            parts.append(f"<p>{para.strip()}</p>")
    return "\n".join(parts)


def group_meetings(pages):
    groups = {}
    unclassified = []
    for page in pages:
        classified = classify_page(page)
        if classified is None:
            unclassified.append(page)
            continue
        year, month, kind, doc_type, subtype = classified
        key = (year, month, kind)
        groups.setdefault(key, {'agenda': [], 'minutes': []})
        groups[key][doc_type].append((subtype, page))
    return groups, unclassified


def build_event(year, month, kind, docs, input_dir):
    all_pages = [p for _, p in docs['agenda']] + [p for _, p in docs['minutes']]
    event_date, date_confirmed = extract_meeting_date(all_pages, year, month)

    month_name = calendar.month_name[month]
    kind_label = {'MEETING': 'Meeting', 'AGM': 'AGM', 'INAUGURAL': 'Inaugural Meeting'}[kind]
    event_name = f"GPCC {kind_label} - {month_name} {year}"

    content_parts = []
    for subtype, page in docs['agenda']:
        content_parts.append(page_to_html(page, 'Agenda'))
    for subtype, page in docs['minutes']:
        heading = 'Minutes' if 'summary' not in subtype.lower() else 'Summary'
        content_parts.append(page_to_html(page, heading))
    content = "\n\n".join(content_parts)

    source_urls = [p.get('url', '') for _, p in docs['agenda'] + docs['minutes']]

    return {
        'event_name': event_name,
        'event_start_date': event_date.isoformat(),
        'content': content,
        'date_confirmed': date_confirmed,
        'source_urls': source_urls,
    }


def get_or_create_category(session, site, name):
    """Look up an Events Manager event-category term by name, creating it if
    it doesn't exist yet. Returns the term id."""
    resp = session.get(
        f"{site}/",
        params={'rest_route': '/events-manager/v1/categories', 'search': name},
        timeout=30,
    )
    resp.raise_for_status()
    for item in resp.json().get('items', []):
        if item['name'].lower() == name.lower():
            return item['id']

    resp = session.post(
        f"{site}/",
        params={'rest_route': '/events-manager/v1/categories'},
        json={'name': name},
        timeout=30,
    )
    resp.raise_for_status()
    return resp.json()['id']


def get_or_create_location(session, site, name, address, town, postcode, region, country):
    """Look up an Events Manager location by name, creating it if it doesn't
    exist yet. Returns the location id."""
    resp = session.get(
        f"{site}/",
        params={'rest_route': '/events-manager/v1/locations', 'search': name},
        timeout=30,
    )
    resp.raise_for_status()
    for item in resp.json().get('items', []):
        if item['name'].lower() == name.lower():
            return item['id']

    resp = session.post(
        f"{site}/",
        params={'rest_route': '/events-manager/v1/locations'},
        json={
            'location_name': name,
            'location_address': address,
            'location_town': town,
            'location_postcode': postcode,
            'location_region': region,
            'location_country': country,
            'post_status': 'publish',
        },
        timeout=30,
    )
    resp.raise_for_status()
    return resp.json()['id']


def create_event(session, site, event, publish, category_id, location_id):
    payload = {
        'event_name': event['event_name'],
        'event_start_date': event['event_start_date'],
        'content': event['content'],
        'post_status': 'publish' if publish else 'draft',
        'event_categories': [category_id],
        'location_id': location_id,
    }
    # Uses the ?rest_route= query-string form rather than /wp-json/... - it
    # works regardless of whether pretty permalinks/.htaccess rewriting are
    # set up on the target site, whereas /wp-json/ 404s until they are.
    resp = session.post(
        f"{site}/",
        params={'rest_route': '/events-manager/v1/events'},
        json=payload,
        timeout=30,
    )
    if resp.status_code != 201:
        return resp

    # Observed in practice: immediately after creating a brand-new location
    # and then rapid-firing many event creates that reference it, the create
    # response body echoes the right location but it isn't actually
    # persisted (looks like a cache/timing race, not a hard API bug - a
    # create done in isolation, well after the location exists, persists
    # correctly). The response can't be used to detect this - it reports
    # success either way - so always follow up with an explicit PATCH rather
    # than try to conditionally detect the failure. Always pass post_status
    # explicitly on that PATCH too, since omitting it resets the event to
    # published regardless of its current status (a separate, real bug in
    # the update endpoint).
    event_id = resp.json()['id']
    return session.patch(
        f"{site}/",
        params={'rest_route': f'/events-manager/v1/events/{event_id}'},
        json={'location_id': location_id, 'post_status': payload['post_status']},
        timeout=30,
    )


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument('--site', required=True, help='Base site URL, e.g. http://localhost:8080')
    parser.add_argument('--user', help='WordPress username (required unless --dry-run)')
    parser.add_argument('--app-password', help='WordPress application password (required unless --dry-run)')
    parser.add_argument('--input-dir', default='gpcc_export')
    parser.add_argument('--dry-run', action='store_true', help="Preview grouping/dates without calling the API")
    parser.add_argument('--publish', action='store_true', help="Create events as published (default: draft)")
    parser.add_argument('--event-category', default='Meeting',
                         help="Events Manager event category applied to every synced meeting (default: Meeting)")
    parser.add_argument('--location-name', default='Cameron House Community Centre',
                         help="Events Manager location applied to every synced meeting")
    parser.add_argument('--location-address', default='34 Prestonfield Avenue')
    parser.add_argument('--location-town', default='Edinburgh')
    parser.add_argument('--location-postcode', default='EH16 5EU')
    parser.add_argument('--location-region', default='Scotland')
    parser.add_argument('--location-country', default='GB', help='ISO 3166-1 alpha-2 country code')
    args = parser.parse_args()

    if not args.dry_run and not (args.user and args.app_password):
        parser.error("--user and --app-password are required unless --dry-run")

    pages = load_meeting_pages(args.input_dir)
    print(f"Loaded {len(pages)} meeting pages from {args.input_dir}/by_category/meetings/")

    groups, unclassified = group_meetings(pages)
    print(f"Grouped into {len(groups)} meeting occurrences")
    if unclassified:
        print(f"\n{len(unclassified)} page(s) didn't match the expected title pattern and were skipped:")
        for p in unclassified:
            print(f"  - {p.get('title')!r} ({p.get('url')})")

    events = []
    for (year, month, kind), docs in sorted(groups.items()):
        event = build_event(year, month, kind, docs, args.input_dir)
        events.append(event)

    events.sort(key=lambda e: e['event_start_date'])

    inferred_dates = [e for e in events if not e['date_confirmed']]

    print(f"\n{'DATE':<12} {'EVENT':<40} SOURCES")
    for e in events:
        marker = '' if e['date_confirmed'] else '  <-- inferred (no explicit date found in source)'
        print(f"{e['event_start_date']:<12} {e['event_name']:<40}{marker}")

    if inferred_dates:
        print(f"\n{len(inferred_dates)} event(s) have an inferred date (3rd Wednesday fallback) - verify these manually.")

    if args.dry_run:
        print("\n--dry-run: no events created.")
        return 0

    session = requests.Session()
    session.auth = (args.user, args.app_password)

    category_id = get_or_create_category(session, args.site, args.event_category)
    print(f"\nUsing event category {args.event_category!r} (term id {category_id})")

    location_id = get_or_create_location(
        session, args.site, args.location_name, args.location_address,
        args.location_town, args.location_postcode, args.location_region, args.location_country,
    )
    print(f"Using location {args.location_name!r} (location id {location_id})")

    print(f"Creating {len(events)} events at {args.site} (status={'publish' if args.publish else 'draft'})...")
    created, failed = 0, 0
    for event in events:
        resp = create_event(session, args.site, event, args.publish, category_id, location_id)
        # 201 = created cleanly; 200 = created then needed the location
        # self-heal PATCH (see create_event) - both are a successful outcome.
        if resp.status_code in (200, 201):
            created += 1
            print(f"  OK    {event['event_name']}")
        else:
            failed += 1
            print(f"  FAIL  {event['event_name']} -> HTTP {resp.status_code}: {resp.text[:200]}")

    print(f"\nDone: {created} created, {failed} failed.")
    return 1 if failed else 0


if __name__ == '__main__':
    sys.exit(main())
