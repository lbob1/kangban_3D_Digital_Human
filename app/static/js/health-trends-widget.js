// 医生健康趋势组件（Day8 实时数据批次）：
// - Tab 卡片展示全部关联长者 + 「全部长者」总览入口；
// - 双视图：每日详细记录（四图 + 明细表）/ 14 天数据总览（汇总表 + 迷你趋势）；
// - 实时性：30 秒自动轮询（页面隐藏时暂停）+ 登记成功后立即刷新；
// - 图表点击卡片右上 ⛶ 全屏查看（同 SVG 重绘，矢量无损）；
// - 登记弹窗：血压/睡眠/夜醒/情绪/主诉 → POST /api/v1/health-records（服务端算分级）；
// 全部手写 SVG（零框架零 CDN）；颜色走主题 token，三主题自适配。
// 用法：KB.renderHealthTrends(containerEl)
(function () {
  "use strict";

  var W = 520, H = 230, PAD = { l: 48, r: 18, t: 20, b: 34 };
  var POLL_MS = 30000;

  function esc(s) {
    if (s == null) return "";
    return String(s).replace(/&/g, "&amp;").replace(/</g, "&lt;").replace(/>/g, "&gt;");
  }
  function shortDate(d) { return String(d).slice(5).replace("-", "/"); }

  // ---- SVG 坐标系 ----
  function plotBox() { return { x: PAD.l, y: PAD.t, w: W - PAD.l - PAD.r, h: H - PAD.t - PAD.b }; }
  function xAt(i, n) { var b = plotBox(); return n <= 1 ? b.x + b.w / 2 : b.x + (b.w * i) / (n - 1); }
  function yAt(v, yMin, yMax) {
    var b = plotBox();
    if (yMax === yMin) yMax = yMin + 1;
    return b.y + b.h - ((v - yMin) / (yMax - yMin)) * b.h;
  }
  function svgOpen(title) {
    return "<svg viewBox='0 0 " + W + " " + H + "' role='img' aria-label='" + esc(title) +
      "' style='width:100%;height:auto;display:block;'>";
  }
  function gridLines(yMin, yMax, ticks, fmt) {
    var b = plotBox(), out = "";
    for (var t = 0; t <= ticks; t++) {
      var v = yMin + ((yMax - yMin) * t) / ticks;
      var y = yAt(v, yMin, yMax);
      out += "<line x1='" + b.x + "' y1='" + y + "' x2='" + (b.x + b.w) + "' y2='" + y +
        "' stroke='var(--line)' stroke-width='1'/>" +
        "<text x='" + (b.x - 6) + "' y='" + (y + 4) + "' text-anchor='end' font-size='11' fill='var(--ink-faint)'>" +
        esc(fmt ? fmt(v) : Math.round(v)) + "</text>";
    }
    return out;
  }
  function xLabels(dates) {
    var b = plotBox(), out = "", n = dates.length;
    var step = Math.max(1, Math.ceil(n / 5));
    for (var i = n - 1; i >= 0; i -= step) {
      out += "<text x='" + xAt(i, n) + "' y='" + (b.y + b.h + 16) +
        "' text-anchor='middle' font-size='11' fill='var(--ink-faint)'>" + esc(shortDate(dates[i])) + "</text>";
    }
    return out;
  }
  function polyline(dates, values, yMin, yMax, color, opts) {
    opts = opts || {};
    var pts = [], dots = "", n = dates.length;
    for (var i = 0; i < n; i++) {
      if (values[i] == null) continue;
      var x = xAt(i, n), y = yAt(values[i], yMin, yMax);
      pts.push(x + "," + y);
      dots += "<circle cx='" + x + "' cy='" + y + "' r='3' fill='" + color + "'>" +
        "<title>" + esc(shortDate(dates[i]) + "：" + values[i]) + "</title></circle>";
    }
    if (!pts.length) return "";
    return "<polyline points='" + pts.join(" ") + "' fill='none' stroke='" + color +
      "' stroke-width='2'" + (opts.dash ? " stroke-dasharray='5,4'" : "") +
      " stroke-linejoin='round' stroke-linecap='round'/>" + dots;
  }
  function areaBand(dates, topVals, botVals, yMin, yMax, color) {
    var up = [], down = [], n = dates.length;
    for (var i = 0; i < n; i++) {
      if (topVals[i] == null || botVals[i] == null) continue;
      up.push(xAt(i, n) + "," + yAt(topVals[i], yMin, yMax));
    }
    for (var j = n - 1; j >= 0; j--) {
      if (topVals[j] == null || botVals[j] == null) continue;
      down.push(xAt(j, n) + "," + yAt(botVals[j], yMin, yMax));
    }
    if (!up.length) return "";
    return "<polygon points='" + up.join(" ") + " " + down.join(" ") + "' fill='" + color + "' opacity='0.45'/>";
  }
  function bars(dates, values, yMin, yMax, color) {
    var out = "", b = plotBox(), n = dates.length;
    var bw = Math.max(4, (b.w / Math.max(n, 1)) * 0.55);
    for (var i = 0; i < n; i++) {
      if (values[i] == null || values[i] <= 0) continue;
      var x = xAt(i, n) - bw / 2, y = yAt(values[i], yMin, yMax);
      out += "<rect x='" + x + "' y='" + y + "' width='" + bw + "' height='" + (b.y + b.h - y) +
        "' rx='3' fill='" + color + "' opacity='0.85'>" +
        "<title>" + esc(shortDate(dates[i]) + "：" + values[i] + " 次") + "</title></rect>";
    }
    return out;
  }
  function lowDots(dates, values, yMin, yMax, threshold) {
    var out = "", n = dates.length;
    for (var i = 0; i < n; i++) {
      if (values[i] == null || values[i] > threshold) continue;
      out += "<circle cx='" + xAt(i, n) + "' cy='" + yAt(values[i], yMin, yMax) +
        "' r='5' fill='none' stroke='var(--lacquer)' stroke-width='2'>" +
        "<title>" + esc(shortDate(dates[i]) + "：" + values[i] + "（≤" + threshold + "，yellow）") + "</title></circle>";
    }
    return out;
  }
  function emptySvg() {
    var b = plotBox();
    return "<svg viewBox='0 0 " + W + " " + H + "' style='width:100%;height:auto;display:block;'>" +
      "<line x1='" + b.x + "' y1='" + (b.y + b.h) + "' x2='" + (b.x + b.w) + "' y2='" + (b.y + b.h) +
      "' stroke='var(--line)'/><text x='" + (W / 2) + "' y='" + (H / 2) +
      "' text-anchor='middle' font-size='13' fill='var(--ink-faint)'>暂无数据</text></svg>";
  }

  // ---- 图表卡片（带全屏按钮）----

  function chartCard(id, title, subtitle, inner) {
    return "<div class='ht-chart' id='ht-" + id + "' style='background:var(--card,#fff);border:1px solid var(--line);" +
      "border-radius:var(--radius,14px);padding:14px 16px;position:relative'>" +
      "<button class='btn btn-ghost btn-sm ht-full' data-chart='ht-" + id +
      "' title='全屏查看' style='position:absolute;top:10px;right:10px'>⛶</button>" +
      "<div class='card-title' style='margin-bottom:4px'><h3 style='font-size:15px'>" + esc(title) + "</h3></div>" +
      "<div class='faint' style='font-size:12px;margin-bottom:6px'>" + esc(subtitle) + "</div>" + inner + "</div>";
  }

  function chartBP(d) {
    var vals = d.bp_systolic.concat(d.bp_diastolic).filter(function (v) { return v != null; });
    if (!vals.length) return chartCard("bp", "血压趋势", "无数据", emptySvg());
    var yMax = Math.max(180, Math.ceil(Math.max.apply(null, vals) / 20) * 20);
    return chartCard("bp", "血压趋势",
      "收缩压（漆红）/ 舒张压（苔绿）· 虚线=yellow 阈值 160 · hover 看数值",
      svgOpen("血压趋势") + gridLines(60, yMax, 4) +
      "<line x1='" + plotBox().x + "' y1='" + yAt(160, 60, yMax) + "' x2='" + (plotBox().x + plotBox().w) +
      "' y2='" + yAt(160, 60, yMax) + "' stroke='var(--amber)' stroke-width='1' stroke-dasharray='4,4' opacity='0.7'>" +
      "<title>yellow 分级阈值 160</title></line>" + xLabels(d.dates) +
      polyline(d.dates, d.bp_systolic, 60, yMax, "var(--lacquer)") +
      polyline(d.dates, d.bp_diastolic, 60, yMax, "var(--moss)") + "</svg>");
  }

  function chartSleep(d) {
    var all = d.sleep.map(function (s) { return s.minutes; }).filter(function (v) { return v != null; });
    if (!all.length) return chartCard("sleep", "睡眠趋势", "无数据", emptySvg());
    var yMax = Math.ceil(Math.max.apply(null, all) / 60) * 60;
    var deep = d.sleep.map(function (s) { return s.deep; });
    var stack = d.sleep.map(function (s) { return (s.deep != null && s.light != null) ? s.deep + s.light : null; });
    var mins = d.sleep.map(function (s) { return s.minutes; });
    return chartCard("sleep", "睡眠趋势",
      "总时长（虚线）· 深睡（赭石）+ 浅睡（苔绿）堆叠 · hover 看数值",
      svgOpen("睡眠趋势") + gridLines(0, yMax, 4, function (v) { return Math.round(v / 60) + "h"; }) +
      xLabels(d.dates) +
      areaBand(d.dates, deep, d.sleep.map(function () { return 0; }), 0, yMax, "var(--clay)") +
      areaBand(d.dates, stack, deep, 0, yMax, "var(--moss)") +
      polyline(d.dates, mins, 0, yMax, "var(--ink-soft)", { dash: true }) + "</svg>");
  }

  function chartAwake(d) {
    var vals = d.sleep.map(function (s) { return s.awakenings; });
    return chartCard("awake", "夜醒次数", "每晚夜醒次数（琥珀柱）· 持续 ≥2 次建议关注",
      svgOpen("夜醒次数") + gridLines(0, 4, 4, function (v) { return Math.round(v); }) +
      xLabels(d.dates) + bars(d.dates, vals, 0, 4, "var(--amber)") + "</svg>");
  }

  function chartMood(d) {
    return chartCard("mood", "情绪自评", "每晚外呼 1–5 分（苔绿线）· ≤2 分标红（yellow 线）",
      svgOpen("情绪自评") + gridLines(1, 5, 4) + xLabels(d.dates) +
      polyline(d.dates, d.mood, 1, 5, "var(--moss)") +
      lowDots(d.dates, d.mood, 1, 5, 2) + "</svg>");
  }

  // 迷你趋势（总览行）：sparkline 折线
  function sparkline(values, color) {
    var w = 120, h = 30, n = values.length;
    var pts = [];
    for (var i = 0; i < n; i++) {
      if (values[i] == null) continue;
      pts.push(((w * i) / Math.max(n - 1, 1)) + "," + (h - 3 - ((values[i] - 40) / 160) * (h - 6)));
    }
    if (!pts.length) return "<span class='faint'>无数据</span>";
    return "<svg viewBox='0 0 " + w + " " + h + "' style='width:110px;height:28px;vertical-align:middle'>" +
      "<polyline points='" + pts.join(" ") + "' fill='none' stroke='" + color + "' stroke-width='1.6'/></svg>";
  }
  function sleepSpark(values) {
    var w = 120, h = 30, n = values.length, pts = [];
    for (var i = 0; i < n; i++) {
      if (values[i] == null) continue;
      pts.push(((w * i) / Math.max(n - 1, 1)) + "," + (h - 3 - ((values[i] - 120) / 360) * (h - 6)));
    }
    if (!pts.length) return "<span class='faint'>无数据</span>";
    return "<svg viewBox='0 0 " + w + " " + h + "' style='width:110px;height:28px;vertical-align:middle'>" +
      "<polyline points='" + pts.join(" ") + "' fill='none' stroke='var(--moss)' stroke-width='1.6'/></svg>";
  }

  // ---- 登记弹窗 ----

  function openRegisterModal(elderId, elderName, onDone) {
    var backdrop = document.createElement("div");
    backdrop.style.cssText = "position:fixed;inset:0;background:rgba(0,0,0,.45);z-index:9999;" +
      "display:flex;align-items:center;justify-content:center";
    var levelHint = "";
    backdrop.innerHTML =
      "<div style='background:var(--card,#fff);border:1px solid var(--line);border-radius:16px;" +
      "padding:22px 24px;width:min(440px,92vw);max-height:88vh;overflow:auto'>" +
      "<div class='card-title'><h3>健康数据登记 · " + esc(elderName) + "</h3></div>" +
      "<p class='faint' style='font-size:12px'>提交后立即入趋势图并自动分级（green/yellow/red 提示）</p>" +
      "<div class='grid grid-2' style='gap:10px;margin-top:10px'>" +
      "<label class='faint'>收缩压<input id='hr-sbp' class='field' type='number' min='50' max='250' placeholder='50–250'></label>" +
      "<label class='faint'>舒张压<input id='hr-dbp' class='field' type='number' min='30' max='150' placeholder='30–150'></label>" +
      "<label class='faint'>睡眠时长（分钟）<input id='hr-sleep' class='field' type='number' min='0' max='1440' placeholder='0–1440'></label>" +
      "<label class='faint'>夜醒次数<input id='hr-awake' class='field' type='number' min='0' max='20' placeholder='0–20'></label>" +
      "<label class='faint'>入睡时刻<input id='hr-bed' class='field' type='time'></label>" +
      "<label class='faint'>起床时刻<input id='hr-wake' class='field' type='time'></label>" +
      "<label class='faint'>情绪自评（1–5）<input id='hr-mood' class='field' type='number' min='1' max='5' placeholder='1–5'></label>" +
      "<label class='faint'>主诉<input id='hr-complaint' class='field' placeholder='如：头晕/没啥事'></label>" +
      "</div><div id='hr-hint' class='mt-8'></div>" +
      "<div style='display:flex;gap:10px;justify-content:flex-end;margin-top:14px'>" +
      "<button class='btn btn-ghost btn-sm' id='hr-cancel'>取消</button>" +
      "<button class='btn btn-primary btn-sm' id='hr-submit'>提交登记</button></div></div>";
    document.body.appendChild(backdrop);
    function close() { backdrop.remove(); }
    backdrop.querySelector("#hr-cancel").addEventListener("click", close);
    backdrop.addEventListener("click", function (ev) { if (ev.target === backdrop) close(); });
    backdrop.querySelector("#hr-submit").addEventListener("click", function () {
      function val(id) { var v = backdrop.querySelector(id).value.trim(); return v === "" ? null : v; }
      var payload = {};
      ["sbp|bp_systolic", "dbp|bp_diastolic", "sleep|sleep_minutes", "awake|sleep_awakenings",
        "mood|mood", "bed|sleep_bed_time", "wake|sleep_wake_time"].forEach(function (pair) {
        var parts = pair.split("|");
        var v = val("#hr-" + parts[0]);
        if (v != null) payload[parts[1]] = (parts[0] === "bed" || parts[0] === "wake") ? v : parseInt(v, 10);
      });
      var complaint = val("#hr-complaint");
      if (complaint) payload.chief_complaint = complaint;
      var hint = backdrop.querySelector("#hr-hint");
      if (!Object.keys(payload).length) {
        hint.innerHTML = "<span style='color:var(--lacquer)'>请至少填写一项数值或主诉</span>";
        return;
      }
      KB.api("POST", "/api/v1/health-records?elder_id=" + elderId, payload)
        .then(function (res) {
          close();
          var color = res.triage_level === "red" ? "var(--lacquer)" :
            res.triage_level === "yellow" ? "var(--amber)" : "var(--moss)";
          var tip = document.createElement("div");
          tip.style.cssText = "position:fixed;top:18px;left:50%;transform:translateX(-50%);z-index:10000;" +
            "background:var(--card,#fff);border:1px solid var(--line);border-radius:12px;padding:10px 18px;" +
            "box-shadow:0 6px 24px rgba(0,0,0,.18);font-size:14px";
          tip.innerHTML = "登记成功 · 分级 <b style='color:" + color + "'>" + esc(res.triage_level || "green") +
            "</b><span class='faint' style='font-size:12px;margin-left:8px'>" + esc(res.triage_reason || "") + "</span>";
          document.body.appendChild(tip);
          setTimeout(function () { tip.remove(); }, 5000);
          onDone();
        })
        .catch(function (err) {
          hint.innerHTML = "<span style='color:var(--lacquer)'>登记失败：" +
            esc((err && err.message) || "未知错误") + "</span>";
        });
    });
  }

  // ---- 全屏覆盖层 ----

  function openFullscreen(chartEl) {
    var svg = chartEl.querySelector("svg");
    if (!svg) return;
    var overlay = document.createElement("div");
    overlay.style.cssText = "position:fixed;inset:0;background:rgba(0,0,0,.6);z-index:9998;" +
      "display:flex;align-items:center;justify-content:center;padding:4vh 4vw";
    var card = document.createElement("div");
    card.style.cssText = "background:var(--card,#fff);border-radius:16px;padding:20px;width:100%;height:100%;" +
      "display:flex;flex-direction:column";
    var title = chartEl.querySelector("h3");
    card.innerHTML = "<div style='display:flex;justify-content:space-between;align-items:center'>" +
      "<h3 style='font-size:18px'>" + esc(title ? title.textContent : "图表") + "</h3>" +
      "<button class='btn btn-ghost btn-sm' id='ht-fs-close'>关闭</button></div>" +
      "<div style='flex:1;min-height:0;display:flex;align-items:center'>" +
      "<div style='width:100%'>" + svg.outerHTML + "</div></div>";
    overlay.appendChild(card);
    document.body.appendChild(overlay);
    function close() { overlay.remove(); }
    overlay.querySelector("#ht-fs-close").addEventListener("click", close);
    overlay.addEventListener("click", function (ev) { if (ev.target === overlay) close(); });
  }

  // ---- 每日明细表 ----

  function detailTable(d) {
    var rows = "";
    for (var i = d.dates.length - 1; i >= 0; i--) {
      var s = d.sleep[i] || {};
      var mood = d.mood[i];
      rows += "<tr>" +
        "<td>" + esc(d.dates[i]) + "</td>" +
        "<td>" + (d.bp_systolic[i] != null ? d.bp_systolic[i] + "/" + d.bp_diastolic[i] : "—") + "</td>" +
        "<td>" + (s.minutes != null ? s.minutes : "—") + "</td>" +
        "<td>" + (s.deep != null ? s.deep + "/" + (s.light != null ? s.light : "—") : "—") + "</td>" +
        "<td>" + (s.awakenings != null ? s.awakenings : "—") + "</td>" +
        "<td>" + (s.bed_time ? s.bed_time + "–" + (s.wake_time || "—") : "—") + "</td>" +
        "<td>" + (mood != null ? mood + " 分" : "—") + "</td>" +
        "<td>" + esc(d.complaintTexts && d.complaintTexts[i] ? d.complaintTexts[i] : "") + "</td>" +
        "</tr>";
    }
    return "<div style='overflow-x:auto'><table class='kb-table' style='width:100%;font-size:13px'>" +
      "<thead><tr><th>日期</th><th>血压</th><th>睡眠(min)</th><th>深/浅(min)</th><th>夜醒</th>" +
      "<th>入睡–起床</th><th>情绪</th><th>主诉</th></tr></thead><tbody>" + rows + "</tbody></table></div>";
  }

  // ---- 总览表 ----

  function overviewTable(data) {
    if (!data.items.length) return "<p class='muted mt-16'>暂无关联长者</p>";
    var rows = data.items.map(function (it) {
      var bpText = (it.avg_sbp != null ? it.avg_sbp : "—") + "/" + (it.avg_dbp != null ? it.avg_dbp : "—");
      var flags = [];
      if (it.red_count) flags.push("<span class='tag tag-red' style='color:var(--lacquer)'>red×" + it.red_count + "</span>");
      if (it.yellow_count) flags.push("<span class='tag tag-amber'>yellow×" + it.yellow_count + "</span>");
      if (!flags.length) flags.push("<span class='tag tag-moss'>全部正常</span>");
      return "<tr data-elder='" + it.elder_id + "' style='cursor:pointer'>" +
        "<td><b>" + esc(it.elder_name) + "</b><span class='faint' style='margin-left:6px'>" +
        esc(it.room_no || "") + "</span></td>" +
        "<td>" + bpText + "</td>" +
        "<td>" + (it.avg_sleep != null ? Math.round(it.avg_sleep) + " min" : "—") + "</td>" +
        "<td>" + (it.avg_awakenings != null ? it.avg_awakenings : "—") + "</td>" +
        "<td>" + (it.avg_mood != null ? it.avg_mood : "—") + "</td>" +
        "<td>" + flags.join(" ") + "</td>" +
        "<td>" + (it.missing_days ? it.missing_days + " 天" : "0 天") + "</td>" +
        "<td>" + sparkline(it.sbp_series, "var(--lacquer)") + "</td>" +
        "<td>" + sleepSpark(it.sleep_series) + "</td></tr>";
    }).join("");
    return "<div style='overflow-x:auto'><table class='kb-table' style='width:100%;font-size:13px'>" +
      "<thead><tr><th>长者</th><th>均值血压</th><th>均睡眠</th><th>均夜醒</th><th>均情绪</th>" +
      "<th>分级告警</th><th>缺采集</th><th>血压迷你趋势</th><th>睡眠迷你趋势</th></tr></thead>" +
      "<tbody>" + rows + "</tbody></table></div>" +
      "<p class='faint mt-8' style='font-size:12px'>点击行进入该长者「每日详细记录」视图</p>";
  }

  // ---- 主渲染 ----

  function renderSection(container) {
    var state = {
      elders: [], elderId: null, view: "all", mode: "daily",
      timer: null, days: 14,
    };

    function setHint() { /* 预留 */ }

    function stopPolling() {
      if (state.timer) { clearInterval(state.timer); state.timer = null; }
    }
    function startPolling() {
      stopPolling();
      state.timer = setInterval(function () {
        if (!document.hidden) loadCurrent(true); // 页面隐藏时跳过，回来下一跳刷新
      }, POLL_MS);
    }

    function loadCurrent(silent) {
      if (state.view === "all") loadOverview(silent);
      else loadDaily(state.elderId, silent);
    }

    function loadDaily(elderId, silent) {
      var listEl = container.querySelector("#ht-charts");
      if (!listEl) return;
      if (!silent) KB.renderState(listEl, { kind: "loading" });
      KB.api("GET", "/api/v1/doctor/health-trends?elder_id=" + elderId + "&days=" + state.days)
        .then(function (d) {
          if (state.view !== "daily" || state.elderId !== elderId) return; // 视图已切换，丢弃过期响应
          d.complaintTexts = {};
          (d.complaints || []).forEach(function (c) {
            if (c.text && c.text !== "没啥事") d.complaintTexts[c.date] = c.text; // 过滤无意义占位主诉
          });
          var complaintTags = (d.complaints || []).filter(function (c) { return c.text && c.text !== "没啥事"; })
            .map(function (c) {
              return "<span class='tag tag-amber'>" + esc(shortDate(c.date)) + " " + esc(c.text) + "</span>";
            }).join(" ");
          listEl.innerHTML =
            "<div class='faint mb-8'>长者：<b>" + esc(d.elder_name) + "</b> · 近 " +
            d.days + " 天 · 缺采集日断点显示 · 30 秒自动刷新</div>" +
            "<div style='display:grid;grid-template-columns:repeat(auto-fit,minmax(320px,1fr));gap:16px'>" +
            chartBP(d) + chartSleep(d) + chartAwake(d) + chartMood(d) + "</div>" +
            "<div class='card mt-16' style='background:var(--card,#fff)'>" +
            "<div class='card-title'><h3 style='font-size:15px'>每日详细记录</h3></div>" +
            detailTable(d) + "</div>" +
            (complaintTags
              ? "<div class='mt-16'><div class='faint mb-8'>期间主诉：</div>" + complaintTags + "</div>"
              : "<div class='faint mt-16'>期间无有效主诉记录</div>");
          bindFullscreen(listEl);
        })
        .catch(function (err) {
          KB.renderState(listEl, {
            kind: "empty",
            message: "趋势加载失败：" + (err && err.message ? err.message : "未知错误"),
          });
        });
    }

    function loadOverview(silent) {
      var listEl = container.querySelector("#ht-charts");
      if (!listEl) return;
      if (!silent) KB.renderState(listEl, { kind: "loading" });
      KB.api("GET", "/api/v1/doctor/health-overview?days=" + state.days)
        .then(function (d) {
          if (state.view !== "all") return;
          listEl.innerHTML =
            "<div class='faint mb-8'>全部关联长者 · 近 " + d.days + " 天汇总 · 点击行进单长者明细 · 30 秒自动刷新</div>" +
            "<div class='card' style='background:var(--card,#fff)'>" + overviewTable(d) + "</div>";
          listEl.querySelectorAll("tr[data-elder]").forEach(function (tr) {
            tr.addEventListener("click", function () {
              state.elderId = tr.getAttribute("data-elder");
              state.view = "daily";
              renderTabs();
              loadDaily(state.elderId, false);
            });
          });
        })
        .catch(function (err) {
          KB.renderState(listEl, {
            kind: "empty",
            message: "总览加载失败：" + (err && err.message ? err.message : "未知错误"),
          });
        });
    }

    function bindFullscreen(scope) {
      scope.querySelectorAll(".ht-full").forEach(function (btn) {
        btn.addEventListener("click", function (ev) {
          ev.stopPropagation();
          var chart = document.getElementById(btn.getAttribute("data-chart"));
          if (chart) openFullscreen(chart);
        });
      });
    }

    function renderTabs() {
      var tabsEl = container.querySelector("#ht-tabs");
      if (!tabsEl) return;
      tabsEl.innerHTML = state.elders.map(function (e) {
        var active = state.view === "daily" && state.elderId === e.elder_id;
        return "<button class='btn btn-sm " + (active ? "btn-primary" : "btn-ghost") +
          "' data-elder='" + e.elder_id + "'>" + esc(e.name) + "</button>";
      }).join("") +
        "<button class='btn btn-sm " + (state.view === "all" ? "btn-primary" : "btn-ghost") +
        "' data-all='1'>全部长者（总览）</button>" +
        "<span class='spacer'></span>" +
        "<button class='btn btn-sm " + (state.mode === "daily" ? "btn-primary" : "btn-ghost") +
        "' data-mode='daily'>每日详细记录</button>" +
        "<button class='btn btn-sm " + (state.mode === "overview" ? "btn-primary" : "btn-ghost") +
        "' data-mode='overview'>14天数据总览</button>" +
        "<button class='btn btn-sm btn-primary' id='ht-register'>登记健康数据</button>";
      tabsEl.querySelectorAll("button[data-elder]").forEach(function (btn) {
        btn.addEventListener("click", function () {
          state.elderId = btn.getAttribute("data-elder");
          state.view = "daily"; state.mode = "daily";
          renderTabs(); loadDaily(state.elderId, false);
        });
      });
      tabsEl.querySelector("button[data-all]").addEventListener("click", function () {
        state.view = "all"; state.mode = "overview";
        renderTabs(); loadOverview(false);
      });
      tabsEl.querySelectorAll("button[data-mode]").forEach(function (btn) {
        btn.addEventListener("click", function () {
          state.mode = btn.getAttribute("data-mode");
          if (state.mode === "overview") { state.view = "all"; renderTabs(); loadOverview(false); }
          else {
            state.view = "daily";
            if (!state.elderId && state.elders.length) state.elderId = state.elders[0].elder_id;
            renderTabs();
            if (state.elderId) loadDaily(state.elderId, false);
          }
        });
      });
      tabsEl.querySelector("#ht-register").addEventListener("click", function () {
        var target = state.elderId || (state.elders[0] && state.elders[0].elder_id);
        var name = null;
        state.elders.forEach(function (e) { if (e.elder_id === target) name = e.name; });
        if (!target) return;
        openRegisterModal(target, name || "长者", function () { loadCurrent(true); });
      });
    }

    function loadElders() {
      KB.api("GET", "/api/v1/elders")
        .then(function (rows) {
          state.elders = rows || [];
          if (state.elders.length && !state.elderId) state.elderId = state.elders[0].elder_id;
          container.innerHTML =
            "<div class='card-title'><h3>健康趋势 · 多日联看</h3></div>" +
            "<div class='toolbar mb-8' id='ht-tabs'></div>" +
            "<div class='faint mb-8' id='ht-meta'>数据源：晚间健康外呼 + 手工登记 · 30 秒自动刷新（页面隐藏时暂停）</div>" +
            "<div id='ht-charts'></div>";
          renderTabs();
          if (state.view === "all") loadOverview(false); else loadDaily(state.elderId, false);
          startPolling();
        })
        .catch(function () {
          KB.renderState(container, { kind: "empty", message: "长者列表加载失败，请刷新重试" });
        });
    }

    // 组件销毁兜底：容器被替换时停止轮询
    var observer = new MutationObserver(function () {
      if (!document.body.contains(container)) { stopPolling(); observer.disconnect(); }
    });
    observer.observe(document.body, { childList: true, subtree: true });

    loadElders();
  }

  window.KB = window.KB || {};
  window.KB.renderHealthTrends = renderSection;
  window.KB.openHealthRegister = openRegisterModal; // 护士页登记入口复用（nurse.html）
})();
