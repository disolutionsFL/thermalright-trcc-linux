#!/bin/bash
# ExecStartPost of trccd.service: the trcc daemon does not restore the last theme
# on its own (on a desktop the GUI does it at login), so load it here. Device key
# and theme dir come from ~/lcd/lcd.json. Waits for the daemon socket, then
# retries while USB/the panel settles.
export PATH=$HOME/.local/bin:$PATH TRCC_DAEMON=1
CFG=$HOME/lcd/lcd.json
read -r KEY THEME < <(python3 -c "import json,os,sys; c=json.load(open(sys.argv[1], encoding='utf-8')); print(c['device'], os.path.expanduser(c['theme_dir']))" "$CFG")
# Never run a trcc client before trccd's socket exists (two-daemon trap).
"$(dirname "$0")/wait-for-trccd.sh" || exit 1
# A fresh rig has no theme yet (make-theme.sh creates it). Don't fail the daemon's
# start over that -- a failed ExecStartPost would put trccd in a restart loop.
if [ ! -d "$THEME" ]; then
    echo "load-theme.sh: no theme at $THEME yet -- run make-theme.sh; daemon left running" >&2
    exit 0
fi
for i in $(seq 1 30); do
    if trcc display load-theme "$KEY" "$THEME"; then
        exit 0
    fi
    sleep 2
done
echo "load-theme.sh: gave up loading $THEME on $KEY" >&2
exit 1
