"""Nonblocking native-terminal replay of the existing horse engine checkpoints."""
from __future__ import annotations

import math

from rich.console import Group
from rich.table import Table
from rich.text import Text

from openvegas.games.horse_racing import (
    HORSE_COLORS,
    HORSE_NAMES,
    TRACK_LENGTH,
    _normalize_checkpoints,
    _render_lane,
)
from openvegas.integrations.game_table import label
from openvegas.tui.theme import ascii_safe_mode

HORSE_REPLAY_SECONDS = 8.0


def race_positions(state, progress):
    horses = state.get('horses') or [{'number': n + 1, 'name': name} for n, name in enumerate(HORSE_NAMES[:8])]
    checkpoints = _normalize_checkpoints(state.get('checkpoints'))
    numbers = [h['number'] for h in horses[:8]]
    if not checkpoints or not all(all(n in cp and math.isfinite(cp[n]) for n in numbers) for cp in checkpoints):
        return {n: 0.0 for n in numbers}
    # Include the starting gate, then interpolate the engine's actual positions.
    checkpoints = [{n: 0.0 for n in numbers}, *checkpoints]
    cursor = max(0.0, min(1.0, progress)) * (len(checkpoints) - 1)
    low = int(cursor)
    high = min(low + 1, len(checkpoints) - 1)
    return {n: checkpoints[low][n] + (checkpoints[high][n] - checkpoints[low][n]) * (cursor - low)
            for n in numbers}


def horse_board(state, *, width, progress, settled):
    horses = state.get('horses') or [{'number': n + 1, 'name': name} for n, name in enumerate(HORSE_NAMES[:8])]
    name_width = 15 if width >= 50 else 5
    track_width = max(5, width - name_width - 4)
    positions = race_positions(state, progress)
    grid = Table.grid(padding=0)
    grid.add_column(width=name_width, no_wrap=True)
    grid.add_column()
    for index, horse in enumerate(horses[:8]):
        number = horse['number']
        name = f"#{number} " + label(horse.get('name'), name_width - 3)
        position = int(max(0, min(track_width - 2, positions[number] / TRACK_LENGTH * (track_width - 2))))
        grid.add_row(Text(name, style='bold ' + HORSE_COLORS[index % len(HORSE_COLORS)]),
                     Text.from_markup(_render_lane(position, track_width, index, ascii_safe_mode())))
    notes = [Text('HORSE RACING / OPENVEGAS DERBY', style='bold #e6c486'),
             Text('Finish <                                      Start', style='#b9c3c8'), Text(''), grid]
    if settled and progress >= 1:
        notes.append(Text('Winner: ' + label(state.get('winner', 'see finish order')), style='bold'))
        notes.append(Text('Finish: ' + ' / '.join(label(n) for n in state.get('finish_order_nums', [])[:8])))
    else:
        notes.append(Text('Racing...' if settled else 'Choose a budget and horse to enter.'))
    return Group(*notes)
