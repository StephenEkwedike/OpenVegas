"""Stage only pinned client inputs from a notice-preserving repository overlay."""

import argparse
import hashlib
import json
import runpy
import tempfile
from pathlib import Path, PurePosixPath


def sha(data):
    return hashlib.sha256(data).hexdigest()


def read(root, name, expected):
    relative = PurePosixPath(name)
    path = root / name
    if relative.is_absolute() or '..' in relative.parts or not path.is_file():
        raise ValueError('Invalid pinned release input')
    if any(p.is_symlink() for p in (path, *path.parents)):
        raise ValueError('Symlinked release input')
    data = path.read_bytes()
    if sha(data) != expected:
        raise ValueError('Pinned release input changed: ' + name)
    return data


def verify(source, exported=None):
    record = json.loads((source / 'native-release-inputs.json').read_bytes())
    if record['version'] != '0.3.18' or record['native_paid_enabled'] is not False:
        raise ValueError('Unexpected release contract')
    for name, expected in record['repository_files'].items():
        read(source, name, expected)
    digest = hashlib.sha256()
    for name, value in sorted(record['client_files'].items()):
        if record['repository_files'].get(name) != value:
            raise ValueError('Conflicting client pin')
        digest.update(name.encode() + b'\0' + bytes.fromhex(value))
    if digest.hexdigest() != record['source_sha256']:
        raise ValueError('Native source identity mismatch')
    if exported is not None:
        actual = {}
        for path in exported.rglob('*'):
            if path.is_symlink():
                raise ValueError('Symlinked exported input')
            if path.is_file():
                actual[path.relative_to(exported).as_posix()] = sha(path.read_bytes())
        actual.pop('release-inputs.json')
        if actual != record['client_files']:
            raise ValueError('Extra, missing or changed client export')
        manifest = json.loads((exported / 'release-inputs.json').read_bytes())
        if manifest['files'] != actual or not manifest['private_art_excluded']:
            raise ValueError('Sanitizer receipt mismatch')
        builder = runpy.run_path(str(exported / 'scripts/build_cli_candidate.py'))
        if builder['source_digest']() != record['source_sha256']:
            raise ValueError('Builder source identity mismatch')
    return record


def stage(source, output):
    record = verify(source)
    if output.exists():
        raise ValueError('Never overwrite a release stage')
    output.parent.mkdir(parents=True, exist_ok=True)
    sanitizer = runpy.run_path(str(source / 'scripts/prepare_public_release.py'))
    with tempfile.TemporaryDirectory(prefix='native-pinned-', dir=output.parent) as temp:
        root = Path(temp)
        for name, expected in record['client_files'].items():
            if not sanitizer['include'](Path(name)):
                raise ValueError('Unapproved public client path')
            data = read(source, name, expected)
            if sanitizer['SECRET'].search(data):
                raise ValueError('Potential credential in pinned input (value withheld)')
            target = root / name
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(data)
        sanitizer['export'](root, output)
    return verify(source, output)


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--source', required=True, type=Path)
    output = parser.add_mutually_exclusive_group()
    output.add_argument('--stage', type=Path)
    output.add_argument('--exported', type=Path)
    args = parser.parse_args()
    result = (stage(args.source.resolve(), args.stage.resolve()) if args.stage else
              verify(args.source.resolve(), args.exported.resolve() if args.exported else None))
    print(json.dumps({'verified': True, 'client_files': len(result['client_files']),
                      'source_sha256': result['source_sha256'], 'published': False}))
