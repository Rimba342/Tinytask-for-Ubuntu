# TinyTask for Wayland

This version works under Wayland sessions (the Ubuntu default since
22.04). It talks to the kernel directly instead of going through the
display server:

- **Recording** reads raw input straight from `/dev/input/event*` via
  `python-evdev`. Works no matter what window is focused, including
  while this app is minimized.
- **Playback** injects events through the kernel's `uinput` interface
  via the `ydotool` command-line tool, which needs its background
  daemon `ydotoold` running.

## One-time setup

```bash
# 1. Install ydotool (lets the script inject clicks/keys on Wayland)
sudo apt install ydotool

# 2. Install python-evdev (lets the script read raw input for recording)
pip3 install evdev --break-system-packages

# 3. Add yourself to the 'input' group so you can read /dev/input/event*
#    without sudo. You MUST log out and back in for this to take effect.
sudo usermod -aG input $USER
```

Log out and log back in now (group membership only applies to new
login sessions).

```bash
# 4. Start the ydotool daemon, owned by your user so you don't need sudo
#    for every playback action. Run this once per session (or add it to
#    your startup apps).
sudo -b ydotoold --socket-path="$HOME/.ydotool_socket" --socket-own="$(id -u):$(id -g)"

# 5. Tell ydotool (and the app) where the daemon's socket is.
#    Add this line to ~/.bashrc so you don't have to repeat it every time:
export YDOTOOL_SOCKET="$HOME/.ydotool_socket"
```

## Run

```bash
python3 tinytask_linux_wayland.py
```

## Using it

Same as before:

1. **● Record**, then switch to the window you want to automate and do
   your actions. You can minimize the app — recording keeps working.
2. **F9** stops recording from anywhere.
3. **▶ Play (F10)** replays it; **Esc** or **F10** aborts mid-playback.
4. **Save current... / Load selected / Delete selected** manage named
   macros, stored as JSON in `~/.config/tinytask-linux/macros/`.

## Troubleshooting

- **"Permission denied opening /dev/input/eventX"** — you skipped step
  3, or logged in before adding yourself to the group. Re-check with:
  ```bash
  groups
  ```
  `input` should be listed. If not, redo step 3 and fully log out/in
  (a reboot also works).

- **"ydotool failed" / "ydotool is not installed"** — make sure step 4's
  `ydotoold` is actually running:
  ```bash
  ps aux | grep ydotoold
  ```
  and that `YDOTOOL_SOCKET` is exported in the terminal you launched
  the app from.

- **Playback feels slightly less smooth than before** — each input
  event is sent to `ydotool` as a separate command, which is a little
  heavier than the old direct-injection approach used on X11. It's
  still fully functional, just not quite as buttery for very fast
  mouse movements.

## Want it to start the daemon automatically every login?

Create `~/.config/autostart/ydotoold.desktop`:

```ini
[Desktop Entry]
Type=Application
Name=ydotoold
Exec=ydotoold --socket-path=/home/YOUR_USERNAME/.ydotool_socket --socket-own=1000:1000
X-GNOME-Autostart-enabled=true
```

(Replace `YOUR_USERNAME` and the UID/GID `1000:1000` with the output of
`id -u` / `id -g` if different.) This still needs root to create the
uinput device, so you may be prompted, or you can set it up as a proper
systemd service instead — see the ydotool project's README for the
systemd unit file approach if you want it fully passwordless.
