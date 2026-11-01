"""Reviewed photography intent and frame-bound localization contracts."""

from copy import deepcopy
from dataclasses import dataclass
import json
from pathlib import Path
import re

from PIL import Image


def object_schema(properties):
    return {"type": "object", "properties": properties, "required": list(properties),
            "additionalProperties": False}


BOX_SCHEMA = {"anyOf": [{"type": "array", "items": {"type": "number"}}, {"type": "null"}]}
TARGET_SCHEMA = object_schema({
    "id": {"type": "string"}, "label": {"type": "string"}, "relevance": {"type": "string"},
    "mode": {"type": "string", "enum": ["object", "fixed"]}, "box": BOX_SCHEMA,
})
INTENT_SCHEMA = object_schema({"condition": {"type": "string"},
                               "targets": {"type": "array", "items": TARGET_SCHEMA}})
SNAPSHOT_SCHEMA = object_schema({
    "frame_id": {"type": "integer"}, "intent_revision": {"type": "integer"},
    "condition": {"type": "string", "enum": ["met", "unmet", "uncertain"]},
    "targets": {"type": "array", "items": object_schema({
        "id": {"type": "string"},
        "status": {"type": "string", "enum": ["visible", "absent", "uncertain"]},
        "box": BOX_SCHEMA,
    })},
})
WAIT_SCHEMA = object_schema({"action": {"type": "string", "enum": ["wait"]}, "snapshot": SNAPSHOT_SCHEMA})


def text(value, label, limit):
    if not isinstance(value, str) or not value.strip() or len(value) > limit:
        raise ValueError(f"{label} must contain 1–{limit} characters.")
    return value.strip()


def box(value, width=1, height=1):
    """Validate in the supplied pixel space and return normalized coordinates."""
    if value is None:
        return None
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
    if not isinstance(targets, list) or not 1 <= len(targets) <= 24:
        raise ValueError("Use between 1 and 24 targets; use a fixed full-frame area for the whole scene.")
    result, ids = [], set()
    for target in targets:
        if not isinstance(target, dict) or set(target) != {"id", "label", "relevance", "mode", "box"}:
            raise ValueError("Invalid target fields.")
        identity = target["id"]
        if not isinstance(identity, str) or not re.fullmatch(r"[A-Za-z0-9_-]{1,64}", identity) or identity in ids:
            raise ValueError("Each target needs a unique ID.")
        ids.add(identity)
        if target["mode"] not in ("object", "fixed"):
            raise ValueError("Choose Object or Fixed area for each target.")
        bounds = box(target["box"], width, height)
        if target["mode"] == "fixed" and bounds is None:
            raise ValueError("Draw a box for every fixed area.")
        result.append({"id": identity, "label": text(target["label"], "Target label", 120),
                       "relevance": text(target["relevance"], "Target relevance", 500),
                       "mode": target["mode"], "box": bounds})
    return {"condition": condition, "targets": result}


def validate_snapshot(value, intent, frame_id, size):
    if not isinstance(value, dict) or set(value) != {"frame_id", "intent_revision", "condition", "targets"}:
        raise ValueError("Missing observation annotations.")
    if (type(value["frame_id"]) is not int or value["frame_id"] != frame_id
            or type(value["intent_revision"]) is not int or value["intent_revision"] != intent["revision"]):
        raise ValueError("Annotations belong to a different frame or intent revision.")
    if value["condition"] not in ("met", "unmet", "uncertain") or not isinstance(value["targets"], list):
        raise ValueError("Invalid observation condition or targets.")
    approved = {t["id"]: t for t in intent["targets"]}
    seen, targets = set(), []
    for target in value["targets"]:
        if not isinstance(target, dict) or set(target) != {"id", "status", "box"}:
            raise ValueError("Invalid observation target.")
        identity = target["id"]
        if not isinstance(identity, str) or identity not in approved or identity in seen:
            raise ValueError("Unknown or duplicate target.")
        seen.add(identity)
        status = target["status"]
        if status not in ("visible", "absent", "uncertain"):
            raise ValueError("Invalid target visibility.")
        bounds = box(target["box"], *size)
        if approved[identity]["mode"] == "fixed":
            bounds = deepcopy(approved[identity]["box"])
        elif status == "visible" and bounds is None:
            raise ValueError("Visible objects need a current box.")
        elif status != "visible" and bounds is not None:
            raise ValueError("Absent or uncertain objects must not have an invented location.")
        targets.append({"id": identity, "status": status, "box": bounds})
    if seen != set(approved):
        raise ValueError("Report every approved target exactly once.")
    return {**value, "targets": targets}


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
        plan = validate_intent({k: draft[k] for k in ("condition", "targets")})
        with Image.open(reference_path) as image:
            if list(image.size) != [draft["width"], draft["height"]]:
                raise ValueError("The reference image dimensions have changed. Review again.")
        data = {key: draft[key] for key in ("id", "revision", "request", "width", "height", "reference_at")}
        data.update(plan, approved_at=approved_at, reference_file="intent-reference.jpg")
        return cls(json.dumps(data, allow_nan=False), Path(reference_path))

    @property
    def data(self):
        return json.loads(self.document)
