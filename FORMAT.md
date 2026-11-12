# LOGO / splash image format

This document describes the format observed in a OnePlus 5 `LOGO` partition
dump. Some fields are still unknown; they are listed separately at the end.
All multi-byte integers are little-endian.

## Overall layout

The partition is a sequence of slots. Each slot contains a 0x200-byte header,
zero padding up to +0x1000, compressed pixel data, and zero padding up to the
next slot. The first slot also contains the offset table inside its header.

```
+0x0000  slot 0 header (includes the offset table)
+0x1000  slot 0 compressed data
...
+0x2000  slot 1 header
+0x3000  slot 1 compressed data
...
```

The file size comes from the file itself. The number of table entries is not
stored anywhere explicitly.

## Block header (0x200 bytes per slot)

| Offset | Size | Meaning |
| --- | --- | --- |
| +0x00 | 8 | Magic `SPLASH!!` for a real block |
| +0x08 | 0x18 | Unknown; zero in the reference dump |
| +0x20 | 4 | Image width in pixels (uint32) |
| +0x24 | 4 | Image height in pixels (uint32) |
| +0x28 | 4 | Compressed data size in bytes (uint32, even) |
| +0x2c | 4 | Unknown; `1` in real blocks, `0` in empty records |
| +0x30 | variable | Offset table, only in block 0 |
| +0x180 | 0x40 | Full name, NUL-terminated ASCII |
| +0x1c0 | 0x40 | Short name, NUL-terminated ASCII |
| +0x200 | 0xe00 | Zero padding in the reference dump |
| +0x1000 | ... | Compressed data |

The width and height in the header match the numbers embedded in the full
name, for example `fhd_oppo_1080_1920_result.raw`.

## Offset table

The table starts at +0x30 in block 0. Each entry is a 4-byte little-endian
offset. Offsets are 0x1000-aligned and strictly increasing. The table ends at
+0x180, where the full-name field starts.

The count is not stored. A parser can derive it by reading increasing,
0x1000-aligned offsets until the next value is not a valid next offset. In the
reference dump this yields 84 entries: 60 real blocks and 24 empty records.

The slot of entry `i` is:

```
slot = offsets[i + 1] - offsets[i]      (for all but the last entry)
slot = file_size - offsets[i]           (for the last entry)
```

The usable data capacity of a slot is `slot - 0x1000`, and the free reserve is
`capacity - compressed_size`.

## Pixel data and RLE

Pixel data is raw 8-bit colour, three bytes per pixel, row by row, with no row
padding. The stream order in the reference dump is BGR. `logotool.py` defaults
to BGR and offers `--channel-order rgb` for other dumps.

Compression is a simple byte-oriented RLE. The compressed stream is a sequence
of pairs:

```
value  count
```

`count` is 1..255 and is never zero. The pair expands to `count` copies of the
same byte. A run longer than 255 bytes is split into several pairs, each
carrying at most 255; the split is canonical, so recompressing already
compressed data produces exactly the same bytes. The decompressed size must be
exactly `width * height * 3`.

Example: 300 zero bytes become `00 FF 00 2D` (255 zeros, then 45 zeros).

## Slots and padding

Data begins at slot offset +0x1000. Bytes between the end of the compressed
data and the next slot offset are zero padding. In the reference dump this is
true for every real block, including the final block up to the end of the
partition.

The tool writes a shorter block by copying its data and then zeroing the rest
of that block's slot. It does not move blocks and does not change offsets.
Blocks that are not edited keep their original bytes exactly.

## Empty records

An empty record has no `SPLASH!!` magic. Fields +0x20..+0x2f are zero, there is
no pixel data, and only the short name at +0x1c0 is populated. In the reference
dump its slot is 0x1000 (header only). The 24 empty records carry names such as
`hd720_oppo`, `hd720_charger`, `fwvga_oppo`, `wqxga_battery`, and so on.

These records look like reserved placeholders for other resolutions. Their
exact meaning is not proven; see the unknown list below.

## Duplicate blocks

Blocks can contain byte-identical compressed data. The tool groups them by
SHA-256 of the compressed bytes. Examples from the reference dump:

- blocks 1, 9, 17, 25, 34, 43, 76 (536x60);
- blocks 5, 13, 21, 29, 38, 47, 80 (392x66);
- blocks 6, 14, 22, 30, 39, 48, 81 (487x69);
- blocks 77 and 79 (1080x2376).

If these blocks are meant to stay identical, change them together. `encode`
warns about duplicate groups and has `--apply-to-duplicates` for this case.

## Unknown or unproven parts

- Bytes +0x08..+0x1f in the header: zero in the reference dump; purpose
  unknown.
- Field +0x2c: exactly `1` in every real block and `0` in every empty record
  in the reference dump. It may be a format/type flag, but one dump is not
  enough to prove it.
- Meaning and intended use of the empty records (24 of them in the reference
  dump): unknown.
- How `aboot` selects a block for a given panel/state: not established. The
  tool never changes the table, so selection behaviour should be unchanged.
- The table count is not stored explicitly; it is inferred from the
  table/name boundary and the shape of the offset sequence.
- Validation covers one OnePlus 5 dump only. Other devices and other LOGO
  partitions may use different offsets, dimensions, channel order, padding or
  unknown fields.
