# /// script
# requires-python = ">=3.12"
# dependencies = ["pillow==12.3.0"]
# ///

# ruff: noqa: E402

import copy
import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

from PIL import Image, ImageColor

ROOT = Path(__file__).resolve().parents[1] / 'skills/stardew-planner-skill'
sys.path.insert(0, str(ROOT / 'scripts'))

from layout_assets import connection_id, sprite
from layout_core import (
    Dataset,
    LayoutError,
    apply_operations,
    can_overlap,
    create_plan,
    generate_plan,
    inventory,
    normalize_plan,
    validate_plan,
)
from layout_import import import_planner
from layout_render import render_plan


class FlooringOverlapTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.data = Dataset(ROOT)
        cls.floors = [
            item['id']
            for item in cls.data.items.values()
            if item['subcategory'] == 'flooring' and not item['hidden_in_menu']
        ]

    def stacked_plan(self, item_type='keg', flooring='wooden-floor'):
        return apply_operations(
            self.data,
            create_plan(self.data, 'regular'),
            [
                {
                    'op': 'add',
                    'item': {
                        'id': 'floor',
                        'type': flooring,
                        'x': 20,
                        'y': 24,
                    },
                },
                {
                    'op': 'add',
                    'item': {
                        'id': 'upper',
                        'type': item_type,
                        'x': 20,
                        'y': 24,
                    },
                },
            ],
        )

    def test_equipment_and_decorations_remain_on_every_floor_type(self):
        for flooring in self.floors:
            for item_type in [
                'keg',
                'preserves-jar',
                'cask',
                'furnace',
                'heavy-furnace',
                'crystalarium',
                'mayonnaise-machine',
                'cheese-press',
                'loom',
                'oil-maker',
                'dehydrator',
                'fish-smoker',
                'seed-maker',
                'recycling-machine',
                'worm-bin',
                'mushroom-log',
                'chest',
                'big-chest',
                'junimo-chest',
                'hopper',
                'scarecrow',
                'rarecrow-1',
                'deluxe-scarecrow',
                'sprinkler',
                'quality-sprinkler',
                'iridium-sprinkler-pressured',
                'bee-house',
                'lightning-rod',
                'solar-panel',
                'garden-pot',
                'wood-sign',
                'text-sign',
                'wood-fence',
                'wood-gate',
                'candle-lamp',
                'wood-lamp-post',
                'wooden-brazier',
                'solid-gold-lewis',
                'lawn-flamingo',
                'oak-chair',
                'anvil',
                'cookout-kit',
                'tent-kit',
            ]:
                with self.subTest(flooring=flooring, item=item_type):
                    plan = self.stacked_plan(item_type, flooring)
                    report = validate_plan(self.data, plan)
                    self.assertTrue(report['passed'], report)
                    self.assertEqual(
                        report['flooring_overlaps'],
                        [
                            {
                                'flooring_id': 'floor',
                                'object_id': 'upper',
                                'cells': [(20, 24)],
                            }
                        ],
                    )
                    self.assertEqual(
                        {
                            item['type']
                            for item in plan['objects']
                            if item['id'] in {'floor', 'upper'}
                        },
                        {flooring, item_type},
                    )
                    self.assertNotIn('omitted_entities', plan)
                    plan['objects'].reverse()
                    self.assertEqual(validate_plan(self.data, plan), report)

    def test_floor_does_not_permit_two_upper_objects(self):
        for second_type in ['keg', 'chest', 'wood-fence', 'oak-chair']:
            plan = self.stacked_plan()
            with self.subTest(second_type=second_type):
                with self.assertRaisesRegex(LayoutError, 'overlap'):
                    apply_operations(
                        self.data,
                        plan,
                        [
                            {
                                'op': 'add',
                                'item': {
                                    'type': second_type,
                                    'x': 20,
                                    'y': 24,
                                },
                            }
                        ],
                    )
                self.assertEqual(
                    len(validate_plan(self.data, plan)['flooring_overlaps']), 1
                )

    def test_floor_does_not_permit_unsupported_surfaces_or_attachments(self):
        floor = {'type': 'wooden-floor', 'x': 20, 'y': 24}
        for item_type in [
            'wooden-floor',
            'stone-floor',
            'hoe-dirt',
            'strawberry-seeds',
            'giant-pumpkin',
            'oak-tree',
            'apple-tree',
            'shed',
            'crab-pot',
            'tapper',
            'heavy-tapper',
        ]:
            other = {'type': item_type, 'x': 20, 'y': 24}
            with self.subTest(item_type=item_type):
                self.assertFalse(can_overlap(self.data, floor, other))
                self.assertFalse(can_overlap(self.data, other, floor))
                with self.assertRaisesRegex(LayoutError, 'overlap'):
                    self.stacked_plan(item_type)

    def test_multi_tile_furniture_preserves_each_floor_and_removal(self):
        plan = apply_operations(
            self.data,
            create_plan(self.data, 'regular'),
            [
                {'op': 'fill', 'type': 'stone-floor', 'rect': [20, 24, 5, 2]},
                {
                    'op': 'add',
                    'item': {
                        'id': 'table',
                        'type': 'modern-dining-table',
                        'x': 20,
                        'y': 24,
                    },
                },
            ],
        )
        self.assertEqual(
            len(validate_plan(self.data, plan)['flooring_overlaps']), 10
        )
        counts = {
            row['type']: row['count']
            for row in inventory(self.data, plan)['items']
        }
        self.assertEqual(counts['stone-floor'], 10)
        self.assertEqual(counts['modern-dining-table'], 1)
        removed = apply_operations(
            self.data, plan, [{'op': 'remove', 'id': 'table'}]
        )
        self.assertEqual(
            sum(item['type'] == 'stone-floor' for item in removed['objects']),
            10,
        )
        self.assertEqual(
            validate_plan(self.data, removed)['flooring_overlaps'], []
        )

    def test_import_normalization_and_recipe_keep_both_layers(self):
        imported = import_planner(
            self.data,
            {
                'tiles': [{'type': 'keg', 'x': 320, 'y': 384}],
                'flooring': [{'type': 'wooden-floor', 'x': 320, 'y': 384}],
            },
        )
        normalized = normalize_plan(self.data, imported)
        self.assertEqual(normalize_plan(self.data, normalized), normalized)
        generated = generate_plan(
            self.data,
            'regular',
            {
                'schema_version': 1,
                'modules': [
                    {
                        'id': 'paved-keg',
                        'objects': [
                            {'type': 'keg', 'x': 0, 'y': 0},
                            {'type': 'wooden-floor', 'x': 0, 'y': 0},
                        ],
                    }
                ],
            },
        )
        for plan in [imported, normalized, generated]:
            self.assertEqual(
                len(validate_plan(self.data, plan)['flooring_overlaps']), 1
            )
            self.assertEqual(
                sum(item['type'] == 'keg' for item in plan['objects']), 1
            )
            self.assertEqual(
                sum(
                    item['type'] == 'wooden-floor' for item in plan['objects']
                ),
                1,
            )

    def test_render_retains_upper_sprite_and_floor_connections(self):
        plan = self.stacked_plan()
        plan = apply_operations(
            self.data,
            plan,
            [
                {
                    'op': 'add',
                    'item': {
                        'id': 'next-floor',
                        'type': 'wooden-floor',
                        'x': 21,
                        'y': 24,
                    },
                }
            ],
        )
        occupied = {}
        for item in plan['objects']:
            for cell in self.data.cells(item):
                occupied.setdefault(cell, []).append(item)
        adjacent = next(
            item for item in plan['objects'] if item['id'] == 'next-floor'
        )
        self.assertEqual(
            connection_id(self.data, adjacent, occupied), 'wooden-floor-he'
        )
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / 'preview.png'
            result = render_plan(self.data, plan, path, scale=1)
            map_x, map_y = result['map_region_px'][:2]
            original = path.read_bytes()
            with Image.open(path) as image:
                keg = sprite(self.data, 'keg')
                opaque = [
                    (x, y)
                    for y in range(keg.height)
                    for x in range(keg.width)
                    if keg.getpixel((x, y))[3] == 255
                ]
                self.assertTrue(opaque)
                x, y = opaque[len(opaque) // 2]
                self.assertEqual(
                    image.convert('RGB').getpixel(
                        (map_x + 20 * 16 + x, map_y + 25 * 16 - keg.height + y)
                    ),
                    keg.getpixel((x, y))[:3],
                )
            reversed_plan = copy.deepcopy(plan)
            reversed_plan['objects'].reverse()
            render_plan(self.data, reversed_plan, path, scale=1)
            self.assertEqual(path.read_bytes(), original)
            diagram = render_plan(
                self.data, plan, path, mode='blueprint', scale=1
            )
            self.assertIn('upper', diagram['numbered_object_ids'])
            self.assertEqual(
                sum(row['count'] for row in diagram['legend']),
                len(plan['objects']),
            )
            colors = {
                row['type']: ImageColor.getrgb(row['color'])
                for row in diagram['legend']
            }
            map_x, map_y = diagram['map_region_px'][:2]
            with Image.open(path) as image:
                self.assertEqual(
                    image.getpixel(
                        (map_x + 20 * 16 + 4, map_y + 24 * 16 + 13)
                    ),
                    colors['wooden-floor'],
                )
                self.assertEqual(
                    image.getpixel((map_x + 20 * 16 + 4, map_y + 24 * 16 + 3)),
                    colors['keg'],
                )

    def test_queries_and_recognition_mark_flooring_without_omitting_objects(
        self,
    ):
        index = json.loads((ROOT / 'data/recognition/index.json').read_text())
        for item_type, allowed in [
            ('keg', True),
            ('solid-gold-lewis', True),
            ('crab-pot', False),
            ('heavy-tapper', False),
        ]:
            for flags in [[], ['--full']]:
                with self.subTest(item_type=item_type, flags=flags):
                    result = subprocess.run(
                        [
                            sys.executable,
                            str(ROOT / 'scripts/query_catalog.py'),
                            item_type,
                            *flags,
                        ],
                        check=True,
                        capture_output=True,
                        text=True,
                    )
                    item = json.loads(result.stdout)['items'][0]
                    self.assertEqual(item['recognition_action'], 'place')
                    rule = item['flooring_overlap']
                    self.assertEqual(rule['can_place_on_flooring'], allowed)
                    self.assertEqual(rule['preserve_flooring'], allowed)
                    self.assertTrue(rule['source_urls'])
                    self.assertEqual(
                        index['items'][index['lookup'][item_type]][
                            'can_place_on_flooring'
                        ],
                        allowed,
                    )
        keg_card = index['items'][index['lookup']['keg']]
        self.assertTrue(
            any(
                '可置地板' in ' '.join(row['lines'])
                or 'on flooring' in ' '.join(row['lines'])
                for row in keg_card['text_regions']
            )
        )
        self.assertEqual(index['items']['torch']['recognition_action'], 'omit')
        self.assertNotIn('can_place_on_flooring', index['items']['torch'])


if __name__ == '__main__':
    unittest.main()
