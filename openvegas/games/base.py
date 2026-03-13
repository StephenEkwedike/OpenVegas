"""Base game interface and shared types."""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass
from decimal import Decimal

from openvegas.rng.provably_fair import ProvablyFairRNG


@dataclass
class GameResult:
    game_id: str
    player_id: str
    bet_amount: Decimal
    payout: Decimal
    net: Decimal
    outcome_data: dict
    server_seed: str
    server_seed_hash: str
    client_seed: str
    nonce: int
    provably_fair: bool = True


class BaseGame(ABC):
    """Interface all OpenVegas games implement.

    Renderer contract guidance:
    - Preferred for new games: expose `render_async(result, console, opts)`.
    - Backward compatible: keep `render(result, console)` (or `render(..., opts)`).
    """

    name: str
    rtp: Decimal

    @abstractmethod
    async def validate_bet(self, bet: dict) -> bool:
        ...

    @abstractmethod
    async def resolve(
        self, bet: dict, rng: ProvablyFairRNG, client_seed: str, nonce: int
    ) -> GameResult:
        ...

    @abstractmethod
    async def render(self, result: GameResult, console) -> None:
        ...
