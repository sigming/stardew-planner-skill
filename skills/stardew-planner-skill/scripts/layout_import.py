from collections import Counter

from layout_core import (
    LayoutError,
    add_object,
    create_plan,
    integer,
    record_omissions,
    require_valid,
    require_vanilla,
)
from recognition_categories import ignored_vegetation


def default_family(item_type):
    if item_type in {'house', 'house-2', 'house-3'}:
        return {'house', 'house-2', 'house-3'}
    if item_type in {'greenhouse', 'greenhouse-repaired'}:
        return {'greenhouse', 'greenhouse-repaired'}
    return {item_type}


def import_planner(dataset, payload, title='Imported planner layout'):
    if not isinstance(payload, dict) or not isinstance(
        payload.get('options', {}), dict
    ):
        raise LayoutError('Invalid planner payload')
    options = payload.get('options', {})
    require_vanilla(payload)
    require_vanilla(options)
    plan = create_plan(
        dataset,
        options.get('layout', 'regular'),
        title,
        options.get('season', 'spring'),
    )
    defaults = plan['objects']
    plan['objects'] = []
    omitted = Counter()
    for group in ['buildings', 'tiles', 'flooring']:
        if not isinstance(payload.get(group, []), list):
            raise LayoutError(f'{group} must be an array')
        for item in payload.get(group, []):
            if entity_id := dataset.omitted_entity_id(item['type']):
                record_omissions(plan, {entity_id: 1}, 'omitted_entities')
                continue
            item_type = dataset.item_id(item['type'])
            if ignored_vegetation(
                dataset.items[item_type], dataset.recognition_config
            ):
                omitted[item_type] += 1
                continue
            x, y = (
                integer(item['x'], 'planner x'),
                integer(item['y'], 'planner y'),
            )
            if x % 16 or y % 16:
                raise LayoutError(
                    'Planner coordinates must be divisible by 16'
                )
            x, y = dataset.from_anchor(item_type, x // 16, y // 16)
            value = {'type': item_type, 'x': x, 'y': y}
            default = next(
                (
                    candidate
                    for candidate in defaults
                    if item_type in default_family(candidate['type'])
                ),
                None,
            )
            if default is not None:
                value['tags'] = list(default.get('tags', ['default']))
                if (x, y) == (default['x'], default['y']):
                    value['id'] = default['id']
            add_object(dataset, plan, value)
    record_omissions(plan, omitted)
    types = {item['type'] for item in plan['objects']}
    for default in defaults:
        family = default_family(default['type'])
        if not types & family:
            plan['objects'].append(default)
    require_valid(dataset, plan)
    return plan
