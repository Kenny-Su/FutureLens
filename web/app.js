const $ = (id) => document.getElementById(id);
const examples = {
  fog: "Take one photo when the village below is clearly visible through the fog.",
  sunrise: "Capture the mountain when warm sunrise light is visible on its summit.",
  lights: "Take one photo of the building each time another apartment goes dark.",
};
const phases = { connecting: "Connecting to camera…", observing: "Saving a fresh preview…", checking: "Checking conversation capacity…", thinking: "Evaluating the scene…", capturing: "Taking and downloading a photo…", waiting: "Waiting for the next observation" };
const reasons = { duration: "Monitoring duration reached.", max_shots: "Photo limit reached.", max_requests: "Observation budget reached.", capacity_reached: "Conversation capacity reached. All previews and history are preserved.", user_stop: "You stopped this session. All saved previews and photos are preserved." };
const captureLabels = { downloaded: "Photo saved", requested: "Preparing capture", shutter_sent: "Downloading photo", failed_after_shutter: "Download unconfirmed", failed_or_unconfirmed: "Capture unconfirmed", skipped_stale_frame: "Skipped · preview too old", skipped_duration_expired: "Skipped · session expired", skipped_expired_before_shutter: "Skipped · capture expired", skipped_cooldown: "Skipped · cooldown", skipped_user_stop: "Skipped · session stopped" };
let state, connected = false, pending = false, showingMonitor = false, selected = null, sessionId = null, initialized = false;
let commandGeneration = 0;
const tiles = new Map();

function text(id, value) { $(id).textContent = value; }
function show(id, visible) { $(id).hidden = !visible; }
function clock(seconds) {
  seconds = Math.max(0, Math.floor(seconds || 0));
  const hours = Math.floor(seconds / 3600);
  const minutes = Math.floor(seconds / 60) % 60;
  return (hours ? `${hours}:` : "") + `${String(minutes).padStart(2, "0")}:${String(seconds % 60).padStart(2, "0")}`;
}
function media(path) { return `/media/${encodeURIComponent(state.session_id)}/${path.split("/").map(encodeURIComponent).join("/")}`; }
function notice(message) { text("notice", message || ""); show("notice", Boolean(message)); }

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
    method: "POST", headers: { "Content-Type": "application/json", "X-FutureLens-Token": state.token }, body: JSON.stringify(data),
  });
  const result = await response.json();
  if (!response.ok) throw new Error(result.error || "The request could not be completed.");
  return result;
}

async function command(path, data = {}) {
  if (pending || !connected) return false;
  pending = true;
  commandGeneration++;
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
    selected = null;
    tiles.clear();
    $("timeline").replaceChildren();
    showingMonitor = Boolean(sessionId);
  } else if (!initialized) {
    showingMonitor = Boolean(sessionId);
  }
  initialized = true;
  render();
}

function renderSetup() {
  const checking = state.operation === "check";
  const check = state.camera_check;
  const captureReady = check?.capabilities?.capture_ready;
  text("helper-status", state.helper_ready ? "Ready" : "Build required");
  $("helper-status").className = state.helper_ready ? "ready" : "missing";
  text("api-status", state.api_ready ? "Configured" : "Key missing");
  $("api-status").className = state.api_ready ? "ready" : "missing";
  text("camera-name", checking ? "Connecting…" : check?.model || "Sony USB camera");
  $("camera-dot").className = `dot ${check?.error ? "error" : captureReady ? "ready" : ""}`;
  text("camera-detail", checking ? "Checking USB connection and preview. No photo will be taken." : check?.error || (check?.model
    ? captureReady ? "Connection checked. Live view and still-photo capture are available." : (check.capabilities?.capture_issues || ["Still-photo capture unavailable."]).join(" ")
    : "Connect your camera in PC Remote mode. Check the connection to confirm your framing."));
  show("check-preview", Boolean(check?.preview));
  show("camera-icon", !check?.preview);
  if (check?.preview) loadPreview($("check-preview"), check.preview);
  $("check-camera").disabled = pending || !connected || Boolean(state.operation) || !state.helper_ready;
  text("check-camera", checking ? "Checking connection…" : "Check connection ↗");
  $("start").disabled = pending || !connected || Boolean(state.operation) || !state.api_ready || !state.helper_ready;
  show("return-session", Boolean(state.session_id));
}

function renderTimeline(entries) {
  let added = false;
  for (const entry of entries) {
    let tile = tiles.get(entry.frame_id);
    if (!tile) {
      tile = document.createElement("button");
      tile.type = "button";
      tile.className = "timeline-item";
      const thumb = document.createElement("span");
      thumb.className = "thumb";
      const img = document.createElement("img");
      img.loading = "lazy";
      img.alt = "";
      loadPreview(img, media(entry.preview_file));
      const mark = document.createElement("span");
      mark.className = "thumb-mark";
      mark.textContent = "✓";
      thumb.append(img, mark);
      const meta = document.createElement("span");
      meta.className = "thumb-meta";
      const timestamp = document.createElement("span");
      timestamp.textContent = clock(entry.timestamp_s);
      const result = document.createElement("span");
      result.className = "thumb-result";
      meta.append(timestamp, result);
      tile.append(thumb, meta);
      tile.addEventListener("click", () => { selected = entry.frame_id; render(); });
      $("timeline").append(tile);
      tiles.set(entry.frame_id, tile);
      added = true;
    }
    const captured = entry.capture_status === "downloaded" && entry.saved_files?.length > 0;
    const active = entry.frame_id === (selected ?? entries.at(-1)?.frame_id);
    tile.classList.toggle("captured", captured);
    tile.setAttribute("aria-pressed", String(active));
    tile.setAttribute("aria-label", `Observation ${entry.frame_id + 1}, ${clock(entry.timestamp_s)}${captured ? ", photo saved" : ""}`);
    tile.querySelector(".thumb-result").textContent = captured ? "Photo" : entry.action === "wait" ? "Wait" : "Preview";
  }
  if (added && selected === null) $("timeline").scrollLeft = $("timeline").scrollWidth;
  show("timeline-empty", !entries.length);
  text("timeline-count", entries.length ? `${entries.length} observation${entries.length === 1 ? "" : "s"}` : "Every observation, in order");
}

function renderViewer(entries, active) {
  const entry = entries.find((item) => item.frame_id === selected) || entries.at(-1);
  text("view-label", selected === null ? "Latest observation" : "Viewing history · monitoring continues");
  if (!active && selected !== null) text("view-label", "Viewing session history");
  show("go-latest", selected !== null);
  show("main-preview", Boolean(entry));
  show("viewer-empty", !entry);
  const captured = entry?.capture_status === "downloaded" && entry.saved_files?.length > 0;
  show("photo-badge", captured);
  show("originals", captured);
  if (!entry) {
    text("empty-title", active ? "Connecting to your camera" : "No preview received");
    text("empty-detail", active ? "The first observation will appear here." : "Review the session status, then start a new session.");
    text("preview-number", "Awaiting first preview");
    text("preview-time", "");
    text("preview-action", "");
    return;
  }
  const url = media(entry.preview_file);
  loadPreview($("main-preview"), url);
  $("main-preview").alt = `Camera observation ${entry.frame_id + 1} at ${clock(entry.timestamp_s)} into the session`;
  text("preview-number", `Observation ${String(entry.frame_id + 1).padStart(2, "0")}`);
  text("preview-time", clock(entry.timestamp_s));
  text("preview-action", captureLabels[entry.capture_status] || (entry.action === "wait" ? "No photo requested" : active ? "Evaluating…" : "Preview saved"));
  const key = `${sessionId}:${entry.frame_id}:${captured ? entry.saved_files.join("|") : ""}`;
  if ($("original-links").dataset.key !== key) {
    $("original-links").dataset.key = key;
    $("original-links").replaceChildren();
    if (captured) for (const path of entry.saved_files) {
      const link = document.createElement("a");
      link.href = media(path);
      link.download = path.split("/").at(-1);
      link.textContent = `${link.download} ↓`;
      $("original-links").append(link);
    }
  }
}

function renderMonitor() {
  const report = state.report || {};
  const entries = report.observations || [];
  const active = state.operation === "monitor";
  const failed = report.status === "failed" || Boolean(state.error);
  const stopping = state.stopping;
  text("monitor-title", active ? "Monitoring" : failed ? "Session ended" : "Session complete");
  text("status-text", !connected ? "Reconnecting" : stopping ? "Stopping safely" : failed ? "Needs attention" : active ? (report.phase === "connecting" || !report.phase ? "Connecting" : "Monitoring") : report.status === "stopped" ? "Stopped" : "Finished");
  $("session-status").className = `status-pill ${failed ? "error" : active ? "busy" : ""}`;
  show("stop", active);
  $("stop").disabled = stopping || pending || !connected;
  show("new-session", !active);
  text("active-instruction", report.request || $("instruction").value);
  text("active-interval", `${report.settings?.interval ?? $("interval").value} sec`);
  text("active-max-shots", report.settings?.max_shots ?? $("max-shots").value);
  text("active-camera", report.camera_model || (active ? "Connecting…" : "Not connected"));
  text("preview-count", entries.length);
  text("shot-count", report.photos_taken || 0);
  text("session-detail", failed ? state.error || report.camera_error || `Session stopped (${report.error_type || "error"}). Check the saved logs.`
    : stopping ? "Waiting for an active operation to finish. A capture already sent to the camera may still complete."
    : active ? "Your instruction and visual history stay together throughout this session."
    : reasons[report.stop_reason] || "This session has ended. Your files are saved locally.");
  text("output-path", !active && state.output ? `Saved in ${state.output}` : "");
  renderTimeline(entries);
  renderViewer(entries, active);
  updateClock();
}

function updateClock() {
  if (!state || !showingMonitor) return;
  const report = state.report || {};
  const active = state.operation === "monitor";
  const duration = report.settings?.duration || Number($("duration").value) * 60;
  const start = Date.parse(report.monitoring_started_at);
  const end = Date.parse(report.ended_at);
  const elapsed = Number.isFinite(start) ? Math.max(0, ((Number.isFinite(end) ? end : Date.now()) - start) / 1000) : 0;
  text("elapsed", `${clock(elapsed)} / ${clock(duration)}`);
  $("duration-progress").value = Math.min(100, elapsed / duration * 100);
  let next = !connected ? "Connection lost · retrying…" : state.stopping ? "Finishing the current operation…" : !active ? "Session finished" : phases[report.phase] || phases.connecting;
  if (connected && active && !state.stopping && report.phase === "waiting" && report.next_observation_at) {
    const remaining = Math.max(0, Math.min(report.next_observation_at, start / 1000 + duration) - Date.now() / 1000);
    const endsFirst = report.next_observation_at >= start / 1000 + duration;
    next = `${endsFirst ? "Session ends" : "Next observation"} in ${clock(Math.ceil(remaining))}`;
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
  for (const key of ["interval", "duration_minutes", "max_shots", "max_requests"]) data[key] = Number(data[key]);
  if (await command("/api/start", data)) { showingMonitor = true; selected = null; render(); window.scrollTo(0, 0); }
});
$("check-camera").addEventListener("click", () => command("/api/check"));
$("stop").addEventListener("click", () => command("/api/stop"));
$("new-session").addEventListener("click", () => { showingMonitor = false; notice(""); render(); window.scrollTo(0, 0); });
$("return-session").addEventListener("click", () => { showingMonitor = true; render(); });
$("go-latest").addEventListener("click", () => { selected = null; render(); $("timeline").scrollLeft = $("timeline").scrollWidth; });
for (const button of document.querySelectorAll("[data-example]")) button.addEventListener("click", () => {
  $("instruction").value = examples[button.dataset.example];
  $("instruction").focus();
});

async function poll() {
  const generation = commandGeneration;
  try {
    const next = await api("/api/state");
    if (!connected) notice("");
    // Do not let an older polling response replace a just-completed command.
    if (!pending && generation === commandGeneration) accept(next);
  } catch {
    connected = false;
    notice("Cannot reach the local server. Keep ui.py running. Monitoring may still be active; the page will reconnect automatically.");
    render();
  } finally { setTimeout(poll, 1000); }
}
setInterval(updateClock, 500);
poll();
