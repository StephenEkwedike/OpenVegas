"""Synthetic release pins for isolated tests, never real asset provisioning."""

import hashlib
import json

from PIL import Image

from server.services import emote_delivery as delivery


def pin_delivery_release(root, monkeypatch):
    """Call after creating all fixture pack directories, before purchase/delivery."""
    entries = []
    for target in sorted(root.iterdir()):
        if not target.is_dir():
            continue
        raw = json.loads((target / "manifest.json").read_bytes())
        provenance = target / "provenance.json"
        if not provenance.exists():
            provenance.write_text(json.dumps({
                "source_sha256": hashlib.sha256(b"synthetic source").hexdigest(),
            }))
        files = {}
        for name in ("manifest.json", raw["sheet"], "provenance.json"):
            data = (target / name).read_bytes()
            files[name] = {"sha256": hashlib.sha256(data).hexdigest(), "bytes": len(data)}
        hashes = {
            "manifest_sha256": files["manifest.json"]["sha256"],
            "sheet_sha256": files[raw["sheet"]]["sha256"],
            "provenance_sha256": files["provenance.json"]["sha256"],
        }
        with Image.open(target / raw["sheet"]) as image:
            rgba = image.convert("RGBA")
            pixels = hashlib.sha256(str(rgba.size).encode() + rgba.tobytes()).hexdigest()
            rgba.close()
        entries.append({
            "pack_id": raw["pack_id"], "version": raw["version"],
            "slot": next(tag for tag in raw["tags"] if tag in {"companion", "completion"}),
            "delivery_resource": target.name, "license_id": raw["license_id"],
            "source_sha256": json.loads(provenance.read_bytes())["source_sha256"],
            "files": files, "pixels_sha256": pixels,
            "artwork_fingerprint": hashlib.sha256(
                (json.dumps(hashes, sort_keys=True, separators=(",", ":")) + "\n").encode()
            ).hexdigest(),
        })
    release = {
        "schema_version": 1, "release_id": "synthetic-fixture", "kind": "private-emote-candidate",
        "sale_enabled": False, "release_approval_required": True,
        "native_compatibility_verified": False, "packs": entries,
    }
    encoded = (json.dumps(release, sort_keys=True, separators=(",", ":")) + "\n").encode()
    (root / delivery.RELEASE_FILE).write_bytes(encoded)
    monkeypatch.setenv(delivery.RELEASE_PIN_ENV, hashlib.sha256(encoded).hexdigest())
    return release
