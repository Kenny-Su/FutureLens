// Read-only reference highlights and historical observation annotations.
export const colors = ["#c2dfac", "#8fd6ef", "#e7bc82", "#d6b5f5", "#f5aebc", "#8ee1c6"];
export function targetColor(id) {
  let hash = 0;
  for (const c of id) hash = (hash * 31 + c.charCodeAt(0)) >>> 0;
  return colors[hash % colors.length];
}
export function validPlan(plan) {
  if (!plan.condition.trim() || plan.condition.length > 8000) return "Describe the capture condition.";
  if (!Array.isArray(plan.targets) || plan.targets.length > 24) return "Use up to 24 optional highlights.";
  for (const t of plan.targets) {
    if (!t.label.trim()) return "Give every object a name.";
    if (!Array.isArray(t.box) || (t.box.length !== 4 || !t.box.every(Number.isFinite) || t.box[0] < 0 || t.box[1] < 0 ||
      t.box[2] > 1 || t.box[3] > 1 || t.box[0] >= t.box[2] || t.box[1] >= t.box[3])) return "Keep boxes inside the image.";
  }
  return "";
}
const ns = "http://www.w3.org/2000/svg";
export function svgNode(tag, attributes = {}) {
  const node = document.createElementNS(ns, tag);
  for (const [key, value] of Object.entries(attributes)) node.setAttribute(key, value);
  return node;
}
export function drawOverlay(svg, targets, width, height) {
  svg.setAttribute("viewBox", `0 0 ${width} ${height}`);
  svg.setAttribute("preserveAspectRatio", "xMidYMid meet");
  svg.replaceChildren();
  const unit = Math.max(width, height) / 100;
  const labels = [];
  for (const target of targets) {
    if (!target.box) continue; // Historical observations can contain unlocated targets.
    const [x1, y1, x2, y2] = target.box.map((n, i) => n * (i % 2 ? height : width));
    const group = svgNode("g", {"data-target": target.id});
    const color = targetColor(target.id);
    const rect = svgNode("rect", {x: x1, y: y1, width: x2 - x1, height: y2 - y1, stroke: color,
      fill: color, "fill-opacity": ".09", "stroke-width": 2, "vector-effect": "non-scaling-stroke"});
    if (target.status === "uncertain" || target.status === "absent") rect.setAttribute("stroke-dasharray", "6 4");
    group.append(rect);
    const label = svgNode("text", {x: x1 + unit * .5, y: Math.max(unit * 2.3, y1 + unit * 2.3), fill: color,
      "font-size": unit * 1.8, "paint-order": "stroke", stroke: "#111413", "stroke-width": unit * .45});
    label.textContent = target.label;
    labels.push(label); svg.append(group);
  }
  svg.append(...labels);
}
export function annotationTargets(entry, intent) {
  // Historical reports can contain sampled localizations. New observations only
  // assess the condition; never project their reference boxes onto a current frame.
  if (entry.assessment_status !== undefined || !intent || !Array.isArray(entry.annotations?.targets) || entry.annotations_status !== "available" ||
      entry.annotations.frame_id !== entry.frame_id || entry.annotations.intent_revision !== intent.revision) return [];
  const byId = new Map(intent.targets.map(t => [t.id, t]));
  return entry.annotations.targets.filter(t => byId.has(t.id)).map(t => ({...byId.get(t.id), ...t}));
}
export function observationText(entry, intent) {
  if (!intent) return "";
  const current = entry.assessment_status !== undefined;
  const status = current ? entry.assessment_status : entry.annotations_status;
  const snapshot = current ? entry.assessment : entry.annotations;
  if (status !== "available" || !snapshot || snapshot.frame_id !== entry.frame_id || snapshot.intent_revision !== intent.revision) {
    return current ? (status === "pending" ? "Evaluating the capture condition…" : "Capture assessment unavailable for this preview")
      : (status === "pending" ? "Locating approved targets…" : "Highlights unavailable for this preview");
  }
  const states = {met: "Capture condition met", unmet: "Waiting for the capture condition"};
  let decision = "";
  if (entry.capture_status === "downloaded") decision = "Photo saved";
  else if (entry.capture_status === "requested") decision = "Photo requested";
  else if (entry.capture_status === "shutter_sent") decision = "Shutter sent · awaiting photo";
  else if (!decision && snapshot.condition === "met") {
    decision = entry.capture_status?.startsWith("skipped_") ? "Photo skipped"
      : entry.capture_status?.startsWith("failed_") ? "Capture failed or unconfirmed"
      : entry.outcome ? "No photo requested" : "Capture decision pending";
  }
  const missing = annotationTargets(entry, intent).filter(t => t.status !== "visible").map(t => `${t.label}: ${t.status}`);
  return [states[snapshot.condition], decision, ...missing].filter(Boolean).join(" · ");
}
