"""Read public Actions metadata only. No auth material is printed or passed to test children."""
import json
import os
import urllib.request
from pathlib import Path


def main():
    p = json.loads(Path('qa-image-pins.json').read_bytes())
    repo = os.environ['GITHUB_REPOSITORY']
    # This lane is bound to the public QA repo ONLY. A private runner must
    # separately review provenance and permissions rather than inheriting this.
    if repo != 'StephenEkwedike/OpenVegas':
        raise ValueError('different repository requires separately approved lane')
    def get(suffix):
        request = urllib.request.Request('https://api.github.com/repos/'+repo+'/actions/'+suffix,
                                         headers={'User-Agent': 'Windows-offline-QA'})
        with urllib.request.urlopen(request, timeout=20) as r:
            return json.loads(r.read(512*1024))
    run = get('runs/'+str(p['run_id']))
    artifact = get('artifacts/'+str(p['artifact_id']))
    if (run['id'] != p['run_id'] or run['head_sha'] != p['head_sha']
            or run['path'] != '.github/workflows/native-public.yml' or run['event'] != 'push'
            or run['head_branch'] != 'codex/native-release-0319'
            or artifact['expired'] is not False or artifact['name'] != 'customer-tarballs-unpublished'
            or artifact['id'] != p['artifact_id'] or artifact['workflow_run']['id'] != p['run_id']
            or artifact['digest'] != 'sha256:'+p['zip_sha256']):
        raise ValueError('matrix artifact identity not established')


if __name__ == '__main__':
    try:
        main()
    except Exception:
        raise SystemExit('Repaired QA artifact provenance not established') from None
