"""Reviewed photography plans and immutable user approval."""

from dataclasses import dataclass
import json
from pathlib import Path
import re

from PIL import Image


def object_schema(properties):
    return {"type": "object", "properties": properties, "required": list(properties),
            "additionalProperties": False}


BOX_SCHEMA = {"type": "array", "items": {"type": "number"},
              "description": "[x1,y1,x2,y2] absolute pixel coordinates within image_width and image_height."}
TARGET_SCHEMA = object_schema({
    "id": {"type": "string"}, "label": {"type": "string"}, "box": BOX_SCHEMA,
})

PLAN_SCHEMA = object_schema({
    "instructions": {"type": "string", "description": "Self-contained shooting instructions."},
    "reference": {
        "anyOf": [{"type": "null"}, object_schema({
            "photo": {"type": "string", "enum": ["reference.jpg"], "description": "The saved planning preview."},
            "purpose": {"type": "string", "description": "How to use this photo when evaluating the current preview."},
            "regions": {"type": "array", "description": "Optional plan regions, independent of conversational highlights.",
                        "items": object_schema({"id": {"type": "string"}, "label": {"type": "string"},
                                                "box": {"type": "array", "items": {"type": "number"},
                                                        "description": "Normalized [x1,y1,x2,y2] coordinates from 0 to 1 on the reference photo."}})},
        })],
    },
})


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


def validate_regions(targets, width=1, height=1):
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
    return result


def validate_plan(value):
    if not isinstance(value, dict) or set(value) != {"instructions", "reference"}:
        raise ValueError("Supply plan instructions and an optional reference.")
    reference = value["reference"]
    if reference is not None:
        if (not isinstance(reference, dict) or set(reference) != {"photo", "purpose", "regions"}
                or reference["photo"] != "reference.jpg"):
            raise ValueError("Use the saved planning photo as the reference.")
        reference = {"photo": reference["photo"], "purpose": text(reference["purpose"], "Reference purpose", 8000),
                     "regions": validate_regions(reference["regions"])}
    return {"instructions": text(value["instructions"], "Plan instructions", 8000), "reference": reference}


def validate_review(value):
    if not isinstance(value, dict) or set(value) != {"reply", "highlights", "plan"}:
        raise ValueError("The review reply is incomplete. Try again.")
    return {"reply": text(value["reply"], "Review reply", 8000),
            "highlights": validate_regions(value["highlights"]),
            "plan": validate_plan(value["plan"]) if value["plan"] is not None else None}


@dataclass(frozen=True)
class ApprovedPlan:
    """Serialized approval prevents later draft mutations from affecting monitoring."""

    document: str
    reference_path: Path

    @classmethod
    def create(cls, draft, reference_path, approved_at):
        if not draft["plans"]:
            raise ValueError("Resolve the latest review before starting.")
        proposal = draft["plans"][-1]
        plan = validate_plan(proposal["plan"])
        with Image.open(reference_path) as image:
            if list(image.size) != [proposal["width"], proposal["height"]]:
                raise ValueError("The reference image dimensions have changed. Review again.")
        data = {key: draft[key] for key in ("id", "revision", "request")}
        data.update({key: proposal[key] for key in ("width", "height", "reference_at")})
        data["plan_id"] = proposal["id"]
        data.update(plan=plan, approved_at=approved_at)
        if "source" in draft:
            data["source"] = draft["source"]
        return cls(json.dumps(data, allow_nan=False), Path(reference_path))

    @property
    def data(self):
        return json.loads(self.document)
