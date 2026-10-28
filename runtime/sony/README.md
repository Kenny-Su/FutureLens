# FutureLens camera runtime

This directory is an application component shipped with FutureLens. Users do not
need to obtain Sony's SDK, install a compiler, or build the camera helper.

`macos/sony_camera` is our prebuilt universal helper for Apple silicon and Intel
Macs (macOS 14.1 or later, subject to Sony's supported OS/firmware combinations).
The `.dylib` files are unmodified binaries from Camera Remote SDK **2.02.00**.
`version.json` records Sony's SDK version and the supported operating system and architectures.
The complete `external/crsdk` runtime from Sony's distribution is retained,
including libraries used by SDK internals. SDK headers, sample applications,
and the SDK documentation are not distributed here.

Sony's [license](https://support.d-imaging.sony.co.jp/app/sdk/licenseagreement/en.html)
continues to govern its proprietary libraries. Distribute this component with
FutureLens; do not treat it as a standalone SDK or relicense Sony's files as
project source. FutureLens is independently developed and supported.

## Open-source notices and source

- `licenses/SDK-OSS-NOTICES.md` preserves the notices supplied in Sony's sample
  package, including libssh2, OpenSSL, OpenCV, libusbK, and cpp-httplib. Some listed
  components apply to other SDK platforms or samples; the notices are retained.
- `licenses/libusb-COPYING.txt` and `licenses/libusb-AUTHORS.txt` reproduce the
  libusb license and attribution from Sony's supplied source archive.
- `licenses/libusb-source.zip` is that complete, unmodified source archive,
  distributed alongside the binary under LGPL 2.1. It includes build scripts.

libusb is dynamically loaded. Its license permits rebuilding and replacing that
library, and debugging modifications under the LGPL's terms; Sony's restrictions
do not override the separate open-source license. The source archive includes
`README.git`, `README.md`, and bootstrap/configure scripts. Build for the architecture you use, then
replace `macos/Contents/Frameworks/CrAdapter/libusb-1.0.0.dylib` with a compatible
build. Universal distributions need both architecture slices.

## Maintainers

Rebuild the helper and refresh this directory with:

```bash
.venv/bin/python scripts/build_sony.py --sdk /path/to/SimpleCli.zip --bundle
```

Keep Sony's sibling `SourceCodeOfOpenSourceSoftware/libusb.zip` available, or
supply `--libusb-source /path/to/libusb.zip`. Only maintainers rebuilding native
code need the SDK headers and Apple's command-line developer tools. CMake and
full Xcode are not required by our script.

Run `macos/sony_camera --check-runtime` to load all bundled libraries and report
the SDK version without initializing the SDK or opening a camera. The helper
uses relative runtime paths, so a checkout can be moved to another directory.
Runtime loading has been checked on Apple silicon. The Intel slice is included
and checked structurally; it has not been run on Intel hardware here. The binaries
have not been notarized for a standalone macOS installer.
