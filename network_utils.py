import sys
import socket
import subprocess
import re
import threading

# --- ITEM 15: async, non-blocking MAC resolver ---
# Instead of shelling out to `arp` inside the request path, we resolve in a
# background thread and cache the result. Callers read the cache instantly.

_MAC_CACHE = {}            # ip -> mac string
_MAC_INFLIGHT = set()      # ips currently being resolved
_MAC_LOCK = threading.Lock()


def _resolve_worker(ip_address):
    mac = "Unknown (Unresolved)"
    try:
        if ip_address in ("127.0.0.1", "::1"):
            mac = "00:00:00:00:00:00 (Local Host)"
        else:
            cmd = ["arp", "-a", ip_address] if sys.platform == "win32" else ["arp", "-n", ip_address]
            output = subprocess.check_output(
                cmd, shell=False, stderr=subprocess.DEVNULL, timeout=3
            ).decode("utf-8", errors="ignore")
            m = re.search(r"([0-9a-fA-F]{2}[:-]){5}([0-9a-fA-F]{2})", output)
            if m:
                mac = m.group(0).upper()
    except Exception:
        pass
    with _MAC_LOCK:
        _MAC_CACHE[ip_address] = mac
        _MAC_INFLIGHT.discard(ip_address)


def resolve_mac_async(ip_address):
    """Non-blocking. Returns cached MAC immediately (or 'Resolving...'),
    kicks off a background lookup on first sighting of an IP."""
    with _MAC_LOCK:
        if ip_address in _MAC_CACHE:
            return _MAC_CACHE[ip_address]
        if ip_address not in _MAC_INFLIGHT:
            _MAC_INFLIGHT.add(ip_address)
            t = threading.Thread(target=_resolve_worker, args=(ip_address,), daemon=True)
            t.start()
        return "Resolving..."


def get_local_ip():
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.connect(("8.8.8.8", 80))
        ip = s.getsockname()[0]
        s.close()
        return ip
    except Exception:
        return "127.0.0.1"
