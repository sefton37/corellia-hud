/* Corellia HUD page. A dumb renderer: the aggregator decides every status; this file never
   derives a colour from raw numbers. The one exception is the aggregator being unreachable, where
   there is nothing to render, so lamps are set to grey / NO DATA. No animation loop: it repaints
   once per poll (>= 10 s). */
(function () {
  "use strict";
  var POLL_MS = 15000;
  var MIN_POLL_MS = 10000;
  var prev = null;
  var timer = null;

  // ---- 5x7 dot-matrix glyphs (rows of columns; "1" = lit) -----------------------------
  var G = {
    "0": ["01110", "10001", "10011", "10101", "11001", "10001", "01110"],
    "1": ["00100", "01100", "00100", "00100", "00100", "00100", "01110"],
    "2": ["01110", "10001", "00001", "00010", "00100", "01000", "11111"],
    "3": ["11110", "00001", "00001", "01110", "00001", "00001", "11110"],
    "4": ["00010", "00110", "01010", "10010", "11111", "00010", "00010"],
    "5": ["11111", "10000", "11110", "00001", "00001", "10001", "01110"],
    "6": ["00110", "01000", "10000", "11110", "10001", "10001", "01110"],
    "7": ["11111", "00001", "00010", "00100", "01000", "01000", "01000"],
    "8": ["01110", "10001", "10001", "01110", "10001", "10001", "01110"],
    "9": ["01110", "10001", "10001", "01111", "00001", "00010", "01100"],
    "%": ["11001", "11010", "00010", "00100", "01000", "01011", "10011"],
    ".": ["0", "0", "0", "0", "0", "1", "1"],
    ",": ["00", "00", "00", "00", "00", "01", "10"],
    ":": ["0", "1", "1", "0", "1", "1", "0"],
    "-": ["00000", "00000", "00000", "11111", "00000", "00000", "00000"],
    "+": ["00000", "00100", "00100", "11111", "00100", "00100", "00000"],
    "/": ["00001", "00010", "00010", "00100", "01000", "01000", "10000"],
    "D": ["11110", "10001", "10001", "10001", "10001", "10001", "11110"],
    "H": ["10001", "10001", "10001", "11111", "10001", "10001", "10001"],
    "M": ["10001", "11011", "10101", "10101", "10001", "10001", "10001"],
    "K": ["10001", "10010", "10100", "11000", "10100", "10010", "10001"],
    "G": ["01110", "10001", "10000", "10111", "10001", "10001", "01111"],
    "B": ["11110", "10001", "10001", "11110", "10001", "10001", "11110"],
    " ": ["00", "00", "00", "00", "00", "00", "00"]
  };

  function esc(s) {
    return String(s == null ? "" : s).replace(/[&<>"']/g, function (c) {
      return { "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c];
    });
  }

  // One SVG per numeral: three <path>s (unlit grid, lit dots, soft halo) so the DOM stays tiny.
  function led(text, pitch, level) {
    text = String(text);
    var r = pitch * 0.36, cols = 0, off = "", on = "", halo = "", ch, g, x0, row, c, cx, cy;
    function dot(cx, cy, rad) {
      return "M" + (cx - rad).toFixed(2) + " " + cy.toFixed(2) + "a" + rad.toFixed(2) + " " + rad.toFixed(2) +
        " 0 1 0 " + (rad * 2).toFixed(2) + " 0a" + rad.toFixed(2) + " " + rad.toFixed(2) + " 0 1 0 " +
        (-rad * 2).toFixed(2) + " 0z";
    }
    for (var i = 0; i < text.length; i++) {
      ch = text.charAt(i);
      g = G[ch] || G[" "];
      x0 = cols;
      for (row = 0; row < 7; row++) {
        for (c = 0; c < g[row].length; c++) {
          cx = (x0 + c + 0.5) * pitch;
          cy = (row + 0.5) * pitch;
          if (g[row].charAt(c) === "1") {
            on += dot(cx, cy, r);
            halo += dot(cx, cy, r * 1.75);
          } else if (ch !== " ") {
            off += dot(cx, cy, r);
          }
        }
      }
      cols += g[0].length + 1;
    }
    var w = Math.max(0, cols - 1) * pitch, h = 7 * pitch;
    return '<span class="led ' + (level || "green") + '" data-v="' + esc(text) + '">' +
      '<svg aria-hidden="true" width="' + w + '" height="' + h + '" viewBox="0 0 ' + w + " " + h + '">' +
      '<path class="off" d="' + off + '"/><path class="halo" d="' + halo + '"/><path class="on" d="' + on + '"/></svg>' +
      '<span class="sr">' + esc(text) + "</span></span>";
  }

  function num(n) { return n == null ? "-" : Number(n).toLocaleString("en-US"); }
  function pct(n) { return n == null ? "-" : Math.round(n) + "%"; }
  function uptime(s) {
    if (s == null) { return "-"; }
    var d = Math.floor(s / 86400), h = Math.floor((s % 86400) / 3600);
    return d > 0 ? d + "D " + h + "H" : h + "H " + Math.floor((s % 3600) / 60) + "M";
  }

  // Every stat is a link to its detail window. The launcher turns target="corellia-hud-detail" into a
  // separate window; anchors (not listeners) so the markup survives the every-poll repaint and is keyboard-reachable.
  // No rel="noopener": a noopener link opens a NEW browser window on every click, which would hide the
  // single-window re-target rule from the browser e2e; the launcher never creates a window.opener anyway.
  function href(kind, id) {
    return "/detail.html?kind=" + encodeURIComponent(kind) + "&id=" + encodeURIComponent(id);
  }
  function hit(kind, id, cls, inner, attrs) {
    return '<a class="' + cls + ' hit" href="' + esc(href(kind, id)) + '" target="corellia-hud-detail"' +
      (attrs || "") + ">" + inner + "</a>";
  }

  function lamp(status) { return '<span class="lamp ' + esc(status) + '"></span>'; }
  function panel(cls, title, body, tr) {
    return '<div class="panel ' + cls + '">' + (title ? "<h2>" + esc(title) + "</h2>" : "") +
      (tr ? '<div class="tr">' + tr + "</div>" : "") + body + "</div>";
  }

  // ---- sections ----------------------------------------------------------------------------
  function renderStatus(s, offline) {
    var m = s.meta, html = "";
    if (offline) {
      return panel("warn", "Aggregator", '<p class="sub"><b>AGGREGATOR UNREACHABLE</b>. Showing the last known names with every lamp set to no data.</p>');
    }
    if (m.banner) {
      html += panel("warn", "Heads up", '<p class="sub">' + esc(m.banner) + "</p>");
    }
    if (s.alerts.length) {
      var items = s.alerts.map(function (a) {
        var st = a.severity === "critical" ? "red" : "amber";
        return hit("alert", a.name, "row " + st, lamp(st) + '<span class="nm">' + esc(a.name) + '</span><span></span><span class="tag ' + st +
          '">' + (st === "red" ? "CRIT" : "WARN") + '</span><div class="why">' + esc(a.summary) + "</div>");
      }).join("");
      html += panel("alert", "Needs attention", items,
        '<span class="chips">' + hit("alert", s.alerts[0].name, "chip", "<i>" + lamp("amber") + s.alerts.length + "</i>", ' aria-label="first alert"') + "</span>");
    } else if (m.all_clear) {
      var c = m.counts;
      html += hit("services", "green", "panel clear", lamp("green") + "<b>All clear</b><span>" + c.green + " up" +
        (c.grey ? ", " + c.grey + " no data" : "") + ", no alerts</span>");
    }
    return html;
  }

  // Compact panel (Spec #492): one summary line, then only the services that are not up (worst first, capped by the
  // aggregator), then "+N more". The decision and its lamp level come from meta.services_summary; nothing here picks a colour.
  var SUM_TEXT = {
    all_up: function (u) { return "<b>" + u.up + " up</b> all services"; },
    problems: function (u) { return "<b>" + u.up + " up</b>, " + u.not_up + " not up"; },
    unavailable: function (u) { return "<b>NO DATA</b>: Prometheus unreachable, " + u.total + " services unknown"; },
    stale: function (u) { return "<b>STALE</b>: collector silent, " + u.total + " services unconfirmed"; },
    no_registry: function () { return "<b>NO DATA</b>: registry unavailable"; },
    empty: function () { return "No services listed"; }
  };
  function svcRow(r) {
    return hit("service", r.name, "row svc " + r.status, lamp(r.status) + '<span class="nm">' + esc(r.name) +
      '</span><span class="rs">' + esc(r.reason) + '</span><span class="tag ' + esc(r.status) + '">' + esc(r.label) + "</span>",
      ' data-name="' + esc(r.name) + '"');
  }
  function renderServices(s, offline) {
    var u = s.meta.services_summary, body, chips = "";
    if (offline) {
      body = '<p class="svc-off"><span class="lamp grey"></span><span><b>NO DATA</b>: aggregator unreachable, ' + s.services.length + " services unknown</span></p>";
    } else if (!u || !SUM_TEXT[u.state]) {
      body = '<p class="svc-off"><span class="lamp grey"></span><span><b>NO DATA</b>: no service summary from the aggregator</span></p>';
    } else {
      var byName = {};
      s.services.forEach(function (r) { byName[r.name] = r; });
      body = hit("services", "all", "svc-sum", lamp(u.level) + "<span>" + SUM_TEXT[u.state](u) + "</span>",
        ' data-state="' + esc(u.state) + '"') +
        u.listed.filter(function (n) { return byName[n]; }).map(function (n) { return svcRow(byName[n]); }).join("") +
        (u.more > 0 ? hit("services", "all", "svc-more", "+" + u.more + " more") : "");
      function chip(status, n) { return hit("services", status, "chip", "<i>" + lamp(status) + n + "</i>"); }
      var c = s.meta.counts;
      chips = '<span class="chips">' + (c.green ? chip("green", c.green) : "") + (c.amber ? chip("amber", c.amber) : "") +
        (c.red ? chip("red", c.red) : "") + (c.grey ? chip("grey", c.grey) : "") + "</span>";
    }
    return panel("", "Services", body, chips);
  }

  // ---- games: one row per game server; every row is a link to its detail window (kind=game) ---------------
  var PLAYER_NOTE = { unavailable: "NO DATA", unknown: "players unknown", stale: "stale: Flint has not polled lately" };
  function gb(b) { return b == null ? "-" : (b / 1073741824).toFixed(1) + "G"; }
  function gameStat(key, label, text) {
    return '<span data-f="' + key + '"><i>' + label + "</i> " + esc(text) + "</span>";
  }
  function gameRow(r, offline) {
    var st = offline ? "grey" : r.status, label = offline ? "NO DATA" : r.label, p = r.players || {};
    var shown = p.state === "ok" && p.count != null ? p.count + "/" + (p.max == null ? "-" : p.max) : "-";
    var why = offline ? "aggregator unreachable" : (st !== "green" ? r.reason : "");
    if (!offline && p.state !== "ok") { why += (why ? ". " : "") + (PLAYER_NOTE[p.state] || p.state); }
    var title = r.title;   // the world name lives in the tooltip and the detail window: two columns leave no room for it
    return '<a class="row grow ' + esc(st) + ' hit" href="' + esc("/detail.html?kind=game&id=" + encodeURIComponent(r.name)) +
      '" target="corellia-hud-detail" data-game="' + esc(r.name) + '" title="' + esc(r.title + (r.world ? " / " + r.world : "") + ": " + (offline ? "no data" : r.reason)) + '">' + lamp(st) + '<span class="nm">' + esc(title) +
      '</span><span class="pl" data-f="players">' + led(shown, 2.2, offline || p.state !== "ok" ? "grey" : "green") + '</span><span class="tag ' + st + '">' + esc(label) + "</span>" +
      '<div class="gs">' + gameStat("uptime", "UP", uptime(r.uptime_s)) + gameStat("ram", "RAM", gb(r.ram_bytes)) +
      gameStat("restarts", "RST24", r.restarts_24h == null ? "-" : String(r.restarts_24h)) + "</div>" +
      (why ? '<div class="why">' + esc(why) + "</div>" : "") + "</a>";
  }
  function renderGames(s, offline) {
    var g = s.games;
    if (!g) { return ""; }
    // Two columns, like the healthy services: four game rows in two grid rows keep the column inside the work area.
    var body = '<div class="rows grows">' + g.servers.map(function (r) { return gameRow(r, offline); }).join("") + "</div>";
    if (!g.servers.length) { body = '<p class="sub">No game servers listed.</p>'; }
    return panel("", "Games", body);
  }

  function spark(vals, level, max) {
    var W = 100, H = 18;
    if (!vals || vals.length < 2) { return '<svg class="spark" viewBox="0 0 ' + W + " " + H + '"><line class="base" x1="0" y1="' + (H - 3) + '" x2="' + W + '" y2="' + (H - 3) + '"/></svg>'; }
    var hi = Math.max(max || 0, Math.max.apply(null, vals)), lo = 0, pts = vals.map(function (v, i) {
      return [(i / (vals.length - 1)) * (W - 6) + 2, H - 4 - ((v - lo) / ((hi - lo) || 1)) * (H - 9)];
    }), d = pts.map(function (p, i) { return (i ? "L" : "M") + p[0].toFixed(1) + " " + p[1].toFixed(1); }).join(""), last = pts[pts.length - 1];
    return '<svg class="spark" viewBox="0 0 ' + W + " " + H + '" preserveAspectRatio="none"><line class="base" x1="0" y1="' + (H - 3) +
      '" x2="' + W + '" y2="' + (H - 3) + '"/><path class="shadow" transform="translate(1.2 1.2)" d="' + d + '"/><path class="line" d="' + d +
      '"/><circle class="end" cx="' + last[0].toFixed(1) + '" cy="' + last[1].toFixed(1) + '" r="2.6"/></svg>';
  }
  function meter(p, level) {
    var lit = p == null ? 0 : Math.round((p / 100) * 20), out = "";
    for (var i = 0; i < 20; i++) { out += "<b" + (i < lit ? ' class="on"' : "") + "></b>"; }
    return '<div class="meter ' + esc(level) + '">' + out + "</div>";
  }

  // ---- Claude usage: limits used (5H, WEEK) and tokens burned (TODAY, 7D, CACHE HIT); every figure links to kind=claude -------
  var LIMIT_NOTE = {
    pending: "FETCHING", disabled: "OFF", no_credentials: "NO CREDENTIALS", cred_insecure: "CREDENTIALS FILE TOO OPEN",
    cred_unreadable: "CREDENTIALS UNREADABLE", cred_malformed: "CREDENTIALS UNREADABLE", token_expired: "TOKEN EXPIRED",
    needs_reauth: "SIGN IN TO CLAUDE CODE", scope_missing: "SCOPE MISSING", rate_limited: "RATE LIMITED",
    http_error: "SERVICE ERROR", timeout: "TIMED OUT", unreachable: "UNREACHABLE", bad_json: "BAD REPLY",
    too_large: "BAD REPLY", schema_mismatch: "UNEXPECTED REPLY", unavailable: "NO DATA", offline: "NO DATA"
  };
  var WINDOW_NOTE = { none: "NO ACTIVE WINDOW", stale_window: "WINDOW ENDED", bad_value: "BAD VALUE" };
  function claudeHit(id, cls, inner, attrs) {
    return '<a class="' + cls + ' hit" href="' + esc("/detail.html?kind=claude&id=" + encodeURIComponent(id)) +
      '" target="corellia-hud-detail"' + (attrs || "") + ">" + inner + "</a>";
  }
  function dur(s) {
    if (s == null) { return "-"; }
    var d = Math.floor(s / 86400), h = Math.floor((s % 86400) / 3600), m = Math.floor((s % 3600) / 60);
    return d > 0 ? d + "D " + h + "H" : h > 0 ? h + "H " + m + "M" : m + "M";
  }
  function tok(n) {
    if (n == null) { return "-"; }
    if (n < 1000) { return String(n); }
    if (n < 1e6) { return (n / 1e3).toFixed(n < 1e4 ? 1 : 0) + "K"; }
    if (n < 1e9) { return (n / 1e6).toFixed(n < 1e7 ? 1 : 0) + "M"; }
    return (n / 1e9).toFixed(n < 1e10 ? 1 : 0) + "B";
  }
  function limitRow(label, key, L, offline) {
    var state = offline ? "offline" : (L && L.state) || "unavailable", w = L && L.windows ? L.windows[key] : null;
    var live = !offline && (state === "ok" || state === "stale") && w;
    var shown = live && w.state === "ok";
    var level = shown ? w.level : "grey";
    var note = shown ? "" : (live ? (WINDOW_NOTE[w.state] || w.state) : (LIMIT_NOTE[state] || state)).toUpperCase();
    var left = shown ? '<span class="cl-left" data-f="' + key + '-left">' + Math.round(w.left_pct) + "% left</span>" : "";
    var reset = shown ? '<span class="cl-reset" data-f="' + key + '-reset">' + (w.resets_in_s != null ? "resets " + dur(w.resets_in_s) : "reset unknown") + "</span>" : "";
    var age = live && state === "stale" ? '<span class="cl-age" data-f="' + key + '-age">stale ' + dur(L.age_s) + "</span>" : "";
    var val = shown ? led(Math.round(w.pct) + "%", 2.6, level) : '<span class="cl-note ' + level + '" data-f="' + key + '-note">' + esc(note) + "</span>";
    return claudeHit("limits", "cl-row", '<span class="cl-k">' + label + '</span><span class="cl-v" data-f="' + key + '-pct">' + val + "</span>" +
      '<span class="cl-m">' + left + reset + age + "</span>" + meter(shown ? w.pct : null, level),
      ' data-claude-fig="' + key + '" data-level="' + esc(level) + '"');
  }
  function figure(id, key, label, text, note) {
    return claudeHit(id, "cl-fig", '<div class="k">' + label + (note ? "<em>" + esc(note) + "</em>" : "") + '</div><div class="cl-n" data-f="' + key + '">' + text + "</div>",
      ' data-claude-fig="' + key + '"');
  }
  function renderClaude(s, offline) {
    var c = s.claude;
    if (!c) { return ""; }
    var U = c.usage || {}, W = U.windows, nodata = offline || !W;
    function n(v) { return nodata ? '<span class="cl-note grey">NO DATA</span>' : led(tok(v), 2.8, "cream"); }
    var hit7 = !nodata && U.cache_hit_rate != null ? Math.round(U.cache_hit_rate * 100) + "%" : null;
    var strip = '<div class="cl-figs">' + figure("usage", "today", "TODAY", n(nodata ? null : W.today.new)) +
      figure("usage", "d7", "7D", n(nodata ? null : W.d7.new), !nodata && W.d7.partial ? "partial" : "") +
      figure("usage", "cache", "CACHE HIT", nodata ? '<span class="cl-note grey">NO DATA</span>' : (hit7 ? led(hit7, 2.8, "cream") : '<span class="cl-note grey">NONE YET</span>')) + "</div>";
    var models = nodata ? "" : (U.models || []).slice(0, 3).map(function (m) {
      return esc(String(m.model).replace(/^claude-/, "")) + " " + (m.share == null ? "-" : Math.round(m.share * 100) + "%");
    }).join("   ");
    var mline = claudeHit("models", "cl-models", models ? models : (nodata ? "" : "no model data"), ' data-claude-fig="models"');
    return panel("claude", "Claude", limitRow("5H", "five_hour", c.limits, offline) + limitRow("WEEK", "seven_day", c.limits, offline) +
      strip + (nodata ? "" : mline));
  }

  function tile(key, label, g, opt) {
    var level = g.level, shown = g.pct == null ? "-" : pct(g.pct);
    return hit("perf", key, "tile", '<div class="k">' + label + (opt.note ? "<em>" + esc(opt.note) + "</em>" : "") + "</div>" +
      '<div class="v">' + led(shown, 3.6, level) + "</div>" + (opt.meter ? meter(g.pct, level) : spark(g.spark, level, 12)),
      ' data-k="' + key + '"');
  }

  function renderPerf(s) {
    var p = s.perf, gb = function (b) { return b == null ? "-" : (b / 1073741824).toFixed(1); };
    var load = hit("perf", "load", "tile", '<div class="k">LOAD<em>1 min</em></div><div class="v">' +
      led(p.load.value == null ? "-" : String(Math.round(p.load.value * 10) / 10), 3.6, p.load.level) + "</div>" + spark(p.load.spark, p.load.level, 8),
      ' data-k="load"');
    var body = '<div class="grid">' + tile("cpu", "CPU", p.cpu, {}) + tile("mem", "MEMORY", p.mem, {}) +
      tile("gpu", "GPU", p.gpu, {}) + tile("vram", "VRAM", p.vram, { meter: true, note: gb(p.vram.used_bytes) + "/" + gb(p.vram.total_bytes) + "G" }) +
      tile("disk", "DISK /", p.disk, { meter: true }) + load + "</div>";
    return panel("", "Machine", body, hit("perf", "uptime", "up", "up " + esc(uptime(p.uptime_s))));
  }

  function delta(d) {
    if (!d) { return ""; }
    if (d.label) { return '<span class="delta ' + (d.label === "new" ? "new" : "flat") + '">' + esc(d.label) + "</span>"; }
    var cls = d.pct > 0 ? "up" : d.pct < 0 ? "down" : "flat";
    return '<span class="delta ' + cls + '">' + (d.pct > 0 ? "+" : "") + d.pct + "%</span>";
  }

  function renderBeacon(b, host) {
    if (b.state === "no_beacon") { return hit("site", host, "metric", '<div class="k">VISITORS</div><span class="note">no beacon</span>'); }
    if (b.state !== "ok") { return hit("site", host, "metric", '<div class="k">VISITORS</div><span class="note err">' + esc(b.state) + "</span>"); }
    return hit("site", host, "metric", '<div class="k">VISITORS 24H</div><div class="big" data-m="visitors-24h">' + led(num(b.visitors_24h), 4, "green") +
      delta(b.delta_24h) + '</div><div class="small" data-m="visitors-7d">7d ' + led(num(b.visitors_7d), 2, "cream") + delta(b.delta_7d) + "</div>");
  }

  function renderCf(c, host) {
    function line(inner) { return hit("site", host, "cfline", inner); }
    if (c.state === "token_needed") { return line('<span class="k">CLOUDFLARE</span><span class="note">token needed</span>'); }
    if (c.state === "token_insecure") { return line('<span class="k">CLOUDFLARE</span><span class="note err">token file too open: chmod 600</span>'); }
    if (c.state !== "ok") { return line('<span class="k">CLOUDFLARE</span><span class="note err">' + esc(c.state === "cf_error" ? "api error" : c.state) + "</span>"); }
    return line('<span class="k">CLOUDFLARE</span><span data-m="cf-requests-24h">' + led(num(c.requests_24h), 3, "cream") + "</span>" +
      delta(c.delta_24h) + '<span class="k">THREATS</span><span data-m="cf-threats-24h">' + led(num(c.threats_24h), 3, c.threats_24h > 0 ? "amber" : "cream") + "</span>");
  }

  // The chat tile: counts and metadata only. Conversation text lives behind the click, in the detail window.
  function renderChat(c, host) {
    if (!c) { return ""; }
    if (c.state !== "ok") {
      return hit("chat", host, "chat", '<div class="k">PORTFOLIO CHAT</div><span class="note err">' + esc(c.state) + "</span>");
    }
    function cell(key, label, value, cls) {
      return '<div class="cm"><div class="ck">' + label + '</div><span data-m="chat-' + key + '">' + led(value, 3, cls || "cream") + "</span></div>";
    }
    var avg = c.avg_response_ms == null ? "-" : (c.avg_response_ms / 1000).toFixed(1);
    var f = c.funnel || {}, k = c.contacts || {};
    var consent = f.state !== "ok" ? '<span class="note err">' + esc(f.state) + "</span>" :
      (f.recorded ? '<span data-m="chat-consents">' + led(num(f.consents), 3, "cream") + "</span>" : '<span class="note">none recorded</span>');
    var contacts = k.state === "ok" ? '<span data-m="chat-contacts">' + led(num(k.accepted), 3, "cream") + "</span>" +
      '<span class="cn">+ ' + num(k.quarantined) + " held</span>" : '<span class="note err">' + esc(k.state || "no data") + "</span>";
    return hit("chat", host, "chat",
      '<div class="k">PORTFOLIO CHAT</div><div class="cgrid">' +
      cell("24h", "CONV 24H", num(c.conversations_24h)) + cell("7d", "CONV 7D", num(c.conversations_7d)) +
      cell("turns", "TURNS 7D", num(c.turns_7d)) + cell("blocked", "BLOCKED 7D", num(c.blocked_7d), c.blocked_7d > 0 ? "amber" : "cream") +
      cell("avg", "AVG RESP S", avg) + cell("opens", "OPENS 7D", f.state === "ok" ? num(f.opens) : "-") + "</div>" +
      '<div class="cline"><span class="ck">CONSENTS</span>' + consent + '<span class="ck">CONTACTS</span>' + contacts + "</div>");
  }

  function renderWeb(s) {
    var body = s.web.sites.map(function (site) {
      return '<div class="site" data-host="' + esc(site.host) + '"><h3>' + esc(site.host) + '</h3><div class="cols">' +
        renderBeacon(site.beacon, site.host) + "</div>" + renderCf(site.cf, site.host) + renderChat(site.chat, site.host) + "</div>";
    }).join("");
    return panel("", "Websites", body);
  }

  function renderFoot(s, offline) {
    var src = s.meta.sources, t = new Date(s.meta.generated_at * 1000);
    var parts = Object.keys(src).map(function (k) { return k + " <b>" + esc(src[k]) + "</b>"; });
    return (offline ? "last good " : "updated ") + t.toLocaleTimeString("en-GB") + "<br>" + parts.join(" / ");
  }

  function clock(s) {
    var t = new Date(s.meta.generated_at * 1000), p = function (n) { return (n < 10 ? "0" : "") + n; };
    return led(p(t.getHours()) + ":" + p(t.getMinutes()), 4, "green");
  }

  function render(s, offline) {
    document.documentElement.classList.toggle("offline", !!offline);
    document.getElementById("status").innerHTML = renderStatus(s, offline);
    document.getElementById("services").innerHTML = renderServices(s, offline);
    document.getElementById("games").innerHTML = renderGames(s, offline);
    document.getElementById("claude").innerHTML = renderClaude(s, offline);
    document.getElementById("perf").innerHTML = renderPerf(s);
    document.getElementById("web").innerHTML = renderWeb(s);
    document.getElementById("foot").innerHTML = renderFoot(s, offline);
    document.getElementById("clock").innerHTML = clock(s);
    document.getElementById("sub").textContent = offline ? "aggregator offline" : "desktop HUD";
    document.body.setAttribute("data-ready", offline ? "offline" : "live");
  }

  function renderNothing() {
    document.getElementById("status").innerHTML = panel("warn", "Aggregator", '<p class="sub"><b>AGGREGATOR UNREACHABLE</b>. No data yet.</p>');
    document.body.setAttribute("data-ready", "offline");
  }

  function poll() {
    clearTimeout(timer);
    fetch("/api/state", { cache: "no-store" }).then(function (r) {
      if (!r.ok) { throw new Error("http " + r.status); }
      return r.json();
    }).then(function (s) {
      prev = s;
      render(s, false);
    }).catch(function () {
      if (prev) { render(prev, true); } else { renderNothing(); }
    }).then(function () {
      timer = setTimeout(poll, Math.max(MIN_POLL_MS, POLL_MS));
    });
  }

  if (/[?&]preview=1/.test(location.search)) { document.documentElement.classList.add("preview"); }
  document.addEventListener("hud:refresh", poll);
  document.getElementById("clock").addEventListener("click", poll);
  poll();
}());
