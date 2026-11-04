const DB_NAME = "pixel-music";
const DB_VERSION = 1;

let dbPromise;

function requestToPromise(request) {
  return new Promise((resolve, reject) => {
    request.onsuccess = () => resolve(request.result);
    request.onerror = () => reject(request.error);
  });
}

function transactionDone(tx) {
  return new Promise((resolve, reject) => {
    tx.oncomplete = () => resolve();
    tx.onabort = () => reject(tx.error || new Error("transaction aborted"));
    tx.onerror = () => reject(tx.error || new Error("transaction failed"));
  });
}

export function openDb() {
  if (!globalThis.indexedDB) return Promise.reject(new Error("IndexedDB is unavailable"));
  if (!dbPromise) {
    dbPromise = new Promise((resolve, reject) => {
      const request = indexedDB.open(DB_NAME, DB_VERSION);
      request.onupgradeneeded = () => {
        const db = request.result;
        if (!db.objectStoreNames.contains("tracks")) db.createObjectStore("tracks", { keyPath: "id" });
        if (!db.objectStoreNames.contains("media")) db.createObjectStore("media", { keyPath: "id" });
      };
      request.onsuccess = () => resolve(request.result);
      request.onerror = () => {
        dbPromise = null;
        reject(request.error);
      };
    });
  }
  return dbPromise;
}

export async function listTracks() {
  const db = await openDb();
  const tx = db.transaction("tracks", "readonly");
  const tracks = await requestToPromise(tx.objectStore("tracks").getAll());
  await transactionDone(tx);
  return tracks;
}

export async function getMedia(id) {
  const db = await openDb();
  const tx = db.transaction("media", "readonly");
  const media = await requestToPromise(tx.objectStore("media").get(id));
  await transactionDone(tx);
  return media || null;
}

export async function saveTrack(meta, audioBlob, artworkBlob) {
  const db = await openDb();
  const tx = db.transaction(["tracks", "media"], "readwrite");
  tx.objectStore("tracks").put(meta);
  tx.objectStore("media").put({ id: meta.id, audio: audioBlob, artwork: artworkBlob || null });
  await transactionDone(tx);
}

export async function deleteTrackData(id) {
  const db = await openDb();
  const tx = db.transaction(["tracks", "media"], "readwrite");
  tx.objectStore("tracks").delete(id);
  tx.objectStore("media").delete(id);
  await transactionDone(tx);
}
