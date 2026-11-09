/* 紧急工单卡片（Day6+ 批5）：护工/医生/家属工作台内嵌展示。
 * 可见性矩阵在服务端：abuse 仅 admin+family（护工/医生不可见）、
 * family 只读自己关联长者的工单、resolve 仅 admin——前端只做渲染。
 * 弹窗提醒仅护工/医生（10s 轮询新增待处理工单，首屏已有也提醒）。
 * 用法：页面引入本文件后调用 KB.renderSensitiveCard()（卡片插在 .page-head 之后）。
 */
(function () {
  "use strict";

  var CATEGORY_LABEL = {
    acute_symptom: "身体急症", fall: "跌倒", lost: "走失",
    emotional_crisis: "情绪危机", emergency_call: "呼救",
    abuse: "虐待指控", other: "其他", general: "—",
  };
  var elderNames = {};

  function esc(s) { return KB.esc(s == null ? "" : String(s)); }
  function fmt(t) { return (t || "").replace("T", " ").slice(0, 16); }

  function categoryTag(cat) {
    var label = CATEGORY_LABEL[cat] || cat || "—";
    var cls = cat === "abuse" ? "tag danger" : "tag warn";
    return "<span class='" + cls + "'>" + esc(label) + "</span>";
  }

  function statusTag(s) {
    return s === "open"
      ? "<span class='tag warn'>待处理</span>"
      : "<span class='tag tag-moss'>已处理</span>";
  }

  function loadNames() {
    return KB.api("GET", "/api/v1/elders").then(function (rows) {
      (rows || []).forEach(function (e) { elderNames[e.elder_id] = e.name; });
    }).catch(function () { /* 名册失败不阻断工单列表 */ });
  }

  function load(status) {
    var box = document.getElementById("sensitive-box");
    if (!box) return;
    KB.renderState(box, { kind: "loading" });
    KB.api("GET", "/api/v1/admin/sensitive-events?status=" + status).then(function (rows) {
      if (!rows.length) {
        return KB.renderState(box, { kind: "empty", message: "当前没有可见的紧急工单" });
      }
      box.innerHTML = rows.map(function (r) {
        return "<div class='record-row' style='align-items:flex-start;'>" +
          "<div class='grow'>" +
          categoryTag(r.category) + " " + statusTag(r.status) +
          "<b style='margin-left:6px;'>" + esc(r.title) + "</b>" +
          "<div class='muted' style='margin-top:6px;'>语句（脱敏）：" + esc((r.description || "").split("\n")[0]) + "</div>" +
          "<div class='faint'>" + esc(elderNames[r.elder_id] || "长者 " + esc(r.elder_id.slice(0, 8))) +
          " · 发生 " + fmt(r.created_at) +
          (r.resolved_at ? " · 处理于 " + fmt(r.resolved_at) : "") + "</div>" +
          "</div></div>";
      }).join("");
    }).catch(function (e) {
      KB.renderState(box, { kind: "error", message: KB.errText(e), traceId: e.traceId }, function () {
        load(status);
      });
    });
  }

  KB.renderSensitiveCard = function () {
    var head = document.querySelector("main.page .page-head");
    if (!head || document.getElementById("sensitive-box")) return; // 已渲染防重
    var role = (KB.getSession().role || "");

    var card = document.createElement("div");
    card.className = "card";
    card.innerHTML =
      "<div class='card-title'><h3 class='kicker'>🚨 紧急工单 · HITL 人工确认队列</h3>" +
      "<div class='toolbar' style='margin-bottom:0;'>" +
      "<select id='sw-status' class='field' style='min-height:36px;width:auto;border:1px solid var(--line);border-radius:10px;padding:0 10px;background:var(--card);color:var(--ink);'>" +
      "<option value='open'>待处理</option><option value='all'>全部</option></select>" +
      "<button class='btn btn-ghost btn-sm' id='sw-refresh'>刷新</button>" +
      "</div></div>" +
      "<p class='faint' style='margin-top:8px;'>命中敏感词表的语句一票否决：数字人只播安抚话术、不调用大模型，事件在此等待人工确认（可见范围由服务端按角色过滤）。</p>" +
      "<div id='sensitive-box'></div>";
    head.insertAdjacentElement("afterend", card);

    var stSel = document.getElementById("sw-status");
    document.getElementById("sw-refresh").addEventListener("click", function () { load(stSel.value); });
    stSel.addEventListener("change", function () { load(stSel.value); });
    loadNames().then(function () { load("open"); });

    // 弹窗提醒：仅护工/医生（首屏已有待处理工单同样提醒）
    if (role === "nurse" || role === "doctor") {
      var seen = {};
      setInterval(function () {
        KB.api("GET", "/api/v1/admin/sensitive-events?status=open&limit=20").then(function (rows) {
          var fresh = rows.filter(function (r) { return !seen[r.workorder_id]; });
          rows.forEach(function (r) { seen[r.workorder_id] = true; });
          if (fresh.length) {
            KB.confirmModal("🚨 紧急工单提醒",
              "有 " + fresh.length + " 条待处理紧急工单，请立即前往确认处理！", true);
          }
        }).catch(function () { /* 轮询失败静默，下次再试 */ });
      }, 10000);
    }
  };
})();
