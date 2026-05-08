from __future__ import annotations
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent
MANIFEST = ROOT / 'required_paths.txt'
PROTECTED_KEEP = [
    ROOT / 'outbox' / 'keep.txt',
    ROOT / 'static' / 'keep.txt',
    ROOT / 'forms' / 'outbox' / 'keep.txt',
    ROOT / 'forms' / 'static' / 'keep.txt',
]


def load_required_paths() -> list[str]:
    lines = []
    for raw in MANIFEST.read_text(encoding='utf-8').splitlines():
        line = raw.strip()
        if not line or line.startswith('#'):
            continue
        lines.append(line)
    return lines


def main() -> int:
    missing: list[str] = []
    required = load_required_paths()
    for rel in required:
        path = ROOT / rel.rstrip('/')
        if rel.endswith('/'):
            if not path.is_dir():
                missing.append(rel)
        else:
            if not path.is_file():
                missing.append(rel)

    for keep in PROTECTED_KEEP:
        if not keep.is_file():
            missing.append(str(keep.relative_to(ROOT)))

    version_file = ROOT / 'version.txt'
    if not version_file.is_file():
        missing.append('version.txt')

    if missing:
        print('PACKAGE CHECK: FAIL')
        print('Missing required paths:')
        for item in missing:
            print(f' - {item}')
        return 1

    total_files = sum(1 for p in ROOT.rglob('*') if p.is_file())
    total_dirs = sum(1 for p in ROOT.rglob('*') if p.is_dir())
    print('PACKAGE CHECK: PASS')
    print(f'Files found: {total_files}')
    print(f'Directories found: {total_dirs}')
    print(f'Manifest entries checked: {len(required)}')
    print('Protected keep files present: yes')
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
