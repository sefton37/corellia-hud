/* Corellia detail window. Fetches /api/detail/<kind>?id=<id> once on open (and again on R / the refresh
   mark) and renders it with DOM construction and textContent only. Visitor messages, referrers, paths and
   journal lines are untrusted: nothing in this file ever turns a string into markup. No timers, no
   animation loop. */
(function () {
  "use strict";
  var SVGNS = "http://www.w3.org/2000/svg";
  var KIND_LABEL = { service: "service", services: "services", perf: "machine", site: "website", chat: "portfolio chat", alert: "alert", game: "game server", claude: "claude usage" };
  var PERF_LABEL = { cpu: "CPU", mem: "Memory", gpu: "GPU", vram: "VRAM", disk: "Disk /", load: "Load (1 min)", uptime: "Uptime" };
  var q = new URLSearchParams(location.search);
  var kind = q.get("kind") || "";
  var ident = q.get("id") || "";
  var body = document.getElementById("body");

  // ---- 5x7 dot-matrix glyphs (same table as the HUD column) ---------------------------------
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

  // ---- DOM helpers (textContent and setAttribute only) ----------------------------------------
  function el(tag, cls, text) {
    var n = document.createElement(tag);
    if (cls) { n.className = cls; }
    if (text != null) { n.textContent = String(text); }
    return n;
  }
  function add(parent) {
    for (var i = 1; i < arguments.length; i++) { if (arguments[i]) { parent.appendChild(arguments[i]); } }
    return parent;
  }
  function sv(tag, attrs, text) {
    var n = document.createElementNS(SVGNS, tag);
    Object.keys(attrs || {}).forEach(function (k) { n.setAttribute(k, String(attrs[k])); });
    if (text != null) { n.textContent = String(text); }
    return n;
  }
  function dot(cx, cy, rad) {
    return "M" + (cx - rad).toFixed(2) + " " + cy.toFixed(2) + "a" + rad.toFixed(2) + " " + rad.toFixed(2) +
      " 0 1 0 " + (rad * 2).toFixed(2) + " 0a" + rad.toFixed(2) + " " + rad.toFixed(2) + " 0 1 0 " + (-rad * 2).toFixed(2) + " 0z";
  }
  // One SVG per numeral: unlit grid, lit dots, soft halo. The text is also kept for screen readers.
  function led(text, pitch, level) {
    text = String(text);
    var r = pitch * 0.36, cols = 0, off = "", on = "", halo = "", i, row, c, g, ch, x0, cx, cy;
    for (i = 0; i < text.length; i++) {
      ch = text.charAt(i);
      g = G[ch] || G[" "];
      x0 = cols;
      for (row = 0; row < 7; row++) {
        for (c = 0; c < g[row].length; c++) {
          cx = (x0 + c + 0.5) * pitch;
          cy = (row + 0.5) * pitch;
          if (g[row].charAt(c) === "1") { on += dot(cx, cy, r); halo += dot(cx, cy, r * 1.75); }
          else if (ch !== " ") { off += dot(cx, cy, r); }
        }
      }
      cols += g[0].length + 1;
    }
    var w = Math.max(0, cols - 1) * pitch, h = 7 * pitch;
    var wrap = el("span", "led " + (level || "green"));
    wrap.setAttribute("data-v", text);
    var s = sv("svg", { "aria-hidden": "true", width: w, height: h, viewBox: "0 0 " + w + " " + h });
    add(s, sv("path", { "class": "off", d: off }), sv("path", { "class": "halo", d: halo }), sv("path", { "class": "on", d: on }));
    return add(wrap, s, el("span", "sr", text));
  }

  function num(n) { return n == null ? "-" : Number(n).toLocaleString("en-US"); }
  function fix1(n) { return n == null ? "-" : Number(n).toFixed(1); }
  function when(epoch) { return epoch == null ? "-" : new Date(epoch * 1000).toLocaleString("en-GB"); }
  function isoWhen(text) { var t = Date.parse(text); return isNaN(t) ? String(text || "-") : new Date(t).toLocaleString("en-GB"); }
  function span(secs) {
    if (secs == null) { return "-"; }
    var d = Math.floor(secs / 86400), h = Math.floor((secs % 86400) / 3600), m = Math.floor((secs % 3600) / 60);
    return d > 0 ? d + "D " + h + "H" : h + "H " + (m < 10 ? "0" : "") + m + "M";
  }
  function lamp(status, big) { return el("span", "lamp " + status + (big ? " big" : "")); }
  function tag(status, label, big) { return el("span", "tag " + status + (big ? " big" : ""), label); }

  function panel(title, cls, right) {
    var p = el("div", "panel " + (cls || ""));
    if (title) { p.appendChild(el("h2", null, title)); }
    if (right) { p.appendChild(add(el("div", "tr"), right)); }
    return p;
  }
  function note(text, err) { return el("p", "dnote" + (err ? " err" : ""), text); }
  function fact(label, node, key) {
    var dd = el("dd");
    if (key) { dd.setAttribute("data-f", key); }
    if (typeof node === "string") { dd.textContent = node; } else if (node) { dd.appendChild(node); }
    return add(el("div"), el("dt", null, label), dd);
  }
  function table(heads, rows) {
    var t = el("table", "tbl"), hr = el("tr");
    heads.forEach(function (h) { hr.appendChild(el("th", null, h)); });
    add(t, add(el("thead"), hr));
    var tb = el("tbody");
    rows.forEach(function (r) {
      var tr = el("tr");
      r.forEach(function (cell, i) {
        var td = el("td", i > 0 ? "n" : null);
        if (cell instanceof Node) { td.appendChild(cell); } else { td.textContent = String(cell); }
        tr.appendChild(td);
      });
      tb.appendChild(tr);
    });
    return add(t, tb);
  }
  function link(kindName, id, text) {
    var a = el("a", null, text);
    a.setAttribute("href", "/detail.html?kind=" + encodeURIComponent(kindName) + "&id=" + encodeURIComponent(id));
    return a;
  }

  // ---- charts ---------------------------------------------------------------------------------
  function chart(series, opts) {
    var W = 520, H = 170, PL = 34, PR = 10, PT = 10, PB = 20;
    var hi = opts.max != null ? opts.max : Math.max.apply(null, series.concat([1])) * 1.1;
    var s = sv("svg", { "class": "chart", viewBox: "0 0 " + W + " " + H, role: "img", "aria-label": opts.label });
    function y(v) { return PT + (1 - Math.min(Math.max(v / hi, 0), 1)) * (H - PT - PB); }
    function x(i) { return PL + (i / Math.max(series.length - 1, 1)) * (W - PL - PR); }
    var th = opts.thresholds;
    if (th) {
      s.appendChild(sv("rect", { "class": "band-amber", x: PL, y: y(th.red), width: W - PL - PR, height: Math.max(0, y(th.amber) - y(th.red)) }));
      s.appendChild(sv("rect", { "class": "band-red", x: PL, y: y(hi), width: W - PL - PR, height: Math.max(0, y(th.red) - y(hi)) }));
    }
    [0, 0.5, 1].forEach(function (f) {
      s.appendChild(sv("line", { "class": "grid", x1: PL, x2: W - PR, y1: y(hi * f), y2: y(hi * f) }));
      s.appendChild(sv("text", { x: 2, y: y(hi * f) + 3 }, (hi * f).toFixed(hi >= 10 ? 0 : 1)));
    });
    s.appendChild(sv("text", { x: PL, y: H - 5 }, "-24h"));
    s.appendChild(sv("text", { x: W - PR - 20, y: H - 5 }, "now"));
    var d = series.map(function (v, i) { return (i ? "L" : "M") + x(i).toFixed(1) + " " + y(v).toFixed(1); }).join("");
    s.appendChild(sv("path", { "class": "shadow", transform: "translate(1.6 1.6)", d: d }));
    s.appendChild(sv("path", { "class": "line", d: d }));
    if (series.length) { s.appendChild(sv("circle", { "class": "end", cx: x(series.length - 1).toFixed(1), cy: y(series[series.length - 1]).toFixed(1), r: 4 })); }
    return s;
  }
  function bars(days) {
    var W = 520, H = 120, PL = 8, PB = 16, n = days.length, bw = (W - 2 * PL) / n;
    var hi = Math.max.apply(null, days.map(function (d) { return d.pageviews; }).concat([1]));
    var s = sv("svg", { "class": "chart", viewBox: "0 0 " + W + " " + H, role: "img", "aria-label": "pageviews per day, last 7 days" });
    days.forEach(function (d, i) {
      var h = (d.pageviews / hi) * (H - PB - 14);
      s.appendChild(sv("rect", { "class": "bar" + (i === n - 1 ? " today" : ""), x: PL + i * bw + 6, y: H - PB - h, width: bw - 12, height: Math.max(h, 1) }));
      s.appendChild(sv("text", { x: PL + i * bw + 6, y: H - 3 }, d.date.slice(5)));
      s.appendChild(sv("text", { x: PL + i * bw + 6, y: H - PB - h - 4 }, d.pageviews));
    });
    return s;
  }

  // ---- views ----------------------------------------------------------------------------------
  function viewService(d) {
    var head = add(el("div", "head"), lamp(d.status, true), el("span", "nm", ident), tag(d.status, d.label, true));
    if (d.reason) { head.appendChild(el("div", "why", d.reason)); }
    var p = add(panel(ident, ""), head);
    if (d.state !== "ok") { p.appendChild(note("Prometheus unreachable: unit figures unavailable.", true)); }
    var probe = { open: "port open", closed: "port closed", no_data: "no data", no_port: "no port probe" }[d.probe] || d.probe;
    var tri = function (v) { return v == null ? "no data" : v ? "yes" : "no"; };
    var kv = add(el("dl", "kv"),
      fact("SCOPE", d.scope || "unknown", "scope"),
      fact("PORT", d.port ? ":" + d.port + " - " + probe : probe, "port"),
      fact("UNIT", d.unit || "none", "unit"),
      fact("ACTIVE", tri(d.unit_active), "active"),
      fact("FAILED", tri(d.unit_failed), "failed"),
      fact("RESTARTS 30M", d.restarts_30m == null ? "no data" : led(String(Math.round(d.restarts_30m)), 3.4, "cream"), "restarts-30m"),
      fact("RESTARTS 24H", d.restarts_24h == null ? "no data" : led(String(Math.round(d.restarts_24h)), 3.4, "cream"), "restarts-24h"),
      fact("TIMER AGE", d.timer_age_s == null ? "n/a" : led(span(d.timer_age_s), 3.4, "cream"), "timer-age"));
    p.appendChild(el("div", "sub2", "STATUS"));
    p.appendChild(kv);
    var rel = panel("Related alerts", "");
    if (d.related_alerts.length) {
      d.related_alerts.forEach(function (a) {
        rel.appendChild(add(el("div", "head"), lamp(a.severity === "critical" ? "red" : "amber"), link("alert", a.name, a.name), el("div", "why", a.summary)));
      });
    } else { rel.appendChild(note("No firing alert mentions this service.")); }
    var jr = panel("Journal", "", tag(d.journal.state === "ok" ? "green" : "amber", d.journal.state.toUpperCase().replace(/_/g, " ")));
    var JMSG = { no_permission: "The journal could not be read (permission denied).", no_lines: "The journal holds no lines for this unit.",
      timeout: "journalctl did not answer in time.", unavailable: "journalctl is not available.", unit_invalid: "This unit name was refused.", no_unit: "This service has no systemd unit." };
    if (d.journal.state === "ok") {
      var log = el("div", "log");
      d.journal.lines.forEach(function (ln) { log.appendChild(el("div", null, ln)); });
      jr.appendChild(note("Last " + d.journal.lines.length + " lines."));
      jr.appendChild(log);
    } else { jr.appendChild(note(JMSG[d.journal.state] || d.journal.state, true)); }
    return [p, rel, jr];
  }

  // The summary states that are NOT a plain list of rows: the window says so instead of presenting grey rows as fact.
  var SUMMARY_NOTE = {
    unavailable: "NO DATA: Prometheus is unreachable, so no service status below is confirmed.",
    stale: "STALE: the collector has been silent, so no service status below is confirmed.",
    no_registry: "NO DATA: the service registry is unavailable.",
    empty: "No services are listed."
  };
  function serviceRow(r, status, grouped) {
    var row = el("div", "row " + status);
    add(row, lamp(status), add(el("span", "nm"), link("service", r.name, r.name)), el("span", "pt", r.port ? ":" + r.port : ""), tag(status, r.label));
    if (r.reason) { row.appendChild(el("div", "why", (grouped ? "" : r.group + " - ") + r.reason)); }
    return row;
  }
  function viewServices(d) {
    if (d.status === "all") {
      var all = panel("All services", "", el("span", "up", d.rows.length + " of " + d.total));
      var strip = el("span", "chips");
      ["green", "amber", "red", "grey"].forEach(function (st) {
        if (d.counts && d.counts[st]) { var chip = link("services", st, ""); chip.className = "chip"; strip.appendChild(add(chip, add(el("i"), lamp(st), document.createTextNode(String(d.counts[st]))))); }
      });
      all.appendChild(add(el("div", "pair"), strip));
      var u = d.summary || {};
      if (SUMMARY_NOTE[u.state]) { all.appendChild(note(SUMMARY_NOTE[u.state], true)); }
      var last = null;
      d.rows.forEach(function (r) {
        if (r.group !== last) { all.appendChild(el("div", "grp", r.group)); last = r.group; }
        all.appendChild(serviceRow(r, r.status, true));
      });
      if (!d.rows.length) { all.appendChild(note("No services to show.")); }
      return [all];
    }
    var p = panel(d.label + " services", "", el("span", "up", d.rows.length + " of " + d.total));
    if (!d.rows.length) { p.appendChild(note("No service has this status right now.")); return [p]; }
    d.rows.forEach(function (r) { p.appendChild(serviceRow(r, d.status)); });
    return [p];
  }

  function viewPerf(d) {
    var title = PERF_LABEL[ident] || ident;
    var p = panel(title, "");
    if (d.state === "unreachable") { p.appendChild(note("Prometheus unreachable: no series available.", true)); }
    if (ident === "uptime") {
      var x = d.extras || {};
      p.appendChild(add(el("dl", "kv"),
        fact("UPTIME", x.uptime_s == null ? "no data" : led(span(x.uptime_s), 3.6, "green"), "uptime"),
        fact("BOOT TIME", when(x.boot_time), "boot-time"),
        fact("REBOOTS, 30 D", x.reboots_30d == null ? "no data" : led(String(x.reboots_30d), 3.6, "cream"), "reboots")));
      return [p];
    }
    var lv = d.level || "grey";
    p.appendChild(add(el("dl", "kv"),
      fact("LAST", d.last == null ? "no data" : led(fix1(d.last), 3.6, lv), "last"), fact("MIN", d.min == null ? "no data" : led(fix1(d.min), 3.4, "cream"), "min"),
      fact("MAX", d.max == null ? "no data" : led(fix1(d.max), 3.4, "cream"), "max"), fact("AVG", d.avg == null ? "no data" : led(fix1(d.avg), 3.4, "cream"), "avg"),
      fact("P95", d.p95 == null ? "no data" : led(fix1(d.p95), 3.4, "cream"), "p95")));
    var out = [p];
    var c = panel("Last 24 hours", "");
    if (d.series.length >= 2) {
      c.appendChild(chart(d.series, { max: d.unit === "%" ? 100 : null, thresholds: d.thresholds, label: title + " over the last 24 hours" }));
      if (d.thresholds) { c.appendChild(note("Amber from " + d.thresholds.amber + "%, red from " + d.thresholds.red + "%. " + d.series.length + " samples, 5 min apart.")); }
    } else { c.appendChild(note(d.state === "unreachable" ? "No data: Prometheus is unreachable." : "No samples in the last 24 hours.", true)); }
    out.push(c);
    if (d.extras && d.extras.total_bytes != null) {
      var gib = function (b) { return b == null ? "-" : (b / 1073741824).toFixed(1) + " GiB"; };
      out.push(add(panel("Capacity", ""), add(el("dl", "kv"), fact("USED", gib(d.extras.used_bytes), "used"), fact("TOTAL", gib(d.extras.total_bytes), "total"))));
    }
    return out;
  }

  function viewSite(d) {
    var b = d.beacon, out = [], p = panel(ident, "");
    if (b.state !== "ok") {
      p.appendChild(note(b.state === "no_beacon" ? "This site has no analytics beacon." : "Beacon data: " + b.state, b.state !== "no_beacon"));
    } else {
      p.appendChild(add(el("dl", "kv"),
        fact("VISITORS 24H", led(num(b.visitors_24h), 3.6, "green"), "visitors-24h"),
        fact("VISITORS 7D", led(num(b.visitors_7d), 3.6, "cream"), "visitors-7d"),
        fact("PAGEVIEWS 24H", led(num(b.pageviews_24h), 3.6, "cream"), "pageviews-24h")));
    }
    out.push(p);
    if (b.state === "ok") {
      out.push(add(panel("Pageviews, 7 days", ""), bars(b.daily_7d)));
      out.push(add(panel("Top pages", ""), b.top_pages.length ? table(["PATH", "VIEWS", "VISITORS"], b.top_pages.map(function (r) { return [r.path, num(r.views), num(r.visitors)]; })) : note("No pageviews in the last 7 days.")));
      out.push(add(panel("Top referrers", ""), b.top_referrers.length ? table(["REFERRER", "VIEWS"], b.top_referrers.map(function (r) { return [r.referrer, num(r.views)]; })) : note("No referrers recorded.")));
      out.push(add(panel("Countries", ""), b.countries.length ? table(["COUNTRY", "PAGEVIEWS"], b.countries.map(function (r) { return [r.country, num(r.pageviews)]; })) : note(b.countries_state === "ok" ? "No country rows in the last 7 days." : "Country data: " + b.countries_state, b.countries_state !== "ok")));
    }
    var cf = d.cf, cp = panel("Cloudflare", "");
    cp.setAttribute("data-section", "cloudflare");
    if (cf.state === "token_needed") { cp.appendChild(note("Token needed. No Cloudflare numbers are shown until a token exists (see the README).")); }
    else if (cf.state === "token_insecure") { cp.appendChild(note("The token file is too open: chmod 600.", true)); }
    else if (cf.state !== "ok") { cp.appendChild(note("Cloudflare: " + (cf.state === "cf_error" ? "api error" : cf.state), true)); }
    else {
      cp.appendChild(add(el("dl", "kv"), fact("REQUESTS 24H", led(num(cf.requests_24h), 3.6, "cream"), "cf-requests-24h"), fact("REQUESTS 7D", led(num(cf.requests_7d), 3.6, "cream"), "cf-requests-7d"),
        fact("THREATS 24H", led(num(cf.threats_24h), 3.6, cf.threats_24h > 0 ? "amber" : "cream"), "cf-threats-24h"), fact("THREATS 7D", led(num(cf.threats_7d), 3.6, "cream"), "cf-threats-7d")));
    }
    out.push(cp);
    return out;
  }

  function viewChat(d) {
    var out = [], s = d.summary || {}, f = d.funnel || {}, k = d.contacts || {};
    var p = panel("Portfolio chat", "");
    if (d.state !== "ok") { p.appendChild(note("Conversation data: " + d.state, true)); out.push(p); return out; }
    var avg = s.avg_response_ms == null ? "-" : (s.avg_response_ms / 1000).toFixed(1);
    p.appendChild(add(el("dl", "kv"),
      fact("CONV 24H", led(num(s.conversations_24h), 3.6, "cream"), "conv-24h"), fact("CONV 7D", led(num(s.conversations_7d), 3.6, "cream"), "conv-7d"),
      fact("TURNS 7D", led(num(s.turns_7d), 3.6, "cream"), "turns-7d"), fact("BLOCKED 7D", led(num(s.blocked_7d), 3.6, s.blocked_7d > 0 ? "amber" : "cream"), "blocked-7d"),
      fact("AVG RESPONSE", add(el("span"), led(avg, 3.6, "cream"), el("span", "u", "s")), "avg-response")));
    out.push(p);
    var fp = panel("Funnel, 7 days", "");
    if (f.state !== "ok") { fp.appendChild(note("Funnel data: " + f.state, true)); }
    else {
      fp.appendChild(add(el("dl", "kv"), fact("CHAT OPENS", led(num(f.opens), 3.6, "cream"), "opens"),
        fact("CONSENTS", f.recorded ? led(num(f.consents), 3.6, "cream") : "none recorded", "consents")));
      if (!f.recorded) { fp.appendChild(note("No consent event has been recorded yet, so no conversion rate is shown.")); }
    }
    out.push(fp);
    var cp = panel("Contact form", "");
    cp.appendChild(k.state === "ok" ? add(el("dl", "kv"), fact("ACCEPTED", led(num(k.accepted), 3.6, "cream"), "contacts-accepted"), fact("QUARANTINED", led(num(k.quarantined), 3.6, "cream"), "contacts-quarantined")) : note("Contact counts: " + k.state, true));
    cp.appendChild(note("Counts only. Contact content is never read or shown."));
    out.push(cp);
    var cv = panel("Conversations, last " + d.window_days + " days", "", el("span", "up", d.conversations.length + " shown"));
    if (!d.conversations.length) { cv.appendChild(note("No conversations in this window.")); }
    d.conversations.forEach(function (c) {
      var box = el("div", "conv");
      box.setAttribute("data-conv", c.id);
      var head = add(el("div", "convhead"), el("span", null, isoWhen(c.started_at)), el("span", "meta", c.total_turns + " turns"),
        el("span", "meta", (c.domains_used.join(", ") || "no domain")),
        el("span", "meta", c.total_response_time_ms == null ? "no timing" : (c.total_response_time_ms / 1000).toFixed(1) + " s total"));
      head.appendChild(c.blocked_at_layer ? tag("red", "BLOCKED " + c.blocked_at_layer) : tag("green", "ANSWERED"));
      box.appendChild(head);
      c.messages.forEach(function (m) {
        var msg = add(el("div", "msg " + m.role), el("span", "who", m.role === "user" ? "VISITOR" : m.role.toUpperCase()));
        msg.appendChild(document.createTextNode(m.content));
        if (m.truncated) { msg.appendChild(el("span", "cutmark", " (cut at 4000 characters)")); }
        box.appendChild(msg);
      });
      if (c.messages_truncated) { box.appendChild(note("Only the first 40 messages are shown.")); }
      cv.appendChild(box);
    });
    if (d.skipped) { cv.appendChild(note(d.skipped + " unreadable or malformed conversation file(s) skipped.", true)); }
    out.push(cv);
    return out;
  }

  function viewAlert(d) {
    var st = d.severity === "critical" ? "red" : "amber";
    var head = add(el("div", "head"), lamp(st, true), el("span", "nm", d.name), tag(st, st === "red" ? "CRIT" : "WARN", true), el("div", "why", d.summary));
    var p = add(panel("Alert", "alert"), head);
    p.appendChild(add(el("dl", "kv"), fact("SEVERITY", d.severity, "severity"), fact("SOURCE", d.source, "source"), fact("SINCE", when(d.since), "since")));
    var out = [p];
    var keys = Object.keys(d.labels || {}), ann = Object.keys(d.annotations || {});
    if (keys.length || ann.length) {
      out.push(add(panel("Labels and annotations", ""), table(["KEY", "VALUE"], keys.map(function (k) { return ["label " + k, d.labels[k]]; }).concat(ann.map(function (k) { return ["note " + k, d.annotations[k]]; })))));
    }
    if (d.runs && d.runs.length) {
      out.push(add(panel("Last " + d.runs.length + " runs", ""), table(["CHECK", "RESULT", "RUN AT (UTC)"], d.runs.map(function (r) { return [r.check_id, r.result, r.run_at]; }))));
    }
    if (d.state !== "ok") { out.push(add(panel("Source", "warn"), note("Alert source: " + d.state, true))); }
    return out;
  }

  // ---- game servers (ARK via Flint; Bedrock via the server-list reply and the journal). Names and journal lines are untrusted text. ----
  var NAMES_NOTE = { mismatch: "Flint's count and its last-seen list disagree, so names are not guessed.",
    unavailable: "No data from Flint.", unknown: "Flint did not report a player count.", stale: "Flint has not polled lately." };
  function gb(b) { return b == null ? "no data" : (b / 1073741824).toFixed(1) + " GB"; }
  function ageText(s) { return s == null ? "n/a" : span(s) + " ago"; }
  function gameHead(d) {
    var head = add(el("div", "head"), lamp(d.status, true), el("span", "nm", d.title), tag(d.status, d.label, true));
    if (d.reason && d.status !== "green") { head.appendChild(el("div", "why", d.reason)); }
    return add(panel(d.title, ""), head);
  }
  function gameFacts(d) {
    var p = d.players || {};
    var count = p.state === "ok" && p.count != null ? led(p.count + "/" + (p.max == null ? "-" : p.max), 3.4, "cream") : (p.state || "no data");
    return add(el("dl", "kv"), fact("PLAYERS", count, "players"),
      fact("UPTIME", d.uptime_s == null ? "n/a" : led(span(d.uptime_s), 3.4, "cream"), "uptime"),
      fact("RAM INCL. CACHE", gb(d.ram_bytes), "ram"),
      fact("CPU (ONE CORE)", d.cpu_pct == null ? "n/a" : Math.round(d.cpu_pct) + "%", "cpu"),
      fact("RESTARTS 24H", d.restarts_24h == null ? "no data" : led(String(d.restarts_24h), 3.4, "cream"), "restarts-24h"));
  }
  function nameList(panelNode, names) {
    names.forEach(function (n) { panelNode.appendChild(el("div", "nm online", n)); });
  }
  function viewGame(d) {
    var p = gameHead(d), out = [p];
    p.appendChild(gameFacts(d));
    if (d.type === "ark") {
      var sv = d.last_save || {};
      p.appendChild(add(el("dl", "kv"),
        fact("LAST SAVE", sv.state === "ok" ? ageText(sv.age_s) : "n/a: " + (sv.reason || sv.state), "last-save"),
        fact("VERSION", d.version || "not exposed", "version"),
        fact("CLUSTER", d.cluster || "-", "cluster"),
        fact("FLINT DATA", (d.freshness.state === "ok" ? "fresh" : d.freshness.state) + (d.freshness.age_s == null ? "" : " (" + Math.round(d.freshness.age_s) + " s)"), "freshness")));
      var on = add(panel("Online now", ""), el("div", "sub2", "FROM FLINT"));
      if (d.online_names.state === "ok" && d.online_names.names.length) { nameList(on, d.online_names.names); }
      else if (d.online_names.state === "ok") { on.appendChild(note("Nobody is online.")); }
      else { on.appendChild(note(NAMES_NOTE[d.online_names.state] || d.online_names.state, true)); }
      out.push(on);
      var seen = panel("Last seen", "", el("span", "up", d.last_seen.length + " listed"));
      if (d.last_seen.length) { seen.appendChild(table(["PLAYER", "LAST SEEN (LOCAL)"], d.last_seen.map(function (x) { return [x.name, x.last_seen ? isoWhen(x.last_seen) : "-"]; }))); }
      else { seen.appendChild(note("Flint lists nobody yet.")); }
      out.push(seen);
      var joins = panel("Recent joins", "");
      if (d.recent_joins.length) {
        joins.appendChild(table(["PLAYER", "JOINED"], d.recent_joins.map(function (j) { return [j.name || "name not exposed", when(j.time)]; })));
      } else { joins.appendChild(note("No join in Flint's last 20 events.")); }
      out.push(joins);
      return out;
    }
    var pg = d.ping || {}, mp = d.max_players || {};
    var maxText = mp.agree === false ? "reply " + mp.ping + " / file " + mp.properties : String(mp.ping != null ? mp.ping : (mp.properties != null ? mp.properties : "n/a"));
    p.appendChild(add(el("dl", "kv"),
      fact("VERSION", d.version || "n/a", "version"), fact("WORLD", d.level || "n/a", "level"),
      fact("MAX PLAYERS", maxText, "max-players"), fact("SERVER-LIST REPLY", pg.state || "n/a", "reply")));
    var onb = add(panel("Online now", ""), el("div", "sub2", "FROM THE JOURNAL"));
    if (d.journal_state !== "ok") { onb.appendChild(note("The start of this run is out of the journal window, so names are unknown.", true)); }
    else if (d.online_names.length) { nameList(onb, d.online_names); }
    else { onb.appendChild(note("Nobody is online.")); }
    if (d.names_match === false) { onb.appendChild(note("The journal and the server-list count disagree.", true)); }
    out.push(onb);
    var ev = panel("Joins and leaves", "");
    if (d.events.length) { ev.appendChild(table(["PLAYER", "EVENT", "TIME"], d.events.map(function (e) { return [e.name, e.kind, e.time ? isoWhen(e.time) : "-"]; }))); }
    else { ev.appendChild(note("No join or leave since the server started.")); }
    out.push(ev);
    var packs = panel("Loaded add-on packs", "");
    if (d.packs.length) { var pt = table(["#", "PACK", "VERSION"], d.packs.map(function (k) { return [k.index, k.name, k.version]; }));
      pt.className = "tbl packs";
      packs.appendChild(pt); }
    else { packs.appendChild(note("No pack lines in the window.")); }
    out.push(packs);
    var jr = panel("Journal", "", tag(d.journal.state === "ok" ? "green" : "amber", String(d.journal.state).toUpperCase().replace(/_/g, " ")));
    if (d.journal.state === "ok") {
      var log = el("div", "log");
      d.journal.lines.forEach(function (ln) { log.appendChild(el("div", null, ln)); });
      jr.appendChild(note("Last " + d.journal.lines.length + " lines, as the journal wrote them."));
      jr.appendChild(log);
    } else { jr.appendChild(note("The journal could not be read: " + d.journal.state, true)); }
    out.push(jr);
    return out;
  }

  // ---- claude usage: limits used, tokens burned, models, projects -------------------------------
  var CLAUDE_STATE = {
    ok: "Current: fetched from Anthropic within the refresh period.", pending: "Fetching from Anthropic. Reload in a moment.", disabled: "Switched off (HUD_CLAUDE_LIMITS=0).",
    no_credentials: "No Claude Code sign-in file was found.", cred_insecure: "The sign-in file is readable by others (or is a link): refused.",
    cred_unreadable: "The sign-in file could not be read.", cred_malformed: "The sign-in file is not in the expected shape.",
    token_expired: "The access token has expired. Run claude once; the HUD recovers by itself.",
    needs_reauth: "Anthropic refused the token (401). Sign in to Claude Code again.", scope_missing: "The token lacks the profile scope (403).",
    rate_limited: "Anthropic asked the HUD to slow down.", http_error: "The service answered with an error.",
    timeout: "The service did not answer in time.", unreachable: "The service could not be reached.",
    bad_json: "The reply was not JSON.", too_large: "The reply was larger than allowed.", schema_mismatch: "The reply was not the expected shape.",
    unavailable: "The aggregator has no Claude source configured.", stale: "Showing the last good value; the latest fetch failed.",
    missing: "No transcript directory was found.", empty: "No usage has been recorded yet."
  };
  var WINDOW_LABEL = { five_hour: "5-hour limit", seven_day: "Weekly limit", seven_day_opus: "Weekly limit, Opus", seven_day_sonnet: "Weekly limit, Sonnet" };
  var WINDOW_STATE = { none: "No active window.", stale_window: "This window has ended; the next fetch will show the new one.", bad_value: "The reported value is not usable." };
  function meterBar(pct, level) {
    var m = el("div", "meter " + level), lit = pct == null ? 0 : Math.min(20, Math.round((pct / 100) * 20));
    for (var i = 0; i < 20; i++) { m.appendChild(el("b", i < lit ? "on" : null)); }
    return m;
  }
  function tok(n) { return n == null ? "-" : num(n); }
  function compact(n) {
    if (n == null) { return "-"; }
    if (n < 1000) { return String(n); }
    var u = n < 1e6 ? [1e3, "K"] : n < 1e9 ? [1e6, "M"] : [1e9, "B"], v = n / u[0];
    return v.toFixed(v < 10 ? 1 : 0) + u[1];
  }
  function dur(secs) {
    if (secs == null) { return "-"; }
    var d = Math.floor(secs / 86400), h = Math.floor((secs % 86400) / 3600), m = Math.floor((secs % 3600) / 60);
    return d > 0 ? d + "D " + h + "H" : h > 0 ? h + "H " + m + "M" : m + "M";
  }
  function stateBanner(d) {
    var lvl = d.state === "ok" ? "green" : d.state === "stale" ? "amber" : "grey";
    return add(el("div", "head"), tag(lvl, String(d.state).toUpperCase().replace(/_/g, " "), true), el("span", "why", CLAUDE_STATE[d.state] || d.state));
  }
  function viewClaudeLimits(d) {
    var out = [], p = panel("Limits used", "");
    p.appendChild(stateBanner(d));
    var wins = d.windows || {}, live = d.state === "ok" || d.state === "stale";
    Object.keys(WINDOW_LABEL).forEach(function (key) {
      var w = wins[key];
      if (!w || !live) { return; }
      var row = el("div", "lim");
      row.setAttribute("data-window", key);
      var shown = w.state === "ok";
      add(row, el("div", "sub2", WINDOW_LABEL[key].toUpperCase()));
      if (shown) {
        add(row, add(el("div", "pair"), led(Math.round(w.pct) + "%", 3.6, w.level), el("span", "u", Math.round(w.left_pct) + "% left")), meterBar(w.pct, w.level));
        row.appendChild(el("p", "dnote", w.resets_at != null ? "Resets " + when(w.resets_at) + " (in " + dur(w.resets_in_s) + ")" : "Reset time unknown."));
      } else { row.appendChild(note(WINDOW_STATE[w.state] || w.state, true)); }
      p.appendChild(row);
    });
    out.push(p);
    var src = panel("Source", "");
    src.appendChild(add(el("dl", "kv"),
      fact("DATA AGE", d.age_s == null ? "no value held" : dur(d.age_s), "age"),
      fact("LAST FAILURE", d.reason || "none", "reason"),
      fact("NEXT ATTEMPT", d.retry_in_s == null && !(d.diag && d.diag.next_attempt_in_s != null) ? "-" : dur(d.retry_in_s != null ? d.retry_in_s : d.diag.next_attempt_in_s), "retry"),
      fact("FETCHES THIS RUN", d.diag ? String(d.diag.fetch_count) : "-", "fetch-count"),
      fact("REFRESH EVERY", d.diag ? dur(d.diag.ttl_s) : "-", "ttl")));
    src.appendChild(note("Read from Claude Code's own sign-in with one GET to Anthropic's usage endpoint, at most once per refresh period. " +
      "The HUD never renews the sign-in. Bars turn amber from " + (d.thresholds ? d.thresholds.amber : "-") + "% and red from " + (d.thresholds ? d.thresholds.red : "-") + "% used."));
    out.push(src);
    return out;
  }
  function barChart(buckets, label, text) {
    var W = 520, H = 130, PL = 6, PB = 16, n = buckets.length, bw = (W - 2 * PL) / n;
    var hi = Math.max.apply(null, buckets.map(function (b) { return b.new == null ? 0 : b.new; }).concat([1]));
    var s = sv("svg", { "class": "chart", viewBox: "0 0 " + W + " " + H, role: "img", "aria-label": label });
    s.setAttribute("data-chart", label);
    buckets.forEach(function (b, i) {
      var none = b.new == null, h = none ? 2 : Math.max(1, (b.new / hi) * (H - PB - 14));
      var r = sv("rect", { "class": "bar" + (none ? " none" : "") + (i === n - 1 ? " today" : ""), x: PL + i * bw + 1, y: H - PB - h, width: Math.max(1, bw - 2), height: h });
      r.setAttribute("data-new", none ? "none" : b.new);
      s.appendChild(r);
    });
    s.appendChild(sv("text", { x: PL, y: H - 3 }, text[0]));
    s.appendChild(sv("text", { x: W - 40, y: H - 3 }, text[1]));
    return s;
  }
  function rateText(v) { return v == null ? "-" : Math.round(v * 100) + "%"; }
  function viewClaudeUsage(d) {
    if (d.state !== "ok") { return [add(panel("Tokens", "warn"), stateBanner(d))]; }
    var out = [], w = d.windows;
    var head = panel("Tokens burned", "");
    head.appendChild(el("div", "sub2", "NEW TOKENS = INPUT + CACHE WRITE + OUTPUT. CACHE READS ARE SHOWN SEPARATELY."));
    var names = { today: "Today", h24: "24 h", d7: "7 days", d30: "30 days" };
    head.appendChild(table(["WINDOW", "NEW", "INPUT", "OUTPUT", "CACHE WRITE", "CACHE READ", "TURNS"],
      Object.keys(names).map(function (k) {
        var v = w[k];
        return [names[k] + (v.partial ? " *" : ""), compact(v.new), compact(v.input), compact(v.output), compact(v.cache_write), compact(v.cache_read), compact(v.turns)];
      })));
    if (w.d7.partial || w.d30.partial) { head.appendChild(note("* partial: the transcript history starts " + d.coverage_date + ".")); }
    out.push(head);
    var f = panel("Facts", "");
    var ls = d.longest_session;
    f.appendChild(add(el("dl", "kv"),
      fact("CACHE HIT, 7 D", rateText(d.cache_hit_rate.d7), "cache-hit-d7"),
      fact("CACHE HIT, TODAY", rateText(d.cache_hit_rate.today), "cache-hit-today"),
      fact("PROCESSED, 7 D", tok(w.d7.processed), "processed-d7"),
      fact("LAST 60 MIN (NEW)", tok(d.burn.last_60m_new), "burn-60m"),
      fact("PER HOUR, LAST 24 H", tok(Math.round(d.burn.hourly_avg_24h)), "burn-24h"),
      fact("SESSIONS TODAY", String(d.sessions_today), "sessions-today"),
      fact("LONGEST SESSION", ls ? dur(ls.seconds) + " (wall-clock span)" : "-", "longest-session"),
      fact("PEAK HOUR (LOCAL)", d.peak_hour ? d.peak_hour.hour + ":00" : "needs 24 h of history", "peak-hour"),
      fact("HISTORY FROM", d.coverage_date || "-", "coverage")));
    f.appendChild(note("History is the transcript retention (about a month). A day before the first transcript has no bar, not a zero."));
    out.push(f);
    var hp = panel("Last 24 hours", "", el("span", "up", "new tokens per hour"));
    hp.appendChild(barChart(d.hourly, "hourly", ["-24h", "now"]));
    out.push(hp);
    var dp = panel("Last 30 days", "", el("span", "up", "new tokens per day"));
    dp.appendChild(barChart(d.daily.map(function (b) { return { new: b.new }; }), "daily", ["-30d", "today"]));
    out.push(dp);
    var go = panel("More", "");
    go.appendChild(add(el("p", "dnote"), link("claude", "models", "Models"), document.createTextNode("  /  "), link("claude", "projects", "Projects"), document.createTextNode("  /  "), link("claude", "limits", "Limits")));
    out.push(go);
    return out;
  }
  function viewClaudeTable(d, title, heads, rowFn) {
    if (d.state !== "ok") { return [add(panel(title, "warn"), stateBanner(d))]; }
    var p = panel(title, "", el("span", "up", "last 7 days" + (d.partial ? ", partial since " + d.coverage_date : "")));
    p.appendChild(d.rows.length ? table(heads, d.rows.map(rowFn)) : note("Nothing in this window."));
    return [p];
  }
  function viewClaude(d) {
    if (d.id === "limits") { return viewClaudeLimits(d); }
    if (d.id === "usage") { return viewClaudeUsage(d); }
    if (d.id === "models") {
      return viewClaudeTable(d, "Models", ["MODEL", "NEW", "INPUT", "OUTPUT", "CACHE WRITE", "RE-READ FROM CACHE", "TURNS", "SHARE"], function (r) {
        return [r.model, tok(r.new), tok(r.input), tok(r.output), tok(r.cache_write), tok(r.cache_read), tok(r.turns), rateText(r.share)];
      });
    }
    var out = viewClaudeTable(d, "Projects", ["PROJECT", "SESSIONS", "NEW", "RE-READ FROM CACHE", "TURNS"], function (r) {
      return [r.project, tok(r.sessions), tok(r.new), tok(r.cache_read), tok(r.turns)];
    });
    if (d.state === "ok" && d.projects_total > d.rows.length) { out.push(note("Top " + d.rows.length + " of " + d.projects_total + " projects by new tokens.")); }
    return out;
  }

  var VIEWS = { claude: viewClaude, game: viewGame, service: viewService, services: viewServices, perf: viewPerf, site: viewSite, chat: viewChat, alert: viewAlert };

  // ---- loading --------------------------------------------------------------------------------
  function show(nodes) {
    while (body.firstChild) { body.removeChild(body.firstChild); }
    nodes.forEach(function (n) { body.appendChild(n); });
  }
  function message(title, text, cls) { return add(panel(title, cls || "warn"), el("p", "sub", text)); }

  function load() {
    document.getElementById("dsub").textContent = (KIND_LABEL[kind] || "detail") + (ident ? " / " + ident : "");
    if (!VIEWS[kind] || !ident) { show([message("Not found", "This detail link is not recognised.")]); document.body.setAttribute("data-ready", "notfound"); return; }
    show([message("Loading", "Fetching detail...", "")]);
    document.body.removeAttribute("data-ready");
    fetch("/api/detail/" + encodeURIComponent(kind) + "?id=" + encodeURIComponent(ident), { cache: "no-store" }).then(function (r) {
      if (r.status === 404) { show([message("Not found", "The aggregator has nothing for this id. It may have cleared since the HUD last refreshed.")]); document.body.setAttribute("data-ready", "notfound"); return null; }
      if (!r.ok) { throw new Error("http " + r.status); }
      return r.json();
    }).then(function (d) {
      if (d) { show(VIEWS[kind](d)); document.body.setAttribute("data-ready", "live"); }
    }).catch(function () {
      show([message("Could not load", "The aggregator did not answer. Press R to retry.")]);
      document.body.setAttribute("data-ready", "error");
    });
  }

  // The launcher registers a script message handler (window.close() is refused for a window WebKit did not
  // open from script); in an ordinary browser window.close() is the way.
  function closeWindow() {
    var h = window.webkit && window.webkit.messageHandlers && window.webkit.messageHandlers.hudDetail;
    if (h) { h.postMessage("close"); } else { window.close(); }
  }
  document.addEventListener("keydown", function (e) {
    if (e.key === "Escape") { e.preventDefault(); closeWindow(); }
    else if ((e.key === "r" || e.key === "R") && !e.ctrlKey && !e.metaKey && !e.altKey) { load(); }
  });
  document.getElementById("close").addEventListener("click", closeWindow);
  document.getElementById("refresh").addEventListener("click", load);
  load();
}());
