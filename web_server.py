import time
from flask import Flask, render_template_string, request
from flask_socketio import SocketIO, emit

from config import (SERVER_CONFIG, SECRET_KEY, MESSAGE_HISTORY, HISTORY_LOCK,
                    STATE_LOCK, conn_logger, err_logger, rate_limit_ok)
from network_utils import resolve_mac_async
from chat_client import CHAT_TEMPLATE
import file_transfer

app = Flask("LANChatServer")
app.secret_key = SECRET_KEY  # ITEM 1: persisted random key
app.config.update(
    SESSION_COOKIE_HTTPONLY=True,
    SESSION_COOKIE_SAMESITE='Lax',
    MAX_CONTENT_LENGTH=None,  # chunked; per-file cap enforced in file_transfer
)

# ITEM 16: single unified Socket.IO config used by chat AND file layer.
socketio = SocketIO(app, async_mode='threading', cors_allowed_origins="*",
                    max_http_buffer_size=2 * 1024 * 1024)

# GUI hook (set by gui_app) to surface events in the desktop feed. ITEM 26 mirrors
# these same events to the browser feed via Socket.IO.
_gui_event = None
def set_gui_event_sink(func):
    global _gui_event
    _gui_event = func

def gui_log(text):
    if _gui_event:
        try:
            _gui_event(text)
        except Exception:
            pass


# ---------- history helpers ----------
def _push_history(entry):
    with HISTORY_LOCK:
        MESSAGE_HISTORY.append(entry)
        overflow = len(MESSAGE_HISTORY) - SERVER_CONFIG["history_size"]
        if overflow > 0:
            del MESSAGE_HISTORY[:overflow]

def _history_snapshot():
    with HISTORY_LOCK:
        return list(MESSAGE_HISTORY)

def _user_count():
    with STATE_LOCK:
        return len(SERVER_CONFIG["active_connections"])

def _nick_for_sid(sid):
    with STATE_LOCK:
        c = SERVER_CONFIG["active_connections"].get(sid)
        return c["nick"] if c else None


# ---------- ITEM 18: file-message broadcast (called from file_transfer) ----------
def broadcast_file_message(sender, meta):
    entry = dict(meta)  # type=file, name, size_str, url, sender, ts
    _push_history(entry)
    socketio.emit('message', entry)
    # ITEM 26: surface upload in chat feed (browser) + desktop GUI
    socketio.emit('system', {"text": f"{sender} shared a file: {meta['name']} ({meta['size_str']})"})
    gui_log(f"📄 {sender} shared {meta['name']} ({meta['size_str']})")

file_transfer.set_chat_broadcast(broadcast_file_message)


# ---------- routes ----------
@app.route('/')
def index():
    ip = request.remote_addr
    with STATE_LOCK:
        if ip in SERVER_CONFIG["blocked_ips"]:
            return "Access Denied.", 403
    return render_template_string(CHAT_TEMPLATE)

@app.route('/upload_chunk', methods=['POST'])
def upload_chunk():
    return file_transfer.handle_upload_chunk()

@app.route('/download/<path:filename>')
def download(filename):
    return file_transfer.handle_download(filename)


# ---------- socket.io chat events ----------
@socketio.on('join')
def on_join(data):
    ip = request.remote_addr
    sid = request.sid

    with STATE_LOCK:
        if ip in SERVER_CONFIG["blocked_ips"]:
            emit('join_rejected', {"reason": "You are blocked from this server."})
            return

    # ITEM 3: rate-limit joins per IP
    if not rate_limit_ok(ip, "join"):
        emit('join_rejected', {"reason": "Too many attempts. Wait a moment."})
        return

    nick = (data or {}).get('nick', '').strip()[:40]
    if not nick:
        emit('join_rejected', {"reason": "Nickname required."})
        return

    with STATE_LOCK:
        if len(SERVER_CONFIG["active_connections"]) >= SERVER_CONFIG["max_users"] \
                and sid not in SERVER_CONFIG["active_connections"]:
            emit('join_rejected', {"reason": "Room is full."})
            return
        SERVER_CONFIG["active_connections"][sid] = {
            "nick": nick, "ip": ip, "joined": time.strftime('%H:%M:%S')
        }

    resolve_mac_async(ip)  # ITEM 15: non-blocking, cache warms in background
    conn_logger.info(f"JOIN {nick}@{ip} sid={sid}")

    emit('join_ok', {"history": _history_snapshot(), "nick": nick})
    sys_txt = f"{nick} joined the chat"
    _push_history({"system": True, "text": sys_txt})
    socketio.emit('system', {"text": sys_txt})
    socketio.emit('user_count', {"count": _user_count()})
    gui_log(f"➕ {sys_txt} ({ip})")


@socketio.on('message')
def on_message(data):
    nick = _nick_for_sid(request.sid)
    if not nick:
        return
    text = (data or {}).get('text', '').strip()
    if not text:
        return
    text = text[:2000]
    entry = {"type": "chat", "sender": nick, "text": text, "ts": time.strftime('%H:%M:%S')}
    _push_history(entry)
    socketio.emit('message', entry)
    gui_log(f"💬 {nick}: {text}")


@socketio.on('typing')
def on_typing(data):
    nick = _nick_for_sid(request.sid)
    if nick:
        emit('typing', {"nick": nick}, broadcast=True, include_self=False)


@socketio.on('disconnect')
def on_disconnect():
    sid = request.sid
    nick = None
    with STATE_LOCK:
        c = SERVER_CONFIG["active_connections"].pop(sid, None)
        if c:
            nick = c["nick"]
    if nick:
        sys_txt = f"{nick} left the chat"
        _push_history({"system": True, "text": sys_txt})
        socketio.emit('system', {"text": sys_txt})
        socketio.emit('user_count', {"count": _user_count()})
        conn_logger.info(f"LEAVE {nick} sid={sid}")
        gui_log(f"➖ {sys_txt}")


def disconnect_all_for_ip(target_ip):
    """Used by GUI block action: drop live sockets for a blocked IP (ITEM 12 bundled)."""
    victims = []
    with STATE_LOCK:
        for sid, c in list(SERVER_CONFIG["active_connections"].items()):
            if c["ip"] == target_ip:
                victims.append((sid, c["nick"]))
    for sid, nick in victims:
        try:
            socketio.emit('system', {"text": f"{nick} was removed by host."})
            socketio.server.disconnect(sid)
        except Exception as e:
            err_logger.error(f"disconnect_all_for_ip: {e}")
    with STATE_LOCK:
        for sid, _ in victims:
            SERVER_CONFIG["active_connections"].pop(sid, None)
    socketio.emit('user_count', {"count": _user_count()})
