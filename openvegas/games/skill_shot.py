"""Skill Shot Timing Bar — pure arcade game."""

from __future__ import annotations

import asyncio
import sys
import select
from decimal import Decimal

from rich.console import Console
from rich.live import Live
from rich.text import Text

from openvegas.games.base import BaseGame, GameResult
from openvegas.rng.provably_fair import ProvablyFairRNG
from openvegas.tui.theme import ascii_safe_mode, render_mode
from openvegas.tui.banners import result_banner

BAR_WIDTHS = {"compact": 40, "standard": 50, "cinematic": 70}


def _render_bar(
    bar_width: int, position: int, ascii_safe: bool,
    green_zone: list | None = None, gold_zone: list | None = None,
) -> str:
    """Render the skill shot bar. Zones only shown if provided (post-result)."""
    chars = []
    empty = "." if ascii_safe else "░"
    cursor_char = "V" if ascii_safe else "▼"

    for i in range(bar_width):
        if i == position:
            chars.append(f"[bold white on red]{cursor_char}[/bold white on red]")
        elif gold_zone and gold_zone[0] <= i < gold_zone[1]:
            if ascii_safe:
                chars.append("#")
            else:
                chars.append("[on yellow] [/on yellow]")
        elif green_zone and green_zone[0] <= i < green_zone[1]:
            if ascii_safe:
                chars.append("=")
            else:
                chars.append("[on green] [/on green]")
        else:
            chars.append(empty)
    return "".join(chars)


class SkillShotGame(BaseGame):
    name = "skill_shot"
    rtp = Decimal("0.94")

    BAR_WIDTH = 40
    GREEN_ZONE_SIZE = 6
    GOLD_ZONE_SIZE = 2

    async def validate_bet(self, bet: dict) -> bool:
        return bet.get("amount", 0) > 0

    async def resolve(
        self, bet: dict, rng: ProvablyFairRNG, client_seed: str, nonce: int
    ) -> GameResult:
        bet_amount = Decimal(str(bet["amount"]))

        green_start = rng.generate_outcome(
            client_seed, nonce, self.BAR_WIDTH - self.GREEN_ZONE_SIZE
        )
        green_end = green_start + self.GREEN_ZONE_SIZE
        gold_start = green_start + (self.GREEN_ZONE_SIZE - self.GOLD_ZONE_SIZE) // 2
        gold_end = gold_start + self.GOLD_ZONE_SIZE

        stop_pos = bet.get("stop_position", self.BAR_WIDTH // 2)

        if gold_start <= stop_pos < gold_end:
            multiplier = Decimal("5.0")
        elif green_start <= stop_pos < green_end:
            multiplier = Decimal("2.0")
        else:
            multiplier = Decimal("0.0")

        payout = (bet_amount * multiplier).quantize(Decimal("0.01"))

        return GameResult(
            game_id=bet["game_id"],
            player_id=bet["player_id"],
            bet_amount=bet_amount,
            payout=payout,
            net=payout - bet_amount,
            outcome_data={
                "green_zone": [green_start, green_end],
                "gold_zone": [gold_start, gold_end],
                "stop_position": stop_pos,
                "multiplier": str(multiplier),
            },
            server_seed=rng.reveal(),
            server_seed_hash=rng.server_seed_hash,
            client_seed=client_seed,
            nonce=nonce,
            provably_fair=True,
        )

    async def render_interactive(self, console: Console) -> int:
        """Animate the moving cursor; return position where user stopped."""
        ascii_safe = ascii_safe_mode()
        mode = render_mode()
        bar_width = BAR_WIDTHS.get(mode, 50)

        console.print("[bold]Press ENTER to stop the cursor![/bold]\n")
        position = 0
        direction = 1
        speed = 0.03

        with Live(console=console, refresh_per_second=30) as live:
            for _ in range(500):
                bar = _render_bar(bar_width, position, ascii_safe)
                live.update(Text.from_markup(bar))

                position += direction
                if position >= bar_width - 1 or position <= 0:
                    direction *= -1

                await asyncio.sleep(speed)
                speed *= 0.999

                if sys.stdin in select.select([sys.stdin], [], [], 0)[0]:
                    sys.stdin.readline()
                    return position

        return position

    async def render(self, result: GameResult, console: Console, opts=None):
        """Render result with zones revealed from the same seeded positions."""
        ascii_safe = ascii_safe_mode()
        mode = render_mode()
        bar_width = BAR_WIDTHS.get(mode, 50)

        green = result.outcome_data["green_zone"]
        gold = result.outcome_data["gold_zone"]
        stop = result.outcome_data["stop_position"]
        multiplier = result.outcome_data["multiplier"]

        # Zones come from resolve() seeded RNG — render reads them, never recomputes
        bar = _render_bar(bar_width, stop, ascii_safe, green_zone=green, gold_zone=gold)
        console.print(Text.from_markup(bar))

        payout = result.payout
        if payout > 0:
            console.print(result_banner([
                f"Multiplier: {multiplier}x",
                f"Won {payout} $V!",
            ]))
        else:
            console.print(f"\n[red]Missed! Lost {result.bet_amount} $V.[/red]")

    async def render_async(self, result: GameResult, console: Console, opts=None):
        await self.render(result, console, opts)
