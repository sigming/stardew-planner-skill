import hashlib
import math
from collections import Counter
from pathlib import Path

from layout_assets import appearances
from layout_core import Dataset, LayoutError, read_json, write_json_atomic
from layout_placement import placement_areas
from layout_style import load_font
from PIL import Image, ImageDraw

REVIEW_CHECKS = (
    'identities',
    'positions',
    'flooring',
    'occlusions',
    'ignored_entities',
)
DEFAULT_REVIEW_GRID = (3, 2)


def render_source_hash(dataset, plan):
    paths = [
        'data/planner/catalog.json',
        'data/recognition/groups.json',
        'data/recognition/supplemental.json',
        'data/maps/maps.json',
        'data/maps/allowlist.json',
        'data/maps/' + dataset.maps[plan['layout']]['restrictions_path'],
        'scripts/map_rules.py',
        'scripts/recognition_categories.py',
        'scripts/layout_core.py',
        'scripts/layout_placement.py',
        'scripts/layout_checkpoint.py',
        'scripts/layout_review.py',
        'scripts/layout_assets.py',
        'scripts/layout_render.py',
        'scripts/layout_visuals.py',
        'scripts/layout_style.py',
        'data/planner/atlas.png',
        'data/maps/'
        + dataset.maps[plan['layout']]['backgrounds'][plan['season']]['path'],
    ]
    paths.extend(
        'data/planner/' + entry['image']
        for item_type in sorted({item['type'] for item in plan['objects']})
        for entry in appearances(dataset, item_type)
    )
    paths.extend(
        decoration['image']
        for item_type in {item['type'] for item in plan['objects']}
        if (
            decoration := dataset.items[item_type]
            .get('building_placement', {})
            .get('front_decoration')
        )
    )
    digest = hashlib.sha256()
    for path in sorted(set(paths)):
        digest.update(path.encode())
        digest.update((dataset.root / path).read_bytes())
    return digest.hexdigest()


def aligned_reference(dataset, plan, reference, calibration):
    info = dataset.maps[plan['layout']]
    with Image.open(reference) as source:
        sx, sy = calibration['tile_size_px']
        ox, oy = calibration['origin_px']
        return source.convert('RGB').transform(
            (info['width_px'], info['height_px']),
            Image.Transform.AFFINE,
            (sx / 16, 0, ox, 0, sy / 16, oy),
            resample=Image.Resampling.BICUBIC,
            fillcolor='#dddddd',
        )


def map_pixels(rendered, dimensions):
    with Image.open(rendered['path']) as source:
        x, y, w, h = rendered['map_region_px']
        return (
            source.crop((x, y, x + w, y + h))
            .convert('RGB')
            .resize(dimensions, Image.Resampling.NEAREST)
        )


def review_regions(width, height, block_size, overlap=2):
    if len(block_size) != 2 or any(
        type(v) is not int or v < 1 for v in block_size
    ):
        raise LayoutError(
            'Review block dimensions must be positive tile integers'
        )
    if type(overlap) is not int or overlap < 0:
        raise LayoutError('Review overlap must be a nonnegative tile integer')
    for row, y in enumerate(range(0, height, block_size[1]), 1):
        for column, x in enumerate(range(0, width, block_size[0]), 1):
            w, h = (
                min(block_size[0], width - x),
                min(block_size[1], height - y),
            )
            left, top = max(0, x - overlap), max(0, y - overlap)
            right, bottom = (
                min(width, x + w + overlap),
                min(height, y + h + overlap),
            )
            yield {
                'id': f'r{row:02d}-c{column:02d}',
                'tile_rect': [x, y, w, h],
                'context_rect': [left, top, right - left, bottom - top],
            }


def _intersects(first, second):
    x, y, w, h = first
    a, b, c, d = second
    return x < a + c and a < x + w and y < b + d and b < y + h


def _flooring_runs(dataset, objects, region):
    left, top, width, height = region
    cells = {}
    for item in objects:
        if dataset.items[item['type']]['subcategory'] == 'flooring':
            for x, y in dataset.cells(item):
                if left <= x < left + width and top <= y < top + height:
                    cells[(x, y)] = item['type']
    result = {'horizontal': [], 'vertical': []}
    for key, horizontal in [('horizontal', True), ('vertical', False)]:
        lines = (
            range(top, top + height)
            if horizontal
            else range(left, left + width)
        )
        start, length = (left, width) if horizontal else (top, height)
        for line in lines:
            previous, run_start = None, start
            for position in range(start, start + length + 1):
                point = (position, line) if horizontal else (line, position)
                kind = cells.get(point) if position < start + length else None
                if kind != previous:
                    if previous:
                        result[key].append(
                            {
                                'type': previous,
                                'line': line,
                                'start': run_start,
                                'end_exclusive': position,
                                'length': position - run_start,
                            }
                        )
                    previous, run_start = kind, position
    return result


def create_patch_review(
    dataset,
    plan,
    preview,
    implementation,
    directory,
    plan_hash,
    reference=None,
    calibration=None,
    block_size=None,
    overlap=2,
):
    directory = Path(directory)
    directory.mkdir()
    info = dataset.maps[plan['layout']]
    if block_size is None:
        block_size = [
            math.ceil(info['width_tiles'] / DEFAULT_REVIEW_GRID[0]),
            math.ceil(info['height_tiles'] / DEFAULT_REVIEW_GRID[1]),
        ]
    images = {}
    if reference:
        if not calibration:
            raise LayoutError(
                'Final reference review requires coordinate calibration'
            )
        images['reference'] = aligned_reference(
            dataset, plan, reference, calibration
        )
    dimensions = (info['width_px'], info['height_px'])
    images['preview'] = map_pixels(preview, dimensions)
    images['implementation'] = map_pixels(implementation, dimensions)
    index = {
        'schema_version': 2,
        'plan_sha256': plan_hash,
        'render_source_sha256': render_source_hash(dataset, plan),
        'layout': plan['layout'],
        'map_size_tiles': [info['width_tiles'], info['height_tiles']],
        'block_size_tiles': list(block_size),
        'overlap_tiles': overlap,
        'image_scale': 1,
        'reference_path': str(Path(reference).resolve())
        if reference
        else None,
        'reference_sha256': calibration['reference_sha256']
        if reference
        else None,
        'patches': [],
    }
    for region in review_regions(
        info['width_tiles'], info['height_tiles'], block_size, overlap
    ):
        x, y, w, h = region['context_rect']
        core_x, core_y, core_w, core_h = region['tile_rect']
        relevant = []
        additional_areas = []
        for item in plan['objects']:
            width, height = dataset.dimensions(item['type'])
            ax, ay = dataset.anchor(item)
            frame = dataset.items[item['type']]['image']
            visible = [
                ax,
                ay + 1 - frame['height_px'] / 16,
                frame['width_px'] / 16,
                frame['height_px'] / 16,
            ]
            if (
                _intersects(
                    [item['x'], item['y'], width, height],
                    region['context_rect'],
                )
                or _intersects(visible, region['context_rect'])
                or any(
                    _intersects(area['tile_rect'], region['context_rect'])
                    for area in placement_areas(
                        dataset.items[item['type']], item['x'], item['y']
                    )['additional_areas']
                )
            ):
                relevant.append(item)
                additional_areas.extend(
                    {'object_id': item['id'], **area}
                    for area in placement_areas(
                        dataset.items[item['type']], item['x'], item['y']
                    )['additional_areas']
                    if _intersects(area['tile_rect'], region['context_rect'])
                )
        region['object_ids'] = [item['id'] for item in relevant]
        region['additional_placement_areas'] = additional_areas
        region['flooring_runs'] = _flooring_runs(
            dataset, relevant, region['tile_rect']
        )
        region['files'] = {}
        region['review_file'] = f'reviews/{region["id"]}.json'
        region['panels'] = {}
        if reference:
            sx, sy = calibration['tile_size_px']
            ox, oy = calibration['origin_px']
            box = [
                ox + core_x * sx,
                oy + core_y * sy,
                ox + (core_x + core_w) * sx,
                oy + (core_y + core_h) * sy,
            ]
            source_w, source_h = calibration['reference_size_px']
            visible_area = max(
                0, min(source_w, box[2]) - max(0, box[0])
            ) * max(0, min(source_h, box[3]) - max(0, box[1]))
            region['reference_box_px'] = box
            region['reference_visible_fraction'] = visible_area / (
                core_w * sx * core_h * sy
            )
        scale = 1
        pane_w, pane_h = w * 16 * scale, h * 16 * scale
        gap, margin, header = 16, 28, 58
        combined = Image.new(
            'RGB',
            (
                len(images) * (pane_w + margin * 2) + gap * (len(images) - 1),
                pane_h + header + margin,
            ),
            '#f8f7f0',
        )
        combined_draw = ImageDraw.Draw(combined)
        title = f'{region["id"]} / x {core_x}:{core_x + core_w}  y {core_y}:{core_y + core_h}'
        for offset, (kind, source) in enumerate(images.items()):
            crop = source.crop(
                (x * 16, y * 16, (x + w) * 16, (y + h) * 16)
            ).resize((pane_w, pane_h), Image.Resampling.NEAREST)
            px = offset * (pane_w + margin * 2 + gap) + margin
            region['panels'][kind] = [px, header, px + pane_w, header + pane_h]
            combined.paste(crop, (px, header))
            combined_draw.text(
                (px, 8),
                f'{kind} / {title}',
                font=load_font(13),
                fill='#344e3e',
            )
            combined_draw.rectangle(
                (
                    px + (core_x - x) * 16 * scale,
                    header + (core_y - y) * 16 * scale,
                    px + (core_x + core_w - x) * 16 * scale - 1,
                    header + (core_y + core_h - y) * 16 * scale - 1,
                ),
                outline='#dc643c',
                width=2,
            )
            for col in range(w):
                tx = px + (col + 0.5) * 16 * scale
                combined_draw.text(
                    (tx, header - 15),
                    str(x + col),
                    anchor='mm',
                    font=load_font(11),
                    fill='#617261',
                )
            for row in range(h):
                ty = header + (row + 0.5) * 16 * scale
                combined_draw.text(
                    (px - 16, ty),
                    str(y + row),
                    anchor='mm',
                    font=load_font(11),
                    fill='#617261',
                )
        target = directory / f'{region["id"]}-comparison.png'
        combined.save(target, optimize=True)
        image_bytes = target.stat().st_size
        region['files']['comparison'] = {
            'path': target.name,
            'sha256': hashlib.sha256(target.read_bytes()).hexdigest(),
            'size_px': list(combined.size),
            'bytes': image_bytes,
            'base64_bytes': 4 * math.ceil(image_bytes / 3),
        }
        index['patches'].append(region)
    index['image_input'] = {
        'comparison_count': len(index['patches']),
        'total_base64_bytes': sum(
            region['files']['comparison']['base64_bytes']
            for region in index['patches']
        ),
        'max_images_per_agent': 2,
        'note': 'File sizes exclude conversation history and API/tool overhead. Open only assigned comparisons; return text findings and file paths.',
    }
    write_json_atomic(directory / 'index.json', index)
    index_hash = hashlib.sha256(
        (directory / 'index.json').read_bytes()
    ).hexdigest()
    review = {
        'schema_version': 2,
        'plan_sha256': plan_hash,
        'index_sha256': index_hash,
        'patches': [],
    }
    for region in index['patches']:
        row = {
            'id': region['id'],
            'checks': dict.fromkeys(REVIEW_CHECKS, 'pending'),
            'findings': [],
        }
        review['patches'].append(row)
        write_json_atomic(
            directory / region['review_file'], {**review, 'patches': [row]}
        )
    write_json_atomic(directory / 'review.json', review)
    return {
        'path': 'patches/review.json',
        'patch_count': len(index['patches']),
        'block_size_tiles': list(block_size),
        'image_input': index['image_input'],
        'status': 'pending',
    }


def validate_checkpoint_files(directory, dataset):
    directory = Path(directory).resolve()
    report = read_json(directory / 'review.json')
    plan = read_json(directory / 'plan.json')
    if (
        hashlib.sha256((directory / 'plan.json').read_bytes()).hexdigest()
        != report['plan_sha256']
    ):
        raise LayoutError('Checkpoint plan changed; create a new checkpoint')
    if report.get('render_source_sha256') != render_source_hash(dataset, plan):
        raise LayoutError(
            'Rendering code or assets changed; create a new checkpoint'
        )
    if not {
        'plan.json',
        'preview.png',
        'implementation.png',
        'validation.json',
    } <= set(report['files']):
        raise LayoutError('Checkpoint file records are incomplete')
    for name, record in report['files'].items():
        path = (directory / name).resolve()
        if (
            not path.is_relative_to(directory)
            or hashlib.sha256(path.read_bytes()).hexdigest()
            != record['sha256']
        ):
            raise LayoutError(
                'Checkpoint files changed; create a new checkpoint'
            )
    reference = report.get('reference')
    if (
        reference
        and hashlib.sha256(Path(reference['path']).read_bytes()).hexdigest()
        != reference['sha256']
    ):
        raise LayoutError('Reference changed; create a new checkpoint')
    return report


def validate_patch_review(
    directory, plan_hash=None, dataset=None, *, review=None
):
    directory = Path(directory).resolve()
    root = directory / 'patches'
    index = read_json(root / 'index.json')
    if review is None:
        review = read_json(root / 'review.json')
    if index['render_source_sha256'] != render_source_hash(
        dataset or Dataset(), read_json(directory / 'plan.json')
    ):
        raise LayoutError(
            'Rendering code or assets changed; create a new final checkpoint'
        )
    actual_hash = hashlib.sha256(
        (directory / 'plan.json').read_bytes()
    ).hexdigest()
    if (
        (plan_hash and actual_hash != plan_hash)
        or actual_hash != index['plan_sha256']
        or actual_hash != review['plan_sha256']
    ):
        raise LayoutError('Patch review belongs to another plan revision')
    overall = validate_checkpoint_files(directory, dataset or Dataset())
    if (
        overall.get('stage') != 'final'
        or overall.get('plan_sha256') != actual_hash
    ):
        raise LayoutError('Overall review belongs to another final checkpoint')
    visual = overall.get('visual_review', {})
    if visual.get('status') not in ['pending', 'compared'] or not isinstance(
        visual.get('findings'), list
    ):
        raise LayoutError(
            'Overall visual review needs a valid status and findings'
        )
    overall_result = visual.get('result', 'pending')
    if overall_result not in [
        'pending',
        'passed',
        'needs_change',
        'uncertain',
    ]:
        raise LayoutError('Overall review needs a valid result')
    finding_statuses = {
        finding.get('status')
        for finding in visual['findings']
        if isinstance(finding, dict)
    }
    if 'needs_change' in finding_statuses:
        overall_result = 'needs_change'
    elif 'uncertain' in finding_statuses and overall_result == 'passed':
        overall_result = 'uncertain'
    if (
        overall_result in ['needs_change', 'uncertain']
        and not visual['findings']
    ):
        raise LayoutError(
            'Failed or uncertain overall checks need concrete findings'
        )
    if (
        hashlib.sha256((root / 'index.json').read_bytes()).hexdigest()
        != review['index_sha256']
    ):
        raise LayoutError('Patch index changed; create a new checkpoint')
    if (
        index.get('reference_path')
        and hashlib.sha256(
            Path(index['reference_path']).read_bytes()
        ).hexdigest()
        != index['reference_sha256']
    ):
        raise LayoutError('Reference changed; create a new final checkpoint')
    expected = {entry['id'] for entry in index['patches']}
    rows = review.get('patches', [])
    if (
        len(rows) != len(expected)
        or {row.get('id') for row in rows} != expected
    ):
        raise LayoutError('Every patch needs exactly one review record')
    for entry in index['patches']:
        for asset in entry['files'].values():
            path = (root / asset['path']).resolve()
            if (
                not path.is_relative_to(root.resolve())
                or hashlib.sha256(path.read_bytes()).hexdigest()
                != asset['sha256']
            ):
                raise LayoutError(
                    'Patch images changed; create a new checkpoint'
                )
    statuses = Counter()
    unmerged = []
    for region in index['patches']:
        recorded_hash = review.get('region_record_sha256', {}).get(
            region['id']
        )
        if recorded_hash:
            independent_path = root / region['review_file']
            if (
                not independent_path.is_file()
                or hashlib.sha256(independent_path.read_bytes()).hexdigest()
                != recorded_hash
            ):
                unmerged.append(region['id'])
    findings = []
    patch_index = {entry['id']: entry for entry in index['patches']}
    for row in rows:
        checks = row.get('checks', {})
        if set(checks) != set(REVIEW_CHECKS) or any(
            value
            not in [
                'pending',
                'passed',
                'needs_change',
                'uncertain',
                'not_applicable',
            ]
            for value in checks.values()
        ):
            raise LayoutError(
                'Every patch needs valid identity, position, flooring, occlusion and dynamic-entity checks'
            )
        patch = patch_index[row['id']]
        required = ['positions']
        if patch['object_ids']:
            required.append('identities')
        if patch['flooring_runs']['horizontal']:
            required.append('flooring')
        if any(checks[key] == 'not_applicable' for key in required):
            raise LayoutError(
                'Required position, identity or flooring checks cannot be skipped'
            )
        if not isinstance(row.get('findings'), list):
            raise LayoutError('Patch findings must be an array')
        if (
            any(
                value in ['needs_change', 'uncertain']
                for value in checks.values()
            )
            and not row['findings']
        ):
            raise LayoutError(
                'Failed or uncertain patch checks need concrete findings'
            )
        status = (
            'pending'
            if 'pending' in checks.values()
            else 'needs_change'
            if 'needs_change' in checks.values()
            else 'uncertain'
            if 'uncertain' in checks.values()
            else 'passed'
        )
        statuses[status] += 1
        findings.extend(
            {'patch_id': row['id'], 'finding': value}
            for value in row['findings']
        )
    return {
        'directory': str(directory),
        'plan_sha256': actual_hash,
        'patch_count': len(expected),
        'status': 'passed'
        if statuses['passed'] == len(expected)
        and visual['status'] == 'compared'
        and overall_result == 'passed'
        and not unmerged
        else 'incomplete',
        'overall_status': visual['status'],
        'overall_result': overall_result,
        'overall_findings': visual['findings'],
        'unmerged_regions': unmerged,
        'counts': dict(statuses),
        'findings': findings,
    }


def merge_patch_reviews(directory, dataset=None):
    """Merge independent agent records only after validating their revision."""
    directory = Path(directory).resolve()
    root = directory / 'patches'
    index = read_json(root / 'index.json')
    index_hash = hashlib.sha256((root / 'index.json').read_bytes()).hexdigest()
    merged = {
        'schema_version': 2,
        'plan_sha256': index['plan_sha256'],
        'index_sha256': index_hash,
        'patches': [],
        'region_record_sha256': {},
    }
    for region in index['patches']:
        path = (root / region['review_file']).resolve()
        if not path.is_relative_to(root):
            raise LayoutError(
                'Agent review path must stay within the checkpoint'
            )
        report = read_json(path)
        rows = report.get('patches')
        if (
            report.get('plan_sha256') != index['plan_sha256']
            or report.get('index_sha256') != index_hash
            or not isinstance(rows, list)
            or len(rows) != 1
            or not isinstance(rows[0], dict)
            or rows[0].get('id') != region['id']
        ):
            raise LayoutError(
                'Agent review must match its assigned patch and revision'
            )
        merged['patches'].extend(rows)
        merged['region_record_sha256'][region['id']] = hashlib.sha256(
            path.read_bytes()
        ).hexdigest()
    validate_patch_review(directory, dataset=dataset, review=merged)
    previous = read_json(root / 'review.json')
    if previous['patches'] != merged['patches']:
        overall_path = directory / 'review.json'
        overall = read_json(overall_path)
        overall['visual_review'] = {
            'status': 'pending',
            'result': 'pending',
            'findings': [],
        }
        write_json_atomic(overall_path, overall)
    write_json_atomic(root / 'review.json', merged)
    return validate_patch_review(directory, dataset=dataset)


def record_region_review(directory, region_id, checks, findings, dataset=None):
    """Write one assigned region record without touching other agents' files."""
    directory = Path(directory).resolve()
    root = directory / 'patches'
    index = read_json(root / 'index.json')
    region = next(
        (item for item in index['patches'] if item['id'] == region_id), None
    )
    if region is None:
        raise LayoutError(f'Unknown review region: {region_id}')
    review = read_json(root / 'review.json')
    row = {'id': region_id, 'checks': checks, 'findings': findings}
    checked = {
        **review,
        'patches': [
            row if old['id'] == region_id else old for old in review['patches']
        ],
    }
    validate_patch_review(directory, dataset=dataset, review=checked)
    target = (root / region['review_file']).resolve()
    if not target.is_relative_to(root):
        raise LayoutError('Region review path is outside the checkpoint')
    write_json_atomic(target, {**review, 'patches': [row]})
    return {'path': str(target), 'region': region_id, 'checks': checks}


def record_overall_review(directory, result, findings, dataset=None):
    directory = Path(directory).resolve()
    report = validate_checkpoint_files(directory, dataset or Dataset())
    if report['stage'] == 'final':
        validate_patch_review(directory, dataset=dataset)
    if result not in ['passed', 'needs_change', 'uncertain']:
        raise LayoutError(
            'Overall result must be passed, needs_change or uncertain'
        )
    if result != 'passed' and not findings:
        raise LayoutError(
            'Failed or uncertain overall checks need concrete findings'
        )
    path = directory / 'review.json'
    report['visual_review'] = {
        'status': 'compared',
        'result': result,
        'findings': findings,
    }
    write_json_atomic(path, report)
    return (
        validate_patch_review(directory, dataset=dataset)
        if report['stage'] == 'final'
        else report['visual_review']
    )
