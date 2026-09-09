from pathlib import Path

from layout_assets import connection_id, seasonal_id, sprite
from layout_core import (
    LayoutError,
    require_valid,
)
from layout_placement import render_order
from layout_style import chinese_font_path, load_font
from layout_visuals import draw_coordinate_ticks, render_diagram
from PIL import Image, ImageDraw


def render_plan(
    dataset,
    plan,
    path,
    mode='render',
    scale=2,
    grid=False,
    crop=None,
    font_path=None,
):
    if (
        mode not in ['render', 'blueprint']
        or type(scale) is not int
        or not 1 <= scale <= 4
    ):
        raise LayoutError(
            'Use render or blueprint mode and an integer scale from 1 to 4'
        )
    if mode == 'blueprint':
        return render_diagram(
            dataset,
            plan,
            path,
            scale,
            crop,
            font_path,
        )
    font_path = chinese_font_path(font_path)
    report = require_valid(dataset, plan)
    info = dataset.maps[plan['layout']]
    left, top, width, height = crop or [
        0,
        0,
        info['width_tiles'],
        info['height_tiles'],
    ]
    if (
        any(type(value) is not int for value in [left, top, width, height])
        or min(left, top) < 0
        or min(width, height) <= 0
        or left + width > info['width_tiles']
        or top + height > info['height_tiles']
    ):
        raise LayoutError('Crop rectangle is outside the map')
    background = Image.open(
        dataset.root
        / 'data/maps'
        / info['backgrounds'][plan['season']]['path']
    ).convert('RGBA')
    atlas = Image.open(dataset.root / 'data/planner/atlas.png').convert('RGBA')
    occupied = {}
    for item in plan['objects']:
        for cell in dataset.cells(item):
            occupied.setdefault(cell, []).append(item)
    # Built-in ground art is below all independent flooring and objects.
    # It contributes no placement cells or inventory entries.
    for item in plan['objects']:
        decoration = (
            dataset.items[item['type']]
            .get('building_placement', {})
            .get('front_decoration')
        )
        if decoration:
            dx, dy, decoration_width, decoration_height = decoration[
                'tile_area'
            ]
            with Image.open(dataset.root / decoration['image']) as source:
                pixels = source.convert('RGBA')
            for row in range(decoration_height):
                for column in range(decoration_width):
                    cell = (item['x'] + dx + column, item['y'] + dy + row)
                    if (
                        not 0 <= cell[0] < info['width_tiles']
                        or not 0 <= cell[1] < info['height_tiles']
                        or any(
                            dataset.items[other['type']]['subcategory']
                            == 'flooring'
                            for other in occupied.get(cell, [])
                        )
                    ):
                        continue
                    tile = pixels.crop(
                        (
                            column * 16,
                            row * 16,
                            (column + 1) * 16,
                            (row + 1) * 16,
                        )
                    )
                    background.alpha_composite(
                        tile, (cell[0] * 16, cell[1] * 16)
                    )
    for item in render_order(dataset, plan):
        metadata = dataset.items[item['type']]
        x, y = dataset.anchor(item)
        connected = connection_id(dataset, item, occupied)
        if (
            metadata['category'] == 'crop'
            and metadata['subcategory'] != 'giant-crops'
        ):
            ground = sprite(dataset, connected, atlas)
            background.alpha_composite(
                ground, (x * 16, (y + 1) * 16 - ground.height)
            )
            connected = item['type']
        rendered_id = seasonal_id(dataset, connected, plan['season'])
        pixels = sprite(dataset, rendered_id, atlas)
        background.alpha_composite(
            pixels, (x * 16, (y + 1) * 16 - pixels.height)
        )
    if grid:
        layer = Image.new('RGBA', background.size)
        draw = ImageDraw.Draw(layer)
        for x in range(info['width_tiles'] + 1):
            draw.line(
                (x * 16, 0, x * 16, info['height_px']), fill=(38, 57, 43, 60)
            )
        for y in range(info['height_tiles'] + 1):
            draw.line(
                (0, y * 16, info['width_px'], y * 16), fill=(38, 57, 43, 60)
            )
        background.alpha_composite(layer)
    cropped = background.crop(
        (left * 16, top * 16, (left + width) * 16, (top + height) * 16)
    )
    cropped = cropped.resize(
        (cropped.width * scale, cropped.height * scale),
        Image.Resampling.NEAREST,
    )
    margin, header = 48, 118
    placeholder_count = sum(
        item.get('crop_kind') == 'placeholder' for item in plan['objects']
    )
    image = Image.new(
        'RGB',
        (max(720, cropped.width) + margin * 2, cropped.height + header + 74),
        '#f5f2e9',
    )
    image.paste(cropped.convert('RGB'), (margin, header))
    draw = ImageDraw.Draw(image)
    ticks = draw_coordinate_ticks(
        draw, margin, header, width, height, left, top, 16 * scale
    )
    season_zh = {
        'spring': '春季',
        'summer': '夏季',
        'fall': '秋季',
        'winter': '冬季',
    }
    title = (
        f'{info["name_zh"]} · {season_zh[plan["season"]]} · 渲染图'
        if font_path
        else f'{info["name_en"]} / {plan["season"]} / Preview'
    )
    draw.text(
        (margin, 24), title, font=load_font(26, font_path), fill='#294033'
    )
    summary = (
        f'{len(plan["objects"])} 个对象 · 使用同一份布局配置生成'
        if font_path
        else f'{len(plan["objects"])} objects / generated from the layout configuration'
    )
    draw.text(
        (margin, 65), summary, font=load_font(14, font_path), fill='#697a69'
    )
    if placeholder_count:
        note = (
            '普通作物统一用草莓示意；巨型作物统一用巨型南瓜示意。'
            if font_path
            else 'Crops use strawberry placeholders; giant crops use giant pumpkins.'
        )
        draw.text(
            (margin, image.height - 30),
            note,
            font=load_font(13, font_path),
            fill='#73806d',
        )
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    image.save(path, format='PNG')
    return {
        'path': str(path.resolve()),
        'width_px': image.width,
        'height_px': image.height,
        'validation': report,
        'map_region_px': [margin, header, cropped.width, cropped.height],
        'coordinate_ticks': ticks,
        'placeholder_crop_count': placeholder_count,
    }
