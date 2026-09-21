import binascii
import io
import struct
import zlib
from types import SimpleNamespace

import pytest
from PIL import Image

from scripts.deterministic_png import (
    MAX_PNG_BYTES,
    encode_rgba_png,
    write_rgba_png,
)


def _chunks(encoded):
    assert encoded[:8] == b"\x89PNG\r\n\x1a\n"
    offset, chunks = 8, []
    while offset < len(encoded):
        length = struct.unpack_from(">I", encoded, offset)[0]
        kind = encoded[offset + 4 : offset + 8]
        data = encoded[offset + 8 : offset + 8 + length]
        checksum = struct.unpack_from(">I", encoded, offset + 8 + length)[0]
        assert checksum == binascii.crc32(kind + data) & 0xFFFFFFFF
        chunks.append((kind, data))
        offset += length + 12
    assert offset == len(encoded)
    assert [kind for kind, _ in chunks] == [b"IHDR", b"IDAT", b"IEND"]
    return dict(chunks)


def test_one_pixel_golden_bytes():
    image = Image.new("RGBA", (1, 1), (1, 2, 3, 4))
    assert encode_rgba_png(image).hex() == (
        "89504e470d0a1a0a0000000d49484452000000010000000108060000001f15c489"
        "00000010494441547801010500faff00010203040019000bb9b0e3eb00000000"
        "49454e44ae426082"
    )


@pytest.mark.parametrize("size", [(1, 1), (5460, 3), (5461, 3), (5462, 3), (257, 128)])
def test_roundtrip_pixels_and_stored_block_boundaries(size):
    length = size[0] * size[1] * 4
    pixels = (bytes(range(256)) * ((length + 255) // 256))[:length]
    image = Image.frombytes("RGBA", size, pixels)
    encoded = encode_rgba_png(image)
    chunks = _chunks(encoded)
    assert chunks[b"IHDR"] == struct.pack(">IIBBBBB", *size, 8, 6, 0, 0, 0)
    assert chunks[b"IEND"] == b""
    stream = chunks[b"IDAT"]
    assert stream[:2] == b"\x78\x01"
    offset, raw, count = 2, bytearray(), 0
    while True:
        final, block_length, complement = struct.unpack_from("<BHH", stream, offset)
        assert final in (0, 1)
        assert 1 <= block_length <= 65535
        assert complement == block_length ^ 0xFFFF
        offset += 5
        raw.extend(stream[offset : offset + block_length])
        offset += block_length
        count += 1
        if final:
            break
        assert block_length == 65535
    assert len(stream) - offset == 4
    assert struct.unpack_from(">I", stream, offset)[0] == zlib.adler32(raw) & 0xFFFFFFFF
    stride = size[0] * 4 + 1
    assert count == (stride * size[1] + 65534) // 65535
    expected = b"".join(
        b"\0" + pixels[row * (stride - 1) : (row + 1) * (stride - 1)]
        for row in range(size[1])
    )
    assert bytes(raw) == expected
    assert zlib.decompress(stream) == expected
    with Image.open(io.BytesIO(encoded)) as decoded:
        assert decoded.mode == "RGBA" and decoded.size == size
        assert decoded.tobytes() == pixels
    assert image.tobytes() == pixels
    assert encode_rgba_png(image) == encoded


def test_transparent_rgb_and_partial_alpha_are_preserved_without_compressor(monkeypatch):
    def forbidden(*args, **kwargs):
        raise AssertionError("Native encoder/compressor must not be used")

    image = Image.frombytes("RGBA", (3, 1), bytes([44, 55, 66, 0, 77, 88, 99, 127, 1, 2, 3, 255]))
    monkeypatch.setattr(zlib, "compress", forbidden)
    monkeypatch.setattr(zlib, "compressobj", forbidden)
    monkeypatch.setattr(Image.Image, "save", forbidden)
    with Image.open(io.BytesIO(encode_rgba_png(image))) as decoded:
        assert decoded.tobytes() == image.tobytes()


@pytest.mark.parametrize("mode", ["RGB", "P", "L", "LA", "I"])
def test_refuses_implicit_mode_conversion(mode):
    with pytest.raises(ValueError, match="already be RGBA"):
        encode_rgba_png(Image.new(mode, (1, 1)))


@pytest.mark.parametrize("size", [(0, 1), (1, 0), (-1, 1), (16385, 1), (1, 16385), (True, 1)])
def test_invalid_dimensions_fail_before_pixel_allocation(size):
    with pytest.raises(ValueError, match="dimensions"):
        encode_rgba_png(SimpleNamespace(mode="RGBA", size=size))


@pytest.mark.parametrize("budget", [0, -1, True, 1.5, MAX_PNG_BYTES + 1])
def test_invalid_byte_budget(budget):
    with pytest.raises(ValueError, match="byte budget"):
        encode_rgba_png(Image.new("RGBA", (1, 1)), max_bytes=budget)


def test_byte_budget_exact_boundary_and_oversize_before_pixel_allocation():
    image = Image.new("RGBA", (1, 1))
    size = len(encode_rgba_png(image))
    assert len(encode_rgba_png(image, max_bytes=size)) == size
    with pytest.raises(ValueError, match="exceeds byte budget"):
        encode_rgba_png(image, max_bytes=size - 1)
    with pytest.raises(ValueError, match="exceeds byte budget"):
        encode_rgba_png(SimpleNamespace(mode="RGBA", size=(16384, 16384)))


def test_invalid_raw_length_rejected():
    with pytest.raises(ValueError, match="byte length"):
        encode_rgba_png(SimpleNamespace(mode="RGBA", size=(1, 1), tobytes=lambda: b"\0"))


def test_writer_exclusive_and_rejection_creates_no_file(tmp_path):
    image = Image.new("RGBA", (2, 1), (10, 20, 30, 0))
    target = tmp_path / "sheet.png"
    write_rgba_png(image, target)
    assert target.read_bytes() == encode_rgba_png(image)
    with pytest.raises(FileExistsError):
        write_rgba_png(Image.new("RGBA", (2, 1)), target)
    assert target.read_bytes() == encode_rgba_png(image)
    rejected = tmp_path / "rejected.png"
    with pytest.raises(ValueError, match="byte budget"):
        write_rgba_png(image, rejected, max_bytes=1)
    assert not rejected.exists()
