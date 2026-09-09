import hashlib
import os
import tempfile
from collections import Counter
from datetime import UTC, datetime
from pathlib import Path

from layout_core import (
    LayoutError,
    normalize_plan,
    read_json,
    require_valid,
    write_json_atomic,
)
from layout_render import render_plan
from layout_review import (
    aligned_reference,
    create_patch_review,
    map_pixels,
    render_source_hash,
)
from recognition_categories import load_groups, recognition_group


def checkpoint(
    dataset,
    plan,
    stage,
    directory,
    reference=None,
    scale=2,
    font_path=None,
    calibration=None,
    block_size=None,
):
    config = load_groups(dataset.root)
    if stage not in config['workflow_order']:
        raise LayoutError(f'Unknown layout stage: {stage}')
    directory = Path(directory).resolve()
    if directory.exists():
        raise LayoutError(
            'Checkpoint directory already exists; choose a new revision directory'
        )
    plan = normalize_plan(dataset, plan)
    validation = require_valid(dataset, plan)
    source = None
    if reference:
        reference = Path(reference).resolve()
        source = {
            'path': str(reference),
            'sha256': hashlib.sha256(reference.read_bytes()).hexdigest(),
        }
    directory.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(
        prefix='.layout-stage-', dir=directory.parent
    ) as temporary:
        staging = Path(temporary) / 'checkpoint'
        staging.mkdir()
        write_json_atomic(staging / 'plan.json', plan)
        plan_hash = hashlib.sha256(
            (staging / 'plan.json').read_bytes()
        ).hexdigest()
        validation['plan_sha256'] = plan_hash
        write_json_atomic(staging / 'validation.json', validation)
        preview = render_plan(
            dataset,
            plan,
            staging / 'preview.png',
            scale=scale,
            font_path=font_path,
        )
        implementation = render_plan(
            dataset,
            plan,
            staging / 'implementation.png',
            mode='blueprint',
            scale=scale,
            font_path=font_path,
        )
        comparison_files = []
        checked = None
        if calibration:
            if not reference:
                raise LayoutError(
                    'A calibrated checkpoint needs its reference image'
                )
            calibration = read_json(calibration)
            if calibration['layout'] != plan['layout']:
                raise LayoutError(
                    'Calibration map does not match the current plan'
                )
            # Recompute from original points; do not trust edited coefficients.
            checked = calibrate_reference(
                dataset, reference, calibration['points'], plan['layout']
            )
            if checked['reference_sha256'] != calibration['reference_sha256']:
                raise LayoutError('Reference image changed; recalibrate first')
            write_json_atomic(staging / 'calibration.json', checked)
            compare_reference(
                dataset,
                plan,
                reference,
                checked,
                preview,
                staging / 'comparison.png',
            )
            comparison_files = ['calibration.json', 'comparison.png']
        patch_review = None
        if stage == 'final':
            patch_review = create_patch_review(
                dataset,
                plan,
                preview,
                implementation,
                staging / 'patches',
                plan_hash,
                reference,
                checked,
                block_size,
            )
            comparison_files.append('patches/index.json')
        counts = Counter(
            recognition_group(dataset.items[item['type']], config)
            for item in plan['objects']
        )
        report = {
            'schema_version': 1,
            'stage': stage,
            'created_at': datetime.now(UTC).isoformat(timespec='microseconds'),
            'plan_sha256': plan_hash,
            'render_source_sha256': render_source_hash(dataset, plan),
            'object_count': len(plan['objects']),
            'category_counts': dict(sorted(counts.items())),
            'reference': source,
            'visual_review': {
                'status': 'pending',
                'result': 'pending',
                'findings': [],
            },
            'patch_review': patch_review,
            'files': {
                name: {
                    'sha256': hashlib.sha256(
                        (staging / name).read_bytes()
                    ).hexdigest()
                }
                for name in [
                    'plan.json',
                    'validation.json',
                    'preview.png',
                    'implementation.png',
                    *comparison_files,
                ]
            },
        }
        write_json_atomic(staging / 'review.json', report)
        os.rename(staging, directory)
    return {'directory': str(directory), **report}


def calibrate_reference(dataset, reference, points, layout='regular'):
    """Fit an axis-aligned screenshot grid from independent terrain points."""
    import math

    from PIL import Image

    layout = dataset.map_id(layout)
    reference = Path(reference).resolve()
    if not isinstance(points, list) or len(points) < 3:
        raise LayoutError(
            'Use at least three noncollinear fixed terrain points'
        )
    with Image.open(reference) as source:
        size = source.size
    info = dataset.maps[layout]
    for entry in points:
        if not isinstance(entry, dict) or set(entry) != {'tile', 'pixel'}:
            raise LayoutError(
                'Each calibration point needs tile and pixel pairs'
            )
        for key in ['tile', 'pixel']:
            pair = entry[key]
            if (
                not isinstance(pair, list)
                or len(pair) != 2
                or any(
                    type(v) not in [int, float] or not math.isfinite(v)
                    for v in pair
                )
            ):
                raise LayoutError(
                    'Calibration coordinates must be finite numeric pairs'
                )
        if not (
            0 <= entry['pixel'][0] <= size[0]
            and 0 <= entry['pixel'][1] <= size[1]
        ):
            raise LayoutError(
                'Calibration pixel is outside the reference image'
            )
        if not (
            0 <= entry['tile'][0] <= info['width_tiles']
            and 0 <= entry['tile'][1] <= info['height_tiles']
        ):
            raise LayoutError('Calibration tile is outside the map')
    origin = points[0]['tile']
    if not any(
        abs(
            (a['tile'][0] - origin[0]) * (b['tile'][1] - origin[1])
            - (b['tile'][0] - origin[0]) * (a['tile'][1] - origin[1])
        )
        > 1e-6
        for a in points[1:]
        for b in points[2:]
    ):
        raise LayoutError(
            'Calibration terrain points must not lie on one line'
        )
    offsets, scales = [], []
    for axis in [0, 1]:
        tiles = [point['tile'][axis] for point in points]
        pixels = [point['pixel'][axis] for point in points]
        tile_mean, pixel_mean = (
            sum(tiles) / len(tiles),
            sum(pixels) / len(pixels),
        )
        denominator = sum((tile - tile_mean) ** 2 for tile in tiles)
        scale = (
            sum(
                (tile - tile_mean) * (pixel - pixel_mean)
                for tile, pixel in zip(tiles, pixels, strict=True)
            )
            / denominator
        )
        if scale <= 0:
            raise LayoutError('Reference grid must have positive axis scales')
        offsets.append(pixel_mean - scale * tile_mean)
        scales.append(scale)
    residuals = [
        math.hypot(
            *[
                (point['pixel'][axis] - offsets[axis]) / scales[axis]
                - point['tile'][axis]
                for axis in [0, 1]
            ]
        )
        for point in points
    ]
    if max(residuals) > 0.25:
        raise LayoutError(
            f'Reference calibration residual is {max(residuals):.3f} tiles; recheck fixed landmarks, crop and grid scale'
        )
    return {
        'schema_version': 1,
        'layout': layout,
        'reference_path': str(reference),
        'reference_sha256': hashlib.sha256(reference.read_bytes()).hexdigest(),
        'reference_size_px': list(size),
        'map_size_tiles': [info['width_tiles'], info['height_tiles']],
        'origin_px': offsets,
        'tile_size_px': scales,
        'points': points,
        'max_residual_tiles': max(residuals),
    }


def locate_reference(
    dataset, calibration, pixel, item_type=None, point_kind='footprint'
):
    import math

    if point_kind not in ['footprint', 'sprite', 'planner-anchor']:
        raise LayoutError(
            'Point kind must be footprint, sprite or planner-anchor'
        )
    if len(pixel) != 2 or any(
        type(v) not in [int, float] or not math.isfinite(v) for v in pixel
    ):
        raise LayoutError('Pixel must be a finite numeric pair')
    point = [
        (pixel[i] - calibration['origin_px'][i])
        / calibration['tile_size_px'][i]
        for i in [0, 1]
    ]
    item_type = dataset.item_id(item_type) if item_type else None
    if point_kind != 'footprint':
        if not item_type:
            raise LayoutError(
                'Sprite and planner-anchor points need an item type'
            )
        if point_kind == 'sprite':
            # Use the full sprite canvas, including transparent padding.
            point[1] += dataset.items[item_type]['image']['height_px'] / 16 - 1
        point = list(dataset.from_anchor(item_type, *point))
    nearest = [round(value) for value in point]
    return {
        'footprint_position': point,
        'nearest_footprint_tile': nearest,
        'rounding_error_tiles': [point[i] - nearest[i] for i in [0, 1]],
        'needs_visual_confirmation': True,
    }


def compare_reference(dataset, plan, reference, calibration, preview, path):
    from layout_style import load_font
    from PIL import Image, ImageDraw

    if calibration['layout'] != plan['layout']:
        raise LayoutError('Calibration map does not match the current plan')
    if (
        calibration['reference_sha256']
        != hashlib.sha256(Path(reference).read_bytes()).hexdigest()
    ):
        raise LayoutError(
            'Reference image changed; recalibrate before comparison'
        )
    info = dataset.maps[plan['layout']]
    dimensions = (info['width_px'], info['height_px'])
    aligned = aligned_reference(dataset, plan, reference, calibration)
    rendered = map_pixels(preview, dimensions)
    gap, header = 24, 38
    canvas = Image.new(
        'RGB', (dimensions[0] * 2 + gap, dimensions[1] + header), '#f8f7f0'
    )
    canvas.paste(aligned, (0, header))
    canvas.paste(rendered, (dimensions[0] + gap, header))
    draw = ImageDraw.Draw(canvas)
    for label, x in [
        ('Reference / calibrated grid', 12),
        ('Current layout / same grid', dimensions[0] + gap + 12),
    ]:
        draw.text((x, 10), label, font=load_font(16), fill='#344e3e')
    # Coordinate marks remain visible on both sides, without affecting output previews.
    for offset in [0, dimensions[0] + gap]:
        for x in range(0, dimensions[0], 80):
            draw.line(
                (offset + x, header, offset + x, canvas.height),
                fill='#778c79',
                width=1,
            )
        for y in range(0, dimensions[1], 80):
            draw.line(
                (offset, header + y, offset + dimensions[0] - 1, header + y),
                fill='#778c79',
                width=1,
            )
    canvas.save(path, format='PNG')
