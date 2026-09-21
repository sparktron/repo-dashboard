#!/usr/bin/env python3
"""repodash -- a live status dashboard for a folder full of git repos.

    ./repodash.py serve            # live dashboard at http://127.0.0.1:8787
    ./repodash.py export           # self-contained HTML snapshot
    ./repodash.py scan             # terminal report (for cron / quick checks)

Stdlib only. Reads git state; never writes to a repo. Binds loopback by
default -- the page exposes local paths and branch names, so opening it to
the network takes an explicit --host.
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlparse, parse_qs

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)

import scan as scanmod        # noqa: E402
import ghinfo                 # noqa: E402

DEFAULT_ROOT = os.environ.get("REPODASH_ROOT") or os.path.dirname(HERE)
DEFAULT_PORT = int(os.environ.get("REPODASH_PORT", "8787"))
ALLOWED_HOSTNAMES = {"localhost", "127.0.0.1", "[::1]", "::1"}


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

    def refresh(self, force_gh=False):
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
        with self.lock:
            self.data = d
        return d

    def get(self):
        with self.lock:
            if self.data is not None:
                return self.data
        return self.refresh()

    def run_background(self, interval, stop):
        while not stop.wait(interval):
            try:
                self.refresh()
            except Exception as e:
                print("[repodash] background refresh failed: %s" % e, file=sys.stderr)


# --------------------------------------------------------------------------
# http
# --------------------------------------------------------------------------

def make_handler(store):
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
            """Reject cross-origin hostnames -- blocks DNS-rebinding at a
            loopback bind, which is the whole security model here."""
            host = (self.headers.get("Host") or "").rsplit(":", 1)[0]
            return host in ALLOWED_HOSTNAMES or host == ""

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

    httpd = ThreadingHTTPServer((args.host, args.port), make_handler(store))
    url = "http://%s:%d/" % (args.host, args.port)
    print("[repodash] serving %s  (background rescan every %ds)" % (url, args.interval),
          flush=True)
    if args.host not in ("127.0.0.1", "localhost", "::1"):
        print("[repodash] WARNING: bound to %s -- this page exposes local paths, "
              "branch names and commit subjects to anyone who can reach it."
              % args.host, file=sys.stderr)
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
