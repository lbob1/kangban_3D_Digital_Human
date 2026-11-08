/* ============================================================
   康伴 · 动效层脚本（前端交互升级 D7 + D6）
   职责三合一：动效总开关（localStorage 记忆）+ 系统「减弱动效」
   偏好跟随 + [data-stagger] 入场延迟分配。无框架，全站一份。
   回退：删除本文件 + 移除各 HTML 引用即整体回退。
   位置约定：以普通 <script> 置于 <head>（documentElement 已存在，
   可在首帧前设好 data-motion，避免入场动画闪播）。
   ============================================================ */
(function () {
  "use strict";
  var KEY = "kb-motion"; // localStorage：用户显式选择（"on"/"off"）
  var root = document.documentElement;
  var mq = window.matchMedia ? window.matchMedia("(prefers-reduced-motion: reduce)") : null;

  function stored() {
    try { return localStorage.getItem(KEY); } catch (e) { return null; }
  }
  function apply(on) { root.setAttribute("data-motion", on ? "on" : "off"); }

  // 初始状态：用户显式选择优先；未设置时跟随系统（减弱动效 → 默认 off）
  var saved = stored();
  apply(saved ? saved === "on" : !(mq && mq.matches));

  // 系统偏好变化：仅当用户未显式选择时实时跟随
  if (mq) {
    var follow = function () { if (!stored()) apply(!mq.matches); };
    if (mq.addEventListener) mq.addEventListener("change", follow);
    else if (mq.addListener) mq.addListener(follow);
  }

  function isOn() { return root.getAttribute("data-motion") !== "off"; }

  function toggle() {
    var next = !isOn();
    apply(next);
    try { localStorage.setItem(KEY, next ? "on" : "off"); } catch (e) { /* 隐私模式等：仅本次会话生效 */ }
  }

  // 左下角固定胶囊开关（D7）：半透明，不占布局
  function mountSwitch() {
    var btn = document.createElement("button");
    btn.type = "button";
    btn.id = "kb-motion-switch";
    btn.title = "动效开/关（记忆偏好）";
    btn.setAttribute("aria-pressed", String(isOn()));
    btn.textContent = "🎬 动效";
    btn.addEventListener("click", function () {
      toggle();
      btn.setAttribute("aria-pressed", String(isOn()));
    });
    document.body.appendChild(btn);
  }

  // 入场 stagger（D6）：仅对显式标记 [data-stagger] 的容器生效，
  // DOMContentLoaded 执行一次；业务 JS 后续渲染/刷新不重播
  function stagger() {
    var boxes = document.querySelectorAll("[data-stagger]");
    Array.prototype.forEach.call(boxes, function (box) {
      Array.prototype.forEach.call(box.children, function (child, i) {
        child.style.animationDelay = (i * 70) + "ms";
      });
    });
  }

  if (document.readyState === "loading") {
    document.addEventListener("DOMContentLoaded", function () { mountSwitch(); stagger(); });
  } else {
    mountSwitch();
    stagger();
  }
})();
