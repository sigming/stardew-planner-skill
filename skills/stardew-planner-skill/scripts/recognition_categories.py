import json
from functools import lru_cache
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


@lru_cache(maxsize=8)
def load_groups(root=ROOT):
    path = Path(root) / 'data/recognition/groups.json'
    return json.loads(path.read_text(encoding='utf-8'))


@lru_cache(maxsize=8)
def load_supplemental(root=ROOT):
    path = Path(root) / 'data/recognition/supplemental.json'
    return (
        json.loads(path.read_text(encoding='utf-8'))
        if path.is_file()
        else {'items': {}}
    )


def ignored_vegetation(item, config=None):
    config = config or load_groups()
    return (
        item.get('subcategory') in config['ignored_subcategories']
        or item.get('id') in config['ignored_ids']
    )


def recognition_subject(items, entities, item_id):
    placeable = item_id in items
    item = items[item_id] if placeable else entities[item_id]
    image = (
        'data/planner/' + item['image']['path']
        if placeable
        else item['sprites']['default']['path']
    )
    return {
        'id': item_id,
        'name_zh': item['name_zh'],
        'name_en': item['name_en'],
        'image': image,
        'placement_supported': placeable,
        'recognition_action': 'place' if placeable else 'omit',
    }


def recognition_group(item, config=None):
    config = config or load_groups()
    if item.get('recognition_group'):
        return item['recognition_group']
    if ignored_vegetation(item, config):
        return 'ignored'
    if item['is_planner_tool']:
        return 'planner'
    for group, ids in config['override_ids'].items():
        if item['id'] in ids:
            return group
    if item['category'] == 'building':
        return 'buildings'
    if item['category'] == 'furniture':
        return 'furniture'
    if item['category'] == 'crop':
        return 'crops'
    if item['subcategory'] == 'tree':
        return 'trees'
    if item['subcategory'] in ['flooring', 'fence', 'gate', 'hoe-dirt']:
        return 'flooring_fences'
    if item['category'] == 'craftable':
        return 'tools'
    return 'decorations'


def legend_sort_keys(dataset, item_types):
    """Reuse recognition categories and families without atlas pagination."""
    config = dataset.recognition_config
    families = {
        (group, item_id): (family_index, item_index)
        for group, info in config['groups'].items()
        for family_index, family in enumerate(info.get('families', []))
        for item_index, item_id in enumerate(family['ids'])
    }
    keys = {}
    for kind in item_types:
        base = dataset.item_id(kind)
        group = recognition_group(dataset.items[base], config)
        info = config['groups'].get(group, {})
        series = config.get('legend_series', {}).get(group, [])
        series_index = next(
            (
                index
                for index, entry in enumerate(series)
                if any(base.startswith(prefix) for prefix in entry['prefixes'])
                or any(token in base for token in entry.get('contains', []))
            ),
            len(series),
        )
        family_index, item_index = families.get(
            (group, base), (len(info.get('families', [])), 0)
        )
        keys[kind] = (
            info.get('atlas_order', len(config['groups']) + 1),
            series_index,
            family_index,
            item_index,
        )
    return keys


def atlas_families(catalog, config=None, supplemental=None):
    config = config or load_groups()
    supplemental = supplemental or load_supplemental()
    result = {group: [] for group in config['groups']}
    all_items = list(catalog['items'].values())
    all_items.extend(
        {
            **catalog['items'][item['planner_type']],
            **item,
            'variant_ids': [],
            'hidden_in_menu': False,
            'aliases': [],
        }
        for item in supplemental['items'].values()
        if item['id'] not in catalog['items']
    )
    all_items.extend(
        {'hidden_in_menu': False, 'variant_ids': [], **item}
        for item in supplemental.get('reference_entities', {}).values()
    )
    for item in all_items:
        if item['hidden_in_menu'] or item['id'] in config.get(
            'normalized_ids', {}
        ):
            continue
        group = recognition_group(item, config)
        if group in result:
            result[group].append(item)
    grouped = {}
    for group, items in result.items():
        remaining = {item['id']: item for item in items}
        families = []
        family_ids = set()
        for family in config['groups'][group].get('families', []):
            if family['id'] in family_ids:
                raise ValueError(
                    f'Duplicate recognition family: {group}/{family["id"]}'
                )
            family_ids.add(family['id'])
            selected = []
            for item_id in family['ids']:
                if item_id not in remaining:
                    raise ValueError(
                        f'Duplicate, missing or misclassified recognition item: {group}/{item_id}'
                    )
                selected.append(remaining.pop(item_id))
            if selected:
                families.append(
                    {
                        key: value
                        for key, value in family.items()
                        if key != 'ids'
                    }
                    | {'items': selected}
                )
        if remaining:
            families.append(
                {
                    'id': 'other',
                    'name_zh': '其他',
                    'name_en': 'Other',
                    'items': [remaining[key] for key in sorted(remaining)],
                }
            )
        grouped[group] = families
    return grouped


def atlas_items(catalog, config=None, supplemental=None):
    return {
        group: [item for family in families for item in family['items']]
        for group, families in atlas_families(
            catalog, config, supplemental
        ).items()
    }
