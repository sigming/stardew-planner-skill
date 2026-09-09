# /// script
# requires-python = ">=3.12"
# dependencies = ["pillow==12.3.0"]
# ///

# ruff: noqa: E402

import hashlib
import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

from PIL import Image

ROOT = Path(__file__).resolve().parents[1] / 'skills/stardew-planner-skill'
sys.path.insert(0, str(ROOT / 'scripts'))
sys.path.insert(
    0, str(Path(__file__).resolve().parents[1] / 'scripts/maintenance')
)
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'scripts'))

from audit_catalog import SOURCE_PATH, audit
from audit_crafting import audit as audit_crafting
from build_recognition import REFERENCE_ICON_SIZE, fit_reference_sprite
from layout_assets import appearances, seasonal_id, sprite
from layout_core import (
    Dataset,
    LayoutError,
    apply_operations,
    create_plan,
    generate_plan,
    inventory,
    normalize_plan,
)
from layout_coverage import coverage
from layout_import import import_planner
from layout_render import render_plan
from recognition_categories import atlas_items, load_supplemental


class CatalogVariantTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.data = Dataset(ROOT)

    def test_cabin_styles_and_upgrades_preserve_appearance(self):
        styles = [
            'stone',
            'plank',
            'log',
            'neighbor',
            'rustic',
            'beach',
            'trailer',
        ]
        seen = set()
        atlas = Image.open(ROOT / 'data/planner/atlas.png').convert('RGBA')
        for style in styles:
            base = f'{style}-cabin'
            entries = appearances(self.data, base)
            self.assertEqual(len(entries), 3)
            for entry in entries:
                kind = entry['id']
                with self.subTest(kind=kind):
                    self.assertEqual(self.data.item_id(kind), kind)
                    self.assertEqual(self.data.dimensions(kind), (5, 3))
                    self.assertEqual(
                        sprite(self.data, kind, atlas).tobytes(),
                        sprite(self.data, kind).tobytes(),
                    )
                    seen.add(
                        hashlib.sha256(
                            sprite(self.data, kind).tobytes()
                        ).hexdigest()
                    )
        self.assertEqual(len(seen), 21)

    def test_omitted_references_share_one_sheet_and_cannot_be_placed(self):
        entities = load_supplemental(ROOT)['reference_entities']
        index = json.loads((ROOT / 'data/recognition/index.json').read_text())
        self.assertEqual(len(entities), 34)
        self.assertTrue(
            {
                'moving-slime',
                'truffle',
                'torch',
                'mailbox',
                'toddler',
                'island-parrot-perch',
                'wild-butterfly',
                'wild-owl',
                'wild-crow',
                'wild-seagull',
                'animal-chicken',
                'animal-duck',
                'animal-dinosaur',
                'animal-rabbit',
                'animal-cow',
                'animal-ostrich',
                'animal-goat',
                'animal-sheep',
                'animal-pig',
                'fairy-companion-1',
                'frog-companion-2',
            }
            <= entities.keys()
        )
        self.assertEqual(len(index['groups']['ignored_entities']['pages']), 1)
        self.assertEqual(
            len({index['items'][kind]['sheet'] for kind in entities}), 1
        )
        for kind in entities:
            with self.subTest(kind=kind):
                self.assertNotIn(kind, self.data.items)
                self.assertIsNone(index['items'][kind]['footprint_tiles'])
                self.assertEqual(
                    index['items'][kind]['recognition_action'], 'omit'
                )
                for appearance in index['items'][kind]['appearances']:
                    left, top, right, bottom = appearance['visible_bounds_px']
                    x1, y1, x2, y2 = appearance['sprite_bounds_px']
                    if entities[kind].get('reference_composite'):
                        self.assertTrue(x1 <= left < right <= x2)
                        self.assertTrue(y1 <= top < bottom <= y2)
                        width, height = entities[kind]['sprites']['default'][
                            'size_px'
                        ]
                        self.assertAlmostEqual(
                            (right - left) / (bottom - top),
                            width / height,
                            delta=0.05,
                        )
                        self.assertGreater(right - left, REFERENCE_ICON_SIZE)
                    else:
                        self.assertEqual(
                            max(right - left, bottom - top),
                            min(REFERENCE_ICON_SIZE, x2 - x1, y2 - y1),
                        )
                with self.assertRaisesRegex(
                    LayoutError, 'Unknown or ambiguous item'
                ):
                    apply_operations(
                        self.data,
                        create_plan(self.data, 'regular'),
                        [
                            {
                                'op': 'add',
                                'item': {'type': kind, 'x': 30, 'y': 30},
                            },
                        ],
                    )
        # Static animal-shaped furniture retains its normal placement behavior.
        plan = apply_operations(
            self.data,
            create_plan(self.data, 'regular'),
            [
                {
                    'op': 'add',
                    'item': {
                        'id': 'cat-tree',
                        'type': 'cat-tree',
                        'x': 30,
                        'y': 30,
                    },
                },
                {
                    'op': 'add',
                    'item': {'type': 'candle-lamp', 'x': 33, 'y': 30},
                },
                {
                    'op': 'add',
                    'item': {'type': 'solid-gold-lewis', 'x': 36, 'y': 30},
                },
            ],
        )
        self.assertTrue(
            {'cat-tree', 'candle-lamp', 'solid-gold-lewis'}
            <= {item['type'] for item in plan['objects']}
        )

    def test_pet_queries_and_card_extraction_preserve_omission(self):
        command = [sys.executable, str(ROOT / 'scripts/query_catalog.py')]
        pages = json.loads(
            subprocess.check_output(
                command
                + [
                    '--sheets',
                    '--recognition-group',
                    'ignored_entities',
                ],
                text=True,
            )
        )
        self.assertEqual(pages['sheet_count'], 1)
        self.assertEqual(pages['sheets'][0]['matched_card_count'], 34)
        with tempfile.TemporaryDirectory() as temporary:
            output = Path(temporary) / 'cat.png'
            query = json.loads(
                subprocess.check_output(
                    command
                    + [
                        'pet-cat-1',
                        '--image-out',
                        str(output),
                    ],
                    text=True,
                )
            )['items'][0]
            self.assertFalse(query['placement_supported'])
            self.assertEqual(query['recognition_action'], 'omit')
            self.assertEqual(
                query['recognition']['recognition_action'], 'omit'
            )
            self.assertNotIn('planner_type', query)
            with Image.open(output) as image:
                image.verify()

    def test_reference_size_ignores_source_padding_and_upscaling(self):
        small = Image.new('RGBA', (24, 24))
        small.paste('#b05921', (7, 4, 17, 20))
        large = small.resize((96, 96), Image.Resampling.NEAREST)
        first = Image.new('RGBA', (160, 160))
        second = first.copy()
        self.assertEqual(
            fit_reference_sprite(first, small, (0, 0, 160, 160)),
            fit_reference_sprite(second, large, (0, 0, 160, 160)),
        )
        self.assertEqual(first.tobytes(), second.tobytes())

    def test_toddler_composite_is_searchable_and_omitted_from_layouts(self):
        record = load_supplemental(ROOT)['reference_entities']['toddler']
        with Image.open(ROOT / record['sprites']['default']['path']) as image:
            self.assertEqual(image.size, (228, 80))
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / 'toddler-card.png'
            result = json.loads(
                subprocess.check_output(
                    [
                        sys.executable,
                        str(ROOT / 'scripts/query_catalog.py'),
                        '小孩',
                        '--image-out',
                        str(path),
                    ],
                    text=True,
                )
            )
            item = result['items'][0]
            self.assertEqual(item['id'], 'toddler')
            self.assertEqual(item['recognition_action'], 'omit')
            self.assertFalse(item['placement_supported'])
            self.assertEqual(
                item['recognition']['sheet'],
                'assets/recognition/10-ignored_entities-01.png',
            )
            with Image.open(path) as image:
                image.verify()
        for name in [
            'toddler',
            '幼儿',
            '小孩',
            '孩子',
            '儿童',
            'child',
            'children',
        ]:
            with self.subTest(name=name):
                plan = create_plan(self.data, 'regular')
                plan['objects'].append(
                    {'id': 'child', 'type': name, 'x': 20, 'y': 24}
                )
                normalized = normalize_plan(self.data, plan)
                self.assertEqual(
                    normalized['omitted_entities']['counts'], {'toddler': 1}
                )
                self.assertEqual(
                    len(normalized['objects']), len(plan['objects']) - 1
                )
        imported = import_planner(
            self.data, {'tiles': [{'type': 'toddler', 'x': 320, 'y': 384}]}
        )
        self.assertEqual(
            imported['omitted_entities']['counts'], {'toddler': 1}
        )

    def test_fence_lights_require_specific_identities_before_omission(self):
        plan = create_plan(self.data, 'regular')
        fence = {'id': 'fence', 'type': 'wood-fence', 'x': 20, 'y': 25}
        for ambiguous in ['火炬', '蜡烛', 'candle']:
            with self.subTest(ambiguous=ambiguous):
                self.assertIsNone(self.data.omitted_entity_id(ambiguous))
                plan['objects'] = [
                    fence,
                    {
                        'id': 'light',
                        'type': ambiguous,
                        'x': 20,
                        'y': 25,
                    },
                ]
                with self.assertRaisesRegex(
                    LayoutError, 'Unknown or ambiguous item'
                ):
                    normalize_plan(self.data, plan)
        for identified in ['torch', '火把', '小型火把']:
            with self.subTest(identified=identified):
                plan['objects'] = [
                    fence,
                    {
                        'id': 'light',
                        'type': identified,
                        'x': 20,
                        'y': 25,
                    },
                ]
                normalized = normalize_plan(self.data, plan)
                self.assertEqual(normalized['objects'], [fence])
                self.assertEqual(
                    normalized['omitted_entities']['counts'], {'torch': 1}
                )
        retained = ['candle-lamp', 'wood-lamp-post', 'iron-lamp-post'] + [
            kind
            for kind, item in self.data.items.items()
            if kind.endswith('-brazier') and not item['hidden_in_menu']
        ]
        for kind in retained:
            with self.subTest(retained=kind):
                name = self.data.items[kind]['name_zh']
                self.assertIsNone(self.data.omitted_entity_id(name))
                plan['objects'] = [
                    fence,
                    {
                        'id': 'light',
                        'type': name
                        if kind
                        in [
                            'candle-lamp',
                            'wood-lamp-post',
                            'iron-lamp-post',
                            'wooden-brazier',
                        ]
                        else kind,
                        'x': 20,
                        'y': 24,
                    },
                ]
                normalized = normalize_plan(self.data, plan)
                self.assertEqual(
                    {
                        (item['type'], item['x'], item['y'])
                        for item in normalized['objects']
                    },
                    {('wood-fence', 20, 25), (kind, 20, 24)},
                )
                self.assertNotIn('omitted_entities', normalized)

    def test_imports_and_existing_plans_omit_truffles_and_animals(self):
        payload = {
            'tiles': [
                {'type': 'truffle', 'x': 20 * 16, 'y': 30 * 16},
                {'type': 'horse', 'x': 20 * 16, 'y': 30 * 16},
                {'type': 'torch', 'x': 20 * 16, 'y': 30 * 16},
                {'type': 'chest', 'x': 20 * 16, 'y': 30 * 16},
            ]
        }
        imported = import_planner(self.data, payload)
        self.assertEqual(
            imported['omitted_entities']['counts'],
            {'horse': 1, 'truffle': 1, 'torch': 1},
        )
        self.assertEqual(
            sum(
                row['count'] for row in inventory(self.data, imported)['items']
            ),
            len(imported['objects']),
        )
        self.assertNotIn(
            'truffle', {item['type'] for item in imported['objects']}
        )
        legacy = create_plan(self.data, 'regular')
        legacy['objects'].append({'type': '松露', 'x': 20, 'y': 30})
        normalized = normalize_plan(self.data, legacy)
        self.assertEqual(
            normalized['omitted_entities']['counts'], {'truffle': 1}
        )
        self.assertEqual(normalize_plan(self.data, normalized), normalized)
        recipe = {
            'schema_version': 1,
            'modules': [
                {
                    'id': 'omitted-only',
                    'objects': [{'type': 'truffle', 'x': 0, 'y': 0}],
                },
            ],
        }
        generated = generate_plan(self.data, 'regular', recipe)
        self.assertEqual(
            generated['omitted_entities']['counts'], {'truffle': 1}
        )
        self.assertNotIn(
            'truffle', {item['type'] for item in generated['objects']}
        )

    def test_rarecrows_are_placeable_and_supply_scarecrow_coverage(self):
        rows = [
            {
                'op': 'add',
                'item': {
                    'id': f'rare-{n}',
                    'type': f'rarecrow-{n}',
                    'x': 15 + n * 2,
                    'y': 24,
                },
            }
            for n in range(1, 9)
        ]
        rows.append(
            {
                'op': 'add',
                'item': {
                    'id': 'crop',
                    'type': 'parsnip-seeds',
                    'x': 20,
                    'y': 25,
                },
            }
        )
        plan = apply_operations(
            self.data, create_plan(self.data, 'regular'), rows
        )
        result = coverage(self.data, plan, 'scarecrow')
        self.assertEqual(result['source_count'], 8)
        self.assertTrue(result['fully_covered'])
        self.assertEqual(
            len(
                {
                    sprite(self.data, f'rarecrow-{n}').tobytes()
                    for n in range(1, 9)
                }
            ),
            8,
        )
        with tempfile.TemporaryDirectory() as directory:
            for mode in ['render', 'blueprint']:
                path = Path(directory) / f'{mode}.png'
                render_plan(self.data, plan, path, mode=mode, scale=1)
                self.assertGreater(path.stat().st_size, 1000)
        types = {row['type'] for row in inventory(self.data, plan)['items']}
        self.assertLessEqual({f'rarecrow-{n}' for n in range(1, 9)}, types)

    def test_seasonal_plants_and_verified_furniture_use_local_images(self):
        for number in range(1, 7):
            base = f'seasonal-plant-{number}'
            self.assertEqual(len(appearances(self.data, base)), 4)
            for season in ['spring', 'summer', 'fall', 'winter']:
                image = sprite(self.data, seasonal_id(self.data, base, season))
                self.assertEqual(image.size, (16, 32))
        for kind in ['joja-bed', 'wizard-bed', 'junimo-bed', 'retro-bed']:
            self.assertEqual(self.data.dimensions(kind), (3, 3))
            self.assertEqual(sprite(self.data, kind).size, (48, 64))
        for kind in [
            'krobus-portrait',
            'hanging-fish',
            *[f'decorative-door-{n}' for n in range(1, 7)],
        ]:
            self.assertEqual(self.data.item_id(kind), kind)
            self.assertIsNotNone(sprite(self.data, kind).getbbox())

    def test_all_crop_ids_share_two_cards_without_changing_footprints(self):
        cards = atlas_items(self.data.catalog)['crops']
        self.assertEqual(
            {item['id'] for item in cards},
            {'strawberry-seeds', 'giant-pumpkin'},
        )
        for kind, metadata in self.data.items.items():
            if metadata['category'] != 'crop':
                continue
            canonical = self.data.item_id(kind)
            expected = (
                'giant-pumpkin'
                if metadata['subcategory'] == 'giant-crops'
                else 'strawberry-seeds'
            )
            self.assertEqual(canonical, expected)
            self.assertEqual(
                self.data.dimensions(kind), self.data.dimensions(canonical)
            )

    def test_source_audit_covers_same_name_styles_and_missing_items(self):
        source = json.loads((ROOT / SOURCE_PATH).read_text())
        report = audit(ROOT, source)
        self.assertEqual(report['unresolved_ids'], [])
        self.assertEqual(report['checked_entry_count'], len(source['entries']))
        for base, expected in [
            ('house-plant', 20),
            ('floor-divider-l', 8),
            ('floor-divider-r', 8),
            ('wall-sconce', 7),
            ('small-junimo-plush', 4),
            ('ceiling-leaves', 5),
            ('fancy-house-plant', 3),
        ]:
            entries = appearances(self.data, base)
            self.assertEqual(len(entries), expected, base)
            self.assertEqual(
                len(
                    {sprite(self.data, row['id']).tobytes() for row in entries}
                ),
                expected,
                base,
            )
        for kind in [
            'drum-block',
            'flute-block',
            'jack-o-lantern',
            'hmtgf',
            'pinky-lemon',
            'foroguemon',
            'mannequin-male',
            'mannequin-female',
            'mannequin-cursed-male',
            'mannequin-cursed-female',
        ]:
            self.assertEqual(self.data.item_id(kind), kind)
            self.assertEqual(self.data.dimensions(kind), (1, 1))

    def test_log_comparison_cards_keep_distinct_identities_and_ready_state(
        self,
    ):
        index = json.loads((ROOT / 'data/recognition/index.json').read_text())
        mushroom, log = (
            index['items']['mushroom-log'],
            index['items']['log-section'],
        )
        self.assertEqual(
            {entry['id'] for entry in mushroom['appearances']},
            {'mushroom-log', 'mushroom-log-ready'},
        )
        self.assertEqual(
            [entry['id'] for entry in mushroom['similar_items']],
            ['log-section'],
        )
        self.assertEqual(
            [entry['id'] for entry in log['similar_items']], ['mushroom-log']
        )
        self.assertEqual(index['lookup']['log-section'], 'log-section')
        self.assertEqual(index['lookup']['mushroom-log-ready'], 'mushroom-log')
        self.assertEqual(self.data.dimensions('mushroom-log-ready'), (1, 1))
        self.assertEqual(
            sprite(self.data, 'mushroom-log-ready').size, (16, 32)
        )
        self.assertNotEqual(
            sprite(self.data, 'mushroom-log').tobytes(),
            sprite(self.data, 'mushroom-log-ready').tobytes(),
        )
        plan = apply_operations(
            self.data,
            create_plan(self.data, 'regular'),
            [
                {
                    'op': 'add',
                    'item': {'type': 'mushroom-log-ready', 'x': 20, 'y': 30},
                },
                {
                    'op': 'add',
                    'item': {'type': 'log-section', 'x': 21, 'y': 30},
                },
            ],
        )
        self.assertTrue(
            {'mushroom-log-ready', 'log-section'}
            <= {item['type'] for item in plan['objects']}
        )

    def test_furniture_animal_comparisons_preserve_placement_and_omission(
        self,
    ):
        index = json.loads((ROOT / 'data/recognition/index.json').read_text())
        payload = {'tiles': []}
        for furniture, animal in [
            ('lawn-flamingo', 'animal-ostrich'),
            ('plush-bunny', 'animal-rabbit'),
        ]:
            for kind, other, placeable in [
                (furniture, animal, True),
                (animal, furniture, False),
            ]:
                with self.subTest(kind=kind):
                    card = index['items'][kind]
                    self.assertEqual(index['lookup'][kind], kind)
                    self.assertEqual(
                        [entry['id'] for entry in card['appearances']], [kind]
                    )
                    self.assertEqual(
                        [entry['id'] for entry in card['similar_items']],
                        [other],
                    )
                    self.assertEqual(
                        card['similar_items'][0]['placement_supported'],
                        not placeable,
                    )
                    self.assertTrue(
                        card['similar_items'][0]['comparison_only']
                    )
                    row = json.loads(
                        subprocess.check_output(
                            [
                                sys.executable,
                                str(ROOT / 'scripts/query_catalog.py'),
                                kind,
                            ],
                            text=True,
                        )
                    )['items'][0]
                    self.assertEqual(row['placement_supported'], placeable)
                    self.assertEqual(row['similar_items'][0]['id'], other)
                    self.assertEqual(
                        row['similar_items'][0]['placement_supported'],
                        not placeable,
                    )
                    self.assertEqual(
                        row['similar_items'][0]['recognition_action'],
                        'omit' if placeable else 'place',
                    )
            x = 20 if furniture == 'lawn-flamingo' else 22
            payload['tiles'].extend(
                [
                    {'type': furniture, 'x': x * 16, 'y': 30 * 16},
                    {'type': animal, 'x': x * 16, 'y': 30 * 16},
                ]
            )
        imported = import_planner(self.data, payload)
        self.assertTrue(
            {'lawn-flamingo', 'plush-bunny'}
            <= {item['type'] for item in imported['objects']}
        )
        self.assertEqual(
            imported['omitted_entities']['counts'],
            {'animal-ostrich': 1, 'animal-rabbit': 1},
        )

    def test_camping_equipment_uses_deployed_size_and_tent_requires_outdoors(
        self,
    ):
        self.assertEqual(self.data.dimensions('cookout-kit'), (1, 1))
        self.assertEqual(self.data.dimensions('tent-kit'), (3, 2))
        plan = apply_operations(
            self.data,
            create_plan(self.data, 'regular'),
            [
                {
                    'op': 'add',
                    'item': {'type': 'cookout-kit', 'x': 20, 'y': 30},
                },
                {'op': 'add', 'item': {'type': 'tent-kit', 'x': 21, 'y': 30}},
            ],
        )
        self.assertTrue(
            {'cookout-kit', 'tent-kit'}
            <= {item['type'] for item in plan['objects']}
        )
        with self.assertRaisesRegex(
            LayoutError, 'outdoor_equipment_requires_outdoors'
        ):
            apply_operations(
                self.data,
                create_plan(self.data, 'shed'),
                [
                    {
                        'op': 'add',
                        'item': {'type': 'tent-kit', 'x': 5, 'y': 5},
                    },
                ],
            )

    def test_requested_crafting_categories_have_no_missing_assets(self):
        snapshot = json.loads(
            (ROOT / 'data/recognition/crafting-source.json').read_text()
        )
        report = audit_crafting(ROOT, snapshot)
        self.assertEqual(report['unresolved_names'], [])
        self.assertEqual(report['checked_item_count'], len(snapshot['items']))
        self.assertEqual(len(report['sections']), 10)
        by_name = {item['name_en']: item for item in report['items']}
        self.assertEqual(by_name['Torch']['status'], 'ignored')
        self.assertEqual(by_name['Tent Kit']['footprint_tiles'], [3, 2])
        self.assertEqual(by_name['Cookout Kit']['footprint_tiles'], [1, 1])
        self.assertEqual(by_name['Gate']['catalog_id'], 'wood-gate')
        for name in ['Iron Bar', 'Gold Bar', 'Explosive Ammo']:
            self.assertEqual(by_name[name]['status'], 'not_placeable')

    def test_native_building_sizes_and_furniture_types_are_distinct(self):
        for kind in ['pet-bowl', 'stone-pet-bowl', 'hay-pet-bowl']:
            self.assertEqual(self.data.dimensions(kind), (2, 2))
            self.assertEqual(sprite(self.data, kind).size, (32, 32))
        for kind in ['food-pet-bowl', 'water-pet-bowl']:
            self.assertEqual(self.data.dimensions(kind), (1, 1))
        for kind in ['greenhouse', 'greenhouse-repaired']:
            self.assertEqual(self.data.dimensions(kind), (7, 6))
            self.assertEqual(sprite(self.data, kind).size, (112, 160))
        self.assertEqual(self.data.items['junimo-hut']['category'], 'building')
        self.assertEqual(
            self.data.items['decorative-junimo-hut']['category'], 'furniture'
        )
        for kind, expected in [
            ('wall-sconce-style-2734', 'sconce'),
            ('jungle-decal-style-2627', 'painting'),
            ('oak-chair', 'chair'),
            ('house-plant-style-1376', 'decor'),
        ]:
            self.assertEqual(self.data.items[kind]['furniture_type'], expected)

    def test_furniture_rotation_footprints_and_nested_style_cards(self):
        for base, expected_size, expected_footprint in [
            ('oak-chair', (16, 32), (1, 1)),
            ('blue-couch', (32, 48), (2, 2)),
            ('bountiful-dining-table', (32, 80), (2, 4)),
            ('red-rug', (32, 48), (2, 3)),
        ]:
            rotated = f'{base}-rotation-1'
            self.assertEqual(self.data.item_id(rotated), rotated)
            self.assertEqual(sprite(self.data, rotated).size, expected_size)
            self.assertEqual(self.data.dimensions(rotated), expected_footprint)
        self.assertEqual(
            sprite(self.data, 'oak-chair-rotation-3').tobytes(),
            sprite(self.data, 'oak-chair-rotation-1')
            .transpose(Image.Transpose.FLIP_LEFT_RIGHT)
            .tobytes(),
        )
        entries = appearances(self.data, 'dining-chair')
        self.assertEqual(len(entries), 8)
        self.assertIn(
            'dining-chair-style-67-rotation-3', {row['id'] for row in entries}
        )
        rotations = [
            item
            for item in self.data.items.values()
            if item.get('source', {}).get('rotation')
        ]
        self.assertEqual(len(rotations), 223)

    def test_compact_queries_and_cactus_component_provenance(self):
        def query(*args):
            output = subprocess.check_output(
                [
                    sys.executable,
                    str(ROOT / 'scripts/query_catalog.py'),
                    *args,
                ],
                text=True,
            )
            return json.loads(output)['items']

        card = query('rarecrow-3')[0]
        self.assertTrue(card['placement_supported'])
        self.assertNotIn('text_regions', card['recognition'])
        self.assertIn(
            'text_regions', query('rarecrow-3', '--full')[0]['recognition']
        )
        self.assertEqual(query('防风草')[0]['id'], 'strawberry-seeds')
        cactus = query('free-cactus')[0]
        self.assertFalse(cactus['placement_supported'])
        data = load_supplemental(ROOT)['components']['free-cactus']
        self.assertEqual(len(data['components']), 64)
        self.assertEqual(
            hashlib.sha256((ROOT / data['sheet']).read_bytes()).hexdigest(),
            data['sheet_sha256'],
        )


if __name__ == '__main__':
    unittest.main()
