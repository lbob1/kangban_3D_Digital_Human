/* ============================================================
   康伴 · 玻璃透明度滑杆（微调轮 3 重构，可整体回退：
   删本文件 + 移除各 HTML <script> 引用 + glass.css .glass-tuner* 段）
   - 登录页 + 五工作台各挂一个；默认只显示右上角触发按钮（毛玻璃材质，
     与卡面共用 --glass-bg 令牌 → 透明度状态始终一致），点击弹出控制面板
   - 面板弹出/收起 0.22s 缓动（opacity+transform，不触发布局重排，丝滑）
   - 实时调本页卡面玻璃 alpha，按页 localStorage 记忆（kb_glass_alpha:<page>）
   - 背景装饰开关（微调轮 5）：切 body.kb-deco-off，全局记忆 kb_deco；
     装饰层本体在 glass.css body.glass-page 多层背景里
   - 最低 alpha 5%（微调轮 3 拍板，原 20%）
   - 夜空黑主题自动禁用（深色玻璃参数固定，防拉成白玻璃破对比度）
   - 实现口径：只写 --glass-alpha / --glass-soft-alpha（elder）内联变量，
     颜色/模糊/投影等其余令牌不动；dock alpha 不在滑杆范围
   ============================================================ */

(function () {
  "use strict";

  var DEFAULTS = { glass: 0.52, soft: 0.68 }; // 与 glass.css 令牌默认值一致
  var MIN = 0.05, MAX = 0.92;

  function init() {
    var page = document.body.dataset.page || "login";
    var key = "kb_glass_alpha:" + page;
    // elder 端卡面走克制档令牌，滑杆对应调 soft；其余页面调通用档
    var prop = page === "elder" ? "--glass-soft-alpha" : "--glass-alpha";
    var def = page === "elder" ? DEFAULTS.soft : DEFAULTS.glass;

    // 触发按钮（毛玻璃材质与卡面同令牌，透明度状态自动一致）
    var btn = document.createElement("button");
    btn.type = "button";
    btn.className = "glass-tuner-btn";
    btn.setAttribute("aria-expanded", "false");
    btn.setAttribute("aria-controls", "glass-tuner-panel");
    btn.textContent = "◈ 玻璃";

    // 控制面板（默认收起）
    var panel = document.createElement("div");
    panel.id = "glass-tuner-panel";
    panel.className = "glass-tuner-panel";
    panel.innerHTML =
      "<div class='gt-head'>玻璃透明度<span class='gt-val'></span></div>" +
      "<input type='range' min='" + MIN + "' max='" + MAX + "' step='0.01'" +
      " aria-label='玻璃透明度'>" +
      "<label class='gt-deco-row'><input type='checkbox' class='gt-deco'>" +
      "背景装饰（光斑 / 圆环）</label>" +
      "<div class='gt-deco-alpha-row'><span>装饰强度</span>" +
      "<input type='range' class='gt-deco-range' min='0' max='1' step='0.05' aria-label='背景装饰强度'>" +
      "<span class='gt-deco-val'></span></div>" +
      "<div class='gt-foot'>" +
      "<span class='gt-hint'></span>" +
      "<button type='button' class='gt-reset' title='恢复默认'>↺ 恢复默认</button>" +
      "</div>";

    document.body.appendChild(btn);
    document.body.appendChild(panel);

    var range = panel.querySelector("input");
    var val = panel.querySelector(".gt-val");
    var hint = panel.querySelector(".gt-hint");
    var deco = panel.querySelector(".gt-deco");

    // ---------- 背景装饰开关 + 强度拉杆（微调轮 5/6） ----------
    // 装饰层烤在 glass.css body.glass-page 多层背景里；开关只切 body.kb-deco-off 类，
    // 强度写 --deco-k（各装饰层 alpha × k）。全站统一观感 → 全局记忆（kb_deco / kb_deco_alpha），
    // 不像 alpha 那样按页分。
    var DECO_KEY = "kb_deco";
    var DECO_ALPHA_KEY = "kb_deco_alpha";
    var decoRange = panel.querySelector(".gt-deco-range");
    var decoVal = panel.querySelector(".gt-deco-val");

    function readDeco() {
      try { return localStorage.getItem(DECO_KEY) !== "off"; } catch (e) { return true; }
    }

    function readDecoAlpha() {
      try {
        var v = parseFloat(localStorage.getItem(DECO_ALPHA_KEY));
        return (v >= 0 && v <= 1) ? v : 1;
      } catch (e) { return 1; }
    }

    function showDecoAlpha(v) { decoVal.textContent = Math.round(v * 100) + "%"; }

    function applyDecoAlpha(v, save) {
      document.documentElement.style.setProperty("--deco-k", String(v));
      decoRange.value = String(v);
      showDecoAlpha(v);
      if (save) { try { localStorage.setItem(DECO_ALPHA_KEY, String(v)); } catch (e) { /* 隐私模式忽略 */ } }
    }

    function applyDeco(on, save) {
      document.body.classList.toggle("kb-deco-off", !on);
      deco.checked = on;
      decoRange.disabled = !on; // 装饰关掉时强度拉杆一并置灰（无效果防误导）
      if (save) { try { localStorage.setItem(DECO_KEY, on ? "on" : "off"); } catch (e) { /* 隐私模式忽略 */ } }
    }

    applyDeco(readDeco(), false);
    applyDecoAlpha(readDecoAlpha(), false);
    deco.addEventListener("change", function () { applyDeco(deco.checked, true); });
    decoRange.addEventListener("input", function () { applyDecoAlpha(parseFloat(decoRange.value), true); });

    function readSaved() {
      try {
        var v = parseFloat(localStorage.getItem(key));
        return (v >= MIN && v <= MAX) ? v : null;
      } catch (e) { return null; }
    }

    function show(v) { val.textContent = Math.round(v * 100) + "%"; }

    function apply(v, save) {
      document.documentElement.style.setProperty(prop, String(v));
      show(v);
      if (save) { try { localStorage.setItem(key, String(v)); } catch (e) { /* 隐私模式忽略 */ } }
    }

    function setOpen(open) {
      panel.classList.toggle("open", open);
      btn.setAttribute("aria-expanded", open ? "true" : "false");
    }

    var initial = readSaved();
    if (initial === null) initial = def;
    range.value = String(initial);
    apply(initial, false);

    btn.addEventListener("click", function () {
      setOpen(!panel.classList.contains("open"));
    });

    // 点面板外收起（按钮自身由 toggle 处理，这里排除按钮与面板）
    document.addEventListener("click", function (ev) {
      if (!panel.classList.contains("open")) return;
      if (panel.contains(ev.target) || btn.contains(ev.target)) return;
      setOpen(false);
    });

    // Esc 收起
    document.addEventListener("keydown", function (ev) {
      if (ev.key === "Escape") setOpen(false);
    });

    range.addEventListener("input", function () { apply(parseFloat(range.value), true); });

    panel.querySelector(".gt-reset").addEventListener("click", function () {
      try { localStorage.removeItem(key); } catch (e) { /* 忽略 */ }
      document.documentElement.style.removeProperty(prop); // 回落到样式表默认
      show(def);
      range.value = String(def);
    });

    // 夜空黑：玻璃参数固定 + 装饰层强制隐藏（CSS），滑杆与装饰勾选一并禁用；
    // 主题切换不刷新页面，监听属性变化同步
    function syncTheme() {
      var dark = document.documentElement.getAttribute("data-theme") === "inknight";
      range.disabled = dark;
      deco.disabled = dark;
      decoRange.disabled = dark || !deco.checked;
      panel.classList.toggle("gt-disabled", dark);
      hint.textContent = dark ? "夜空黑玻璃参数固定" : "范围 5%–92%";
      btn.classList.toggle("gt-disabled", dark);
      btn.title = dark ? "夜空黑主题玻璃参数固定；切换到浅色主题可调" : "调整本页玻璃透明度";
    }
    syncTheme();
    if (window.MutationObserver) {
      new MutationObserver(syncTheme).observe(document.documentElement, {
        attributes: true,
        attributeFilter: ["data-theme"],
      });
    }
  }

  if (document.readyState === "loading") {
    document.addEventListener("DOMContentLoaded", init);
  } else {
    init();
  }
})();
