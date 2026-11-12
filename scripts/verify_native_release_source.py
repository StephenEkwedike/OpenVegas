"""Stage only pinned client inputs from a notice-preserving repository overlay."""

import argparse
import hashlib
import json
import re
import runpy
import tempfile
from pathlib import Path, PureWindowsPath


def sha(data):
    return hashlib.sha256(data).hexdigest()


def unique_object(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError('Duplicate release receipt key')
        result[key] = value
    return result


def normalized_name(name):
    if not isinstance(name, str) or not name or PureWindowsPath(name).drive:
        raise ValueError('Invalid release path')
    parts = name.replace('\\', '/').split('/')
    if any(not part or part in {'.', '..'} or part.endswith((' ', '.'))
            or any(ord(char) < 32 or ord(char) == 127 or char in '<>:"|?*' for char in part)
            or PureWindowsPath(part).is_reserved() for part in parts):
        raise ValueError('Unsafe or ambiguous release path')
    return '/'.join(parts)


def normalized_files(files):
    if type(files) is not dict:
        raise ValueError('Invalid release inventory')
    result, seen, directories = {}, set(), set()
    for name, expected in files.items():
        name = normalized_name(name)
        folded = name.casefold()
        parts = folded.split('/')
        parents = {'/'.join(parts[:index]) for index in range(1, len(parts))}
        if folded in seen or folded in directories or parents & seen:
            raise ValueError('Colliding release paths')
        if type(expected) is not str or not re.fullmatch('[0-9a-f]{64}', expected):
            raise ValueError('Invalid release hash')
        result[name] = expected
        seen.add(folded)
        directories.update(parents)
    return result


def read(root, name, expected):
    name = normalized_name(name)
    path = root.joinpath(*name.split('/'))
    if not path.is_file():
        raise ValueError('Invalid pinned release input')
    if any(p.is_symlink() for p in (path, *path.parents)):
        raise ValueError('Symlinked release input')
    data = path.read_bytes()
    if sha(data) != expected:
        raise ValueError('Pinned release input changed: ' + name)
    return data


def verify(source, exported=None):
    record = json.loads((source / 'native-release-inputs.json').read_bytes(), object_pairs_hook=unique_object)
    if record['version'] != '0.3.18' or record['native_paid_enabled'] is not False:
        raise ValueError('Unexpected release contract')
    repository_files = normalized_files(record['repository_files'])
    client_files = normalized_files(record['client_files'])
    for name, expected in repository_files.items():
        read(source, name, expected)
    digest = hashlib.sha256()
    for name, value in sorted(client_files.items()):
        if repository_files.get(name) != value:
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
                name = path.relative_to(exported).as_posix()
                if normalized_name(name) != name:
                    raise ValueError('Noncanonical exported path')
                actual[name] = sha(path.read_bytes())
        actual.pop('release-inputs.json')
        actual = normalized_files(actual)
        if actual != client_files:
            raise ValueError('Extra, missing or changed client export')
        manifest = json.loads((exported / 'release-inputs.json').read_bytes(), object_pairs_hook=unique_object)
        if normalized_files(manifest['files']) != actual or manifest['private_art_excluded'] is not True:
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
        for name, expected in normalized_files(record['client_files']).items():
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
