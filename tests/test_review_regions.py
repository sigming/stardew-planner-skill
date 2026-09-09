# /// script
# requires-python = ">=3.12"
# dependencies = ["pillow==12.3.0"]
# ///

import copy
import hashlib
import json
import shutil
import sys
import tempfile
import unittest
from pathlib import Path

from PIL import Image

sys.path.insert(
    0,
    str(
        Path(__file__).resolve().parents[1]
        / 'skills/stardew-planner-skill/scripts'
    ),
)

from layout_checkpoint import checkpoint
from layout_core import Dataset, LayoutError, create_plan, write_json_atomic
from layout_delivery import deliver
from layout_review import (
    REVIEW_CHECKS,
    merge_patch_reviews,
    validate_patch_review,
)


class ReviewRegionTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.dataset = Dataset()

    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.stage = Path(self.temporary.name) / 'final'
        checkpoint(
            self.dataset,
            create_plan(self.dataset, 'regular'),
            'final',
            self.stage,
            scale=1,
        )
        self.root = self.stage / 'patches'
        self.index = json.loads((self.root / 'index.json').read_text())

    def complete_region_records(self):
        for region in self.index['patches']:
            path = self.root / region['review_file']
            report = json.loads(path.read_text())
            report['patches'][0]['checks'] = dict.fromkeys(
                REVIEW_CHECKS, 'passed'
            )
            write_json_atomic(path, report)

    def test_default_six_regions_cover_standard_farm_once(self):
        self.assertEqual(len(self.index['patches']), 6)
        self.assertEqual(self.index['block_size_tiles'], [27, 33])
        seen = set()
        total_encoded = 0
        for region in self.index['patches']:
            x, y, width, height = region['tile_rect']
            cells = {
                (col, row)
                for col in range(x, x + width)
                for row in range(y, y + height)
            }
            self.assertFalse(seen & cells)
            seen.update(cells)
            self.assertEqual(set(region['files']), {'comparison'})
            image_info = region['files']['comparison']
            image_path = self.root / image_info['path']
            self.assertEqual(image_info['bytes'], image_path.stat().st_size)
            self.assertEqual(
                image_info['sha256'],
                hashlib.sha256(image_path.read_bytes()).hexdigest(),
            )
            total_encoded += image_info['base64_bytes']
            with Image.open(image_path) as image:
                self.assertLessEqual(max(image.size), 2048)
                for panel in region['panels'].values():
                    self.assertEqual(
                        list(image.crop(panel).size),
                        [value * 16 for value in region['context_rect'][2:]],
                    )
        self.assertEqual(len(seen), 80 * 65)
        self.assertEqual(len(list(self.root.glob('*.png'))), 6)
        self.assertEqual(
            self.index['image_input']['total_base64_bytes'], total_encoded
        )
        self.assertLess(total_encoded, 8 * 1024 * 1024)

    def test_merge_preserves_pending_and_requires_main_overall_check(self):
        pending = merge_patch_reviews(self.stage, self.dataset)
        self.assertEqual(pending['counts']['pending'], 6)
        self.complete_region_records()
        regional = merge_patch_reviews(self.stage, self.dataset)
        self.assertEqual(regional['counts'], {'passed': 6})
        self.assertEqual(regional['overall_status'], 'pending')
        self.assertEqual(regional['status'], 'incomplete')
        path = self.stage / 'review.json'
        report = json.loads(path.read_text())
        report['visual_review'] = {
            'status': 'compared',
            'result': 'passed',
            'findings': [],
        }
        write_json_atomic(path, report)
        self.assertEqual(validate_patch_review(self.stage)['status'], 'passed')

    def test_merge_rejects_another_region_or_revision_without_overwriting(
        self,
    ):
        self.complete_region_records()
        path = self.root / self.index['patches'][0]['review_file']
        original = json.loads(path.read_text())
        previous = (self.root / 'review.json').read_bytes()
        for change in ['id', 'hash', 'duplicate']:
            with self.subTest(change=change):
                report = copy.deepcopy(original)
                if change == 'id':
                    report['patches'][0]['id'] = self.index['patches'][1]['id']
                elif change == 'hash':
                    report['plan_sha256'] = 'stale'
                else:
                    report['patches'].append(report['patches'][0])
                write_json_atomic(path, report)
                with self.assertRaises(LayoutError):
                    merge_patch_reviews(self.stage, self.dataset)
                self.assertEqual(
                    (self.root / 'review.json').read_bytes(), previous
                )

    def test_merge_reports_errors_and_uncertainty_instead_of_approving(self):
        self.complete_region_records()
        for region, status in zip(
            self.index['patches'][:2],
            ['needs_change', 'uncertain'],
            strict=True,
        ):
            path = self.root / region['review_file']
            report = json.loads(path.read_text())
            report['patches'][0]['checks']['positions'] = status
            report['patches'][0]['findings'] = [
                'Check the placement at (20, 24).'
            ]
            write_json_atomic(path, report)
        result = merge_patch_reviews(self.stage, self.dataset)
        self.assertEqual(
            result['counts'], {'needs_change': 1, 'uncertain': 1, 'passed': 4}
        )
        self.assertEqual(len(result['findings']), 2)
        self.assertEqual(result['status'], 'incomplete')

    def test_changed_overall_image_invalidates_review(self):
        (self.stage / 'preview.png').write_bytes(b'changed')
        with self.assertRaisesRegex(LayoutError, 'Checkpoint files changed'):
            validate_patch_review(self.stage)

    def test_changed_source_atlas_invalidates_review_without_manifest_edits(
        self,
    ):
        root = Path(self.temporary.name) / 'skill-copy'
        planner = root / 'data/planner'
        planner.mkdir(parents=True)
        for name in ['scripts', 'assets']:
            (root / name).symlink_to(
                self.dataset.root / name, target_is_directory=True
            )
        for path in (self.dataset.root / 'data').iterdir():
            if path.name != 'planner':
                (root / 'data' / path.name).symlink_to(
                    path, target_is_directory=path.is_dir()
                )
        for path in (self.dataset.root / 'data/planner').iterdir():
            destination = planner / path.name
            if path.name == 'atlas.png':
                shutil.copyfile(path, destination)
            else:
                destination.symlink_to(path, target_is_directory=path.is_dir())
        copied = Dataset(root)
        self.assertEqual(
            validate_patch_review(self.stage, dataset=copied)['status'],
            'incomplete',
        )
        with Image.open(planner / 'atlas.png') as image:
            changed = image.convert('RGBA')
        changed.putpixel((0, 0), (255, 0, 255, 255))
        changed.save(planner / 'atlas.png')
        with self.assertRaisesRegex(
            LayoutError, 'Rendering code or assets changed'
        ):
            validate_patch_review(self.stage, dataset=copied)

    def test_main_review_errors_block_delivery_and_uncertainty_is_retained(
        self,
    ):
        self.complete_region_records()
        merge_patch_reviews(self.stage, self.dataset)
        path = self.stage / 'review.json'
        report = json.loads(path.read_text())
        report['visual_review'] = {
            'status': 'compared',
            'result': 'passed',
            'findings': [
                {
                    'status': 'needs_change',
                    'description': 'The entrance at (20, 24) is blocked.',
                }
            ],
        }
        write_json_atomic(path, report)
        self.assertEqual(
            validate_patch_review(self.stage)['overall_result'], 'needs_change'
        )
        plan = json.loads((self.stage / 'plan.json').read_text())
        with self.assertRaisesRegex(LayoutError, 'known errors'):
            deliver(
                self.dataset,
                plan,
                Path(self.temporary.name) / 'blocked',
                workspace=self.temporary.name,
                review_directory=self.stage,
                scale=1,
            )
        report['visual_review']['findings'][0]['status'] = 'uncertain'
        write_json_atomic(path, report)
        result = deliver(
            self.dataset,
            plan,
            Path(self.temporary.name) / 'draft',
            workspace=self.temporary.name,
            review_directory=self.stage,
            scale=1,
        )
        self.assertTrue(result['manifest']['review']['requires_user_decision'])


if __name__ == '__main__':
    unittest.main()
