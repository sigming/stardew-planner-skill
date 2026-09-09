# /// script
# requires-python = ">=3.12"
# dependencies = ["pillow==12.3.0"]
# ///

import hashlib
import json
import subprocess
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
from layout_images import crop_image
from layout_progress import record_checkpoint, record_progress, work_status
from layout_review import (
    REVIEW_CHECKS,
    merge_patch_reviews,
    record_overall_review,
    record_region_review,
)

ROOT = Path(__file__).resolve().parents[1] / 'skills/stardew-planner-skill'


class WorkflowHelperTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.dataset = Dataset()

    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.plan = self.root / 'plan.json'

    def cli(self, *args, script='layout.py', succeeds=True):
        result = subprocess.run(
            [sys.executable, str(ROOT / 'scripts' / script), *map(str, args)],
            cwd=self.root,
            capture_output=True,
            text=True,
        )
        self.assertEqual(result.returncode == 0, succeeds, result.stderr)
        return json.loads(result.stdout) if succeeds else result

    def test_progress_survives_process_restart_and_failed_edits(self):
        self.cli('new', self.plan)
        self.cli(
            'note',
            self.plan,
            '--stage',
            'buildings',
            '--message',
            'Keep the greenhouse entrance clear.',
            '--next',
            'Place flooring.',
        )
        before = (self.root / 'plan.progress.json').read_bytes()
        self.cli(
            'add', self.plan, 'chest', '--x', -1, '--y', 10, succeeds=False
        )
        self.assertEqual(
            (self.root / 'plan.progress.json').read_bytes(), before
        )
        result = self.cli('status', self.root)
        self.assertEqual(result['last_action'], 'note')
        self.assertEqual(result['notes'][0]['next'], 'Place flooring.')
        plan = json.loads(self.plan.read_text())
        plan['title'] = 'Manually edited'
        write_json_atomic(self.plan, plan)
        result = self.cli('status', self.plan)
        self.assertTrue(result['unrecorded_plan_changes'])

    def test_status_discovers_a_saved_plan_in_the_design_subfolder(self):
        design = self.root / 'stardew-design'
        self.cli('new', design / 'plan.json')
        result = self.cli('status', self.root)
        self.assertEqual(
            Path(result['plan']), (design / 'plan.json').resolve()
        )
        self.cli('new', self.root / 'second-design/plan.json')
        failure = self.cli('status', self.root, succeeds=False)
        self.assertIn('Multiple saved plans', failure.stderr)

    def test_partial_review_resume_merge_and_overall_progress(self):
        plan = create_plan(self.dataset, 'regular')
        write_json_atomic(self.plan, plan)
        record_progress(self.plan, 'new')
        stage = self.root / 'stages/final-r1'
        checkpoint(self.dataset, plan, 'final', stage, scale=1)
        record_checkpoint(self.plan, stage, 'final')
        index = json.loads((stage / 'patches/index.json').read_text())
        checks = dict.fromkeys(REVIEW_CHECKS, 'passed')
        first = index['patches'][0]['id']
        record_region_review(
            stage,
            first,
            checks,
            ['Inspected the assigned comparison.'],
            self.dataset,
        )
        status = work_status(self.root, self.dataset)
        self.assertEqual(status['state'], 'region_review_pending')
        self.assertEqual(len(status['pending_regions']), 5)
        self.assertEqual(status['completed_independent_regions'], [first])
        for region in index['patches'][1:]:
            record_region_review(stage, region['id'], checks, [], self.dataset)
        self.assertEqual(
            work_status(self.root, self.dataset)['state'], 'merge_pending'
        )
        merge_patch_reviews(stage, self.dataset)
        self.assertEqual(
            work_status(self.root, self.dataset)['state'],
            'overall_review_pending',
        )
        record_overall_review(
            stage, 'passed', ['Inspected the complete layout.'], self.dataset
        )
        self.assertEqual(
            work_status(self.root, self.dataset)['state'], 'ready'
        )
        self.cli(
            'deliver',
            self.plan,
            '--review-dir',
            stage,
            '--out-dir',
            self.root / 'result',
            '--scale',
            1,
        )
        self.assertEqual(self.cli('status', self.root)['state'], 'delivered')
        record_region_review(
            stage,
            first,
            checks | {'positions': 'uncertain'},
            ['Position needs user clarification.'],
            self.dataset,
        )
        merge_patch_reviews(stage, self.dataset)
        record_overall_review(
            stage,
            'uncertain',
            ['Retain the new position question.'],
            self.dataset,
        )
        status = self.cli('status', self.root)
        self.assertEqual(status['state'], 'ready')
        self.assertNotIn('delivery', status)
        plan['title'] = 'A new revision'
        write_json_atomic(self.plan, plan)
        self.assertEqual(
            work_status(self.root, self.dataset)['state'], 'checkpoint_stale'
        )

    def test_completed_checkpoint_recovered_when_progress_write_was_interrupted(
        self,
    ):
        plan = create_plan(self.dataset, 'regular')
        write_json_atomic(self.plan, plan)
        stage = self.root / 'stages/final-r1'
        checkpoint(self.dataset, plan, 'final', stage, scale=1)
        status = work_status(self.root, self.dataset)
        self.assertEqual(status['state'], 'region_review_pending')
        self.assertEqual(len(status['pending_regions']), 6)

    def test_crop_keeps_source_unchanged_and_reports_actual_size(self):
        source = self.root / 'source.png'
        Image.new('RGBA', (20, 30), '#123456').save(source)
        original = source.read_bytes()
        result = crop_image(source, self.root / 'detail.png', [2, 4, 5, 6], 2)
        self.assertEqual(result['size_px'], [10, 12])
        self.assertEqual(source.read_bytes(), original)
        self.assertEqual(
            result['sha256'],
            hashlib.sha256(Path(result['path']).read_bytes()).hexdigest(),
        )
        for destination, rect in [
            (source, [0, 0, 1, 1]),
            (self.root / 'outside.png', [19, 0, 2, 2]),
        ]:
            with self.assertRaises(LayoutError):
                crop_image(source, destination, rect)

    def test_updated_region_blocks_reuse_of_merged_approval(self):
        plan = create_plan(self.dataset, 'regular')
        write_json_atomic(self.plan, plan)
        stage = self.root / 'stages/final-r1'
        checkpoint(self.dataset, plan, 'final', stage, scale=1)
        record_checkpoint(self.plan, stage, 'final')
        index = json.loads((stage / 'patches/index.json').read_text())
        checks = dict.fromkeys(REVIEW_CHECKS, 'passed')
        for region in index['patches']:
            record_region_review(stage, region['id'], checks, [], self.dataset)
        merge_patch_reviews(stage, self.dataset)
        record_overall_review(stage, 'passed', [], self.dataset)
        self.assertEqual(
            work_status(self.root, self.dataset)['state'], 'ready'
        )
        first = index['patches'][0]['id']
        record_region_review(
            stage,
            first,
            checks | {'positions': 'needs_change'},
            ['Move the chest at (20, 24).'],
            self.dataset,
        )
        status = work_status(self.root, self.dataset)
        self.assertEqual(status['state'], 'needs_change')
        self.assertEqual(status['regions_needing_change'], [first])
        self.cli(
            'deliver',
            self.plan,
            '--review-dir',
            stage,
            '--out-dir',
            self.root / 'result',
            succeeds=False,
        )
        self.assertFalse((self.root / 'result').exists())
        record_region_review(
            stage,
            first,
            checks,
            ['Rechecked the reported position.'],
            self.dataset,
        )
        self.assertEqual(
            work_status(self.root, self.dataset)['state'], 'merge_pending'
        )
        merge_patch_reviews(stage, self.dataset)
        self.assertEqual(
            work_status(self.root, self.dataset)['state'],
            'overall_review_pending',
        )

    def test_intermediate_checkpoint_changes_are_detected(self):
        plan = create_plan(self.dataset, 'regular')
        write_json_atomic(self.plan, plan)
        stage = self.root / 'stages/buildings'
        checkpoint(self.dataset, plan, 'buildings', stage, scale=1)
        record_checkpoint(self.plan, stage, 'buildings')
        record_overall_review(stage, 'passed', [], self.dataset)
        self.assertEqual(
            work_status(self.root, self.dataset)['state'], 'planning'
        )
        with (stage / 'preview.png').open('ab') as file:
            file.write(b'changed')
        self.assertEqual(
            work_status(self.root, self.dataset)['state'], 'checkpoint_stale'
        )

    def test_query_extracts_exact_sprite_card_and_paginates(self):
        card = self.cli(
            'rarecrow-3',
            '--image-out',
            self.root / 'card.png',
            script='query_catalog.py',
        )
        self.assertTrue(Path(card['image']['path']).is_file())
        sprite = self.cli(
            'oak-tree',
            '--image-kind',
            'sprite',
            '--season',
            'winter',
            '--image-out',
            self.root / 'sprite.png',
            script='query_catalog.py',
        )
        actual = Path(sprite['image']['path'])
        with Image.open(actual) as image:
            expected = self.dataset.items['oak-tree-winter']['image']
            self.assertEqual(
                list(image.size), [expected['width_px'], expected['height_px']]
            )
        result = self.cli(
            '--category',
            'furniture',
            '--limit',
            3,
            '--offset',
            2,
            script='query_catalog.py',
        )
        self.assertEqual(len(result['items']), 3)
        self.assertGreater(result['count'], result['returned_count'])
        self.cli(
            '--category',
            'furniture',
            '--image-out',
            self.root / 'ambiguous.png',
            script='query_catalog.py',
            succeeds=False,
        )
        self.assertFalse((self.root / 'ambiguous.png').exists())


if __name__ == '__main__':
    unittest.main()
