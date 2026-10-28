# FutureLens

**Describe the moment worth waiting for.**

The village becomes visible through the fog. Sunrise lights the mountain summit.
Tell FutureLens what you want to see in the photograph, and let it watch the scene
for you.

FutureLens uses Claude to evaluate your Sony camera's live view against your
instruction. Each observation adds a preview to the same conversation, keeping
earlier views available for comparison. Every sampled preview is also saved locally.
When Claude identifies a match, your camera takes the photograph and sends the
original file to your computer.

> “Take one photo when the village below is clearly visible through the fog.”

## From instruction to photograph

1. **Describe the shot.** Give FutureLens a visual condition to look for.
2. **Set the session.** Choose how often to inspect the scene, how long to watch,
   and how many photos to take.
3. **Let the camera shoot.** Claude compares the current view with the session's
   earlier observations and requests a photograph when it identifies a match.
4. **Watch the story unfold.** Browse a timeline of every preview. Saved photographs
   stand out, with access to the original files. Exploring earlier frames keeps
   monitoring running.

## Moments to capture

| Scene | Example instruction |
| --- | --- |
| A village emerging from fog | “Take one photo when the village below is clearly visible through the fog.” |
| Sunrise over a mountain | “Capture the mountain when warm sunrise light is visible on its summit.” |
| Smart timelapse of an apartment building — experimental | “Take one photo of the building each time another apartment goes dark.” |

The fog and sunrise instructions describe a result visible in the current preview:
the village in view or the summit lit.

**Smart timelapse uses visual history.** Claude sees the session's earlier previews
and actual capture outcomes before requesting another photograph.
The aim is a sequence that advances as apartments go dark. This is experimental;
recognizing every change accurately in a real building still needs field testing.

## You choose the moment—and the limits

- **Scene-specific triggers.** Combine subjects, visibility, light, and composition
  in a plain-language instruction.
- **A continuous visual history.** Each observation stays in the conversation,
  with a local copy of every sampled preview.
- **Original camera files.** Download JPEG, HEIF, RAW, or paired files according
  to your camera's recording and transfer settings.
- **Adjustable sessions.** Set inspection frequency, session duration, photo
  limits, and the observation budget. The default is one observation
  per minute, suited to watching landscapes over time.
- **Controlled repeat shots.** Claude receives the actual results of earlier
  capture requests. The app enforces cooldowns and photo limits.
- **Automatic camera detection.** Connect one supported Sony camera over USB.
  FutureLens checks its capabilities before enabling capture and supports
  autofocus or manual focus, depending on the camera's configuration.
- **A record of every session.** Review decisions, capture outcomes, and downloaded
  files. Local preview checks help you check framing before an automated session.
- **A quiet camera workspace.** Set your instruction and limits, then move into
  a focused monitor with the latest preview, session progress, and a visual timeline.
  A photo is highlighted only after its download succeeds.

## Sony camera support

FutureLens uses the **Sony Camera Remote SDK**. See
[Sony's official compatibility page](https://support.d-imaging.sony.co.jp/app/sdk/en/)
for supported cameras and firmware requirements.

FutureLens works over **USB** and checks the connected camera's capabilities
before enabling capture. Available features depend on the camera, firmware,
settings, and installed SDK version.

## Available today

FutureLens is an early camera tool for **macOS and one Sony USB camera**, controlled
through a local browser interface.
You set exposure and image format on the camera; FutureLens handles the capture
decision. It suits scenes that remain visible long enough for a preview to be
evaluated and the shutter to respond.

AI decisions use Anthropic's Claude API and require your API key. Each request
includes the session's accumulated previews and conversation. Sessions stop when
they reach their configured limits or conversation capacity; earlier observations
are never silently removed. Preview copies and downloaded photographs are saved
on your computer.

**[Try FutureLens →](docs/usage.md)** — setup, camera operation, and troubleshooting.
