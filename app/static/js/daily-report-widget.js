/* 家属每日日报卡（Day8 步8，大纲 8.3）：GET /family/daily-report + TTS 语音条。
 * 大字号无障碍：正文 ≥16px、标题 ≥20px、播放按钮 ≥44px 高对比；
 * 语音条：fetch blob（Bearer 经 header，<audio> 不带凭证）→ objectURL 播放/暂停；
 * TTS 失败（503）降级纯文字不白屏；375px 无横滚（纵向堆叠布局）。
 * 用法：页面引入本文件后调用 KB.renderDailyReportCard()。
 */
(function () {
  "use strict";

  var LEVEL_TAG = {
    green: ["tag tag-moss", "平稳"],
    yellow: ["tag tag-amber", "需留意"],
    red: ["tag danger", "需关注"],
  };
  var audioCache = {}; // elder_id → { url, playing }
  var currentAudio = null;

  function esc(s) { return KB.esc(s == null ? "" : String(s)); }
  function moodText(m) { return m == null ? "—" : m + " 分（5 分制）"; }
  function sleepText(minutes, awak) {
    if (minutes == null) return "—";
    var h = Math.floor(minutes / 60), m = minutes % 60;
    var t = h + " 小时" + (m ? " " + m + " 分钟" : "");
    if (awak != null) t += "（夜醒 " + awak + " 次）";
    return t;
  }

  function load(elderId, elderName, box) {
    KB.renderState(box, { kind: "loading" });
    KB.api("GET", "/api/v1/family/daily-report?elder_id=" + elderId).then(function (r) {
      var lt = LEVEL_TAG[r.triage_level];
      var bp = r.bp_systolic != null && r.bp_diastolic != null
        ? r.bp_systolic + " / " + r.bp_diastolic + " mmHg" : "—";
      var med = r.medication.total
        ? r.medication.total + " 次中已确认 " + r.medication.confirmed + " 次" +
          (r.medication.escalated ? "，" + r.medication.escalated + " 次已人工跟进" : "")
        : "今日暂无用药计划";
      box.innerHTML =
        "<div class='dr-head' style='display:flex;flex-wrap:wrap;align-items:center;gap:8px;'>" +
        "<b style='font-size:20px;'>" + esc(elderName) + " · 今日日报</b>" +
        (lt ? "<span class='" + lt[0] + "'>" + esc(lt[1]) + "</span>" : "<span class='tag'>暂无记录</span>") +
        "<span class='faint'>" + esc(r.date) + "</span>" +
        "</div>" +
        "<div class='dr-body' style='font-size:16px;line-height:1.8;margin-top:12px;'>" +
        "<div>🩺 <b>血压</b>：" + esc(bp) +
        (r.triage_reason ? "　<span class='faint'>（" + esc(r.triage_reason) + "）</span>" : "") + "</div>" +
        "<div>😴 <b>睡眠</b>：" + esc(sleepText(r.sleep_minutes, r.sleep_awakenings)) + "</div>" +
        "<div>😊 <b>心情</b>：" + esc(moodText(r.mood)) + "</div>" +
        (r.complaint ? "<div>💬 <b>主诉</b>：" + esc(r.complaint) + "</div>" : "") +
        "<div>💊 <b>用药</b>：" + esc(med) + "</div>" +
        "</div>" +
        "<div class='dr-text faint' style='font-size:16px;margin-top:12px;'>" + esc(r.summary_text) + "</div>" +
        "<div class='toolbar mt-16'>" +
        "<button class='btn btn-primary' id='dr-play' style='min-height:44px;font-size:16px;'>▶ 播放语音日报</button>" +
        "<span class='faint' id='dr-audio-state'></span>" +
        "</div>";

      var btn = box.querySelector("#dr-play");
      btn.addEventListener("click", function () { toggleAudio(elderId, btn); });
    }).catch(function (e) {
      KB.renderState(box, {
        kind: "error",
        message: KB.errText(e),
        traceId: e.traceId,
      }, function () { load(elderId, elderName, box); });
    });
  }

  function toggleAudio(elderId, btn) {
    var state = document.getElementById("dr-audio-state");
    // 正在播放 → 暂停
    if (currentAudio && !currentAudio.paused) {
      currentAudio.pause();
      btn.textContent = "▶ 播放语音日报";
      return;
    }
    if (audioCache[elderId]) {
      play(audioCache[elderId], btn);
      return;
    }
    btn.disabled = true;
    if (state) state.textContent = "语音合成中…";
    // blob 需原生 fetch（Bearer 经 header，<audio> 不携带凭证）
    fetch("/api/v1/family/daily-report/audio?elder_id=" + elderId, {
      headers: { Authorization: "Bearer " + sessionStorage.getItem("kb_token") },
    }).then(function (resp) {
      if (!resp.ok) {
        return resp.json().catch(function () { return null; }).then(function (data) {
          var err = (data && data.error) || {};
          var e = new Error(err.message || "请求失败（HTTP " + resp.status + "）");
          e.status = resp.status;
          throw e;
        });
      }
      return resp.blob();
    }).then(function (blob) {
      audioCache[elderId] = URL.createObjectURL(blob);
      btn.disabled = false;
      play(audioCache[elderId], btn);
    }).catch(function (e) {
      btn.disabled = false;
      if (state) state.textContent = "";
      // 降级文字：语音不可用时正文 summary_text 已在卡片上，只提示不白屏
      KB.toast("语音暂不可用，已展示文字版日报（" + KB.errText(e) + "）", "error");
    });
  }

  function play(url, btn) {
    if (currentAudio) { currentAudio.pause(); currentAudio.src = ""; }
    currentAudio = new Audio(url);
    btn.textContent = "⏸ 暂停播放";
    currentAudio.onended = function () { btn.textContent = "▶ 播放语音日报"; };
    currentAudio.onerror = function () {
      btn.textContent = "▶ 播放语音日报";
      KB.toast("音频播放失败，请查看文字版日报", "error");
    };
    currentAudio.play().catch(function () {
      btn.textContent = "▶ 播放语音日报";
    });
  }

  /** 家属日报卡：激活 family.html「每日日报」占位（多关联长者时下拉切换）。 */
  KB.renderDailyReportCard = function () {
    var anchor = document.getElementById("daily-report-section");
    if (!anchor || anchor.dataset.ready) return;
    anchor.dataset.ready = "1";
    KB.api("GET", "/api/v1/elders").then(function (rows) {
      var elders = (rows || []).filter(function (e) { return e.status !== "discharged"; });
      if (!elders.length) {
        anchor.innerHTML =
          "<div class='card-title'><h3 style='font-size:20px;'>📰 每日日报</h3></div>" +
          "<p class='muted' style='font-size:16px;'>暂无关联的长者，请联系社区工作人员为您建立关联。</p>";
        return;
      }
      var sel = elders.length > 1
        ? "<select id='dr-elder' class='field' style='min-height:44px;font-size:16px;max-width:220px;'>" +
          elders.map(function (e) { return "<option value='" + e.elder_id + "'>" + esc(e.name) + "</option>"; }).join("") +
          "</select>"
        : "";
      anchor.innerHTML =
        "<div class='card-title'><h3 style='font-size:20px;'>📰 每日日报</h3>" +
        "<div class='toolbar' style='margin-bottom:0;'>" + sel +
        "<button class='btn btn-ghost btn-sm' id='dr-refresh' style='min-height:44px;'>刷新</button></div></div>" +
        "<div id='dr-box' class='mt-8'></div>";
      var box = anchor.querySelector("#dr-box");
      var current = function () {
        var sel = anchor.querySelector("#dr-elder"); // 单位长者时不渲染下拉，直接取第一位
        return (sel && elders.find(function (e) { return e.elder_id === sel.value; })) || elders[0];
      };
      var render = function () {
        var e = current();
        load(e.elder_id, e.name, box);
      };
      if (anchor.querySelector("#dr-elder")) {
        anchor.querySelector("#dr-elder").addEventListener("change", render);
      }
      anchor.querySelector("#dr-refresh").addEventListener("click", render);
      render();
    }).catch(function (e) {
      anchor.innerHTML =
        "<div class='card-title'><h3 style='font-size:20px;'>📰 每日日报</h3></div>" +
        "<p class='muted' style='font-size:16px;'>加载失败：" + esc(KB.errText(e)) + "</p>";
    });
  };
})();
