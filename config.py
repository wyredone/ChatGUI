import os
import time
import json
import secrets
import logging
import threading

# --- PATHS ---
APP_DIR = os.path.join(os.path.expanduser("~"), "Downloads", ".lan_chat")
os.makedirs(APP_DIR, exist_ok=True)
SHARED_FILES_DIR = os.path.join(APP_DIR, "shared_files")
UPLOAD_TMP_DIR = os.path.join(APP_DIR, ".upload_tmp")
os.makedirs(SHARED_FILES_DIR, exist_ok=True)
os.makedirs(UPLOAD_TMP_DIR, exist_ok=True)

CONN_LOG_FILE = os.path.join(APP_DIR, "connection_history.log")
ERR_LOG_FILE = os.path.join(APP_DIR, "system_errors.log")
APP_CONFIG_FILE = os.path.join(APP_DIR, "config.json")
SECRET_KEY_FILE = os.path.join(APP_DIR, ".secret_key")

for lf in [CONN_LOG_FILE, ERR_LOG_FILE]:
    if not os.path.exists(lf):
        with open(lf, "w", encoding="utf-8") as f:
            f.write(f"--- Log Initialized: {time.strftime('%Y-%m-%d %H:%M:%S')} ---\n")


# --- ITEM 1 (bundled with recommendation): persisted random secret key ---
def get_or_create_secret_key():
    if os.path.exists(SECRET_KEY_FILE):
        try:
            with open(SECRET_KEY_FILE, "r", encoding="utf-8") as f:
                k = f.read().strip()
            if k:
                return k
        except Exception:
            pass
    k = secrets.token_hex(32)
    try:
        with open(SECRET_KEY_FILE, "w", encoding="utf-8") as f:
            f.write(k)
    except Exception:
        pass
    return k


SECRET_KEY = get_or_create_secret_key()

# --- GLOBAL CONFIG ---
SERVER_CONFIG = {
    "shared_dir": SHARED_FILES_DIR,
    "upload_tmp_dir": UPLOAD_TMP_DIR,
    "port": 48317,
    "running": False,
    "server_instance": None,
    "web_theme": "dark",
    "auto_start_on_launch": False,
    "window_geometry": "760x860+100+100",
    "max_users": 50,
    "history_size": 200,
    "min_disk_space_gb": 2,

    # --- ITEM 8: per-file size cap + extension allow/deny ---
    "max_file_size_mb": 512,
    # empty allow-list means "all allowed except denied"
    "allowed_extensions": [],
    "denied_extensions": [".exe", ".bat", ".cmd", ".com", ".scr", ".msi",
                          ".ps1", ".vbs", ".js", ".jar", ".dll"],

    # --- ITEM 3: login/join rate limiting ---
    "rate_limit_window_sec": 60,
    "rate_limit_max_joins": 10,   # nickname joins per IP per window
    "rate_limit_max_uploads": 40, # upload-chunk POSTs per IP per window

    # runtime state (guarded by locks below)
    "active_connections": {},   # sid -> {nick, ip, joined}
    "blocked_ips": [],
}

# --- ITEM 9: locks around shared mutable state ---
STATE_LOCK = threading.RLock()          # active_connections, blocked_ips
HISTORY_LOCK = threading.RLock()        # message history
RATE_LOCK = threading.RLock()           # rate-limit counters

# in-memory chat history (guarded by HISTORY_LOCK)
MESSAGE_HISTORY = []

# rate-limit buckets: ip -> {"join": [timestamps], "upload": [timestamps]}
RATE_BUCKETS = {}


# --- ITEM 24: pluggable file-processing hook registry (optional, no forced imports) ---
_FILE_HOOKS = []

def register_file_hook(func):
    """Register callable(final_path, meta) run after an upload finalizes.
    Nothing (Pillow/pytesseract/etc.) is imported unless a hook is added."""
    _FILE_HOOKS.append(func)
    return func

def run_file_hooks(final_path, meta):
    for h in list(_FILE_HOOKS):
        try:
            h(final_path, meta)
        except Exception as e:
            err_logger.error(f"File hook error: {e}")


# --- LOGGING ---
class FileLogHandler(logging.Handler):
    def __init__(self, filepath):
        super().__init__()
        self.filepath = filepath

    def emit(self, record):
        try:
            msg = self.format(record)
            with open(self.filepath, "a", encoding="utf-8") as f:
                f.write(msg + "\n")
        except Exception:
            pass


conn_logger = logging.getLogger("ChatConnLogger")
conn_logger.setLevel(logging.INFO)
_ch = FileLogHandler(CONN_LOG_FILE)
_ch.setFormatter(logging.Formatter('%(asctime)s - %(message)s', datefmt='%Y-%m-%d %H:%M:%S'))
conn_logger.addHandler(_ch)

err_logger = logging.getLogger("ChatErrLogger")
err_logger.setLevel(logging.ERROR)
_eh = FileLogHandler(ERR_LOG_FILE)
_eh.setFormatter(logging.Formatter('%(asctime)s - [%(levelname)s] - %(message)s', datefmt='%Y-%m-%d %H:%M:%S'))
err_logger.addHandler(_eh)


def format_size(bytes_size):
    for unit in ['B', 'KB', 'MB', 'GB', 'TB']:
        if bytes_size < 1024.0:
            return f"{bytes_size:.1f} {unit}"
        bytes_size /= 1024.0
    return f"{bytes_size:.1f} PB"


# --- ITEM 3 helper: rate limiting ---
def rate_limit_ok(ip, kind):
    """kind = 'join' or 'upload'. Returns True if under limit."""
    now = time.time()
    window = SERVER_CONFIG["rate_limit_window_sec"]
    cap = SERVER_CONFIG["rate_limit_max_joins"] if kind == "join" else SERVER_CONFIG["rate_limit_max_uploads"]
    with RATE_LOCK:
        bucket = RATE_BUCKETS.setdefault(ip, {"join": [], "upload": []})
        stamps = [t for t in bucket[kind] if now - t < window]
        if len(stamps) >= cap:
            bucket[kind] = stamps
            return False
        stamps.append(now)
        bucket[kind] = stamps
        return True


# --- ITEM 8 helper: extension / size validation ---
def extension_allowed(filename):
    ext = os.path.splitext(filename)[1].lower()
    denied = [e.lower() for e in SERVER_CONFIG["denied_extensions"]]
    allowed = [e.lower() for e in SERVER_CONFIG["allowed_extensions"]]
    if ext in denied:
        return False
    if allowed:  # non-empty allow-list = strict mode
        return ext in allowed
    return True


def file_size_ok(total_bytes):
    cap = SERVER_CONFIG["max_file_size_mb"] * 1024 * 1024
    return total_bytes <= cap


def load_application_preferences():
    if os.path.exists(APP_CONFIG_FILE):
        try:
            with open(APP_CONFIG_FILE, "r", encoding="utf-8") as f:
                data = json.load(f)
            for key in ["shared_dir", "upload_tmp_dir", "port", "web_theme",
                        "auto_start_on_launch", "window_geometry", "max_users",
                        "history_size", "min_disk_space_gb", "max_file_size_mb",
                        "allowed_extensions", "denied_extensions", "blocked_ips"]:
                if key in data:
                    SERVER_CONFIG[key] = data[key]
        except Exception:
            pass


def save_application_preferences(geometry_string=None):
    try:
        if geometry_string:
            SERVER_CONFIG["window_geometry"] = geometry_string
        payload = {k: SERVER_CONFIG[k] for k in [
            "shared_dir", "upload_tmp_dir", "port", "web_theme",
            "auto_start_on_launch", "window_geometry", "max_users",
            "history_size", "min_disk_space_gb", "max_file_size_mb",
            "allowed_extensions", "denied_extensions", "blocked_ips"]}
        with open(APP_CONFIG_FILE, "w", encoding="utf-8") as f:
            json.dump(payload, f, indent=4)
    except Exception:
        pass
