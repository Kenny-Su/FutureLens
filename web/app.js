import { renderPlanReference } from "./intent.mjs";
import { IntentReview } from "./review.js";

const $ = (id) => document.getElementById(id);
const demoMode = document.documentElement.dataset.mode === "demo";
if (demoMode) document.title = "WaitLess · Demo";
const phases = { connecting: "Connecting to camera…", observing: "Saving a fresh preview…", thinking: "Evaluating the scene…", capturing: "Taking a photo…", waiting: "Waiting for the next observation" };
const reasons = {
  user_stop: "You stopped this session. All saved previews and photos are preserved.",
  video_end: "The video has ended. Your captured frames are saved locally.",
  camera_disconnected: "iPhone disconnected. Reconnect it and review the scene to start again.",
};
let state, connected = false, pending = false, showingMonitor = false, sessionId = null, initialized = false;
let commandGeneration = 0, following = true, viewerFrameId = null, savedOnly = false;
let connectionProblemReported = false, lastCheckError = null, lastSourceError = null;
const rows = new Map();
const review = new IntentReview({request: (path, data) => { if (data !== undefined) commandGeneration++; return api(path, data); }, accept, notify: notice,
  cancelled: () => { showingMonitor = false; render(); },
  started: () => { showingMonitor = true; following = true; render(); window.scrollTo(0, 0); }});

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

function sessionError(next) {
  const report = next.report || {};
  return next.error || report.error || report.camera_error || (report.status === "failed" ? "Session failed." : null);
}

function loadPreview(image, url) {
  if (image.getAttribute("src") !== url) image.src = url;
}

async function api(path, data) {
  const upload = data instanceof File;
  const response = await fetch(path, data === undefined ? { cache: "no-store" } : {
    method: "POST", headers: { "Content-Type": upload ? "application/octet-stream" : "application/json",
      "X-WaitLess-Token": state.token,
      ...(upload ? {"X-Video-Name": encodeURIComponent(data.name)} : {}) },
    body: upload ? data : JSON.stringify(data),
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
    $("timeline").replaceChildren();
    showingMonitor = Boolean(sessionId);
  } else if (!initialized) {
    showingMonitor = Boolean(sessionId);
  }
  initialized = true;
  render();
  const checkError = state.camera_check?.error || null;
  if (checkError && checkError !== lastCheckError) notice(checkError);
  lastCheckError = checkError;
  if (state.source_error && state.source_error !== lastSourceError) notice(state.source_error);
  lastSourceError = state.source_error;
}

function renderCameraStatus() {
  const monitoring = state.operation === "monitor";
  const connecting = state.operation === "connect";
  const disconnecting = state.operation === "disconnect";
  const report = monitoring ? state.report || {} : {};
  const checking = state.operation === "check" || (monitoring && (!report.phase || report.phase === "connecting"));
  const check = state.camera_check;
  const error = check?.error || report.camera_error;
  const ready = !error && (check ? Boolean(check.preview) : report.status !== "failed" && Boolean(report.monitoring_started_at));
  const healthy = connected && !checking && !connecting && !disconnecting && ready;
  const demo = state.source.mode === "video";
  text("camera-status", !connected ? "Connection lost" : state.operation === "import" ? "Importing video…"
    : connecting ? "Connecting iPhone…" : disconnecting ? "Disconnecting iPhone…"
    : checking ? demo ? "Loading video…" : "Checking camera…" : error ? demo ? "Check video" : "Check camera"
    : ready ? demo ? "Demo video ready" : "Camera connected" : demo ? "Demo video" : state.source.id ? "Check camera" : demoMode ? "Import a video" : "iPhone disconnected");
  $("camera-dot").className = `dot ${healthy ? "ready" : error || !connected ? "error" : ""}`;
  $("check-camera").disabled = pending || !connected || Boolean(state.operation) || !state.source.id;
  $("check-camera").title = demo ? "Refresh the video reference" : healthy ? "Last connection check succeeded. Click to check again." : "Check camera connection";
  const locked = pending || review.busy || !connected || Boolean(state.operation);
  const hasIPhone = !demo && Boolean(state.source.id);
  $("source-camera").disabled = locked;
  $("source-camera").title = hasIPhone ? "Release the iPhone camera and end this review" : "Connect to your iPhone camera";
  $("source-video").disabled = locked;
  text("source-camera", connecting ? "Connecting iPhone…" : disconnecting ? "Disconnecting iPhone…" : hasIPhone ? "Disconnect iPhone" : "Connect iPhone");
  text("source-video", demo ? "Replace video" : "Import video");
  text("source-detail", demo ? `${state.source.name} · ${clock(state.source.duration_s)} · Decision-paced`
    : state.source.id ? state.source.name
    : demoMode ? "Import a video to begin" : "iPhone Continuity Camera");
}

function element(tag, className, value) {
  const node = document.createElement(tag);
  if (className) node.className = className;
  if (value !== undefined) node.textContent = value;
  return node;
}
function captured(entry) { return entry.saved_files?.length > 0; }
function entries() { return state.report?.observations || []; }
function browsing() { following = false; show("go-latest", true); }
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
function renderPreview(row, entry) {
  if (!row.firstChild) {
    const surface = element("div", "preview-card");
    const heading = element("div", "preview-heading");
    heading.append(element("h3", "preview-label", entry.video_position_s !== undefined
      ? `Video · ${clock(entry.video_position_s)}` : `Preview ${String(entry.frame_id + 1).padStart(2, "0")}`),
      element("time", "preview-time", clock(entry.timestamp_s)));
    surface.append(heading);
    const button = element("button", "preview-button");
    button.type = "button";
    button.setAttribute("aria-label", `Enlarge observation ${entry.frame_id + 1}`);
    const image = element("img");
    image.alt = `Preview ${entry.frame_id + 1}`;
    image.loading = "lazy";
    loadPreview(image, media(entry.preview_file));
    button.append(image, element("span", "saved-marker", "✓ Photo saved"));
    button.addEventListener("click", () => openImage(entry.frame_id));
    surface.append(button, element("div", "download-links"));
    row.append(surface);
  }
  downloads(row.querySelector(".download-links"), entry);
  row.classList.toggle("captured", captured(entry));
  row.querySelector(".saved-marker").hidden = !captured(entry);
}
function followLatest() {
  $("timeline").scrollTop = $("timeline").scrollHeight;
}
function renderTimeline(observations) {
  const timeline = $("timeline");
  const previousTop = timeline.scrollTop;
  let previousRow = null;
  const items = observations.filter(entry => !savedOnly || captured(entry));
  const visibleIds = new Set(items.map(entry => entry.frame_id));
  for (const [id, row] of rows) if (!visibleIds.has(id)) row.remove();
  timeline.classList.toggle("saved-only", savedOnly);
  for (const entry of items) {
    let row = rows.get(entry.frame_id);
    if (!row) {
      row = element("article", "thread-item kind-preview");
      row.id = `observation-${entry.frame_id}`;
      rows.set(entry.frame_id, row);
    }
    const next = previousRow ? previousRow.nextElementSibling : timeline.firstElementChild;
    if (next !== row) timeline.insertBefore(row, next);
    previousRow = row;
    renderPreview(row, entry);
  }
  if (following) followLatest();
  else timeline.scrollTop = previousTop;
  show("go-latest", !following);
  const count = savedOnly ? observations.filter(captured).length : observations.length;
  show("timeline-empty", !count);
  if (savedOnly) text("timeline-empty", "No photos saved yet. Qualifying moments will collect here for you to review.");
  if ($("image-viewer").open) {
    const entry = observations.find(item => item.frame_id === viewerFrameId);
    if (entry) downloads($("viewer-downloads"), entry);
  }
}

function renderMonitor() {
  const report = state.report || {};
  const demo = report.mode === "demo_video" || (!report.mode && state.source.mode === "video");
  const entries = report.observations || [];
  const active = state.operation === "monitor";
  const failed = Boolean(sessionError(state));
  const stopping = state.stopping;
  const scheduled = active && report.phase === "scheduled";
  text("monitor-title", scheduled ? "Monitoring scheduled" : active ? demo ? "Demo monitoring" : "Monitoring" : failed ? "Session ended" : report.status === "stopped" ? "Session stopped" : "Session complete");
  text("status-text", !connected ? "Reconnecting" : stopping ? "Stopping safely" : failed ? "Needs attention" : scheduled ? "Scheduled" : active ? (report.phase === "connecting" || !report.phase ? "Connecting" : "Monitoring") : report.status === "stopped" ? "Stopped" : "Finished");
  $("session-status").className = `status-pill ${failed ? "error" : active ? "busy" : ""}`;
  show("stop", active);
  $("stop").disabled = stopping || pending || !connected;
  show("new-session", !active);
  text("active-instruction", report.approval?.plan.instructions ||
    (state.intent?.status === "approved" ? state.intent.plans.at(-1)?.plan.instructions : "") || report.request || state.intent?.request || "");
  show("active-plan-reference", Boolean(report.approval?.plan.reference));
  renderPlanReference($("active-reference"), report.approval?.plan, report.approval?.plan.reference?.photo ? media(report.approval.plan.reference.photo) : "");
  text("interval-label", demo ? "Sampling at" : "Every");
  text("active-interval", demo ? `${report.settings?.demo_fps ?? 1} fps` : `${report.settings?.interval_s ?? 60} sec`);
  text("active-camera", report.camera_model || (scheduled ? demo ? state.source.name : "Camera" : active ? demo ? "Opening video…" : "Connecting…" : "Not connected"));
  text("timeline-empty", demo ? "The first video preview will appear here when monitoring begins." : "The first preview will appear here after the camera connects.");
  text("shot-count", report.photos_taken || 0);
  text("session-detail", failed ? sessionError(state)
    : stopping ? "Waiting for an active operation to finish. A capture already sent to the camera may still complete."
    : scheduled ? `Starts at ${new Date(report.scheduled_start_at * 1000).toLocaleString()}` : active ? ""
    : reasons[report.stop_reason] || "This session has ended. Your files are saved locally.");
  text("output-path", !active && state.output ? `Saved in ${state.output}` : "");
  renderTimeline(entries);
  updateClock();
}

function updateClock() {
  if (!state || !showingMonitor) return;
  const report = state.report || {};
  const demo = report.mode === "demo_video" || (!report.mode && state.source.mode === "video");
  const active = state.operation === "monitor";
  const start = Date.parse(report.monitoring_started_at);
  const end = Date.parse(report.ended_at);
  const elapsed = Number.isFinite(start) ? Math.max(0, ((Number.isFinite(end) ? end : Date.now()) - start) / 1000) : 0;
  text("elapsed", clock(elapsed));
  let next = !connected ? "Connection lost · retrying…" : state.stopping ? "Finishing the current operation…" : !active ? "Session finished"
    : demo && (!report.phase || report.phase === "connecting") ? "Opening video…" : phases[report.phase] || phases.connecting;
  if (connected && active && !state.stopping && report.phase === "scheduled") {
    const remaining = Math.max(0, report.scheduled_start_at - Date.now() / 1000);
    next = `Monitoring starts in ${clock(Math.ceil(remaining))}`;
  } else if (connected && active && !state.stopping && report.phase === "waiting" && report.next_observation_at) {
    const remaining = Math.max(0, report.next_observation_at - Date.now() / 1000);
    next = `Next observation in ${clock(Math.ceil(remaining))}`;
  }
  if (demo && report.observations?.length) {
    const position = report.observations.at(-1).video_position_s;
    next = `${clock(position)} / ${clock(report.video.duration_s)} · ${next}`;
  }
  text("next-check", next);
}

function render() {
  if (!state) return;
  const reviewing = state.intent && !["approved", "cancelled"].includes(state.intent.status);
  show("intent-review", !showingMonitor || Boolean(reviewing));
  show("monitor", showingMonitor && !reviewing);
  renderCameraStatus();
  review.render(state, connected, pending, Boolean(state.source.id));
  if (showingMonitor && !reviewing) {
    renderMonitor();
    if ($("image-viewer").open && viewerFrameId !== null) openImage(viewerFrameId);
  }
}

for (const [id, onlySaved] of [["view-all", false], ["view-saved", true]]) {
  $(id).addEventListener("click", () => {
    savedOnly = onlySaved;
    $("view-all").setAttribute("aria-pressed", String(!savedOnly));
    $("view-saved").setAttribute("aria-pressed", String(savedOnly));
    following = true;
    renderMonitor();
  });
}

$("source-camera").addEventListener("click", () => state.source.mode === "camera" && state.source.id
  ? command("/api/camera/disconnect") : command("/api/source", {mode: "camera"}));
$("source-video").addEventListener("click", () => $("demo-file").click());
$("demo-file").addEventListener("change", async event => {
  const file = event.target.files[0];
  if (file) {
    if (file.size > 2 * 1024 ** 3) notice("Choose a video file up to 2 GB.");
    else await command("/api/demo", file);
  }
  event.target.value = "";
});
$("check-camera").addEventListener("click", () => command("/api/check"));
$("stop").addEventListener("click", () => command("/api/stop"));
$("new-session").addEventListener("click", () => {
  showingMonitor = false; notice(""); render(); window.scrollTo(0, 0);
  if (state.source.id) command("/api/check");
});
$("return-session").addEventListener("click", () => { showingMonitor = true; render(); });
$("go-latest").addEventListener("click", () => {
  following = true;
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
    if (!pending && !review.busy && generation === commandGeneration) {
      accept(next);
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
