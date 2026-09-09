# /// script
# requires-python = ">=3.12"
# dependencies = ["pillow==12.3.0"]
# ///

"""Refresh allowlisted vanilla maps from native TMX and audit farm baselines."""

import argparse
import copy
import hashlib
import json
import re
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime
from io import BytesIO
from pathlib import Path
from urllib.parse import quote, urljoin
from urllib.request import Request, urlopen

from map_tmx import TmxMap, truth
from PIL import Image, ImageChops

ROOT = Path(__file__).resolve().parents[2] / 'skills/stardew-planner-skill'
GAME_VERSION = '1.6.15'
ASSET_URL = f'https://assets.stardewplan.com/assets/{GAME_VERSION}/'
SEASONS = ['spring', 'summer', 'fall', 'winter']
FARMS = {
    'regular': 'Farm',
    'fishing': 'Farm_Fishing',
    'foraging': 'Farm_Foraging',
    'mining': 'Farm_Mining',
    'combat': 'Farm_Combat',
    'fourcorners': 'Farm_FourCorners',
    'beach': 'Farm_Island',
    'ranching': 'Farm_Ranching',
}
INTERIORS = {
    'farmhouse': (
        '农舍内部',
        'Farmhouse',
        'FarmHouse',
        ['farmhouse-0'],
        'interior',
    ),
    'farmhouse_upgraded_1': (
        '农舍内部（升级1）',
        'Farmhouse (Upgrade 1)',
        'FarmHouse1',
        ['farmhouse-1'],
        'interior',
    ),
    'farmhouse_upgraded_2': (
        '农舍内部（升级2）',
        'Farmhouse (Upgrade 2)',
        'FarmHouse2',
        ['farmhouse-2'],
        'interior',
    ),
    'farmhouse_upgraded_3': (
        '农舍内部（升级3）',
        'Farmhouse (Upgrade 3)',
        'FarmHouse2',
        ['farmhouse-3'],
        'interior',
    ),
    'farmhouse_cellar': (
        '地窖',
        'Cellar',
        'Cellar',
        ['cellar', 'farmhouse-cellar'],
        'interior',
    ),
    'shed': (
        '棚屋内部',
        'Shed Interior',
        'Shed',
        ['shed-interior'],
        'interior',
    ),
    'shed-big': (
        '大棚屋内部',
        'Big Shed Interior',
        'Shed2',
        ['big-shed'],
        'interior',
    ),
    'barn': ('畜棚内部', 'Barn Interior', 'Barn', ['barn-interior'], 'barn'),
    'barn-big': (
        '大畜棚内部',
        'Big Barn Interior',
        'Barn2',
        ['big-barn'],
        'barn',
    ),
    'barn-deluxe': (
        '高级畜棚内部',
        'Deluxe Barn Interior',
        'Barn3',
        ['deluxe-barn'],
        'barn',
    ),
    'coop': ('鸡舍内部', 'Coop Interior', 'Coop', ['coop-interior'], 'coop'),
    'coop-big': (
        '大鸡舍内部',
        'Big Coop Interior',
        'Coop2',
        ['big-coop'],
        'coop',
    ),
    'coop-deluxe': (
        '高级鸡舍内部',
        'Deluxe Coop Interior',
        'Coop3',
        ['deluxe-coop'],
        'coop',
    ),
    'slime-hutch': (
        '史莱姆屋内部',
        'Slime Hutch Interior',
        'SlimeHutch',
        [],
        'interior',
    ),
    'greenhouse': (
        '温室内部',
        'Greenhouse Interior',
        'Greenhouse',
        ['greenhouse-interior'],
        'interior',
    ),
    'farm-cave': (
        '农场洞穴内部',
        'Farm Cave Interior',
        'FarmCave',
        [],
        'interior',
    ),
    'island-farmhouse': (
        '姜岛农舍内部',
        'Island Farmhouse Interior',
        'IslandFarmHouse',
        ['island_farmhouse'],
        'interior',
    ),
    'ginger_island': (
        '姜岛农场',
        'Ginger Island Farm',
        'Island_W',
        ['ginger-island', 'island-west'],
        'island',
    ),
}
ISLAND_PATCHES = [
    ('Island_House_Restored', [0, 0, 7, 9], 74, 33),
    ('Island_House_Bin', [0, 0, 2, 2], 90, 38),
    ('Island_House_Cave', [0, 0, 3, 4], 95, 30),
    ('Island_W_Obelisk', [0, 0, 3, 9], 71, 29),
]


def sha256(value):
    return hashlib.sha256(value).hexdigest()


def write_json(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    text = json.dumps(value, ensure_ascii=False, indent=2)
    text = re.sub(r'\[\n\s+(-?\d+),\n\s+(-?\d+)\n\s+\]', r'[\1, \2]', text)
    path.write_text(text + '\n', encoding='utf-8')


class Sources:
    def __init__(self, root, offline=False, refresh=False):
        self.root = root
        self.offline = offline
        self.refresh = refresh
        manifest = root / 'sources.json'
        self.records = (
            json.loads(manifest.read_text()) if manifest.exists() else {}
        )

    def get(self, path, url):
        file = self.root / path
        previous = self.records.get(path)
        if self.offline or (file.exists() and previous and not self.refresh):
            if not previous or not file.exists() or previous['url'] != url:
                raise ValueError(f'Missing verified source: {path}')
            blob = file.read_bytes()
            if sha256(blob) != previous['sha256']:
                raise ValueError(f'Source hash mismatch: {path}')
            return blob, previous
        with urlopen(
            Request(url, headers={'User-Agent': 'stardew-planner-skill/1.0'}),
            timeout=45,
        ) as response:
            blob = response.read()
            content_type = response.headers.get('Content-Type', '')
        file.parent.mkdir(parents=True, exist_ok=True)
        file.write_bytes(blob)
        record = {
            'path': path,
            'url': url,
            'retrieved_at': datetime.now(UTC).isoformat(),
            'sha256': sha256(blob),
            'bytes': len(blob),
            'content_type': content_type,
        }
        self.records[path] = record
        return blob, record

    def save(self):
        write_json(self.root / 'sources.json', self.records)


def fetch_site(url):
    with urlopen(
        Request(url, headers={'User-Agent': 'stardew-planner-skill/1.0'}),
        timeout=45,
    ) as response:
        blob = response.read()
    return blob, {
        'url': url,
        'sha256': sha256(blob),
        'bytes': len(blob),
        'retrieved_at': datetime.now(UTC).isoformat(),
    }


def site_catalog(sources):
    path = sources.root / 'site-catalog.json'
    if sources.offline:
        return json.loads(path.read_text())
    homepage, home_source = fetch_site('https://stardewplan.com/')
    match = re.search(
        r'import\("(\./_app/immutable/entry/app\.[^" ]+\.js)"\)',
        homepage.decode(),
    )
    if not match:
        raise ValueError(
            'Cannot find the current stardewplan application entry'
        )
    app_url = urljoin('https://stardewplan.com/', match.group(1))
    app_blob, app_source = fetch_site(app_url)
    chunks = sorted(
        set(re.findall(r'"(\.\./chunks/[^" ]+\.js)"', app_blob.decode()))
    )

    def fetch_chunk(relative):
        url = urljoin(app_url, relative)
        with urlopen(
            Request(url, headers={'User-Agent': 'stardew-planner-skill/1.0'}),
            timeout=45,
        ) as response:
            return url, response.read()

    with ThreadPoolExecutor(max_workers=6) as pool:
        matches = [
            (url, blob)
            for url, blob in pool.map(fetch_chunk, chunks)
            if b'mapFile:"Farm.tmx"' in blob and b'mapFile:"Coop3.tmx"' in blob
        ]
    if len(matches) != 1:
        raise ValueError('Cannot identify a unique stardewplan map catalog')
    url, blob = matches[0]
    map_files = set(re.findall(r'mapFile:"([^" ]+\.tmx)"', blob.decode()))
    expected = {f'{name}.tmx' for name in FARMS.values()} | {
        f'{entry[2]}.tmx' for entry in INTERIORS.values()
    }
    if not expected <= map_files:
        raise ValueError(
            f'Maps missing from current website: {sorted(expected - map_files)}'
        )
    result = {
        'homepage_source': home_source,
        'entry_source': app_source,
        'catalog_source': {
            'url': url,
            'sha256': sha256(blob),
            'bytes': len(blob),
            'retrieved_at': datetime.now(UTC).isoformat(),
        },
        'verified_vanilla_map_files': sorted(expected),
    }
    write_json(path, result)
    return result


def get_map(sources, name):
    blob, record = sources.get(
        f'tmx/{name}.tmx', f'{ASSET_URL}maps/{name}.tmx'
    )
    return TmxMap(blob), record


def tile_properties(tmx, key):
    return sorted(
        [x, y]
        for y in range(tmx.height)
        for x in range(tmx.width)
        if truth(tmx.props('Back', x, y).get(key))
    )


def map_rules(map_id, category):
    return {
        'allow_farm_buildings': category == 'farm',
        'allow_ground_crops': category in ['farm', 'island']
        or map_id == 'greenhouse',
        'allow_giant_crops': category == 'farm',
        'allow_trees': category in ['farm', 'island']
        or map_id == 'greenhouse',
        'building_scope': 'Only exterior farm building layouts are supported by this skill.',
        'crop_rule_source': 'rules-sources.json#giant_crop_locations',
        'sprinkler_terrain': 'tmx_NoSprinklers',
    }


def sync(offline=False, refresh=False):
    output = ROOT / 'data/maps'
    existing = json.loads((output / 'maps.json').read_text())
    sources = Sources(output / 'sources', offline, refresh)
    site = site_catalog(sources)
    names = sorted(
        set(FARMS.values())
        | {value[2] for value in INTERIORS.values()}
        | {patch[0] for patch in ISLAND_PATCHES}
        | {'FarmHouse_Cellar'}
    )
    native = {}
    for name in names:
        native[name] = get_map(sources, name)
    records = {}
    verification = {}
    for map_id, name in FARMS.items():
        record = copy.deepcopy(existing['maps'][map_id])
        tmx, source = native[name]
        if (record['width_tiles'], record['height_tiles']) != (
            tmx.width,
            tmx.height,
        ):
            raise ValueError(f'Farm size differs from native TMX: {map_id}')
        restriction_path = output / record['restrictions_path']
        current = json.loads(restriction_path.read_text())
        original = current.get('baseline_blocked', current['blocked'])
        current['baseline_blocked'] = {
            key: original[key]
            for key in ['accessible', 'buildable', 'tillable']
        }
        blocked, _ = tmx.restrictions('farm')
        differences = {}
        for key in ['accessible', 'buildable', 'tillable']:
            previous = set(map(tuple, original[key]))
            differences[key] = {
                'baseline_blocked': len(previous),
                'native_blocked': len(blocked[key]),
                'newly_blocked': len(blocked[key] - previous),
                'baseline_only': len(previous - blocked[key]),
            }
        # Retain verified farm placement coordinates; fix water passability.
        current['blocked']['accessible'] = sorted(
            set(map(tuple, original['accessible'])) | blocked['accessible']
        )
        current['blocked']['furniture'] = sorted(
            set(map(tuple, current['blocked']['accessible']))
            | blocked['furniture']
        )
        current['sources']['native_tmx'] = source
        current['wall_tiles'] = []
        current['blocked']['wall'] = sorted(blocked['wall'])
        current['water_tiles'] = tile_properties(tmx, 'Water')
        current['no_sprinkler_tiles'] = tile_properties(tmx, 'NoSprinklers')
        write_json(restriction_path, current)
        record.update(
            {
                'category': 'farm',
                'vanilla': True,
                'game_version': GAME_VERSION,
                'seasonal': True,
                'source_map': source,
                'gameplay_rules': map_rules(map_id, 'farm'),
            }
        )
        farm_sheets = {}
        for sheet in tmx.tilesets:
            sheet_name = (
                sheet['source'].replace('\\', '/').split('/')[-1] + '.png'
            )
            blob, _ = sources.get(
                f'tilesheets/{sheet_name}',
                f'{ASSET_URL}tilesheets/{quote(sheet_name)}',
            )
            farm_sheets[sheet['source']] = Image.open(BytesIO(blob)).convert(
                'RGBA'
            )
        native_image = tmx.render(farm_sheets).convert('RGB')
        old_background = Image.open(
            output / record['backgrounds']['spring']['path']
        ).convert('RGB')
        difference = ImageChops.difference(
            native_image, old_background
        ).convert('L')
        different = sum(
            value > 10 for value in difference.get_flattened_data()
        )
        verification[map_id] = {
            'dimensions_match': True,
            'spring_background_comparison': {
                'method': 'Native TMX layers versus retained background; absolute luma difference greater than 10.',
                'different_pixels': different,
                'different_pixel_ratio': round(
                    different / (native_image.width * native_image.height), 6
                ),
                'note': 'Native first-frame water animation and cleared default building ground can differ from the retained background.',
            },
            'native_size_tiles': [tmx.width, tmx.height],
            'restrictions_comparison': differences,
            'background_status': 'Existing four-season native-scale backgrounds retained; map dimensions and restrictions checked against TMX.',
        }
        records[map_id] = record
    for map_id, (
        name_zh,
        name_en,
        name,
        aliases,
        category,
    ) in INTERIORS.items():
        tmx, source = native[name]
        tmx = copy.deepcopy(tmx)
        patches = []
        if map_id == 'ginger_island':
            for patch_name, rect, x, y in ISLAND_PATCHES:
                patch, patch_source = native[patch_name]
                tmx.paste(patch, rect, x, y)
                patches.append(
                    {
                        'source': patch_source,
                        'source_rect_tiles': rect,
                        'destination_tile': [x, y],
                    }
                )
        elif map_id == 'farmhouse_upgraded_3':
            patch, patch_source = native['FarmHouse_Cellar']
            rect = [0, 0, patch.width, patch.height]
            tmx.paste(patch, rect, 0, 0)
            patches.append(
                {
                    'source': patch_source,
                    'source_rect_tiles': rect,
                    'destination_tile': [0, 0],
                }
            )
        sheets, sheet_sources = {}, {}
        for sheet in tmx.tilesets:
            sheet_name = sheet['source'].replace('\\', '/').split('/')[-1]
            if not sheet_name.endswith('.png'):
                sheet_name += '.png'
            data, sheet_source = sources.get(
                f'tilesheets/{sheet_name}',
                f'{ASSET_URL}tilesheets/{quote(sheet_name)}',
            )
            sheets[sheet['source']] = Image.open(BytesIO(data)).convert('RGBA')
            sheet_sources[sheet['source']] = sheet_source
        folder = output / map_id
        folder.mkdir(exist_ok=True)
        image = tmx.render(sheets)
        image.save(folder / 'spring.png')
        blocked, wall_tiles = tmx.restrictions(
            category, map_id == 'greenhouse'
        )
        fixed = [
            {
                'id': f'fixed-passage-{index}',
                'name_zh': f'固定通道 {index}',
                'name_en': f'Fixed passage {index}',
                'fixed': True,
                'x': x,
                'y': y,
                'tiles': [[x, y]],
                'source_layer': 'TMX Warp/Action/TouchAction',
                'destination': destination,
            }
            for index, (x, y, destination) in enumerate(
                tmx.interaction_tiles(), 1
            )
        ]
        defaults = []
        if category in ['coop', 'barn']:
            defaults.append(
                {
                    'id': 'default-feed-hopper',
                    'type': 'feed-hopper',
                    'x': 3 if category == 'coop' else 6,
                    'y': 3,
                    'tags': ['default', 'fixed'],
                }
            )
            if category == 'coop' and map_id != 'coop':
                defaults.append(
                    {
                        'id': 'default-incubator',
                        'type': 'incubator',
                        'x': 2,
                        'y': 3,
                        'tags': ['default', 'fixed'],
                    }
                )
        restriction_data = {
            'schema_version': 2,
            'map': map_id,
            'unit': 'tile',
            'blocked': {key: sorted(value) for key, value in blocked.items()},
            'wall_tiles': sorted(wall_tiles),
            'water_tiles': tile_properties(tmx, 'Water'),
            'no_sprinkler_tiles': tile_properties(tmx, 'NoSprinklers'),
            'sources': {'native_tmx': source, 'patches': patches},
            'derivation': 'TMX Back/Buildings tile properties, per-tile overrides, WallID and Warp; Paths spawn markers excluded from rendered backgrounds.',
        }
        write_json(folder / 'restrictions.json', restriction_data)
        background = {
            'path': f'{map_id}/spring.png',
            'sha256': sha256((folder / 'spring.png').read_bytes()),
            'source': source,
            'tilesheet_sources': sheet_sources,
            'patches': patches,
            'derived': True,
        }
        limitations = (
            [
                'Farmhouse renovation combinations, spouse-room variants and custom wall/floor patterns are not included.'
            ]
            if name.startswith('FarmHouse')
            else []
        )
        if map_id == 'ginger_island':
            limitations.append(
                'Restored farmhouse, shipping bin, cave and return obelisk are included as fixed background tiles.'
            )
        record = {
            'id': map_id,
            'name_zh': name_zh,
            'name_en': name_en,
            'aliases': sorted(
                set(
                    aliases
                    + [
                        name_zh.replace('内部', ''),
                        name_en.replace(' Interior', ''),
                    ]
                )
                - {map_id, name_zh, name_en}
            ),
            'category': category,
            'vanilla': True,
            'game_version': GAME_VERSION,
            'game_farm_id': None,
            'width_tiles': tmx.width,
            'height_tiles': tmx.height,
            'width_px': tmx.width * 16,
            'height_px': tmx.height * 16,
            'seasonal': False,
            'backgrounds': {season: background for season in SEASONS},
            'restrictions_path': f'{map_id}/restrictions.json',
            'source_map': source,
            'default_objects': defaults,
            'fixed_landmarks': fixed,
            'gameplay_rules': map_rules(map_id, category),
            'limitations': limitations,
        }
        records[map_id] = record
        verification[map_id] = {
            'native_size_tiles': [tmx.width, tmx.height],
            'background_size_px': list(image.size),
            'background_status': 'Rendered from native TMX layers and native tilesheets at 16 pixels per tile.',
            'blocked_counts': {
                key: len(value) for key, value in blocked.items()
            },
        }
        print(
            f'{map_id}: {tmx.width} x {tmx.height} tiles ({category})',
            flush=True,
        )
    # The allowlist is independent of either website menu and never imports mods.
    allowlist = {
        'schema_version': 1,
        'game_mode': 'vanilla',
        'categories': ['farm', 'interior', 'coop', 'barn', 'island'],
        'map_ids': list(records),
    }
    write_json(output / 'allowlist.json', allowlist)
    write_json(
        output / 'source-maps.json',
        {
            map_id: {
                'map_file': record['source_map']['path'].split('/')[-1],
                'category': record['category'],
                'source': record['source_map'],
            }
            for map_id, record in records.items()
        },
    )
    write_json(
        output / 'verification.json',
        {
            'schema_version': 1,
            'game_version': GAME_VERSION,
            'site_verification': site,
            'maps': verification,
        },
    )
    existing.update(
        {
            'schema_version': 2,
            'game_version': GAME_VERSION,
            'source_map_count': len(records),
            'supported_map_count': len(records),
            'allowlist_path': 'allowlist.json',
            'maps': records,
            'stardewplan_source': site['catalog_source'],
            'limitations': [
                'Supported map scope is restricted by allowlist.json to vanilla farms, building interiors and Ginger Island.',
                'Farm backgrounds retain the existing four-season assets; new backgrounds are derived from native 16-pixel TMX maps.',
                'Map tile restrictions do not reproduce every item-specific gameplay rule.',
            ],
        }
    )
    existing.pop('other_map_ids', None)
    write_json(output / 'maps.json', existing)
    for obsolete in ['homepage.html', 'site-entry.js']:
        sources.records.pop(obsolete, None)
        (sources.root / obsolete).unlink(missing_ok=True)
    sources.save()
    return existing


def main():
    parser = argparse.ArgumentParser(
        description='Refresh vanilla interior/island maps and verify original farm terrain.'
    )
    parser.add_argument('--offline', action='store_true')
    parser.add_argument('--refresh', action='store_true')
    args = parser.parse_args()
    if args.offline and args.refresh:
        parser.error('--offline and --refresh cannot be combined')
    sync(args.offline, args.refresh)


if __name__ == '__main__':
    main()
