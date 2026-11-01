"""Compact half-block QR renderer for terminal output."""

from __future__ import annotations

from openvegas.qr_runtime import ensure_qrcode_available


def _build_matrix(value: str, border: int) -> list[list[bool]]:
    ok, reason = ensure_qrcode_available()
    if not ok:
        raise RuntimeError(str(reason or "qrcode runtime unavailable"))
    import qrcode  # type: ignore

    qr = qrcode.QRCode(
        border=max(0, int(border)),
        box_size=1,
        error_correction=qrcode.constants.ERROR_CORRECT_L,
    )
    qr.add_data(str(value))
    qr.make(fit=True)
    return qr.get_matrix()


def qr_half_block(value: str, border: int = 1) -> str:
    """Render QR in half-block Unicode, roughly half the vertical size."""
    matrix = _build_matrix(value, border)
    lines: list[str] = []
    rows = len(matrix)
    cols = len(matrix[0]) if rows else 0
    for y in range(0, rows, 2):
        out: list[str] = []
        for x in range(cols):
            top = bool(matrix[y][x])
            bot = bool(matrix[y + 1][x]) if y + 1 < rows else False
            if top and bot:
                out.append("█")
            elif top:
                out.append("▀")
            elif bot:
                out.append("▄")
            else:
                out.append(" ")
        lines.append("".join(out))
    return "\n".join(lines)


def qr_width(value: str, border: int = 1) -> int:
    """Character width of rendered QR."""
    matrix = _build_matrix(value, border)
    return len(matrix[0]) if matrix else 0
