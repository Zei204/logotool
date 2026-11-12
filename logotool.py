#!/usr/bin/env python3
"""Inspect, decode and encode OnePlus/OPPO LOGO splash partitions.

The on-disk format is described in FORMAT.md.  The tool only uses the Python
standard library and Pillow.  Importing this module has no side effects; all
work happens in main().
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import struct
import sys
import tempfile
from collections import Counter, defaultdict
from pathlib import Path
from typing import Iterable

from PIL import Image

MAGIC = b"SPLASH!!"
TABLE_START = 0x30
TABLE_LIMIT = 0x180
OFFSET_ALIGNMENT = 0x1000
HEADER_SIZE = 0x200
DATA_START = 0x1000

FIELD_WIDTH = 0x20
FIELD_HEIGHT = 0x24
FIELD_SIZE = 0x28
FIELD_0X2C = 0x2C
FULL_NAME_OFFSET = 0x180
SHORT_NAME_OFFSET = 0x1C0
NAME_LENGTH = 0x40

# A run is one byte plus at most 254 equal bytes.  The regexp engine finds the
# run boundaries in C, which keeps pure-Python encoding reasonably fast.
_RLE_RUN = re.compile(rb"(.)\1{0,254}", re.DOTALL)

_SIMPLIFY_HINT = (
    "Use flat fills, no gradients, no anti-aliasing and no dithering. "
    "The background must be exactly (0,0,0)."
)
_OVER_BUDGET_HINT = (
    "If an image editor introduced colour noise (for example 252,253,253 "
    "instead of pure white), try --snap-threshold N: pixels lighter than N "
    "become white, all others become black.  Run again with --report to see "
    "the most expensive 40-row bands and their top colours."
)


class FormatError(Exception):
    """A problem with the image, the manifest or a command line request."""


def _u32(buf: bytes, offset: int) -> int:
    return struct.unpack_from("<I", buf, offset)[0]


def _sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def read_c_string(buf: bytes, start: int, length: int) -> str:
    return buf[start : start + length].split(b"\x00", 1)[0].decode(
        "ascii", errors="replace"
    )


def swap_red_blue(data: bytes) -> bytes:
    """Swap the first and third byte of every RGB/BGR triple."""
    result = bytearray(data)
    result[0::3] = data[2::3]
    result[2::3] = data[0::3]
    return bytes(result)


def stream_to_rgb(data: bytes, channel_order: str) -> bytes:
    return swap_red_blue(data) if channel_order == "BGR" else data


def rgb_to_stream(data: bytes, channel_order: str) -> bytes:
    return swap_red_blue(data) if channel_order == "BGR" else data


def compress_rle(data: bytes) -> bytes:
    """Compress bytes as canonical (value, count) pairs, splitting at 255."""
    out = bytearray()
    for match in _RLE_RUN.finditer(data):
        start, end = match.span()
        out.append(data[start])
        out.append(end - start)
    return bytes(out)


def decompress_rle(data: bytes, expected: int, label: str) -> bytes:
    if len(data) % 2 != 0:
        raise FormatError(
            f"{label}: compressed data has an odd size ({len(data)} bytes)"
        )
    out = bytearray()
    for pos in range(0, len(data), 2):
        value = data[pos]
        count = data[pos + 1]
        if count == 0:
            raise FormatError(f"{label}: zero counter in pair #{pos // 2}")
        out.extend(bytes((value,)) * count)
        if len(out) > expected:
            raise FormatError(
                f"{label}: decompression exceeded the expected {expected} bytes"
            )
    if len(out) != expected:
        raise FormatError(
            f"{label}: decompressed {len(out)} bytes instead of {expected} (w*h*3)"
        )
    return bytes(out)


def read_table(raw: bytes) -> list[int]:
    """Derive the table length by scanning increasing, aligned offsets.

    The count is not stored explicitly.  In this format the table starts at
    0x30, offsets are 0x1000-aligned, and the name field starts at 0x180.
    Scanning stops at the first value that cannot be the next block offset.
    """
    if len(raw) < TABLE_START + 4:
        raise FormatError("file is smaller than the offset table")

    offsets: list[int] = []
    pos = TABLE_START
    limit = min(len(raw), TABLE_LIMIT)
    while pos + 4 <= limit:
        value = _u32(raw, pos)
        if value >= len(raw):
            break
        if value % OFFSET_ALIGNMENT != 0:
            break
        if offsets and value <= offsets[-1]:
            break
        offsets.append(value)
        pos += 4

    if not offsets:
        raise FormatError("no block offsets found at the start of the table")
    return offsets


def _safe_png_stem(name: str, index: int) -> str:
    stem = name
    if stem.endswith("_result.raw"):
        stem = stem[: -len("_result.raw")]
    stem = re.sub(r"[^A-Za-z0-9._-]+", "_", stem).strip("._-")
    return stem or f"block_{index}"


def parse_image(raw: bytes) -> dict:
    """Parse and fully validate an image; return records and decoded streams."""
    file_size = len(raw)
    offsets = read_table(raw)
    records: list[dict] = []
    unpacked_cache: dict[int, bytes] = {}

    for idx, offset in enumerate(offsets):
        slot_end = offsets[idx + 1] if idx + 1 < len(offsets) else file_size
        slot = slot_end - offset
        if offset < 0 or offset + HEADER_SIZE > file_size:
            raise FormatError(
                f"record {idx}: offset {offset:#x} is outside the file"
            )
        if slot <= 0:
            raise FormatError(
                f"record {idx}: invalid slot {slot} (next record at {slot_end:#x})"
            )
        if slot < DATA_START:
            raise FormatError(
                f"record {idx}: slot {slot:#x} is smaller than the "
                f"header area {DATA_START:#x}"
            )

        header = raw[offset : offset + HEADER_SIZE]
        full_name = read_c_string(header, FULL_NAME_OFFSET, NAME_LENGTH)
        short_name = read_c_string(header, SHORT_NAME_OFFSET, NAME_LENGTH)
        width, height, size, field_2c = struct.unpack_from(
            "<IIII", raw, offset + FIELD_WIDTH
        )

        record = {
            "index": idx,
            "offset": offset,
            "offset_hex": hex(offset),
            "kind": "empty",
            "real": False,
            "width": width,
            "height": height,
            "size": size,
            "field_0x2c": field_2c,
            "full_name": full_name,
            "short_name": short_name,
            "sha256_compressed": None,
            "sha256_uncompressed": None,
            "slot": slot,
            "capacity": slot - DATA_START,
            "reserve": slot - DATA_START,
            "header_0x200_hex": header.hex(),
            "header_0x08_0x1f_hex": header[0x08:0x20].hex(),
            "png": None,
            "duplicate_group": None,
        }

        label = f"record {idx} ({full_name or short_name or 'unnamed'})"
        has_magic = raw[offset : offset + 8] == MAGIC

        if not has_magic:
            if any((width, height, size, field_2c)):
                raise FormatError(
                    f"{label}: magic {MAGIC!r} is missing, but fields "
                    "+0x20..+0x2f are not zero"
                )
            records.append(record)
            continue

        record["kind"] = "real"
        record["real"] = True

        if size % 2 != 0:
            raise FormatError(f"{label}: compressed size is odd ({size})")
        capacity = slot - DATA_START
        if size > capacity:
            raise FormatError(
                f"{label}: data ({size} bytes) goes past the slot "
                f"(only {capacity} bytes available)"
            )
        data_start = offset + DATA_START
        data_end = data_start + size
        if data_end > file_size:
            raise FormatError(f"{label}: data goes past the end of the file")

        expected = width * height * 3
        if expected <= 0:
            raise FormatError(f"{label}: empty image {width}x{height}")

        compressed = raw[data_start:data_end]
        unpacked = decompress_rle(compressed, expected, label)

        record["reserve"] = capacity - size
        record["sha256_compressed"] = _sha256(compressed)
        record["sha256_uncompressed"] = _sha256(unpacked)
        record["png"] = f"{idx:02d}_{_safe_png_stem(full_name, idx)}.png"
        unpacked_cache[idx] = unpacked
        records.append(record)

    duplicate_groups = find_duplicates(records)
    return {
        "offsets": offsets,
        "records": records,
        "duplicate_groups": duplicate_groups,
        "unpacked": unpacked_cache,
    }


def find_duplicates(records: Iterable[dict]) -> list[dict]:
    groups: dict[str, list[dict]] = defaultdict(list)
    for record in records:
        if record["real"]:
            groups[record["sha256_compressed"]].append(record)

    duplicate_groups: list[dict] = []
    group_number = 0
    for digest, members in sorted(groups.items(), key=lambda item: item[1][0]["index"]):
        if len(members) < 2:
            continue
        group_number += 1
        group_id = f"D{group_number:02d}"
        for member in members:
            member["duplicate_group"] = group_id
        duplicate_groups.append(
            {
                "id": group_id,
                "sha256_compressed": digest,
                "indices": [member["index"] for member in members],
                "width": members[0]["width"],
                "height": members[0]["height"],
                "size": members[0]["size"],
            }
        )
    return duplicate_groups


def build_manifest(
    raw: bytes,
    source_name: str,
    parsed: dict,
    channel_order: str,
) -> dict:
    offsets = parsed["offsets"]
    return {
        "tool": "logotool.py",
        "format": "OnePlus/OPPO LOGO splash (SPLASH!!) RLE",
        "source": {
            "file": Path(source_name).name,
            "size": len(raw),
            "sha256": _sha256(raw),
        },
        "channel_order": channel_order,
        "table": {
            "start": hex(TABLE_START),
            "count": len(offsets),
            "entry_type": "uint32 little-endian",
            "entry_size": 4,
            "alignment": hex(OFFSET_ALIGNMENT),
            "end": hex(TABLE_START + 4 * len(offsets)),
        },
        "header": {
            "magic_hex": MAGIC.hex(),
            "size": hex(HEADER_SIZE),
            "data_start": hex(DATA_START),
            "full_name_offset": hex(FULL_NAME_OFFSET),
            "short_name_offset": hex(SHORT_NAME_OFFSET),
            "name_length": NAME_LENGTH,
            "field_width": hex(FIELD_WIDTH),
            "field_height": hex(FIELD_HEIGHT),
            "field_size": hex(FIELD_SIZE),
            "field_0x2c": hex(FIELD_0X2C),
        },
        "unknowns": [
            "byte range +0x08..+0x1f (zero in the reference dump)",
            "field +0x2c (1 for real blocks, 0 for empty records in the "
            "reference dump; purpose not proven)",
            "purpose of empty records and how aboot selects a block",
            "table count is not stored explicitly; it is derived from the "
            "table/name boundary",
        ],
        "duplicate_groups": parsed["duplicate_groups"],
        "records": parsed["records"],
    }


def print_table(records: list[dict]) -> None:
    print(
        " idx  offset      size      slot      reserve  WxH          dup  name"
    )
    for record in records:
        if record["real"]:
            dimensions = f"{record['width']}x{record['height']}"
            print(
                f"{record['index']:4d}  {record['offset_hex']:>10}  "
                f"{record['size']:9d} {record['slot']:#9x} "
                f"{record['reserve']:9d}  {dimensions:<12} "
                f"{record['duplicate_group'] or '-':>4}  "
                f"{record['full_name']}"
            )
        else:
            print(
                f"{record['index']:4d}  {record['offset_hex']:>10}  "
                f"{'-':>9} {record['slot']:#9x} "
                f"{record['reserve']:9d}  {'-':<12} {'-':>4}  "
                f"{record['short_name']} (empty)"
            )


def print_duplicates(duplicate_groups: list[dict]) -> None:
    print()
    if not duplicate_groups:
        print("No duplicate groups (sha256 of compressed data).")
        return
    print("Duplicate groups (same sha256 of compressed data):")
    for group in duplicate_groups:
        print(
            f"  {group['id']}  {group['width']}x{group['height']}  "
            f"size={group['size']}  sha256={group['sha256_compressed']}"
        )
        print(f"      blocks: {', '.join(str(i) for i in group['indices'])}")


def check_output_directory(outdir: Path, force: bool) -> None:
    if not outdir.exists():
        return
    if not outdir.is_dir():
        raise FormatError(f"output path exists and is not a directory: {outdir}")
    entries = list(outdir.iterdir())
    if entries and not force:
        raise FormatError(
            f"output directory is not empty: {outdir}. "
            "Use --force to write into it anyway."
        )


def atomic_write_bytes(path: Path, data: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temp_name = tempfile.mkstemp(
        prefix=path.name + ".", suffix=".tmp", dir=str(path.parent)
    )
    temp_path = Path(temp_name)
    try:
        with os.fdopen(fd, "wb") as handle:
            handle.write(data)
            handle.flush()
            os.fsync(handle.fileno())
        if temp_path.read_bytes() != data:
            raise FormatError(f"temporary file verification failed for {path}")
        os.replace(temp_path, path)
    except Exception:
        try:
            temp_path.unlink(missing_ok=True)
        except OSError:
            pass
        raise


def cmd_info(args: argparse.Namespace) -> int:
    source = Path(args.image)
    raw = source.read_bytes()
    parsed = parse_image(raw)
    records = parsed["records"]
    real = [record for record in records if record["real"]]
    empty = [record for record in records if not record["real"]]

    print(f"File: {source} ({len(raw)} bytes)")
    print(f"sha256: {_sha256(raw)}")
    print(
        f"Table: {len(parsed['offsets'])} entries at {hex(TABLE_START)}, "
        f"end {hex(TABLE_START + 4 * len(parsed['offsets']))} "
        "(uint32 little-endian)"
    )
    print(f"Blocks: {len(real)} real, {len(empty)} empty")
    print()
    print_table(records)
    print_duplicates(parsed["duplicate_groups"])
    return 0


def cmd_decode(args: argparse.Namespace) -> int:
    outdir = Path(args.outdir)
    check_output_directory(outdir, args.force)

    source = Path(args.image)
    raw = source.read_bytes()
    parsed = parse_image(raw)
    records = parsed["records"]
    channel_order = args.channel_order.upper()

    outdir.mkdir(parents=True, exist_ok=True)
    first_rgb: bytes | None = None
    first_record: dict | None = None

    for record in records:
        if not record["real"]:
            continue
        unpacked = parsed["unpacked"][record["index"]]
        rgb = stream_to_rgb(unpacked, channel_order)
        image = Image.frombytes(
            "RGB", (record["width"], record["height"]), rgb
        )
        image.save(outdir / record["png"])
        if first_record is None:
            first_record = record
            first_rgb = rgb

    manifest = build_manifest(raw, source.name, parsed, channel_order)
    manifest_path = outdir / "manifest.json"
    atomic_write_bytes(
        manifest_path,
        (json.dumps(manifest, ensure_ascii=False, indent=2) + "\n").encode("utf-8"),
    )

    print_table(records)
    print_duplicates(parsed["duplicate_groups"])
    print()

    if first_record is not None and first_rgb is not None:
        with Image.open(outdir / first_record["png"]) as image:
            image = image.convert("RGB")
            expected_size = (first_record["width"], first_record["height"])
            if image.size != expected_size:
                raise FormatError(
                    f"PNG self-check: size {image.size}, expected {expected_size}"
                )
            if image.tobytes() != first_rgb:
                raise FormatError(
                    "PNG self-check: saved pixels do not match the decoded block"
                )
        print(
            f"PNG self-check: block {first_record['index']} was reopened and "
            "matches the decoded pixels."
        )

    print(
        f"Wrote {len(parsed['unpacked'])} PNG files and "
        f"{manifest_path.name} to {outdir}"
    )
    return 0


def load_manifest(workdir: Path) -> dict:
    path = workdir / "manifest.json"
    if not path.is_file():
        raise FormatError(f"manifest not found: {path}")
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as error:
        raise FormatError(f"cannot read {path}: {error}") from error


def verify_source(raw: bytes, manifest: dict) -> str:
    source = manifest.get("source") or {}
    expected_size = source.get("size")
    expected_sha256 = source.get("sha256")
    digest = _sha256(raw)
    if expected_size is not None and len(raw) != expected_size:
        raise FormatError(
            f"source size {len(raw)} does not match manifest ({expected_size})"
        )
    if expected_sha256 and digest != expected_sha256:
        raise FormatError(
            "source sha256 does not match manifest: "
            f"got {digest}, expected {expected_sha256}"
        )
    return digest


def validate_manifest(manifest: dict, parsed: dict) -> dict[int, dict]:
    table_meta = manifest.get("table") or {}
    if table_meta.get("count") is not None:
        if table_meta["count"] != len(parsed["offsets"]):
            raise FormatError(
                f"manifest table count {table_meta['count']} does not match "
                f"the file ({len(parsed['offsets'])})"
            )
    if table_meta.get("start") is not None:
        try:
            start = int(str(table_meta["start"]), 16)
        except ValueError as error:
            raise FormatError("manifest table.start is not a hex number") from error
        if start != TABLE_START:
            raise FormatError(
                f"manifest table start {table_meta['start']} is not {hex(TABLE_START)}"
            )

    manifest_records = manifest.get("records") or []
    by_index: dict[int, dict] = {}
    for record in manifest_records:
        idx = record.get("index")
        if not isinstance(idx, int) or idx < 0:
            raise FormatError(f"bad record index in manifest: {idx!r}")
        if idx in by_index:
            raise FormatError(f"duplicate record index in manifest: {idx}")
        by_index[idx] = record

    parsed_indices = {record["index"] for record in parsed["records"]}
    if set(by_index) != parsed_indices:
        missing = sorted(parsed_indices - set(by_index))
        extra = sorted(set(by_index) - parsed_indices)
        raise FormatError(
            f"manifest records do not match the file (missing={missing}, extra={extra})"
        )

    for record in parsed["records"]:
        mrec = by_index[record["index"]]
        if mrec.get("offset") != record["offset"]:
            raise FormatError(
                f"record {record['index']}: manifest offset {mrec.get('offset')} "
                f"does not match the table ({record['offset']})"
            )
        if bool(mrec.get("real")) != record["real"]:
            raise FormatError(
                f"record {record['index']}: manifest real/empty flag differs"
            )
        for key in ("width", "height", "size", "field_0x2c"):
            if mrec.get(key) != record[key]:
                raise FormatError(
                    f"record {record['index']}: manifest {key} "
                    f"{mrec.get(key)!r} does not match the file ({record[key]!r})"
                )
        if record["real"]:
            png = mrec.get("png")
            if not png:
                raise FormatError(
                    f"record {record['index']}: manifest has no PNG name"
                )
            png_path = Path(png)
            if png_path.is_absolute() or ".." in png_path.parts:
                raise FormatError(
                    f"record {record['index']}: PNG path must be relative: {png!r}"
                )
    return by_index


def parse_only(selectors: list[str] | None, records: list[dict]) -> set[int]:
    real = [record for record in records if record["real"]]
    if not selectors:
        return {record["index"] for record in real}

    selected: set[int] = set()
    for raw_selector in selectors:
        for selector in raw_selector.split(","):
            selector = selector.strip()
            if not selector:
                continue
            if re.fullmatch(r"\d+", selector):
                index = int(selector)
                record = next(
                    (record for record in real if record["index"] == index), None
                )
                if record is None:
                    raise FormatError(f"--only {selector}: no real block with that index")
                selected.add(index)
                continue

            matches = []
            for record in real:
                names = {
                    record["full_name"],
                    record["short_name"],
                    record["full_name"].removesuffix("_result.raw"),
                }
                if selector in names:
                    matches.append(record["index"])
            if not matches:
                raise FormatError(f"--only {selector}: no block with that name")
            if len(matches) > 1:
                raise FormatError(
                    f"--only {selector}: ambiguous name, matches blocks "
                    f"{', '.join(str(i) for i in matches)}; use an index"
                )
            selected.add(matches[0])
    return selected


def load_png_rgb(
    path: Path,
    record: dict,
    flatten: bool,
    snap_threshold: int | None,
) -> tuple[bytes, str]:
    if not path.is_file():
        raise FormatError(
            f"block {record['index']}: PNG not found: {path}"
        )
    try:
        with Image.open(path) as image:
            mode = image.mode
            size = image.size
            expected_size = (record["width"], record["height"])
            if size != expected_size:
                raise FormatError(
                    f"block {record['index']} ({record['full_name']}): "
                    f"PNG size {size[0]}x{size[1]}, expected "
                    f"{expected_size[0]}x{expected_size[1]}"
                )

            if mode == "RGB":
                rgb = image.tobytes()
            elif mode == "RGBA" and flatten:
                background = Image.new("RGBA", size, (0, 0, 0, 255))
                rgb = Image.alpha_composite(
                    background, image.convert("RGBA")
                ).convert("RGB").tobytes()
            elif mode in ("LA", "PA") and flatten:
                background = Image.new("RGBA", size, (0, 0, 0, 255))
                rgb = Image.alpha_composite(
                    background, image.convert("RGBA")
                ).convert("RGB").tobytes()
            else:
                extra = (
                    " Use --flatten to composite RGBA over black."
                    if mode in ("RGBA", "LA", "PA")
                    else " Save the PNG as RGB."
                )
                raise FormatError(
                    f"block {record['index']} ({record['full_name']}): "
                    f"PNG mode is {mode}, exactly RGB is required.{extra}"
                )
    except OSError as error:
        raise FormatError(f"cannot open PNG {path}: {error}") from error

    if snap_threshold is not None:
        threshold = int(snap_threshold)
        image = Image.frombytes("RGB", size, rgb)
        image = image.convert("L").point(
            lambda value: 255 if value > threshold else 0
        ).convert("RGB")
        rgb = image.tobytes()

    expected_length = record["width"] * record["height"] * 3
    if len(rgb) != expected_length:
        raise FormatError(
            f"block {record['index']}: pixel data has {len(rgb)} bytes, "
            f"expected {expected_length}"
        )
    return rgb, mode


def resolve_effective(
    duplicate_groups: list[dict],
    records: list[dict],
    streams: dict[int, bytes],
    changed: set[int],
    apply_to_duplicates: bool,
) -> dict[int, int]:
    by_index = {record["index"]: record for record in records}
    group_by_index: dict[int, dict] = {}
    for group in duplicate_groups:
        for idx in group.get("indices", []):
            group_by_index[idx] = group

    if apply_to_duplicates:
        effective: dict[int, int] = {}
        for group in duplicate_groups:
            members = [idx for idx in group.get("indices", []) if idx in by_index]
            changed_members = [idx for idx in members if idx in changed]
            if not changed_members:
                continue
            source = changed_members[0]
            for other in changed_members[1:]:
                if streams[other] != streams[source]:
                    raise FormatError(
                        f"group {group.get('id')}: PNGs of blocks {source} and "
                        f"{other} were changed differently; with "
                        "--apply-to-duplicates it is unclear which one to use. "
                        "Make them identical first."
                    )
            for member in members:
                effective[member] = source
            print(
                f"Duplicates {group.get('id')}: PNG of block {source} is applied "
                f"to all blocks in the group: "
                f"{', '.join(str(i) for i in members)}"
            )
        for idx in changed:
            effective.setdefault(idx, idx)
        return effective

    warned: set[str] = set()
    for idx in sorted(changed):
        group = group_by_index.get(idx)
        if not group:
            continue
        group_id = str(group.get("id"))
        if group_id in warned:
            continue
        warned.add(group_id)
        print(
            f"WARNING: block {idx} has duplicates by compressed sha256 "
            f"(group {group_id}: "
            f"{', '.join(str(i) for i in group.get('indices', []))}). "
            "Without --apply-to-duplicates only this block is changed."
        )
    return {idx: idx for idx in changed}


def print_compression_report(
    record: dict,
    stream: bytes,
    channel_order: str,
    rows_per_band: int = 40,
    top_bands: int = 10,
    top_colors: int = 8,
) -> None:
    width = record["width"]
    height = record["height"]
    band_count = (height + rows_per_band - 1) // rows_per_band
    bands: list[tuple[int, int, int]] = []
    for band in range(band_count):
        first_row = band * rows_per_band
        last_row = min(height, first_row + rows_per_band)
        start = first_row * width * 3
        end = last_row * width * 3
        compressed = compress_rle(stream[start:end])
        bands.append((len(compressed), first_row, last_row))

    bands.sort(reverse=True)
    print()
    print(
        f"Compression report for block {record['index']} "
        f"({record['full_name']}), {width}x{height}:"
    )
    print(
        f"  Most expensive 40-row bands (top {min(top_bands, len(bands))} "
        f"of {len(bands)}):"
    )
    for compressed_size, first_row, last_row in bands[:top_bands]:
        start = first_row * width * 3
        end = last_row * width * 3
        counts: Counter[tuple[int, int, int]] = Counter()
        for pos in range(start, end, 3):
            if channel_order == "BGR":
                pixel = (stream[pos + 2], stream[pos + 1], stream[pos])
            else:
                pixel = (stream[pos], stream[pos + 1], stream[pos + 2])
            counts[pixel] += 1
        colors = "  ".join(
            f"#{r:02X}{g:02X}{b:02X} x{count}"
            for (r, g, b), count in counts.most_common(top_colors)
        )
        print(
            f"    rows {first_row:4d}..{last_row - 1:4d}: "
            f"{compressed_size:7d} compressed bytes; {colors}"
        )


def encode_effective_blocks(
    by_index: dict[int, dict],
    streams: dict[int, bytes],
    effective: dict[int, int],
    channel_order: str,
    report: bool,
) -> dict[int, dict]:
    cache: dict[str, bytes] = {}
    result: dict[int, dict] = {}
    failures: list[dict] = []

    for idx in sorted(effective):
        source = effective[idx]
        record = by_index[idx]
        stream = streams[source]
        expected = record["width"] * record["height"] * 3
        if len(stream) != expected:
            raise FormatError(
                f"block {idx}: {len(stream)} pixel bytes, expected "
                f"w*h*3={expected}"
            )
        key = _sha256(stream)
        if key not in cache:
            cache[key] = compress_rle(stream)
        compressed = cache[key]
        if len(compressed) % 2 != 0:
            raise FormatError(
                f"block {idx}: compressed size is odd ({len(compressed)})"
            )
        capacity = record["slot"] - DATA_START
        if len(compressed) > capacity:
            failures.append(
                {
                    "index": idx,
                    "record": record,
                    "stream": stream,
                    "size": len(compressed),
                    "capacity": capacity,
                    "over": len(compressed) - capacity,
                }
            )
            continue
        result[idx] = {
            "source": source,
            "stream": stream,
            "compressed": compressed,
            "old_size": record["size"],
            "new_size": len(compressed),
            "capacity": capacity,
            "reserve": capacity - len(compressed),
        }

    if failures:
        if report:
            for failure in failures:
                print_compression_report(
                    failure["record"], failure["stream"], channel_order
                )
        messages = []
        for failure in failures:
            record = failure["record"]
            messages.append(
                f"block {record['index']} ({record['full_name']}): compressed "
                f"data ({failure['size']} bytes) does not fit into the slot "
                f"(capacity {failure['capacity']} bytes after the header), "
                f"over by {failure['over']} bytes."
            )
        messages.append(_SIMPLIFY_HINT)
        messages.append(_OVER_BUDGET_HINT)
        raise FormatError("\n".join(messages))
    return result


def build_buffer(
    original: bytes,
    by_index: dict[int, dict],
    encoded: dict[int, dict],
) -> bytearray:
    buffer = bytearray(original)
    for idx, info in encoded.items():
        record = by_index[idx]
        offset = record["offset"]
        data_start = offset + DATA_START
        slot_end = offset + record["slot"]
        compressed = info["compressed"]
        buffer[data_start : data_start + len(compressed)] = compressed
        tail_start = data_start + len(compressed)
        if tail_start < slot_end:
            buffer[tail_start:slot_end] = bytes(slot_end - tail_start)
        struct.pack_into("<I", buffer, offset + FIELD_SIZE, len(compressed))
    return buffer


def compare_outside_ranges(
    original: bytes, buffer: bytes, ranges: list[tuple[int, int]]
) -> None:
    position = 0
    for start, end in sorted(ranges):
        if start > position and buffer[position:start] != original[position:start]:
            raise FormatError(
                "bytes outside the changed blocks were modified: range "
                f"0x{position:08X}..0x{start:08X}"
            )
        position = max(position, end)
    if position < len(original) and buffer[position:] != original[position:]:
        raise FormatError(
            "bytes outside the changed blocks were modified: range "
            f"0x{position:08X}..0x{len(original):08X}"
        )


def validate_prewrite(
    original: bytes,
    buffer: bytearray,
    offsets: list[int],
    by_index: dict[int, dict],
    encoded: dict[int, dict],
) -> None:
    if len(buffer) != len(original):
        raise FormatError(
            f"buffer size {len(buffer)} differs from the source ({len(original)})"
        )

    table_end = TABLE_START + 4 * len(offsets)
    if bytes(buffer[TABLE_START:table_end]) != original[TABLE_START:table_end]:
        raise FormatError("the offset table changed")

    for idx, record in by_index.items():
        if record["real"] and bytes(buffer[record["offset"] : record["offset"] + 8]) != MAGIC:
            raise FormatError(f"block {idx}: magic is corrupted")

    ranges = [
        (by_index[idx]["offset"], by_index[idx]["offset"] + by_index[idx]["slot"])
        for idx in encoded
    ]
    compare_outside_ranges(original, bytes(buffer), ranges)

    for idx, info in encoded.items():
        record = by_index[idx]
        offset = record["offset"]
        data_start = offset + DATA_START
        slot_end = offset + record["slot"]
        if bytes(buffer[offset : offset + FIELD_SIZE]) != original[offset : offset + FIELD_SIZE]:
            raise FormatError(
                f"block {idx}: header bytes before +0x28 changed"
            )
        if (
            bytes(buffer[offset + FIELD_0X2C : data_start])
            != original[offset + FIELD_0X2C : data_start]
        ):
            raise FormatError(
                f"block {idx}: header bytes after +0x28 changed "
                "(including +0x2c, names and table)"
            )
        stored_size = _u32(bytes(buffer), offset + FIELD_SIZE)
        if stored_size != info["new_size"]:
            raise FormatError(f"block {idx}: +0x28 was written incorrectly")
        compressed = info["compressed"]
        if bytes(buffer[data_start : data_start + len(compressed)]) != compressed:
            raise FormatError(f"block {idx}: data was written incorrectly")
        tail_start = data_start + len(compressed)
        if tail_start < slot_end and any(buffer[tail_start:slot_end]):
            raise FormatError(f"block {idx}: slot tail after data is not zeroed")


def independent_decode(raw: bytes, records: list[dict]) -> dict[int, dict]:
    offsets = read_table(raw)
    file_size = len(raw)
    decoded: dict[int, dict] = {}
    for record in records:
        idx = record["index"]
        offset = offsets[idx]
        if raw[offset : offset + 8] != MAGIC:
            raise FormatError(f"post-check: block {idx}: magic is missing")
        width, height, size, field_2c = struct.unpack_from(
            "<IIII", raw, offset + FIELD_WIDTH
        )
        slot_end = offsets[idx + 1] if idx + 1 < len(offsets) else file_size
        capacity = slot_end - offset - DATA_START
        if size % 2 != 0:
            raise FormatError(f"post-check: block {idx}: odd size {size}")
        if size > capacity:
            raise FormatError(
                f"post-check: block {idx}: data {size} is larger than slot {capacity}"
            )
        data_start = offset + DATA_START
        compressed = raw[data_start : data_start + size]
        expected = width * height * 3
        unpacked = decompress_rle(
            compressed, expected, f"post-check: block {idx}"
        )
        tail_start = data_start + size
        if tail_start < slot_end and any(raw[tail_start:slot_end]):
            raise FormatError(
                f"post-check: block {idx}: bytes after data are not zero"
            )
        decoded[idx] = {
            "width": width,
            "height": height,
            "size": size,
            "field_0x2c": field_2c,
            "compressed": compressed,
            "unpacked": unpacked,
        }
    return decoded


def verify_post_build(
    raw: bytes,
    records: list[dict],
    encoded: dict[int, dict],
    original_unpacked: dict[int, bytes],
) -> None:
    decoded = independent_decode(raw, records)
    for record in records:
        idx = record["index"]
        block = decoded[idx]
        if (block["width"], block["height"]) != (
            record["width"],
            record["height"],
        ):
            raise FormatError(
                f"post-check: block {idx}: header dimensions changed"
            )
        if block["field_0x2c"] != record["field_0x2c"]:
            raise FormatError(f"post-check: block {idx}: field +0x2c changed")
        if idx in encoded:
            info = encoded[idx]
            expected_unpacked = info["stream"]
            expected_compressed = info["compressed"]
            expected_size = info["new_size"]
        else:
            expected_unpacked = original_unpacked[idx]
            expected_compressed = raw[
                record["offset"] + DATA_START :
                record["offset"] + DATA_START + record["size"]
            ]
            expected_size = record["size"]
        if block["size"] != expected_size:
            raise FormatError(
                f"post-check: block {idx}: size {block['size']}, "
                f"expected {expected_size}"
            )
        if block["compressed"] != expected_compressed:
            raise FormatError(
                f"post-check: block {idx}: compressed bytes differ"
            )
        if block["unpacked"] != expected_unpacked:
            raise FormatError(
                f"post-check: block {idx}: pixels differ from the expected data"
            )
    print(
        f"Post-check with an independent decoder: all {len(records)} real "
        "blocks match pixel for pixel."
    )


def diff_ranges(original: bytes, new: bytes) -> list[tuple[int, int]]:
    ranges: list[tuple[int, int]] = []
    start: int | None = None
    for pos, (old_byte, new_byte) in enumerate(zip(original, new)):
        if old_byte != new_byte:
            if start is None:
                start = pos
        elif start is not None:
            ranges.append((start, pos))
            start = None
    if start is not None:
        ranges.append((start, len(original)))
    return ranges


def print_diff_ranges(original: bytes, new: bytes) -> None:
    ranges = diff_ranges(original, new)
    if not ranges:
        print("No bytes changed.")
        return
    exact_bytes = sum(end - start for start, end in ranges)
    note = ""
    if len(ranges) > 50:
        merged: list[tuple[int, int]] = []
        for start, end in ranges:
            if merged and start - merged[-1][1] <= 64:
                merged[-1] = (merged[-1][0], end)
            else:
                merged.append((start, end))
        ranges = merged
        note = " (nearby ranges merged, gap up to 64 bytes)"
    print(
        f"Changed byte ranges, start..end exclusive{note} "
        f"(actually changed {exact_bytes} bytes):"
    )
    for start, end in ranges:
        print(f"  0x{start:08X}..0x{end:08X}  ({end - start} bytes)")


def write_output(
    buffer: bytearray,
    source_path: Path,
    out_path: Path,
    force: bool,
    dry_run: bool,
) -> bytes:
    data = bytes(buffer)
    if out_path.resolve() == source_path.resolve():
        raise FormatError(
            "refusing to overwrite the input image; choose a different --out"
        )

    if dry_run:
        print(f"--dry-run: {out_path} was not written.")
        return data

    if out_path.exists():
        if not out_path.is_file():
            raise FormatError(f"output path is not a file: {out_path}")
        if not force:
            raise FormatError(
                f"output file already exists: {out_path}. Use --force to overwrite."
            )

    if not out_path.parent.is_dir():
        raise FormatError(f"output directory does not exist: {out_path.parent}")

    atomic_write_bytes(out_path, data)
    written = out_path.read_bytes()
    if written != data:
        raise FormatError(
            "file contents after writing do not match the verified buffer"
        )
    return written


def preflight_rle(
    raw: bytes,
    records: list[dict],
    unpacked: dict[int, bytes],
    manifest_by_index: dict[int, dict],
) -> None:
    matched = 0
    for record in records:
        idx = record["index"]
        label = f"record {idx} ({record['full_name'] or record['short_name']})"
        offset = record["offset"]
        compressed = raw[offset + DATA_START : offset + DATA_START + record["size"]]
        recompressed = compress_rle(unpacked[idx])
        if recompressed != compressed:
            first = next(
                (
                    pos
                    for pos in range(min(len(compressed), len(recompressed)))
                    if compressed[pos] != recompressed[pos]
                ),
                None,
            )
            raise FormatError(
                f"{label}: recompression did not match the original; "
                f"old size {len(compressed)}, new size {len(recompressed)}, "
                f"first difference at {first}"
            )
        mrec = manifest_by_index[idx]
        if (
            mrec.get("sha256_uncompressed")
            and mrec["sha256_uncompressed"] != record["sha256_uncompressed"]
        ):
            raise FormatError(
                f"{label}: decompressed sha256 does not match the manifest"
            )
        if (
            mrec.get("sha256_compressed")
            and mrec["sha256_compressed"] != record["sha256_compressed"]
        ):
            raise FormatError(
                f"{label}: compressed sha256 does not match the manifest"
            )
        matched += 1

    print(
        f"RLE self-check: recompressed data matched the original in "
        f"{matched}/{len(records)} real blocks."
    )


def cmd_encode(args: argparse.Namespace) -> int:
    source_path = Path(args.image)
    workdir = Path(args.workdir)
    out_path = Path(args.out)

    if not workdir.is_dir():
        raise FormatError(f"work directory does not exist: {workdir}")

    raw = source_path.read_bytes()
    manifest = load_manifest(workdir)
    source_sha256 = verify_source(raw, manifest)

    parsed = parse_image(raw)
    records = parsed["records"]
    real_records = [record for record in records if record["real"]]
    manifest_by_index = validate_manifest(manifest, parsed)
    by_index = {record["index"]: record for record in records}

    preflight_rle(
        raw, real_records, parsed["unpacked"], manifest_by_index
    )

    channel_order = (
        args.channel_order.upper()
        if args.channel_order
        else str(manifest.get("channel_order") or "BGR").upper()
    )
    if channel_order not in ("BGR", "RGB"):
        raise FormatError(f"unsupported channel order in manifest: {channel_order}")

    selected = parse_only(args.only, records)
    streams: dict[int, bytes] = {}
    changed: set[int] = set()

    for idx in sorted(selected):
        record = by_index[idx]
        mrec = manifest_by_index[idx]
        png_path = workdir / Path(mrec["png"])
        rgb, _mode = load_png_rgb(
            png_path, record, args.flatten, args.snap_threshold
        )
        stream = rgb_to_stream(rgb, channel_order)
        streams[idx] = stream
        if _sha256(stream) != record["sha256_uncompressed"]:
            changed.add(idx)

    if changed:
        effective = resolve_effective(
            parsed["duplicate_groups"],
            records,
            streams,
            changed,
            args.apply_to_duplicates,
        )
        encoded = encode_effective_blocks(
            by_index,
            streams,
            effective,
            channel_order,
            args.report,
        )
    else:
        effective = {}
        encoded = {}
        print("No changed PNGs: every block will stay byte-for-byte original.")

    buffer = build_buffer(raw, by_index, encoded)
    validate_prewrite(raw, buffer, parsed["offsets"], by_index, encoded)
    verify_post_build(bytes(buffer), real_records, encoded, parsed["unpacked"])

    new_raw = write_output(
        buffer, source_path, out_path, args.force, args.dry_run
    )
    new_sha256 = _sha256(new_raw)

    print()
    print(f"sha256 source : {source_sha256}")
    print(f"sha256 result : {new_sha256}")
    print()
    print_diff_ranges(raw, new_raw)

    if encoded:
        print()
        print("Changed blocks:")
        for idx in sorted(encoded):
            info = encoded[idx]
            source_note = (
                f" (PNG of block {info['source']} via duplicate rule)"
                if info["source"] != idx
                else ""
            )
            print(
                f"  block {idx}: old size {info['old_size']}, "
                f"new size {info['new_size']}, "
                f"reserve {info['reserve']} of {info['capacity']} bytes"
                f"{source_note}"
            )
    else:
        print("No blocks changed.")

    print()
    if args.dry_run:
        print(
            "Dry-run finished successfully. Run again without --dry-run to "
            f"write {out_path.name}."
        )
    else:
        print(f"Done: {out_path}")
    print()
    print("REMINDER BEFORE FLASHING THE PARTITION:")
    print("  1. Keep a verified backup of the original partition image.")
    print(f"  2. Check that {out_path.name} is exactly {len(raw)} bytes.")
    print("  3. Write only to the LOGO partition, never blindly to another one.")
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="logotool.py",
        description=(
            "Inspect, decode and encode OnePlus/OPPO LOGO splash images. "
            "The tool never flashes anything by itself."
        ),
    )
    subparsers = parser.add_subparsers(dest="command", required=True)

    info = subparsers.add_parser(
        "info", help="print the block table without writing any files"
    )
    info.add_argument("image", help="LOGO partition image")
    info.set_defaults(func=cmd_info)

    decode = subparsers.add_parser(
        "decode", help="decode all real blocks to RGB PNG files and a manifest"
    )
    decode.add_argument("image", help="LOGO partition image")
    decode.add_argument("outdir", help="output directory for PNG files and manifest.json")
    decode.add_argument(
        "--channel-order",
        choices=("bgr", "rgb"),
        default="bgr",
        help="channel order of the compressed stream (default: bgr)",
    )
    decode.add_argument(
        "--force",
        action="store_true",
        help="allow writing into a non-empty output directory",
    )
    decode.set_defaults(func=cmd_decode)

    encode = subparsers.add_parser(
        "encode", help="replace changed blocks from PNG files and write a new image"
    )
    encode.add_argument("image", help="source LOGO partition image")
    encode.add_argument("workdir", help="directory containing manifest.json and PNGs")
    encode.add_argument(
        "--out", required=True, help="output image file (never the input file)"
    )
    encode.add_argument(
        "--only",
        action="append",
        metavar="INDEX_OR_NAME",
        help=(
            "process only this block index or name (repeatable, comma-separated "
            "values are accepted)"
        ),
    )
    encode.add_argument(
        "--apply-to-duplicates",
        action="store_true",
        help="apply a changed PNG to every block in its duplicate group",
    )
    encode.add_argument(
        "--channel-order",
        choices=("bgr", "rgb"),
        help="override the channel order stored in manifest.json",
    )
    encode.add_argument(
        "--flatten",
        action="store_true",
        help="composite RGBA (or LA/PA) PNGs over black before encoding",
    )
    encode.add_argument(
        "--snap-threshold",
        type=int,
        metavar="N",
        help=(
            "make pixels lighter than N white and all other pixels black "
            "(useful against colour noise from image editors)"
        ),
    )
    encode.add_argument(
        "--report",
        action="store_true",
        help="on a slot overflow, show the most expensive 40-row bands and colours",
    )
    encode.add_argument(
        "--dry-run",
        action="store_true",
        help="run all checks and build the image, but do not write the output file",
    )
    encode.add_argument(
        "--force",
        action="store_true",
        help="overwrite an existing output file",
    )
    encode.set_defaults(func=cmd_encode)
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)

    if getattr(args, "snap_threshold", None) is not None:
        if not 0 <= args.snap_threshold <= 255:
            parser.error("--snap-threshold must be between 0 and 255")

    try:
        return args.func(args)
    except FormatError as error:
        print(f"ERROR: {error}", file=sys.stderr)
        return 1
    except OSError as error:
        print(f"ERROR: {error}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
