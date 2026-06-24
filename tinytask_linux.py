#!/usr/bin/env python3
"""
TinyTask for Linux
-------------------
A lightweight mouse & keyboard macro recorder/player for Ubuntu/Linux,
inspired by the classic Windows "TinyTask" utility.

Features:
  - Record mouse moves, clicks, scrolls, and keystrokes with real timing
  - Play back recorded macros, with adjustable speed and loop count
  - Save/load macros as JSON files (~/.config/tinytask-linux/macros)
  - Global hotkeys: F9 = stop recording, F10 = start/stop playback,
    Esc = abort playback (works even when the app window isn't focused)

Requirements:
    pip install pynput
    (tkinter is usually preinstalled; if not: sudo apt install python3-tk)

Note: pynput's global hooks rely on X11. On Ubuntu with Wayland (the
default since 22.04+), log out and choose "Ubuntu on Xorg" at the login
screen for recording/playback to work system-wide.

Run:
    python3 tinytask_linux.py
"""

import json
import os
import threading
import time
import tkinter as tk
from tkinter import ttk, messagebox, simpledialog

from pynput import mouse, keyboard

CONFIG_DIR = os.path.expanduser("~/.config/tinytask-linux")
MACROS_DIR = os.path.join(CONFIG_DIR, "macros")
os.makedirs(MACROS_DIR, exist_ok=True)

MOVE_THROTTLE = 0.02  # seconds between recorded mouse-move samples


# --------------------------------------------------------------------------
# Key (de)serialization helpers
# --------------------------------------------------------------------------
def serialize_key(key):
    if isinstance(key, keyboard.KeyCode):
        if key.char is not None:
            return {"char": key.char}
        return {"vk": key.vk}
    return {"name": key.name}


def deserialize_key(data):
    if "name" in data:
        return getattr(keyboard.Key, data["name"])
    if "char" in data:
        return keyboard.KeyCode.from_char(data["char"])
    return keyboard.KeyCode.from_vk(data["vk"])


def serialize_button(button):
    return button.name


def deserialize_button(name):
    return getattr(mouse.Button, name)


# --------------------------------------------------------------------------
# Recorder
# --------------------------------------------------------------------------
class MacroRecorder:
    def __init__(self, on_stop):
        self.events = []
        self.start_time = None
        self._last_move_t = 0
        self.on_stop = on_stop
        self._mouse_listener = None
        self._kb_listener = None
        self._stopped = False

    def start(self):
        self.events = []
        self._stopped = False
        self.start_time = time.time()
        self._last_move_t = 0

        self._mouse_listener = mouse.Listener(
            on_move=self._on_move, on_click=self._on_click, on_scroll=self._on_scroll
        )
        self._kb_listener = keyboard.Listener(
            on_press=self._on_press, on_release=self._on_release
        )
        self._mouse_listener.start()
        self._kb_listener.start()

    def _t(self):
        return time.time() - self.start_time

    def _on_move(self, x, y):
        t = self._t()
        if t - self._last_move_t >= MOVE_THROTTLE:
            self._last_move_t = t
            self.events.append({"t": t, "type": "move", "x": x, "y": y})

    def _on_click(self, x, y, button, pressed):
        self.events.append(
            {
                "t": self._t(),
                "type": "click",
                "x": x,
                "y": y,
                "button": serialize_button(button),
                "pressed": pressed,
            }
        )

    def _on_scroll(self, x, y, dx, dy):
        self.events.append(
            {"t": self._t(), "type": "scroll", "x": x, "y": y, "dx": dx, "dy": dy}
        )

    def _on_press(self, key):
        if key == keyboard.Key.f9:
            self.stop()
            return False  # stop this listener
        self.events.append({"t": self._t(), "type": "key_down", "key": serialize_key(key)})

    def _on_release(self, key):
        if key == keyboard.Key.f9:
            return
        self.events.append({"t": self._t(), "type": "key_up", "key": serialize_key(key)})

    def stop(self):
        if self._stopped:
            return
        self._stopped = True
        if self._mouse_listener:
            self._mouse_listener.stop()
        if self._kb_listener:
            self._kb_listener.stop()
        if self.on_stop:
            self.on_stop(self.events)


# --------------------------------------------------------------------------
# Player
# --------------------------------------------------------------------------
class MacroPlayer:
    def __init__(self, events, on_finish=None, on_progress=None):
        self.events = events
        self.on_finish = on_finish
        self.on_progress = on_progress
        self._abort = threading.Event()
        self._mouse = mouse.Controller()
        self._keyboard = keyboard.Controller()
        self._abort_listener = None

    def play(self, loops=1, speed=1.0):
        threading.Thread(target=self._run, args=(loops, speed), daemon=True).start()

    def _watch_escape(self):
        def on_press(key):
            if key == keyboard.Key.esc:
                self._abort.set()
                return False

        self._abort_listener = keyboard.Listener(on_press=on_press)
        self._abort_listener.start()

    def _run(self, loops, speed):
        self._abort.clear()
        self._watch_escape()
        total = len(self.events)

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
                self._dispatch(ev)
                if self.on_progress:
                    self.on_progress(loop_i + 1, loops, idx + 1, total)

        if self._abort_listener:
            self._abort_listener.stop()
        if self.on_finish:
            self.on_finish(aborted=self._abort.is_set())

    def _dispatch(self, ev):
        etype = ev["type"]
        if etype == "move":
            self._mouse.position = (ev["x"], ev["y"])
        elif etype == "click":
            self._mouse.position = (ev["x"], ev["y"])
            btn = deserialize_button(ev["button"])
            if ev["pressed"]:
                self._mouse.press(btn)
            else:
                self._mouse.release(btn)
        elif etype == "scroll":
            self._mouse.position = (ev["x"], ev["y"])
            self._mouse.scroll(ev["dx"], ev["dy"])
        elif etype == "key_down":
            self._keyboard.press(deserialize_key(ev["key"]))
        elif etype == "key_up":
            self._keyboard.release(deserialize_key(ev["key"]))

    def abort(self):
        self._abort.set()


# --------------------------------------------------------------------------
# GUI
# --------------------------------------------------------------------------
class App:
    def __init__(self, root):
        self.root = root
        root.title("TinyTask for Linux")
        root.geometry("420x480")
        root.resizable(False, False)

        self.current_events = []
        self.recorder = None
        self.player = None
        self.global_hotkeys = None

        self._build_ui()
        self._refresh_macro_list()
        self._start_global_hotkeys()

    # ---------------- UI layout ----------------
    def _build_ui(self):
        pad = {"padx": 8, "pady": 6}

        title = ttk.Label(self.root, text="TinyTask for Linux", font=("Sans", 16, "bold"))
        title.pack(pady=(10, 0))
        subtitle = ttk.Label(
            self.root,
            text="Record and replay mouse & keyboard macros",
            foreground="#666",
        )
        subtitle.pack(pady=(0, 10))

        # --- Record / Play controls ---
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
            text="F9 stops recording  •  F10 starts/stops playback  •  Esc aborts playback",
            foreground="#888",
            font=("Sans", 8),
        )
        hint.grid(row=1, column=0, columnspan=2, padx=6, pady=(0, 8))

        # --- Playback options ---
        opt_frame = ttk.LabelFrame(self.root, text="Playback options")
        opt_frame.pack(fill="x", **pad)

        ttk.Label(opt_frame, text="Loops:").grid(row=0, column=0, padx=6, pady=6, sticky="w")
        self.loops_var = tk.StringVar(value="1")
        self.loops_spin = ttk.Spinbox(
            opt_frame, from_=1, to=9999, textvariable=self.loops_var, width=6
        )
        self.loops_spin.grid(row=0, column=1, padx=6, pady=6, sticky="w")

        self.infinite_var = tk.BooleanVar(value=False)
        ttk.Checkbutton(
            opt_frame, text="Infinite loop", variable=self.infinite_var
        ).grid(row=0, column=2, padx=6, pady=6, sticky="w")

        ttk.Label(opt_frame, text="Speed:").grid(row=1, column=0, padx=6, pady=6, sticky="w")
        self.speed_var = tk.DoubleVar(value=1.0)
        speed_scale = ttk.Scale(
            opt_frame, from_=0.1, to=5.0, variable=self.speed_var, orient="horizontal"
        )
        speed_scale.grid(row=1, column=1, columnspan=2, padx=6, pady=6, sticky="ew")
        opt_frame.columnconfigure(2, weight=1)

        # --- Saved macros ---
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

        # --- Status bar ---
        self.status_var = tk.StringVar(value="Ready. 0 events loaded.")
        status = ttk.Label(self.root, textvariable=self.status_var, relief="sunken", anchor="w")
        status.pack(fill="x", side="bottom")

    # ---------------- Global hotkeys (F10 play/stop, even unfocused) ----------------
    def _start_global_hotkeys(self):
        def on_press(key):
            if key == keyboard.Key.f10:
                self.root.after(0, self.toggle_play)

        self.global_hotkeys = keyboard.Listener(on_press=on_press)
        self.global_hotkeys.start()

    # ---------------- Recording ----------------
    def toggle_record(self):
        if self.recorder is not None:
            return  # already recording; F9 stops it
        self.recorder = MacroRecorder(on_stop=self._on_record_stopped)
        self.recorder.start()
        self.record_btn.config(text="■ Recording... (F9 to stop)")
        self.play_btn.config(state="disabled")
        self.status_var.set("Recording... move to target window now. Press F9 to stop.")

    def _on_record_stopped(self, events):
        def update():
            self.current_events = events
            self.recorder = None
            self.record_btn.config(text="● Record")
            self.play_btn.config(state="normal")
            self.status_var.set(f"Recording stopped. {len(events)} events captured.")

        self.root.after(0, update)

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

    def _on_progress(self, loop_i, loops, ev_i, total):
        def update():
            loop_text = "∞" if self.infinite_var.get() else str(loops)
            self.status_var.set(f"Playing loop {loop_i}/{loop_text} — event {ev_i}/{total}")

        self.root.after(0, update)

    def _on_play_finished(self, aborted):
        def update():
            self.player = None
            self.play_btn.config(text="▶ Play (F10)")
            self.record_btn.config(state="normal")
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
        if self.recorder:
            self.recorder.stop()
        if self.player:
            self.player.abort()
        if self.global_hotkeys:
            self.global_hotkeys.stop()
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
