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
    lastError: null
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
        title: "Repos with commits ahead of their upstream" },
      { k: "Behind", v: s.behind, status: "info", filter: null,
        title: "Repos behind their upstream" },
      { k: "Clean & synced", v: s.counts.good, status: "good", filter: "good",
        title: "Nothing outstanding" },
      { k: "Repos tracked", v: s.repo_count, status: null, filter: null,
        title: "Git repos found directly under the root" }
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
    if (r.upstream === null || r.upstream === undefined)
      return '<span style="color:var(--serious)">untracked</span>';
    var a = r.ahead || 0, b = r.behind || 0;
    if (!a && !b) return '<span style="color:var(--good)">✓ synced</span>';
    var parts = [];
    if (a) parts.push('<b style="color:var(--warning)">↑' + a + '</b>');
    if (b) parts.push('<b style="color:var(--ink-2)">↓' + b + '</b>');
    return '<span class="counts">' + parts.join(" ") + '</span>';
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

    if (r.errors && r.errors.length) {
      cols.push('<div><h4>Errors</h4><ul>' + r.errors.map(function (e) {
        return '<li style="color:var(--critical)">' + esc(e) + '</li>';
      }).join("") + '</ul></div>');
    }

    return '<div class="detailbox">' + cols.join("") + '</div>';
  }

  // Commands are shown, never run. Read before pasting.
  function suggestions(r) {
    var ids = (r.flags || []).map(function (f) { return f.id; });
    var p = r.path, out = [];
    if (ids.indexOf("stale-lock") !== -1) out.push("rm " + p + "/.git/index.lock");
    if (ids.indexOf("conflicts") !== -1) out.push("git -C " + p + " status");
    if (ids.indexOf("operation") !== -1) out.push("git -C " + p + " status   # then --continue or --abort");
    if (ids.indexOf("detached") !== -1) out.push("git -C " + p + " switch -c rescue/" + r.name);
    if (ids.indexOf("uncommitted") !== -1) out.push("git -C " + p + " add -A && git -C " + p + " commit");
    if (ids.indexOf("unpushed") !== -1) out.push("git -C " + p + " push");
    if (ids.indexOf("on-agent-branch") !== -1)
      out.push("git -C " + p + " log --oneline main.." + (r.branch || "HEAD") +
               "   # review, then merge or abandon");
    if (ids.indexOf("agent-branch-buildup") !== -1)
      out.push("git -C " + p + " branch --list 'ccode/*' 'claude/*' 'codex/*'" +
               "   # review before deleting anything");
    if (ids.indexOf("agent-scaffolding") !== -1)
      out.push("echo '.claude/' >> " + p + "/.gitignore");
    if (ids.indexOf("diverged") !== -1) out.push("git -C " + p + " pull --rebase   # review before pushing");
    if (ids.indexOf("behind") !== -1) out.push("git -C " + p + " pull --ff-only");
    if (ids.indexOf("no-upstream") !== -1)
      out.push("git -C " + p + " push -u origin " + (r.branch || "HEAD"));
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
      var open = state.expanded.has(r.name);
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
        '<tr class="repo' + (open ? " open" : "") + '" data-name="' + esc(r.name) +
          '" tabindex="0" aria-expanded="' + open + '">' +
          '<td style="color:var(--ink-3)">' + (open ? "▾" : "▸") + '</td>' +
          '<td>' + statusCell(r) + '</td>' +
          '<td><div class="name">' + nameCell + '</div>' +
              '<div class="headline">' + esc(r.headline) + '</div></td>' +
          '<td><span class="branch">' + esc(r.branch || "—") + branchWarn + '</span></td>' +
          '<td class="num">' + pushCell(r) + '</td>' +
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
    if (!state.data) return;
    var d = state.data;
    $("rootpath").textContent = d.root;
    $("scanTime").textContent = "scanned " + clock(d.generated_at) +
      " (" + d.scan_seconds + "s)";
    $("footScan").textContent = d.summary.repo_count + " git repos, " +
      d.summary.non_git_count + " other dirs · scan took " + d.scan_seconds + "s";
    var badge = $("modeBadge");
    badge.className = "mode-badge " + (LIVE ? "live" : "snapshot") + (state.busy ? " busy" : "");
    $("modeText").textContent = state.lastError ? "error"
      : LIVE ? (state.busy ? "refreshing" : "live") : "snapshot";
    if (state.lastError) badge.title = state.lastError;

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

  // ---------- events ----------
  document.addEventListener("click", function (e) {
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
      var n = row.getAttribute("data-name");
      if (state.expanded.has(n)) state.expanded.delete(n); else state.expanded.add(n);
      renderRows();
    }
  });

  document.addEventListener("keydown", function (e) {
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
  $("interval").addEventListener("change", scheduleRefresh);
  $("expandAll").addEventListener("click", function () {
    var vis = visibleRepos();
    if (state.expanded.size >= vis.length && vis.length) state.expanded.clear();
    else vis.forEach(function (r) { state.expanded.add(r.name); });
    renderRows();
  });
  $("theme").addEventListener("click", function () {
    var cur = document.documentElement.getAttribute("data-theme");
    var next = cur === "dark" ? "light" : cur === "light" ? "dark"
      : (window.matchMedia("(prefers-color-scheme: dark)").matches ? "light" : "dark");
    document.documentElement.setAttribute("data-theme", next);
  });

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
