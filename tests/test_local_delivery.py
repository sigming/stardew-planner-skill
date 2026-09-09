# /// script
# requires-python = ">=3.12"
# dependencies = ["pillow==12.3.0"]
# ///

import contextlib
import hashlib
import io
import json
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

from layout_core import Dataset, LayoutError, create_plan, normalize_plan
from layout_delivery import DELIVERY_FORMAT, deliver
from layout_import import import_planner


class LocalDeliveryTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.data = Dataset()

    def test_local_delivery_exposes_editable_plan_and_two_images(self):
        plan = create_plan(self.data, 'beach', season='fall')
        with tempfile.TemporaryDirectory() as temporary:
            directory = Path(temporary) / 'layout'
            result = deliver(
                self.data, plan, directory, scale=1, workspace=temporary
            )
            self.assertEqual(
                {path.name for path in directory.iterdir()},
                {'plan.json', 'preview.png', 'implementation.png', 'working'},
            )
            self.assertEqual(
                result['plan_path'], str((directory / 'plan.json').resolve())
            )
            self.assertEqual(result['manifest']['format'], DELIVERY_FORMAT)
            self.assertEqual(result['manifest']['plan_path'], 'plan.json')
            self.assertNotIn('website_import', result['manifest'])
            saved = json.loads((directory / 'plan.json').read_text())
            self.assertEqual(saved, normalize_plan(self.data, plan))
            self.assertEqual(saved['season'], 'fall')
            self.assertEqual(saved['layout'], 'beach')
            self.assertEqual(
                result['manifest']['plan_sha256'],
                hashlib.sha256(
                    (directory / 'plan.json').read_bytes()
                ).hexdigest(),
            )
            self.assertFalse((directory / 'working/plan.json').exists())
            self.assertFalse(list(directory.rglob('*.xml')))
            self.assertFalse(list(directory.rglob('*.zip')))
            self.assertFalse(list(directory.rglob('*.planner.json')))
            for name in ['preview.png', 'implementation.png']:
                with Image.open(directory / name) as image:
                    image.verify()
            for record in result['manifest']['files']:
                self.assertEqual(
                    hashlib.sha256(
                        (directory / record['path']).read_bytes()
                    ).hexdigest(),
                    record['sha256'],
                )

    def test_force_migrates_v3_delivery_and_preserves_backup(self):
        with tempfile.TemporaryDirectory() as temporary:
            directory = Path(temporary) / 'layout'
            deliver(
                self.data,
                create_plan(self.data, 'regular'),
                directory,
                scale=1,
                workspace=temporary,
            )
            manifest_path = directory / 'working/manifest.json'
            legacy = json.loads(manifest_path.read_text())
            legacy['format'] = 'stardew-layout-delivery-v3'
            manifest_path.write_text(json.dumps(legacy))
            (directory / 'plan.json').rename(directory / 'working/plan.json')
            (directory / 'layout.zip').write_bytes(b'legacy import archive')
            before = {
                path.relative_to(directory): path.read_bytes()
                for path in directory.rglob('*')
                if path.is_file()
            }
            result = deliver(
                self.data,
                create_plan(self.data, 'beach', season='winter'),
                directory,
                scale=1,
                force=True,
                workspace=temporary,
            )
            backup = Path(result['backup'])
            self.assertEqual(
                {
                    path.relative_to(backup): path.read_bytes()
                    for path in backup.rglob('*')
                    if path.is_file()
                },
                before,
            )
            self.assertTrue((directory / 'plan.json').is_file())
            self.assertFalse((directory / 'working/plan.json').exists())
            self.assertFalse((directory / 'layout.zip').exists())

    def test_failed_render_keeps_existing_delivery_unchanged(self):
        with tempfile.TemporaryDirectory() as temporary:
            directory = Path(temporary) / 'layout'
            plan = create_plan(self.data, 'regular')
            deliver(self.data, plan, directory, scale=1, workspace=temporary)
            before = {
                path.relative_to(directory): path.read_bytes()
                for path in directory.rglob('*')
                if path.is_file()
            }
            with (
                patch(
                    'layout_delivery.render_plan',
                    side_effect=OSError('simulated render failure'),
                ),
                self.assertRaisesRegex(OSError, 'simulated render failure'),
            ):
                deliver(
                    self.data,
                    plan,
                    directory,
                    scale=1,
                    force=True,
                    workspace=temporary,
                )
            self.assertEqual(
                {
                    path.relative_to(directory): path.read_bytes()
                    for path in directory.rglob('*')
                    if path.is_file()
                },
                before,
            )
            self.assertEqual(list(Path(temporary).iterdir()), [directory])

    def test_delivery_requires_final_overall_review(self):
        report = {
            'status': 'incomplete',
            'overall_status': 'pending',
            'counts': {'pending': 0, 'needs_change': 0},
        }
        with (
            tempfile.TemporaryDirectory() as temporary,
            patch(
                'layout_delivery.validate_patch_review', return_value=report
            ),
            patch('layout_delivery.render_plan') as render,
        ):
            with self.assertRaisesRegex(LayoutError, 'pending'):
                deliver(
                    self.data,
                    create_plan(self.data, 'regular'),
                    Path(temporary) / 'layout',
                    workspace=temporary,
                    review_directory=Path(temporary) / 'review',
                )
            render.assert_not_called()
            self.assertFalse(list(Path(temporary).iterdir()))

    def test_legacy_local_import_keeps_object_positions_and_layers(self):
        payload = {
            'options': {'layout': 'regular', 'season': 'summer'},
            'tiles': [
                {'type': 'oak-tree', 'x': 19 * 16, 'y': 24 * 16},
                {'type': 'chest', 'x': 21 * 16, 'y': 25 * 16},
            ],
            'flooring': [
                {'type': 'cobblestone', 'x': 21 * 16, 'y': 25 * 16},
            ],
        }
        restored = import_planner(self.data, payload)
        positions = {
            item['type']: (item['x'], item['y'])
            for item in restored['objects']
        }
        self.assertEqual(restored['season'], 'summer')
        self.assertEqual(positions['oak-tree'], (20, 24))
        self.assertEqual(positions['chest'], (21, 25))
        self.assertEqual(positions['cobblestone'], (21, 25))
        self.assertIn('house', positions)
        self.assertIn('greenhouse-repaired', positions)

    def test_cli_has_local_commands_and_review_merge(self):
        from layout import main, parser

        arguments = parser()
        self.assertEqual(
            arguments.parse_args(['merge-review', 'review']).command,
            'merge-review',
        )
        for command in ['publish', 'export']:
            with (
                contextlib.redirect_stderr(io.StringIO()),
                self.assertRaises(SystemExit),
            ):
                arguments.parse_args([command, 'plan.json'])
        stream = io.StringIO()
        with (
            patch('sys.argv', ['layout.py', 'maps']),
            contextlib.redirect_stdout(stream),
        ):
            main()
        maps = json.loads(stream.getvalue())
        self.assertEqual(len(maps), len(self.data.maps))
        for entry in maps:
            self.assertEqual(
                set(entry),
                {
                    'id',
                    'name_zh',
                    'name_en',
                    'size_tiles',
                    'category',
                    'limitations',
                },
            )


if __name__ == '__main__':
    unittest.main()
