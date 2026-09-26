# TRCC service files

Optional OS-level service registration. Three units live here:

| Unit | What it does |
|------|--------------|
| `trccd.service` | the IPC daemon that owns USB and serves CLI/API/GUI clients |
| `trcc-display.service` | the render-and-send ticker, so the panel shows live metrics after a reboot |
| `trccd-headless.service` | `trccd.service` for machines with **no desktop login** -- see [Headless machines](#headless-machines-no-desktop-login) |

**Install one or the other, not both.** Only one process may hold a panel's USB
interface; running both makes them fight over it.

The daemon auto-spawns when any UI calls it, so installing `trccd.service` is
**not required** — it just makes the daemon survive reboots / logout.

## Linux (systemd user unit)

```bash
# Install the unit (one-time)
mkdir -p ~/.config/systemd/user
cp trccd.service ~/.config/systemd/user/

# Enable + start
systemctl --user daemon-reload
systemctl --user enable --now trccd.service

# Check status
systemctl --user status trccd

# Tail logs (the daemon also writes ~/.trcc/trcc.log)
journalctl --user -u trccd -f
```

To uninstall:

```bash
systemctl --user disable --now trccd.service
rm ~/.config/systemd/user/trccd.service
systemctl --user daemon-reload
```

## LCD stats ticker (systemd user unit)

Drives the panel with the active theme on the configured refresh interval, from
login. Without it the LCD only updates while the GUI or `trcc display play` is
open, so it goes stale after a reboot.

```bash
# Install the unit + its helper (one-time)
mkdir -p ~/.config/systemd/user
cp trcc-display.service ~/.config/systemd/user/
sudo install -Dm755 trcc-display-ticker /usr/bin/trcc-display-ticker

systemctl --user daemon-reload
systemctl --user enable --now trcc-display.service

systemctl --user status trcc-display
journalctl --user -u trcc-display -f
```

The helper runs `trcc detect` and drives the first panel it finds, so the unit
carries no hardcoded VID:PID. To pin a specific panel, or to point at a
pip/pipx install rather than `/usr/bin/trcc`:

```bash
mkdir -p ~/.trcc
cat > ~/.trcc/ticker.env <<'EOF'
TRCC_DEVICE=0416:5408
TRCC_BIN=/home/you/.local/bin/trcc
EOF
systemctl --user restart trcc-display
```

To use the GUI while the ticker is running, stop it first — the ticker holds the
USB interface, and the GUI blocks on its splash rather than reporting the device
is busy:

```bash
systemctl --user stop trcc-display
trcc qtgui
systemctl --user restart trcc-display
```

To uninstall:

```bash
systemctl --user disable --now trcc-display.service
rm ~/.config/systemd/user/trcc-display.service
sudo rm /usr/bin/trcc-display-ticker
systemctl --user daemon-reload
```

## Headless machines (no desktop login)

Servers and render boxes often boot to `multi-user.target` and nobody ever logs in. Both
`trccd.service` and `trcc-display.service` are `PartOf`/`After=graphical-session.target`, so on such
a box they never start. Use `trccd-headless.service` instead -- same daemon, no session
dependency, `QT_QPA_PLATFORM=offscreen`, `Restart=always` (the install steps are in the unit's
header). `sudo loginctl enable-linger "$USER"` is what makes a user unit start at boot without a
login.

Verified on Ubuntu 24.04 with a `0416:5302` panel (320x240, which goes blank as soon as frames
stop, so the daemon has to run continuously). Two things the unit does **not** handle:

### 1. The daemon does not restore the last theme on start

At attach the daemon runs `ConnectDevice` only (`app.py`, the hotplug/coldplug path);
`RestoreDeviceState` runs from `ResetDevice` and the GUI/API, not at daemon startup. After a
restart `trcc status` still names your theme, but nothing is on the panel and
`GET /devices/<key>/display/preview` answers `404 No active theme`. On a desktop the GUI restores
it at login; headless, load it yourself after the daemon is up -- e.g. a drop-in:

```ini
# ~/.config/systemd/user/trccd.service.d/theme.conf
[Service]
ExecStartPost=%h/.local/bin/trcc-load-theme.sh
TimeoutStartSec=120
```

```bash
#!/bin/bash
# ~/.local/bin/trcc-load-theme.sh -- wait for the socket (see 2.), then load.
export PATH=$HOME/.local/bin:$PATH TRCC_DAEMON=1
KEY=0416:5302                                              # your key, from `trcc detect`
THEME=$HOME/.trcc-user/data/theme320240/mytheme           # the DIRECTORY, not the bare name
SOCK=${XDG_RUNTIME_DIR:-/run/user/$(id -u)}/trcc.sock
for i in $(seq 1 120); do [ -S "$SOCK" ] && break; sleep 1; done
for i in $(seq 1 30); do trcc display load-theme "$KEY" "$THEME" && exit 0; sleep 2; done
exit 1
```

Overlay elements you add with `trcc display overlay-add` live in settings, not the theme: run
`trcc theme save <key> <name> --overwrite` after editing, or a restart loses them.

### 2. A client that starts before the daemon spawns a SECOND daemon

Any trcc client (CLI, `trcc shell`, `trcc serve`) that finds no daemon socket auto-spawns a daemon.
If one runs while `trccd` is still starting -- an `ExecStartPost`, a companion service, a cron
job at boot -- you end up with **two daemons**: the spawned one takes the socket, `trccd` keeps the
USB panel. Every command then "succeeds" against a daemon with no screen: `overlay-list` shows
your change, the panel never does, and the preview route says `Not attached`.

Check: `pgrep -fc "[t]rcc daemon"` must print `1`, and that PID must equal
`systemctl --user show -p MainPID --value trccd`. Avoid it by waiting for
`$XDG_RUNTIME_DIR/trcc.sock` to exist before starting **any** client (the loop in the script above),
and give companion units `After=trccd.service` plus an `ExecStartPre` that does the same wait.

### Watching the panel remotely

`trcc serve --host 127.0.0.1 --port 8787` (with `TRCC_DAEMON=1`, after the socket exists) exposes
`GET /devices/<key>/display/preview`, the exact frame being sent to the panel. Keep it on
localhost -- the same API has device-control routes -- and put a read-only proxy in front of it if
something else needs to see the screen.

## macOS (LaunchAgent)

```bash
cp ../launchd/com.thermalright.trccd.plist ~/Library/LaunchAgents/
launchctl load ~/Library/LaunchAgents/com.thermalright.trccd.plist
```

To uninstall:

```bash
launchctl unload ~/Library/LaunchAgents/com.thermalright.trccd.plist
rm ~/Library/LaunchAgents/com.thermalright.trccd.plist
```

## Windows

A scheduled task is installed by `trcc system setup` (Phase 11 wires this in).
Manual install:

```powershell
schtasks /Create /SC ONLOGON /TN "TRCC Daemon" /TR "trcc daemon" /F
```

## Verifying

After install, in any terminal:

```bash
# Should print info about the running daemon (or auto-spawn one)
trcc detect

# The IPC socket should exist:
ls -la $XDG_RUNTIME_DIR/trcc.sock            # Linux (verified: the name is trcc.sock, not trcc-linux.sock)
ls -la /tmp/trcc-linux.sock                  # macOS / fallback
```
