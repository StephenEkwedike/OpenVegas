"""Backend-bound integration credentials; no secrets in plugin manifests."""
from __future__ import annotations

import hashlib
import json
import os
import shlex
import stat
import sys
from pathlib import Path
from urllib.parse import urlsplit
from openvegas.keychain_access import keychain_call


class IntegrationError(Exception):
    def __init__(self, message, status=None):
        super().__init__(message)
        self.status = status


def backend_url() -> str:
    from openvegas.config import get_backend_url
    url = get_backend_url().rstrip("/")
    parsed = urlsplit(url)
    if (parsed.username or parsed.password or parsed.query or parsed.fragment or parsed.path
            or not parsed.hostname or (parsed.scheme != "https" and not (
                parsed.scheme == "http" and parsed.hostname in {"localhost", "127.0.0.1", "::1"}))):
        raise IntegrationError("Integrations require an HTTPS backend (HTTP is allowed only on loopback).")
    return url


def profile() -> Path:
    from openvegas.config import CONFIG_DIR
    return Path(os.environ.get("OPENVEGAS_INTEGRATION_PROFILE", str(CONFIG_DIR))).expanduser().absolute()


def game_login_command() -> str:
    """Use the same account profile as this helper, not a different global CLI."""
    from openvegas.config import CONFIG_DIR
    root = Path(__file__).resolve().parents[2]
    if (CONFIG_DIR.resolve() == root / ".local/home/.openvegas"
            and urlsplit(backend_url()).hostname in {"localhost", "127.0.0.1", "::1"}
            and (root / "dev/openvegas").is_file()):
        return shlex.join([str(root / "dev/openvegas"), "login"])
    return shlex.join([sys.executable if getattr(sys, "frozen", False) else "openvegas", "login"])


class Credentials:
    def __init__(self, backend: str):
        self.identity = hashlib.sha256((str(profile()) + "\n" + backend).encode()).hexdigest()
        self.path = profile() / "integrations" / (self.identity + ".credential")

    def _directory(self):
        # Refuse symlinked credential directories rather than following a project-controlled path.
        for ancestor in [self.path.parent, *self.path.parent.parents]:
            if ancestor.is_symlink():
                raise IntegrationError("Integration credential directory must not contain symlinks.")
        self.path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        if os.name == "posix":
            info = self.path.parent.stat()
            if info.st_uid != os.getuid():
                raise IntegrationError("Integration credential directory has a different owner.")
            os.chmod(self.path.parent, 0o700)

    def load(self) -> str | None:
        # A file exists only after explicit file-storage consent. Do not unlock
        # an unrelated/older Keychain item before validating that selected store.
        if self.path.exists() or self.path.is_symlink():
            return self._load_file()
        try:
            import keyring
            value = keychain_call(keyring.get_password, "openvegas-integrations", self.identity)
            if value:
                return value
        except Exception:  # noqa: BLE001,S110 - third-party keyring backends vary; never log secrets.
            pass
        return None

    def _load_file(self) -> str | None:
        if os.name != "posix":
            raise IntegrationError("An OS credential store is required on Windows.")
        self._directory()
        fd = os.open(self.path, os.O_RDONLY | os.O_NOFOLLOW)
        try:
            info = os.fstat(fd)
            if (not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid()
                    or info.st_mode & 0o077 or info.st_size > 512):
                raise IntegrationError("Integration credential file permissions are unsafe.")
            with os.fdopen(fd, "r", closefd=False) as stream:
                return json.load(stream)["token"]
        finally:
            os.close(fd)

    def load_noninteractive(self) -> str | None:
        """HUD polling must never open macOS authorization dialogs."""
        if self.path.exists() or self.path.is_symlink():
            return self._load_file()
        if sys.platform != "darwin":
            return self.load()
        try:
            import keyring
            return keychain_call(keyring.get_password, "openvegas-integrations", self.identity,
                                 interactive=False)
        except Exception:  # No interactive retry or plaintext migration on a denied read.
            return None

    def save(self, token: str, *, allow_file=False):
        self._directory()
        try:
            import keyring
            keychain_call(keyring.set_password, "openvegas-integrations", self.identity, token)
            if keychain_call(keyring.get_password, "openvegas-integrations", self.identity) != token:
                raise RuntimeError
            self.path.unlink(missing_ok=True)
            return "OS credential store"
        except Exception:  # noqa: BLE001 - storage backends can raise platform-specific errors.
            if not allow_file or os.name != "posix":
                raise IntegrationError("OS credential storage is unavailable. On POSIX only, rerun with "
                                       "--allow-file-credential to accept a local 0600 credential file.") from None
        # Exclusive replacement avoids following symlinks or exposing a partial credential.
        import secrets
        tmp = self.path.with_name(self.path.name + "." + secrets.token_hex(8))
        fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
        try:
            with os.fdopen(fd, "w") as stream:
                json.dump({"token": token}, stream)
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(tmp, self.path)
        finally:
            tmp.unlink(missing_ok=True)
        return "permission-restricted local file (0600)"

    def clear(self):
        try:
            import keyring
            keychain_call(keyring.delete_password, "openvegas-integrations", self.identity)
        except Exception:  # noqa: BLE001,S110 - absent credentials are already cleared.
            pass
        self.path.unlink(missing_ok=True)
