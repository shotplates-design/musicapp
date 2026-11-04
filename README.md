# Pixel Music

A small, installable music player for your iPhone. Paste a **direct audio file link** you have the rights to use. The server downloads that file, reads the title, artist, and artwork, and keeps it in a library you can play from the phone.

The pixel look — colors, Press Start 2P and Silkscreen, icons, and the chunky CSS — is adapted from [shotplates-design/pixel-music](https://github.com/shotplates-design/pixel-music). Thank you to that project for the theme. The font licenses are in `app/static/fonts/`.

This app does **not** rip YouTube, Spotify, Apple Music, or any other streaming service. There is no yt-dlp or similar tool. Only add audio you have rights to.

## Library

Tracks are stored on the computer running the server, in a `library/` folder (override with `PIXEL_LIBRARY`):

- `tracks.json` — one record per song
- `media/<id>.<ext>` — the original audio bytes, not re-encoded
- `media/<id>.artwork` — embedded cover art, when the file has any

Each record follows the pixel-music track shape: `title`, `artist`, `duration`, `mimeType`, `byteSize`, `artworkSize`, `filename`, `addedAt`, `source`, and `sourceUrl`. The list is newest first, then by title. The header total counts audio plus artwork. **Del** asks you to confirm, then removes the song from the list, from disk, and from the player.

Title and artist come from ID3 (`TIT2` / `TPE1`), MP4 (`©nam` / `©ART`), or Vorbis/FLAC comments. If those are missing, the title is the file name and the artist is “Unknown artist”. Cover art is the ID3 `APIC`, MP4 `covr`, or FLAC picture.

Accepted direct links: `.mp3`, `.m4a`, `.aac`, `.ogg`, `.opus`, `.wav`, `.flac`. The response `Content-Type` must be an audio type that matches the extension. Files over 80 MB are refused (`PIXEL_MAX_BYTES` changes the cap). Private, loopback, and link-local addresses are refused.

## Run it on your computer

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
pytest
uvicorn app.main:app --host 0.0.0.0 --port 8000
```

Open `http://127.0.0.1:8000`. Paste a link on **Add**, then play it from **Lib**. The bottom bar seeks, pauses, and moves to the next or previous track. Tap the bar for a larger player.

There is no account and no API key. Nothing in this repo is a secret. `cert.pem` and `key.pem` are gitignored if you create them for HTTPS below.

## Use it on an iPhone

Add to Home Screen only works from **Safari**. Chrome on iOS cannot install this as a standalone app.

iOS only installs the service worker in a secure context (HTTPS, or localhost on the phone itself). An `http://192.168.x.x` address can open the player, but the home-screen app will not cache its shell. Use one of the free HTTPS options below, then install from that HTTPS address.

The service worker caches the app shell (HTML, CSS, fonts, icons). Songs stay in `library/` on the computer, so playback needs that server to be reachable.

### 1. Free HTTPS tunnel (fastest)

[cloudflared](https://developers.cloudflare.com/cloudflare-one/connections/connect-networks/downloads/) quick tunnels are free and do not need an account.

```bash
# macOS
brew install cloudflared

# Debian/Ubuntu
curl -L https://github.com/cloudflare/cloudflared/releases/latest/download/cloudflared-linux-amd64 -o cloudflared
chmod +x cloudflared
sudo mv cloudflared /usr/local/bin/
```

Start the app, then the tunnel:

```bash
uvicorn app.main:app --host 127.0.0.1 --port 8000
cloudflared tunnel --url http://127.0.0.1:8000
```

cloudflared prints an `https://….trycloudflare.com` URL. Open **that** URL in Safari on the iPhone.

The tunnel is public. Anyone with the URL can open your player and add links. Do not share it, and stop cloudflared when you are done (`Ctrl-C`). For a server that stays on your Wi-Fi only, use mkcert instead.

### 2. mkcert on the same Wi-Fi (stays private)

[mkcert](https://github.com/FiloSottile/mkcert) makes a locally trusted certificate. It is free.

```bash
# macOS
brew install mkcert
# Debian/Ubuntu
sudo apt install mkcert   # or download the release binary

mkcert -install
```

Find the computer’s LAN address (`ipconfig getifaddr en0` on macOS, `hostname -I` on Linux). Replace the example below with yours.

```bash
mkcert -cert-file cert.pem -key-file key.pem localhost 127.0.0.1 192.168.1.20
uvicorn app.main:app --host 0.0.0.0 --port 8443 \
  --ssl-keyfile key.pem --ssl-certfile cert.pem
```

Trust the mkcert root on the iPhone, or Safari will warn and the service worker will not install:

1. On the computer, run `mkcert -CAROOT`. It prints a folder that contains `rootCA.pem`.
2. AirDrop `rootCA.pem` to the iPhone, or mail it to yourself and open it in Safari.
3. Install the profile: Settings → Profile Downloaded → Install.
4. Settings → General → About → Certificate Trust Settings → enable full trust for the mkcert root.

Then, on the same Wi-Fi, open `https://192.168.1.20:8443` in Safari. The lock icon should be clean.

A self-signed OpenSSL certificate works the same way if you would rather not install mkcert. Create one with a `subjectAltName` for your LAN IP, install that certificate as a trusted root on the iPhone, and pass it to uvicorn with `--ssl-certfile` and `--ssl-keyfile`. mkcert is the less fiddly of the two.

### 3. Add to Home Screen

1. In **Safari**, open the HTTPS URL from the tunnel or from mkcert.
2. Tap the **Share** button (the square with the arrow).
3. Scroll the sheet and tap **Add to Home Screen**.
4. Leave the name as **Pixel Music**. The icon is the pixel note. Tap **Add**.
5. Open **Pixel Music** from the new home-screen icon. It should fill the screen, with the status bar sitting on the purple background (safe areas are padded).
6. If you previously added an `http://` icon, delete that icon and add this HTTPS one. The service worker is tied to the HTTPS origin.

Play a track, then lock the phone or switch away. The Media Session API publishes play, pause, next, previous, and seek to the lock screen and the Control Center. Those controls are reliable when the app was launched from the home-screen icon. A normal Safari tab can suspend the page.

## Tests

```bash
pytest
```

The tests cover the link endpoint: direct audio is saved, ID3 title/artist/artwork are kept, the library sorts newest first, delete removes the file, and YouTube, Spotify, Apple Music, private addresses, non-audio responses, and oversized files are refused with nothing left on disk. They also check the web manifest, the Apple web-app meta tags, the touch icon, and the service worker.
