#!/usr/bin/env python3
"""PlanRef daily source check.

Runs every morning via GitHub Actions. It:
 1. scans official sources for new planning-related instruments and judgments
 2. updates the "checked" stamp in changes.json so the app can show users
    the feed was checked this morning - including on no-change days
 3. writes anything interesting to candidates.json for Steve to review.
    It NEVER publishes new entries itself - editorial control stays human.

The "checked" stamp is only written when at least one source was actually
scanned. If every source fails, the stamp is left alone and the run fails
loudly so the problem is visible.
"""
import json, re, sys, urllib.request
from datetime import datetime, timezone, timedelta

KEYWORDS = re.compile(
    r'planning|town and country|infrastructure|permitted development|'
    r'compulsory purchase|neighbourhood|listed building|environmental impact|'
    r'habitats|biodiversity|community infrastructure levy|use classes|'
    r'development order|local plan|green belt', re.I)

SOURCES = [
    ('New UK statutory instruments', 'https://www.legislation.gov.uk/new/uksi/data.feed'),
    ('Planning Court judgments', 'https://caselaw.nationalarchives.gov.uk/atom.xml?court=ewhc%2Fadmin&order=-date'),
]

def uk_now():
    # UK local time: BST (UTC+1) roughly Apr-Oct, GMT otherwise. Good enough for a stamp.
    utc = datetime.now(timezone.utc)
    bst = utc.month in (4,5,6,7,8,9) or (utc.month == 3 and utc.day >= 25) or (utc.month == 10 and utc.day <= 25)
    return utc + timedelta(hours=1) if bst else utc

def fetch(url):
    req = urllib.request.Request(url, headers={'User-Agent': 'PlanRef-daily-check/1.0'})
    with urllib.request.urlopen(req, timeout=30) as r:
        return r.read().decode('utf-8', 'replace')

def titles_and_links(xml):
    out = []
    for entry in re.findall(r'<entry[\s>].*?</entry>', xml, re.S):
        t = re.search(r'<title[^>]*>(.*?)</title>', entry, re.S)
        l = re.search(r'<link[^>]*href="([^"]+)"', entry)
        if t:
            title = re.sub(r'<[^>]+>', '', t.group(1)).strip()
            out.append((title, l.group(1) if l else ''))
    return out

def main():
    scanned, candidates = [], []
    for name, url in SOURCES:
        try:
            xml = fetch(url)
        except Exception as e:
            print(f'WARN could not scan {name}: {e}', file=sys.stderr)
            continue
        scanned.append(name)
        for title, link in titles_and_links(xml)[:60]:
            if KEYWORDS.search(title):
                candidates.append({'source': name, 'title': title, 'url': link})

    if not scanned:
        print('FAIL: no source could be scanned - leaving the checked stamp untouched', file=sys.stderr)
        sys.exit(1)

    now = uk_now()
    stamp = now.strftime('%-d %B %Y, %H:%M (UK)')

    with open('changes.json', encoding='utf-8') as f:
        data = json.load(f)
    data['checked'] = stamp
    with open('changes.json', 'w', encoding='utf-8') as f:
        json.dump(data, f, ensure_ascii=False, indent=1)

    with open('candidates.json', 'w', encoding='utf-8') as f:
        json.dump({'checked': stamp, 'scanned': scanned, 'forReview': candidates},
                  f, ensure_ascii=False, indent=1)

    print(f'checked stamp -> {stamp}')
    print(f'sources scanned: {", ".join(scanned)}')
    print(f'candidates for review: {len(candidates)}')
    for c in candidates:
        print(f'  - [{c["source"]}] {c["title"]}')

if __name__ == '__main__':
    main()
