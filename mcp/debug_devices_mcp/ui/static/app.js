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
  phoneScreenMjpeg: "/api/phone/screen.mjpg",
  phoneRotation: "/api/phone/rotation",
  phoneOrientation: "/api/phone/orientation",
  phoneInSensorZoom: "/api/phone/in-sensor-zoom",
  phoneAfMode: "/api/phone/af-mode",
  phoneMarkings: "/api/phone/markings",
  overlayLayout: "/api/overlay-layout",
  phoneFocus: "/api/phone/focus",
  phoneClearHighlights: "/api/phone/highlight/clear",
  board: "/api/board",
  devices: "/api/devices",
  devicesSelect: "/api/devices/select",
  devicesDisconnect: "/api/devices/disconnect",
  devicesWifi: "/api/devices/wifi",
  devicesPair: "/api/devices/pair",
  devicesConnect: "/api/devices/connect",
  boardNames: "/api/board/names",
  boardOpen: "/api/board/open",
  boardSearch: "/api/board/search",
  boardRegister: "/api/board/register",
  callImage: (id, index) => `/api/calls/${id}/images/${index}`,
  staged: "/api/staged",
  stagedOne: (id) => `/api/staged/${id}`,
  stagedPhoto: (id) => `/api/staged/${id}/photo.jpg`,
  stagedMeter: (id) => `/api/staged/${id}/meter.jpg`,
};
const MAX_LOG_ROWS = 200;
const MIN_CROP_PIXELS = 8;
const ARGS_PREVIEW_CHARS = 120;
const RECONNECT_MS = 2000;
const MULTIMETER_TOOL = "multimeter_read";
const ZOOM_DECIMALS = 2;
// Phone screen wire format: 4-byte big-endian payload length, 1 kind byte, payload.
const SCREEN_HEADER_BYTES = 5;
const SCREEN_KIND = { config: 0, key: 1, delta: 2 };
const SCREEN_RECONNECT_MS = 2000;
const MICROSECONDS_PER_FRAME = 16666;
// The MJPEG fallback (browsers without H.264 in WebCodecs): the page draws the image into the canvas this often.
const FALLBACK_DRAW_MS = 100;
const FALLBACK_RECONNECT_MS = 2000;
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
const NEW_SNAPSHOT_KEY = "s";
// The snapshot flips: the server applies them to the snapshot, and the phone to its camera preview.
const FLIP_KEYS = { h: "horizontal", v: "vertical" };
const FLIP_FIELDS = { horizontal: "flip_horizontal", vertical: "flip_vertical" };
const ZOOM_LABEL_DECIMALS = 2;
const FULL_TURN = 360;
const QUARTER_TURN = 90;
const HALF_TURN = 180;
const ROTATION_AUTO = "auto";
// A single click on the live view focuses after this delay; a double-click (full screen) comes before it and cancels it.
const FOCUS_CLICK_DELAY_MS = 250;
// The ring at the click point fades in this time (the CSS animation has the same length).
const FOCUS_RING_MS = 1000;
// The focus label stays this long after the last focus state change.
const FOCUS_LABEL_MS = 3000;
// The MCP SDK text before a tool error message.
const TOOL_ERROR_PREFIX = /^Error executing tool \w+: /;
// A click on the snapshot that moves less than this (px) picks a registration point; more is a pan.
const PICK_MAX_MOVE_PX = 5;
const REGISTER_MIN_POINTS = 4;
// The tools whose result image is the agent's snapshot with its boxes.
const ANNOTATED_TOOLS = new Set(["phone_highlight", "board_locate_in_photo"]);
// In the full-screen phone views: show or hide the markings.
const MARKINGS_KEY = "k";
const MARKINGS_VIEWS = new Set(["phone-view", "snapshot-view"]);
// The phone answer of an old app (no `visible` in POST /v1/overlay): the server removes its boxes instead.
const OLD_APP_MARKER = "the phone app is old";
const AF_MACRO = "macro";
const AF_CONTINUOUS = "continuous";
const AF_MODE_TEXT = { continuous: "continuous autofocus", macro: "macro (close range)", unknown: "unknown" };
// Closer than this, the page suggests the macro focus mode.
const MACRO_HINT_CM = 15;
// A pick waits this long: a double-click (full screen) comes before it and cancels it.
const PICK_CLICK_DELAY_MS = 250;
const DEGREES_PER_HALF_TURN = 180;
const BOARD_TOOL_PREFIX = "board_";
// The note "Scene changed: highlights cleared" shows this long.
const SCENE_NOTE_MS = 4000;
const FOCUS_TAP_STATES = { scanning: "focusing…", focused: "focused", unfocused: "could not focus" };

// endregion: constants

const $ = (id) => document.getElementById(id);

const state = {
  settings: null, // SettingsView
  frame: { width: 0, height: 0 }, // webcam frame size in pixels
  crop: null, // {x, y, width, height} in frame pixels
  phone: null, // PhoneState
  snapshotSeq: 0, // the phone snapshot that the page shows
  version: null, // the code version of the server that served this page
  orientationKey: "", // the flips of the snapshot that the page shows
};

async function api(method, url, body, timeoutMs) {
  const options = { method, headers: {} };
  if (body !== undefined) {
    options.headers["Content-Type"] = "application/json";
    options.body = JSON.stringify(body);
  }
  if (timeoutMs) options.signal = AbortSignal.timeout(timeoutMs);
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

// The webcam info comes as a `webcam` event when it changes; the page loads it when its event stream opens.
function applyWebcamInfo(info) {
  if (info.width && info.height) state.frame = { width: info.width, height: info.height };
  $("webcam-info").textContent =
    `${info.device} ${info.width ?? "?"}×${info.height ?? "?"}` + (info.error ? ` – ${info.error}` : "");
  $("webcam-off").hidden = info.frames > 0;
  $("webcam-off").textContent = info.error || "Waiting for the first frame…";
  drawCrop();
}

async function refreshWebcamInfo() {
  try {
    applyWebcamInfo(await api("GET", API.webcamInfo));
  } catch (error) {
    $("webcam-info").textContent = error.message;
    $("webcam-off").hidden = false;
    $("webcam-off").textContent = error.message;
  }
}

// A hidden tab has no webcam stream (see "hidden tab"): no reconnect then.
function connectWebcam() {
  if (!document.hidden) $("webcam").src = `${API.webcamStream}?t=${Date.now()}`;
}

function startWebcam() {
  const img = $("webcam");
  img.addEventListener("load", drawCrop);
  img.addEventListener("error", () => setTimeout(connectWebcam, RECONNECT_MS));
  connectWebcam();
  setupCropEditor();
  $("multimeter-read").addEventListener("click", readMultimeter);
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
  showFocusTap(phone.focus);
  showHighlights(phone);
  showMarkings(phone);
  showTracking(phone);
  showAfMode(phone);
  showSceneChange(phone.scene_changed_at);
  showRestartNotice(phone.restart_notice);
  showInSensorZoom(phone);
  showLiveZoom(phone.status, false);
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

// One choice, three toggles (panel, full-screen live bar, full-screen snapshot bar): all of them wait for the
// request (the camera rebinds, a few seconds), and all show the same state afterwards.
async function toggleInSensorZoom() {
  const toggles = document.querySelectorAll("[data-isz]");
  for (const toggle of toggles) toggle.disabled = true;
  try {
    await phoneAction(API.phoneInSensorZoom, { enabled: !state.phone?.in_sensor_zoom_choice });
  } finally {
    for (const toggle of toggles) toggle.disabled = false;
  }
}

// The Macro focus toggles (panel and both full-screen bars) share one choice, like the sensor zoom toggles.
async function toggleAfMode() {
  const toggles = document.querySelectorAll("[data-af]");
  for (const toggle of toggles) toggle.disabled = true;
  const mode = state.phone?.af_mode_choice === AF_MACRO ? AF_CONTINUOUS : AF_MACRO;
  try {
    await phoneAction(API.phoneAfMode, { mode });
  } finally {
    for (const toggle of toggles) toggle.disabled = false;
  }
}

function showAfMode(phone) {
  for (const button of document.querySelectorAll("[data-af]")) {
    button.setAttribute("aria-pressed", String(phone.af_mode_choice === AF_MACRO));
  }
  const status = phone.status;
  const mode = status?.af_mode;
  $("phone-af-state").textContent = !status ? "–" : mode ? AF_MODE_TEXT[mode] ?? mode : "not in this app";
  // Close to the board with the normal autofocus: suggest the macro mode.
  const distance = phone.focus?.distance_cm;
  const hint = $("phone-af-hint");
  hint.hidden = !(mode === AF_CONTINUOUS && distance != null && distance < MACRO_HINT_CM);
  hint.textContent = hint.hidden ? "" : "close range: try Macro focus";
}

let snapshotBusy = false;

// A new phone snapshot. It arrives as a phone update with a new snapshot number: the image reloads (full size in
// full screen) and the digital zoom resets.
async function takeSnapshot() {
  if (snapshotBusy) return;
  snapshotBusy = true;
  const buttons = [$("phone-snapshot"), ...document.querySelectorAll("[data-new-snapshot]")];
  for (const button of buttons) button.disabled = true;
  try {
    await phoneAction(API.phoneSnapshot);
  } finally {
    for (const button of buttons) button.disabled = false;
    snapshotBusy = false;
  }
}

function setupPhone() {
  for (const button of document.querySelectorAll("[data-isz]")) {
    button.addEventListener("click", toggleInSensorZoom);
  }
  for (const button of document.querySelectorAll("[data-af]")) {
    button.addEventListener("click", toggleAfMode);
  }
  $("phone-connect").addEventListener("click", (e) => phoneAction(API.phoneConnect, undefined, e.currentTarget));
  $("phone-refresh").addEventListener("click", (e) => phoneAction(API.phoneStatus, undefined, e.currentTarget));
  $("zoom-in").addEventListener("click", (e) => phoneAction(API.phoneZoom, { step: "in" }, e.currentTarget));
  $("zoom-out").addEventListener("click", (e) => phoneAction(API.phoneZoom, { step: "out" }, e.currentTarget));
  $("zoom-slider").addEventListener("change", (e) =>
    phoneAction(API.phoneZoom, { ratio: Number(e.currentTarget.value) }),
  );
  $("torch").addEventListener("change", (e) => phoneAction(API.phoneTorch, { enabled: e.currentTarget.checked }));
  $("phone-snapshot").addEventListener("click", takeSnapshot);
  for (const button of document.querySelectorAll("[data-new-snapshot]")) button.addEventListener("click", takeSnapshot);
  for (const button of document.querySelectorAll("[data-clear-highlights]")) {
    button.addEventListener("click", () => phoneAction(API.phoneClearHighlights, undefined, button));
  }
  // The camera zoom in the full-screen snapshot bar: the same rate limit as the live view.
  for (const button of document.querySelectorAll("[data-snapshot-zoom]")) {
    button.addEventListener("click", () => liveZoomStep(button.dataset.snapshotZoom));
  }
  document.addEventListener("keydown", (event) => {
    if (event.key !== NEW_SNAPSHOT_KEY || document.fullscreenElement !== $("snapshot-view")) return;
    if (event.ctrlKey || event.metaKey || event.altKey || event.target.closest?.(FORM_FIELDS)) return;
    event.preventDefault();
    takeSnapshot();
  });
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

const screenState = {
  decoder: null,
  codec: null,
  waitForKey: true,
  timestamp: 0,
  fallback: false,
  abort: null,
  // The size of the last drawn frame, before the view rotation: a click maps back to it.
  frameWidth: 0,
  frameHeight: 0,
};

class FallbackNeeded extends Error {}

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
  drawScreenSource(frame, frame.displayWidth, frame.displayHeight);
  frame.close();
}

// Draw a video frame or the fallback image, turned by the view rotation, into the phone screen canvas.
function drawScreenSource(source, width, height) {
  const canvas = $("phone-screen");
  const angle = viewRotation();
  // A quarter turn swaps the sides of the box, so the whole screen stays visible.
  const sideways = angle % HALF_TURN !== 0;
  const boxWidth = sideways ? height : width;
  const boxHeight = sideways ? width : height;
  if (canvas.width !== boxWidth || canvas.height !== boxHeight) {
    canvas.width = boxWidth;
    canvas.height = boxHeight;
    canvas.style.setProperty("--screen-ratio", String(boxWidth / boxHeight));
  }
  screenState.frameWidth = width;
  screenState.frameHeight = height;
  const context = canvas.getContext("2d");
  context.save();
  context.translate(boxWidth / 2, boxHeight / 2);
  context.rotate((angle * Math.PI) / HALF_TURN);
  context.drawImage(source, -width / 2, -height / 2);
  context.restore();
  canvas.hidden = false;
  $("phone-screen-note").hidden = true;
}

function configureScreen(codec) {
  if (screenState.decoder && screenState.decoder.state !== "closed" && screenState.codec === codec) return;
  if (screenState.decoder && screenState.decoder.state !== "closed") screenState.decoder.close();
  screenState.decoder = new VideoDecoder({
    output: drawScreenFrame,
    // This browser cannot decode the stream after all (for example "Operation is not supported"): use MJPEG.
    error: (error) => startFallback(`decoder error: ${error.message}`),
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

// Ask before the first decode: some browsers (for example Camoufox) have WebCodecs without H.264.
async function checkCodec(codec) {
  const support = await VideoDecoder.isConfigSupported({ codec, optimizeForLatency: true }).catch(() => null);
  if (!support?.supported) throw new FallbackNeeded(`${codec} is not supported by this browser`);
}

async function readScreen() {
  screenState.abort = new AbortController();
  const response = await fetch(API.phoneScreen, { signal: screenState.abort.signal });
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
      const payload = buffer.slice(start, start + length);
      if (kind === SCREEN_KIND.config) await checkCodec(JSON.parse(new TextDecoder().decode(payload)).codec);
      if (screenState.fallback) return;
      handleScreenMessage(kind, payload);
      offset = start + length;
    }
    buffer = buffer.slice(offset);
  }
}

async function startScreen() {
  if (typeof VideoDecoder === "undefined") {
    startFallback("this browser has no WebCodecs VideoDecoder");
    return;
  }
  while (!screenState.fallback) {
    await tabVisible();
    try {
      // A new connection gets the config and the frames since the last key frame.
      screenState.codec = null;
      await readScreen();
    } catch (error) {
      if (error instanceof FallbackNeeded) {
        startFallback(error.message);
        return;
      }
      // A hidden tab stops the stream on purpose: that is no error.
      if (!screenState.fallback && !document.hidden) $("phone-screen-note").textContent = `Phone screen: ${error.message}`;
    }
    await new Promise((resolve) => setTimeout(resolve, SCREEN_RECONNECT_MS));
  }
}

// The phone screen as MJPEG from the server. It is drawn into the same canvas: rotation, full screen, and the
// zoom keys work the same way.
function startFallback(reason) {
  if (screenState.fallback) return;
  screenState.fallback = true;
  screenState.abort?.abort();
  if (screenState.decoder && screenState.decoder.state !== "closed") screenState.decoder.close();
  console.info(`phone screen: MJPEG fallback (${reason})`);
  $("phone-screen-mode").hidden = false;
  $("phone-screen-mode").title = reason;
  const img = $("phone-screen-mjpeg");
  img.addEventListener("error", () => setTimeout(connectFallback, FALLBACK_RECONNECT_MS));
  connectFallback();
  // Local drawing of the loaded image into the canvas: no request.
  setInterval(() => {
    if (!document.hidden && img.complete && img.naturalWidth) drawScreenSource(img, img.naturalWidth, img.naturalHeight);
  }, FALLBACK_DRAW_MS);
}

function connectFallback() {
  if (screenState.fallback && !document.hidden) $("phone-screen-mjpeg").src = `${API.phoneScreenMjpeg}?t=${Date.now()}`;
}

// endregion: phone screen

// endregion: phone

// region: hidden tab

// A hidden tab stops the webcam and the phone screen streams, so a slow tunnel carries only what the user sees. The
// event stream stays open: the log, the staged list, and the phone state stay current.
function tabVisible() {
  if (!document.hidden) return Promise.resolve();
  return new Promise((resolve) => {
    const check = () => {
      if (document.hidden) return;
      document.removeEventListener("visibilitychange", check);
      resolve();
    };
    document.addEventListener("visibilitychange", check);
  });
}

function setupHiddenTab() {
  document.addEventListener("visibilitychange", () => {
    if (document.hidden) {
      $("webcam").removeAttribute("src");
      $("phone-screen-mjpeg").removeAttribute("src");
      screenState.abort?.abort();
      return;
    }
    // startScreen() waits in tabVisible() and connects again by itself.
    connectWebcam();
    connectFallback();
  });
}

// endregion: hidden tab

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
  setupFocusTap();
  setupHighlights();
  setupMarkings();
  setupBoard();
  setupDevices();
  setupStaged();
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

// region: click to focus

const focusTap = { timer: null, labelTimer: null, active: false, lastState: null };

// A click on the canvas -> a point on the phone screen as the stream shows it, from 0 to 1. It undoes the CSS
// scaling and the letterbox (object-fit: contain in full screen), then the view rotation. Null outside the picture.
function screenPoint(canvas, clientX, clientY) {
  const rect = canvas.getBoundingClientRect();
  if (!screenState.frameWidth || !rect.width || !rect.height) return null;
  const scale = Math.min(rect.width / canvas.width, rect.height / canvas.height);
  const boxX = (clientX - rect.left - (rect.width - canvas.width * scale) / 2) / scale;
  const boxY = (clientY - rect.top - (rect.height - canvas.height * scale) / 2) / scale;
  if (boxX < 0 || boxY < 0 || boxX > canvas.width || boxY > canvas.height) return null;
  // drawScreenSource turns the frame clockwise by the angle around the box center: turn the point back.
  const radians = (-viewRotation() * Math.PI) / HALF_TURN;
  const cos = Math.round(Math.cos(radians));
  const sin = Math.round(Math.sin(radians));
  const dx = boxX - canvas.width / 2;
  const dy = boxY - canvas.height / 2;
  const frameX = dx * cos - dy * sin + screenState.frameWidth / 2;
  const frameY = dx * sin + dy * cos + screenState.frameHeight / 2;
  const clamp = (value) => Math.min(1, Math.max(0, value));
  return { screen_x: clamp(frameX / screenState.frameWidth), screen_y: clamp(frameY / screenState.frameHeight) };
}

function drawFocusRing(view, clientX, clientY) {
  const rect = view.getBoundingClientRect();
  const ring = document.createElement("span");
  ring.className = "focus-ring";
  ring.style.left = `${clientX - rect.left}px`;
  ring.style.top = `${clientY - rect.top}px`;
  view.append(ring);
  setTimeout(() => ring.remove(), FOCUS_RING_MS);
}

function setFocusLabel(text) {
  const label = $("focus-tap");
  label.textContent = text;
  label.hidden = false;
  clearTimeout(focusTap.labelTimer);
  focusTap.labelTimer = setTimeout(() => {
    label.hidden = true;
    focusTap.active = false;
  }, FOCUS_LABEL_MS);
}

// The status poll brings the focus state: show it while a click focus is in progress.
function showFocusTap(focus) {
  const current = focus?.focus_state ?? null;
  if (!focusTap.active || current === focusTap.lastState) return;
  focusTap.lastState = current;
  if (current) setFocusLabel(FOCUS_TAP_STATES[current] ?? current);
}

async function focusAt(view, point, clientX, clientY) {
  drawFocusRing(view, clientX, clientY);
  focusTap.active = true;
  focusTap.lastState = null;
  setFocusLabel(FOCUS_TAP_STATES.scanning);
  try {
    const phone = await api("POST", API.phoneFocus, point);
    focusTap.lastState = phone.focus?.focus_state ?? null;
    applyPhone(phone);
  } catch (err) {
    focusTap.active = false;
    // For example "phone API error 400 bad_request: outside the preview".
    setFocusLabel(err.message.replace(TOOL_ERROR_PREFIX, ""));
  }
}

function setupFocusTap() {
  const view = $("phone-view");
  const canvas = $("phone-screen");
  canvas.addEventListener("click", (event) => {
    clearTimeout(focusTap.timer);
    // The second click of a double-click: the dblclick handler toggles the full screen.
    if (event.detail > 1) return;
    const point = screenPoint(canvas, event.clientX, event.clientY);
    if (!point) return;
    const { clientX, clientY } = event;
    focusTap.timer = setTimeout(() => focusAt(view, point, clientX, clientY), FOCUS_CLICK_DELAY_MS);
  });
  canvas.addEventListener("dblclick", () => clearTimeout(focusTap.timer));
}

// endregion: click to focus

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

// Every zoom ratio label (the full-screen live bar and the full-screen snapshot bar) shows the same value.
function showLiveZoom(status, changed) {
  if (!status) return;
  const labels = document.querySelectorAll("[data-zoom-ratio]");
  for (const label of labels) {
    label.textContent = `${status.zoom_ratio.toFixed(LIVE_ZOOM_DECIMALS)}x`;
    if (changed) label.classList.add("changed");
  }
  if (!changed) return;
  clearTimeout(liveZoom.highlight);
  liveZoom.highlight = setTimeout(() => {
    for (const label of labels) label.classList.remove("changed");
  }, LIVE_ZOOM_HIGHLIGHT_MS);
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
    for (const label of document.querySelectorAll("[data-zoom-ratio]")) label.textContent = error.message;
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
  drawHighlights();
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

// region: highlights

const sceneNote = { seen: undefined, timer: null };

// The agent's boxes (phone_highlight) on the true-orientation snapshot, shown as the page shows the snapshot:
// with the snapshot flips, the letterbox of the full screen view, and the wheel zoom and pan.
function drawHighlights() {
  const layer = $("snapshot-boxes");
  const img = $("snapshot");
  const boxes = state.phone?.highlights ?? [];
  layer.replaceChildren();
  const arrows = state.phone?.arrows ?? [];
  // Where the phone draws boxes: overlay_region (without the system bars and the status label), else preview_region
  // (an older app). The status poll brings a new one after a flip or a rotation change.
  const status = state.phone?.status;
  const region = status?.overlay_region ?? status?.preview_region;
  if ((!boxes.length && !arrows.length && !register.points.length && !region) || !img.naturalWidth) return;
  if ($("snapshot-view").hidden) return;
  const view = $("snapshot-view").getBoundingClientRect();
  const picture = snapshotPicture();
  const { width, height } = picture;
  const left = picture.left - view.left;
  const top = picture.top - view.top;
  // The boxes and arrows are on the phone still: the snapshot turn, then the flips (orientation.ImageTransform).
  const transform = snapshotTransform();
  // The part of the snapshot where the phone shows boxes: a thin frame.
  if (region) {
    const shown = boxFromTrue(region, transform);
    const frame = document.createElement("div");
    frame.className = "preview-frame";
    frame.title = "The part where the phone screen shows boxes";
    frame.style.left = `${left + shown.x * width}px`;
    frame.style.top = `${top + shown.y * height}px`;
    frame.style.width = `${shown.width * width}px`;
    frame.style.height = `${shown.height * height}px`;
    layer.append(frame);
  }
  // Boxes of another MCP server (a secondary): say whose they are.
  if (state.phone.overlay_origin && (boxes.length || arrows.length)) {
    const origin = document.createElement("span");
    origin.className = "overlay-origin";
    origin.textContent = `boxes from ${state.phone.overlay_origin}`;
    layer.append(origin);
  }
  // Boxes and arrows by the highlight layout (docs/overlay-layout.md): the server computes it for this picture.
  drawLayout(layer, { left, top, width, height }, boxes, arrows, transform);
  // The registration points that the user picked (already in the shown orientation).
  for (const point of register.points) {
    const mark = document.createElement("div");
    mark.className = "register-mark";
    mark.style.left = `${left + point.x * width}px`;
    mark.style.top = `${top + point.y * height}px`;
    const label = document.createElement("span");
    label.textContent = point.refdes;
    mark.append(label);
    layer.append(mark);
  }
}

// An angle on the true-orientation snapshot -> the same direction on the shown (flipped) snapshot.
// The transform of the shown snapshot: its clockwise turn (SnapshotInfo.turn_degrees), then the flips of now.
// The same math as orientation.ImageTransform on the server.
function snapshotTransform() {
  const flips = state.phone?.orientation ?? {};
  return {
    turn: state.phone?.snapshot_turn ?? 0,
    flipH: Boolean(flips.flip_horizontal),
    flipV: Boolean(flips.flip_vertical),
  };
}

function turnPoint(x, y, turn) {
  switch (((turn % FULL_TURN) + FULL_TURN) % FULL_TURN) {
    case QUARTER_TURN:
      return [1 - y, x];
    case HALF_TURN:
      return [1 - x, 1 - y];
    case FULL_TURN - QUARTER_TURN:
      return [y, 1 - x];
    default:
      return [x, y];
  }
}

// A normalized point on the phone still -> the same point on the shown snapshot.
function pointFromTrue(x, y, transform) {
  let [shownX, shownY] = turnPoint(x, y, transform.turn);
  if (transform.flipH) shownX = 1 - shownX;
  if (transform.flipV) shownY = 1 - shownY;
  return [shownX, shownY];
}

function boxFromTrue(box, transform) {
  const [ax, ay] = pointFromTrue(box.snapshot_x, box.snapshot_y, transform);
  const [bx, by] = pointFromTrue(box.snapshot_x + box.width, box.snapshot_y + box.height, transform);
  return { x: Math.min(ax, bx), y: Math.min(ay, by), width: Math.abs(bx - ax), height: Math.abs(by - ay) };
}

// An angle on the phone still (0 = right, 90 = down) -> the same direction on the shown snapshot.
function angleFromTrue(angle, transform) {
  let shown = angle + transform.turn;
  if (transform.flipH) shown = DEGREES_PER_HALF_TURN - shown;
  if (transform.flipV) shown = -shown;
  return ((shown % FULL_TURN) + FULL_TURN) % FULL_TURN;
}

// The shown picture of the snapshot in page pixels: after the zoom transform, inside the panel border, and without
// the letterbox of object-fit: contain (a uniform scale, so the picture is in the middle of the box).
function snapshotPicture() {
  const img = $("snapshot");
  const outer = img.getBoundingClientRect();
  // The panel image has a border (the full screen one has none, so the zoom does not scale it).
  const border = img.clientLeft;
  const box = { left: outer.left + border, top: outer.top + border, width: outer.width - 2 * border, height: outer.height - 2 * border };
  const scale = Math.min(box.width / img.naturalWidth, box.height / img.naturalHeight);
  const width = img.naturalWidth * scale;
  const height = img.naturalHeight * scale;
  return { left: box.left + (box.width - width) / 2, top: box.top + (box.height - height) / 2, width, height };
}

function showHighlights(phone) {
  const count = phone.highlights?.length ?? 0;
  for (const button of document.querySelectorAll("[data-clear-highlights]")) {
    button.disabled = count === 0;
    button.title = count ? `Remove the ${count} highlight box(es) from the phone and the page` : "No highlight boxes";
  }
  drawHighlights();
}

// The phone app restarted: the notice stays until the agent's next phone_snapshot.
function showRestartNotice(notice) {
  const note = $("restart-note");
  note.hidden = !notice;
  note.textContent = notice ? `Phone app restarted: ${notice.message.replace(/^the phone app restarted: /, "")}` : "";
}

function showSceneChange(changedAt) {
  const first = sceneNote.seen === undefined;
  const changed = changedAt !== sceneNote.seen;
  sceneNote.seen = changedAt ?? null;
  // A page that opens later shows only a recent change.
  const recent = changedAt && Date.now() - Date.parse(changedAt) < SCENE_NOTE_MS;
  if (!changedAt || !changed || (first && !recent)) return;
  const notes = document.querySelectorAll(".scene-note");
  for (const note of notes) note.hidden = false;
  clearTimeout(sceneNote.timer);
  sceneNote.timer = setTimeout(() => {
    for (const note of notes) note.hidden = true;
  }, SCENE_NOTE_MS);
}

function setupHighlights() {
  $("snapshot").addEventListener("load", drawHighlights);
  // The full screen change and window resizes move the image.
  new ResizeObserver(drawHighlights).observe($("snapshot-view"));
  document.addEventListener("fullscreenchange", () => requestAnimationFrame(drawHighlights));
}

// The last snapshot with green boxes that an agent got (phone_highlight or a highlight of board_locate_in_photo),
// from this server or a secondary one: the user sees what the agent sees.
function showAnnotated(call) {
  if (!ANNOTATED_TOOLS.has(call.tool) || call.status !== "ok" || !call.images?.length) return;
  $("annotated").src = API.callImage(call.id, call.images.length - 1);
  const time = new Date(call.started_at).toLocaleTimeString();
  $("annotated-caption").textContent = `${call.origin ?? "this server"} · ${call.tool} · ${time}`;
  $("annotated-view").hidden = false;
}

// The Markings toggle: one state for the page (in the settings file). Off hides the page's drawings and the
// phone's own boxes; they are kept and come back when shown.
function showMarkings(phone) {
  const visible = phone.markings_visible !== false;
  document.body.classList.toggle("markings-hidden", !visible);
  for (const button of document.querySelectorAll("[data-markings]")) {
    button.setAttribute("aria-pressed", String(visible));
  }
  const note = $("markings-note");
  const old = Boolean(phone.markings_phone?.startsWith(OLD_APP_MARKER));
  note.hidden = !old;
  note.textContent = old ? phone.markings_phone : "";
}

let markingsBusy = false;

async function toggleMarkings() {
  if (markingsBusy) return;
  markingsBusy = true;
  const buttons = document.querySelectorAll("[data-markings]");
  for (const button of buttons) button.disabled = true;
  try {
    await phoneAction(API.phoneMarkings, { visible: state.phone?.markings_visible === false });
  } finally {
    for (const button of buttons) button.disabled = false;
    markingsBusy = false;
  }
}

function setupMarkings() {
  for (const button of document.querySelectorAll("[data-markings]")) button.addEventListener("click", toggleMarkings);
  document.addEventListener("keydown", (event) => {
    if (event.key !== MARKINGS_KEY || event.ctrlKey || event.metaKey || event.altKey) return;
    if (!MARKINGS_VIEWS.has(document.fullscreenElement?.id) || event.target.closest?.(FORM_FIELDS)) return;
    event.preventDefault();
    toggleMarkings();
  });
}

// region: highlight layout

// The layout for the boxes and arrows in the shown picture (pixels of the picture as it is drawn now). The server
// has the one implementation (overlay_layout.py); the page asks it and keeps the last answer.
// `shape`: the boxes, arrows, and transform without the picture size; `width`, `height`: the size of `layout`.
const layoutCache = { key: null, layout: null, pending: null, shape: null, width: 0, height: 0 };
// Rule 4 of docs/overlay-layout.md: the page uses picture pixels, like the annotated image.
const LAYOUT_MIN_BOX = 32;
const SVG_NS = "http://www.w3.org/2000/svg";

function layoutRequest(picture, boxes, arrows, transform) {
  return {
    width: Math.round(picture.width),
    height: Math.round(picture.height),
    min_box: LAYOUT_MIN_BOX,
    inset: true,
    boxes: boxes.map((box) => {
      const shown = boxFromTrue(box, transform);
      return {
        x: shown.x * picture.width,
        y: shown.y * picture.height,
        width: shown.width * picture.width,
        height: shown.height * picture.height,
        label: box.label ?? "",
        tag: box.tag ?? null,
      };
    }),
    arrows: arrows.map((arrow) => ({
      angle_deg: angleFromTrue(arrow.angle_deg, transform),
      label: arrow.label ?? "",
      tag: arrow.tag ?? null,
    })),
  };
}

function drawLayout(layer, picture, boxes, arrows, transform) {
  if (!boxes.length && !arrows.length) return;
  const request = layoutRequest(picture, boxes, arrows, transform);
  const key = JSON.stringify(request);
  const shape = JSON.stringify({ boxes, arrows, transform });
  if (layoutCache.key !== key) {
    if (layoutCache.pending !== key) {
      layoutCache.pending = key;
      api("POST", API.overlayLayout, request)
        .then((layout) => {
          Object.assign(layoutCache, { key, layout, shape, width: request.width, height: request.height });
          drawHighlights();
        })
        .catch((error) => console.warn("highlight layout:", error.message))
        .finally(() => {
          if (layoutCache.pending === key) layoutCache.pending = null;
        });
    }
    // Only the size changed (a wheel zoom or a resize): draw the last layout scaled until the new one comes, so the
    // boxes do not flicker (B-S9 of QA round 4).
    if (layoutCache.layout && layoutCache.shape === shape && layoutCache.width && layoutCache.height) {
      const scaled = scaledLayout(layoutCache.layout, request.width / layoutCache.width, request.height / layoutCache.height);
      paintLayout(layer, picture, scaled, request.boxes);
    }
    return;
  }
  paintLayout(layer, picture, layoutCache.layout, request.boxes);
}

function scaledRect(rect, sx, sy) {
  return { x: rect.x * sx, y: rect.y * sy, width: rect.width * sx, height: rect.height * sy };
}

function scaledLayout(layout, sx, sy) {
  const point = ([x, y]) => [x * sx, y * sy];
  return {
    ...layout,
    boxes: layout.boxes.map((box) => ({ ...box, rect: scaledRect(box.rect, sx, sy) })),
    badges: layout.badges.map((badge) => ({
      ...badge,
      rect: scaledRect(badge.rect, sx, sy),
      leader: badge.leader ? [...point(badge.leader.slice(0, 2)), ...point(badge.leader.slice(2))] : null,
    })),
    arrows: layout.arrows.map((arrow) => ({ ...arrow, anchor: point(arrow.anchor) })),
    legend: layout.legend ? { ...layout.legend, rect: scaledRect(layout.legend.rect, sx, sy) } : null,
    inset: layout.inset
      ? { ...layout.inset, source: scaledRect(layout.inset.source, sx, sy), dest: scaledRect(layout.inset.dest, sx, sy) }
      : null,
  };
}

function placed(element, picture, rect) {
  element.style.left = `${picture.left + rect.x}px`;
  element.style.top = `${picture.top + rect.y}px`;
  element.style.width = `${rect.width}px`;
  element.style.height = `${rect.height}px`;
  return element;
}

function paintLayout(layer, picture, layout, boxes) {
  if (layout.inset) paintInset(layer, picture, layout, boxes);
  for (const box of layout.boxes) {
    const element = placed(document.createElement("div"), picture, box.rect);
    element.className = "highlight-box";
    element.style.borderColor = box.colour;
    element.title = `${box.tag}: ${box.label}`;
    layer.append(element);
  }
  const leaders = document.createElementNS(SVG_NS, "svg");
  leaders.classList.add("highlight-leaders");
  for (const badge of layout.badges) {
    if (badge.leader) {
      const [x1, y1, x2, y2] = badge.leader;
      const line = document.createElementNS(SVG_NS, "line");
      for (const [name, value] of Object.entries({ x1, y1, x2, y2 })) {
        line.setAttribute(name, String((name.startsWith("x") ? picture.left : picture.top) + value));
      }
      line.setAttribute("stroke", badge.colour);
      leaders.append(line);
    }
    const element = placed(document.createElement("span"), picture, badge.rect);
    element.className = "highlight-badge";
    element.style.color = badge.colour;
    element.style.borderColor = badge.colour;
    element.textContent = badge.tag;
    layer.append(element);
  }
  layer.append(leaders);
  for (const arrow of layout.arrows) {
    const element = document.createElement("div");
    element.className = "pointer-arrow";
    element.style.left = `${picture.left + arrow.anchor[0]}px`;
    element.style.top = `${picture.top + arrow.anchor[1]}px`;
    const head = document.createElement("span");
    head.className = "pointer-arrow-head";
    head.textContent = "➜";
    head.style.color = arrow.colour;
    head.style.transform = `translate(-50%, -50%) rotate(${arrow.angle_deg}deg)`;
    const tag = document.createElement("span");
    tag.className = "highlight-badge pointer-arrow-tag";
    tag.style.color = arrow.colour;
    tag.style.borderColor = arrow.colour;
    tag.textContent = arrow.tag;
    element.append(head, tag);
    layer.append(element);
  }
  if (layout.legend) paintLegend(layer, picture, layout.legend);
}

function paintLegend(layer, picture, legend) {
  const element = document.createElement("div");
  element.className = "highlight-legend";
  placed(element, picture, legend.rect);
  element.style.width = "auto";
  element.style.height = "auto";
  if (legend.outside) {
    // Every corner has a box: at the bottom of the view, under the picture when there is room.
    const view = $("snapshot-view").getBoundingClientRect();
    const below = picture.top + picture.height + 8;
    element.style.top = `${Math.min(below, view.height - legend.rect.height - 4)}px`;
    element.classList.add("outside");
  }
  for (const row of legend.rows) {
    const line = document.createElement("div");
    line.style.color = row.colour;
    line.textContent = `${row.tag}: ${row.label}`;
    element.append(line);
  }
  layer.append(element);
}

// Rule 7: the area around small boxes, enlarged, with the same outlines and tags. The inset shows the real box
// areas (`boxes`, the layout request), not the drawn rectangles of at least LAYOUT_MIN_BOX (B-S4 of QA round 4).
function paintInset(layer, picture, layout, boxes) {
  const inset = layout.inset;
  const img = $("snapshot");
  const canvas = document.createElement("canvas");
  canvas.className = "highlight-inset";
  placed(canvas, picture, inset.dest);
  canvas.width = Math.max(1, Math.round(inset.dest.width));
  canvas.height = Math.max(1, Math.round(inset.dest.height));
  const context = canvas.getContext("2d");
  const toImage = img.naturalWidth / picture.width;
  try {
    context.drawImage(
      img,
      inset.source.x * toImage,
      inset.source.y * toImage,
      inset.source.width * toImage,
      inset.source.height * toImage,
      0,
      0,
      canvas.width,
      canvas.height,
    );
  } catch {
    return; // no image yet
  }
  context.font = "bold 12px sans-serif";
  context.textBaseline = "top";
  layout.boxes.forEach((drawn, index) => {
    const box = boxes[index];
    if (!box) return;
    const x = (box.x - inset.source.x) * inset.scale;
    const y = (box.y - inset.source.y) * inset.scale;
    const width = box.width * inset.scale;
    const height = box.height * inset.scale;
    // Rule 5: the dark outline 2 px wider on each side, then the colour outline.
    context.lineWidth = 7;
    context.strokeStyle = "rgba(0, 0, 0, 0.6)";
    context.strokeRect(x, y, width, height);
    context.lineWidth = 3;
    context.strokeStyle = drawn.colour;
    context.strokeRect(x, y, width, height);
    const textWidth = context.measureText(drawn.tag).width;
    context.fillStyle = "rgba(0, 0, 0, 0.9)";
    context.fillRect(x, y, textWidth + 6, 16);
    context.fillStyle = drawn.colour;
    context.fillText(drawn.tag, x + 3, y + 2);
  });
  layer.append(canvas);
}

// endregion: highlight layout

// endregion: highlights

// region: board panel

const board = { names: { parts: [], nets: [] }, partKeys: new Map(), sha: null, loading: false };
const register = { points: [], pick: null, down: null, timer: null };

function showBoardError(message) {
  const error = $("board-error");
  error.textContent = message ?? "";
  error.hidden = !message;
}

async function loadBoard() {
  let view;
  try {
    view = await api("GET", API.board);
  } catch (error) {
    $("board-info").textContent = error.message;
    return;
  }
  const summary = view.summary;
  $("board-info").textContent = summary
    ? `${summary.path.split("/").pop()} · ${summary.parts} parts · ${summary.nets} nets`
    : "no board";
  if (summary && !$("board-path").value) $("board-path").value = summary.path;
  const remote = $("board-remote");
  const remoteOther = view.remote && view.remote.path !== summary?.path;
  remote.hidden = !remoteOther;
  remote.textContent = remoteOther ? `${view.remote.origin} opened ${view.remote.path}. Press Open to use it here.` : "";
  if (remoteOther && !summary) $("board-path").value = view.remote.path;
  for (const element of $("board-search-form").elements) element.disabled = !summary;
  const registration = view.registration;
  $("board-registration").textContent = registration
    ? `${registration.side}, ${registration.refdes.length} parts${registration.stale ? " · stale: the board moved" : ""}`
    : "none";
  if (summary && summary.sha256 !== board.sha) await loadBoardNames(summary.sha256);
}

async function loadBoardNames(sha) {
  const names = await api("GET", API.boardNames);
  board.names = names;
  board.sha = sha;
  board.partKeys = new Map(names.parts.map((name) => [name.toLowerCase(), name]));
  const options = [...names.parts, ...names.nets].map((name) => {
    const option = document.createElement("option");
    option.value = name;
    return option;
  });
  $("board-names").replaceChildren(...options);
}

async function openBoard(event) {
  event.preventDefault();
  const button = event.submitter;
  button.disabled = true;
  showBoardError(null);
  try {
    await api("POST", API.boardOpen, { path: $("board-path").value.trim() });
    await loadBoard();
  } catch (error) {
    showBoardError(error.message);
  } finally {
    button.disabled = false;
  }
}

function fact(list, name, value) {
  const term = document.createElement("dt");
  term.textContent = name;
  const detail = document.createElement("dd");
  detail.textContent = value;
  list.append(term, detail);
}

function showSearch(result) {
  const facts = $("board-facts");
  facts.replaceChildren();
  if (result.part) {
    const part = result.part;
    fact(facts, "Part", part.name + (part.mfgcode ? ` (${part.mfgcode})` : ""));
    fact(facts, "Side", part.side);
    fact(facts, "Position", `x ${part.x_mm} mm, y ${part.y_mm} mm`);
    fact(facts, "Pins", String(part.pin_count));
    const more = part.nets_total > part.nets.length ? ` … (${part.nets_total})` : "";
    fact(facts, "Nets", part.nets.join(", ") + more);
  }
  if (result.net) {
    const net = result.net;
    fact(facts, "Net", net.name);
    fact(facts, "Parts", `${net.part_count} (${net.pin_count} pins): ${net.parts.join(", ")}`);
    fact(facts, "Test points", net.test_points.join(", ") || "none");
  }
  if (result.others.length) fact(facts, "Also", result.others.join(", "));
  if (result.message) fact(facts, "Note", result.message);
  const highlight = $("board-highlight");
  highlight.textContent = result.highlight ? `Camera: ${result.highlight.message}` : "";
  highlight.classList.toggle("shown", Boolean(result.highlight?.shown));
  const render = $("board-render");
  render.hidden = !result.render_call_id;
  if (result.render_call_id) render.src = API.callImage(result.render_call_id, 0);
  $("board-result").hidden = false;
}

async function searchBoard(event) {
  event.preventDefault();
  const button = event.submitter;
  const query = $("board-query").value.trim();
  if (!query) return;
  button.disabled = true;
  showBoardError(null);
  try {
    showSearch(await api("POST", API.boardSearch, { query }));
  } catch (error) {
    showBoardError(error.message);
  } finally {
    button.disabled = false;
  }
}

function showRegisterPoints() {
  const list = $("register-points");
  list.replaceChildren(
    ...register.points.map((point) => {
      const item = document.createElement("li");
      item.textContent = `${point.refdes} at ${(point.x * 100).toFixed(1)}%, ${(point.y * 100).toFixed(1)}%`;
      return item;
    }),
  );
  $("register-undo").disabled = register.points.length === 0;
  $("register-send").disabled = register.points.length < REGISTER_MIN_POINTS;
  $("snapshot").classList.toggle("picking", Boolean(register.pick));
  drawHighlights();
}

function startPick() {
  const typed = $("board-query").value.trim().toLowerCase();
  const refdes = board.partKeys.get(typed);
  const status = $("register-status");
  if (!refdes) {
    status.textContent = "Type a part name of the open board in the search box first.";
    return;
  }
  if ($("snapshot-view").hidden) {
    status.textContent = "Take a phone snapshot first.";
    return;
  }
  register.pick = refdes;
  status.textContent = `Click the center of ${refdes} on the snapshot.`;
  showRegisterPoints();
}

function placePoint(x, y) {
  if (!register.pick) return;
  register.points.push({ refdes: register.pick, x, y });
  $("register-status").textContent = `${register.pick} placed. Pick the next part.`;
  register.pick = null;
  showRegisterPoints();
}

function showTracking(phone) {
  const text = { following: "live tracking: following the board", lost: "live tracking lost: register again" };
  $("board-tracking").textContent = text[phone.tracking] ?? "";
}

function setupSnapshotPick() {
  const img = $("snapshot");
  img.addEventListener("pointerdown", (event) => {
    register.down = { x: event.clientX, y: event.clientY };
  });
  img.addEventListener("click", (event) => {
    clearTimeout(register.timer);
    const down = register.down;
    // The second click of a double-click: full screen, not a point.
    if (!register.pick || !down || event.detail > 1) return;
    if (Math.hypot(event.clientX - down.x, event.clientY - down.y) > PICK_MAX_MOVE_PX) return;
    const picture = snapshotPicture();
    const x = (event.clientX - picture.left) / picture.width;
    const y = (event.clientY - picture.top) / picture.height;
    if (x < 0 || y < 0 || x > 1 || y > 1) return;
    register.timer = setTimeout(() => placePoint(x, y), PICK_CLICK_DELAY_MS);
  });
  img.addEventListener("dblclick", () => clearTimeout(register.timer));
}

async function sendRegistration(event) {
  const button = event.currentTarget;
  const status = $("register-status");
  button.disabled = true;
  try {
    const result = await api("POST", API.boardRegister, { side: $("register-side").value, points: register.points });
    const check = result.checked ? "checked" : "exact fit of 4 parts: add a 5th part to check it";
    register.points = [];
    showRegisterPoints();
    await loadBoard();
    status.textContent = `Registered: error ${result.rms_error_px} px (max ${result.max_error_px} px), ${check}.`;
  } catch (error) {
    status.textContent = error.message;
    button.disabled = false;
  }
}

function boardCallFinished(call) {
  if (!call.tool.startsWith(BOARD_TOOL_PREFIX) || call.status === "running" || board.loading) return;
  board.loading = true;
  loadBoard().finally(() => {
    board.loading = false;
  });
}

function setupBoard() {
  $("board-open-form").addEventListener("submit", openBoard);
  $("board-search-form").addEventListener("submit", searchBoard);
  $("register-pick").addEventListener("click", startPick);
  $("register-undo").addEventListener("click", () => {
    register.points.pop();
    showRegisterPoints();
  });
  $("register-send").addEventListener("click", sendRegistration);
  setupSnapshotPick();
  loadBoard();
}

// endregion: board panel

// region: staged captures

// Space (also a USB foot pedal that sends Space) and C capture; never in a text field or with a modifier.
const STAGED_KEYS = new Set([" ", "c", "C"]);
const STAGED_FLASH_MS = 250;
// The capture sound plays only after the server took the capture (202); a refused or failed capture has another one.
const STAGED_BEEP = { frequency: 880, seconds: 0.08, gain: 0.15, type: "sine" };
const STAGED_ERROR_BEEP = { frequency: 220, seconds: 0.4, gain: 0.2, type: "square" };
// The server answers a capture at once (the photo and the reading follow): no answer in this time is an error.
const STAGED_POST_TIMEOUT_MS = 10000;
const STAGED_SENDING = "sending…";
// A UUID (version 4) for each key press, from crypto.getRandomValues (crypto.randomUUID needs https or localhost).
const UUID_BYTES = 16;
const UUID_VERSION = { index: 6, mask: 0x0f, bits: 0x40 };
const UUID_VARIANT = { index: 8, mask: 0x3f, bits: 0x80 };
const UUID_GROUPS = [8, 4, 4, 4, 12];
const HEX = 16;
const staged = { audio: null, flashTimer: null, rendered: new Map() };

function stagedKey(event) {
  if (!STAGED_KEYS.has(event.key) || event.ctrlKey || event.metaKey || event.altKey) return false;
  return !event.target.closest?.(FORM_FIELDS) && !event.target.isContentEditable;
}

function stagedBeep(tone) {
  try {
    staged.audio ??= new AudioContext();
    const oscillator = staged.audio.createOscillator();
    const gain = staged.audio.createGain();
    oscillator.type = tone.type;
    oscillator.frequency.value = tone.frequency;
    gain.gain.value = tone.gain;
    oscillator.connect(gain).connect(staged.audio.destination);
    oscillator.start();
    oscillator.stop(staged.audio.currentTime + tone.seconds);
  } catch {
    // No sound (no audio device, or not allowed yet): the flash still shows.
  }
}

function stagedFlash() {
  const flash = $("staged-flash");
  flash.hidden = false;
  flash.classList.remove("on");
  void flash.offsetWidth; // restart the animation
  flash.classList.add("on");
  clearTimeout(staged.flashTimer);
  staged.flashTimer = setTimeout(() => {
    flash.hidden = true;
  }, STAGED_FLASH_MS);
}

function stagedMessage(text, isError = false) {
  const message = $("staged-message");
  message.textContent = text;
  message.classList.toggle("error", isError);
  if (text) $("staged-panel").hidden = false;
}

function newRequestId() {
  const bytes = crypto.getRandomValues(new Uint8Array(UUID_BYTES));
  bytes[UUID_VERSION.index] = (bytes[UUID_VERSION.index] & UUID_VERSION.mask) | UUID_VERSION.bits;
  bytes[UUID_VARIANT.index] = (bytes[UUID_VARIANT.index] & UUID_VARIANT.mask) | UUID_VARIANT.bits;
  const hex = [...bytes].map((byte) => byte.toString(HEX).padStart(2, "0")).join("");
  let start = 0;
  return UUID_GROUPS.map((length) => hex.slice(start, (start += length))).join("-");
}

// The list itself comes as a `staged` event (the server pushes each change). The request id is new for each key
// press: when the browser sends the same POST again (a connection that closed with no answer, for example through a
// tunnel), the server gives the first answer and takes no second capture.
async function stagedCapture() {
  stagedFlash();
  stagedMessage(STAGED_SENDING);
  try {
    await api("POST", API.staged, { request_id: newRequestId() }, STAGED_POST_TIMEOUT_MS);
    stagedBeep(STAGED_BEEP);
    if ($("staged-message").textContent === STAGED_SENDING) stagedMessage("");
  } catch (error) {
    stagedBeep(STAGED_ERROR_BEEP);
    const reason = error.name === "TimeoutError" ? `no answer in ${STAGED_POST_TIMEOUT_MS / 1000} s` : error.message;
    stagedMessage(`Capture failed: ${reason}`, true);
  }
}

function stagedItem(capture) {
  const item = document.createElement("li");
  item.dataset.captureId = capture.capture_id;
  item.classList.toggle("failed", capture.state === "failed");
  if (capture.has_photo) {
    const photo = document.createElement("img");
    photo.src = API.stagedPhoto(capture.capture_id);
    photo.alt = "The phone photo of this capture";
    item.append(photo);
  }
  if (capture.has_meter_image) {
    const meter = document.createElement("img");
    meter.className = "staged-meter";
    meter.src = API.stagedMeter(capture.capture_id);
    meter.alt = "The meter image of this reading (the crop box)";
    item.append(meter);
  }
  const text = document.createElement("div");
  const time = document.createElement("div");
  time.textContent = new Date(capture.captured_at).toLocaleTimeString();
  const value = document.createElement("div");
  value.className = "staged-value";
  value.textContent = capture.state === "pending"
    ? "reading…"
    : capture.meter_text ? `${capture.meter_text} (${capture.meter_status})` : "no meter value";
  text.append(time, value);
  if (capture.bench_notice) {
    const bench = document.createElement("div");
    bench.className = "staged-bench";
    bench.textContent = capture.bench_notice;
    bench.title = capture.bench_notice;
    text.append(bench);
  }
  if (capture.notes.length) {
    const notes = document.createElement("div");
    notes.className = "staged-notes";
    notes.textContent = capture.notes.join(" · ");
    text.append(notes);
  }
  item.append(text, deviceButton("×", () => deleteStaged(capture.capture_id), false, "Remove this capture"));
  return item;
}

function showStaged(list) {
  const captures = list.captures ?? [];
  $("staged-count").textContent = `${captures.length} staged`;
  $("staged-count").classList.toggle("mcp", captures.length > 0);
  $("staged-panel").hidden = captures.length === 0 && !$("staged-message").textContent;
  // Keep the items that did not change, so their photos do not load again.
  const items = captures.map((capture) => {
    const key = [capture.state, capture.has_photo, capture.has_meter_image, capture.meter_text, ...capture.notes].join("|");
    const known = staged.rendered.get(capture.capture_id);
    if (known?.key === key) return known.item;
    const item = stagedItem(capture);
    staged.rendered.set(capture.capture_id, { key, item });
    return item;
  });
  const ids = new Set(captures.map((capture) => capture.capture_id));
  for (const id of staged.rendered.keys()) if (!ids.has(id)) staged.rendered.delete(id);
  $("staged-list").replaceChildren(...items);
}

// At start and after each reconnect of the event stream; between them, `staged` events carry the changes.
async function loadStaged() {
  try {
    showStaged(await api("GET", API.staged));
  } catch (error) {
    stagedMessage(error.message, true);
  }
}

async function deleteStaged(id) {
  try {
    await api("DELETE", API.stagedOne(id));
  } catch (error) {
    stagedMessage(error.message, true);
  }
}

function setupStaged() {
  $("staged-capture").addEventListener("click", stagedCapture);
  $("staged-clear").addEventListener("click", async () => {
    try {
      await api("DELETE", API.staged);
    } catch (error) {
      stagedMessage(error.message, true);
    }
  });
  document.addEventListener("keydown", (event) => {
    if (!stagedKey(event)) return;
    // Also for a focused button: Space captures, it does not click the button or scroll the page.
    event.preventDefault();
    if (!event.repeat) stagedCapture();
  });
  document.addEventListener("keyup", (event) => {
    if (stagedKey(event)) event.preventDefault();
  });
}

// endregion: staged captures

// region: devices

const APP_TEXT = { true: "installed", false: "app not installed", null: "unknown" };
const SELECTED_FROM_TEXT = { page: "selected here", config: "from the config", none: "no choice" };

function deviceButton(label, onClick, disabled = false, title = "") {
  const button = document.createElement("button");
  button.type = "button";
  button.textContent = label;
  button.disabled = disabled;
  button.title = title;
  button.addEventListener("click", onClick);
  return button;
}

function showDevices(list) {
  const selected = list.selected_serial;
  $("devices-selected").textContent = selected
    ? `${selected} (${SELECTED_FROM_TEXT[list.selected_from]}${list.selected_gone ? ", gone" : ""})`
    : SELECTED_FROM_TEXT.none;
  const rows = list.devices.map((device) => {
    const row = document.createElement("tr");
    row.classList.toggle("selected", device.selected);
    const app = device.note && device.app_installed !== true ? device.note : APP_TEXT[device.app_installed];
    for (const text of [device.serial, device.transport, device.state, device.model || "–", app]) {
      const cell = document.createElement("td");
      cell.textContent = text;
      row.append(cell);
    }
    const actions = document.createElement("td");
    actions.className = "actions";
    const ready = device.state === "device";
    actions.append(
      deviceButton(device.selected ? "In use" : "Use this phone", () => deviceAction(API.devicesSelect, { serial: device.serial }),
        device.selected || !ready),
    );
    if (device.transport === "usb" && ready) {
      actions.append(
        deviceButton("Switch to Wi-Fi", () => deviceAction(API.devicesWifi, { serial: device.serial }), false,
          "adb tcpip 5555 on this phone, then adb connect over Wi-Fi, then use the Wi-Fi serial"),
      );
    }
    row.append(actions);
    return row;
  });
  if (list.selected_gone) rows.unshift(goneRow(list));
  $("devices-table").tBodies[0].replaceChildren(...rows);
  showNetwork(list);
}

// The selected phone is not in adb devices: it stays selected until the user decides, but it is not connected.
function goneRow(list) {
  const row = document.createElement("tr");
  row.classList.add("selected", "gone");
  const link = /^\d{1,3}(\.\d{1,3}){3}:\d+$/.test(list.selected_serial) ? "wifi" : "usb";
  const note = "not in adb devices: connect it again (Found on the network, Pair, or Connect), or Disconnect";
  for (const text of [list.selected_serial, link, "gone", "–", note]) {
    const cell = document.createElement("td");
    cell.textContent = text;
    row.append(cell);
  }
  const actions = document.createElement("td");
  actions.className = "actions";
  actions.append(deviceButton("Disconnect", () => deviceAction(API.devicesDisconnect), false,
    "Stop the phone and clear the selection (the phone is gone)"));
  row.append(actions);
  return row;
}

// Wireless-debugging phones that the server found on the network (zeroconf or avahi-browse). The user chooses:
// Connect runs adb connect; Pair only fills the pair form (the user types the code from the phone).
function showNetwork(list) {
  const network = list.network ?? [];
  $("devices-network").hidden = network.length === 0;
  const rows = network.map((candidate) => {
    const row = document.createElement("tr");
    const inAdb = candidate.adb_serial ?? "–";
    for (const text of [candidate.name, candidate.model || "–", candidate.address, candidate.kind, inAdb]) {
      const cell = document.createElement("td");
      cell.textContent = text;
      row.append(cell);
    }
    const actions = document.createElement("td");
    actions.className = "actions";
    if (candidate.kind === "connect") {
      actions.append(
        deviceButton(candidate.adb_serial ? "Connected" : "Connect", () => deviceAction(API.devicesConnect, { address: candidate.address }),
          Boolean(candidate.adb_serial), `adb connect ${candidate.address}`),
      );
    } else {
      actions.append(
        deviceButton("Pair", () => fillPairForm(candidate, network), false,
          "Fill the pair form with this address; then type the 6-digit code that the phone shows"),
      );
    }
    row.append(actions);
    return row;
  });
  $("devices-network-table").tBodies[0].replaceChildren(...rows);
  // adb's own mDNS note only when the server's discovery did not help either.
  const mdns = list.mdns.map((service) => `${service.name} ${service.address}`).join(", ");
  const notes = [];
  if (network.length === 0 && list.network_note) notes.push(`Network discovery: ${list.network_note}`);
  if (network.length === 0 && list.mdns_note) notes.push(`adb mDNS: ${list.mdns_note}`);
  if (!list.mdns_note && mdns) notes.push(`adb mDNS (not connected): ${mdns}`);
  $("devices-mdns").textContent = notes.join(" · ");
}

function fillPairForm(candidate, network) {
  $("pair-address").value = candidate.address;
  // The connect address of the same phone, when the network list has it.
  const connect = network.find((other) => other.kind === "connect" && other.host === candidate.host);
  if (connect) $("pair-connect").value = connect.address;
  $("pair-code").value = "";
  $("pair-code").focus();
}

function showSteps(steps) {
  $("devices-steps").replaceChildren(
    ...steps.map((step) => {
      const item = document.createElement("li");
      item.textContent = `${step.step}: ${step.ok ? step.detail || "ok" : step.detail}`;
      item.classList.toggle("failed", !step.ok);
      return item;
    }),
  );
}

async function loadDevices() {
  try {
    showDevices(await api("GET", API.devices));
  } catch (error) {
    showSteps([{ step: "list", ok: false, detail: error.message }]);
  }
}

async function deviceAction(url, body) {
  const panel = $("phone-devices");
  for (const button of panel.querySelectorAll("button")) button.disabled = true;
  showSteps([{ step: "working", ok: true, detail: "…" }]);
  try {
    const result = await api("POST", url, body);
    showSteps(result.steps);
    showDevices(result.devices);
  } catch (error) {
    showSteps([{ step: "request", ok: false, detail: error.message }]);
    await loadDevices();
  } finally {
    for (const button of panel.querySelectorAll("#devices-refresh, #devices-clear, form button")) button.disabled = false;
  }
}

function setupDevices() {
  $("phone-devices").addEventListener("toggle", (event) => {
    if (event.currentTarget.open) loadDevices();
  });
  $("devices-refresh").addEventListener("click", loadDevices);
  $("devices-clear").addEventListener("click", () => deviceAction(API.devicesDisconnect));
  $("devices-connect").addEventListener("submit", (event) => {
    event.preventDefault();
    deviceAction(API.devicesConnect, { address: $("connect-address").value.trim() });
  });
  $("devices-pair").addEventListener("submit", (event) => {
    event.preventDefault();
    deviceAction(API.devicesPair, {
      pair_address: $("pair-address").value.trim(),
      code: $("pair-code").value.trim(),
      connect_address: $("pair-connect").value.trim(),
    });
  });
}

// endregion: devices

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
  // One event stream for all live data. At start and after a reconnect, the page loads what it can have missed.
  source.addEventListener("open", () => {
    link.textContent = "live";
    link.className = "badge ok";
    loadStaged();
    refreshWebcamInfo();
  });
  source.addEventListener("error", () => {
    link.textContent = "disconnected";
    link.className = "badge error";
  });
  source.addEventListener("call", (event) => {
    const call = JSON.parse(event.data);
    renderCall(call);
    boardCallFinished(call);
    showAnnotated(call);
  });
  // After a reconnect, another code version means a new server (dev reload): load the new page.
  source.addEventListener("version", (event) => {
    const { version } = JSON.parse(event.data);
    if (state.version === null) state.version = version;
    else if (version !== state.version) window.location.reload();
  });
  source.addEventListener("phone", (event) => applyPhone(JSON.parse(event.data)));
  source.addEventListener("staged", (event) => showStaged(JSON.parse(event.data)));
  source.addEventListener("webcam", (event) => applyWebcamInfo(JSON.parse(event.data)));
}

async function loadState() {
  const view = await api("GET", API.state);
  state.version = view.version;
  applySettings(view.settings);
  applyPhone(view.phone);
  if (view.webcam?.width) state.frame = { width: view.webcam.width, height: view.webcam.height };
  for (const call of view.calls) {
    renderCall(call);
    showAnnotated(call);
  }
}

// endregion: live events

async function main() {
  setupPhone();
  setupSettings();
  setupBench();
  setupFullscreen();
  setupHiddenTab();
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
