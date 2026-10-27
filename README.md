# FutureLens

**Tell your camera what to look for.**

Describe a scene in plain language. FutureLens watches your Sony camera's live
view, uses Claude to judge when your condition is met, and triggers the shutter.
Your camera takes the photograph and sends the original file to your computer.

> “Take a photo when a person is visible in the center of the frame.”

## From instruction to photograph

1. **Describe the shot.** Give FutureLens a visual condition to look for.
2. **Set the session.** Choose how often to inspect the scene, how long to watch,
   and how many photos to take.
3. **Let the camera shoot.** FutureLens checks fresh previews and requests a
   photograph when Claude identifies a match.

## Ideas to try

| Scene | Example instruction |
| --- | --- |
| A portrait session | “Take a photo when the person is facing the camera with both hands raised.” |
| A tabletop composition | “Take a photo when a red mug is visible in the center of the table.” |
| A fixed camera watching a doorway | “Take a photo when a person is clearly visible in the doorway.” |

Each decision uses the current preview, so instructions work best when the
condition is visible in a single image.

## You choose the moment—and the limits

- **Natural-language triggers.** Change what the camera looks for by changing
  your instruction.
- **Original camera files.** Download JPEG, HEIF, RAW, or paired files according
  to your camera's recording and transfer settings.
- **Adjustable sessions.** Set inspection frequency, session duration, photo
  limits, and the minimum gap between captures. The default inspection limit is
  once per second; actual timing depends on camera and AI response time.
- **Controlled repeat shots.** After a capture, FutureLens waits for the condition
  to clear before another match can trigger a photo.
- **Automatic camera detection.** Connect one supported Sony camera over USB.
  FutureLens checks its capabilities before enabling capture and supports
  autofocus or manual focus, depending on the camera's configuration.
- **A record of every session.** Review decisions, capture outcomes, and downloaded
  files. Local preview checks help you check framing before an automated session.

## Sony camera support

FutureLens uses the **Sony Camera Remote SDK**. See
[Sony's official compatibility page](https://support.d-imaging.sony.co.jp/app/sdk/en/)
for supported cameras and firmware requirements.

FutureLens works over **USB** and checks the connected camera's capabilities
before enabling capture. Available features depend on the camera, firmware,
settings, and installed SDK version.

## Available today

FutureLens is an early command-line tool for **macOS and one Sony USB camera**.
You set exposure and image format on the camera; FutureLens handles the capture
decision. It suits scenes that remain visible long enough for a preview to be
evaluated and the shutter to respond.

AI decisions use Anthropic's Claude API and require your API key. Preview images
are sent to Anthropic for evaluation; downloaded photographs are saved locally.

**[Try FutureLens →](docs/usage.md)** — setup, camera operation, and troubleshooting.
