"""Build a small synthetic LOGO image for the tests.

The real dump from a phone is deliberately not part of this repository.
"""

from __future__ import annotations

import struct
import sys
from pathlib import Path

from PIL import Image, ImageDraw

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import logotool  # noqa: E402


def _image_alpha() -> Image.Image:
    image = Image.new("RGB", (12, 8), (0, 0, 0))
    draw = ImageDraw.Draw(image)
    draw.rectangle((1, 1, 6, 4), fill=(255, 255, 255))
    draw.rectangle((2, 2, 4, 3), fill=(255, 0, 0))
    draw.rectangle((8, 1, 10, 6), fill=(0, 255, 0))
    draw.point((11, 7), fill=(0, 0, 255))
    return image


def _image_beta() -> Image.Image:
    image = Image.new("RGB", (9, 7), (0, 0, 0))
    draw = ImageDraw.Draw(image)
    draw.rectangle((0, 0, 8, 1), fill=(255, 255, 0))
    draw.rectangle((3, 2, 5, 5), fill=(0, 128, 255))
    draw.point((8, 6), fill=(255, 255, 255))
    return image


def _header(
    width: int,
    height: int,
    compressed_size: int,
    full_name: str,
    short_name: str,
    real: bool,
    field_0x2c: int,
) -> bytearray:
    header = bytearray(logotool.HEADER_SIZE)
    if real:
        header[0:8] = logotool.MAGIC
        struct.pack_into(
            "<IIII",
            header,
            0x20,
            width,
            height,
            compressed_size,
            field_0x2c,
        )
    for offset, name in (
        (logotool.FULL_NAME_OFFSET, full_name),
        (logotool.SHORT_NAME_OFFSET, short_name),
    ):
        encoded = name.encode("ascii")
        if len(encoded) >= logotool.NAME_LENGTH:
            raise ValueError("name is too long for the synthetic header")
        header[offset : offset + len(encoded)] = encoded
    return header


def _block(
    slot: int,
    rgb: Image.Image | None,
    full_name: str,
    short_name: str,
    table_offsets: list[int] | None = None,
) -> bytes:
    if rgb is None:
        header = _header(0, 0, 0, "", short_name, False, 0)
        return bytes(header) + bytes(slot - logotool.HEADER_SIZE)

    stream = logotool.rgb_to_stream(rgb.tobytes(), "BGR")
    compressed = logotool.compress_rle(stream)
    capacity = slot - logotool.DATA_START
    if len(compressed) > capacity:
        raise ValueError("synthetic image does not fit into its slot")

    header = _header(
        rgb.width,
        rgb.height,
        len(compressed),
        full_name,
        short_name,
        True,
        1,
    )
    if table_offsets is not None:
        if len(table_offsets) * 4 > logotool.FULL_NAME_OFFSET - logotool.TABLE_START:
            raise ValueError("synthetic table is too long")
        for pos, offset in enumerate(table_offsets):
            struct.pack_into(
                "<I", header, logotool.TABLE_START + 4 * pos, offset
            )

    body = bytearray(slot)
    body[0 : logotool.HEADER_SIZE] = header
    body[
        logotool.DATA_START : logotool.DATA_START + len(compressed)
    ] = compressed
    return bytes(body)


def build_synthetic_image(path: Path) -> dict:
    """Create a four-entry image (3 real blocks, 1 empty) and return metadata."""
    slot = 0x2000
    offsets = [0, slot, 2 * slot, 3 * slot]
    file_size = 4 * slot
    alpha = _image_alpha()
    beta = _image_beta()

    blocks = [
        _block(
            slot,
            alpha,
            "test_alpha_12_8_result.raw",
            "test_alpha",
            offsets,
        ),
        _block(slot, beta, "test_beta_9_7_result.raw", "test_beta"),
        _block(slot, alpha, "test_gamma_12_8_result.raw", "test_gamma"),
        _block(slot, None, "", "hd720_oppo"),
    ]
    data = b"".join(blocks)
    if len(data) != file_size:
        raise AssertionError("synthetic builder produced the wrong file size")
    path.write_bytes(data)
    return {
        "path": path,
        "offsets": offsets,
        "slot": slot,
        "alpha": alpha,
        "beta": beta,
        "real": 3,
        "empty": 1,
    }


def build_small_slot_image(
    path: Path, width: int = 64, height: int = 64, slot: int = 0x2000
) -> dict:
    """Create a real block with a small slot plus one empty record."""
    image = Image.new("RGB", (width, height), (0, 0, 0))
    draw = ImageDraw.Draw(image)
    draw.rectangle((1, 1, width // 2, height // 2), fill=(255, 255, 255))
    offsets = [0, slot]
    real = _block(
        slot,
        image,
        "noise_64_64_result.raw",
        "noise",
        offsets,
    )
    empty = _block(0x1000, None, "", "hd720_oppo")
    path.write_bytes(real + empty)
    return {"path": path, "image": image, "slot": slot}
