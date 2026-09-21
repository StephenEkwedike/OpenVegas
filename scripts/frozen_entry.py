"""Frozen CLI entry point; bundle verification never opens a microphone or account."""

import json
import os
import sys


def verify_bundle():
    os.environ["OPENVEGAS_DOTENV_OVERRIDE"] = "0"
    import keyring
    import numpy
    import sounddevice

    from openvegas.emotes.resources import PackRepository

    repository = PackRepository()
    names = repository.names()
    if len(names) != 6:
        raise ValueError("Expected six preview packs")
    for name in names:
        repository.load(name)
    backend = keyring.get_keyring()
    report = {
        "frozen": bool(getattr(sys, "frozen", False)),
        "packs": names,
        "numpy": numpy.__version__,
        "portaudio_loaded": bool(sounddevice.get_portaudio_version()[0]),
        "keyring_backend": type(backend).__module__ + "." + type(backend).__name__,
        "scope": "bundle dependency check, not microphone/biometric/native UX approval",
    }
    if not report["frozen"] or not report["portaudio_loaded"]:
        raise ValueError("Incomplete frozen runtime")
    print(json.dumps(report, sort_keys=True))


if __name__ == "__main__":
    if sys.argv[1:] == ["--verify-bundle"]:
        try:
            verify_bundle()
        except Exception as exc:  # noqa: BLE001 - keep dependency errors credential-free
            print(json.dumps({"status": "failed", "error": type(exc).__name__}))
            raise SystemExit(1) from None
    else:
        from openvegas.cli import cli

        cli()
