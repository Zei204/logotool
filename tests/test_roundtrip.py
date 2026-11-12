from __future__ import annotations

import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

from PIL import Image

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent
sys.path.insert(0, str(HERE))

from synthetic import build_synthetic_image  # noqa: E402

TOOL = ROOT / "logotool.py"


def run_tool(*args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, str(TOOL), *args],
        cwd=str(ROOT),
        text=True,
        capture_output=True,
    )


class RoundTripTest(unittest.TestCase):
    def test_decode_encode_is_byte_identical(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_name:
            tmp = Path(tmp_name)
            image_path = tmp / "synthetic.img"
            meta = build_synthetic_image(image_path)
            original = image_path.read_bytes()

            info = run_tool("info", str(image_path))
            self.assertEqual(info.returncode, 0, info.stderr)
            self.assertIn("Blocks: 3 real, 1 empty", info.stdout)
            self.assertIn("D01", info.stdout)

            outdir = tmp / "decoded"
            decode = run_tool("decode", str(image_path), str(outdir))
            self.assertEqual(decode.returncode, 0, decode.stderr)

            manifest = json.loads((outdir / "manifest.json").read_text("utf-8"))
            self.assertEqual(manifest["channel_order"], "BGR")
            self.assertEqual(len(manifest["records"]), 4)
            self.assertEqual(
                [record["index"] for record in manifest["records"] if record["real"]],
                [0, 1, 2],
            )
            self.assertTrue(manifest["records"][0]["header_0x200_hex"])
            self.assertEqual(manifest["records"][0]["field_0x2c"], 1)
            self.assertEqual(manifest["records"][3]["kind"], "empty")

            alpha_png = Image.open(outdir / "00_test_alpha_12_8.png").convert("RGB")
            self.assertEqual(alpha_png.tobytes(), meta["alpha"].tobytes())
            beta_png = Image.open(outdir / "01_test_beta_9_7.png").convert("RGB")
            self.assertEqual(beta_png.tobytes(), meta["beta"].tobytes())

            rebuilt = tmp / "rebuilt.img"
            encode = run_tool(
                "encode", str(image_path), str(outdir), "--out", str(rebuilt)
            )
            self.assertEqual(encode.returncode, 0, encode.stderr)
            self.assertEqual(rebuilt.read_bytes(), original)

            decode_again = run_tool("decode", str(image_path), str(outdir))
            self.assertNotEqual(decode_again.returncode, 0)
            self.assertIn("--force", decode_again.stderr)
            forced = run_tool(
                "decode", str(image_path), str(outdir), "--force"
            )
            self.assertEqual(forced.returncode, 0, forced.stderr)

    def test_help_does_not_create_files(self) -> None:
        before = set(ROOT.iterdir())
        result = run_tool("--help")
        self.assertEqual(result.returncode, 0)
        self.assertIn("info", result.stdout)
        after = set(ROOT.iterdir())
        self.assertEqual(before, after)


if __name__ == "__main__":
    unittest.main()
