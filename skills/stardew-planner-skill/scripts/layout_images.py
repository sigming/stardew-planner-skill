import hashlib
import os
import tempfile
from pathlib import Path

from layout_core import LayoutError
from PIL import Image, ImageDraw


def crop_image(source, destination, rect=None, scale=1, highlight_box=None):
    source, destination = Path(source).resolve(), Path(destination).resolve()
    if source == destination:
        raise LayoutError('Image output must not replace its source')
    if destination.exists():
        raise LayoutError('Image output exists; choose a new path')
    if type(scale) is not int or not 1 <= scale <= 4:
        raise LayoutError('Image scale must be an integer from 1 to 4')
    with Image.open(source) as original:
        x, y, width, height = rect or [0, 0, *original.size]
        if any(type(value) is not int for value in [x, y, width, height]) or (
            min(x, y) < 0
            or min(width, height) < 1
            or x + width > original.width
            or y + height > original.height
        ):
            raise LayoutError(
                'Image rectangle must stay within the source bounds'
            )
        cropped = original.convert('RGBA').crop((x, y, x + width, y + height))
        cropped = cropped.resize(
            (width * scale, height * scale), Image.Resampling.NEAREST
        )
    if highlight_box is not None:
        if (
            not isinstance(highlight_box, (list, tuple))
            or len(highlight_box) != 4
            or any(type(v) is not int for v in highlight_box)
            or not x <= highlight_box[0] < highlight_box[2] <= x + width
            or not y <= highlight_box[1] < highlight_box[3] <= y + height
        ):
            raise LayoutError(
                'Highlight box must be [left, top, right, bottom] within the crop in source pixels'
            )
        left, top, right, bottom = highlight_box
        ImageDraw.Draw(cropped).rectangle(
            (
                (left - x) * scale,
                (top - y) * scale,
                (right - x) * scale - 1,
                (bottom - y) * scale - 1,
            ),
            outline='#e53935',
            width=max(2, scale),
        )
    if max(cropped.size) > 4096 or cropped.width * cropped.height > 12_000_000:
        raise LayoutError(
            'Requested image is too large; use a smaller crop or scale'
        )
    destination.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary = tempfile.mkstemp(
        prefix='.image-', dir=destination.parent
    )
    os.close(descriptor)
    try:
        cropped.save(temporary, format='PNG', optimize=True)
        os.replace(temporary, destination)
    finally:
        Path(temporary).unlink(missing_ok=True)
    size = destination.stat().st_size
    return {
        'path': str(destination),
        'source': str(source),
        'source_rect_px': [x, y, width, height],
        'highlight_box_px': list(highlight_box) if highlight_box else None,
        'size_px': list(cropped.size),
        'bytes': size,
        'base64_bytes': 4 * ((size + 2) // 3),
        'sha256': hashlib.sha256(destination.read_bytes()).hexdigest(),
    }


def recognition_crops(plan, reference, directory):
    """Extract unresolved identities without assigning a guessed object ID."""
    records = []
    issues = [
        issue
        for issue in plan.get('review_issues', [])
        if issue['kind'] == 'identity' and issue['status'] == 'unresolved'
    ]
    for index, issue in enumerate(issues, 1):
        record = {'issue_id': issue['id'], 'description': issue['description']}
        box = issue.get('reference_box_px')
        if not reference or not box:
            records.append(
                {
                    **record,
                    'status': 'missing_reference'
                    if not reference
                    else 'missing_reference_box',
                }
            )
            continue
        with Image.open(reference) as source:
            width, height = source.size
        if box[2] > width or box[3] > height:
            raise LayoutError(
                f'Reference box for {issue["id"]} is outside the source image'
            )
        left, top = max(0, box[0] - 24), max(0, box[1] - 24)
        right, bottom = min(width, box[2] + 24), min(height, box[3] + 24)
        filename = f'unidentified-{index:03d}.png'
        result = crop_image(
            reference,
            Path(directory) / filename,
            [left, top, right - left, bottom - top],
            highlight_box=box,
        )
        records.append(
            {
                **record,
                **{
                    key: value
                    for key, value in result.items()
                    if key != 'path'
                },
                'path': 'working/recognition/' + filename,
                'reference_box_px': box,
                'source_sha256': hashlib.sha256(
                    Path(reference).read_bytes()
                ).hexdigest(),
                'status': 'extracted',
            }
        )
    return records
