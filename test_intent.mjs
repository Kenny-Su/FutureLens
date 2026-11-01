import test from "node:test";
import assert from "node:assert/strict";
import {drawOverlay, annotationTargets, observationText, targetColor, validPlan} from "./web/intent.mjs";

test("whiteboard uses image-space geometry without interactive controls and clears old highlights", () => {
  class Node {
    constructor(tag) { this.tag = tag; this.attributes = {}; this.children = []; }
    setAttribute(key, value) { this.attributes[key] = String(value); }
    replaceChildren() { this.children = []; }
    append(...children) { this.children.push(...children); }
  }
  const previousDocument = globalThis.document;
  globalThis.document = {createElementNS: (_, tag) => new Node(tag)};
  try {
    for (const [width, height] of [[960, 540], [540, 960]]) {
      const svg = new Node("svg");
      drawOverlay(svg, [{id: "peak", label: "Summit", box: [.1, .2, .5, .8]}], width, height);
      assert.equal(svg.attributes.viewBox, `0 0 ${width} ${height}`);
      assert.equal(svg.attributes.preserveAspectRatio, "xMidYMid meet");
      const [group, label] = svg.children, rect = group.children[0];
      assert.equal(Number(rect.attributes.x), width * .1);
      assert.equal(Number(rect.attributes.y), height * .2);
      assert.equal(group.children.length, 1);
      assert.equal(group.attributes.role, undefined);
      assert.equal(group.attributes.tabindex, undefined);
      assert.equal(label.textContent, "Summit");
      drawOverlay(svg, [], width, height);
      assert.deepEqual(svg.children, []);
    }
  } finally { globalThis.document = previousDocument; }
});
test("frame and revision checks prevent old boxes appearing on a new preview", () => {
  const intent = {revision: 2, targets: [{id: "peak", label: "Smaller peak", mode: "object"}]};
  const entry = {frame_id: 4, annotations_status: "available", annotations: {frame_id: 4, intent_revision: 2, condition: "uncertain",
    targets: [{id: "peak", status: "uncertain", box: null}]}};
  assert.equal(annotationTargets(entry, intent)[0].box, null);
  assert.match(observationText(entry, intent), /Smaller peak: uncertain/);
  assert.deepEqual(annotationTargets({...entry, frame_id: 5}, intent), []);
  assert.deepEqual(annotationTargets(entry, {...intent, revision: 3}), []);
  assert.deepEqual(annotationTargets({...entry, annotations_status: "unavailable"}, intent), []);
  assert.equal(observationText({}, null), "");
  assert.match(observationText({}, intent), /unavailable/);
});
test("highlights are optional but malformed highlights cannot be approved", () => {
  assert.equal(targetColor("peak"), targetColor("peak"));
  const plan = {condition: "Peak emerges", targets: [{id: "peak", label: "Peak", box: null}]};
  assert.match(validPlan(plan), /inside/);
  plan.targets[0].box = [0, 0, 1, 1];
  assert.equal(validPlan(plan), "");
  plan.targets[0].label = " ";
  assert.match(validPlan(plan), /name/);
  plan.targets = [];
  assert.equal(validPlan(plan), "");
});
test("met captions distinguish saved photos, justified waiting, and historical no-action turns", () => {
  const intent = {revision: 1, targets: []};
  const entry = {frame_id: 0, outcome: "no_photo", capture_status: "not_requested", annotations_status: "available",
    annotations: {frame_id: 0, intent_revision: 1, condition: "met", targets: []}};
  assert.equal(observationText(entry, intent), "Capture condition met · No photo requested");
  assert.match(observationText({...entry, capture_status: "downloaded"}, intent), /Photo saved/);
  assert.match(observationText({...entry, wait_reason: "already_captured"}, intent), /already photographed/);
  assert.match(observationText({...entry, wait_reason: "cooldown"}, intent), /Waiting for capture cooldown/);
  assert.match(observationText({...entry, capture_status: "skipped_stale_frame"}, intent), /Photo skipped/);
});
test("new observations show the condition without tracking or projecting reference boxes", () => {
  const intent = {revision: 1, targets: [{id: "scene", label: "Scene", box: [0, 0, 1, 1]}]};
  const entry = {frame_id: 0, outcome: "no_photo", capture_status: "not_requested", assessment_status: "available",
    assessment: {frame_id: 0, intent_revision: 1, condition: "unmet"}};
  assert.deepEqual(annotationTargets(entry, intent), []);
  assert.equal(observationText(entry, intent), "Waiting for the capture condition");
  assert.equal(observationText({...entry, assessment: null, assessment_status: "pending"}, intent), "Evaluating the capture condition…");
  assert.match(observationText({...entry, assessment: null, assessment_status: "unavailable"}, intent), /Capture assessment unavailable/);
  assert.match(observationText({...entry, frame_id: 1}, intent), /unavailable/);
  const mixed = {...entry, annotations_status: "available", annotations: {...entry.assessment, targets: intent.targets}};
  assert.deepEqual(annotationTargets(mixed, intent), []);
});
