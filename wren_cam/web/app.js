// wren-cam single-page UI.

const $ = (sel, root = document) => root.querySelector(sel);
const $$ = (sel, root = document) => Array.from(root.querySelectorAll(sel));

const RESOLUTIONS = [
  [640, 480], [1280, 720], [1640, 1232], [1920, 1080],
  [2304, 1296], [3840, 2160], [4608, 2592],
];

let cfg = null;
let statusTimer = null;

function toast(msg) {
  const el = $("#toast");
  el.textContent = msg;
  el.classList.add("show");
  clearTimeout(toast._t);
  toast._t = setTimeout(() => el.classList.remove("show"), 2200);
}

async function api(path, opts = {}) {
  const res = await fetch(path, {
    headers: { "Content-Type": "application/json" },
    ...opts,
  });
  if (!res.ok) {
    const text = await res.text();
    throw new Error(`${res.status}: ${text}`);
  }
  if (res.status === 204) return null;
  return res.json();
}

function formatBytes(n) {
  if (n < 1024) return n + " B";
  if (n < 1024 ** 2) return (n / 1024).toFixed(1) + " KB";
  if (n < 1024 ** 3) return (n / 1024 ** 2).toFixed(1) + " MB";
  return (n / 1024 ** 3).toFixed(2) + " GB";
}

function formatTime(epoch) {
  return new Date(epoch * 1000).toLocaleString();
}

// ---- tab switching ----
$$("nav button").forEach((btn) => {
  btn.onclick = () => {
    $$("nav button").forEach((b) => b.classList.toggle("active", b === btn));
    const id = btn.dataset.tab;
    $$(".tab").forEach((t) => t.classList.toggle("active", t.id === "tab-" + id));
    if (id === "recordings") loadRecordings();
    if (id === "settings") renderSettings();
  };
});

// ---- live view ----
function renderLive() {
  const grid = $("#cam-grid");
  grid.innerHTML = "";
  if (!cfg) return;
  cfg.cameras.forEach((cam) => {
    const card = document.createElement("div");
    card.className = "cam-card";
    card.innerHTML = `
      <img src="/stream/${cam.id}" alt="cam ${cam.id}" />
      <div class="meta">
        <span>${cam.name} — ${cam.width}×${cam.height} @ ${cam.framerate}fps</span>
        <span class="status" data-cam="${cam.id}"></span>
      </div>
    `;
    grid.appendChild(card);
  });
}

async function pollStatus() {
  try {
    const s = await api("/api/status");
    s.cameras.forEach((c) => {
      const el = document.querySelector(`.status[data-cam="${c.camera_id}"]`);
      if (!el) return;
      const bits = [];
      if (c.recording) bits.push('<span class="rec">● REC</span>');
      if (c.motion_active) bits.push("motion");
      bits.push(`Δ${c.last_changed_pixels}`);
      el.innerHTML = bits.join(" · ");
    });
  } catch (e) {
    /* silent */
  }
}

// ---- settings ----
function renderSettings() {
  if (!cfg) return;

  const app = $("#app-settings");
  app.innerHTML = `
    <div class="panel">
      <h2>App</h2>
      <div class="field"><label>Recordings directory</label>
        <input type="text" id="app-recdir" value="${cfg.recordings_dir}" /></div>
      <div class="field"><label>Stream JPEG quality (1-100)</label>
        <input type="number" id="app-quality" min="1" max="100" value="${cfg.stream_quality}" /></div>
      <div class="field"><label>Stream max framerate</label>
        <input type="number" id="app-maxrate" min="1" max="60" value="${cfg.stream_maxrate}" /></div>
      <button id="save-app">Save app settings</button>
    </div>
  `;
  $("#save-app").onclick = saveApp;

  const cams = $("#camera-settings");
  cams.innerHTML = "";
  cfg.cameras.forEach((cam) => cams.appendChild(renderCameraPanel(cam)));
}

function renderCameraPanel(cam) {
  const div = document.createElement("div");
  div.className = "panel";
  const resOptions = RESOLUTIONS.map(([w, h]) => {
    const sel = w === cam.width && h === cam.height ? " selected" : "";
    return `<option value="${w}x${h}"${sel}>${w}×${h}</option>`;
  }).join("");
  const isCustom = !RESOLUTIONS.some(([w, h]) => w === cam.width && h === cam.height);

  div.innerHTML = `
    <h2>Camera ${cam.id}</h2>
    <div class="field"><label>Name</label>
      <input type="text" data-k="name" value="${cam.name}" /></div>
    <div class="field"><label>Resolution</label>
      <select data-k="resolution">
        ${resOptions}
        <option value="custom"${isCustom ? " selected" : ""}>Custom…</option>
      </select></div>
    <div class="field"><label>Width</label>
      <input type="number" data-k="width" min="320" max="4608" value="${cam.width}" /></div>
    <div class="field"><label>Height</label>
      <input type="number" data-k="height" min="240" max="2592" value="${cam.height}" /></div>
    <div class="field"><label>Framerate</label>
      <input type="number" data-k="framerate" min="2" max="60" value="${cam.framerate}" /></div>
    <div class="field"><label>Autofocus</label>
      <select data-k="autofocus">
        <option value="continuous"${cam.autofocus === "continuous" ? " selected" : ""}>Continuous</option>
        <option value="manual"${cam.autofocus === "manual" ? " selected" : ""}>Manual</option>
      </select></div>
    <div class="field"><label>Lens position (manual)</label>
      <input type="number" data-k="lens_position" step="0.1" min="0" max="15" value="${cam.lens_position}" /></div>
    <hr style="border-color:#333"/>
    <div class="field"><label>Motion recording</label>
      <input type="checkbox" data-k="motion_enabled" ${cam.motion_enabled ? "checked" : ""} /></div>
    <div class="field"><label>Motion threshold (pixels)</label>
      <input type="number" data-k="motion_threshold" min="1" value="${cam.motion_threshold}" /></div>
    <div class="field"><label>Noise level (1-255)</label>
      <input type="number" data-k="noise_level" min="1" max="255" value="${cam.noise_level}" /></div>
    <div class="field"><label>Event gap (seconds)</label>
      <input type="number" data-k="event_gap_seconds" min="1" max="3600" value="${cam.event_gap_seconds}" /></div>
    <div class="field"><label>Max clip length (s, 0 = no cap)</label>
      <input type="number" data-k="max_clip_seconds" min="0" max="86400" value="${cam.max_clip_seconds}" /></div>
    <button data-act="save">Save</button>
    <button class="secondary" data-act="restart">Restart camera</button>
  `;

  const resSel = div.querySelector('[data-k="resolution"]');
  const wIn = div.querySelector('[data-k="width"]');
  const hIn = div.querySelector('[data-k="height"]');
  resSel.onchange = () => {
    if (resSel.value === "custom") return;
    const [w, h] = resSel.value.split("x").map(Number);
    wIn.value = w;
    hIn.value = h;
  };

  div.querySelector('[data-act="save"]').onclick = () => saveCamera(cam.id, div);
  div.querySelector('[data-act="restart"]').onclick = () => restartCamera(cam.id);
  return div;
}

async function saveApp() {
  const body = {
    recordings_dir: $("#app-recdir").value,
    stream_quality: parseInt($("#app-quality").value, 10),
    stream_maxrate: parseInt($("#app-maxrate").value, 10),
  };
  try {
    cfg = await api("/api/config", { method: "PATCH", body: JSON.stringify(body) });
    toast("App settings saved");
  } catch (e) {
    toast("Save failed: " + e.message);
  }
}

async function saveCamera(camId, panel) {
  const body = {};
  panel.querySelectorAll("[data-k]").forEach((el) => {
    const key = el.dataset.k;
    if (key === "resolution") return;
    if (el.type === "checkbox") body[key] = el.checked;
    else if (el.type === "number") body[key] = parseFloat(el.value);
    else body[key] = el.value;
  });
  try {
    await api(`/api/cameras/${camId}`, { method: "PATCH", body: JSON.stringify(body) });
    cfg = await api("/api/config");
    toast("Camera settings saved");
    renderLive();
  } catch (e) {
    toast("Save failed: " + e.message);
  }
}

async function restartCamera(camId) {
  try {
    await api(`/api/cameras/${camId}/restart`, { method: "POST" });
    toast("Camera restarted");
    renderLive();
  } catch (e) {
    toast("Restart failed: " + e.message);
  }
}

// ---- recordings ----
async function loadRecordings() {
  const list = $("#recordings-list");
  const count = $("#recordings-count");
  list.innerHTML = "Loading…";
  try {
    const files = await api("/api/recordings");
    count.textContent = `${files.length} file${files.length === 1 ? "" : "s"}`;
    list.innerHTML = "";
    files.forEach((f) => {
      const li = document.createElement("li");
      li.className = "recording";
      li.innerHTML = `
        <header>
          <strong>${f.name}</strong>
          <span>${formatBytes(f.size)} · ${formatTime(f.modified)}</span>
        </header>
        <video controls preload="none" src="/api/recordings/${encodeURIComponent(f.name)}"></video>
        <div class="row" style="margin-top:0.5rem">
          <a href="/api/recordings/${encodeURIComponent(f.name)}" download><button class="secondary">Download</button></a>
          <button class="danger" data-name="${f.name}">Delete</button>
        </div>
      `;
      li.querySelector(".danger").onclick = async () => {
        if (!confirm(`Delete ${f.name}?`)) return;
        try {
          await api(`/api/recordings/${encodeURIComponent(f.name)}`, { method: "DELETE" });
          toast("Deleted");
          loadRecordings();
        } catch (e) {
          toast("Delete failed: " + e.message);
        }
      };
      list.appendChild(li);
    });
  } catch (e) {
    list.innerHTML = `<li>Error: ${e.message}</li>`;
  }
}

$("#refresh-recordings").onclick = loadRecordings;

// ---- bootstrap ----
(async () => {
  try {
    cfg = await api("/api/config");
    renderLive();
    pollStatus();
    statusTimer = setInterval(pollStatus, 2000);
  } catch (e) {
    toast("Failed to load config: " + e.message);
  }
})();
