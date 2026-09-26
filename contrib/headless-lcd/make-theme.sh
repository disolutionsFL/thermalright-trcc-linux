#!/bin/bash
# make-theme.sh -- build the headless-lcd layout as a trcc theme, in one command.
#
#   ./make-theme.sh TITLE [THEME_NAME]
#
#   TITLE       text across the top, e.g. MYRIG
#   THEME_NAME  theme directory name (default: TITLE lowercased)
#
# Environment overrides:
#   TRCC_KEY=VVVV:PPPP   device key (default: first device from `trcc detect`)
#   GPUS=N               GPU rows, 0-3 (default: `nvidia-smi` count, 0 without it)
#   TITLE_COLOR=#ffb000  title colour
#
# Layout (320x240 panels; positions are element CENTRES):
#   title                                        y=20
#   CPU   <temp>   <load%>                       y=55
#   GPUn  <temp>   <load%>  + white job line     y=90 / 142 / 194 (+25 for the job line)
# Element ids match lcd.example.json: cpu_t / g{n}_t get band colours, g{n}_j job text.
#
# Needs trccd running (install.sh). Re-running with the same name rebuilds the theme.
set -euo pipefail

TITLE=${1:?usage: make-theme.sh TITLE [THEME_NAME]}
NAME=${2:-$(echo "$TITLE" | tr '[:upper:]' '[:lower:]')}
TITLE_COLOR=${TITLE_COLOR:-#ffb000}
HERE=$(cd "$(dirname "$0")" && pwd)
export PATH=$HOME/.local/bin:$PATH TRCC_DAEMON=1

# Never start a trcc client before trccd owns the socket (two-daemon trap).
WAIT=$HERE/wait-for-trccd.sh; [ -x "$WAIT" ] || WAIT=$HOME/lcd/wait-for-trccd.sh
"$WAIT"

KEY=${TRCC_KEY:-$(trcc detect 2>/dev/null | grep -oE '[0-9a-fA-F]{4}:[0-9a-fA-F]{4}' | head -1)}
[ -n "$KEY" ] || { echo "make-theme.sh: no device found (trcc detect)" >&2; exit 1; }

CANVAS=$(trcc device canvas "$KEY" 2>/dev/null | head -1 | grep -oE '^[0-9]+x[0-9]+' || true)
if [ "$CANVAS" != "320x240" ]; then
    echo "make-theme.sh: $KEY is ${CANVAS:-unknown}; this layout is for 320x240 panels only" >&2
    exit 1
fi

if [ -z "${GPUS:-}" ]; then
    GPUS=$(nvidia-smi --query-gpu=index --format=csv,noheader 2>/dev/null | wc -l || echo 0)
fi
if [ "$GPUS" -gt 3 ]; then
    echo "make-theme.sh: $GPUS GPUs -- this layout fits 3 rows; set GPUS=3 or adapt the y positions" >&2
    exit 1
fi
echo "make-theme.sh: theme '$NAME' on $KEY ($CANVAS), title '$TITLE', $GPUS GPU row(s)"

# Plain black 320x240 background, written with the stdlib (trcc ships no Pillow).
BG=$HOME/lcd/black-320x240.png
mkdir -p "$(dirname "$BG")"
python3 - "$BG" <<'EOF'
import struct, sys, zlib
w, h = 320, 240
raw = b"".join(b"\x00" + b"\x00\x00\x00" * w for _ in range(h))
def chunk(t, d):
    return struct.pack(">I", len(d)) + t + d + struct.pack(">I", zlib.crc32(t + d) & 0xFFFFFFFF)
png = (b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", struct.pack(">IIBBBBB", w, h, 8, 2, 0, 0, 0))
       + chunk(b"IDAT", zlib.compress(raw)) + chunk(b"IEND", b""))
open(sys.argv[1], "wb").write(png)
EOF

# Creating/loading a theme CLEARS the user-layer elements, so add them after.
trcc theme create "$KEY" "$NAME" --bg "$BG" >/dev/null
for id in $(trcc display overlay-list "$KEY" 2>/dev/null | awk 'NF>1 && $2 ~ /^(text|metric|clock)$/ {print $1}'); do
    trcc display overlay-delete "$KEY" "$id" >/dev/null
done

add() { trcc display overlay-add "$KEY" "$@" >/dev/null || { echo "make-theme.sh: failed: overlay-add $*" >&2; exit 1; }; }
WHITE="#ffffff"; BLUE="#5ec8ff"

add text   --id title --text "$TITLE" --x 160 --y 20 --size 26 --bold --color "$TITLE_COLOR"
add text   --id cpu_l --text CPU --x 58 --y 55 --size 24 --bold --color "$WHITE"
add metric --id cpu_t --metric cpu:temp  --x 158 --y 55 --size 24 --bold --color "$WHITE" --format "{value:.0f}°C"
add metric --id cpu_u --metric cpu:usage --x 262 --y 55 --size 24 --color "$BLUE" --format "{value:.0f}%"
for ((g = 0; g < GPUS; g++)); do
    y=$((90 + 52 * g))
    add text   --id "g${g}_l" --text "GPU$g" --x 58 --y "$y" --size 22 --color "$WHITE"
    add metric --id "g${g}_t" --metric "gpu:$g:temp"  --x 158 --y "$y" --size 22 --color "$WHITE" --format "{value:.0f}°C"
    add metric --id "g${g}_u" --metric "gpu:$g:usage" --x 262 --y "$y" --size 22 --color "$BLUE" --format "{value:.0f}%"
    # Job line: monospace so lcd_watcher's no-break-space padding left-justifies it.
    add text   --id "g${g}_j" --text idle --x 168 --y $((y + 25)) --size 12 --color "$WHITE" --font "DejaVu Sans Mono"
done

trcc theme save "$KEY" "$NAME" --overwrite >/dev/null
THEME_DIR=$(trcc theme list "$KEY" 2>/dev/null | awk -v n="$NAME" '$1 == n {print $NF}' | head -1)
echo "make-theme.sh: saved ${THEME_DIR:-$NAME}"
echo "Set in ~/lcd/lcd.json:  \"device\": \"$KEY\", \"theme_dir\": \"${THEME_DIR:-<theme dir>}\""
echo "then: systemctl --user restart lcd-watcher"
