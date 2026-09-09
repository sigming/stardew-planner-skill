def fixed_landmarks(map_id, accessible_layer):
    """Use immutable transition tiles as alignment references."""
    pending = {
        tuple(map(int, value.split(',')))
        for value in accessible_layer['accessible.warp']['Tiles']
    }
    groups = []
    while pending:
        first = min(pending)
        pending.remove(first)
        group, stack = {first}, [first]
        while stack:
            x, y = stack.pop()
            for neighbor in [(x - 1, y), (x + 1, y), (x, y - 1), (x, y + 1)]:
                if neighbor in pending:
                    pending.remove(neighbor)
                    group.add(neighbor)
                    stack.append(neighbor)
        groups.append(sorted(group))
    cave_tiles = {
        'regular': (34, 5),
        'fishing': (34, 5),
        'foraging': (34, 5),
        'mining': (34, 5),
        'combat': (34, 5),
        'fourcorners': (30, 35),
        'beach': (34, 15),
        'ranching': (88, 54),
    }
    result = []
    for index, cells in enumerate(groups, 1):
        x, y = min(cells)
        is_cave = cave_tiles.get(map_id) in cells
        result.append(
            {
                'id': 'farm-cave' if is_cave else f'fixed-passage-{index}',
                'name_zh': '农场洞穴入口' if is_cave else f'固定通道 {index}',
                'name_en': 'Farm cave entrance'
                if is_cave
                else f'Fixed passage {index}',
                'fixed': True,
                'x': x,
                'y': y,
                'tiles': [list(cell) for cell in cells],
                'source_layer': 'accessible.warp',
            }
        )
    return sorted(
        result,
        key=lambda item: (item['id'] != 'farm-cave', item['y'], item['x']),
    )


def edge_references(map_info, blocked, per_side=4):
    """Pick stable terrain cells next to usable ground on each map side.

    These are alignment marks, not game objects or reserved placement tiles.
    The choice depends only on immutable map restrictions, never on the plan.
    """
    width, height = map_info['width_tiles'], map_info['height_tiles']
    unavailable = blocked['accessible'] | blocked['buildable']
    usable = {
        (x, y) for y in range(height) for x in range(width)
    } - unavailable
    directions = [('N', 0, 1), ('E', -1, 0), ('S', 0, -1), ('W', 1, 0)]
    references = []
    used = set()
    for side, dx, dy in directions:
        length = width if side in ['N', 'S'] else height
        depth = height if side in ['N', 'S'] else width
        candidates = []
        for tangent in range(1, length - 1):
            for step in range(depth):
                x, y = (
                    (tangent, step)
                    if side == 'N'
                    else (width - 1 - step, tangent)
                    if side == 'E'
                    else (tangent, height - 1 - step)
                    if side == 'S'
                    else (step, tangent)
                )
                if (x, y) not in usable:
                    continue
                outside = (x - dx, y - dy)
                if outside in usable:
                    continue
                patch = {
                    (
                        x + dx * inward + dy * across,
                        y + dy * inward - dx * across,
                    )
                    for inward in range(3)
                    for across in [-1, 0, 1]
                }
                if not patch <= usable:
                    continue
                point = (
                    outside
                    if 0 <= outside[0] < width and 0 <= outside[1] < height
                    else (x, y)
                )
                candidates.append(
                    {'point': point, 'adjacent': (x, y), 'tangent': tangent}
                )
                break
        if not candidates:
            continue

        def distance_from_side(value, side=side):
            x, y = value['point']
            return (
                y
                if side == 'N'
                else width - 1 - x
                if side == 'E'
                else height - 1 - y
                if side == 'S'
                else x
            )

        side_limit = max(
            min(map(distance_from_side, candidates)) + 6, depth * 0.35
        )
        near_edge = [
            value
            for value in candidates
            if distance_from_side(value) <= side_limit
        ]
        if len(near_edge) >= per_side:
            candidates = near_edge
        lower = min(value['tangent'] for value in candidates)
        upper = max(value['tangent'] for value in candidates)
        chosen = []
        for index in range(per_side):
            target = lower + (upper - lower) * (index + 0.5) / per_side
            available = [
                value
                for value in candidates
                if value['point'] not in used
                and all(
                    abs(value['tangent'] - other['tangent'])
                    >= max(2, length // 12)
                    for other in chosen
                )
            ]
            if not available:
                available = [
                    value
                    for value in candidates
                    if value['point'] not in used and value not in chosen
                ]
            if not available:
                continue
            value = min(
                available,
                key=lambda value: (
                    abs(value['tangent'] - target),
                    value['tangent'],
                ),
            )
            chosen.append(value)
            used.add(value['point'])
        for index, value in enumerate(
            sorted(chosen, key=lambda value: value['tangent']), 1
        ):
            x, y = value['point']
            references.append(
                {
                    'id': f'{side}{index}',
                    'side': side,
                    'x': x,
                    'y': y,
                    'adjacent_usable_tile': list(value['adjacent']),
                    'kind': 'terrain_alignment_reference',
                    'source': 'accessible and buildable map restrictions',
                }
            )
    return references
