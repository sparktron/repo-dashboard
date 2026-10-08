(function () {
  "use strict";

  var LIVE = BOOTSTRAP === null;
  var state = {
    data: BOOTSTRAP,
    q: "",
    statuses: new Set(),
    onlyAction: false,
    onlyAgent: false,
    sort: "attention",
    expanded: new Set(),
    busy: false,
    lastError: null,
    gitBusy: {},     // path -> "pull" | "push" while a click is in flight
    gitResult: {},   // path -> { action, ok, output, ts } from the last click
    gitBatch: null,  // current bulk action; also locks individual buttons
    batchResult: null
  };

  var SEV = { critical: 0, serious: 1, warning: 2, info: 3, good: 4 };
  var ICON = { critical: "✖", serious: "▲", warning: "●",
               info: "○", good: "✓" };
  var LABEL = { critical: "Critical", serious: "Serious", warning: "Attention",
                info: "Info", good: "Clean" };
  var COLORVAR = { critical: "--critical", serious: "--serious",
                   warning: "--warning", info: "--ink-3", good: "--good" };

  // ---------- helpers ----------
  function esc(s) {
    return String(s === null || s === undefined ? "" : s)
      .replace(/&/g, "&amp;").replace(/</g, "&lt;").replace(/>/g, "&gt;")
      .replace(/"/g, "&quot;").replace(/'/g, "&#39;");
  }
  function $(id) { return document.getElementById(id); }

  function age(ts) {
    if (!ts) return "—";
    var s = Math.floor(Date.now() / 1000) - ts;
    if (s < 0) s = 0;
    if (s < 60) return s + "s";
    if (s < 3600) return Math.floor(s / 60) + "m";
    if (s < 86400) return Math.floor(s / 3600) + "h";
    if (s < 86400 * 30) return Math.floor(s / 86400) + "d";
    if (s < 86400 * 365) return Math.floor(s / 2592000) + "mo";
    return (s / 31536000).toFixed(1) + "y";
  }
  function clock(ts) {
    if (!ts) return "—";
    return new Date(ts * 1000).toLocaleString(undefined,
      { month: "short", day: "numeric", hour: "2-digit", minute: "2-digit" });
  }

  // ---------- data shaping ----------
  function visibleRepos() {
    var d = state.data;
    if (!d) return [];
    var q = state.q.trim().toLowerCase();
    var out = d.repos.filter(function (r) {
      if (state.onlyAction && !r.needs_action) return false;
      if (state.onlyAgent && !r.on_agent_branch &&
          !(r.abandoned_agent_branches || []).length) return false;
      if (state.statuses.size && !state.statuses.has(r.status)) return false;
      if (!q) return true;
      var hay = [r.name, r.branch, r.headline, r.remote_slug,
                 (r.last_commit && r.last_commit.subject) || "",
                 (r.last_commit && r.last_commit.author) || ""].join(" ").toLowerCase();
      return hay.indexOf(q) !== -1;
    });

    var by = state.sort;
    out.sort(function (a, b) {
      if (by === "name") return a.name.localeCompare(b.name);
      if (by === "branch") return String(a.branch).localeCompare(String(b.branch))
        || a.name.localeCompare(b.name);
      if (by === "recent" || by === "stalest") {
        var ta = (a.last_commit && a.last_commit.ts) || 0;
        var tb = (b.last_commit && b.last_commit.ts) || 0;
        return by === "recent" ? tb - ta : ta - tb;
      }
      return (SEV[a.status] - SEV[b.status]) || a.name.localeCompare(b.name);
    });
    return out;
  }

  // ---------- render: tiles ----------
  function renderTiles() {
    var s = state.data.summary;
    var tiles = [
      { k: "Needs action", v: s.needs_action, status: null, filter: "action",
        title: "Repos with at least one non-informational flag" },
      { k: "On agent branch", v: s.on_agent_branch, status: "warning", filter: "agent",
        title: "Repos checked out on a ccode/claude/codex/cursor branch right now \u2014 an agent session left mid-flight" },
      { k: "Abandoned agent branches", v: s.abandoned_agent_branches, status: "warning", filter: "agent",
        title: "Agent branches nobody is on, holding unpushed commits or no upstream at all" },
      { k: "Uncommitted", v: s.dirty, status: "warning", filter: null,
        title: "Repos with real uncommitted work (agent scaffolding like .claude/ excluded)" },
      { k: "Unpushed", v: s.unpushed, status: "warning", filter: null,
        title: "Repos with commits ahead of their upstream, or on no remote at all" },
      { k: "Behind", v: s.behind, status: "info", filter: null,
        title: "Repos behind their upstream" },
      { k: "Clean & synced", v: s.counts.good, status: "good", filter: "good",
        title: "Nothing outstanding" },
      { k: "Repos tracked", v: s.repo_count, status: null, filter: null,
        title: "Git repos scanned (--root children plus extra-repos.txt)" }
    ];
    $("tiles").innerHTML = tiles.map(function (t) {
      var pressed = (t.filter === "action" && state.onlyAction) ||
                    (t.filter === "agent" && state.onlyAgent) ||
                    (t.filter === "good" && state.statuses.has("good"));
      var dot = t.status
        ? '<span class="dot" style="background:var(' + COLORVAR[t.status] + ')"></span>' : "";
      return '<button class="tile" data-filter="' + esc(t.filter || "") + '" ' +
             'aria-pressed="' + (pressed ? "true" : "false") + '" title="' + esc(t.title) + '">' +
             '<div class="v num">' + t.v + '</div>' +
             '<div class="k">' + dot + esc(t.k) + '</div></button>';
    }).join("");
  }

  // ---------- render: rows ----------
  function statusCell(r) {
    return '<span class="status s-' + r.status + '">' +
      '<span class="ico" aria-hidden="true">' + ICON[r.status] + '</span>' +
      esc(LABEL[r.status]) + '</span>';
  }

  function pushCell(r) {
    if (!r.has_remote) return '<span style="color:var(--ink-3)">no remote</span>';
    // Without a live upstream git has no ahead count; local_only_commits is
    // what the scan measured against every remote ref instead.
    var lo = r.local_only_commits
      ? ' <b style="color:var(--warning)" title="commits on no remote">↑' +
        r.local_only_commits + '</b>' : "";
    if (r.upstream === null || r.upstream === undefined)
      return '<span style="color:var(--serious)">untracked</span>' + lo;
    if (upstreamGone(r))
      return '<span style="color:var(--serious)" title="' + esc(r.upstream) +
        ' was deleted on the remote">upstream gone</span>' + lo;
    var a = r.ahead || 0, b = r.behind || 0;
    if (!a && !b) return '<span style="color:var(--good)">✓ synced</span>';
    var parts = [];
    if (a) parts.push('<b style="color:var(--warning)">↑' + a + '</b>');
    if (b) parts.push('<b style="color:var(--ink-2)">↓' + b + '</b>');
    return '<span class="counts">' + parts.join(" ") + '</span>';
  }

  // The current branch tracks a remote branch that has since been deleted.
  function upstreamGone(r) {
    return (r.branches || []).some(function (b) { return b.name === r.branch && b.gone; });
  }

  // Mirrors git_action_args() in repodash.py; the server re-checks anyway.
  function gitBlock(r, action) {
    if (r.operation) return r.operation + " in progress";
    if (r.detached || !r.branch) return "detached HEAD";
    var gone = upstreamGone(r);
    if (action === "pull") {
      if (!r.upstream) return "no upstream to pull from";
      if (gone) return "upstream " + r.upstream + " was deleted on the remote";
      return null;
    }
    if (!r.has_remote) return "no origin remote";
    if (r.upstream && !r.ahead && !gone) return "nothing to push";
    return null;
  }

  function syncButtons(r) {
    if (!LIVE || !r.has_remote) return "";
    var busy = state.gitBusy[r.path];
    var res = state.gitResult[r.path];
    function btn(action, label, title) {
      var why = gitBlock(r, action);
      var text = busy === action ? (action === "pull" ? "Pulling…" : "Pushing…") : label;
      return '<button class="gitbtn" data-git="' + action + '" data-path="' + esc(r.path) + '"' +
        ((why || busy || state.gitBatch) ? " disabled" : "") +
        ' title="' + esc(why || title) + '">' + text + '</button>';
    }
    var pushTitle = !r.upstream ? "git push -u origin " + (r.branch || "")
      : upstreamGone(r) ? "recreate " + r.upstream + " on the remote"
      : "git push to " + r.upstream;
    var msg = "";
    if (res && !busy) {
      msg = res.ok
        ? '<span class="gitmsg" style="color:var(--good)" title="' + esc(res.output) + '">✓ ' +
          (res.action === "pull" ? "pulled" : "pushed") + '</span>'
        : '<span class="gitmsg" style="color:var(--critical)" title="' + esc(res.output) + '">✖ ' +
          res.action + ' failed</span>';
    }
    return '<div class="sync">' + msg +
      btn("pull", "Pull", "git pull --ff-only from " + (r.upstream || "")) +
      btn("push", "Push", pushTitle) + '</div>';
  }

  function treeCell(r) {
    if (r.conflicts) return '<b style="color:var(--critical)">' + r.conflicts + ' conflicted</b>';
    var noise = r.untracked_noise
      ? '<span style="color:var(--ink-3)" title="agent scaffolding (' +
        esc((r.noise_paths || []).join(", ")) + ') \u2014 not counted as work">+' +
        r.untracked_noise + ' scaffold</span>' : "";
    if (!r.dirty_real) {
      return noise || '<span style="color:var(--ink-3)">clean</span>';
    }
    var p = [];
    if (r.staged) p.push('<span>S</span><b>' + r.staged + '</b>');
    if (r.unstaged) p.push('<span>M</span><b>' + r.unstaged + '</b>');
    if (r.untracked_real) p.push('<span>?</span><b>' + r.untracked_real + '</b>');
    return '<span class="counts" title="staged / modified / untracked">' +
           p.join(" ") + (noise ? " " + noise : "") + '</span>';
  }

  function ciCell(r) {
    var bits = [];
    var gh = r.gh;
    if (gh && gh.pr_count) {
      bits.push('<span class="badge on">' + gh.pr_count + ' PR' +
                (gh.pr_count > 1 ? "s" : "") + '</span>');
    }
    if (gh && gh.ci) {
      var lvl = r.ci_level || "info";
      var txt = gh.ci.status === "completed" ? (gh.ci.conclusion || "done") : gh.ci.status;
      bits.push('<span class="status s-' + lvl + '" style="font-size:11px">' +
                '<span class="ico" aria-hidden="true">' + ICON[lvl] + '</span>' + esc(txt) + '</span>');
    }
    if (!bits.length) return '<span style="color:var(--ink-3)">—</span>';
    return bits.join(" ");
  }

  function agentCell(r) {
    var m = r.markers || {};
    var defs = [["claude_md", "CLAUDE"], ["agents_md", "AGENTS"], ["claude_dir", ".claude"],
                ["mcp_config", "MCP"], ["github_actions", "CI"]];
    var on = defs.filter(function (d) { return m[d[0]]; });
    if (!on.length) return '<span style="color:var(--ink-3)">—</span>';
    return '<span class="badges">' + on.map(function (d) {
      return '<span class="badge on">' + esc(d[1]) + '</span>';
    }).join("") + '</span>';
  }

  function detailHTML(r) {
    var cols = [];

    if (r.flags && r.flags.length) {
      cols.push('<div><h4>All flags</h4><ul>' + r.flags.map(function (f) {
        return '<li><span class="status s-' + f.level + '" style="font-size:11px">' +
               '<span class="ico" aria-hidden="true">' + ICON[f.level] + '</span></span> ' +
               esc(f.text) + '</li>';
      }).join("") + '</ul></div>');
    }

    var lc = r.last_commit;
    var info = ['<div><h4>Repo</h4><ul>'];
    info.push('<li><span class="mono">' + esc(r.path) + '</span></li>');
    if (r.remote_slug) info.push('<li>remote: <span class="mono">' + esc(r.remote_slug) + '</span></li>');
    else if (r.remote_url) info.push('<li>remote: <span class="mono">' + esc(r.remote_url) + '</span></li>');
    info.push('<li>' + (r.branch_count || 0) + ' local branch(es), ' +
              (r.stash_count || 0) + ' stash entr(ies)</li>');
    if (lc) info.push('<li>last commit ' + esc(clock(lc.ts)) + ' by ' + esc(lc.author) +
                      ' <span class="mono">' + esc(lc.short) + '</span></li>');
    if (r.git_touched_ts) info.push('<li>git last touched this repo ' + esc(age(r.git_touched_ts)) + ' ago</li>');
    info.push('</ul></div>');
    cols.push(info.join(""));

    if (lc) {
      cols.push('<div><h4>Latest commit message</h4><div class="cmd">' +
                esc(lc.subject) + '</div></div>');
    }

    var ab = r.abandoned_agent_branches || [];
    if (ab.length) {
      cols.push('<div><h4>Abandoned agent branches (' + ab.length + ')</h4><ul>' +
        ab.slice(0, 25).map(function (b) {
          var d = b.upstream ? ("↑" + b.ahead + " unpushed, ↓" + b.behind + " behind")
                             : "never pushed";
          return '<li><span class="mono">' + esc(b.name) + '</span> — ' + esc(d) +
                 (b.ts ? ' <span style="color:var(--ink-3)">(' + esc(age(b.ts)) + ' old)</span>' : "") +
                 '</li>';
        }).join("") + '</ul>' +
        (ab.length > 25 ? '<div style="font-size:11px;color:var(--ink-3)">…and ' +
          (ab.length - 25) + ' more</div>' : "") + '</div>');
    }

    // Agent branches already have their own block above; don't list them twice.
    var abNames = {};
    ab.forEach(function (b) { abNames[b.name] = 1; });
    var ub = (r.unpushed_branches || []).filter(function (b) {
      return b.name !== r.branch && !abNames[b.name];
    });
    if (ub.length) {
      cols.push('<div><h4>Other branches with work</h4><ul>' + ub.map(function (b) {
        var d = b.upstream ? ("↑" + b.ahead) : "no upstream";
        return '<li><span class="mono">' + esc(b.name) + '</span> — ' + esc(d) + '</li>';
      }).join("") + '</ul></div>');
    }

    if (r.worktrees && r.worktrees.length) {
      cols.push('<div><h4>Extra worktrees</h4><ul>' + r.worktrees.map(function (w) {
        return '<li><span class="mono">' + esc(w.branch || "?") + '</span> at ' +
               '<span class="mono">' + esc(w.path) + '</span></li>';
      }).join("") + '</ul></div>');
    }

    if (r.changed_files && r.changed_files.length) {
      cols.push('<div><h4>Changed files (first ' + r.changed_files.length + ')</h4><ul>' +
        r.changed_files.map(function (f) {
          return '<li><span class="mono">' + esc(f) + '</span></li>';
        }).join("") + '</ul></div>');
    }

    if (r.gh && r.gh.prs && r.gh.prs.length) {
      cols.push('<div><h4>Open pull requests</h4><ul>' + r.gh.prs.map(function (p) {
        return '<li><a href="' + esc(p.url) + '" target="_blank" rel="noopener">#' +
               p.number + '</a> ' + esc(p.title) + (p.draft ? " <em>(draft)</em>" : "") + '</li>';
      }).join("") + '</ul></div>');
    }

    var cmds = suggestions(r);
    if (cmds.length) {
      cols.push('<div><h4>Suggested next step</h4><div class="cmd">' +
                cmds.map(esc).join("\n") + '</div></div>');
    }

    var gr = state.gitResult[r.path];
    if (gr) {
      cols.push('<div><h4>Last ' + esc(gr.action) + ' (' + esc(age(gr.ts)) + ' ago)</h4>' +
                '<div class="cmd" style="' + (gr.ok ? "" : "border-color:var(--critical)") + '">' +
                esc(gr.output) + '</div></div>');
    }

    if (r.errors && r.errors.length) {
      cols.push('<div><h4>Errors</h4><ul>' + r.errors.map(function (e) {
        return '<li style="color:var(--critical)">' + esc(e) + '</li>';
      }).join("") + '</ul></div>');
    }

    return '<div class="detailbox">' + cols.join("") + '</div>';
  }

  // POSIX single-quoting, so a path with spaces pastes as one argument.
  function shq(s) {
    s = String(s);
    return /^[\w@%+=:,.\/-]+$/.test(s) ? s : "'" + s.replace(/'/g, "'\\''") + "'";
  }

  // The repo's own main branch -- not every repo calls it `main`.
  function baseBranch(r) {
    var have = {};
    (r.branches || []).forEach(function (b) { have[b.name] = 1; });
    var mains = ["main", "master", "trunk", "develop"];
    for (var i = 0; i < mains.length; i++) if (have[mains[i]]) return mains[i];
    return "main";
  }

  // Commands are shown, never run. Read before pasting.
  function suggestions(r) {
    var ids = (r.flags || []).map(function (f) { return f.id; });
    var has = function (id) { return ids.indexOf(id) !== -1; };
    var g = "git -C " + shq(r.path), out = [];
    var br = shq(r.branch || "HEAD");
    // git_dir, not <path>/.git: in a linked worktree .git is a file.
    if (has("stale-lock"))
      out.push("rm " + shq((r.git_dir || r.path + "/.git") + "/index.lock"));
    if (has("conflicts")) out.push(g + " status");
    if (has("operation")) out.push(g + " status   # then --continue or --abort");
    if (has("detached")) out.push(g + " switch -c " + shq("rescue/" + r.name));
    if (has("uncommitted")) out.push(g + " add -A && " + g + " commit");
    if (has("unpushed")) out.push(g + " push");
    if (has("on-agent-branch"))
      out.push(g + " log --oneline " + shq(baseBranch(r) + ".." + (r.branch || "HEAD")) +
               "   # review, then merge or abandon");
    if (has("agent-branch-buildup"))
      out.push(g + " branch --list 'ccode/*' 'claude/*' 'codex/*'" +
               "   # review before deleting anything");
    if (has("agent-scaffolding"))
      out.push("echo '.claude/' >> " + shq(r.path + "/.gitignore"));
    if (has("diverged")) out.push(g + " pull --rebase   # review before pushing");
    if (has("behind")) out.push(g + " pull --ff-only");
    if (has("no-upstream") || has("local-only"))
      out.push(g + " push -u origin " + br);
    return out;
  }

  function renderRows() {
    var repos = visibleRepos();
    var tb = $("rows");
    if (!repos.length) {
      tb.innerHTML = "";
      $("emptyMsg").hidden = false;
      return;
    }
    $("emptyMsg").hidden = true;

    tb.innerHTML = repos.map(function (r) {
      var open = state.expanded.has(r.path);
      var lc = r.last_commit;
      var nameCell = r.remote_slug
        ? '<a href="https://' + esc(r.remote_host) + '/' + esc(r.remote_slug) +
          '" target="_blank" rel="noopener" onclick="event.stopPropagation()">' + esc(r.name) + '</a>'
        : esc(r.name);
      var branchWarn = r.detached ? ' <span class="warnmark" title="detached HEAD">⚠</span>' : "";
      if (r.on_agent_branch) {
        branchWarn += ' <span class="badge" style="color:var(--serious);border-color:var(--serious)" ' +
          'title="an agent session branch, not a main branch">agent</span>';
      }
      var abandoned = (r.abandoned_agent_branches || []).length;
      if (abandoned) {
        branchWarn += ' <span class="badge" title="' + abandoned +
          ' abandoned agent branches">+' + abandoned + '</span>';
      }

      var row =
        '<tr class="repo' + (open ? " open" : "") + '" data-path="' + esc(r.path) +
          '" tabindex="0" aria-expanded="' + open + '">' +
          '<td style="color:var(--ink-3)">' + (open ? "▾" : "▸") + '</td>' +
          '<td>' + statusCell(r) + '</td>' +
          '<td><div class="name">' + nameCell + '</div>' +
              '<div class="headline">' + esc(r.headline) + '</div></td>' +
          '<td><span class="branch">' + esc(r.branch || "—") + branchWarn + '</span></td>' +
          '<td class="num">' + pushCell(r) + syncButtons(r) + '</td>' +
          '<td class="num col-opt">' + treeCell(r) + '</td>' +
          '<td class="col-opt"><div class="age">' + (lc ? esc(age(lc.ts)) + " ago" : "—") + '</div>' +
              (lc ? '<div class="subject" title="' + esc(lc.subject) + '">' + esc(lc.subject) + '</div>' : "") +
          '</td>' +
          '<td class="col-opt">' + ciCell(r) + '</td>' +
          '<td class="col-opt">' + agentCell(r) + '</td>' +
        '</tr>';

      if (open) {
        row += '<tr class="detail"><td colspan="9">' + detailHTML(r) + '</td></tr>';
      }
      return row;
    }).join("");
  }

  function renderNonGit() {
    var ng = (state.data.non_git || []).slice().sort(function (a, b) {
      return (b.looks_like_project ? 1 : 0) - (a.looks_like_project ? 1 : 0)
        || a.name.localeCompare(b.name);
    });
    $("ngCount").textContent = ng.length;
    $("nongit").innerHTML = ng.map(function (d) {
      var sub = d.error ? esc(d.error)
        : d.looks_like_project
          ? "looks like a project — " + esc(d.project_signals.slice(0, 4).join(", "))
          : (d.entry_count || 0) + " entries";
      var mark = d.looks_like_project
        ? '<span class="badge" style="color:var(--warning);border-color:var(--warning)">not in git</span> ' : "";
      return '<div><div class="n">' + mark + esc(d.name) + '</div><div class="s">' + sub + '</div></div>';
    }).join("");
  }

  function renderGhNotice() {
    var gh = state.data.gh_status;
    var box = $("ghNotice");
    if (!gh) { box.innerHTML = ""; return; }
    if (gh.available) {
      box.innerHTML = "";
      return;
    }
    box.innerHTML = '<div class="notice warn"><b>PR / CI columns are off.</b> ' +
      esc(gh.reason) + ' Everything else on this page is unaffected.</div>';
  }

  function render() {
    renderBulkControls();
    if (!state.data) return;
    var d = state.data;
    $("rootpath").textContent = (d.extra && d.extra.length)
      ? d.root + " +" + d.extra.length + " extra"
      : d.root;
    $("rootpath").title = d.extra && d.extra.length
      ? [d.root].concat(d.extra).join("\n")
      : (d.root || "");
    $("scanTime").textContent = "scanned " + clock(d.generated_at) +
      " (" + d.scan_seconds + "s)";
    $("footScan").textContent = d.summary.repo_count + " git repos, " +
      d.summary.non_git_count + " other dirs · scan took " + d.scan_seconds + "s";
    var badge = $("modeBadge");
    badge.className = "mode-badge " + (LIVE ? "live" : "snapshot") + (state.busy ? " busy" : "");
    $("modeText").textContent = state.lastError ? "error"
      : LIVE ? (state.busy ? "refreshing" : "live") : "snapshot";
    badge.title = state.lastError || "";

    $("onlyAction").setAttribute("aria-pressed", state.onlyAction);
    $("onlyAgent").setAttribute("aria-pressed", state.onlyAgent);
    renderTiles();
    renderGhNotice();
    renderRows();
    renderNonGit();
  }

  // ---------- live refresh ----------
  var timer = null;
  function scheduleRefresh() {
    if (timer) clearInterval(timer);
    var secs = parseInt($("interval").value, 10);
    if (LIVE && secs > 0) timer = setInterval(refresh, secs * 1000);
  }

  function refresh() {
    if (!LIVE || state.busy) return;
    state.busy = true; render();
    var y = window.scrollY;
    fetch("/api/repos", { cache: "no-store" })
      .then(function (res) {
        if (!res.ok) throw new Error("server returned " + res.status);
        return res.json();
      })
      .then(function (d) {
        state.data = d; state.lastError = null; state.busy = false;
        render(); window.scrollTo(0, y);
      })
      .catch(function (e) {
        state.lastError = e.message; state.busy = false; render();
      });
  }

  function renderBulkControls() {
    var locked = !LIVE || !state.data || !state.data.repos.length ||
      !!state.gitBatch || Object.keys(state.gitBusy).length > 0;
    $("pullAll").disabled = locked;
    $("pushAll").disabled = locked;
    var batch = state.gitBatch || state.batchResult;
    $("bulkNotice").hidden = !batch;
    if (!batch) return;
    var label = batch.action === "pull" ? "Pull all" : "Push all";
    $("bulkStatus").textContent = state.gitBatch
      ? label + ": " + batch.done + "/" + batch.total + " repos processed…"
      : label + ": " + batch.ok + " succeeded, " + batch.failed + " failed, " +
        batch.skipped + " skipped (" + batch.total + " repos).";
    $("bulkDetails").hidden = !batch.issues.length;
    $("bulkIssues").innerHTML = batch.issues.map(function (issue) {
      return '<li><b>' + esc(issue.repo.name) + '</b> (' + esc(issue.repo.path) +
        '): ' + esc(issue.status) + ' — ' + esc(issue.output) + '</li>';
    }).join("");
  }

  function gitAction(path, action) {
    if (!LIVE || !state.data || state.gitBatch || state.gitBusy[path]) return;
    var r = state.data.repos.find(function (x) { return x.path === path; });
    if (!r || gitBlock(r, action)) return;
    if (action === "push" &&
        !window.confirm("Push " + (r.branch || "HEAD") + " in " + r.name + " to " +
                        (r.upstream || "origin/" + r.branch) + "?")) return;
    return runGitAction(r, action);
  }

  function runGitAction(r, action) {
    var path = r.path;
    state.gitBusy[path] = action; render();
    return fetch("/api/git", {
      method: "POST",
      cache: "no-store",
      headers: { "Content-Type": "application/json", "X-Repodash": "1" },
      // The server refuses if the repo has left this branch since the scan.
      body: JSON.stringify({ path: path, action: action, branch: r.branch })
    })
      .then(function (res) {
        return res.text().then(function (t) {
          var j = null;
          try { j = JSON.parse(t); } catch (err) { /* plain-text error body */ }
          if (!j) throw new Error("server returned " + res.status + ": " + t.trim());
          return j;
        });
      })
      .then(function (j) {
        if (j.data) state.data = j.data;
        return { action: action, ok: j.ok, output: j.output };
      }, function (err) {
        return { action: action, ok: false, output: err.message };
      })
      .then(function (res) {
        res.ts = Math.floor(Date.now() / 1000);
        state.gitResult[path] = res;
        delete state.gitBusy[path];
        if (!res.ok) state.expanded.add(path);
        render();
        return res;
      });
  }

  async function gitActionAll(action) {
    if (!LIVE || !state.data || state.gitBatch || Object.keys(state.gitBusy).length) return;
    // Snapshot every repo, including ones hidden by filters. Keep the branches
    // the user saw even when earlier requests return a refreshed scan.
    var repos = state.data.repos.slice();
    if (!repos.length) return;
    var eligible = repos.filter(function (r) { return !gitBlock(r, action); });
    if (action === "push" && eligible.length && !window.confirm(
      "Push all: push " + eligible.length + " repos; skip " +
      (repos.length - eligible.length) + " repos that cannot push.\n\n" +
      eligible.map(function (r) {
        return r.name + " (" + r.path + "): " + r.branch + " → " +
          (r.upstream || "origin/" + r.branch);
      }).join("\n") + "\n\nContinue?")) return;
    var batch = { action: action, total: repos.length, done: 0,
      ok: 0, failed: 0, skipped: 0, issues: [] };
    state.gitBatch = batch;
    render();
    try {
      for (var r of repos) {
        var why = gitBlock(r, action);
        if (why) {
          batch.skipped++;
          batch.issues.push({ repo: r, status: "skipped", output: why });
        } else {
          // One request at a time; a failure must not prevent later repos.
          var result = await runGitAction(r, action);
          if (result.ok) batch.ok++;
          else {
            batch.failed++;
            batch.issues.push({ repo: r, status: "failed", output: result.output });
          }
        }
        batch.done++;
        renderBulkControls();
      }
    } finally {
      state.batchResult = batch;
      state.gitBatch = null;
      render();
    }
  }

  // ---------- events ----------
  document.addEventListener("click", function (e) {
    var gb = e.target.closest(".gitbtn");
    if (gb) {
      gitAction(gb.getAttribute("data-path"), gb.getAttribute("data-git"));
      return;
    }
    var tile = e.target.closest(".tile");
    if (tile) {
      var f = tile.getAttribute("data-filter");
      if (f === "action") { state.onlyAction = !state.onlyAction; state.statuses.clear(); }
      else if (f === "agent") {
        state.onlyAgent = !state.onlyAgent;
        state.onlyAction = false; state.statuses.clear();
      }
      else if (f === "good") {
        state.onlyAction = false;
        if (state.statuses.has("good")) state.statuses.delete("good");
        else { state.statuses.clear(); state.statuses.add("good"); }
      } else return;
      render(); return;
    }
    var row = e.target.closest("tr.repo");
    if (row) {
      var n = row.getAttribute("data-path");
      if (state.expanded.has(n)) state.expanded.delete(n); else state.expanded.add(n);
      renderRows();
    }
  });

  document.addEventListener("keydown", function (e) {
    if (e.target.tagName === "BUTTON" && e.key === "Enter") return;
    if (e.target.tagName === "INPUT" || e.target.tagName === "SELECT") {
      if (e.key === "Escape") { e.target.blur(); }
      return;
    }
    if (e.key === "/") { e.preventDefault(); $("q").focus(); }
    else if (e.key === "r") { refresh(); }
    else if (e.key === "e") { $("expandAll").click(); }
    else if (e.key === "Enter") {
      var row = e.target.closest && e.target.closest("tr.repo");
      if (row) row.click();
    }
  });

  $("q").addEventListener("input", function () { state.q = this.value; renderRows(); });
  $("onlyAction").addEventListener("click", function () {
    state.onlyAction = !state.onlyAction;
    this.setAttribute("aria-pressed", state.onlyAction);
    render();
  });
  $("onlyAgent").addEventListener("click", function () {
    state.onlyAgent = !state.onlyAgent;
    this.setAttribute("aria-pressed", state.onlyAgent);
    render();
  });
  $("sort").addEventListener("change", function () { state.sort = this.value; renderRows(); });
  $("refresh").addEventListener("click", refresh);
  $("pullAll").addEventListener("click", function () { return gitActionAll("pull"); });
  $("pushAll").addEventListener("click", function () { return gitActionAll("push"); });
  $("interval").addEventListener("change", scheduleRefresh);
  $("expandAll").addEventListener("click", function () {
    var vis = visibleRepos();
    var allOpen = vis.length && vis.every(function (r) { return state.expanded.has(r.path); });
    if (allOpen) state.expanded.clear();
    else vis.forEach(function (r) { state.expanded.add(r.path); });
    renderRows();
  });
  // ---------- theme ----------
  // The inline script in ui.html already applied any stored choice before
  // paint; this only handles changes made from the button.
  var THEME_KEY = "repodash-theme";
  var THEME_ICON = { light: "☀", dark: "☾", system: "◐" };

  function applyTheme(mode) {
    var root = document.documentElement;
    if (mode === "system") root.removeAttribute("data-theme");
    else root.setAttribute("data-theme", mode);
    try {
      if (mode === "system") localStorage.removeItem(THEME_KEY);
      else localStorage.setItem(THEME_KEY, mode);
    } catch (e) { /* storage blocked — theme still applies for this page load */ }
    var btn = $("theme");
    btn.textContent = THEME_ICON[mode];
    btn.title = "Theme: " + mode + " (click to change)";
    btn.setAttribute("aria-label", "Theme: " + mode);
  }

  // Three-state cycle so "follow the OS" stays reachable. The order is seeded
  // from the OS preference: leaving system goes to the opposite of what is on
  // screen, so the first click always visibly changes something.
  function nextTheme(cur) {
    var osDark = window.matchMedia("(prefers-color-scheme: dark)").matches;
    var cycle = osDark ? ["system", "light", "dark"] : ["system", "dark", "light"];
    return cycle[(cycle.indexOf(cur) + 1) % cycle.length];
  }

  $("theme").addEventListener("click", function () {
    var cur = document.documentElement.getAttribute("data-theme") || "system";
    applyTheme(nextTheme(cur));
  });
  applyTheme(document.documentElement.getAttribute("data-theme") || "system");

  // ---------- boot ----------
  if (LIVE) {
    $("interval").disabled = false;
    refresh();
    scheduleRefresh();
  } else {
    $("interval").disabled = true;
    $("refresh").disabled = true;
    $("footHint").innerHTML = "Static snapshot — re-run <span class=\"mono\">repodash.py export</span> to update.";
    render();
  }
})();
