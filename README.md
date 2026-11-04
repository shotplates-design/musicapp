# Pixel Music

A small music player you can add to an iPhone home screen. Paste a **direct audio file link** you have the rights to use. The server checks the link and streams the file back. The song, its artwork, and its details are saved **in the browser** (IndexedDB) and play from there, including offline.

The pixel look — colors, Press Start 2P and Silkscreen, icons, and the chunky CSS — is adapted from [shotplates-design/pixel-music](https://github.com/shotplates-design/pixel-music). Thank you to that project for the theme. The font licenses are in `app/static/fonts/`.

This app does **not** rip YouTube, Spotify, Apple Music, or any other streaming service. There is no yt-dlp or similar tool. Only add audio you have rights to.

There is no login. The server does not keep a library.

## Library on the phone

Each track record matches the pixel-music shape: `title`, `artist`, `duration`, `mimeType`, `byteSize`, `artworkSize`, `filename`, `addedAt`, `source`, and `sourceUrl`. The list is newest first, then by title. **Del** asks you to confirm, then removes the audio and artwork from this phone.

On launch the app calls `navigator.storage.persist()`. The header shows storage **used** and **free** from `navigator.storage.estimate()` when the browser provides them.

Accepted links: `.mp3`, `.m4a`, `.aac`, `.ogg`, `.opus`, `.wav`, `.flac`. The response `Content-Type` must be audio that matches the extension, and the leading bytes must match that type (ID3 or an MP3 frame, M4A `ftyp`, Ogg, FLAC, WAV `RIFF`/`WAVE`, or AAC ADTS). Private, loopback, link-local, and disguised forms of those addresses are refused. The hostname is resolved once and the connection uses that address.

## Size limit

[Vercel Hobby](https://vercel.com/docs/functions/limitations) caps a function **response body at 4.5 MB**. That page does not raise the cap for streamed responses (it mentions streaming only for how long a function may run). This app still streams the file back, and it refuses anything over **4 MB** (4 MiB, `4 * 1024 * 1024` bytes) so the body stays under 4.5 MB. The Add screen says so, and a file that is too big shows that error instead of being saved.

`PIXEL_MAX_BYTES` can raise the cap for a **local** server. Leave it unset on Vercel. A larger response is rejected by the platform even if this app tried to send it.

## Run it on your computer

The server listens on `127.0.0.1` only unless you set `PIXEL_HOST`.

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
pytest
python -m app
```

Open `http://127.0.0.1:8000`. Paste a link on **Add**. The song is stored on this browser. The bottom bar seeks, pauses, and moves to the next or previous track.

`PIXEL_PORT` changes the port (the default is 8000). Nothing in this repo is a secret. Do not commit `.env`.

**No login.** If you set `PIXEL_HOST` to anything other than loopback (`0.0.0.0`, a LAN address, and so on), the process prints a warning: anyone who can reach that address can add and delete tracks on the phones that use it, and can use the server to fetch a URL. A tunnel is the same idea. Do not share the address.

## Use it on an iPhone

Add to Home Screen only works from **Safari**. iOS only installs the service worker over HTTPS. The service worker caches the app shell. Songs live in IndexedDB on the phone, so they play after the shell is cached even if the phone is offline. Fetching a new link still needs the server.

### Free public URL on Vercel Hobby

No environment variables and no Blob store. The Hobby plan is enough.

1. Sign in at [vercel.com](https://vercel.com) with the GitHub account that has this repo.
2. **Add New… → Project** and import this repository.
3. Framework preset: **Other** (Vercel detects the FastAPI app from `pyproject.toml`: `api.index:app`).
4. Leave **Environment Variables** empty. Do not add a token.
5. Deploy. Open the `https://….vercel.app` URL.

`vercel.json` allows the function 60 seconds, inside the Hobby maximum of 300 seconds.

### Same Wi-Fi without Vercel

```bash
export PIXEL_HOST=0.0.0.0
export PIXEL_PORT=8443
export PIXEL_SSL_CERTFILE=cert.pem
export PIXEL_SSL_KEYFILE=key.pem
python -m app
```

Create `cert.pem` and `key.pem` with [mkcert](https://github.com/FiloSottile/mkcert) for your LAN IP, install mkcert’s root on the iPhone (AirDrop `rootCA.pem`, install the profile, then Settings → General → About → Certificate Trust Settings), and open `https://<lan-ip>:8443`. The process prints the no-login warning because it is not bound to loopback only.

A free Cloudflare quick tunnel also works, and it is public:

```bash
python -m app
cloudflared tunnel --url http://127.0.0.1:8000
```

Anyone with that `https://….trycloudflare.com` URL can add and delete songs in **their** browser and can ask your computer to fetch a file URL. Stop the tunnel when you are done.

### Add to Home Screen

1. In **Safari**, open the HTTPS URL. Wait until the library loads.
2. Tap **Share** (the square with the arrow).
3. Tap **Add to Home Screen**, then **Add**.
4. Open **Pixel Music** from the icon. It fills the screen. The status bar sits on the purple background.
5. If you previously added an `http://` icon, delete it and add the HTTPS one again.

Play a track, then lock the phone. The Media Session API publishes play, pause, next, previous, and seek. Those controls are reliable when the app was opened from the home-screen icon.

## Tests

```bash
pytest
```

The tests cover the fetch endpoint: the file is streamed back with its tags, nothing is left on the server, and YouTube, Spotify, Apple Music, private addresses, bad file signatures, and oversized files are refused. They also check the 4 MB cap, the no-login warning, the web manifest, and the service worker.
