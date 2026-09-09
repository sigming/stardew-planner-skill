import hashlib
import os
from datetime import UTC, datetime
from pathlib import Path

from layout_core import LayoutError, read_json, write_json_atomic
from layout_review import (
    REVIEW_CHECKS,
    validate_checkpoint_files,
    validate_patch_review,
)


def plan_path(value):
    path = Path(value).resolve()
    if path.is_dir():
        directory = path
        path = directory / 'plan.json'
        if not path.is_file():
            candidates = []
            for state in [
                *directory.glob('*.progress.json'),
                *directory.glob('*/*.progress.json'),
            ]:
                record = read_json(state)
                candidate = (state.parent / record.get('plan', '')).resolve()
                if (
                    candidate.is_file()
                    and candidate.parent == state.parent.resolve()
                ):
                    candidates.append(candidate)
            candidates = sorted(set(candidates))
            if len(candidates) == 1:
                path = candidates[0]
            elif candidates:
                raise LayoutError(
                    'Multiple saved plans found; pass one plan path: '
                    + ', '.join(map(str, candidates))
                )
    if not path.is_file():
        raise LayoutError(
            f'Plan not found: {path}; pass its file or work directory'
        )
    return path


def progress_path(plan):
    return Path(plan).with_suffix('.progress.json')


def file_hash(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def relative(path, base):
    return os.path.relpath(Path(path).resolve(), base)


def record_progress(
    path,
    action,
    *,
    stage=None,
    checkpoint=None,
    delivery=None,
    reference=None,
    calibration=None,
    message=None,
    next_action=None,
):
    path = plan_path(path)
    destination = progress_path(path)
    if destination.is_file():
        previous = read_json(destination)
        if (
            previous.get('schema_version') != 1
            or previous.get('plan') != path.name
        ):
            raise LayoutError(
                f'Progress file does not belong to this plan: {destination}'
            )
    else:
        previous = {
            'schema_version': 1,
            'plan': path.name,
            'history': [],
            'notes': [],
        }
    updated = {
        **previous,
        'updated_at': datetime.now(UTC).isoformat(timespec='seconds'),
        'plan_sha256': file_hash(path),
        'last_action': action,
    }
    if stage:
        updated['stage'] = stage
    for key, value in [
        ('checkpoint', checkpoint),
        ('delivery', delivery),
        ('reference', reference),
        ('calibration', calibration),
    ]:
        if value:
            updated[key] = relative(value, path.parent)
    if message:
        updated['notes'] = [
            *previous.get('notes', []),
            {
                'at': updated['updated_at'],
                'stage': updated.get('stage'),
                'message': message,
                'next': next_action,
            },
        ]
    updated['history'] = [
        *previous.get('history', []),
        {
            'at': updated['updated_at'],
            'action': action,
            'stage': updated.get('stage'),
            'plan_sha256': updated['plan_sha256'],
        },
    ]
    write_json_atomic(destination, updated)
    return str(destination)


def record_checkpoint(
    path, directory, stage, reference=None, calibration=None
):
    path, directory = plan_path(path), Path(directory).resolve()
    report_path = directory / 'review.json'
    report = read_json(report_path)
    report['source_plan_path'] = relative(path, directory)
    # This identifies the input before normalization, and detects later edits.
    report['source_plan_sha256'] = file_hash(path)
    write_json_atomic(report_path, report)
    return record_progress(
        path,
        'checkpoint',
        stage=stage,
        checkpoint=directory,
        reference=reference,
        calibration=calibration,
    )


def record_review(directory, action):
    directory = Path(directory).resolve()
    report = read_json(directory / 'review.json')
    if report.get('source_plan_path'):
        source = (directory / report['source_plan_path']).resolve()
        if source.is_file():
            record_progress(
                source, action, checkpoint=directory, stage=report['stage']
            )


def _latest_checkpoint(path, progress):
    candidates = set()
    if progress.get('checkpoint'):
        candidates.add((path.parent / progress['checkpoint']).resolve())
    # Recover a completed checkpoint if execution stopped before the progress write.
    for report in (path.parent / 'stages').glob('*/review.json'):
        candidates.add(report.parent.resolve())
    matches = []
    for directory in candidates:
        report_path = directory / 'review.json'
        if not report_path.is_file():
            continue
        report = read_json(report_path)
        origin = report.get('source_plan_path')
        if origin and (directory / origin).resolve() != path:
            continue
        created = report.get('created_at')
        timestamp = (
            datetime.fromisoformat(created).timestamp()
            if created
            else report_path.stat().st_mtime
        )
        matches.append((timestamp, directory, report))
    return (
        max(matches, key=lambda entry: entry[0])[1:]
        if matches
        else (None, None)
    )


def work_status(value, dataset):
    path = plan_path(value)
    plan = read_json(path)
    destination = progress_path(path)
    progress = read_json(destination) if destination.is_file() else {}
    current_hash = file_hash(path)
    result = {
        'plan': str(path),
        'progress_file': str(destination),
        'layout': plan.get('layout'),
        'object_count': len(plan.get('objects', [])),
        'plan_sha256': current_hash,
        'unrecorded_plan_changes': bool(progress)
        and progress.get('plan_sha256') != current_hash,
        'last_action': progress.get('last_action'),
        'stage': progress.get('stage'),
        'state': 'planning',
        'notes': progress.get('notes', [])[-5:],
        'note_count': len(progress.get('notes', [])),
        'reference': str((path.parent / progress['reference']).resolve())
        if progress.get('reference')
        else None,
        'calibration': str((path.parent / progress['calibration']).resolve())
        if progress.get('calibration')
        else None,
        'next_action': 'Continue editing the plan, then create a checkpoint.',
    }
    directory, report = _latest_checkpoint(path, progress)
    if directory:
        result['checkpoint'] = str(directory)
        result['stage'] = report['stage']
        expected_source = report.get(
            'source_plan_sha256', report['plan_sha256']
        )
        try:
            validate_checkpoint_files(directory, dataset)
        except (ValueError, OSError, KeyError, TypeError) as error:
            result.update(
                state='checkpoint_stale',
                checkpoint_error=str(error),
                next_action='The checkpoint or its inputs changed; create a new checkpoint.',
            )
            return result
        if current_hash != expected_source:
            result.update(
                state='checkpoint_stale',
                next_action='The active plan changed; create a new checkpoint before review.',
            )
        elif report['stage'] != 'final':
            compared = (
                report.get('visual_review', {}).get('status') == 'compared'
            )
            result.update(
                state='planning' if compared else 'stage_review_pending',
                next_action='Continue placement and record the next stage.'
                if compared
                else 'Inspect the current stage image and record its visual review.',
            )
            if report.get('visual_review', {}).get('result') == 'needs_change':
                result.update(
                    state='needs_change',
                    next_action='Correct the recorded stage errors before continuing.',
                )
        else:
            try:
                review = validate_patch_review(directory, dataset=dataset)
                aggregate = read_json(directory / 'patches/review.json')
                index = read_json(directory / 'patches/index.json')
                aggregate_rows = {
                    row['id']: row for row in aggregate['patches']
                }
                pending, errors, ready_to_merge = [], [], []
                for region in index['patches']:
                    row = aggregate_rows[region['id']]
                    independent_path = (
                        directory / 'patches' / region['review_file']
                    )
                    if independent_path.is_file():
                        independent = read_json(independent_path)
                        if (
                            independent.get('plan_sha256')
                            == index['plan_sha256']
                            and independent.get('index_sha256')
                            == aggregate['index_sha256']
                        ):
                            rows = independent.get('patches', [])
                            if (
                                len(rows) == 1
                                and rows[0].get('id') == region['id']
                            ):
                                independent_row = rows[0]
                                checks = independent_row.get('checks', {})
                                if (
                                    set(checks) == set(REVIEW_CHECKS)
                                    and 'pending' not in checks.values()
                                    and independent_row != row
                                ):
                                    ready_to_merge.append(region['id'])
                                    row = independent_row
                    values = row['checks'].values()
                    if 'pending' in values:
                        pending.append(region['id'])
                    if 'needs_change' in values:
                        errors.append(region['id'])
                result.update(
                    review={
                        key: review[key]
                        for key in [
                            'status',
                            'counts',
                            'overall_status',
                            'overall_result',
                            'unmerged_regions',
                        ]
                    }
                    | {
                        'finding_count': len(review['findings']),
                        'record_path': str(directory / 'patches/review.json'),
                    },
                    pending_regions=pending,
                    regions_needing_change=errors,
                    completed_independent_regions=ready_to_merge,
                )
                if errors or review.get('overall_result') == 'needs_change':
                    result.update(
                        state='needs_change',
                        next_action='Correct the recorded errors and create a new checkpoint.',
                    )
                elif pending:
                    result.update(
                        state='region_review_pending',
                        next_action='Review only the pending regions; existing records are retained.',
                    )
                elif ready_to_merge or review.get('unmerged_regions'):
                    result.update(
                        state='merge_pending',
                        next_action='Run merge-review for this checkpoint, then perform the overall check.',
                    )
                elif (
                    review['overall_status'] != 'compared'
                    or review['overall_result'] == 'pending'
                ):
                    result.update(
                        state='overall_review_pending',
                        next_action='Inspect the whole layout and set the overall review status and result.',
                    )
                else:
                    result.update(
                        state='ready',
                        next_action='Deliver this reviewed version; retain unresolved findings in the output.',
                    )
            except (LayoutError, OSError, KeyError, TypeError) as error:
                result.update(
                    state='checkpoint_stale',
                    checkpoint_error=str(error),
                    next_action='The checkpoint or its inputs changed; create a new final checkpoint.',
                )
    if progress.get('delivery') and result['state'] == 'ready':
        delivery = (path.parent / progress['delivery']).resolve()
        marker = delivery / 'working/manifest.json'
        if marker.is_file():
            manifest = read_json(marker)
            snapshot_hash = file_hash(directory / 'plan.json')
            if (
                manifest.get('plan_sha256') == snapshot_hash
                and manifest.get('review', {}).get('patch_review') == review
                and manifest.get('files')
                and all(
                    (delivery / record['path']).is_file()
                    and file_hash(delivery / record['path'])
                    == record['sha256']
                    for record in manifest.get('files', [])
                )
            ):
                result.update(
                    state='delivered',
                    delivery=str(delivery),
                    next_action='The reviewed files are ready. Apply new requests to the active plan.',
                )
    return result
