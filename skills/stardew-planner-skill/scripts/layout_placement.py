"""Placement overlaps and construction check areas."""

from map_rules import WALL_FURNITURE_TYPES, furniture_type


def flooring_overlap(metadata):
    """Describe the same rule used by validation and recognition labels."""
    item_id = (metadata.get('variant') or {}).get('base_id', metadata['id'])
    source_urls = ['https://stardewvalleywiki.com/Hoes#Notes']
    if metadata.get('subcategory') == 'flooring':
        reason = 'flooring_base'
        note = '地板和道路作为下层保留；同格只能有一块铺装。'
    elif item_id == 'crab-pot':
        reason = 'requires_water'
        note = '捕蟹笼需要放在岸边水域，不能放在地板上；仍需保留此物品。'
        source_urls = ['https://stardewvalleywiki.com/Crab_Pot']
    elif item_id in {'tapper', 'heavy-tapper'}:
        reason = 'requires_tree'
        note = '树液采集器需要附着在树上，不能作为地板摆件；仍需保留此物品。'
        source_urls = ['https://stardewvalleywiki.com/Tapper']
    elif furniture_type(metadata) in WALL_FURNITURE_TYPES:
        reason = 'requires_wall'
        note = '壁饰需要放在室内墙面，不能作为地板摆件；仍需保留此物品。'
        source_urls = ['https://stardewvalleywiki.com/Furniture']
    elif not metadata.get('is_planner_tool') and (
        metadata.get('category') in {'craftable', 'furniture'}
        or metadata.get('subcategory') in {'fence', 'gate'}
    ):
        reason = 'object_on_flooring'
        note = '可与各种地板、道路共用格子；上层物品和下层铺装均须保留，分别计数。不能与另一件上层物品重叠；地形和室内外限制仍适用。'
    else:
        reason = 'not_supported'
        note = '本规划器未允许此物品与地板共用格子。'
        source_urls = []
    return {
        'can_place_on_flooring': reason == 'object_on_flooring',
        'preserve_flooring': reason in {'object_on_flooring', 'flooring_base'},
        'reason': reason,
        'note_zh': note,
        'source_urls': source_urls,
    }


def rectangle_cells(rect):
    x, y, width, height = rect
    return {
        (column, row)
        for row in range(y, y + height)
        for column in range(x, x + width)
    }


def porch_cells(metadata, x=0, y=0):
    return {
        (x + dx, y + dy)
        for dx, dy in metadata.get('building_placement', {}).get(
            'porch_object_cells', []
        )
    }


def porch_overlap(dataset, building, other):
    """Allow floor-standing objects only on the building's usable porch."""
    metadata = dataset.items[building['type']]
    if (
        not porch_cells(metadata)
        or not flooring_overlap(dataset.items[other['type']])[
            'can_place_on_flooring'
        ]
    ):
        return False
    shared = dataset.cells(building) & dataset.cells(other)
    return bool(shared) and shared <= porch_cells(
        metadata, building['x'], building['y']
    )


def render_order(dataset, plan):
    """Keep depth order, drawing a porch's host before its contents."""
    ordered = sorted(
        plan['objects'],
        key=lambda item: (
            dataset.items[item['type']]['subcategory']
            not in ['flooring', 'hoe-dirt'],
            dataset.anchor(item)[1],
            item['type'] in ['tapper', 'heavy-tapper'],
            item['id'],
        ),
    )
    hosts = [
        item for item in ordered if porch_cells(dataset.items[item['type']])
    ]
    drawn = set()
    result = []
    for item in ordered:
        for candidate in [
            *(host for host in hosts if porch_overlap(dataset, host, item)),
            item,
        ]:
            if candidate['id'] not in drawn:
                drawn.add(candidate['id'])
                result.append(candidate)
    return result


def placement_areas(metadata, x=0, y=0):
    footprint = metadata['placement']['footprint']
    solid = [x, y, footprint['width'], footprint['height']]
    additional = []
    for area in metadata.get('building_placement', {}).get(
        'additional_placement_tiles', []
    ):
        left, top, width, height = area['tile_area']
        additional.append(
            {
                'tile_rect': [x + left, y + top, width, height],
                'requirement': 'passable'
                if area['only_needs_to_be_passable']
                else 'buildable',
            }
        )
    return {'solid_rect': solid, 'additional_areas': additional}


def placement_label(metadata):
    footprint = metadata['placement']['footprint']
    parts = [f'{footprint["width"]}×{footprint["height"]}']
    parts.extend(
        f'{area["tile_rect"][2]}×{area["tile_rect"][3]}'
        for area in placement_areas(metadata)['additional_areas']
    )
    return ' + '.join(parts)


def allows_clearance(metadata, requirement):
    # Ground coverings do not obstruct construction. Ordinary crop placeholders
    # and rugs can be walked through, but do not establish buildable ground.
    if metadata['subcategory'] in ['flooring', 'hoe-dirt']:
        return True
    return requirement == 'passable' and (
        (
            metadata['category'] == 'crop'
            and metadata['subcategory'] != 'giant-crops'
        )
        or metadata.get('furniture_type') == 'rug'
    )


def additional_placement_errors(dataset, plan, occupied):
    info = dataset.maps[plan['layout']]
    blocked = dataset.blocked(plan['layout'])
    errors = []
    for item in plan['objects']:
        if item.get('type') not in dataset.items or any(
            type(item.get(axis)) is not int for axis in ['x', 'y']
        ):
            continue
        areas = placement_areas(
            dataset.items[item['type']], item['x'], item['y']
        )
        for area in areas['additional_areas']:
            cells = rectangle_cells(area['tile_rect'])
            outside = {
                cell
                for cell in cells
                if not (
                    0 <= cell[0] < info['width_tiles']
                    and 0 <= cell[1] < info['height_tiles']
                )
            }
            if outside:
                errors.append(
                    {
                        'code': 'additional_placement_out_of_bounds',
                        'id': item['id'],
                        'cells': sorted(outside),
                    }
                )
            layer = (
                'accessible'
                if area['requirement'] == 'passable'
                else 'buildable'
            )
            forbidden = cells & blocked[layer]
            if forbidden:
                errors.append(
                    {
                        'code': 'additional_placement_terrain',
                        'id': item['id'],
                        'requirement': area['requirement'],
                        'cells': sorted(forbidden),
                    }
                )
            obstructions = {}
            for cell in cells:
                for other in occupied.get(cell, []):
                    if other['id'] != item['id'] and not allows_clearance(
                        dataset.items[other['type']], area['requirement']
                    ):
                        obstructions.setdefault(other['id'], []).append(cell)
            for other_id, overlap in obstructions.items():
                errors.append(
                    {
                        'code': 'additional_placement_obstructed',
                        'id': item['id'],
                        'obstruction_id': other_id,
                        'requirement': area['requirement'],
                        'cells': sorted(overlap),
                    }
                )
    return errors
