/* 紧急工单卡片（Day6+ 批5/批6）：护工/医生/家属工作台内嵌展示。
 * 可见性矩阵在服务端：abuse 仅 admin+family（护工/医生不可见）、
 * family 只读自己关联长者的工单、resolve 开放 admin/nurse/doctor（批6）——前端只做渲染。
 * 批6 新增：按账号「已读/未读」标记（详情打开时调 read 端点）、
 * 行点击详情弹窗（含完整原因说明+解决方案时间线）、护工/医生「标记已处理」
 * 两栏必填弹窗（原因说明 + 解决方案）；家属端可见处理状态与解决方案（只读）。
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
  function nl(s) { return esc(s).replace(/\n/g, "<br>"); }

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

  function readTag(read) {
    return read
      ? "<span class='faint' style='margin-left:4px;'>✓ 已读</span>"
      : "<span class='tag tag-amber' style='margin-left:4px;'>未读</span>";
  }

  function loadNames() {
    return KB.api("GET", "/api/v1/elders").then(function (rows) {
      (rows || []).forEach(function (e) { elderNames[e.elder_id] = e.name; });
    }).catch(function () { /* 名册失败不阻断工单列表 */ });
  }

  // ---------- 详情弹窗（批6）：完整原因说明 + 解决方案；打开即标已读 ----------

  function openDetail(r, onDone) {
    var role = (KB.getSession().role || "");
    var canResolve = (role === "nurse" || role === "doctor") && r.status === "open";
    var elderName = elderNames[r.elder_id] || ("长者 " + r.elder_id.slice(0, 8));

    var solutionBlock = r.status === "resolved"
      ? "<div class='card mt-16' style='border-left:3px solid var(--moss,#4c7a5c);'>" +
        "<div class='card-title'><h3>✅ 解决方案</h3></div>" +
        "<p class='ai-text'>" + (r.solution ? nl(r.solution) : "<span class='faint'>处理人未填写解决方案</span>") + "</p>" +
        "</div>"
      : "";

    var m = KB.openModal(
      "<h3>🚨 紧急工单详情</h3>" +
      "<p style='margin-top:8px;'>" + categoryTag(r.category) + " " + statusTag(r.status) +
      " <b style='margin-left:6px;'>" + esc(r.title) + "</b></p>" +
      "<p class='faint'>长者：" + esc(elderName) +
      " ｜ 发生 " + fmt(r.created_at) +
      (r.resolved_at ? " ｜ 处理于 " + fmt(r.resolved_at) : "") + "</p>" +
      "<hr class='divider'>" +
      "<h3 class='hp-head'>语句（脱敏）与原因说明</h3>" +
      "<p class='ai-text'>" + (r.description ? nl(r.description) : "—") + "</p>" +
      solutionBlock +
      "<div class='modal-actions'>" +
      (canResolve ? "<button class='btn btn-primary' id='wo-resolve'>标记已处理</button>" : "") +
      "<button class='btn btn-ghost' data-act='close'>关闭</button>" +
      "</div>"
    );
    m.modal.style.width = "min(640px, 100%)";
    m.modal.querySelector("[data-act='close']").addEventListener("click", m.close);

    // 打开详情即标已读（幂等；未读才请求，成功后回调刷新列表）
    if (!r.read_by_me) {
      KB.api("POST", "/api/v1/admin/sensitive-events/" + r.workorder_id + "/read")
        .then(function () { if (onDone) onDone(); })
        .catch(function () { /* 标记失败不影响浏览，下次再试 */ });
    }

    if (canResolve) {
      m.modal.querySelector("#wo-resolve").addEventListener("click", function () {
        openResolveForm(r, function () { m.close(); if (onDone) onDone(); });
      });
    }
  }

  // ---------- 标记已处理（批6）：原因说明 + 解决方案两栏必填 ----------

  function openResolveForm(r, onDone) {
    var m = KB.openModal(
      "<h3>标记已处理</h3>" +
      "<p class='faint'>「" + esc(r.title) + "」— 两项均需填写（审计留痕，解决方案家属端可见）</p>" +
      "<div class='field' style='margin-top:12px;'><label>原因说明 *</label>" +
      "<textarea id='wo-note' rows='3' placeholder='例如：长者主诉头晕，已现场测血压 135/85，属体位性低血压'></textarea></div>" +
      "<div class='field' style='margin-top:8px;'><label>解决方案 *</label>" +
      "<textarea id='wo-solution' rows='3' placeholder='例如：已联系家属，安排明日随访，睡前放缓起身动作'></textarea></div>" +
      "<div id='wo-err' class='warn-line' style='display:none;'></div>" +
      "<div class='modal-actions'>" +
      "<button class='btn btn-primary' id='wo-submit'>提交处理</button>" +
      "<button class='btn btn-ghost' data-act='close'>取消</button></div>"
    );
    m.modal.style.width = "min(520px, 100%)";
    m.modal.querySelector("[data-act='close']").addEventListener("click", m.close);

    m.modal.querySelector("#wo-submit").addEventListener("click", function () {
      var note = m.modal.querySelector("#wo-note").value.trim();
      var solution = m.modal.querySelector("#wo-solution").value.trim();
      var errBox = m.modal.querySelector("#wo-err");
      if (!note || !solution) {
        errBox.textContent = "⚠ 原因说明与解决方案均为必填项";
        errBox.style.display = "";
        return;
      }
      var btn = this;
      btn.disabled = true;
      KB.api("POST", "/api/v1/admin/sensitive-events/" + r.workorder_id + "/resolve",
        { note: note, solution: solution }).then(function () {
          KB.toast("已标记为已处理", "success");
          m.close();
          if (onDone) onDone();
        }).catch(function (e) {
          btn.disabled = false;
          errBox.textContent = "⚠ " + KB.errText(e);
          errBox.style.display = "";
        });
    });
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
        return "<div class='record-row' style='align-items:flex-start;cursor:pointer;' data-id='" + r.workorder_id + "'>" +
          "<div class='grow'>" +
          categoryTag(r.category) + " " + statusTag(r.status) + readTag(r.read_by_me) +
          "<b style='margin-left:6px;'>" + esc(r.title) + "</b>" +
          "<div class='muted' style='margin-top:6px;'>语句（脱敏）：" + esc((r.description || "").split("\n")[0]) + "</div>" +
          "<div class='faint'>" + esc(elderNames[r.elder_id] || "长者 " + esc(r.elder_id.slice(0, 8))) +
          " · 发生 " + fmt(r.created_at) +
          (r.resolved_at ? " · 处理于 " + fmt(r.resolved_at) : "") + "</div>" +
          "</div></div>";
      }).join("");
      box.querySelectorAll("[data-id]").forEach(function (row) {
        row.addEventListener("click", function () {
          var r = rows.find(function (x) { return x.workorder_id === row.dataset.id; });
          if (r) openDetail(r, function () { load(document.getElementById("sw-status").value); });
        });
      });
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
      "<p class='faint' style='margin-top:8px;'>命中敏感词表的语句一票否决：数字人只播安抚话术、不调用大模型，事件在此等待人工确认（可见范围由服务端按角色过滤；点击条目查看详情" +
      (role === "nurse" || role === "doctor" ? "并标记已处理" : "") + "）。</p>" +
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
