# /// script
# requires-python = ">=3.12"
# dependencies = ["pillow==12.3.0"]
# ///

# ruff: noqa: E402

import argparse
import hashlib
import json
import math
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1] / 'skills/stardew-planner-skill'
sys.path.insert(0, str(ROOT / 'scripts'))

from layout_assets import (
    appearance_note,
    fit_sprite,
    seasonal_id,
    sprite,
)
from layout_assets import appearances as asset_appearances
from layout_core import Dataset, LayoutError, write_json_atomic
from layout_placement import flooring_overlap, placement_areas, placement_label
from layout_style import chinese_font_path, load_font
from PIL import Image, ImageDraw
from recognition_categories import (
    atlas_families,
    ignored_vegetation,
    load_groups,
    load_supplemental,
    recognition_subject,
)

SHEET_LAYOUTS = {
    'compact': {
        'card_width': 216,
        'card_height': 140,
        'columns': 8,
        'rows': 12,
    },
    'gallery': {
        'card_width': 320,
        'card_height': 228,
        'columns': 5,
        'rows': 8,
    },
}
GALLERY_GROUPS = {
    'buildings',
    'flooring_fences',
    'trees',
    'ornamental_plants',
    'ignored_entities',
}
REFERENCE_ICON_SIZE = 112
MARGIN, GAP, HEADER, FOOTER = 12, 6, 64, 32
SEASONS = [
    ('spring', '春'),
    ('summer', '夏'),
    ('fall', '秋'),
    ('winter', '冬'),
]


def paginate_families(families, capacity=96):
    if type(capacity) is not int or capacity < 1:
        raise LayoutError('Recognition page capacity must be positive')
    pages, page, used = [], [], 0
    for family in families:
        first = 0
        while first < len(family['items']):
            count = min(capacity - used, len(family['items']) - first)
            page.append(
                {
                    **family,
                    'items': family['items'][first : first + count],
                    'continued': first > 0,
                }
            )
            first += count
            used += count
            if used == capacity:
                pages.append(page)
                page, used = [], 0
    if page:
        pages.append(page)
    return pages


def fitted_text(draw, box, text, font_path, size, fill, max_lines=3):
    left, top, right, bottom = box
    for candidate_size in range(size, 9, -1):
        font = load_font(candidate_size, font_path)
        lines = ['']
        for char in text:
            if draw.textlength(lines[-1] + char, font=font) > right - left:
                lines.append('')
            lines[-1] += char
        line_height = candidate_size + 2
        if (
            len(lines) <= max_lines
            and len(lines) * line_height <= bottom - top
        ):
            for index, line in enumerate(lines):
                draw.text(
                    (left, top + index * line_height),
                    line,
                    anchor='lt',
                    font=font,
                    fill=fill,
                )
            return {
                'bounds_px': list(box),
                'font_size': candidate_size,
                'lines': lines,
            }
    raise LayoutError(f'Recognition label does not fit: {text}')


def card_sprites(dataset, item, group, font_path):
    entries = []
    simplification = dataset.appearance_simplifications.get(item['id'])
    if simplification:
        kind = simplification['default_sprite_id']
        label = simplification[
            'card_label_zh' if font_path else 'card_label_en'
        ]
        entries.append((sprite(dataset, kind), label, {'id': kind}))
    elif item.get('recognition_only'):
        for key, asset in item['sprites'].items():
            with Image.open(dataset.root / asset['path']) as source:
                pixels = source.convert('RGBA')
            label = (
                dict(SEASONS).get(key, '')
                if font_path
                else ''
                if key == 'default'
                else key[:3]
            )
            entries.append(
                (
                    pixels,
                    label,
                    {
                        'id': item['id'],
                        'season': key,
                        'image': asset['path'],
                        'source_page': item['source_page'],
                    },
                )
            )
    elif item['id'] == 'greenhouse-repaired':
        for kind, label in [
            ('greenhouse', '修复前'),
            ('greenhouse-repaired', '已修复'),
        ]:
            entries.append(
                (
                    sprite(dataset, kind),
                    label if font_path else kind,
                    {'id': kind},
                )
            )
    elif any(
        seasonal_id(dataset, item['id'], season) != item['id']
        for season, _ in SEASONS
    ):
        for season, label in SEASONS:
            variant = seasonal_id(dataset, item['id'], season)
            label = label if font_path else season[:3]
            if appearance_note(dataset, variant):
                label += ' ?'
            entries.append(
                (
                    sprite(dataset, variant),
                    label,
                    {'id': variant, 'season': season},
                )
            )
    elif group == 'flooring_fences' or item['variant_ids']:
        for entry in asset_appearances(dataset, item['id']):
            suffix = entry['id'][len(item['id']) :].lstrip('-') or 'base'
            if group == 'buildings' and font_path:
                suffix = (
                    '初始' if suffix == 'base' else f'升级 {int(suffix) - 1}'
                )
            elif group != 'flooring_fences':
                suffix = str(len(entries) + 1)
            entries.append(
                (sprite(dataset, entry['id']), suffix, {'id': entry['id']})
            )
    else:
        entries.append((sprite(dataset, item['id']), '', {'id': item['id']}))
    return entries


def fit_reference_sprite(canvas, pixels, box, composite=False):
    bounds = pixels.getchannel('A').getbbox()
    if not bounds:
        raise LayoutError('Empty recognition reference')
    pixels = pixels.crop(bounds)
    left, top, right, bottom = box
    target = min(REFERENCE_ICON_SIZE, right - left, bottom - top)
    scale = (
        min((right - left) / pixels.width, (bottom - top) / pixels.height, 1)
        if composite
        else target / max(pixels.size)
    )
    size = tuple(max(1, round(side * scale)) for side in pixels.size)
    pixels = pixels.resize(size, Image.Resampling.NEAREST)
    x, y = (
        round((left + right - size[0]) / 2),
        round((top + bottom - size[1]) / 2),
    )
    canvas.paste(pixels, (x, y), pixels)
    return [x, y, x + size[0], y + size[1]]


def draw_card(
    canvas, dataset, item, group, box, number, font_path, family, layout
):
    left, top, right, bottom = box
    draw = ImageDraw.Draw(canvas)
    draw.rectangle(
        (left, top, right - 1, bottom - 1), fill='#ffffff', outline='#d9e0d8'
    )
    draw.text(
        (left + 6, top + 6),
        f'{number:03d}',
        anchor='lt',
        font=load_font(11),
        fill='#71816d',
    )
    family_label = (
        family['name_zh'] if font_path else family['id'].replace('_', ' ')
    )
    text_regions = [
        fitted_text(
            draw,
            (left + 34, top + 5, right - 6, top + 21),
            family_label,
            font_path,
            11,
            '#71816d',
            max_lines=1,
        )
    ]
    entries = card_sprites(dataset, item, group, font_path)
    guide = dataset.recognition_guides.get(item['id'], {})
    if guide.get('similar_item_ids'):
        labeled = []
        for pixels, _label, record in entries:
            subject = recognition_subject(
                dataset.items, dataset.reference_entities, record['id']
            )
            name = subject['name_zh' if font_path else 'name_en']
            if guide.get('show_placement_actions'):
                action = (
                    ('保留' if subject['placement_supported'] else '忽略')
                    if font_path
                    else ('Keep' if subject['placement_supported'] else 'Omit')
                )
                name = (
                    f'{action}：{name}' if font_path else f'{action}: {name}'
                )
                record = {
                    **record,
                    'recognition_action': subject['recognition_action'],
                    'placement_supported': subject['placement_supported'],
                }
            labeled.append((pixels, name, record))
        entries = labeled
        for similar_id in guide.get('similar_item_ids', []):
            similar = recognition_subject(
                dataset.items, dataset.reference_entities, similar_id
            )
            with Image.open(dataset.root / similar['image']) as source:
                pixels = source.convert('RGBA')
            name = similar['name_zh' if font_path else 'name_en']
            if guide.get('show_placement_actions'):
                prefix = (
                    ('保留' if similar['placement_supported'] else '忽略')
                    if font_path
                    else ('Keep' if similar['placement_supported'] else 'Omit')
                )
            else:
                prefix = '相似' if font_path else 'Similar'
            entries.append(
                (
                    pixels,
                    f'{prefix}：{name}' if font_path else f'{prefix}: {name}',
                    {
                        **similar,
                        'comparison_only': True,
                    },
                )
            )
    for reference in guide.get('reference_images', []):
        with Image.open(dataset.root / reference['image']) as source:
            pixels = source.convert('RGBA')
        if replaced := reference.get('replaces_sprite_id'):
            entries = [
                entry for entry in entries if entry[2]['id'] != replaced
            ]
        entries.append(
            (
                pixels,
                reference['label_zh' if font_path else 'label_en'],
                {**reference, 'reference_only': True},
            )
        )
    if layout == 'compact' and len(entries) > 1:
        image_box = (left + 6, top + 26, right - 6, top + 83)
        name_box = (left + 6, top + 85, right - 6, top + 103)
        id_box = (left + 6, top + 104, right - 6, top + 120)
    elif layout == 'compact':
        image_box = (left + 6, top + 28, left + 70, top + 115)
        name_box = (left + 77, top + 27, right - 6, top + 69)
        id_box = (left + 77, top + 74, right - 6, top + 117)
    else:
        image_box = (left + 8, top + 27, right - 8, top + 165)
        name_box = (left + 8, top + 171, right - 8, top + 191)
        id_box = (left + 8, top + 194, right - 8, top + 210)
    card_note = guide.get('card_note_zh' if font_path else 'card_note_en')
    if card_note and layout == 'gallery':
        image_box = (*image_box[:3], top + 141)
        text_regions.append(
            fitted_text(
                draw,
                (left + 8, top + 145, right - 8, top + 169),
                card_note,
                font_path,
                10,
                '#8a542c',
                max_lines=2,
            )
        )
    columns = (
        6
        if group == 'flooring_fences'
        else min(8 if layout == 'gallery' else 4, len(entries))
    )
    rows = math.ceil(len(entries) / columns)
    cell_width = (image_box[2] - image_box[0]) // columns
    cell_height = (image_box[3] - image_box[1]) // rows
    rendered = []
    for index, (pixels, label, record) in enumerate(entries):
        if layout == 'compact' and len(entries) > 1 and not guide:
            label = ''
        if guide and record['id'] == 'mushroom-log-ready':
            label = '长出蘑菇' if font_path else 'Ready'
        label_height = 26 if guide and not font_path else 14
        x = image_box[0] + index % columns * cell_width
        y = image_box[1] + index // columns * cell_height
        icon_box = (
            x,
            y,
            x + cell_width - 2,
            y + cell_height - (label_height if label else 0),
        )
        if item.get('recognition_action') == 'omit' or guide:
            visible_box = fit_reference_sprite(
                canvas,
                pixels,
                icon_box,
                composite=item.get('reference_composite', False),
            )
            record = {**record, 'visible_bounds_px': visible_box}
        else:
            fit_sprite(
                canvas,
                pixels,
                icon_box,
                max_scale=3 if layout == 'compact' else 4,
                preserve_canvas=True,
            )
        if label:
            label_region = fitted_text(
                draw,
                (
                    x,
                    y + cell_height - label_height + 1,
                    x + cell_width - 2,
                    y + cell_height,
                ),
                label,
                font_path,
                10,
                '#71816d',
                max_lines=2 if guide and not font_path else 1,
            )
            if guide:
                text_regions.append(label_region)
        rendered.append({**record, 'sprite_bounds_px': list(icon_box)})
    name = (
        (
            '温室（已修复）'
            if item['id'] == 'greenhouse-repaired'
            else item['name_zh'] or item['name_en']
        )
        if font_path
        else item['name_en']
    )
    text_regions.append(
        fitted_text(draw, name_box, name, font_path, 14, '#263d30')
    )
    if id_box:
        text_regions.append(
            fitted_text(draw, id_box, item['id'], font_path, 11, '#243e4c')
        )
    if item.get('recognition_action') == 'omit':
        size = '仅识别 · 忽略放置' if font_path else 'Identify and omit'
    else:
        size = placement_label(item) + (' 格' if font_path else ' tiles')
        dimensions = list(
            dict.fromkeys(
                dataset.dimensions(entry['id'])
                for entry in asset_appearances(dataset, item['id'])
            )
        )
        if len(dimensions) > 1:
            size = ' / '.join(
                f'{width}×{height}' for width, height in dimensions
            )
            size += ' 格' if font_path else ' tiles'
        if item.get('recognition_only'):
            size += ' · 仅识别' if font_path else ' / reference only'
        if flooring_overlap(item)['can_place_on_flooring']:
            size += (
                ' · 可置地板，保留' if font_path else ' / on flooring; keep'
            )
        elif item.get('subcategory') == 'flooring':
            size += ' · 下层保留' if font_path else ' / keep under objects'
    text_regions.append(
        fitted_text(
            draw,
            (left + 7, bottom - 18, right - 6, bottom - 3),
            size,
            font_path,
            11,
            '#687568',
            max_lines=1,
        )
    )
    return rendered, text_regions


def build(dataset, output_root=None, font_path=None):
    output_root = Path(output_root or dataset.root).resolve()
    directory = output_root / 'assets/recognition'
    directory.mkdir(parents=True, exist_ok=True)
    font_path = chinese_font_path(font_path)
    config = load_groups(dataset.root)
    supplemental = load_supplemental(dataset.root)
    grouped = atlas_families(dataset.catalog, config, supplemental)
    index = {
        'schema_version': 1,
        'catalog_sha256': hashlib.sha256(
            (dataset.root / 'data/planner/catalog.json').read_bytes()
        ).hexdigest(),
        'groups_sha256': hashlib.sha256(
            (dataset.root / 'data/recognition/groups.json').read_bytes()
        ).hexdigest(),
        'overrides_sha256': hashlib.sha256(
            (dataset.root / 'data/catalog-overrides.json').read_bytes()
        ).hexdigest(),
        'supplemental_sha256': hashlib.sha256(
            (dataset.root / 'data/recognition/supplemental.json').read_bytes()
        ).hexdigest(),
        'coordinate_system': 'Sheet card bounds are pixels [left, top, right, bottom], right/bottom exclusive; not farm tile coordinates.',
        'label_language': 'zh-en' if font_path else 'en',
        'workflow_order': config['workflow_order'],
        'ordering': 'Configured family and item order; fill pages consecutively and continue large families on the next page.',
        'groups': {},
        'items': {},
        'lookup': {},
    }
    for group, families in grouped.items():
        group_info = config['groups'][group]
        layout_name = 'gallery' if group in GALLERY_GROUPS else 'compact'
        layout = SHEET_LAYOUTS[layout_name]
        if group == 'ignored_entities':
            count = sum(len(family['items']) for family in families)
            layout = {
                **layout,
                'card_width': 216,
                'columns': 8,
                'rows': max(1, math.ceil(count / 8)),
            }
        card_width, card_height = layout['card_width'], layout['card_height']
        columns = layout['columns']
        capacity = columns * layout['rows']
        family_pages = paginate_families(families, capacity)
        page_count = len(family_pages)
        pages = []
        first = 0
        for page_number, page_families in enumerate(family_pages):
            page_items = [
                (family, item)
                for family in page_families
                for item in family['items']
            ]
            rows = math.ceil(len(page_items) / columns)
            width = MARGIN * 2 + card_width * columns + GAP * (columns - 1)
            height = HEADER + rows * card_height + (rows - 1) * GAP + FOOTER
            canvas = Image.new('RGB', (width, height), '#eef2eb')
            draw = ImageDraw.Draw(canvas)
            title = (
                group_info['name_zh'] if font_path else group_info['name_en']
            )
            draw.text(
                (MARGIN, 8),
                f'{group_info["atlas_order"]:02d}  {title}  {page_number + 1}/{page_count}',
                font=load_font(23, font_path),
                fill='#263d30',
            )
            subtitle = (
                '名称 · ID · 规划器占地 | 图中像素坐标不可用于农场摆放'
                if font_path
                else 'Names / IDs / planner footprints | Sheet pixels are not farm coordinates'
            )
            if group == 'ignored_entities':
                subtitle = (
                    '幼儿、宠物、动物、召唤物、鹦鹉及支架、松露、火把、自带信箱 | 仅供识别，规划时忽略 | 大小不代表游戏尺寸'
                    if font_path
                    else 'Toddlers, animals, companions, perches, truffles, torches and bundled mailboxes | Identify and omit'
                )
            draw.text(
                (MARGIN, 40),
                subtitle,
                font=load_font(12, font_path),
                fill='#687568',
            )
            name = f'{group_info["atlas_order"]:02d}-{group}-{page_number + 1:02d}.png'
            relative = f'assets/recognition/{name}'
            for offset, (family, item) in enumerate(page_items):
                left = MARGIN + offset % columns * (card_width + GAP)
                top = HEADER + offset // columns * (card_height + GAP)
                box = [left, top, left + card_width, top + card_height]
                appearances, text_regions = draw_card(
                    canvas,
                    dataset,
                    item,
                    group,
                    box,
                    first + offset + 1,
                    font_path,
                    family,
                    layout_name,
                )
                similar_items = [
                    entry
                    for entry in appearances
                    if entry.get('comparison_only')
                ]
                reference_images = [
                    entry
                    for entry in appearances
                    if entry.get('reference_only')
                ]
                appearances = [
                    entry
                    for entry in appearances
                    if not entry.get('comparison_only')
                    and not entry.get('reference_only')
                ]
                entry = {
                    'id': item['id'],
                    'name_zh': item['name_zh'],
                    'name_en': item['name_en'],
                    'group': group,
                    'family_id': family['id'],
                    'family_name_zh': family['name_zh'],
                    'family_name_en': family['name_en'],
                    'sheet': relative,
                    'card': first + offset + 1,
                    'card_bounds_px': box,
                    'text_regions': text_regions,
                    'footprint_tiles': None
                    if item.get('recognition_action') == 'omit'
                    else list(
                        dataset.dimensions(
                            item.get('planner_type', item['id'])
                        )
                    ),
                    'placement_areas_tiles': None
                    if item.get('recognition_action') == 'omit'
                    else placement_areas(item),
                    'recognition_only': item.get('recognition_only', False),
                    'planner_type': None
                    if item.get('recognition_action') == 'omit'
                    else item.get('planner_type', item['id']),
                    'appearances': appearances,
                    'available_appearances': appearances
                    if item.get('recognition_only')
                    else asset_appearances(dataset, item['id']),
                }
                if similar_items:
                    entry['similar_items'] = similar_items
                if reference_images:
                    entry['reference_images'] = reference_images
                if guide := dataset.recognition_guides.get(item['id']):
                    entry['recognition_guide'] = guide
                if item.get('building_placement'):
                    entry['building_placement'] = item['building_placement']
                if item.get('recognition_action') != 'omit':
                    entry['can_place_on_flooring'] = flooring_overlap(item)[
                        'can_place_on_flooring'
                    ]
                if item.get('recognition_action') == 'omit':
                    entry['recognition_action'] = 'omit'
                    entry['placement_supported'] = False
                index['items'][item['id']] = entry
                aliases = [
                    item['id'],
                    *item['aliases'],
                    *item['variant_ids'],
                    *(
                        entry['id']
                        for entry in appearances
                        if entry['id'].startswith(item['id'] + '-')
                    ),
                ]
                for alias in aliases:
                    index['lookup'].setdefault(alias, item['id'])
            note = (
                '来源：stardew.info、stardewplan.com、星露谷 Wiki。图中外观均支持本地布局与渲染。'
                if font_path
                else 'Sources: stardew.info, stardewplan.com and Stardew Valley Wiki. All appearances support local layouts.'
            )
            if group == 'ignored_entities':
                note = (
                    '来源：星露谷 Wiki。Stardew Valley / ConcernedApe。主条目及其他颜色均忽略；相似对照中标注“保留”的家具仍需保留。'
                    if font_path
                    else 'Source: Stardew Valley Wiki / ConcernedApe. Omit primary entries and other colors; retain comparison furniture labeled Keep.'
                )
            elif any(
                dataset.recognition_guides.get(item['id'], {}).get(
                    'show_placement_actions'
                )
                for _family, item in page_items
            ):
                note = (
                    '来源：stardew.info、stardewplan.com、星露谷 Wiki。相似图片仅供比较；按“保留/忽略”标记处理家具与动物。'
                    if font_path
                    else 'Sources: stardew.info, stardewplan.com and Stardew Valley Wiki. Similar images are comparisons; follow Keep/Omit labels.'
                )
            draw.text(
                (MARGIN, height - 22),
                note,
                font=load_font(11, font_path),
                fill='#687568',
            )
            path = directory / name
            canvas.save(path, format='PNG')
            pages.append(
                {
                    'path': relative,
                    'item_count': len(page_items),
                    'family_ids': [family['id'] for family in page_families],
                    'size_px': [width, height],
                    'sha256': hashlib.sha256(path.read_bytes()).hexdigest(),
                }
            )
            first += len(page_items)
        index['groups'][group] = {
            **group_info,
            'item_count': first,
            'sheet_layout': {
                'name': layout_name,
                **layout,
                'capacity': capacity,
            },
            'pages': pages,
        }
    for old_id, item_id in config['normalized_ids'].items():
        if item_id in index['items']:
            index['lookup'][old_id] = item_id
    index['item_count'] = len(index['items'])
    expected = {
        key
        for key, value in dataset.items.items()
        if not value['is_planner_tool']
        and not ignored_vegetation(value, config)
    }
    index['coverage'] = {
        'catalog_item_count': len(expected),
        'indexed_catalog_item_count': len(expected & set(index['lookup'])),
        'missing_ids': sorted(expected - set(index['lookup'])),
    }
    if index['coverage']['missing_ids']:
        raise LayoutError(
            f'Missing recognition entries: {index["coverage"]["missing_ids"]}'
        )
    for page in directory.glob('*.png'):
        if page.name not in {
            Path(info['path']).name
            for group in index['groups'].values()
            for info in group['pages']
        }:
            if any(
                page.name.startswith(f'{info["atlas_order"]:02d}-{key}-')
                for key, info in config['groups'].items()
            ):
                page.unlink()
    write_json_atomic(output_root / 'data/recognition/index.json', index)
    return index


def main():
    parser = argparse.ArgumentParser(
        description='Build categorized recognition sheets and an ID-to-card index from local sprites.'
    )
    parser.add_argument('--output-root', type=Path, default=ROOT)
    parser.add_argument('--font', type=Path)
    args = parser.parse_args()
    index = build(Dataset(), args.output_root, args.font)
    print(
        json.dumps(
            {
                'items': index['item_count'],
                'groups': {
                    key: {
                        'items': info['item_count'],
                        'pages': len(info['pages']),
                    }
                    for key, info in index['groups'].items()
                },
                'index': str(
                    args.output_root.resolve() / 'data/recognition/index.json'
                ),
            },
            ensure_ascii=False,
            indent=2,
        )
    )


if __name__ == '__main__':
    main()
