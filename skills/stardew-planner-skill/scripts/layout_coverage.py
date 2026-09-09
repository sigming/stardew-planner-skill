import hashlib
import json
from collections import Counter

from layout_core import LayoutError

EFFECT_ORDER = ('sprinkler', 'scarecrow', 'bee', 'junimo')
FLOWER_TYPES = {
    'blue-jazz',
    'jazz-seeds',
    'tulip',
    'tulip-bulb',
    'poppy',
    'poppy-seeds',
    'summer-spangle',
    'spangle-seeds',
    'fairy-rose',
    'fairy-seeds',
    'sunflower',
    'sunflower-seeds',
}
SUGGESTIONS = {
    'sprinkler': [
        '用户可选择人工浇水，或调整洒水器和种植位',
    ],
    'scarecrow': ['用户可选择保留原布局，或调整稻草人和种植位'],
    'junimo': ['用户可选择人工收获，或调整祝尼魔小屋和种植区域'],
}


def coverage(
    dataset,
    plan,
    group='sprinkler',
    target_types=None,
    target_tag=None,
    source_ids=None,
):
    if group not in {'sprinkler', 'scarecrow', 'bee', 'junimo'}:
        raise LayoutError(f'Unknown effect group: {group}')
    selected_types = {dataset.item_id(value) for value in target_types or []}
    target_objects = []
    providers = []
    skipped_placeholders = []
    skipped_nonflowers = []
    for item in plan['objects']:
        metadata = dataset.items[item['type']]
        effect = metadata['effect']
        if (
            effect
            and effect['group'] == group
            and (not source_ids or item['id'] in source_ids)
        ):
            providers.append(item)
        type_matches = (
            item['type'] in selected_types
            if selected_types
            else metadata['category'] == 'crop'
            and metadata['subcategory'] != 'giant-crops'
        )
        if target_tag:
            type_matches = (
                not selected_types or item['type'] in selected_types
            ) and target_tag in item.get('tags', [])
        if (
            type_matches
            and group == 'bee'
            and item.get('crop_kind') == 'placeholder'
        ):
            skipped_placeholders.append(item['id'])
            continue
        if (
            type_matches
            and group == 'bee'
            and item['type'] not in FLOWER_TYPES
        ):
            skipped_nonflowers.append(item['id'])
            continue
        if type_matches:
            target_objects.append(item)
    if source_ids and set(source_ids) != {item['id'] for item in providers}:
        raise LayoutError(
            'One or more source IDs do not exist or belong to another effect group'
        )
    map_info = dataset.maps[plan['layout']]
    coverage_counts = Counter()
    by_source = []
    for item in providers:
        effect = dataset.items[item['type']]['effect']
        cells = {
            (item['x'] + dx, item['y'] + dy)
            for dx, dy in effect['target_cells_excluding_footprint']
        }
        cells = {
            (x, y)
            for x, y in cells
            if 0 <= x < map_info['width_tiles']
            and 0 <= y < map_info['height_tiles']
        }
        coverage_counts.update(cells)
        by_source.append(
            {
                'id': item['id'],
                'type': item['type'],
                'covered_cell_count': len(cells),
                'target_ids_in_range': [
                    target['id']
                    for target in target_objects
                    if dataset.cells(target) <= cells
                ],
            }
        )
    covered = set(coverage_counts)
    targets = (
        set().union(*(dataset.cells(item) for item in target_objects))
        if target_objects
        else set()
    )
    uncovered = targets - covered
    target_results = [
        {
            'id': item['id'],
            'fully_covered': dataset.cells(item) <= covered,
            'uncovered_cells': sorted(dataset.cells(item) - covered),
        }
        for item in target_objects
    ]
    result = {
        'mode': 'geometric',
        'group': group,
        'layout': plan['layout'],
        'source_count': len(providers),
        'target_object_count': len(target_objects),
        'target_cell_count': len(targets),
        'covered_target_cell_count': len(targets & covered),
        'coverage_ratio': len(targets & covered) / len(targets)
        if targets
        else None,
        'fully_covered': bool(targets) and not uncovered,
        'uncovered_cells': sorted(uncovered),
        'coverage_cells': sorted(covered),
        'overlapping_coverage_cells': sorted(
            cell for cell, count in coverage_counts.items() if count > 1
        ),
        'sources': by_source,
        'targets': target_results,
        'placeholder_targets_not_treated_as_flowers': skipped_placeholders,
        'nonflower_targets_skipped': skipped_nonflowers,
        'gameplay_verified': False,
        'limitations': [
            'Checks the planner range geometry. Crop eligibility, seasons, obstacles, buildings and game-specific effects are not simulated.'
        ]
        + (
            ['Beach sand watering restrictions are not evaluated.']
            if plan['layout'] == 'beach' and group == 'sprinkler'
            else []
        ),
    }

    result['requirement_met'] = group == 'bee' or result['fully_covered']
    if group == 'bee':
        result['flower_bonus_optional'] = True
        result['sources_without_flowers'] = [
            source['id']
            for source in by_source
            if not source['target_ids_in_range']
        ]
        result['note'] = (
            'Flowers are optional for honey production; flowering, season and maturity are not verified.'
        )
    return result


def delivery_targets(plan, group):
    all_targets = plan.get('coverage_targets', {})
    if not isinstance(all_targets, dict) or set(all_targets) - set(
        EFFECT_ORDER
    ):
        raise LayoutError(
            'coverage_targets supports sprinkler, scarecrow, bee and junimo'
        )
    config = all_targets.get(group, {})
    if not isinstance(config, dict) or set(config) - {
        'target_types',
        'target_tag',
    }:
        raise LayoutError(
            'Each coverage setting supports target_types and target_tag'
        )
    types, tag = config.get('target_types'), config.get('target_tag')
    if types is not None and (
        not isinstance(types, list)
        or not types
        or any(not isinstance(value, str) for value in types)
    ):
        raise LayoutError('target_types must be a nonempty array of item IDs')
    if tag is not None and (not isinstance(tag, str) or not tag.strip()):
        raise LayoutError('target_tag must be a nonempty string')
    return types, tag


def audit_coverage(dataset, plan):
    reports, issues = {}, []
    for group in EFFECT_ORDER:
        types, tag = delivery_targets(plan, group)
        report = coverage(dataset, plan, group, types, tag)
        report['target_selection'] = {'target_types': types, 'target_tag': tag}
        if group == 'bee':
            status = (
                'not_applicable'
                if not report['source_count']
                else 'optional_no_flowers'
                if not report['target_object_count']
                else 'optional_flower_gaps'
                if report['sources_without_flowers']
                else 'flowers_in_range'
            )
        elif (
            group == 'junimo'
            and not report['source_count']
            and group not in plan.get('coverage_targets', {})
        ):
            status = 'not_requested'
        elif not report['target_object_count']:
            status = (
                'targets_unspecified' if types or tag else 'not_applicable'
            )
        elif not report['source_count']:
            status = 'no_sources'
        else:
            status = 'covered' if report['fully_covered'] else 'gaps'
        report['status'] = status
        relevant_ids = {
            row['id'] for key in ['sources', 'targets'] for row in report[key]
        }
        report['fingerprint'] = hashlib.sha256(
            json.dumps(
                {
                    'group': group,
                    'layout': plan['layout'],
                    'season': plan['season'],
                    'selection': report['target_selection'],
                    'catalog_source': dataset.catalog['sources'][
                        'planner_bundle'
                    ]['sha256'],
                    'objects': sorted(
                        (
                            item
                            for item in plan['objects']
                            if item['id'] in relevant_ids
                        ),
                        key=lambda item: item['id'],
                    ),
                },
                sort_keys=True,
            ).encode()
        ).hexdigest()
        accepted = any(
            issue.get('kind') == 'coverage'
            and issue.get('status') == 'accepted'
            and issue.get('group') == group
            and issue.get('coverage_fingerprint') == report['fingerprint']
            for issue in plan.get('review_issues', [])
        )
        if group != 'bee' and status in [
            'gaps',
            'no_sources',
            'targets_unspecified',
        ]:
            issues.append(
                {
                    'code': 'coverage_' + status,
                    'group': group,
                    'uncovered_cell_count': len(report['uncovered_cells']),
                    'uncovered_cells': report['uncovered_cells'],
                    'object_ids': [
                        row['id']
                        for row in report['targets']
                        if not row['fully_covered']
                    ],
                    'suggestions': SUGGESTIONS[group],
                    'coverage_fingerprint': report['fingerprint'],
                    'requires_user_decision': not accepted,
                    'decision_status': 'accepted' if accepted else 'pending',
                    'report_in_completion_summary': True,
                    'preserve_reference_layout': True,
                }
            )
        reports[group] = report
    return {
        'groups': reports,
        'issues': issues,
        'requires_user_decision': any(
            issue['requires_user_decision'] for issue in issues
        ),
    }
