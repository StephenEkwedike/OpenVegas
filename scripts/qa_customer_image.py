"""Verify the exact downloaded packages and installed x64 PE. No app import or execution."""
import hashlib
import json
import os
import struct
import sys
from pathlib import Path

MAIN = 'openvegas-ai-0.3.19.tgz'
WIN = 'lordesteph-openvegas-win32-x64-0.3.19.tgz'
BIN = 'openvegas-win32-x64.exe'
SOURCE = 'f85cb9440c8d20ac65df5c2ff4275c80df9df4676672dd76dca6cbab8779bf79'
HOSTS = {
    'CODEX': '9d563ee4e0441ff9d59d612013e0460ddf8e5709735c2d9f6b98b7ef8ca4eb42',
    'CLAUDE': '345996b4bdbae21d817f750fae2f34cbf8d81224a5444c78136091d121beb298',
}


def sha(path):
    with path.open('rb') as f:
        return hashlib.file_digest(f, 'sha256').hexdigest()


def image(path, digest):
    if sha(path) != digest:
        raise ValueError('PE digest mismatch')
    with path.open('rb') as f:
        if f.read(2) != b'MZ':
            raise ValueError('not PE')
        f.seek(0x3c)
        start, = struct.unpack('<I', f.read(4))
        f.seek(start)
        if f.read(6) != b'PE\0\0\x64\x86':
            raise ValueError('not AMD64')
        f.seek(start + 24)
        if f.read(2) != b'\x0b\x02':
            raise ValueError('not PE32+')


def main():
    pin = json.loads(Path('qa-image-pins.json').read_bytes())
    if sys.platform != 'win32' or sys.maxsize != 2**63 - 1 or pin['source'] != SOURCE:
        raise ValueError('wrong supplemental lane')
    for name in (MAIN, WIN):
        if sha(Path('packs', name)) != pin[name]:
            raise ValueError('customer tarball does not match independent pin')
    if sys.argv[1:] == ['packs']:
        return
    if sys.argv[1:] != ['installed']:
        raise ValueError('invalid verification stage')
    root = Path('installed/node_modules').resolve(strict=True)
    app = root / 'openvegas-ai'
    release = json.loads((app / 'release.json').read_bytes())
    pkg = '@lordesteph/openvegas-win32-x64'
    if (release['version'] != '0.3.19' or release['source_sha256'] != SOURCE
            or release['platforms']['win32-x64'] != {'package': pkg, 'binary': BIN, 'sha256': pin['pe']}
            or sha(app / 'bin/openvegas.js') != pin['launcher']):
        raise ValueError('installed release identity mismatch')
    pe = root / pkg / 'bin' / BIN
    image(pe, pin['pe'])
    for host, digest in HOSTS.items():
        image(Path(os.environ['OV_WINDOWS_' + host + '_EXE']), digest)
    with Path(os.environ['GITHUB_ENV']).open('a', encoding='utf-8') as f:
        print('OV_QA_PE=' + str(pe), file=f)
        print('OV_QA_APP=' + str(app / 'bin/openvegas.js'), file=f)
    for name in ('source', MAIN, WIN, 'pe', 'launcher'):
        print(name, pin[name])
    for name, digest in HOSTS.items():
        print(name, digest)


if __name__ == '__main__':
    try:
        main()
    except Exception:
        sys.exit('Customer package or executable identity not established')
