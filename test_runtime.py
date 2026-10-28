"""The shipped runtime works without SDK headers, a compiler, or an absolute build path."""

import json
from pathlib import Path
import platform
import shutil
import subprocess
import tempfile
import unittest
import zipfile

from camera import DEFAULT_HELPER
from scripts.build_sony import LIBRARIES, RUNTIME


class RuntimeTests(unittest.TestCase):
    def test_bundle_contains_runtime_not_sdk_headers(self):
        self.assertEqual(DEFAULT_HELPER, RUNTIME / "macos/sony_camera")
        for name in ("sony_camera", *LIBRARIES):
            self.assertTrue((RUNTIME / "macos" / name).is_file(), name)
        self.assertFalse(list(RUNTIME.rglob("*.h")))
        with zipfile.ZipFile(RUNTIME / "licenses/libusb-source.zip") as source:
            self.assertEqual(source.read("libusb/COPYING"), (RUNTIME / "licenses/libusb-COPYING.txt").read_bytes())
            self.assertIn("libusb/configure.ac", source.namelist())

    @unittest.skipUnless(platform.system() == "Darwin", "The bundled runtime targets macOS")
    def test_relocated_runtime_loads_without_opening_a_camera(self):
        with tempfile.TemporaryDirectory(prefix="futurelens clone ") as folder:
            relocated = Path(folder) / "runtime"
            shutil.copytree(RUNTIME / "macos", relocated)
            result = subprocess.run([str(relocated / "sony_camera"), "--check-runtime"],
                                    cwd=folder, capture_output=True, text=True, timeout=20)
            self.assertEqual(result.returncode, 0, result.stderr + result.stdout)
            report = json.loads(result.stdout)
            self.assertEqual(report["event"], "runtime")
            version = json.loads((RUNTIME / "version.json").read_text())
            self.assertEqual(report["sdk_version"], version["sdk_version"])
            self.assertEqual({p.name for p in Path(folder).iterdir()}, {"runtime"})

    @unittest.skipUnless(platform.system() == "Darwin", "Mach-O architecture check")
    def test_helper_and_sony_libraries_include_both_mac_architectures(self):
        # Read the universal Mach-O header directly; end users need no developer tools.
        for name in ("sony_camera", *LIBRARIES):
            with (RUNTIME / "macos" / name).open("rb") as binary:
                magic = int.from_bytes(binary.read(4), "big")
                self.assertEqual(magic, 0xcafebabe, name)
                count = int.from_bytes(binary.read(4), "big")
                architectures = set()
                for _ in range(count):
                    architectures.add(int.from_bytes(binary.read(4), "big"))
                    binary.read(16)
                self.assertEqual(architectures, {0x01000007, 0x0100000c}, name)


if __name__ == "__main__":
    unittest.main()
