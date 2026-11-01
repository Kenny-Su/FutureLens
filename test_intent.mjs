import test from "node:test";
import assert from "node:assert/strict";
import {imageRect, imagePoint, moveBox, resizeBox, annotationTargets, annotationText, targetColor, validPlan} from "./web/intent.mjs";

test("landscape and portrait overlays account for letterboxing and viewer resizing", () => {
  assert.deepEqual(imageRect(600, 400, 1920, 1080), {x: 0, y: 31.25, width: 600, height: 337.5});
  assert.deepEqual(imageRect(600, 400, 1080, 1920), {x: 187.5, y: 0, width: 225, height: 400});
  for (const [width, height] of [[600, 400], [1100, 800], [300, 700]]) {
    const rect = {left: 20, top: 40, width, height};
    assert.deepEqual(imagePoint(20 + width/2, 40 + height/2, rect, {width: 1920, height: 1080}), [.5, .5]);
  }
  assert.equal(imagePoint(0, 0, {left: 0, top: 0, width: 600, height: 400}, {width: 1080, height: 1920}, false), null);
});
test("moving and resizing boxes preserves positive area and stays inside the image", () => {
  assert.deepEqual(moveBox([.1, .2, .3, .4], -2, -2), [0, 0, .19999999999999998, .2]);
  assert.deepEqual(resizeBox([.1, .2, .3, .4], "se", [2, 2]), [.1, .2, 1, 1]);
  const small = resizeBox([.1, .2, .3, .4], "nw", [1, 1]);
  assert.ok(small[0] < small[2] && small[1] < small[3]);
});
test("frame and revision checks prevent old boxes appearing on a new preview", () => {
  const intent = {revision: 2, targets: [{id: "peak", label: "Smaller peak", mode: "object"}]};
  const entry = {frame_id: 4, annotations_status: "available", annotations: {frame_id: 4, intent_revision: 2, condition: "uncertain",
    targets: [{id: "peak", status: "uncertain", box: null}]}};
  assert.equal(annotationTargets(entry, intent)[0].box, null);
  assert.match(annotationText(entry, intent), /Smaller peak: uncertain/);
  assert.deepEqual(annotationTargets({...entry, frame_id: 5}, intent), []);
  assert.deepEqual(annotationTargets(entry, {...intent, revision: 3}), []);
  assert.deepEqual(annotationTargets({...entry, annotations_status: "unavailable"}, intent), []);
  assert.equal(annotationText({}, null), "");
  assert.match(annotationText({}, intent), /unavailable/);
});
test("target colors are stable and invalid drafts cannot be approved", () => {
  assert.equal(targetColor("peak"), targetColor("peak"));
  const plan = {condition: "Peak emerges", targets: [{id: "peak", label: "Peak", relevance: "Subject", mode: "object", box: null}]};
  assert.equal(validPlan(plan), "");
  plan.targets[0].mode = "fixed";
  assert.match(validPlan(plan), /Draw a box/);
  plan.targets[0].box = [0, 0, 1, 1];
  assert.equal(validPlan(plan), "");
});
