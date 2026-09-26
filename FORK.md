# About this fork

`disolutionsFL/thermalright-trcc-linux` is a fork of
[Lexonight1/thermalright-trcc-linux](https://github.com/Lexonight1/thermalright-trcc-linux),
kept for running Thermalright cooler LCDs on **headless** Linux machines (no desktop login),
driven from the CLI/daemon rather than the GUI.

Everything upstream documents still applies. This fork adds only:

| Change | Where | Status |
|---|---|---|
| Headless variant of the daemon unit | `packaging/systemd/trccd-headless.service` | in use |
| "Headless machines" guide: linger, theme restore after restart, the two-daemon startup race, remote preview | `packaging/systemd/README.md` | in use |
| Socket name fix in the systemd README (`trcc.sock`, not `trcc-linux.sock`) | `packaging/systemd/README.md` | verified on Linux |

Planned, and intended to be offered upstream as PRs rather than kept as fork-only divergence:

- per-element **colour bands** for metric overlays (e.g. a temperature that turns yellow / orange /
  red past thresholds), wired through every face -- CLI, API, both GUIs -- with tests;
- per-element **text alignment** (today every element is centred on its x,y);
- the daemon **restoring the last theme on start**, so headless boxes need no `ExecStartPost`.

## Syncing with upstream

```bash
git remote add upstream https://github.com/Lexonight1/thermalright-trcc-linux.git   # once
git fetch upstream
git merge upstream/main          # fork changes are additive files/sections, so this stays clean
```

Installed from a clone with `pipx install -e ".[nvidia]"`, a `git pull` is all a machine needs to
pick up changes -- no reinstall.

This repository is public: keep machine names, addresses and other deployment details out of it.
