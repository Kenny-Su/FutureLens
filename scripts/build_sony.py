"""Maintainer build: compile the macOS helper, optionally refresh its bundled runtime."""

import argparse
import json
from pathlib import Path
import platform
import shutil
import subprocess
import zipfile

ROOT = Path(__file__).resolve().parents[1]
RUNTIME = ROOT / "runtime/sony"
ADAPTERS = Path("Contents/Frameworks/CrAdapter")
LIBRARIES = (
    "libCr_Core.dylib", "libmonitor_protocol.dylib", "libmonitor_protocol_pf.dylib",
    *(str(ADAPTERS / name) for name in
      ("libCr_PTP_IP.dylib", "libCr_PTP_USB.dylib", "libssh2.dylib", "libusb-1.0.0.dylib")),
)


def build(sdk, output):
    headers = output / "include"
    headers.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(sdk) as archive:
        for item in archive.infolist():
            source = Path(item.filename)
            if not item.is_dir() and source.parent.as_posix() == "app/CRSDK" and source.suffix == ".h":
                (headers / source.name).write_bytes(archive.read(item))
        for name in LIBRARIES:
            destination = output / name
            source = "external/crsdk/" + ("CrAdapter/" + destination.name if "/" in name else name)
            destination.parent.mkdir(parents=True, exist_ok=True)
            destination.write_bytes(archive.read(source))
    subprocess.run([
        "clang++", "-std=c++17", "-O2", "-Wall", "-Wextra", "-Wno-unused-parameter",
        "-arch", "arm64", "-arch", "x86_64", "-mmacosx-version-min=14.1",
        "-isystem", str(headers), str(ROOT / "native/sony_camera.cpp"),
        "-L", str(output), "-lCr_Core", "-Wl,-rpath,@executable_path",
        "-o", str(output / "sony_camera"),
    ], check=True)


def bundle(sdk, source_zip, output):
    # Keep Sony's binaries and their runtime layout unchanged; headers stay private to the build.
    with zipfile.ZipFile(sdk) as archive, zipfile.ZipFile(source_zip) as source:
        notices = archive.read("README.md").decode().split("## copyright notice and disclaimer for OSS", 1)[1]
        copying, authors = source.read("libusb/COPYING"), source.read("libusb/AUTHORS")
    for name in ("sony_camera", *LIBRARIES):
        destination = RUNTIME / "macos" / name
        destination.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(output / name, destination)
        destination.chmod(0o755 if name == "sony_camera" else 0o644)
    licenses = RUNTIME / "licenses"
    licenses.mkdir(parents=True, exist_ok=True)
    (licenses / "SDK-OSS-NOTICES.md").write_text("# Third-party notices supplied with Sony Camera Remote SDK\n" + notices)
    (licenses / "libusb-COPYING.txt").write_bytes(copying)
    (licenses / "libusb-AUTHORS.txt").write_bytes(authors)
    shutil.copyfile(source_zip, licenses / "libusb-source.zip")
    result = subprocess.run([str(RUNTIME / "macos/sony_camera"), "--check-runtime"],
                            capture_output=True, text=True, check=True)
    metadata = json.loads(result.stdout)
    (RUNTIME / "version.json").write_text(json.dumps({
        "sdk_version": metadata["sdk_version"],
        "platform": "macOS", "minimum_os": "14.1", "architectures": ["arm64", "x86_64"],
    }, indent=2) + "\n")
    print(f"Bundled camera runtime: {RUNTIME / 'macos'}")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--sdk", type=Path, default=Path.home() / "Downloads/CrSDK_v2/SimpleCli.zip")
    parser.add_argument("--bundle", action="store_true", help="Refresh the runtime shipped with FutureLens")
    parser.add_argument("--libusb-source", type=Path, help="Sony-supplied libusb.zip; defaults to the SDK's sibling source folder")
    args = parser.parse_args()
    if platform.system() != "Darwin":
        parser.error("This build script currently supports macOS only.")
    sdk = args.sdk.resolve()
    source_zip = args.libusb_source or sdk.parent / "SourceCodeOfOpenSourceSoftware/libusb.zip"
    if args.bundle and not source_zip.is_file():
        parser.error("Bundling requires Sony's libusb source archive alongside the runtime.")
    output = ROOT / "build/sony"
    build(sdk, output)
    if args.bundle:
        bundle(sdk, source_zip, output)
    print(output / "sony_camera")


if __name__ == "__main__":
    main()
