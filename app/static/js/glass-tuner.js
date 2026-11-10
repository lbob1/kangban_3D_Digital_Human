/* ============================================================
   康伴 · 玻璃透明度滑杆（微调轮 2 新增，可整体回退：
   删本文件 + 移除各 HTML <script> 引用 + glass.css .glass-tuner 段）
   - 登录页 + 五工作台各挂一个；右上角悬浮，实时调本页卡面玻璃 alpha
   - 按页 localStorage 记忆（kb_glass_alpha:<page>），↺ 恢复默认
   - 夜空黑主题自动禁用（深色玻璃参数固定，防拉成白玻璃破对比度）
   - 实现口径：只写 --glass-alpha / --glass-soft-alpha（elder）内联变量，
     颜色/模糊/投影等其余令牌不动；dock alpha 不在滑杆范围
   ============================================================ */

(function () {
  "use strict";

  var DEFAULTS = { glass: 0.52, soft: 0.68 }; // 与 glass.css 令牌默认值一致
  var MIN = 0.2, MAX = 0.92;

  function init() {
    var page = document.body.dataset.page || "login";
    var key = "kb_glass_alpha:" + page;
    // elder 端卡面走克制档令牌，滑杆对应调 soft；其余页面调通用档
    var prop = page === "elder" ? "--glass-soft-alpha" : "--glass-alpha";
    var def = page === "elder" ? DEFAULTS.soft : DEFAULTS.glass;

    var t = document.createElement("div");
    t.className = "glass-tuner";
    t.innerHTML =
      "<label class='gt-label'>玻璃透明度</label>" +
      "<input type='range' min='" + MIN + "' max='" + MAX + "' step='0.01'" +
      " aria-label='玻璃透明度'>" +
      "<span class='gt-val'></span>" +
      "<button type='button' class='gt-reset' title='恢复默认'>↺</button>";
    document.body.appendChild(t);

    var range = t.querySelector("input");
    var val = t.querySelector(".gt-val");

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

    var initial = readSaved();
    if (initial === null) initial = def;
    range.value = String(initial);
    apply(initial, false);

    range.addEventListener("input", function () { apply(parseFloat(range.value), true); });

    t.querySelector(".gt-reset").addEventListener("click", function () {
      try { localStorage.removeItem(key); } catch (e) { /* 忽略 */ }
      document.documentElement.style.removeProperty(prop); // 回落到样式表默认
      show(def);
      range.value = String(def);
    });

    // 夜空黑：玻璃参数固定，禁用滑杆；主题切换不刷新页面，监听属性变化同步
    function syncTheme() {
      var dark = document.documentElement.getAttribute("data-theme") === "inknight";
      range.disabled = dark;
      t.classList.toggle("gt-disabled", dark);
      t.title = dark ? "夜空黑主题玻璃参数固定；切换到陶瓷白/陶土红主题可调" : "";
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
