"""Pixel-preserving PNG output; animation owns only a standalone preview screen."""

import base64
import io
import os
from dataclasses import dataclass

from PIL import Image


def image_display_enabled(console):
    return (
        console.is_terminal
        and os.getenv("OPENVEGAS_TERMINAL_IMAGES", "0").lower() in {"1", "true", "on"}
        and os.getenv("TERM_PROGRAM", "") in {"vscode", "iTerm.app", "WezTerm"}
        and not os.getenv("TMUX") and not os.getenv("STY")
        and os.getenv("TERM", "") != "dumb"
        and "NO_COLOR" not in os.environ
    )


@dataclass(frozen=True)
class PixelFrame:
    data: str

    @property
    def byte_size(self):
        return len(self.data) * 3 // 4 - (len(self.data) - len(self.data.rstrip("=")))

    @classmethod
    def from_image(cls, frame):
        if frame is None:
            return cls("")
        if not 1 <= max(frame.size) <= 512:
            raise ValueError("Frame exceeds image display limit")
        # Integer enlargement keeps the authored pixel edges crisp. Unlike the
        # text renderer, no source pixels are discarded to fit terminal cells.
        scale = min(4, 512 // max(frame.size))
        with frame.resize((frame.width * scale, frame.height * scale), Image.Resampling.NEAREST) as enlarged:
            with io.BytesIO() as output:
                enlarged.save(output, format="PNG")
                return cls(base64.b64encode(output.getvalue()).decode("ascii"))

def print_static_frame(console, frame):
    """Print once in normal scrollback, without clearing or taking over input."""
    columns, rows = console.size
    if not frame.data:
        return
    console.file.write(
        f"\x1b]1337;File=inline=1;size={frame.byte_size};width=auto;"
        f"height={max(1, rows - 4)};preserveAspectRatio=1:{frame.data}\x07\r\n"
    )
    console.file.flush()


def preview_images_enabled(console):
    # This sizing/cleanup path is tested against VS Code's xterm image addon.
    # Do not infer equivalent animation semantics for every image-capable host.
    return image_display_enabled(console) and os.getenv("TERM_PROGRAM") == "vscode"


class PreviewImageDisplay:
    """Never used by chat or watch; restore the original screen on every exit."""

    def __init__(self, renderable, *, console, **kwargs):
        self.console = console
        self.frame = renderable
        self._last = None

    def __enter__(self):
        try:
            self.console.file.write("\x1b[?1049h\x1b[?25l")
            self.update(self.frame, refresh=True)
        except BaseException:
            self.__exit__(None, None, None)
            raise
        return self

    def update(self, frame, *, refresh=False):
        if not refresh:
            return
        columns, rows = self.console.size
        signature = (frame.data, columns, rows)
        if self._last == signature:
            return
        # xterm IIP ignores height when width + preserveAspectRatio are set.
        # Auto/auto fits the image to the viewport with one spare terminal row.
        # Start at row 1, not after a header, and never emit scrolling newlines.
        data = "\x1b[H\x1b[2J"
        if frame.data and rows >= 3 and columns >= 3:
            data += f"\x1b]1337;File=inline=1;size={frame.byte_size};width=auto;height=auto;preserveAspectRatio=1:" + frame.data + "\x07"
        label = "Preview | Ctrl+C exits" if rows >= 3 else "Enlarge terminal"
        data += f"\x1b[{max(1, rows)};1H" + label[:max(0, columns - 1)] + "\x1b[H"
        self.console.file.write(data)
        self.console.file.flush()
        self._last = signature

    def __exit__(self, *args):
        try:
            self.console.file.write("\x1b[?25h\x1b[?1049l")
        finally:
            self.console.file.flush()


