"""Intent contracts: coordinates, corrections, absence, and immutable approval."""

from copy import deepcopy
import json
from pathlib import Path
import tempfile
import unittest

from PIL import Image

from intent import ApprovedIntent, validate_intent, validate_snapshot, box


def plan():
    return {"condition": "Capture the requested change", "targets": [
        {"id": "scene", "label": "Scene", "relevance": "The requested change", "mode": "fixed", "box": [0, 0, 1, 1]}]}


def draft(root, request="scene"):
    root = Path(root)
    root.mkdir(parents=True, exist_ok=True)
    Image.new("RGB", (160, 96), "green").save(root / "reference.jpg")
    return {"id": root.name, "revision": 1, "request": request, "status": "ready", "error": None,
            "width": 160, "height": 96, "reference_at": "2026-11-01T00:00:00+00:00",
            "preview": f"/media/{root.name}/reference.jpg", **plan()}


def approval(root, request="scene"):
    data = draft(root, request)
    return ApprovedIntent.create(data, Path(root) / "reference.jpg", "2026-11-01T00:01:00+00:00")


def snapshot(frame_id=-1, condition="met"):
    return {"frame_id": frame_id, "intent_revision": 1, "condition": condition,
            "targets": [{"id": "scene", "status": "visible", "box": [0, 0, 160, 96]}]}


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
    def test_coordinates_and_whole_scene(self):
        self.assertEqual(box([0, 20, 480, 540], 960, 540), [0, 20/540, .5, 1])
        self.assertEqual(box([54, 96, 540, 960], 540, 960), [.1, .1, 1, 1])
        for bad in ([0, 0, 0, 1], [-1, 0, 1, 1], [0, 0, 2, 1], [0, 0, float("nan"), 1],
                    [0, 0, float("inf"), 1], [0, 0, 10**400, 1], [False, 0, 1, 1], [0, 1, 1], "box"):
            with self.subTest(box=bad), self.assertRaises(ValueError):
                box(bad)
        self.assertEqual(validate_intent(plan()), plan())

    def test_unknown_objects_can_be_reviewed_but_fixed_regions_need_geometry(self):
        data = plan()
        data["targets"][0].update(mode="object", box=None)
        self.assertIsNone(validate_intent(data)["targets"][0]["box"])
        data["targets"][0]["mode"] = "fixed"
        with self.assertRaises(ValueError):
            validate_intent(data)

    def test_snapshot_is_bound_to_frame_revision_and_exact_target_set(self):
        intent = {**plan(), "revision": 1}
        for change in ({"frame_id": 2}, {"frame_id": True}, {"intent_revision": 2}, {"targets": []},
                       {"targets": [{"id": "wrong", "status": "visible", "box": None}]},
                       {"targets": snapshot()["targets"] * 2}):
            with self.subTest(change=change), self.assertRaises(ValueError):
                validate_snapshot({**snapshot(0), **change}, intent, 0, (160, 96))

    def test_fixed_regions_never_drift_and_objects_relocalize(self):
        intent = {**plan(), "revision": 1}
        value = snapshot(0)
        value["targets"][0]["box"] = [20, 10, 80, 60]
        self.assertEqual(validate_snapshot(value, intent, 0, (160, 96))["targets"][0]["box"], [0, 0, 1, 1])
        intent["targets"][0]["mode"] = "object"
        self.assertEqual(validate_snapshot(value, intent, 0, (160, 96))["targets"][0]["box"], [20/160, 10/96, .5, 60/96])

    def test_absence_can_satisfy_condition_but_never_fabricates_object_boxes(self):
        intent = {**plan(), "revision": 1}
        intent["targets"][0]["mode"] = "object"
        value = snapshot(0)
        value["targets"][0].update(status="absent", box=None)
        self.assertEqual(validate_snapshot(value, intent, 0, (160, 96))["condition"], "met")
        value["condition"] = "uncertain"
        value["targets"][0]["status"] = "uncertain"
        self.assertIsNone(validate_snapshot(value, intent, 0, (160, 96))["targets"][0]["box"])
        value["targets"][0]["box"] = [0, 0, 160, 96]
        with self.assertRaises(ValueError):
            validate_snapshot(value, intent, 0, (160, 96))

    def test_approval_is_an_independent_snapshot(self):
        with tempfile.TemporaryDirectory() as folder:
            data = draft(folder)
            approved = ApprovedIntent.create(data, Path(folder) / "reference.jpg", "now")
            data["targets"][0]["label"] = "Different object"
            approved.data["targets"][0]["box"][0] = .2
            self.assertEqual(approved.data["targets"][0]["label"], "Scene")
            self.assertEqual(approved.data["targets"][0]["box"], [0, 0, 1, 1])


if __name__ == "__main__":
    unittest.main()
