# Using WaitLess

[Back to the project overview](../README.md)

This guide covers setup, camera operation, troubleshooting, and development.
Run the commands from the repository root. WaitLess runs on macOS 14.1 or later
(Apple silicon and Intel, subject to Sony's supported OS versions) and controls
one Sony camera over USB through its local browser interface.

Anthropic is the only supported API provider. WaitLess uses its Messages API with
**Claude Sonnet 5.5** (`claude-sonnet-5-5`). Each session has one persistent agent
conversation. Every observation adds a new preview, freshness and cooldown state, history availability,
and latest factual capture outcome. Claude can list history, retrieve earlier images,
request a photograph, or change the automatic preview interval. An ordinary end
of turn finishes the observation without shooting and keeps the current interval.
When no action is needed, Claude is instructed to reply **No action.** without a tool
call; the host schedules the next preview automatically. Repeating the current interval
is recorded as unchanged, preserves the last actual interval change, and ends the
observation without another inference. No explanations are required.
Sonnet uses its standard API effort (**`high`**)
with **adaptive thinking**. Thinking summaries are omitted from responses; signed
thinking and redacted-thinking blocks are preserved unchanged across observations.
Older context is summarized using Anthropic's native on-demand compaction; recent
observations retain their original images, thinking, and tool results. The output
limit is **8,192 tokens**, shared by reasoning and the response. Ordinary inference
refusals, truncated or malformed responses, and API failures end monitoring.
Compaction failures preserve the old context and allow observations to continue.

## Camera compatibility

See [Sony's official compatibility page](https://support.d-imaging.sony.co.jp/app/sdk/en/)
for supported cameras and firmware requirements. Consult the API Reference included
with your SDK for each camera's supported connection types. WaitLess uses USB;
network connections are outside its current scope. Newly supported cameras may
require a new WaitLess runtime release.

The bundled runtime uses Sony Camera Remote SDK 2.02.00. Hardware testing so far
used an A7 IV
(ILCE-7M4). Other models need on-device verification. WaitLess checks the
connected camera's capabilities before enabling capture.

## Setup

The repository includes a prebuilt camera helper and Sony's runtime libraries in
`runtime/sony/`. Normal setup needs **no Sony SDK download, compiler, CMake, or Xcode**.
The runtime is about 30 MB and uses ordinary Git files, so Git LFS is not required.
SDK headers are needed only by maintainers changing native code.

Use Python 3.11+ in a project virtual environment:

```bash
python3 -m venv .venv
.venv/bin/python -m pip install -r requirements.txt
```

Dependencies stay in `.venv`. Configure the project `.env`:

```dotenv
ANTHROPIC_API_KEY=your-key
```

The active session conversation, including preview images and any summary, goes directly to
`https://api.anthropic.com/v1/messages` using `claude-sonnet-5-5`. A capacity check
sends the same input to `/v1/messages/count_tokens` before each inference or compaction.
The endpoint and model are fixed in code. Environment variables override `.env`
for credentials; keys belong in the ignored local `.env` file.
If your key requires an explicit workspace, also set `ANTHROPIC_WORKSPACE_ID`.

The HTTP client uses API version `2026-10-21` and the `compact-2026-09-04` beta header.
Ordinary inference and token-count failures are not retried. Failed compaction can
be attempted again after at least 60 seconds and a new completed observation.
There are no alternate endpoints or model fallbacks. Images sent to the API are resized to a maximum edge of
960 pixels; saved photos are the camera's separate captures. API errors retain the HTTP status,
known error type, numeric retry delay, and request ID when available. Free-form provider
error text, credentials, and encoded images are not logged.

## Control a Sony camera over USB (macOS)

Connect one supported Sony camera by USB and select **Remote Shooting / PC Remote** on the camera.
Use still-photo mode and **Single Shooting** drive mode. Keep exposure and image format
configured on the camera. Manual focus is useful for a fixed scene; with autofocus,
the helper half-presses and waits briefly for focus before triggering the shutter.
Close other applications that are controlling the same camera.

The interface uses the included camera runtime automatically.

Runtime license details, third-party notices, and the Sony-supplied libusb source
are in [runtime/sony](../runtime/sony/README.md). The proprietary libraries are
included as a component of WaitLess; their separate licenses continue to apply.

The helper considers every USB camera enumerated by the installed Sony SDK, without
a fixed model whitelist. Network cameras are ignored. If multiple USB cameras are
found, it lists their models and asks you to connect only one; it never chooses the
first arbitrarily. Camera/firmware compatibility is defined by the installed SDK;
see [Sony's supported-device list](https://support.d-imaging.sony.co.jp/app/sdk/en/).

Before capture, WaitLess checks live-view availability, the advertised shutter
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

After setup, launch the local interface:

```bash
.venv/bin/python ui.py
```

It opens `http://127.0.0.1:8765` in your browser. Use `--no-open` to open it yourself,
or `--port 8766` to choose another port. Keep the terminal running. This command
only launches the interface; instructions, settings, connection checks, and monitoring
are controlled in the browser.

1. Enter an instruction under **Describe the moment**. **Preview timing** is automatic:
   Claude chooses intervals between **10 seconds and 5 minutes**.
   There are no session duration, photograph-count, or AI-call budgets. Describe
   whether to capture once or repeat photographs when the condition occurs again.
   Monitoring continues until you press **Stop session**. The header shows elapsed
   time and cumulative preview, photograph, and AI-call counts.
2. Opening the setup page checks the camera once. The header shows **Camera connected**
   after a successful check; click it (or **Check camera**) to check again.
   This does not call Claude, enable capture, or take a photo. The status reflects
   the last check, not continuous USB detection. Monitoring checks the connection again.
   Connection and startup errors appear in a dismissible dialog; normal polling
   does not repeatedly reopen the same error. Runtime and API configuration are
   validated at startup without permanent status panels.
3. **Start monitoring** opens a vertical **Session activity** stream. Previews,
   scene evaluations, tool calls, and context compaction appear as peers in time
   order. Timestamps are relative to the start of the session. Progress updates
   revise the same action instead of creating duplicate entries; a no-photo decision
   reads **Keep watching**. The stream shows sampled observations, not continuous video
   or internal model reasoning. Interval changes such as **300 → 10 sec**
   appear as independent actions, and the header shows the current interval.
   A repeated setting appears as **Preview interval unchanged**, not as a change.
4. Open any thumbnail in the image viewer. History retrieval actions include their
   returned thumbnails and links back to the original previews. Scrolling upward
   preserves your position while monitoring continues. **Return to latest** resumes
   following new previews and actions. Evaluation limits, expired previews, and
   failures have distinct outcomes.
5. A photograph's preparation, shutter, and download statuses update its tool-call
   entry. **Photo saved** and green preview markers require a confirmed download.
   Download links on the photo action provide the original JPEG, HEIF, RAW, or paired files.
   A model's `shoot` request or an unconfirmed shutter is not marked as a saved photo.
6. **Stop session** interrupts the interval wait and prevents subsequent operations.
   An API or SDK operation already in progress finishes first. A capture already
   sent to the camera can still complete; its downloaded files remain recorded.

The browser refreshes UI status once per second; this **does not change the camera
observation interval or add AI calls**. Refreshing or closing the browser does not
stop monitoring. Reopening the URL restores the current session while the server
is running. Ctrl-C in the terminal requests a stop, waits for camera cleanup, and
closes the server. Starting a new session keeps previous session files on disk;
the interface only browses the current server session.

The interface uses Python's standard-library HTTP server and plain HTML/CSS/JavaScript.
It passes validated settings directly to the monitoring engine in one worker thread. There is no Node build step
or extra Python dependency. The server binds only to loopback, checks request origins
and a per-server control token, and serves only recorded previews and confirmed
photo files. API credentials remain in Python; they are never sent to the browser.
The interface itself loads no external fonts, scripts, or analytics.

## The observation loop

The first observation happens immediately after camera setup. Preview timing is
always automatic; there is no mode selector or manual interval setting.

1. Retrieve a fresh camera preview and save it locally.
2. Append to the session's conversation with the instruction, timestamped preview,
   freshness and cooldown state, available history, latest capture facts,
   interval bounds, and the current interval.
3. Check capacity, then call Claude. A response may contain one tool call or end
   the observation without a photograph.
4. Return history tool results to Claude and continue within the same run. A `shoot`
   call uses the capture safeguards, records its actual outcome, and automatically
   finishes the observation, with no extra inference afterward. A
   successful `set_preview_interval` call also finishes the observation without
   another inference or a photograph.
5. Compact older context if needed, then wait for the next scheduled observation.
   The next preview continues the same conversation, including the previous
   observation's final capture or interval-change result.

**Auto mode:** each new session starts at **60 seconds**. Claude calls
`set_preview_interval(seconds)` only when a different interval is needed, within
the **10 to 300 second** bounds. No reason is requested. The guidance is to wait longer when history shows a
stable scene far from the target, and check sooner as the desired moment approaches.
Claude can compare images already in context or retrieve missing images to assess change. The current interval is
included in every observation's context and persists until a valid tool call
changes it. Plain end turns, photographs, rejected changes, expired previews, and
run limits all keep the current interval. A successful change takes effect for the
next preview and ends the observation without another inference. Starting a new
session resets Auto to 60 seconds.

Auto's interval is measured from the current preview's host timestamp, including
retrieval and processing time. If processing outlasts that interval, the next
preview is taken as soon as the current observation finishes. Observations never
overlap and missed previews are not replayed. The countdown reflects the next
scheduled observation.

Everything runs sequentially: there is one evaluation or compaction in flight and no background
preview sampler or scene-change detector. During a long wait, a new event cannot
be detected until the next preview. Auto can reduce checks during quiet periods,
but may miss a brief clearing between them. Field performance and net resource
savings remain unmeasured. Every sampled preview is retained; the program does not
save every frame from the camera's live stream.

Sessions have no time, photograph-count, or total AI-call limit. Photographs do not
end monitoring; follow-up observations include the capture outcome so Claude can
follow the user's requested repetition. **Stop session** ends monitoring. Camera
or inference errors and hard context capacity limits still stop a session.

Each observation can make up to **six** inference calls, including its initial evaluation. Reaching six without a
completed decision displays **Run limit reached**, and monitoring continues at the
next scheduled observation. Compaction does not consume this six-evaluation allowance.
The AI-call count includes scene evaluations and compaction requests (successful or
failed). Token-count requests are recorded separately. Each API request has a
90-second timeout.

The preview expires after **30 seconds**, including history lookup and API time.
No further inference or capture starts for that preview. An in-flight request may
finish, but cannot authorize an expired capture. Stop also prevents a subsequent
capture. An SDK operation already in progress may take time to finish.

Use **Check camera** to check the connection without an API key, inference call,
shutter command, or changes to capture settings. The check saves a local preview.

## Agent tools and capacity

| Tool | Input | Result |
| --- | --- | --- |
| `list_history` | Optional `start_s`, `end_s`, `cursor`, `limit` | Earlier previews, newest first: IDs, timestamps, capture facts, and `next_cursor`. Defaults to 20, maximum 50. Time filters are inclusive seconds since monitoring began. |
| `get_history` | `frame_ids`: one to eight distinct earlier IDs | Actual images, timestamps, and factual capture outcomes. |
| `shoot` | No arguments | Downloaded, skipped, or failed capture outcome; always finishes the observation. |
| `set_preview_interval` | Integer `seconds` (10–300); no other arguments | Changes the persistent preview interval and finishes the observation without a photograph or another inference. The interval stays in effect until changed. Invalid arguments return a tool error the model can correct within its remaining calls. |

History tools can access only earlier previews in the current session. They accept
IDs, never arbitrary file paths. Invalid history requests produce a tool error the
agent can correct within its remaining calls. There is no mandatory history lookup;
the agent chooses when earlier images help and can use images already in context.
A successful download, its preview ID, and the latest capture outcome are included
in subsequent observations as factual context. Follow-up evaluations receive fresh
freshness, cooldown, and remaining calls for that observation inside the preceding tool result.
Tool IDs are deduplicated across the entire session, including across compaction.

Interval changes are also saved in each run's transcript and activity trace.
`results.json` records `current_interval_s` and the latest
`last_interval_change` (previous seconds, new seconds, and triggering frame ID);
each observation includes `next_preview_interval_s`. Refreshing the interface restores these
values from the running server's report. Stop and preview freshness still take
precedence over scheduling decisions.
The session start endpoint accepts only `request`. Former settings such as
`preview_mode`, `interval`, `duration_minutes`, `max_shots`, and `max_requests` are
rejected before camera or API work starts. Existing output folders
are kept as recorded.

Every observation's preview is saved under `previews/` at its received resolution
as a JPEG. `previews/index.jsonl` records IDs, session timestamps, SDK frame numbers,
dimensions, and filenames. Images sent to Claude are resized to a maximum edge of
960 pixels. Previews accumulate in the session conversation, with automatic prompt
caching enabled. The entire local archive remains available after compaction.

### Session context compaction

After an observation, WaitLess checks soft thresholds of **100,000 input tokens**,
**20,000,000 request bytes**, or **400 images**. The token estimate uses the latest
validated input count plus response output usage (or the output allowance when usage
is unavailable); the next inference still gets an exact token-count preflight.
Image count and byte size include nested history tool results. These are internal
defaults, not user settings.

When a threshold is reached and there is a legal older prefix to summarize, WaitLess
sends `compaction: {"type": "summarize"}` to the same Sonnet model. It retains at
least the **latest four observations** word for word. The split uses a previously
sent request boundary, with its assistant reply at the start of the retained tail.
Tool calls and their results stay together. This can retain an extra fragment of
an older observation to preserve thinking signatures. The system prompt and tool
definitions remain constant throughout the session.

The summary preserves goals, observations with frame IDs and timestamps, uncertainty,
interval changes, and factual capture outcomes. It is lossy memory, not image
evidence: missing visual details must be retrieved with `get_history`. Only a valid
signed compaction block replaces the old prefix. The block and retained messages
are passed back unchanged; subsequent compaction includes the previous summary.
An empty ordinary end turn is recorded as no action without adding an empty
assistant message to later requests.

Compaction runs **before fetching the next preview**, with a 90-second request timeout.
It can delay the next observation. If the interval has elapsed, the next preview is
taken as soon as compaction finishes. The new preview gets its own freshness window.
Stop takes precedence, including when a compaction request is already in flight.

If compaction fails or produces no valid summary, the original conversation stays
intact. The UI records the failure and monitoring continues. A new attempt requires
both another completed observation and at least **60 seconds** since the failure.
There is no immediate retry. If the unchanged context reaches a hard capacity limit,
the session stops; it never silently drops images, truncates messages, or resets the agent.
Compaction call usage is aggregated from `usage.iterations`, including cache tokens,
without double-counting the top-level usage fields.

### Hard capacity limits

Before **every inference or compaction**, including after history retrieval, the program checks:

| Resource | Maximum |
| --- | --- |
| Images in the current request, including tool results | 600 |
| Serialized inference request body | 30,000,000 bytes (30 MB) |
| Input tokens reported by Anthropic's token-count endpoint | 990,000 |

Image count and byte size are checked locally before either API endpoint is called.
The token-count request includes the conversation, system instructions, tools,
thinking setting, and cache configuration. An invalid count or failed check before
an ordinary inference ends monitoring; it never sends an unchecked inference. A token-count
check and the inference each make an HTTP request. For compaction, a failed check
does not send an unchecked summary request; it leaves the old context for later observations.

If a request would exceed a ceiling, monitoring ends with `capacity_reached` and
reports the resource and limit. All previews, event traces, and transcripts remain
on disk, including the observation that could not be sent. 990,000 tokens is an
upper ceiling, not an expected request size.

Each new session stores:

- `conversation.jsonl`: the session's append-only message and request history,
  context revisions, compaction attempts, and signed summary replacements with the
  exact prefix length replaced. Replaying appends and replacements reconstructs
  the active context; no original log entries are deleted.
- `runs/<preview ID>/conversation.jsonl`: that observation's instruction and new context,
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

The report adds `context_revision`, `compaction_requests`, `compactions_completed`,
`compaction_settings`, and the latest `compaction` status, duration, and safe usage
or error metadata. `api_requests` includes both evaluations and summary calls;
each observation's `model_calls` counts only its evaluations. The UI shows context
organization in the activity trace and includes summary calls in the AI-call count.
Summary text and thinking blocks remain in local transcripts, outside the UI state response.

## Capture behavior

A `shoot` tool call requests one photo of the newest observation. The application
rejects it when Stop has been requested, the preview is older than **30 seconds**,
or the capture cooldown is active.
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

## Rebuilding the native runtime (maintainers only)

Users can skip this section. To change the C++ helper or upgrade the bundled SDK,
obtain Sony's SDK and use Apple's command-line developer tools:

```bash
.venv/bin/python scripts/build_sony.py \
  --sdk "$HOME/Downloads/CrSDK_v2/SimpleCli.zip" --bundle
```

The simple build script extracts headers into ignored `build/sony/include`, builds
our helper for both `arm64` and `x86_64`, and copies it plus Sony's unchanged runtime
libraries into `runtime/sony/macos`. It preserves the required relative library
layout and retains the full `external/crsdk` runtime. No SDK headers or sample
source are included in the distributed runtime. `--bundle` also refreshes the
SDK's open-source notices and libusb source; keep Sony's sibling source folder
available, or supply `--libusb-source /path/to/libusb.zip`.

Without `--bundle`, the script updates only the ignored development build; the
interface continues using the checked-in runtime. Always bundle after changing
the native helper, then run the tests before distributing the update.

The offline check below loads the runtime libraries without initializing the SDK,
connecting to a camera, or calling Claude:

```bash
runtime/sony/macos/sony_camera --check-runtime
```

This Git checkout includes a compiled helper, rather than a standalone signed
and notarized macOS app installer.

## Code and checks

Our native bridge uses Sony's SDK headers and libraries directly. It provides the
persistent internal process protocol, USB selection, capability checks, and capture
safeguards. Claude decisions, timing, and capture checks remain in Python.

- `model.py`: fixed Anthropic transport, native tool payloads, strict turn validation, and capacity checks.
- `agent.py`: session conversation, thinking preservation, signed compaction boundaries, and append-only context log.
- `history.py`: session-scoped history tools, per-run journals, and capture records.
- `waitless.py`: validated session settings, monitoring, capture policy, and result recording.
- `ui.py` / `web/`: local camera interface, session controls, and preview timeline.
- `camera.py`: persistent SDK-process adapter used by the interface and monitoring engine.
- `native/sony_camera.cpp`: USB connection, live view, autofocus/shutter, and downloads.
- `native/camera_capabilities.h`: USB selection and capability-based capture policy.
- `scripts/build_sony.py`: maintainer-only universal macOS build and runtime packaging with `clang++`.
- `test_model.py`: tool-call validation, capacity boundaries, and fixed-endpoint transport checks.
- `test_agent.py`: retained context, compaction signatures, repeated compaction, and journal reconstruction.
- `test_history.py`: preview and conversation persistence with local image references.
- `test_camera.py`: agent replay, per-observation call limits, scheduling, capacity stops, continuous capture, and cleanup.
- `test_ui.py`: settings validation, exclusive camera ownership, local HTTP controls, and media access.
- `test_runtime.py`: bundled library loading, portable paths, and package contents.
- `test_native.py` / `native/test_camera_capabilities.cpp`: offline USB-selection and
  capability checks, compiled against the installed SDK headers without connecting to a camera.

```bash
.venv/bin/python -m unittest -v
```

Offline replays exercise session conversations, native compaction, paginated history, retrieved images,
changing and unchanged scenes, capture confirmation, observation limits, Stop, timing, and
capacity preservation. Simulated UI sessions check the unified activity stream, history browsing,
image viewing, capture markers, and refresh recovery. These checks validate the
workflow, not recognition accuracy on a physical scene.

The activity projection also has dependency-free Node tests for ordering, progress
updates, capture outcomes, and cross-observation IDs: `node --test test_activity.mjs`.
Node is only needed to run these developer tests, not to run WaitLess.

[Claude Sonnet 5.5](https://platform.claude.com/docs/en/models/sonnet-5-5/overview),
[effort](https://platform.claude.com/docs/en/build-with-claude/effort),
[thinking](https://platform.claude.com/docs/en/build-with-claude/thinking),
[on-demand compaction](https://platform.claude.com/docs/en/build-with-claude/compaction-on-demand),
[compaction and thinking](https://platform.claude.com/docs/en/build-with-claude/compaction-thinking-blocks),
[token counting](https://platform.claude.com/docs/en/api/messages/count_tokens),
[prompt caching](https://platform.claude.com/docs/en/build-with-claude/prompt-caching),
and [tool use](https://platform.claude.com/docs/en/agents-and-tools/tool-use/handle-tool-calls).
