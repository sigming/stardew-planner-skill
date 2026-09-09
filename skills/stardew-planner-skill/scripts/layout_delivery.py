import hashlib
import json
import os
import shutil
import tempfile
import uuid
from pathlib import Path

from layout_core import (
    LayoutError,
    normalize_plan,
    read_json,
    require_valid,
    write_json_atomic,
)
from layout_coverage import audit_coverage
from layout_images import recognition_crops
from layout_render import render_plan
from layout_review import validate_patch_review

DELIVERY_FORMAT = 'stardew-layout-delivery-v4'
REPLACEABLE_DELIVERY_FORMATS = {
    'stardew-layout-delivery-v3',
    DELIVERY_FORMAT,
}


def _file_records(directory):
    return [
        {
            'path': path.relative_to(directory).as_posix(),
            'bytes': path.stat().st_size,
            'sha256': hashlib.sha256(path.read_bytes()).hexdigest(),
        }
        for path in sorted(directory.rglob('*'))
        if path.is_file()
    ]


def deliver(
    dataset,
    original,
    directory=None,
    scale=2,
    crop=None,
    font_path=None,
    force=False,
    workspace=None,
    review_directory=None,
    reference=None,
):
    plan = normalize_plan(dataset, original)
    validation = require_valid(dataset, plan)
    audit = audit_coverage(dataset, plan)
    workspace = Path(workspace or Path.cwd()).resolve()
    directory = Path(directory or workspace / 'stardew-layout').resolve()
    if directory == workspace or not directory.is_relative_to(workspace):
        raise LayoutError(
            'Delivery must use a new subfolder of the current workspace'
        )
    if directory.exists():
        marker = directory / 'working/manifest.json'
        try:
            previous = json.loads(marker.read_text(encoding='utf-8'))
        except (OSError, ValueError):
            previous = {}
        if (
            not force
            or previous.get('format') not in REPLACEABLE_DELIVERY_FORMATS
        ):
            raise LayoutError(
                'Output exists; --force only replaces a prior delivery folder and keeps a backup'
            )
    directory.parent.mkdir(parents=True, exist_ok=True)
    stage = Path(
        tempfile.mkdtemp(prefix=f'.{directory.name}.', dir=directory.parent)
    )
    local_stage = stage / 'working'
    local_stage.mkdir()
    backup = None
    try:
        write_json_atomic(stage / 'plan.json', plan)
        plan_hash = hashlib.sha256(
            (stage / 'plan.json').read_bytes()
        ).hexdigest()
        patch_review = (
            validate_patch_review(review_directory, plan_hash, dataset)
            if review_directory
            else {'status': 'not_provided'}
        )
        if (
            patch_review.get('overall_status') == 'pending'
            or patch_review.get('overall_result')
            in ['pending', 'needs_change']
            or patch_review.get('counts', {}).get('pending')
            or patch_review.get('counts', {}).get('needs_change')
            or patch_review.get('unmerged_regions')
        ):
            raise LayoutError(
                'Final patch review has pending checks or known errors; inspect and correct the layout before delivery'
            )
        validation['plan_sha256'] = plan_hash
        write_json_atomic(local_stage / 'validation.json', validation)
        preview = render_plan(
            dataset,
            plan,
            stage / 'preview.png',
            scale=scale,
            crop=crop,
            font_path=font_path,
        )
        implementation = render_plan(
            dataset,
            plan,
            stage / 'implementation.png',
            mode='blueprint',
            scale=scale,
            font_path=font_path,
        )
        write_json_atomic(
            local_stage / 'diagram-legend.json',
            {
                'plan_sha256': plan_hash,
                'legend': implementation['legend'],
                'edge_references': implementation['edge_references'],
                'fixed_landmarks': plan['fixed_landmarks'],
                'additional_placement_areas': implementation[
                    'additional_placement_areas'
                ],
            },
        )
        for group, report in audit['groups'].items():
            report['plan_sha256'] = plan_hash
            write_json_atomic(local_stage / f'coverage-{group}.json', report)
        unresolved = [
            issue
            for issue in plan.get('review_issues', [])
            if issue['status'] == 'unresolved'
        ]
        source_record = (
            read_json(Path(review_directory) / 'review.json').get('reference')
            if review_directory
            else None
        )
        reference = reference or (source_record or {}).get('path')
        if (
            reference
            and source_record
            and hashlib.sha256(Path(reference).read_bytes()).hexdigest()
            != source_record['sha256']
        ):
            raise LayoutError(
                'Reference image changed since the final checkpoint'
            )
        crops = recognition_crops(plan, reference, local_stage / 'recognition')
        appearance_warnings = []
        for warning in validation['warnings']:
            if warning['code'] != 'unverified_appearance':
                continue
            accepted = any(
                issue.get('kind') == 'appearance'
                and issue.get('status') in ['accepted', 'resolved']
                and issue.get('sprite_id') == warning['sprite_id']
                and issue.get('image_sha256') == warning['image_sha256']
                and warning['id'] in issue.get('object_ids', [])
                for issue in plan.get('review_issues', [])
            )
            appearance_warnings.append(
                {**warning, 'requires_user_decision': not accepted}
            )
        review = {
            'plan_sha256': plan_hash,
            'patch_review': patch_review,
            'coverage_issues': audit['issues'],
            'recognition_issues': [
                issue for issue in unresolved if issue['kind'] != 'coverage'
            ],
            'recognition_crops': crops,
            'additional_coverage_issues': [
                issue for issue in unresolved if issue['kind'] == 'coverage'
            ],
            'appearance_warnings': appearance_warnings,
            'accepted_decisions': [
                issue
                for issue in plan.get('review_issues', [])
                if issue['status'] == 'accepted'
            ],
            'validation_warnings': validation['warnings'],
            'requires_user_decision': bool(
                patch_review.get('counts', {}).get('uncertain', 0)
                or patch_review.get('overall_result') == 'uncertain'
                or audit['requires_user_decision']
                or unresolved
                or any(
                    warning['requires_user_decision']
                    for warning in appearance_warnings
                )
            ),
            'completion_summary': {
                'coverage_gaps': audit['issues'],
                'unidentified_objects': crops,
                'instruction_zh': '在任务完成总结中说明覆盖缺口的设施类型、未覆盖格数和坐标，并附未识别物体的带框裁切图。保留复刻位置，由用户决定如何处理；勿代替用户补设施、移动作物或确认物体身份。',
            },
        }
        write_json_atomic(local_stage / 'review.json', review)
        coverage_summary = {
            group: {
                key: report[key]
                for key in [
                    'status',
                    'source_count',
                    'target_cell_count',
                    'covered_target_cell_count',
                    'fully_covered',
                ]
            }
            for group, report in audit['groups'].items()
        }
        manifest = {
            'format': DELIVERY_FORMAT,
            'layout': plan['layout'],
            'game_mode': 'vanilla',
            'plan_sha256': plan_hash,
            'object_count': len(plan['objects']),
            'placeholder_crop_count': sum(
                item.get('crop_kind') == 'placeholder'
                for item in plan['objects']
            ),
            'fixed_landmark_count': len(plan['fixed_landmarks']),
            'edge_reference_count': len(implementation['edge_references']),
            'image_coordinates': {
                name: {
                    'map_region_px': rendered['map_region_px'],
                    'coordinate_ticks': rendered['coordinate_ticks'],
                }
                for name, rendered in [
                    ('preview.png', preview),
                    ('implementation.png', implementation),
                ]
            },
            'coverage': coverage_summary,
            'review': review,
            'plan_path': 'plan.json',
            'files': _file_records(stage),
            'limitations': [
                'Coverage checks planner geometry; seasonal growth and other gameplay conditions are not fully simulated.',
                'All ordinary crops use strawberry placeholders; giant crops use giant pumpkins with their 3 x 3 footprints.',
            ],
        }
        write_json_atomic(local_stage / 'manifest.json', manifest)
        if directory.exists():
            backup = directory.with_name(
                f'{directory.name}.bak-{uuid.uuid4().hex[:8]}'
            )
            os.replace(directory, backup)
        try:
            os.replace(stage, directory)
        except OSError:
            if backup:
                os.replace(backup, directory)
            raise
        return {
            'directory': str(directory),
            'plan_path': str(directory / 'plan.json'),
            'local_directory': str(directory / 'working'),
            'backup': str(backup) if backup else None,
            'manifest': manifest,
        }
    finally:
        if stage.exists():
            shutil.rmtree(stage)
