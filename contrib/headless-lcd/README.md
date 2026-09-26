# headless-lcd

Run a Thermalright cooler LCD on a **headless** Linux machine (no desktop login) as a live status
screen: the machine's name, CPU and per-GPU temperature and load, temperatures **coloured by band**
(green / yellow / orange / red), and optionally each GPU's current **ComfyUI job**. A small
read-only HTTP API publishes the same data plus the exact frame on the panel, for a dashboard.

```
      MYRIG
 CPU   41°C    12%
GPU0   76°C    99%
wan2.2_i2v_14B_Q4 · 3:12 · +2
GPU1   58°C    98%
kdp-cover · 0:47
GPU2   37°C     0%
idle
```

This is a `contrib/` add-on: it drives trcc through its CLI/daemon and changes nothing in
`src/trcc`. Built and run on Ubuntu 24.04 with a `0416:5302` panel (320x240, Frozen Warframe 360)
and three NVIDIA GPUs. The layout script targets **320x240** panels.

## Files

| File | Installed to | What it does |
|---|---|---|
| `install.sh` | *(run in place)* | Copies everything below, enables linger, enables + starts the services |
| `make-theme.sh` | `~/lcd/` | Builds the layout above as a trcc theme, in one command |
| `lcd_watcher.py` | `~/lcd/` | Colour bands, job lines, status API. Stdlib only, system `python3` |
| `load-theme.sh` | `~/lcd/` | Loads the theme after the daemon starts (the daemon doesn't) |
| `wait-for-trccd.sh` | `~/lcd/` | Blocks until trccd's socket exists -- see [Traps](#traps) |
| `lcd.example.json` | `~/lcd/lcd.json` | Config (only copied if `lcd.json` doesn't exist) |
| `trccd.service` | `~/.config/systemd/user/` | The trcc daemon, headless, with the theme hook |
| `trcc-api.service` | `~/.config/systemd/user/` | trcc's REST API on **127.0.0.1:8787** -- source of the screen frame |
| `lcd-watcher.service` | `~/.config/systemd/user/` | Runs the watcher |

## Setting up a new machine

```bash
# 1. trcc from this repo, editable
sudo apt install -y pipx libusb-1.0-0 sg3-utils p7zip-full libxcb-cursor0 git
git clone https://github.com/disolutionsFL/thermalright-trcc-linux ~/src/thermalright-trcc-linux
pipx install -e "$HOME/src/thermalright-trcc-linux[nvidia]"
sudo env PATH=$HOME/.local/bin:$PATH trcc system setup --yes       # udev rules
~/.local/bin/trcc detect                                           # note your device key

# 2. Install headless-lcd (services start; no theme yet is fine)
~/src/thermalright-trcc-linux/contrib/headless-lcd/install.sh

# 3. Build the layout -- the title is the only value you must choose
~/lcd/make-theme.sh MYRIG

# 4. Edit ~/lcd/lcd.json: "device" + "theme_dir" (make-theme.sh prints both),
#    and the jobs/api sections if you use them; then
systemctl --user restart lcd-watcher
```

To update later: `git pull` in the clone, re-run `install.sh` (it keeps your `lcd.json`).

`make-theme.sh` options: `TRCC_KEY=` (default: first detected device), `GPUS=` (default: the
`nvidia-smi` count; the layout fits 0-3), `TITLE_COLOR=`. The element ids it creates are the ones
`lcd.example.json` expects, so the two work together unchanged.

## Configuration (`~/lcd/lcd.json`)

| Key | Meaning |
|---|---|
| `device` | trcc device key, `VVVV:PPPP` from `trcc detect` |
| `theme_dir` | the theme **directory** (`~/.trcc-user/data/theme320240/<name>`) |
| `interval` | seconds between samples (2) |
| `hysteresis` | °C a reading must fall below a threshold before dropping a band (2) |
| `resync_seconds` | full re-send of every value (60); repaints the panel after trccd restarts |
| `cpu` / `gpu` | `element` id (`{n}` = GPU index) and `bands`: `[[min_temp, colour], ...]` ascending |
| `jobs` | *(optional)* one line per GPU from ComfyUI: `ports` maps GPU index -> ComfyUI port |
| `api` | *(optional)* status API: `bind`, `port`, `trcc_api_port` |

Only the temperature **numbers** are coloured; labels stay white and load stays blue, because each
is a separate element. The default GPU red threshold (83°C) sits just under the 84°C temperature
target where GeForce Turing/Ampere cards start cutting clocks -- adjust per card family.

Sensors: CPU from hwmon (`coretemp` Package id 0, `k10temp` Tctl), GPUs from `nvidia-smi` (none ->
GPU elements are left alone).

## Job lines

One white line per GPU: `<label or model> · <elapsed m:ss> · +<queued>`, `idle`, or
`comfyui down`. Built for one ComfyUI instance per GPU (e.g. `--cuda-device N --port 8188+N`).

- **Label**: the prompt's `extra_data.label` if the submitter sent one, else the model named by
  the loader node (`unet_name`, `ckpt_name`, ...), else `job`. Send a label from your own tooling
  to get readable lines.
- **Elapsed** counts from when the watcher first saw the job running.
- **No step progress**: ComfyUI only sends `progress` events to the websocket client that queued
  the job, so a third party can't see them reliably.
- **Left-justified by padding**: trcc centres every element on its x,y, so the watcher pads each
  line to `max_chars` with **no-break spaces** (Qt trims ordinary trailing spaces when centring),
  and `make-theme.sh` gives the element a **monospace** font. The block then has a fixed width
  and its left edge never moves (size 12, 31 chars, centre x=168 -> left edge under the `G` of `GPU`).
- ComfyUI **caches identical prompts**: resubmitting the same workflow finishes instantly and never
  shows as running. Vary an input when testing.

## Status API

Read-only, **no authentication**. `lcd.example.json` binds `127.0.0.1`; bind `0.0.0.0` only on a
network you trust, or put an authenticating proxy in front.

| Route | Returns |
|---|---|
| `GET /status` | JSON: `host`, `updated` (epoch), `cpu {temp_c, usage_pct, colour}`, `gpus[] {index, name, temp, usage, colour, job {state: running/idle/offline, label, prompt_id, elapsed_s, pending, comfyui_port}, job_line}` |
| `GET /screen.png` | The frame currently on the panel (from trcc's `/devices/<key>/display/preview` on localhost). 503 JSON if unavailable |
| `GET /health` | `{"ok": true, "status_age_s": 0.8}`; 503 if the loop hasn't updated in 30s |

trcc's own API (`trcc-api.service`) stays on localhost: it has device-control routes.

Dry run, without sending anything or starting the API:
`python3 ~/lcd/lcd_watcher.py --config ~/lcd/lcd.json --once`.

## Traps

All of these were hit for real setting this up.

- **Two daemons.** A trcc client that finds no daemon socket **auto-spawns a daemon**. If anything
  runs `trcc` while `trccd` is still starting, the spawned daemon takes the command socket and
  `trccd` keeps the USB panel. Every command then "succeeds" against a daemon with no screen:
  `overlay-list` shows your change, the panel never does, the preview says `Not attached`. Every
  script and unit here waits for `$XDG_RUNTIME_DIR/trcc.sock` first (`wait-for-trccd.sh`). Check:
  `pgrep -fc "[t]rcc daemon$"` (anchored with `$` so it doesn't also count `trcc daemon-status` or any command line that merely contains the words) must be `1`, and that PID must equal
  `systemctl --user show -p MainPID --value trccd`.
- **`overlay-list` is not proof the panel changed** -- it reads settings. Ground truth is
  `/screen.png` (or your eyes).
- **The daemon doesn't restore the theme on start** -- hence `load-theme.sh`. Details in
  [`packaging/systemd/README.md`](../../packaging/systemd/README.md#headless-machines-no-desktop-login).
- **Loading or creating a theme clears the user-layer elements.** Add elements after loading, then
  `trcc theme save <key> <name> --overwrite`, or a restart loses them (`make-theme.sh` does this).
- **Some panels blank as soon as frames stop** (0416:5302 does), so the daemon must run
  continuously.
- **A bare metric renders `20.0` with no unit.** The unit comes from the format string
  (`{value:.0f}°C`).
- **`gpu:N` follows `nvidia-smi`'s numbering, which is PCI bus order, not slot order.** On a
  multi-GPU board, GPU0 is not necessarily the top slot.
- **Never `pkill -f "trcc daemon"` inside `ssh host '...'`**: the pattern matches the remote
  shell's own command line and kills your session. Use `pkill -f "[t]rcc daemon"`.
- **Files copied from a Windows checkout** may have CRLF line endings, which break the shell
  scripts. `sed -i 's/\r$//' <file>` before running.
