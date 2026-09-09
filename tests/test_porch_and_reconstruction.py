# /// script
# requires-python = ">=3.12"
# dependencies = ["pillow==12.3.0"]
# ///

# ruff: noqa: E402

import copy
import hashlib
import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from PIL import Image, ImageColor

ROOT = Path(__file__).resolve().parents[1] / 'skills/stardew-planner-skill'
sys.path.insert(0, str(ROOT / 'scripts'))

from layout_assets import sprite
from layout_core import (
    Dataset,
    LayoutError,
    apply_operations,
    create_plan,
    inventory,
    normalize_plan,
    validate_plan,
    write_json_atomic,
)
from layout_delivery import deliver
from layout_images import crop_image, recognition_crops
from layout_import import import_planner
from layout_placement import porch_cells
from layout_render import render_plan


class PorchAndReconstructionTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.data = Dataset(ROOT)

    def porch_plan(self, kind='house-3', guest='keg'):
        plan = create_plan(self.data, 'regular')
        x, y = sorted(porch_cells(self.data.items[kind]))[0]
        plan['objects'] = [
            {'id': 'z-house', 'type': kind, 'x': 20, 'y': 24},
            {'id': 'a-guest', 'type': guest, 'x': 20 + x, 'y': 24 + y},
        ]
        return normalize_plan(self.data, plan)

    def test_all_residential_upgrades_keep_porch_objects_and_counts(self):
        hosts = [
            key for key, item in self.data.items.items() if porch_cells(item)
        ]
        self.assertEqual(len(hosts), 24)
        for kind in hosts:
            for guest in ['keg', 'oak-chair', 'candle-lamp', 'wooden-brazier']:
                with self.subTest(kind=kind, guest=guest):
                    plan = self.porch_plan(kind, guest)
                    report = validate_plan(self.data, plan)
                    self.assertTrue(report['passed'], report)
                    self.assertEqual(len(report['porch_overlaps']), 1)
                    self.assertEqual(report['flooring_overlaps'], [])
                    self.assertEqual(
                        {
                            row['type']: row['count']
                            for row in inventory(self.data, plan)['items']
                        },
                        {kind: 1, guest: 1},
                    )
                    plan['objects'].reverse()
                    self.assertEqual(validate_plan(self.data, plan), report)

    def test_porch_rule_rejects_walls_mailboxes_stairs_and_second_objects(
        self,
    ):
        for kind, dx, dy in [
            ('house-3', 1, 2),
            ('house-3', 4, 3),
            ('house-3', 9, 4),
            ('stone-cabin', 4, 2),
            ('stone-cabin', 1, 1),
        ]:
            with self.subTest(kind=kind, position=(dx, dy)):
                plan = self.porch_plan(kind)
                plan['objects'][1].update(x=20 + dx, y=24 + dy)
                self.assertFalse(validate_plan(self.data, plan)['passed'])
        for guest in [
            'oak-tree',
            'strawberry-seeds',
            'crab-pot',
            'tapper',
            'shed',
        ]:
            with self.subTest(guest=guest):
                self.assertFalse(
                    validate_plan(self.data, self.porch_plan(guest=guest))[
                        'passed'
                    ]
                )
        plan = self.porch_plan()
        before = copy.deepcopy(plan)
        with self.assertRaisesRegex(LayoutError, 'overlap'):
            apply_operations(
                self.data,
                plan,
                [
                    {
                        'op': 'add',
                        'item': {
                            **plan['objects'][1],
                            'id': 'second',
                            'type': 'oak-chair',
                        },
                    }
                ],
            )
        self.assertEqual(plan, before)

    def test_multi_tile_furniture_must_fit_usable_porch(self):
        plan = self.porch_plan(guest='oak-bench')
        self.assertTrue(validate_plan(self.data, plan)['passed'])
        plan['objects'][1]['x'] += 2
        self.assertFalse(validate_plan(self.data, plan)['passed'])

    def test_porch_surface_only_exempts_its_own_underlying_terrain(self):
        plan = self.porch_plan()
        guest = plan['objects'][1]
        blocked = copy.deepcopy(self.data.blocked('regular'))
        blocked['accessible'].add((guest['x'], guest['y']))
        with patch.object(self.data, 'blocked', return_value=blocked):
            self.assertTrue(validate_plan(self.data, plan)['passed'])
            plan['objects'] = [guest]
            self.assertIn(
                'terrain_restriction',
                {
                    row['code']
                    for row in validate_plan(self.data, plan)['errors']
                },
            )

    def test_porch_sprite_and_diagram_marker_remain_visible(self):
        plan = self.porch_plan()
        guest = plan['objects'][1]
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / 'preview.png'
            result = render_plan(self.data, plan, path, scale=1)
            original = path.read_bytes()
            x0, y0 = result['map_region_px'][:2]
            pixels = sprite(self.data, 'keg')
            with Image.open(path) as image:
                for y in range(pixels.height):
                    for x in range(pixels.width):
                        pixel = pixels.getpixel((x, y))
                        if pixel[3] == 255:
                            self.assertEqual(
                                image.getpixel(
                                    (
                                        x0 + guest['x'] * 16 + x,
                                        y0
                                        + (guest['y'] + 1) * 16
                                        - pixels.height
                                        + y,
                                    )
                                ),
                                pixel[:3],
                            )
            plan['objects'].reverse()
            render_plan(self.data, plan, path, scale=1)
            self.assertEqual(path.read_bytes(), original)
            diagram = render_plan(
                self.data, plan, path, mode='blueprint', scale=1
            )
            self.assertEqual(
                set(diagram['numbered_object_ids']), {'z-house', 'a-guest'}
            )
            color = next(
                row['color']
                for row in diagram['legend']
                if row['type'] == 'keg'
            )
            x0, y0 = diagram['map_region_px'][:2]
            with Image.open(path) as image:
                self.assertEqual(
                    image.getpixel(
                        (x0 + guest['x'] * 16 + 2, y0 + guest['y'] * 16 + 2)
                    ),
                    ImageColor.getrgb(color),
                )

    def test_bundled_mailbox_is_omitted_on_normalize_and_import(self):
        for kind in ['mailbox', '信箱', '邮箱']:
            plan = self.porch_plan()
            plan['objects'].append(
                {'id': 'mail', 'type': kind, 'x': 29, 'y': 28}
            )
            normalized = normalize_plan(self.data, plan)
            self.assertEqual(len(normalized['objects']), 2)
            self.assertEqual(
                normalized['omitted_entities']['counts'], {'mailbox': 1}
            )
            self.assertEqual(normalize_plan(self.data, normalized), normalized)
        imported = import_planner(
            self.data, {'tiles': [{'type': 'mailbox', 'x': 400, 'y': 400}]}
        )
        self.assertEqual(
            imported['omitted_entities']['counts'], {'mailbox': 1}
        )

    def test_queries_and_atlas_expose_mailbox_and_shipping_references(self):
        index = json.loads((ROOT / 'data/recognition/index.json').read_text())
        shipping = index['items']['shipping-bin']
        self.assertEqual(
            (shipping['group'], shipping['family_id']), ('tools', 'storage')
        )
        self.assertEqual(shipping['footprint_tiles'], [2, 1])
        self.assertEqual(
            index['items']['mini-shipping-bin']['footprint_tiles'], [1, 1]
        )
        for kind in [
            'house',
            'house-3',
            'stone-cabin',
            'shipping-bin',
            'mailbox',
        ]:
            result = subprocess.run(
                [sys.executable, str(ROOT / 'scripts/query_catalog.py'), kind],
                capture_output=True,
                text=True,
                check=True,
            )
            item = json.loads(result.stdout)['items'][0]
            if kind in ['house', 'house-3', 'stone-cabin']:
                self.assertTrue(
                    item['building_placement']['porch_object_cells']
                )
                self.assertEqual(
                    item['building_placement']['bundled_mailbox'][
                        'recognition_action'
                    ],
                    'ignore_as_separate_object',
                )
            elif kind == 'mailbox':
                self.assertEqual(item['recognition_action'], 'omit')
            for reference in item.get('reference_images', []):
                self.assertEqual(
                    hashlib.sha256(
                        Path(reference['image_absolute_path']).read_bytes()
                    ).hexdigest(),
                    reference['sha256'],
                )

    def test_boxed_crop_uses_source_bounds_and_preserves_original(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            source = root / 'reference.png'
            Image.new('RGB', (100, 100), 'green').save(source)
            before = source.read_bytes()
            result = crop_image(
                source,
                root / 'crop.png',
                [10, 20, 60, 50],
                scale=2,
                highlight_box=[20, 30, 40, 50],
            )
            with Image.open(result['path']) as image:
                self.assertEqual(image.size, (120, 100))
                self.assertEqual(image.getpixel((20, 20)), (229, 57, 53, 255))
                self.assertEqual(image.getpixel((40, 40)), (0, 128, 0, 255))
            self.assertEqual(source.read_bytes(), before)
            with self.assertRaisesRegex(LayoutError, 'Highlight box'):
                crop_image(
                    source,
                    root / 'bad.png',
                    [10, 20, 60, 50],
                    highlight_box=[0, 0, 40, 50],
                )
            self.assertFalse((root / 'bad.png').exists())

    def test_delivery_preserves_gaps_and_extracts_unplaced_identity(self):
        plan = create_plan(self.data, 'regular')
        plan['objects'] = [
            {'id': 'hut', 'type': 'junimo-hut', 'x': 20, 'y': 30},
            {'id': 'sprinkler', 'type': 'sprinkler', 'x': 20, 'y': 25},
            {'id': 'crop', 'type': 'strawberry-seeds', 'x': 50, 'y': 50},
        ]
        plan['review_issues'] = [
            {
                'id': '../unidentified',
                'kind': 'identity',
                'status': 'unresolved',
                'description': '参考图中物体未识别。',
                'object_ids': [],
                'candidates': [],
                'reference_box_px': [0, 0, 30, 40],
            }
        ]
        plan = normalize_plan(self.data, plan)
        before = copy.deepcopy(plan)
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            reference = root / 'reference.png'
            Image.new('RGB', (100, 90), 'green').save(reference)
            review_dir = root / 'checkpoint'
            write_json_atomic(
                review_dir / 'review.json',
                {
                    'reference': {
                        'path': str(reference),
                        'sha256': hashlib.sha256(
                            reference.read_bytes()
                        ).hexdigest(),
                    }
                },
            )
            with patch(
                'layout_delivery.validate_patch_review',
                return_value={
                    'overall_result': 'uncertain',
                    'overall_status': 'completed',
                    'counts': {'uncertain': 1},
                },
            ):
                result = deliver(
                    self.data,
                    plan,
                    root / 'result',
                    scale=1,
                    crop=[18, 22, 40, 32],
                    workspace=root,
                    review_directory=review_dir,
                )
            summary = result['manifest']['review']['completion_summary']
            for group in ['sprinkler', 'junimo']:
                gap = next(
                    row
                    for row in summary['coverage_gaps']
                    if row['group'] == group
                )
                self.assertEqual(gap['uncovered_cells'], [(50, 50)])
                self.assertTrue(gap['requires_user_decision'])
                self.assertEqual(gap['decision_status'], 'pending')
            image = summary['unidentified_objects'][0]
            self.assertEqual(image['status'], 'extracted')
            self.assertTrue((root / 'result' / image['path']).is_file())
            with Image.open(root / 'result' / image['path']) as pixels:
                self.assertEqual(pixels.getpixel((0, 0)), (229, 57, 53, 255))
                self.assertEqual(pixels.size, (54, 64))
            self.assertEqual(
                json.loads((root / 'result/plan.json').read_text()), before
            )
            self.assertEqual(plan, before)
            coordinates = result['manifest']['image_coordinates']
            self.assertEqual(
                [
                    row['value']
                    for row in coordinates['preview.png']['coordinate_ticks'][
                        'x'
                    ]
                ],
                list(range(18, 58)),
            )
            self.assertEqual(
                len(
                    coordinates['implementation.png']['coordinate_ticks']['x']
                ),
                80,
            )

    def test_missing_and_out_of_bounds_identity_boxes_remain_explicit(self):
        plan = {
            'review_issues': [
                {
                    'id': 'unknown',
                    'kind': 'identity',
                    'status': 'unresolved',
                    'description': '未识别物体。',
                }
            ]
        }
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            reference = root / 'source.png'
            Image.new('RGB', (30, 30)).save(reference)
            self.assertEqual(
                recognition_crops(plan, None, root)[0]['status'],
                'missing_reference',
            )
            self.assertEqual(
                recognition_crops(plan, reference, root)[0]['status'],
                'missing_reference_box',
            )
            plan['review_issues'][0]['reference_box_px'] = [0, 0, 31, 30]
            with self.assertRaisesRegex(LayoutError, 'outside'):
                recognition_crops(plan, reference, root)

    def test_preview_ticks_match_diagram_on_all_scales_and_crops(self):
        plan = self.porch_plan()
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            for scale in [1, 2, 4]:
                with self.subTest(scale=scale):
                    preview = render_plan(
                        self.data,
                        plan,
                        root / 'preview.png',
                        scale=scale,
                        crop=[19, 22, 15, 10],
                    )
                    diagram = render_plan(
                        self.data,
                        plan,
                        root / 'implementation.png',
                        mode='blueprint',
                        scale=scale,
                        crop=[19, 22, 15, 10],
                    )
                    for axis in ['x', 'y']:
                        self.assertEqual(
                            [
                                (r['value'], r['major'])
                                for r in preview['coordinate_ticks'][axis]
                            ],
                            [
                                (r['value'], r['major'])
                                for r in diagram['coordinate_ticks'][axis]
                            ],
                        )
                    with Image.open(root / 'preview.png') as image:
                        for axis in ['x', 'y']:
                            for tick in preview['coordinate_ticks'][axis]:
                                for segment in tick['segments_px']:
                                    x, y = map(int, segment[:2])
                                    self.assertEqual(
                                        image.getpixel((x, y)),
                                        ImageColor.getrgb('#617261'),
                                    )


if __name__ == '__main__':
    unittest.main()
