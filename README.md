# ghstatus

A small native window that shows a GitHub repo's status at a glance and auto-refreshes every 60 seconds. Single Python file, stdlib only (Tkinter), no virtualenv. Requires Homebrew `python@3.14` — see Requirements below.

## What it shows

- Repo header (name, description, visibility, stars)
- Latest release
- Latest commit on any branch (most recently updated branch wins) + CI status badge
- Repo notifications (unread, filtered to this repo)
- Open PRs (up to 10)
- Recently merged PRs (last 3)
- Open issues (up to 10)

Every row is clickable and opens in your default browser.

## Requirements

- macOS with Homebrew `python@3.14` installed (`brew install python@3.14`). **Apple's stock `/usr/bin/python3` does not work** — its `_tkinter` links against the deprecated system Tcl/Tk 8.5.9, which aborts on `Tk()` on macOS 26 with a bogus version-mismatch error. Homebrew's `python@3.14` bundles its own working Tcl/Tk 9.0.
- [`gh`](https://cli.github.com) installed and authenticated (`brew install gh && gh auth login`)
- `git` on `$PATH`

The script's shebang is `#!/usr/bin/env python3.14`, so as long as `python3.14` is on your `$PATH` (which `brew install python@3.14` arranges) it will pick up the right interpreter on both Apple Silicon and Intel Macs.

## Install

The script is self-contained — symlink it onto your `$PATH`:

```sh
ln -s "$PWD/ghstatus.py" ~/bin/ghstatus
```

(Or copy it; it doesn't depend on its install location.)

## Usage

```sh
ghstatus                                  # current directory's git repo
ghstatus owner/name                       # explicit slug
ghstatus https://github.com/owner/name    # explicit URL
ghstatus -h                               # help
```

The window auto-refreshes every 60 seconds. Hit **Refresh** in the footer to force an immediate fetch.
