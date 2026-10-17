(() => {
  "use strict";

  const STORAGE_KEY = "rack.apps.v1";

  /** @typedef {{id:string, name:string, url:string, thumb:string|null}} AppEntry */

  /** @type {AppEntry[]} */
  let apps = loadApps();
  let editingId = null;
  let pendingThumb = null; // data URL staged in the form before save

  // ---- DOM refs ----------------------------------------------------------
  const grid = document.getElementById("grid");
  const emptyState = document.getElementById("emptyState");
  const clockEl = document.getElementById("clock");

  const overlay = document.getElementById("overlay");
  const panelTitle = document.getElementById("panelTitle");
  const openAddBtn = document.getElementById("openAddBtn");
  const closePanelBtn = document.getElementById("closePanelBtn");
  const cancelBtn = document.getElementById("cancelBtn");
  const deleteBtn = document.getElementById("deleteBtn");
  const form = document.getElementById("appForm");

  const fieldId = document.getElementById("appId");
  const fieldName = document.getElementById("fieldName");
  const fieldUrl = document.getElementById("fieldUrl");

  const dropzone = document.getElementById("dropzone");
  const dropzoneEmpty = document.getElementById("dropzoneEmpty");
  const thumbPreview = document.getElementById("thumbPreview");
  const fileInput = document.getElementById("fileInput");
  const resetThumbBtn = document.getElementById("resetThumbBtn");

  // ---- Storage ------------------------------------------------------------
  function loadApps() {
    try {
      const raw = localStorage.getItem(STORAGE_KEY);
      return raw ? JSON.parse(raw) : [];
    } catch (e) {
      console.error("Could not read saved apps:", e);
      return [];
    }
  }

  function saveApps() {
    try {
      localStorage.setItem(STORAGE_KEY, JSON.stringify(apps));
    } catch (e) {
      console.error("Could not save apps:", e);
      alert("Couldn't save — your browser storage may be full.");
    }
  }

  // ---- Helpers --------------------------------------------------------------
  function normalizeUrl(raw) {
    let url = raw.trim();
    if (!/^https?:\/\//i.test(url)) url = "http://" + url;
    return url;
  }

  function faviconFor(url) {
    try {
      const host = new URL(url).hostname;
      return `https://www.google.com/s2/favicons?sz=128&domain=${encodeURIComponent(host)}`;
    } catch {
      return null;
    }
  }

  function hostLabel(url) {
    try {
      const u = new URL(url);
      return u.hostname + (u.port ? ":" + u.port : "");
    } catch {
      return url;
    }
  }

  function uid() {
    return Date.now().toString(36) + Math.random().toString(36).slice(2, 8);
  }

  // ---- Rendering --------------------------------------------------------
  function render() {
    grid.innerHTML = "";
    emptyState.hidden = apps.length !== 0;

    for (const app of apps) {
      grid.appendChild(buildCard(app));
    }
  }

  function buildCard(app) {
    const card = document.createElement("div");
    card.className = "card";
    card.setAttribute("role", "button");
    card.setAttribute("tabindex", "0");
    card.title = `Open ${app.name}`;

    const thumbWrap = document.createElement("div");
    thumbWrap.className = "card-thumb" + (app.thumb ? " is-custom" : "");

    const img = document.createElement("img");
    img.src = app.thumb || faviconFor(app.url) || "";
    img.alt = "";
    img.loading = "lazy";
    img.onerror = () => { img.style.visibility = "hidden"; };
    thumbWrap.appendChild(img);

    const body = document.createElement("div");
    body.className = "card-body";
    const name = document.createElement("div");
    name.className = "card-name";
    name.textContent = app.name;
    const url = document.createElement("div");
    url.className = "card-url";
    url.textContent = hostLabel(app.url);
    body.append(name, url);

    const status = document.createElement("div");
    status.className = "card-status";

    const editBtn = document.createElement("button");
    editBtn.type = "button";
    editBtn.className = "card-edit";
    editBtn.textContent = "✎";
    editBtn.setAttribute("aria-label", `Edit ${app.name}`);
    editBtn.addEventListener("click", (e) => {
      e.stopPropagation();
      openPanel(app);
    });

    card.append(editBtn, status, thumbWrap, body);

    const open = () => window.open(app.url, "_blank", "noopener");
    card.addEventListener("click", open);
    card.addEventListener("keydown", (e) => {
      if (e.key === "Enter" || e.key === " ") { e.preventDefault(); open(); }
    });

    // Allow dropping an image straight onto a card to set its thumbnail
    card.addEventListener("dragover", (e) => { e.preventDefault(); card.style.borderColor = "var(--amber)"; });
    card.addEventListener("dragleave", () => { card.style.borderColor = ""; });
    card.addEventListener("drop", async (e) => {
      e.preventDefault();
      e.stopPropagation();
      card.style.borderColor = "";
      const dataUrl = await extractImageFromDrop(e.dataTransfer);
      if (dataUrl) {
        app.thumb = dataUrl;
        saveApps();
        render();
      }
    });

    return card;
  }

  function tickClock() {
    const now = new Date();
    clockEl.textContent = now.toLocaleTimeString([], { hour: "2-digit", minute: "2-digit", second: "2-digit" });
  }

  // ---- Drag & drop image extraction --------------------------------------
  function readFileAsDataUrl(file) {
    return new Promise((resolve, reject) => {
      const reader = new FileReader();
      reader.onload = () => resolve(reader.result);
      reader.onerror = () => reject(reader.error);
      reader.readAsDataURL(file);
    });
  }

  async function extractImageFromDrop(dataTransfer) {
    if (!dataTransfer) return null;

    // Case 1: an actual image file was dropped
    if (dataTransfer.files && dataTransfer.files.length) {
      const file = [...dataTransfer.files].find(f => f.type.startsWith("image/"));
      if (file) {
        try { return await readFileAsDataUrl(file); } catch { /* fall through */ }
      }
    }

    // Case 2: an image was dragged from another tab/page (URL or HTML payload)
    const uriList = dataTransfer.getData("text/uri-list") || dataTransfer.getData("text/plain");
    if (uriList && /^https?:\/\//i.test(uriList.trim())) {
      try {
        return await urlToDataUrl(uriList.trim());
      } catch {
        return null;
      }
    }

    const html = dataTransfer.getData("text/html");
    if (html) {
      const match = html.match(/<img[^>]+src=["']([^"']+)["']/i);
      if (match) {
        try { return await urlToDataUrl(match[1]); } catch { return null; }
      }
    }

    return null;
  }

  async function urlToDataUrl(url) {
    const res = await fetch(url, { mode: "cors" });
    const blob = await res.blob();
    return new Promise((resolve, reject) => {
      const reader = new FileReader();
      reader.onload = () => resolve(reader.result);
      reader.onerror = () => reject(reader.error);
      reader.readAsDataURL(blob);
    });
  }

  // ---- Panel (add / edit) ------------------------------------------------
  function openPanel(app) {
    editingId = app ? app.id : null;
    pendingThumb = app ? app.thumb : null;

    panelTitle.textContent = app ? "Edit service" : "Add service";
    deleteBtn.hidden = !app;
    fieldId.value = app ? app.id : "";
    fieldName.value = app ? app.name : "";
    fieldUrl.value = app ? app.url : "";

    updateThumbPreview();
    overlay.hidden = false;
    setTimeout(() => fieldName.focus(), 0);
  }

  function closePanel() {
    overlay.hidden = true;
    form.reset();
    pendingThumb = null;
    editingId = null;
  }

  function updateThumbPreview() {
    if (pendingThumb) {
      thumbPreview.src = pendingThumb;
      thumbPreview.hidden = false;
      dropzoneEmpty.hidden = true;
      resetThumbBtn.hidden = false;
    } else {
      thumbPreview.hidden = true;
      dropzoneEmpty.hidden = false;
      resetThumbBtn.hidden = true;
    }
  }

  openAddBtn.addEventListener("click", () => openPanel(null));
  closePanelBtn.addEventListener("click", closePanel);
  cancelBtn.addEventListener("click", closePanel);
  overlay.addEventListener("click", (e) => { if (e.target === overlay) closePanel(); });
  document.addEventListener("keydown", (e) => { if (e.key === "Escape" && !overlay.hidden) closePanel(); });

  resetThumbBtn.addEventListener("click", () => {
    pendingThumb = null;
    updateThumbPreview();
  });

  dropzone.addEventListener("click", () => fileInput.click());
  dropzone.addEventListener("keydown", (e) => {
    if (e.key === "Enter" || e.key === " ") { e.preventDefault(); fileInput.click(); }
  });

  fileInput.addEventListener("change", async () => {
    const file = fileInput.files[0];
    if (!file) return;
    pendingThumb = await readFileAsDataUrl(file);
    updateThumbPreview();
  });

  dropzone.addEventListener("dragover", (e) => { e.preventDefault(); dropzone.classList.add("is-dragover"); });
  dropzone.addEventListener("dragleave", () => dropzone.classList.remove("is-dragover"));
  dropzone.addEventListener("drop", async (e) => {
    e.preventDefault();
    dropzone.classList.remove("is-dragover");
    const dataUrl = await extractImageFromDrop(e.dataTransfer);
    if (dataUrl) {
      pendingThumb = dataUrl;
      updateThumbPreview();
    }
  });

  deleteBtn.addEventListener("click", () => {
    if (!editingId) return;
    if (!confirm("Remove this service from the rack?")) return;
    apps = apps.filter(a => a.id !== editingId);
    saveApps();
    render();
    closePanel();
  });

  form.addEventListener("submit", (e) => {
    e.preventDefault();
    const name = fieldName.value.trim();
    const url = normalizeUrl(fieldUrl.value);
    if (!name || !url) return;

    if (editingId) {
      const app = apps.find(a => a.id === editingId);
      if (app) { app.name = name; app.url = url; app.thumb = pendingThumb; }
    } else {
      apps.push({ id: uid(), name, url, thumb: pendingThumb });
    }
    saveApps();
    render();
    closePanel();
  });

  // ---- Init ---------------------------------------------------------------
  render();
  tickClock();
  setInterval(tickClock, 1000);
})();
