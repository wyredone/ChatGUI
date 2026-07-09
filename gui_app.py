import os
import sys
import time
import threading
import qrcode
import customtkinter as ctk
from tkinter import filedialog, messagebox

from config import (SERVER_CONFIG, STATE_LOCK, CONN_LOG_FILE, ERR_LOG_FILE,
                    load_application_preferences, save_application_preferences)
from network_utils import get_local_ip, resolve_mac_async
from web_server import (app, socketio, set_gui_event_sink, disconnect_all_for_ip,
                        _user_count)
from file_transfer import sweep_orphaned_chunks


class ChatServerApp(ctk.CTk):
    def __init__(self):
        super().__init__()
        self.title("LAN Chat Server")
        load_application_preferences()
        self.geometry(SERVER_CONFIG["window_geometry"])
        ctk.set_appearance_mode(SERVER_CONFIG["web_theme"].capitalize())

        self.protocol('WM_DELETE_WINDOW', self.on_close)
        self._build_ui()

        # ITEM 26: pipe server events into the desktop feed
        set_gui_event_sink(self.feed_event)

        self._start_timers()
        if SERVER_CONFIG["auto_start_on_launch"]:
            self.after(500, self.toggle_server)

    # ---------- UI ----------
    def _build_ui(self):
        self.tabs = ctk.CTkTabview(self)
        self.tabs.pack(fill="both", expand=True, padx=12, pady=10)
        self.dash = self.tabs.add("Dashboard")
        self.users_tab = self.tabs.add("Connected Users")
        self.settings_tab = self.tabs.add("Settings")

        # Dashboard
        info = ctk.CTkFrame(self.dash)
        info.pack(fill="x", padx=10, pady=10)
        self.status_label = ctk.CTkLabel(info, text="Server: Stopped", text_color="#ef4444",
                                         font=ctk.CTkFont(weight="bold", size=16))
        self.status_label.pack(pady=6)
        self.url_label = ctk.CTkLabel(info, text="", font=ctk.CTkFont(size=13))
        self.url_label.pack(pady=2)
        self.copy_btn = ctk.CTkButton(info, text="Copy URL", width=110, command=self.copy_url,
                                      state="disabled")
        self.copy_btn.pack(pady=4)
        self.qr_label = ctk.CTkLabel(info, text="")
        self.qr_label.pack(pady=6)

        ctk.CTkLabel(self.dash, text="Live Feed", anchor="w",
                     font=ctk.CTkFont(weight="bold")).pack(fill="x", padx=14)
        self.feed_box = ctk.CTkTextbox(self.dash, height=240, state="disabled",
                                       fg_color="#141414", text_color="#d4d4d4")
        self.feed_box.pack(fill="both", expand=True, padx=10, pady=6)

        self.toggle_btn = ctk.CTkButton(self.dash, text="Start Server", command=self.toggle_server,
                                        fg_color="#16a34a", hover_color="#15803d", height=44)
        self.toggle_btn.pack(pady=12, fill="x", padx=20)

        # Users tab
        self.users_container = ctk.CTkScrollableFrame(self.users_tab)
        self.users_container.pack(fill="both", expand=True, padx=10, pady=10)

        # Settings tab
        cfg = ctk.CTkScrollableFrame(self.settings_tab)
        cfg.pack(fill="both", expand=True, padx=6, pady=6)

        dir_group = ctk.CTkFrame(cfg); dir_group.pack(fill="x", pady=8, padx=5)
        self.dir_label = ctk.CTkLabel(dir_group, text=f"Shared files folder:\n{SERVER_CONFIG['shared_dir']}",
                                      wraplength=460, anchor="w", justify="left")
        self.dir_label.pack(side="left", padx=15, pady=10, fill="x", expand=True)
        ctk.CTkButton(dir_group, text="Change", width=100, command=self.browse_dir).pack(side="right", padx=15, pady=10)

        net = ctk.CTkFrame(cfg); net.pack(fill="x", pady=8, padx=5)
        self._entry(net, "Server port:", "port", 0)
        self._entry(net, "Max users:", "max_users", 1)
        self._entry(net, "History size (messages):", "history_size", 2)
        self._entry(net, "Max file size (MB):", "max_file_size_mb", 3)
        self._entry(net, "Min free disk (GB):", "min_disk_space_gb", 4)

        opt = ctk.CTkFrame(cfg); opt.pack(fill="x", pady=8, padx=5)
        self.autostart_switch = ctk.CTkSwitch(opt, text="Auto-start server on launch",
                                              command=self.toggle_autostart)
        if SERVER_CONFIG["auto_start_on_launch"]:
            self.autostart_switch.select()
        self.autostart_switch.pack(anchor="w", padx=15, pady=10)

        theme = ctk.CTkFrame(cfg); theme.pack(fill="x", pady=8, padx=5)
        ctk.CTkLabel(theme, text="Theme:").pack(side="left", padx=15, pady=10)
        self.theme_combo = ctk.CTkComboBox(theme, values=["Dark", "Light", "System"],
                                           command=self.change_theme)
        self.theme_combo.set(SERVER_CONFIG["web_theme"].capitalize())
        self.theme_combo.pack(side="right", padx=15, pady=10)

    def _entry(self, parent, label, key, row):
        ctk.CTkLabel(parent, text=label).grid(row=row, column=0, padx=15, pady=6, sticky="w")
        e = ctk.CTkEntry(parent, width=120)
        e.insert(0, str(SERVER_CONFIG[key]))
        e.grid(row=row, column=1, padx=15, pady=6, sticky="e")
        e.bind("<FocusOut>", lambda ev, k=key, w=e: self._save_int(k, w))
        setattr(self, f"entry_{key}", e)

    def _save_int(self, key, widget):
        try:
            SERVER_CONFIG[key] = int(widget.get().strip())
            save_application_preferences(self.geometry())
        except ValueError:
            widget.delete(0, 'end'); widget.insert(0, str(SERVER_CONFIG[key]))

    # ---------- feed ----------
    def feed_event(self, text):
        def _append():
            if self.feed_box.winfo_exists():
                self.feed_box.configure(state="normal")
                self.feed_box.insert("end", f"[{time.strftime('%H:%M:%S')}] {text}\n")
                self.feed_box.see("end")
                self.feed_box.configure(state="disabled")
        try:
            self.after(0, _append)
        except Exception:
            pass

    # ---------- users list ----------
    def render_users(self):
        for w in self.users_container.winfo_children():
            w.destroy()
        with STATE_LOCK:
            conns = list(SERVER_CONFIG["active_connections"].items())
        if not conns:
            ctk.CTkLabel(self.users_container, text="No one connected.",
                         font=ctk.CTkFont(slant="italic")).pack(pady=20)
            return
        for sid, c in conns:
            row = ctk.CTkFrame(self.users_container); row.pack(fill="x", pady=4, padx=2)
            ctk.CTkLabel(row, text=c["nick"], width=140, text_color="#3b82f6",
                         font=ctk.CTkFont(weight="bold")).grid(row=0, column=0, padx=6)
            ctk.CTkLabel(row, text=c["ip"], width=120, text_color="#22c55e").grid(row=0, column=1, padx=6)
            ctk.CTkLabel(row, text=resolve_mac_async(c["ip"]), width=150,
                         font=ctk.CTkFont(family="Courier", size=11),
                         text_color="#eab308").grid(row=0, column=2, padx=6)
            ctk.CTkLabel(row, text=c["joined"], width=70).grid(row=0, column=3, padx=6)
            ctk.CTkButton(row, text="Block", width=60, fg_color="#dc2626", hover_color="#991b1b",
                          command=lambda ip=c["ip"]: self.block_ip(ip)).grid(row=0, column=4, padx=6)

    def block_ip(self, ip):
        with STATE_LOCK:
            if ip not in SERVER_CONFIG["blocked_ips"]:
                SERVER_CONFIG["blocked_ips"].append(ip)
        disconnect_all_for_ip(ip)  # ITEM 12 bundled: kill live sockets too
        save_application_preferences(self.geometry())
        self.feed_event(f"🚫 Blocked {ip}")
        self.render_users()

    # ---------- settings actions ----------
    def browse_dir(self):
        if SERVER_CONFIG["running"]:
            messagebox.showwarning("Server running", "Stop the server before changing the folder.")
            return
        d = filedialog.askdirectory(initialdir=SERVER_CONFIG["shared_dir"])
        if d:
            SERVER_CONFIG["shared_dir"] = d
            SERVER_CONFIG["upload_tmp_dir"] = os.path.join(d, ".upload_tmp")
            os.makedirs(SERVER_CONFIG["upload_tmp_dir"], exist_ok=True)
            self.dir_label.configure(text=f"Shared files folder:\n{d}")
            save_application_preferences(self.geometry())

    def toggle_autostart(self):
        SERVER_CONFIG["auto_start_on_launch"] = (self.autostart_switch.get() == 1)
        save_application_preferences(self.geometry())

    def change_theme(self, choice):
        SERVER_CONFIG["web_theme"] = choice.lower()
        ctk.set_appearance_mode(choice)
        save_application_preferences(self.geometry())

    def copy_url(self):
        self.clipboard_clear()
        self.clipboard_append(self._current_url or "")

    # ---------- server lifecycle ----------
    def toggle_server(self):
        if not SERVER_CONFIG["running"]:
            for k in ["port", "max_users", "history_size", "max_file_size_mb", "min_disk_space_gb"]:
                self._save_int(k, getattr(self, f"entry_{k}"))
            sweep_orphaned_chunks()
            SERVER_CONFIG["running"] = True
            ip = get_local_ip()
            self._current_url = f"http://{ip}:{SERVER_CONFIG['port']}"
            self.status_label.configure(text="Server: Running", text_color="#22c55e")
            self.url_label.configure(text=f"Join at: {self._current_url}")
            self.copy_btn.configure(state="normal")
            self.toggle_btn.configure(text="Stop Server", fg_color="#dc2626", hover_color="#991b1b")

            qr = qrcode.QRCode(box_size=3, border=1)
            qr.add_data(self._current_url); qr.make(fit=True)
            img = qr.make_image(fill_color="black", back_color="white").convert("RGB")
            self.qr_label.configure(image=ctk.CTkImage(light_image=img, size=(150, 150)))
            self.feed_event(f"✅ Server started at {self._current_url}")

            self.server_thread = threading.Thread(target=self._run_server, daemon=True)
            self.server_thread.start()
        else:
            self._stop_server()

    def _run_server(self):
        try:
            socketio.run(app, host='0.0.0.0', port=SERVER_CONFIG["port"],
                         allow_unsafe_werkzeug=True)
        except Exception as e:
            self.feed_event(f"⚠️ Server error: {e}")

    def _stop_server(self):
        SERVER_CONFIG["running"] = False
        try:
            socketio.stop()
        except Exception:
            pass
        self.status_label.configure(text="Server: Stopped", text_color="#ef4444")
        self.url_label.configure(text="")
        self.qr_label.configure(image=None)
        self.copy_btn.configure(state="disabled")
        self.toggle_btn.configure(text="Start Server", fg_color="#16a34a", hover_color="#15803d")
        self.feed_event("⏹ Server stopped.")

    def _start_timers(self):
        def tick():
            if self.tabs.get() == "Connected Users":
                self.render_users()
            self.after(1500, tick)
        self.after(1500, tick)

    def on_close(self):
        save_application_preferences(self.geometry())
        try:
            if SERVER_CONFIG["running"]:
                socketio.stop()
        except Exception:
            pass
        self.quit(); self.destroy(); sys.exit(0)
