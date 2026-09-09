# /// script
# requires-python = ">=3.12"
# dependencies = ["pillow==12.3.0", "tree-sitter==0.25.2", "tree-sitter-javascript==0.25.0"]
# ///

import argparse
import hashlib
import json
import re
from collections import Counter
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime
from html.parser import HTMLParser
from io import BytesIO
from pathlib import Path
from urllib.parse import urljoin
from urllib.request import Request, urlopen

import tree_sitter_javascript
from audit_catalog import validate_catalog
from PIL import Image
from sync_variants import apply_supplemental
from tree_sitter import Language, Parser

REPO_ROOT = Path(__file__).resolve().parents[2]
ROOT = REPO_ROOT / 'skills/stardew-planner-skill'
BASE_URL = 'https://stardew.info/'
TILE_SIZE = 16
SEASONS_ZH = {
    'spring': '春季',
    'summer': '夏季',
    'fall': '秋季',
    'winter': '冬季',
}
CONNECTION_SUFFIXES = {
    'c',
    'e',
    'hc',
    'he',
    'hw',
    'n',
    'ne',
    'nw',
    's',
    'se',
    'sw',
    'vc',
    'vn',
    'vs',
    'w',
}
FIELDS = {
    'x',
    'y',
    'height',
    'width',
    'name',
    'id',
    'group',
    'footprint',
    'restrictionLayer',
    'subGroup',
    'hideInMenu',
    'description',
    'season',
    'seasonSprite',
    'highlight',
    'importerCorrection',
}


def write_json(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(value, ensure_ascii=False, indent=2) + '\n',
        encoding='utf-8',
    )


def sha256(data):
    return hashlib.sha256(data).hexdigest()


class ScriptLinks(HTMLParser):
    def __init__(self):
        super().__init__()
        self.sources = []

    def handle_starttag(self, tag, attrs):
        values = dict(attrs)
        if tag == 'script' and values.get('src'):
            self.sources.append(values['src'])


class Downloads:
    def __init__(self, cache, offline=False):
        self.cache = cache
        self.offline = offline
        self.cache.mkdir(parents=True, exist_ok=True)

    def get(self, name, url, refresh=False):
        path = self.cache / name
        manifest = path.with_suffix(path.suffix + '.source.json')
        if self.offline or (
            path.exists() and manifest.exists() and not refresh
        ):
            if not path.exists() or not manifest.exists():
                raise ValueError(f'Missing cached source: {name}')
            info = json.loads(manifest.read_text())
            data = path.read_bytes()
            if info['sha256'] != sha256(data):
                raise ValueError(f'Cached source mismatch: {name}')
            if info['url'] == url:
                return data, info
            if self.offline:
                raise ValueError(f'Cached source URL mismatch: {name}')
        request = Request(
            url, headers={'User-Agent': 'stardew-planner-catalog/0.1'}
        )
        with urlopen(request, timeout=40) as response:
            data = response.read()
            content_type = response.headers.get('Content-Type', '')
        if not data:
            raise ValueError(f'Empty response: {url}')
        info = {
            'url': url,
            'retrieved_at': datetime.now(UTC).isoformat(),
            'sha256': sha256(data),
            'bytes': len(data),
            'content_type': content_type,
        }
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(data)
        write_json(manifest, info)
        return data, info


def parse_literal(node, source):
    """Decode data literals from the JS syntax tree without executing JS."""
    text = source[node.start_byte : node.end_byte].decode()
    if node.type == 'object':
        result = {}
        for pair in node.named_children:
            if pair.type != 'pair':
                raise ValueError(f'Non-data object member: {pair.type}')
            key = parse_literal(pair.child_by_field_name('key'), source)
            if key in result:
                raise ValueError(f'Duplicate data key: {key}')
            result[key] = parse_literal(
                pair.child_by_field_name('value'), source
            )
        return result
    if node.type == 'array':
        return [parse_literal(child, source) for child in node.named_children]
    if node.type == 'string':
        return json.loads(text)
    if node.type == 'property_identifier':
        return text
    if node.type == 'number':
        if text.startswith(('0x', '0X')):
            return int(text, 16)
        if any(char in text.lower() for char in ['e', '.']):
            value = float(text)
            return int(value) if value.is_integer() else value
        return int(text)
    if node.type in ['true', 'false', 'null']:
        return json.loads(text)
    if node.type == 'unary_expression':
        if text in ['!0', '!1']:
            return text == '!0'
        if text.startswith('-'):
            return -parse_literal(node.named_children[-1], source)
    raise ValueError(f'Unsupported data literal: {node.type} {text[:80]}')


def extract_objects(bundle):
    parser = Parser(Language(tree_sitter_javascript.language()))
    root = parser.parse(bundle).root_node
    if root.has_error:
        raise ValueError('Planner JavaScript could not be parsed')
    matches = []
    stack = [root]
    while stack:
        node = stack.pop()
        if node.type == 'object':
            pairs = [
                child for child in node.named_children if child.type == 'pair'
            ]
            if len(pairs) > 100:
                keys = {
                    bundle[
                        p.child_by_field_name(
                            'key'
                        ).start_byte : p.child_by_field_name('key').end_byte
                    ]
                    .decode()
                    .strip('"')
                    for p in pairs
                }
                if {
                    'coop',
                    'parsnip-seeds',
                    'quality-sprinkler',
                    'chest',
                } <= keys:
                    matches.append(node)
        stack.extend(node.named_children)
    if len(matches) != 1:
        raise ValueError(f'Expected one sprite catalog, found {len(matches)}')
    table = parse_literal(matches[0], bundle)
    for key, item in table.items():
        if (
            not isinstance(item, dict)
            or not {'id', 'name', 'group', 'x', 'y', 'width', 'height'}
            <= item.keys()
        ):
            raise ValueError(f'Invalid source item: {key}')
    return table


def canonicalize(table):
    items = {}
    aliases = {}
    conflicts = []
    for key, value in table.items():
        item_id = value['id']
        preferred = table.get(item_id, value)
        if item_id in items and items[item_id] != preferred:
            raise ValueError(f'Conflicting canonical item: {item_id}')
        items[item_id] = preferred
        if key != item_id:
            aliases[key] = item_id
            differing = [
                field
                for field in value
                if field != 'hideInMenu'
                and value[field] != preferred.get(field)
            ]
            if differing:
                conflicts.append(
                    {
                        'alias': key,
                        'canonical_id': item_id,
                        'fields': differing,
                    }
                )
    return items, aliases, conflicts


def relationships(items, overrides=None):
    variants = {}
    for key, item in items.items():
        if item.get('seasonSprite'):
            season = item['seasonSprite']
            variants[key] = {
                'kind': 'season',
                'base_id': season['originalSpriteId'],
                'value': season['season'],
            }
        elif item.get('hideInMenu') and '-' in key:
            parent, suffix = key.rsplit('-', 1)
            if parent in items and suffix in CONNECTION_SUFFIXES:
                variants[key] = {
                    'kind': 'connection',
                    'base_id': parent,
                    'value': suffix,
                }
            elif parent in items and suffix.isdigit():
                variants[key] = {
                    'kind': 'visual',
                    'base_id': parent,
                    'value': suffix,
                }
    for item_id, override in (overrides or {}).items():
        if item_id not in items or override['base_id'] not in items:
            raise ValueError(f'Unknown variant override: {item_id}')
        variants[item_id] = {
            key: override[key] for key in ['kind', 'base_id', 'value']
        }
    return variants


def resolve_names(items, localized, overrides, variants):
    resolved = {}
    active = set()

    def resolve(item_id):
        if item_id in resolved:
            return resolved[item_id]
        if item_id in active:
            raise ValueError(f'Cyclic variant names: {item_id}')
        active.add(item_id)
        if item_id in overrides:
            result = {**overrides[item_id], 'source': 'local_override'}
        elif item_id in localized:
            result = {
                'value': localized[item_id],
                'source': 'planner_zh_CN',
                'status': 'source_translation',
            }
        elif item_id in variants:
            relation = variants[item_id]
            base = resolve(relation['base_id'])
            if base['value'] is None:
                result = {'value': None, 'source': None, 'status': 'missing'}
            else:
                suffix = {
                    'season': SEASONS_ZH.get(
                        relation['value'], relation['value']
                    ),
                    'connection': f'连接贴图 {relation["value"]}',
                    'visual': f'贴图 {relation["value"]}',
                }[relation['kind']]
                result = {
                    'value': f'{base["value"]}（{suffix}）',
                    'source': 'derived_from_base',
                    'base_id': relation['base_id'],
                    'status': 'derived_label',
                }
        else:
            result = {'value': None, 'source': None, 'status': 'missing'}
        active.remove(item_id)
        resolved[item_id] = result
        return result

    for item_id in items:
        resolve(item_id)
    return resolved


def image_filename(item_id):
    slug = re.sub(r'[^a-zA-Z0-9_.-]', '-', item_id).strip('.')
    if slug != item_id:
        slug += '-' + sha256(item_id.encode())[:8]
    return slug + '.png'


def footprint(item):
    value = item.get('footprint', {})
    result = {
        'width': value.get('width', 1),
        'height': value.get('height', 1),
        'anchor_offset_column': value.get('offsetColumn', 0),
        'anchor_offset_row': value.get('offsetRow', 0),
        'source': 'planner_explicit'
        if value
        else 'planner_default_single_tile',
        'coordinate_unit': 'tile',
    }
    if result['width'] < 1 or result['height'] < 1:
        raise ValueError(f'Invalid footprint: {item["id"]}')
    result['top_left_from_planner_anchor'] = [
        result['anchor_offset_column'],
        result['anchor_offset_row'] - result['height'] + 1,
    ]
    return result


def decode_effect(item, data, source, output):
    image = Image.open(BytesIO(data)).convert('RGBA')
    if image.width % TILE_SIZE or image.height % TILE_SIZE:
        raise ValueError(f'Non-grid highlight: {item["id"]}')
    width, height = image.width // TILE_SIZE, image.height // TILE_SIZE
    if width % 2 != 1 or height % 2 != 1:
        raise ValueError(f'Highlight is not centered on a tile: {item["id"]}')
    highlight = item['highlight']
    placement = footprint(item)
    shift_x, shift_y = placement['top_left_from_planner_anchor']
    cells = []
    nonuniform = []
    ambiguous = []
    alpha = image.getchannel('A')
    for y in range(height):
        for x in range(width):
            tile = alpha.crop(
                (
                    x * TILE_SIZE,
                    y * TILE_SIZE,
                    (x + 1) * TILE_SIZE,
                    (y + 1) * TILE_SIZE,
                )
            )
            low, high = tile.getextrema()
            center_is_covered = bool(
                alpha.getpixel((x * TILE_SIZE + 8, y * TILE_SIZE + 8))
            )
            majority_is_covered = (
                sum(pixel > 127 for pixel in tile.get_flattened_data())
                >= TILE_SIZE * TILE_SIZE / 2
            )
            if center_is_covered != majority_is_covered:
                ambiguous.append([x, y])
            if low != high:
                nonuniform.append([x, y])
            if center_is_covered:
                cells.append(
                    [
                        x
                        - width // 2
                        + highlight.get('offsetColumn', 0)
                        - shift_x,
                        y
                        - height // 2
                        + highlight.get('offsetRow', 0)
                        - shift_y,
                    ]
                )
    occupied = {
        (x, y)
        for x in range(placement['width'])
        for y in range(placement['height'])
    }
    target_cells = [cell for cell in cells if tuple(cell) not in occupied]
    path = output / 'highlights' / image_filename(item['id'])
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(data)
    return {
        'group': highlight['group'],
        'status': 'planner_overlay_decoded',
        'source': source,
        'image_path': path.relative_to(output).as_posix(),
        'image_width_px': image.width,
        'image_height_px': image.height,
        'width_tiles': width,
        'height_tiles': height,
        'cell_origin': 'item_footprint_top_left',
        'overlay_cells': cells,
        'overlay_cell_count': len(cells),
        'target_cells_excluding_footprint': target_cells,
        'target_cell_count': len(target_cells),
        'nonuniform_alpha_tiles': nonuniform,
        'ambiguous_alpha_tiles': ambiguous,
        'decoding_method': 'Sample the alpha value at each tile center and verify against its majority alpha. Edge-only pixels do not add cells.',
        'gameplay_verified': False,
        'limitations': [
            'Website visualization geometry only.',
            'Target cells subtract the item footprint; crops, terrain, obstructions and game-specific eligibility are not evaluated.',
        ],
    }


def build_catalog(output, cache, offline=False, refresh=False):
    output.mkdir(parents=True, exist_ok=True)
    downloads = Downloads(cache, offline)
    html, home_source = downloads.get(
        'home.html', BASE_URL, refresh=not offline
    )
    links = ScriptLinks()
    links.feed(html.decode())
    bundle_urls = [
        urljoin(BASE_URL, url)
        for url in links.sources
        if '/planner/lib/planner.js' in url
    ]
    if len(bundle_urls) != 1:
        raise ValueError('Could not identify the current planner bundle')
    bundle, bundle_source = downloads.get(
        'planner.js', bundle_urls[0], refresh=refresh or not offline
    )
    version_match = re.search(
        rb'\\"name\\":\\"planner_v3\\",\\"version\\":\\"([^\\"]+)', bundle
    )
    if not version_match:
        version_match = re.search(
            rb'"name":"planner_v3","version":"([^"]+)', bundle
        )
    if not version_match:
        raise ValueError('Could not identify planner version')
    version = version_match.group(1).decode()
    table = extract_objects(bundle)
    items, aliases, conflicts = canonicalize(table)
    print(
        f'Planner {version}: {len(table)} source entries, {len(items)} unique IDs, {len(aliases)} aliases',
        flush=True,
    )
    names_url = f'{BASE_URL}planner/assets/i18n/names.zh-CN.json?v={version}'
    atlas_url = f'{BASE_URL}planner/assets/tilesheets/sprites.png?v={version}'
    with ThreadPoolExecutor(max_workers=4) as pool:
        names_future = pool.submit(
            downloads.get, 'names.zh-CN.json', names_url, refresh
        )
        atlas_future = pool.submit(
            downloads.get, 'sprites.png', atlas_url, refresh
        )
        localized_data, localized_source = names_future.result()
        atlas_data, atlas_source = atlas_future.result()
    localized = json.loads(localized_data)
    atlas = Image.open(BytesIO(atlas_data)).convert('RGBA')
    (output / 'atlas.png').write_bytes(atlas_data)
    write_json(output / 'source-items.json', table)
    write_json(output / 'names.zh-CN.json', localized)
    overrides_path = ROOT / 'data' / 'catalog-overrides.json'
    corrections = json.loads(overrides_path.read_text())
    variants = relationships(items, corrections.get('variants'))
    overrides = corrections['names_zh']
    names = resolve_names(items, localized, overrides, variants)
    highlighted_ids = [
        key for key, item in items.items() if item.get('highlight')
    ]

    def fetch_highlight(item_id):
        url = f'{BASE_URL}planner/assets/highlights/{item_id}.png?v={version}'
        return item_id, downloads.get(
            f'highlights/{item_id}.png', url, refresh
        )

    effects = {}
    with ThreadPoolExecutor(max_workers=4) as pool:
        for item_id, (data, source) in pool.map(
            fetch_highlight, highlighted_ids
        ):
            effects[item_id] = decode_effect(
                items[item_id], data, source, output
            )
    images_dir = output / 'images'
    images_dir.mkdir(parents=True, exist_ok=True)
    records = {}
    for item_id, item in sorted(items.items()):
        x, y, width, height = (
            item[key] for key in ['x', 'y', 'width', 'height']
        )
        if (
            min(x, y) < 0
            or min(width, height) < 1
            or x + width > atlas.width
            or y + height > atlas.height
        ):
            raise ValueError(f'Sprite frame outside atlas: {item_id}')
        image = atlas.crop((x, y, x + width, y + height))
        path = images_dir / image_filename(item_id)
        image.save(path)
        records[item_id] = {
            'id': item_id,
            'aliases': sorted(
                key for key, value in aliases.items() if value == item_id
            ),
            'name_en': item['name'],
            'name_zh': names[item_id]['value'],
            'name_zh_metadata': names[item_id],
            'category': item['group'],
            'subcategory': item.get('subGroup'),
            'hidden_in_menu': item.get('hideInMenu', False),
            'is_planner_tool': item['group'] == 'planner',
            'seasons': item.get('season', '').split(),
            'description_en': item.get('description') or None,
            'image': {
                'path': path.relative_to(output).as_posix(),
                'width_px': width,
                'height_px': height,
                'sha256': sha256(path.read_bytes()),
                'atlas_path': 'atlas.png',
                'atlas_rect_px': {
                    'x': x,
                    'y': y,
                    'width': width,
                    'height': height,
                },
            },
            'placement': {
                'footprint': footprint(item),
                'restriction_layer': item.get('restrictionLayer'),
                'restriction_status': 'explicit'
                if item.get('restrictionLayer')
                else 'not_provided',
                'importer_correction': item.get('importerCorrection'),
                'gameplay_footprint_verified': False,
            },
            'variant': variants.get(item_id),
            'variant_ids': sorted(
                key
                for key, value in variants.items()
                if value['base_id'] == item_id
            ),
            'effect_status': 'provided'
            if item_id in effects
            else 'not_provided_by_planner',
            'effect': effects.get(item_id),
            'additional_source_fields': {
                key: value for key, value in item.items() if key not in FIELDS
            },
        }
    catalog = {
        'schema_version': 1,
        'scope': 'All sprite IDs recognized by the current stardew.info planner, including hidden variants and planner tools. This is not the complete Stardew Valley inventory.',
        'planner_version': version,
        'tile_size_px': TILE_SIZE,
        'sources': {
            'homepage': home_source,
            'planner_bundle': bundle_source,
            'localized_names': localized_source,
            'atlas': atlas_source,
            'local_name_overrides': {
                'path': '../catalog-overrides.json',
                'sha256': sha256(overrides_path.read_bytes()),
            },
        },
        'coordinates': {
            'plan_unit': 'tile',
            'planner_export_unit': 'pixel',
            'planner_sprite_anchor': 'bottom_left',
            'footprint_origin': 'top_left',
            'top_left_column': 'planner_anchor_column + anchor_offset_column',
            'top_left_row': 'planner_anchor_row + anchor_offset_row - height + 1',
            'sprite_top_left_px': '[planner_anchor_column * 16, (planner_anchor_row + 1) * 16 - image_height_px]',
        },
        'stats': {
            'source_entries': len(table),
            'unique_items': len(records),
            'visible_entries': sum(
                not item['hidden_in_menu'] for item in records.values()
            ),
            'visible_game_items': sum(
                not item['hidden_in_menu'] and not item['is_planner_tool']
                for item in records.values()
            ),
            'hidden_entries': sum(
                item['hidden_in_menu'] for item in records.values()
            ),
            'planner_tools': sum(
                item['is_planner_tool'] for item in records.values()
            ),
            'aliases': len(aliases),
            'effect_masks': len(effects),
            'by_category': dict(
                Counter(item['category'] for item in records.values())
            ),
            'by_chinese_name_source': dict(
                Counter(
                    item['name_zh_metadata']['source']
                    for item in records.values()
                )
            ),
        },
        'limitations': [
            'Placement data reproduces the planner; missing footprint fields use its 1 x 1 default.',
            'Image dimensions, occupied tiles and gameplay placement constraints are separate concepts.',
            'No effect record means the planner provides no overlay; it does not prove the item has no gameplay effect.',
            'Chinese labels come from the website, derived variant labels or explicitly marked local translations; official game localization has not been independently verified.',
            'Asset attribution: Stardew Valley / ConcernedApe; planner data / Henrik Peinar.',
        ],
        'alias_field_differences': conflicts,
        'aliases': aliases,
        'items': records,
    }
    supplemental_path = output.parent / 'recognition/supplemental.json'
    if supplemental_path.is_file():
        supplemental = json.loads(
            supplemental_path.read_text(encoding='utf-8')
        )
        if supplemental.get('schema_version', 1) >= 2:
            apply_supplemental(catalog, supplemental, output.parent.parent)
    validation = validate_catalog(catalog, output)
    write_json(output / 'catalog.json', catalog)
    write_json(output / 'validation.json', validation)
    print(json.dumps(catalog['stats'], ensure_ascii=False, indent=2))
    print(json.dumps(validation, ensure_ascii=False, indent=2))
    if not validation['passed']:
        raise SystemExit(1)


def main():
    parser = argparse.ArgumentParser(
        description='Download and normalize the public Stardew Planner item catalog.'
    )
    parser.add_argument(
        '--output', type=Path, default=ROOT / 'data' / 'planner'
    )
    parser.add_argument(
        '--cache', type=Path, default=REPO_ROOT / '.cache' / 'catalog-sync'
    )
    parser.add_argument('--offline', action='store_true')
    parser.add_argument('--refresh', action='store_true')
    args = parser.parse_args()
    if args.offline and args.refresh:
        parser.error('--offline and --refresh cannot be combined')
    build_catalog(args.output, args.cache, args.offline, args.refresh)


if __name__ == '__main__':
    main()
