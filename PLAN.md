# ghstatus — Plan

## Goal
A native desktop window that shows a GitHub repo's status at a glance and auto-refreshes while open. Run from any git repo to show that repo, or pass a slug/URL to override.

## Constraints
- No terminal UI, no local web server, no browser.
- No per-directory Python environment. Uses Homebrew `python@3.14` and stdlib only.
- Single file. Installed once, runnable from anywhere.
- Shells out to `gh` (already authenticated on this machine) for all GitHub data.
- Must handle all errors — no silent `try/except: pass`.

## Tech choice
- **Python 3.14 (Homebrew) + Tkinter** (stdlib GUI). Apple's stock `/usr/bin/python3` cannot be used: its `_tkinter` is hard-linked against the system `Tcl/Tk 8.5.9` framework, which aborts on `Tk()` instantiation on macOS 26 with `macOS 26 (2603) or later required, have instead 16 (1603) !` — an internal version assertion in the deprecated 8.5.9 binary; Apple has not shipped a fix. Homebrew's `python@3.14` bundles its own working Tcl/Tk 9.0.
- **`gh` CLI** for GitHub data — avoids hand-rolling auth.
- **`git` CLI** to detect the repo from cwd.

## File layout
- `ghstatus.py` — the entire app, single file, in this repo.
- `README.md` — install/run instructions (1 short page).
- Install path: symlink `~/bin/ghstatus` → `<repo>/ghstatus.py` (chmod +x, shebang `#!/usr/bin/env python3.14`). User runs `ghstatus` from any directory.

## Behavior

### Argument parsing
- `ghstatus`            → detect repo from `git remote get-url origin` in cwd.
- `ghstatus owner/name` → use that slug.
- `ghstatus https://github.com/owner/name[.git]` → parse to slug.
- `ghstatus -h` → usage.
- If cwd is not a git repo and no arg given → window opens with an error message (not a crash).

### Data shown (one window, vertical layout)
1. **Header**: `owner/name` (large, clickable → opens repo), description, visibility, stars.
2. **Latest release**: tag, name, published-relative-time, click → release page. "No releases" if none.
3. **Last commit (most recent on any branch)**: SHA (short), branch the commit is on, author, relative time, first line of message, click → commit. CI status badge for that commit (success / failure / pending / none) sourced from `gh api repos/<slug>/commits/<sha>/check-runs`. Sourced by paginating *all* branch refs (100 per page, capped at 10 pages = 1000 branches) and sorting client-side by tip-commit `committedDate` descending, taking the most recent. Server-side ordering cannot be used: GitHub's GraphQL silently ignores `orderBy:{field:TAG_COMMIT_DATE, ...}` on branch refs (it only honors it for tag refs), returning refs in some other order — verified empirically against `cli/cli`, where `first:1` with that orderBy returned a 2021 branch on a repo that gets daily commits.
4. **Repo notifications**: unread notifications filtered to this repo from `gh api notifications`. Shows count and top 3 (reason + title + click → URL). Hidden if zero.
5. **Open PRs** (count + list, max 10): number, title, author, draft flag, mergeable state, updated-relative-time. Click → browser.
6. **Recently merged PRs** (last 3): number, title, author, merged-relative-time. Click → browser.
7. **Open issues** (count + list, max 10): number, title, author, updated-relative-time. Click → browser.
8. **Footer**: "Last refreshed: HH:MM:SS" + manual Refresh button + any error from the last fetch.

### Refresh
- Background `threading.Thread` runs gh calls; UI thread updates via `root.after(...)` (Tkinter is not thread-safe).
- Auto-refresh every **60 seconds** (configurable constant at top of file).
- Manual Refresh button forces an immediate fetch.
- While a fetch is in flight, button is disabled and footer shows "Refreshing…".
- If a fetch fails (network, gh not installed, not authed, repo not found), show the error in a status bar but keep last good data visible.

### gh / git commands used
- `gh repo view <slug> --json name,owner,description,visibility,stargazerCount,url`
- `gh api graphql` with a paginated query: `repository.refs(refPrefix:"refs/heads/", first:100, after:$cursor)` returning each branch's tip commit (oid, messageHeadline, committedDate, author, url) plus `pageInfo { hasNextPage endCursor }`. Loop until `hasNextPage` is false, capped at 10 pages (1000 branches) as a safety bound. Sort the collected branches client-side by `committedDate` descending; the first is "the latest commit on any branch". Why client-side: GitHub's GraphQL silently ignores `orderBy:{field:TAG_COMMIT_DATE, ...}` on branch refs.
- `gh api repos/<slug>/commits/<sha>/check-runs` → CI status for that commit
- `gh release view -R <slug> --json tagName,name,publishedAt,url` (exit-code-1 with "release not found" → treat as "no releases")
- `gh pr list -R <slug> --state open --json number,title,author,isDraft,mergeable,updatedAt,url -L 10`
- `gh pr list -R <slug> --state merged --json number,title,author,mergedAt,url -L 3`
- `gh issue list -R <slug> --state open --json number,title,author,updatedAt,url -L 10`
- `gh api notifications` → filter client-side to entries where `repository.full_name == slug`
- For deriving the slug when no arg is passed (one-time, at launch): `git -C <cwd-at-launch> remote get-url origin`, then parse to a slug.
- All shell calls go through `subprocess.run(..., capture_output=True, text=True, check=False)`; non-zero exit is inspected, not ignored. JSON parsed via `json.loads`.
- `cwd` is captured **at launch** via `os.getcwd()` and stashed — used only to derive a slug when no argument is passed. The script does not assume the user runs it from this folder; if installed on `$PATH`, it will be invoked from arbitrary directories.

### Error handling
- `gh` not installed → window shows "gh CLI not found. Install: brew install gh".
- `gh` not authed → "gh not authenticated. Run: gh auth login".
- Repo not found / no access → "Repository <slug> not found or no access".
- Network failure → keep last data, show error in footer.
- No `try/except: pass`. Every `except` either updates UI state or re-raises.

## Validation (how we know it works)
Manual checks performed before declaring done:
1. **From a git repo**: `cd ~/cursor/PhotoCalorie && ghstatus` → window shows PhotoCalorie's repo.
2. **With slug arg**: `ghstatus cli/cli` → window shows the gh CLI repo.
3. **With URL arg**: `ghstatus https://github.com/cli/cli.git` → same as above.
4. **From a non-repo dir with no arg**: `cd /tmp && ghstatus` → window opens with a clear "not in a git repo, pass a slug" message, no crash.
5. **Auto-refresh**: leave window open, change a PR title on GitHub, within 60s the new title appears.
6. **Manual refresh**: click Refresh, footer timestamp updates.
7. **Click PR/issue/commit row**: opens correct URL in default browser.
8. **gh missing**: temporarily rename `gh` on PATH, launch → error message shown, no crash. Restore.
9. **Non-existent repo**: `ghstatus nope/nope-nope-nope` → error shown, no crash.
10. **Long lists**: a repo with 50+ open issues — only first 10 shown, count reflects total.

This is a small interactive tool with no automated test suite. Validation = the manual checklist above, executed in order; all 10 must pass.

## Out of scope (explicitly not doing)
- Multiple repos in one window.
- Writing data back (closing PRs, commenting).
- Caching to disk.
- Configurable refresh interval via UI (constant in source is enough).
- Backwards compatibility / migration code.
- Packaging as a .app bundle.

## Open questions for the user
None — proceeding on approval.
