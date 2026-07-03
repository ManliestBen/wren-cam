// wren-cam single-page UI.

const $ = (sel, root = document) => root.querySelector(sel);
const $$ = (sel, root = document) => Array.from(root.querySelectorAll(sel));

const RESOLUTIONS = [
  [640, 480], [1280, 720], [1640, 1232], [1920, 1080],
  [2304, 1296], [3840, 2160], [4608, 2592],
];

let cfg = null;
let statusTimer = null;
let isAdmin = false;

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

// api() throws Error("<status>: <body>"); pull out the server's detail message.
function errDetail(e) {
  const m = String(e.message).match(/^\d+:\s*([\s\S]*)$/);
  if (!m) return e.message;
  try {
    return JSON.parse(m[1]).detail || m[1];
  } catch {
    return m[1];
  }
}

// ---- auth ----
function updateAuthUI() {
  document.body.classList.toggle("is-admin", isAdmin);
  const btn = $("#auth-btn");
  if (btn) btn.textContent = isAdmin ? "Logout" : "Login";
  const state = $("#auth-state");
  if (state) state.textContent = isAdmin ? "Admin" : "";
}

async function refreshSession() {
  try {
    const s = await api("/api/session");
    isAdmin = !!s.admin;
  } catch {
    isAdmin = false;
  }
  updateAuthUI();
}

function openLoginModal() {
  const m = $("#login-modal");
  $("#login-password").value = "";
  m.hidden = false;
  $("#login-password").focus();
}

function closeLoginModal() {
  $("#login-modal").hidden = true;
}

async function doLogin() {
  const password = $("#login-password").value;
  try {
    await api("/api/login", { method: "POST", body: JSON.stringify({ password }) });
    isAdmin = true;
    updateAuthUI();
    closeLoginModal();
    toast("Logged in as admin");
    // Re-render whatever is on screen so admin-only controls appear.
    renderLive();
    if ($("#tab-settings").classList.contains("active")) renderSettings();
  } catch (e) {
    toast("Login failed: " + errDetail(e));
  }
}

async function doLogout() {
  try {
    await api("/api/logout", { method: "POST" });
  } catch {
    /* ignore */
  }
  isAdmin = false;
  updateAuthUI();
  toast("Logged out");
  renderLive();
  if ($("#tab-settings").classList.contains("active")) renderSettings();
}

$("#auth-btn").onclick = () => (isAdmin ? doLogout() : openLoginModal());
$("#login-submit").onclick = doLogin;
$("#login-cancel").onclick = closeLoginModal;
$("#login-password").addEventListener("keydown", (e) => {
  if (e.key === "Enter") doLogin();
  if (e.key === "Escape") closeLoginModal();
});
$("#login-modal").addEventListener("click", (e) => {
  if (e.target.id === "login-modal") closeLoginModal();
});

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
      <div class="actions">
        <button class="secondary admin-only" data-snap="${cam.id}">Snapshot</button>
      </div>
    `;
    card.querySelector(`[data-snap="${cam.id}"]`).onclick = async (ev) => {
      const btn = ev.currentTarget;
      btn.disabled = true;
      const orig = btn.textContent;
      btn.textContent = "Saving…";
      try {
        const res = await api(`/api/cameras/${cam.id}/snapshot`, { method: "POST" });
        toast(`Saved ${res.name}`);
      } catch (e) {
        toast("Snapshot failed: " + e.message);
      } finally {
        btn.disabled = false;
        btn.textContent = orig;
      }
    };
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
    <div class="panel viewer-only">
      <p style="margin:0;color:var(--muted)">
        You're viewing as a guest. <a href="#" id="settings-login" style="color:var(--accent)">Log in as admin</a>
        to change settings, take snapshots, or delete recordings.
      </p>
    </div>

    <div class="panel">
      <h2>Storage</h2>
      <div id="storage-info" style="color:var(--muted)">Loading…</div>
      <div id="storage-bar" class="storage-bar" hidden><span></span></div>
      <div class="admin-only" style="margin-top:0.75rem">
        <button class="danger" id="delete-all">Delete ALL recordings &amp; snapshots</button>
      </div>
    </div>

    <div class="panel">
      <h2>App</h2>
      <div class="field"><label>Recordings directory</label>
        <input type="text" id="app-recdir" value="${cfg.recordings_dir}" /></div>
      <div class="field"><label>Stream JPEG quality (1-100)</label>
        <input type="number" id="app-quality" min="1" max="100" value="${cfg.stream_quality}" /></div>
      <div class="field"><label>Stream max framerate</label>
        <input type="number" id="app-maxrate" min="1" max="60" value="${cfg.stream_maxrate}" /></div>
      <div class="field"><label>Reserve free space (MB)</label>
        <input type="number" id="app-minfree" min="50" max="1000000" value="${cfg.min_free_mb}" /></div>
      <div class="field"><label>Auto-delete recordings after (days)</label>
        <input type="number" id="app-retention" min="0" max="3650" value="${cfg.retention_days}" /></div>
      <p style="margin:0.25rem 0 0.75rem;color:var(--muted);font-size:0.85rem">
        New clips &amp; snapshots stop once free space would drop below the reserve.
        Auto-delete removes media older than the given number of days (0 = keep forever).
      </p>
      <button id="save-app" class="admin-only">Save app settings</button>
    </div>
  `;
  $("#save-app").onclick = saveApp;
  $("#delete-all").onclick = deleteAllRecordings;
  const loginLink = $("#settings-login");
  if (loginLink) loginLink.onclick = (e) => { e.preventDefault(); openLoginModal(); };

  loadStorage();

  const cams = $("#camera-settings");
  cams.innerHTML = "";
  cfg.cameras.forEach((cam) => cams.appendChild(renderCameraPanel(cam)));

  // Change-password panel lives at the very bottom of the settings page.
  const admin = $("#admin-settings");
  admin.innerHTML = `
    <div class="panel admin-only">
      <h2>Change admin password</h2>
      <div class="field"><label>Current password</label>
        <input type="password" id="pw-current" autocomplete="current-password" /></div>
      <div class="field"><label>New password</label>
        <input type="password" id="pw-new" autocomplete="new-password" /></div>
      <div class="field"><label>Confirm new password</label>
        <input type="password" id="pw-confirm" autocomplete="new-password" /></div>
      <button id="save-password">Change password</button>
    </div>
  `;
  $("#save-password").onclick = savePassword;
}

async function loadStorage() {
  const info = $("#storage-info");
  const bar = $("#storage-bar");
  if (!info) return;
  try {
    const s = await api("/api/storage");
    const usedPct = s.total ? Math.round((s.used / s.total) * 100) : 0;
    info.innerHTML =
      `<strong>${formatBytes(s.free)}</strong> free of ${formatBytes(s.total)} ` +
      `(${usedPct}% used)`;
    if (bar) {
      bar.hidden = false;
      const span = bar.querySelector("span");
      span.style.width = usedPct + "%";
      span.classList.toggle("full", usedPct >= 90);
    }
  } catch (e) {
    info.textContent = "Storage info unavailable";
    if (bar) bar.hidden = true;
  }
}

async function savePassword() {
  const current_password = $("#pw-current").value;
  const new_password = $("#pw-new").value;
  const confirm2 = $("#pw-confirm").value;
  if (new_password.length < 4) {
    toast("New password must be at least 4 characters");
    return;
  }
  if (new_password !== confirm2) {
    toast("New passwords don't match");
    return;
  }
  try {
    await api("/api/admin/password", {
      method: "POST",
      body: JSON.stringify({ current_password, new_password }),
    });
    toast("Password changed");
    $("#pw-current").value = $("#pw-new").value = $("#pw-confirm").value = "";
  } catch (e) {
    toast("Change failed: " + e.message);
  }
}

async function deleteAllRecordings() {
  if (!confirm("Delete ALL recordings and snapshots on the device? This cannot be undone.")) return;
  try {
    const r = await api("/api/recordings", { method: "DELETE" });
    toast(`Deleted ${r.deleted} item${r.deleted === 1 ? "" : "s"}`);
    loadStorage();
    if ($("#tab-recordings").classList.contains("active")) loadRecordings();
  } catch (e) {
    toast("Delete failed: " + e.message);
  }
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
    <div class="field"><label>Rotate 180° (upside-down mount)</label>
      <input type="checkbox" data-k="rotate_180" ${cam.rotate_180 ? "checked" : ""} /></div>
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
    <button class="admin-only" data-act="save">Save</button>
    <button class="secondary admin-only" data-act="restart">Restart camera</button>
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
    min_free_mb: parseInt($("#app-minfree").value, 10),
    retention_days: parseInt($("#app-retention").value, 10),
  };
  try {
    cfg = await api("/api/config", { method: "PATCH", body: JSON.stringify(body) });
    toast("App settings saved");
    loadStorage();
  } catch (e) {
    toast("Save failed: " + errDetail(e));
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
const REC_PAGE_SIZE = 12;
const recState = { date: "", offset: 0 };

// Reload the date dropdown, keeping the current selection if it still exists,
// otherwise defaulting to the newest date.
async function loadRecordingDates() {
  const sel = $("#recordings-date");
  const dates = await api("/api/recordings/dates");
  sel.innerHTML = "";
  if (!dates.length) {
    recState.date = "";
    sel.innerHTML = `<option value="">No recordings</option>`;
    sel.disabled = true;
    return;
  }
  sel.disabled = false;
  dates.forEach((d) => {
    const opt = document.createElement("option");
    opt.value = d.date;
    opt.textContent = `${d.date} (${d.count})`;
    sel.appendChild(opt);
  });
  if (!dates.some((d) => d.date === recState.date)) {
    recState.date = dates[0].date;
  }
  sel.value = recState.date;
}

function renderRecording(f) {
  const li = document.createElement("li");
  li.className = "recording";
  const url = `/api/recordings/${encodeURIComponent(f.name)}`;
  const media = f.kind === "photo"
    ? `<img class="snapshot" loading="lazy" src="${url}" alt="${f.name}" />`
    : `<video controls preload="none" src="${url}"></video>`;
  li.innerHTML = `
    <header>
      <strong>${f.name}</strong>
      <span>${formatBytes(f.size)} · ${formatTime(f.modified)}</span>
    </header>
    ${media}
    <div class="row" style="margin-top:0.5rem">
      <a href="${url}" download><button class="secondary">Download</button></a>
      <button class="danger admin-only" data-name="${f.name}">Delete</button>
    </div>
  `;
  li.querySelector(".danger").onclick = async () => {
    if (!confirm(`Delete ${f.name}?`)) return;
    try {
      await api(`/api/recordings/${encodeURIComponent(f.name)}`, { method: "DELETE" });
      toast("Deleted");
      await loadRecordingDates();
      renderRecordingsPage();
    } catch (e) {
      toast("Delete failed: " + e.message);
    }
  };
  return li;
}

// Fetch and render the current page for the selected date.
async function renderRecordingsPage() {
  const list = $("#recordings-list");
  const count = $("#recordings-count");
  const pager = $("#recordings-pager");
  list.innerHTML = "Loading…";
  if (!recState.date) {
    list.innerHTML = "<li>No recordings yet.</li>";
    count.textContent = "0 files";
    pager.hidden = true;
    return;
  }
  try {
    const params = new URLSearchParams({
      date: recState.date,
      limit: REC_PAGE_SIZE,
      offset: recState.offset,
    });
    const { items, total, offset, limit } = await api(`/api/recordings?${params}`);
    // The page can fall past the end (e.g. after deletes) — step back.
    if (!items.length && offset > 0 && total > 0) {
      recState.offset = Math.max(0, offset - limit);
      return renderRecordingsPage();
    }
    count.textContent = `${total} file${total === 1 ? "" : "s"} on ${recState.date}`;
    list.innerHTML = "";
    items.forEach((f) => list.appendChild(renderRecording(f)));

    pager.hidden = total <= limit;
    $("#recordings-page").textContent = total
      ? `${offset + 1}–${offset + items.length} of ${total}`
      : "";
    $("#recordings-prev").disabled = offset === 0;
    $("#recordings-next").disabled = offset + limit >= total;
  } catch (e) {
    list.innerHTML = `<li>Error: ${e.message}</li>`;
    pager.hidden = true;
  }
}

// Full refresh: reload available dates, reset to the first page.
async function loadRecordings() {
  await loadRecordingDates();
  recState.offset = 0;
  renderRecordingsPage();
}

$("#refresh-recordings").onclick = loadRecordings;
$("#delete-day").onclick = async () => {
  if (!recState.date) return;
  if (!confirm(`Delete ALL recordings and snapshots for ${recState.date}? This cannot be undone.`)) return;
  try {
    const params = new URLSearchParams({ date: recState.date });
    const r = await api(`/api/recordings?${params}`, { method: "DELETE" });
    toast(`Deleted ${r.deleted} item${r.deleted === 1 ? "" : "s"}`);
    await loadRecordings();
  } catch (e) {
    toast("Delete failed: " + e.message);
  }
};
$("#recordings-date").onchange = (e) => {
  recState.date = e.target.value;
  recState.offset = 0;
  renderRecordingsPage();
};
$("#recordings-prev").onclick = () => {
  recState.offset = Math.max(0, recState.offset - REC_PAGE_SIZE);
  renderRecordingsPage();
};
$("#recordings-next").onclick = () => {
  recState.offset += REC_PAGE_SIZE;
  renderRecordingsPage();
};

// ---- bootstrap ----
(async () => {
  try {
    await refreshSession();
    cfg = await api("/api/config");
    renderLive();
    pollStatus();
    statusTimer = setInterval(pollStatus, 2000);
  } catch (e) {
    toast("Failed to load config: " + e.message);
  }
})();
