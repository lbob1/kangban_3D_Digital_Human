/* 护工看板待处理队列（Day8 步7）：四类工单聚合（GET /nurse/workqueue）。
 * 四组折叠面板：健康告警默认展开，其余（用药漏服/敏感事件/求助）默认收起；
 * 交互模式与 sensitive-widget 一致（record-row 行 + 详情弹窗 + 两栏必填处理弹窗）。
 * HITL 接图（步6）：health_alert 行带图挂起站点——
 *   pending_site=nurse_confirm → 「确认处理」（resolve 续图）/「升级医生」（escalate 续图）；
 *   pending_site=doctor_confirm → 提示已升级待医生确认（护工不可再操作）；
 *   其余三类 → 0015 审核链 resolve（与敏感事件同口径：原因说明+解决方案必填）。
 * 楼栋隔离与 abuse 排除在服务端，前端只做渲染。
 * 用法：页面引入本文件后调用 KB.renderWorkqueueCard()。
 */
(function () {
  "use strict";

  var TYPE_META = {
    health_alert: { label: "健康告警", icon: "🩺", open: true },
    medication_missed: { label: "用药漏服", icon: "💊", open: false },
    sensitive_alert: { label: "敏感事件", icon: "🚨", open: false },
    help_request: { label: "求助", icon: "🆘", open: false },
  };
  var PRIORITY_META = {
    urgent: ["tag danger", "紧急"],
    high: ["tag tag-amber", "高"],
    normal: ["tag", "普通"],
    low: ["tag faint", "低"],
  };
  var PENDING_HINT = {
    nurse_confirm: "待护工确认",
    doctor_confirm: "待医生确认",
  };

  var elderNames = {};
  var collapsed = {}; // 折叠状态（默认按 TYPE_META.open）

  function esc(s) { return KB.esc(s == null ? "" : String(s)); }
  function fmt(t) { return (t || "").replace("T", " ").slice(0, 16); }
  function nl(s) { return esc(s).replace(/\n/g, "<br>"); }
  function elderName(r) {
    return elderNames[r.elder_id] || ("长者 " + esc(r.elder_id.slice(0, 8)));
  }
  function priTag(p) {
    var m = PRIORITY_META[p] || ["tag", p];
    return "<span class='" + m[0] + "'>" + esc(m[1]) + "</span>";
  }
  function statusTag(r) {
    if (r.status === "open") {
      return r.pending_site
        ? "<span class='tag warn'>" + esc(PENDING_HINT[r.pending_site] || "待处理") + "</span>"
        : "<span class='tag warn'>待处理</span>";
    }
    if (r.status === "acknowledged") return "<span class='tag tag-moss'>已确认</span>";
    if (r.status === "resolved") {
      return r.review_status === "pending"
        ? "<span class='tag tag-amber'>已处理·待审核</span>"
        : r.review_status === "approved"
          ? "<span class='tag tag-moss'>已审核</span>"
          : "<span class='tag tag-moss'>已处理</span>";
    }
    return "<span class='tag'>" + esc(r.status) + "</span>";
  }

  function loadNames() {
    return KB.api("GET", "/api/v1/elders").then(function (rows) {
      (rows || []).forEach(function (e) { elderNames[e.elder_id] = e.name; });
    }).catch(function () { /* 名册失败不阻断队列 */ });
  }

  // ---------- 行渲染：按四类分组，组内按紧急度/时间排序 ----------

  function rowHtml(r) {
    var meta = TYPE_META[r.type] || { label: r.type, icon: "📋" };
    return "<div class='record-row' style='align-items:flex-start;cursor:pointer;' data-id='" + r.workorder_id + "'>" +
      "<div class='grow'>" +
      "<span class='tag'>" + meta.icon + " " + esc(meta.label) + "</span> " + priTag(r.priority) + " " + statusTag(r) +
      "<b style='margin-left:6px;'>" + esc(r.title) + "</b>" +
      "<div class='muted' style='margin-top:6px;'>" + (r.description ? esc((r.description).split("\n")[0]) : "—") + "</div>" +
      "<div class='faint'>" + esc(elderName(r)) + " · 上报 " + fmt(r.created_at) + "</div>" +
      "</div></div>";
  }

  function renderGroups(rows) {
    var box = document.getElementById("workqueue-box");
    var html = "";
    Object.keys(TYPE_META).forEach(function (type) {
      var meta = TYPE_META[type];
      var items = rows.filter(function (r) { return r.type === type; });
      var isCollapsed = collapsed[type] === undefined ? !meta.open : collapsed[type];
      html +=
        "<div class='wq-group' data-type='" + type + "' style='margin-bottom:8px;'>" +
        "<div class='wq-head record-row' style='cursor:pointer;user-select:none;'>" +
        "<div class='grow'><b>" + meta.icon + " " + esc(meta.label) + "</b>" +
        " <span class='tag " + (items.length ? "tag-amber" : "tag") + "'>" + items.length + "</span>" +
        "</div><span class='faint'>" + (isCollapsed ? "▸ 展开" : "▾ 收起") + "</span></div>" +
        "<div class='wq-body' style='display:" + (isCollapsed ? "none" : "block") + ";'>" +
        (items.length
          ? items.map(rowHtml).join("")
          : "<p class='faint' style='padding:8px 0;'>暂无" + esc(meta.label) + "工单</p>") +
        "</div></div>";
    });
    box.innerHTML = html;

    // 折叠开关
    box.querySelectorAll(".wq-head").forEach(function (head) {
      head.addEventListener("click", function () {
        var type = head.parentElement.dataset.type;
        collapsed[type] = !(collapsed[type] === undefined ? !TYPE_META[type].open : collapsed[type]);
        renderGroups(currentRows);
      });
    });
    // 行点击详情
    box.querySelectorAll("[data-id]").forEach(function (row) {
      row.addEventListener("click", function () {
        var r = currentRows.find(function (x) { return x.workorder_id === row.dataset.id; });
        if (r) openDetail(r);
      });
    });
  }

  var currentRows = [];

  function load(status) {
    var box = document.getElementById("workqueue-box");
    if (!box) return;
    KB.renderState(box, { kind: "loading" });
    KB.api("GET", "/api/v1/nurse/workqueue?status=" + status).then(function (rows) {
      currentRows = rows || [];
      if (!currentRows.length) {
        box.innerHTML = "";
        return KB.renderState(box, { kind: "empty", message: "当前没有待处理工单，一切正常" });
      }
      renderGroups(currentRows);
    }).catch(function (e) {
      KB.renderState(box, { kind: "error", message: KB.errText(e), traceId: e.traceId }, function () {
        load(document.getElementById("wq-status").value);
      });
    });
  }

  // ---------- 详情弹窗：按类型与挂起站点给出可执行操作 ----------

  function openDetail(r) {
    var meta = TYPE_META[r.type] || { label: r.type, icon: "📋" };
    var isHealth = r.type === "health_alert";
    var canResolve = r.status === "open" &&
      (!isHealth || r.pending_site === "nurse_confirm");
    var canEscalate = isHealth && r.status === "open" && r.pending_site === "nurse_confirm";

    var hint = "";
    if (isHealth && r.pending_site === "doctor_confirm") {
      hint = "<div class='warn-line mt-16'>⏫ 该告警已升级或为急警，等待<b>医生</b>确认（医生端「健康急警确认」处理）。</div>";
    }

    var m = KB.openModal(
      "<h3>" + meta.icon + " " + esc(meta.label) + " · 工单详情</h3>" +
      "<p style='margin-top:8px;'>" + priTag(r.priority) + " " + statusTag(r) +
      " <b style='margin-left:6px;'>" + esc(r.title) + "</b></p>" +
      "<p class='faint'>长者：" + esc(elderName(r)) + " ｜ 上报 " + fmt(r.created_at) + "</p>" +
      hint +
      "<hr class='divider'>" +
      "<h3 class='hp-head'>说明</h3>" +
      "<p class='ai-text'>" + (r.description ? nl(r.description) : "—") + "</p>" +
      (r.solution
        ? "<hr class='divider'><h3 class='hp-head'>解决方案</h3><p class='ai-text'>" + nl(r.solution) + "</p>"
        : "") +
      "<div class='modal-actions'>" +
      (canResolve ? "<button class='btn btn-primary' id='wq-resolve'>确认处理</button>" : "") +
      (canEscalate ? "<button class='btn btn-primary' id='wq-escalate' style='background:var(--lacquer,#b3261e);'>升级医生</button>" : "") +
      "<button class='btn btn-ghost' data-act='close'>关闭</button>" +
      "</div>"
    );
    m.modal.style.width = "min(640px, 100%)";
    m.modal.querySelector("[data-act='close']").addEventListener("click", m.close);
    if (canResolve) {
      m.modal.querySelector("#wq-resolve").addEventListener("click", function () {
        openResolveForm(r, function () { m.close(); reload(); });
      });
    }
    if (canEscalate) {
      m.modal.querySelector("#wq-escalate").addEventListener("click", function () {
        openEscalateForm(r, function () { m.close(); reload(); });
      });
    }
  }

  function reload() {
    load(document.getElementById("wq-status").value);
  }

  // ---------- 确认处理：原因说明 + 解决方案两栏必填（审计留痕，家属端可见） ----------

  function openResolveForm(r, onDone) {
    var isHealth = r.type === "health_alert";
    var m = KB.openModal(
      "<h3>确认处理</h3>" +
      "<p class='faint'>" +
      (isHealth
        ? "确认后健康告警工单流转「已确认」，图内分级记录归档。"
        : "处理后将进入审核队列（管理员终审），解决方案家属端可见。") +
      " 两项均为必填（审计留痕）</p>" +
      "<div class='field' style='margin-top:12px;'><label>原因说明 *</label>" +
      "<textarea id='wq-note' rows='3' placeholder='例如：已上门查看，长者主诉缓解'></textarea></div>" +
      "<div class='field' style='margin-top:8px;'><label>解决方案 *</label>" +
      "<textarea id='wq-solution' rows='3' placeholder='例如：已通知家属并安排明日随访'></textarea></div>" +
      "<div id='wq-err' class='warn-line' style='display:none;'></div>" +
      "<div class='modal-actions'>" +
      "<button class='btn btn-primary' id='wq-submit'>提交处理</button>" +
      "<button class='btn btn-ghost' data-act='close'>取消</button></div>"
    );
    m.modal.style.width = "min(540px, 100%)";
    m.modal.querySelector("[data-act='close']").addEventListener("click", m.close);

    m.modal.querySelector("#wq-submit").addEventListener("click", function () {
      var note = m.modal.querySelector("#wq-note").value.trim();
      var solution = m.modal.querySelector("#wq-solution").value.trim();
      var errBox = m.modal.querySelector("#wq-err");
      if (!note || !solution) {
        errBox.textContent = "⚠ 原因说明与解决方案均为必填项";
        errBox.style.display = "";
        return;
      }
      var btn = this;
      btn.disabled = true;
      KB.api("POST", "/api/v1/nurse/workqueue/" + r.workorder_id + "/resolve",
        { note: note, solution: solution }).then(function () {
          KB.toast(isHealth ? "已确认，健康告警归档" : "已处理，待管理员审核", "success");
          m.close();
          if (onDone) onDone();
        }).catch(function (e) {
          btn.disabled = false;
          errBox.textContent = "⚠ " + KB.errText(e);
          errBox.style.display = "";
        });
    });
  }

  // ---------- 升级医生（仅健康告警）：resume escalate → 升 urgent 进医生确认 ----------

  function openEscalateForm(r, onDone) {
    KB.confirmModal("⫟ 升级给医生确认",
      "升级后该健康告警工单将转为「紧急」，等待医生在医生端确认（确认前不会向长者发出任何通知）。确定升级？",
      true
    ).then(function (ok) {
      if (!ok) return;
      KB.api("POST", "/api/v1/nurse/workqueue/" + r.workorder_id + "/escalate", {})
        .then(function () {
          KB.toast("已升级，待医生确认", "success");
          if (onDone) onDone();
        })
        .catch(function (e) {
          KB.toast("⚠ " + KB.errText(e), "error");
        });
    });
  }

  // ---------- 卡片入口 ----------

  KB.renderWorkqueueCard = function () {
    var head = document.querySelector("main.page .page-head");
    if (!head || document.getElementById("workqueue-box")) return; // 已渲染防重
    // 插入顺序：排在敏感工单卡之后（若已渲染），否则紧随页头
    var anchor = document.getElementById("sensitive-box");
    var card = document.createElement("div");
    card.className = "card lift mt-24";
    card.innerHTML =
      "<div class='card-title'><h3 class='kicker'>📋 待处理队列 · 四类工单</h3>" +
      "<div class='toolbar' style='margin-bottom:0;'>" +
      "<select id='wq-status' class='field' style='min-height:36px;width:auto;border:1px solid var(--line);border-radius:10px;padding:0 10px;background:var(--card);color:var(--ink);'>" +
      "<option value='open'>待处理</option><option value='all'>全部</option></select>" +
      "<button class='btn btn-ghost btn-sm' id='wq-refresh'>刷新</button>" +
      "</div></div>" +
      "<p class='faint' style='margin-top:8px;'>健康告警经服务端分级后在此等待人工确认（HITL）：护工可确认或升级医生，急警由医生确认后才通知长者；范围由服务端按楼栋过滤。</p>" +
      "<div id='workqueue-box'></div>";
    if (anchor && anchor.closest(".card")) {
      anchor.closest(".card").insertAdjacentElement("afterend", card);
    } else {
      head.insertAdjacentElement("afterend", card);
    }

    var stSel = document.getElementById("wq-status");
    document.getElementById("wq-refresh").addEventListener("click", reload);
    stSel.addEventListener("change", reload);
    loadNames().then(function () { load("open"); });
  };
})();
