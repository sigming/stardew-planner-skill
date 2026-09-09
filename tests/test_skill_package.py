# /// script
# requires-python = ">=3.12"
# dependencies = ["pillow==12.3.0", "pyyaml==6.0.2"]
# ///

# ruff: noqa: E402

import hashlib
import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch
from zipfile import ZipFile

from PIL import Image

REPO_ROOT = Path(__file__).resolve().parents[1]
ROOT = REPO_ROOT / 'skills/stardew-planner-skill'
sys.path.insert(0, str(REPO_ROOT / 'scripts'))

from check_install import skill_metadata
from package_skill import build_package


class SkillMetadataTests(unittest.TestCase):
    def test_release_tag_matches_metadata_version(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            (root / 'SKILL.md').write_text(
                '---\nname: example-skill\ndescription: Example skill.\n'
                'metadata:\n  version: "1.2.3"\n---\n'
            )
            metadata = skill_metadata(root, 'v1.2.3')
            self.assertEqual(metadata['metadata']['version'], '1.2.3')
            for tag in ['v1.2.4', '1.2.3', 'main']:
                with (
                    self.subTest(tag=tag),
                    self.assertRaisesRegex(
                        ValueError, 'Release tag must be v1.2.3'
                    ),
                ):
                    skill_metadata(root, tag)

    def test_missing_or_invalid_versions_are_rejected(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            for version in ['null', '1.2', '"01.2.3"', '"1.2"', '"v1.2.3"']:
                with self.subTest(version=version):
                    (root / 'SKILL.md').write_text(
                        '---\nname: example-skill\ndescription: Example.\n'
                        f'metadata:\n  version: {version}\n---\n'
                    )
                    with self.assertRaisesRegex(
                        ValueError, 'metadata.version'
                    ):
                        skill_metadata(root)

    def test_runtime_and_repository_license_and_python_version_match(self):
        for name in ['LICENSE', '.python-version']:
            with self.subTest(file=name):
                self.assertEqual(
                    (ROOT / name).read_bytes(),
                    (REPO_ROOT / name).read_bytes(),
                )

    def test_installable_directory_excludes_repository_tools(self):
        self.assertFalse((REPO_ROOT / 'SKILL.md').exists())
        for path in [
            '.git',
            '.github',
            'tests',
            'ruff.toml',
            'scripts/maintenance',
            'scripts/package_skill.py',
            'scripts/check_install.py',
            'scripts/build_recognition.py',
        ]:
            with self.subTest(path=path):
                self.assertFalse((ROOT / path).exists())


class SkillPackageTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.temporary = tempfile.TemporaryDirectory(
            prefix='skill-package-test-'
        )
        cls.addClassCleanup(cls.temporary.cleanup)
        cls.workspace = Path(cls.temporary.name)
        with patch('package_skill.REPO_ROOT', cls.workspace):
            cls.package = build_package(ROOT)
        cls.path = Path(cls.package['path'])
        cls.installed = cls.workspace / 'installed'
        with ZipFile(cls.path) as archive:
            archive.extractall(cls.installed)
            cls.files = archive.namelist()
        cls.skill = cls.installed / cls.package['verification']['skill']

    def test_package_contains_only_the_installable_skill(self):
        verification = self.package['verification']
        self.assertEqual(
            self.path.name,
            f'{verification["skill"]}-{verification["version"]}.zip',
        )
        prefix = f'{verification["skill"]}/'
        self.assertTrue(all(path.startswith(prefix) for path in self.files))
        relative_paths = {path.removeprefix(prefix) for path in self.files}
        self.assertIn('SKILL.md', relative_paths)
        self.assertIn('LICENSE', relative_paths)
        self.assertIn('scripts/layout.py', relative_paths)
        for path in relative_paths:
            self.assertFalse(
                {'.git', '.github', 'tests', 'maintenance', '__pycache__'}
                & set(Path(path).parts),
                path,
            )
        for path in [
            'scripts/package_skill.py',
            'scripts/check_install.py',
            'scripts/build_recognition.py',
        ]:
            self.assertNotIn(path, relative_paths)

    def test_packaged_map_sources_keep_recorded_hashes(self):
        root = self.skill / 'data/maps/sources'
        sources = json.loads((root / 'sources.json').read_text())
        for path, record in sources.items():
            with self.subTest(source=path):
                blob = (root / path).read_bytes()
                self.assertEqual(
                    hashlib.sha256(blob).hexdigest(), record['sha256']
                )
                self.assertEqual(len(blob), record['bytes'])

    def test_extracted_skill_runs_from_an_unrelated_directory(self):
        cli = self.skill / 'scripts/layout.py'
        plan = self.workspace / 'plan.json'
        preview = self.workspace / 'preview.png'
        for args in [
            ['new', str(plan), '--map', 'greenhouse'],
            ['render', str(plan), '--out', str(preview)],
        ]:
            result = subprocess.run(
                [sys.executable, str(cli), *args],
                cwd=self.workspace,
                capture_output=True,
                text=True,
            )
            self.assertEqual(
                result.returncode, 0, result.stdout + result.stderr
            )
        self.assertTrue(plan.is_file())
        with Image.open(preview) as image:
            image.verify()


if __name__ == '__main__':
    unittest.main()
