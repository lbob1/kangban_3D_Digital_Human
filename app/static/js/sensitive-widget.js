/* 紧急工单卡片（Day6+ 批5-7）：护工/医生/家属工作台内嵌展示。
 * 可见性矩阵在服务端：abuse 仅 admin+family（护工/医生不可见）、
 * family 只读自己关联长者的工单、resolve 开放 admin/nurse/doctor（批6）——前端只做渲染。
 * 批6：按账号「已读/未读」标记（详情打开时调 read 端点）、行点击详情弹窗、
 *      护工/医生「标记已处理」两栏必填弹窗；家属端可见处理状态与解决方案（只读）。
 * 批7：详情弹窗升级为处理时间线（每条处理记录=角色+姓名+时间+原因说明+解决方案）、
 *      多角色处理显示「护工+医生 共同处理」徽标、「已请XX协助待补充」提示、
 *      处理弹窗带身份提示与「请医生/护士协助」勾选、resolved 后可「补充解决方案」
 *      （追加痕迹）；KB.sensitiveDetail() 暴露给 admin.html 表格复用。
 * 用法：页面引入本文件后调用 KB.renderSensitiveCard()（卡片插在 .page-head 之后）。
 */
(function () {
  "use strict";

  var CATEGORY_LABEL = {
    acute_symptom: "身体急症", fall: "跌倒", lost: "走失",
    emotional_crisis: "情绪危机", emergency_call: "呼救",
    abuse: "虐待指控", other: "其他", general: "—",
  };
  var ROLE_LABEL = { admin: "管理员", nurse: "护工", doctor: "医生", family: "家属" };
  var elderNames = {};

  function esc(s) { return KB.esc(s == null ? "" : String(s)); }
  function fmt(t) { return (t || "").replace("T", " ").slice(0, 16); }
  function nl(s) { return esc(s).replace(/\n/g, "<br>"); }
  function roleLabel(role) { return ROLE_LABEL[role] || role || "—"; }

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

  // 批7：协作徽标——处理记录覆盖 ≥2 种角色（如 护工+医生）即「共同处理」
  function collabTag(actions) {
    var roles = {};
    (actions || []).forEach(function (a) { roles[a.role] = true; });
    var list = Object.keys(roles);
    if (list.length < 2) return "";
    return "<span class='tag tag-clay' style='margin-left:4px;'>🤝 " +
      esc(list.map(roleLabel).join("+")) + " 共同处理</span>";
  }

  function loadNames() {
    return KB.api("GET", "/api/v1/elders").then(function (rows) {
      (rows || []).forEach(function (e) { elderNames[e.elder_id] = e.name; });
    }).catch(function () { /* 名册失败不阻断工单列表 */ });
  }

  // ---------- 处理记录时间线（批7） ----------

  function actionsTimeline(actions) {
    if (!actions || !actions.length) {
      return "<p class='faint mt-8'>暂无处理记录（历史数据早于批7）</p>";
    }
    return actions.map(function (a, i) {
      var head = "<span class='tag " + (a.role === "doctor" ? "tag-lacquer" : a.role === "nurse" ? "tag-moss" : "tag-clay") + "'>"
        + esc(roleLabel(a.role)) + "</span> <b>" + esc(a.actor_name) + "</b>" +
        (a.assist_requested
          ? " <span class='tag tag-amber'>已请" + esc(roleLabel(a.role === "nurse" ? "doctor" : "nurse")) + "协助</span>"
          : "");
      return "<div class='wo-action mt-8'>" + head +
        "<div class='faint'>" + fmt(a.created_at) + " · 第 " + (i + 1) + " 次处理</div>" +
        (a.note ? "<div class='muted mt-4'><b>原因说明：</b>" + nl(a.note) + "</div>" : "") +
        "<div class='muted mt-4'><b>解决方案：</b>" + nl(a.solution) + "</div>" +
        "</div>";
    }).join("");
  }

  // ---------- 详情弹窗：完整原因说明 + 解决方案时间线；打开即标已读 ----------

  function openDetail(r, onDone) {
    var role = (KB.getSession().role || "");
    var canResolve = (role === "nurse" || role === "doctor") && r.status === "open";
    // 批7：resolved 后护工/医生/管理员均可补充解决方案（追加痕迹）
    var canSupplement = r.status === "resolved" &&
      (role === "nurse" || role === "doctor" || role === "admin");
    var elderName = elderNames[r.elder_id] || ("长者 " + r.elder_id.slice(0, 8));

    var assistHint = "";
    if (r.assist_pending_for_me) {
      assistHint = "<div class='warn-line mt-16'>🤝 对方请求您协助处理，请在下方补充您的解决方案。</div>";
    } else if (r.actions && r.actions.length && r.actions[r.actions.length - 1].assist_requested) {
      var last = r.actions[r.actions.length - 1];
      var target = last.role === "nurse" ? "医生" : "护士";
      if (role !== target && !(r.actions.some(function (a) { return a.role === last.role && false; }))) {
        assistHint = "<div class='warn-line mt-16'>🤝 " + esc(last.actor_name) + "（" +
          esc(roleLabel(last.role)) + "）已请求 " + target + " 协助，等待补充。</div>";
      }
    }

    var m = KB.openModal(
      "<h3>🚨 紧急工单详情</h3>" +
      "<p style='margin-top:8px;'>" + categoryTag(r.category) + " " + statusTag(r.status) +
      collabTag(r.actions) +
      " <b style='margin-left:6px;'>" + esc(r.title) + "</b></p>" +
      "<p class='faint'>长者：" + esc(elderName) +
      " ｜ 发生 " + fmt(r.created_at) +
      (r.resolved_at ? " ｜ 处理于 " + fmt(r.resolved_at) : "") + "</p>" +
      assistHint +
      "<hr class='divider'>" +
      "<h3 class='hp-head'>语句（脱敏）与原因说明</h3>" +
      "<p class='ai-text'>" + (r.description ? nl(r.description) : "—") + "</p>" +
      "<hr class='divider'>" +
      "<h3 class='hp-head'>处理记录（" + ((r.actions || []).length) + " 条，按时间）</h3>" +
      actionsTimeline(r.actions) +
      "<div class='modal-actions'>" +
      (canResolve ? "<button class='btn btn-primary' id='wo-resolve'>标记已处理</button>" : "") +
      (canSupplement ? "<button class='btn btn-primary' id='wo-supplement'>补充解决方案</button>" : "") +
      "<button class='btn btn-ghost' data-act='close'>关闭</button>" +
      "</div>"
    );
    m.modal.style.width = "min(680px, 100%)";
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
    if (canSupplement) {
      m.modal.querySelector("#wo-supplement").addEventListener("click", function () {
        openSupplementForm(r, function () { m.close(); if (onDone) onDone(); });
      });
    }
  }
  KB.sensitiveDetail = openDetail; // 批7：供 admin.html 表格「查看」复用（同一详情/已读/补充逻辑）

  // ---------- 标记已处理（批6/7）：身份提示 + 两栏必填 + 「请另一角色协助」勾选 ----------

  function openResolveForm(r, onDone) {
    var role = (KB.getSession().role || "");
    var assistBox = "";
    if (role === "nurse" || role === "doctor") {
      var other = role === "nurse" ? "医生" : "护士";
      assistBox =
        "<label class='field' style='flex-direction:row;align-items:center;gap:8px;margin-top:10px;'>" +
        "<input type='checkbox' id='wo-assist' style='width:auto;' />" +
        "<span>🤝 请" + other + "协助处理（其解决方案将追加为共同处理记录）</span></label>";
    }
    var m = KB.openModal(
      "<h3>标记已处理</h3>" +
      "<p class='faint'>您将以 <b>" + esc(roleLabel(role)) + "</b> 身份处理「" + esc(r.title) + "」，两项均需填写（审计留痕，解决方案家属端可见）</p>" +
      "<div class='field' style='margin-top:12px;'><label>原因说明 *</label>" +
      "<textarea id='wo-note' rows='3' placeholder='例如：长者主诉头晕，已现场测血压 135/85，属体位性低血压'></textarea></div>" +
      "<div class='field' style='margin-top:8px;'><label>解决方案 *</label>" +
      "<textarea id='wo-solution' rows='3' placeholder='例如：已联系家属，安排明日随访，睡前放缓起身动作'></textarea></div>" +
      assistBox +
      "<div id='wo-err' class='warn-line' style='display:none;'></div>" +
      "<div class='modal-actions'>" +
      "<button class='btn btn-primary' id='wo-submit'>提交处理</button>" +
      "<button class='btn btn-ghost' data-act='close'>取消</button></div>"
    );
    m.modal.style.width = "min(540px, 100%)";
    m.modal.querySelector("[data-act='close']").addEventListener("click", m.close);

    m.modal.querySelector("#wo-submit").addEventListener("click", function () {
      var note = m.modal.querySelector("#wo-note").value.trim();
      var solution = m.modal.querySelector("#wo-solution").value.trim();
      var assist = !!(m.modal.querySelector("#wo-assist") &&
        m.modal.querySelector("#wo-assist").checked);
      var errBox = m.modal.querySelector("#wo-err");
      if (!note || !solution) {
        errBox.textContent = "⚠ 原因说明与解决方案均为必填项";
        errBox.style.display = "";
        return;
      }
      var btn = this;
      btn.disabled = true;
      KB.api("POST", "/api/v1/admin/sensitive-events/" + r.workorder_id + "/resolve",
        { note: note, solution: solution, assist_requested: assist }).then(function () {
          KB.toast(assist ? "已处理，已发出协助请求" : "已标记为已处理", "success");
          m.close();
          if (onDone) onDone();
        }).catch(function (e) {
          btn.disabled = false;
          errBox.textContent = "⚠ " + KB.errText(e);
          errBox.style.display = "";
        });
    });
  }

  // ---------- 补充解决方案（批7）：resolved 后追加处理记录，不覆盖既有痕迹 ----------

  function openSupplementForm(r, onDone) {
    var role = (KB.getSession().role || "");
    var m = KB.openModal(
      "<h3>补充解决方案</h3>" +
      "<p class='faint'>您将以 <b>" + esc(roleLabel(role)) + "</b> 身份补充「" + esc(r.title) + "」——追加记录、保留此前处理痕迹，家属端可见</p>" +
      "<div class='field' style='margin-top:12px;'><label>补充/完善的解决方案 *</label>" +
      "<textarea id='wo-sup' rows='3' placeholder='例如：复查血压正常；建议 prolong 睡前缓起身，已同步医生随访计划'></textarea></div>" +
      "<div class='field' style='margin-top:8px;'><label>补充说明（可选）</label>" +
      "<input id='wo-supnote' placeholder='如：基于今日随访结果补充' /></div>" +
      "<div id='wo-err' class='warn-line' style='display:none;'></div>" +
      "<div class='modal-actions'>" +
      "<button class='btn btn-primary' id='wo-sup-submit'>提交补充</button>" +
      "<button class='btn btn-ghost' data-act='close'>取消</button></div>"
    );
    m.modal.style.width = "min(540px, 100%)";
    m.modal.querySelector("[data-act='close']").addEventListener("click", m.close);

    m.modal.querySelector("#wo-sup-submit").addEventListener("click", function () {
      var solution = m.modal.querySelector("#wo-sup").value.trim();
      var note = m.modal.querySelector("#wo-supnote").value.trim();
      var errBox = m.modal.querySelector("#wo-err");
      if (!solution) {
        errBox.textContent = "⚠ 补充的解决方案为必填项";
        errBox.style.display = "";
        return;
      }
      var btn = this;
      btn.disabled = true;
      KB.api("POST", "/api/v1/admin/sensitive-events/" + r.workorder_id + "/supplement",
        { solution: solution, note: note || null }).then(function () {
          KB.toast("已追加处理记录", "success");
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
          categoryTag(r.category) + " " + statusTag(r.status) + collabTag(r.actions) + readTag(r.read_by_me) +
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
      (role === "nurse" || role === "doctor" ? "并标记已处理，可勾选请医生/护士协助" : "") + "）。</p>" +
      "<div id='sensitive-box'></div>";
    head.insertAdjacentElement("afterend", card);

    var stSel = document.getElementById("sw-status");
    document.getElementById("sw-refresh").addEventListener("click", function () { load(stSel.value); });
    stSel.addEventListener("change", function () { load(stSel.value); });
    loadNames().then(function () { load("open"); });

    // 弹窗提醒：仅护工/医生——待处理新工单 + 对方请求我协助的已处理工单（批7）
    if (role === "nurse" || role === "doctor") {
      var seen = {};
      setInterval(function () {
        KB.api("GET", "/api/v1/admin/sensitive-events?status=all&limit=50").then(function (rows) {
          var freshOpen = rows.filter(function (r) {
            return r.status === "open" && !seen[r.workorder_id];
          });
          var assistMe = rows.filter(function (r) {
            return r.assist_pending_for_me && !seen["assist:" + r.workorder_id];
          });
          rows.forEach(function (r) {
            seen[r.workorder_id] = true;
            if (r.assist_pending_for_me) seen["assist:" + r.workorder_id] = true;
          });
          if (freshOpen.length) {
            KB.confirmModal("🚨 紧急工单提醒",
              "有 " + freshOpen.length + " 条待处理紧急工单，请立即前往确认处理！", true);
          } else if (assistMe.length) {
            KB.confirmModal("🤝 协助请求提醒",
              "有 " + assistMe.length + " 条工单请求您（" + roleLabel(role) + "）协助处理，请前往补充解决方案！", true);
          }
        }).catch(function () { /* 轮询失败静默，下次再试 */ });
      }, 10000);
    }
  };
})();
