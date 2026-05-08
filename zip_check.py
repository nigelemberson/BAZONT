from __future__ import annotations
import sys
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent
MANIFEST = ROOT / 'required_paths.txt'


def load_required_paths() -> list[str]:
    lines = []
    for raw in MANIFEST.read_text(encoding='utf-8').splitlines():
        line = raw.strip()
        if not line or line.startswith('#'):
            continue
        lines.append(line)
    return lines


def main(argv: list[str]) -> int:
    if len(argv) != 2:
        print('Usage: python zip_check.py <zipfile>')
        return 2
    zip_path = Path(argv[1]).resolve()
    if not zip_path.is_file():
        print(f'ZIP CHECK: FAIL\nMissing zip file: {zip_path}')
        return 1

    required = load_required_paths()
    with zipfile.ZipFile(zip_path) as zf:
        names = set(zf.namelist())
        # V21 folder-preservation hard rule:
        # folder entries listed in required_paths.txt must exist explicitly in the ZIP.
        # Do not silently treat a folder as present just because a child file exists.
        missing = [rel for rel in required if rel not in names]
        if missing:
            print('ZIP CHECK: FAIL')
            print('Missing required ZIP paths:')
            for item in missing:
                print(f' - {item}')
            return 1
        explicit_dirs = sorted(n for n in names if n.endswith('/'))
        print('ZIP CHECK: PASS')
        print(f'ZIP file: {zip_path.name}')
        print(f'ZIP entries found: {len(zf.namelist())}')
        print(f'Explicit folders found: {len(explicit_dirs)}')
        print(f'Manifest entries checked: {len(required)}')
        return 0


if __name__ == '__main__':
    raise SystemExit(main(sys.argv))
