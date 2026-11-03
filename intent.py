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


def validate_intent(value, width=1, height=1, *, allow_empty=False):
    if not isinstance(value, dict) or set(value) != {"condition", "targets"}:
        raise ValueError("Supply the capture condition and targets.")
    condition = "" if allow_empty and value["condition"] == "" else text(value["condition"], "Capture condition", 8000)
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


def validate_review(value, *, width=1, height=1):
    fields = {"condition", "reply", "question", "targets", "plan_ready"}
    if not isinstance(value, dict) or set(value) != fields:
        raise ValueError("The review reply is incomplete. Try again.")
    question = value["question"]
    if not isinstance(question, str) or len(question) > 2000:
        raise ValueError("The clarification question must be text up to 2,000 characters.")
    if type(value["plan_ready"]) is not bool or (value["plan_ready"] and question.strip()):
        raise ValueError("A plan cannot be ready while clarification is unresolved.")
    if not value["plan_ready"] and value["condition"] != "":
        raise ValueError("Leave the shooting plan empty until it is ready for approval.")
    result = {"plan_ready": value["plan_ready"],
              "reply": text(value["reply"], "Review reply", 8000), "question": question.strip()}
    result.update(validate_intent({k: value[k] for k in ("condition", "targets")}, width, height,
                                  allow_empty=not value["plan_ready"]))
    return result


@dataclass(frozen=True)
class ApprovedIntent:
    """Serialized approval prevents later draft mutations from affecting monitoring."""

    document: str
    reference_path: Path

    @classmethod
    def create(cls, draft, reference_path, approved_at):
        if (draft.get("plan_ready") is not True or draft.get("condition_stale")
                or draft.get("question") or draft.get("pending_message_id")):
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
