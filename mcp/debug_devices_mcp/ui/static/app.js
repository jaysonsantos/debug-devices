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
  phoneScreen: "/api/phone/screen",
  phoneRotation: "/api/phone/rotation",
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
    if (event.button !== 0 || !frameSize().width) return;
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
    const snapshotRotation = $("snapshot-rotation");
    if (document.activeElement !== snapshotRotation) {
      snapshotRotation.value = status.rotation_locked ? String(status.rotation_degrees) : ROTATION_AUTO;
    }
    snapshotRotation.disabled = false;
  }
  showViewRotation();
  if (phone.has_snapshot) {
    const url = `${API.phoneSnapshotImage}?t=${Date.now()}`;
    $("snapshot").src = url;
    $("snapshot-link").href = url;
    $("snapshot-link").hidden = false;
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

function setupPhone() {
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
  q(".source").textContent = call.source;
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
