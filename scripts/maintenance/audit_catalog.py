# /// script
# requires-python = ">=3.12"
# dependencies = ["pillow==12.3.0"]
# ///

"""Audit vanilla source IDs, native sprites, footprints and recognition coverage."""

import argparse
import hashlib
import json
import re
from collections import Counter, defaultdict
from concurrent.futures import ThreadPoolExecutor
from functools import lru_cache
from io import BytesIO
from pathlib import Path
from urllib.error import URLError
from urllib.parse import quote
from urllib.request import Request, urlopen

from PIL import Image

ROOT = Path(__file__).resolve().parents[2] / 'skills/stardew-planner-skill'
VERSION = '1.6.15'
ROTATION_SOURCE_URL = (
    'https://stardewplan.com/_app/immutable/chunks/Bna96aps.js'
)
ROTATION_SOURCE_SHA256 = (
    '1734f0e00b2ba34fb5398716386bcda096c828222a3dc1cc5aeefb00a7bed6bb'
)
SOURCE_PATH = 'data/recognition/source-index.json'
REPORT_PATH = 'data/recognition/coverage-audit.json'
FURNITURE_SIZES = {
    'chair': [1, 2],
    'bench': [2, 2],
    'couch': [3, 2],
    'armchair': [2, 2],
    'dresser': [2, 2],
    'long table': [5, 3],
    'painting': [2, 2],
    'lamp': [1, 3],
    'decor': [1, 2],
    'bookcase': [2, 3],
    'table': [2, 3],
    'rug': [3, 2],
    'window': [1, 2],
    'fireplace': [2, 5],
    'torch': [1, 2],
    'sconce': [1, 2],
    'bed': [3, 5],
    'bed double': [3, 5],
    'bed child': [2, 4],
    'fishtank': [3, 2],
    'other': [1, 2],
    'randomized_plant': [1, 2],
}
FURNITURE_FOOTPRINTS = {
    'chair': [1, 1],
    'bench': [2, 1],
    'couch': [3, 1],
    'armchair': [2, 1],
    'dresser': [2, 1],
    'long table': [5, 2],
    'painting': [2, 2],
    'lamp': [1, 1],
    'decor': [1, 1],
    'bookcase': [2, 1],
    'table': [2, 2],
    'rug': [3, 2],
    'window': [1, 2],
    'fireplace': [2, 1],
    'torch': [1, 1],
    'sconce': [1, 2],
}
# These data records represent removed prototypes or map/event-only objects.
# Museum rewards that became Furniture records are covered by their current IDs.
LEGACY_CRAFTABLES = {
    **{str(n): 'legacy_house_plant' for n in range(8)},
    '26': 'legacy_wood_chair',
    '27': 'legacy_wood_chair',
    **{
        str(n): 'moved_to_furniture' for n in [31, 85, 86, 87, 88, 89, 94, 112]
    },
    '83': 'legacy_wicked_statue',
}
EVENT_CRAFTABLES = {
    'Door',
    'Locked Door',
    'Boulder',
    'Staircase',
    'Slime Ball',
    'Barrel',
    'Crate',
    'Cursed P.K. Arcade System',
    'Table Piece L',
    'Table Piece R',
}
UNSUPPORTED_CATALOG_IDS = {
    'ccfishtank',
    'table-piece-l',
    'table-piece-r',
    'wood-chair',
    'door',
    'locked-door',
    'cursed-p.k.-arcade-system',
}
EQUIVALENTS = {
    'Furniture/JojaCatalogue': 'joja-furniture-catalogue',
    'Furniture/MorrisPortrait': 'manager-of-the-year',
    'Furniture/WitchBroom': "witch's-broom",
    'Furniture/LeafyPlant': 'swamp-plant',
    'Furniture/CoupleElixirs': 'two-elixirs',
    'Furniture/WizardTower': "'wizard's-tower'",
    'Furniture/JunimoHut': 'decorative-junimo-hut',
    'BigCraftables/54': 'stone-owl-night-market',
    'BigCraftables/95': 'stone-owl',
    'BigCraftables/155': 'hmtgf',
    'BigCraftables/161': 'pinky-lemon',
    'BigCraftables/162': 'foroguemon',
    'Buildings/Farmhouse': 'house',
    'Buildings/Greenhouse': 'greenhouse-repaired',
    'Buildings/Cabin': 'stone-cabin',
    'Objects/251': 'tea-bush',
    'FloorsAndPaths/0': 'wooden-floor',
    'FloorsAndPaths/6': 'wooden-path',
    'FloorsAndPaths/8': 'cobblestone',
    'FloorsAndPaths/12': 'stone-walkway',
}
OBJECT_IDS = (
    '93',
    '251',
    '325',
    '463',
    '464',
    '599',
    '621',
    '645',
    '710',
    '746',
)
MANNEQUINS = {
    'male': ('男式人体模特', 2),
    'female': ('女式人体模特', 6),
    'cursed-male': ('被诅咒的男式人体模特', 10),
    'cursed-female': ('被诅咒的女式人体模特', 14),
}


def digest(data):
    return hashlib.sha256(data).hexdigest()


def write_json(path, data):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, ensure_ascii=False, indent=2) + '\n')


def normalized(value):
    return re.sub(r'[^a-z0-9]', '', value.lower().replace('&', 'and'))


def wiki_page(entry):
    name = entry['name_en']
    if entry['table'] == 'Mannequins':
        name = (
            'Cursed Mannequins'
            if 'cursed' in entry['source_id']
            else 'Mannequins'
        )
    elif entry['table'] == 'Furniture' and entry['source_id'] == 'JunimoHut':
        name = 'Junimo Hut (furniture)'
    elif name in ['Floor Divider L', 'Floor Divider R']:
        name = 'Floor Divider'
    elif entry['source_id'].startswith('DecorativeDoor'):
        name = 'Decorative Door'
    else:
        name = re.sub(r'([a-z0-9])([A-Z])', r'\1 \2', name)
    return 'https://stardewvalleywiki.com/' + quote(
        name.replace(' ', '_'), safe="_'()"
    )


def sprite_signature(image):
    """Ignore unused transparent RGB and padding, retaining exact visible pixels."""
    image = image.convert('RGBA')
    box = image.getbbox()
    if box:
        image = image.crop(box)
    background = Image.new('RGB', image.size)
    background.paste(image, mask=image.getchannel('A'))
    return digest(str(image.size).encode() + background.tobytes())


@lru_cache(maxsize=128)
def download(url):
    request = Request(url, headers={'User-Agent': 'stardew-planner-skill/1.0'})
    for attempt in range(3):
        try:
            with urlopen(request, timeout=45) as response:
                return response.read()
        except URLError:
            if attempt == 2:
                raise


def furniture_rotations(rect, count, kind, footprint):
    """Port the verified 1.6.15 planner's N5/rotationFootprints rules."""
    left, top, right, bottom = rect
    width, height = right - left, bottom - top
    result = []
    adjust_x, adjust_y = (
        (-1, 1)
        if kind in ['couch', 'armchair']
        else (-1, 0)
        if kind == 'long table'
        else (0, 0)
    )
    alt_width, alt_height = width, height
    rotated_footprint = footprint
    if footprint[0] != footprint[1]:
        alt_width = height - 16 + adjust_y * 16 + (16 if kind == 'rug' else 0)
        alt_height = width + 16 + adjust_x * 16 - (16 if kind == 'rug' else 0)
        rotated_footprint = [footprint[1] + adjust_y, footprint[0] + adjust_x]
    for rotation in range(1, count):
        if rotation == 2:
            source_rect = [
                right + alt_width,
                top,
                right + alt_width + width,
                bottom,
            ]
        else:
            source_rect = [right, top, right + alt_width, top + alt_height]
        result.append(
            {
                'rotation': rotation,
                'source_rect_px': source_rect,
                'flip_horizontal': rotation == 3,
                'footprint_tiles': footprint
                if rotation == 2
                else rotated_footprint,
            }
        )
    return result


def source_index(version=VERSION):
    """Keep a small, hashed source snapshot so later audits need no network."""
    base = f'https://assets.stardewplan.com/assets/{version}'
    tables = [
        'Furniture',
        'BigCraftables',
        'Buildings',
        'Objects',
        'FloorsAndPaths',
        'Fences',
        'Crops',
        'GiantCrops',
    ]
    urls = {name: f'{base}/data/{name}.json' for name in tables}
    with ThreadPoolExecutor(max_workers=6) as pool:
        data = dict(zip(urls, pool.map(download, urls.values()), strict=True))
    sources = {
        name: {'url': url, 'sha256': digest(data[name])}
        for name, url in urls.items()
    }
    rotation_source = download(ROTATION_SOURCE_URL)
    if digest(rotation_source) != ROTATION_SOURCE_SHA256:
        raise ValueError(
            'The furniture rotation source changed; review its crop and footprint rules before syncing.'
        )
    sources['furniture_rotation_logic'] = {
        'url': ROTATION_SOURCE_URL,
        'sha256': ROTATION_SOURCE_SHA256,
        'functions': ['N5', 'V5.rotationFootprints'],
    }
    parsed = {name: json.loads(value) for name, value in data.items()}
    sheet_names = {'craftables', 'furniture', 'Mannequins', 'springobjects'}
    sheet_names.update(
        fields[9].split('\\')[-1]
        for value in parsed['Furniture'].values()
        if len(fields := value.split('/')) > 9 and fields[9]
    )
    sheet_urls = {
        name: f'{base}/tilesheets/{name}.png' for name in sorted(sheet_names)
    }
    with ThreadPoolExecutor(max_workers=6) as pool:
        sheets = dict(
            zip(
                sheet_urls,
                pool.map(download, sheet_urls.values()),
                strict=True,
            )
        )
    sources.update(
        {
            name: {'url': url, 'sha256': digest(sheets[name])}
            for name, url in sheet_urls.items()
        }
    )
    images = {
        name: Image.open(BytesIO(value)).convert('RGBA')
        for name, value in sheets.items()
    }
    entries = {}

    def add(table, key, name, footprint, sheet=None, rect=None, **extra):
        entry = {
            'table': table,
            'source_id': key,
            'name_en': name,
            'footprint_tiles': footprint,
            **extra,
        }
        if sheet:
            pixels = images[sheet].crop(rect)
            if extra.get('flip_horizontal'):
                pixels = pixels.transpose(Image.Transpose.FLIP_LEFT_RIGHT)
            entry.update(
                {
                    'sheet': sheet,
                    'source_rect_px': list(rect),
                    'sprite_size_px': list(pixels.size),
                    'sprite_signature': sprite_signature(pixels),
                }
            )
        qualified = f'{table}/{key}'
        if extra.get('rotation'):
            qualified += f'@rotation-{extra["rotation"]}'
        entries[qualified] = entry

    for key, raw in parsed['Furniture'].items():
        fields = raw.split('/')
        name, kind = fields[:2]
        size = (
            FURNITURE_SIZES[kind]
            if fields[2] == '-1'
            else list(map(int, fields[2].split()))
        )
        footprint = (
            FURNITURE_FOOTPRINTS.get(kind, [1, 1])
            if fields[3] == '-1'
            else list(map(int, fields[3].split()))
        )
        sheet = (
            fields[9].split('\\')[-1]
            if len(fields) > 9 and fields[9]
            else 'furniture'
        )
        index = (
            int(fields[8])
            if len(fields) > 8 and fields[8].isdigit()
            else int(key)
            if key.isdigit()
            else 0
        )
        left = index * 16 % images[sheet].width
        top = index * 16 // images[sheet].width * 16
        add(
            'Furniture',
            key,
            name,
            footprint,
            sheet,
            (left, top, left + size[0] * 16, top + size[1] * 16),
            furniture_type=kind,
            rotations=int(fields[4]),
        )
        for rotation in furniture_rotations(
            [left, top, left + size[0] * 16, top + size[1] * 16],
            int(fields[4]),
            kind,
            footprint,
        ):
            add(
                'Furniture',
                key,
                name,
                rotation['footprint_tiles'],
                sheet,
                rotation['source_rect_px'],
                furniture_type=kind,
                rotation=rotation['rotation'],
                flip_horizontal=rotation['flip_horizontal'],
            )
    for key, item in parsed['BigCraftables'].items():
        index = item['SpriteIndex']
        left, top = index % 8 * 16, index // 8 * 32
        add(
            'BigCraftables',
            key,
            item['Name'],
            [1, 1],
            'craftables',
            (left, top, left + 16, top + 32),
        )
    for key, item in parsed['Buildings'].items():
        add(
            'Buildings',
            key,
            key,
            [item['Size']['X'], item['Size']['Y']],
            building_placement=building_placement(item, sources['Buildings']),
        )
    for key in OBJECT_IDS:
        item = parsed['Objects'][key]
        index = item['SpriteIndex']
        left, top = index % 24 * 16, index // 24 * 16
        add(
            'Objects',
            key,
            item['Name'],
            [1, 1],
            'springobjects',
            (left, top, left + 16, top + 16),
        )
    for table in ['FloorsAndPaths', 'Fences']:
        for key, item in parsed[table].items():
            object_id = str(item.get('ItemId', key))
            add(table, key, parsed['Objects'][object_id]['Name'], [1, 1])
    for key, (name_zh, index) in MANNEQUINS.items():
        left, top = index % 8 * 16, index // 8 * 32
        add(
            'Mannequins',
            key,
            f'Mannequin {key}',
            [1, 1],
            'Mannequins',
            (left, top, left + 16, top + 32),
            name_zh=name_zh,
        )
    for table in ['Crops', 'GiantCrops']:
        for key in parsed[table]:
            add(table, key, key, [3, 3] if table == 'GiantCrops' else [1, 1])
    return {
        'schema_version': 1,
        'asset_version': version,
        'sources': sources,
        'source_entry_counts': {
            key: len(value) for key, value in parsed.items()
        },
        'entries': entries,
        'scope': 'Vanilla placeable furniture, machines, buildings, paths, fences, selected objects, mannequins and normalized crops. Event props, legacy prototypes, mods and community assets are excluded.',
        'limitations': [
            {
                'id': 'wallpaper-and-indoor-floor-patterns',
                'status': 'surface_materials_not_implemented',
            },
            {'id': 'free-cactus', 'status': 'components_only'},
            {'id': 'cauldron-active', 'status': 'ignored_visual_effect'},
        ],
    }, sheets


def exclusion(entry):
    table, key, name = entry['table'], entry['source_id'], entry['name_en']
    if table == 'Furniture' and key == 'CCFishTank':
        return 'community_center_fixed_fish_tank'
    if table == 'BigCraftables':
        if key in LEGACY_CRAFTABLES:
            return LEGACY_CRAFTABLES[key]
        if name in EVENT_CRAFTABLES:
            return 'event_or_map_object'
    return None


def catalog_matches(catalog, root):
    names, pixels, sources = defaultdict(list), defaultdict(list), {}
    for key, item in catalog['items'].items():
        for name in [key, item['name_en'], *item.get('aliases', [])]:
            names[normalized(name)].append(key)
        with Image.open(
            root / 'data/planner' / item['image']['path']
        ) as image:
            pixels[sprite_signature(image)].append(key)
        source = item.get('source', {})
        if source.get('table'):
            source_id = f'{source["table"]}/{source["item_id"]}'
            if source.get('rotation'):
                source_id += f'@rotation-{source["rotation"]}'
            sources[source_id] = key
    return names, pixels, sources


def supplemental_records(root, snapshot, sheets):
    """Preserve existing IDs, separate same-name styles, and use 16 px assets."""
    catalog = json.loads((root / 'data/planner/catalog.json').read_text())
    items = catalog['items']
    names, pixels, sources = catalog_matches(catalog, root)
    previous = json.loads(
        (root / 'data/recognition/supplemental.json').read_text()
    )['items']
    records = {}
    grouped = defaultdict(list)
    for key, entry in snapshot['entries'].items():
        if entry.get('rotation'):
            continue
        if entry['table'] in [
            'Furniture',
            'BigCraftables',
            'Mannequins',
        ] or key in ['Objects/463', 'Objects/464', 'Objects/746']:
            if not exclusion(entry) and key != 'Furniture/FreeCactus':
                grouped[(entry['table'], normalized(entry['name_en']))].append(
                    (key, entry)
                )

    for (table, _), rows in grouped.items():
        all_named = list(
            dict.fromkeys(names.get(normalized(rows[0][1]['name_en']), []))
        )
        named = [
            key
            for key in all_named
            if (
                items[key]['category'] == 'furniture'
                if table == 'Furniture'
                else True
            )
        ]
        if table == 'Furniture' and rows[0][0] == 'Furniture/JunimoHut':
            named = (
                ['decorative-junimo-hut']
                if 'decorative-junimo-hut' in items
                else []
            )
        base_id = next(
            (
                value
                for value in sorted(
                    named, key=lambda value: (len(value), value)
                )
                if not items[value]['hidden_in_menu']
            ),
            None,
        )
        matched = {}
        for key, entry in rows:
            candidates = pixels.get(entry['sprite_signature'], [])
            # Exact sprite matches are restricted to the same game item name
            # when available; furniture/building names can coincide.
            same_name = [value for value in candidates if value in named]
            expected = sources.get(key) or EQUIVALENTS.get(key)
            if expected:
                matched[key] = expected
            elif same_name:
                matched[key] = sorted(
                    same_name, key=lambda value: (len(value), value)
                )[0]
            elif len(rows) == 1 and base_id:
                matched[key] = base_id
            elif table == 'BigCraftables' and candidates:
                matched[key] = sorted(
                    candidates, key=lambda value: (len(value), value)
                )[0]
        if base_id is None:
            base_id = next(iter(matched.values()), None)
        if base_id is None:
            first_key, first = rows[0]
            base_id = EQUIVALENTS.get(first_key) or (
                'mannequin-' + first['source_id']
                if table == 'Mannequins'
                else re.sub(
                    r'[^a-z0-9]+', '-', first['name_en'].lower()
                ).strip('-')
            )
        if base_id not in matched.values():
            matched[rows[0][0]] = base_id
        template = (
            base_id
            if base_id in items
            else next((value for value in named if value in items), None)
        )
        template = template or (
            'large-junimo-hut'
            if base_id == 'decorative-junimo-hut'
            else 'chicken-statue'
        )
        for index, (key, entry) in enumerate(rows, 1):
            item_id = (
                matched.get(key)
                or f'{base_id}-style-{entry["source_id"].lower()}'
            )
            item_template = item_id if item_id in items else template
            if item_id != base_id and len(rows) > 1:
                item_template = base_id
            existing = items.get(item_id) or items[item_template]
            footprint = existing['placement']['footprint']
            if (
                item_id in items
                and item_id in pixels.get(entry['sprite_signature'], [])
                and [footprint['width'], footprint['height']]
                == entry['footprint_tiles']
                and entry.get('rotations', 1) == 1
            ):
                if previous.get(item_id) and (
                    previous[item_id].get('source_table')
                    or table == 'Furniture'
                ):
                    records[item_id] = previous[item_id]
                    records[item_id]['source_page'] = wiki_page(entry)
                    records[item_id]['source_table'] = table
                    if table == 'Furniture':
                        records[item_id]['furniture_type'] = entry[
                            'furniture_type'
                        ]
                        records[item_id]['category'] = 'furniture'
                continue
            source = snapshot['sources'][entry['sheet']]
            with Image.open(BytesIO(sheets[entry['sheet']])) as sheet:
                sprite = sheet.convert('RGBA').crop(entry['source_rect_px'])
            path = root / f'data/planner/images/{item_id}-source.png'
            sprite.save(path)
            name_en = (
                existing['name_en'] if item_id in items else entry['name_en']
            )
            name_zh = existing['name_zh'] or name_en
            if table == 'Mannequins':
                name_en, name_zh = entry['name_en'], entry['name_zh']
            elif item_id == 'decorative-junimo-hut':
                name_en, name_zh = 'Decorative Junimo Hut', '装饰祝尼魔小屋'
            elif item_id == 'stone-owl-night-market':
                name_en, name_zh = (
                    'Stone Owl (Night Market)',
                    '石猫头鹰（夜市）',
                )
            elif item_id in ['hmtgf', 'pinky-lemon', 'foroguemon']:
                name_en = name_zh = entry['name_en']
            elif item_id != base_id and len(rows) > 1 and item_id not in items:
                name_en = f'{items.get(base_id, existing)["name_en"]} (Style {index})'
                name_zh = f'{items.get(base_id, existing)["name_zh"]}（样式 {index}）'
            group = existing.get('recognition_group')
            if not group:
                import sys

                sys.path.insert(0, str(root / 'scripts'))
                from recognition_categories import recognition_group

                group = recognition_group(existing)
            if table == 'Mannequins':
                group = 'furniture'
            elif table == 'Objects':
                group = (
                    'tools'
                    if entry['source_id'] in ['463', '464']
                    else 'decorations'
                )
                name_en = entry['name_en']
                name_zh = {'463': '鼓块', '464': '长笛块', '746': '南瓜灯'}[
                    entry['source_id']
                ]
            page = wiki_page(entry)
            record = {
                'id': item_id,
                'template_type': item_template,
                'planner_type': item_id,
                'name_zh': name_zh,
                'name_en': name_en,
                'recognition_group': group,
                'family_id': existing.get('family_id', 'other'),
                'source_page': page,
                'source_item_id': entry['source_id'],
                'source_table': table,
                'recognition_only': False,
                'footprint_tiles': entry['footprint_tiles'],
                'footprint_source': f'stardewplan_{table.lower()}_data',
                'sprites': {
                    'default': {
                        'path': path.relative_to(root).as_posix(),
                        'url': source['url'],
                        'source_sha256': source['sha256'],
                        'source_rect_px': entry['source_rect_px'],
                        'sha256': digest(path.read_bytes()),
                        'size_px': list(sprite.size),
                    },
                },
            }
            if table == 'Furniture':
                record['category'] = 'furniture'
                record['furniture_type'] = entry['furniture_type']
                for rotation in range(1, entry.get('rotations', 1)):
                    view = snapshot['entries'][f'{key}@rotation-{rotation}']
                    with Image.open(BytesIO(sheets[entry['sheet']])) as sheet:
                        pixels_image = sheet.convert('RGBA').crop(
                            view['source_rect_px']
                        )
                    if view['flip_horizontal']:
                        pixels_image = pixels_image.transpose(
                            Image.Transpose.FLIP_LEFT_RIGHT
                        )
                    path = (
                        root
                        / f'data/planner/images/{item_id}-rotation-{rotation}.png'
                    )
                    pixels_image.save(path)
                    record['sprites'][f'rotation-{rotation}'] = {
                        'path': path.relative_to(root).as_posix(),
                        'url': source['url'],
                        'source_sha256': source['sha256'],
                        'source_rect_px': view['source_rect_px'],
                        'sha256': digest(path.read_bytes()),
                        'size_px': list(pixels_image.size),
                        'footprint_tiles': view['footprint_tiles'],
                        'rotation': rotation,
                        'flip_horizontal': view['flip_horizontal'],
                    }
            elif table == 'Objects':
                record['category'] = 'craftable'
            if item_id != base_id and len(rows) > 1:
                record['parent_id'] = base_id
                record['appearance_label'] = str(index)
            records[item_id] = record
    return records


def building_placement(item, source):
    placement = {
        'human_door': [item['HumanDoor']['X'], item['HumanDoor']['Y']],
        'additional_placement_tiles': [
            {
                'tile_area': [
                    entry['TileArea'][key]
                    for key in ['X', 'Y', 'Width', 'Height']
                ],
                'only_needs_to_be_passable': entry.get(
                    'OnlyNeedsToBePassable', False
                ),
            }
            for entry in item.get('AdditionalPlacementTiles') or []
        ],
        'source_url': source['url'],
        'source_sha256': source['sha256'],
        'semantics': 'AdditionalPlacementTiles are checked when placing or moving the building; they are not a permanent occupied footprint.',
    }
    collision = [
        row.strip()
        for row in (item.get('CollisionMap') or '').splitlines()
        if row.strip()
    ]
    if collision:
        placement['collision_map'] = collision
        restricted = set()
        for entry in item.get('TileProperties') or []:
            if entry['Name'] == 'NoFurniture' and entry['Value'] == 't':
                area = entry['TileArea']
                restricted.update(
                    (x, y)
                    for y in range(area['Y'], area['Y'] + area['Height'])
                    for x in range(area['X'], area['X'] + area['Width'])
                )
        placement['porch_object_cells'] = [
            [x, y]
            for y, row in enumerate(collision)
            for x, value in enumerate(row)
            if value == 'O' and (x, y) not in restricted
        ]
    mailbox = [
        [entry['Tile']['X'], entry['Tile']['Y']]
        for entry in item.get('ActionTiles') or []
        if entry['Action'] == 'Mailbox'
    ]
    if mailbox:
        placement['bundled_mailbox'] = {
            'cells': mailbox,
            'recognition_action': 'ignore_as_separate_object',
            'note_zh': '农舍和联机小屋自带信箱；忽略独立识别、放置和计数。建筑图片中已有的信箱保留，勿当作出货箱、灯具或未知物体。',
        }
    return placement


def apply_source_metadata(catalog, snapshot, root=ROOT):
    for source_id, local_ids in {
        'Farmhouse': ['house', 'house-2', 'house-3'],
        'Cabin': [
            key
            for key in catalog['items']
            if re.fullmatch(
                r'(stone|plank|log|neighbor|rustic|beach|trailer)-cabin(?:-[23])?',
                key,
            )
        ],
        'Greenhouse': ['greenhouse', 'greenhouse-repaired'],
    }.items():
        placement = snapshot['entries'][f'Buildings/{source_id}'].get(
            'building_placement'
        )
        if placement:
            for item_id in local_ids:
                if item_id in catalog['items']:
                    catalog['items'][item_id]['building_placement'] = placement
    names, pixels, sources = catalog_matches(catalog, root)
    for key, entry in snapshot['entries'].items():
        if entry['table'] != 'Furniture' or exclusion(entry):
            continue
        candidates = pixels.get(entry.get('sprite_signature'), [])
        expected = sources.get(key) or EQUIVALENTS.get(key)
        if expected in catalog['items']:
            candidates = [expected]
        candidates = [
            value
            for value in candidates
            if catalog['items'][value]['category'] == 'furniture'
        ]
        named = set(names.get(normalized(entry['name_en']), []))
        candidates.sort(
            key=lambda value: (value not in named, len(value), value)
        )
        if not candidates:
            continue
        item = catalog['items'][candidates[0]]
        item['furniture_type'] = entry['furniture_type']
        item['source'] = {
            **item.get('source', {}),
            'table': 'Furniture',
            'item_id': entry['source_id'],
            'asset_version': snapshot['asset_version'],
            'data_url': snapshot['sources']['Furniture']['url'],
            'data_sha256': snapshot['sources']['Furniture']['sha256'],
            'rotation': entry.get('rotation', 0),
        }


def audit(root, snapshot):
    catalog = json.loads((root / 'data/planner/catalog.json').read_text())
    recognition = json.loads(
        (root / 'data/recognition/index.json').read_text()
    )
    names, pixels, sources = catalog_matches(catalog, root)
    supplemental = json.loads(
        (root / 'data/recognition/supplemental.json').read_text()
    )
    omitted_sources = {
        f'{item["source_table"]}/{item["source_item_id"]}': item
        for item in supplemental.get('reference_entities', {}).values()
        if item.get('source_table') and item.get('source_item_id')
    }
    records = {}
    for key, entry in snapshot['entries'].items():
        record = {'name_en': entry['name_en']}
        if reason := exclusion(entry):
            record.update(status='excluded', reason=reason)
            records[key] = record
            continue
        if item := omitted_sources.get(key):
            records[key] = {
                **record,
                'status': 'excluded'
                if item['id'] not in catalog['items']
                and recognition.get('items', {})
                .get(item['id'], {})
                .get('recognition_action')
                == 'omit'
                else 'omission_mismatch',
                'reason': 'user_requested_layout_omission',
                'recognition_id': item['id'],
            }
            continue
        if key == 'Furniture/FreeCactus':
            records[key] = {
                **record,
                'status': 'components_only',
                'catalog_id': 'free-cactus',
            }
            continue
        candidates = pixels.get(entry.get('sprite_signature'), [])
        expected_id = sources.get(key) or EQUIVALENTS.get(key)
        if entry['table'] in ['Crops', 'GiantCrops']:
            expected_id = (
                'giant-pumpkin'
                if entry['table'] == 'GiantCrops'
                else 'strawberry-seeds'
            )
        if expected_id in catalog['items']:
            candidates = [expected_id]
        if not candidates:
            candidates = list(
                dict.fromkeys(names.get(normalized(entry['name_en']), []))
            )
        if len(candidates) > 1:
            matching_names = set(names.get(normalized(entry['name_en']), []))
            candidates.sort(
                key=lambda value: (
                    value not in matching_names,
                    len(value),
                    value,
                )
            )
        if not candidates:
            records[key] = {**record, 'status': 'missing'}
            continue
        item_id = candidates[0]
        item = catalog['items'][item_id]
        exact = item_id in pixels.get(entry.get('sprite_signature'), [])
        footprint = item['placement']['footprint']
        size_matches = [footprint['width'], footprint['height']] == entry[
            'footprint_tiles'
        ]
        status = (
            'covered'
            if exact
            or not entry.get('sprite_signature')
            or entry['table'] == 'Objects'
            else 'appearance_mismatch'
        )
        if not size_matches:
            status = 'footprint_mismatch'
        if item_id not in recognition['lookup']:
            status = 'missing_recognition'
        records[key] = {
            **record,
            'status': status,
            'catalog_id': item_id,
            'match': 'exact_pixels'
            if exact
            else 'source_id'
            if expected_id
            else 'name',
            'source_footprint': entry['footprint_tiles'],
            'catalog_footprint': [footprint['width'], footprint['height']],
        }
    counts = dict(Counter(row['status'] for row in records.values()))
    unresolved = [
        key
        for key, row in records.items()
        if row['status'] not in ['covered', 'excluded', 'components_only']
    ]
    return {
        'schema_version': 1,
        'asset_version': snapshot['asset_version'],
        'source_index_sha256': digest((root / SOURCE_PATH).read_bytes()),
        'catalog_sha256': digest(
            (root / 'data/planner/catalog.json').read_bytes()
        ),
        'recognition_index_sha256': digest(
            (root / 'data/recognition/index.json').read_bytes()
        ),
        'source_entry_counts': snapshot['source_entry_counts'],
        'checked_entry_count': len(records),
        'counts': counts,
        'unresolved_ids': unresolved,
        'records': records,
        'limitations': snapshot['limitations'],
    }


def validate_catalog(catalog, output):
    ids = set(catalog['items'])
    filenames = set()
    missing_names = []
    ambiguous_highlights = []
    edge_artifacts = {}
    for item_id, item in catalog['items'].items():
        image_path = output / item['image']['path']
        if image_path.as_posix().casefold() in filenames:
            raise ValueError(f'Duplicate image path: {image_path}')
        filenames.add(image_path.as_posix().casefold())
        image = Image.open(image_path)
        if image.size != (
            item['image']['width_px'],
            item['image']['height_px'],
        ):
            raise ValueError(f'Image dimensions mismatch: {item_id}')
        if not item['name_zh']:
            missing_names.append(item_id)
        for variant in item['variant_ids']:
            if variant not in ids:
                raise ValueError(f'Unknown variant: {variant}')
        if item['effect']:
            if item['effect']['ambiguous_alpha_tiles']:
                ambiguous_highlights.append(item_id)
            if item['effect']['nonuniform_alpha_tiles']:
                edge_artifacts[item_id] = len(
                    item['effect']['nonuniform_alpha_tiles']
                )
    if not set(catalog['aliases'].values()) <= ids:
        raise ValueError('Unknown alias target')
    return {
        'passed': not missing_names and not ambiguous_highlights,
        'item_count': len(ids),
        'image_count': len(filenames),
        'missing_chinese_names': missing_names,
        'ambiguous_highlight_tiles': ambiguous_highlights,
        'highlight_edges_verified_by_majority_alpha': edge_artifacts,
        'alias_targets_valid': True,
        'image_dimensions_valid': True,
        'variant_targets_valid': True,
    }


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--root', type=Path, default=ROOT)
    parser.add_argument('--refresh', action='store_true')
    parser.add_argument('--version', default=VERSION)
    args = parser.parse_args()
    root = args.root.resolve()
    if args.refresh:
        snapshot, _ = source_index(args.version)
        write_json(root / SOURCE_PATH, snapshot)
    else:
        snapshot = json.loads((root / SOURCE_PATH).read_text())
    report = audit(root, snapshot)
    write_json(root / REPORT_PATH, report)
    print(
        json.dumps(
            {
                key: report[key]
                for key in ['checked_entry_count', 'counts', 'unresolved_ids']
            },
            ensure_ascii=False,
        )
    )
    raise SystemExit(bool(report['unresolved_ids']))


if __name__ == '__main__':
    main()
