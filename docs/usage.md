# Using FutureLens

[Back to the project overview](../README.md)

This guide covers setup, camera operation, troubleshooting, and development.
Run the commands from the repository root. FutureLens currently runs as a CLI on
macOS and controls one Sony camera over USB.

Anthropic is the only supported API provider. The decision function uses its Messages API
with **Claude Haiku 5.5**
(`claude-haiku-5-5`). Each request sends one live-view image and one instruction,
and a JSON schema constrains the result to `{"shot": true}` or `{"shot": false}`.
Low effort and disabled thinking keep this small classification task focused on latency.
The model has no frame history. Insufficient visual evidence means false; refusals,
invalid answers, and API failures stop the run with an error.

## Camera compatibility

See [Sony's official compatibility page](https://support.d-imaging.sony.co.jp/app/sdk/en/)
for supported cameras and firmware requirements. Consult the API Reference included
with your SDK for each camera's supported connection types. FutureLens uses USB;
network connections are outside its current scope. Newly supported cameras may
require an SDK update and a helper rebuild.

Hardware testing so far used Sony Camera Remote SDK 2.02.00 with an A7 IV
(ILCE-7M4). Other models need on-device verification. FutureLens checks the
connected camera's capabilities before enabling capture.

## Setup

Use Python 3.11+ in a project virtual environment:

```bash
python3 -m venv .venv
.venv/bin/python -m pip install -r requirements.txt
```

Dependencies stay in `.venv`. Configure the project `.env`:

```dotenv
ANTHROPIC_API_KEY=your-key
```

Previews go directly to `https://api.anthropic.com/v1/messages` using `claude-haiku-5-5`.
The endpoint and model are fixed in code. Environment variables override `.env`
for credentials; keys belong in the ignored local `.env` file.
If your key requires an explicit workspace, also set `ANTHROPIC_WORKSPACE_ID`.

The HTTP client uses API version `2026-10-21`. No automatic retries, alternate endpoints,
or model fallbacks are used. Images sent to the API are resized to a maximum edge of
960 pixels; saved photos are the camera's separate captures. API errors retain the HTTP status,
known error type, numeric retry delay, and request ID when available. Free-form provider
error text, credentials, and encoded images are not logged.

## Control a Sony camera over USB (macOS)

Connect one supported Sony camera by USB and select **Remote Shooting / PC Remote** on the camera.
Use still-photo mode and **Single Shooting** drive mode. Keep exposure and image format
configured on the camera. Manual focus is useful for a fixed scene; with autofocus,
the helper half-presses and waits briefly for focus before triggering the shutter.
Close other applications that are controlling the same camera.

Build the native helper using Apple's command-line developer tools and your downloaded
Sony SDK. CMake and a full Xcode installation are not required:

```bash
.venv/bin/python scripts/build_sony.py \
  --sdk "$HOME/Downloads/CrSDK_v2/SimpleCli.zip"
```

The script extracts Sony's headers and runtime libraries unchanged, then calls
`clang++` directly to compile our helper and link it to `libCr_Core`. It preserves
the SDK's required runtime-library layout. The files stay under the ignored
`build/sony/` directory, and the original SDK download remains untouched.
Rebuild after source changes; the helper is `build/sony/sony_camera`, and the Python
adapter checks its capability-protocol version.

The helper considers every USB camera enumerated by the installed Sony SDK, without
a fixed model whitelist. Network cameras are ignored. If multiple USB cameras are
found, it lists their models and asks you to connect only one; it never chooses the
first arbitrarily. Camera/firmware compatibility is defined by the installed SDK;
see [Sony's supported-device list](https://support.d-imaging.sony.co.jp/app/sdk/en/).

Before capture, FutureLens checks live-view availability, the advertised shutter
command, still-photo exposure mode, Single Shooting drive mode, computer-transfer
options, file type, and focus controls. It validates allowed property values as well as writability. All checks
finish before capture settings are changed, and run again before each shutter
command. Startup failures stop the run with an explanation before any model
request; later setting changes can stop a capture before the shutter command. A camera with live view
but no supported still-photo workflow can still be used in preview/dry-run mode.

Autofocus requires remote half-press and readable focus confirmation. If either is
unavailable, select MF on the camera; the app does not change the focus mode for you.
SDK compatibility alone does not guarantee this particular still-photo workflow.
Offline tests cover different capability combinations.

Check detection, capabilities, and a preview locally, without an API call or shutter command:

```bash
.venv/bin/python camera.py
```

Add `--capture` to take **one actual test photograph** and download it. Each test
creates a new output directory containing `preview.jpg`, `camera-test.json`,
`camera-sdk.log`, and any downloaded photos under `moments/`. `camera-test.json`
records the actual model and capability assessment, including reasons capture is
unavailable. Preview mode does not change capture settings.

Run the live decision workflow:

```bash
.venv/bin/python futurelens.py \
  --request "Photograph as soon as a real person is clearly visible in the scene" \
  --duration 30 --fps 1 --max-requests 60 --max-shots 1
```

`--dry-run` retrieves and saves live previews without loading API credentials, making
API calls, sending shutter commands, or changing capture settings. `--fps` defaults to
1 check per second and sets an upper sampling rate, not a promised decision rate.
Each iteration requests the latest SDK live-view image after the preceding decision
finishes; previews are not queued. `--duration` is wall-clock monitoring
time after connection/setup, and `--max-requests` is enforced during the run.

The application rejects decisions when the monitoring duration has expired or the
preview request is older than `--max-frame-age` (default 3 seconds). The remaining
time is also passed to the helper so focusing cannot silently use up the deadline.
This age measures elapsed host time, not the camera sensor's exact exposure timestamp.
SDK/USB latency and shutter latency still apply.

The first true decision triggers a photo. A false decision rearms capture; a
`--cooldown` of 5 seconds also applies after a completed capture. `--max-shots`
defaults to 1; increase it to photograph additional false-to-true transitions.
For the person-entry test, start with an empty scene and then walk into view. The model
evaluates presence in the current image; a person already in view can trigger the first shot.
The helper temporarily selects computer + memory-card storage when available,
or computer-only storage when that is the supported option. The selected storage
strategy is recorded in the capability report. It requests original-size transfer
where the camera exposes that setting; cameras without an adjustable transfer-size
property use their native transfer behavior, reported as `camera_default`.
The helper attempts to restore changed settings on normal exit or errors while
the connection remains available. It preserves the camera's exposure and image
format. Downloaded files can therefore be JPEG, HEIF, RAW, or a pair, according to
the camera's recording and PC-save settings.

`results.json` separates the model's `shot` decision from `capture_status`:
requested, shutter sent, downloaded, skipped, or failed. Only a completed download
with a nonempty file is counted as a successful capture. A timeout or disconnect
stops the run without retrying an uncertain shutter command. A photo may still be
on the memory card after a transfer failure. Ctrl-C closes the session; an already
started capture may need time to finish and release the shutter.
The report also records the detected `camera_model`, a matching `source`, and
`camera_capabilities`; it does not assume an A7 IV.

When launched from a restricted coding sandbox, the SDK may report no camera even
though macOS sees it. The helper needs USB-device access; run it in a normal local
terminal or grant the app's execution request for USB access.

If the camera is detected but connection fails with SDK error `0x8208`, confirm
Remote Shooting / PC Remote mode and quit Preview, Image Capture, and other apps
using the camera. Preview can keep macOS's `ptpcamerad` service attached to the USB
interface, even while you are not importing photos. In the verified A7 IV case,
quitting Preview released the interface and restored connection without `sudo`.
Stopping `ptpcamerad` alone was ineffective because Preview restarted it. Other
connection-timeout causes are still possible; a USB handle alone does not prove
an app is blocking the SDK.

## Code and checks

Our native bridge uses Sony's SDK headers and libraries directly. It provides the
persistent Python command interface, USB selection, capability checks, and capture
safeguards. Claude decisions, timing, and capture limits remain in Python.

- `model.py`: live-image `decide` function, Claude Messages API connection, and strict boolean parsing.
- `futurelens.py`: Sony camera CLI, timing, capture policy, and result recording.
- `camera.py`: persistent SDK-process adapter and offline camera smoke test.
- `native/sony_camera.cpp`: USB connection, live view, autofocus/shutter, and downloads.
- `native/camera_capabilities.h`: USB selection and capability-based capture policy.
- `scripts/build_sony.py`: local macOS helper build using the downloaded SDK and `clang++`.
- `test_model.py`: mocked decision and API configuration checks.
- `test_camera.py`: mocked live timing, capture limits, download failures, and cleanup.
- `test_native.py` / `native/test_camera_capabilities.cpp`: offline USB-selection and
  capability checks, compiled against the installed SDK headers without connecting to a camera.

```bash
.venv/bin/python -m unittest -v
```

[Claude Haiku 5.5](https://platform.claude.com/docs/en/models/haiku-5-5/overview),
[structured outputs](https://platform.claude.com/docs/en/build-with-claude/structured-outputs),
and [effort settings](https://platform.claude.com/docs/en/build-with-claude/effort).
