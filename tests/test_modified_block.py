from __future__ import annotations

import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

from PIL import Image, ImageDraw

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent
sys.path.insert(0, str(HERE))
sys.path.insert(0, str(ROOT))

import logotool  # noqa: E402
from synthetic import build_small_slot_image, build_synthetic_image  # noqa: E402

TOOL = ROOT / "logotool.py"


def run_tool(*args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, str(TOOL), *args],
        cwd=str(ROOT),
        text=True,
        capture_output=True,
    )


class ModifiedBlockTest(unittest.TestCase):
    def test_changed_block_only_and_duplicate_flag(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_name:
            tmp = Path(tmp_name)
            image_path = tmp / "synthetic.img"
            meta = build_synthetic_image(image_path)
            original = image_path.read_bytes()
            offsets = meta["offsets"]

            outdir = tmp / "decoded"
            decode = run_tool("decode", str(image_path), str(outdir))
            self.assertEqual(decode.returncode, 0, decode.stderr)

            alpha_png = outdir / "00_test_alpha_12_8.png"
            edited = Image.open(alpha_png).convert("RGB")
            draw = ImageDraw.Draw(edited)
            draw.rectangle((0, 7, 4, 7), fill=(255, 0, 255))
            edited.save(alpha_png)
            edited_bytes = edited.tobytes()

            modified_path = tmp / "modified.img"
            encode = run_tool(
                "encode",
                str(image_path),
                str(outdir),
                "--only",
                "0",
                "--out",
                str(modified_path),
            )
            self.assertEqual(encode.returncode, 0, encode.stderr)

            modified = modified_path.read_bytes()
            self.assertEqual(len(modified), len(original))
            self.assertEqual(modified[: offsets[0]], original[: offsets[0]])
            self.assertEqual(modified[offsets[1] :], original[offsets[1] :])
            self.assertNotEqual(
                modified[offsets[0] : offsets[1]],
                original[offsets[0] : offsets[1]],
            )

            checkdir = tmp / "check"
            check = run_tool("decode", str(modified_path), str(checkdir))
            self.assertEqual(check.returncode, 0, check.stderr)
            self.assertEqual(
                Image.open(checkdir / "00_test_alpha_12_8.png").convert("RGB").tobytes(),
                edited_bytes,
            )
            self.assertEqual(
                Image.open(checkdir / "01_test_beta_9_7.png").convert("RGB").tobytes(),
                meta["beta"].tobytes(),
            )
            self.assertEqual(
                Image.open(checkdir / "02_test_gamma_12_8.png").convert("RGB").tobytes(),
                meta["alpha"].tobytes(),
            )

            duplicate_path = tmp / "duplicate.img"
            duplicate = run_tool(
                "encode",
                str(image_path),
                str(outdir),
                "--only",
                "test_alpha",
                "--apply-to-duplicates",
                "--out",
                str(duplicate_path),
            )
            self.assertEqual(duplicate.returncode, 0, duplicate.stderr)

            dupdir = tmp / "dupcheck"
            dupcheck = run_tool("decode", str(duplicate_path), str(dupdir))
            self.assertEqual(dupcheck.returncode, 0, dupcheck.stderr)
            self.assertEqual(
                Image.open(dupdir / "00_test_alpha_12_8.png").convert("RGB").tobytes(),
                edited_bytes,
            )
            self.assertEqual(
                Image.open(dupdir / "02_test_gamma_12_8.png").convert("RGB").tobytes(),
                edited_bytes,
            )

    def test_refuses_existing_output_without_force(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_name:
            tmp = Path(tmp_name)
            image_path = tmp / "synthetic.img"
            build_synthetic_image(image_path)
            outdir = tmp / "decoded"
            self.assertEqual(
                run_tool("decode", str(image_path), str(outdir)).returncode, 0
            )

            output = tmp / "out.img"
            first = run_tool(
                "encode", str(image_path), str(outdir), "--out", str(output)
            )
            self.assertEqual(first.returncode, 0, first.stderr)
            second = run_tool(
                "encode", str(image_path), str(outdir), "--out", str(output)
            )
            self.assertNotEqual(second.returncode, 0)
            self.assertIn("--force", second.stderr)
            forced = run_tool(
                "encode",
                str(image_path),
                str(outdir),
                "--out",
                str(output),
                "--force",
            )
            self.assertEqual(forced.returncode, 0, forced.stderr)

    def test_refuses_overwriting_input_and_dry_run_writes_nothing(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_name:
            tmp = Path(tmp_name)
            image_path = tmp / "synthetic.img"
            build_synthetic_image(image_path)
            original = image_path.read_bytes()
            outdir = tmp / "decoded"
            self.assertEqual(
                run_tool("decode", str(image_path), str(outdir)).returncode, 0
            )

            overwrite = run_tool(
                "encode", str(image_path), str(outdir), "--out", str(image_path)
            )
            self.assertNotEqual(overwrite.returncode, 0)
            self.assertIn("input", overwrite.stderr)
            self.assertEqual(image_path.read_bytes(), original)

            dry_path = tmp / "dry.img"
            dry = run_tool(
                "encode",
                str(image_path),
                str(outdir),
                "--out",
                str(dry_path),
                "--dry-run",
            )
            self.assertEqual(dry.returncode, 0, dry.stderr)
            self.assertFalse(dry_path.exists())

    def test_rgba_requires_flatten_and_snap_threshold(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_name:
            tmp = Path(tmp_name)
            image_path = tmp / "synthetic.img"
            build_synthetic_image(image_path)
            outdir = tmp / "decoded"
            self.assertEqual(
                run_tool("decode", str(image_path), str(outdir)).returncode, 0
            )

            alpha_png = outdir / "00_test_alpha_12_8.png"
            rgba = Image.open(alpha_png).convert("RGBA")
            rgba.putalpha(128)
            rgba.save(alpha_png)

            rejected = run_tool(
                "encode",
                str(image_path),
                str(outdir),
                "--out",
                str(tmp / "rgba.img"),
            )
            self.assertNotEqual(rejected.returncode, 0)
            self.assertIn("--flatten", rejected.stderr)

            flattened = run_tool(
                "encode",
                str(image_path),
                str(outdir),
                "--out",
                str(tmp / "flat.img"),
                "--flatten",
                "--snap-threshold",
                "128",
            )
            self.assertEqual(flattened.returncode, 0, flattened.stderr)

            checkdir = tmp / "check"
            self.assertEqual(
                run_tool("decode", str(tmp / "flat.img"), str(checkdir)).returncode,
                0,
            )
            result = Image.open(checkdir / "00_test_alpha_12_8.png").convert("RGB")
            pixels = result.tobytes()
            self.assertTrue(
                all(
                    pixels[pos : pos + 3] in (b"\x00\x00\x00", b"\xff\xff\xff")
                    for pos in range(0, len(pixels), 3)
                )
            )

    def test_overflow_report_and_hint(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_name:
            tmp = Path(tmp_name)
            image_path = tmp / "noise.img"
            build_small_slot_image(image_path)
            outdir = tmp / "decoded"
            self.assertEqual(
                run_tool("decode", str(image_path), str(outdir)).returncode, 0
            )

            import random

            noisy_png = outdir / "00_noise_64_64.png"
            rng = random.Random(99)
            noise = bytes(rng.randrange(256) for _ in range(64 * 64 * 3))
            Image.frombytes("RGB", (64, 64), noise).save(noisy_png)

            result = run_tool(
                "encode",
                str(image_path),
                str(outdir),
                "--out",
                str(tmp / "noise_new.img"),
                "--report",
            )
            self.assertNotEqual(result.returncode, 0)
            self.assertIn("--snap-threshold", result.stderr)
            self.assertIn("--report", result.stderr)
            self.assertIn("Compression report for block 0", result.stdout)
            self.assertIn("rows", result.stdout)
            self.assertFalse((tmp / "noise_new.img").exists())

    def test_only_ranges_accept_both_index_and_name(self) -> None:
        records = [
            {
                "index": 7,
                "real": True,
                "full_name": "fhd_oppo_1080_1920_result.raw",
                "short_name": "fhd_oppo",
            }
        ]
        self.assertEqual(logotool.parse_only(["7"], records), {7})
        self.assertEqual(logotool.parse_only(["fhd_oppo"], records), {7})
        self.assertEqual(
            logotool.parse_only(["fhd_oppo_1080_1920_result.raw"], records), {7}
        )


if __name__ == "__main__":
    unittest.main()
