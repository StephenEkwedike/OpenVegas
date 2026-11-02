"""Credential-store policy. OpenVegas never accesses macOS Keychain."""
import sys


def private_login_files():
    return sys.platform == "darwin"


def keychain_call(operation, *args, interactive=True):
    if private_login_files():
        # Fail before importing a bridge or running any operation, even if a
        # future caller accidentally asks for interactive access.
        raise RuntimeError("OpenVegas does not use macOS Keychain. Reconnect in your browser.")
    return operation(*args)
