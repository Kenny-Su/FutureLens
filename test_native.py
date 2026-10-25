"""Compile and exercise the native capability policy without a camera or SDK calls."""

from pathlib import Path
import shutil
import subprocess
import tempfile
import unittest


class NativeCapabilityTests(unittest.TestCase):
    def test_camera_capability_policy(self):
        root = Path(__file__).resolve().parent
        headers = root / "build/sony/include"
        if not shutil.which("clang++") or not (headers / "CrDeviceProperty.h").is_file():
            self.skipTest("Build the Sony helper first to install the SDK headers; clang++ is required")
        with tempfile.TemporaryDirectory() as folder:
            binary = Path(folder) / "capability_tests"
            compiled = subprocess.run(["clang++", "-std=c++17", "-Wall", "-Wextra", "-Werror", "-isystem", str(headers),
                                       str(root / "native/test_camera_capabilities.cpp"), "-o", str(binary)],
                                      capture_output=True, text=True)
            self.assertEqual(compiled.returncode, 0, compiled.stdout + compiled.stderr)
            result = subprocess.run([str(binary)], capture_output=True, text=True)
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)


if __name__ == "__main__":
    unittest.main()
