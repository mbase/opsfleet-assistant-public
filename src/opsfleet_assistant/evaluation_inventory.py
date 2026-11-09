"""Standard-library-only Part 5 input identity and early startup verification.

This detects source drift, not hostile Python startup or a malicious local owner.
Keep this module independent of application imports so the terminal bootstrap can
load it by filename before importing the application package.
"""
import argparse
import hashlib
import json
import os
from pathlib import Path
import subprocess

POLICY = 'part5-inputs-v2'
INPUT_DIRS = {'src', 'tests', 'config', 'scripts', '.github'}
AREA = 'docs/validation/part5-v1'
SKIP_ROOT_DIRS = {'.git', '.venv', '.idea', 'runtime', 'build', 'dist'}
CACHE_DIRS = {'__pycache__', '.pytest_cache', '.ruff_cache', '.mypy_cache'}
ROOT_FILES = {'pyproject.toml', 'requirements.txt', 'requirements.step4.lock',
              'requirements.part5.lock', '.gitignore', '.coveragerc', '.python-version'}
EXEC_SUFFIXES = {'.py', '.pyw', '.pyc', '.pth', '.so', '.pyd', '.dll'}
ROOT_CONFIG_SUFFIXES = {'.toml', '.ini', '.cfg'}
DIFF_PATHS = sorted(INPUT_DIRS | ROOT_FILES) + [
    ':(top,glob)*' + suffix for suffix in sorted(EXEC_SUFFIXES | ROOT_CONFIG_SUFFIXES)]


def digest(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def canonical(files):
    return hashlib.sha256(''.join(f'{files[p]}  {p}\n' for p in sorted(files)).encode()).hexdigest()


def selected(relative):
    """One selection policy for clean copies and Git/untracked/ignored inputs."""
    p = Path(relative)
    if p.parts[0] in SKIP_ROOT_DIRS or any(part in CACHE_DIRS or part.endswith('.egg-info') for part in p.parts):
        return False
    if relative.startswith(AREA + '/'):
        rest = p.parts[len(Path(AREA).parts):]
        if rest[0] in {'results', 'history', 'runs', 'private'}:
            return False
        if len(rest) == 1 and rest[0] in {'candidate_manifest.json', 'acceptance_report.md'}:
            return False
        return True
    if p.parts[0] in INPUT_DIRS:
        return True
    # Catch root import/test hooks and packages outside the normal src layout.
    return (relative in ROOT_FILES or p.suffix in EXEC_SUFFIXES or
            len(p.parts) == 1 and p.suffix in ROOT_CONFIG_SUFFIXES)


def inventory(root):
    root = Path(root).resolve()
    names = []
    for directory, dirs, files in os.walk(root, followlinks=False):
        relative = Path(directory).relative_to(root)
        retained = []
        for name in dirs:
            path = relative / name
            if name in CACHE_DIRS or name.endswith('.egg-info') or relative == Path('.') and name in SKIP_ROOT_DIRS:
                continue
            if path.as_posix() in {AREA + '/' + n for n in ('results', 'history', 'runs', 'private')}:
                continue
            if (root / path).is_symlink():
                raise ValueError('input_directory_symlink')
            retained.append(name)
        dirs[:] = retained
        for name in files:
            relative_file = (relative / name).as_posix()
            if selected(relative_file):
                if (root / relative_file).is_symlink():
                    raise ValueError('input_file_symlink')
                names.append(relative_file)
    return {name: digest(root / name) for name in sorted(names)}


def tracked_diff_hash(root):
    patch = subprocess.check_output(['git', 'diff', '--binary', 'HEAD', '--', *DIFF_PATHS], cwd=root)
    return hashlib.sha256(patch).hexdigest()


def source_identity(root):
    """Freeze the same complete inventory in checkouts and source-only copies."""
    root = Path(root).resolve()
    files = inventory(root)
    source = {'files': files, 'revision': canonical(files), 'inventory_policy': POLICY,
              'application_head': None, 'application_tree': None,
              'tracked_application_diff_sha256': None,
              'uncommitted': True, 'untracked_inputs_included': True}
    if (root / '.git').exists():
        for key, revision in (('application_head', 'HEAD'), ('application_tree', 'HEAD^{tree}')):
            source[key] = subprocess.check_output(
                ['git', 'rev-parse', revision], cwd=root, text=True).strip()
        source['tracked_application_diff_sha256'] = tracked_diff_hash(root)
    return source


def verify_inventory(root, source):
    root = Path(root).resolve()
    if source.get('inventory_policy') != POLICY:
        raise ValueError('input_inventory_policy_required')
    expected = source['files']
    if canonical(expected) != source['revision']:
        raise ValueError('source_aggregate_mismatch')
    actual = inventory(root)
    if set(actual) != set(expected):
        raise ValueError('input_inventory_mismatch')
    if actual != expected:
        raise ValueError('source_path_mismatch')
    if (root / '.git').exists():
        head = subprocess.check_output(['git', 'rev-parse', 'HEAD'], cwd=root, text=True).strip()
        tree = subprocess.check_output(['git', 'rev-parse', 'HEAD^{tree}'], cwd=root, text=True).strip()
        if head != source['application_head'] or tree != source['application_tree']:
            raise ValueError('application_head_or_tree_changed')
        if tracked_diff_hash(root) != source['tracked_application_diff_sha256']:
            raise ValueError('tracked_application_diff_changed')
    return source['revision']


def part5_parser():
    parser = argparse.ArgumentParser(description='Version-bound Part 5 evaluation; dry by default.')
    parser.add_argument('--manifest', type=Path, required=True)
    parser.add_argument('--run-id', required=True)
    parser.add_argument('--layer', choices=('interpretation', 'warehouse'), required=True)
    parser.add_argument('--select', nargs='+')
    parser.add_argument('--live', action='store_true')
    parser.add_argument('--confirm-owner-run', action='store_true')
    parser.add_argument('--dry-run', action='store_true')
    parser.add_argument('--env-file', type=Path,
                        help='Read this KEY=value file only in confirmed live mode; its values override exported variables')
    parser.add_argument('--state', type=Path, help='New disposable directory below OS temporary root, outside repository')
    parser.add_argument('--resume', action='store_true', help='Requires --state and explicit --select; attempted cases never replay')
    return parser


def bootstrap(root, argv):
    parser = part5_parser()
    args = parser.parse_args(argv)
    try:
        manifest = json.loads(args.manifest.read_text())
        verify_inventory(root, manifest['source'])
    except (ValueError, KeyError, OSError, subprocess.CalledProcessError):
        parser.error('invalid_frozen_input_inventory; application not imported')
