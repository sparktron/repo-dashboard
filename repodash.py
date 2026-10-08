#!/usr/bin/env python3
"""repodash -- a live status dashboard for a folder full of git repos.

    ./repodash.py serve            # live dashboard at http://127.0.0.1:8787
    ./repodash.py export           # self-contained HTML snapshot
    ./repodash.py scan             # terminal report (for cron / quick checks)

Stdlib only. Scans are read-only; the only writes are the Pull / Push buttons
in the live page (fast-forward pull, non-force push), each run on an explicit
click. Binds loopback by default -- the page exposes local paths and branch
names, so opening it to the network takes an explicit --host. Pull / Push
only ever answer requests from this machine.
"""
from __future__ import annotations

import argparse
import ipaddress
import json
import os
import socket
import subprocess
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlparse, urlsplit, parse_qs

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)

import scan as scanmod        # noqa: E402
import ghinfo                 # noqa: E402

DEFAULT_ROOT = os.environ.get("REPODASH_ROOT") or os.path.dirname(HERE)
DEFAULT_PORT = int(os.environ.get("REPODASH_PORT", "8787"))
ALLOWED_HOSTNAMES = frozenset({"localhost", "127.0.0.1", "::1"})
WILDCARD_HOSTS = ("0.0.0.0", "::", "")
GIT_ACTION_TIMEOUT = 120


def host_hostname(value):
    """The hostname in a Host/Origin authority, lowercased and without port
    or IPv6 brackets: "[::1]:8787" -> "::1". None when it can't be parsed."""
    try:
        return urlsplit("//" + value).hostname or ""
    except ValueError:
        return None


def allowed_hostnames(bind_host, extra=()):
    """Hostnames the Host header may carry. Loopback always; the bind
    address when it is a specific one; for a wildcard bind, this machine's
    own names and addresses. Anything else (a VPN name, a reverse proxy)
    has to be named with --allow-host."""
    names = set(ALLOWED_HOSTNAMES)
    names.update(h for h in (host_hostname(x) for x in extra) if h)
    if bind_host not in WILDCARD_HOSTS:
        names.add(host_hostname(bind_host) or bind_host)
        return names
    try:
        hn = socket.gethostname()
        names.update({hn.lower(), socket.getfqdn().lower()})
        names.update(ai[4][0] for ai in socket.getaddrinfo(hn, None))
    except OSError:
        pass
    try:
        # The address the default route leaves from (a UDP connect sends nothing).
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
            s.connect(("192.0.2.1", 9))
            names.add(s.getsockname()[0])
    except OSError:
        pass
    return names


class ThreadingHTTPServerV6(ThreadingHTTPServer):
    address_family = socket.AF_INET6


def merge_extra(cli_also):
    """extra-repos.txt, then REPODASH_ALSO, then --also. Unique, order kept."""
    extras = scanmod.load_extra_repos()
    env = os.environ.get("REPODASH_ALSO") or ""
    for part in env.replace(",", ":").split(":"):
        part = part.strip()
        if part:
            extras.append(part)
    extras.extend(cli_also or [])
    seen, out = set(), []
    for path in extras:
        key = os.path.realpath(os.path.abspath(os.path.expanduser(path)))
        if key in seen:
            continue
        seen.add(key)
        out.append(path)
    return out


# --------------------------------------------------------------------------
# pull / push -- the only code path that writes to a repo
# --------------------------------------------------------------------------

def upstream_gone(repo):
    """The current branch tracks a remote branch that no longer exists."""
    return any(b["name"] == repo.get("branch") and b.get("gone")
               for b in repo.get("branches") or [])


def tracking_config(path, branch):
    """(remote, merge ref) for a branch, or (None, None) when it doesn't
    track a branch on a real remote ("." means a local upstream)."""
    _, remote, _ = scanmod.git(path, "config", "--get", "branch.%s.remote" % branch)
    _, merge, _ = scanmod.git(path, "config", "--get", "branch.%s.merge" % branch)
    if (not remote or remote == "." or remote.startswith("-")
            or not merge.startswith("refs/heads/")):
        return None, None
    return remote, merge


def git_action_args(repo, action, remote=None, merge=None):
    """The git argv for a button click, or (None, reason) when it can't run.

    Both commands name the remote and the exact ref. Left bare, `git push`
    defers to remote.<name>.push / push.default / pushRemote and can push
    other branches or a `+` (forcing) refspec; spelling it out pins the
    push to the one branch the user confirmed, never forced. Pull is
    fast-forward only, so a click can't rewrite history or make a merge."""
    if repo.get("operation"):
        return None, "%s in progress -- finish or abort it first" % repo["operation"]
    if repo.get("detached") or not repo.get("branch"):
        return None, "detached HEAD -- check out a branch first"
    upstream = repo.get("upstream")
    if upstream and not remote:
        return None, "upstream %s is not a branch on a remote" % upstream
    src = "refs/heads/" + repo["branch"]
    gone = upstream_gone(repo)
    if action == "pull":
        if not upstream:
            return None, "branch has no upstream to pull from"
        if gone:
            return None, "upstream %s was deleted on the remote" % upstream
        return ["pull", "--ff-only", "--no-rebase", remote, merge], None
    if action == "push":
        if upstream:
            if not gone and not repo.get("ahead"):
                return None, "nothing to push"
            return ["push", "--no-follow-tags", remote, "%s:%s" % (src, merge)], None
        if repo.get("has_remote"):
            return ["push", "--no-follow-tags", "-u", "origin", "%s:%s" % (src, src)], None
        return None, "no origin remote to push to"
    return None, "unknown action: %s" % action


def run_git_action(repo, action):
    """Returns (ok, output). Never prompts: no tty, no stdin, no askpass.
    `repo` must be a fresh scan -- the argv is built from its state."""
    remote = merge = None
    if repo.get("upstream") and not repo.get("detached"):
        remote, merge = tracking_config(repo["path"], repo["branch"])
    args, reason = git_action_args(repo, action, remote, merge)
    if args is None:
        return False, reason
    env = dict(os.environ, GIT_TERMINAL_PROMPT="0")
    try:
        p = subprocess.run(
            ["git", *args], cwd=repo["path"], env=env,
            stdin=subprocess.DEVNULL, capture_output=True, text=True,
            errors="replace", timeout=GIT_ACTION_TIMEOUT, start_new_session=True,
        )
    except subprocess.TimeoutExpired:
        return False, "git %s timed out after %ss" % (" ".join(args), GIT_ACTION_TIMEOUT)
    except OSError as e:
        return False, str(e)
    out = "\n".join(x for x in (p.stdout.strip(), p.stderr.strip()) if x)
    return p.returncode == 0, "$ git %s\n%s" % (" ".join(args), out or "(no output)")


# --------------------------------------------------------------------------
# page assembly -- one code path for both server and export
# --------------------------------------------------------------------------

def render_page(data=None):
    """Inline app.js, and the payload too when building a static snapshot."""
    with open(os.path.join(HERE, "ui.html"), encoding="utf-8") as f:
        html = f.read()
    with open(os.path.join(HERE, "app.js"), encoding="utf-8") as f:
        js = f.read()

    if data is not None:
        payload = json.dumps(data, separators=(",", ":"))
        # </script> inside data would close the tag early; < is safe in JSON.
        payload = payload.replace("<", "\\u003c")
        html = html.replace("/*__DATA__*/null/*__ENDDATA__*/", payload)

    html = html.replace('<script src="app.js"></script>',
                        "<script>\n" + js + "\n</script>")
    return html


# --------------------------------------------------------------------------
# snapshot store -- a background thread keeps this warm so requests never wait
# --------------------------------------------------------------------------

class Store:
    def __init__(self, root, extra=None, use_gh=True, gh_ttl=ghinfo.DEFAULT_TTL):
        self.root = root
        self.extra = list(extra or [])
        self.use_gh = use_gh
        self.gh_ttl = gh_ttl
        self.lock = threading.Lock()
        self.data = None
        self.busy_paths = set()   # repos with a pull/push in flight
        self._started = 0         # refreshes begun
        self._stored = 0          # sequence number of the scan in self.data

    def refresh(self, force_gh=False):
        with self.lock:
            self._started += 1
            seq = self._started
        d = scanmod.scan_all(self.root, extra=self.extra)
        if self.use_gh:
            try:
                d["gh_status"] = ghinfo.enrich(d["repos"], ttl=self.gh_ttl,
                                               force=force_gh)
            except Exception as e:  # gh must never take the dashboard down
                d["gh_status"] = {"available": False,
                                  "reason": "gh enrichment failed: %s" % e}
        else:
            d["gh_status"] = {"available": False, "reason": "disabled with --no-gh."}
        # Scans overlap (background thread, Refresh, a pull/push). One that
        # started earlier must not land last and put back pre-push state.
        with self.lock:
            if seq > self._stored:
                self.data, self._stored = d, seq
        return d

    def get(self):
        with self.lock:
            if self.data is not None:
                return self.data
        return self.refresh()

    def find(self, path):
        """A repo from the current scan -- the action endpoint only touches
        paths the dashboard itself discovered."""
        for r in self.get()["repos"]:
            if r["path"] == path:
                return r
        return None

    def claim(self, path):
        with self.lock:
            if path in self.busy_paths:
                return False
            self.busy_paths.add(path)
            return True

    def release(self, path):
        with self.lock:
            self.busy_paths.discard(path)

    def run_background(self, interval, stop):
        while not stop.wait(interval):
            try:
                self.refresh()
            except Exception as e:
                print("[repodash] background refresh failed: %s" % e, file=sys.stderr)


# --------------------------------------------------------------------------
# http
# --------------------------------------------------------------------------

def make_handler(store, allowed_hosts=None):
    allowed = frozenset(allowed_hosts or ALLOWED_HOSTNAMES)

    class Handler(BaseHTTPRequestHandler):
        server_version = "repodash"
        protocol_version = "HTTP/1.1"

        def log_message(self, fmt, *args):
            if os.environ.get("REPODASH_VERBOSE"):
                super().log_message(fmt, *args)

        def _send(self, code, body, ctype):
            if isinstance(body, str):
                body = body.encode("utf-8")
            self.send_response(code)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.send_header("X-Content-Type-Options", "nosniff")
            self.end_headers()
            self.wfile.write(body)

        def _host_ok(self):
            """Reject foreign hostnames -- blocks DNS-rebinding, which is the
            whole security model here."""
            host = host_hostname(self.headers.get("Host") or "")
            return host == "" or host in allowed

        def _client_is_local(self):
            """Pull/push run as this user on this machine; a page reached
            over --host is for looking, not for writing."""
            try:
                ip = ipaddress.ip_address(self.client_address[0])
            except ValueError:
                return False
            mapped = getattr(ip, "ipv4_mapped", None)
            return (mapped or ip).is_loopback

        def _origin_ok(self):
            """A cross-site page can still aim a POST at loopback with a
            loopback Host header, so writes also require a same-origin
            Origin (when sent) and a custom header. The header forces a
            CORS preflight, and this server never answers OPTIONS."""
            if self.headers.get("X-Repodash") != "1":
                return False
            origin = self.headers.get("Origin")
            if origin is None:
                return True
            try:
                return (urlparse(origin).hostname or "") in allowed
            except ValueError:
                return False

        def do_POST(self):
            if not self._host_ok() or not self._origin_ok():
                self._send(403, "forbidden\n", "text/plain")
                return
            if not self._client_is_local():
                self._send(403, "pull/push only from the machine running repodash\n",
                           "text/plain")
                return
            if urlparse(self.path).path != "/api/git":
                self._send(404, "not found\n", "text/plain")
                return
            try:
                length = int(self.headers.get("Content-Length") or 0)
            except ValueError:
                length = -1
            if not 0 < length <= 4096:
                self._send(400, "bad request\n", "text/plain")
                return
            try:
                req = json.loads(self.rfile.read(length))
                path, action, branch = req["path"], req["action"], req["branch"]
                upstream = req["upstream"]      # may be null: "no upstream"
            except (ValueError, KeyError, TypeError):
                self._send(400, "bad request\n", "text/plain")
                return
            if action not in ("pull", "push"):
                self._send(400, "unknown action\n", "text/plain")
                return
            if store.find(path) is None:
                self._send(404, "not a tracked repo\n", "text/plain")
                return
            if not store.claim(path):
                self._send(409, json.dumps({"ok": False, "action": action,
                                            "output": "another pull/push is running"}),
                           "application/json")
                return
            try:
                # The stored scan can be up to --interval old. Act only on
                # what the repo looks like now, and only if it is still on
                # the branch the user saw and confirmed, still tracking the
                # same upstream -- a bulk push can reach a repo minutes after
                # its one confirmation, and its target must not have moved.
                fresh = scanmod.scan_repo(path)
                if fresh.get("branch") != branch:
                    ok, output = False, (
                        "%s is now on %s, not %s -- nothing was run; check "
                        "the refreshed row and try again"
                        % (fresh.get("name"), fresh.get("branch") or "no branch", branch))
                elif fresh.get("upstream") != upstream:
                    ok, output = False, (
                        "%s %s now tracks %s, not %s -- nothing was run; check "
                        "the refreshed row and try again"
                        % (fresh.get("name"), branch, fresh.get("upstream") or "no upstream",
                           upstream or "no upstream"))
                else:
                    ok, output = run_git_action(fresh, action)
                d = store.refresh()
            finally:
                store.release(path)
            self._send(200, json.dumps({"ok": ok, "action": action,
                                        "output": output, "data": d}),
                       "application/json")

        def do_GET(self):
            if not self._host_ok():
                self._send(403, "forbidden host header\n", "text/plain")
                return
            u = urlparse(self.path)
            q = parse_qs(u.query)

            if u.path in ("/", "/index.html"):
                self._send(200, render_page(None), "text/html; charset=utf-8")
            elif u.path == "/api/repos":
                if q.get("force"):
                    d = store.refresh(force_gh=bool(q.get("gh")))
                else:
                    d = store.get()
                self._send(200, json.dumps(d), "application/json")
            elif u.path == "/healthz":
                self._send(200, "ok\n", "text/plain")
            else:
                self._send(404, "not found\n", "text/plain")

    return Handler


def cmd_serve(args):
    extra = merge_extra(args.also)
    store = Store(args.root, extra=extra, use_gh=not args.no_gh, gh_ttl=args.gh_ttl)
    print("[repodash] scanning %s …" % args.root, flush=True)
    d = store.refresh()
    print("[repodash] %d repos in %.2fs (%d need action)"
          % (d["summary"]["repo_count"], d["scan_seconds"], d["summary"]["needs_action"]),
          flush=True)

    stop = threading.Event()
    t = threading.Thread(target=store.run_background, args=(args.interval, stop),
                         daemon=True)
    t.start()

    allowed = allowed_hostnames(args.host, args.allow_host)
    server_cls = ThreadingHTTPServerV6 if ":" in args.host else ThreadingHTTPServer
    httpd = server_cls((args.host, args.port), make_handler(store, allowed))
    shown = "localhost" if args.host in WILDCARD_HOSTS else args.host
    url = "http://%s:%d/" % ("[%s]" % shown if ":" in shown else shown, args.port)
    print("[repodash] serving %s  (background rescan every %ds)" % (url, args.interval),
          flush=True)
    if args.host not in ("127.0.0.1", "localhost", "::1"):
        print("[repodash] WARNING: bound to %s -- this page exposes local paths, "
              "branch names and commit subjects to anyone who can reach it. "
              "Pull/Push stay limited to this machine.\n"
              "[repodash] accepted Host names: %s  (add more with --allow-host)"
              % (args.host, ", ".join(sorted(allowed))), file=sys.stderr)
    if args.open:
        import webbrowser
        threading.Timer(0.4, lambda: webbrowser.open(url)).start()
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        print("\n[repodash] stopping")
    finally:
        stop.set()
        httpd.server_close()
    return 0


def cmd_export(args):
    extra = merge_extra(args.also)
    store = Store(args.root, extra=extra, use_gh=not args.no_gh, gh_ttl=args.gh_ttl)
    d = store.refresh(force_gh=args.refresh_gh)
    html = render_page(d)
    out = os.path.abspath(args.out)
    os.makedirs(os.path.dirname(out), exist_ok=True)
    tmp = out + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        f.write(html)
    os.replace(tmp, out)
    print("[repodash] wrote %s (%.1f KB, %d repos, %d need action)"
          % (out, os.path.getsize(out) / 1024.0,
             d["summary"]["repo_count"], d["summary"]["needs_action"]))
    if args.json:
        jout = os.path.splitext(out)[0] + ".json"
        with open(jout, "w", encoding="utf-8") as f:
            json.dump(d, f, indent=1)
        print("[repodash] wrote %s" % jout)
    return 0


ANSI = {"critical": "\033[31;1m", "serious": "\033[33;1m", "warning": "\033[33m",
        "info": "\033[36m", "good": "\033[32m", "off": "\033[0m", "dim": "\033[2m"}


def cmd_scan(args):
    extra = merge_extra(args.also)
    store = Store(args.root, extra=extra, use_gh=not args.no_gh, gh_ttl=args.gh_ttl)
    d = store.refresh()
    if args.json:
        json.dump(d, sys.stdout, indent=1)
        print()
        return 0

    color = sys.stdout.isatty() and not args.no_color
    def c(k, s):
        return "%s%s%s" % (ANSI[k], s, ANSI["off"]) if color else s

    s = d["summary"]
    print("%s  %d repos, %d need action  (%.2fs)"
          % (d["root"], s["repo_count"], s["needs_action"], d["scan_seconds"]))
    print("-" * 78)
    for r in d["repos"]:
        if args.only_action and not r.get("needs_action"):
            continue
        mark = {"critical": "XX", "serious": "!!", "warning": " !",
                "info": " ·", "good": " ✓"}[r["status"]]
        print("%s %-26s %-22s %s"
              % (c(r["status"], mark), r["name"][:26],
                 (r.get("branch") or "-")[:22], c("dim", r["headline"])))
    if s["non_git_count"]:
        proj = [n["name"] for n in d["non_git"] if n.get("looks_like_project")]
        print("-" * 78)
        print("%d dirs not in git%s"
              % (s["non_git_count"],
                 (" — look like projects: " + ", ".join(proj)) if proj else ""))
    if args.exit_code and s["needs_action"]:
        return 1
    return 0


def main(argv=None):
    p = argparse.ArgumentParser(prog="repodash", description=__doc__,
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--root", default=DEFAULT_ROOT,
                   help="folder containing the repos (default: %(default)s)")
    p.add_argument("--also", action="append", default=[], metavar="PATH",
                   help="extra git repo, or folder of repos (repeatable)")
    p.add_argument("--no-gh", action="store_true",
                   help="skip the gh CLI entirely (no PR/CI columns)")
    p.add_argument("--gh-ttl", type=int, default=ghinfo.DEFAULT_TTL,
                   help="seconds a cached gh result stays fresh (default: %(default)s)")
    sub = p.add_subparsers(dest="cmd")

    sv = sub.add_parser("serve", help="run the live dashboard")
    sv.add_argument("--host", default="127.0.0.1")
    sv.add_argument("--allow-host", action="append", default=[], metavar="NAME",
                    help="extra hostname the page may be reached by, e.g. a VPN "
                         "name (repeatable; only matters with a non-loopback --host)")
    sv.add_argument("--port", type=int, default=DEFAULT_PORT)
    sv.add_argument("--interval", type=int, default=20,
                    help="background rescan interval in seconds (default: %(default)s)")
    sv.add_argument("--open", action="store_true", help="open a browser")
    sv.set_defaults(func=cmd_serve)

    ex = sub.add_parser("export", help="write a self-contained HTML snapshot")
    ex.add_argument("--out", default=os.path.join(HERE, "repo-status.html"))
    ex.add_argument("--json", action="store_true", help="also write the raw JSON")
    ex.add_argument("--refresh-gh", action="store_true",
                    help="bypass the gh cache for this export")
    ex.set_defaults(func=cmd_export)

    sc = sub.add_parser("scan", help="print a terminal report")
    sc.add_argument("--json", action="store_true")
    sc.add_argument("--only-action", action="store_true")
    sc.add_argument("--no-color", action="store_true")
    sc.add_argument("--exit-code", action="store_true",
                    help="exit 1 when any repo needs action (for cron)")
    sc.set_defaults(func=cmd_scan)

    args = p.parse_args(argv)
    if not args.cmd:
        p.print_help()
        return 2
    if not os.path.isdir(args.root):
        print("root is not a directory: %s" % args.root, file=sys.stderr)
        return 2
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
