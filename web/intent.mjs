// Plan reference display.
export const colors = ["#c2dfac", "#8fd6ef", "#e7bc82", "#d6b5f5", "#f5aebc", "#8ee1c6"];
export function targetColor(id) {
  let hash = 0;
  for (const c of id) hash = (hash * 31 + c.charCodeAt(0)) >>> 0;
  return colors[hash % colors.length];
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
    const [x1, y1, x2, y2] = target.box.map((n, i) => n * (i % 2 ? height : width));
    const group = svgNode("g", {"data-target": target.id});
    const color = targetColor(target.id);
    const rect = svgNode("rect", {x: x1, y: y1, width: x2 - x1, height: y2 - y1, stroke: color,
      fill: color, "fill-opacity": ".09", "stroke-width": 2, "vector-effect": "non-scaling-stroke"});
    group.append(rect);
    const label = svgNode("text", {x: x1 + unit * .5, y: Math.max(unit * 2.3, y1 + unit * 2.3), fill: color,
      "font-size": unit * 1.8, "paint-order": "stroke", stroke: "#111413", "stroke-width": unit * .45});
    label.textContent = target.label;
    labels.push(label); svg.append(group);
  }
  svg.append(...labels);
}
export function renderPlanReference(container, plan, source) {
  container.hidden = !plan?.reference;
  if (container.hidden) { container.replaceChildren(); delete container.dataset.key; return; }
  const key = JSON.stringify([source, plan.reference]);
  if (container.dataset.key === key) return;
  container.dataset.key = key;
  const purpose = document.createElement("p"); purpose.textContent = plan.reference.purpose;
  const surface = document.createElement("div"); surface.className = "image-surface";
  const image = document.createElement("img"); image.alt = "Plan reference photo";
  const overlay = svgNode("svg", {class: "intent-overlay", role: "img", "aria-label": plan.reference.regions.map(t => t.label).join(", ") || "No highlighted regions"});
  image.addEventListener("load", () => drawOverlay(overlay, plan.reference.regions, image.naturalWidth, image.naturalHeight));
  image.src = source;
  surface.append(image, overlay); container.replaceChildren(surface, purpose);
}
