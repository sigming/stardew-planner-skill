import math
from pathlib import Path

from layout_assets import fit_sprite, seasonal_id, sprite
from layout_core import (
    LayoutError,
    inventory,
    require_valid,
)
from layout_landmarks import edge_references
from layout_placement import placement_areas, render_order
from layout_style import (
    chinese_font_path,
    item_styles,
    load_font,
)
from PIL import Image, ImageDraw


def _terrain(dataset, plan):
    info = dataset.maps[plan['layout']]
    path = (
        dataset.root
        / 'data/maps'
        / info['backgrounds'][plan['season']]['path']
    )
    with Image.open(path) as image:
        return image.convert('RGB')


def _draw_edge_labels(
    draw,
    references,
    map_x,
    map_y,
    map_width,
    map_height,
    left,
    top,
    unit,
    font,
):
    for reference in references:
        px = map_x + (reference['x'] - left + 0.5) * unit
        py = map_y + (reference['y'] - top + 0.5) * unit
        if not (
            map_x <= px <= map_x + map_width
            and map_y <= py <= map_y + map_height
        ):
            continue
        side = reference['side']
        cx = (
            px
            if side in ['N', 'S']
            else map_x - 73
            if side == 'W'
            else map_x + map_width + 73
        )
        cy = (
            py
            if side in ['E', 'W']
            else map_y - 49
            if side == 'N'
            else map_y + map_height + 43
        )
        label = f'{reference["id"]}  {reference["x"]},{reference["y"]}'
        draw.line((cx, cy, px, py), fill='#9baea3', width=1)
        draw.rounded_rectangle(
            (cx - 54, cy - 15, cx + 54, cy + 15),
            radius=3,
            fill='#f1f6ef',
            outline='#537662',
            width=1,
        )
        draw.text((cx, cy), label, anchor='mm', font=font, fill='#274c37')
        draw.rectangle(
            (px - 5, py - 5, px + 5, py + 5),
            fill='#fffef8',
            outline='#345e45',
            width=2,
        )
        draw.line((px - 3, py, px + 3, py), fill='#345e45')
        draw.line((px, py - 3, px, py + 3), fill='#345e45')


def draw_coordinate_ticks(draw, map_x, map_y, width, height, left, top, unit):
    ticks = {'x': [], 'y': []}
    for axis, start, count in [('x', left, width), ('y', top, height)]:
        for value in range(start, start + count):
            major = value % 5 == 0
            length = 8 if major else 4
            offset = (value - start + 0.5) * unit
            if axis == 'x':
                position = map_x + offset
                segments = [
                    [position, map_y - length, position, map_y - 1],
                    [
                        position,
                        map_y + height * unit,
                        position,
                        map_y + height * unit + length,
                    ],
                ]
                labels = [
                    (position, map_y - 17),
                    (position, map_y + height * unit + 17),
                ]
            else:
                position = map_y + offset
                segments = [
                    [map_x - length, position, map_x - 1, position],
                    [
                        map_x + width * unit,
                        position,
                        map_x + width * unit + length,
                        position,
                    ],
                ]
                labels = [
                    (map_x - 22, position),
                    (map_x + width * unit + 22, position),
                ]
            for segment in segments:
                draw.line(segment, fill='#617261', width=1)
            if major:
                for label in labels:
                    draw.text(
                        label,
                        str(value),
                        anchor='mm',
                        font=load_font(11),
                        fill='#617261',
                    )
            ticks[axis].append(
                {'value': value, 'major': major, 'segments_px': segments}
            )
    return ticks


def render_diagram(
    dataset,
    plan,
    path,
    scale=2,
    crop=None,
    font_path=None,
):
    validation = require_valid(dataset, plan)
    if type(scale) is not int or not 1 <= scale <= 4:
        raise LayoutError('Diagram scale must be an integer from 1 to 4')
    font_path = chinese_font_path(font_path)
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
    styles = item_styles(plan, dataset)
    image = _terrain(dataset, plan)
    grid = ImageDraw.Draw(image, 'RGBA')
    for x in range(info['width_tiles'] + 1):
        grid.line(
            (x * 16, 0, x * 16, info['height_px']),
            fill=(54, 76, 47, 56 if x % 5 == 0 else 26),
        )
    for y in range(info['height_tiles'] + 1):
        grid.line(
            (0, y * 16, info['width_px'], y * 16),
            fill=(54, 76, 47, 56 if y % 5 == 0 else 26),
        )
    draw = ImageDraw.Draw(image)
    ordered = render_order(dataset, plan)
    owners = {}
    for item in ordered:
        w, h = dataset.dimensions(item['type'])
        # A uniform inset leaves a small, symmetric gap. No dark border,
        # internal grid, shaded edge or raised label is drawn on the marker.
        draw.rectangle(
            (
                item['x'] * 16 + 1,
                item['y'] * 16 + 1,
                (item['x'] + w) * 16 - 2,
                (item['y'] + h) * 16 - 2,
            ),
            fill=styles[item['type']]['color'],
        )
        for cell in dataset.cells(item):
            owners[cell] = item['id']
    by_id = {item['id']: item for item in ordered}
    for overlap in validation['flooring_overlaps']:
        flooring = by_id[overlap['flooring_id']]
        for x, y in overlap['cells']:
            # Show covered flooring as a flat strip below the upper marker.
            draw.rectangle(
                (x * 16 + 2, y * 16 + 13, x * 16 + 13, y * 16 + 14),
                fill=styles[flooring['type']]['color'],
            )
    numbered = []
    for item in ordered:
        style = styles[item['type']]
        if style['number'] is None:
            continue
        w, h = dataset.dimensions(item['type'])
        if owners.get((item['x'] + w // 2, item['y'] + h // 2)) != item['id']:
            continue
        numbered.append(item['id'])
        text = str(style['number'])
        draw.text(
            ((item['x'] + w / 2) * 16, (item['y'] + h / 2) * 16),
            text,
            anchor='mm',
            font=load_font(8 if len(text) < 3 else 6),
            fill='#20352d',
        )
    additional_areas = []
    for item in ordered:
        for area in placement_areas(
            dataset.items[item['type']], item['x'], item['y']
        )['additional_areas']:
            additional_areas.append({'object_id': item['id'], **area})
            x, y, w, h = area['tile_rect']
            x1, y1, x2, y2 = (
                x * 16 + 2,
                y * 16 + 2,
                (x + w) * 16 - 3,
                (y + h) * 16 - 3,
            )
            color = (
                '#875825' if area['requirement'] == 'passable' else '#75588c'
            )
            for pos in range(x1, x2 + 1, 7):
                draw.line((pos, y1, min(pos + 3, x2), y1), fill=color)
                draw.line((pos, y2, min(pos + 3, x2), y2), fill=color)
            for pos in range(y1, y2 + 1, 7):
                draw.line((x1, pos, x1, min(pos + 3, y2)), fill=color)
                draw.line((x2, pos, x2, min(pos + 3, y2)), fill=color)
    image = image.crop(
        (left * 16, top * 16, (left + width) * 16, (top + height) * 16)
    )
    image = image.resize(
        (image.width * scale, image.height * scale), Image.Resampling.NEAREST
    )
    references = edge_references(info, dataset.blocked(plan['layout']))
    landmarks = plan.get('fixed_landmarks', info.get('fixed_landmarks', []))
    legend = []
    for row in inventory(dataset, plan)['items']:
        kind = row['type']
        rendered_id = seasonal_id(dataset, kind, plan['season'])
        legend.append(
            {
                **styles[kind],
                'label': row['name_zh'] if font_path else row['name_en'],
                'type': kind,
                'crop_kind': row['crop_kind'],
                'count': row['count'],
                'existing_count': row['existing_count'],
                'planned_count': row['planned_count'],
                'icon_id': rendered_id,
                'icon': dataset.items[rendered_id]['image']['path'],
            }
        )
    style_order = {kind: index for index, kind in enumerate(styles)}
    legend.sort(
        key=lambda entry: (
            entry['number'] is None,
            style_order[entry['type']],
            entry['crop_kind'] or '',
        )
    )
    return _finish_diagram(
        dataset,
        plan,
        path,
        image,
        left,
        top,
        width,
        height,
        scale,
        numbered,
        references,
        landmarks,
        legend,
        validation,
        font_path,
        additional_areas,
    )


def _finish_diagram(
    dataset,
    plan,
    path,
    map_image,
    left,
    top,
    width,
    height,
    scale,
    numbered,
    references,
    landmarks,
    legend,
    validation,
    font_path,
    additional_areas,
):
    content_width = max(800, map_image.width)
    margin = 144 if references else 56
    header = 170 if references else 115
    lower_band = 82 if references else 46
    columns = min(6, max(1, content_width // 360))
    row_height = 60
    legend_rows = math.ceil(len(legend) / columns)
    footer = 78 + legend_rows * row_height
    image = Image.new(
        'RGB',
        (
            content_width + 2 * margin,
            header + map_image.height + lower_band + footer,
        ),
        '#f8f7f0',
    )
    map_x, map_y = margin + (content_width - map_image.width) // 2, header
    image.paste(map_image, (map_x, map_y))
    draw = ImageDraw.Draw(image)
    info = dataset.maps[plan['layout']]
    title = (
        f'{info["name_zh"]} · 实施简图'
        if font_path
        else f'{info["name_en"]} / implementation'
    )
    draw.text(
        (margin, 24), title, font=load_font(27, font_path), fill='#273d33'
    )
    summary = (
        '设施：颜色＋编号 · 作物：浅色，无编号 · 坐标从 (0, 0) 开始'
        if font_path
        else 'Facilities: color + number. Crops: pale, unnumbered. Tile origin: (0, 0).'
    )
    if additional_areas:
        summary += (
            ' · 虚线：建造/移动检查区'
            if font_path
            else ' Dashed: construction/move checks.'
        )
    draw.text(
        (margin, 70), summary, font=load_font(15, font_path), fill='#68796c'
    )
    if validation['flooring_overlaps']:
        draw.text(
            (margin, 94),
            '叠放处底边色条表示下层地板；地板和上层物品分别计数。'
            if font_path
            else 'Bottom strips show underlying flooring; count both flooring and objects.',
            font=load_font(12, font_path),
            fill='#68796c',
        )
    ticks = draw_coordinate_ticks(
        draw, map_x, map_y, width, height, left, top, 16 * scale
    )
    if references:
        _draw_edge_labels(
            draw,
            references,
            map_x,
            map_y,
            map_image.width,
            map_image.height,
            left,
            top,
            16 * scale,
            load_font(12),
        )
    legend_y = map_y + map_image.height + lower_band
    column_width = content_width // columns
    label_font = load_font(14, font_path)
    for index, entry in enumerate(legend):
        x = margin + (index // legend_rows) * column_width
        y = legend_y + (index % legend_rows) * row_height
        card_right = x + column_width - 12
        draw.rounded_rectangle(
            (x, y, card_right, y + row_height - 5),
            radius=4,
            fill='#fffef9',
            outline='#d8ded0',
        )
        draw.rounded_rectangle(
            (x + 8, y + 14, x + 34, y + 40),
            radius=3,
            fill=entry['color'],
            outline='#8d9a89',
        )
        if entry['number'] is not None:
            draw.text(
                (x + 21, y + 27),
                str(entry['number']),
                anchor='mm',
                font=load_font(14),
                fill='#23372d',
            )
        icon_box = [card_right - 100, y + 5, card_right - 52, y + 49]
        # Reserve independent columns for name, seasonal icon and quantity.
        text_width = icon_box[0] - x - 47
        label = entry['label']
        lines = []
        while label:
            line = ''
            while (
                label
                and draw.textlength(line + label[0], font=label_font)
                <= text_width
            ):
                line += label[0]
                label = label[1:]
            if not line:
                raise LayoutError('Legend name column is too narrow')
            lines.append(line)
        if len(lines) > 3:
            raise LayoutError('Legend label exceeds three lines')
        for offset, line in enumerate(lines):
            draw.text(
                (x + 43, y + 27 - len(lines) * 8 + offset * 16),
                line,
                font=label_font,
                fill='#344e3e',
            )
        fit_sprite(image, sprite(dataset, entry['icon_id']), icon_box)
        draw.text(
            (card_right - 6, y + 27),
            f'×{entry["count"]}' if font_path else f'x{entry["count"]}',
            anchor='rm',
            font=load_font(15, font_path),
            fill='#344e3e',
        )
        entry['card_bounds_px'] = [x, y, card_right, y + row_height - 5]
        entry['icon_bounds_px'] = icon_box
    caption_y = legend_y + legend_rows * row_height + 13
    caption = (
        'N 北 · E 东 · S 南 · W 西：紧邻可操作地块的固定地形参照点。数量为布局中的对象总数。'
        if font_path
        else 'N / E / S / W: fixed terrain references. Quantities count all placed objects.'
    )
    draw.text(
        (margin, caption_y),
        caption,
        font=load_font(13, font_path),
        fill='#647666',
    )
    placeholder_count = sum(
        item.get('crop_kind') == 'placeholder' for item in plan['objects']
    )
    if placeholder_count:
        note = (
            '普通作物统一用草莓示意；巨型作物统一用巨型南瓜示意。'
            if font_path
            else 'Crops use strawberry placeholders; giant crops use giant pumpkins.'
        )
        draw.text(
            (margin, caption_y + 27),
            note,
            font=load_font(12, font_path),
            fill='#7b8477',
        )
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    image.save(path, format='PNG')
    return {
        'path': str(path.resolve()),
        'width_px': image.width,
        'height_px': image.height,
        'validation': validation,
        'map_region_px': [map_x, map_y, map_image.width, map_image.height],
        'numbered_object_ids': numbered,
        'legend': legend,
        'additional_placement_areas': additional_areas,
        'legend_columns': columns,
        'legend_order': 'top-to-bottom then left-to-right',
        'coordinate_ticks': ticks,
        'edge_references': references,
        'fixed_references': landmarks,
        'placeholder_crop_count': placeholder_count,
    }
