#!/usr/bin/env python3
"""
TinyTask for Linux (Wayland edition)
-------------------------------------
A mouse & keyboard macro recorder/player that works on Wayland sessions.

Wayland blocks normal apps from listening to or injecting global input
through the display server (a security feature, not a bug). So this
version talks to the kernel directly instead of the display server:

  - RECORDING reads raw events straight from /dev/input/event* via
    python-evdev. This works no matter what window is focused or
    whether this app is minimized.
  - PLAYBACK injects events through the kernel's uinput interface via
    the `ydotool` command-line tool (and its `ydotoold` background
    daemon), which is the standard Wayland-safe equivalent of pynput's
    input-control feature.

ONE-TIME SETUP (see README-wayland.md for the full walkthrough):
    sudo apt install ydotool
    pip3 install evdev --break-system-packages
    sudo usermod -aG input $USER        # then log out & back in
    sudo -b ydotoold --socket-path="$HOME/.ydotool_socket" \\
        --socket-own="$(id -u):$(id -g)"
    export YDOTOOL_SOCKET="$HOME/.ydotool_socket"   # add to ~/.bashrc too

Run:
    python3 tinytask_linux_wayland.py
"""

import json
import os
import select
import subprocess
import threading
import time
import tkinter as tk
from tkinter import ttk, messagebox, simpledialog

import evdev
from evdev import ecodes

CONFIG_DIR = os.path.expanduser("~/.config/tinytask-linux")
MACROS_DIR = os.path.join(CONFIG_DIR, "macros")
os.makedirs(MACROS_DIR, exist_ok=True)

MOVE_THROTTLE = 0.03  # seconds between flushed mouse-move samples

# Make sure ydotool can find the daemon socket even if the user didn't
# export YDOTOOL_SOCKET in this particular shell.
os.environ.setdefault("YDOTOOL_SOCKET", os.path.expanduser("~/.ydotool_socket"))

HOTKEY_F9 = ecodes.KEY_F9    # stop recording
HOTKEY_F10 = ecodes.KEY_F10  # start/stop playback
HOTKEY_ESC = ecodes.KEY_ESC  # abort playback


# --------------------------------------------------------------------------
# Low-level: continuously read every keyboard/mouse device from the kernel
# --------------------------------------------------------------------------
class EvdevHub:
    """
    Runs for the lifetime of the app. Always watches for hotkeys.
    While `recording` is True, also appends raw events into `events`.
    """

    def __init__(self, hotkey_callback):
        self.hotkey_callback = hotkey_callback
        self.events = []
        self.recording = False
        self.start_time = 0.0
        self._dx = 0
        self._dy = 0
        self._last_flush = 0.0
        self._running = False
        self._thread = None
        self._devices = {}
        self._error = None

    def _open_devices(self):
        devices = {}
        try:
            paths = evdev.list_devices()
        except Exception as e:
            self._error = f"Could not list input devices: {e}"
            return devices
        if not paths:
            self._error = (
                "No /dev/input/event* devices were visible. "
                "Are you in the 'input' group? (sudo usermod -aG input $USER, "
                "then log out and back in)"
            )
        for path in paths:
            try:
                dev = evdev.InputDevice(path)
                caps = dev.capabilities()
                if ecodes.EV_KEY in caps or ecodes.EV_REL in caps:
                    devices[dev.fd] = dev
            except PermissionError:
                self._error = (
                    f"Permission denied opening {path}. "
                    "Add yourself to the 'input' group: "
                    "sudo usermod -aG input $USER (then log out and back in)."
                )
            except OSError:
                pass
        return devices

    def start(self):
        self._devices = self._open_devices()
        self._running = True
        self._thread = threading.Thread(target=self._loop, daemon=True)
        self._thread.start()

    def stop(self):
        self._running = False
        for dev in self._devices.values():
            try:
                dev.close()
            except Exception:
                pass

    def begin_recording(self):
        self.events = []
        self.start_time = time.time()
        self._dx = 0
        self._dy = 0
        self._last_flush = 0.0
        self.recording = True

    def end_recording(self):
        self._flush_move(time.time() - self.start_time)
        self.recording = False
        return self.events

    def _flush_move(self, t):
        if self._dx or self._dy:
            self.events.append({"t": t, "type": "move", "dx": self._dx, "dy": self._dy})
            self._dx = 0
            self._dy = 0
            self._last_flush = t

    def _loop(self):
        while self._running:
            if not self._devices:
                time.sleep(0.5)
                self._devices = self._open_devices()
                continue
            try:
                r, _, _ = select.select(self._devices.keys(), [], [], 0.2)
            except Exception:
                time.sleep(0.2)
                continue
            for fd in r:
                dev = self._devices.get(fd)
                if dev is None:
                    continue
                try:
                    for event in dev.read():
                        self._handle(event)
                except (OSError, BlockingIOError):
                    pass

    def _handle(self, event):
        t = time.time() - self.start_time if self.recording else 0.0

        if event.type == ecodes.EV_KEY:
            code, value = event.code, event.value
            if value == 2:  # ignore key-repeat
                return
            if code == HOTKEY_F9 and value == 1:
                self.hotkey_callback("f9")
                return
            if code == HOTKEY_F10 and value == 1:
                self.hotkey_callback("f10")
                return
            if code == HOTKEY_ESC and value == 1:
                self.hotkey_callback("esc")
            if self.recording:
                self.events.append({"t": t, "type": "key", "code": code, "value": value})

        elif event.type == ecodes.EV_REL and self.recording:
            if event.code == ecodes.REL_X:
                self._dx += event.value
            elif event.code == ecodes.REL_Y:
                self._dy += event.value
            elif event.code in (ecodes.REL_WHEEL, ecodes.REL_HWHEEL):
                self.events.append(
                    {"t": t, "type": "scroll", "code": event.code, "value": event.value}
                )

        elif event.type == ecodes.EV_SYN and self.recording:
            if t - self._last_flush >= MOVE_THROTTLE:
                self._flush_move(t)


# --------------------------------------------------------------------------
# Playback via ydotool (kernel uinput injection — works on Wayland)
# --------------------------------------------------------------------------
class MacroPlayer:
    def __init__(self, events, on_finish=None, on_progress=None):
        self.events = events
        self.on_finish = on_finish
        self.on_progress = on_progress
        self._abort = threading.Event()

    def play(self, loops=1, speed=1.0):
        threading.Thread(target=self._run, args=(loops, speed), daemon=True).start()

    def abort(self):
        self._abort.set()

    def _run(self, loops, speed):
        self._abort.clear()
        total = len(self.events)
        error = None

        for loop_i in range(loops):
            if self._abort.is_set():
                break
            last_t = 0.0
            for idx, ev in enumerate(self.events):
                if self._abort.is_set():
                    break
                delay = (ev["t"] - last_t) / max(speed, 0.01)
                if delay > 0:
                    time.sleep(delay)
                last_t = ev["t"]
                try:
                    self._dispatch(ev)
                except FileNotFoundError:
                    error = "ydotool is not installed or not on PATH."
                    self._abort.set()
                    break
                except subprocess.CalledProcessError as e:
                    error = f"ydotool failed: {e}"
                if self.on_progress:
                    self.on_progress(loop_i + 1, loops, idx + 1, total)

        if self.on_finish:
            self.on_finish(aborted=self._abort.is_set(), error=error)

    def _dispatch(self, ev):
        etype = ev["type"]
        if etype == "move":
            subprocess.run(
                ["ydotool", "mousemove", "--", str(ev["dx"]), str(ev["dy"])],
                check=True, capture_output=True,
            )
        elif etype == "scroll":
            if ev["code"] == ecodes.REL_WHEEL:
                args = ["ydotool", "mousemove", "--wheel", "--", "0", str(ev["value"])]
            else:
                args = ["ydotool", "mousemove", "--wheel", "--", str(ev["value"]), "0"]
            subprocess.run(args, check=True, capture_output=True)
        elif etype == "key":
            state = 1 if ev["value"] == 1 else 0
            subprocess.run(
                ["ydotool", "key", f"{ev['code']}:{state}"], check=True, capture_output=True
            )


# --------------------------------------------------------------------------
# GUI
# --------------------------------------------------------------------------
class App:
    def __init__(self, root):
        self.root = root
        root.title("TinyTask for Linux (Wayland)")
        root.geometry("440x560")
        root.minsize(420, 480)
        root.resizable(True, True)

        self.current_events = []
        self.is_recording = False
        self.player = None

        self.hub = EvdevHub(hotkey_callback=self._on_hotkey)

        self._build_ui()
        self._refresh_macro_list()
        self.hub.start()
        self.root.after(300, self._check_hub_error)

    # ---------------- UI layout ----------------
    def _build_ui(self):
        pad = {"padx": 8, "pady": 6}

        title = ttk.Label(
            self.root, text="TinyTask for Linux", font=("Sans", 16, "bold")
        )
        title.pack(pady=(10, 0))
        subtitle = ttk.Label(
            self.root, text="Wayland edition — evdev + ydotool", foreground="#666"
        )
        subtitle.pack(pady=(0, 10))

        ctrl_frame = ttk.LabelFrame(self.root, text="Controls")
        ctrl_frame.pack(fill="x", **pad)

        self.record_btn = ttk.Button(ctrl_frame, text="● Record", command=self.toggle_record)
        self.record_btn.grid(row=0, column=0, padx=6, pady=8, sticky="ew")

        self.play_btn = ttk.Button(ctrl_frame, text="▶ Play (F10)", command=self.toggle_play)
        self.play_btn.grid(row=0, column=1, padx=6, pady=8, sticky="ew")
        ctrl_frame.columnconfigure(0, weight=1)
        ctrl_frame.columnconfigure(1, weight=1)

        hint = ttk.Label(
            ctrl_frame,
            text="F9 stops recording  •  F10 starts/stops playback  •  Esc aborts playback\n"
            "All hotkeys work even while minimized.",
            foreground="#888",
            font=("Sans", 8),
            justify="center",
        )
        hint.grid(row=1, column=0, columnspan=2, padx=6, pady=(0, 8))

        opt_frame = ttk.LabelFrame(self.root, text="Playback options")
        opt_frame.pack(fill="x", **pad)

        ttk.Label(opt_frame, text="Loops:").grid(row=0, column=0, padx=6, pady=6, sticky="w")
        self.loops_var = tk.StringVar(value="1")
        ttk.Spinbox(
            opt_frame, from_=1, to=9999, textvariable=self.loops_var, width=6
        ).grid(row=0, column=1, padx=6, pady=6, sticky="w")

        self.infinite_var = tk.BooleanVar(value=False)
        ttk.Checkbutton(
            opt_frame, text="Infinite loop", variable=self.infinite_var
        ).grid(row=0, column=2, padx=6, pady=6, sticky="w")

        ttk.Label(opt_frame, text="Speed:").grid(row=1, column=0, padx=6, pady=6, sticky="w")
        self.speed_var = tk.DoubleVar(value=1.0)
        ttk.Scale(
            opt_frame, from_=0.1, to=5.0, variable=self.speed_var, orient="horizontal"
        ).grid(row=1, column=1, columnspan=2, padx=6, pady=6, sticky="ew")
        opt_frame.columnconfigure(2, weight=1)

        list_frame = ttk.LabelFrame(self.root, text="Saved macros")
        list_frame.pack(fill="both", expand=True, **pad)

        self.macro_listbox = tk.Listbox(list_frame, height=8)
        self.macro_listbox.pack(fill="both", expand=True, padx=6, pady=6)
        self.macro_listbox.bind("<Double-Button-1>", lambda e: self.load_selected())

        btn_row = ttk.Frame(list_frame)
        btn_row.pack(fill="x", padx=6, pady=(0, 6))
        ttk.Button(btn_row, text="Save current...", command=self.save_current).pack(
            side="left", expand=True, fill="x", padx=2
        )
        ttk.Button(btn_row, text="Load selected", command=self.load_selected).pack(
            side="left", expand=True, fill="x", padx=2
        )
        ttk.Button(btn_row, text="Delete selected", command=self.delete_selected).pack(
            side="left", expand=True, fill="x", padx=2
        )

        self.status_var = tk.StringVar(value="Ready. 0 events loaded.")
        status = ttk.Label(self.root, textvariable=self.status_var, relief="sunken", anchor="w")
        status.pack(fill="x", side="bottom")

    def _check_hub_error(self):
        if self.hub._error:
            messagebox.showwarning("Input access issue", self.hub._error)

    # ---------------- Hotkey dispatch (runs on evdev thread -> hop to UI thread) ----------------
    def _on_hotkey(self, name):
        if name == "f9":
            self.root.after(0, self._stop_recording_ui)
        elif name == "f10":
            self.root.after(0, self.toggle_play)
        elif name == "esc":
            self.root.after(0, self._abort_playback_ui)

    # ---------------- Recording ----------------
    def toggle_record(self):
        if self.is_recording:
            return
        self.is_recording = True
        self.hub.begin_recording()
        self.record_btn.config(text="■ Recording... (F9 to stop)")
        self.play_btn.config(state="disabled")
        self.status_var.set("Recording... switch to your target window now. Press F9 to stop.")

    def _stop_recording_ui(self):
        if not self.is_recording:
            return
        events = self.hub.end_recording()
        self.is_recording = False
        self.current_events = events
        self.record_btn.config(text="● Record")
        self.play_btn.config(state="normal")
        self.status_var.set(f"Recording stopped. {len(events)} events captured.")

    # ---------------- Playback ----------------
    def toggle_play(self):
        if self.player is not None:
            self.player.abort()
            return
        if not self.current_events:
            messagebox.showinfo("No macro", "Record or load a macro first.")
            return

        loops = 999999 if self.infinite_var.get() else max(1, int(self.loops_var.get() or 1))
        speed = max(0.1, float(self.speed_var.get()))

        self.player = MacroPlayer(
            self.current_events, on_finish=self._on_play_finished, on_progress=self._on_progress
        )
        self.play_btn.config(text="■ Stop (F10)")
        self.record_btn.config(state="disabled")
        self.status_var.set("Playing... press F10 or Esc to stop.")
        self.player.play(loops=loops, speed=speed)

    def _abort_playback_ui(self):
        if self.player is not None:
            self.player.abort()

    def _on_progress(self, loop_i, loops, ev_i, total):
        def update():
            loop_text = "∞" if self.infinite_var.get() else str(loops)
            self.status_var.set(f"Playing loop {loop_i}/{loop_text} — event {ev_i}/{total}")

        self.root.after(0, update)

    def _on_play_finished(self, aborted, error=None):
        def update():
            self.player = None
            self.play_btn.config(text="▶ Play (F10)")
            self.record_btn.config(state="normal")
            if error:
                self.status_var.set(f"Stopped: {error}")
                messagebox.showerror("Playback error", error)
            else:
                self.status_var.set("Playback aborted." if aborted else "Playback finished.")

        self.root.after(0, update)

    # ---------------- Save / load / delete ----------------
    def _refresh_macro_list(self):
        self.macro_listbox.delete(0, tk.END)
        for fname in sorted(os.listdir(MACROS_DIR)):
            if fname.endswith(".json"):
                self.macro_listbox.insert(tk.END, fname[:-5])

    def save_current(self):
        if not self.current_events:
            messagebox.showinfo("Nothing to save", "Record a macro first.")
            return
        name = simpledialog.askstring("Save macro", "Macro name:")
        if not name:
            return
        path = os.path.join(MACROS_DIR, f"{name}.json")
        with open(path, "w") as f:
            json.dump(self.current_events, f)
        self._refresh_macro_list()
        self.status_var.set(f"Saved macro '{name}' ({len(self.current_events)} events).")

    def load_selected(self):
        sel = self.macro_listbox.curselection()
        if not sel:
            messagebox.showinfo("No selection", "Select a macro from the list first.")
            return
        name = self.macro_listbox.get(sel[0])
        path = os.path.join(MACROS_DIR, f"{name}.json")
        with open(path) as f:
            self.current_events = json.load(f)
        self.status_var.set(f"Loaded '{name}' ({len(self.current_events)} events).")

    def delete_selected(self):
        sel = self.macro_listbox.curselection()
        if not sel:
            return
        name = self.macro_listbox.get(sel[0])
        if messagebox.askyesno("Delete macro", f"Delete '{name}'?"):
            os.remove(os.path.join(MACROS_DIR, f"{name}.json"))
            self._refresh_macro_list()
            self.status_var.set(f"Deleted '{name}'.")

    def on_close(self):
        if self.player:
            self.player.abort()
        self.hub.stop()
        self.root.destroy()


def main():
    root = tk.Tk()
    try:
        style = ttk.Style()
        style.theme_use("clam")
    except Exception:
        pass
    app = App(root)
    root.protocol("WM_DELETE_WINDOW", app.on_close)
    root.mainloop()


if __name__ == "__main__":
    main()
