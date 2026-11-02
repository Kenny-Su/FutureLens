"""Reviewed photography intent and frame-bound capture assessments."""

from dataclasses import dataclass
import json
from pathlib import Path
import re

from PIL import Image


def object_schema(properties):
    return {"type": "object", "properties": properties, "required": list(properties),
            "additionalProperties": False}


BOX_SCHEMA = {"type": "array", "items": {"type": "number"}}
TARGET_SCHEMA = object_schema({
    "id": {"type": "string"}, "label": {"type": "string"}, "box": BOX_SCHEMA,
})
REVIEW_SCHEMA = object_schema({
    "condition": {"type": "string"}, "reply": {"type": "string"},
    "question": {"type": "string"},
    "targets": {"type": "array", "items": TARGET_SCHEMA},
})
SNAPSHOT_SCHEMA = object_schema({
    "frame_id": {"type": "integer"}, "intent_revision": {"type": "integer"},
    "condition": {"type": "string", "enum": ["met", "unmet", "uncertain"]},
})
WAIT_REASON_SCHEMA = {"type": "string", "enum": [
    "condition_unmet", "condition_uncertain", "already_captured", "cooldown",
]}
WAIT_SCHEMA = object_schema({"action": {"type": "string", "enum": ["wait"]},
                             "snapshot": SNAPSHOT_SCHEMA, "wait_reason": WAIT_REASON_SCHEMA})


def validate_wait_reason(reason, condition, *, photos_taken, cooldown_remaining_s):
    """A non-capture decision needs a reason consistent with observable session facts.

    The agent still decides which requested occurrence/quantity has been captured;
    a positive photo count alone never implies that a repeating request is finished.
    """
    if reason not in WAIT_REASON_SCHEMA["enum"]:
        raise ValueError("Supply a valid wait_reason for a decision without a photograph.")
    if reason in ("condition_unmet", "condition_uncertain"):
        if condition != reason.removeprefix("condition_"):
            raise ValueError("The wait_reason contradicts the reported capture condition.")
    elif reason == "already_captured" and photos_taken <= 0:
        raise ValueError("No photograph has been downloaded; already_captured is not supported.")
    elif reason == "cooldown" and cooldown_remaining_s <= 0:
        raise ValueError("No capture cooldown is active.")
    return reason


def text(value, label, limit):
    if not isinstance(value, str) or not value.strip() or len(value) > limit:
        raise ValueError(f"{label} must contain 1–{limit} characters.")
    return value.strip()


def box(value, width=1, height=1):
    """Validate in the supplied pixel space and return normalized coordinates."""
    if (not isinstance(value, list) or len(value) != 4
            or any(type(n) not in (int, float) or not 0 <= n <= max(width, height) for n in value)):
        raise ValueError("A box needs four finite coordinates.")
    x1, y1, x2, y2 = value
    if not (0 <= x1 < x2 <= width and 0 <= y1 < y2 <= height):
        raise ValueError("Boxes must have positive area and stay inside the image.")
    return [x1 / width, y1 / height, x2 / width, y2 / height]


def validate_intent(value, width=1, height=1):
    if not isinstance(value, dict) or set(value) != {"condition", "targets"}:
        raise ValueError("Supply the capture condition and targets.")
    condition = text(value["condition"], "Capture condition", 8000)
    targets = value["targets"]
    if not isinstance(targets, list) or len(targets) > 24:
        raise ValueError("Supply up to 24 optional highlights of visible details.")
    result, ids = [], set()
    for target in targets:
        if not isinstance(target, dict) or set(target) != {"id", "label", "box"}:
            raise ValueError("Invalid target fields.")
        identity = target["id"]
        if not isinstance(identity, str) or not re.fullmatch(r"[A-Za-z0-9_-]{1,64}", identity) or identity in ids:
            raise ValueError("Each target needs a unique ID.")
        ids.add(identity)
        bounds = box(target["box"], width, height)
        result.append({"id": identity, "label": text(target["label"], "Target label", 120),
                       "box": bounds})
    return {"condition": condition, "targets": result}


def validate_snapshot(value, intent, frame_id):
    if not isinstance(value, dict) or set(value) != {"frame_id", "intent_revision", "condition"}:
        raise ValueError("Supply the current frame, intent revision, and capture condition assessment.")
    if (type(value["frame_id"]) is not int or value["frame_id"] != frame_id
            or type(value["intent_revision"]) is not int or value["intent_revision"] != intent["revision"]):
        raise ValueError("The assessment belongs to a different frame or intent revision.")
    if value["condition"] not in ("met", "unmet", "uncertain"):
        raise ValueError("Invalid capture condition assessment.")
    return dict(value)


def validate_review(value, *, width=1, height=1):
    fields = {"condition", "reply", "question", "targets"}
    if not isinstance(value, dict) or set(value) != fields:
        raise ValueError("The review reply is incomplete. Try again.")
    question = value["question"]
    if not isinstance(question, str) or len(question) > 2000:
        raise ValueError("The clarification question must be text up to 2,000 characters.")
    result = {"condition": text(value["condition"], "Capture condition", 8000),
              "reply": text(value["reply"], "Review reply", 8000), "question": question.strip()}
    result.update(validate_intent({k: value[k] for k in ("condition", "targets")}, width, height))
    return result


def json_response(response):
    if not isinstance(response, dict) or response.get("stop_reason") != "end_turn":
        raise ValueError("The interpretation was incomplete. Try again.")
    blocks = response.get("content")
    if not isinstance(blocks, list) or any(not isinstance(b, dict) or b.get("type") not in
                                         ("text", "thinking", "redacted_thinking") for b in blocks):
        raise ValueError("Invalid interpretation response.")
    try:
        return json.loads("".join(b["text"] for b in blocks if b["type"] == "text"))
    except (KeyError, TypeError, ValueError):
        raise ValueError("The interpretation was not valid JSON. Try again.") from None


@dataclass(frozen=True)
class ApprovedIntent:
    """Serialized approval prevents later draft mutations from affecting monitoring."""

    document: str
    reference_path: Path

    @classmethod
    def create(cls, draft, reference_path, approved_at):
        if draft.get("condition_stale") or draft.get("question") or draft.get("pending_message_id"):
            raise ValueError("Resolve the latest review before starting.")
        plan = validate_intent({k: draft[k] for k in ("condition", "targets")})
        with Image.open(reference_path) as image:
            if list(image.size) != [draft["width"], draft["height"]]:
                raise ValueError("The reference image dimensions have changed. Review again.")
        data = {key: draft[key] for key in ("id", "revision", "request", "width", "height", "reference_at")}
        data.update(plan, approved_at=approved_at, reference_file="intent-reference.jpg")
        if "source" in draft:
            data["source"] = draft["source"]
        return cls(json.dumps(data, allow_nan=False), Path(reference_path))

    @property
    def data(self):
        return json.loads(self.document)
