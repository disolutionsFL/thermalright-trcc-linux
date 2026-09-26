#!/bin/bash
# Block until trccd's socket exists (up to 120s). Run this before ANY trcc client
# starts: a client that finds no daemon auto-spawns a second one, which takes the
# command socket while trccd keeps the USB panel (see README.md, "Traps").
SOCK=${XDG_RUNTIME_DIR:-/run/user/$(id -u)}/trcc.sock
for i in $(seq 1 120); do [ -S "$SOCK" ] && exit 0; sleep 1; done
echo "wait-for-trccd.sh: no daemon socket at $SOCK after 120s" >&2
exit 1
