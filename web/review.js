import {drawOverlay, imagePoint, moveBox, resizeBox, validPlan, targetColor} from "./intent.mjs";

const $ = id => document.getElementById(id);
const clone = value => structuredClone(value);
export class IntentReview {
  constructor({request, accept, notify, started, cancelled}) {
    Object.assign(this, {request, accept, notify, started, cancelled});
    this.draft = null; this.plan = null; this.revision = 0;
    this.dirty = false; this.saving = false; this.busy = false; this.version = 0;
    this.selected = null; this.drawing = false; this.drag = null;
    $("intent-condition").addEventListener("input", () => { this.plan.condition = $("intent-condition").value; this.changed(); });
    $("add-target").addEventListener("click", () => { this.drawing = "new"; this.controls(); $("intent-overlay").focus({preventScroll: true}); });
    $("whole-frame").addEventListener("click", () => this.add([0, 0, 1, 1], "fixed", "Whole scene"));
    $("intent-refresh").addEventListener("click", () => this.perform("/api/intent/prepare", {request: this.draft.request}));
    $("intent-cancel").addEventListener("click", async () => {
      $("instruction").value = this.draft.request;
      if (await this.perform("/api/intent/cancel", this.identity())) this.cancelled();
    });
    $("intent-refine").addEventListener("click", async () => {
      if (this.dirty && !await this.save()) return;
      await this.perform("/api/intent/refine", {...this.identity(), feedback: $("intent-feedback").value});
    });
    $("intent-start").addEventListener("click", async () => {
      if (this.dirty && !await this.save()) return;
      if (await this.perform("/api/start", this.identity())) this.started();
    });
    $("intent-reload").addEventListener("click", async () => {
      try { const state = await this.request("/api/state"); this.dirty = false; this.revision = 0; this.accept(state); }
      catch (error) { this.notify(error.message); }
    });
    $("intent-feedback").addEventListener("input", () => this.controls());
    const svg = $("intent-overlay");
    svg.addEventListener("pointerdown", event => this.pointerDown(event));
    svg.addEventListener("pointermove", event => this.pointerMove(event));
    svg.addEventListener("pointerup", event => this.pointerUp(event));
    svg.addEventListener("pointercancel", () => this.abortDrag());
    svg.addEventListener("focusin", event => {
      const id = event.target.closest("[data-target]")?.dataset.target;
      if (id && id !== this.selected) { this.selected = id; this.overlay(); }
    });
    svg.addEventListener("keydown", event => this.key(event));
    window.addEventListener("beforeunload", event => { if (this.dirty || this.saving) { event.preventDefault(); event.returnValue = ""; } });
  }
  identity() { return {intent_id: this.draft.id, revision: this.revision}; }
  active() { return this.draft?.status === "ready" && !this.busy && !this.operation && this.connected; }
  render(state, connected = true) {
    this.connected = connected; this.operation = state.operation;
    const draft = state.intent;
    if (!draft || ["approved", "cancelled"].includes(draft.status)) {
      clearTimeout(this.timer); this.dirty = false; this.drag = null; return;
    }
    const replace = draft.id !== this.draft?.id || (!this.dirty && !this.saving && draft.revision !== this.revision);
    this.draft = draft;
    if (replace) {
      this.plan = {condition: draft.condition, targets: clone(draft.targets)};
      this.revision = draft.revision; this.dirty = false; this.selected = this.plan.targets[0]?.id;
      this.drawing = false; this.drag = null; this.saveError = "";
      $("intent-condition").value = this.plan.condition;
      this.list();
    }
    $("review-request").textContent = draft.request;
    $("intent-error").textContent = draft.error || this.saveError || "";
    $("intent-image-wrap").hidden = !draft.preview;
    if (draft.preview && $("intent-image").getAttribute("src") !== draft.preview) $("intent-image").src = draft.preview;
    $("intent-reference-time").textContent = draft.reference_at ? `Reference captured ${new Date(draft.reference_at).toLocaleTimeString()}` : "Connecting for a reference preview…";
    this.overlay(); this.controls();
  }
  controls() {
    if (!this.draft || !this.plan) return;
    const active = this.active();
    const problem = validPlan(this.plan);
    $("review-fields").disabled = !active;
    $("intent-start").disabled = !active || this.saving || this.dirty || Boolean(this.drag || this.drawing || problem);
    $("intent-refine").disabled = !this.connected || this.busy || this.saving || Boolean(this.operation) ||
      !this.draft.preview || !$("intent-feedback").value.trim() || (this.dirty && Boolean(problem));
    $("intent-refresh").disabled = !this.connected || this.busy || this.saving || Boolean(this.operation);
    $("intent-cancel").disabled = !this.connected || this.busy || this.saving;
    $("intent-reload").hidden = !this.saveError;
    const phases = {preparing: "Reading the scene…", refining: "Updating interpretation…", failed: "Interpretation unavailable"};
    $("intent-save-status").textContent = !this.connected ? "Connection lost · edits kept here" : phases[this.draft.status] ||
      (this.saving ? "Saving corrections…" : this.dirty ? problem || "Unsaved corrections" : "All corrections saved");
    $("intent-editor-help").textContent = this.drawing ? "Drag on the image to draw a box. Escape cancels." :
      "Drag a box or its corners. Arrow keys move; Shift + arrows resize; Delete removes. Object locations update each observation. Fixed areas stay in place.";
    $("intent-overlay").classList.toggle("drawing", Boolean(this.drawing));
    $("intent-overlay").classList.toggle("locked", !active);
  }
  async perform(path, data) {
    clearTimeout(this.timer); this.busy = true; this.controls();
    try {
      const state = await this.request(path, data);
      if (path !== "/api/intent/update") this.dirty = false;
      this.accept(state); return true;
    } catch (error) { this.notify(error.message); return false; }
    finally { this.busy = false; this.controls(); }
  }
  changed() {
    this.dirty = true; this.version++; this.saveError = "";
    this.overlay(); this.controls();
    clearTimeout(this.timer);
    this.timer = setTimeout(() => this.save(), 500);
  }
  async save() {
    clearTimeout(this.timer);
    if (this.saving || !this.active() || validPlan(this.plan)) return false;
    if (!this.dirty) return true;
    const version = this.version;
    this.saving = true; this.controls();
    try {
      const state = await this.request("/api/intent/update", {...this.identity(), plan: clone(this.plan)});
      this.revision = state.intent.revision; this.draft = state.intent;
      this.dirty = this.version !== version; this.saving = false;
      this.accept(state);
      if (this.dirty) this.timer = setTimeout(() => this.save(), 100);
      return !this.dirty;
    } catch (error) {
      this.saveError = error.message;
      $("intent-error").textContent = error.message;
      return false;
    } finally { this.saving = false; this.controls(); }
  }
  list() {
    const container = $("intent-targets"); container.replaceChildren();
    for (const target of this.plan.targets) {
      const card = document.createElement("div"); card.className = "intent-target"; card.dataset.target = target.id;
      const select = document.createElement("button"); select.type = "button"; select.className = "target-select";
      select.textContent = target.label; select.style.color = targetColor(target.id);
      select.addEventListener("click", () => { this.selected = target.id; this.overlay(); this.highlightList(); });
      card.append(select);
      const field = (label, tag, value, update) => {
        const wrapper = document.createElement("label"); wrapper.textContent = label;
        const input = document.createElement(tag); input.value = value;
        input.addEventListener("input", () => { update(input.value); this.changed(); });
        wrapper.append(input); card.append(wrapper); return input;
      };
      field("Label", "input", target.label, value => { target.label = value; select.textContent = value; }).maxLength = 120;
      const mode = field("Highlight behavior", "select", target.mode, value => { target.mode = value; this.list(); });
      for (const [value, label] of [["object", "Object · follow subject"], ["fixed", "Fixed area · stay in frame"]]) {
        const option = document.createElement("option"); option.value = value; option.textContent = label; mode.append(option);
      }
      mode.value = target.mode;
      field("Why it matters", "textarea", target.relevance, value => { target.relevance = value; }).maxLength = 500;
      const buttons = document.createElement("div"); buttons.className = "target-actions";
      for (const [label, action] of [[target.box ? "Redraw box" : "Draw location", () => { this.selected = target.id; this.drawing = target.id; this.controls(); $("intent-overlay").focus({preventScroll: true}); }],
        ...(target.mode === "object" ? [["Not visible", () => { target.box = null; this.changed(); this.list(); }]] : []),
        ["Remove", () => this.remove(target.id)]]) {
        const button = document.createElement("button"); button.type = "button"; button.className = "text-button";
        button.textContent = label; button.addEventListener("click", action); buttons.append(button);
      }
      card.append(buttons);
      if (!target.box) { const note = document.createElement("p"); note.className = "muted small"; note.textContent = "No location marked in this reference"; card.append(note); }
      container.append(card);
    }
    this.highlightList();
  }
  highlightList() {
    for (const card of $("intent-targets").children) {
      card.classList.toggle("selected", card.dataset.target === this.selected);
      card.querySelector("button").setAttribute("aria-pressed", String(card.dataset.target === this.selected));
    }
  }
  overlay() {
    if (!this.draft?.width || !this.plan) return;
    drawOverlay($("intent-overlay"), this.plan.targets, this.draft.width, this.draft.height,
      {selected: this.selected, editable: this.active()});
    this.highlightList();
  }
  add(box, mode = "object", label = "New target") {
    if (this.plan.targets.length >= 24) { this.notify("Use up to 24 targets."); return; }
    const target = {id: `target-${crypto.randomUUID()}`, label, relevance: "", mode, box};
    this.plan.targets.push(target); this.selected = target.id; this.drawing = false;
    this.changed(); this.list();
    $("intent-targets").lastElementChild.querySelector("input").focus();
  }
  remove(id) { this.plan.targets = this.plan.targets.filter(t => t.id !== id); this.selected = this.plan.targets[0]?.id; this.changed(); this.list(); }
  point(event, clamp = true) {
    return imagePoint(event.clientX, event.clientY, $("intent-overlay").getBoundingClientRect(),
      {width: this.draft.width, height: this.draft.height}, clamp);
  }
  pointerDown(event) {
    if (!this.active() || event.button !== 0) return;
    const point = this.point(event, false); if (!point) return;
    const group = event.target.closest("[data-target]");
    if (this.drawing) this.drag = {start: point, drawing: this.drawing};
    else if (group) {
      const target = this.plan.targets.find(t => t.id === group.dataset.target);
      this.selected = target.id;
      this.drag = {start: point, id: target.id, box: [...target.box], corner: event.target.dataset.corner};
    } else return;
    event.preventDefault(); clearTimeout(this.timer);
    $("intent-overlay").setPointerCapture(event.pointerId);
    this.overlay(); this.controls();
  }
  pointerMove(event) {
    if (!this.drag) return;
    const point = this.point(event), drag = this.drag;
    if (drag.drawing) {
      drag.box = [Math.min(drag.start[0], point[0]), Math.min(drag.start[1], point[1]), Math.max(drag.start[0], point[0]), Math.max(drag.start[1], point[1])];
      const temporary = {id: "drawing", label: "New region", box: drag.box};
      drawOverlay($("intent-overlay"), [...this.plan.targets, temporary], this.draft.width, this.draft.height);
    } else {
      const target = this.plan.targets.find(t => t.id === drag.id);
      target.box = drag.corner ? resizeBox(drag.box, drag.corner, point) : moveBox(drag.box, point[0] - drag.start[0], point[1] - drag.start[1]);
      this.overlay();
    }
  }
  pointerUp(event) {
    if (!this.drag) return;
    const drag = this.drag; this.drag = null;
    $("intent-overlay").releasePointerCapture(event.pointerId);
    if (drag.drawing && drag.box && drag.box[2] - drag.box[0] >= .002 && drag.box[3] - drag.box[1] >= .002) {
      if (drag.drawing === "new") this.add(drag.box);
      else { this.plan.targets.find(t => t.id === drag.drawing).box = drag.box; this.drawing = false; this.changed(); this.list(); }
    } else if (!drag.drawing) this.changed();
    this.overlay(); this.controls();
    if (this.selected) $("intent-overlay").querySelector(`[data-target="${this.selected}"]`)?.focus();
  }
  abortDrag() {
    if (this.drag?.id) this.plan.targets.find(t => t.id === this.drag.id).box = this.drag.box;
    this.drag = null; this.drawing = false; this.overlay(); this.controls();
  }
  key(event) {
    if (!this.active()) return;
    if (event.key === "Escape") { this.abortDrag(); return; }
    const id = event.target.closest("[data-target]")?.dataset.target || this.selected;
    const target = this.plan.targets.find(t => t.id === id); if (!target) return;
    if (event.key === "Delete" || event.key === "Backspace") { event.preventDefault(); this.remove(id); return; }
    if (event.key === "Enter" || event.key === " ") { event.preventDefault(); this.selected = id; this.overlay(); return; }
    const delta = {ArrowLeft: [-.005, 0], ArrowRight: [.005, 0], ArrowUp: [0, -.005], ArrowDown: [0, .005]}[event.key];
    if (!delta || !target.box) return;
    event.preventDefault(); this.selected = id;
    target.box = event.shiftKey ? resizeBox(target.box, "se", [target.box[2] + delta[0], target.box[3] + delta[1]]) : moveBox(target.box, ...delta);
    this.changed(); $("intent-overlay").querySelector(`[data-target="${id}"]`)?.focus();
  }
}
