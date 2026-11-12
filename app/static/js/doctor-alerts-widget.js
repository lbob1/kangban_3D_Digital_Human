/* 医生端健康急警待办卡（Day12 补 UI 闭环）：GET /api/v1/doctor/health-alerts。
 * 数据源=健康图建的 health_alert 工单（red 直达 / yellow 升级共用 interrupt ②）；
 * pending_site=doctor_confirm → 「确认放行」（POST /doctor/health-alerts/{id}/confirm，
 * 确认即放行通知+转诊——确认前零通知红线在图内保证，前端只驱动）；
 * pending_site=nurse_confirm → 只读展示「待护工确认」。可见范围在服务端过滤，前端只渲染。
 * 用法：页面引入本文件后调用 KB.renderDoctorAlertsCard()。
 */
(function () {
  "use strict";

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

  function esc(s) { return KB.esc(s == null ? "" : String(s)); }
  function fmt(t) { return (t || "").replace("T", " ").slice(0, 16); }
  function nl(s) { return esc(s).replace(/\n/g, "<br>"); }
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
    return "<span class='tag'>" + esc(r.status) + "</span>";
  }

  function rowHtml(r) {
    return "<div class='record-row' style='align-items:flex-start;cursor:pointer;' data-id='" + r.workorder_id + "'>" +
      "<div class='grow'>" +
      priTag(r.priority) + " " + statusTag(r) +
      "<b style='margin-left:6px;'>" + esc(r.title) + "</b>" +
      "<div class='muted' style='margin-top:6px;'>" + (r.description ? esc(r.description.split("\n")[0]) : "—") + "</div>" +
      "<div class='faint'>" + esc(r.elder_name || ("长者 " + r.elder_id.slice(0, 8))) + " · 上报 " + fmt(r.created_at) + "</div>" +
      "</div></div>";
  }

  var currentRows = [];

  function renderList(rows) {
    var box = document.getElementById("doctor-alerts-box");
    currentRows = rows || [];
    if (!currentRows.length) {
      box.innerHTML = "";
      return KB.renderState(box, { kind: "empty", message: "当前没有健康急警工单，一切正常" });
    }
    box.innerHTML = currentRows.map(rowHtml).join("");
    box.querySelectorAll("[data-id]").forEach(function (row) {
      row.addEventListener("click", function () {
        var r = currentRows.find(function (x) { return x.workorder_id === row.dataset.id; });
        if (r) openDetail(r);
      });
    });
  }

  function load(status) {
    var box = document.getElementById("doctor-alerts-box");
    if (!box) return;
    KB.renderState(box, { kind: "loading" });
    KB.api("GET", "/api/v1/doctor/health-alerts?status=" + status).then(function (rows) {
      renderList(rows);
    }).catch(function (e) {
      KB.renderState(box, { kind: "error", message: KB.errText(e), traceId: e.traceId }, function () {
        load(document.getElementById("da-status").value);
      });
    });
  }

  function reload() {
    load(document.getElementById("da-status").value);
  }

  // ---------- 详情弹窗：doctor_confirm 可确认放行 ----------

  function openDetail(r) {
    var canConfirm = r.status === "open" &&
      (r.pending_site === "doctor_confirm" || r.pending_site === "nurse_confirm");
    var isYellow = r.status === "open" && r.pending_site === "nurse_confirm";
    var hint = "";
    if (isYellow) {
      hint = "<div class='warn-line mt-16'>ℹ 该告警为 yellow 级，正等待<b>护工</b>确认；您也可直接确认放行" +
        "（工单归档，<b>不发通知</b>，与护工确认同口径）。</div>";
    }
    var m = KB.openModal(
      "<h3>🩺 健康急警 · 工单详情</h3>" +
      "<p style='margin-top:8px;'>" + priTag(r.priority) + " " + statusTag(r) +
      " <b style='margin-left:6px;'>" + esc(r.title) + "</b></p>" +
      "<p class='faint'>长者：" + esc(r.elder_name || ("长者 " + r.elder_id.slice(0, 8))) + " ｜ 上报 " + fmt(r.created_at) + "</p>" +
      hint +
      "<hr class='divider'>" +
      "<h3 class='hp-head'>分级依据（阈值在代码，不经模型）</h3>" +
      "<p class='ai-text'>" + (r.description ? nl(r.description) : "—") + "</p>" +
      (r.solution
        ? "<hr class='divider'><h3 class='hp-head'>处理说明</h3><p class='ai-text'>" + nl(r.solution) + "</p>"
        : "") +
      "<div class='modal-actions'>" +
      (canConfirm ? "<button class='btn btn-primary' id='da-confirm' style='background:var(--lacquer,#b3261e);'>" +
        (isYellow ? "确认归档" : "确认放行") + "</button>" : "") +
      "<button class='btn btn-ghost' data-act='close'>关闭</button>" +
      "</div>"
    );
    m.modal.style.width = "min(640px, 100%)";
    m.modal.querySelector("[data-act='close']").addEventListener("click", m.close);
    if (canConfirm) {
      m.modal.querySelector("#da-confirm").addEventListener("click", function () {
        var msg = isYellow
          ? "该告警为 yellow 级：确认后工单归档，不向任何端发通知（与护工确认同口径）。确定归档？"
          : "确认后立即向护工/家属放行紧急通知，并生成 mock 转诊单（确认前不会向长者发出任何通知）。确定放行？";
        KB.confirmModal(isYellow ? "⫟ 确认归档该 yellow 告警？" : "⫟ 确认放行该健康急警？", msg, true).then(function (ok) {
          if (!ok) return;
          KB.api("POST", "/api/v1/doctor/health-alerts/" + r.workorder_id + "/confirm", {})
            .then(function () {
              KB.toast(isYellow ? "已确认，工单归档（未发通知）" : "已确认放行，通知已发出", "success");
              m.close();
              reload();
            })
            .catch(function (e) {
              KB.toast("⚠ " + KB.errText(e), "error");
            });
        });
      });
    }
  }

  // ---------- 卡片入口 ----------

  KB.renderDoctorAlertsCard = function () {
    var head = document.querySelector("main.page .page-head");
    if (!head || document.getElementById("doctor-alerts-box")) return; // 已渲染防重
    var card = document.createElement("div");
    card.className = "card lift mt-24";
    card.innerHTML =
      "<div class='card-title'><h3 class='kicker'>🩺 健康急警 · 待医生确认</h3>" +
      "<div class='toolbar' style='margin-bottom:0;'>" +
      "<select id='da-status' class='field' style='min-height:36px;width:auto;border:1px solid var(--line);border-radius:10px;padding:0 10px;background:var(--card);color:var(--ink);'>" +
      "<option value='open'>待处理</option><option value='all'>全部</option></select>" +
      "<button class='btn btn-ghost btn-sm' id='da-refresh'>刷新</button>" +
      "</div></div>" +
      "<p class='faint' style='margin-top:8px;'>服务端分级产生的健康告警在此等待人工确认：red 由医生放行（通知三端+mock 转诊）；yellow 待护工确认，医生也可直接归档（不发通知）。确认前零通知。</p>" +
      "<div id='doctor-alerts-box'></div>";
    head.insertAdjacentElement("afterend", card);

    var stSel = document.getElementById("da-status");
    document.getElementById("da-refresh").addEventListener("click", reload);
    stSel.addEventListener("change", reload);
    load("open");
  };
})();
