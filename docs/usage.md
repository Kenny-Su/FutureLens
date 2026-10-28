# Using FutureLens

[Back to the project overview](../README.md)

This guide covers setup, camera operation, troubleshooting, and development.
Run the commands from the repository root. FutureLens runs on macOS and controls
one Sony camera over USB through its local browser interface.

Anthropic is the only supported API provider. FutureLens uses its Messages API with
**Claude Haiku 5.5** (`claude-haiku-5-5`). Each observation starts a fresh conversation
with the instruction, current preview, session limits, history availability, and
latest factual capture outcome. Claude can list history, retrieve earlier images,
or request a photograph. An ordinary end of turn finishes the observation without
shooting. No explanations are required. Low effort and disabled thinking remain
configured. Refusals, malformed responses, and API failures end monitoring.

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

The current run's conversation, including any retrieved preview images, goes directly to
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
but no supported still-photo workflow can still show a preview through **Check connection**.

Autofocus requires remote half-press and readable focus confirmation. If either is
unavailable, select MF on the camera; the app does not change the focus mode for you.
SDK compatibility alone does not guarantee this particular still-photo workflow.
Offline tests cover different capability combinations.

## Camera interface

After setup and building the camera helper, launch the local interface:

```bash
.venv/bin/python ui.py
```

It opens `http://127.0.0.1:8765` in your browser. Use `--no-open` to open it yourself,
or `--port 8766` to choose another port. Keep the terminal running. This command
only launches the interface; instructions, settings, connection checks, and monitoring
are controlled in the browser.

1. Enter an instruction, or choose an example. Set the observation interval in
   **seconds**, duration in **minutes**, and photo limit. Defaults are 60 seconds,
   60 minutes, and one photo. Under **AI budget**, **Maximum AI calls** defaults to 360.
   Each observation can use up to six calls; the header counts calls and previews separately.
2. Optionally **Check connection** to detect the camera, assess capabilities, and
   see a preview. This does not call Claude, enable capture, or take a photo.
   The connection is released afterward and checked again when monitoring starts.
3. **Start monitoring** opens a horizontal timeline. Each saved preview creates one
   card with its run's vertical activity trace beneath the image. The latest card
   expands automatically; older cards can be independently expanded or collapsed.
   The timeline shows sampled observations, not a continuous video feed.
4. Open any thumbnail in the image viewer. History retrievals appear inside the
   requesting card, with thumbnails and links back to their original observations.
   Scrolling back preserves your position while monitoring continues. **Return to
   latest** resumes following new previews. Cards blocked before inference show
   **Not sent**; run limits, expired previews, and failures have distinct outcomes.
5. Green photo markers indicate confirmed downloads, linked to their triggering
   preview. Select one to download its original JPEG, HEIF, RAW, or paired files.
   A model's `shoot` request or an unconfirmed shutter is not marked as a saved photo.
6. **Stop session** interrupts the interval wait and prevents subsequent operations.
   An API or SDK operation already in progress finishes first. A capture already
   sent to the camera can still complete; its downloaded files remain recorded.

The browser refreshes UI status once per second; this **does not change the camera
observation interval or add AI calls**. Refreshing or closing the browser does not
stop monitoring. Reopening the URL restores the current session while the server
is running. Ctrl-C in the terminal requests a stop, waits for camera cleanup, and
closes the server. Starting a new session keeps previous session files on disk;
the first version of the interface only browses the current server session.

The interface uses Python's standard-library HTTP server and plain HTML/CSS/JavaScript.
It passes validated settings directly to the monitoring engine in one worker thread. There is no Node build step
or extra Python dependency. The server binds only to loopback, checks request origins
and a per-server control token, and serves only recorded previews and confirmed
photo files. API credentials remain in Python; they are never sent to the browser.
The interface itself loads no external fonts, scripts, or analytics.

## The observation loop

The first observation happens immediately after camera setup. Subsequent observations
follow the **Observe every** setting on the session's schedule (default **60 seconds**):

1. Retrieve a fresh camera preview and save it locally.
2. Start a fresh agent conversation with the instruction, timestamped preview,
   session limits, available history, and latest capture facts.
3. Check capacity, then call Claude. A response may contain one tool call or end
   the observation without a photograph.
4. Return history tool results to Claude and continue within the same run. A `shoot`
   call uses the capture safeguards, records its actual outcome, and automatically
   finishes the observation, with no extra inference afterward.
5. Wait for the next scheduled observation, then start another fresh run.

Everything runs sequentially: there is one inference in flight and no background
preview sampler. Camera and API processing time do not add drift to the schedule.
An operation that overruns a scheduled observation skips that slot; there are no
catch-up bursts. Every sampled preview is retained, but the program does not save
every frame from the camera's live stream. Events between observations may be missed.

**Watch for** defaults to **60 minutes**, measured after connection/setup.
**Maximum AI calls** defaults to **360** across the session. A run can make up to
**six** inference calls, including its initial evaluation. Reaching six without a
completed decision displays **Run limit reached**, and monitoring continues at the
next scheduled observation. Using the session budget ends monitoring. Token-count
checks are recorded separately and consume neither budget. **Photo limit** defaults
to **one** confirmed download; increase it for a sequence.

The preview expires after **30 seconds**, including history lookup and API time.
No further inference or capture starts for that preview. An in-flight request may
finish, but cannot authorize an expired capture. The same applies to Stop and the
session deadline. An SDK operation already in progress may take time to finish.

Use **Check connection** for a local preview without an API key, inference call,
shutter command, or changes to capture settings.

## History tools and capacity

| Tool | Input | Result |
| --- | --- | --- |
| `list_history` | Optional `start_s`, `end_s`, `cursor`, `limit` | Earlier previews, newest first: IDs, timestamps, capture facts, and `next_cursor`. Defaults to 20, maximum 50. Time filters are inclusive seconds since monitoring began. |
| `get_history` | `frame_ids`: one to eight distinct earlier IDs | Actual images, timestamps, and factual capture outcomes. |
| `shoot` | No arguments | Downloaded, skipped, or failed capture outcome; always finishes the observation. |

History tools can access only earlier previews in the current session. They accept
IDs, never arbitrary file paths. Invalid history requests produce a tool error the
agent can correct within its remaining calls. There is no mandatory history lookup;
the agent chooses when earlier images help. A successful download, its preview ID,
and the latest capture outcome are included in subsequent runs as factual context.

Every observation's preview is saved under `previews/` at its received resolution
as a JPEG. `previews/index.jsonl` records IDs, session timestamps, SDK frame numbers,
dimensions, and filenames. Images sent to Claude are resized to a maximum edge of
960 pixels. Older previews enter a fresh conversation only through `get_history`;
the entire local archive remains available. Automatic prompt caching is enabled
within each run. No run is summarized or silently truncated to fit capacity.

Before **every inference**, including after history retrieval, the program checks:

| Resource | Maximum |
| --- | --- |
| Images in the current request, including tool results | 600 |
| Serialized inference request body | 30,000,000 bytes (30 MB) |
| Input tokens reported by Anthropic's token-count endpoint | 990,000 |

Image count and byte size are checked locally before either API endpoint is called.
The token-count request includes the conversation, system instructions, tools,
thinking setting, and cache configuration. An invalid count or failed check ends
monitoring; it never falls back to sending an unchecked inference request. A token-count
check and the inference each make an HTTP request, with no automatic retries.

If a request would exceed a ceiling, monitoring ends with `capacity_reached` and
reports the resource and limit. All previews, event traces, and transcripts remain
on disk, including the observation that could not be sent. 990,000 tokens is an
upper ceiling, not an expected request size.

New sessions use journal format version 2:

- `runs/<preview ID>/conversation.jsonl`: that run's instruction and context,
  model responses, and tool results, including the final capture outcome.
- `runs/<preview ID>/events.jsonl`: append-only request, tool, capture, and run
  progress. Events have stable IDs, sequence numbers, and UTC timestamps.
- `results.json`: session state and compact per-observation events used by the UI.
- `captures.jsonl`: confirmed downloads with their triggering preview IDs.

Image blocks use local relative file references, including images nested in tool
results, so journals do not repeat base64 data. The state API does not send full
conversation transcripts to the browser. Existing output folders stay untouched;
resuming old sessions remains unsupported. Refreshing the page reconstructs the
current session from its report while the local server remains running.

## Capture behavior

A `shoot` tool call requests one photo of the newest observation. The application
rejects it when the monitoring duration has expired, the preview is older than
**30 seconds**, or the capture cooldown is active.
Freshness includes preview retrieval, token counting, and inference time. It is
checked again immediately before shutter preparation, and the remaining allowance
is passed to the helper so autofocus cannot silently use up the deadline.
This age measures elapsed host time, not the sensor's exact exposure timestamp.

The capture cooldown is **5 seconds** between completed captures. There is at most
one shutter attempt per observation. Claude receives the actual result, such as
`downloaded` or `skipped_stale_frame`, with the next preview. A request to shoot is
not reported as a successful photograph until its files download. Consecutive
observations can each request a photograph; recognizing a genuinely new event is
the model's responsibility, and semantic duplicate prevention is not guaranteed.

Recognition remains experimental. Keep framing stable and make relevant details
large enough to distinguish in previews. Fog, exposure changes, occlusion, or several
apartments going dark between observations can make the scene ambiguous. Synthetic
replays exercise the agent protocol; they do not establish real-world
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

`results.json` records each entry under `observations`, including `run_status`,
`outcome`, `model_calls`, and `capture_status`: not requested, requested, shutter
sent, downloaded, skipped, or failed. It also records the detected camera and capabilities, saved
preview count, inference and token-count request counts, capacity checks, and the
session's stop reason. `captures.jsonl` records successful downloads with their
trigger preview IDs and saved files.

Only a completed download with a nonempty file counts as a successful capture.
Timeouts or disconnects stop the run without retrying an uncertain shutter command;
a photo may still be on the memory card after a transfer failure. **Stop session**
ends monitoring; an already started capture may need time to finish and release the shutter.
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
persistent internal process protocol, USB selection, capability checks, and capture
safeguards. Claude decisions, timing, and capture limits remain in Python.

- `model.py`: fixed Anthropic transport, native tool payloads, strict turn validation, and capacity checks.
- `history.py`: session-scoped history tools, per-run journals, and capture records.
- `futurelens.py`: validated session settings, monitoring, capture policy, and result recording.
- `ui.py` / `web/`: local camera interface, session controls, and preview timeline.
- `camera.py`: persistent SDK-process adapter used by the interface and monitoring engine.
- `native/sony_camera.cpp`: USB connection, live view, autofocus/shutter, and downloads.
- `native/camera_capabilities.h`: USB selection and capability-based capture policy.
- `scripts/build_sony.py`: local macOS helper build using the downloaded SDK and `clang++`.
- `test_model.py`: tool-call validation, capacity boundaries, and fixed-endpoint transport checks.
- `test_history.py`: preview and conversation persistence with local image references.
- `test_camera.py`: agent replay, call budgets, scheduling, capacity stops, capture limits, and cleanup.
- `test_ui.py`: settings validation, exclusive camera ownership, local HTTP controls, and media access.
- `test_native.py` / `native/test_camera_capabilities.cpp`: offline USB-selection and
  capability checks, compiled against the installed SDK headers without connecting to a camera.

```bash
.venv/bin/python -m unittest -v
```

Offline replays exercise fresh conversations, paginated history, retrieved images,
changing and unchanged scenes, capture confirmation, budgets, Stop, timing, and
capacity preservation. Simulated UI sessions check trace expansion, history browsing,
image viewing, capture markers, and refresh recovery. These checks validate the
workflow, not recognition accuracy on a physical scene.

[Claude Haiku 5.5](https://platform.claude.com/docs/en/models/haiku-5-5/overview),
[token counting](https://platform.claude.com/docs/en/api/messages/count_tokens),
[prompt caching](https://platform.claude.com/docs/en/build-with-claude/prompt-caching),
and [tool use](https://platform.claude.com/docs/en/agents-and-tools/tool-use/handle-tool-calls).
