"""Serialize this process's Keychain access while a background read forbids UI."""
from threading import RLock
import sys

_lock = RLock()


def keychain_call(operation, *args, interactive=True):
    if sys.platform != "darwin":
        return operation(*args)
    with _lock:
        if interactive:
            return operation(*args)
        import Security
        status, allowed = Security.SecKeychainGetUserInteractionAllowed(None)
        if status != 0:
            raise RuntimeError("Keychain interaction state unavailable")
        if Security.SecKeychainSetUserInteractionAllowed(False) != 0:
            raise RuntimeError("Unable to suppress Keychain interaction")
        try:
            return operation(*args)
        finally:
            if Security.SecKeychainSetUserInteractionAllowed(allowed) != 0:
                raise RuntimeError("Unable to restore Keychain interaction state")
