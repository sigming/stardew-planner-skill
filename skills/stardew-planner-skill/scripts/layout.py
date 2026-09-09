# /// script
# requires-python = ">=3.12"
# dependencies = ["pillow==12.3.0"]
# ///

import argparse
import copy
import json
import sys
from pathlib import Path

from layout_checkpoint import calibrate_reference, checkpoint, locate_reference
from layout_core import (
    Dataset,
    LayoutError,
    apply_operations,
    create_plan,
    generate_plan,
    normalize_plan,
    read_json,
    require_valid,
    validate_plan,
    write_json_atomic,
)
from layout_coverage import coverage
from layout_delivery import deliver
from layout_images import crop_image
from layout_import import import_planner
from layout_progress import (
    record_checkpoint,
    record_progress,
    record_review,
    work_status,
)
from layout_render import render_plan
from layout_review import (
    merge_patch_reviews,
    record_overall_review,
    record_region_review,
    validate_patch_review,
)
from recognition_categories import load_groups


def output(value, path=None):
    if path:
        write_json_atomic(path, value)
    print(json.dumps(value, ensure_ascii=False, indent=2))


def save_plan(path, plan, old_path=None, force=False, action='edit'):
    path = Path(path)
    same = old_path is not None and path.resolve() == Path(old_path).resolve()
    if path.exists() and not same and not force:
        raise LayoutError(
            f'File already exists: {path}; use --force to replace it'
        )
    if same:
        write_json_atomic(
            path.with_suffix(path.suffix + '.bak'), read_json(path)
        )
    write_json_atomic(path, plan)
    record_progress(path, action)


def parser():
    root = argparse.ArgumentParser(
        description='Create, edit, validate and render Stardew Planner layouts locally.'
    )
    commands = root.add_subparsers(dest='command', required=True)
    commands.add_parser('maps', help='List locally supported maps')
    status = commands.add_parser(
        'status',
        help='Read saved progress and inspect unfinished checks after resuming.',
    )
    status.add_argument('path', type=Path)
    note = commands.add_parser(
        'note', help='Save the current stage, decisions and next action.'
    )
    note.add_argument('path', type=Path)
    note.add_argument('--stage', required=True)
    note.add_argument('--message', required=True)
    note.add_argument('--next', dest='next_action')
    cropped = commands.add_parser(
        'crop-image', help='Extract a bounded region from a local image.'
    )
    cropped.add_argument('source', type=Path)
    cropped.add_argument(
        '--rect',
        nargs=4,
        type=int,
        required=True,
        metavar=('X', 'Y', 'WIDTH', 'HEIGHT'),
    )
    cropped.add_argument('--out', type=Path, required=True)
    cropped.add_argument('--scale', type=int, default=1)
    cropped.add_argument(
        '--box',
        nargs=4,
        type=int,
        metavar=('LEFT', 'TOP', 'RIGHT', 'BOTTOM'),
        help='Draw a red box using source-image pixel bounds inside --rect.',
    )
    review = commands.add_parser(
        'check-review', help='Verify every final image patch was reviewed'
    )
    review.add_argument('directory', type=Path)
    merged_review = commands.add_parser(
        'merge-review', help='Combine independent patch review results'
    )
    merged_review.add_argument('directory', type=Path)
    region_review = commands.add_parser(
        'review-region', help='Record checks for one inspected region.'
    )
    region_review.add_argument('directory', type=Path)
    region_review.add_argument('region_id')
    region_review.add_argument(
        '--checks', nargs='+', required=True, metavar='CHECK=STATUS'
    )
    region_review.add_argument('--finding', action='append', default=[])
    overall_review = commands.add_parser(
        'review-overall', help='Record the main agent overall inspection.'
    )
    overall_review.add_argument('directory', type=Path)
    overall_review.add_argument(
        '--result',
        choices=['passed', 'needs_change', 'uncertain'],
        required=True,
    )
    overall_review.add_argument('--finding', action='append', default=[])
    calibration = commands.add_parser(
        'calibrate',
        help='Fit screenshot coordinates from fixed terrain landmarks',
    )
    calibration.add_argument('reference', type=Path)
    calibration.add_argument('--points', type=Path, required=True)
    calibration.add_argument('--map', default='regular')
    calibration.add_argument('--out', type=Path, required=True)
    locate = commands.add_parser(
        'locate',
        help='Convert a calibrated reference pixel to footprint coordinates',
    )
    locate.add_argument('calibration', type=Path)
    locate.add_argument('--pixel', type=float, nargs=2, required=True)
    locate.add_argument('--type')
    locate.add_argument(
        '--point-kind',
        choices=['footprint', 'sprite', 'planner-anchor'],
        default='footprint',
    )
    new = commands.add_parser(
        'new', help='Create a plan with explicit default buildings'
    )
    new.add_argument('path', type=Path)
    new.add_argument('--map', default='regular')
    new.add_argument('--title', default='')
    new.add_argument('--season', default='spring')
    new.add_argument('--force', action='store_true')
    generate = commands.add_parser(
        'generate', help='Place recipe modules in valid terrain'
    )
    generate.add_argument('path', type=Path)
    generate.add_argument('--map', default='regular')
    generate.add_argument('--recipe', type=Path, required=True)
    generate.add_argument('--title', default='')
    generate.add_argument('--season', default='spring')
    generate.add_argument('--force', action='store_true')
    for action in ['add', 'update', 'remove', 'apply', 'set']:
        sub = commands.add_parser(action)
        sub.add_argument('path', type=Path)
        sub.add_argument('--out', type=Path)
        sub.add_argument('--force', action='store_true')
        if action == 'add':
            sub.add_argument('type')
            sub.add_argument('--id')
            sub.add_argument('--x', required=True, type=int)
            sub.add_argument('--y', required=True, type=int)
            sub.add_argument('--tag', action='append', default=[])
        elif action in ['update', 'remove']:
            sub.add_argument('id')
            if action == 'update':
                sub.add_argument('--x', type=int)
                sub.add_argument('--y', type=int)
                sub.add_argument('--type')
                sub.add_argument('--label')
                sub.add_argument('--tag', action='append')
        elif action == 'apply':
            sub.add_argument('operations', type=Path)
        else:
            sub.add_argument('--season')
            sub.add_argument('--title')
    for name in [
        'validate',
        'coverage',
        'render',
    ]:
        sub = commands.add_parser(name)
        sub.add_argument('path', type=Path)
        if name in ['validate', 'coverage']:
            sub.add_argument('--out', type=Path)
        if name == 'coverage':
            sub.add_argument(
                '--group',
                choices=['sprinkler', 'scarecrow', 'bee', 'junimo'],
                default='sprinkler',
            )
            sub.add_argument('--target-type', action='append')
            sub.add_argument('--target-tag')
        if name == 'coverage':
            sub.add_argument('--source-id', action='append')
            sub.add_argument('--require-full', action='store_true')
        if name == 'render':
            sub.add_argument('--out', type=Path, required=True)
            sub.add_argument(
                '--mode', choices=['render', 'blueprint'], default='render'
            )
            sub.add_argument('--scale', type=int, default=2)
            sub.add_argument('--grid', action='store_true')
            sub.add_argument(
                '--crop',
                nargs=4,
                type=int,
                metavar=('X', 'Y', 'WIDTH', 'HEIGHT'),
            )
            sub.add_argument('--font', type=Path)
    imported = commands.add_parser(
        'import', help='Convert an existing local planner JSON into a plan'
    )
    imported.add_argument('source', type=Path)
    imported.add_argument('--out', type=Path, required=True)
    imported.add_argument('--title', default='Imported planner layout')
    imported.add_argument('--force', action='store_true')
    stage = commands.add_parser(
        'checkpoint',
        help='Save the current plan and previews for visual comparison at one layout stage',
    )
    stage.add_argument('path', type=Path)
    stage.add_argument(
        '--stage', choices=load_groups()['workflow_order'], required=True
    )
    stage.add_argument('--out-dir', type=Path, required=True)
    stage.add_argument('--reference', type=Path)
    stage.add_argument('--calibration', type=Path)
    stage.add_argument(
        '--block-size', type=int, nargs=2, metavar=('WIDTH', 'HEIGHT')
    )
    stage.add_argument('--scale', type=int, default=2)
    stage.add_argument('--font', type=Path)
    delivery = commands.add_parser(
        'deliver', help='Generate the complete local delivery from one plan'
    )
    delivery.add_argument('path', type=Path)
    delivery.add_argument('--out-dir', type=Path)
    delivery.add_argument('--scale', type=int, default=2)
    delivery.add_argument(
        '--review-dir',
        type=Path,
        required=True,
        help='Final checkpoint after every patch has been compared',
    )
    delivery.add_argument(
        '--crop', nargs=4, type=int, metavar=('X', 'Y', 'WIDTH', 'HEIGHT')
    )
    delivery.add_argument('--font', type=Path)
    delivery.add_argument('--reference', type=Path)
    delivery.add_argument(
        '--force',
        action='store_true',
        help='Replace a previous delivery and retain its backup',
    )
    return root


def main():
    args = parser().parse_args()
    try:
        dataset = Dataset()
        command = args.command
        if command == 'status':
            output(work_status(args.path, dataset))
            return
        if command == 'note':
            record_progress(
                args.path,
                'note',
                stage=args.stage,
                message=args.message,
                next_action=args.next_action,
            )
            output(work_status(args.path, dataset))
            return
        if command == 'crop-image':
            output(
                crop_image(
                    args.source, args.out, args.rect, args.scale, args.box
                )
            )
            return
        if command == 'merge-review':
            output(merge_patch_reviews(args.directory, dataset=dataset))
            record_review(args.directory, 'merge-review')
            return
        if command == 'review-region':
            pairs = [value.split('=', 1) for value in args.checks]
            if any(len(pair) != 2 for pair in pairs) or len(
                {pair[0] for pair in pairs}
            ) != len(pairs):
                raise LayoutError('Use each CHECK=STATUS exactly once')
            output(
                record_region_review(
                    args.directory,
                    args.region_id,
                    dict(pairs),
                    args.finding,
                    dataset,
                )
            )
            return
        if command == 'review-overall':
            output(
                record_overall_review(
                    args.directory, args.result, args.finding, dataset
                )
            )
            record_review(args.directory, 'review-overall')
            return
        if command == 'check-review':
            report = validate_patch_review(args.directory, dataset=dataset)
            output(report)
            record_review(args.directory, 'check-review')
            if report['status'] != 'passed':
                raise SystemExit(1)
            return
        if command == 'calibrate':
            result = calibrate_reference(
                dataset, args.reference, read_json(args.points), args.map
            )
            if args.out.resolve() in [
                args.reference.resolve(),
                args.points.resolve(),
            ]:
                raise LayoutError(
                    'Calibration output must not overwrite its input'
                )
            output(result, args.out)
            return
        if command == 'locate':
            raw = read_json(args.calibration)
            checked = calibrate_reference(
                dataset, raw['reference_path'], raw['points'], raw['layout']
            )
            if checked['reference_sha256'] != raw['reference_sha256']:
                raise LayoutError('Reference image changed; recalibrate first')
            output(
                locate_reference(
                    dataset, checked, args.pixel, args.type, args.point_kind
                )
            )
            return
        if command == 'maps':
            output(
                [
                    {
                        'id': key,
                        'name_zh': value['name_zh'],
                        'name_en': value['name_en'],
                        'category': value.get('category', 'farm'),
                        'limitations': value.get('limitations', []),
                        'size_tiles': [
                            value['width_tiles'],
                            value['height_tiles'],
                        ],
                    }
                    for key, value in dataset.maps.items()
                ]
            )
            return
        if command == 'import':
            plan = import_planner(dataset, read_json(args.source), args.title)
            save_plan(args.out, plan, force=args.force, action='import')
            output({'path': str(args.out), 'objects': len(plan['objects'])})
            return
        if command in ['new', 'generate']:
            plan = (
                create_plan(dataset, args.map, args.title, args.season)
                if command == 'new'
                else generate_plan(
                    dataset,
                    args.map,
                    read_json(args.recipe),
                    args.title,
                    args.season,
                )
            )
            report = require_valid(dataset, plan)
            save_plan(args.path, plan, force=args.force, action=command)
            output({'path': str(args.path.resolve()), 'validation': report})
            return
        plan = normalize_plan(dataset, read_json(args.path))
        if (
            command
            in [
                'validate',
                'coverage',
                'render',
            ]
            and args.out
            and args.out.resolve() == args.path.resolve()
        ):
            raise LayoutError(
                'Derived output must not overwrite the source plan'
            )
        if command in ['add', 'update', 'remove', 'apply', 'set']:
            if command == 'add':
                item = {
                    'type': args.type,
                    'x': args.x,
                    'y': args.y,
                    'tags': args.tag,
                }
                if args.id:
                    item['id'] = args.id
                operations = [{'op': 'add', 'item': item}]
            elif command == 'update':
                changes = {
                    key: getattr(args, key)
                    for key in ['x', 'y', 'type', 'label']
                    if getattr(args, key) is not None
                }
                if args.tag is not None:
                    changes['tags'] = args.tag
                if not changes:
                    raise LayoutError('No update fields supplied')
                operations = [{'op': 'update', 'id': args.id, 'set': changes}]
            elif command == 'remove':
                operations = [{'op': 'remove', 'id': args.id}]
            elif command == 'apply':
                raw = read_json(args.operations)
                operations = (
                    raw['operations'] if isinstance(raw, dict) else raw
                )
            else:
                changed = copy.deepcopy(plan)
                for key in ['season', 'title']:
                    if getattr(args, key) is not None:
                        changed[key] = getattr(args, key)
                plan = normalize_plan(dataset, changed)
                operations = []
            plan = apply_operations(dataset, plan, operations)
            destination = args.out or args.path
            save_plan(destination, plan, args.path, args.force)
            output(
                {
                    'path': str(destination.resolve()),
                    'objects': len(plan['objects']),
                    'validation': validate_plan(dataset, plan),
                }
            )
        elif command == 'validate':
            result = validate_plan(dataset, plan)
            output(result, args.out)
            if not result['passed']:
                raise SystemExit(1)
        elif command == 'coverage':
            require_valid(dataset, plan)
            result = coverage(
                dataset,
                plan,
                args.group,
                args.target_type,
                args.target_tag,
                args.source_id,
            )
            output(result, args.out)
            if args.require_full and not result['requirement_met']:
                raise SystemExit(1)
        elif command == 'render':
            output(
                render_plan(
                    dataset,
                    plan,
                    args.out,
                    args.mode,
                    args.scale,
                    args.grid,
                    args.crop,
                    args.font,
                )
            )
        elif command == 'checkpoint':
            result = checkpoint(
                dataset,
                plan,
                args.stage,
                args.out_dir,
                args.reference,
                args.scale,
                args.font,
                args.calibration,
                args.block_size,
            )
            record_checkpoint(
                args.path,
                args.out_dir,
                args.stage,
                args.reference,
                args.calibration,
            )
            output(result)
        elif command == 'deliver':
            result = deliver(
                dataset,
                plan,
                args.out_dir,
                args.scale,
                args.crop,
                args.font,
                args.force,
                review_directory=args.review_dir,
                reference=args.reference,
            )
            record_progress(
                args.path,
                'deliver',
                stage='final',
                delivery=result['directory'],
                checkpoint=args.review_dir,
            )
            output(
                {
                    'directory': result['directory'],
                    'plan_path': result['plan_path'],
                    'local_directory': result['local_directory'],
                    'backup': result['backup'],
                    'object_count': result['manifest']['object_count'],
                    'placeholder_crop_count': result['manifest'][
                        'placeholder_crop_count'
                    ],
                    'fixed_landmark_count': result['manifest'][
                        'fixed_landmark_count'
                    ],
                    'edge_reference_count': result['manifest'][
                        'edge_reference_count'
                    ],
                    'review': result['manifest']['review'],
                    'coverage': result['manifest']['coverage'],
                }
            )
    except (ValueError, TypeError, KeyError, OSError) as error:
        print(
            json.dumps({'error': str(error)}, ensure_ascii=False),
            file=sys.stderr,
        )
        raise SystemExit(2) from error


if __name__ == '__main__':
    main()
