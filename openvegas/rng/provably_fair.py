"""Commit-reveal provably fair RNG scheme."""

import hashlib
import hmac
import secrets


class ProvablyFairRNG:
    """Commit-reveal scheme so users can verify game outcomes."""

    def __init__(self):
        self.server_seed: str = ""
        self.server_seed_hash: str = ""

    def new_round(self) -> str:
        """Generate server seed and return its hash (commitment)."""
        self.server_seed = secrets.token_hex(32)
        self.server_seed_hash = hashlib.sha256(
            self.server_seed.encode()
        ).hexdigest()
        return self.server_seed_hash

    def generate_outcome(self, client_seed: str, nonce: int, max_value: int) -> int:
        """
        Deterministic outcome from server_seed + client_seed + nonce.
        User can reproduce this after reveal.
        """
        message = f"{client_seed}:{nonce}"
        h = hmac.new(
            self.server_seed.encode(),
            message.encode(),
            hashlib.sha256,
        ).hexdigest()
        raw = int(h[:8], 16)
        return raw % max_value

    def reveal(self) -> str:
        """Reveal server seed so user can verify."""
        return self.server_seed

    @staticmethod
    def verify(server_seed: str, committed_hash: str) -> bool:
        """User-side verification that the seed matches the commitment."""
        return hashlib.sha256(server_seed.encode()).hexdigest() == committed_hash
