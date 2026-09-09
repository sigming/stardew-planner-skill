# /// script
# requires-python = ">=3.12"
# dependencies = ["pillow==12.3.0"]
# ///

"""Sync vanilla appearances and compile the local catalog from native assets."""

import argparse
import copy
import hashlib
import json
from collections import Counter
from concurrent.futures import ThreadPoolExecutor
from io import BytesIO
from pathlib import Path
from urllib.parse import quote

from audit_catalog import (
    SOURCE_PATH,
    UNSUPPORTED_CATALOG_IDS,
    apply_source_metadata,
    source_index,
    supplemental_records,
    validate_catalog,
)
from audit_catalog import (
    download as fetch,
)
from PIL import Image, ImageDraw
from sync_reference_assets import refresh_reference_assets

ROOT = Path(__file__).resolve().parents[2] / 'skills/stardew-planner-skill'
VERSION = '1.6.15'
SEASONS = ('spring', 'summer', 'fall', 'winter')
CABINS = {
    'stone-cabin': ('Stone Cabin', '石头小屋'),
    'plank-cabin': ('Plank Cabin', '木板小屋'),
    'log-cabin': ('Log Cabin', '原木小屋'),
    'neighbor-cabin': ('Neighbor Cabin', '邻居小屋'),
    'rustic-cabin': ('Rustic Cabin', '乡村小屋'),
    'beach-cabin': ('Beach Cabin', '沙滩小屋'),
    'trailer-cabin': ('Trailer Cabin', '拖车小屋'),
}


def digest(value):
    return hashlib.sha256(value).hexdigest()


def write_json(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(value, ensure_ascii=False, indent=2) + '\n',
        encoding='utf-8',
    )


def save_sprite(root, item_id, appearance, source, url, rect):
    with Image.open(BytesIO(source)) as image:
        pixels = image.convert('RGBA').crop(rect)
    path = root / f'data/planner/images/{item_id}-{appearance}.png'
    path.parent.mkdir(parents=True, exist_ok=True)
    pixels.save(path)
    return {
        'path': path.relative_to(root).as_posix(),
        'url': url,
        'source_sha256': digest(source),
        'source_rect_px': list(rect),
        'sha256': digest(path.read_bytes()),
        'size_px': list(pixels.size),
    }


def craftable_rect(index):
    left, top = index % 8 * 16, index // 8 * 32
    return left, top, left + 16, top + 32


def cactus_components(root, source, url):
    path = root / 'assets/recognition/free-cactus-components.png'
    canvas = Image.new('RGB', (512, 640), '#f5f2e9')
    draw = ImageDraw.Draw(canvas)
    source_image = Image.open(BytesIO(source)).convert('RGBA')
    pieces = []
    for group, count, base_y, offset in [
        ('pot', 16, 96, 0),
        ('body', 24, 48, -8),
        ('top', 24, 0, -24),
    ]:
        for index in range(count):
            left, top = index % 8 * 16, base_y + index // 8 * 16
            rect = [left, top, left + 16, top + 16]
            x, y = len(pieces) % 8 * 64, len(pieces) // 8 * 80
            pixels = source_image.crop(rect).resize(
                (48, 48), Image.Resampling.NEAREST
            )
            canvas.paste(pixels, (x + 8, y + 5), pixels)
            piece_id = f'{group}-{index + 1:02d}'
            draw.text((x + 5, y + 58), piece_id, fill='#263d30')
            pieces.append(
                {
                    'id': piece_id,
                    'source_rect_px': rect,
                    'draw_offset_y_px': offset,
                }
            )
    canvas.save(path)
    return {
        'id': 'free-cactus',
        'name_en': 'Random Cactus',
        'name_zh': '免费仙人掌',
        'placement_supported': False,
        'recognition_only': True,
        'source_page': 'https://stardewvalleywiki.com/Desert_Festival#Free_Cactus',
        'note_zh': '组件图支持识别盆、主体和顶部；本地布局尚未实现随机组件组合，不以其他植物替代。',
        'url': url,
        'source_sha256': digest(source),
        'sheet': path.relative_to(root).as_posix(),
        'sheet_sha256': digest(path.read_bytes()),
        'size_px': list(canvas.size),
        'component_counts': {'pot': 16, 'body': 24, 'top': 24},
        'components': pieces,
    }


def building_records(root, version):
    base = f'https://assets.stardewplan.com/assets/{version}/buildings'
    records = {}
    for item_id, name_zh in [
        ('pet-bowl', '宠物碗'),
        ('stone-pet-bowl', '石制宠物碗'),
        ('hay-pet-bowl', '干草宠物碗'),
    ]:
        name_en = item_id.replace('-', ' ').title()
        url = f'{base}/{quote(name_en)}.png'
        source = fetch(url)
        records[item_id] = {
            'id': item_id,
            'template_type': item_id,
            'planner_type': item_id,
            'name_zh': name_zh,
            'name_en': name_en,
            'recognition_group': 'buildings',
            'family_id': 'pet_bowls',
            'source_page': 'https://stardewvalleywiki.com/Pets',
            'source_item_id': 'Pet Bowl' if item_id == 'pet-bowl' else name_en,
            'source_table': 'Buildings',
            'recognition_only': False,
            'footprint_tiles': [2, 2],
            'category': 'building',
            'sprites': {
                season: save_sprite(
                    root,
                    item_id,
                    season,
                    source,
                    url,
                    (0, index * 32, 32, (index + 1) * 32),
                )
                for index, season in enumerate(SEASONS)
            },
        }
    url = f'{base}/Greenhouse.png'
    source = fetch(url)
    for item_id, top, name_zh in [
        ('greenhouse', 0, '温室（修复前）'),
        ('greenhouse-repaired', 160, '温室（已修复）'),
    ]:
        records[item_id] = {
            'id': item_id,
            'template_type': item_id,
            'planner_type': item_id,
            'name_zh': name_zh,
            'name_en': 'Greenhouse' if top == 0 else 'Greenhouse (repaired)',
            'recognition_group': 'buildings',
            'family_id': 'farmhouse',
            'source_page': 'https://stardewvalleywiki.com/Greenhouse',
            'source_item_id': 'Greenhouse'
            if top
            else 'Greenhouse (unrepaired)',
            'source_table': 'Buildings',
            'recognition_only': False,
            'footprint_tiles': [7, 6],
            'category': 'building',
            'building_placement': {
                'human_door': [3, 5],
                'additional_placement_tiles': [
                    {
                        'tile_area': [2, 6, 3, 2],
                        'only_needs_to_be_passable': True,
                    },
                ],
                'source_url': f'https://assets.stardewplan.com/assets/{version}/data/Buildings.json',
                'semantics': 'AdditionalPlacementTiles are checked when placing or moving the building; they are not a permanent occupied footprint.',
            },
            'sprites': {
                'default': save_sprite(
                    root,
                    item_id,
                    'default',
                    source,
                    url,
                    (0, top, 112, top + 160),
                )
            },
        }
    return records


def refresh_sources(root, version):
    base = f'https://assets.stardewplan.com/assets/{version}'
    urls = {
        'buildings': f'{base}/data/Buildings.json',
        'craftables': f'{base}/data/BigCraftables.json',
        'giant_crops': f'{base}/data/GiantCrops.json',
        'craftables_image': f'{base}/tilesheets/craftables.png',
        'wiki_cabins': 'https://stardewvalleywiki.com/Cabin',
        'wiki_scarecrows': 'https://stardewvalleywiki.com/Scarecrow',
        'wiki_seasonal_plants': 'https://stardewvalleywiki.com/Seasonal_Plant',
        **{
            item_id: f'{base}/buildings/{quote(name)}.png'
            for item_id, (name, _) in CABINS.items()
        },
    }
    with ThreadPoolExecutor(max_workers=6) as pool:
        data = dict(zip(urls, pool.map(fetch, urls.values()), strict=True))
    buildings = json.loads(data['buildings'])
    craftables = json.loads(data['craftables'])
    cabin = buildings['Cabin']
    styles = {skin['Id'] for skin in cabin['Skins']} | {'Stone Cabin'}
    if styles != {name for name, _ in CABINS.values()}:
        raise ValueError(f'Cabin style list changed: {sorted(styles)}')
    if cabin['Size'] != {'X': 5, 'Y': 3}:
        raise ValueError('Cabin footprint changed; check the render anchor')
    if cabin['SourceRect'] != {'X': 0, 'Y': 0, 'Width': 80, 'Height': 112}:
        raise ValueError(
            'Cabin sprite layout changed; check the source rectangles'
        )
    old_path = root / 'data/recognition/supplemental.json'
    old = json.loads(old_path.read_text(encoding='utf-8'))['items']
    records = {}
    rarecrows = sorted(
        (int(key), value)
        for key, value in craftables.items()
        if value['Name'] == 'Rarecrow'
    )
    if len(rarecrows) != 8:
        raise ValueError(f'Expected eight rarecrows, got {len(rarecrows)}')
    for number, (source_id, value) in enumerate(rarecrows, 1):
        item_id = f'rarecrow-{number}'
        records[item_id] = {
            'id': item_id,
            'template_type': 'scarecrow',
            'planner_type': item_id,
            'name_zh': f'珍奇稻草人 {number}',
            'name_en': f'Rarecrow {number}',
            'recognition_group': 'tools',
            'family_id': 'scarecrows',
            'source_page': urls['wiki_scarecrows'],
            'source_item_id': str(source_id),
            'recognition_only': False,
            'sprites': {
                'default': save_sprite(
                    root,
                    item_id,
                    'default',
                    data['craftables_image'],
                    urls['craftables_image'],
                    craftable_rect(value['SpriteIndex']),
                ),
            },
        }
    for item_id, item in old.items():
        if item_id.startswith('seasonal-plant-'):
            index = 184 + (int(item_id.rsplit('-', 1)[1]) - 1) * 4
            indices = range(index, index + 4)
            template = 'seasonal-plant'
        elif item_id == 'seasonal-decor-four-seasons':
            index, indices, template = 48, range(48, 52), 'seasonal-decor'
        elif item_id == 'tub-flowers-four-seasons':
            index, indices, template = (
                108,
                (108, 108, 109, 109),
                "tub-o'-flowers",
            )
        else:
            continue
        records[item_id] = {
            **{
                key: item[key]
                for key in (
                    'id',
                    'name_zh',
                    'name_en',
                    'recognition_group',
                    'family_id',
                    'source_page',
                )
            },
            'template_type': template,
            'planner_type': item_id,
            'source_item_id': str(index),
            'recognition_only': False,
            'sprites': {
                season: save_sprite(
                    root,
                    item_id,
                    season,
                    data['craftables_image'],
                    urls['craftables_image'],
                    craftable_rect(sprite_index),
                )
                for season, sprite_index in zip(SEASONS, indices, strict=True)
            },
        }
    for item_id, (name_en, name_zh) in CABINS.items():
        records[item_id] = {
            'id': item_id,
            'template_type': 'stone-cabin',
            'planner_type': item_id,
            'name_zh': name_zh,
            'name_en': name_en,
            'recognition_group': 'buildings',
            'family_id': 'cabins',
            'source_page': urls['wiki_cabins'],
            'source_item_id': 'Cabin' if item_id == 'stone-cabin' else name_en,
            'recognition_only': False,
            'footprint_tiles': [5, 3],
            'sprites': {
                f'stage-{stage}': save_sprite(
                    root,
                    item_id,
                    f'stage-{stage}',
                    data[item_id],
                    urls[item_id],
                    ((stage - 1) * 80, 0, stage * 80, 112),
                )
                for stage in range(1, 4)
            },
        }
    snapshot, sheets = source_index(version)
    write_json(root / SOURCE_PATH, snapshot)
    sources = snapshot['sources']
    components = cactus_components(
        root, sheets['FreeCactuses'], sources['FreeCactuses']['url']
    )
    records.update(supplemental_records(root, snapshot, sheets))
    records.update(building_records(root, version))
    result = {
        'schema_version': 2,
        'attribution': 'Stardew Valley / ConcernedApe; sprites and game data served by stardewplan.com, identities checked against Stardew Valley Wiki.',
        'asset_version': version,
        'sources': {
            **sources,
            **{
                key: {'url': url, 'sha256': digest(data[key])}
                for key, url in urls.items()
            },
        },
        'items': records,
        'components': {'free-cactus': components},
        'audit': {
            'giant_crop_types': list(json.loads(data['giant_crops'])),
            'source_entry_counts': snapshot['source_entry_counts'],
            'coverage_source_index': SOURCE_PATH,
            'coverage_report': 'data/recognition/coverage-audit.json',
            'existing_equivalent_ids': {
                'JojaCatalogue': 'joja-furniture-catalogue',
                'MorrisPortrait': 'manager-of-the-year',
                'WitchBroom': "witch's-broom",
                'LeafyPlant': 'swamp-plant',
                'CoupleElixirs': 'two-elixirs',
                'WizardTower': "'wizard's-tower'",
                'DecorativeDoor6': 'decorative-door',
            },
            'comparison_method': 'Compare normalized item names, then compare extracted sprite pixels over an opaque background to resolve renamed items. Check obtainability for new items against official wiki pages.',
            'known_limitations': [
                {
                    'id': 'cauldron-active',
                    'status': 'ignored_visual_effect',
                    'source_page': urls.get(
                        'wiki_cauldron',
                        'https://stardewvalleywiki.com/Cauldron',
                    ),
                    'note_zh': '按规划规则忽略坩埚的绿色烟雾及激活效果，使用默认空坩埚样式，无需确认。',
                },
                {
                    'id': 'free-cactus',
                    'status': 'components_only',
                    'note_zh': '保留16种盆、24种主体和24种顶部的组件图；9216种组合未实现本地放置。',
                },
            ],
        },
    }
    return refresh_reference_assets(root, result, version)


def apply_supplemental(catalog, supplemental, root=ROOT):
    """Compile independently placeable variants using the existing catalog schema."""
    items = catalog['items']
    templates = {
        entry['template_type']: copy.deepcopy(items[entry['template_type']])
        for entry in supplemental['items'].values()
    }
    for item_id, entry in supplemental['items'].items():
        variants = []
        for appearance, asset in entry['sprites'].items():
            if appearance in ['default', 'stage-1', 'spring']:
                kind, relation = item_id, None
            elif appearance.startswith('stage-'):
                stage = int(appearance.removeprefix('stage-'))
                kind = f'{item_id}-{stage}'
                relation = {
                    'kind': 'appearance',
                    'base_id': item_id,
                    'value': stage,
                }
            elif appearance in SEASONS:
                kind = f'{item_id}-{appearance}'
                relation = {
                    'kind': 'season',
                    'base_id': item_id,
                    'value': appearance,
                }
            else:
                kind = f'{item_id}-{appearance}'
                relation = {
                    'kind': 'appearance',
                    'base_id': item_id,
                    'value': appearance,
                }
            label_zh = entry['name_zh']
            label_en = entry['name_en']
            if appearance.startswith('stage-'):
                stage = int(appearance.removeprefix('stage-'))
                label_zh += (
                    '（初始）' if stage == 1 else f'（升级 {stage - 1}）'
                )
                label_en += (
                    ' (Initial)' if stage == 1 else f' (Upgrade {stage - 1})'
                )
            elif appearance.startswith('rotation-'):
                rotation = asset['rotation']
                label_zh += f'（旋转 {rotation}）'
                label_en += f' (Rotation {rotation})'
            if relation:
                variants.append(kind)
            record = copy.deepcopy(templates[entry['template_type']])
            record.update(
                {
                    'id': kind,
                    'aliases': [],
                    'name_zh': label_zh,
                    'name_en': label_en,
                    'name_zh_metadata': {
                        'value': label_zh,
                        'source': 'local_translation',
                        'status': 'local_translation',
                        'basis': entry['source_page'],
                    },
                    'recognition_group': entry['recognition_group'],
                    'hidden_in_menu': bool(relation),
                    'variant': relation,
                    'variant_ids': [],
                    'image': {
                        'path': str(
                            Path(asset['path']).relative_to('data/planner')
                        ),
                        'width_px': asset['size_px'][0],
                        'height_px': asset['size_px'][1],
                        'sha256': asset['sha256'],
                    },
                    'source': {
                        'page': entry['source_page'],
                        'item_id': entry['source_item_id'],
                        'asset_version': supplemental['asset_version'],
                        'rotation': asset.get('rotation', 0),
                        'flip_horizontal': asset.get('flip_horizontal', False),
                        **(
                            {'table': entry['source_table']}
                            if entry.get('source_table')
                            else {}
                        ),
                        **{
                            key: asset[key]
                            for key in (
                                'url',
                                'source_sha256',
                                'source_rect_px',
                            )
                        },
                        **(
                            {'layers': asset['source_layers']}
                            if asset.get('source_layers')
                            else {}
                        ),
                    },
                }
            )
            if entry.get('category'):
                record['category'] = entry['category']
            if entry.get('furniture_type'):
                record['furniture_type'] = entry['furniture_type']
            if entry.get('building_placement'):
                record['building_placement'] = entry['building_placement']
            if entry.get('placement_notes'):
                record['placement_notes'] = entry['placement_notes']
            if entry.get('parent_id') and appearance == 'default':
                record['variant'] = {
                    'kind': 'appearance',
                    'base_id': entry['parent_id'],
                    'value': entry['appearance_label'],
                }
                record['hidden_in_menu'] = True
            if 'footprint_tiles' in entry:
                width, height = asset.get(
                    'footprint_tiles', entry['footprint_tiles']
                )
                record['placement']['footprint'].update(
                    {
                        'width': width,
                        'height': height,
                        'top_left_from_planner_anchor': [0, 1 - height],
                        'anchor_offset_column': 0,
                        'anchor_offset_row': 0,
                        'source': entry.get(
                            'footprint_source',
                            'stardewplan_buildings_and_official_wiki',
                        ),
                    }
                )
                record['placement']['gameplay_footprint_verified'] = True
            if entry['template_type'] == 'scarecrow':
                record['effect'] = None
                record['effect_source_id'] = 'scarecrow'
                record['effect_status'] = 'inherited'
            items[kind] = record
        items[item_id]['variant_ids'] = list(
            dict.fromkeys(
                [
                    *items[item_id]['variant_ids'],
                    *variants,
                ]
            )
        )
    for item_id, entry in supplemental['items'].items():
        if parent := entry.get('parent_id'):
            items[parent]['variant_ids'] = list(
                dict.fromkeys(
                    [
                        *items[parent]['variant_ids'],
                        item_id,
                    ]
                )
            )
    snapshot_path = root / SOURCE_PATH
    if snapshot_path.is_file():
        apply_source_metadata(
            catalog, json.loads(snapshot_path.read_text()), root
        )
    for item_id in (
        UNSUPPORTED_CATALOG_IDS
        | supplemental.get('reference_entities', {}).keys()
    ):
        items.pop(item_id, None)
    catalog['aliases'] = {
        alias: target
        for alias, target in catalog['aliases'].items()
        if target in items
    }
    catalog['aliases']['温室'] = 'greenhouse-repaired'
    items['greenhouse-repaired']['aliases'] = list(
        dict.fromkeys(
            [
                *items['greenhouse-repaired']['aliases'],
                '温室',
            ]
        )
    )
    catalog['sources']['supplemental'] = {
        'path': '../recognition/supplemental.json',
        'asset_version': supplemental['asset_version'],
        'sha256': digest(
            (root / 'data/recognition/supplemental.json').read_bytes()
        ),
    }
    overrides = root / 'data/catalog-overrides.json'
    if overrides.is_file():
        catalog['sources']['local_name_overrides']['sha256'] = digest(
            overrides.read_bytes()
        )
        corrections = json.loads(overrides.read_text())
        apply_placement_overrides(catalog, corrections)
        for alias, target in corrections.get('aliases', {}).items():
            if target not in items:
                raise ValueError(f'Unknown alias target: {target}')
            catalog['aliases'][alias] = target
            items[target]['aliases'] = list(
                dict.fromkeys([*items[target]['aliases'], alias])
            )
    catalog['scope'] = (
        'Local vanilla layout catalog, including Stardew Valley 1.6 cabin skins, upgrade appearances, rarecrows and seasonal plants.'
    )
    catalog['stats'].update(
        {
            'unique_items': len(items),
            'visible_entries': sum(
                not item['hidden_in_menu'] for item in items.values()
            ),
            'visible_game_items': sum(
                not item['hidden_in_menu'] and not item['is_planner_tool']
                for item in items.values()
            ),
            'hidden_entries': sum(
                item['hidden_in_menu'] for item in items.values()
            ),
            'by_category': dict(
                Counter(item['category'] for item in items.values())
            ),
            'by_chinese_name_source': dict(
                Counter(
                    item['name_zh_metadata']['source']
                    for item in items.values()
                )
            ),
            'effect_masks': sum(
                bool(item['effect']) for item in items.values()
            ),
        }
    )
    return catalog


def apply_placement_overrides(catalog, overrides):
    for item in catalog['items'].values():
        base_id = (item.get('variant') or {}).get('base_id', item['id'])
        for convention_id, convention in overrides.get(
            'placement_conventions', {}
        ).items():
            if base_id not in convention['ids']:
                continue
            width, height = convention['footprint_tiles']
            column, row = convention['anchor_offset_tiles']
            item['placement']['footprint'].update(
                width=width,
                height=height,
                anchor_offset_column=column,
                anchor_offset_row=row,
                top_left_from_planner_anchor=[column, row - height + 1],
                source='local_layout_convention',
            )
            item['placement']['convention'] = {
                'id': convention_id,
                **{
                    key: value
                    for key, value in convention.items()
                    if key not in ['ids', 'anchor_offset_tiles']
                },
            }
            if placement := convention.get('building_placement'):
                item['building_placement'] = {
                    **item.get('building_placement', {}),
                    **copy.deepcopy(placement),
                }


def referenced_image_paths(root, catalog, supplemental, overrides):
    return (
        {
            (root / 'data/planner' / item['image']['path']).resolve()
            for item in catalog['items'].values()
        }
        | {
            (root / asset['path']).resolve()
            for item in supplemental['items'].values()
            for asset in item['sprites'].values()
        }
        | {
            (root / reference['image']).resolve()
            for guide in overrides.get('recognition_guides', {}).values()
            for reference in guide.get('reference_images', [])
        }
        | {
            (root / decoration['image']).resolve()
            for item in catalog['items'].values()
            if (
                decoration := item.get('building_placement', {}).get(
                    'front_decoration'
                )
            )
        }
    )


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--root', type=Path, default=ROOT)
    parser.add_argument('--version', default=VERSION)
    parser.add_argument(
        '--offline',
        action='store_true',
        help='Recompile the existing local sprites without downloads.',
    )
    args = parser.parse_args()
    root = args.root.resolve()
    path = root / 'data/recognition/supplemental.json'
    if args.offline:
        supplemental = json.loads(path.read_text(encoding='utf-8'))
    else:
        supplemental = refresh_sources(root, args.version)
        write_json(path, supplemental)
    catalog_path = root / 'data/planner/catalog.json'
    catalog = json.loads(catalog_path.read_text(encoding='utf-8'))
    apply_supplemental(catalog, supplemental, root)
    write_json(catalog_path, catalog)
    write_json(
        root / 'data/planner/validation.json',
        validate_catalog(catalog, root / 'data/planner'),
    )
    overrides = json.loads((root / 'data/catalog-overrides.json').read_text())
    referenced_images = referenced_image_paths(
        root, catalog, supplemental, overrides
    )
    removed_images = []
    for path in (root / 'data/planner/images').glob('*.png'):
        if path.resolve() not in referenced_images:
            path.unlink()
            removed_images.append(path.name)
    print(
        json.dumps(
            {
                'families': len(supplemental['items']),
                'sprites': sum(
                    len(item['sprites'])
                    for item in supplemental['items'].values()
                ),
                'removed_unreferenced_images': len(removed_images),
            }
        )
    )


if __name__ == '__main__':
    main()
