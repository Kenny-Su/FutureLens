import {drawOverlay, validPlan} from "./intent.mjs";

const $ = id => document.getElementById(id);
export class IntentReview {
  constructor({request, accept, notify, started, cancelled}) {
    Object.assign(this, {request, accept, notify, started, cancelled});
    this.draft = null; this.busy = false;
    this.messageNodes = new Map();
    try {
      const saved = JSON.parse(localStorage.getItem("waitless.schedule"));
      if (saved) {
        $("start-delay").value = saved.start_delay_s / 60;
        $("check-interval").value = saved.interval_s;
        if (!this.schedule()) throw new Error("Invalid saved timing");
      }
    } catch {
      $("start-delay").value = 0; $("check-interval").value = 60;
    }
    for (const id of ["start-delay", "check-interval"]) $(id).addEventListener("input", () => {
      const timing = this.schedule();
      if (timing) { try { localStorage.setItem("waitless.schedule", JSON.stringify(timing)); } catch {} }
      this.controls();
    });
    $("intent-refresh").addEventListener("click", () => this.draft
      ? this.perform("/api/intent/prepare", this.identity()) : this.perform("/api/check", {}));
    $("intent-cancel").addEventListener("click", async () => {
      if (!this.draft || await this.perform("/api/intent/cancel", this.identity())) {
        $("intent-feedback").value = ""; this.cancelled();
      }
    });
    $("review-composer").addEventListener("submit", event => { event.preventDefault(); this.send(); });
    $("intent-start").addEventListener("click", async () => {
      if ($("intent-start").disabled) return;
      const timing = this.schedule();
      if (timing && await this.perform("/api/start", {...this.identity(), ...timing})) this.started();
    });
    $("intent-feedback").addEventListener("input", () => this.controls());
    $("intent-feedback").addEventListener("keydown", event => {
      if (event.key === "Enter" && !event.shiftKey && !event.isComposing) { event.preventDefault(); this.send(); }
    });
    window.addEventListener("beforeunload", event => {
      if ($("intent-feedback").value.trim()) { event.preventDefault(); event.returnValue = ""; }
    });
  }
  identity() { return {intent_id: this.draft.id, revision: this.draft.revision}; }
  schedule() {
    const delay = $("start-delay"), interval = $("check-interval");
    if (!delay.validity.valid || !interval.validity.valid) return null;
    return {start_delay_s: delay.valueAsNumber * 60, interval_s: interval.valueAsNumber};
  }
  render(state, connected = true, pending = false) {
    this.connected = connected; this.operation = state.operation; this.pending = pending;
    this.hasSource = Boolean(state.source.id);
    const draft = state.intent && !["approved", "cancelled"].includes(state.intent.status) ? state.intent : null;
    if (draft?.id !== this.draft?.id) $("intent-feedback").value = "";
    this.draft = draft;
    const reference = draft?.preview ? draft : state.camera_check?.preview ? state.camera_check : state.intent;
    const preview = reference?.preview;
    const demo = state.source.mode === "video";
    const refreshing = state.operation === "check" || draft?.status === "preparing";
    $("intent-error").textContent = !connected ? "Connection lost. Reconnecting…" : draft?.error || "";
    $("intent-image").hidden = !preview;
    $("intent-preview-empty").hidden = Boolean(preview);
    $("intent-preview-empty").textContent = state.operation === "import" ? "Importing video…" : refreshing
      ? demo ? "Loading video preview…" : "Connecting to camera…" : "Connect a camera or import a video.";
    if (preview && $("intent-image").getAttribute("src") !== preview) $("intent-image").src = preview;
    $("intent-reference-time").textContent = refreshing ? "Refreshing preview…" : reference?.reference_at
      ? `Reference captured ${new Date(reference.reference_at).toLocaleTimeString()}` : preview ? demo ? "Video reference · 00:00" : "Camera preview" : "";
    if (draft?.width && draft.height) {
      drawOverlay($("intent-overlay"), draft.targets, draft.width, draft.height);
      $("intent-overlay").setAttribute("aria-label", `AI highlights: ${draft.targets.map(t => t.label).join(", ")}`);
    }
    $("intent-overlay").toggleAttribute("hidden", !draft?.targets.length);
    $("return-session").hidden = Boolean(draft) || !state.session_id;
    this.conversation(); this.controls();
  }
  controls() {
    const locked = !this.connected || this.busy || this.pending || Boolean(this.operation);
    const active = this.draft?.status === "ready" && !locked;
    const message = $("intent-feedback").value.trim();
    const retry = this.draft?.error && this.draft.pending_message_id;
    const timing = this.schedule();
    $("start-delay").disabled = locked;
    $("check-interval").disabled = locked;
    $("intent-start").textContent = timing?.start_delay_s ? "Approve and schedule" : "Approve and start";
    $("intent-start").disabled = !active || this.draft.condition_stale ||
      Boolean(this.draft.question || this.draft.pending_message_id || message || validPlan(this.draft)) || !timing;
    $("intent-refine").disabled = locked || !this.hasSource || (this.draft && !this.draft.preview) || !(message || retry);
    $("intent-refine").textContent = !message && retry ? "Retry reply" : "Send ↑";
    $("intent-feedback").disabled = locked;
    $("intent-refresh").disabled = locked || !this.hasSource || Boolean(message);
    $("intent-cancel").disabled = !this.connected || this.busy || this.pending || !(this.draft || message);
    const thinking = ["preparing", "refining"].includes(this.draft?.status);
    $("review-thinking").hidden = !thinking;
  }
  conversation() {
    const log = $("review-messages"), turns = this.draft?.conversation || [];
    const follow = log.scrollHeight - log.scrollTop - log.clientHeight < 60;
    const ids = new Set(turns.map(turn => turn.id));
    for (const [id, node] of this.messageNodes) if (!ids.has(id)) { node.remove(); this.messageNodes.delete(id); }
    let added = false;
    for (const turn of turns) {
      if (this.messageNodes.has(turn.id)) continue;
      const article = document.createElement("article"); article.className = `review-message ${turn.role}`;
      const author = document.createElement("span"); author.className = "message-author";
      author.textContent = turn.role === "user" ? "You" : "WaitLess";
      const body = document.createElement("p"); body.className = "message-text"; body.textContent = turn.text;
      article.append(author, body);
      if (turn.question) {
        const question = document.createElement("p"); question.className = "message-question";
        question.textContent = turn.question; article.append(question);
      }
      log.append(article); this.messageNodes.set(turn.id, article); added = true;
    }
    if (added && (follow || this.sentMessage)) log.scrollTop = log.scrollHeight;
    this.sentMessage = false;
  }
  async send() {
    if ($("intent-refine").disabled) return;
    const input = $("intent-feedback"), feedback = input.value;
    this.sentMessage = true;
    const path = this.draft ? "/api/intent/refine" : "/api/intent/prepare";
    const data = this.draft ? {...this.identity(), feedback} : {request: feedback};
    if (await this.perform(path, data)) {
      if (input.value === feedback) input.value = "";
      this.controls();
    }
  }
  async perform(path, data) {
    this.busy = true; this.controls();
    try {
      this.accept(await this.request(path, data)); return true;
    } catch (error) { this.notify(error.message); return false; }
    finally { this.busy = false; this.controls(); }
  }
}
