"""Stage the pinned public client, then restore only hashed build metadata."""
import hashlib
import importlib.util
import json
import shutil
import sys
from pathlib import Path

source, output = (Path(arg).resolve() for arg in sys.argv[1:])
expected = 'cd2686f46906db0f8a03d64d0bc3029a4629c3ef8ad796017bfe9326cc23e5b1'
raw = (source / 'native-router-freeze.json').read_bytes()
assert hashlib.sha256(raw).hexdigest() == expected
receipt = json.loads(raw)
assert receipt['version'] == '0.3.19'
spec = importlib.util.spec_from_file_location('source_verifier', source / 'scripts/verify_native_release_source.py')
helper = importlib.util.module_from_spec(spec)
spec.loader.exec_module(helper)
helper.stage(source, output / 'cli')
(output / 'cli/release-inputs.json').replace(output / 'public-export-receipt.json')
for name, digest in receipt['inputs']['cli'].items():
    if name.startswith('openvegas-0.3.19.dist-info/'):
        data = helper.read(source, name, digest)
        path = output / 'cli' / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(data)
shutil.copyfile(source / 'native-router-freeze.json', output / 'freeze.json')
actual = {p.relative_to(output / 'cli').as_posix(): hashlib.sha256(p.read_bytes()).hexdigest()
          for p in (output / 'cli').rglob('*') if p.is_file()}
assert actual == receipt['inputs']['cli']
assert len(receipt['checkout_native_inputs']) == 28
print(json.dumps({'frozen': str(output), 'freeze_sha256': expected,
                  'input_files': len(actual), 'version': receipt['version']}))
