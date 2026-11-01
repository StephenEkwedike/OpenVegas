import runpy
from pathlib import Path
import pytest

check = runpy.run_path(str(Path(__file__).resolve().parents[2] / 'scripts/build_cli_candidate.py'))['verify_version_output']


def test_release_version_matches():
    check('openvegas-darwin-arm64, version 0.3.11\n', '0.3.11')


@pytest.mark.parametrize('output', ['', 'openvegas, version 0.3.10', 'openvegas, version 0.3.110'])
def test_release_version_mismatch_fails(output):
    with pytest.raises(ValueError, match='differs'):
        check(output, '0.3.11')
