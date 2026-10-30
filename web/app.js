import { sessionActivity } from "./activity.mjs";

const $ = (id) => document.getElementById(id);
const phases = { connecting: "Connecting to camera…", observing: "Saving a fresh preview…", checking: "Checking request capacity…", thinking: "Evaluating the scene…", retrieving: "Looking up visual history…", capturing: "Taking and downloading a photo…", scheduling: "Updating preview interval…", compacting: "Organizing session context…", waiting: "Waiting for the next observation" };
const reasons = { duration: "Monitoring duration reached.", max_shots: "Photo limit reached.", max_requests: "Maximum AI calls reached.", capacity_reached: "Request capacity reached. All previews and run transcripts are preserved.", user_stop: "You stopped this session. All saved previews and photos are preserved." };
const captureLabels = { downloaded: "Photo saved", requested: "Preparing capture", shutter_sent: "Downloading photo", failed_after_shutter: "Download unconfirmed", failed_or_unconfirmed: "Capture unconfirmed", skipped_stale_frame: "Preview expired", skipped_duration_expired: "Session expired", skipped_expired_before_shutter: "Capture expired", skipped_cooldown: "Capture cooldown", skipped_photo_limit: "Photo limit reached", skipped_user_stop: "Session stopped" };
const outcomes = { no_photo: "No photo", preview_interval_set: "Preview interval set", run_limit: "Run limit reached", session_budget: "Maximum AI calls reached", stale_preview: "Preview expired", duration: "Session expired", capacity_reached: "Capacity reached", user_stop: "Session stopped", error: "Run failed", ...captureLabels };
const toolLabels = { list_history: "List history", get_history: "Retrieve images", shoot: "Shoot", set_preview_interval: "Change preview interval" };
let state, connected = false, pending = false, showingMonitor = false, sessionId = null, initialized = false;
let commandGeneration = 0, following = true, viewerFrameId = null;
let checkedOnLoad = false, connectionProblemReported = false, lastCheckError = null, lastSessionError = null;
const rows = new Map();
const previews = new Map();

function text(id, value) { $(id).textContent = value; }
function show(id, visible) { $(id).hidden = !visible; }
function clock(seconds) {
  seconds = Math.max(0, Math.floor(seconds || 0));
  const hours = Math.floor(seconds / 3600);
  const minutes = Math.floor(seconds / 60) % 60;
  return (hours ? `${hours}:` : "") + `${String(minutes).padStart(2, "0")}:${String(seconds % 60).padStart(2, "0")}`;
}
function media(path) { return `/media/${encodeURIComponent(state.session_id)}/${path.split("/").map(encodeURIComponent).join("/")}`; }
function notice(message) {
  const dialog = $("error-dialog");
  if (!message) { if (dialog.open) dialog.close(); return; }
  text("error-message", message);
  if (!dialog.open) dialog.showModal();
}

function cameraCheckError(check) {
  if (!check) return null;
  if (check.error) return check.error;
  if (!check.capabilities?.capture_ready) {
    return check.capabilities?.capture_issues?.join(" ") || "The camera is connected but cannot take photographs. Check its shooting settings.";
  }
  return null;
}

function sessionError(next) {
  const report = next.report || {};
  return next.error || (report.status === "failed" ? report.camera_error || `Session stopped (${report.error_type || "error"}). Check the saved logs.`
    : report.status === "capacity_reached" ? reasons.capacity_reached : null);
}

function loadPreview(image, url) {
  if (image.dataset.source === url) return;
  image.dataset.source = url;
  let retries = 0;
  image.onerror = () => {
    if (retries++ < 2) setTimeout(() => {
      if (image.isConnected && image.dataset.source === url) image.src = `${url}?retry=${retries}`;
    }, 800 * retries);
  };
  image.src = url;
}

async function api(path, data) {
  const response = await fetch(path, data === undefined ? { cache: "no-store" } : {
    method: "POST", headers: { "Content-Type": "application/json", "X-WaitLess-Token": state.token }, body: JSON.stringify(data),
  });
  const result = await response.json();
  if (!response.ok) throw new Error(result.error || "The request could not be completed.");
  return result;
}

async function command(path, data = {}) {
  if (pending || !connected) return false;
  pending = true;
  commandGeneration++;
  if (path === "/api/check") lastCheckError = null;
  notice("");
  render();
  try {
    accept(await api(path, data));
    return true;
  } catch (error) {
    notice(error.message);
    return false;
  } finally {
    pending = false;
    render();
  }
}

function accept(next) {
  state = next;
  connected = true;
  if (sessionId !== state.session_id) {
    sessionId = state.session_id;
    following = true;
    viewerFrameId = null;
    $("image-viewer").close();
    rows.clear();
    previews.clear();
    $("timeline").replaceChildren();
    showingMonitor = Boolean(sessionId);
  } else if (!initialized) {
    showingMonitor = Boolean(sessionId);
  }
  initialized = true;
  render();
  const checkError = cameraCheckError(state.camera_check);
  if (checkError && checkError !== lastCheckError) notice(checkError);
  lastCheckError = checkError;
  const error = sessionError(state);
  const key = error ? `${sessionId}:${error}` : null;
  if (key && key !== lastSessionError) notice(error);
  lastSessionError = key;
}

function renderSetup() {
  const report = state.report || {};
  const monitoring = state.operation === "monitor";
  const checking = state.operation === "check" || (monitoring && (!report.phase || report.phase === "connecting"));
  const check = state.camera_check;
  const error = check ? cameraCheckError(check) : report.camera_error;
  const ready = !error && (check ? check.capabilities?.capture_ready : report.status !== "failed" && report.camera_capabilities?.capture_ready);
  const healthy = connected && !checking && ready;
  text("camera-status", !connected ? "Connection lost" : checking ? "Checking camera…" : error ? "Check camera" : ready ? "Camera connected" : "Check camera");
  $("camera-dot").className = `dot ${healthy ? "ready" : error || !connected ? "error" : ""}`;
  $("check-camera").disabled = pending || !connected || Boolean(state.operation);
  $("check-camera").title = healthy ? "Last connection check succeeded. Click to check again." : "Check camera connection";
  // Configuration is validated by the server; failures are shown in the dialog.
  $("start").disabled = pending || !connected || Boolean(state.operation);
  show("return-session", Boolean(state.session_id));
}

function element(tag, className, value) {
  const node = document.createElement(tag);
  if (className) node.className = className;
  if (value !== undefined) node.textContent = value;
  return node;
}
function captured(entry) { return entry.capture_status === "downloaded" && entry.saved_files?.length > 0; }
function entries() { return state.report?.observations || []; }
function browsing() { following = false; show("go-latest", true); }
function focusObservation(id) {
  const row = previews.get(id);
  if (!row) return;
  browsing();
  for (const preview of previews.values()) preview.classList.toggle("focused", preview === row);
  const timeline = $("timeline");
  timeline.scrollTop += row.getBoundingClientRect().top - timeline.getBoundingClientRect().top - 8;
  row.querySelector(".preview-button").focus({ preventScroll: true });
}
function downloads(container, entry) {
  const key = captured(entry) ? entry.saved_files.join("|") : "";
  if (container.dataset.key === key) return;
  container.dataset.key = key;
  container.replaceChildren();
  if (captured(entry)) for (const path of entry.saved_files) {
    const link = element("a", "", `${path.split("/").at(-1)} ↓`);
    link.href = media(path);
    link.download = path.split("/").at(-1);
    container.append(link);
  }
}
function openImage(id) {
  const entry = entries().find((item) => item.frame_id === id);
  if (!entry) return;
  if (id !== entries().at(-1)?.frame_id) browsing();
  viewerFrameId = id;
  text("image-title", `Observation ${String(id + 1).padStart(2, "0")} · ${clock(entry.timestamp_s)}`);
  loadPreview($("viewer-image"), media(entry.preview_file));
  $("viewer-image").alt = `Observation ${id + 1} at ${clock(entry.timestamp_s)} into the session`;
  downloads($("viewer-downloads"), entry);
  if (!$("image-viewer").open) $("image-viewer").showModal();
}
function activityHeading(surface, label, item) {
  const heading = element("div", "activity-heading");
  const timestamp = element("time", "activity-time", clock(item.elapsed));
  if (item.timestamp) {
    timestamp.dateTime = item.timestamp;
    timestamp.title = new Date(item.timestamp).toLocaleTimeString();
  }
  heading.append(element("h3", "activity-label", label), timestamp);
  surface.append(heading);
}
function renderPreview(row, item) {
  const entry = item.entry;
  if (!row.firstChild) {
    const surface = element("div", "preview-card");
    activityHeading(surface, `Preview ${String(entry.frame_id + 1).padStart(2, "0")}`, item);
    const button = element("button", "preview-button");
    button.type = "button";
    button.setAttribute("aria-label", `Enlarge observation ${entry.frame_id + 1}`);
    const image = element("img");
    image.alt = `Preview ${entry.frame_id + 1}`;
    image.loading = "lazy";
    loadPreview(image, media(entry.preview_file));
    button.append(image, element("span", "saved-marker", "✓ Photo saved"));
    button.addEventListener("click", () => openImage(entry.frame_id));
    surface.append(button);
    row.append(surface);
  }
  row.classList.toggle("captured", captured(entry));
  row.querySelector(".saved-marker").hidden = !captured(entry);
}
function renderAction(row, item) {
  if (row.dataset.revision === item.revision) return;
  const event = item.event;
  row.replaceChildren();
  row.dataset.revision = item.revision;
  row.dataset.status = event.status;
  let label, detail = "", saved = false;
  if (item.kind === "model") {
    label = item.noPhoto ? "Keep watching" : event.status === "completed" ? "Scene evaluated" : "Evaluate scene";
    detail = event.status === "checking" ? "Preparing to evaluate the latest preview"
      : event.status === "started" ? "Evaluating the scene…"
      : event.status === "completed" ? (item.noPhoto ? "No photograph requested · " : "") + `${event.latency_s.toFixed(1)} sec`
      : event.status === "failed" ? "Evaluation failed · session ended" : outcomes[event.reason] || "Evaluation skipped";
  } else if (item.kind === "compaction") {
    label = "Organize session context";
    detail = event.status === "checking" ? "Preparing earlier observations for a summary"
      : event.status === "started" ? "Preparing summary before the next preview"
      : event.status === "completed" ? `Context updated · ${event.latency_s.toFixed(1)} sec`
      : event.status === "failed" ? "Summary unavailable · keeping history and continuing observations" : "Skipped";
  } else if (item.kind === "tool" && event.name !== "shoot") {
    const unchanged = event.name === "set_preview_interval" && event.changed === false;
    label = unchanged ? "Preview interval unchanged" : toolLabels[event.name] || "Tool";
    detail = event.error ? `Tool error · ${event.error}` : event.status === "started" ? "In progress"
      : event.status === "failed" ? "Tool failed" : event.name === "list_history" ? `${event.count} preview${event.count === 1 ? "" : "s"}${event.next_cursor !== null ? " · more available" : ""}`
      : event.name === "get_history" ? `${event.frame_ids.length} image${event.frame_ids.length === 1 ? "" : "s"}`
      : event.name === "set_preview_interval" ? (unchanged ? `Keeping ${event.seconds} sec · monitoring continues` : `${event.previous_seconds} → ${event.seconds} sec`) : "Finished";
  } else if (item.kind === "capture" || event.name === "shoot") {
    const capture = item.capture || event;
    const status = capture.capture_status || event.capture_status;
    label = captureLabels[status] || "Take photograph";
    saved = status === "downloaded";
    detail = saved ? `${(event.saved_files || capture.saved_files || []).length} original file${(event.saved_files || capture.saved_files || []).length === 1 ? "" : "s"} downloaded`
      : status === "shutter_sent" ? "Shutter sent · awaiting download"
      : status?.startsWith("failed") || event.status === "failed" ? "Capture unconfirmed · no automatic retry"
      : status?.startsWith("skipped") ? "No shutter command was sent" : "Preparing the camera";
    row.dataset.status = saved ? "completed" : capture.status || event.status;
  } else {
    label = outcomes[event.outcome] || "Observation ended";
    detail = event.outcome === "run_limit" ? "Evaluation limit reached · continuing with the next preview"
      : event.outcome === "stale_preview" ? "Waiting for a fresh preview" : "";
  }
  row.classList.toggle("success", saved);
  const surface = element("div", "action-card");
  activityHeading(surface, label, item);
  if (detail) surface.append(element("p", "activity-detail", detail));
  if (saved) {
    const links = element("div", "download-links");
    downloads(links, item.entry);
    surface.append(links);
  }
  if (item.kind === "tool" && event.frame_ids) {
    const thumbnails = element("div", "history-images");
    for (const id of event.frame_ids) {
      const source = entries().find(entry => entry.frame_id === id);
      if (!source) continue;
      const imageItem = element("div", "history-image");
      const button = element("button");
      button.type = "button";
      button.setAttribute("aria-label", `Enlarge retrieved observation ${id + 1}`);
      const image = element("img");
      image.alt = `Retrieved observation ${id + 1}`;
      image.loading = "lazy";
      loadPreview(image, media(source.preview_file));
      button.append(image);
      button.addEventListener("click", () => openImage(id));
      const link = element("button", "history-link", `Preview ${String(id + 1).padStart(2, "0")} · ${clock(source.timestamp_s)} ↗`);
      link.type = "button";
      link.setAttribute("aria-label", `Go to observation ${id + 1}`);
      link.addEventListener("click", () => focusObservation(id));
      imageItem.append(button, link);
      thumbnails.append(imageItem);
    }
    surface.append(thumbnails);
  }
  row.append(surface);
}
function followLatest() {
  $("timeline").scrollTop = $("timeline").scrollHeight;
}
function renderTimeline(observations) {
  const timeline = $("timeline");
  const previousTop = timeline.scrollTop;
  let previousRow = null;
  for (const item of sessionActivity(observations)) {
    let row = rows.get(item.id);
    if (!row) {
      row = element("article", `thread-item kind-${item.kind}`);
      row.dataset.activityId = item.id;
      row.dataset.observationId = item.entry.frame_id;
      if (item.kind === "preview") {
        row.id = `observation-${item.entry.frame_id}`;
        previews.set(item.entry.frame_id, row);
      }
      rows.set(item.id, row);
    }
    const next = previousRow ? previousRow.nextElementSibling : timeline.firstElementChild;
    if (next !== row) timeline.insertBefore(row, next);
    previousRow = row;
    if (item.kind === "preview") renderPreview(row, item);
    else renderAction(row, item);
  }
  if (following) followLatest();
  else timeline.scrollTop = previousTop;
  show("go-latest", !following);
  show("timeline-empty", !observations.length);
  text("timeline-count", `${observations.length} preview${observations.length === 1 ? "" : "s"} · one continuous session`);
  if ($("image-viewer").open) {
    const entry = observations.find(item => item.frame_id === viewerFrameId);
    if (entry) downloads($("viewer-downloads"), entry);
  }
}

function renderMonitor() {
  const report = state.report || {};
  const entries = report.observations || [];
  const active = state.operation === "monitor";
  const failed = Boolean(sessionError(state));
  const stopping = state.stopping;
  text("monitor-title", active ? "Monitoring" : failed ? "Session ended" : report.status === "stopped" ? "Session stopped" : "Session complete");
  text("status-text", !connected ? "Reconnecting" : stopping ? "Stopping safely" : failed ? "Needs attention" : active ? (report.phase === "connecting" || !report.phase ? "Connecting" : "Monitoring") : report.status === "stopped" ? "Stopped" : "Finished");
  $("session-status").className = `status-pill ${failed ? "error" : active ? "busy" : ""}`;
  show("stop", active);
  $("stop").disabled = stopping || pending || !connected;
  show("new-session", !active);
  text("active-instruction", report.request || $("instruction").value);
  text("active-interval", `${report.current_interval_s ?? 60} sec`);
  text("active-camera", report.camera_model || (active ? "Connecting…" : "Not connected"));
  text("preview-count", entries.length);
  text("shot-count", report.photos_taken || 0);
  text("call-count", report.api_requests || 0);
  $("call-count").title = `${report.compaction_requests || 0} context summary calls included · ${report.compactions_completed || 0} completed`;
  text("session-detail", failed ? ""
    : stopping ? "Waiting for an active operation to finish. A capture already sent to the camera may still complete."
    : active ? ""
    : reasons[report.stop_reason] || "This session has ended. Your files are saved locally.");
  text("output-path", !active && state.output ? `Saved in ${state.output}` : "");
  renderTimeline(entries);
  updateClock();
}

function updateClock() {
  if (!state || !showingMonitor) return;
  const report = state.report || {};
  const active = state.operation === "monitor";
  const start = Date.parse(report.monitoring_started_at);
  const end = Date.parse(report.ended_at);
  const elapsed = Number.isFinite(start) ? Math.max(0, ((Number.isFinite(end) ? end : Date.now()) - start) / 1000) : 0;
  text("elapsed", clock(elapsed));
  let next = !connected ? "Connection lost · retrying…" : state.stopping ? "Finishing the current operation…" : !active ? "Session finished" : phases[report.phase] || phases.connecting;
  if (connected && active && !state.stopping && report.phase === "waiting" && report.next_observation_at) {
    const remaining = Math.max(0, report.next_observation_at - Date.now() / 1000);
    next = `Next observation in ${clock(Math.ceil(remaining))}`;
  }
  text("next-check", next);
}

function render() {
  if (!state) return;
  show("setup", !showingMonitor);
  show("monitor", showingMonitor);
  renderSetup();
  if (showingMonitor) renderMonitor();
}

$("session-form").addEventListener("submit", async (event) => {
  event.preventDefault();
  const data = Object.fromEntries(new FormData(event.target));
  if (await command("/api/start", data)) { showingMonitor = true; following = true; render(); window.scrollTo(0, 0); }
});
$("check-camera").addEventListener("click", () => command("/api/check"));
$("stop").addEventListener("click", () => command("/api/stop"));
$("new-session").addEventListener("click", () => { showingMonitor = false; notice(""); render(); window.scrollTo(0, 0); });
$("return-session").addEventListener("click", () => { showingMonitor = true; render(); });
$("go-latest").addEventListener("click", () => {
  following = true;
  for (const preview of previews.values()) preview.classList.remove("focused");
  followLatest();
  show("go-latest", false);
});
$("timeline").addEventListener("scroll", () => {
  const timeline = $("timeline");
  if (following && timeline.scrollHeight - timeline.clientHeight - timeline.scrollTop > 24) browsing();
}, { passive: true });
new ResizeObserver(() => { if (following) followLatest(); }).observe($("timeline"));
$("close-viewer").addEventListener("click", () => $("image-viewer").close());
$("image-viewer").addEventListener("click", (event) => {
  if (event.target === $("image-viewer")) {
    const rect = event.target.getBoundingClientRect();
    if (event.clientX < rect.left || event.clientX > rect.right || event.clientY < rect.top || event.clientY > rect.bottom) event.target.close();
  }
});
async function poll() {
  const generation = commandGeneration;
  try {
    const next = await api("/api/state");
    if (connectionProblemReported) { connectionProblemReported = false; notice(""); }
    // Do not let an older polling response replace a just-completed command.
    if (!pending && generation === commandGeneration) {
      accept(next);
      if (!checkedOnLoad) {
        checkedOnLoad = true;
        if (!next.operation && !next.session_id) await command("/api/check");
      }
    }
  } catch {
    connected = false;
    if (!connectionProblemReported) notice("Cannot reach the local server. Keep ui.py running. Monitoring may still be active; the page will reconnect automatically.");
    connectionProblemReported = true;
    render();
  } finally { setTimeout(poll, 1000); }
}
setInterval(updateClock, 500);
poll();
