# FutureLens

**Sony A7 IV live view + a user instruction → take a photograph when the condition is met.**

FutureLens controls one Sony A7 IV (ILCE-7M4) over USB on macOS. It retrieves a fresh
live-view image, evaluates your instruction, triggers the camera shutter when appropriate,
and downloads the actual photograph. Sony USB control is the only application workflow.

Anthropic is the only supported API provider. The decision function uses its Messages API
with **Claude Haiku 5.5**
(`claude-haiku-5-5`). Each request sends one live-view image and one instruction,
and a JSON schema constrains the result to `{"shot": true}` or `{"shot": false}`.
Low effort and disabled thinking keep this small classification task focused on latency.
The model has no frame history. Insufficient visual evidence means false; refusals,
invalid answers, and API failures stop the run with an error.

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
960 pixels; saved photos keep the original resolution. API errors retain the HTTP status,
known error type, numeric retry delay, and request ID when available. Free-form provider
error text, credentials, and encoded images are not logged.

## Control a Sony A7 IV over USB (macOS)

Connect the ILCE-7M4 by USB and select **Remote Shooting / PC Remote** on the camera.
Use still-photo mode and **Single Shooting** drive mode. Keep exposure and image format
configured on the camera. Manual focus is useful for a fixed scene; with autofocus,
the helper half-presses and waits briefly for focus before triggering the shutter.
Close other applications that are controlling the same camera.

Build the native helper using Apple's command-line developer tools and your downloaded
Sony SDK. CMake and a full Xcode installation are not required by this build script:

```bash
.venv/bin/python scripts/build_sony.py \
  --sdk "$HOME/Downloads/CrSDK_v2/SimpleCli.zip"
```

The Sony libraries and headers stay under the ignored `build/sony/` directory;
the original SDK download is left in place. The helper currently selects one USB
ILCE-7M4 and rejects ambiguous multiple-camera connections.

Check the connection locally, without an API call:

```bash
.venv/bin/python camera.py
```

Add `--capture` to take **one actual test photograph** and download it. Each test
creates a new output directory containing `preview.jpg`, `camera-test.json`,
`camera-sdk.log`, and any downloaded photos under `moments/`.

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
The helper temporarily selects computer + memory-card storage and original-size
transfer, and attempts to restore those settings on normal exit or errors while
the connection remains available. It preserves the camera's exposure and image
format. Downloaded files can therefore be JPEG, HEIF, RAW, or a pair, according to
the camera's recording and PC-save settings.

`results.json` separates the model's `shot` decision from `capture_status`:
requested, shutter sent, downloaded, skipped, or failed. Only a completed download
with a nonempty file is counted as a successful capture. A timeout or disconnect
stops the run without retrying an uncertain shutter command. A photo may still be
on the memory card after a transfer failure. Ctrl-C closes the session; an already
started capture may need time to finish and release the shutter.

When launched from a restricted coding sandbox, the SDK may report no camera even
though macOS sees it. The helper needs USB-device access; run it in a normal local
terminal or grant the app's execution request for USB access.

## Code and checks

- `model.py`: live-image `decide` function, Claude Messages API connection, and strict boolean parsing.
- `futurelens.py`: Sony camera CLI, timing, capture policy, and result recording.
- `camera.py`: persistent SDK-process adapter and offline camera smoke test.
- `native/sony_camera.cpp`: USB connection, live view, autofocus/shutter, and downloads.
- `scripts/build_sony.py`: local macOS helper build using the downloaded SDK.
- `test_model.py`: mocked decision and API configuration checks.
- `test_camera.py`: mocked live timing, capture limits, download failures, and cleanup.

```bash
.venv/bin/python -m unittest -v
```

[Claude Haiku 5.5](https://platform.claude.com/docs/en/models/haiku-5-5/overview),
[structured outputs](https://platform.claude.com/docs/en/build-with-claude/structured-outputs),
and [effort settings](https://platform.claude.com/docs/en/build-with-claude/effort).
