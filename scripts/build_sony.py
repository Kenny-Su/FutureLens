"""Build the macOS camera helper from the user's downloaded Sony SimpleCli.zip."""

import argparse
from pathlib import Path
import platform
import shutil
import subprocess
import zipfile


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--sdk", type=Path, default=Path.home() / "Downloads/CrSDK_v2/SimpleCli.zip")
    args = parser.parse_args()
    if platform.system() != "Darwin":
        parser.error("This build script currently supports macOS only.")
    root = Path(__file__).resolve().parents[1]
    build = root / "build/sony"
    headers = build / "include"
    adapters = build / "Contents/Frameworks/CrAdapter"
    headers.mkdir(parents=True, exist_ok=True)
    adapters.mkdir(parents=True, exist_ok=True)
    # Extract only the headers and runtime libraries needed by our own helper.
    with zipfile.ZipFile(args.sdk) as archive:
        for item in archive.infolist():
            source = Path(item.filename)
            if item.is_dir():
                continue
            if source.parent.as_posix() == "app/CRSDK" and source.suffix == ".h":
                target = headers / source.name
            elif source.parent.as_posix() == "external/crsdk" and source.suffix == ".dylib":
                target = build / source.name
            elif source.parent.as_posix() == "external/crsdk/CrAdapter" and source.suffix == ".dylib":
                target = adapters / source.name
            else:
                continue
            with archive.open(item) as src, target.open("wb") as dst:
                shutil.copyfileobj(src, dst)
    subprocess.run([
        "clang++", "-std=c++17", "-O2", "-Wall", "-Wextra", "-Wno-unused-parameter",
        "-isystem", str(headers), str(root / "native/sony_camera.cpp"),
        "-L", str(build), "-lCr_Core", "-Wl,-rpath,@executable_path",
        "-o", str(build / "sony_camera"),
    ], check=True)
    print(build / "sony_camera")


if __name__ == "__main__":
    main()
