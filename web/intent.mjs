// Shared image-space geometry for editing and viewing intent annotations.
export const colors = ["#c2dfac", "#8fd6ef", "#e7bc82", "#d6b5f5", "#f5aebc", "#8ee1c6"];
export function targetColor(id) {
  let hash = 0;
  for (const c of id) hash = (hash * 31 + c.charCodeAt(0)) >>> 0;
  return colors[hash % colors.length];
}
export function imageRect(width, height, imageWidth, imageHeight) {
  const scale = Math.min(width / imageWidth, height / imageHeight);
  const w = imageWidth * scale, h = imageHeight * scale;
  return { x: (width - w) / 2, y: (height - h) / 2, width: w, height: h };
}
export function imagePoint(x, y, rect, size, clamp = true) {
  const fit = imageRect(rect.width, rect.height, size.width, size.height);
  const px = (x - rect.left - fit.x) / fit.width, py = (y - rect.top - fit.y) / fit.height;
  if (!clamp && (px < 0 || px > 1 || py < 0 || py > 1)) return null;
  return [Math.max(0, Math.min(1, px)), Math.max(0, Math.min(1, py))];
}
export function moveBox(box, dx, dy) {
  dx = Math.max(-box[0], Math.min(1 - box[2], dx));
  dy = Math.max(-box[1], Math.min(1 - box[3], dy));
  return [box[0] + dx, box[1] + dy, box[2] + dx, box[3] + dy];
}
export function resizeBox(box, corner, point, minimum = .002) {
  const b = [...box];
  if (corner.includes("w")) b[0] = Math.min(b[2] - minimum, Math.max(0, point[0]));
  else b[2] = Math.max(b[0] + minimum, Math.min(1, point[0]));
  if (corner.includes("n")) b[1] = Math.min(b[3] - minimum, Math.max(0, point[1]));
  else b[3] = Math.max(b[1] + minimum, Math.min(1, point[1]));
  return b;
}
export function validPlan(plan) {
  if (!plan.condition.trim() || plan.condition.length > 8000) return "Describe the capture condition.";
  if (!plan.targets.length || plan.targets.length > 24) return "Add 1–24 targets, or a full-frame area for the whole scene.";
  for (const t of plan.targets) {
    if (!t.label.trim() || !t.relevance.trim()) return "Give every target a label and explain its relevance.";
    if (!t.box && t.mode === "fixed") return "Draw a box for every fixed area.";
    if (t.box && (t.box.length !== 4 || !t.box.every(Number.isFinite) || t.box[0] < 0 || t.box[1] < 0 ||
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
export function drawOverlay(svg, targets, width, height, {selected = null, editable = false} = {}) {
  const focused = svg.contains(document.activeElement) ? document.activeElement.dataset.target : null;
  svg.setAttribute("viewBox", `0 0 ${width} ${height}`);
  svg.replaceChildren();
  const unit = Math.max(width, height) / 100;
  for (const target of targets) {
    if (!target.box) continue;
    const [x1, y1, x2, y2] = target.box.map((n, i) => n * (i % 2 ? height : width));
    const group = svgNode("g", {"data-target": target.id, class: `target-box${selected === target.id ? " selected" : ""}`});
    if (editable) {
      group.setAttribute("tabindex", "0"); group.setAttribute("role", "button");
      group.setAttribute("aria-label", `${target.label}. Arrow keys move, Shift and arrows resize, Delete removes.`);
    }
    const color = targetColor(target.id);
    const rect = svgNode("rect", {x: x1, y: y1, width: x2 - x1, height: y2 - y1, stroke: color,
      fill: color, "fill-opacity": ".09", "stroke-width": selected === target.id ? 3 : 2, "vector-effect": "non-scaling-stroke"});
    if (target.status === "uncertain" || target.status === "absent") rect.setAttribute("stroke-dasharray", "6 4");
    group.append(rect);
    const label = svgNode("text", {x: x1 + unit * .5, y: Math.max(unit * 2.3, y1 + unit * 2.3), fill: color,
      "font-size": unit * 1.8, "paint-order": "stroke", stroke: "#111413", "stroke-width": unit * .45});
    label.textContent = target.label;
    group.append(label);
    if (editable && selected === target.id) for (const [corner, x, y] of [["nw", x1, y1], ["ne", x2, y1], ["sw", x1, y2], ["se", x2, y2]]) {
      group.append(svgNode("rect", {x: x - unit * .6, y: y - unit * .6, width: unit * 1.2, height: unit * 1.2,
        fill: color, stroke: "#111413", "data-corner": corner}));
    }
    svg.append(group);
  }
  if (focused && editable) [...svg.children].find(node => node.dataset.target === focused)?.focus({preventScroll: true});
}
export function annotationTargets(entry, intent) {
  if (!intent || !entry.annotations || entry.annotations_status !== "available" ||
      entry.annotations.frame_id !== entry.frame_id || entry.annotations.intent_revision !== intent.revision) return [];
  const byId = new Map(intent.targets.map(t => [t.id, t]));
  return entry.annotations.targets.filter(t => byId.has(t.id)).map(t => ({...byId.get(t.id), ...t}));
}
export function annotationText(entry, intent) {
  if (!intent) return "";
  if (entry.annotations_status !== "available") return entry.annotations_status === "pending" ? "Locating approved targets…" : "Highlights unavailable for this preview";
  const states = {met: "Capture condition met", unmet: "Waiting for the capture condition", uncertain: "Condition uncertain · keep checking"};
  const missing = annotationTargets(entry, intent).filter(t => t.status !== "visible").map(t => `${t.label}: ${t.status}`);
  return [states[entry.annotations.condition], ...missing].join(" · ");
}
