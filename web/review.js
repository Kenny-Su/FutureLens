import {drawOverlay, validPlan} from "./intent.mjs";

const $ = id => document.getElementById(id);
export class IntentReview {
  constructor({request, accept, notify, started, cancelled}) {
    Object.assign(this, {request, accept, notify, started, cancelled});
    this.draft = null; this.busy = false;
    this.messageNodes = new Map();
    $("intent-refresh").addEventListener("click", () => this.perform("/api/intent/prepare", this.identity()));
    $("intent-cancel").addEventListener("click", async () => {
      $("instruction").value = this.draft.request;
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
  render(state, connected = true) {
    this.connected = connected; this.operation = state.operation;
    const draft = state.intent;
    if (!draft || ["approved", "cancelled"].includes(draft.status)) { this.draft = null; return; }
    if (draft.id !== this.draft?.id) $("intent-feedback").value = "";
    this.draft = draft;
    $("intent-error").textContent = !connected ? "Connection lost. Your conversation is saved." : draft.error || "";
    $("intent-image-wrap").hidden = !draft.preview;
    if (draft.preview && $("intent-image").getAttribute("src") !== draft.preview) $("intent-image").src = draft.preview;
    $("intent-reference-time").textContent = draft.reference_at ? `Reference captured ${new Date(draft.reference_at).toLocaleTimeString()}` : "Connecting for a reference preview…";
    if (draft.width && draft.height) {
      drawOverlay($("intent-overlay"), draft.targets, draft.width, draft.height);
      $("intent-overlay").setAttribute("aria-label", `AI highlights: ${draft.targets.map(t => t.label).join(", ")}`);
    }
    $("intent-overlay").toggleAttribute("hidden", !draft.targets.length);
    this.conversation(); this.controls();
  }
  controls() {
    if (!this.draft) return;
    const locked = !this.connected || this.busy || Boolean(this.operation);
    const active = this.draft.status === "ready" && !locked;
    const message = $("intent-feedback").value.trim();
    const retry = this.draft.error && this.draft.pending_message_id;
    const outdated = this.draft.condition_stale;
    $("intent-start").disabled = !active || outdated ||
      Boolean(this.draft.question || this.draft.pending_message_id || message || validPlan(this.draft));
    $("intent-refine").disabled = locked || !this.draft.preview || !(message || retry);
    $("intent-refine").textContent = !message && retry ? "Retry reply" : "Send ↑";
    $("intent-feedback").disabled = locked;
    $("intent-refresh").disabled = locked || Boolean(message);
    $("intent-cancel").disabled = !this.connected || this.busy;
    const thinking = ["preparing", "refining"].includes(this.draft.status);
    $("review-thinking").hidden = !thinking;
    $("review-thinking").textContent = this.draft.status === "preparing" ? "Looking at your reference…" : "Considering your reply…";
  }
  conversation() {
    const log = $("review-messages"), turns = this.draft.conversation;
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
    if (await this.perform("/api/intent/refine", {...this.identity(), feedback})) {
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
