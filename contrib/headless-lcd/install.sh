#!/bin/bash
# install.sh -- install headless-lcd for the current user.
#
#   ./install.sh            copy files, enable linger, enable + (re)start the services
#
# Copies (never symlinks, so a `git pull` doesn't change what's running until you
# re-run this):
#   lcd_watcher.py load-theme.sh wait-for-trccd.sh make-theme.sh -> ~/lcd/
#   lcd.example.json -> ~/lcd/lcd.json            (only if lcd.json doesn't exist yet)
#   *.service        -> ~/.config/systemd/user/
# Prerequisite: trcc installed (pipx, ~/.local/bin/trcc) and `trcc system setup` done.
set -euo pipefail

HERE=$(cd "$(dirname "$0")" && pwd)
DEST=$HOME/lcd
UNITS=$HOME/.config/systemd/user

[ -x "$HOME/.local/bin/trcc" ] || { echo "install.sh: ~/.local/bin/trcc not found -- install trcc-linux first (see README)" >&2; exit 1; }
command -v python3 >/dev/null || { echo "install.sh: python3 not found" >&2; exit 1; }

mkdir -p "$DEST" "$UNITS"
for f in lcd_watcher.py load-theme.sh wait-for-trccd.sh make-theme.sh; do
    install -m 755 "$HERE/$f" "$DEST/$f"
done
if [ -f "$DEST/lcd.json" ]; then
    echo "install.sh: keeping existing $DEST/lcd.json"
else
    install -m 644 "$HERE/lcd.example.json" "$DEST/lcd.json"
    echo "install.sh: created $DEST/lcd.json from the example -- edit it (device, theme_dir, bands, jobs, api)"
fi
for u in trccd.service trcc-api.service lcd-watcher.service; do
    install -m 644 "$HERE/$u" "$UNITS/$u"
done

if [ "$(loginctl show-user "$USER" -p Linger --value 2>/dev/null)" != "yes" ]; then
    echo "install.sh: enabling linger so the services start at boot without a login (sudo)"
    sudo loginctl enable-linger "$USER"
fi

systemctl --user daemon-reload
systemctl --user enable trccd.service trcc-api.service lcd-watcher.service >/dev/null 2>&1
systemctl --user restart trccd.service
systemctl --user restart trcc-api.service lcd-watcher.service
sleep 3
systemctl --user --no-pager --lines=0 status trccd trcc-api lcd-watcher | grep -E "●|Active:" || true
echo "daemons running: $(pgrep -fc '[t]rcc daemon$') (must be 1)"
echo
echo "Next, if this rig has no theme yet:  ~/lcd/make-theme.sh MYRIG"
echo "then set device + theme_dir in ~/lcd/lcd.json and: systemctl --user restart lcd-watcher"
