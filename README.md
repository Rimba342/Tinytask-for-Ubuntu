# TinyTask for Ubuntu

A TinyTask-style mouse & keyboard macro recorder/player for Ubuntu.

## Install dependencies

```bash
sudo apt install python3-tk python3-pip
pip3 install pynput
```

## Run

```bash
python3 tinytask_linux.py
```

## Using it

1. Click **● Record**, then switch to whatever window you want to automate
   and perform the actions (clicks, typing, scrolling, mouse movement).
2. Press **F9** at any time to stop recording (works even if the app
   window isn't focused).
3. Click **▶ Play** (or press **F10**) to replay the macro. Set **Loops**
   and **Speed** first if you want it repeated or sped up/slowed down.
4. Press **F10** or **Esc** anytime to abort playback early.
5. Use **Save current...** to store a macro by name, and **Load
   selected** / **Delete selected** to manage saved macros. They're
   stored as JSON files in `~/.config/tinytask-linux/macros/`.

## Wayland note

Ubuntu defaults to Wayland since 22.04, and `pynput`'s global mouse/
keyboard hooks need X11 to work system-wide. If recording/playback
doesn't capture events in other windows, log out and pick **"Ubuntu on
Xorg"** at the login screen (gear icon), then log back in and try again.

## Optional: make it a clickable app

To launch it from your applications menu instead of the terminal, create
`~/.local/share/applications/tinytask-linux.desktop`:

```ini
[Desktop Entry]
Type=Application
Name=TinyTask for Linux
Exec=python3 /full/path/to/tinytask_linux.py
Icon=utilities-terminal
Terminal=false
Categories=Utility;
```

(Replace `/full/path/to/` with wherever you keep the script.)
