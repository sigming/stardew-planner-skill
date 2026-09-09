import math

from PIL import Image

SEASONS = ('spring', 'summer', 'fall', 'winter')

COMPASS = {
    0: '',
    15: 'c',
    14: 'n',
    13: 's',
    11: 'e',
    7: 'w',
    9: 'se',
    5: 'sw',
    10: 'ne',
    6: 'nw',
    12: 'hc',
    3: 'vc',
    1: 'vs',
    2: 'vn',
    4: 'hw',
    8: 'he',
}
FENCES = {
    **COMPASS,
    15: 'hc',
    14: 'hc',
    13: 'hc',
    11: 'se',
    7: 'sw',
    10: 'he',
    6: 'hw',
}


def resolve_effects(items):
    for item in items.values():
        if source_id := item.get('effect_source_id'):
            item['effect'] = items[source_id]['effect']


def base_item_id(dataset, item_type):
    base = seasonal_base_id(dataset, item_type)
    relation = dataset.items[base].get('variant') or {}
    if relation.get('kind') == 'connection':
        return relation['base_id']
    return base


def connection_id(dataset, item, occupied):
    item_type = base_item_id(dataset, item['type'])
    metadata = dataset.items[item_type]
    subgroup = metadata['subcategory']
    if subgroup == 'giant-crops':
        return item['type']
    if (
        subgroup not in ['flooring', 'hoe-dirt', 'fence', 'gate']
        and metadata['category'] != 'crop'
    ):
        return item['type']
    bits = 0
    for bit, point in [
        (1, (item['x'], item['y'] - 1)),
        (2, (item['x'], item['y'] + 1)),
        (4, (item['x'] + 1, item['y'])),
        (8, (item['x'] - 1, item['y'])),
    ]:
        neighbors = occupied.get(point, [])
        if any(
            base_item_id(dataset, other['type']) == item_type
            or (
                subgroup in ['fence', 'gate']
                and dataset.items[other['type']]['subcategory']
                in ['fence', 'gate']
            )
            for other in neighbors
        ):
            bits |= bit
    if subgroup == 'gate':
        suffix = 'vc' if bits & 3 else ''
    else:
        suffix = (FENCES if subgroup == 'fence' else COMPASS)[bits]
    base = 'hoe-dirt' if metadata['category'] == 'crop' else item_type
    candidate = base + '-' + suffix if suffix else base
    return candidate if candidate in dataset.items else base


def seasonal_base_id(dataset, item_type):
    # Some upstream seasonSprite records have the wrong parent/season.
    # A seasonal filename with an existing base is unambiguous locally.
    base, _, suffix = item_type.rpartition('-')
    if suffix in SEASONS and base in dataset.items:
        return base
    relation = dataset.items[item_type].get('variant') or {}
    if relation.get('kind') == 'season':
        return relation['base_id']
    return item_type


def seasonal_id(dataset, item_type, season):
    base = seasonal_base_id(dataset, item_type)
    candidate = f'{base}-{season}'
    return candidate if candidate in dataset.items else base


def appearance_note(dataset, item_type):
    note = dataset.appearance_notes.get(item_type)
    if (
        note
        and note.get('image_sha256')
        == dataset.items[item_type]['image']['sha256']
    ):
        return note
    return None


def appearances(dataset, item_type):
    base = base_item_id(dataset, item_type)
    seasonal = any(f'{base}-{season}' in dataset.items for season in SEASONS)
    result = (
        [
            {'id': seasonal_id(dataset, base, season), 'season': season}
            for season in SEASONS
        ]
        if seasonal
        else [{'id': base}]
    )
    known = {entry['id'] for entry in result}
    pending = list(dataset.items[base]['variant_ids'])
    while pending:
        variant = pending.pop(0)
        relation = dataset.items[variant].get('variant') or {}
        if relation.get('kind') != 'season' and variant not in known:
            result.append({'id': variant, 'kind': relation.get('kind')})
            known.add(variant)
            pending[0:0] = dataset.items[variant]['variant_ids']
    return [
        {
            **entry,
            'image': dataset.items[entry['id']]['image']['path'],
            'footprint_tiles': [
                dataset.items[entry['id']]['placement']['footprint'][axis]
                for axis in ['width', 'height']
            ],
            **(
                {'note': appearance_note(dataset, entry['id'])}
                if appearance_note(dataset, entry['id'])
                else {}
            ),
        }
        for entry in result
    ]


def sprite(dataset, item_type, atlas=None):
    if atlas is None or not dataset.items[item_type]['image'].get(
        'atlas_rect_px'
    ):
        path = (
            dataset.root
            / 'data/planner'
            / dataset.items[item_type]['image']['path']
        )
        with Image.open(path) as source:
            return source.convert('RGBA')
    rect = dataset.items[item_type]['image']['atlas_rect_px']
    return atlas.crop(
        (
            rect['x'],
            rect['y'],
            rect['x'] + rect['width'],
            rect['y'] + rect['height'],
        )
    )


def fit_sprite(canvas, pixels, box, max_scale=4, preserve_canvas=False):
    bounds = pixels.getbbox()
    if bounds and not preserve_canvas:
        pixels = pixels.crop(bounds)
    left, top, right, bottom = box
    ratio = min(
        (right - left) / pixels.width,
        (bottom - top) / pixels.height,
        max_scale,
    )
    ratio = max(1, math.floor(ratio)) if ratio >= 1 else ratio
    pixels = pixels.resize(
        (
            max(1, round(pixels.width * ratio)),
            max(1, round(pixels.height * ratio)),
        ),
        Image.Resampling.NEAREST,
    )
    position = (
        round((left + right - pixels.width) / 2),
        round((top + bottom - pixels.height) / 2),
    )
    canvas.paste(pixels, position, pixels)
