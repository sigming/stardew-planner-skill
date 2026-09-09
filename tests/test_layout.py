# /// script
# requires-python = ">=3.12"
# dependencies = ["pillow==12.3.0"]
# ///

import contextlib
import copy
import hashlib
import json
import subprocess
import sys
import tempfile
import unittest
from collections import Counter
from pathlib import Path
from unittest.mock import patch

from PIL import Image, ImageChops

sys.path.insert(
    0,
    str(
        Path(__file__).resolve().parents[1]
        / 'skills/stardew-planner-skill/scripts'
    ),
)

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'scripts'))

from build_recognition import build as build_recognition
from layout_assets import appearances, seasonal_id, sprite
from layout_checkpoint import calibrate_reference, checkpoint, locate_reference
from layout_core import (
    Dataset,
    LayoutError,
    apply_operations,
    create_plan,
    generate_plan,
    normalize_plan,
    validate_plan,
)
from layout_coverage import audit_coverage, coverage
from layout_delivery import deliver
from layout_import import import_planner
from layout_landmarks import edge_references
from layout_render import render_plan
from layout_review import REVIEW_CHECKS, review_regions, validate_patch_review
from layout_style import item_styles
from recognition_categories import (
    atlas_items,
    ignored_vegetation,
    load_supplemental,
    recognition_group,
)


class LayoutTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.data = Dataset()

    def setUp(self):
        self.plan = create_plan(self.data, 'standard')

    def apply(self, operations, plan=None):
        return apply_operations(self.data, plan or self.plan, operations)

    def test_defaults_on_all_maps(self):
        for name in self.data.maps:
            with self.subTest(map=name):
                self.assertTrue(
                    validate_plan(self.data, create_plan(self.data, name))[
                        'passed'
                    ]
                )

    def test_greenhouse_normalizes_to_repaired_without_moving(self):
        for value in [
            'greenhouse',
            'greenhouse-repaired',
            '温室',
            'Greenhouse',
            'Greenhouse (repaired)',
        ]:
            self.assertEqual(self.data.item_id(value), 'greenhouse-repaired')
        self.assertEqual(
            self.data.item_id('greenhouse', normalize=False), 'greenhouse'
        )
        for name in self.data.maps:
            if self.data.maps[name].get('category') != 'farm':
                continue
            plan = create_plan(self.data, name)
            building = next(
                item
                for item in plan['objects']
                if item['type'] == 'greenhouse-repaired'
            )
            self.assertEqual(self.data.dimensions(building['type']), (7, 6))
            before = copy.deepcopy(building)
            building['type'] = 'greenhouse'
            normalized = normalize_plan(self.data, plan)
            self.assertEqual(
                next(
                    item
                    for item in normalized['objects']
                    if item['id'] == building['id']
                ),
                before,
            )
            self.assertEqual(building['type'], 'greenhouse')
        updated = self.apply(
            [
                {
                    'op': 'update',
                    'id': 'default-greenhouse',
                    'set': {'type': 'greenhouse'},
                }
            ]
        )
        self.assertEqual(updated['objects'][1]['type'], 'greenhouse-repaired')

    def test_ignored_vegetation_is_omitted_idempotently(self):
        grass_ids = [
            key
            for key, item in self.data.items.items()
            if ignored_vegetation(item)
        ]
        self.assertTrue({'grass', 'blue-grass', 'weeds'} <= set(grass_ids))
        original = copy.deepcopy(self.plan)
        original['objects'].extend(
            {'id': f'ignored-{i}', 'type': key, 'x': 20, 'y': 24}
            for i, key in enumerate(grass_ids)
        )
        normalized = normalize_plan(self.data, original)
        self.assertEqual(normalized['objects'], self.plan['objects'])
        self.assertEqual(
            normalized['omitted_vegetation']['total'], len(grass_ids)
        )
        self.assertEqual(normalize_plan(self.data, normalized), normalized)
        self.assertEqual(
            len(original['objects']),
            len(self.plan['objects']) + len(grass_ids),
        )
        self.assertFalse(validate_plan(self.data, original)['passed'])
        with (
            tempfile.TemporaryDirectory() as temporary,
            contextlib.chdir(temporary),
        ):
            before, after = (
                Path(temporary) / 'before.png',
                Path(temporary) / 'after.png',
            )
            render_plan(self.data, self.plan, before, scale=1)
            render_plan(self.data, normalized, after, scale=1)
            with Image.open(before) as a, Image.open(after) as b:
                self.assertIsNone(ImageChops.difference(a, b).getbbox())
            with self.assertRaises(LayoutError):
                render_plan(
                    self.data,
                    original,
                    Path(temporary) / 'invalid.png',
                    scale=1,
                )

    def test_grass_import_recipe_and_explicit_operations(self):
        payload = {
            'tiles': [
                {'type': key, 'x': 320, 'y': 384}
                for key in ['grass', 'blue-grass', 'weeds']
            ]
        }
        restored = import_planner(self.data, payload)
        self.assertEqual(restored['omitted_vegetation']['total'], 3)
        self.assertEqual(restored['objects'], self.plan['objects'])
        recipe = {
            'schema_version': 1,
            'modules': [
                {
                    'id': 'grass-only',
                    'objects': [{'type': 'grass', 'x': 0, 'y': 0}],
                },
                {
                    'id': 'mixed',
                    'objects': [
                        {'type': 'blue-grass', 'x': 0, 'y': 0},
                        {'type': 'chest', 'x': 0, 'y': 0},
                    ],
                },
            ],
        }
        generated = generate_plan(self.data, 'regular', recipe)
        self.assertEqual(
            generated['omitted_vegetation']['counts'],
            {'blue-grass': 1, 'grass': 1},
        )
        self.assertEqual(len(generated['objects']), 3)
        for operation in [
            {'op': 'add', 'item': {'type': 'grass', 'x': 20, 'y': 24}},
            {'op': 'fill', 'type': 'blue-grass', 'rect': [20, 24, 2, 2]},
            {
                'op': 'update',
                'id': 'default-greenhouse',
                'set': {'type': 'weeds'},
            },
        ]:
            with (
                self.subTest(operation=operation),
                self.assertRaises(LayoutError),
            ):
                self.apply([operation])
        self.assertEqual(len(self.plan['objects']), 2)

    def test_recognition_classification_has_complete_distinct_categories(self):
        groups = atlas_items(self.data.catalog)
        all_ids = [item['id'] for items in groups.values() for item in items]
        self.assertEqual(len(all_ids), len(set(all_ids)))
        expected_buildings = {
            self.data.item_id(key)
            for key, item in self.data.items.items()
            if item['category'] == 'building'
            and not item['hidden_in_menu']
            and key != 'shipping-bin'
        }
        self.assertEqual(
            {item['id'] for item in groups['buildings']}, expected_buildings
        )
        for items in groups.values():
            for item in items:
                self.assertNotIn(
                    recognition_group(item),
                    ['ignored', 'planner'],
                )
                self.assertFalse(item['hidden_in_menu'])
        self.assertEqual(
            recognition_group(self.data.items['dead-tree']), 'trees'
        )
        self.assertEqual(
            recognition_group(self.data.items['oak-chair']), 'furniture'
        )
        self.assertEqual(
            recognition_group(self.data.items['bee-house']), 'tools'
        )
        self.assertEqual(
            recognition_group(self.data.items['shipping-bin']), 'tools'
        )
        self.assertNotIn('greenhouse', all_ids)

    def test_complete_furniture_flower_and_scarecrow_recognition(self):
        groups = atlas_items(self.data.catalog)
        represented = {
            item['id'] for values in groups.values() for item in values
        }
        expected_furniture = {
            self.data.item_id(key)
            for key, value in self.data.items.items()
            if value['category'] == 'furniture' and not value['hidden_in_menu']
        }
        self.assertLessEqual(expected_furniture, represented)
        self.assertEqual(
            recognition_group(self.data.items['pig-painting']), 'artworks'
        )
        self.assertEqual(
            recognition_group(self.data.items['needlepoint-flower']),
            'artworks',
        )
        self.assertEqual(
            recognition_group(self.data.items['cat-tree']), 'furniture'
        )
        self.assertEqual(
            recognition_group(self.data.items['chicken-statue']), 'furniture'
        )
        self.assertEqual(
            recognition_group(self.data.items['blue-sleeping-junimo']),
            'furniture',
        )
        self.assertEqual(
            recognition_group(self.data.items['junimo-flower']),
            'ornamental_plants',
        )
        self.assertLessEqual(
            {f'rarecrow-{n}' for n in range(1, 9)}, represented
        )
        supplemental = load_supplemental(self.data.root)['items']
        for n in range(1, 7):
            item = supplemental[f'seasonal-plant-{n}']
            self.assertEqual(
                set(item['sprites']), {'spring', 'summer', 'fall', 'winter'}
            )
        self.assertEqual(self.data.item_id('rarecrow-3'), 'rarecrow-3')

    def test_recognition_builder_indexes_readable_cards_and_variants(self):
        with (
            tempfile.TemporaryDirectory() as temporary,
            contextlib.chdir(temporary),
        ):
            root = Path(temporary)
            index = build_recognition(self.data, root)
            self.assertEqual(
                index['item_count'],
                sum(
                    len(items)
                    for items in atlas_items(self.data.catalog).values()
                ),
            )
            self.assertEqual(
                index['lookup']['greenhouse'], 'greenhouse-repaired'
            )
            self.assertEqual(index['lookup']['wood-fence-hc'], 'wood-fence')
            self.assertEqual(index['lookup']['oak-tree-winter'], 'oak-tree')
            for group, metadata in index['groups'].items():
                capacity = metadata['sheet_layout']['capacity']
                self.assertEqual(
                    len(metadata['pages']),
                    (metadata['item_count'] + capacity - 1) // capacity,
                )
                for page_index, page in enumerate(metadata['pages']):
                    if page_index < len(metadata['pages']) - 1:
                        self.assertEqual(page['item_count'], capacity)
                    with Image.open(root / page['path']) as image:
                        self.assertEqual(list(image.size), page['size_px'])
                        cards = [
                            entry
                            for entry in index['items'].values()
                            if entry['sheet'] == page['path']
                        ]
                        self.assertEqual(len(cards), page['item_count'])
                        for card in cards:
                            left, top, right, bottom = card['card_bounds_px']
                            self.assertTrue(
                                0 <= left < right <= image.width
                                and 0 <= top < bottom <= image.height
                            )
                            self.assertEqual(card['group'], group)
                            for text in card['text_regions']:
                                x1, y1, x2, y2 = text['bounds_px']
                                self.assertTrue(
                                    left <= x1 < x2 <= right
                                    and top <= y1 < y2 <= bottom
                                )
                                self.assertGreaterEqual(text['font_size'], 10)
                            self.assertIn(
                                card['id'],
                                [
                                    ''.join(text['lines'])
                                    for text in card['text_regions']
                                ],
                            )
                            for appearance in card['appearances']:
                                x1, y1, x2, y2 = appearance['sprite_bounds_px']
                                self.assertTrue(
                                    left <= x1 < x2 <= right
                                    and top <= y1 < y2 <= bottom
                                )
                            self.assertGreater(
                                len(
                                    image.crop(
                                        (left, top, right, bottom)
                                    ).getcolors(1000000)
                                ),
                                5,
                            )
            self.assertTrue((root / 'data/recognition/index.json').is_file())

    def test_checkpoints_preserve_actual_plan_and_require_visual_review(self):
        with (
            tempfile.TemporaryDirectory() as temporary,
            contextlib.chdir(temporary),
        ):
            root = Path(temporary)
            first = checkpoint(
                self.data, self.plan, 'buildings', root / 'buildings', scale=1
            )
            changed = self.apply(
                [{'op': 'add', 'item': {'type': 'chest', 'x': 20, 'y': 24}}]
            )
            second = checkpoint(
                self.data,
                changed,
                'tools',
                root / 'tools',
                reference=root / 'buildings/preview.png',
                scale=1,
            )
            self.assertEqual(first['visual_review']['status'], 'pending')
            self.assertEqual(second['visual_review']['status'], 'pending')
            self.assertEqual(
                second['category_counts'], {'buildings': 2, 'tools': 1}
            )
            self.assertEqual(
                json.loads((root / 'buildings/plan.json').read_text()),
                self.plan,
            )
            self.assertEqual(
                json.loads((root / 'tools/plan.json').read_text()), changed
            )
            self.assertEqual(
                second['plan_sha256'],
                hashlib.sha256(
                    (root / 'tools/plan.json').read_bytes()
                ).hexdigest(),
            )
            self.assertNotEqual(first['plan_sha256'], second['plan_sha256'])
            self.assertEqual(
                second['reference']['sha256'],
                hashlib.sha256(
                    (root / 'buildings/preview.png').read_bytes()
                ).hexdigest(),
            )
            with self.assertRaises(LayoutError):
                checkpoint(
                    self.data, changed, 'tools', root / 'buildings', scale=1
                )
            self.assertEqual(
                json.loads((root / 'buildings/plan.json').read_text()),
                self.plan,
            )
            invalid = copy.deepcopy(changed)
            invalid['objects'][-1]['x'] = -1
            with self.assertRaises(LayoutError):
                checkpoint(
                    self.data, invalid, 'tools', root / 'invalid', scale=1
                )
            self.assertFalse((root / 'invalid').exists())

    def test_crop_varieties_normalize_to_placeholder(self):
        plan = self.apply(
            [
                {
                    'op': 'add',
                    'item': {
                        'id': 'crop-position',
                        'type': 'crop',
                        'x': 20,
                        'y': 24,
                    },
                }
            ]
        )
        crop = plan['objects'][-1]
        self.assertEqual(
            (crop['type'], crop['crop_kind']),
            ('strawberry-seeds', 'placeholder'),
        )
        moved = self.apply(
            [{'op': 'update', 'id': 'crop-position', 'set': {'x': 21}}], plan
        )
        self.assertEqual(moved['objects'][-1]['crop_kind'], 'placeholder')
        selected = self.apply(
            [
                {
                    'op': 'update',
                    'id': 'crop-position',
                    'set': {'type': 'parsnip-seeds'},
                }
            ],
            moved,
        )
        self.assertEqual(
            (
                selected['objects'][-1]['type'],
                selected['objects'][-1]['crop_kind'],
            ),
            ('strawberry-seeds', 'placeholder'),
        )

    def test_generic_crop_fill_and_recipe(self):
        plan = self.apply(
            [{'op': 'fill', 'type': 'crop', 'rect': [20, 24, 2, 2]}]
        )
        self.assertEqual(
            sum(
                item.get('crop_kind') == 'placeholder'
                for item in plan['objects']
            ),
            4,
        )
        recipe = {
            'schema_version': 1,
            'modules': [
                {'id': 'field', 'objects': [{'type': 'crop', 'x': 0, 'y': 0}]}
            ],
        }
        generated = generate_plan(self.data, 'regular', recipe)
        self.assertEqual(generated['objects'][-1]['crop_kind'], 'placeholder')
        old = copy.deepcopy(plan)
        old['objects'][-1] = {
            'id': 'chosen',
            'type': 'parsnip-seeds',
            'x': 22,
            'y': 24,
        }
        normalized = normalize_plan(self.data, old)
        self.assertEqual(normalized['objects'][-1]['type'], 'strawberry-seeds')

    def test_fixed_references_are_immutable_but_house_can_move(self):
        self.assertEqual(
            (
                self.plan['fixed_landmarks'][0]['x'],
                self.plan['fixed_landmarks'][0]['y'],
            ),
            (34, 5),
        )
        changed = copy.deepcopy(self.plan)
        changed['fixed_landmarks'][0]['x'] += 1
        self.assertFalse(validate_plan(self.data, changed)['passed'])
        with self.assertRaises(LayoutError):
            self.apply(
                [{'op': 'add', 'item': {'type': 'chest', 'x': 34, 'y': 5}}]
            )
        moved = self.apply(
            [
                {
                    'op': 'update',
                    'id': 'default-house',
                    'set': {'x': 50, 'y': 24},
                }
            ]
        )
        self.assertTrue(validate_plan(self.data, moved)['passed'])
        self.assertEqual(
            moved['fixed_landmarks'], self.plan['fixed_landmarks']
        )

    def test_modded_inputs_are_rejected(self):
        for marker in [
            {'mods': ['SVE']},
            {'game_mode': 'modded'},
            {'modded': True},
            {'customLayout': 'custom.png'},
        ]:
            with self.subTest(marker=marker), self.assertRaises(LayoutError):
                normalize_plan(self.data, {**self.plan, **marker})
        with self.assertRaises(LayoutError):
            create_plan(self.data, 'grandpas-farm-v2')
        with self.assertRaises(LayoutError):
            generate_plan(
                self.data,
                'regular',
                {'schema_version': 1, 'mods': ['SVE'], 'modules': []},
            )
        with self.assertRaises(LayoutError):
            import_planner(
                self.data,
                {
                    'options': {
                        'layout': 'regular',
                        'customLayout': 'custom.png',
                    }
                },
            )

    def test_placeholder_is_not_implicitly_a_flower(self):
        plan = self.apply(
            [
                {'op': 'add', 'item': {'type': 'bee-house', 'x': 20, 'y': 24}},
                {
                    'op': 'add',
                    'item': {
                        'id': 'unknown-crop',
                        'type': 'crop',
                        'x': 21,
                        'y': 24,
                        'tags': ['flowers'],
                    },
                },
            ]
        )
        report = coverage(self.data, plan, 'bee', target_tag='flowers')
        self.assertFalse(report['fully_covered'])
        self.assertEqual(report['target_object_count'], 0)
        self.assertEqual(
            report['placeholder_targets_not_treated_as_flowers'],
            ['unknown-crop'],
        )

    def test_delivery_contains_plan_two_images_and_working_data(self):
        recipe = json.loads(
            (self.data.root / 'examples/delivery-demo.recipe.json').read_text()
        )
        plan = generate_plan(self.data, 'regular', recipe)
        with (
            tempfile.TemporaryDirectory() as temporary,
            contextlib.chdir(temporary),
        ):
            directory = Path(temporary) / 'delivery'
            result = deliver(self.data, plan, directory, scale=1)
            local, manifest = (
                Path(result['local_directory']),
                result['manifest'],
            )
            self.assertEqual(local, (directory / 'working').resolve())
            self.assertEqual(
                {p.name for p in directory.iterdir()},
                {'plan.json', 'preview.png', 'implementation.png', 'working'},
            )
            self.assertFalse(list(directory.rglob('*.zip')))
            self.assertEqual(manifest['placeholder_crop_count'], 40)
            self.assertEqual(manifest['edge_reference_count'], 16)
            self.assertEqual(
                set(manifest['coverage']),
                {'sprinkler', 'scarecrow', 'bee', 'junimo'},
            )
            for group in manifest['coverage']:
                report = json.loads(
                    (local / f'coverage-{group}.json').read_text()
                )
                self.assertEqual(
                    report['plan_sha256'], manifest['plan_sha256']
                )
                self.assertNotIn('diagram', report)
            for entry in manifest['files']:
                self.assertEqual(
                    hashlib.sha256(
                        (directory / entry['path']).read_bytes()
                    ).hexdigest(),
                    entry['sha256'],
                )
            legend = json.loads((local / 'diagram-legend.json').read_text())[
                'legend'
            ]
            self.assertEqual(
                sum(row['count'] for row in legend), len(plan['objects'])
            )
            self.assertEqual(sum(row['existing_count'] for row in legend), 2)
            self.assertFalse(list(directory.rglob('materials*')))
            self.assertFalse((local / 'icons').exists())
            self.assertTrue((directory / 'plan.json').exists())

    def test_delivery_replace_keeps_backup_and_drops_stale_files(self):
        plan = self.apply(
            [
                {
                    'op': 'add',
                    'item': {
                        'id': 'bee',
                        'type': 'bee-house',
                        'x': 20,
                        'y': 24,
                    },
                }
            ]
        )
        with (
            tempfile.TemporaryDirectory() as temporary,
            contextlib.chdir(temporary),
        ):
            directory = Path(temporary) / 'delivery'
            first = deliver(self.data, plan, directory, scale=1)
            self.assertEqual(
                first['manifest']['coverage']['bee']['status'],
                'optional_no_flowers',
            )
            self.assertFalse(
                first['manifest']['review']['requires_user_decision']
            )
            old = (directory / 'working/manifest.json').read_bytes()
            with self.assertRaises(LayoutError):
                deliver(self.data, plan, directory, scale=1)
            second = deliver(
                self.data,
                create_plan(self.data, 'beach'),
                directory,
                scale=1,
                force=True,
            )
            self.assertFalse((directory / 'layout.zip').exists())
            backup = Path(second['backup'])
            self.assertEqual(
                (backup / 'working/manifest.json').read_bytes(), old
            )
            self.assertTrue((backup / 'plan.json').exists())
            unrelated = Path(temporary) / 'unrelated'
            unrelated.mkdir()
            (unrelated / 'notes.txt').write_text('keep')
            with self.assertRaises(LayoutError):
                deliver(self.data, plan, unrelated, force=True)
            self.assertEqual((unrelated / 'notes.txt').read_text(), 'keep')

    def test_delivery_supports_local_beach_layout(self):
        with (
            tempfile.TemporaryDirectory() as temporary,
            contextlib.chdir(temporary),
        ):
            result = deliver(
                self.data, create_plan(self.data, 'beach'), scale=1
            )
            directory = Path(result['directory'])
            self.assertEqual(
                directory, Path(temporary).resolve() / 'stardew-layout'
            )
            self.assertNotIn('website_import', result['manifest'])
            self.assertFalse(list(directory.glob('*.zip')))
            self.assertTrue((directory / 'plan.json').exists())
            self.assertEqual(len(list(directory.glob('*.png'))), 2)

    def test_four_side_references_touch_usable_ground(self):
        for name, info in self.data.maps.items():
            if info.get('category') != 'farm':
                continue
            blocked = self.data.blocked(name)
            references = edge_references(info, blocked)
            self.assertEqual(
                Counter(reference['side'] for reference in references),
                {'N': 4, 'E': 4, 'S': 4, 'W': 4},
            )
            self.assertEqual(
                len(
                    {
                        (reference['x'], reference['y'])
                        for reference in references
                    }
                ),
                16,
            )
            self.assertEqual(references, edge_references(info, blocked))
            for reference in references:
                point = (reference['x'], reference['y'])
                usable = tuple(reference['adjacent_usable_tile'])
                self.assertIn(
                    point, blocked['accessible'] | blocked['buildable']
                )
                self.assertNotIn(
                    usable, blocked['accessible'] | blocked['buildable']
                )
                self.assertEqual(
                    abs(point[0] - usable[0]) + abs(point[1] - usable[1]), 1
                )

    def test_crop_blocks_are_pale_and_unnumbered(self):
        plan = self.apply(
            [
                {
                    'op': 'add',
                    'item': {'id': 'crop', 'type': 'crop', 'x': 20, 'y': 24},
                },
                {
                    'op': 'add',
                    'item': {'id': 'box', 'type': 'chest', 'x': 22, 'y': 24},
                },
            ]
        )
        styles = item_styles(plan, self.data)
        self.assertIsNone(styles['strawberry-seeds']['number'])
        self.assertGreater(
            min(
                int(styles['strawberry-seeds']['color'][i : i + 2], 16)
                for i in [1, 3, 5]
            ),
            220,
        )
        with (
            tempfile.TemporaryDirectory() as temporary,
            contextlib.chdir(temporary),
        ):
            result = render_plan(
                self.data,
                plan,
                Path(temporary) / 'diagram.png',
                mode='blueprint',
                scale=1,
            )
            self.assertNotIn('crop', result['numbered_object_ids'])
            self.assertIn('box', result['numbered_object_ids'])
            crop_legend = next(
                entry
                for entry in result['legend']
                if entry['type'] == 'strawberry-seeds'
            )
            self.assertIsNone(crop_legend['number'])

    def test_source_texture_is_preserved_and_markers_are_flat(self):
        plan = self.apply(
            [
                {
                    'op': 'add',
                    'item': {'id': 'coop', 'type': 'coop', 'x': 35, 'y': 25},
                }
            ]
        )
        with (
            tempfile.TemporaryDirectory() as temporary,
            contextlib.chdir(temporary),
        ):
            path = Path(temporary) / 'map.png'
            result = render_plan(
                self.data, plan, path, mode='blueprint', scale=1
            )
            map_x, map_y, _, _ = result['map_region_px']
            rendered = Image.open(path).convert('RGB')
            source = Image.open(
                self.data.root / 'data/maps/regular/spring.png'
            ).convert('RGB')
            self.assertEqual(
                rendered.getpixel((map_x + 45 * 16 + 8, map_y + 15 * 16 + 8)),
                source.getpixel((45 * 16 + 8, 15 * 16 + 8)),
            )
            color = item_styles(plan, self.data)['coop']['color']
            rgb = tuple(int(color[i : i + 2], 16) for i in [1, 3, 5])
            for x, y in [
                (35 * 16 + 2, 25 * 16 + 2),
                (36 * 16, 26 * 16),
                (40 * 16 + 13, 27 * 16 + 13),
            ]:
                self.assertEqual(
                    rendered.getpixel((map_x + x, map_y + y)), rgb
                )

    def test_legend_has_seasonal_icons_readable_names_and_counts(self):
        plan = self.apply(
            [
                {
                    'op': 'add',
                    'item': {
                        'id': 'oak',
                        'type': 'oak-tree',
                        'x': 30,
                        'y': 30,
                    },
                },
                {'op': 'add', 'item': {'type': 'crop', 'x': 35, 'y': 30}},
                {
                    'op': 'add',
                    'item': {'type': 'strawberry-seeds', 'x': 36, 'y': 30},
                },
            ]
        )
        plan['season'] = 'winter'
        with (
            tempfile.TemporaryDirectory() as temporary,
            contextlib.chdir(temporary),
        ):
            path = Path(temporary) / 'diagram.png'
            result = render_plan(
                self.data, plan, path, mode='blueprint', scale=1
            )
            oak = next(
                row for row in result['legend'] if row['type'] == 'oak-tree'
            )
            self.assertEqual(oak['icon_id'], 'oak-tree-winter')
            crop_rows = [
                row
                for row in result['legend']
                if row['type'] == 'strawberry-seeds'
            ]
            self.assertEqual(
                {row['crop_kind'] for row in crop_rows},
                {'placeholder'},
            )
            self.assertEqual(
                sum(row['count'] for row in result['legend']),
                len(plan['objects']),
            )
            with Image.open(path) as image:
                for entry in result['legend']:
                    left, top, right, bottom = entry['icon_bounds_px']
                    x1, y1, x2, y2 = entry['card_bounds_px']
                    self.assertTrue(
                        x1 < left < right < x2 and y1 < top < bottom < y2
                    )
                    self.assertTrue(
                        0 <= x1 < x2 <= image.width
                        and 0 <= y1 < y2 <= image.height
                    )
                    self.assertGreater(
                        len(
                            image.crop((left, top, right, bottom)).getcolors(
                                1000000
                            )
                        ),
                        5,
                    )

    def test_legend_is_compact_and_groups_related_types_and_series(self):
        # Deliberately interleave categories and furniture sets on the map.
        kinds = [
            'quality-sprinkler',
            'scarecrow',
            'sprinkler',
            'bee-house',
            'iridium-sprinkler',
            'deluxe-scarecrow',
            'anvil',
            'well',
            'joja-chair',
            'retro-chair',
            'joja-table',
            'retro-table',
            'wood-fence',
            'stone-floor',
            'wooden-floor',
            'wood-gate',
        ]
        plan = self.apply(
            [
                {
                    'op': 'add',
                    'item': {
                        'id': kind,
                        'type': kind,
                        'x': 20 + (index % 8) * 4,
                        'y': 24 + (index // 8) * 8,
                    },
                }
                for index, kind in enumerate(kinds)
            ]
        )
        with tempfile.TemporaryDirectory() as temporary:
            result = render_plan(
                self.data,
                plan,
                Path(temporary) / 'diagram.png',
                mode='blueprint',
                scale=1,
            )
            numbered = [
                row for row in result['legend'] if row['number'] is not None
            ]
            numbers = {row['type']: row['number'] for row in numbered}
            for related in [
                ['sprinkler', 'quality-sprinkler', 'iridium-sprinkler'],
                ['scarecrow', 'deluxe-scarecrow'],
                ['wood-fence', 'wood-gate'],
                ['joja-chair', 'joja-table'],
                ['retro-chair', 'retro-table'],
            ]:
                self.assertEqual(
                    [numbers[kind] for kind in related],
                    list(
                        range(
                            numbers[related[0]],
                            numbers[related[0]] + len(related),
                        )
                    ),
                )
            self.assertEqual(
                [row['number'] for row in numbered],
                list(range(1, len(numbered) + 1)),
            )
            self.assertEqual(
                numbered,
                sorted(
                    numbered,
                    key=lambda row: (
                        row['card_bounds_px'][0],
                        row['card_bounds_px'][1],
                    ),
                ),
            )
            self.assertEqual(
                sum(row['count'] for row in result['legend']),
                len(plan['objects']),
            )
            self.assertLessEqual(
                max(
                    row['card_bounds_px'][3] - row['card_bounds_px'][1]
                    for row in numbered
                ),
                60,
            )
            shuffled = dict(plan, objects=list(reversed(plan['objects'])))
            self.assertEqual(
                item_styles(plan, self.data), item_styles(shuffled, self.data)
            )

    def test_coordinate_rulers_have_minor_ticks_on_all_four_sides(self):
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / 'diagram.png'
            result = render_plan(
                self.data,
                self.plan,
                path,
                mode='blueprint',
                crop=[12, 13, 20, 18],
                scale=2,
            )
            self.assertEqual(
                [tick['value'] for tick in result['coordinate_ticks']['x']],
                list(range(12, 32)),
            )
            self.assertEqual(
                [tick['value'] for tick in result['coordinate_ticks']['y']],
                list(range(13, 31)),
            )
            for ticks in result['coordinate_ticks'].values():
                for tick in ticks:
                    self.assertEqual(tick['major'], tick['value'] % 5 == 0)
                    self.assertEqual(len(tick['segments_px']), 2)
            with Image.open(path) as image:
                for axis in ['x', 'y']:
                    tick = next(
                        value
                        for value in result['coordinate_ticks'][axis]
                        if not value['major']
                    )
                    for line in tick['segments_px']:
                        x, y = (
                            round((line[0] + line[2]) / 2),
                            round((line[1] + line[3]) / 2),
                        )
                        self.assertEqual(image.getpixel((x, y)), (97, 114, 97))

    def test_patch_regions_cover_all_tiles_with_shared_context(self):
        regions = list(review_regions(23, 17, [9, 9], 2))
        seen = Counter()
        for entry in regions:
            x, y, w, h = entry['tile_rect']
            left, top, width, height = entry['context_rect']
            self.assertTrue(left <= x < x + w <= left + width <= 23)
            self.assertTrue(top <= y < y + h <= top + height <= 17)
            seen.update(
                (col, row)
                for col in range(x, x + w)
                for row in range(y, y + h)
            )
        self.assertEqual(len(seen), 23 * 17)
        self.assertEqual(set(seen.values()), {1})
        self.assertEqual(regions[0]['context_rect'], [0, 0, 11, 11])
        self.assertEqual(regions[1]['context_rect'], [7, 0, 13, 11])

    def test_final_patches_align_images_measure_floors_and_require_each_review(
        self,
    ):
        plan = self.apply(
            [{'op': 'fill', 'type': 'stone-floor', 'rect': [20, 24, 11, 2]}]
        )
        with (
            tempfile.TemporaryDirectory() as temporary,
            contextlib.chdir(temporary),
        ):
            root = Path(temporary)
            preview = render_plan(
                self.data, plan, root / 'preview.png', scale=1
            )
            reference = root / 'reference.png'
            with Image.open(root / 'preview.png') as source:
                x, y, w, h = preview['map_region_px']
                reference_map = source.crop((x, y, x + w, y + h))
                full = Image.new('RGB', (w + 100, h + 80))
                full.paste(reference_map, (50, 40))
                full.save(reference)
            points = [
                {'tile': [x, y], 'pixel': [50 + x * 16, 40 + y * 16]}
                for x, y in [(0, 0), (80, 0), (0, 65), (80, 65)]
            ]
            calibration = calibrate_reference(self.data, reference, points)
            path = root / 'calibration.json'
            path.write_text(json.dumps(calibration))
            stage = root / 'final'
            checkpoint(
                self.data,
                plan,
                'final',
                stage,
                reference=reference,
                calibration=path,
                block_size=[30, 20],
                scale=1,
            )
            index = json.loads((stage / 'patches/index.json').read_text())
            self.assertEqual(len(index['patches']), 12)
            self.assertEqual(
                validate_patch_review(stage)['status'], 'incomplete'
            )
            with self.assertRaisesRegex(LayoutError, 'pending'):
                deliver(
                    self.data,
                    plan,
                    root / 'pending',
                    scale=1,
                    review_directory=stage,
                )
            horizontal = []
            for region in index['patches']:
                with Image.open(
                    stage / 'patches' / region['files']['comparison']['path']
                ) as image:
                    ref = image.crop(region['panels']['reference'])
                    rendered = image.crop(region['panels']['preview'])
                    self.assertEqual(ref.size, rendered.size)
                    self.assertIsNone(
                        ImageChops.difference(ref, rendered).getbbox()
                    )
                horizontal.extend(region['flooring_runs']['horizontal'])
            self.assertEqual(sum(row['length'] for row in horizontal), 22)
            self.assertIn(10, [row['length'] for row in horizontal])
            self.assertIn(1, [row['length'] for row in horizontal])
            review_path = stage / 'patches/review.json'
            review = json.loads(review_path.read_text())
            for row in review['patches']:
                row['checks'] = dict.fromkeys(REVIEW_CHECKS, 'passed')
            review_path.write_text(json.dumps(review))
            self.assertEqual(
                validate_patch_review(stage)['status'], 'incomplete'
            )
            overall_path = stage / 'review.json'
            overall = json.loads(overall_path.read_text())
            overall['visual_review'] = {
                'status': 'compared',
                'result': 'passed',
                'findings': [],
            }
            overall_path.write_text(json.dumps(overall))
            self.assertEqual(validate_patch_review(stage)['status'], 'passed')
            command = [
                sys.executable,
                str(self.data.root / 'scripts/layout.py'),
                'deliver',
                str(stage / 'plan.json'),
                '--review-dir',
                str(stage),
                '--out-dir',
                str(root / 'cli-result'),
                '--scale',
                '1',
            ]
            completed = subprocess.run(command, capture_output=True, text=True)
            self.assertEqual(completed.returncode, 0, completed.stderr)
            self.assertTrue((root / 'cli-result/implementation.png').is_file())
            review['patches'][0]['checks']['positions'] = 'needs_change'
            review['patches'][0]['findings'] = ['位置待修改']
            review_path.write_text(json.dumps(review))
            with self.assertRaisesRegex(LayoutError, 'known errors'):
                deliver(
                    self.data,
                    plan,
                    root / 'errors',
                    scale=1,
                    review_directory=stage,
                )
            review['patches'][0]['checks']['positions'] = 'passed'
            review['patches'][0]['findings'] = []
            review_path.write_text(json.dumps(review))
            delivered = deliver(
                self.data,
                plan,
                root / 'result',
                scale=1,
                review_directory=stage,
            )
            self.assertEqual(
                delivered['manifest']['review']['patch_review']['status'],
                'passed',
            )
            altered = apply_operations(
                self.data,
                plan,
                [{'op': 'add', 'item': {'type': 'chest', 'x': 40, 'y': 24}}],
            )
            with self.assertRaises(LayoutError):
                deliver(
                    self.data,
                    altered,
                    root / 'stale',
                    scale=1,
                    review_directory=stage,
                )
            review['patches'][0]['checks']['positions'] = 'not_applicable'
            review_path.write_text(json.dumps(review))
            with self.assertRaises(LayoutError):
                validate_patch_review(stage)
            review['patches'].pop()
            review_path.write_text(json.dumps(review))
            with self.assertRaises(LayoutError):
                validate_patch_review(stage)

    def test_delivery_failure_preserves_the_entire_folder(self):
        with (
            tempfile.TemporaryDirectory() as temporary,
            contextlib.chdir(temporary),
        ):
            directory = Path(temporary) / 'share'
            deliver(self.data, self.plan, directory, scale=1)
            before = (directory / 'working/manifest.json').read_bytes()
            preview = (directory / 'preview.png').read_bytes()
            with (
                patch(
                    'layout_delivery.render_plan',
                    side_effect=OSError('simulated rendering failure'),
                ),
                self.assertRaises(OSError),
            ):
                deliver(self.data, self.plan, directory, scale=1, force=True)
            self.assertEqual(
                (directory / 'working/manifest.json').read_bytes(), before
            )
            self.assertEqual((directory / 'preview.png').read_bytes(), preview)
            self.assertEqual(
                {p.name for p in Path(temporary).iterdir()}, {'share'}
            )

    def test_delivery_rejects_paths_outside_current_workspace(self):
        with (
            tempfile.TemporaryDirectory() as temporary,
            tempfile.TemporaryDirectory() as outside,
            contextlib.chdir(temporary),
        ):
            for destination in [Path(temporary), Path(outside) / 'result']:
                with self.assertRaises(LayoutError):
                    deliver(self.data, self.plan, destination, scale=1)
            link = Path(temporary) / 'linked'
            link.symlink_to(outside, target_is_directory=True)
            with self.assertRaises(LayoutError):
                deliver(self.data, self.plan, link / 'result', scale=1)
            self.assertFalse((Path(outside) / 'result').exists())

    def test_season_changes_resolve_existing_variant_and_upstream_bad_metadata(
        self,
    ):
        self.assertEqual(
            seasonal_id(self.data, 'oak-tree-fall', 'winter'),
            'oak-tree-winter',
        )
        self.assertEqual(
            seasonal_id(self.data, 'oak-tree-fall', 'spring'), 'oak-tree'
        )
        self.assertEqual(
            seasonal_id(self.data, 'apple-tree-summer', 'winter'),
            'apple-tree-winter',
        )
        self.assertEqual(
            seasonal_id(self.data, 'pomegranate-tree', 'summer'),
            'pomegranate-tree-summer',
        )
        self.assertEqual(
            [row['id'] for row in appearances(self.data, 'apple-tree-summer')],
            [
                'apple-tree',
                'apple-tree-summer',
                'apple-tree-fall',
                'apple-tree-winter',
            ],
        )
        plan = self.apply(
            [
                {
                    'op': 'add',
                    'item': {
                        'id': 'oak',
                        'type': 'oak-tree-fall',
                        'x': 30,
                        'y': 30,
                    },
                }
            ]
        )
        plan['season'] = 'winter'
        with (
            tempfile.TemporaryDirectory() as temporary,
            contextlib.chdir(temporary),
        ):
            path = Path(temporary) / 'winter.png'
            rendered = render_plan(self.data, plan, path, scale=1)
            replacement = copy.deepcopy(plan)
            replacement['objects'][-1]['type'] = 'oak-tree'
            second = Path(temporary) / 'base.png'
            render_plan(self.data, replacement, second, scale=1)
            with Image.open(path) as first, Image.open(second) as other:
                self.assertIsNone(
                    ImageChops.difference(first, other).getbbox()
                )
            self.assertIn('map_region_px', rendered)

    def test_seasonal_and_connected_imports_use_one_canonical_object(self):
        payload = {
            'tiles': [
                {'type': 'apple-tree-summer', 'x': 30 * 16, 'y': 30 * 16}
            ],
            'flooring': [
                {'type': 'stone-floor-sw', 'x': 20 * 16, 'y': 24 * 16}
            ],
        }
        imported = import_planner(self.data, payload)
        self.assertEqual(
            {item['type'] for item in imported['objects']}
            - {item['type'] for item in self.plan['objects']},
            {'apple-tree', 'stone-floor'},
        )
        self.assertEqual(
            self.data.items['apple-tree-summer']['name_zh'], '苹果树（夏季）'
        )
        self.assertNotIn(
            'apple-tree-summer',
            self.data.items['pomegranate-tree']['variant_ids'],
        )
        self.assertTrue(
            any(
                row['id'] == 'stone-floor-sw'
                for row in appearances(self.data, 'stone-floor-sw')
            )
        )

    def test_unverified_seasonal_asset_is_reported_until_confirmed(self):
        plan = self.apply(
            [
                {
                    'op': 'add',
                    'item': {
                        'id': 'banana',
                        'type': 'banana-tree',
                        'x': 30,
                        'y': 30,
                    },
                }
            ]
        )
        plan['season'] = 'winter'
        with (
            tempfile.TemporaryDirectory() as temporary,
            contextlib.chdir(temporary),
        ):
            first = deliver(self.data, plan, 'draft', scale=1)
            warnings = first['manifest']['review']['appearance_warnings']
            self.assertEqual(len(warnings), 1)
            self.assertTrue(warnings[0]['requires_user_decision'])
            self.assertTrue(
                first['manifest']['review']['requires_user_decision']
            )
            self.assertEqual(warnings[0]['sprite_id'], 'banana-tree-winter')
            plan['review_issues'] = [
                {
                    'id': 'winter-banana',
                    'kind': 'appearance',
                    'status': 'accepted',
                    'description': '冬季素材外观待确认',
                    'resolution': '用户确认保留此素材',
                    'sprite_id': warnings[0]['sprite_id'],
                    'image_sha256': warnings[0]['image_sha256'],
                    'object_ids': ['banana'],
                }
            ]
            second = deliver(self.data, plan, 'confirmed', scale=1)
            self.assertFalse(
                second['manifest']['review']['requires_user_decision']
            )
            self.assertFalse(
                second['manifest']['review']['appearance_warnings'][0][
                    'requires_user_decision'
                ]
            )

    def test_cli_rejects_external_output_and_keeps_bee_exception(self):
        command = [sys.executable, str(self.data.root / 'scripts/layout.py')]
        with (
            tempfile.TemporaryDirectory() as temporary,
            tempfile.TemporaryDirectory() as outside,
            contextlib.chdir(temporary),
        ):
            path = Path(temporary) / 'plan.json'
            path.write_text(json.dumps(self.plan))
            rejected = subprocess.run(
                [
                    *command,
                    'deliver',
                    str(path),
                    '--out-dir',
                    str(Path(outside) / 'result'),
                    '--review-dir',
                    str(Path(temporary) / 'final'),
                ],
                capture_output=True,
                text=True,
            )
            self.assertEqual(rejected.returncode, 2)
            self.assertIn('workspace', rejected.stderr)
            self.assertFalse((Path(outside) / 'result').exists())
            bee = subprocess.run(
                [
                    *command,
                    'coverage',
                    str(path),
                    '--group',
                    'bee',
                    '--require-full',
                ],
                capture_output=True,
                text=True,
            )
            self.assertEqual(bee.returncode, 0, bee.stderr)
            sprinkler = subprocess.run(
                [*command, 'coverage', str(path), '--require-full'],
                capture_output=True,
                text=True,
            )
            self.assertEqual(sprinkler.returncode, 1)
            rendered = subprocess.run(
                [
                    *command,
                    'render',
                    str(path),
                    '--mode',
                    'blueprint',
                    '--out',
                    str(Path(temporary) / 'implementation.png'),
                    '--scale',
                    '1',
                ],
                capture_output=True,
                text=True,
            )
            self.assertEqual(rendered.returncode, 0, rendered.stderr)

    def test_coverage_reports_missing_sources_and_retains_accepted_decisions(
        self,
    ):
        plan = self.apply(
            [
                {
                    'op': 'add',
                    'item': {'id': 'plant', 'type': 'crop', 'x': 30, 'y': 30},
                }
            ]
        )
        report = audit_coverage(self.data, plan)
        self.assertEqual(report['groups']['sprinkler']['status'], 'no_sources')
        self.assertEqual(report['groups']['junimo']['status'], 'not_requested')
        self.assertEqual(
            {issue['group'] for issue in report['issues']},
            {'sprinkler', 'scarecrow'},
        )
        plan['review_issues'] = [
            {
                'id': issue['group'],
                'kind': 'coverage',
                'status': 'accepted',
                'description': '保留当前区域',
                'resolution': '用户确认保留当前覆盖情况',
                'group': issue['group'],
                'coverage_fingerprint': issue['coverage_fingerprint'],
            }
            for issue in report['issues']
        ]
        self.assertFalse(
            audit_coverage(self.data, plan)['requires_user_decision']
        )
        plan['objects'][-1]['x'] += 1
        self.assertTrue(
            audit_coverage(self.data, plan)['requires_user_decision']
        )

    def test_coverage_checks_entire_multi_tile_target_and_rechecks_after_move(
        self,
    ):
        plan = self.apply(
            [
                {
                    'op': 'add',
                    'item': {
                        'id': 'pond',
                        'type': 'fish-pond',
                        'x': 30,
                        'y': 25,
                    },
                },
                {
                    'op': 'add',
                    'item': {
                        'id': 'water',
                        'type': 'iridium-sprinkler',
                        'x': 29,
                        'y': 27,
                    },
                },
            ]
        )
        result = coverage(self.data, plan, target_types=['fish-pond'])
        self.assertEqual(result['target_cell_count'], 25)
        self.assertEqual(result['covered_target_cell_count'], 10)
        self.assertFalse(result['fully_covered'])
        crops = self.apply(
            [
                {
                    'op': 'add',
                    'item': {'id': 'plant', 'type': 'crop', 'x': 30, 'y': 30},
                },
                {
                    'op': 'add',
                    'item': {
                        'id': 'water',
                        'type': 'sprinkler',
                        'x': 31,
                        'y': 30,
                    },
                },
            ]
        )
        self.assertTrue(coverage(self.data, crops)['fully_covered'])
        changed = apply_operations(
            self.data,
            crops,
            [{'op': 'update', 'id': 'water', 'set': {'x': 32}}],
        )
        self.assertFalse(coverage(self.data, changed)['fully_covered'])
        changed['coverage_targets'] = {'sprinkler': {'target_tag': 'missing'}}
        self.assertEqual(
            audit_coverage(self.data, changed)['groups']['sprinkler'][
                'status'
            ],
            'targets_unspecified',
        )

    def test_crop_placeholders_do_not_establish_flower_access_per_bee_house(
        self,
    ):
        plan = self.apply(
            [
                {
                    'op': 'add',
                    'item': {
                        'id': 'bee-a',
                        'type': 'bee-house',
                        'x': 20,
                        'y': 24,
                    },
                },
                {
                    'op': 'add',
                    'item': {
                        'id': 'bee-b',
                        'type': 'bee-house',
                        'x': 35,
                        'y': 24,
                    },
                },
                {'op': 'add', 'item': {'type': 'tulip', 'x': 21, 'y': 24}},
                {
                    'op': 'add',
                    'item': {
                        'type': 'parsnip-seeds',
                        'x': 36,
                        'y': 24,
                        'tags': ['flowers'],
                    },
                },
            ]
        )
        result = coverage(self.data, plan, 'bee')
        self.assertTrue(result['requirement_met'])
        self.assertEqual(result['sources_without_flowers'], ['bee-a', 'bee-b'])
        report = audit_coverage(self.data, plan)
        self.assertEqual(
            report['groups']['bee']['status'], 'optional_no_flowers'
        )
        self.assertNotIn('bee', {issue['group'] for issue in report['issues']})

    def test_review_uncertainty_survives_delivery_and_resolution(self):
        plan = copy.deepcopy(self.plan)
        plan['review_issues'] = [
            {
                'id': 'roof',
                'kind': 'appearance',
                'status': 'unresolved',
                'description': '建筑屋顶未识别清楚',
                'candidates': ['coop', 'big-coop'],
                'reference_box_px': [10, 20, 40, 60],
            }
        ]
        with (
            tempfile.TemporaryDirectory() as temporary,
            contextlib.chdir(temporary),
        ):
            first = deliver(self.data, plan, 'one', scale=1)
            self.assertTrue(
                first['manifest']['review']['requires_user_decision']
            )
            self.assertEqual(
                first['manifest']['review']['recognition_issues'][0][
                    'candidates'
                ],
                ['coop', 'big-coop'],
            )
            plan['review_issues'][0]['status'] = 'resolved'
            with self.assertRaises(LayoutError):
                normalize_plan(self.data, plan)
            plan['review_issues'][0]['resolution'] = '用户确认建筑类型'
            second = deliver(self.data, plan, 'two', scale=1)
            self.assertFalse(
                second['manifest']['review']['requires_user_decision']
            )

    def test_reference_calibration_handles_letterbox_crop_and_sprite_anchor(
        self,
    ):
        with (
            tempfile.TemporaryDirectory() as temporary,
            contextlib.chdir(temporary),
        ):
            reference = Path(temporary) / 'reference.png'
            Image.new('RGB', (1100, 720), '#eeeeee').save(reference)
            points = [
                {'tile': [x, y], 'pixel': [197 + x * 11, -40 + y * 11]}
                for x, y in [(2, 10), (70, 10), (2, 60), (70, 60)]
            ]
            calibrated = calibrate_reference(self.data, reference, points)
            self.assertEqual(calibrated['origin_px'], [197, -40])
            self.assertEqual(calibrated['tile_size_px'], [11, 11])
            item = {'type': 'oak-tree', 'x': 30, 'y': 30}
            ax, ay = self.data.anchor(item)
            px, py = (
                197 + ax * 11,
                -40
                + (ay + 1 - sprite(self.data, 'oak-tree').height / 16) * 11,
            )
            located = locate_reference(
                self.data, calibrated, [px, py], 'oak-tree', 'sprite'
            )
            self.assertEqual(located['nearest_footprint_tile'], [30, 30])
            broken = copy.deepcopy(points)
            broken[-1]['pixel'][0] += 25
            with self.assertRaises(LayoutError):
                calibrate_reference(self.data, reference, broken)
            with self.assertRaises(LayoutError):
                calibrate_reference(self.data, reference, points[:2])
            path = Path(temporary) / 'calibration.json'
            path.write_text(json.dumps(calibrated))
            stage = Path(temporary) / 'stage'
            checkpoint(
                self.data,
                self.plan,
                'final',
                stage,
                reference=reference,
                calibration=path,
                scale=1,
            )
            self.assertTrue((stage / 'comparison.png').is_file())
            with self.assertRaises(LayoutError):
                checkpoint(
                    self.data,
                    create_plan(self.data, 'foraging'),
                    'final',
                    Path(temporary) / 'wrong-map',
                    reference=reference,
                    calibration=path,
                    scale=1,
                )
            Image.new('RGB', (1100, 720), '#bbbbbb').save(reference)
            with self.assertRaises(LayoutError):
                checkpoint(
                    self.data,
                    self.plan,
                    'final',
                    Path(temporary) / 'stale',
                    reference=reference,
                    calibration=path,
                    scale=1,
                )
            self.assertFalse((Path(temporary) / 'stale').exists())

    def test_add_update_remove_keep_other_objects(self):
        added = self.apply(
            [
                {
                    'op': 'add',
                    'item': {'id': 'box', 'type': 'chest', 'x': 20, 'y': 24},
                }
            ]
        )
        changed = self.apply(
            [
                {
                    'op': 'update',
                    'id': 'box',
                    'set': {'x': 21, 'type': 'big-chest'},
                }
            ],
            added,
        )
        self.assertEqual(
            changed['objects'][-1],
            {'id': 'box', 'type': 'big-chest', 'x': 21, 'y': 24},
        )
        removed = self.apply([{'op': 'remove', 'id': 'box'}], changed)
        self.assertEqual(removed, self.plan)
        self.assertEqual(len(self.plan['objects']), 2)

    def test_failed_transaction_preserves_original(self):
        before = copy.deepcopy(self.plan)
        with self.assertRaises(LayoutError):
            self.apply(
                [
                    {'op': 'add', 'item': {'type': 'chest', 'x': 20, 'y': 24}},
                    {'op': 'add', 'item': {'type': 'chest', 'x': 20, 'y': 24}},
                ]
            )
        self.assertEqual(self.plan, before)

    def test_batch_can_swap_occupied_positions(self):
        plan = self.apply(
            [
                {
                    'op': 'add',
                    'item': {
                        'id': f'box-{x}',
                        'type': 'chest',
                        'x': x,
                        'y': 24,
                    },
                }
                for x in [20, 21]
            ]
        )
        swapped = self.apply(
            [
                {'op': 'update', 'id': 'box-20', 'set': {'x': 21}},
                {'op': 'update', 'id': 'box-21', 'set': {'x': 20}},
            ],
            plan,
        )
        self.assertTrue(validate_plan(self.data, swapped)['passed'])

    def test_overlap_layers(self):
        plan = self.apply(
            [
                {'op': 'add', 'item': {'type': 'road', 'x': 20, 'y': 24}},
                {'op': 'add', 'item': {'type': 'chest', 'x': 20, 'y': 24}},
            ]
        )
        self.assertTrue(validate_plan(self.data, plan)['passed'])
        with self.assertRaises(LayoutError):
            self.apply(
                [
                    {
                        'op': 'add',
                        'item': {'type': 'cobblestone', 'x': 20, 'y': 24},
                    },
                    {
                        'op': 'add',
                        'item': {'type': 'parsnip-seeds', 'x': 20, 'y': 24},
                    },
                ]
            )

    def test_local_furniture_and_tree_attachments_preserve_flooring(self):
        plan = self.apply(
            [
                {
                    'op': 'add',
                    'item': {'type': 'wooden-floor', 'x': 20, 'y': 24},
                },
                {'op': 'add', 'item': {'type': 'oak-chair', 'x': 20, 'y': 24}},
                {
                    'op': 'add',
                    'item': {
                        'id': 'z-tree',
                        'type': 'oak-tree',
                        'x': 23,
                        'y': 24,
                    },
                },
                {
                    'op': 'add',
                    'item': {
                        'id': 'a-tapper',
                        'type': 'heavy-tapper',
                        'x': 23,
                        'y': 24,
                    },
                },
            ]
        )
        self.assertTrue(validate_plan(self.data, plan)['passed'])
        for kind in ['chest', 'tapper']:
            with self.subTest(type=kind), self.assertRaises(LayoutError):
                self.apply(
                    [{'op': 'add', 'item': {'type': kind, 'x': 23, 'y': 24}}],
                    plan,
                )
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / 'preview.png'
            first = render_plan(self.data, plan, path, scale=1)
            before = path.read_bytes()
            renamed = copy.deepcopy(plan)
            renamed['objects'][-2]['id'] = 'a-tree'
            renamed['objects'][-1]['id'] = 'z-tapper'
            render_plan(self.data, renamed, path, scale=1)
            self.assertEqual(path.read_bytes(), before)
            diagram = render_plan(
                self.data, plan, path, mode='blueprint', scale=1
            )
            self.assertEqual(
                sum(row['count'] for row in diagram['legend']),
                len(plan['objects']),
            )
            self.assertEqual(first['validation']['passed'], True)

    def test_invalid_coordinates_and_terrain(self):
        for x, y in [
            (-1, 24),
            (20, -1),
            (80, 24),
            (20, 65),
            (False, 24),
            (1.5, 24),
            (0, 0),
        ]:
            with self.subTest(x=x, y=y), self.assertRaises(LayoutError):
                self.apply(
                    [{'op': 'add', 'item': {'type': 'chest', 'x': x, 'y': y}}]
                )

    def test_fill_excludes_sprinkler_tile(self):
        plan = self.apply(
            [
                {
                    'op': 'add',
                    'item': {
                        'id': 'sprinkler',
                        'type': 'q-sprinkler',
                        'x': 21,
                        'y': 25,
                    },
                },
                {
                    'op': 'fill',
                    'type': 'parsnip-seeds',
                    'rect': [20, 24, 3, 3],
                    'exclude': [[21, 25]],
                },
            ]
        )
        report = coverage(self.data, plan)
        self.assertEqual(report['covered_target_cell_count'], 8)
        self.assertTrue(report['fully_covered'])
        moved = self.apply(
            [{'op': 'update', 'id': 'sprinkler', 'set': {'x': 27}}], plan
        )
        self.assertEqual(len(coverage(self.data, moved)['uncovered_cells']), 8)

    def test_nonrectangular_ranges_and_offset(self):
        plan = self.apply(
            [
                {'op': 'add', 'item': {'type': 'sprinkler', 'x': 21, 'y': 25}},
                {
                    'op': 'add',
                    'item': {'type': 'parsnip-seeds', 'x': 22, 'y': 26},
                },
            ]
        )
        self.assertFalse(coverage(self.data, plan)['fully_covered'])
        hut = self.apply(
            [
                {
                    'op': 'add',
                    'item': {'type': 'junimo-hut', 'x': 30, 'y': 25},
                },
                {
                    'op': 'add',
                    'item': {'type': 'parsnip-seeds', 'x': 39, 'y': 34},
                },
            ]
        )
        self.assertTrue(coverage(self.data, hut, 'junimo')['fully_covered'])

    def test_bee_excludes_flower_varieties_replaced_by_placeholders(self):
        self.assertTrue(
            coverage(self.data, self.plan, 'bee')['requirement_met']
        )
        plan = self.apply(
            [
                {'op': 'add', 'item': {'type': 'bee-house', 'x': 21, 'y': 25}},
                {
                    'op': 'add',
                    'item': {
                        'type': 'tulip',
                        'x': 26,
                        'y': 25,
                        'tags': ['flowers'],
                    },
                },
            ]
        )
        result = coverage(self.data, plan, 'bee', target_tag='flowers')
        self.assertFalse(result['fully_covered'])
        self.assertEqual(
            result['sources'][0]['target_ids_in_range'],
            [],
        )
        self.assertEqual(
            result['placeholder_targets_not_treated_as_flowers'],
            [plan['objects'][-1]['id']],
        )

    def test_no_targets_is_not_success(self):
        result = coverage(self.data, self.plan)
        self.assertFalse(result['fully_covered'])
        self.assertIsNone(result['coverage_ratio'])

    def test_fill_respects_multi_tile_footprint(self):
        with self.assertRaises(LayoutError):
            self.apply(
                [{'op': 'fill', 'type': 'coop', 'rect': [20, 24, 3, 3]}]
            )
        filled = self.apply(
            [{'op': 'fill', 'type': 'coop', 'rect': [20, 24, 8, 3]}]
        )
        self.assertEqual(
            sum(item['type'] == 'coop' for item in filled['objects']), 1
        )

    def test_recipe_generation_is_deterministic(self):
        recipe = {
            'schema_version': 1,
            'modules': [
                {'id': 'box', 'objects': [{'type': 'chest', 'x': 0, 'y': 0}]}
            ],
        }
        self.assertEqual(
            generate_plan(self.data, 'forest', recipe),
            generate_plan(self.data, 'forest', recipe),
        )
        bad = copy.deepcopy(recipe)
        bad['modules'][0]['objects'].append({'type': 'chest', 'x': 0, 'y': 0})
        with self.assertRaises(LayoutError):
            generate_plan(self.data, 'forest', bad)

    def test_normalize_v1_restores_default_buildings(self):
        plan = normalize_plan(
            self.data,
            {'schema_version': 1, 'layout': 'regular', 'objects': []},
        )
        self.assertEqual(len(plan['objects']), 2)
        self.assertEqual(plan['schema_version'], 2)

    def test_cli_failure_never_writes_input(self):
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / 'plan.json'
            path.write_text(json.dumps(self.plan))
            original = path.read_bytes()
            command = [
                sys.executable,
                str(self.data.root / 'scripts/layout.py'),
            ]
            result = subprocess.run(
                [*command, 'add', str(path), 'chest', '--x', '-1', '--y', '0'],
                capture_output=True,
                check=False,
            )
            self.assertNotEqual(result.returncode, 0)
            self.assertEqual(path.read_bytes(), original)
            result = subprocess.run(
                [*command, 'coverage', str(path), '--out', str(path)],
                capture_output=True,
                check=False,
            )
            self.assertNotEqual(result.returncode, 0)
            self.assertEqual(path.read_bytes(), original)

    def test_render_is_deterministic_and_crop_bounded(self):
        with tempfile.TemporaryDirectory() as temp:
            a, b = Path(temp) / 'a.png', Path(temp) / 'b.png'
            render_plan(
                self.data, self.plan, a, scale=1, crop=[18, 20, 20, 15]
            )
            render_plan(
                self.data, self.plan, b, scale=1, crop=[18, 20, 20, 15]
            )
            self.assertEqual(a.read_bytes(), b.read_bytes())
            with self.assertRaises(LayoutError):
                render_plan(self.data, self.plan, a, crop=[-1, 0, 3, 3])


if __name__ == '__main__':
    unittest.main()
