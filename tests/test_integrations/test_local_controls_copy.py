from pathlib import Path


def test_packaged_skill_shortcut_matches_executable_prefix():
    from openvegas.integrations.local_controls import LocalKeys
    assert LocalKeys.MENU == b'\x1do'
    skill = Path('openvegas/integrations/assets/SKILL.md').read_text()
    assert 'Hold Control and press ], release' in skill
    assert 'Hold Control and press X' not in skill
    assert 'lowercase o (no Shift)' in skill
    assert 'fallback DOES require available host inference' in skill
    assert 'report that in-session entry is unavailable in that session' not in skill
