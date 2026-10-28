const $ = (id) => document.getElementById(id);
const examples = {
  fog: "Take one photo when the village below is clearly visible through the fog.",
  sunrise: "Capture the mountain when warm sunrise light is visible on its summit.",
  lights: "Take one photo of the building each time another apartment goes dark.",
};
const phases = { connecting: "Connecting to camera…", observing: "Saving a fresh preview…", checking: "Checking request capacity…", thinking: "Evaluating the scene…", retrieving: "Looking up visual history…", capturing: "Taking and downloading a photo…", waiting: "Waiting for the next observation" };
const reasons = { duration: "Monitoring duration reached.", max_shots: "Photo limit reached.", max_requests: "Maximum AI calls reached.", capacity_reached: "Request capacity reached. All previews and run transcripts are preserved.", user_stop: "You stopped this session. All saved previews and photos are preserved." };
const captureLabels = { downloaded: "Photo saved", requested: "Preparing capture", shutter_sent: "Downloading photo", failed_after_shutter: "Download unconfirmed", failed_or_unconfirmed: "Capture unconfirmed", skipped_stale_frame: "Preview expired", skipped_duration_expired: "Session expired", skipped_expired_before_shutter: "Capture expired", skipped_cooldown: "Capture cooldown", skipped_photo_limit: "Photo limit reached", skipped_user_stop: "Session stopped" };
const outcomes = { no_photo: "No photo", run_limit: "Run limit reached", session_budget: "Maximum AI calls reached", stale_preview: "Preview expired", duration: "Session expired", capacity_reached: "Capacity reached", user_stop: "Session stopped", error: "Run failed", ...captureLabels };
const toolLabels = { list_history: "List history", get_history: "Retrieve images", shoot: "Shoot" };
let state, connected = false, pending = false, showingMonitor = false, sessionId = null, initialized = false;
let commandGeneration = 0, following = true, viewerFrameId = null;
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
    following = true;
    viewerFrameId = null;
    $("image-viewer").close();
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

function element(tag, className, value) {
  const node = document.createElement(tag);
  if (className) node.className = className;
  if (value !== undefined) node.textContent = value;
  return node;
}
function captured(entry) { return entry.capture_status === "downloaded" && entry.saved_files?.length > 0; }
function entries() { return state.report?.observations || []; }
function browsing() { following = false; show("go-latest", true); }
function expand(tile, value) {
  tile.querySelector(".run-toggle").setAttribute("aria-expanded", String(value));
  tile.querySelector(".activity").hidden = !value;
}
function focusObservation(id) {
  const tile = tiles.get(id);
  if (!tile) return;
  browsing();
  for (const card of tiles.values()) card.classList.toggle("focused", card === tile);
  expand(tile, true);
  $("timeline").scrollLeft = tile.offsetLeft - $("timeline").offsetLeft - 4;
  tile.querySelector(".run-toggle").focus({ preventScroll: true });
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
function runLabel(entry) {
  if (entry.run_status === "failed") return (entry.model_calls ? "" : "Not sent · ") + "Run failed";
  if (entry.outcome) return (entry.model_calls ? "" : "Not sent · ") + (outcomes[entry.outcome] || "Skipped");
  return entry.model_calls ? "Evaluating" : "Saved · preparing run";
}
function traceContent(row, event) {
  row.replaceChildren();
  row.dataset.status = event.status;
  row.classList.toggle("success", event.capture_status === "downloaded" || event.outcome === "downloaded");
  let label, detail = "";
  if (event.kind === "model") {
    label = `Evaluate · call ${event.turn}`;
    detail = event.status === "checking" ? "Checking capacity" : event.status === "started" ? "Request in progress"
      : event.status === "completed" ? `${event.latency_s.toFixed(1)} sec` : event.status === "failed" ? "Request failed · session ended" : outcomes[event.reason] || "Not sent";
  } else if (event.kind === "tool") {
    label = toolLabels[event.name] || "Tool";
    detail = event.error ? `Tool error · ${event.error}` : event.status === "started" ? "In progress"
      : event.status === "failed" ? "Tool failed" : event.name === "list_history" ? `${event.count} preview${event.count === 1 ? "" : "s"}${event.next_cursor !== null ? " · more available" : ""}`
      : event.name === "get_history" ? `${event.frame_ids.length} image${event.frame_ids.length === 1 ? "" : "s"}` : event.name === "shoot" ? "" : "Finished";
  } else if (event.kind === "capture") {
    label = captureLabels[event.capture_status] || "Capture";
    detail = event.capture_status === "downloaded" ? `${event.saved_files.length} original file${event.saved_files.length === 1 ? "" : "s"} downloaded`
      : event.capture_status === "shutter_sent" ? "Shutter sent · awaiting download" : event.status === "failed" ? "No automatic retry" : "";
  } else {
    label = outcomes[event.outcome] || "Run finished";
    detail = `${event.model_calls} / ${state.report?.calls_per_observation || 6} AI calls`;
  }
  const heading = element("div", "trace-heading");
  const timestamp = element("time", "trace-time", `+${clock(event.elapsed_s)}`);
  timestamp.dateTime = event.timestamp;
  timestamp.title = new Date(event.timestamp).toLocaleTimeString();
  heading.append(element("span", "trace-label", label), timestamp);
  row.append(heading);
  if (detail) row.append(element("p", "trace-detail", detail));
  if (event.kind === "tool" && event.frame_ids) {
    const thumbnails = element("div", "history-images");
    for (const id of event.frame_ids) {
      const source = entries().find((entry) => entry.frame_id === id);
      if (!source) continue;
      const item = element("div", "history-image");
      const button = element("button");
      button.type = "button";
      button.setAttribute("aria-label", `Enlarge retrieved observation ${id + 1}`);
      const image = element("img");
      image.alt = `Retrieved observation ${id + 1}`;
      image.loading = "lazy";
      loadPreview(image, media(source.preview_file));
      button.append(image);
      button.addEventListener("click", () => openImage(id));
      const link = element("button", "history-link", `#${String(id + 1).padStart(2, "0")} · ${clock(source.timestamp_s)} ↗`);
      link.type = "button";
      link.setAttribute("aria-label", `Go to observation ${id + 1}`);
      link.addEventListener("click", () => focusObservation(id));
      item.append(button, link);
      thumbnails.append(item);
    }
    row.append(thumbnails);
  }
}
function renderTrace(tile, events) {
  // Collapse progress updates for one action into a stable row; the full events stay on disk.
  const grouped = new Map();
  for (const event of events) {
    if (event.kind === "run" && (event.status === "started" || event.outcome === "downloaded")) continue;
    grouped.set(event.action_id, { ...grouped.get(event.action_id), ...event });
  }
  const trace = tile.querySelector(".activity");
  for (const [id, event] of grouped) {
    let row = tile.traceRows.get(id);
    if (!row) {
      row = element("div", "trace-row");
      tile.traceRows.set(id, row);
      trace.append(row);
    }
    if (row.dataset.event !== event.id) {
      traceContent(row, event);
      row.dataset.event = event.id;
    }
  }
}
function renderTimeline(observations) {
  let added = false;
  for (const entry of observations) {
    let tile = tiles.get(entry.frame_id);
    if (!tile) {
      tile = element("article", "observation");
      tile.id = `observation-${entry.frame_id}`;
      tile.traceRows = new Map();
      const heading = element("div", "card-heading");
      heading.append(element("h3", "", `Preview ${String(entry.frame_id + 1).padStart(2, "0")}`), element("time", "", clock(entry.timestamp_s)));
      const button = element("button", "preview-button");
      button.type = "button";
      button.setAttribute("aria-label", `Enlarge observation ${entry.frame_id + 1}`);
      const image = element("img");
      image.alt = `Preview ${entry.frame_id + 1}`;
      image.loading = "lazy";
      loadPreview(image, media(entry.preview_file));
      button.append(image, element("span", "saved-marker", "✓ Photo saved"));
      button.addEventListener("click", () => openImage(entry.frame_id));
      const toggle = element("button", "run-toggle");
      toggle.type = "button";
      toggle.setAttribute("aria-controls", `trace-${entry.frame_id}`);
      toggle.append(element("span", "run-dot"), element("span", "run-label"), element("span", "run-calls"), element("span", "chevron", "⌄"));
      toggle.addEventListener("click", () => {
        if (entry.frame_id !== entries().at(-1)?.frame_id) browsing();
        expand(tile, toggle.getAttribute("aria-expanded") !== "true");
      });
      const trace = element("div", "activity");
      trace.id = `trace-${entry.frame_id}`;
      trace.setAttribute("aria-label", `Agent activity for observation ${entry.frame_id + 1}`);
      tile.append(heading, button, toggle, element("div", "download-links"), trace);
      expand(tile, entry.frame_id === observations.at(-1)?.frame_id);
      $("timeline").append(tile);
      tiles.set(entry.frame_id, tile);
      added = true;
    }
    tile.classList.toggle("captured", captured(entry));
    tile.dataset.status = entry.run_status || "saved";
    tile.querySelector(".saved-marker").hidden = !captured(entry);
    tile.querySelector(".run-label").textContent = runLabel(entry);
    tile.querySelector(".run-calls").textContent = `${entry.model_calls || 0} / ${state.report?.calls_per_observation || 6}`;
    downloads(tile.querySelector(".download-links"), entry);
    renderTrace(tile, entry.events || []);
  }
  if (added && following) $("timeline").scrollLeft = $("timeline").scrollWidth;
  show("go-latest", !following);
  show("timeline-empty", !observations.length);
  text("timeline-count", `${observations.length} preview${observations.length === 1 ? "" : "s"} · activity beneath each frame`);
  if ($("image-viewer").open) {
    const entry = observations.find((item) => item.frame_id === viewerFrameId);
    if (entry) downloads($("viewer-downloads"), entry);
  }
}

function renderMonitor() {
  const report = state.report || {};
  const entries = report.observations || [];
  const active = state.operation === "monitor";
  const failed = report.status === "failed" || Boolean(state.error);
  const stopping = state.stopping;
  text("monitor-title", active ? "Monitoring" : failed ? "Session ended" : report.status === "stopped" ? "Session stopped" : "Session complete");
  text("status-text", !connected ? "Reconnecting" : stopping ? "Stopping safely" : failed ? "Needs attention" : active ? (report.phase === "connecting" || !report.phase ? "Connecting" : "Monitoring") : report.status === "stopped" ? "Stopped" : "Finished");
  $("session-status").className = `status-pill ${failed ? "error" : active ? "busy" : ""}`;
  show("stop", active);
  $("stop").disabled = stopping || pending || !connected;
  show("new-session", !active);
  text("active-instruction", report.request || $("instruction").value);
  text("active-interval", `${report.settings?.interval ?? $("interval").value} sec`);
  text("active-camera", report.camera_model || (active ? "Connecting…" : "Not connected"));
  text("preview-count", entries.length);
  text("shot-count", `${report.photos_taken || 0} / ${report.settings?.max_shots ?? $("max-shots").value}`);
  text("call-count", `${report.api_requests || 0} / ${report.settings?.max_requests ?? $("max-requests").value}`);
  text("session-detail", failed ? state.error || report.camera_error || `Session stopped (${report.error_type || "error"}). Check the saved logs.`
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
  if (await command("/api/start", data)) { showingMonitor = true; following = true; render(); window.scrollTo(0, 0); }
});
$("check-camera").addEventListener("click", () => command("/api/check"));
$("stop").addEventListener("click", () => command("/api/stop"));
$("new-session").addEventListener("click", () => { showingMonitor = false; notice(""); render(); window.scrollTo(0, 0); });
$("return-session").addEventListener("click", () => { showingMonitor = true; render(); });
$("go-latest").addEventListener("click", () => {
  following = true;
  for (const tile of tiles.values()) tile.classList.remove("focused");
  const latest = tiles.get(entries().at(-1)?.frame_id);
  if (latest) expand(latest, true);
  $("timeline").scrollLeft = $("timeline").scrollWidth;
  show("go-latest", false);
});
$("timeline").addEventListener("scroll", () => {
  const timeline = $("timeline");
  if (following && timeline.scrollWidth - timeline.clientWidth - timeline.scrollLeft > 12) browsing();
}, { passive: true });
$("close-viewer").addEventListener("click", () => $("image-viewer").close());
$("image-viewer").addEventListener("click", (event) => {
  if (event.target === $("image-viewer")) {
    const rect = event.target.getBoundingClientRect();
    if (event.clientX < rect.left || event.clientX > rect.right || event.clientY < rect.top || event.clientY > rect.bottom) event.target.close();
  }
});
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
