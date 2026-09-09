# /// script
# requires-python = ">=3.12"
# dependencies = ["pillow==12.3.0", "beautifulsoup4==4.13.4"]
# ///

"""Check selected Wiki crafting categories against the local item atlas."""

import argparse
import json
from collections import Counter
from pathlib import Path
from urllib.parse import urljoin

from audit_catalog import digest, download, normalized, write_json

ROOT = Path(__file__).resolve().parents[2] / 'skills/stardew-planner-skill'
WIKI_URL = 'https://zh.stardewvalleywiki.com/%E6%89%93%E9%80%A0'
SOURCE_PATH = 'data/recognition/crafting-source.json'
REPORT_PATH = 'data/recognition/crafting-audit.json'
SECTIONS = (
    '围栏',
    '洒水器',
    '工匠设备',
    '装饰',
    '照明',
    '精炼设备',
    '家具',
    '存储设备',
    '标牌',
    '杂项',
)
NON_PLACEABLE = {'Explosive Ammo', 'Iron Bar', 'Gold Bar'}


def refresh_snapshot(root):
    from bs4 import BeautifulSoup

    source = download(WIKI_URL)
    content = BeautifulSoup(source, 'html.parser').select_one(
        '.mw-parser-output'
    )
    if content is None:
        raise ValueError('Crafting page content was not found')
    rows, seen, section = [], set(), ''
    for node in content.find_all(['h2', 'h3', 'table']):
        if node.name in ['h2', 'h3']:
            section = (
                node.get_text(' ', strip=True).replace('[编辑]', '').strip()
            )
            continue
        if section not in SECTIONS:
            continue
        for row in node.find_all('tr'):
            cells = row.find_all('td', recursive=False)
            if len(cells) < 2:
                continue
            name, image = cells[1].find('a'), cells[0].find('img')
            if name is None or image is None:
                continue
            name_en = (
                image.get('alt', '').removesuffix('.png').replace('_', ' ')
            )
            key = (section, name_en)
            if not name_en or key in seen:
                raise ValueError(f'Empty or duplicate crafting row: {key}')
            seen.add(key)
            rows.append(
                {
                    'section': section,
                    'name_zh': name.get_text(' ', strip=True),
                    'name_en': name_en,
                    'source_page': urljoin(WIKI_URL, name.get('href', '')),
                    'image_url': urljoin(WIKI_URL, image.get('src', '')),
                }
            )
    if set(SECTIONS) != {row['section'] for row in rows}:
        raise ValueError(
            'One or more requested crafting categories are missing'
        )
    snapshot = {
        'schema_version': 1,
        'source': {'url': WIKI_URL, 'sha256': digest(source)},
        'sections': list(SECTIONS),
        'item_count': len(rows),
        'items': rows,
    }
    write_json(root / SOURCE_PATH, snapshot)
    return snapshot


def audit(root, snapshot):
    root = Path(root)
    catalog = json.loads((root / 'data/planner/catalog.json').read_text())
    supplemental = json.loads(
        (root / 'data/recognition/supplemental.json').read_text()
    )
    index = json.loads((root / 'data/recognition/index.json').read_text())
    entities = supplemental.get('reference_entities', {})
    records = []
    for source in snapshot['items']:
        record = {**source}
        name = source['name_en']
        if name in NON_PLACEABLE:
            record.update(
                status='not_placeable', reason='material_or_ammunition'
            )
            records.append(record)
            continue
        candidates = [
            kind
            for kind, item in {**catalog['items'], **entities}.items()
            if normalized(name)
            in {
                normalized(item['name_en']),
                *(normalized(alias) for alias in item.get('aliases', [])),
            }
        ]
        candidates.sort(
            key=lambda kind: (
                catalog['items'].get(kind, {}).get('hidden_in_menu', False),
                len(kind),
                kind,
            )
        )
        if not candidates:
            record['status'] = 'missing'
        else:
            kind = candidates[0]
            card = index['items'].get(index['lookup'].get(kind))
            record['catalog_id'] = kind
            if card is None:
                record['status'] = 'missing_recognition'
            elif kind in entities:
                record['status'] = (
                    'ignored'
                    if card.get('recognition_action') == 'omit'
                    else 'omission_mismatch'
                )
                record['reason'] = 'user_requested_layout_omission'
                record['sheet'] = card['sheet']
            else:
                item = catalog['items'][kind]
                footprint = item['placement']['footprint']
                pixels = root / 'data/planner' / item['image']['path']
                record.update(
                    status='covered'
                    if pixels.is_file()
                    and digest(pixels.read_bytes()) == item['image']['sha256']
                    else 'invalid_sprite',
                    footprint_tiles=[footprint['width'], footprint['height']],
                    recognition_group=card['group'],
                    sheet=card['sheet'],
                )
        records.append(record)
    unresolved = [
        row['name_en']
        for row in records
        if row['status'] not in {'covered', 'ignored', 'not_placeable'}
    ]
    return {
        'schema_version': 1,
        'source': snapshot['source'],
        'source_snapshot_sha256': digest((root / SOURCE_PATH).read_bytes()),
        'catalog_sha256': digest(
            (root / 'data/planner/catalog.json').read_bytes()
        ),
        'recognition_index_sha256': digest(
            (root / 'data/recognition/index.json').read_bytes()
        ),
        'checked_item_count': len(records),
        'counts': dict(Counter(row['status'] for row in records)),
        'sections': {
            section: dict(
                Counter(
                    row['status']
                    for row in records
                    if row['section'] == section
                )
            )
            for section in snapshot['sections']
        },
        'unresolved_names': unresolved,
        'items': records,
    }


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--root', type=Path, default=ROOT)
    parser.add_argument('--refresh', action='store_true')
    args = parser.parse_args()
    root = args.root.resolve()
    snapshot = (
        refresh_snapshot(root)
        if args.refresh
        else json.loads((root / SOURCE_PATH).read_text())
    )
    report = audit(root, snapshot)
    write_json(root / REPORT_PATH, report)
    print(
        json.dumps(
            {
                key: report[key]
                for key in [
                    'checked_item_count',
                    'counts',
                    'sections',
                    'unresolved_names',
                ]
            },
            ensure_ascii=False,
        )
    )
    raise SystemExit(bool(report['unresolved_names']))


if __name__ == '__main__':
    main()
