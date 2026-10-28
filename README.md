# FutureLens

**Describe the moment worth waiting for.**

The village becomes visible through the fog. Sunrise lights the mountain summit.
Tell FutureLens what you want to see in the photograph, and let it watch the scene
for you.

FutureLens uses Claude to evaluate your Sony camera's live view against your
instruction. Each observation starts a fresh agent run. Claude can look up earlier previews,
retrieve their images, and compare how the scene has changed. Every sampled preview
is saved locally.
When Claude identifies a match, your camera takes the photograph and sends the
original file to your computer.

> “Take one photo when the village below is clearly visible through the fog.”

## From instruction to photograph

1. **Describe the shot.** Give FutureLens a visual condition to look for.
2. **Set the session.** Choose how often to inspect the scene, how long to watch,
   and how many photos to take.
3. **Let the camera shoot.** Claude inspects the current view, retrieves earlier
   observations when useful, and requests a photograph when it identifies a match.
4. **Watch the story unfold.** Browse a timeline of every preview, with each
   agent run's activity beneath it. Saved photographs stand out, with access to
   the original files. Exploring earlier frames keeps
   monitoring running.

## Moments to capture

| Scene | Example instruction |
| --- | --- |
| A village emerging from fog | “Take one photo when the village below is clearly visible through the fog.” |
| Sunrise over a mountain | “Capture the mountain when warm sunrise light is visible on its summit.” |
| Smart timelapse of an apartment building — experimental | “Take one photo of the building each time another apartment goes dark.” |

The fog and sunrise instructions describe a result visible in the current preview:
the village in view or the summit lit.

**Smart timelapse uses visual history.** Claude can retrieve the session's earlier
images and sees factual capture outcomes when deciding whether to request another
photograph.
The aim is a sequence that advances as apartments go dark. This is experimental;
recognizing every change accurately in a real building still needs field testing.

## You choose the moment—and the limits

- **Scene-specific triggers.** Combine subjects, visibility, light, and composition
  in a plain-language instruction.
- **Visual memory on demand.** Every preview stays in a local archive. Claude can
  list past observations and retrieve the images it needs for each decision.
- **Original camera files.** Download JPEG, HEIF, RAW, or paired files according
  to your camera's recording and transfer settings.
- **Adjustable sessions.** Set inspection frequency, session duration, photo
  limits, and the maximum number of AI calls. The default is one observation
  per minute, suited to watching landscapes over time.
- **Controlled repeat shots.** Claude receives the actual results of earlier
  capture requests. The app enforces cooldowns and photo limits.
- **Automatic camera detection.** Connect one supported Sony camera over USB.
  FutureLens checks its capabilities before enabling capture and supports
  autofocus or manual focus, depending on the camera's configuration.
- **A record of every session.** Review decisions, capture outcomes, and downloaded
  files. Local preview checks help you check framing before an automated session.
- **A quiet camera workspace.** Set your instruction and limits, then move into
  a timeline of previews and agent activity, with compact session controls.
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
through a local browser interface. The macOS camera runtime is included for Apple
silicon and Intel Macs, so users can clone the repository and run FutureLens without
downloading Sony's SDK or compiling native code.
You set exposure and image format on the camera; FutureLens handles the capture
decision. It suits scenes that remain visible long enough for a preview to be
evaluated and the shutter to respond.

AI decisions use Anthropic's Claude API and require your API key. Each run starts
with the current preview; historical images are sent only when retrieved. Sessions
stop when they reach their limits or request capacity; local history is preserved. Preview copies and downloaded photographs are saved
on your computer.

**[Try FutureLens →](docs/usage.md)** — setup, camera operation, and troubleshooting.
