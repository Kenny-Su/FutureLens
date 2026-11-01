"""Review coordinates, immutable approval, and lightweight capture assessments."""

from copy import deepcopy
import json
from pathlib import Path
import tempfile
import unittest

from PIL import Image

from intent import ApprovedIntent, validate_intent, validate_review, validate_snapshot, validate_wait_reason, box


def plan():
    return {"condition": "Capture the requested change", "targets": [
        {"id": "scene", "label": "Scene", "box": [0, 0, 1, 1]}]}


def review_reply(condition="Capture the requested change", question=""):
    return {"condition": condition, "reply": "I will use the marked scene as our reference.", "question": question,
            "targets": plan()["targets"]}


def draft(root, request="scene"):
    root = Path(root)
    root.mkdir(parents=True, exist_ok=True)
    Image.new("RGB", (160, 96), "green").save(root / "reference.jpg")
    return {"id": root.name, "revision": 1, "request": request, "status": "ready", "error": None, "condition_stale": False,
            "question": "", "pending_message_id": None,
            "conversation": [{"id": "user-1", "role": "user", "text": request},
                             {"id": "assistant-1", "role": "assistant", "text": review_reply()["reply"],
                              "condition": plan()["condition"], "question": "", "revision": 1,
                              "targets": plan()["targets"], "reference_id": root.name}],
            "width": 160, "height": 96, "reference_at": "2026-11-01T00:00:00+00:00",
            "preview": f"/media/{root.name}/reference.jpg", **plan()}


def approval(root, request="scene"):
    data = draft(root, request)
    return ApprovedIntent.create(data, Path(root) / "reference.jpg", "2026-11-01T00:01:00+00:00")


def snapshot(frame_id=-1, condition="met"):
    return {"frame_id": frame_id, "intent_revision": 1, "condition": condition}


def bind_snapshot(response, payload):
    """Resolve placeholder snapshots in scripted test responses to their requested frame."""
    response = deepcopy(response)
    current = None
    for message in reversed(payload["messages"]):
        for block in message["content"]:
            if block["type"] == "text":
                try:
                    value = json.loads(block["text"])
                except ValueError:
                    continue
                if isinstance(value, dict) and "approved_intent" in value:
                    current = value
                    break
        if current:
            break
    for block in response.get("content", []):
        if not isinstance(block, dict):
            continue
        if block.get("type") == "tool_use":
            value = block["input"]
        elif block.get("type") == "text":
            try:
                value = json.loads(block["text"])
            except ValueError:
                continue
        else:
            continue
        if isinstance(value, dict) and value.get("snapshot", {}).get("frame_id") == -1 and current:
            value["snapshot"]["frame_id"] = current["preview"]["frame_id"]
            value["snapshot"]["intent_revision"] = current["intent_revision"]
            if block["type"] == "text":
                block["text"] = json.dumps(value)
    return response


class IntentTests(unittest.TestCase):
    def test_wait_reasons_need_consistent_assessments_and_session_evidence(self):
        for reason, condition, photos, cooldown in [
            ("condition_unmet", "unmet", 0, 0), ("condition_uncertain", "uncertain", 0, 0),
            ("already_captured", "met", 1, 0), ("cooldown", "met", 1, 5),
        ]:
            self.assertEqual(validate_wait_reason(reason, condition, photos_taken=photos,
                                                 cooldown_remaining_s=cooldown), reason)
        for reason, condition, photos, cooldown in [
            (None, "met", 0, 0), ("", "met", 0, 0), ([], "met", 0, 0),
            ("condition_unmet", "met", 0, 0), ("condition_uncertain", "met", 0, 0),
            ("already_captured", "met", 0, 0), ("cooldown", "met", 1, 0),
        ]:
            with self.subTest(reason=reason), self.assertRaises(ValueError):
                validate_wait_reason(reason, condition, photos_taken=photos, cooldown_remaining_s=cooldown)

    def test_coordinates_and_whole_scene(self):
        self.assertEqual(box([0, 20, 480, 540], 960, 540), [0, 20/540, .5, 1])
        self.assertEqual(box([54, 96, 540, 960], 540, 960), [.1, .1, 1, 1])
        for bad in ([0, 0, 0, 1], [-1, 0, 1, 1], [0, 0, 2, 1], [0, 0, float("nan"), 1],
                    [0, 0, float("inf"), 1], [0, 0, 10**400, 1], [False, 0, 1, 1], [0, 1, 1], "box"):
            with self.subTest(box=bad), self.assertRaises(ValueError):
                box(bad)
        self.assertEqual(validate_intent(plan()), plan())

    def test_optional_highlights_require_a_real_box_when_present(self):
        data = plan()
        self.assertEqual(validate_intent(data), data)
        data["targets"][0]["box"] = None
        with self.assertRaises(ValueError):
            validate_intent(data)
        data["targets"][0]["box"] = [0, 0, 1, 1]
        data["targets"][0]["label"] = " "
        with self.assertRaises(ValueError):
            validate_intent(data)
        data["targets"] = []
        self.assertEqual(validate_intent(data), data)
        with tempfile.TemporaryDirectory() as folder:
            approved = ApprovedIntent.create({**draft(folder), **data}, Path(folder) / "reference.jpg", "now")
            self.assertEqual(approved.data["targets"], [])

    def test_snapshot_is_bound_to_frame_revision_and_condition(self):
        intent = {**plan(), "revision": 1}
        for change in ({"frame_id": 2}, {"frame_id": True}, {"intent_revision": 2},
                       {"condition": "maybe"}, {"targets": []}):
            with self.subTest(change=change), self.assertRaises(ValueError):
                validate_snapshot({**snapshot(0), **change}, intent, 0)

    def test_assessment_does_not_require_target_locations_or_visibility(self):
        intent = {**plan(), "revision": 1}
        intent["targets"].append({**intent["targets"][0], "id": "object", "box": None})
        for condition in ("met", "unmet", "uncertain"):
            self.assertEqual(validate_snapshot(snapshot(0, condition), intent, 0), snapshot(0, condition))

    def test_review_still_rejects_duplicate_target_ids(self):
        value = plan()
        value["targets"] *= 2
        with self.assertRaises(ValueError):
            validate_intent(value)

    def test_approval_is_an_independent_snapshot(self):
        with tempfile.TemporaryDirectory() as folder:
            data = draft(folder)
            approved = ApprovedIntent.create(data, Path(folder) / "reference.jpg", "now")
            data["targets"][0]["label"] = "Different object"
            approved.data["targets"][0]["box"][0] = .2
            self.assertEqual(approved.data["targets"][0]["label"], "Scene")
            self.assertEqual(approved.data["targets"][0]["box"], [0, 0, 1, 1])

    def test_outdated_explanation_cannot_be_approved(self):
        with tempfile.TemporaryDirectory() as folder:
            for field, value in (("condition_stale", True), ("question", "One photo or every occurrence?"),
                                 ("pending_message_id", "in-flight")):
                data = draft(folder); data[field] = value
                with self.subTest(field=field), self.assertRaises(ValueError):
                    ApprovedIntent.create(data, Path(folder) / "reference.jpg", "now")

    def test_review_reply_requires_a_condition_reply_and_explicit_question_state(self):
        self.assertEqual(validate_review(review_reply()), review_reply())
        for invalid in ({"condition": "Capture"}, {**review_reply(), "question": None},
                        {**review_reply(), "reply": ""}, {**review_reply(), "targets": None},
                        {**review_reply(), "question": "x" * 2001}):
            with self.subTest(invalid=invalid), self.assertRaises(ValueError):
                validate_review(invalid)


if __name__ == "__main__":
    unittest.main()
