"""Start the verified installed JS launcher in the QA-owned foreground console."""
import os
import subprocess
import sys
from pathlib import Path


def main(host, node, app, bank_port):
    if (sys.platform != 'win32' or not sys.flags.isolated or host not in ('codex', 'claude')
            or not all(os.isatty(i) for i in (0, 1, 2))
            or Path.cwd() != Path(os.environ['USERPROFILE']) / 'work'
            or not Path.cwd().parent.name.startswith('ov-rehearsal-')
            or not (0 < int(bank_port) < 65536)):
        return 21
    if not (Path(node).resolve(strict=True).name.lower() == 'node.exe'
            and Path(app).resolve(strict=True).parts[-4:] == ('node_modules', 'openvegas-ai', 'bin', 'openvegas.js')):
        return 21
    if any(k.startswith(('GITHUB_', 'ACTIONS_')) for k in os.environ):
        return 21
    print('OV-QA-READY', flush=True)
    if input() != 'OV-QA-START':
        return 22
    env = dict(os.environ, OPENVEGAS_BACKEND_URL=f'http://127.0.0.1:{int(bank_port)}',
               OPENVEGAS_INTEGRATION_PROFILE=str(Path.cwd().parent / '.openvegas'))
    # No imports from a source installation, no in-process broker substitution,
    # no hosted launcher fallback. Local mode preserves the disposable endpoint.
    return subprocess.run([node, app, '--local', 'integration', 'terminal', '--attach',
                           '--host', host, '--managed-billing'], env=env, timeout=450).returncode


if __name__ == '__main__':
    sys.exit(main(*sys.argv[1:]))
