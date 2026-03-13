"""Horse Racing — the flagship game."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from decimal import Decimal

from rich.console import Console
from rich.live import Live
from rich.table import Table

from openvegas.games.base import BaseGame, GameResult
from openvegas.rng.provably_fair import ProvablyFairRNG
from openvegas.tui.theme import ascii_safe_mode, render_mode, ANIM
from openvegas.tui.banners import result_banner


@dataclass
class Horse:
    name: str
    number: int
    odds: Decimal
    position: float = 0.0
    speed_base: float = 0.0
    stamina: float = 1.0
    finished: bool = False


HORSE_NAMES = [
    "Thunder Byte", "Null Pointer", "Stack Overflow", "Segfault Sally",
    "Cache Miss", "Race Condition", "Deadlock Dan", "Heap Corruption",
    "Buffer Blitz", "Async Awaiter",
]

HORSE_COLORS = ["red", "blue", "green", "yellow", "magenta", "cyan", "white", "bright_red"]

TRACK_WIDTHS = {"compact": 60, "standard": 80, "cinematic": 100}

# Resolve still uses a fixed internal length for game logic (not display)
TRACK_LENGTH = 60
RACE_DURATION_SEC = 30
BASE_SPEED_MIN = 0.90
BASE_SPEED_SPAN = 0.80
NOISE_MIN = 0.78
NOISE_SPAN = 0.44
STAMINA_DECAY_MIN = 0.9964
STAMINA_DECAY_MAX = 0.9989
BURST_CHANCE_PER_1000 = 32
BURST_MULTIPLIER = 1.22
BURST_COOLDOWN_TICKS = 6
CHECKPOINT_INTERVAL = 2
FINAL_RANK_OFFSET = 1.5
RACE_MODEL_VERSION = "horse-v2-balanced"


def _horse_sprite(index: int, ascii_safe: bool) -> str:
    """Left-facing horse sprite. < before glyph locks head direction."""
    color = HORSE_COLORS[index % len(HORSE_COLORS)]
    glyph = "H" if ascii_safe else "𓃗"
    return f"[bold white]<[/bold white][bold {color}]{glyph}[/bold {color}]"


def _render_lane(pos: int, track_width: int, horse_index: int, ascii_safe: bool) -> str:
    """Render one lane: finish line + empty + horse sprite + colored trail (right-to-left)."""
    pos = max(0, min(track_width - 2, pos))
    color = HORSE_COLORS[horse_index % len(HORSE_COLORS)]
    trail_char = "=" if ascii_safe else "█"
    empty_char = "." if ascii_safe else "░"
    finish = "|" if ascii_safe else "[bold white]║[/bold white]"

    remaining = track_width - pos - 2
    empty = empty_char * max(0, remaining)
    sprite = _horse_sprite(horse_index, ascii_safe)
    trail = f"[{color}]{trail_char * pos}[/{color}]"
    return f"{finish}{empty}{sprite}{trail}"


def _normalize_checkpoints(raw_checkpoints: object) -> list[dict[int, float]]:
    """Normalize checkpoint keys/values after JSON serialization round-trips."""
    if not isinstance(raw_checkpoints, list):
        return []

    normalized: list[dict[int, float]] = []
    for cp in raw_checkpoints:
        if not isinstance(cp, dict):
            continue
        parsed: dict[int, float] = {}
        for key, value in cp.items():
            try:
                parsed[int(key)] = float(value)
            except (TypeError, ValueError):
                continue
        if parsed:
            normalized.append(parsed)
    return normalized


class HorseRacing(BaseGame):
    name = "horse_racing"
    rtp = Decimal("0.95")

    def __init__(self, num_horses: int = 8, render_duration_sec: float | None = None):
        self.num_horses = min(num_horses, len(HORSE_NAMES))
        self.horses: list[Horse] = []
        self.render_duration_sec = render_duration_sec

    async def validate_bet(self, bet: dict) -> bool:
        horse = bet.get("horse", 0)
        bet_type = bet.get("type", "win")
        amount = bet.get("amount", 0)
        return (
            1 <= horse <= self.num_horses
            and bet_type in ("win", "place", "show")
            and amount > 0
        )

    def setup_race(self, rng: ProvablyFairRNG, client_seed: str, nonce: int):
        self.horses = []
        for i in range(self.num_horses):
            speed_val = rng.generate_outcome(client_seed, nonce + i, 1000)
            base_speed = BASE_SPEED_MIN + (speed_val / 1000) * BASE_SPEED_SPAN
            raw_odds = Decimal("2.0") + Decimal(str((1000 - speed_val) / 200))

            self.horses.append(Horse(
                name=HORSE_NAMES[i],
                number=i + 1,
                odds=raw_odds.quantize(Decimal("0.1")),
                speed_base=base_speed,
            ))

    async def resolve(
        self, bet: dict, rng: ProvablyFairRNG, client_seed: str, nonce: int
    ) -> GameResult:
        self.setup_race(rng, client_seed, nonce)

        tick = 0
        finish_order: list[Horse] = []
        checkpoints: list[dict[int, float]] = []
        stamina_decay: dict[int, float] = {}
        burst_cooldown: dict[int, int] = {}

        for horse in self.horses:
            profile_roll = rng.generate_outcome(
                client_seed, nonce + 50_000 + horse.number, 1000
            )
            stamina_decay[horse.number] = STAMINA_DECAY_MIN + (
                profile_roll / 1000
            ) * (STAMINA_DECAY_MAX - STAMINA_DECAY_MIN)
            burst_cooldown[horse.number] = 0

        while len(finish_order) < self.num_horses:
            tick += 1
            for horse in self.horses:
                if horse.finished:
                    continue

                noise_roll = rng.generate_outcome(
                    client_seed, nonce + 1_000 + tick * self.num_horses + horse.number, 1000
                )
                noise_factor = NOISE_MIN + (noise_roll / 1000) * NOISE_SPAN

                burst_factor = 1.0
                if burst_cooldown[horse.number] > 0:
                    burst_cooldown[horse.number] -= 1
                else:
                    burst_roll = rng.generate_outcome(
                        client_seed,
                        nonce + 200_000 + tick * self.num_horses + horse.number,
                        1000,
                    )
                    if burst_roll < BURST_CHANCE_PER_1000:
                        burst_factor = BURST_MULTIPLIER
                        burst_cooldown[horse.number] = BURST_COOLDOWN_TICKS

                speed = horse.speed_base * horse.stamina * noise_factor * burst_factor
                horse.stamina *= stamina_decay[horse.number]
                horse.position += speed

                if horse.position >= TRACK_LENGTH:
                    horse.finished = True
                    finish_order.append(horse)

            # Sample checkpoint (unclamped — preserve real positions for ordering)
            if tick % CHECKPOINT_INTERVAL == 0 or len(finish_order) == self.num_horses:
                checkpoints.append({
                    h.number: h.position for h in self.horses
                })

        finish_order_nums = [h.number for h in finish_order]

        # Apply rank offsets to final checkpoint so visual order matches finish_order_nums
        # even when multiple horses cross the line in the same tick.
        # Use sub-TRACK_LENGTH values so they survive the (pos / TRACK_LENGTH) scaling
        # in render() without being clamped to the same pixel.
        final = checkpoints[-1]
        for rank, num in enumerate(finish_order_nums):
            final[num] = TRACK_LENGTH - rank * FINAL_RANK_OFFSET

        winner = finish_order[0]
        bet_type = bet["type"]
        bet_horse = bet["horse"]
        bet_amount = Decimal(str(bet["amount"]))

        payout = Decimal("0")
        if bet_type == "win" and bet_horse == winner.number:
            payout = bet_amount * winner.odds
        elif bet_type == "place" and bet_horse in [h.number for h in finish_order[:2]]:
            placed = next(h for h in self.horses if h.number == bet_horse)
            payout = bet_amount * (placed.odds / 2)
        elif bet_type == "show" and bet_horse in [h.number for h in finish_order[:3]]:
            showed = next(h for h in self.horses if h.number == bet_horse)
            payout = bet_amount * (showed.odds / 3)

        return GameResult(
            game_id=bet["game_id"],
            player_id=bet["player_id"],
            bet_amount=bet_amount,
            payout=payout.quantize(Decimal("0.01")),
            net=(payout - bet_amount).quantize(Decimal("0.01")),
            outcome_data={
                "finish_order": [h.name for h in finish_order],
                "finish_order_nums": finish_order_nums,
                "winner": winner.name,
                "bet_type": bet_type,
                "bet_horse": bet_horse,
                "horses": [
                    {"number": h.number, "name": h.name, "odds": str(h.odds)}
                    for h in self.horses
                ],
                "checkpoints": checkpoints,
                "race_model_version": RACE_MODEL_VERSION,
            },
            server_seed=rng.reveal(),
            server_seed_hash=rng.server_seed_hash,
            client_seed=client_seed,
            nonce=nonce,
            provably_fair=True,
        )

    async def render(self, result: GameResult, console: Console, opts=None):
        ascii_safe = ascii_safe_mode()
        mode = render_mode()
        track_width = TRACK_WIDTHS.get(mode, 80)
        duration = self.render_duration_sec if self.render_duration_sec is not None else RACE_DURATION_SEC
        num_frames = max(1, int(duration / ANIM["frame_delay"]))

        horses_data = result.outcome_data["horses"]
        checkpoints = _normalize_checkpoints(result.outcome_data.get("checkpoints"))
        replay_ready = bool(checkpoints) and all(
            all(h["number"] in cp for h in horses_data) for cp in checkpoints
        )
        num_checkpoints = len(checkpoints)
        speed_map: dict[int, float] = {}
        if not replay_ready:
            # Legacy fallback for historical payloads that predate checkpoints.
            for h in horses_data:
                odds = float(h["odds"])
                speed_map[h["number"]] = max(0.3, 2.0 - (odds - 2.0) * 0.2)

        # Race header
        if not ascii_safe:
            console.print(f"\n[bold cyan]    ★ OPENVEGAS DERBY ★[/bold cyan]\n")
        else:
            console.print(f"\n    * OPENVEGAS DERBY *\n")

        with Live(console=console, refresh_per_second=15) as live:
            for frame in range(num_frames):
                progress = frame / max(num_frames - 1, 1)
                cp_lo = cp_hi = 0
                t = 0.0
                if replay_ready:
                    # Map frame to checkpoint pair.
                    cp_pos = progress * (num_checkpoints - 1)
                    cp_lo = int(cp_pos)
                    cp_hi = min(cp_lo + 1, num_checkpoints - 1)
                    t = cp_pos - cp_lo  # interpolation factor 0..1

                table = Table(show_header=False, box=None, padding=(0, 0))
                table.add_column(width=16)
                table.add_column(width=track_width + 5)
                table.add_column(width=6, justify="right")

                for idx, h in enumerate(horses_data):
                    num = h["number"]
                    if replay_ready:
                        # Lerp between two checkpoints from resolve-time simulation.
                        pos_lo = checkpoints[cp_lo][num]
                        pos_hi = checkpoints[cp_hi][num]
                        raw_pos = pos_lo + (pos_hi - pos_lo) * t
                        scaled = (raw_pos / TRACK_LENGTH) * (track_width - 2)
                        pos = int(max(0, min(track_width - 2, scaled)))
                    else:
                        target = min(
                            track_width - 2,
                            progress * track_width * (speed_map[num] / 1.5),
                        )
                        pos = int(target)

                    lane = _render_lane(pos, track_width, idx, ascii_safe)
                    color = HORSE_COLORS[idx % len(HORSE_COLORS)]
                    label = f"[bold {color}]#{num}[/bold {color}] {h['name'][:10]}"
                    odds_str = f"[dim]{h['odds']}x[/dim]"
                    table.add_row(label, lane, odds_str)

                live.update(table)
                await asyncio.sleep(ANIM["frame_delay"])

        # Results
        winner_name = result.outcome_data["winner"]
        net = result.net
        payout = result.payout
        bet_amount = result.bet_amount

        if net > 0:
            banner_lines = [
                f"WINNER: {winner_name}",
                f"Payout: {payout} $V (+{net} net)",
            ]
            console.print(result_banner(banner_lines))
        elif net == 0:
            console.print(f"\n[yellow]Push — {winner_name} won.[/yellow]")
        else:
            console.print(f"\n[red]{winner_name} won. You lost {bet_amount} $V.[/red]")

    async def render_async(self, result: GameResult, console: Console, opts=None):
        if opts is not None and getattr(opts, "duration_sec", None) is not None:
            self.render_duration_sec = float(opts.duration_sec)
        await self.render(result, console, opts)
