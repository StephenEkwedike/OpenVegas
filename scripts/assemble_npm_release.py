"""Assemble version-pinned npm packages from verified native candidates; never publish.

All five supported targets must be supplied from the same public source digest.
This packaging gate does not certify native UX, signing, payments or deployment.
"""
import argparse
import hashlib
import json
import re
import shutil
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
TARGETS = {'darwin-arm64', 'darwin-x64', 'linux-arm64', 'linux-x64', 'win32-x64'}
NAME = 'openvegas-ai'
SCOPE = '@lordesteph'


def regular(path):
    if not path.is_file() or any(p.is_symlink() for p in (path, *path.parents)):
        raise ValueError('Missing or symlinked release input')
    return path


def inspect_candidates(candidates):
    selected = {}
    identity = None
    for directory in candidates:
        directory = Path(directory).absolute()
        record = json.loads(regular(directory / 'candidate.json').read_text())
        platform = record['os'] + '-' + record['arch']
        if platform not in TARGETS or platform in selected:
            raise ValueError('Unsupported or duplicate native target')
        if (record.get('npm_install_verified') is not True
                or record.get('includes_private_review_packs') is not False):
            raise ValueError('Require verified public-only native candidate')
        if record['os'] == 'darwin' and record.get('terminal_profile_command_verified') is not True:
            raise ValueError('macOS terminal profile was not verified')
        if not re.fullmatch(r'\d+\.\d+\.\d+', record['version']):
            raise ValueError('Invalid native candidate version')
        if not re.fullmatch(r'[a-f0-9]{64}', record['source_sha256']):
            raise ValueError('Invalid source identity')
        pair = (record['version'], record['source_sha256'])
        if identity is not None and pair != identity:
            raise ValueError(
                'Native candidates have different versions or source digests: '
                f'{platform} has {pair}, expected {identity}'
            )
        identity = pair
        expected = 'openvegas-' + platform + ('.exe' if record['os'] == 'win32' else '')
        if record['binary'] != expected:
            raise ValueError('Unexpected native binary path')
        binary = regular(directory / 'bin' / expected)
        if hashlib.sha256(binary.read_bytes()).hexdigest() != record['sha256']:
            raise ValueError('Native binary checksum mismatch')
        selected[platform] = (record, binary)
    if set(selected) != TARGETS:
        raise ValueError('Missing verified native targets: ' + ', '.join(sorted(TARGETS - selected.keys())))
    return selected, identity


def assemble(candidates, output):
    output = Path(output).absolute()
    if output.exists() or any(p.is_symlink() for p in (output, *output.parents)):
        raise ValueError('Use a new nonsymlinked output directory')
    selected, (version, source) = inspect_candidates(candidates)
    launcher = regular(ROOT / 'npm-cli/bin/release.js').read_bytes()
    output.mkdir(parents=True)
    index = {'version': version, 'source_sha256': source, 'platforms': {}}
    optional = {}
    for target, (record, binary) in sorted(selected.items()):
        name = SCOPE + '/openvegas-' + target
        folder = output / target
        (folder / 'bin').mkdir(parents=True)
        shutil.copy2(binary, folder / 'bin' / binary.name)
        (folder / 'bin' / binary.name).chmod(0o755)
        manifest = {'name': name, 'version': version, 'license': 'MIT',
                    'description': 'OpenVegas native runtime for ' + target,
                    'os': [record['os']], 'cpu': [record['arch']], 'files': ['bin'],
                    'publishConfig': {'access': 'public'}}
        (folder / 'package.json').write_text(json.dumps(manifest, indent=2) + '\n')
        optional[name] = version
        index['platforms'][target] = {'package': name, 'binary': binary.name,
                                      'sha256': record['sha256']}
    package = output / NAME
    (package / 'bin').mkdir(parents=True)
    (package / 'bin/openvegas.js').write_bytes(launcher)
    (package / 'bin/openvegas.js').chmod(0o755)
    manifest = {'name': NAME, 'version': version, 'type': 'module', 'license': 'MIT',
                'description': 'OpenVegas - Agentic Entertainment in your terminal',
                'engines': {'node': '>=20'},
                'bin': {key: 'bin/openvegas.js' for key in (NAME, 'openvegas', 'ov')},
                'files': ['bin', 'release.json', 'README.md'],
                'optionalDependencies': optional, 'publishConfig': {'access': 'public'},
                'homepage': 'https://openvegas.ai',
                'repository': {'type': 'git', 'url': 'https://github.com/StephenEkwedike/OpenVegas.git'}}
    (package / 'package.json').write_text(json.dumps(manifest, indent=2) + '\n')
    (package / 'release.json').write_text(json.dumps(index, indent=2) + '\n')
    (package / 'README.md').write_text(
        '# OpenVegas\n\nAgentic Entertainment.\n\n'
        'Run `npx openvegas-ai` to set up OpenVegas, then use '
        '`/openvegas play` in Claude or `$openvegas play` in Codex. '
        '`npx openvegas-ai play` also opens the terminal lobby.\n\n'
        'Node.js 20+ is required. Keep npm optional dependencies enabled. '
        'The matching native package is pinned to this exact version and verified '
        'before execution. No Python installation or install-time script is used.\n\n'
        'Host billing is the default. Games can lose the stake. '
        'OpenVegas does not reset your host subscription allowance.\n')
    (output / 'assembly.json').write_text(json.dumps({
        'name': NAME, 'version': version, 'source_sha256': source,
        'targets': sorted(selected), 'published': False,
        'scope': 'npm assembly only; native UX and public-release gates remain separate'
    }, indent=2) + '\n')
    return package


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--candidate', action='append', required=True, type=Path)
    parser.add_argument('--output', required=True, type=Path)
    args = parser.parse_args()
    print(assemble(args.candidate, args.output))
