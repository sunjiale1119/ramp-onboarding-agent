"""Audit tracked release files and package only Ramp source/docs from Git HEAD.

Usage: python scripts/package_source.py --audit
       python scripts/package_source.py --output PATH.zip
Raw evaluation transcripts and superseded UI screenshots are excluded from ZIP.
"""
import argparse
import hashlib
import re
import subprocess
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
def git(*args):
    return subprocess.check_output(['git', '-c', f'safe.directory={ROOT.as_posix()}', *args], cwd=ROOT)

def inspect_file(name, data):
    p = Path(name)
    if (p.name == '.env' or p.suffix.lower() in {'.pem', '.key', '.sqlite', '.db', '.log'}
            or any(x in p.parts for x in {'.git', '.venv', '__pycache__', 'node_modules'})):
        raise ValueError('Disallowed file: ' + name)
    if p.suffix.lower() in {'.png', '.jpg', '.jpeg'}:
        return
    text = data.decode('utf-8', errors='replace')
    patterns = [r'(?<![A-Za-z0-9])sk-[A-Za-z0-9_-]{20,}', r'gh[pousr]_[A-Za-z0-9]{20,}',
                r'AKIA[A-Z0-9]{16}', r'LTAI[A-Za-z0-9]{12,}',
                r'-----BEGIN (?:RSA |OPENSSH |EC )?PRIVATE KEY-----']
    if any(re.search(pat, text) for pat in patterns):
        raise ValueError('Potential secret in ' + name + ' (value withheld)')

def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--audit', action='store_true')
    parser.add_argument('--output', type=Path)
    args = parser.parse_args()
    if args.audit:
        names = git('ls-files', '--cached', '--others', '--exclude-standard', '-z').decode().split('\0')
        for name in filter(None, names):
            inspect_file(name, (ROOT / name).read_bytes())
        print('Working-tree source scan passed (no matching secret patterns).')
        return
    if not args.output:
        parser.error('--output is required')
    revision = git('rev-parse', 'HEAD').decode().strip()
    names = git('ls-tree', '-r', '--name-only', 'HEAD').decode().splitlines()
    selected = []
    for name in names:
        if name.startswith('ramp/eval/reports/'):
            continue
        if name.startswith('docs/screenshots/') and not name.startswith('docs/screenshots/latest/'):
            continue
        selected.append(name)
    args.output.parent.mkdir(parents=True, exist_ok=True)
    if args.output.exists():
        raise ValueError('Refusing to overwrite existing ZIP')
    manifest = {}
    with zipfile.ZipFile(args.output, 'x', zipfile.ZIP_DEFLATED) as archive:
        for name in selected:
            data = git('show', f'HEAD:{name}')
            inspect_file(name, data)
            archive.writestr('ramp/' + name, data)
            manifest[name] = hashlib.sha256(data).hexdigest()
        archive.writestr('ramp/DELIVERY.txt', 'Ramp source delivery\nGit commit: ' + revision +
                         '\nIncludes current UI screenshots. Excludes secrets, local databases, raw evaluation transcripts and historical screenshots.\n')
    with zipfile.ZipFile(args.output) as archive:
        assert archive.testzip() is None
        for name, digest in manifest.items():
            assert hashlib.sha256(archive.read('ramp/' + name)).hexdigest() == digest
    print(f'ZIP verified: {len(selected)} tracked files; commit {revision}')
    print(str(args.output.resolve()))
    print(f'Size: {args.output.stat().st_size} bytes')
    print('SHA256: ' + hashlib.sha256(args.output.read_bytes()).hexdigest())

if __name__ == '__main__':
    main()
