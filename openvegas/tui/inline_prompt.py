"""Keep the inline composer small without owning terminal scrollback."""

import os

from prompt_toolkit.filters import to_filter
from prompt_toolkit.layout import Dimension
from prompt_toolkit.layout.containers import Container
from prompt_toolkit.layout.screen import WritePosition


def mouse_actions_enabled(*, owned: bool = False) -> bool:
    # Clickable controls are the default. Terminals can bypass mouse reporting
    # with Shift for native selection/scrolling; an explicit opt-out remains.
    return os.getenv("OPENVEGAS_CHAT_MOUSE_ACTIONS", "1").strip().lower() in {
        "1", "true", "yes", "on",
    }


class _CompactContainer(Container):
    def __init__(self, content):
        self.content = content

    def reset(self):
        self.content.reset()

    def preferred_width(self, max_available_width):
        return self.content.preferred_width(max_available_width)

    def preferred_height(self, width, max_available_height):
        return self.content.preferred_height(width, max_available_height)

    def get_children(self):
        return [self.content]

    def write_to_screen(self, screen, mouse_handlers, write_position, parent_style, erase_bg, z_index):
        # The renderer offers at least the previous frame's height. Do not fill
        # that stale space: its normal screen diff will erase the old rows.
        height = min(write_position.height, self.preferred_height(
            write_position.width, write_position.height,
        ).preferred)
        self.content.write_to_screen(
            screen, mouse_handlers,
            WritePosition(write_position.xpos, write_position.ypos, write_position.width, height),
            parent_style, erase_bg, z_index,
        )


def _remember_inline_origin(app):
    renderer = app.renderer
    screen = renderer.last_rendered_screen
    if not renderer.full_screen and renderer._min_available_height > 0 and screen is not None:
        # Expanding at the bottom scrolls the physical origin upward. Erasing
        # rows on shrink does not move it back down. Retain that high-water
        # mark for prompt_toolkit's absolute mouse-coordinate translation,
        # independently of the compact layout's (smaller) visible height.
        renderer._min_available_height = min(
            app.output.get_size().rows,
            max(renderer._min_available_height, screen.height),
        )


def configure_inline_prompt(session):
    """Bound wrapped drafts, follow the cursor, and shrink on history changes."""
    if isinstance(session.layout.container, _CompactContainer):
        return
    for window in session.layout.find_all_windows():
        if getattr(window.content, "buffer", None) is session.default_buffer:
            window.height = Dimension(min=1, max=8)
            window.dont_extend_height = to_filter(True)
    session.layout.container = _CompactContainer(session.layout.container)
    session.app.after_render += _remember_inline_origin
