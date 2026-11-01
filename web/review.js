import {drawOverlay, validPlan} from "./intent.mjs";

const $ = id => document.getElementById(id);
export class IntentReview {
  constructor({request, accept, notify, started, cancelled}) {
    Object.assign(this, {request, accept, notify, started, cancelled});
    this.draft = null; this.busy = false;
    this.messageNodes = new Map();
    $("intent-refresh").addEventListener("click", () => this.draft
      ? this.perform("/api/intent/prepare", this.identity()) : this.perform("/api/check", {}));
    $("intent-cancel").addEventListener("click", async () => {
      if (await this.perform("/api/intent/cancel", this.identity())) {
        $("intent-feedback").value = ""; this.cancelled();
      }
    });
    $("review-composer").addEventListener("submit", event => { event.preventDefault(); this.send(); });
    $("intent-start").addEventListener("click", async () => {
      if (await this.perform("/api/start", this.identity())) this.started();
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
  render(state, connected = true, pending = false) {
    this.connected = connected; this.operation = state.operation; this.pending = pending;
    const draft = state.intent && !["approved", "cancelled"].includes(state.intent.status) ? state.intent : null;
    if (draft?.id !== this.draft?.id) $("intent-feedback").value = "";
    this.draft = draft;
    const reference = draft?.preview ? draft : state.camera_check?.preview ? state.camera_check : state.intent;
    const preview = reference?.preview;
    const refreshing = state.operation === "check" || draft?.status === "preparing";
    $("intent-error").textContent = !connected ? "Connection lost. Reconnecting…" : draft?.error || "";
    $("intent-image").hidden = !preview;
    $("intent-preview-empty").hidden = Boolean(preview);
    $("intent-preview-empty").textContent = refreshing ? "Connecting to camera…" : "Connect your camera and refresh the preview.";
    if (preview && $("intent-image").getAttribute("src") !== preview) $("intent-image").src = preview;
    $("intent-reference-time").textContent = refreshing ? "Refreshing preview…" : reference?.reference_at
      ? `Reference captured ${new Date(reference.reference_at).toLocaleTimeString()}` : preview ? "Camera preview" : "";
    if (draft?.width && draft.height) {
      drawOverlay($("intent-overlay"), draft.targets, draft.width, draft.height);
      $("intent-overlay").setAttribute("aria-label", `AI highlights: ${draft.targets.map(t => t.label).join(", ")}`);
    }
    $("intent-overlay").toggleAttribute("hidden", !draft?.targets.length);
    $("intent-cancel").hidden = !draft;
    $("return-session").hidden = Boolean(draft) || !state.session_id;
    $("review-footer").hidden = !draft;
    $("intent-feedback").placeholder = draft ? "Correct a detail, answer a question, or ask “what if…”" : "Describe the moment you want to capture…";
    this.conversation(); this.controls();
  }
  controls() {
    const locked = !this.connected || this.busy || this.pending || Boolean(this.operation);
    const active = this.draft?.status === "ready" && !locked;
    const message = $("intent-feedback").value.trim();
    const retry = this.draft?.error && this.draft.pending_message_id;
    $("intent-start").disabled = !active || this.draft.condition_stale ||
      Boolean(this.draft.question || this.draft.pending_message_id || message || validPlan(this.draft));
    $("intent-refine").disabled = locked || (this.draft && !this.draft.preview) || !(message || retry);
    $("intent-refine").textContent = !message && retry ? "Retry reply" : "Send ↑";
    $("intent-feedback").disabled = this.draft ? locked : !this.connected || this.busy;
    $("intent-refresh").disabled = locked || Boolean(this.draft && message);
    $("intent-cancel").disabled = !this.connected || this.busy || this.pending;
    const thinking = ["preparing", "refining"].includes(this.draft?.status);
    $("review-thinking").hidden = !thinking;
    $("review-thinking").textContent = this.draft?.status === "preparing" ? "Looking at your reference…" : "Considering your reply…";
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
