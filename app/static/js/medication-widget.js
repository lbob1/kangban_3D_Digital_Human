// 用药方案管理组件（Day7+）：医生/管理员可增删改（PATCH/DELETE 软删），
// 护工/家属只读。数据源 GET /api/v1/medication/plans（家属端后端自动按关联过滤）。
// 用法：KB.renderMedicationSection(containerEl, { writable: true|false })
(function () {
  "use strict";

  var STATUS = {
    planned: ["tag-amber", "待生效"],
    active: ["tag-moss", "执行中"],
    paused: ["tag-amber", "已暂停"],
    completed: ["tag-moss", "已完成"],
    cancelled: ["tag-lacquer", "已停用"],
  };
  var STATUS_OPTIONS = ["planned", "active", "paused", "completed", "cancelled"];

  function statusTag(s) {
    var m = STATUS[s] || ["", s];
    return "<span class='tag " + m[0] + "'>" + KB.esc(m[1]) + "</span>";
  }

  function fmtDate(s) {
    if (!s) return "—";
    return String(s).slice(0, 10);
  }

  function slotText(schedule) {
    if (!schedule || !schedule.slots || !schedule.slots.length) return "—";
    return schedule.slots
      .map(function (s) { return (s.label ? s.label + " " : "") + s.time; })
      .join(" / ");
  }

  function rowHtml(p, eldersById, writable) {
    var e = eldersById[p.elder_id];
    var actions = "";
    if (writable) {
      actions =
        "<button class='btn btn-ghost btn-sm' data-edit='" + p.plan_id + "'>编辑</button>" +
        "<button class='btn btn-ghost btn-sm' data-cancel='" + p.plan_id + "'>停用</button>";
    }
    return "<div class='record-row'><div class='grow'>" +
      "<b>" + KB.esc(p.drug_name) + "</b> " + KB.esc(p.dose) + " " + statusTag(p.status) +
      "<div class='faint'>" + KB.esc(e ? e.name : "未知长者") +
      " · 服药时间：" + KB.esc(slotText(p.schedule)) +
      " · " + KB.esc(p.instructions || "无特别说明") +
      " · 起始 " + fmtDate(p.start_date) +
      (p.end_date ? " · 至 " + fmtDate(p.end_date) : "") + "</div></div>" +
      "<div class='toolbar'>" + actions + "</div></div>";
  }

  function planForm(p, elders, writableStatus) {
    // p 为空 = 新增；否则编辑（预填）
    var isEdit = !!p;
    var v = p || {
      elder_id: elders.length ? elders[0].elder_id : "",
      drug_name: "", dose: "", instructions: "",
      start_date: new Date().toISOString().slice(0, 10), end_date: "", status: "planned",
      schedule: { slots: [{ time: "08:00", label: "每天早餐后" }] },
    };
    var slot0 = (v.schedule && v.schedule.slots && v.schedule.slots[0]) || { time: "08:00", label: "" };
    var elderOpts = elders.map(function (e) {
      return "<option value='" + e.elder_id + "'" + (e.elder_id === v.elder_id ? " selected" : "") + ">" +
        KB.esc(e.name) + "</option>";
    }).join("");
    var statusField = "";
    if (isEdit && writableStatus) {
      statusField = "<label class='field-label mt-8'>状态</label><select id='mf-status' class='field'>" +
        STATUS_OPTIONS.map(function (s) {
          return "<option value='" + s + "'" + (s === v.status ? " selected" : "") + ">" +
            KB.esc(STATUS[s] ? STATUS[s][1] : s) + "</option>";
        }).join("") + "</select>";
    }
    return KB.openModal(
      "<h3>" + (isEdit ? "修改用药方案" : "新增用药方案") + "</h3>" +
      "<p class='faint'>仅医生/管理员可保存；停用为软删（保留漏服证据链）</p>" +
      (isEdit ? "" :
        "<label class='field-label'>长者</label><select id='mf-elder' class='field'>" + elderOpts + "</select>") +
      "<label class='field-label mt-8'>药名</label><input id='mf-drug' class='field' value='" + KB.esc(v.drug_name) + "'>" +
      "<label class='field-label mt-8'>剂量</label><input id='mf-dose' class='field' value='" + KB.esc(v.dose) + "'>" +
      "<label class='field-label mt-8'>服药时间</label>" +
      "<div class='toolbar'><input id='mf-time' class='field' style='width:110px' value='" + KB.esc(slot0.time) + "' placeholder='08:00'>" +
      "<input id='mf-label' class='field' value='" + KB.esc(slot0.label || "") + "' placeholder='每天早餐后'></div>" +
      "<label class='field-label mt-8'>用法说明</label><input id='mf-inst' class='field' value='" + KB.esc(v.instructions || "") + "'>" +
      "<label class='field-label mt-8'>起始日期</label><input id='mf-start' type='date' class='field' value='" + KB.esc(fmtDate(v.start_date)) + "'>" +
      "<label class='field-label mt-8'>结束日期（可空）</label><input id='mf-end' type='date' class='field' value='" + KB.esc(v.end_date ? fmtDate(v.end_date) : "") + "'>" +
      statusField +
      "<div class='modal-actions'>" +
      "<button class='btn btn-primary' id='mf-save'>保存</button>" +
      "<button class='btn btn-ghost' data-act='close'>取消</button></div>"
    );
  }

  function renderSection(container, opts) {
    var writable = !!(opts && opts.writable);
    var state = { elders: [] };

    function load() {
      KB.renderState(container, { kind: "loading" });
      Promise.all([
        KB.api("GET", "/api/v1/elders").catch(function () { return []; }),
        KB.api("GET", "/api/v1/medication/plans?limit=200"),
      ]).then(function (rs) {
        state.elders = rs[0] || [];
        var plans = rs[1] || [];
        var byId = {};
        state.elders.forEach(function (e) { byId[e.elder_id] = e; });

        var toolbar = "<div class='toolbar'>" +
          (writable ? "<button class='btn btn-primary' id='med-add'>+ 新增用药方案</button>" : "") +
          "<span class='spacer'></span><span class='faint'>" +
          (writable
            ? "写权限：医生/管理员 · 停用=软删保留证据链"
            : "只读视图：修改请请联系医生/管理员") +
          "</span></div>";
        var list = plans.length
          ? plans.map(function (p) { return rowHtml(p, byId, writable); }).join("")
          : "<p class='muted mt-16'>暂无用药计划" +
            (writable ? "，点击「新增用药方案」录入" : "") + "</p>";
        container.innerHTML = "<h3 class='mt-8'>💊 用药方案管理</h3>" + toolbar +
          "<div id='med-list'>" + list + "</div>";

        if (writable) {
          container.querySelector("#med-add").addEventListener("click", function () {
            if (!state.elders.length) return KB.toast("暂无长者档案，请先建长者", "error");
            openForm(null);
          });
          container.querySelectorAll("[data-edit]").forEach(function (b) {
            var p = plans.find(function (x) { return x.plan_id === b.dataset.edit; });
            if (p) b.addEventListener("click", function () { openForm(p); });
          });
          container.querySelectorAll("[data-cancel]").forEach(function (b) {
            var p = plans.find(function (x) { return x.plan_id === b.dataset.cancel; });
            if (p) b.addEventListener("click", function () { cancelPlan(p); });
          });
        }
      }).catch(function (e) {
        KB.renderState(container, { kind: "error", message: KB.errText(e), traceId: e.traceId }, load);
      });
    }

    function openForm(p) {
      var m = planForm(p, state.elders, true);
      m.modal.style.width = "min(520px, 100%)";
      m.modal.querySelector("[data-act='close']").addEventListener("click", m.close);
      m.modal.querySelector("#mf-save").addEventListener("click", function () {
        var body = {
          drug_name: m.modal.querySelector("#mf-drug").value.trim(),
          dose: m.modal.querySelector("#mf-dose").value.trim(),
          instructions: m.modal.querySelector("#mf-inst").value.trim() || null,
          start_date: m.modal.querySelector("#mf-start").value || null,
          end_date: m.modal.querySelector("#mf-end").value || null,
          schedule: {
            slots: [{
              time: m.modal.querySelector("#mf-time").value.trim() || "08:00",
              label: m.modal.querySelector("#mf-label").value.trim(),
            }],
          },
        };
        var statusEl = m.modal.querySelector("#mf-status");
        var method, url;
        if (p) {
          method = "PATCH";
          url = "/api/v1/medication/plans/" + p.plan_id;
          body.status = statusEl ? statusEl.value : p.status;
        } else {
          method = "POST";
          url = "/api/v1/medication/plans";
          body.elder_id = m.modal.querySelector("#mf-elder").value;
          if (!body.drug_name || !body.dose) return KB.toast("药名与剂量必填", "error");
          if (!body.start_date) return KB.toast("起始日期必填", "error");
        }
        var btn = this;
        btn.disabled = true;
        KB.api(method, url, body).then(function () {
          KB.toast(p ? "已保存修改" : "已新增用药方案", "success");
          m.close();
          load();
        }).catch(function (e) {
          KB.toast(KB.errText(e), "error");
          btn.disabled = false;
        });
      });
    }

    function cancelPlan(p) {
      KB.confirmModal(
        "停用用药方案",
        "停用「" + p.drug_name + "」后不再触发提醒（软删，保留漏服证据链）。确认停用？"
      ).then(function (ok) {
        if (!ok) return;
        KB.api("DELETE", "/api/v1/medication/plans/" + p.plan_id).then(function () {
          KB.toast("已停用", "success");
          load();
        }).catch(function (e) { KB.toast(KB.errText(e), "error"); });
      });
    }

    load();
    return { reload: load };
  }

  KB.renderMedicationSection = renderSection;
})();
