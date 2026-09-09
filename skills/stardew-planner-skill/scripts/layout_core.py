import copy
import json
import math
import os
import tempfile
from collections import Counter, defaultdict
from pathlib import Path

from layout_assets import (
    appearance_note,
    base_item_id,
    resolve_effects,
    seasonal_id,
)
from layout_placement import (
    additional_placement_errors,
    flooring_overlap,
    porch_cells,
    porch_overlap,
)
from map_rules import item_scope_error, placement_layer
from recognition_categories import (
    ignored_vegetation,
    load_groups,
    load_supplemental,
)

ROOT = Path(__file__).resolve().parents[1]
SEASONS = {'spring', 'summer', 'fall', 'winter'}
DEFAULT_CROP = 'strawberry-seeds'
DEFAULT_GIANT_CROP = 'giant-pumpkin'
CROP_ALIASES = {'crop', 'generic-crop', '作物', '种植位'}
GIANT_CROP_ALIASES = {'giant-crop', 'giant_crop', '巨型作物', '超大作物'}


class LayoutError(ValueError):
    pass


def require_vanilla(value):
    if not isinstance(value, dict):
        raise LayoutError('Expected a vanilla layout configuration object')
    if (
        value.get('game_mode', 'vanilla') != 'vanilla'
        or value.get('mods')
        or value.get('modded')
        or value.get('customLayout')
    ):
        raise LayoutError(
            'Only the bundled original Stardew Valley maps are supported'
        )


def normalize_crop(dataset, item):
    item_type = item.get('type')
    mode = item.get('crop_kind')
    if mode is not None and mode not in ['placeholder', 'specified']:
        raise LayoutError('crop_kind must be placeholder or specified')
    item_type = dataset.item_id(item_type)
    metadata = dataset.items[item_type]
    if metadata['category'] == 'crop':
        giant = metadata['subcategory'] == 'giant-crops'
        dimensions = (3, 3) if giant else (1, 1)
        if dataset.dimensions(item_type) != dimensions:
            raise LayoutError(f'Unexpected crop footprint: {item_type}')
        item_type = DEFAULT_GIANT_CROP if giant else DEFAULT_CROP
        item['crop_kind'] = 'placeholder'
    elif mode is not None:
        raise LayoutError('crop_kind can only be used for crop objects')
    item['type'] = item_type
    return item


def integer(value, label):
    if isinstance(value, bool) or not isinstance(value, int):
        raise LayoutError(f'{label} must be an integer')
    return value


def read_json(path):
    return json.loads(Path(path).read_text(encoding='utf-8'))


def write_json_atomic(path, value):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    text = (
        json.dumps(value, ensure_ascii=False, indent=2, allow_nan=False) + '\n'
    )
    fd, temporary = tempfile.mkstemp(prefix=f'.{path.name}.', dir=path.parent)
    try:
        with os.fdopen(fd, 'w', encoding='utf-8') as file:
            file.write(text)
        os.replace(temporary, path)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def record_omissions(plan, omitted, field='omitted_vegetation'):
    if not omitted:
        return
    previous_report = plan.get(field, {})
    if not isinstance(previous_report, dict):
        raise LayoutError(f'{field} must be an object')
    previous = previous_report.get('counts', {})
    if not isinstance(previous, dict) or any(
        type(count) is not int or count < 0 for count in previous.values()
    ):
        raise LayoutError(f'{field}.counts must contain nonnegative integers')
    counts = Counter(previous)
    counts.update(omitted)
    plan[field] = {
        'reason': 'Reference-only children, animals, companions, parrot perches, truffles, torches and bundled mailboxes are excluded; source map textures are retained.'
        if field == 'omitted_entities'
        else 'Placeable grass, blue grass and weeds are excluded; source map textures are retained.',
        'counts': dict(sorted(counts.items())),
        'total': counts.total(),
    }


class Dataset:
    def __init__(self, root=ROOT):
        self.root = Path(root).resolve()
        self.catalog = read_json(self.root / 'data/planner/catalog.json')
        self.map_catalog = read_json(self.root / 'data/maps/maps.json')
        self.items = self.catalog['items']
        resolve_effects(self.items)
        self.maps = self.map_catalog['maps']
        self.supported_maps = set(
            read_json(
                self.root / 'data/maps' / self.map_catalog['allowlist_path']
            )['map_ids']
        )
        self.recognition_config = load_groups(self.root)
        self.reference_entities = load_supplemental(self.root).get(
            'reference_entities', {}
        )
        self.normalized_ids = self.recognition_config['normalized_ids']
        self._blocked = {}
        overrides = read_json(self.root / 'data/catalog-overrides.json')
        self.appearance_notes = overrides.get('appearance_notes', {})
        self.appearance_simplifications = overrides.get(
            'appearance_simplifications', {}
        )
        self.recognition_guides = overrides.get('recognition_guides', {})

    def map_id(self, value):
        if not isinstance(value, str):
            raise LayoutError('Map ID must be a string')
        if value in self.maps and value in self.supported_maps:
            return value
        matches = [
            key
            for key, entry in self.maps.items()
            if value in entry['aliases']
            or value in [entry['name_zh'], entry['name_en']]
        ]
        if len(matches) != 1 or matches[0] not in self.supported_maps:
            raise LayoutError(
                f'Unsupported map: {value}; modded and custom maps are not supported'
            )
        return matches[0]

    def item_id(self, value, *, normalize=True):
        if not isinstance(value, str):
            raise LayoutError('Item ID must be a string')
        if value in CROP_ALIASES:
            value = DEFAULT_CROP
        if value in GIANT_CROP_ALIASES:
            value = DEFAULT_GIANT_CROP
        substitutions = self.normalized_ids if normalize else {}
        item_id = self.catalog['aliases'].get(value, value)
        item_id = substitutions.get(item_id, item_id)
        if item_id not in self.items:
            matches = {
                substitutions.get(key, key)
                for key, item in self.items.items()
                if value in [item['name_en'], item['name_zh']]
                and not item['hidden_in_menu']
            }
            if len(matches) != 1:
                raise LayoutError(f'Unknown or ambiguous item: {value}')
            item_id = matches.pop()
        if self.items[item_id]['is_planner_tool']:
            raise LayoutError(f'Planner tool cannot be placed: {item_id}')
        return base_item_id(self, item_id) if normalize else item_id

    def blocked(self, map_id):
        if map_id not in self._blocked:
            path = (
                self.root
                / 'data/maps'
                / self.maps[map_id]['restrictions_path']
            )
            restrictions = read_json(path)
            self._blocked[map_id] = {
                layer: {tuple(cell) for cell in cells}
                for layer, cells in restrictions['blocked'].items()
            }
            water = set(map(tuple, restrictions.get('water_tiles', [])))
            accessible_blocked = self._blocked[map_id]['accessible']
            info = self.maps[map_id]
            shoreline = {
                (x, y)
                for x, y in water
                if any(
                    0 <= x + dx < info['width_tiles']
                    and 0 <= y + dy < info['height_tiles']
                    and (x + dx, y + dy) not in accessible_blocked
                    for dx, dy in [(1, 0), (-1, 0), (0, 1), (0, -1)]
                )
            }
            self._blocked[map_id]['shore_water'] = {
                (x, y)
                for y in range(info['height_tiles'])
                for x in range(info['width_tiles'])
            } - shoreline
        return self._blocked[map_id]

    def dimensions(self, item_type):
        fp = self.items[item_type]['placement']['footprint']
        return fp['width'], fp['height']

    def omitted_entity_id(self, value):
        if not isinstance(value, str):
            return None
        if value in self.reference_entities:
            return value
        matches = [
            key
            for key, item in self.reference_entities.items()
            if value
            in [item['name_en'], item['name_zh'], *item.get('aliases', [])]
        ]
        return matches[0] if len(matches) == 1 else None

    def cells(self, item):
        width, height = self.dimensions(item['type'])
        return {
            (x, y)
            for y in range(item['y'], item['y'] + height)
            for x in range(item['x'], item['x'] + width)
        }

    def anchor(self, item):
        dx, dy = self.items[item['type']]['placement']['footprint'][
            'top_left_from_planner_anchor'
        ]
        return item['x'] - dx, item['y'] - dy

    def from_anchor(self, item_type, x, y):
        dx, dy = self.items[item_type]['placement']['footprint'][
            'top_left_from_planner_anchor'
        ]
        return x + dx, y + dy


def create_plan(dataset, layout, title='', season='spring'):
    layout = dataset.map_id(layout)
    if not isinstance(title, str):
        raise LayoutError('title must be a string')
    if not isinstance(season, str) or season not in SEASONS:
        raise LayoutError(f'Unsupported season: {season}')
    return {
        'schema_version': 2,
        'game_mode': 'vanilla',
        'title': title or dataset.maps[layout]['name_zh'],
        'layout': layout,
        'season': season,
        'coordinate_system': 'zero-based tiles; object x/y is footprint top-left',
        'planner_version': dataset.catalog['planner_version'],
        'objects': [
            {**copy.deepcopy(item), 'type': dataset.item_id(item['type'])}
            for item in dataset.maps[layout]['default_objects']
        ],
        'fixed_landmarks': copy.deepcopy(
            dataset.maps[layout].get('fixed_landmarks', [])
        ),
    }


def review_issues(plan):
    issues = plan.get('review_issues', [])
    if not isinstance(issues, list):
        raise LayoutError('review_issues must be an array')
    ids = set()
    for issue in issues:
        if (
            not isinstance(issue, dict)
            or not isinstance(issue.get('id'), str)
            or not issue['id'].strip()
            or issue['id'] in ids
        ):
            raise LayoutError('Each review issue needs a unique nonempty ID')
        ids.add(issue['id'])
        if issue.get('status') not in ['unresolved', 'resolved', 'accepted']:
            raise LayoutError(
                'Review status must be unresolved, resolved or accepted'
            )
        if issue.get('kind') not in [
            'identity',
            'position',
            'appearance',
            'coverage',
        ]:
            raise LayoutError(
                'Review kind must be identity, position, appearance or coverage'
            )
        if (
            not isinstance(issue.get('description'), str)
            or not issue['description'].strip()
        ):
            raise LayoutError('Review issues need a concrete description')
        if issue['status'] != 'unresolved' and not issue.get('resolution'):
            raise LayoutError(
                'Resolved or accepted issues must record the user decision or verification'
            )
        for key in ['object_ids', 'candidates']:
            values = issue.get(key, [])
            if not isinstance(values, list) or any(
                not isinstance(value, str) for value in values
            ):
                raise LayoutError(f'{key} must be an array of strings')
        box = issue.get('reference_box_px')
        if box is not None and (
            not isinstance(box, list)
            or len(box) != 4
            or any(type(v) is not int for v in box)
            or min(box[:2]) < 0
            or box[2] <= box[0]
            or box[3] <= box[1]
        ):
            raise LayoutError(
                'reference_box_px must be [left, top, right, bottom] in source image pixels'
            )
    return issues


def normalize_plan(dataset, raw):
    if (
        not isinstance(raw, dict)
        or type(raw.get('schema_version')) is not int
        or raw.get('schema_version') not in [1, 2]
    ):
        raise LayoutError('Expected layout schema_version 1 or 2')
    plan = copy.deepcopy(raw)
    require_vanilla(plan)
    review_issues(plan)
    plan['game_mode'] = 'vanilla'
    if not isinstance(plan.get('title', ''), str):
        raise LayoutError('title must be a string')
    plan['layout'] = dataset.map_id(plan.get('layout'))
    plan['season'] = plan.get('season', 'spring')
    if not isinstance(plan['season'], str) or plan['season'] not in SEASONS:
        raise LayoutError(f'Unsupported season: {plan["season"]}')
    if not isinstance(plan.get('objects'), list):
        raise LayoutError('objects must be an array')
    kept, omitted = [], Counter()
    for item in plan['objects']:
        if not isinstance(item, dict):
            raise LayoutError('Each object must be an object')
        if entity_id := dataset.omitted_entity_id(item.get('type')):
            record_omissions(plan, {entity_id: 1}, 'omitted_entities')
            continue
        normalize_crop(dataset, item)
        if ignored_vegetation(
            dataset.items[item['type']], dataset.recognition_config
        ):
            omitted[item['type']] += 1
        else:
            kept.append(item)
    plan['objects'] = kept
    if omitted:
        record_omissions(plan, omitted)
    plan.setdefault(
        'fixed_landmarks',
        copy.deepcopy(dataset.maps[plan['layout']].get('fixed_landmarks', [])),
    )
    if plan['schema_version'] == 1:
        present = {item['type'] for item in plan['objects']}
        for default in dataset.maps[plan['layout']]['default_objects']:
            family = (
                {'house', 'house-2', 'house-3'}
                if default['type'] == 'house'
                else {'greenhouse', 'greenhouse-repaired'}
                if default['type'] in ['greenhouse', 'greenhouse-repaired']
                else {default['type']}
            )
            if not present & family:
                plan['objects'].append(
                    {
                        **copy.deepcopy(default),
                        'type': dataset.item_id(default['type']),
                    }
                )
    plan['schema_version'] = 2
    plan['coordinate_system'] = (
        'zero-based tiles; object x/y is footprint top-left'
    )
    return plan


def can_overlap(dataset, left, right):
    a, b = dataset.items[left['type']], dataset.items[right['type']]
    if porch_overlap(dataset, left, right) or porch_overlap(
        dataset, right, left
    ):
        return True
    if a['subcategory'] == 'flooring' or b['subcategory'] == 'flooring':
        other = b if a['subcategory'] == 'flooring' else a
        return flooring_overlap(other)['can_place_on_flooring']
    tappers = {'tapper', 'heavy-tapper'}
    single_tile_trees = {
        'oak-tree',
        'maple-tree',
        'pine-tree',
        'mahogany-tree',
        'mystic-tree',
    }
    if (left['type'] in tappers and right['type'] in single_tile_trees) or (
        right['type'] in tappers and left['type'] in single_tile_trees
    ):
        return (left['x'], left['y']) == (right['x'], right['y'])
    return (a['subcategory'] == 'hoe-dirt' and b['category'] == 'crop') or (
        b['subcategory'] == 'hoe-dirt' and a['category'] == 'crop'
    )


def validate_plan(dataset, plan):
    errors, warnings = [], []
    require_vanilla(plan)
    layout = dataset.maps[plan['layout']]
    expected_landmarks = layout.get('fixed_landmarks', [])
    if plan.get('fixed_landmarks', expected_landmarks) != expected_landmarks:
        errors.append(
            {
                'code': 'fixed_landmarks_modified',
                'message': 'Map reference coordinates cannot be moved, removed or replaced',
            }
        )
    landmark_cells = {
        tuple(cell)
        for landmark in expected_landmarks
        for cell in landmark['tiles']
    }
    blocked = dataset.blocked(plan['layout'])
    ids = set()
    occupied = defaultdict(list)
    collision_pairs = defaultdict(list)
    flooring_pairs = defaultdict(list)
    porch_pairs = defaultdict(list)
    hosts = [
        item
        for item in plan['objects']
        if item.get('type') in dataset.items
        and all(type(item.get(axis)) is int for axis in ['x', 'y'])
        and porch_cells(dataset.items[item['type']])
    ]
    for index, item in enumerate(plan['objects']):
        item_id = item.get('id')
        if not isinstance(item_id, str) or not item_id:
            errors.append({'code': 'invalid_id', 'index': index})
            continue
        if item_id in ids:
            errors.append({'code': 'duplicate_id', 'id': item_id})
        ids.add(item_id)
        try:
            integer(item.get('x'), f'{item_id}.x')
            integer(item.get('y'), f'{item_id}.y')
            metadata = dataset.items[item['type']]
        except (LayoutError, KeyError) as error:
            errors.append(
                {
                    'code': 'invalid_object',
                    'id': item_id,
                    'message': str(error),
                }
            )
            continue
        if not isinstance(item.get('tags', []), list) or any(
            not isinstance(tag, str) for tag in item.get('tags', [])
        ):
            errors.append({'code': 'invalid_tags', 'id': item_id})
        if metadata['is_planner_tool']:
            errors.append({'code': 'planner_tool', 'id': item_id})
        if ignored_vegetation(metadata, dataset.recognition_config):
            errors.append({'code': 'excluded_grass_object', 'id': item_id})
        if item['type'] == 'greenhouse':
            errors.append(
                {
                    'code': 'unrepaired_greenhouse',
                    'id': item_id,
                    'message': 'Use greenhouse-repaired, footprint 7 x 6',
                }
            )
        rendered_id = seasonal_id(dataset, item['type'], plan['season'])
        if scope_error := item_scope_error(layout, metadata):
            errors.append(
                {'code': scope_error, 'id': item_id, 'type': item['type']}
            )
        note = appearance_note(dataset, rendered_id)
        if note:
            warnings.append(
                {
                    'code': 'unverified_appearance',
                    'id': item_id,
                    'sprite_id': rendered_id,
                    **note,
                }
            )
        crop_kind = item.get('crop_kind')
        if crop_kind is not None and (
            metadata['category'] != 'crop'
            or crop_kind not in ['placeholder', 'specified']
        ):
            errors.append({'code': 'invalid_crop_kind', 'id': item_id})
        if crop_kind == 'placeholder' and item['type'] not in {
            DEFAULT_CROP,
            DEFAULT_GIANT_CROP,
        }:
            errors.append(
                {'code': 'noncanonical_crop_placeholder', 'id': item_id}
            )
        cells = dataset.cells(item)
        if cells & landmark_cells and metadata['subcategory'] != 'flooring':
            errors.append(
                {
                    'code': 'fixed_landmark_obstructed',
                    'id': item_id,
                    'cells': sorted(cells & landmark_cells),
                }
            )
        outside = sorted(
            (x, y)
            for x, y in cells
            if not (
                0 <= x < layout['width_tiles']
                and 0 <= y < layout['height_tiles']
            )
        )
        if outside:
            errors.append(
                {'code': 'out_of_bounds', 'id': item_id, 'cells': outside}
            )
        # Objects without a declared layer use accessible terrain as a
        # conservative local rule, so ordinary objects cannot be put on water.
        layer = placement_layer(layout, metadata)
        # Porch tiles provide their own surface over the underlying map.
        supported = set().union(
            *(
                porch_cells(dataset.items[host['type']], host['x'], host['y'])
                for host in hosts
                if porch_overlap(dataset, host, item)
            )
        )
        forbidden = sorted((cells - supported) & blocked[layer])
        unchanged_default = any(
            item['type'] == default['type']
            and item['x'] == default['x']
            and item['y'] == default['y']
            for default in layout['default_objects']
        )
        if forbidden and unchanged_default:
            warnings.append(
                {
                    'code': 'existing_default_terrain_exception',
                    'id': item_id,
                    'cells': forbidden,
                }
            )
            forbidden = []
        if forbidden:
            errors.append(
                {
                    'code': 'terrain_restriction',
                    'id': item_id,
                    'layer': layer,
                    'cells': forbidden,
                }
            )
        for cell in cells:
            for other in occupied[cell]:
                if not can_overlap(dataset, item, other):
                    pair = tuple(sorted((item_id, other['id'])))
                    collision_pairs[pair].append(cell)
                elif metadata['subcategory'] == 'flooring':
                    flooring_pairs[(item_id, other['id'])].append(cell)
                elif dataset.items[other['type']]['subcategory'] == 'flooring':
                    flooring_pairs[(other['id'], item_id)].append(cell)
                elif porch_overlap(dataset, item, other):
                    porch_pairs[(item_id, other['id'])].append(cell)
                elif porch_overlap(dataset, other, item):
                    porch_pairs[(other['id'], item_id)].append(cell)
            occupied[cell].append(item)
        if (
            metadata['category'] == 'crop'
            and metadata['seasons']
            and plan['season'] not in metadata['seasons']
            and item.get('crop_kind') != 'placeholder'
        ):
            warnings.append(
                {
                    'code': 'crop_season_mismatch',
                    'id': item_id,
                    'season': plan['season'],
                    'known_seasons': metadata['seasons'],
                }
            )
    errors.extend(
        {'code': 'overlap', 'ids': list(pair), 'cells': sorted(set(cells))}
        for pair, cells in collision_pairs.items()
    )
    errors.extend(additional_placement_errors(dataset, plan, occupied))
    for default in layout['default_objects']:
        family = (
            {'house', 'house-2', 'house-3'}
            if default['type'] == 'house'
            else {'greenhouse', 'greenhouse-repaired'}
            if default['type'] in ['greenhouse', 'greenhouse-repaired']
            else {default['type']}
        )
        if 'fixed' in default.get('tags', []):
            matching = [
                item for item in plan['objects'] if item['id'] == default['id']
            ]
            if len(matching) != 1 or any(
                matching[0].get(key) != default[key]
                for key in ['type', 'x', 'y']
            ):
                errors.append(
                    {'code': 'fixed_object_modified', 'id': default['id']}
                )
        if not any(item['type'] in family for item in plan['objects']):
            warnings.append(
                {
                    'code': 'missing_default_object',
                    'family': sorted(family),
                }
            )
    if plan['layout'] == 'beach' and any(
        (dataset.items[item['type']]['effect'] or {}).get('group')
        == 'sprinkler'
        for item in plan['objects']
    ):
        warnings.append(
            {
                'code': 'beach_sand_sprinkler_rule_unverified',
                'message': 'Geometric coverage does not confirm whether sprinklers work on the selected beach tiles.',
            }
        )
    return {
        'passed': not errors,
        'layout': plan['layout'],
        'object_count': len(plan['objects']),
        'errors': errors,
        'warnings': warnings,
        'flooring_overlaps': [
            {
                'flooring_id': flooring_id,
                'object_id': object_id,
                'cells': sorted(set(cells)),
            }
            for (flooring_id, object_id), cells in sorted(
                flooring_pairs.items()
            )
        ],
        'porch_overlaps': [
            {
                'building_id': building_id,
                'object_id': object_id,
                'cells': sorted(set(cells)),
            }
            for (building_id, object_id), cells in sorted(porch_pairs.items())
        ],
        'scope': 'Map bounds, item footprints, overlap and bundled placement restrictions; not a complete gameplay simulation.',
    }


def require_valid(dataset, plan):
    report = validate_plan(dataset, plan)
    if not report['passed']:
        raise LayoutError(json.dumps(report, ensure_ascii=False))
    return report


def next_id(plan, item_type):
    ids = {item['id'] for item in plan['objects']}
    index = 1
    while f'{item_type}-{index}' in ids:
        index += 1
    return f'{item_type}-{index}'


def add_object(dataset, plan, value):
    item = copy.deepcopy(value)
    normalize_crop(dataset, item)
    if ignored_vegetation(
        dataset.items[item['type']], dataset.recognition_config
    ):
        raise LayoutError(
            'Grass, blue grass and weeds are omitted from layouts'
        )
    if 'id' not in item:
        item['id'] = next_id(plan, item['type'])
    if any(other['id'] == item['id'] for other in plan['objects']):
        raise LayoutError(f'Duplicate object ID: {item["id"]}')
    integer(item.get('x'), 'x')
    integer(item.get('y'), 'y')
    plan['objects'].append(item)
    return item


def apply_operations(dataset, original, operations):
    plan = copy.deepcopy(original)
    if not isinstance(operations, list):
        raise LayoutError('operations must be an array')
    for operation in operations:
        if not isinstance(operation, dict):
            raise LayoutError('Each operation must be an object')
        kind = operation.get('op')
        if kind == 'add':
            add_object(dataset, plan, operation['item'])
        elif kind in ['update', 'remove']:
            matches = [
                item
                for item in plan['objects']
                if item['id'] == operation.get('id')
            ]
            if len(matches) != 1:
                raise LayoutError(
                    f'Object ID not found: {operation.get("id")}'
                )
            item = matches[0]
            if kind == 'remove':
                plan['objects'].remove(item)
            else:
                changes = operation.get('set', {})
                if not isinstance(changes, dict) or set(changes) - {
                    'x',
                    'y',
                    'type',
                    'tags',
                    'label',
                    'crop_kind',
                }:
                    raise LayoutError(
                        'update supports x, y, type, tags, label and crop_kind'
                    )
                if 'type' in changes and 'crop_kind' not in changes:
                    item.pop('crop_kind', None)
                item.update(copy.deepcopy(changes))
                normalize_crop(dataset, item)
        elif kind == 'fill':
            template = normalize_crop(
                dataset,
                {
                    'type': operation['type'],
                    **(
                        {'crop_kind': operation['crop_kind']}
                        if 'crop_kind' in operation
                        else {}
                    ),
                },
            )
            item_type = template['type']
            x, y, width, height = operation['rect']
            for name, number in zip(
                ['x', 'y', 'width', 'height'],
                [x, y, width, height],
                strict=True,
            ):
                integer(number, name)
            if width < 1 or height < 1:
                raise LayoutError(
                    'fill rectangle must have positive dimensions'
                )
            map_info = dataset.maps[plan['layout']]
            if (
                min(x, y) < 0
                or x + width > map_info['width_tiles']
                or y + height > map_info['height_tiles']
            ):
                raise LayoutError('fill rectangle is outside the map')
            step_x, step_y = operation.get(
                'step', dataset.dimensions(item_type)
            )
            integer(step_x, 'step x')
            integer(step_y, 'step y')
            if min(step_x, step_y) < 1:
                raise LayoutError('fill step must be positive')
            excluded = {tuple(cell) for cell in operation.get('exclude', [])}
            object_width, object_height = dataset.dimensions(item_type)
            if object_width > width or object_height > height:
                raise LayoutError(
                    'fill rectangle is smaller than the object footprint'
                )
            for row in range(y, y + height - object_height + 1, step_y):
                for column in range(x, x + width - object_width + 1, step_x):
                    if (column, row) not in excluded:
                        add_object(
                            dataset,
                            plan,
                            {
                                **template,
                                'type': item_type,
                                'x': column,
                                'y': row,
                                'tags': operation.get('tags', []),
                            },
                        )
        else:
            raise LayoutError(f'Unknown operation: {kind}')
    require_valid(dataset, plan)
    return plan


def inventory(dataset, plan):
    grouped = defaultdict(list)
    for item in plan['objects']:
        grouped[(item['type'], item.get('crop_kind', ''))].append(item)
    return {
        'layout': plan['layout'],
        'title': plan.get('title'),
        'object_count': len(plan['objects']),
        'items': [
            {
                'type': item_type,
                'name_zh': (
                    '巨型作物（南瓜示意）'
                    if item_type == DEFAULT_GIANT_CROP
                    else '种植位（草莓示意）'
                )
                if crop_kind == 'placeholder'
                else dataset.items[item_type]['name_zh'],
                'name_en': (
                    'Giant crops (pumpkin placeholder)'
                    if item_type == DEFAULT_GIANT_CROP
                    else 'Crop positions (strawberry placeholder)'
                )
                if crop_kind == 'placeholder'
                else dataset.items[item_type]['name_en'],
                'count': len(values),
                'existing_count': sum(
                    'default' in item.get('tags', [])
                    or 'existing' in item.get('tags', [])
                    for item in values
                ),
                'planned_count': sum(
                    'default' not in item.get('tags', [])
                    and 'existing' not in item.get('tags', [])
                    for item in values
                ),
                'crop_kind': crop_kind or None,
                'image': dataset.items[item_type]['image']['path'],
                'footprint_tiles': list(dataset.dimensions(item_type)),
                'objects': [
                    {'id': item['id'], 'x': item['x'], 'y': item['y']}
                    for item in values
                ],
            }
            for (item_type, crop_kind), values in sorted(grouped.items())
        ],
    }


def generate_plan(dataset, layout, recipe, title='', season='spring'):
    require_vanilla(recipe)
    if recipe.get('schema_version') != 1 or not isinstance(
        recipe.get('modules'), list
    ):
        raise LayoutError(
            'Expected recipe schema_version 1 with a modules array'
        )
    plan = create_plan(
        dataset, layout, title or recipe.get('title', ''), season
    )
    if 'coverage_targets' in recipe:
        plan['coverage_targets'] = copy.deepcopy(recipe['coverage_targets'])
    placed = []
    map_info = dataset.maps[plan['layout']]
    center_x, center_y = (
        map_info['width_tiles'] // 2,
        map_info['height_tiles'] // 2,
    )
    names = set()
    occupied = set().union(*(dataset.cells(item) for item in plan['objects']))
    blocked = dataset.blocked(plan['layout'])
    for index, module in enumerate(recipe['modules']):
        name = module.get('id', f'module-{index + 1}')
        if name in names:
            raise LayoutError(f'Duplicate module ID: {name}')
        names.add(name)
        relative = []
        omitted = Counter()
        omitted_entities = Counter()
        for item in module.get('objects', []):
            value = copy.deepcopy(item)
            if entity_id := dataset.omitted_entity_id(value.get('type')):
                omitted_entities[entity_id] += 1
                continue
            normalize_crop(dataset, value)
            if ignored_vegetation(
                dataset.items[value['type']], dataset.recognition_config
            ):
                omitted[value['type']] += 1
                continue
            integer(value.get('x'), 'module x')
            integer(value.get('y'), 'module y')
            relative.append(value)
        if omitted:
            record_omissions(plan, omitted)
        record_omissions(plan, omitted_entities, 'omitted_entities')
        if not relative and (omitted or omitted_entities):
            continue
        if not relative:
            raise LayoutError(f'Module {name} is empty')
        for first_index, first in enumerate(relative):
            for second in relative[first_index + 1 :]:
                if dataset.cells(first) & dataset.cells(
                    second
                ) and not can_overlap(dataset, first, second):
                    raise LayoutError(
                        f'Overlapping objects within module {name}'
                    )
        all_cells = set().union(*(dataset.cells(item) for item in relative))
        min_x, min_y = (
            min(x for x, _ in all_cells),
            min(y for _, y in all_cells),
        )
        max_x, max_y = (
            max(x for x, _ in all_cells),
            max(y for _, y in all_cells),
        )
        margin = integer(module.get('clearance', 1), 'clearance')
        if margin < 0:
            raise LayoutError('clearance cannot be negative')
        pref_x, pref_y = module.get('preferred_offset', [0, 0])
        integer(pref_x, 'preferred x')
        integer(pref_y, 'preferred y')
        origins = [
            (x, y)
            for y in range(-min_y, map_info['height_tiles'] - max_y)
            for x in range(-min_x, map_info['width_tiles'] - max_x)
        ]
        origins.sort(
            key=lambda pos: (
                math.dist(pos, (center_x + pref_x, center_y + pref_y)),
                pos[1],
                pos[0],
            )
        )
        chosen = None
        for origin_x, origin_y in origins:
            cells = {(x + origin_x, y + origin_y) for x, y in all_cells}
            padded = {
                (x + dx, y + dy)
                for x, y in cells
                for dx in range(-margin, margin + 1)
                for dy in range(-margin, margin + 1)
            }
            if padded & occupied:
                continue
            if any(
                {(x + origin_x, y + origin_y) for x, y in dataset.cells(item)}
                & blocked[
                    placement_layer(map_info, dataset.items[item['type']])
                ]
                for item in relative
            ):
                continue
            candidate = copy.deepcopy(plan)
            new_items = []
            for number, item in enumerate(relative, 1):
                value = {
                    **item,
                    'id': f'{name}-{number}',
                    'x': item['x'] + origin_x,
                    'y': item['y'] + origin_y,
                    'tags': list(dict.fromkeys([*item.get('tags', []), name])),
                }
                new_items.append(add_object(dataset, candidate, value))
            if validate_plan(dataset, candidate)['passed']:
                plan = candidate
                chosen = [origin_x, origin_y]
                occupied |= cells
                placed.append(
                    {
                        'module': name,
                        'origin': chosen,
                        'object_ids': [item['id'] for item in new_items],
                    }
                )
                break
        if chosen is None:
            raise LayoutError(
                f'No valid position found for module {name} on {plan["layout"]}'
            )
    plan['generation'] = {
        'method': 'nearest-valid-module-placement',
        'modules': placed,
    }
    require_valid(dataset, plan)
    return plan
