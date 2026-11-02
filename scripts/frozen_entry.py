"""Frozen CLI entry point; bundle verification never opens a microphone or account."""

import json
import os
import sys

REQUIRED_PACKS = {"beat-maker", "bicycle-finish", "pixel-courier", "skyline-dunk",
                  "three-point-glow", "visor-explorer"}
REVIEW_PACKS = {"naruto-shadow-clone", "lebron-court", "q50-jewelry", "q50-jewelry-motion"}


def verify_pack_inventory(names, *, include_review_packs=False):
    expected = REQUIRED_PACKS | (REVIEW_PACKS if include_review_packs else set())
    if len(names) != len(expected) or set(names) != expected:
        raise ValueError("Unexpected pack inventory; private review packs require explicit opt-in")


def verify_integration_assets():
    """Generate both plugin bundles in memory, without installing or signing in."""
    from openvegas.integrations.installer import bundle_files

    counts = {}
    for host in ('claude', 'codex'):
        files = bundle_files({'host': host, 'mod': host == 'claude'}, 'openvegas-bundle-check')
        if not any(name.endswith('/SKILL.md') for name in files):
            raise ValueError('Missing native integration skill')
        if host == 'claude' and not any(name.endswith('/register.js') for name in files):
            raise ValueError('Missing native integration renderer')
        counts[host] = len(files)
    return counts


def verify_mac_credential_files():
    """Exercise packaged storage using synthetic tokens; forbid all OS bridges."""
    import builtins
    import tempfile
    from pathlib import Path
    from openvegas import config
    from openvegas.integrations.credentials import Credentials
    from openvegas.integrations.game_browser import game_credentials

    original_import = builtins.__import__
    previous = (config.CONFIG_DIR, config.CONFIG_FILE, os.environ.get('OPENVEGAS_INTEGRATION_PROFILE'))
    def guarded_import(name, *args, **kwargs):
        if name.split('.')[0] in {'keyring', 'Security', 'objc'}:
            raise AssertionError('macOS credential path attempted an OS credential import')
        return original_import(name, *args, **kwargs)
    try:
        builtins.__import__ = guarded_import
        with tempfile.TemporaryDirectory(prefix='ov-private-credential-check-') as temporary:
            home = Path(temporary).resolve()
            config.CONFIG_DIR, config.CONFIG_FILE = home, home / 'config.json'
            os.environ['OPENVEGAS_INTEGRATION_PROFILE'] = str(home)
            config.save_session('synthetic-access', 'synthetic-refresh')
            assert config.get_session()['refresh_token'] == 'synthetic-refresh'
            assert config.CONFIG_FILE.stat().st_mode & 0o777 == 0o600
            config.clear_session()
            config.save_config({'session': {'refresh_storage': 'platform_credential_store'}})
            assert config.get_session()['refresh_token'] == ''
            assert not config.platform_keychain_available()
            assert config.load_refresh_from_platform_store() == ''
            config.clear_persisted_refresh_token()
            for store in (Credentials('https://example.invalid'), game_credentials('https://example.invalid')):
                assert store.load() is None
                store.save('synthetic-device')
                assert store.path.stat().st_mode & 0o777 == 0o600
                assert store.load_noninteractive() == 'synthetic-device'
                store.clear()
                assert store.load() is None
            return {'private_files': True, 'keychain_calls': 0, 'synthetic_only': True}
    finally:
        builtins.__import__ = original_import
        config.CONFIG_DIR, config.CONFIG_FILE = previous[:2]
        if previous[2] is None:
            os.environ.pop('OPENVEGAS_INTEGRATION_PROFILE', None)
        else:
            os.environ['OPENVEGAS_INTEGRATION_PROFILE'] = previous[2]


def verify_bundle(*, include_review_packs=False):
    os.environ["OPENVEGAS_DOTENV_OVERRIDE"] = "0"
    import numpy
    import sounddevice

    from openvegas.emotes.resources import PackRepository

    keychain_frameworks_imported = False
    credential_storage_check = None
    keyring_backend = 'private_file'
    if sys.platform == "darwin":
        credential_storage_check = verify_mac_credential_files()
    else:
        import keyring
        backend = keyring.get_keyring()
        keyring_backend = type(backend).__module__ + "." + type(backend).__name__

    repository = PackRepository()
    names = repository.names()
    verify_pack_inventory(names, include_review_packs=include_review_packs)
    for name in names:
        repository.load(name)
    terminal_profile = None
    if sys.platform == 'darwin':
        from pathlib import Path
        from openvegas.integrations.terminal_profile import profile_spec
        terminal_profile = profile_spec(Path.home() / 'native-terminal-probe')
    report = {
        "frozen": bool(getattr(sys, "frozen", False)),
        "packs": names,
        "private_review_packs": sorted(REVIEW_PACKS) if include_review_packs else [],
        "publication_approved": False,
        "numpy": numpy.__version__,
        "portaudio_loaded": bool(sounddevice.get_portaudio_version()[0]),
        "keyring_backend": keyring_backend,
        "keychain_frameworks_imported": keychain_frameworks_imported,
        "credential_storage_check": credential_storage_check,
        "native_plugin_assets": verify_integration_assets(),
        "terminal_profile": terminal_profile,
        "scope": "bundle dependency check, not microphone/credential/native UX approval",
    }
    if not report["frozen"] or not report["portaudio_loaded"]:
        raise ValueError("Incomplete frozen runtime")
    print(json.dumps(report, sort_keys=True))


if __name__ == "__main__":
    if sys.argv[1:3] == ['integration', 'serve'] and '--billing-hook' in sys.argv[3:]:
        # Five-second host hooks must not import the full chat/audio CLI or MCP
        # server merely to notify the local terminal supervisor.
        import argparse
        parser = argparse.ArgumentParser()
        parser.add_argument('--host', required=True, choices=('claude', 'codex'))
        parser.add_argument('--backend')
        parser.add_argument('--profile')
        parser.add_argument('--billing-hook', action='store_true')
        args = parser.parse_args(sys.argv[3:])
        from openvegas.integrations.use import hook_main

        hook_main(args.host)
    elif len(sys.argv) == 3 and sys.argv[1] == '--_render-terminal-game':
        from openvegas.integrations.terminal_handoff import render_main

        render_main(int(sys.argv[2]))
    elif sys.argv[1:2] == ['--_terminal-relay']:
        from openvegas.integrations.terminal_relay import main

        main(sys.argv[2:])
    elif sys.argv[1:] in (["--verify-bundle"], ["--verify-bundle", "--include-review-packs"]):
        try:
            verify_bundle(include_review_packs="--include-review-packs" in sys.argv)
        except Exception as exc:  # noqa: BLE001 - keep dependency errors credential-free
            print(json.dumps({"status": "failed", "error": type(exc).__name__}))
            raise SystemExit(1) from None
    else:
        from openvegas.cli import cli

        cli()
