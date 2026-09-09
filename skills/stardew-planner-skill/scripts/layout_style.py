import colorsys
import os
from pathlib import Path

from PIL import ImageFont
from recognition_categories import legend_sort_keys

PALETTE = [
    '#91b9d0',
    '#e0b36c',
    '#ada4d8',
    '#86bca1',
    '#df9d92',
    '#bacb78',
    '#d3afd0',
    '#76b6bc',
    '#d29c70',
    '#a5b8e0',
    '#90c29a',
    '#dfa9be',
    '#c5bf80',
    '#99b4a7',
    '#baa2c7',
    '#dfc28b',
]


def load_font(size, path=None):
    if path:
        return ImageFont.truetype(str(path), size=size)
    return ImageFont.load_default(size=size)


def chinese_font_path(requested=None):
    if requested:
        return Path(requested)
    candidates = [
        Path('/System/Library/Fonts/Hiragino Sans GB.ttc'),
        Path('/System/Library/Fonts/STHeiti Medium.ttc'),
        Path('/usr/share/fonts/opentype/noto/NotoSansCJK-Regular.ttc'),
        Path('/usr/share/fonts/truetype/wqy/wqy-microhei.ttc'),
        Path(os.environ.get('WINDIR', 'C:/Windows')) / 'Fonts/msyh.ttc',
    ]
    return next((path for path in candidates if path.is_file()), None)


def color_at(index):
    if index < len(PALETTE):
        return PALETTE[index]
    red, green, blue = colorsys.hls_to_rgb(
        (index * 0.61803398875) % 1, 0.73, 0.42
    )
    return f'#{round(red * 255):02x}{round(green * 255):02x}{round(blue * 255):02x}'


def pale_color(color):
    rgb = [int(color[index : index + 2], 16) for index in [1, 3, 5]]
    return tuple(round(value * 0.25 + 255 * 0.75) for value in rgb)


def item_styles(plan, dataset=None):
    item_types = list(
        dict.fromkeys(
            item['type']
            for item in sorted(
                plan['objects'],
                key=lambda item: (
                    item['y'],
                    item['x'],
                    item['type'],
                    item['id'],
                ),
            )
        )
    )
    if dataset is not None:
        keys = legend_sort_keys(dataset, item_types)
        # Stable sorting retains spatial order for uncategorized items.
        item_types.sort(key=keys.__getitem__)
    styles = {
        kind: {'number': index + 1, 'color': color_at(index)}
        for index, kind in enumerate(item_types)
    }
    if dataset is not None:
        number = 0
        for kind, style in styles.items():
            if dataset.items[kind]['category'] == 'crop':
                red, green, blue = pale_color(style['color'])
                style['color'] = f'#{red:02x}{green:02x}{blue:02x}'
                style['number'] = None
            else:
                number += 1
                style['number'] = number
    return styles
