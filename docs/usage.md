# Using FutureLens

[Back to the project overview](../README.md)

This guide covers setup, camera operation, troubleshooting, and development.
Run the commands from the repository root. FutureLens currently runs as a CLI on
macOS and controls one Sony camera over USB.

Anthropic is the only supported API provider. FutureLens uses its Messages API with
**Claude Haiku 5.5** (`claude-haiku-5-5`). One conversation holds your instruction,
timestamped previews, model actions, and actual capture outcomes for the whole session.
Each observation produces one response: `{"action":"shoot"}` or `{"action":"wait"}`.
There are no model-written explanations or history-retrieval tools. Low effort and
disabled thinking remain configured. Refusals, malformed actions, and API failures
stop the run with an error.

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

The accumulated conversation, including earlier preview images, goes directly to
`https://api.anthropic.com/v1/messages` using `claude-haiku-5-5`. A capacity check
sends the same input to `/v1/messages/count_tokens` before each inference.
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

For example, watch for a village to become visible through fog for up to
30 minutes, stopping after one photograph:

```bash
.venv/bin/python futurelens.py \
  --request "Take one photo when the village below is clearly visible through the fog" \
  --duration 1800 --interval 60 --max-requests 30 --max-shots 1
```

## The observation loop

The first observation happens immediately after camera setup. Subsequent observations
follow `--interval` seconds on the session's schedule (default **60 seconds**):

1. Retrieve a fresh camera preview and save it locally.
2. Append its timestamp and image to the ongoing conversation, along with the actual
   outcome of the previous action. Your instruction appears at the start of the chat.
3. Check conversation capacity, then call Claude once for `shoot` or `wait`.
4. For `shoot`, check capture limits, take the photograph if permitted, and record
   whether it downloaded successfully.
5. Wait for the next scheduled observation.

Everything runs sequentially: there is one inference in flight and no background
preview sampler. Camera and API processing time do not add drift to the schedule.
An operation that overruns a scheduled observation skips that slot; there are no
catch-up bursts. Every sampled preview is retained, but the program does not save
every frame from the camera's live stream. Events between observations may be missed.

`--duration` defaults to **3600 seconds**, measured after connection/setup.
`--max-requests` defaults to **60 inference calls**; token-count checks are recorded
separately and do not consume that budget. `--max-shots` defaults to **one**; increase
it for a sequence. The first reached limit ends the session. An in-flight operation
may take time to finish after the deadline, but cannot authorize a new shutter
command after expiry.

`--dry-run` saves previews at the same interval without loading credentials, making
API calls, sending shutter commands, or changing capture settings. It runs for the
configured duration; the inference-call budget does not limit a dry run.

The CLI uses `--interval` in seconds. The old `--fps`, `--max-agent-turns`, and
`--max-context-images` options have been removed.

## Conversation history and capacity

Every observation's preview is saved under `previews/` at its received resolution
as a JPEG. `previews/index.jsonl` records IDs, session timestamps, SDK frame numbers,
dimensions, and filenames. Images sent to Claude are resized to a maximum edge of
960 pixels. All previous observations remain in each request; neither images nor
conversation turns are summarized or silently discarded. Automatic prompt caching
is enabled for the growing conversation. Caching does not remove context or request
size limits, and every request still transmits the conversation.

Before inference, the program checks these ceilings:

| Resource | Maximum |
| --- | --- |
| Images in the accumulated conversation | 600 |
| Serialized inference request body | 30,000,000 bytes (30 MB) |
| Input tokens reported by Anthropic's token-count endpoint | 990,000 |

Image count and byte size are checked locally before either API endpoint is called.
The token-count request includes the conversation, system instructions, output schema,
thinking setting, and cache configuration. An invalid count or failed check stops the
run; it does not fall back to sending an unchecked inference request. A token-count
check and the inference each make an HTTP request, with no automatic retries.

If the next observation would exceed a ceiling, the run ends with `capacity_reached`
and reports the resource and limit. The newly saved preview and full conversation
journal are preserved even when that observation cannot be sent for inference.
Capacity depends on image content and session length; 990,000 tokens is an upper
ceiling, not the expected size of each request.

`conversation.jsonl` records the instruction, observations, valid model responses,
and host-written capture outcomes. Image blocks use relative references to the local
preview files instead of duplicating encoded image data in the journal. The final
capture outcome is also recorded when there will be no next observation. Existing
output directories are never overwritten. A new run starts a new conversation;
resuming old sessions is not implemented.

## Capture behavior

A `shoot` action requests one photo of the newest observation. The application
rejects it when the monitoring duration has expired, the preview is older than
`--max-frame-age` (default **30 seconds**), or the capture cooldown is active.
Freshness includes preview retrieval, token counting, and inference time. It is
checked again immediately before shutter preparation, and the remaining allowance
is passed to the helper so autofocus cannot silently use up the deadline.
This age measures elapsed host time, not the sensor's exact exposure timestamp.

`--cooldown` defaults to **5 seconds** between completed captures. There is at most
one shutter attempt per observation. Claude receives the actual result, such as
`downloaded` or `skipped_stale_frame`, with the next preview. A request to shoot is
not reported as a successful photograph until its files download. Consecutive
observations can each request a photograph; recognizing a genuinely new event is
the model's responsibility, and semantic duplicate prevention is not guaranteed.

Recognition remains experimental. Keep framing stable and make relevant details
large enough to distinguish in previews. Fog, exposure changes, occlusion, or several
apartments going dark between observations can make the scene ambiguous. Synthetic
API checks exercise the conversation protocol; they do not establish real-world
smart-timelapse accuracy.

The helper temporarily selects computer + memory-card storage when available,
or computer-only storage when that is the supported option. The selected storage
strategy is recorded in the capability report. It requests original-size transfer
where the camera exposes that setting; cameras without an adjustable transfer-size
property use their native transfer behavior, reported as `camera_default`.
The helper attempts to restore changed settings on normal exit or errors while
the connection remains available. It preserves the camera's exposure and image
format. Downloaded files can therefore be JPEG, HEIF, RAW, or a pair, according to
the camera's recording and PC-save settings.

`results.json` records each entry under `observations`, separating the model's
`action` from `capture_status`: not requested, requested, shutter sent, downloaded,
skipped, or failed. It also records the detected camera and capabilities, saved
preview count, inference and token-count request counts, capacity checks, and the
session's stop reason. `captures.jsonl` records successful downloads with their
trigger preview IDs and saved files.

Only a completed download with a nonempty file counts as a successful capture.
Timeouts or disconnects stop the run without retrying an uncertain shutter command;
a photo may still be on the memory card after a transfer failure. Ctrl-C closes the
session; an already started capture may need time to finish and release the shutter.
Storage failures also stop the run. Credentials, encoded image data, and free-form
provider error text are not written to the logs.

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

- `model.py`: fixed Anthropic transport, cached conversation payloads, action parsing, and capacity checks.
- `history.py`: append-only preview archive, conversation journal, and capture records.
- `futurelens.py`: Sony camera CLI, timing, capture policy, and result recording.
- `camera.py`: persistent SDK-process adapter and offline camera smoke test.
- `native/sony_camera.cpp`: USB connection, live view, autofocus/shutter, and downloads.
- `native/camera_capabilities.h`: USB selection and capability-based capture policy.
- `scripts/build_sony.py`: local macOS helper build using the downloaded SDK and `clang++`.
- `test_model.py`: action validation, capacity boundaries, and fixed-endpoint transport checks.
- `test_history.py`: preview and conversation persistence with local image references.
- `test_camera.py`: conversation replay, scheduling, capacity stops, capture limits, failures, and cleanup.
- `test_native.py` / `native/test_camera_capabilities.cpp`: offline USB-selection and
  capability checks, compiled against the installed SDK headers without connecting to a camera.

```bash
.venv/bin/python -m unittest -v
```

The sequential conversation passed a live API replay with three synthetic previews:
`wait` for the initial scene, `shoot` for a white-to-black change, then `wait` after
a simulated successful capture of the same scene. Token counting and automatic
cache reuse worked in that check. No physical camera was used for this replay.

[Claude Haiku 5.5](https://platform.claude.com/docs/en/models/haiku-5-5/overview),
[token counting](https://platform.claude.com/docs/en/api/messages/count_tokens),
[prompt caching](https://platform.claude.com/docs/en/build-with-claude/prompt-caching),
[structured outputs](https://platform.claude.com/docs/en/build-with-claude/structured-outputs),
and [effort settings](https://platform.claude.com/docs/en/build-with-claude/effort).
