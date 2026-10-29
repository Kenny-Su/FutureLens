# WaitLess

**Delegate the waiting.**

A mountain emerges from the clouds. Fog lifts from a village. The first light
reaches a summit. You know the photograph you want; you just don't know when it
will happen.

WaitLess watches your camera's scene for the moment you describe. It inspects
lightweight previews and triggers a full-resolution photograph when it identifies
a match. The goal: spend less time waiting for the shot, with fewer files to sort
through afterward.

> “Take one photo when the mountain summit is clearly visible through the clouds.”

**[Get started with WaitLess →](docs/usage.md)**

## The moment is unpredictable. The waiting is real.

A photographer may watch a scene for hours for an event that lasts only briefly.
The composition is ready, the camera is set, but the clouds won't follow a schedule.
Keeping your eyes on the scene takes attention; looking away risks missing the
moment you came for.

An intervalometer offers a practical solution: keep taking photographs, then choose
the right one later. But frequent full-resolution captures consume storage and
battery, and leave a collection of near-identical images to review. The waiting
becomes sorting.

## Observe frequently. Capture selectively.

Watching a scene doesn't need to mean taking a full-resolution photograph every
time. WaitLess separates observation from capture:

1. **Describe your moment.** Use a plain-language instruction about the subject,
   visibility, or light you want.
2. **Let WaitLess watch.** It samples your Sony camera's live-view previews at your
   chosen interval. Claude evaluates the scene and can compare earlier previews
   when the instruction depends on a change.
3. **Capture when it matches.** When Claude identifies the requested condition,
   WaitLess asks the camera to take a photograph and downloads its original files.
   You choose exposure and image format on the camera.

| | Interval shooting | WaitLess |
| --- | --- | --- |
| During the wait | A full-resolution photograph at each interval | A lightweight preview at each observation |
| What triggers a photograph | The next scheduled interval | A match to your visual instruction |
| After the session | Search the captured sequence for the moment | Review selective captures alongside a preview timeline |

## Less watching. Less sorting. Your camera's quality.

- **Delegate the waiting.** Leave the connected camera and computer monitoring
  the scene instead of continuously watching it yourself. The aim is to preserve
  the opportunity to capture your moment while freeing your attention.
- **Keep fewer unnecessary originals.** When the requested condition is rare,
  selective capture can reduce the full-resolution files you store, transfer, and
  review. Every sampled preview is still saved locally as a record of the session.
- **Keep the photograph you came for.** Captures are separate camera exposures,
  with JPEG, HEIF, RAW, or paired files according to the camera's recording and
  transfer settings.
- **Choose how much to delegate.** Set observation frequency, session duration,
  photo limits, and an AI-call budget. Review previews and capture outcomes in the
  local browser interface while monitoring continues.

Live view, the connected computer, and AI evaluation still have power and API
costs. Net battery savings have not been measured.

## What would you wait for?

| Moment | Example instruction |
| --- | --- |
| A mountain emerging from clouds | “Take one photo when the mountain summit is clearly visible through the clouds.” |
| A village revealed by lifting fog | “Take one photo when the village below is clearly visible through the fog.” |
| Sunrise reaching a summit | “Capture the mountain when warm sunrise light is visible on its summit.” |
| A building going to sleep | “Take one photo of the building each time another apartment goes dark.” |

The last example explores **smart timelapse**: a sequence that advances when the
scene changes. WaitLess can retrieve earlier previews to compare observations;
reliably recognizing each change still needs field testing.

## Try it today

WaitLess is an early tool for **macOS and one Sony camera connected over USB**,
controlled through a local browser interface. The camera runtime is included for
Apple silicon and Intel Macs; normal setup needs no SDK download or native build.
You'll need Python 3.11+ and an Anthropic API key. Sampled previews are sent to
Claude for evaluation; preview copies and downloaded photographs are stored locally.

It is best suited to scenes that change slowly enough for a preview to be evaluated
and the shutter to respond. Recognition is experimental: events between
observations or during processing may be missed, and a visual match is not
guaranteed. The default observation interval is one minute.

Hardware testing so far has used a **Sony A7 IV**. Other models need on-device
verification; WaitLess checks camera capabilities before enabling capture. See the
[camera compatibility guide](docs/usage.md#camera-compatibility) for details.

**[Set up WaitLess →](docs/usage.md#setup)** ·
[Camera operation and troubleshooting](docs/usage.md#control-a-sony-camera-over-usb-macos) ·
[Development and tests](docs/usage.md#code-and-checks)
