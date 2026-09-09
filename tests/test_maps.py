# /// script
# requires-python = ">=3.12"
# dependencies = ["pillow==12.3.0"]
# ///

# ruff: noqa: E402

import base64
import hashlib
import json
import shutil
import struct
import subprocess
import sys
import tempfile
import unittest
import xml.etree.ElementTree as ET
import zlib
from pathlib import Path

from PIL import Image

ROOT = Path(__file__).resolve().parents[1] / 'skills/stardew-planner-skill'
sys.path.insert(0, str(ROOT / 'scripts'))
sys.path.insert(
    0, str(Path(__file__).resolve().parents[1] / 'scripts/maintenance')
)

from layout_core import (
    Dataset,
    LayoutError,
    apply_operations,
    create_plan,
    generate_plan,
    validate_plan,
)
from map_rules import item_scope_error, placement_layer
from map_tmx import TmxMap, decode_layer
from sync_maps import FARMS, INTERIORS, Sources


class MapSourceTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.source_root = ROOT / 'data/maps/sources'
        cls.sources = Sources(cls.source_root, offline=True)

    def test_all_cached_sources_match_manifest(self):
        self.assertTrue(self.sources.records)
        for path, record in self.sources.records.items():
            with self.subTest(source=path):
                blob, verified = self.sources.get(path, record['url'])
                self.assertEqual(verified['path'], path)
                self.assertEqual(len(blob), verified['bytes'])

    @unittest.skipUnless(shutil.which('git'), 'Git is required')
    def test_tmx_sources_preserve_hashes_through_git_checkout(self):
        tmx_sources = {
            path: record
            for path, record in self.sources.records.items()
            if path.endswith('.tmx')
        }
        self.assertTrue(tmx_sources)

        def git(checkout, autocrlf, *args):
            return subprocess.run(
                [
                    'git',
                    '-c',
                    f'core.autocrlf={autocrlf}',
                    '-c',
                    'core.safecrlf=false',
                    *args,
                ],
                cwd=checkout,
                check=True,
                capture_output=True,
            )

        for autocrlf in ['false', 'true', 'input']:
            with (
                self.subTest(autocrlf=autocrlf),
                tempfile.TemporaryDirectory() as temporary,
            ):
                checkout = Path(temporary)
                shutil.copyfile(
                    ROOT.parents[1] / '.gitattributes',
                    checkout / '.gitattributes',
                )
                source_path = 'skills/stardew-planner-skill/data/maps/sources'
                source_root = checkout / source_path
                for path in tmx_sources:
                    destination = source_root / path
                    destination.parent.mkdir(parents=True, exist_ok=True)
                    shutil.copyfile(self.source_root / path, destination)
                git(checkout, autocrlf, 'init', '--quiet')
                git(
                    checkout,
                    autocrlf,
                    'add',
                    '.gitattributes',
                    source_path,
                )
                for path in tmx_sources:
                    (source_root / path).unlink()
                git(checkout, autocrlf, 'checkout-index', '--all')
                for path, record in tmx_sources.items():
                    with self.subTest(source=path):
                        blob = (source_root / path).read_bytes()
                        self.assertEqual(
                            hashlib.sha256(blob).hexdigest(),
                            record['sha256'],
                        )
                        self.assertEqual(len(blob), record['bytes'])


class NativeMapTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.catalog = json.loads((ROOT / 'data/maps/maps.json').read_text())
        cls.allowlist = json.loads(
            (ROOT / 'data/maps/allowlist.json').read_text()
        )
        cls.maps = cls.catalog['maps']

    def test_runtime_places_wall_and_floor_furniture_and_protects_fixed_objects(
        self,
    ):
        data = Dataset(ROOT)
        recipe = {
            'schema_version': 1,
            'modules': [
                {
                    'id': 'painting',
                    'clearance': 0,
                    'objects': [{'type': 'my-first-painting', 'x': 0, 'y': 0}],
                },
                {
                    'id': 'chair',
                    'clearance': 0,
                    'objects': [{'type': 'oak-chair', 'x': 0, 'y': 0}],
                },
            ],
        }
        plan = generate_plan(data, 'farmhouse', recipe)
        self.assertTrue(validate_plan(data, plan)['passed'])
        for item in plan['objects']:
            if item['type'] == 'my-first-painting':
                self.assertFalse(
                    data.cells(item) & data.blocked('farmhouse')['wall']
                )
            elif item['type'] == 'oak-chair':
                self.assertFalse(
                    data.cells(item) & data.blocked('farmhouse')['furniture']
                )
        coop = create_plan(data, 'coop-big')
        fixed = next(
            item for item in coop['objects'] if 'fixed' in item.get('tags', [])
        )
        for operation in [
            {'op': 'remove', 'id': fixed['id']},
            {'op': 'update', 'id': fixed['id'], 'set': {'x': fixed['x'] + 1}},
        ]:
            with self.assertRaises(LayoutError):
                apply_operations(data, coop, [operation])
        for map_id in ['farmhouse', 'coop', 'barn', 'ginger_island']:
            wrong = create_plan(data, map_id)
            wrong['objects'].append(
                {'id': 'building', 'type': 'coop', 'x': 10, 'y': 10}
            )
            self.assertIn(
                'farm_building_outside_farm',
                {
                    error['code']
                    for error in validate_plan(data, wrong)['errors']
                },
            )

    def test_crab_pots_use_shore_water_instead_of_dry_land(self):
        data = Dataset(ROOT)
        plan = create_plan(data, 'fishing')
        water = set(
            map(
                tuple,
                json.loads(
                    (ROOT / 'data/maps/fishing/restrictions.json').read_text()
                )['water_tiles'],
            )
        )
        shoreline = water - data.blocked('fishing')['shore_water']
        self.assertTrue(shoreline)
        x, y = sorted(shoreline)[0]
        placed = apply_operations(
            data,
            plan,
            [{'op': 'add', 'item': {'type': 'crab-pot', 'x': x, 'y': y}}],
        )
        self.assertTrue(validate_plan(data, placed)['passed'])
        with self.assertRaises(LayoutError):
            apply_operations(
                data,
                plan,
                [{'op': 'add', 'item': {'type': 'chest', 'x': x, 'y': y}}],
            )

    def test_only_explicit_vanilla_maps_are_included(self):
        self.assertEqual(set(self.maps), set(FARMS) | set(INTERIORS))
        self.assertEqual(set(self.allowlist['map_ids']), set(self.maps))
        self.assertEqual(len(self.maps), 26)
        for map_id, entry in self.maps.items():
            with self.subTest(map=map_id):
                self.assertTrue(entry['vanilla'])
                self.assertNotIn('mods/', entry['source_map']['url'])
                self.assertIn(entry['category'], self.allowlist['categories'])
        for unsupported in ['quarry', 'bus-stop', 'IF2R', 'grandpas-farm']:
            self.assertNotIn(unsupported, self.maps)

    def test_native_dimensions_sources_and_backgrounds(self):
        for map_id, entry in self.maps.items():
            with self.subTest(map=map_id):
                source = entry['source_map']
                blob = (
                    ROOT / 'data/maps/sources' / source['path']
                ).read_bytes()
                self.assertEqual(
                    hashlib.sha256(blob).hexdigest(), source['sha256']
                )
                native = TmxMap(blob)
                self.assertEqual(
                    (native.width, native.height),
                    (entry['width_tiles'], entry['height_tiles']),
                )
                for background in entry['backgrounds'].values():
                    path = ROOT / 'data/maps' / background['path']
                    with Image.open(path) as image:
                        self.assertEqual(
                            image.size, (native.width * 16, native.height * 16)
                        )
                    if background.get('derived'):
                        self.assertEqual(
                            hashlib.sha256(path.read_bytes()).hexdigest(),
                            background['sha256'],
                        )

    def test_terrain_coordinates_are_bounded_and_wall_floor_disjoint(self):
        for map_id, entry in self.maps.items():
            with self.subTest(map=map_id):
                restrictions = json.loads(
                    (
                        ROOT / 'data/maps' / entry['restrictions_path']
                    ).read_text()
                )
                full = {
                    (x, y)
                    for y in range(entry['height_tiles'])
                    for x in range(entry['width_tiles'])
                }
                blocked = {
                    layer: set(map(tuple, values))
                    for layer, values in restrictions['blocked'].items()
                }
                for values in blocked.values():
                    self.assertTrue(values <= full)
                walls = set(map(tuple, restrictions['wall_tiles']))
                self.assertEqual(blocked['wall'], full - walls)
                self.assertTrue(walls <= blocked['furniture'])
                self.assertTrue(
                    set(map(tuple, restrictions['water_tiles']))
                    <= blocked['accessible']
                )
                for default in entry['default_objects']:
                    self.assertIn((default['x'], default['y']), full)

    def test_water_restriction_repairs_original_farms(self):
        for map_id in ['beach', 'fishing']:
            restrictions = json.loads(
                (ROOT / f'data/maps/{map_id}/restrictions.json').read_text()
            )
            old = set(
                map(tuple, restrictions['baseline_blocked']['accessible'])
            )
            current = set(map(tuple, restrictions['blocked']['accessible']))
            self.assertGreater(len(current - old), 1000)

    def test_greenhouse_has_exactly_120_ground_crop_tiles(self):
        entry = self.maps['greenhouse']
        restrictions = json.loads(
            (ROOT / 'data/maps/greenhouse/restrictions.json').read_text()
        )
        allowed = entry['width_tiles'] * entry['height_tiles'] - len(
            restrictions['blocked']['tillable']
        )
        self.assertEqual(allowed, 120)
        self.assertTrue(entry['gameplay_rules']['allow_ground_crops'])
        self.assertFalse(entry['gameplay_rules']['allow_giant_crops'])

    def test_house_tiers_use_actual_native_sizes_and_cellar_patch(self):
        self.assertEqual(self.maps['farmhouse']['width_tiles'], 12)
        self.assertEqual(self.maps['farmhouse_upgraded_1']['width_tiles'], 30)
        self.assertEqual(self.maps['farmhouse_upgraded_2']['width_tiles'], 70)
        before = self.maps['farmhouse_upgraded_2']['backgrounds']['spring']
        after = self.maps['farmhouse_upgraded_3']['backgrounds']['spring']
        self.assertNotEqual(before['sha256'], after['sha256'])
        self.assertEqual(
            after['patches'][0]['source']['path'], 'tmx/FarmHouse_Cellar.tmx'
        )

    def test_island_restoration_patches_and_constant_season(self):
        entry = self.maps['ginger_island']
        background = entry['backgrounds']['summer']
        self.assertEqual(len(background['patches']), 4)
        self.assertEqual(
            {value['path'] for value in entry['backgrounds'].values()},
            {'ginger_island/spring.png'},
        )
        self.assertFalse(entry['seasonal'])

    def test_map_scope_and_wall_placement_rules(self):
        greenhouse = self.maps['greenhouse']
        island = self.maps['ginger_island']
        house = self.maps['farmhouse']
        crop = {'category': 'crop', 'subcategory': None}
        giant = {'category': 'crop', 'subcategory': 'giant-crops'}
        building = {'category': 'building'}
        painting = {
            'category': 'furniture',
            'furniture_type': 'painting',
            'placement': {'restriction_layer': None},
        }
        chair = {
            'category': 'furniture',
            'furniture_type': 'chair',
            'placement': {'restriction_layer': None},
        }
        self.assertIsNone(item_scope_error(greenhouse, crop))
        self.assertIsNone(item_scope_error(island, crop))
        self.assertEqual(
            item_scope_error(house, crop), 'ground_crop_not_supported_on_map'
        )
        self.assertEqual(
            item_scope_error(island, giant), 'giant_crop_not_supported_on_map'
        )
        self.assertEqual(
            item_scope_error(island, building), 'farm_building_outside_farm'
        )
        self.assertEqual(placement_layer(house, painting), 'wall')
        self.assertEqual(placement_layer(house, chair), 'furniture')

    def test_tmx_csv_and_compressed_layer_decoding(self):
        numbers = [0, 1, 2147483650, 35]
        encoded = base64.b64encode(
            zlib.compress(struct.pack('<4I', *numbers))
        ).decode()
        layer = ET.fromstring(
            f'<data encoding="base64" compression="zlib">{encoded}</data>'
        )
        self.assertEqual(decode_layer(layer), numbers)
        csv = ET.fromstring('<data encoding="csv">0,1,2147483650,35</data>')
        self.assertEqual(decode_layer(csv), numbers)


if __name__ == '__main__':
    unittest.main()
