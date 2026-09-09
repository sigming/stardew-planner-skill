# /// script
# requires-python = ">=3.12"
# dependencies = ["pillow==12.3.0", "pyyaml==6.0.2"]
# ///

import argparse
import hashlib
import json
import os
import tempfile
from pathlib import Path
from zipfile import ZIP_DEFLATED, ZipFile, ZipInfo

from check_install import check

REPO_ROOT = Path(__file__).resolve().parents[1]
ROOT = REPO_ROOT / 'skills/stardew-planner-skill'
PACKAGE_FILES = ['SKILL.md', 'LICENSE', '.python-version']
PACKAGE_DIRECTORIES = ['agents', 'assets', 'data', 'examples', 'scripts']
IGNORED_PARTS = {'__pycache__', '.DS_Store', '.cache', '.ruff_cache', '.venv'}


def package_files(root):
    files = [root / name for name in PACKAGE_FILES]
    for name in PACKAGE_DIRECTORIES:
        files.extend(
            path
            for path in (root / name).rglob('*')
            if path.is_file()
            and not set(path.relative_to(root).parts) & IGNORED_PARTS
            and path.suffix not in {'.pyc', '.pyo', '.bak'}
        )
    for path in files:
        if not path.resolve().is_relative_to(root):
            raise ValueError(
                f'Package resource points outside project: {path}'
            )
    return sorted(files)


def build_package(root, destination=None, force=False):
    root = Path(root).resolve()
    verification = check(root)
    if destination is None:
        destination = (
            REPO_ROOT
            / 'dist'
            / f'{verification["skill"]}-{verification["version"]}.zip'
        )
    destination = Path(destination).resolve()
    if destination.suffix != '.zip':
        raise ValueError('Skill package must use a .zip filename')
    if destination.exists() and not force:
        raise ValueError('Package already exists; use --force to replace it')
    if any(
        destination.is_relative_to(root / name) for name in PACKAGE_DIRECTORIES
    ):
        raise ValueError(
            'Package output must stay outside packaged source folders'
        )
    files = package_files(root)
    destination.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary = tempfile.mkstemp(
        prefix='.skill-package-', dir=destination.parent
    )
    os.close(descriptor)
    try:
        with ZipFile(temporary, 'w', compression=ZIP_DEFLATED) as archive:
            for path in files:
                name = f'{verification["skill"]}/{path.relative_to(root).as_posix()}'
                entry = ZipInfo(name, date_time=(1980, 1, 1, 0, 0, 0))
                entry.compress_type = ZIP_DEFLATED
                entry.create_system = 3
                entry.external_attr = 0o100644 << 16
                archive.writestr(entry, path.read_bytes())
        os.replace(temporary, destination)
    finally:
        Path(temporary).unlink(missing_ok=True)
    return {
        'path': str(destination),
        'files': len(files),
        'bytes': destination.stat().st_size,
        'sha256': hashlib.sha256(destination.read_bytes()).hexdigest(),
        'verification': verification,
    }


def main():
    parser = argparse.ArgumentParser(
        description='Validate and package the installable skill without caches, outputs or Git history.'
    )
    parser.add_argument(
        '--out', type=Path, help='Default: dist/<skill>-<version>.zip'
    )
    parser.add_argument('--force', action='store_true')
    args = parser.parse_args()
    print(
        json.dumps(
            build_package(ROOT, args.out, args.force),
            ensure_ascii=False,
            indent=2,
        )
    )


if __name__ == '__main__':
    main()
