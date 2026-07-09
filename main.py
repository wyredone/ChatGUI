import sys
import subprocess

# --- Dependency auto-installer ---
_REQUIRED = {
    "customtkinter": "customtkinter",
    "flask": "flask",
    "flask_socketio": "flask-socketio",
    "qrcode": "qrcode[pil]",
    "PIL": "pillow",
    "engineio": "python-engineio",
    "socketio": "python-socketio",
    "simple_websocket": "simple-websocket",
}

def _ensure_deps():
    for module_name, pip_name in _REQUIRED.items():
        try:
            __import__(module_name)
        except ImportError:
            try:
                subprocess.check_call([sys.executable, "-m", "pip", "install", pip_name])
            except Exception as e:
                print(f"[warn] Could not auto-install {pip_name}: {e}")

_ensure_deps()

from gui_app import ChatServerApp

if __name__ == "__main__":
    app_gui = ChatServerApp()
    app_gui.mainloop()
