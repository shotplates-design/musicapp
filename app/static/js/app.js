import { deleteTrackData, getMedia, listPlaylists, listTracks, replacePlaylists, saveTrack } from "./db.js";

const audio = document.querySelector("#audio");
const view = document.querySelector("#view");
const seek = document.querySelector("#seek");

const UNKNOWN_ARTIST = "Unknown artist";

const state = {
  tab: "library",
  tracks: [],
  playlists: [],
  playlistId: null,
  currentId: null,
  queue: [],
  queueIndex: 0,
  seeking: false,
  sheetOpen: false,
  modal: null,
  draftUrl: "",
  busy: false,
};

let positionTimer = 0;

function el(tag, props = {}, children = []) {
  const node = document.createElement(tag);
  for (const [key, value] of Object.entries(props)) {
    if (value == null || value === false) continue;
    if (key === "class") node.className = value;
    else if (key === "text") node.textContent = String(value);
    else if (key.startsWith("on") && typeof value === "function") node.addEventListener(key.slice(2), value);
    else if (value === true) node.setAttribute(key, "");
    else node.setAttribute(key, String(value));
  }
  for (const child of children) if (child) node.append(child);
  return node;
}

function formatDuration(seconds) {
  if (!Number.isFinite(seconds) || seconds < 0) return "?:??";
  const total = Math.floor(seconds);
  const secs = total % 60;
  const mins = Math.floor(total / 60) % 60;
  const hours = Math.floor(total / 3600);
  const pad = (value) => String(value).padStart(2, "0");
  if (hours > 0) return `${hours}:${pad(mins)}:${pad(secs)}`;
  return `${mins}:${pad(secs)}`;
}

function formatBytes(bytes) {
  const value = Number(bytes);
  if (!Number.isFinite(value) || value <= 0) return "0 B";
  if (value < 1024) return `${Math.round(value)} B`;
  const units = ["KB", "MB", "GB", "TB"];
  let size = value / 1024;
  let unit = 0;
  while (size >= 1024 && unit < units.length - 1) {
    size /= 1024;
    unit += 1;
  }
  const digits = size >= 10 ? 1 : 2;
  return `${size.toFixed(digits).replace(/\.0+$/, "").replace(/(\.\d)0$/, "$1")} ${units[unit]}`;
}

const artUrls = new Map();

function sortTracks(tracks) {
  return tracks.slice().sort((a, b) => (b.addedAt || 0) - (a.addedAt || 0) || String(a.title).localeCompare(String(b.title)));
}

function cleanPlaylistName(name) {
  return String(name || "").replace(/\u0000/g, " ").replace(/\s+/g, " ").trim();
}

function playlistNameKey(name) {
  return cleanPlaylistName(name).toLowerCase();
}

function isUserPlaylist(playlist) {
  return playlist?.kind !== "artist";
}

function sortPlaylists(playlists) {
  return playlists.slice().sort((a, b) => String(a.name).localeCompare(String(b.name), undefined, { sensitivity: "base" }));
}

function playlistNameTaken(playlists, name, ignoreId) {
  const key = playlistNameKey(name);
  if (!key) return false;
  return playlists.some((playlist) => isUserPlaylist(playlist) && playlist.id !== ignoreId && playlistNameKey(playlist.name) === key);
}

function createUserPlaylist(playlists, name) {
  const cleaned = cleanPlaylistName(name);
  if (!cleaned) return { error: "Name the playlist." };
  if (playlistNameTaken(playlists, cleaned)) return { error: "That playlist already exists." };
  const created = { id: createId(), name: cleaned, kind: "manual", trackIds: [] };
  playlists.push(created);
  return { playlist: created };
}

function renameUserPlaylist(playlists, playlistId, name) {
  const cleaned = cleanPlaylistName(name);
  if (!cleaned) return { error: "Name the playlist." };
  const playlist = playlists.find((item) => item.id === playlistId && isUserPlaylist(item));
  if (!playlist) return { error: "That playlist is not yours to rename." };
  if (playlistNameTaken(playlists, cleaned, playlistId)) return { error: "That playlist already exists." };
  playlist.name = cleaned;
  playlist.kind = "manual";
  return { playlist };
}

function deleteUserPlaylist(playlists, playlistId) {
  const index = playlists.findIndex((item) => item.id === playlistId && isUserPlaylist(item));
  if (index < 0) return { error: "That playlist is not yours to delete." };
  const [playlist] = playlists.splice(index, 1);
  return { playlist };
}

function createId() {
  if (globalThis.crypto?.randomUUID) return crypto.randomUUID();
  return `id-${Date.now().toString(36)}-${Math.random().toString(36).slice(2, 10)}`;
}

function headerText(response, name) {
  const raw = response.headers.get(name);
  if (!raw) return "";
  try { return decodeURIComponent(raw); } catch { return raw; }
}

function totalBytes(tracks) {
  return tracks.reduce((sum, track) => sum + (Number(track.byteSize) || 0) + (Number(track.artworkSize) || 0), 0);
}

function trackById(id) {
  return state.tracks.find((track) => track.id === id) || null;
}

function toast(message) {
  const node = document.querySelector("#toast");
  node.hidden = false;
  node.textContent = message;
  clearTimeout(toast.timer);
  toast.timer = setTimeout(() => {
    node.hidden = true;
  }, 3400);
}

function artNode(track, large = false) {
  const classes = large ? "art lg" : "art";
  const src = track ? artUrls.get(track.id) : "";
  if (!src) return el("span", { class: `${classes} art-empty` });
  return el("img", { class: classes, src, alt: "" });
}

function nextIndex(index, length) {
  if (!Number.isInteger(length) || length <= 0) return -1;
  if (!Number.isInteger(index) || index < 0 || index + 1 >= length) return -1;
  return index + 1;
}

function previousAction(index, length, currentTime, threshold = 3) {
  if (!Number.isInteger(length) || length <= 0 || !Number.isInteger(index) || index < 0 || index >= length) {
    return { type: "none", index: -1 };
  }
  if (Number.isFinite(currentTime) && currentTime > threshold) return { type: "restart", index };
  if (index > 0) return { type: "goto", index: index - 1 };
  return { type: "restart", index };
}

async function refreshStorage() {
  const node = document.querySelector("#usage");
  let line = `${formatBytes(totalBytes(state.tracks))} in library`;
  try {
    if (navigator.storage?.estimate) {
      const estimate = await navigator.storage.estimate();
      const usage = Number(estimate.usage);
      const quota = Number(estimate.quota);
      if (Number.isFinite(usage) && Number.isFinite(quota) && quota > 0) {
        const free = Math.max(0, quota - usage);
        line = `${formatBytes(usage)} used\n${formatBytes(free)} free`;
      }
    }
  } catch { /* estimate is optional */ }
  node.textContent = line;
}

function syncUserPlaylists(tracks, playlists) {
  const live = new Set(tracks.map((track) => track.id));
  const kept = [];
  for (const playlist of playlists) {
    if (!isUserPlaylist(playlist)) continue;
    playlist.kind = "manual";
    playlist.trackIds = (playlist.trackIds || []).filter((id) => live.has(id));
    delete playlist.artistKey;
    kept.push(playlist);
  }
  return sortPlaylists(kept);
}

async function refresh() {
  const tracks = sortTracks(await listTracks());
  for (const track of tracks) {
    if (artUrls.has(track.id)) continue;
    const media = await getMedia(track.id);
    artUrls.set(track.id, media?.artwork ? URL.createObjectURL(media.artwork) : "");
  }
  state.tracks = tracks;
  state.playlists = syncUserPlaylists(tracks, await listPlaylists());
  if (state.playlistId && !state.playlists.some((playlist) => playlist.id === state.playlistId)) {
    state.playlistId = null;
  }
  await replacePlaylists(state.playlists);
  await refreshStorage();
  renderView();
  updatePlayerChrome();
}

function setTab(tab) {
  state.tab = tab;
  if (tab !== "lists") state.playlistId = null;
  renderView();
}

async function addLink(event) {
  event.preventDefault();
  if (state.busy) return;
  const url = state.draftUrl.trim();
  if (!url) {
    toast("Paste a playerprops.com link.");
    return;
  }
  state.busy = true;
  renderView();
  toast("Downloading…");
  let response;
  try {
    response = await fetch("api/fetch", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ url }),
    });
  } catch {
    state.busy = false;
    renderView();
    toast("Couldn't reach the server.");
    return;
  }
  if (!response.ok) {
    const body = await response.json().catch(() => ({}));
    state.busy = false;
    renderView();
    toast(typeof body.detail === "string" ? body.detail : "Couldn't add that link.");
    return;
  }
  const sourceUrl = headerText(response, "x-pixel-source-url");
  if (sourceUrl && state.tracks.some((track) => track.sourceUrl === sourceUrl)) {
    state.busy = false;
    state.draftUrl = "";
    state.tab = "library";
    renderView();
    toast("Already in your library.");
    return;
  }
  const artLen = Number(response.headers.get("x-pixel-artwork-length") || 0);
  const buffer = await response.arrayBuffer();
  const bytes = new Uint8Array(buffer);
  const artBytes = artLen > 0 ? bytes.slice(0, artLen) : null;
  const audioBytes = bytes.slice(Math.max(0, artLen));
  const mime = response.headers.get("x-pixel-mime") || "audio/mp4";
  const artMime = response.headers.get("x-pixel-artwork-mime") || "image/jpeg";
  const duration = Number(response.headers.get("x-pixel-duration"));
  const meta = {
    id: createId(),
    title: headerText(response, "x-pixel-title") || "Untitled",
    artist: headerText(response, "x-pixel-artist") || UNKNOWN_ARTIST,
    duration: Number.isFinite(duration) && duration > 0 ? duration : null,
    mimeType: mime,
    byteSize: audioBytes.byteLength,
    artworkSize: artBytes ? artBytes.byteLength : 0,
    artworkMime: artBytes ? artMime : null,
    filename: headerText(response, "x-pixel-filename") || "audio",
    addedAt: Date.now(),
    source: "link",
    sourceUrl,
  };
  try {
    await saveTrack(
      meta,
      new Blob([audioBytes], { type: mime }),
      artBytes ? new Blob([artBytes], { type: artMime }) : null,
    );
  } catch {
    state.busy = false;
    renderView();
    toast("Couldn't save that song on this phone. Free some space and try again.");
    return;
  }
  state.busy = false;
  state.draftUrl = "";
  state.tab = "library";
  toast("Saved to this phone.");
  try {
    await refresh();
  } catch {
    renderView();
    toast("Saved, but the library didn't reload.");
  }
}

function playIds(ids, index) {
  const playable = ids.filter((id) => trackById(id));
  const current = ids[index];
  const start = Math.max(0, playable.indexOf(current));
  state.queue = playable;
  state.queueIndex = start;
  return loadCurrent(true);
}

async function loadCurrent(autoplay) {
  const id = state.queue[state.queueIndex];
  const track = trackById(id);
  if (!track) return;
  state.currentId = id;
  const media = await getMedia(id);
  if (!media?.audio) {
    toast("That audio file is missing from this phone.");
    return;
  }
  if (audio._objectUrl) URL.revokeObjectURL(audio._objectUrl);
  const playable = media.audio.type === "audio/mp4" ? media.audio : new Blob([media.audio], { type: "audio/mp4" });
  audio._objectUrl = URL.createObjectURL(playable);
  audio.src = audio._objectUrl;
  publishSession(track);
  updatePlayerChrome();
  if (autoplay) {
    try {
      await audio.play();
    } catch {
      toast("Tap Play to start.");
    }
  }
}

function stopPlayback() {
  audio.pause();
  audio.removeAttribute("src");
  audio.load();
  state.currentId = null;
  state.queue = [];
  state.queueIndex = 0;
  state.sheetOpen = false;
  if ("mediaSession" in navigator) {
    navigator.mediaSession.metadata = null;
    navigator.mediaSession.playbackState = "none";
  }
  renderSheet();
  updatePlayerChrome();
}

async function step(direction) {
  if (!state.queue.length) return;
  if (direction < 0) {
    const action = previousAction(state.queueIndex, state.queue.length, audio.currentTime || 0);
    if (action.type === "restart") {
      audio.currentTime = 0;
      publishPosition();
      updatePlayerChrome();
      return;
    }
    if (action.type !== "goto") return;
    state.queueIndex = action.index;
  } else {
    const index = nextIndex(state.queueIndex, state.queue.length);
    if (index < 0) {
      audio.pause();
      return;
    }
    state.queueIndex = index;
  }
  await loadCurrent(true);
}

function seekBy(delta) {
  if (!Number.isFinite(audio.duration)) return;
  audio.currentTime = Math.min(audio.duration, Math.max(0, (audio.currentTime || 0) + delta));
  publishPosition();
  updatePlayerChrome();
}

function publishSession(track) {
  if (!("mediaSession" in navigator) || !track) return;
  const artwork = [];
  if (track.artworkSize > 0) {
    artwork.push({
      src: artUrls.get(track.id) || new URL("icons/icon-512.png", document.baseURI).href,
      sizes: "512x512",
      type: track.artworkMime || "image/jpeg",
    });
  }
  artwork.push({
    src: new URL("icons/icon-512.png", document.baseURI).href,
    sizes: "512x512",
    type: "image/png",
  });
  try {
    navigator.mediaSession.metadata = new MediaMetadata({
      title: track.title,
      artist: track.artist,
      album: "Pixel Music",
      artwork,
    });
  } catch { /* some browsers reject an artwork URL */ }
}

function publishPosition() {
  if (!("mediaSession" in navigator) || !Number.isFinite(audio.duration) || audio.duration <= 0) return;
  const position = Math.min(Math.max(audio.currentTime || 0, 0), audio.duration);
  try {
    navigator.mediaSession.setPositionState({
      duration: audio.duration,
      playbackRate: audio.playbackRate || 1,
      position,
    });
  } catch { /* ignore a position the browser won't accept */ }
}

function installSessionHandlers() {
  if (!("mediaSession" in navigator)) return;
  const actions = [
    ["play", () => { audio.play().catch(() => {}); }],
    ["pause", () => audio.pause()],
    ["previoustrack", () => { void step(-1); }],
    ["nexttrack", () => { void step(1); }],
    ["seekbackward", (details) => seekBy(-(details.seekOffset || 10))],
    ["seekforward", (details) => seekBy(details.seekOffset || 10)],
    ["seekto", (details) => {
      if (Number.isFinite(details.seekTime)) audio.currentTime = details.seekTime;
      publishPosition();
      updatePlayerChrome();
    }],
    ["stop", () => audio.pause()],
  ];
  for (const [name, handler] of actions) {
    try { navigator.mediaSession.setActionHandler(name, handler); } catch { /* unsupported action */ }
  }
}

function updatePlayerChrome() {
  const track = trackById(state.currentId);
  document.querySelector("#player-title").textContent = track ? track.title : "No tape loaded";
  document.querySelector("#player-artist").textContent = track
    ? `${track.artist} · ${formatBytes(track.byteSize)}`
    : "Add a link to start";
  const toggle = document.querySelector("#player-toggle");
  const playing = track && !audio.paused;
  toggle.textContent = playing ? "Pause" : "Play";
  toggle.setAttribute("aria-label", playing ? "Pause" : "Play");
  document.querySelector("#time-now").textContent = formatDuration(audio.currentTime || 0);
  const end = Number.isFinite(audio.duration) ? audio.duration : track?.duration;
  document.querySelector("#time-end").textContent = formatDuration(end);
  if (!state.seeking && Number.isFinite(audio.duration) && audio.duration > 0) {
    seek.disabled = false;
    seek.value = String(Math.round((audio.currentTime / audio.duration) * 1000));
  } else if (!track) {
    seek.disabled = true;
    seek.value = "0";
  }
  const art = document.querySelector("#player-art");
  const artKey = track && track.artworkSize ? track.id : "";
  if (art.dataset.art !== artKey) {
    const replacement = artNode(track);
    replacement.id = "player-art";
    replacement.dataset.art = artKey;
    art.replaceWith(replacement);
  }
  document.querySelectorAll("[data-play]").forEach((button) => {
    const active = button.dataset.play === state.currentId && !audio.paused;
    button.textContent = active ? "Pause" : "Play";
  });
  const sheetPlay = document.querySelector("#sheet-play");
  if (sheetPlay) sheetPlay.textContent = audio.paused ? "Play" : "Pause";
  const sheetSeek = document.querySelector("#sheet-seek");
  if (sheetSeek && !state.seeking && Number.isFinite(audio.duration) && audio.duration > 0) {
    sheetSeek.value = seek.value;
  }
  const sheetTimes = document.querySelectorAll("#sheet .time span");
  if (sheetTimes.length === 2) {
    sheetTimes[0].textContent = formatDuration(audio.currentTime || 0);
    sheetTimes[1].textContent = formatDuration(end);
  }
  document.title = track ? `${track.title} — Pixel Music` : "Pixel Music";
}

function renderSheet() {
  const sheet = document.querySelector("#sheet");
  const track = trackById(state.currentId);
  if (!state.sheetOpen || !track) {
    sheet.hidden = true;
    sheet.replaceChildren();
    return;
  }
  sheet.hidden = false;
  const cover = artNode(track, true);
  sheet.replaceChildren(el("div", { class: "sheet-card", role: "dialog", "aria-label": "Player" }, [
    cover,
    el("h2", { text: track.title }),
    el("p", { class: "help", text: track.artist }),
    el("label", { class: "seek" }, [
      el("span", { class: "visually-hidden", text: "Seek" }),
      el("input", {
        id: "sheet-seek",
        type: "range",
        min: "0",
        max: "1000",
        value: seek.value,
        oninput: (event) => {
          state.seeking = true;
          const ratio = Number(event.target.value) / 1000;
          if (Number.isFinite(audio.duration)) audio.currentTime = ratio * audio.duration;
          publishPosition();
          updatePlayerChrome();
        },
        onchange: () => { state.seeking = false; },
      }),
    ]),
    el("p", { class: "time" }, [
      el("span", { text: formatDuration(audio.currentTime || 0) }),
      el("span", { text: formatDuration(Number.isFinite(audio.duration) ? audio.duration : track.duration) }),
    ]),
    el("div", { class: "stack" }, [
      el("button", { class: "btn ghost", type: "button", text: "Prev", onclick: () => { void step(-1); } }),
      el("button", {
        id: "sheet-play",
        class: "btn",
        type: "button",
        text: audio.paused ? "Play" : "Pause",
        onclick: () => { audio.paused ? audio.play().catch(() => toast("Tap Play to start.")) : audio.pause(); },
      }),
      el("button", { class: "btn ghost", type: "button", text: "Next", onclick: () => { void step(1); } }),
    ]),
    el("button", {
      class: "btn ghost",
      type: "button",
      text: "Close",
      onclick: () => { state.sheetOpen = false; renderSheet(); },
    }),
  ]));
}

function renderView() {
  document.querySelectorAll(".nav-btn").forEach((button) => {
    button.setAttribute("aria-current", button.dataset.tab === state.tab ? "page" : "false");
  });
  view.replaceChildren();
  if (state.tab === "add") renderAdd();
  else if (state.tab === "lists") renderLists();
  else renderLibrary();
  mountInstallHint();
  renderModal();
}

function playlistTracks(playlist) {
  const ids = new Set(playlist.trackIds || []);
  return state.tracks.filter((track) => ids.has(track.id));
}

function openPlaylist(id) {
  state.playlistId = id;
  state.tab = "lists";
  renderView();
}

function songCountLabel(count) {
  return count === 1 ? "1 song" : `${count} songs`;
}

function renderPlaylistCard(playlist) {
  const songs = playlistTracks(playlist);
  const count = songs.length;
  view.append(el("article", { class: "card" }, [
    artNode(songs.find((track) => artUrls.get(track.id)) || null),
    el("div", { class: "body" }, [
      el("p", { class: "track-title", text: playlist.name }),
      el("p", { class: "meta", text: songCountLabel(count) }),
    ]),
    el("div", { class: "row-actions" }, [
      el("button", {
        class: "btn",
        type: "button",
        text: "Open",
        onclick: () => openPlaylist(playlist.id),
      }),
      el("button", {
        class: "btn",
        type: "button",
        text: "Play",
        disabled: count === 0,
        onclick: () => { void playIds(songs.map((track) => track.id), 0); },
      }),
    ]),
  ]));
}

function renderLibrary() {
  if (!state.tracks.length) {
    view.append(el("section", { class: "panel" }, [
      el("h2", { text: "Library" }),
      el("p", { class: "help", text: "No tracks yet. A saved song stays on this phone and can play offline. Put songs into Lists when you want a playlist." }),
      el("button", { class: "btn", type: "button", text: "Add a link", onclick: () => setTab("add") }),
    ]));
    return;
  }
  view.append(el("h2", { text: "Library" }));
  for (const track of state.tracks) {
    const playing = state.currentId === track.id && !audio.paused;
    view.append(el("article", { class: "card" }, [
      artNode(track),
      el("div", { class: "body" }, [
        el("p", { class: "track-title", text: track.title }),
        el("p", {
          class: "meta",
          text: `${track.artist} · ${formatDuration(track.duration)} · ${formatBytes((track.byteSize || 0) + (track.artworkSize || 0))}`,
        }),
      ]),
      el("div", { class: "row-actions" }, [
        el("button", {
          class: "btn",
          type: "button",
          "data-play": track.id,
          text: playing ? "Pause" : "Play",
          onclick: () => {
            if (state.currentId === track.id && audio.src) {
              audio.paused ? audio.play().catch(() => toast("Tap Play to start.")) : audio.pause();
              return;
            }
            void playIds(state.tracks.map((item) => item.id), state.tracks.indexOf(track));
          },
        }),
        el("button", { class: "btn", type: "button", text: "Add", onclick: () => openAddToPlaylists(track) }),
        el("button", { class: "btn danger", type: "button", text: "Del", onclick: () => confirmDelete(track) }),
      ]),
    ]));
  }
}

function renderLists() {
  const open = state.playlists.find((playlist) => playlist.id === state.playlistId);
  if (open) {
    renderPlaylist(open);
    return;
  }
  view.append(el("div", { class: "section-head" }, [
    el("h2", { text: "Lists" }),
    el("button", { class: "btn", type: "button", text: "New", onclick: openCreatePlaylist }),
  ]));
  if (!state.playlists.length) {
    view.append(el("p", { class: "help", text: "No playlists yet. Name one, then add any song from Lib." }));
    return;
  }
  for (const playlist of state.playlists) renderPlaylistCard(playlist);
}

function renderPlaylist(playlist) {
  const songs = playlistTracks(playlist);
  view.append(el("div", { class: "inline" }, [
    el("button", {
      class: "btn ghost",
      type: "button",
      text: "Lists",
      onclick: () => { state.playlistId = null; renderView(); },
    }),
    el("button", { class: "btn", type: "button", text: "Rename", onclick: () => openRenamePlaylist(playlist) }),
    el("button", { class: "btn danger", type: "button", text: "Delete", onclick: () => confirmDeletePlaylist(playlist) }),
  ]));
  view.append(el("h2", { text: playlist.name }));
  view.append(el("p", { class: "help", text: "Your playlist" }));
  if (!songs.length) {
    view.append(el("p", { class: "help", text: "No songs in this playlist yet. Open Lib and tap Add." }));
  }
  for (const track of songs) {
    const playing = state.currentId === track.id && !audio.paused;
    view.append(el("article", { class: "card" }, [
      artNode(track),
      el("div", { class: "body" }, [
        el("p", { class: "track-title", text: track.title }),
        el("p", {
          class: "meta",
          text: `${track.artist} · ${formatDuration(track.duration)} · ${formatBytes((track.byteSize || 0) + (track.artworkSize || 0))}`,
        }),
      ]),
      el("div", { class: "row-actions" }, [
        el("button", {
          class: "btn",
          type: "button",
          "data-play": track.id,
          text: playing ? "Pause" : "Play",
          onclick: () => {
            if (state.currentId === track.id && audio.src) {
              audio.paused ? audio.play().catch(() => toast("Tap Play to start.")) : audio.pause();
              return;
            }
            const queued = playlistTracks(playlist);
            void playIds(queued.map((item) => item.id), queued.indexOf(track));
          },
        }),
        el("button", {
          class: "btn ghost",
          type: "button",
          text: "Remove",
          onclick: () => { void removeSongFromPlaylist(playlist, track.id); },
        }),
        el("button", { class: "btn danger", type: "button", text: "Del", onclick: () => confirmDelete(track) }),
      ]),
    ]));
  }
}

function renderAdd() {
  view.append(el("section", { class: "panel" }, [
    el("h2", { text: "Add a link" }),
    el("p", {
      class: "help",
      text: "Only playerprops.com links are supported. The file is saved on this phone as M4A and plays here. Add it to a List when you want. Files over 4 MB are refused: a streamed Vercel Hobby response still cannot exceed 4.5 MB.",
    }),
    el("form", { class: "form", onsubmit: (event) => { void addLink(event); } }, [
      el("label", { class: "field", for: "audio-url" }, [
        el("span", { text: "Audio URL" }),
        el("input", {
          id: "audio-url",
          type: "url",
          inputmode: "url",
          placeholder: "https://playerprops.com/…",
          autocomplete: "off",
          autocapitalize: "off",
          autocorrect: "off",
          spellcheck: "false",
          required: true,
          value: state.draftUrl,
          oninput: (event) => { state.draftUrl = event.target.value; },
        }),
      ]),
      el("button", {
        class: "btn pink",
        type: "submit",
        text: state.busy ? "Saving" : "Save",
        disabled: state.busy,
      }),
    ]),
  ]));
}

function closeModal() {
  state.modal = null;
  renderModal();
}

function renderModal() {
  const root = document.querySelector("#modal");
  if (!state.modal) {
    root.hidden = true;
    root.replaceChildren();
    return;
  }
  const modal = state.modal;
  const children = [el("h2", { text: modal.title })];
  if (modal.body) children.push(el("p", { class: "help", text: modal.body }));
  if (modal.nameInput) {
    children.push(el("label", { class: "field" }, [
      el("span", { text: "Name" }),
      el("input", {
        id: "playlist-name",
        type: "text",
        maxlength: "80",
        value: modal.draftName || "",
        autocomplete: "off",
        oninput: (event) => { modal.draftName = event.target.value; },
        onkeydown: (event) => {
          if (event.key === "Enter") {
            event.preventDefault();
            modal.onConfirm();
          }
        },
      }),
    ]));
  }
  if (modal.checks) {
    children.push(el("div", { class: "checks" }, modal.checks.map((check) => el("label", { class: "check" }, [
      el("input", {
        type: "checkbox",
        checked: check.checked,
        onchange: (event) => { check.checked = event.target.checked; },
      }),
      el("span", { text: check.label }),
    ]))));
  }
  children.push(el("div", { class: "inline" }, [
    el("button", { class: "btn ghost", type: "button", text: "Cancel", onclick: closeModal }),
    el("button", {
      class: modal.confirmClass || "btn danger",
      type: "button",
      text: modal.confirmLabel,
      onclick: () => modal.onConfirm(),
    }),
  ]));
  root.hidden = false;
  root.replaceChildren(el("div", { class: "modal-card", role: "dialog", "aria-modal": "true" }, children));
  const field = root.querySelector("#playlist-name");
  if (field) field.focus();
}

async function persistPlaylists() {
  try {
    await replacePlaylists(state.playlists);
  } catch {
    toast("Couldn't save that playlist.");
    return false;
  }
  renderView();
  return true;
}

function openCreatePlaylist() {
  state.modal = {
    title: "New playlist",
    body: "Names match without case, so Road Trip and road trip are the same playlist.",
    nameInput: true,
    draftName: "",
    confirmLabel: "Create",
    confirmClass: "btn pink",
    onConfirm: () => { void submitPlaylistName("create"); },
  };
  renderModal();
}

function openRenamePlaylist(playlist) {
  state.modal = {
    title: "Rename playlist",
    nameInput: true,
    draftName: playlist.name,
    playlistId: playlist.id,
    confirmLabel: "Rename",
    confirmClass: "btn pink",
    onConfirm: () => { void submitPlaylistName("rename"); },
  };
  renderModal();
}

async function submitPlaylistName(mode) {
  const modal = state.modal;
  if (!modal) return;
  const result = mode === "rename"
    ? renameUserPlaylist(state.playlists, modal.playlistId, modal.draftName)
    : createUserPlaylist(state.playlists, modal.draftName);
  if (result.error) {
    toast(result.error);
    return;
  }
  if (mode === "create") {
    state.playlistId = result.playlist.id;
    state.tab = "lists";
  }
  closeModal();
  if (await persistPlaylists()) toast(mode === "create" ? "Playlist created." : "Playlist renamed.");
}

function confirmDeletePlaylist(playlist) {
  state.modal = {
    title: "Delete playlist",
    body: `Delete “${playlist.name}”? Songs stay in your library.`,
    confirmLabel: "Delete",
    onConfirm: () => { void removeManualPlaylist(playlist.id); },
  };
  renderModal();
}

async function removeManualPlaylist(id) {
  const result = deleteUserPlaylist(state.playlists, id);
  if (result.error) {
    toast(result.error);
    return;
  }
  if (state.playlistId === id) state.playlistId = null;
  closeModal();
  if (await persistPlaylists()) toast("Playlist deleted.");
}

function openAddToPlaylists(track) {
  const manuals = sortPlaylists(state.playlists.filter((playlist) => isUserPlaylist(playlist)));
  if (!manuals.length) {
    toast("Create a playlist first.");
    return;
  }
  const checks = manuals.map((playlist) => ({
    id: playlist.id,
    label: playlist.name,
    checked: (playlist.trackIds || []).includes(track.id),
  }));
  state.modal = {
    title: "Add to playlists",
    body: track.title,
    checks,
    confirmLabel: "Save",
    confirmClass: "btn pink",
    onConfirm: () => { void applySongPlaylists(track.id, checks); },
  };
  renderModal();
}

async function applySongPlaylists(trackId, checks) {
  for (const check of checks) {
    const playlist = state.playlists.find((item) => item.id === check.id && isUserPlaylist(item));
    if (!playlist) continue;
    const ids = (playlist.trackIds || []).filter((id) => id !== trackId);
    if (check.checked) ids.push(trackId);
    playlist.trackIds = ids;
  }
  closeModal();
  if (await persistPlaylists()) toast("Playlists updated.");
}

async function removeSongFromPlaylist(playlist, trackId) {
  if (!isUserPlaylist(playlist)) return;
  playlist.trackIds = (playlist.trackIds || []).filter((id) => id !== trackId);
  if (await persistPlaylists()) toast("Removed from playlist.");
}

function confirmDelete(track) {
  state.modal = {
    title: "Delete track",
    body: `Remove “${track.title}” and its audio from this phone?`,
    confirmLabel: "Delete",
    onConfirm: () => {
      closeModal();
      void deleteTrack(track.id);
    },
  };
  renderModal();
}

async function deleteTrack(id) {
  const removed = artUrls.get(id);
  if (removed) URL.revokeObjectURL(removed);
  artUrls.delete(id);
  try {
    await deleteTrackData(id);
  } catch {
    toast("Couldn't delete that track.");
    return;
  }
  if (state.currentId === id) stopPlayback();
  else state.queue = state.queue.filter((item) => item !== id);
  toast("Track deleted.");
  try { await refresh(); } catch { toast("Couldn't load the library."); }
}

function mountInstallHint() {
  const ios = /iphone|ipad|ipod/i.test(navigator.userAgent) || (navigator.platform === "MacIntel" && navigator.maxTouchPoints > 1);
  const standalone = window.navigator.standalone === true || window.matchMedia("(display-mode: standalone)").matches;
  let dismissed = false;
  try { dismissed = localStorage.getItem("pixel-music-hint") === "1"; } catch { dismissed = false; }
  if (!ios || standalone || dismissed) return;
  const host = location.hostname;
  const secure = location.protocol === "https:" || host === "localhost" || host === "127.0.0.1" || host === "::1";
  const hint = el("div", { id: "hint", class: "hint" }, [
    el("p", {
      class: "help",
      text: secure
        ? "In Safari, tap Share, then Add to Home Screen, and open Pixel Music from the icon. The installed app plays full screen, with lock-screen controls."
        : "Add to Home Screen from Safari after you open the HTTPS address. iOS only installs the service worker over HTTPS. The README has a free tunnel and an mkcert walkthrough.",
    }),
    el("button", {
      class: "btn ghost",
      type: "button",
      text: "Hide",
      onclick: () => {
        try { localStorage.setItem("pixel-music-hint", "1"); } catch { /* private mode */ }
        hint.remove();
      },
    }),
  ]);
  view.prepend(hint);
}

function bindChrome() {
  document.querySelectorAll(".nav-btn").forEach((button) => {
    button.addEventListener("click", () => setTab(button.dataset.tab));
  });
  document.querySelector("#player-toggle").addEventListener("click", () => {
    if (!state.currentId) {
      if (state.tracks[0]) void playIds(state.tracks.map((track) => track.id), 0);
      return;
    }
    audio.paused ? audio.play().catch(() => toast("Tap Play to start.")) : audio.pause();
  });
  document.querySelector("#player-prev").addEventListener("click", () => { void step(-1); });
  document.querySelector("#player-next").addEventListener("click", () => { void step(1); });
  document.querySelector("#player-open").addEventListener("click", () => {
    if (!state.currentId) return;
    state.sheetOpen = true;
    renderSheet();
  });
  seek.addEventListener("input", () => {
    state.seeking = true;
    if (Number.isFinite(audio.duration) && audio.duration > 0) {
      audio.currentTime = (Number(seek.value) / 1000) * audio.duration;
    }
    publishPosition();
    updatePlayerChrome();
  });
  seek.addEventListener("change", () => { state.seeking = false; });
  audio.addEventListener("play", () => {
    if ("mediaSession" in navigator) navigator.mediaSession.playbackState = "playing";
    updatePlayerChrome();
  });
  audio.addEventListener("pause", () => {
    if ("mediaSession" in navigator) navigator.mediaSession.playbackState = "paused";
    updatePlayerChrome();
  });
  audio.addEventListener("ended", () => { void step(1); });
  audio.addEventListener("timeupdate", () => {
    updatePlayerChrome();
    const now = Date.now();
    if (now - positionTimer > 1000) {
      positionTimer = now;
      publishPosition();
    }
  });
  audio.addEventListener("loadedmetadata", () => {
    publishPosition();
    updatePlayerChrome();
  });
  installSessionHandlers();
}

bindChrome();
if (navigator.storage?.persist) navigator.storage.persist().catch(() => {});
if ("serviceWorker" in navigator) {
  navigator.serviceWorker.register("./sw.js").catch(() => {});
}
refresh().catch(() => {
  view.textContent = "Couldn't open the library.";
});
