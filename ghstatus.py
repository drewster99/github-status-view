#!/usr/bin/env python3.14
"""ghstatus - GitHub repo status panel.

Run from a git repo to show that repo's status, or pass owner/name or a
GitHub URL to point at any repo. Auto-refreshes every 60 seconds.
"""

import argparse
import json
import os
import queue
import re
import subprocess
import sys
import threading
import tkinter as tk
import webbrowser
from datetime import datetime, timezone
from tkinter import ttk
from typing import Any, Optional

REFRESH_INTERVAL_MS = 60_000
POLL_INTERVAL_MS = 100
MAX_OPEN_PRS = 10
MAX_OPEN_ISSUES = 10
MAX_MERGED_PRS = 3
MAX_NOTIFICATIONS = 3
MAX_BRANCH_PAGES = 10
INITIAL_VISIBLE_PRS = 3
INITIAL_VISIBLE_ISSUES = 3
WRAP = 640
NUM_COL_MIN = 64

SECTION_TITLE_FONT = (".AppleSystemUIFontMedium", 10)
EMPTY_FONT = ("TkDefaultFont", 11, "italic")
WRAP_PAD = 16
MIN_WRAP = 200

LAUNCH_CWD = os.getcwd()


# ---------- dark mode colors ----------

def _detect_dark_mode() -> bool:
    try:
        proc = subprocess.run(
            ["defaults", "read", "-g", "AppleInterfaceStyle"],
            capture_output=True, text=True, check=False,
        )
    except OSError:
        return False
    return proc.returncode == 0 and proc.stdout.strip().lower() == "dark"


DARK_MODE = _detect_dark_mode()

if DARK_MODE:
    COL_LINK = "#58a6ff"
    COL_META = "#8b949e"
    COL_EMPTY = "#6e7681"
    COL_BG = "#1e1e1e"
else:
    COL_LINK = "#0366d6"
    COL_META = "#666666"
    COL_EMPTY = "#8a8f98"
    COL_BG = "#ffffff"


# ---------- shell helpers ----------

def run(cmd: list[str], cwd: Optional[str] = None) -> tuple[int, str, str]:
    proc = subprocess.run(
        cmd, capture_output=True, text=True, check=False, cwd=cwd
    )
    return proc.returncode, proc.stdout, proc.stderr


# ---------- parsing ----------

URL_RE = re.compile(
    r"^(?:https?://github\.com/|ssh://git@github\.com/|git@github\.com:)"
    r"([^/\s]+)/([^/\s#?]+?)"
    r"(?:\.git)?"
    r"(?:[/?#].*)?$"
)


def parse_repo_arg(arg: str) -> Optional[str]:
    arg = arg.strip()
    m = URL_RE.match(arg)
    if m:
        return f"{m.group(1)}/{m.group(2)}"
    if re.match(r"^[^/\s]+/[^/\s]+$", arg):
        return arg
    return None


def slug_from_git_dir(cwd: str) -> Optional[str]:
    rc, out, _ = run(["git", "-C", cwd, "remote", "get-url", "origin"])
    if rc != 0:
        return None
    return parse_repo_arg(out.strip())


def parse_iso(s: Optional[str]) -> Optional[datetime]:
    if not s:
        return None
    try:
        return datetime.fromisoformat(s.replace("Z", "+00:00"))
    except ValueError:
        return None


def format_dt(iso: Optional[str]) -> str:
    dt = parse_iso(iso)
    if dt is None:
        return "?"
    return dt.astimezone().strftime("%Y-%m-%d %H:%M")


def relative_time(iso: Optional[str]) -> str:
    dt = parse_iso(iso)
    if dt is None:
        return "?"
    delta = datetime.now(timezone.utc) - dt
    secs = int(delta.total_seconds())
    if secs < 0:
        return "just now"
    if secs < 60:
        return f"{secs}s ago"
    mins = secs // 60
    if mins < 60:
        return f"{mins}m ago"
    hours = mins // 60
    if hours < 24:
        return f"{hours}h ago"
    days = hours // 24
    if days < 30:
        return f"{days}d ago"
    if days < 365:
        return f"{days // 30}mo ago"
    return f"{days // 365}y ago"


# ---------- gh wrappers ----------

class FetchError(Exception):
    pass


def gh_json(args: list[str]) -> Any:
    rc, out, err = run(["gh", *args])
    if rc != 0:
        raise FetchError((err or out).strip() or f"gh {' '.join(args)} failed")
    if not out.strip():
        return None
    try:
        return json.loads(out)
    except json.JSONDecodeError as e:
        raise FetchError(f"invalid JSON from gh {' '.join(args)}: {e}") from e


# GitHub's GraphQL silently ignores orderBy:TAG_COMMIT_DATE on branch refs
# (it only honors it for tag refs), so we cannot push the sort to the server.
# We paginate all branches and sort client-side by committedDate.
LAST_COMMIT_QUERY = """
query($owner:String!, $name:String!, $cursor:String) {
  repository(owner:$owner, name:$name) {
    refs(refPrefix:"refs/heads/", first:100, after:$cursor) {
      pageInfo { hasNextPage endCursor }
      nodes {
        name
        target {
          ... on Commit {
            oid
            messageHeadline
            committedDate
            author { name }
            url
          }
        }
      }
    }
  }
}
"""


def fetch_all(slug: str) -> dict:
    result: dict[str, Any] = {"slug": slug, "errors": []}

    repo = gh_json([
        "repo", "view", slug,
        "--json",
        "name,owner,description,visibility,stargazerCount,url",
    ])
    result["repo"] = repo
    owner = repo["owner"]["login"]
    name = repo["name"]

    all_branches: list[dict] = []
    cursor: Optional[str] = None
    for _ in range(MAX_BRANCH_PAGES):
        args = [
            "api", "graphql",
            "-F", f"owner={owner}",
            "-F", f"name={name}",
            "-f", "query=" + LAST_COMMIT_QUERY,
        ]
        if cursor is not None:
            args.extend(["-F", f"cursor={cursor}"])
        page = gh_json(args)
        refs = (
            (((page or {}).get("data") or {}).get("repository") or {})
            .get("refs")
            or {}
        )
        all_branches.extend(refs.get("nodes") or [])
        page_info = refs.get("pageInfo") or {}
        if not page_info.get("hasNextPage"):
            break
        cursor = page_info.get("endCursor")
        if not cursor:
            break

    if not all_branches:
        raise FetchError("no branches found in repository")

    all_branches.sort(
        key=lambda n: (n.get("target") or {}).get("committedDate") or "",
        reverse=True,
    )
    node = all_branches[0]
    target = node.get("target") or {}
    result["last_commit"] = {
        "sha": target.get("oid") or "",
        "branch": node.get("name") or "?",
        "message": target.get("messageHeadline") or "",
        "committedDate": target.get("committedDate") or "",
        "authorName": (target.get("author") or {}).get("name") or "?",
        "url": target.get("url") or "",
    }
    sha = result["last_commit"]["sha"]

    try:
        result["check_runs"] = gh_json(
            ["api", f"repos/{slug}/commits/{sha}/check-runs"]
        )
    except FetchError as e:
        result["errors"].append(f"check-runs: {e}")
        result["check_runs"] = None

    try:
        result["release"] = gh_json([
            "release", "view", "-R", slug,
            "--json", "tagName,name,publishedAt,url",
        ])
    except FetchError as e:
        msg = str(e).lower()
        if "release not found" in msg or "no releases" in msg:
            result["release"] = None
        else:
            result["errors"].append(f"release: {e}")
            result["release"] = None

    result["open_prs"] = gh_json([
        "pr", "list", "-R", slug, "--state", "open",
        "--json", "number,title,author,isDraft,mergeable,updatedAt,url",
        "-L", str(MAX_OPEN_PRS),
    ]) or []

    result["merged_prs"] = gh_json([
        "pr", "list", "-R", slug, "--state", "merged",
        "--json", "number,title,author,mergedAt,url",
        "-L", str(MAX_MERGED_PRS),
    ]) or []

    result["open_issues"] = gh_json([
        "issue", "list", "-R", slug, "--state", "open",
        "--json", "number,title,author,updatedAt,url",
        "-L", str(MAX_OPEN_ISSUES),
    ]) or []

    try:
        all_notifs = gh_json(["api", "notifications"]) or []
        result["notifications"] = [
            n for n in all_notifs
            if (n.get("repository") or {}).get("full_name") == slug
        ]
    except FetchError as e:
        result["errors"].append(f"notifications: {e}")
        result["notifications"] = []

    return result


# ---------- check-run summary ----------

def summarize_checks(check_runs: Optional[dict]) -> tuple[str, str]:
    if not check_runs:
        return ("—", "no checks")
    runs = check_runs.get("check_runs") or []
    if not runs:
        return ("—", "no checks")
    failed = [
        r for r in runs
        if r.get("conclusion") in ("failure", "timed_out", "cancelled", "action_required")
    ]
    if failed:
        names = ", ".join(r.get("name", "?") for r in failed[:2])
        return ("✗", f"failing: {names}")
    statuses = [r.get("status") for r in runs]
    if any(s in ("queued", "in_progress", "waiting", "pending") for s in statuses):
        return ("⏳", "running")
    conclusions = [r.get("conclusion") for r in runs if r.get("conclusion")]
    if conclusions and all(c in ("success", "skipped", "neutral") for c in conclusions):
        return ("✓", "passing")
    return ("?", "mixed")


def notif_html_url(notif: dict) -> str:
    subj = notif.get("subject") or {}
    api_url = subj.get("url") or ""
    if not api_url:
        return (notif.get("repository") or {}).get("html_url", "")
    url = api_url.replace("https://api.github.com/repos/", "https://github.com/")
    url = url.replace("/pulls/", "/pull/")
    return url


# ---------- worker ----------

class Worker:
    def __init__(self, slug: str, cwd: str):
        self.slug = slug
        self.cwd = cwd
        self.requests: queue.Queue = queue.Queue()
        self.results: queue.Queue = queue.Queue()
        self.thread = threading.Thread(target=self._loop, daemon=True)
        self.thread.start()

    def request_fetch(self) -> None:
        self.requests.put("fetch")

    def _loop(self) -> None:
        while True:
            req = self.requests.get()
            if req == "stop":
                return
            try:
                data = fetch_all(self.slug)
                data["fatal"] = None
            except FetchError as e:
                data = {"fatal": str(e)}
            except Exception as e:
                data = {"fatal": f"unexpected: {type(e).__name__}: {e}"}
            data["fetched_at"] = datetime.now()
            self.results.put(data)


# ---------- UI ----------

class App:
    def __init__(self, root: tk.Tk, slug: Optional[str], cwd: str, init_error: Optional[str]):
        self.root = root
        self.slug = slug
        self.cwd = cwd
        self.last_data: Optional[dict] = None
        self.fetching = False
        self.worker: Optional[Worker] = None
        self.prs_expanded = False
        self.issues_expanded = False
        self._embedded_frames: list[tk.Widget] = []

        root.title(f"ghstatus · {slug or '(no repo)'}")
        root.geometry("760x920")
        root.minsize(520, 400)

        self._build_ui()

        if init_error:
            self._set_error(init_error)
            self.refresh_btn.state(["disabled"])
            return

        if not slug:
            self._set_error("Not in a git repo. Pass owner/name or a GitHub URL.")
            self.refresh_btn.state(["disabled"])
            return

        self.worker = Worker(slug, cwd)
        self._poll_results()
        self._manual_refresh()
        self.root.after(REFRESH_INTERVAL_MS, self._auto_refresh_tick)

    # ---- ui construction ----

    def _build_ui(self) -> None:
        # We use a tk.Text widget as the scrollable container. On Tk 9.0 /
        # macOS, trackpad two-finger scroll fires <TouchpadScroll> (single
        # angle brackets, *not* virtual <<TouchpadScroll>>), but only the
        # Text widget class has a default binding for it. We mirror that
        # binding via bind_all so scroll works no matter which embedded
        # child widget the cursor is hovering over.
        outer = ttk.Frame(self.root, padding=8)
        outer.pack(fill="both", expand=True)

        # Match the Text widget's bg to the ttk theme's TFrame bg so embedded
        # frames don't have a visible halo around them.
        style = ttk.Style()
        frame_bg = style.lookup("TFrame", "background") or COL_BG

        self.scroll = ttk.Scrollbar(outer, orient="vertical")
        self.text = tk.Text(
            outer,
            wrap="none",
            state="disabled",
            cursor="",
            takefocus=0,
            highlightthickness=0,
            borderwidth=0,
            padx=0,
            pady=0,
            background=frame_bg,
            yscrollcommand=self.scroll.set,
        )
        self.scroll.configure(command=self.text.yview)
        self.scroll.pack(side="right", fill="y")
        self.text.pack(side="left", fill="both", expand=True)

        self.root.bind_all("<TouchpadScroll>", self._on_touchpad_scroll)
        self.root.bind_all("<MouseWheel>", self._on_mousewheel)

        footer = ttk.Frame(self.root, padding=(12, 6))
        footer.pack(fill="x", side="bottom")
        self.refresh_btn = ttk.Button(footer, text="Refresh", command=self._manual_refresh)
        self.refresh_btn.pack(side="right")
        self.footer_label = ttk.Label(footer, text="Starting…")
        self.footer_label.pack(side="left")

    def _embed_frame(self, frame: tk.Widget) -> None:
        self.text.configure(state="normal")
        self.text.window_create("end", window=frame, stretch=True)
        self.text.insert("end", "\n")
        self.text.configure(state="disabled")
        self._embedded_frames.append(frame)

    def _on_touchpad_scroll(self, event: tk.Event) -> None:
        # When the cursor is directly over the Text widget, the Text class
        # binding already handles the scroll; skipping here prevents 2x speed.
        if event.widget is self.text:
            return
        # %D is packed: high 16 bits = deltaX, low 16 bits = signed deltaY.
        # Mirror of tk::PreciseScrollDeltas from the Tk 9.0 library.
        dxdy = event.delta
        low = dxdy & 0xFFFF
        dy = low if low < 0x8000 else low - 0x10000
        if dy:
            self.text.yview_scroll(-dy, "pixels")

    def _on_mousewheel(self, event: tk.Event) -> None:
        if event.widget is self.text:
            return
        delta = event.delta
        if not delta:
            return
        # Matches the tk::MouseWheel binding: amount/-4.0 pixels.
        pixels = int(round(-delta / 4.0))
        if pixels:
            self.text.yview_scroll(pixels, "pixels")

    def _bind_wrap(self, frame: tk.Widget, label: tk.Widget, offset: int) -> None:
        def update(event: tk.Event) -> None:
            new_wrap = max(event.width - offset, MIN_WRAP)
            try:
                current = int(label.cget("wraplength"))
            except (tk.TclError, ValueError):
                current = 0
            if abs(current - new_wrap) > 1:
                label.configure(wraplength=new_wrap)
        frame.bind("<Configure>", update)

    def _clear_body(self) -> None:
        self.text.configure(state="normal")
        self.text.delete("1.0", "end")
        self.text.configure(state="disabled")
        for frame in self._embedded_frames:
            frame.destroy()
        self._embedded_frames.clear()

    def _section(self, title: str) -> ttk.Frame:
        container = ttk.Frame(self.text)
        if title:
            ttk.Label(
                container,
                text=title,
                font=SECTION_TITLE_FONT,
            ).pack(anchor="w", pady=(0, 3))
        self._embed_frame(container)
        return container

    def _empty(self, parent: tk.Widget) -> None:
        ttk.Label(
            parent,
            text="None",
            foreground=COL_EMPTY,
            font=EMPTY_FONT,
        ).pack(anchor="w", padx=(16, 0))

    def _pill(
        self,
        parent: tk.Widget,
        text: str,
        bg: str,
        fg: str,
    ) -> tk.Canvas:
        font = ("TkDefaultFont", 8, "bold")
        pad_x, pad_y, radius = 9, 3, 999
        margin = 2
        probe = tk.Label(parent, text=text, font=font)
        probe.update_idletasks()
        text_w = probe.winfo_reqwidth()
        text_h = probe.winfo_reqheight()
        probe.destroy()
        inner_w = text_w + pad_x * 2
        inner_h = text_h + pad_y * 2
        w = inner_w + margin * 2
        h = inner_h + margin * 2
        style = ttk.Style()
        parent_bg = style.lookup("TFrame", "background") or self.root.cget("background")
        canvas = tk.Canvas(
            parent,
            width=w,
            height=h,
            highlightthickness=0,
            bd=0,
            bg=parent_bg,
        )
        x0, y0 = margin, margin
        x1, y1 = margin + inner_w, margin + inner_h
        r = min(radius, inner_h // 2)
        points = [
            x0 + r, y0,
            x1 - r, y0,
            x1, y0,
            x1, y0 + r,
            x1, y1 - r,
            x1, y1,
            x1 - r, y1,
            x0 + r, y1,
            x0, y1,
            x0, y1 - r,
            x0, y0 + r,
            x0, y0,
        ]
        canvas.create_polygon(points, smooth=True, fill=bg, outline=bg)
        canvas.create_text(w // 2, h // 2 + 1, text=text, fill=fg, font=font)
        return canvas

    def _numbered_row(
        self,
        parent: tk.Widget,
        num: str,
        title: str,
        meta: str,
        url: str,
        num_min: int = NUM_COL_MIN,
    ) -> None:
        row = ttk.Frame(parent)
        row.pack(fill="x", anchor="w", pady=(0, 4))
        row.grid_columnconfigure(0, minsize=num_min)
        row.grid_columnconfigure(1, weight=1)

        num_lbl = ttk.Label(row, text=num, foreground=COL_LINK, cursor="hand2")
        num_lbl.grid(row=0, column=0, sticky="nw")

        title_lbl = ttk.Label(
            row,
            text=title,
            foreground=COL_LINK,
            cursor="hand2",
            wraplength=MIN_WRAP,
            justify="left",
        )
        title_lbl.grid(row=0, column=1, sticky="w")
        self._bind_wrap(row, title_lbl, offset=num_min + WRAP_PAD)

        meta_lbl = ttk.Label(row, text=meta, foreground=COL_META)
        meta_lbl.grid(row=1, column=1, sticky="w")

        if url:
            for w in (num_lbl, title_lbl, meta_lbl):
                w.bind("<Button-1>", lambda e, u=url: webbrowser.open(u))

    def _add_expand_toggle(
        self,
        parent: tk.Widget,
        total: int,
        expanded: bool,
        on_toggle: Any,
    ) -> None:
        text = "Show fewer" if expanded else f"Show all {total}"
        lbl = ttk.Label(parent, text=text, foreground=COL_LINK, cursor="hand2")
        lbl.pack(anchor="w", padx=(16, 0), pady=(2, 0))
        lbl.bind("<Button-1>", lambda e: on_toggle())

    def _link_label(
        self,
        parent: tk.Widget,
        text: str,
        url: str,
        font: Any = None,
        wraplength: int = WRAP,
    ) -> ttk.Label:
        kwargs: dict[str, Any] = {
            "text": text,
            "foreground": COL_LINK,
            "cursor": "hand2",
            "wraplength": wraplength,
            "justify": "left",
        }
        if font is not None:
            kwargs["font"] = font
        lbl = ttk.Label(parent, **kwargs)
        if url:
            lbl.bind("<Button-1>", lambda e: webbrowser.open(url))
        return lbl

    # ---- refresh flow ----

    def _auto_refresh_tick(self) -> None:
        if self.worker is not None and not self.fetching:
            self.worker.request_fetch()
            self.fetching = True
            self.footer_label.configure(text="Refreshing…")
        self.root.after(REFRESH_INTERVAL_MS, self._auto_refresh_tick)

    def _manual_refresh(self) -> None:
        if self.worker is None or self.fetching:
            return
        self.worker.request_fetch()
        self.fetching = True
        self.refresh_btn.state(["disabled"])
        self.footer_label.configure(text="Refreshing…")

    def _poll_results(self) -> None:
        if self.worker is not None:
            try:
                while True:
                    data = self.worker.results.get_nowait()
                    self._on_data(data)
            except queue.Empty:
                pass
        self.root.after(POLL_INTERVAL_MS, self._poll_results)

    def _on_data(self, data: dict) -> None:
        self.fetching = False
        self.refresh_btn.state(["!disabled"])
        if data.get("fatal"):
            if self.last_data is None:
                self._set_error(data["fatal"])
            else:
                ts = datetime.now().strftime("%H:%M:%S")
                self.footer_label.configure(
                    text=f"Last refresh failed at {ts}: {data['fatal']}"
                )
            return
        self.last_data = data
        self._render(data)

    def _set_error(self, msg: str) -> None:
        self._clear_body()
        frame = ttk.Frame(self.text, padding=20)
        ttk.Label(
            frame, text="Error", font=("TkDefaultFont", 14, "bold")
        ).pack(anchor="w")
        ttk.Label(
            frame, text=msg, wraplength=WRAP, justify="left"
        ).pack(anchor="w", pady=(8, 0))
        self._embed_frame(frame)
        self.footer_label.configure(text=msg)

    # ---- rendering ----

    def _render(self, data: dict) -> None:
        prev_yview_top = self.text.yview()[0]
        self._clear_body()
        repo = data["repo"]
        slug = f"{repo['owner']['login']}/{repo['name']}"

        header = ttk.Frame(self.text)

        title_row = ttk.Frame(header)
        title_row.pack(fill="x")
        title_row.grid_columnconfigure(1, weight=1)

        visibility = (repo.get("visibility") or "").upper()
        vis_bg, vis_fg = {
            "PRIVATE": ("#9a3412", "white"),
            "PUBLIC": ("#1a7f37", "white"),
            "INTERNAL": ("#0969da", "white"),
        }.get(visibility, ("#57606a", "white"))
        if visibility:
            self._pill(title_row, visibility, vis_bg, vis_fg).grid(
                row=0, column=0, padx=(0, 8), sticky="w"
            )

        title_lbl = ttk.Label(
            title_row,
            text=slug,
            foreground=COL_LINK,
            cursor="hand2",
            font=("TkDefaultFont", 12, "bold"),
        )
        title_lbl.grid(row=0, column=1, sticky="w")
        if repo.get("url"):
            url = repo["url"]
            title_lbl.bind("<Button-1>", lambda e: webbrowser.open(url))

        ttk.Label(
            title_row,
            text=f"★ {repo.get('stargazerCount', 0)}",
            foreground=COL_META,
        ).grid(row=0, column=2, sticky="e")

        if repo.get("description"):
            desc_lbl = ttk.Label(
                header,
                text=repo["description"],
                wraplength=MIN_WRAP,
                justify="left",
            )
            desc_lbl.pack(anchor="w", pady=(4, 0))
            self._bind_wrap(header, desc_lbl, offset=WRAP_PAD)

        self._embed_frame(header)

        rel = data.get("release")
        if rel:
            rel_section = self._section("Latest release")
            tag = rel.get("tagName", "?")
            name = rel.get("name") or ""
            when = relative_time(rel.get("publishedAt"))
            text = tag + (f" — {name}" if name and name != tag else "") + f"  ·  {when}"
            self._link_label(rel_section, text, rel.get("url", "")).pack(anchor="w")

        commit_section = self._section("Last commit")
        lc = data["last_commit"]
        sha = (lc.get("sha") or "")[:7]
        branch = lc.get("branch", "?")
        when = format_dt(lc.get("committedDate"))
        rel = relative_time(lc.get("committedDate"))
        author = lc.get("authorName", "?")
        badge, badge_text = summarize_checks(data.get("check_runs"))
        self._numbered_row(
            commit_section,
            num=f"{sha} on {branch}",
            title=lc.get("message", ""),
            meta=f"{when}  ({rel})  ·  {author}  ·  {badge} {badge_text}",
            url=lc.get("url", ""),
            num_min=150,
        )

        notifs = data.get("notifications") or []
        if notifs:
            n_section = self._section(f"Notifications ({len(notifs)})")
            for n in notifs[:MAX_NOTIFICATIONS]:
                subj = n.get("subject") or {}
                title = subj.get("title", "?")
                reason = n.get("reason", "")
                self._link_label(
                    n_section, f"[{reason}] {title}", notif_html_url(n)
                ).pack(anchor="w")

        prs = data.get("open_prs") or []
        pr_count_label = f"{len(prs)}+" if len(prs) >= MAX_OPEN_PRS else str(len(prs))
        pr_section = self._section(f"Open PRs ({pr_count_label})")
        if not prs:
            self._empty(pr_section)
        else:
            visible = len(prs) if self.prs_expanded else INITIAL_VISIBLE_PRS
            for pr in prs[:visible]:
                self._render_pr_row(pr_section, pr)
            if len(prs) > INITIAL_VISIBLE_PRS:
                self._add_expand_toggle(
                    pr_section, len(prs), self.prs_expanded, self._toggle_prs
                )

        merged = data.get("merged_prs") or []
        if merged:
            m_section = self._section("Recently merged")
            for pr in merged:
                self._render_merged_row(m_section, pr)

        issues = data.get("open_issues") or []
        i_count_label = (
            f"{len(issues)}+" if len(issues) >= MAX_OPEN_ISSUES else str(len(issues))
        )
        i_section = self._section(f"Open issues ({i_count_label})")
        if not issues:
            self._empty(i_section)
        else:
            visible = len(issues) if self.issues_expanded else INITIAL_VISIBLE_ISSUES
            for issue in issues[:visible]:
                self._render_issue_row(i_section, issue)
            if len(issues) > INITIAL_VISIBLE_ISSUES:
                self._add_expand_toggle(
                    i_section, len(issues), self.issues_expanded, self._toggle_issues
                )

        ts = data.get("fetched_at") or datetime.now()
        suffix = ""
        errs = data.get("errors") or []
        if errs:
            suffix = f"  ·  partial: {len(errs)} sub-fetch error(s)"
        self.footer_label.configure(
            text=f"Last refreshed: {ts.strftime('%H:%M:%S')}{suffix}"
        )
        self.root.after_idle(lambda: self.text.yview_moveto(prev_yview_top))

    def _toggle_prs(self) -> None:
        self.prs_expanded = not self.prs_expanded
        if self.last_data is not None:
            self._render(self.last_data)

    def _toggle_issues(self) -> None:
        self.issues_expanded = not self.issues_expanded
        if self.last_data is not None:
            self._render(self.last_data)

    def _render_pr_row(self, parent: tk.Widget, pr: dict) -> None:
        author = (pr.get("author") or {}).get("login", "?")
        draft = " [draft]" if pr.get("isDraft") else ""
        when = format_dt(pr.get("updatedAt"))
        rel = relative_time(pr.get("updatedAt"))
        num = f"#{pr.get('number')}"
        title = f"{pr.get('title','')}{draft}"
        meta = f"{when}  ({rel})  ·  {author}"
        self._numbered_row(parent, num, title, meta, pr.get("url", ""))

    def _render_merged_row(self, parent: tk.Widget, pr: dict) -> None:
        author = (pr.get("author") or {}).get("login", "?")
        when = format_dt(pr.get("mergedAt"))
        rel = relative_time(pr.get("mergedAt"))
        num = f"#{pr.get('number')}"
        meta = f"{when}  ({rel})  ·  {author}"
        self._numbered_row(parent, num, pr.get("title", ""), meta, pr.get("url", ""))

    def _render_issue_row(self, parent: tk.Widget, issue: dict) -> None:
        author = (issue.get("author") or {}).get("login", "?")
        when = format_dt(issue.get("updatedAt"))
        rel = relative_time(issue.get("updatedAt"))
        num = f"#{issue.get('number')}"
        meta = f"{when}  ({rel})  ·  {author}"
        self._numbered_row(parent, num, issue.get("title", ""), meta, issue.get("url", ""))


# ---------- entry ----------

def main() -> int:
    parser = argparse.ArgumentParser(
        prog="ghstatus",
        description="Show a GitHub repo's status in a small desktop window.",
    )
    parser.add_argument(
        "repo",
        nargs="?",
        help="owner/name or GitHub URL (default: repo of current directory)",
    )
    args = parser.parse_args()

    init_error: Optional[str] = None
    slug: Optional[str] = None

    if args.repo:
        slug = parse_repo_arg(args.repo)
        if not slug:
            init_error = f"Could not parse repo argument: {args.repo!r}"
    else:
        slug = slug_from_git_dir(LAUNCH_CWD)
        if not slug:
            init_error = (
                "Not in a git repo with a GitHub origin. "
                "Pass owner/name or a GitHub URL as an argument."
            )

    if slug and init_error is None:
        rc, _, _ = run(["gh", "--version"])
        if rc != 0:
            init_error = "gh CLI not found. Install with: brew install gh"

    root = tk.Tk()
    App(root, slug, LAUNCH_CWD, init_error)
    root.mainloop()
    return 0


if __name__ == "__main__":
    sys.exit(main())
