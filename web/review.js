import {drawOverlay, renderPlanReference} from "./intent.mjs";

const $ = id => document.getElementById(id);
export class IntentReview {
  constructor({request, accept, notify, started, cancelled}) {
    Object.assign(this, {request, accept, notify, started, cancelled});
    this.draft = null; this.busy = false;
    this.demo = document.documentElement.dataset.mode === "demo";
    $("check-interval").value = this.demo ? 1 : 60;
    if (this.demo) {
      $("check-interval").min = "0.1";
      $("check-interval").step = "0.1";
      $("check-interval").removeAttribute("max");
    }
    this.messageNodes = new Map();
    this.dialogPlan = null;
    for (const id of ["start-delay", "check-interval"]) $(id).addEventListener("input", () => this.controls());
    $("intent-refresh").addEventListener("click", () => this.draft
      ? this.perform("/api/intent/prepare", this.identity()) : this.perform("/api/check", {}));
    $("intent-cancel").addEventListener("click", async () => {
      if (!this.draft || await this.perform("/api/intent/cancel", this.identity())) {
        $("intent-feedback").value = ""; this.cancelled();
      }
    });
    $("review-composer").addEventListener("submit", event => { event.preventDefault(); this.send(); });
    $("plan-close").addEventListener("click", () => $("plan-dialog").close());
    $("plan-edit").addEventListener("click", () => {
      $("plan-dialog").close();
      $("intent-feedback").placeholder = "What would you like to change about the plan?";
      $("intent-feedback").focus();
    });
    $("intent-start").addEventListener("click", async () => {
      if ($("intent-start").disabled || !$("plan-dialog").open || this.dialogPlan !== this.planKey()) return;
      const timing = this.schedule();
      if (timing && await this.perform("/api/start", {...this.identity(), ...timing})) {
        $("plan-dialog").close(); this.started();
      }
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
    return this.demo ? {demo_fps: interval.valueAsNumber}
      : {start_delay_s: delay.valueAsNumber * 60, interval_s: interval.valueAsNumber};
  }
  render(state, connected = true, pending = false, hasSource = false) {
    this.connected = connected; this.operation = state.operation; this.pending = pending;
    this.hasSource = hasSource;
    const draft = state.intent && !["approved", "cancelled"].includes(state.intent.status) ? state.intent : null;
    if (draft?.id !== this.draft?.id) $("intent-feedback").value = "";
    this.draft = draft;
    const reference = draft?.preview ? draft : state.camera_check?.preview ? state.camera_check : state.intent;
    const preview = reference?.preview;
    const demo = state.source.mode === "video";
    const refreshing = ["check", "connect"].includes(state.operation) || draft?.status === "preparing";
    $("intent-error").textContent = !connected ? "Connection lost. Reconnecting…" : draft?.error || "";
    $("intent-image").hidden = !preview;
    $("intent-preview-empty").hidden = Boolean(preview);
    $("intent-preview-empty").textContent = state.operation === "connect" ? "Connecting iPhone…" : state.operation === "import" ? "Importing video…" : refreshing
      ? demo ? "Loading video preview…" : "Connecting to camera…" : this.demo ? "Import a video to begin the demo." : "Connect your iPhone.";
    if (preview && $("intent-image").getAttribute("src") !== preview) $("intent-image").src = preview;
    $("intent-reference-time").textContent = refreshing ? "Refreshing preview…" : reference?.reference_at
      ? `Reference captured ${new Date(reference.reference_at).toLocaleTimeString()}` : preview ? demo ? "Video reference · 00:00" : "Camera preview" : "";
    if (draft?.width && draft.height) {
      drawOverlay($("intent-overlay"), draft.highlights, draft.width, draft.height);
      $("intent-overlay").setAttribute("aria-label", `AI highlights: ${draft.highlights.map(t => t.label).join(", ")}`);
    }
    $("intent-overlay").toggleAttribute("hidden", !draft?.highlights.length);
    $("return-session").hidden = Boolean(draft) || !state.session_id;
    this.conversation(); this.controls();
  }
  controls() {
    const locked = !this.connected || this.busy || this.pending || Boolean(this.operation);
    const active = this.draft?.status === "ready" && !locked;
    const ready = Boolean(this.draft?.plans.length);
    const composer = $("intent-feedback");
    if (this.composerValue !== composer.value) {
      this.composerValue = composer.value;
      composer.style.height = "auto";
      composer.style.height = `${Math.min(composer.scrollHeight, 180)}px`;
    }
    const message = composer.value.trim();
    const retry = this.draft?.error && this.draft.pending_message_id;
    const timing = this.schedule();
    $("start-delay").disabled = locked;
    $("check-interval").disabled = locked;
    $("intent-start").textContent = timing?.start_delay_s ? "Confirm and schedule" : "Confirm and start";
    $("intent-start").disabled = !active || !this.hasSource || this.dialogPlan !== this.planKey() ||
      !ready || !timing;
    $("intent-refine").disabled = locked || !this.hasSource || (this.draft && !this.draft.preview) || !(message || retry);
    const sendLabel = !message && retry ? "Retry reply" : "Send message";
    $("intent-refine").setAttribute("aria-label", sendLabel);
    $("intent-refine").title = sendLabel;
    $("intent-feedback").disabled = locked;
    $("intent-refresh").disabled = locked || !this.hasSource || Boolean(message);
    $("intent-cancel").disabled = !this.connected || this.busy || this.pending || !(this.draft || message);
    const thinking = ["preparing", "refining"].includes(this.draft?.status);
    $("review-thinking").hidden = !thinking;
    this.canReviewPlan = ready && active && this.hasSource;
    for (const button of $("review-messages").querySelectorAll(".review-plan-button")) {
      const current = button.dataset.planKey === this.planKey();
      button.disabled = !this.canReviewPlan;
      button.textContent = current ? "Review shooting plan" : "View previous plan";
      button.closest(".plan-ready-notice").querySelector("p").textContent = current
        ? "Your shooting plan is ready, waiting for your approval."
        : "This is a previous proposal.";
    }
    const historical = this.dialogPlan !== this.planKey();
    $("plan-title").textContent = historical ? "Previous shooting plan" : "Your shooting plan";
    $("plan-close").hidden = !historical;
    $("plan-timing").hidden = historical;
    $("plan-timing-note").hidden = historical;
    $("plan-edit").hidden = historical;
    $("intent-start").hidden = historical;
    $("plan-edit").disabled = locked;
    if ($("plan-dialog").open && (!ready || !this.draft.plans.some(plan => plan.id === this.dialogPlan) || !this.connected || !this.hasSource)) {
      $("plan-dialog").close();
    }
  }
  planKey() { return this.draft?.plans.at(-1)?.id ?? null; }
  openPlan(id) {
    if (!this.canReviewPlan) return;
    const proposal = this.draft.plans.find(plan => plan.id === id);
    if (!proposal) return;
    this.dialogPlan = id;
    $("plan-instructions").textContent = proposal.plan.instructions;
    renderPlanReference($("plan-reference"), proposal.plan, proposal.preview);
    this.controls();
    if (!$("plan-dialog").open) $("plan-dialog").showModal();
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
      if (turn.role === "assistant" && turn.plan_id) {
        const notice = document.createElement("div"); notice.className = "plan-ready-notice";
        const description = document.createElement("p");
        description.textContent = "Your shooting plan is ready, waiting for your approval.";
        const button = document.createElement("button");
        button.type = "button"; button.className = "button secondary review-plan-button";
        button.dataset.planKey = turn.plan_id;
        button.textContent = "Review shooting plan";
        button.addEventListener("click", () => {
          this.openPlan(button.dataset.planKey);
        });
        notice.append(description, button); article.append(notice);
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
