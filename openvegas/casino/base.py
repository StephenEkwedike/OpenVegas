"""Casino game interface — multi-action rounds with state machines."""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from decimal import Decimal

from openvegas.rng.provably_fair import ProvablyFairRNG


@dataclass
class CasinoRoundState:
    round_id: str
    game_code: str
    wager_v: Decimal
    state: dict = field(default_factory=dict)
    actions_taken: list = field(default_factory=list)
    resolved: bool = False
    payout_multiplier: Decimal = Decimal("0")
    outcome_data: dict = field(default_factory=dict)


class BaseCasinoGame(ABC):
    game_code: str
    rtp: Decimal

    @abstractmethod
    def initial_state(self, rng: ProvablyFairRNG, client_seed: str, nonce: int) -> dict:
        """Set up initial game state (deal cards, etc.)."""
        ...

    @abstractmethod
    def apply_action(
        self, state: dict, action: str, payload: dict,
        rng: ProvablyFairRNG, client_seed: str, nonce: int
    ) -> dict:
        """Apply player action, return updated state."""
        ...

    @abstractmethod
    def resolve(self, state: dict) -> tuple[Decimal, dict]:
        """Resolve final outcome. Returns (payout_multiplier, outcome_data)."""
        ...

    @abstractmethod
    def valid_actions(self, state: dict) -> list[str]:
        """Return list of valid actions for current state. Empty = must resolve."""
        ...

    @abstractmethod
    def is_resolved(self, state: dict) -> bool:
        """True if the round has concluded and needs resolution."""
        ...
