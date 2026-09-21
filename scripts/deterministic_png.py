"""Bounded, byte-reproducible RGBA8 PNG authoring without a native compressor.

Filter-zero scanlines and explicit stored DEFLATE blocks preserve every input
byte, including RGB under transparent pixels. Larger files are intentional:
Pillow/zlib and zlib-ng may compress identical artwork to different bytes.
"""

from __future__ import annotations

import binascii
import struct
from pathlib import Path
from typing import TYPE_CHECKING
from zlib import adler32

if TYPE_CHECKING:
    from PIL import Image


MAX_PNG_BYTES = 16 * 1024 * 1024
MAX_DIMENSION = 16_384
ENCODING = "rgba8-filter0-stored-deflate-v1"


def _chunk(kind: bytes, data: bytes | bytearray) -> bytes:
    checksum = binascii.crc32(data, binascii.crc32(kind)) & 0xFFFFFFFF
    return struct.pack(">I", len(data)) + kind + data + struct.pack(">I", checksum)


def encode_rgba_png(image: Image.Image, *, max_bytes: int = MAX_PNG_BYTES) -> bytes:
    if type(max_bytes) is not int or not 1 <= max_bytes <= MAX_PNG_BYTES:
        raise ValueError("PNG byte budget must be within 1..16777216")
    if image.mode != "RGBA":
        raise ValueError("Canonical PNG input must already be RGBA")
    width, height = image.size
    if any(type(n) is not int or not 1 <= n <= MAX_DIMENSION for n in (width, height)):
        raise ValueError("PNG dimensions must be within 1..16384")

    row_bytes = width * 4
    scanline_bytes = (row_bytes + 1) * height
    blocks = (scanline_bytes + 65_534) // 65_535
    expected_size = 57 + 2 + scanline_bytes + blocks * 5 + 4
    if expected_size > max_bytes:
        raise ValueError("Canonical PNG exceeds byte budget")

    pixels = image.tobytes()
    if len(pixels) != row_bytes * height:
        raise ValueError("Invalid RGBA byte length")
    scanlines = bytearray(scanline_bytes)
    view = memoryview(pixels)
    for row in range(height):
        start = row * (row_bytes + 1) + 1
        scanlines[start : start + row_bytes] = view[row * row_bytes : (row + 1) * row_bytes]

    # RFC 1950 zlib header, then RFC 1951 byte-aligned uncompressed blocks.
    stream = bytearray(b"\x78\x01")
    view = memoryview(scanlines)
    for offset in range(0, scanline_bytes, 65_535):
        length = min(65_535, scanline_bytes - offset)
        final = int(offset + length == scanline_bytes)
        stream.extend(struct.pack("<BHH", final, length, length ^ 0xFFFF))
        stream.extend(view[offset : offset + length])
    stream.extend(struct.pack(">I", adler32(scanlines) & 0xFFFFFFFF))
    header = struct.pack(">IIBBBBB", width, height, 8, 6, 0, 0, 0)
    encoded = (
        b"\x89PNG\r\n\x1a\n"
        + _chunk(b"IHDR", header)
        + _chunk(b"IDAT", stream)
        + _chunk(b"IEND", b"")
    )
    if len(encoded) != expected_size:
        raise ValueError("Canonical PNG size invariant failed")
    return encoded


def write_rgba_png(image: Image.Image, path: Path, *, max_bytes: int = MAX_PNG_BYTES) -> None:
    encoded = encode_rgba_png(image, max_bytes=max_bytes)
    with Path(path).open("xb") as output:
        output.write(encoded)
