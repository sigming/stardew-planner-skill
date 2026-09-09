# /// script
# requires-python = ">=3.12"
# dependencies = ["pillow==12.3.0", "pyyaml==6.0.2"]
# ///

# ruff: noqa: E402

import argparse
import hashlib
import json
import re
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1] / 'skills/stardew-planner-skill'
sys.path.insert(0, str(ROOT / 'scripts'))

import yaml
from layout_core import Dataset, generate_plan, normalize_plan, require_valid
from layout_coverage import coverage
from layout_render import render_plan
from PIL import Image
from recognition_categories import (
    atlas_families,
    atlas_items,
    load_groups,
    load_supplemental,
)


def require(condition, message):
    if not condition:
        raise ValueError(message)


def resource(root, name):
    require(isinstance(name, str), 'Resource path must be a string')
    path = (root / name).resolve()
    require(
        path.is_relative_to(root.resolve()),
        f'Resource outside package: {name}',
    )
    require(path.is_file(), f'Missing resource: {name}')
    return path


def skill_metadata(root, tag=None):
    root = root.resolve()
    skill = resource(root, 'SKILL.md').read_text(encoding='utf-8')
    match = re.match(r'^---\n(.*?)\n---(?:\n|$)', skill, re.DOTALL)
    require(match is not None, 'SKILL.md has no YAML frontmatter')
    metadata = yaml.safe_load(match.group(1))
    require(isinstance(metadata, dict), 'Skill frontmatter must be a mapping')
    name = metadata.get('name', '')
    require(
        isinstance(name, str)
        and re.fullmatch(r'[a-z0-9]+(?:-[a-z0-9]+)*', name) is not None,
        'Invalid skill name',
    )
    require(len(name) <= 64, 'Skill name is too long')
    description = metadata.get('description', '')
    require(
        isinstance(description, str) and 1 <= len(description) <= 1024,
        'Invalid skill description',
    )
    extra = metadata.get('metadata')
    require(isinstance(extra, dict), 'Missing skill metadata')
    version = extra.get('version')
    require(
        isinstance(version, str)
        and re.fullmatch(
            r'(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)', version
        )
        is not None,
        'Skill metadata.version must be a version string such as 1.0.0',
    )
    if tag is not None:
        require(tag == f'v{version}', f'Release tag must be v{version}')
    return metadata


def check(root, tag=None):
    root = root.resolve()
    metadata = skill_metadata(root, tag)
    name = metadata['name']
    version = metadata['metadata']['version']
    ui = yaml.safe_load(
        resource(root, 'agents/openai.yaml').read_text(encoding='utf-8')
    )
    require(
        '$' + name in ui['interface']['default_prompt'],
        'Default prompt does not reference the skill',
    )
    require(
        25 <= len(ui['interface']['short_description']) <= 64,
        'UI description must be 25–64 characters',
    )
    for filename in [
        'scripts/layout.py',
        'scripts/layout_progress.py',
        'scripts/layout_images.py',
        'scripts/map_rules.py',
        'scripts/layout_core.py',
        'scripts/layout_placement.py',
        'scripts/layout_review.py',
        'scripts/layout_assets.py',
        'scripts/layout_coverage.py',
        'scripts/layout_import.py',
        'scripts/layout_render.py',
        'scripts/layout_delivery.py',
        'scripts/layout_landmarks.py',
        'scripts/layout_style.py',
        'scripts/layout_visuals.py',
        'scripts/layout_checkpoint.py',
        'scripts/recognition_categories.py',
        'scripts/query_catalog.py',
        'LICENSE',
        '.python-version',
        'data/recognition/groups.json',
        'data/catalog-overrides.json',
        'examples/delivery-demo.recipe.json',
    ]:
        resource(root, filename)
    dataset = Dataset(root)
    for guide in dataset.recognition_guides.values():
        for reference in guide.get('reference_images', []):
            path = resource(root, reference['image'])
            require(
                hashlib.sha256(path.read_bytes()).hexdigest()
                == reference['sha256'],
                f'Recognition reference hash mismatch: {path}',
            )
            with Image.open(path) as image:
                image.verify()
    config = load_groups(root)
    supplemental = load_supplemental(root)
    recognition = json.loads(
        resource(root, 'data/recognition/index.json').read_text(
            encoding='utf-8'
        )
    )
    for source, key in [
        ('data/planner/catalog.json', 'catalog_sha256'),
        ('data/recognition/groups.json', 'groups_sha256'),
        ('data/catalog-overrides.json', 'overrides_sha256'),
        ('data/recognition/supplemental.json', 'supplemental_sha256'),
    ]:
        require(
            hashlib.sha256(resource(root, source).read_bytes()).hexdigest()
            == recognition[key],
            f'Recognition index is stale: {source}',
        )
    expected = {
        item['id']
        for items in atlas_items(
            dataset.catalog, config, supplemental
        ).values()
        for item in items
    }
    require(
        set(recognition['items']) == expected,
        'Recognition sheets do not cover the selected categories',
    )
    recognition_pages = 0
    page_sizes = {}
    for group in recognition['groups'].values():
        for page in group['pages']:
            path = resource(root, page['path'])
            require(
                hashlib.sha256(path.read_bytes()).hexdigest()
                == page['sha256'],
                f'Recognition sheet hash mismatch: {page["path"]}',
            )
            with Image.open(path) as image:
                require(
                    list(image.size) == page['size_px'],
                    'Recognition sheet size mismatch',
                )
                image.verify()
            page_sizes[page['path']] = page['size_px']
            recognition_pages += 1
    for group, families in atlas_families(
        dataset.catalog, config, supplemental
    ).items():
        expected_order = []
        for family in families:
            cards = [
                recognition['items'][item['id']] for item in family['items']
            ]
            require(
                [card['card'] for card in cards]
                == list(
                    range(cards[0]['card'], cards[0]['card'] + len(cards))
                ),
                f'Recognition family is not contiguous: {group}/{family["id"]}',
            )
            expected_order.extend(item['id'] for item in family['items'])
            for card in cards:
                require(
                    card['group'] == group
                    and card['family_id'] == family['id'],
                    f'Recognition family mismatch: {card["id"]}',
                )
                width, height = page_sizes[card['sheet']]
                left, top, right, bottom = card['card_bounds_px']
                require(
                    0 <= left < right <= width and 0 <= top < bottom <= height,
                    f'Recognition card outside sheet: {card["id"]}',
                )
        actual_order = [
            card['id']
            for card in sorted(
                recognition['items'].values(), key=lambda card: card['card']
            )
            if card['group'] == group
        ]
        require(
            actual_order == expected_order,
            f'Recognition family order differs: {group}',
        )
    supplemental = load_supplemental(root)
    require(
        not recognition['coverage']['missing_ids'],
        'Incomplete catalog recognition index',
    )
    asset_audit = json.loads(
        resource(root, 'data/recognition/coverage-audit.json').read_text()
    )
    require(
        not asset_audit['unresolved_ids'],
        'Source item audit has unresolved entries',
    )
    for filename, key in [
        ('data/planner/catalog.json', 'catalog_sha256'),
        ('data/recognition/index.json', 'recognition_index_sha256'),
        ('data/recognition/source-index.json', 'source_index_sha256'),
    ]:
        require(
            hashlib.sha256(resource(root, filename).read_bytes()).hexdigest()
            == asset_audit[key],
            f'Item audit is stale: {filename}; run maintenance/audit_catalog.py',
        )
    for item in [
        *supplemental['items'].values(),
        *supplemental.get('reference_entities', {}).values(),
    ]:
        require(
            item['id'] in recognition['lookup'],
            f'Missing supplemental card: {item["id"]}',
        )
        if item.get('recognition_action') == 'omit':
            require(
                item['id'] not in dataset.items
                and item.get('placement_supported') is False
                and recognition['items'][item['id']].get('recognition_action')
                == 'omit'
                and recognition['items'][item['id']]['footprint_tiles']
                is None,
                f'Recognition-only entity is placeable: {item["id"]}',
            )
        else:
            require(
                item['planner_type'] in dataset.items,
                'Unknown supplemental substitution candidate',
            )
        for asset in item['sprites'].values():
            path = resource(root, asset['path'])
            require(
                hashlib.sha256(path.read_bytes()).hexdigest()
                == asset['sha256'],
                f'Supplemental sprite hash mismatch: {path}',
            )
            with Image.open(path) as image:
                require(
                    list(image.size) == asset['size_px'],
                    'Supplemental sprite dimensions mismatch',
                )
                image.verify()
    crafting_audit = json.loads(
        resource(root, 'data/recognition/crafting-audit.json').read_text()
    )
    require(
        not crafting_audit['unresolved_names'],
        'Crafting categories contain missing items',
    )
    for filename, key in [
        ('data/recognition/crafting-source.json', 'source_snapshot_sha256'),
        ('data/planner/catalog.json', 'catalog_sha256'),
        ('data/recognition/index.json', 'recognition_index_sha256'),
    ]:
        require(
            hashlib.sha256(resource(root, filename).read_bytes()).hexdigest()
            == crafting_audit[key],
            f'Crafting audit is stale: {filename}; run maintenance/audit_crafting.py',
        )
    planner_root = root / 'data/planner'
    atlas_path = resource(planner_root, 'atlas.png')
    require(
        hashlib.sha256(atlas_path.read_bytes()).hexdigest()
        == dataset.catalog['sources']['atlas']['sha256'],
        'Sprite atlas hash mismatch',
    )
    with Image.open(atlas_path) as atlas:
        atlas.verify()
    for item in dataset.items.values():
        if decoration := item.get('building_placement', {}).get(
            'front_decoration'
        ):
            path = resource(root, decoration['image'])
            require(
                hashlib.sha256(path.read_bytes()).hexdigest()
                == decoration['sha256'],
                f'Building decoration hash mismatch: {path}',
            )
            with Image.open(path) as image:
                _, _, width, height = decoration['tile_area']
                require(
                    image.size == (width * 16, height * 16),
                    'Building decoration dimensions mismatch',
                )
                image.verify()
        image_path = resource(root, 'data/planner/' + item['image']['path'])
        require(
            hashlib.sha256(image_path.read_bytes()).hexdigest()
            == item['image']['sha256'],
            f'Image hash mismatch: {item["id"]}',
        )
        with Image.open(image_path) as image:
            require(
                image.size
                == (item['image']['width_px'], item['image']['height_px']),
                f'Image size mismatch: {item["id"]}',
            )
            image.verify()
        if item['effect']:
            resource(planner_root, item['effect']['image_path'])
    require(
        dataset.map_catalog['source']['sha256']
        == dataset.catalog['sources']['planner_bundle']['sha256'],
        'Map and item versions differ',
    )
    backgrounds = set()
    for map_id, entry in dataset.maps.items():
        dataset.blocked(map_id)
        require(
            entry.get('vanilla', False), f'Non-vanilla map included: {map_id}'
        )
        if entry.get('category', 'farm') == 'farm':
            require(
                bool(entry.get('fixed_landmarks')),
                f'Missing fixed map references: {map_id}',
            )
            require(
                any(
                    item['type'] == 'greenhouse-repaired'
                    for item in entry['default_objects']
                ),
                f'Missing repaired greenhouse: {map_id}',
            )
        for background in entry['backgrounds'].values():
            path = resource(root / 'data/maps', background['path'])
            with Image.open(path) as image:
                require(
                    image.size == (entry['width_px'], entry['height_px']),
                    f'Map size mismatch: {map_id}',
                )
                image.verify()
            backgrounds.add(background['path'])
    recipe = json.loads(
        resource(root, 'examples/delivery-demo.recipe.json').read_text(
            encoding='utf-8'
        )
    )
    plan = generate_plan(dataset, 'forest', recipe)
    effect = coverage(dataset, plan)
    require(effect['fully_covered'], 'Bundled recipe fails sprinkler coverage')
    restored = normalize_plan(dataset, json.loads(json.dumps(plan)))
    require_valid(dataset, restored)
    require(restored == plan, 'Local plan serialization changed the layout')
    with tempfile.TemporaryDirectory(
        prefix='stardew-install-check-'
    ) as temporary:
        for mode in ['render', 'blueprint']:
            output = Path(temporary) / f'{mode}.png'
            render_plan(dataset, plan, output, scale=1, mode=mode)
            with Image.open(output) as image:
                image.verify()
    return {
        'passed': True,
        'skill': name,
        'version': version,
        'items': len(dataset.items),
        'maps': len(dataset.maps),
        'background_images': len(backgrounds),
        'recognition_items': len(expected),
        'recognition_pages': recognition_pages,
        'source_item_audit': {
            'checked': asset_audit['checked_entry_count'],
            'counts': asset_audit['counts'],
        },
        'crafting_audit': {
            'checked': crafting_audit['checked_item_count'],
            'counts': crafting_audit['counts'],
        },
        'sample_object_count': len(plan['objects']),
        'sample_coverage': f'{effect["covered_target_cell_count"]}/{effect["target_cell_count"]}',
        'network_access_required_for_checks': False,
    }


def main():
    parser = argparse.ArgumentParser(
        description='Check skill metadata, bundled assets and local generation.'
    )
    parser.add_argument('--root', type=Path, default=ROOT)
    parser.add_argument(
        '--tag', help='Require a release tag matching the skill version'
    )
    args = parser.parse_args()
    try:
        print(
            json.dumps(
                check(args.root, args.tag), ensure_ascii=False, indent=2
            )
        )
    except (ValueError, KeyError, OSError, yaml.YAMLError) as error:
        print(
            json.dumps(
                {'passed': False, 'error': str(error)}, ensure_ascii=False
            )
        )
        raise SystemExit(1) from error


if __name__ == '__main__':
    main()
