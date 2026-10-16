"""Windows room launcher. Uses a worker event loop; Tk stays on the main thread."""
from __future__ import annotations

import asyncio
import json
import os
from pathlib import Path
import queue
import ssl
import subprocess
import sys
import threading
import tkinter as tk
from tkinter import ttk, filedialog, messagebox

from .client import RoomClient, EmulatorBridge
from .transport import RelayServer
from .play_profiles import generate_play_profile, cabinet_initialized, configure_cabinet
from .hosting import host_identity, import_invitation, make_invitation, validate_host

APP_DIR = Path(os.environ.get("LOCALAPPDATA", str(Path.home()))) / "SeedDestinyOnline"
PROJECT = Path(__file__).resolve().parent.parent
BUNDLE_DIR = Path(sys.executable).resolve().parent if getattr(sys, "frozen", False) else PROJECT
SETTINGS = APP_DIR / "launcher.json"
GRAPHICS = {"Original · Vulkan": ("Vulkan", 1), "Enhanced 3× · OpenGL": ("OpenGL", 3)}


class Launcher(tk.Tk):
    def __init__(self):
        super().__init__()
        self.title("SEED Destiny Online — Private Rooms")
        self.geometry("1000x740")
        self.minsize(940, 720)
        self.configure(bg="#101827")
        self.messages = queue.Queue()
        self.loop = asyncio.new_event_loop()
        self.worker = threading.Thread(target=self.loop.run_forever, daemon=True)
        self.worker.start()
        self.client = None
        self.bridge = None
        self.server = None
        self.event_task = None
        self.emulator = None
        self.preparing = False
        self.preparation_process = None
        self.closing = False
        self.connected = False
        self.room_state = {}
        self.local_port = None
        self.host_address = None
        self.host_certificate = None
        self.starting_host = False
        self.status = tk.StringVar(value="Create a private room or join with a room code and passcode.")
        defaults = {"name": "Player", "host": "127.0.0.1", "port": "7443", "tls": False, "ca": "", "iso": "", "dongle": "", "emulator": "", "graphics": "Original · Vulkan"}
        if SETTINGS.exists():
            try:
                defaults.update(json.loads(SETTINGS.read_text(encoding="utf-8")))
            except (ValueError, OSError):
                pass
        # Only discover the explicit game folder beside this project.
        folder_name = "NM00034 SEED DESTINY Rengou vs Z.A.F.T. II"
        candidates = [PROJECT.parent / folder_name, BUNDLE_DIR.parent.parent.parent / folder_name]
        folder = next((p for p in candidates if p.exists()), candidates[0])
        if folder.exists():
            for key, pattern in (("iso", "*.iso"), ("dongle", "*.bin")):
                if not defaults[key]:
                    defaults[key] = str(next(folder.glob(pattern), ""))
        defaults.pop("bios", None)
        defaults.pop("boot", None)
        runtime = BUNDLE_DIR / "emulator" if (BUNDLE_DIR / "emulator" / "Play.exe").exists() else APP_DIR / "runtime-play"
        if runtime.joinpath("Play.exe").is_file():
            defaults["emulator"] = str(runtime / "Play.exe")
        if defaults["graphics"] not in GRAPHICS:
            defaults["graphics"] = "Original · Vulkan"
        self.values = {k: (tk.BooleanVar(value=v) if k == "tls" else tk.StringVar(value=v)) for k, v in defaults.items()}
        self.passcode = tk.StringVar()
        self.code = tk.StringVar()
        self.public_host = tk.StringVar()
        self.host_port = tk.StringVar(value="7443")
        self.host_status = tk.StringVar(value="The host must keep this app and PC running during the match.")
        self._styles()
        self._layout()
        self.protocol("WM_DELETE_WINDOW", self.close_app)
        self.after(100, self.poll)
        self._readiness()

    def _styles(self):
        s = ttk.Style(self)
        s.theme_use("clam")
        s.configure("TFrame", background="#101827")
        s.configure("Card.TFrame", background="#19263a")
        s.configure("TLabel", background="#101827", foreground="#e8edf7", font=("Segoe UI", 10))
        s.configure("Card.TLabel", background="#19263a", foreground="#e8edf7")
        s.configure("Muted.TLabel", foreground="#a8b8d0")
        s.configure("Title.TLabel", font=("Segoe UI", 25, "bold"), foreground="#edf5ff")
        s.configure("Section.TLabel", font=("Segoe UI", 13, "bold"))
        s.configure("TButton", padding=(12, 7), font=("Segoe UI", 10), background="#283f5c", foreground="white")
        s.map("TButton", background=[("active", "#3b5a80"), ("disabled", "#27303d")], foreground=[("disabled", "#8b96a4")])
        s.configure("Accent.TButton", background="#087b92")
        s.configure("TEntry", fieldbackground="#0c1422", foreground="#edf5ff", padding=7, insertcolor="white")
        s.configure("TCombobox", fieldbackground="#0c1422", foreground="#edf5ff", padding=6)
        s.map("TCombobox", fieldbackground=[("readonly", "#0c1422")], foreground=[("readonly", "#edf5ff")])
        s.configure("TCheckbutton", background="#101827", foreground="#d8e6f8")
        s.configure("TNotebook", background="#101827", borderwidth=0)
        s.configure("TNotebook.Tab", padding=(18, 9), background="#19263a", foreground="#dbe7f9")
        s.map("TNotebook.Tab", background=[("selected", "#087b92")])

    def _layout(self):
        outer = ttk.Frame(self, padding=18)
        outer.pack(fill="both", expand=True)
        ttk.Label(outer, text="SEED DESTINY", style="Title.TLabel").pack(anchor="w")
        ttk.Label(outer, text="PRIVATE ROOMS  /  WINDOWS ARCADE  /  2 VS 2", style="Muted.TLabel").pack(anchor="w", pady=(3, 10))
        self.readiness = ttk.Label(outer, wraplength=990, style="Muted.TLabel")
        self.readiness.pack(anchor="w", pady=(0, 12))
        # Reserve status space before the expandable notebook, including at high DPI.
        self.status_label = ttk.Label(outer, textvariable=self.status, wraplength=920)
        self.status_label.pack(side="bottom", anchor="w", fill="x", pady=(8, 0))
        tabs = ttk.Notebook(outer)
        tabs.pack(fill="both", expand=True)
        room = ttk.Frame(tabs, padding=14)
        setup = ttk.Frame(tabs, padding=20)
        tabs.add(room, text="Playroom")
        tabs.add(setup, text="Game & graphics")
        hosting = ttk.Frame(tabs, padding=20)
        tabs.add(hosting, text="Host on this PC")
        hosting.columnconfigure(1, weight=1)
        self._row(hosting, 0, "Internet address or DNS name", self.public_host)
        self._row(hosting, 1, "Room server port", self.host_port)
        ttk.Label(hosting, text="Enter the address your friends can reach. Forward this TCP port in your router to this PC and allow the launcher through the firewall. Keep your VPN configuration in mind when choosing a reachable address.", wraplength=840, style="Muted.TLabel").grid(row=2, column=0, columnspan=2, sticky="w", pady=18)
        host_buttons = ttk.Frame(hosting)
        host_buttons.grid(row=3, column=0, columnspan=2, sticky="w")
        ttk.Button(host_buttons, text="Start encrypted room server", command=self.start_host, style="Accent.TButton").pack(side="left", padx=(0, 12))
        ttk.Button(host_buttons, text="Stop server", command=self.stop_host).pack(side="left")
        ttk.Label(hosting, textvariable=self.host_status, wraplength=840).grid(row=4, column=0, columnspan=2, sticky="w", pady=20)
        ttk.Label(hosting, text="After starting, return to Playroom and create a room. Copy the invitation and send it to your friends with the installer. Share the passcode separately. Friends use Paste invitation, enter their name and passcode, then Join room.\n\nInvitations include the public certificate for your server; the private key stays on this PC. Changing your internet address requires restarting the server and sending a new invitation.\n\nStarting the server does not prove that your router allows incoming connections. Test the invitation from a different internet connection before the match.", wraplength=840, style="Muted.TLabel").grid(row=5, column=0, columnspan=2, sticky="w", pady=10)
        room.columnconfigure(1, weight=1)
        self._row(room, 0, "Player name", self.values["name"])
        row = ttk.Frame(room)
        row.grid(row=1, column=0, columnspan=2, sticky="ew", pady=8)
        ttk.Label(row, text="Room server").pack(side="left", padx=(0, 15))
        ttk.Entry(row, textvariable=self.values["host"], width=27).pack(side="left", fill="x", expand=True)
        ttk.Label(row, text=":").pack(side="left", padx=5)
        ttk.Entry(row, textvariable=self.values["port"], width=7).pack(side="left")
        ttk.Checkbutton(row, text="TLS", variable=self.values["tls"]).pack(side="left", padx=12)
        ttk.Button(row, text="Local test server", command=self.local_server).pack(side="left")
        ttk.Button(row, text="Paste invitation", command=self.paste_invite).pack(side="left", padx=(8, 0))
        self._row(room, 2, "Room code", self.code)
        self._row(room, 3, "Passcode", self.passcode, show="•")
        buttons = ttk.Frame(room)
        buttons.grid(row=4, column=0, columnspan=2, sticky="ew", pady=8)
        self.create_button = ttk.Button(buttons, text="Create private room", style="Accent.TButton", command=lambda: self.connect_room(True))
        self.create_button.pack(side="left", padx=(0, 8))
        self.join_button = ttk.Button(buttons, text="Join room", command=lambda: self.connect_room(False))
        self.join_button.pack(side="left", padx=8)
        self.leave_button = ttk.Button(buttons, text="Leave", command=self.leave_room, state="disabled")
        self.leave_button.pack(side="left", padx=8)
        self.copy_button = ttk.Button(buttons, text="Copy invitation", command=self.copy_invite, state="disabled")
        self.copy_button.pack(side="right")
        self.room_label = ttk.Label(room, text="NO ROOM CONNECTED", style="Section.TLabel")
        self.room_label.grid(row=5, column=0, columnspan=2, sticky="w", pady=(6, 12))
        seats = ttk.Frame(room)
        seats.grid(row=6, column=0, columnspan=2, sticky="ew")
        seats.columnconfigure((0, 1), weight=1)
        self.seats = []
        for i in range(4):
            card = ttk.Frame(seats, style="Card.TFrame", padding=10)
            card.grid(row=i % 2, column=i // 2, sticky="ew", padx=(0, 10), pady=5)
            label = ttk.Label(card, text=f"CABINET {i + 1}   ·   Waiting for player", style="Card.TLabel")
            label.pack(anchor="w")
            self.seats.append(label)
        ttk.Label(room, text="Lobby seats identify cabinets. Choose the 2-on-2 teams in the game's own menu.", style="Muted.TLabel", wraplength=900).grid(row=7, column=0, columnspan=2, sticky="w", pady=10)
        actions = ttk.Frame(room)
        actions.grid(row=8, column=0, columnspan=2, sticky="ew", pady=5)
        self.launch_button = ttk.Button(actions, text="Launch my game", style="Accent.TButton", command=self.launch_game, state="disabled")
        self.launch_button.pack(side="left", padx=(0, 10))
        self.ready_button = ttk.Button(actions, text="Ready", command=self.ready, state="disabled")
        self.ready_button.pack(side="left", padx=5)
        self.start_button = ttk.Button(actions, text="All ready · coordinate start", command=self.start_match, state="disabled")
        self.start_button.pack(side="right")
        ttk.Label(room, text="All four players should join and launch before starting. Internet connection quality affects match speed.", style="Muted.TLabel", wraplength=900).grid(row=9, column=0, columnspan=2, sticky="w", pady=12)
        setup.columnconfigure(1, weight=1)
        for i, (key, label) in enumerate((("emulator", "Included game player .exe"), ("iso", "Your game disc .iso"), ("dongle", "Your original dongle .bin"), ("ca", "Server certificate (optional)"))):
            self._row(setup, i, label, self.values[key], browse=key)
        ttk.Label(setup, text="Graphics profile").grid(row=6, column=0, sticky="w", pady=14)
        ttk.Combobox(setup, textvariable=self.values["graphics"], values=list(GRAPHICS), state="readonly").grid(row=6, column=1, sticky="ew")
        ttk.Label(setup, text="Enhanced OpenGL renders at 3× resolution (1920 × 1344) with display synchronization to address flashing. Visual confirmation is pending, and some scenes still have missing scenery. Original Vulkan remains the stable option.", style="Muted.TLabel", wraplength=850).grid(row=7, column=0, columnspan=3, sticky="w", pady=12)
        ttk.Button(setup, text="Save and check files", command=self.save_settings).grid(row=8, column=1, sticky="w", pady=8)
        ttk.Label(setup, text="Each player supplies their disc and dongle. Cabinet setup runs automatically the first time. No separate BIOS is needed. Use Backspace for credit and Enter to start; configure a controller in the game player's settings.", style="Muted.TLabel", wraplength=850).grid(row=9, column=0, columnspan=3, sticky="w", pady=12)

    def _row(self, parent, row, label, variable, show=None, browse=None):
        ttk.Label(parent, text=label).grid(row=row, column=0, sticky="w", padx=(0, 16), pady=4)
        ttk.Entry(parent, textvariable=variable, show=show or "").grid(row=row, column=1, sticky="ew", pady=4)
        if browse:
            ttk.Button(parent, text="Browse", command=lambda: self.browse(browse)).grid(row=row, column=2, padx=(8, 0))

    def browse(self, key):
        chosen = filedialog.askopenfilename(parent=self, title=f"Select {key}")
        if chosen:
            self.values[key].set(chosen)
            self._readiness()

    def save_settings(self):
        APP_DIR.mkdir(parents=True, exist_ok=True)
        SETTINGS.write_text(json.dumps({k: v.get() for k, v in self.values.items()}, indent=2), encoding="utf-8")
        self._readiness()
        self.status.set("Settings saved. Room passcodes are never saved.")

    def _readiness(self):
        labels = {"emulator": "included game player", "iso": "disc", "dongle": "dongle"}
        missing = [label for k, label in labels.items() if not self.values[k].get() or not Path(self.values[k].get()).is_file()]
        self.readiness.configure(text="Game setup needs: " + ", ".join(missing) if missing else "Game files found. Join a room, then launch your game.")

    def submit(self, coro, success=None):
        f = asyncio.run_coroutine_threadsafe(coro, self.loop)
        def finished(future):
            try:
                self.messages.put(("result", success, future.result()))
            except Exception as exc:
                self.messages.put(("error", None, str(exc)))
        f.add_done_callback(finished)

    def poll(self):
        try:
            while True:
                kind, callback, data = self.messages.get_nowait()
                if kind == "error":
                    self.status.set("Could not complete: " + data)
                    self.preparing = False
                    if self.starting_host:
                        self.starting_host = False
                        self.host_status.set("Could not start the server: " + data)
                    self._buttons()
                elif kind == "room":
                    self.render_room(data)
                elif callback:
                    callback(data)
        except queue.Empty:
            pass
        if not self.closing:
            self.after(100, self.poll)

    def _buttons(self):
        self.create_button.configure(state="disabled" if self.connected else "normal")
        self.join_button.configure(state="disabled" if self.connected else "normal")
        for button in (self.leave_button, self.copy_button, self.ready_button, self.launch_button):
            button.configure(state="normal" if self.connected else "disabled")
        self.start_button.configure(state="normal" if self.connected else "disabled")

    def local_server(self):
        if self.host_address or self.starting_host:
            self.status.set("Your encrypted host is already running. Create a room, or stop it in Host on this PC.")
            return
        async def start():
            if self.server is None:
                self.server = await RelayServer().start(host="127.0.0.1", port=0)
            return self.server.port
        def done(port):
            self.values["host"].set("127.0.0.1")
            self.values["port"].set(str(port))
            self.values["tls"].set(False)
            self.values["ca"].set("")
            self.status.set("Local test server running. This address is only accessible on this PC.")
        self.submit(start(), done)

    def start_host(self):
        if self.server or self.connected or self.starting_host:
            self.host_status.set("Leave your room and stop the existing server before starting a new host.")
            return
        try:
            address = validate_host(self.public_host.get())
            port = int(self.host_port.get())
            if not 1 <= port <= 65535:
                raise ValueError("Choose a port between 1 and 65535.")
        except ValueError as exc:
            self.host_status.set(str(exc))
            return
        self.host_status.set("Preparing your private server…")
        self.starting_host = True
        async def start():
            cert, key = await asyncio.to_thread(host_identity, APP_DIR / 'host', address)
            context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
            context.minimum_version = ssl.TLSVersion.TLSv1_2
            context.load_cert_chain(cert, key)
            self.server = await RelayServer().start(host="0.0.0.0", port=port, ssl_context=context)
            return cert
        def done(cert):
            self.starting_host = False
            self.host_address = address
            self.host_certificate = cert
            self.values['host'].set('127.0.0.1')
            self.values['port'].set(str(port))
            self.values['tls'].set(True)
            self.values['ca'].set(str(cert))
            self.host_status.set(f"Server listening on TCP {port}. Friends will use {address}:{port}. Router reachability has not been verified. Return to Playroom to create a room.")
            self.status.set("Encrypted host is running. Create a private room in Playroom.")
        self.submit(start(), done)

    def stop_host(self):
        if self.starting_host:
            self.host_status.set("Wait for the server to finish starting.")
            return
        if self.connected:
            self.host_status.set("Leave your room first. Stopping the server disconnects every player.")
            return
        async def stop():
            if self.server:
                await self.server.close()
                self.server = None
        def done(_):
            self.host_address = None
            self.host_certificate = None
            self.host_status.set("Server stopped. Existing rooms are closed.")
        self.submit(stop(), done)

    def paste_invite(self):
        if self.connected:
            self.status.set("Leave the current room before importing another invitation.")
            return
        try:
            invitation = import_invitation(self.clipboard_get(), APP_DIR / 'trusted-servers')
            for key in ('host', 'port', 'tls', 'ca'):
                self.values[key].set(invitation[key])
            self.code.set(invitation['room'])
            self.status.set("Invitation loaded. Enter your player name and the passcode from the host, then Join room.")
        except (ValueError, TypeError, OSError, tk.TclError) as exc:
            self.status.set("Could not read invitation: " + str(exc))

    def connect_room(self, create):
        if self.connected:
            return
        host = self.values["host"].get().strip()
        try:
            port = int(self.values["port"].get())
            if not 1 <= port <= 65535:
                raise ValueError()
        except ValueError:
            self.status.set("Enter a port between 1 and 65535.")
            return
        context = None
        try:
            if self.values["tls"].get():
                ca = self.values["ca"].get().strip()
                context = ssl.create_default_context(cafile=ca or None)
            elif host not in ("localhost", "127.0.0.1", "::1"):
                raise ValueError("Enable TLS for a remote server.")
        except (ValueError, OSError) as exc:
            self.status.set(str(exc))
            return
        password, code, name = self.passcode.get(), self.code.get().strip().upper(), self.values["name"].get().strip()
        if len(password) < 8:
            self.status.set("Use a passcode of at least 8 characters.")
            return
        self.create_button.configure(state="disabled")
        self.join_button.configure(state="disabled")
        self.status.set("Connecting to room server…")
        async def connect():
            client = await RoomClient.connect(host, port, ssl_context=context, server_hostname=host if context else None)
            try:
                if create:
                    await client.create(password, name=name)
                else:
                    await client.join(code, password, name=name)
                bridge = await EmulatorBridge(client).start(port=0)
            except BaseException:
                await client.close()
                raise
            self.client, self.bridge = client, bridge
            async def events():
                while True:
                    event = await client.events.get()
                    self.messages.put(("room", None, event))
            self.event_task = asyncio.create_task(events())
            return {"room": client.state, "port": bridge.port}
        def done(result):
            self.connected = True
            self.local_port = result["port"]
            self.passcode.set("")
            self._buttons()
            self.render_room(result["room"])
            self.status.set("Connected. Share the room code and passcode privately; each player launches their own game.")
        self.submit(connect(), done)

    def render_room(self, state):
        if isinstance(state, dict) and state.get("event") == "disconnected":
            self.connected = False
            self._buttons()
            self.room_label.configure(text="SERVER DISCONNECTED")
            self.status.set("Room connection lost. Leave/rejoin after restoring the server; restart the game link if needed.")
            self.submit(self.disconnect())
            return
        if isinstance(state, dict) and isinstance(state.get("state"), dict):
            state = state["state"]
        if isinstance(state, dict) and "room" in state and isinstance(state["room"], dict):
            state = state["room"]
        if not isinstance(state, dict):
            return
        self.room_state = state
        code = state.get("code") or state.get("room_code") or (self.client.room_code if self.client else "")
        if code:
            self.code.set(code)
            self.room_label.configure(text=f"ROOM {code}   ·   4 CABINETS")
        members = state.get("members", state.get("players", []))
        if isinstance(members, dict):
            members = list(members.values())
        for i, label in enumerate(self.seats):
            member = next((m for m in members if m and m.get("cabinet_id") == i + 1), None)
            label.configure(text=f"CABINET {i + 1}   ·   " + (f"{member.get('name', 'Player')}   {'READY' if member.get('ready') else 'IN ROOM'}" if member else "Waiting for player"))
        if state.get("started"):
            self.status.set("All players ready. Coordinate the match from the game's network menu.")

    def copy_invite(self):
        try:
            cert_path = self.values['ca'].get()
            certificate = Path(cert_path).read_text(encoding='ascii') if cert_path else ''
            address = self.values['host'].get()
            if self.host_address and address == '127.0.0.1' and self.server and int(self.values['port'].get()) == self.server.port:
                address = self.host_address
            text = make_invitation(address, int(self.values['port'].get()), self.code.get(), certificate, self.values['tls'].get())
        except (ValueError, OSError) as exc:
            self.status.set("Could not create invitation: " + str(exc))
            return
        self.clipboard_clear()
        self.clipboard_append(text)
        self.status.set("Invitation copied. It does not contain your passcode.")

    def ready(self):
        if self.client:
            self.submit(self.client.ready(True), lambda _: self.status.set("Marked ready. Launch and configure your game before coordinating a match."))

    def start_match(self):
        if self.client:
            self.submit(self.client.start_match(), lambda _: self.status.set("Lobby start signal sent. Use the game's controls to start the match."))

    def launch_game(self):
        if not self.client or not self.local_port:
            return
        if self.preparing:
            return
        if self.emulator and self.emulator.poll() is None:
            self.status.set("Your emulator is already running.")
            return
        self.save_settings()
        self.preparing = True
        self.status.set("Preparing your player cabinet. First launch can take up to a minute…")
        client = self.client
        renderer, scale = GRAPHICS.get(self.values["graphics"].get(), ("Vulkan", 1))
        arguments = dict(seat=client.cabinet_id, emulator_path=self.values["emulator"].get(),
                         iso_path=self.values["iso"].get(), dongle_path=self.values["dongle"].get(),
                         bridge_port=self.local_port, renderer=renderer, scale=scale)

        async def prepare():
            profile = await asyncio.to_thread(generate_play_profile, APP_DIR / "players", **arguments)
            if self.closing or self.client is not client:
                return None
            if not cabinet_initialized(profile.backup_ram):
                env = profile.environment.copy()
                env.pop("SEED_UEPCB_ENABLE", None)
                process = await asyncio.create_subprocess_exec(*profile.preparation_command,
                    cwd=profile.data_dir, env=env, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
                self.preparation_process = process
                try:
                    code = await asyncio.wait_for(process.wait(), timeout=75)
                    if code != 0:
                        raise ValueError("Cabinet preparation did not finish. Check your disc and dongle files and retry.")
                finally:
                    if process.returncode is None:
                        process.terminate()
                        await process.wait()
                    self.preparation_process = None
            await asyncio.to_thread(configure_cabinet, profile)
            return profile

        def launch(profile):
            self.preparing = False
            if profile is None or self.closing or self.client is not client:
                return
            try:
                self.emulator = subprocess.Popen(profile.command, cwd=profile.data_dir, env=profile.environment)
                self.status.set(f"Player {profile.seat} launched. Wait for everyone to connect, then insert credit and start.")
            except OSError as exc:
                self.status.set("Game cannot start: " + str(exc))
        self.submit(prepare(), launch)

    async def disconnect(self):
        if self.preparation_process and self.preparation_process.returncode is None:
            self.preparation_process.terminate()
            await self.preparation_process.wait()
        if self.event_task:
            self.event_task.cancel()
            self.event_task = None
        if self.bridge:
            await self.bridge.close()
            self.bridge = None
        if self.client:
            await self.client.close()
            self.client = None

    def leave_room(self):
        def done(_):
            self.connected = False
            self.local_port = None
            self._buttons()
            self.room_label.configure(text="NO ROOM CONNECTED")
            for i, label in enumerate(self.seats):
                label.configure(text=f"CABINET {i + 1}   ·   Waiting for player")
            self.status.set("Left the room. Any running emulator stays open; its network link is disconnected.")
        self.submit(self.disconnect(), done)

    def close_app(self):
        self.closing = True
        async def cleanup():
            await self.disconnect()
            if self.server:
                await self.server.close()
        future = asyncio.run_coroutine_threadsafe(cleanup(), self.loop)
        try:
            future.result(timeout=3)
        except Exception:
            pass
        self.loop.call_soon_threadsafe(self.loop.stop)
        self.destroy()


def main():
    Launcher().mainloop()


if __name__ == "__main__":
    main()
