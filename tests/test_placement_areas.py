# /// script
# requires-python = ">=3.12"
# dependencies = ["pillow==12.3.0"]
# ///

import copy
import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from PIL import Image

sys.path.insert(
    0,
    str(
        Path(__file__).resolve().parents[1]
        / 'skills/stardew-planner-skill/scripts'
    ),
)
sys.path.insert(
    0, str(Path(__file__).resolve().parents[1] / 'scripts/maintenance')
)

from layout_assets import sprite
from layout_checkpoint import checkpoint
from layout_core import (
    Dataset,
    LayoutError,
    apply_operations,
    create_plan,
    inventory,
    normalize_plan,
    validate_plan,
)
from layout_import import import_planner
from layout_placement import placement_areas, rectangle_cells
from layout_render import render_plan
from sync_variants import apply_placement_overrides, referenced_image_paths


class PlacementAreaTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.data = Dataset()

    def setUp(self):
        self.plan = create_plan(self.data, 'regular')
        self.greenhouse = next(
            item
            for item in self.plan['objects']
            if item['type'] == 'greenhouse-repaired'
        )

    def test_greenhouse_reserves_only_42_cells_and_keeps_front_art_separate(
        self,
    ):
        item = {'type': 'greenhouse-repaired', 'x': 10, 'y': 20}
        areas = placement_areas(self.data.items[item['type']], 10, 20)
        self.assertEqual(areas['solid_rect'], [10, 20, 7, 6])
        self.assertEqual(areas['additional_areas'], [])
        solid = self.data.cells(item)
        decoration = self.data.items[item['type']]['building_placement'][
            'front_decoration'
        ]
        self.assertEqual(decoration['tile_area'], [2, 6, 3, 2])
        extra = rectangle_cells([12, 26, 3, 2])
        self.assertEqual(len(solid), 42)
        self.assertFalse(solid & extra)
        self.assertNotIn((10, 26), solid | extra)
        self.assertNotIn((16, 27), solid | extra)

    def test_greenhouse_front_accepts_flooring_and_independent_objects(self):
        x, y = self.greenhouse['x'] + 2, self.greenhouse['y'] + 6
        floor = apply_operations(
            self.data,
            self.plan,
            [{'op': 'fill', 'type': 'wooden-floor', 'rect': [x, y, 3, 2]}],
        )
        self.assertTrue(validate_plan(self.data, floor)['passed'])
        for kind in ['keg', 'chest', 'oak-chair', 'candle-lamp', 'wood-fence']:
            with self.subTest(kind=kind):
                placed = apply_operations(
                    self.data,
                    floor,
                    [{'op': 'fill', 'type': kind, 'rect': [x, y, 3, 2]}],
                )
                self.assertTrue(validate_plan(self.data, placed)['passed'])
                self.assertEqual(
                    sum(item['type'] == kind for item in placed['objects']), 6
                )
                self.assertEqual(
                    len(validate_plan(self.data, placed)['flooring_overlaps']),
                    6,
                )
        beside = apply_operations(
            self.data,
            self.plan,
            [{'op': 'add', 'item': {'type': 'chest', 'x': x - 2, 'y': y}}],
        )
        self.assertTrue(validate_plan(self.data, beside)['passed'])
        for dx, dy in [(0, 0), (2, 5), (6, 5)]:
            with (
                self.subTest(solid_cell=(dx, dy)),
                self.assertRaisesRegex(LayoutError, 'overlap'),
            ):
                apply_operations(
                    self.data,
                    self.plan,
                    [
                        {
                            'op': 'add',
                            'item': {
                                'type': 'keg',
                                'x': self.greenhouse['x'] + dx,
                                'y': self.greenhouse['y'] + dy,
                            },
                        }
                    ],
                )

    def test_mature_trees_fit_beside_greenhouse_and_share_only_canopies(self):
        trees = [
            'maple-tree',
            'oak-tree',
            'pine-tree',
            'mahogany-tree',
            'palm-tree',
            'palm-tree-island',
            'mushroom-tree',
            'mystic-tree',
            'green-tree-1',
            'green-tree-2',
            'green-tree-3',
            'apricot-tree',
            'cherry-tree',
            'banana-tree',
            'mango-tree',
            'orange-tree',
            'peach-tree',
            'apple-tree',
            'pomegranate-tree',
        ]
        x, y = self.greenhouse['x'] + 7, self.greenhouse['y'] + 3
        for kind in trees:
            with self.subTest(kind=kind):
                operations = [
                    {
                        'op': 'add',
                        'item': {'id': 'tree', 'type': kind, 'x': x, 'y': y},
                    },
                    {
                        'op': 'add',
                        'item': {
                            'id': 'neighbor',
                            'type': kind,
                            'x': x + 1,
                            'y': y,
                        },
                    },
                    {
                        'op': 'add',
                        'item': {'type': 'chest', 'x': x, 'y': y + 1},
                    },
                ]
                plan = apply_operations(self.data, self.plan, operations)
                tree = next(
                    item for item in plan['objects'] if item['id'] == 'tree'
                )
                self.assertEqual(self.data.cells(tree), {(x, y)})
                self.assertTrue(validate_plan(self.data, plan)['passed'])
                with self.assertRaisesRegex(LayoutError, 'overlap'):
                    apply_operations(
                        self.data,
                        plan,
                        [
                            {
                                'op': 'add',
                                'item': {'type': 'chest', 'x': x, 'y': y},
                            },
                        ],
                    )

    def test_tree_import_and_seasonal_sprites_use_trunk_coordinates(self):
        for kind in ['apple-tree-summer', 'mushroom-tree', 'oak-tree']:
            with self.subTest(kind=kind):
                plan = import_planner(
                    self.data,
                    {
                        'tiles': [{'type': kind, 'x': 30 * 16, 'y': 30 * 16}],
                    },
                )
                tree = next(
                    item
                    for item in plan['objects']
                    if item['type'] == self.data.item_id(kind)
                )
                self.assertEqual((tree['x'], tree['y']), (31, 30))
                self.assertEqual(self.data.anchor(tree), (30, 30))
                self.assertEqual(self.data.dimensions(kind), (1, 1))
                self.assertEqual(self.data.cells(tree), {(31, 30)})

    def test_slime_hutch_leaves_old_footprint_space_available(self):
        plan = apply_operations(
            self.data,
            self.plan,
            [
                {
                    'op': 'add',
                    'item': {
                        'id': 'hutch',
                        'type': 'slime-hutch',
                        'x': 20,
                        'y': 30,
                    },
                },
                {'op': 'add', 'item': {'type': 'chest', 'x': 27, 'y': 30}},
                {
                    'op': 'add',
                    'item': {'type': 'apple-tree', 'x': 20, 'y': 34},
                },
            ],
        )
        hutch = next(item for item in plan['objects'] if item['id'] == 'hutch')
        self.assertEqual(len(self.data.cells(hutch)), 28)
        self.assertTrue(validate_plan(self.data, plan)['passed'])
        convention = self.data.items['slime-hutch']['placement']['convention']
        self.assertEqual(convention['since_game_version'], '1.6')
        self.assertEqual(convention['legacy_footprint_tiles'], [11, 6])

    def test_front_art_does_not_add_boundary_or_terrain_restrictions(self):
        plan = create_plan(self.data, 'regular')
        plan['objects'] = [
            {
                'id': 'greenhouse',
                'type': 'greenhouse-repaired',
                'x': 10,
                'y': 59,
            }
        ]
        empty = {layer: set() for layer in self.data.blocked('regular')}
        with patch.object(self.data, 'blocked', return_value=empty):
            codes = {
                error['code']
                for error in validate_plan(self.data, plan)['errors']
            }
        self.assertNotIn('additional_placement_out_of_bounds', codes)
        self.assertNotIn('out_of_bounds', codes)
        x, y = self.greenhouse['x'] + 2, self.greenhouse['y'] + 6
        blocked = {
            layer: set(cells)
            for layer, cells in self.data.blocked('regular').items()
        }
        blocked['accessible'].add((x, y))
        with patch.object(self.data, 'blocked', return_value=blocked):
            codes = {
                error['code']
                for error in validate_plan(self.data, self.plan)['errors']
            }
        self.assertNotIn('additional_placement_terrain', codes)
        blocked_plan = copy.deepcopy(self.plan)
        blocked_plan['objects'].append(
            {'id': 'blocked-keg', 'type': 'keg', 'x': x, 'y': y}
        )
        with patch.object(self.data, 'blocked', return_value=blocked):
            self.assertIn(
                'terrain_restriction',
                {
                    error['code']
                    for error in validate_plan(self.data, blocked_plan)[
                        'errors'
                    ]
                },
            )
        plan['objects'][0]['y'] += 1
        with patch.object(self.data, 'blocked', return_value=empty):
            self.assertIn(
                'out_of_bounds',
                {
                    error['code']
                    for error in validate_plan(self.data, plan)['errors']
                },
            )

    def test_farmhouse_preserves_distinct_buildable_and_passable_checks(self):
        for kind in ['house', 'house-2', 'house-3']:
            with self.subTest(kind=kind):
                regions = placement_areas(self.data.items[kind])[
                    'additional_areas'
                ]
                self.assertEqual(
                    regions,
                    [
                        {
                            'tile_rect': [9, 4, 1, 1],
                            'requirement': 'buildable',
                        },
                        {'tile_rect': [5, 5, 1, 1], 'requirement': 'passable'},
                    ],
                )
        house = next(
            item for item in self.plan['objects'] if item['type'] == 'house'
        )
        for x, y in [
            (house['x'] + 9, house['y'] + 4),
            (house['x'] + 5, house['y'] + 5),
        ]:
            with self.assertRaisesRegex(
                LayoutError, 'additional_placement_obstructed'
            ):
                apply_operations(
                    self.data,
                    self.plan,
                    [{'op': 'add', 'item': {'type': 'chest', 'x': x, 'y': y}}],
                )

    def test_query_diagram_and_patch_index_do_not_reserve_greenhouse_front(
        self,
    ):
        root = (
            Path(__file__).resolve().parents[1]
            / 'skills/stardew-planner-skill'
        )
        query = json.loads(
            subprocess.check_output(
                [
                    sys.executable,
                    str(root / 'scripts/query_catalog.py'),
                    'greenhouse-repaired',
                ],
                text=True,
            )
        )['items'][0]
        self.assertEqual(query['footprint_tiles'], [7, 6])
        self.assertEqual(query['placement_size_label'], '7×6')
        self.assertEqual(
            query['placement_areas_tiles']['additional_areas'], []
        )
        self.assertFalse(
            query['building_placement']['front_decoration'][
                'count_as_flooring'
            ]
        )
        self.assertEqual(len(query['reference_images']), 2)
        with tempfile.TemporaryDirectory() as temporary:
            directory = Path(temporary)
            rendered = render_plan(
                self.data,
                self.plan,
                directory / 'implementation.png',
                mode='blueprint',
                scale=1,
            )
            expected = {
                'object_id': self.greenhouse['id'],
                'tile_rect': [
                    self.greenhouse['x'] + 2,
                    self.greenhouse['y'] + 6,
                    3,
                    2,
                ],
                'requirement': 'passable',
            }
            self.assertNotIn(expected, rendered['additional_placement_areas'])
            self.assertEqual(
                sum(row['count'] for row in rendered['legend']),
                len(self.plan['objects']),
            )
            checkpoint(
                self.data, self.plan, 'final', directory / 'final', scale=1
            )
            index = json.loads(
                (directory / 'final/patches/index.json').read_text()
            )
            self.assertFalse(
                any(
                    expected in region['additional_placement_areas']
                    for region in index['patches']
                )
            )

    def test_front_art_renders_below_flooring_and_kegs_without_extra_counts(
        self,
    ):
        plan = create_plan(self.data, 'regular')
        plan['objects'] = [
            {
                'id': 'greenhouse',
                'type': 'greenhouse-repaired',
                'x': 20,
                'y': 24,
            }
        ]
        original = copy.deepcopy(plan)
        with tempfile.TemporaryDirectory() as temporary:
            directory = Path(temporary)
            result = render_plan(
                self.data, plan, directory / 'empty.png', scale=1
            )
            x0, y0 = result['map_region_px'][:2]
            with Image.open(directory / 'empty.png') as empty_image:
                base = empty_image.copy()
            front = self.data.items['greenhouse-repaired'][
                'building_placement'
            ]['front_decoration']
            with Image.open(self.data.root / front['image']) as art:
                self.assertEqual(
                    base.crop(
                        (
                            x0 + 22 * 16,
                            y0 + 30 * 16,
                            x0 + 25 * 16,
                            y0 + 32 * 16,
                        )
                    ).tobytes(),
                    art.convert('RGB').tobytes(),
                )
            self.assertEqual(plan, original)
            self.assertEqual(
                [
                    (row['type'], row['count'])
                    for row in inventory(self.data, plan)['items']
                ],
                [('greenhouse-repaired', 1)],
            )
            floor = apply_operations(
                self.data,
                plan,
                [
                    {
                        'op': 'fill',
                        'type': 'stone-floor',
                        'rect': [22, 30, 3, 2],
                    }
                ],
            )
            render_plan(self.data, floor, directory / 'floor.png', scale=1)
            floor_without_art = copy.deepcopy(floor)
            floor_without_art['objects'] = [
                item for item in floor['objects'] if item['id'] != 'greenhouse'
            ]
            render_plan(
                self.data,
                floor_without_art,
                directory / 'floor-only.png',
                scale=1,
            )
            region = (x0 + 22 * 16, y0 + 30 * 16, x0 + 25 * 16, y0 + 32 * 16)
            with (
                Image.open(directory / 'floor.png') as covered,
                Image.open(directory / 'floor-only.png') as plain,
            ):
                self.assertEqual(
                    covered.crop(region).tobytes(),
                    plain.crop(region).tobytes(),
                )
            occupied = apply_operations(
                self.data,
                floor,
                [
                    {
                        'op': 'add',
                        'item': {'id': 'keg', 'type': 'keg', 'x': 22, 'y': 30},
                    }
                ],
            )
            render_plan(self.data, occupied, directory / 'keg.png', scale=1)
            keg = sprite(self.data, 'keg')
            with Image.open(directory / 'keg.png') as image:
                for y in range(keg.height):
                    for x in range(keg.width):
                        color = keg.getpixel((x, y))
                        if color[3] == 255:
                            self.assertEqual(
                                image.getpixel(
                                    (
                                        x0 + 22 * 16 + x,
                                        y0 + 31 * 16 - keg.height + y,
                                    )
                                ),
                                color[:3],
                            )
            self.assertEqual(normalize_plan(self.data, occupied), occupied)

    def test_source_refresh_preserves_convention_and_reference_assets(self):
        catalog = copy.deepcopy(self.data.catalog)
        root = self.data.root
        overrides = json.loads(
            (root / 'data/catalog-overrides.json').read_text()
        )
        supplemental = json.loads(
            (root / 'data/recognition/supplemental.json').read_text()
        )
        snapshot = json.loads(
            (root / 'data/recognition/source-index.json').read_text()
        )
        for kind in ['greenhouse', 'greenhouse-repaired']:
            catalog['items'][kind]['building_placement'] = copy.deepcopy(
                snapshot['entries']['Buildings/Greenhouse'][
                    'building_placement'
                ]
            )
        apply_placement_overrides(catalog, overrides)
        for kind in ['greenhouse', 'greenhouse-repaired']:
            self.assertEqual(
                placement_areas(catalog['items'][kind])['additional_areas'], []
            )
        references = referenced_image_paths(
            root, catalog, supplemental, overrides
        )
        for filename in [
            'greenhouse-front-decoration.png',
            'greenhouse-complete-reference.png',
            'greenhouse-flooring-reference.png',
            'shipping-bin-open.png',
        ]:
            self.assertIn(root / 'data/planner/images' / filename, references)


if __name__ == '__main__':
    unittest.main()
