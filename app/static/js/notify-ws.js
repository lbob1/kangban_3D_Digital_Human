/* Day9 步 7：管理端通知 WS 客户端（nurse/doctor/family 三页共用）。
   铁律（主文档 §4.4）：只收不发；权限判断全在服务端（收到即展示）；
   断线静默重连 5s；弹窗复用既有玻璃令牌（KB.toast，零新样式）；
   业务 JS（voice.js/elder.js/engine）零改动。elder 端不接入。 */
(function () {
  "use strict";
  var RECONNECT_MS = 5000;
  var timer = null;
  var ws = null;

  function connect() {
    var token = sessionStorage.getItem("kb_token");
    if (!token) return; // 未登录：不连接（页面守卫已回登录页）
    var proto = location.protocol === "https:" ? "wss://" : "ws://";
    try {
      ws = new WebSocket(proto + location.host + "/ws/notify?token=" + encodeURIComponent(token));
    } catch (e) {
      scheduleReconnect();
      return;
    }
    ws.onopen = function () { /* 在线：静默 */ };
    ws.onmessage = function (ev) {
      var data;
      try { data = JSON.parse(ev.data); } catch (e) { return; }
      if (data.type !== "notify") return;
      // 复用既有玻璃 toast 令牌：紧急 urgent 用 error 红、warning 黄、其余常规
      var kind = data.severity_label || (data.level === "red" ? "urgent" : "warning");
      var text = data.elder_name
        ? "🔔 " + data.elder_name + "：" + (data.reason || "健康事件需关注")
        : "🔔 " + (data.reason || "新的工作台通知");
      if (window.KB && KB.toast) KB.toast(text, kind === "urgent" ? "error" : "info");
    };
    ws.onclose = scheduleReconnect;
    ws.onerror = function () { try { ws.close(); } catch (e) { /* 静默 */ } };
  }

  function scheduleReconnect() {
    if (timer) return;
    timer = setTimeout(function () {
      timer = null;
      connect();
    }, RECONNECT_MS);
  }

  connect();
})();
