"use strict";

// region: constants

const API = {
  state: "/api/state",
  events: "/api/events",
  settings: "/api/settings",
  clearCrop: "/api/settings/crop",
  webcamStream: "/api/webcam/stream.mjpg",
  webcamInfo: "/api/webcam/info",
  phoneConnect: "/api/phone/connect",
  phoneStatus: "/api/phone/status",
  phoneZoom: "/api/phone/zoom",
  phoneTorch: "/api/phone/torch",
  phoneSnapshot: "/api/phone/snapshot",
  phoneSnapshotImage: "/api/phone/snapshot.jpg",
  multimeterRead: "/api/multimeter/read",
  benchStart: "/api/bench/start",
  benchStop: "/api/bench/stop",
  phoneScreen: "/api/phone/screen",
  phoneRotation: "/api/phone/rotation",
  phoneOrientation: "/api/phone/orientation",
  phoneInSensorZoom: "/api/phone/in-sensor-zoom",
  callImage: (id, index) => `/api/calls/${id}/images/${index}`,
};
const MAX_LOG_ROWS = 200;
const MIN_CROP_PIXELS = 8;
const PREVIEW_INTERVAL_MS = 500;
const INFO_INTERVAL_MS = 3000;
const ARGS_PREVIEW_CHARS = 120;
const RECONNECT_MS = 2000;
const MULTIMETER_TOOL = "multimeter_read";
const ZOOM_DECIMALS = 2;
// Phone screen wire format: 4-byte big-endian payload length, 1 kind byte, payload.
const SCREEN_HEADER_BYTES = 5;
const SCREEN_KIND = { config: 0, key: 1, delta: 2 };
const SCREEN_RECONNECT_MS = 2000;
const MICROSECONDS_PER_FRAME = 16666;
const FULLSCREEN_KEY = "f";
const FULL_SNAPSHOT_QUERY = "full=true";
const FORM_FIELDS = "input, select, textarea";
// Digital zoom of the full-screen phone snapshot (a still image: the phone camera zoom does not change it).
const SNAPSHOT_ZOOM_STEP = 1.25;
const SNAPSHOT_ZOOM_MIN = 1;
const SNAPSHOT_ZOOM_MAX = 8;
const SNAPSHOT_ZOOM_RESET_KEY = "0";
// Phone camera zoom from the full-screen live view (phone_zoom, not a digital zoom of the video).
const LIVE_ZOOM_INTERVAL_MS = 150;
const LIVE_ZOOM_HIGHLIGHT_MS = 1200;
const LIVE_ZOOM_KEYS = { ArrowUp: "in", ArrowRight: "in", ArrowDown: "out", ArrowLeft: "out" };
const LIVE_ZOOM_DECIMALS = 1;
// The snapshot flips: the server applies them to the snapshot, and the phone to its camera preview.
const FLIP_KEYS = { h: "horizontal", v: "vertical" };
const FLIP_FIELDS = { horizontal: "flip_horizontal", vertical: "flip_vertical" };
const ZOOM_LABEL_DECIMALS = 2;
const FULL_TURN = 360;
const QUARTER_TURN = 90;
const HALF_TURN = 180;
const ROTATION_AUTO = "auto";

// endregion: constants

const $ = (id) => document.getElementById(id);

const state = {
  settings: null, // SettingsView
  frame: { width: 0, height: 0 }, // webcam frame size in pixels
  crop: null, // {x, y, width, height} in frame pixels
  phone: null, // PhoneState
  snapshotSeq: 0, // the phone snapshot that the page shows
  orientationKey: "", // the flips of the snapshot that the page shows
};

async function api(method, url, body) {
  const options = { method, headers: {} };
  if (body !== undefined) {
    options.headers["Content-Type"] = "application/json";
    options.body = JSON.stringify(body);
  }
  const response = await fetch(url, options);
  const data = await response.json().catch(() => ({}));
  if (!response.ok) {
    throw new Error(data.error || `${method} ${url}: ${response.status}`);
  }
  return data;
}

// region: webcam and crop

function frameSize() {
  const img = $("webcam");
  return {
    width: state.frame.width || img.naturalWidth,
    height: state.frame.height || img.naturalHeight,
  };
}

function toFrame(event) {
  const rect = $("stage").getBoundingClientRect();
  const size = frameSize();
  const x = ((event.clientX - rect.left) / rect.width) * size.width;
  const y = ((event.clientY - rect.top) / rect.height) * size.height;
  return {
    x: Math.min(Math.max(x, 0), size.width),
    y: Math.min(Math.max(y, 0), size.height),
  };
}

function drawCrop() {
  const box = $("crop");
  const size = frameSize();
  const crop = state.crop;
  $("crop-text").textContent = crop ? `${crop.x},${crop.y},${crop.width},${crop.height}` : "none";
  if (!crop || !size.width || !size.height) {
    box.hidden = true;
    return;
  }
  box.hidden = false;
  $("stage").style.setProperty("--frame-ratio", String(size.width / size.height));
  box.style.left = `${(crop.x / size.width) * 100}%`;
  box.style.top = `${(crop.y / size.height) * 100}%`;
  box.style.width = `${(crop.width / size.width) * 100}%`;
  box.style.height = `${(crop.height / size.height) * 100}%`;
}

function normalized(a, b) {
  return {
    x: Math.round(Math.min(a.x, b.x)),
    y: Math.round(Math.min(a.y, b.y)),
    width: Math.round(Math.abs(a.x - b.x)),
    height: Math.round(Math.abs(a.y - b.y)),
  };
}

function setupCropEditor() {
  const stage = $("stage");
  let drag = null;

  stage.addEventListener("pointerdown", (event) => {
    // The crop box is read-only in full screen.
    if (event.button !== 0 || !frameSize().width || document.fullscreenElement) return;
    // preventDefault below stops the focus change, so focus the view here: the key "f" needs it.
    $("webcam-view").focus({ preventScroll: true });
    const point = toFrame(event);
    const corner = event.target.dataset?.corner;
    const crop = state.crop;
    if (corner && crop) {
      // The fixed point is the opposite corner.
      const fixed = {
        x: corner.includes("w") ? crop.x + crop.width : crop.x,
        y: corner.includes("n") ? crop.y + crop.height : crop.y,
      };
      drag = { mode: "resize", fixed };
    } else if (event.target === $("crop") && crop) {
      drag = { mode: "move", start: point, origin: { ...crop } };
    } else {
      drag = { mode: "new", fixed: point };
    }
    stage.setPointerCapture(event.pointerId);
    event.preventDefault();
  });

  stage.addEventListener("pointermove", (event) => {
    if (!drag) return;
    const point = toFrame(event);
    if (drag.mode === "move") {
      const size = frameSize();
      const o = drag.origin;
      const x = Math.min(Math.max(o.x + point.x - drag.start.x, 0), size.width - o.width);
      const y = Math.min(Math.max(o.y + point.y - drag.start.y, 0), size.height - o.height);
      state.crop = { ...o, x: Math.round(x), y: Math.round(y) };
    } else {
      state.crop = normalized(drag.fixed, point);
    }
    drawCrop();
  });

  const finish = async () => {
    if (!drag) return;
    drag = null;
    const crop = state.crop;
    if (crop && (crop.width < MIN_CROP_PIXELS || crop.height < MIN_CROP_PIXELS)) {
      // A click without a drag: keep the saved crop.
      state.crop = state.settings.effective.webcam_crop;
      drawCrop();
      return;
    }
    await saveSettings({ ...state.settings.saved, webcam_crop: crop });
  };
  stage.addEventListener("pointerup", finish);
  stage.addEventListener("pointercancel", finish);

  $("clear-crop").addEventListener("click", async () => {
    try {
      applySettings(await api("DELETE", API.clearCrop));
    } catch (error) {
      $("settings-status").textContent = error.message;
    }
  });
}

function drawPreview() {
  const img = $("webcam");
  const canvas = $("crop-preview");
  if (!img.complete || !img.naturalWidth) return;
  const size = frameSize();
  const crop = state.crop || { x: 0, y: 0, width: size.width, height: size.height };
  // The page image can have another size than the frame; scale the crop to it.
  const sx = img.naturalWidth / size.width;
  const sy = img.naturalHeight / size.height;
  if (canvas.width !== crop.width || canvas.height !== crop.height) {
    canvas.width = crop.width;
    canvas.height = crop.height;
  }
  try {
    canvas.getContext("2d").drawImage(
      img, crop.x * sx, crop.y * sy, crop.width * sx, crop.height * sy, 0, 0, crop.width, crop.height,
    );
  } catch {
    // The image has no frame yet.
  }
}

async function refreshWebcamInfo() {
  try {
    const info = await api("GET", API.webcamInfo);
    if (info.width && info.height) state.frame = { width: info.width, height: info.height };
    $("webcam-info").textContent =
      `${info.device} ${info.width ?? "?"}×${info.height ?? "?"}` + (info.error ? ` – ${info.error}` : "");
    $("webcam-off").hidden = info.frames > 0;
    $("webcam-off").textContent = info.error || "Waiting for the first frame…";
    drawCrop();
  } catch (error) {
    $("webcam-info").textContent = error.message;
    $("webcam-off").hidden = false;
    $("webcam-off").textContent = error.message;
  }
}

function startWebcam() {
  const img = $("webcam");
  img.addEventListener("load", drawCrop);
  img.addEventListener("error", () => setTimeout(() => (img.src = `${API.webcamStream}?t=${Date.now()}`), RECONNECT_MS));
  img.src = API.webcamStream;
  setupCropEditor();
  $("multimeter-read").addEventListener("click", readMultimeter);
  refreshWebcamInfo();
  setInterval(refreshWebcamInfo, INFO_INTERVAL_MS);
  setInterval(drawPreview, PREVIEW_INTERVAL_MS);
}

async function readMultimeter(event) {
  const button = event.currentTarget;
  const error = $("multimeter-error");
  const result = $("multimeter-result");
  button.disabled = true;
  error.hidden = true;
  result.textContent = "reading…";
  try {
    const call = await api("POST", API.multimeterRead, { source: $("multimeter-source").value });
    renderReading(result, call.details.reading);
  } catch (err) {
    result.textContent = "";
    error.textContent = err.message;
    error.hidden = false;
  } finally {
    button.disabled = false;
  }
}

// endregion: webcam and crop

// region: phone

function applyPhone(phone) {
  state.phone = phone;
  $("phone-serial").textContent = phone.serial || "not connected";
  $("phone-scrcpy").textContent = phone.scrcpy_running ? "window open" : "off";
  $("phone-screen-state").textContent = phone.screen + (phone.screen_error ? ` – ${phone.screen_error}` : "");
  const status = phone.status;
  const slider = $("zoom-slider");
  const torch = $("torch");
  if (status) {
    $("phone-zoom").textContent =
      `${status.zoom_ratio.toFixed(ZOOM_DECIMALS)}× (${status.min_zoom_ratio}–${status.max_zoom_ratio})`;
    $("phone-torch").textContent = status.has_flash_unit ? (status.torch_enabled ? "on" : "off") : "no flash unit";
    slider.min = status.min_zoom_ratio;
    slider.max = status.max_zoom_ratio;
    if (document.activeElement !== slider) slider.value = status.zoom_ratio;
    slider.disabled = false;
    torch.checked = status.torch_enabled;
    torch.disabled = !status.has_flash_unit;
    $("phone-rotation").textContent =
      `${status.rotation_degrees}° (${status.rotation_locked ? "locked" : "follows the phone"})`;
    $("phone-preview-flip").textContent = previewFlipText(status);
    const snapshotRotation = $("snapshot-rotation");
    if (document.activeElement !== snapshotRotation) {
      snapshotRotation.value = status.rotation_locked ? String(status.rotation_degrees) : ROTATION_AUTO;
    }
    snapshotRotation.disabled = false;
  }
  showFocus(phone.focus);
  showInSensorZoom(phone);
  showViewRotation();
  const orientationKey = applyOrientation(phone.orientation);
  const newOrientation = orientationKey !== state.orientationKey;
  state.orientationKey = orientationKey;
  if (phone.has_snapshot && (phone.snapshot_seq !== state.snapshotSeq || newOrientation)) {
    // Only a new snapshot or new flips reload the image: the status poll also sends phone updates.
    state.snapshotSeq = phone.snapshot_seq;
    resetSnapshotZoom();
    const stamp = Date.now();
    $("snapshot").src = snapshotUrl(document.fullscreenElement === $("snapshot-view"), stamp);
    $("snapshot-full").href = snapshotUrl(true, stamp);
    $("snapshot-view").hidden = false;
    $("snapshot-full").hidden = false;
  }
}

async function phoneAction(url, body, button) {
  const error = $("phone-error");
  error.hidden = true;
  if (button) button.disabled = true;
  try {
    applyPhone(await api("POST", url, body));
  } catch (err) {
    error.textContent = err.message;
    error.hidden = false;
  } finally {
    if (button) button.disabled = false;
  }
}

function toggleInSensorZoom(button) {
  phoneAction(API.phoneInSensorZoom, { enabled: !state.phone?.in_sensor_zoom_choice }, button);
}

function setupPhone() {
  for (const button of document.querySelectorAll("[data-isz]")) {
    button.addEventListener("click", () => toggleInSensorZoom(button));
  }
  $("phone-connect").addEventListener("click", (e) => phoneAction(API.phoneConnect, undefined, e.currentTarget));
  $("phone-refresh").addEventListener("click", (e) => phoneAction(API.phoneStatus, undefined, e.currentTarget));
  $("zoom-in").addEventListener("click", (e) => phoneAction(API.phoneZoom, { step: "in" }, e.currentTarget));
  $("zoom-out").addEventListener("click", (e) => phoneAction(API.phoneZoom, { step: "out" }, e.currentTarget));
  $("zoom-slider").addEventListener("change", (e) =>
    phoneAction(API.phoneZoom, { ratio: Number(e.currentTarget.value) }),
  );
  $("torch").addEventListener("change", (e) => phoneAction(API.phoneTorch, { enabled: e.currentTarget.checked }));
  $("phone-snapshot").addEventListener("click", (e) => phoneAction(API.phoneSnapshot, undefined, e.currentTarget));
  $("view-rotate-left").addEventListener("click", () =>
    saveViewRotation(String((viewRotation() + FULL_TURN - QUARTER_TURN) % FULL_TURN)),
  );
  $("view-rotate-right").addEventListener("click", () =>
    saveViewRotation(String((viewRotation() + QUARTER_TURN) % FULL_TURN)),
  );
  $("view-auto").addEventListener("click", () => saveViewRotation(ROTATION_AUTO));
  $("snapshot-rotation").addEventListener("change", (e) => {
    const value = e.currentTarget.value;
    phoneAction(API.phoneRotation, value === ROTATION_AUTO ? { auto: true } : { degrees: Number(value) });
  });
}

// region: phone screen

const screenState = { decoder: null, codec: null, waitForKey: true, timestamp: 0 };

// The clockwise angle of the phone screen on the page. Auto undoes the turn of the phone: the app reports the
// counterclockwise turn of the phone (Android surface rotation) in `rotation_degrees`.
function viewRotation() {
  const setting = state.settings?.effective.screen_rotation ?? ROTATION_AUTO;
  if (setting !== ROTATION_AUTO) return Number(setting);
  const phoneTurn = state.phone?.status?.rotation_degrees ?? 0;
  return (FULL_TURN - phoneTurn) % FULL_TURN;
}

function showViewRotation() {
  const setting = state.settings?.effective.screen_rotation ?? ROTATION_AUTO;
  const auto = setting === ROTATION_AUTO;
  $("view-rotation").textContent = auto ? `auto, now ${viewRotation()}°` : `${setting}°`;
  $("view-auto").classList.toggle("active", auto);
}

function saveViewRotation(value) {
  saveSettings({ ...state.settings.saved, screen_rotation: value });
}

function drawScreenFrame(frame) {
  const canvas = $("phone-screen");
  const width = frame.displayWidth;
  const height = frame.displayHeight;
  const angle = viewRotation();
  // A quarter turn swaps the sides of the box, so the whole screen stays visible.
  const sideways = angle % HALF_TURN !== 0;
  const boxWidth = sideways ? height : width;
  const boxHeight = sideways ? width : height;
  if (canvas.width !== boxWidth || canvas.height !== boxHeight) {
    canvas.width = boxWidth;
    canvas.height = boxHeight;
  }
  const context = canvas.getContext("2d");
  context.save();
  context.translate(boxWidth / 2, boxHeight / 2);
  context.rotate((angle * Math.PI) / HALF_TURN);
  context.drawImage(frame, -width / 2, -height / 2);
  context.restore();
  frame.close();
  canvas.hidden = false;
  $("phone-screen-note").hidden = true;
}

function configureScreen(codec) {
  if (screenState.decoder && screenState.decoder.state !== "closed" && screenState.codec === codec) return;
  if (screenState.decoder && screenState.decoder.state !== "closed") screenState.decoder.close();
  screenState.decoder = new VideoDecoder({
    output: drawScreenFrame,
    error: (error) => {
      // The next key frame configures a new decoder.
      $("phone-screen-note").textContent = `Decoder error: ${error.message}`;
      $("phone-screen-note").hidden = false;
      screenState.codec = null;
      screenState.waitForKey = true;
    },
  });
  screenState.decoder.configure({ codec, optimizeForLatency: true });
  screenState.codec = codec;
  screenState.waitForKey = true;
}

function handleScreenMessage(kind, payload) {
  if (kind === SCREEN_KIND.config) {
    configureScreen(JSON.parse(new TextDecoder().decode(payload)).codec);
    return;
  }
  const decoder = screenState.decoder;
  if (!decoder || decoder.state !== "configured") {
    if (screenState.codec) configureScreen(screenState.codec);
    else return;
  }
  const key = kind === SCREEN_KIND.key;
  if (screenState.waitForKey && !key) return;
  screenState.waitForKey = false;
  screenState.timestamp += MICROSECONDS_PER_FRAME;
  screenState.decoder.decode(
    new EncodedVideoChunk({ type: key ? "key" : "delta", timestamp: screenState.timestamp, data: payload }),
  );
}

async function readScreen() {
  const response = await fetch(API.phoneScreen);
  if (!response.ok) throw new Error((await response.json().catch(() => ({}))).error || `${response.status}`);
  const reader = response.body.getReader();
  let buffer = new Uint8Array(0);
  for (;;) {
    const { value, done } = await reader.read();
    if (done) return;
    const joined = new Uint8Array(buffer.length + value.length);
    joined.set(buffer);
    joined.set(value, buffer.length);
    buffer = joined;
    let offset = 0;
    while (buffer.length - offset >= SCREEN_HEADER_BYTES) {
      const view = new DataView(buffer.buffer, buffer.byteOffset + offset);
      const length = view.getUint32(0);
      if (buffer.length - offset < SCREEN_HEADER_BYTES + length) break;
      const kind = view.getUint8(4);
      const start = offset + SCREEN_HEADER_BYTES;
      handleScreenMessage(kind, buffer.slice(start, start + length));
      offset = start + length;
    }
    buffer = buffer.slice(offset);
  }
}

async function startScreen() {
  if (typeof VideoDecoder === "undefined") {
    $("phone-screen-note").textContent = "This browser has no WebCodecs VideoDecoder.";
    return;
  }
  for (;;) {
    try {
      // A new connection gets the config and the frames since the last key frame.
      screenState.codec = null;
      await readScreen();
    } catch (error) {
      $("phone-screen-note").textContent = `Phone screen: ${error.message}`;
    }
    await new Promise((resolve) => setTimeout(resolve, SCREEN_RECONNECT_MS));
  }
}

// endregion: phone screen

// endregion: phone

// region: settings

function applySettings(view) {
  state.settings = view;
  state.crop = view.effective.webcam_crop;
  const model = $("vision-model");
  const warmup = $("warmup");
  model.placeholder = view.start.vision_model;
  warmup.placeholder = String(view.start.webcam_warmup_frames);
  if (document.activeElement !== model) model.value = view.saved.vision_model ?? "";
  if (document.activeElement !== warmup) warmup.value = view.saved.webcam_warmup_frames ?? "";
  $("settings-file").textContent = view.settings_file;
  drawCrop();
  showViewRotation();
}

async function saveSettings(saved) {
  const status = $("settings-status");
  try {
    applySettings(await api("PUT", API.settings, saved));
    status.textContent = "Saved.";
  } catch (error) {
    status.textContent = error.message;
  }
}

function setupSettings() {
  $("settings-form").addEventListener("submit", (event) => {
    event.preventDefault();
    const model = $("vision-model").value.trim();
    const warmup = $("warmup").value.trim();
    saveSettings({
      ...state.settings.saved,
      vision_model: model || null,
      webcam_warmup_frames: warmup === "" ? null : Number(warmup),
    });
  });
  $("settings-reset").addEventListener("click", () => {
    $("vision-model").value = "";
    $("warmup").value = "";
    saveSettings({ ...state.settings.saved, vision_model: null, webcam_warmup_frames: null });
  });
}

// endregion: settings

// region: activity log

function formatTime(iso) {
  return new Date(iso).toLocaleTimeString([], { hour12: false });
}

function formatDuration(ms) {
  if (ms === null || ms === undefined) return "…";
  return ms < 1000 ? `${Math.round(ms)} ms` : `${(ms / 1000).toFixed(1)} s`;
}

function formatArgs(args) {
  const text = Object.entries(args)
    .map(([key, value]) => `${key}=${JSON.stringify(value)}`)
    .join(" ");
  return text.length > ARGS_PREVIEW_CHARS ? `${text.slice(0, ARGS_PREVIEW_CHARS)}…` : text;
}

function renderReading(target, reading) {
  target.hidden = false;
  target.textContent = "";
  const value = reading.readable ? `${reading.display_text} ${reading.unit}` : "not readable";
  target.append(value);
  const small = document.createElement("small");
  small.textContent =
    ` ${reading.mode}${reading.range ? `, range ${reading.range}` : ""}` +
    `, confidence ${Math.round(reading.confidence * 100)}%` +
    (reading.flags?.length ? `, ${reading.flags.join(" ")}` : "") +
    (reading.notes ? ` – ${reading.notes}` : "");
  target.append(small);
}

function renderCall(call) {
  let row = document.querySelector(`li[data-id="${call.id}"]`);
  const isNew = !row;
  if (isNew) {
    row = $("log-row").content.firstElementChild.cloneNode(true);
    row.dataset.id = call.id;
  }
  const q = (selector) => row.querySelector(selector);
  q(".time").textContent = formatTime(call.started_at);
  // A call of another MCP server shows who made it, for example "mcp · codex 1824788".
  q(".source").textContent = call.origin ? `${call.source} · ${call.origin}` : call.source;
  q(".source").className = `source badge ${call.source}`;
  q(".tool").textContent = call.tool;
  q(".args").textContent = formatArgs(call.arguments);
  q(".args").title = JSON.stringify(call.arguments, null, 2);
  q(".duration").textContent = formatDuration(call.duration_ms);
  q(".status").textContent = call.status;
  q(".status").className = `status badge ${call.status}`;
  q(".summary").textContent = call.error || call.summary;

  const errorText = q(".error-text");
  errorText.hidden = !call.error;
  errorText.textContent = call.error || "";

  const reading = call.details?.reading;
  if (reading) renderReading(q(".reading"), reading);

  const otherDetails = Object.fromEntries(Object.entries(call.details || {}).filter(([key]) => key !== "reading"));
  const detailsJson = q(".details-json");
  detailsJson.hidden = Object.keys(otherDetails).length === 0 && !reading;
  detailsJson.textContent = JSON.stringify(reading ? { ...otherDetails, reading } : otherDetails, null, 2);

  const images = q(".images");
  images.textContent = "";
  for (const image of call.images) {
    const url = API.callImage(call.id, image.index);
    const figure = document.createElement("figure");
    const link = document.createElement("a");
    link.href = url;
    link.target = "_blank";
    const img = document.createElement("img");
    img.src = url;
    img.alt = image.label;
    img.loading = "lazy";
    link.append(img);
    const caption = document.createElement("figcaption");
    caption.textContent = image.label;
    figure.append(link, caption);
    images.append(figure);
  }
  if (call.tool === MULTIMETER_TOOL && call.status !== "running") q("details").open = true;

  if (isNew) {
    const log = $("log");
    log.prepend(row);
    while (log.children.length > MAX_LOG_ROWS) log.lastElementChild.remove();
  }
  $("log-count").textContent = `(${$("log").children.length})`;
}

// endregion: activity log

// region: full screen

// The panel shows the scaled snapshot; the full screen view loads the full-resolution one.
function snapshotUrl(full, stamp) {
  return `${API.phoneSnapshotImage}?${full ? `${FULL_SNAPSHOT_QUERY}&` : ""}t=${stamp}`;
}

function toggleFullscreen(view) {
  if (document.fullscreenElement) {
    document.exitFullscreen();
    return;
  }
  view.requestFullscreen().catch((error) => console.warn("full screen refused:", error.message));
}

function setupFullscreen() {
  for (const view of document.querySelectorAll(".view")) {
    view.addEventListener("dblclick", (event) => {
      if (event.target.closest("button")) return;
      toggleFullscreen(view);
    });
    view.querySelector(".fs-button").addEventListener("click", (event) => {
      event.stopPropagation();
      toggleFullscreen(view);
    });
  }
  document.addEventListener("keydown", (event) => {
    if (event.key !== FULLSCREEN_KEY || event.ctrlKey || event.metaKey || event.altKey) return;
    if (event.target.closest?.(FORM_FIELDS)) return;
    const view = document.fullscreenElement ?? document.activeElement?.closest?.(".view");
    if (!view) return;
    event.preventDefault();
    toggleFullscreen(view);
  });
  document.addEventListener("fullscreenchange", () => {
    resetSnapshotZoom();
    if ($("snapshot-view").hidden) return;
    $("snapshot").src = snapshotUrl(document.fullscreenElement === $("snapshot-view"), Date.now());
  });
  setupSnapshotZoom();
  setupFlips();
  setupLiveZoom();
  const controls = document.querySelector("#phone-view .fs-controls");
  for (const button of controls.querySelectorAll("[data-zoom]")) {
    button.addEventListener("click", () => phoneAction(API.phoneZoom, { step: button.dataset.zoom }));
  }
  controls.querySelector("[data-torch]").addEventListener("click", () =>
    phoneAction(API.phoneTorch, { enabled: !state.phone?.status?.torch_enabled }),
  );
}

// endregion: full screen

// region: phone distance

const FOCUS_ADVICE_SHOWN = new Set(["far", "too_close"]);
// The in-sensor zoom gives real extra detail in this zoom range.
const SENSOR_ZOOM_MIN = 2;
const SENSOR_ZOOM_MAX = 4;
const SENSOR_ZOOM_STATES = {
  on: "on",
  off: "off",
  unsupported: "not on this phone",
  fallback: "failed; normal camera",
};
const APPROXIMATE = "approximate";

// For example "≈ 29 cm · ~9 px/mm · focused". The server computes the values; the page only formats them.
function focusText(focus) {
  if (!focus) return "–";
  const parts = [];
  if (focus.distance_cm !== null) {
    parts.push(`${focus.calibration === APPROXIMATE ? "≈ " : ""}${focus.distance_cm} cm`);
  }
  if (focus.detail_px_per_mm !== null) {
    // With the in-sensor zoom at 2x or more, the real detail is higher than this estimate (no number is made up).
    parts.push(`~${Math.round(focus.detail_px_per_mm)} px/mm${focus.sensor_zoom_boost ? " + sensor zoom" : ""}`);
  }
  if (focus.focus_state) parts.push(focus.focus_state);
  return parts.length ? parts.join(" · ") : focus.advice_text;
}

function showInSensorZoom(phone) {
  for (const button of document.querySelectorAll("[data-isz]")) {
    button.setAttribute("aria-pressed", String(Boolean(phone.in_sensor_zoom_choice)));
  }
  const status = phone.status;
  const state = status?.in_sensor_zoom;
  $("phone-isz-state").textContent = !status ? "–" : state ? SENSOR_ZOOM_STATES[state] ?? state : "not in this app";
  const zoom = status?.zoom_ratio ?? 0;
  const inRange = zoom >= SENSOR_ZOOM_MIN && zoom <= SENSOR_ZOOM_MAX;
  const hint = $("phone-isz-hint");
  hint.hidden = !(state === "on" && inRange);
  hint.textContent = hint.hidden ? "" : "real sensor detail at this zoom (not optics)";
}

function showFocus(focus) {
  const text = focusText(focus);
  const adviceClass = focus ? `advice-${focus.advice}` : "";
  for (const id of ["phone-focus", "live-focus"]) {
    const element = $(id);
    element.textContent = text;
    element.className = `${element.className.replace(/\s*advice-\S+/g, "")} ${adviceClass}`.trim();
  }
  const advice = $("phone-focus-advice");
  const shown = Boolean(focus && FOCUS_ADVICE_SHOWN.has(focus.advice));
  advice.hidden = !shown;
  advice.textContent = shown ? focus.advice_text : "";
  advice.className = `hint focus-advice ${adviceClass}`.trim();
  $("live-focus").title = shown ? focus.advice_text : "Distance to the board and detail (estimate)";
}

// endregion: phone distance

// region: snapshot flips

// Show the flips on the buttons. Return a key of the flips. The live phone view gets no CSS flip: the phone
// mirrors its camera preview itself (POST /v1/preview), so its status text stays readable.
function applyOrientation(orientation) {
  const flips = orientation ?? { flip_horizontal: false, flip_vertical: false };
  for (const button of document.querySelectorAll("[data-flip]")) {
    button.setAttribute("aria-pressed", String(Boolean(flips[FLIP_FIELDS[button.dataset.flip]])));
  }
  return `${flips.flip_horizontal}/${flips.flip_vertical}`;
}

function previewFlipText(status) {
  const flips = [status.preview_flip_horizontal && "horizontally", status.preview_flip_vertical && "vertically"];
  const on = flips.filter(Boolean);
  return on.length ? `flipped ${on.join(" and ")}` : "not flipped";
}

function toggleFlip(direction) {
  const field = FLIP_FIELDS[direction];
  const current = Boolean(state.phone?.orientation?.[field]);
  phoneAction(API.phoneOrientation, { [field]: !current });
}

function setupFlips() {
  for (const button of document.querySelectorAll("[data-flip]")) {
    button.addEventListener("click", () => toggleFlip(button.dataset.flip));
  }
  document.addEventListener("keydown", (event) => {
    const direction = FLIP_KEYS[event.key];
    if (!direction || event.ctrlKey || event.metaKey || event.altKey || event.target.closest?.(FORM_FIELDS)) return;
    const view = document.fullscreenElement ?? document.activeElement?.closest?.(".view");
    if (view !== $("snapshot-view")) return;
    event.preventDefault();
    toggleFlip(direction);
  });
}

// endregion: snapshot flips

// region: live zoom

const liveZoom = { busy: false, last: 0, highlight: null };

function liveZoomActive() {
  return document.fullscreenElement === $("phone-view");
}

function showLiveZoom(status, changed) {
  const label = $("live-zoom");
  if (!status) return;
  label.textContent = `${status.zoom_ratio.toFixed(LIVE_ZOOM_DECIMALS)}x`;
  if (!changed) return;
  label.classList.add("changed");
  clearTimeout(liveZoom.highlight);
  liveZoom.highlight = setTimeout(() => label.classList.remove("changed"), LIVE_ZOOM_HIGHLIGHT_MS);
}

// One zoom step at most per interval, and none while a request runs: a fast scroll drops its extra events.
async function liveZoomStep(step) {
  const now = performance.now();
  if (liveZoom.busy || now - liveZoom.last < LIVE_ZOOM_INTERVAL_MS) return;
  liveZoom.busy = true;
  liveZoom.last = now;
  try {
    const phone = await api("POST", API.phoneZoom, { step });
    applyPhone(phone);
    showLiveZoom(phone.status, true);
  } catch (error) {
    $("live-zoom").textContent = error.message;
  } finally {
    liveZoom.busy = false;
  }
}

function setupLiveZoom() {
  $("phone-view").addEventListener(
    "wheel",
    (event) => {
      if (!liveZoomActive()) return;
      event.preventDefault();
      liveZoomStep(event.deltaY < 0 ? "in" : "out");
    },
    { passive: false },
  );
  document.addEventListener("keydown", (event) => {
    const step = LIVE_ZOOM_KEYS[event.key];
    if (!step || !liveZoomActive() || event.target.closest?.(FORM_FIELDS)) return;
    event.preventDefault();
    liveZoomStep(step);
  });
  document.addEventListener("fullscreenchange", () => {
    if (liveZoomActive()) showLiveZoom(state.phone?.status, false);
  });
}

// endregion: live zoom

// region: snapshot zoom

// The full-screen image box covers the screen from (0, 0). transform = translate(x, y) scale(scale), origin 0 0.
const snapshotZoom = { scale: SNAPSHOT_ZOOM_MIN, x: 0, y: 0, pan: null };

function snapshotZoomActive() {
  return document.fullscreenElement === $("snapshot-view");
}

function applySnapshotZoom() {
  const img = $("snapshot");
  const { scale, x, y } = snapshotZoom;
  img.style.transform = scale === SNAPSHOT_ZOOM_MIN ? "" : `translate(${x}px, ${y}px) scale(${scale})`;
  img.classList.toggle("zoomed", scale > SNAPSHOT_ZOOM_MIN);
  $("snapshot-zoom").textContent = `${Number(scale.toFixed(ZOOM_LABEL_DECIMALS))}x`;
}

// Keep the image inside the screen: no empty band at an edge when zoomed in.
function clampSnapshotPan() {
  // The layout size, without the transform: the full-screen box.
  const width = $("snapshot").offsetWidth;
  const height = $("snapshot").offsetHeight;
  snapshotZoom.x = Math.min(0, Math.max(width - width * snapshotZoom.scale, snapshotZoom.x));
  snapshotZoom.y = Math.min(0, Math.max(height - height * snapshotZoom.scale, snapshotZoom.y));
}

function resetSnapshotZoom() {
  Object.assign(snapshotZoom, { scale: SNAPSHOT_ZOOM_MIN, x: 0, y: 0, pan: null });
  $("snapshot").classList.remove("panning");
  applySnapshotZoom();
}

// Zoom around the mouse: the image point under the pointer stays under it.
function zoomSnapshotAt(clientX, clientY, factor) {
  const next = Math.min(SNAPSHOT_ZOOM_MAX, Math.max(SNAPSHOT_ZOOM_MIN, snapshotZoom.scale * factor));
  const localX = (clientX - snapshotZoom.x) / snapshotZoom.scale;
  const localY = (clientY - snapshotZoom.y) / snapshotZoom.scale;
  snapshotZoom.x = clientX - localX * next;
  snapshotZoom.y = clientY - localY * next;
  snapshotZoom.scale = next;
  clampSnapshotPan();
  applySnapshotZoom();
}

function setupSnapshotZoom() {
  const view = $("snapshot-view");
  const img = $("snapshot");
  // Only here, and only in full screen, the wheel zooms instead of scrolling the page.
  view.addEventListener(
    "wheel",
    (event) => {
      if (!snapshotZoomActive()) return;
      event.preventDefault();
      zoomSnapshotAt(event.clientX, event.clientY, event.deltaY < 0 ? SNAPSHOT_ZOOM_STEP : 1 / SNAPSHOT_ZOOM_STEP);
    },
    { passive: false },
  );
  img.addEventListener("pointerdown", (event) => {
    if (!snapshotZoomActive() || event.button !== 0 || snapshotZoom.scale === SNAPSHOT_ZOOM_MIN) return;
    snapshotZoom.pan = { pointerX: event.clientX, pointerY: event.clientY, x: snapshotZoom.x, y: snapshotZoom.y };
    img.setPointerCapture(event.pointerId);
    img.classList.add("panning");
  });
  img.addEventListener("pointermove", (event) => {
    const pan = snapshotZoom.pan;
    if (!pan) return;
    snapshotZoom.x = pan.x + event.clientX - pan.pointerX;
    snapshotZoom.y = pan.y + event.clientY - pan.pointerY;
    clampSnapshotPan();
    applySnapshotZoom();
  });
  const endPan = () => {
    snapshotZoom.pan = null;
    img.classList.remove("panning");
  };
  img.addEventListener("pointerup", endPan);
  img.addEventListener("pointercancel", endPan);
  document.addEventListener("keydown", (event) => {
    if (event.key !== SNAPSHOT_ZOOM_RESET_KEY || !snapshotZoomActive()) return;
    event.preventDefault();
    resetSnapshotZoom();
  });
}

// endregion: snapshot zoom

// region: bench

function benchSummary(result) {
  return result.steps.map((step) => `${step.name}: ${step.status}`).join(" · ");
}

async function benchAction(url, button) {
  const status = $("bench-status");
  button.disabled = true;
  status.textContent = "…";
  try {
    const result = await api("POST", url);
    status.textContent = benchSummary(result);
    status.title = result.steps.map((step) => `${step.name}: ${step.detail}`).join("\n");
  } catch (error) {
    status.textContent = error.message;
  } finally {
    button.disabled = false;
  }
}

function setupBench() {
  $("bench-start").addEventListener("click", (e) => benchAction(API.benchStart, e.currentTarget));
  $("bench-stop").addEventListener("click", (e) => {
    benchAction(API.benchStop, e.currentTarget).then(() => {
      $("bench-status").textContent += " · the page stops now; say \"start the bench\" to the agent";
    });
  });
}

// endregion: bench

// region: live events

function connectEvents() {
  const link = $("link");
  const source = new EventSource(API.events);
  source.addEventListener("open", () => {
    link.textContent = "live";
    link.className = "badge ok";
  });
  source.addEventListener("error", () => {
    link.textContent = "disconnected";
    link.className = "badge error";
  });
  source.addEventListener("call", (event) => renderCall(JSON.parse(event.data)));
  source.addEventListener("phone", (event) => applyPhone(JSON.parse(event.data)));
}

async function loadState() {
  const view = await api("GET", API.state);
  applySettings(view.settings);
  applyPhone(view.phone);
  if (view.webcam?.width) state.frame = { width: view.webcam.width, height: view.webcam.height };
  for (const call of view.calls) renderCall(call);
}

// endregion: live events

async function main() {
  setupPhone();
  setupSettings();
  setupBench();
  setupFullscreen();
  try {
    await loadState();
  } catch (error) {
    $("link").textContent = error.message;
  }
  startWebcam();
  connectEvents();
  startScreen();
}

main();
