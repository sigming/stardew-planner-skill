# /// script
# requires-python = ">=3.12"
# dependencies = ["pillow==12.3.0"]
# ///

import argparse
import json
from pathlib import Path
from types import SimpleNamespace

from layout_assets import appearances, resolve_effects, seasonal_id
from layout_images import crop_image
from layout_placement import flooring_overlap, placement_areas, placement_label
from recognition_categories import (
    ignored_vegetation,
    load_groups,
    load_supplemental,
    recognition_group,
    recognition_subject,
)

ROOT = Path(__file__).resolve().parents[1]


def recognition_record(card, root, full=False):
    if not card:
        return None
    selected = (
        card
        if full
        else {
            key: card[key]
            for key in (
                'id',
                'group',
                'family_id',
                'sheet',
                'card',
                'card_bounds_px',
                'footprint_tiles',
            )
        }
    )
    return {
        **selected,
        'sheet_absolute_path': str(root / card['sheet']),
        'placement_supported': not card.get('recognition_only', False),
        **(
            {'recognition_action': card['recognition_action']}
            if card.get('recognition_action')
            else {}
        ),
    }


def recognition_guidance(
    item_id, overrides, catalog, supplemental, index, root
):
    guide = overrides.get('recognition_guides', {}).get(item_id)
    if not guide:
        return {}
    similar_items = []
    for similar_id in guide.get('similar_item_ids', []):
        subject = recognition_subject(
            catalog['items'],
            supplemental.get('reference_entities', {}),
            similar_id,
        )
        similar_items.append(
            {
                **subject,
                'image_absolute_path': str(root / subject['image']),
                'comparison_only': True,
                'recognition': recognition_record(
                    index.get('items', {}).get(
                        index.get('lookup', {}).get(similar_id)
                    ),
                    root,
                ),
            }
        )
    return {
        'recognition_guide': guide,
        'similar_items': similar_items,
        'reference_images': [
            {**entry, 'image_absolute_path': str(root / entry['image'])}
            for entry in guide.get('reference_images', [])
        ],
    }


def sheet_locations(recognition, root, group=None, family=None):
    pages = []
    for group_id, info in recognition.get('groups', {}).items():
        if group and group_id != group:
            continue
        for page in info['pages']:
            cards = sorted(
                (
                    card
                    for card in recognition['items'].values()
                    if card['sheet'] == page['path']
                    and (not family or card['family_id'] == family)
                ),
                key=lambda card: card['card'],
            )
            if not cards:
                continue
            families = {}
            for card in cards:
                row = families.setdefault(
                    card['family_id'],
                    {
                        'id': card['family_id'],
                        'name_zh': card['family_name_zh'],
                        'name_en': card['family_name_en'],
                        'card_range': [card['card'], card['card']],
                    },
                )
                row['card_range'][1] = card['card']
            pages.append(
                {
                    'group': group_id,
                    'name_zh': info['name_zh'],
                    'sheet_absolute_path': str(root / page['path']),
                    'size_px': page['size_px'],
                    'matched_card_count': len(cards),
                    'families': list(families.values()),
                }
            )
    return {
        'index_absolute_path': str(root / 'data/recognition/index.json'),
        'sheet_count': len(pages),
        'sheets': pages,
    }


def main():
    parser = argparse.ArgumentParser(
        description='Query the local item catalog by ID, Chinese or English name.'
    )
    parser.add_argument('query', nargs='?', default='')
    parser.add_argument(
        '--catalog',
        type=Path,
        default=ROOT / 'data' / 'planner' / 'catalog.json',
    )
    parser.add_argument('--include-hidden', action='store_true')
    parser.add_argument('--category')
    parser.add_argument(
        '--recognition-group', choices=list(load_groups()['groups'])
    )
    parser.add_argument(
        '--sheets',
        action='store_true',
        help='List indexed sheet paths and family card ranges without item records.',
    )
    parser.add_argument(
        '--recognition-family',
        help='Filter --sheets by a family ID returned in the sheet index.',
    )
    parser.add_argument('--effects-only', action='store_true')
    parser.add_argument('--limit', type=int, default=20)
    parser.add_argument('--offset', type=int, default=0)
    parser.add_argument(
        '--image-out',
        type=Path,
        help='Extract one matched card or sprite into a local PNG.',
    )
    parser.add_argument(
        '--image-kind', choices=['card', 'sprite'], default='card'
    )
    parser.add_argument(
        '--season', choices=['spring', 'summer', 'fall', 'winter']
    )
    parser.add_argument('--scale', type=int, default=1)
    parser.add_argument(
        '--full',
        action='store_true',
        help='Return complete records instead of a compact index.',
    )
    args = parser.parse_args()
    if args.limit < 1 or args.offset < 0:
        parser.error('Use a positive limit and a nonnegative offset')
    catalog = json.loads(args.catalog.read_text(encoding='utf-8'))
    resolve_effects(catalog['items'])
    root = args.catalog.resolve().parents[2]
    recognition_path = root / 'data/recognition/index.json'
    recognition = (
        json.loads(recognition_path.read_text(encoding='utf-8'))
        if recognition_path.is_file()
        else {}
    )
    if args.recognition_family and not args.sheets:
        parser.error('--recognition-family requires --sheets')
    if args.sheets:
        if not recognition:
            parser.error(
                'Recognition index is missing; reinstall the skill or rebuild it from the source repository'
            )
        if (
            args.query
            or args.full
            or args.category
            or args.effects_only
            or args.include_hidden
            or args.image_out
        ):
            parser.error(
                '--sheets supports only recognition group/family filters'
            )
        print(
            json.dumps(
                sheet_locations(
                    recognition,
                    root,
                    args.recognition_group,
                    args.recognition_family,
                ),
                ensure_ascii=False,
                indent=2,
            )
        )
        return
    config = (
        load_groups(root)
        if (root / 'data/recognition/groups.json').is_file()
        else load_groups()
    )
    notes_path = root / 'data/catalog-overrides.json'
    overrides = (
        json.loads(notes_path.read_text()) if notes_path.is_file() else {}
    )
    dataset = SimpleNamespace(
        items=catalog['items'],
        root=root,
        appearance_notes=overrides.get('appearance_notes', {}),
    )
    supplemental_data = load_supplemental(root)
    supplemental = {
        **supplemental_data['items'],
        **supplemental_data.get('reference_entities', {}),
    }
    query = args.query.casefold()
    exact_id = catalog['aliases'].get(args.query, args.query)
    exact_id = config['normalized_ids'].get(exact_id, exact_id)
    exact_match = exact_id in catalog['items'] or exact_id in supplemental
    matching_normalized = {
        target
        for source, target in config['normalized_ids'].items()
        if source in catalog['items']
        and query
        and query
        in ' '.join(
            [
                source,
                catalog['items'][source]['name_en'],
                catalog['items'][source]['name_zh'] or '',
            ]
        ).casefold()
    }
    matches = []
    for item in catalog['items'].values():
        if (
            ignored_vegetation(item, config)
            or item['id'] in config['normalized_ids']
        ):
            continue
        if exact_match:
            if item['id'] != exact_id:
                continue
        else:
            if not args.include_hidden and (
                item['hidden_in_menu'] or item['is_planner_tool']
            ):
                continue
            searchable = ' '.join(
                [
                    item['id'],
                    item['name_en'],
                    item['name_zh'] or '',
                    *item['aliases'],
                ]
            ).casefold()
            if (
                query
                and query not in searchable
                and item['id'] not in matching_normalized
            ):
                continue
        if args.category and item['category'] != args.category:
            continue
        if args.effects_only and not item['effect']:
            continue
        group = recognition_group(item, config)
        if args.recognition_group and group != args.recognition_group:
            continue
        recognition_id = recognition.get('lookup', {}).get(item['id'])
        card = recognition.get('items', {}).get(recognition_id)
        extra = {
            'placement_supported': True,
            'recognition_action': 'place',
            'flooring_overlap': flooring_overlap(item),
            'placement_areas_tiles': placement_areas(item),
            'placement_size_label': placement_label(item),
            **(
                {'building_placement': item['building_placement']}
                if item.get('building_placement')
                else {}
            ),
            'recognition_group': group,
            'supplemental_reference_ids': [
                key
                for key, value in supplemental.items()
                if value.get('planner_type') == item['id']
            ],
            'appearances': [
                {
                    **entry,
                    'image_absolute_path': str(
                        root / 'data/planner' / entry['image']
                    ),
                }
                for entry in appearances(dataset, item['id'])
            ],
            'footprint_from_planner_anchor': item['placement']['footprint'][
                'top_left_from_planner_anchor'
            ],
            'recognition': recognition_record(card, root, args.full),
            'known_limitations': [
                entry
                for entry in supplemental_data.get('audit', {}).get(
                    'known_limitations', []
                )
                if entry['id'].startswith(item['id'] + '-')
            ],
            'placement_convention': item['placement'].get('convention'),
            **(
                {'placement_notes': item['placement_notes']}
                if item.get('placement_notes')
                else {}
            ),
            **recognition_guidance(
                item['id'],
                overrides,
                catalog,
                supplemental_data,
                recognition,
                root,
            ),
            **(
                {
                    'appearance_simplification': overrides[
                        'appearance_simplifications'
                    ][item['id']]
                }
                if item['id']
                in overrides.get('appearance_simplifications', {})
                else {}
            ),
        }
        if args.full:
            matches.append({**item, **extra})
        else:
            footprint = item['placement']['footprint']
            matches.append(
                {
                    'id': item['id'],
                    'name_zh': item['name_zh'],
                    'name_en': item['name_en'],
                    'category': item['category'],
                    'footprint_tiles': [
                        footprint['width'],
                        footprint['height'],
                    ],
                    'image': item['image']['path'],
                    'effect_status': item['effect_status'],
                    **extra,
                }
            )
    for item in supplemental.values():
        if item['id'] in catalog['items']:
            continue
        if exact_match and item['id'] != exact_id:
            continue
        if (
            not exact_match
            and query
            and query
            not in ' '.join(
                [
                    item['id'],
                    item['name_zh'],
                    item['name_en'],
                    *item.get('aliases', []),
                ]
            ).casefold()
        ):
            continue
        if (
            args.recognition_group
            and item['recognition_group'] != args.recognition_group
        ):
            continue
        if args.effects_only or (
            args.category
            and catalog['items']
            .get(item.get('planner_type'), {})
            .get('category')
            != args.category
        ):
            continue
        card = recognition.get('items', {}).get(item['id'])
        matches.append(
            {
                **item,
                'placement_supported': False,
                'note': item.get('note_zh')
                if item.get('recognition_action') == 'omit'
                else 'Recognition reference only; planner_type is a proposed substitution, not an equivalent appearance. Obtain user approval before substitution.',
                'appearances': [
                    {
                        'season': season,
                        **asset,
                        'image_absolute_path': str(root / asset['path']),
                    }
                    for season, asset in item['sprites'].items()
                ],
                'recognition': recognition_record(card, root, args.full),
                **recognition_guidance(
                    item['id'],
                    overrides,
                    catalog,
                    supplemental_data,
                    recognition,
                    root,
                ),
            }
        )
    for item in supplemental_data.get('components', {}).values():
        if (
            not query
            or query
            not in ' '.join(
                [item['id'], item['name_en'], item['name_zh']]
            ).casefold()
        ):
            continue
        if args.effects_only or args.category or args.recognition_group:
            continue
        entry = (
            item
            if args.full
            else {
                key: item[key]
                for key in (
                    'id',
                    'name_zh',
                    'name_en',
                    'placement_supported',
                    'recognition_only',
                    'source_page',
                    'note_zh',
                    'component_counts',
                    'sheet',
                    'size_px',
                )
            }
        )
        matches.append(
            {**entry, 'sheet_absolute_path': str(root / item['sheet'])}
        )
    result = {
        'count': len(matches),
        'offset': args.offset,
        'items': matches[args.offset : args.offset + args.limit],
    }
    if args.image_out:
        if len(matches) != 1:
            parser.error(
                '--image-out needs exactly one matched item; query its exact ID'
            )
        item = matches[0]
        try:
            if args.image_kind == 'sprite':
                kind = item['id']
                if kind not in catalog['items']:
                    parser.error(
                        'This reference has no placeable sprite; extract its card instead'
                    )
                if args.season:
                    kind = seasonal_id(dataset, kind, args.season)
                source = (
                    root
                    / 'data/planner'
                    / catalog['items'][kind]['image']['path']
                )
                rect = None
            elif item.get('recognition'):
                card = item['recognition']
                source = Path(card['sheet_absolute_path'])
                left, top, right, bottom = card['card_bounds_px']
                rect = [left, top, right - left, bottom - top]
            else:
                source = Path(item['sheet_absolute_path'])
                rect = None
            result['image'] = crop_image(
                source, args.image_out, rect, args.scale
            )
        except (ValueError, KeyError, OSError) as error:
            parser.error(str(error))
    result['returned_count'] = len(result['items'])
    print(
        json.dumps(
            result,
            ensure_ascii=False,
            indent=2,
        )
    )


if __name__ == '__main__':
    main()
