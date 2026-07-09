# ChatGUI — LAN Chat Server (Windows 10/11)

A Python GUI-based LAN chat server. Launch the desktop app on a Windows PC, click
**Start Server**, and any device on the same Wi-Fi can join a real-time chat room
from a browser (scan the QR code or open the URL) — no app install on phones.

Includes file sharing: attach a file in the browser and it appears as a downloadable
message bubble for everyone in the room.

## Features

- CustomTkinter desktop control panel (dark theme) — status, join URL + QR code, live feed, connected-users list, settings
- Flask-SocketIO real-time chat (WebSocket with polling fallback)
- Browser client (mobile-responsive): nickname join, message bubbles, typing indicator, system messages
- File sharing via chunked resumable uploads + range (resumable) downloads
- In-memory message history replayed to new joiners
- LAN-only (binds `0.0.0.0`), HTTP

## Security / robustness

- Persisted random Flask secret key
- Per-IP rate limiting on joins and uploads
- `secure_filename` on all upload/download paths (blocks path traversal + Windows reserved names)
- Per-file size cap + extension allow/deny list
- Thread locks around all shared state
- Atomic chunk assembly (temp write + rename); orphaned-chunk sweep on start
- Non-blocking background MAC-address resolver
- Host can block an IP (drops live sockets too)
- Optional, pluggable file-processing hooks (nothing extra imported unless a hook is registered)

## Requirements

- Windows 10/11, Python 3.9+
- Dependencies auto-install on first run (`customtkinter`, `flask`, `flask-socketio`,
  `qrcode[pil]`, `pillow`, `python-engineio`, `python-socketio`, `simple-websocket`)

## Run

```bash
python main.py
```

Then click **Start Server** and share the displayed URL / QR code with devices on the same network.

## Module layout

| File | Responsibility |
|------|----------------|
| `main.py` | Entry point + dependency auto-installer |
| `config.py` | Global config, secret key, locks, rate limiting, file guards, hook registry |
| `network_utils.py` | Local IP detection + async MAC resolver |
| `file_transfer.py` | Chunked upload, range download, path/size/extension guards |
| `web_server.py` | Flask-SocketIO chat events, routes, broadcasts |
| `chat_client.py` | Embedded mobile browser client (HTML/JS) |
| `gui_app.py` | CustomTkinter desktop control panel |

## Notes

Designed for trusted local networks. Uses HTTP (no TLS) for zero-config LAN use.
