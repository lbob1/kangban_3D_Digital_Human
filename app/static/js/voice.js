/**
 * 康伴 Day3 语音测试台主逻辑。
 *
 * 链路（Day3 规划 §3.1，用户决策①②）：
 * - 音频：getUserMedia → WebRTC（POST /api/v1/rtc/offer，设备 Token 鉴权）；
 * - 事件：同源 WS /api/v1/voice/ws（首条消息 auth，JSON 事件流）；
 * - 声纹：首句服务端 TEN-VAD + CAMPPlus 验证 → verification 事件 → 300s 免验；
 * - TTS：tts 事件（base64 16k PCM）→ AudioContext 播放；
 * - 降级：error 事件 → 可见提示 + 文字通道（验收项 5，不白屏不静默）。
 */
(function () {
  "use strict";

  // ---------- DOM ----------
  var $ = function (id) { return document.getElementById(id); };
  var statusEl = $("status"), scoreEl = $("score"), badge = $("conn-badge");
  var logAsr = $("log-asr"), logReply = $("log-reply");
  var loginCard = $("login-card"), voiceCard = $("voice-card"), fallbackCard = $("fallback-card");

  // ---------- 状态 ----------
  var deviceToken = sessionStorage.getItem("kb_device_token") || "";
  var ws = null, pc = null, micStream = null;
  var lastVoiceToken = null; // verification 通过后下发，重连时 resume 免验
  var audioCtx = null, ttsQueue = [], ttsPlaying = false;
  var connected = false;

  function setStatus(text, cls) {
    statusEl.textContent = text;
    statusEl.className = "voice-status" + (cls ? " " + cls : "");
  }
  function setBadge(text, ok) {
    badge.innerHTML = '<span class="dot"></span>' + text;
    badge.style.borderColor = ok === true ? "#2e7d32" : ok === false ? "#c62828" : "#ccc";
  }
  function appendLog(el, text, cls) {
    if (el.textContent === "—") el.textContent = "";
    var line = document.createElement("div");
    if (cls) line.className = cls;
    line.textContent = text;
    el.appendChild(line);
    el.scrollTop = el.scrollHeight;
  }
  function showFallback(reason) {
    fallbackCard.style.display = "";
    $("fallback-reason").textContent = reason;
  }
  function hideFallback() { fallbackCard.style.display = "none"; }

  // ---------- WS 事件通道 ----------
  function connectWS(token, onReady) {
    var proto = location.protocol === "https:" ? "wss://" : "ws://";
    ws = new WebSocket(proto + location.host + "/api/v1/voice/ws");
    ws.onopen = function () {
      ws.send(JSON.stringify({ type: "auth", token: token }));
    };
    ws.onmessage = function (e) {
      var ev;
      try { ev = JSON.parse(e.data); } catch (err) { return; }
      handleEvent(ev, onReady);
    };
    ws.onclose = function (e) {
      connected = false;
      setBadge("连接关闭(" + e.code + ")", false);
      setStatus("连接已关闭：" + (e.reason || e.code), "err");
      if (e.code === 4403) appendLog(logAsr, "[鉴权失败] 人 JWT 不能用于设备通道", "e");
      else if (e.code === 4401) appendLog(logAsr, "[鉴权失败] 设备 Token 无效", "e");
    };
    ws.onerror = function () { setBadge("连接异常", false); };
  }

  function handleEvent(ev, onReady) {
    switch (ev.type) {
      case "ready":
        connected = true;
        setBadge("已连接 · " + (ev.mode === "single" ? "单人间" : "双人间"), true);
        setStatus(ev.voiceprint_ready
          ? "已就绪，请说第一句话完成声纹验证"
          : "已就绪（本房间长者尚未录入声纹，可点「录入声纹」）", "warn");
        if (lastVoiceToken) ws.send(JSON.stringify({ type: "resume", token: lastVoiceToken }));
        if (onReady) onReady();
        break;
      case "verification":
        if (ev.accepted) {
          lastVoiceToken = ev.token || lastVoiceToken;
          scoreEl.textContent = "声纹验证通过 · 相似度 " + ev.score;
          setStatus("声纹通过！请继续说话", "ok");
        } else {
          scoreEl.textContent = "声纹未通过" + (ev.score != null ? " · 相似度 " + ev.score : "") +
            (ev.reason ? " · " + ev.reason : "");
          setStatus("声纹未通过，请再说一次（靠近设备）", "warn");
        }
        break;
      case "state":
        if (ev.state === "verified") setStatus("身份已确认，请说话", "ok");
        if (ev.state === "awaiting_voiceprint") setStatus("免验已过期，请再说一句完成验证", "warn");
        break;
      case "transcript":
        appendLog(logAsr, (ev.is_final ? "【终】" : "【部分】") + ev.text +
          " (" + ev.mode + " " + ev.confidence + ")", ev.is_final ? "t" : "");
        break;
      case "tts":
        appendLog(logReply, "🔊 " + ev.text, "r");
        playTts(ev.audio, ev.sample_rate);
        break;
      case "done":
        if (!ev.text) { setStatus("没有听清，请再说一次", "warn"); }
        else setStatus("本轮完成，请继续说话", "ok");
        break;
      case "need_confirm":
        setStatus("声纹连续未通过，请在下方选择身份", "err");
        var box = $("confirm-box"), wrap = $("confirm-candidates");
        box.style.display = "";
        wrap.innerHTML = "";
        (ev.candidates || []).forEach(function (name) {
          var b = document.createElement("button");
          b.className = "btn btn-ghost";
          b.textContent = name;
          b.onclick = function () {
            box.style.display = "none";
            setStatus("已选择「" + name + "」，请管理员重新录入声纹后重试", "warn");
            appendLog(logAsr, "[主动确认] 已选择长者：" + name, "e");
          };
          wrap.appendChild(b);
        });
        break;
      case "error":
        setStatus("⚠️ " + ev.message, "err");
        appendLog(logReply, "[降级] " + ev.message, "e");
        if (ev.code === "asr_unavailable" || ev.code === "asr_unreachable" ||
            ev.code === "voiceprint_unavailable") {
          showFallback(ev.message + "（语音链路中断时可在此使用文字输入）");
        }
        if (ev.reply_text) appendLog(logReply, "💬 " + ev.reply_text, "r");
        break;
      case "pong":
        break;
      default:
        break;
    }
  }

  // ---------- TTS 播放（base64 16k PCM → AudioContext）----------
  function playTts(base64Pcm, sampleRate) {
    var bin = atob(base64Pcm);
    var bytes = new Uint8Array(bin.length);
    for (var i = 0; i < bin.length; i++) bytes[i] = bin.charCodeAt(i);
    ttsQueue.push({ bytes: bytes, rate: sampleRate || 16000 });
    drainTts();
  }
  function drainTts() {
    if (ttsPlaying || ttsQueue.length === 0) return;
    ttsPlaying = true;
    var item = ttsQueue.shift();
    audioCtx = audioCtx || new (window.AudioContext || window.webkitAudioContext)();
    var buf = audioCtx.createBuffer(1, item.bytes.length / 2, item.rate);
    var ch = buf.getChannelData(0);
    var view = new DataView(item.bytes.buffer);
    for (var i = 0; i < ch.length; i++) ch[i] = view.getInt16(i * 2, true) / 32768;
    var src = audioCtx.createBufferSource();
    src.buffer = buf;
    src.connect(audioCtx.destination);
    src.onended = function () { ttsPlaying = false; drainTts(); };
    src.start();
  }

  // ---------- WebRTC 音频上行 ----------
  function startVoice() {
    if (!connected) { setStatus("请先连接语音", "warn"); return; }
    if (pc) { setStatus("对话进行中", "ok"); return; }
    navigator.mediaDevices.getUserMedia({
      audio: { echoCancellation: true, noiseSuppression: true, autoGainControl: true }
    }).then(function (stream) {
      micStream = stream;
      pc = new RTCPeerConnection(); // 本地 host 候选；跨机器时由服务端注入 ICE
      stream.getTracks().forEach(function (t) { pc.addTrack(t, stream); });
      return pc.createOffer().then(function (offer) {
        return pc.setLocalDescription(offer).then(function () {
          return fetch("/api/v1/rtc/offer", {
            method: "POST",
            headers: {
              "Content-Type": "application/json",
              Authorization: "Bearer " + deviceToken
            },
            body: JSON.stringify({ sdp: pc.localDescription.sdp, type: pc.localDescription.type })
          });
        });
      }).then(function (resp) {
        if (resp.status === 403) throw new Error("设备 Token 无权访问 /rtc/offer");
        if (resp.status === 401) throw new Error("设备 Token 已失效，请重新连接");
        if (!resp.ok) throw new Error("信令失败 HTTP " + resp.status);
        return resp.json();
      }).then(function (answer) {
        setStatus("语音通道建立中…", "warn");
        return pc.setRemoteDescription({ sdp: answer.sdp, type: answer.type });
      }).then(function () {
        setStatus("🎤 正在聆听（首句将做声纹验证）…", "warn");
      });
    }).catch(function (err) {
      setStatus("语音启动失败：" + err.message, "err");
      stopVoice();
    });
  }

  function stopVoice() {
    if (pc) { pc.close(); pc = null; }
    if (micStream) { micStream.getTracks().forEach(function (t) { t.stop(); }); micStream = null; }
    setStatus("已停止聆听", "");
  }

  // ---------- 声纹录入（AudioWorklet 采集 2 段 × 3 秒）----------
  function enroll() {
    if (!deviceToken) { setStatus("请先设备登录", "warn"); return; }
    setStatus("声纹录入：请准备好，念「今天天气真好，我很开心」约 3 秒 × 2 段", "warn");
    navigator.mediaDevices.getUserMedia({ audio: true }).then(function (stream) {
      var ctx = new (window.AudioContext || window.webkitAudioContext)();
      return ctx.audioWorklet.addModule("/js/audio-worklet.js").then(function () {
        var src = ctx.createMediaStreamSource(stream);
        var node = new AudioWorkletNode(ctx, "capture-processor");
        src.connect(node); // 不连 destination，避免回放啸叫
        var samples = [];
        var segments = [];
        return new Promise(function (resolve) {
          node.port.onmessage = function (e) {
            samples.push(e.data);
            var total = samples.reduce(function (s, a) { return s + a.length; }, 0);
            if (total >= 48000 * 3) { // 3 秒 @48kHz
              segments.push(downsampleToInt16(flatten(samples), ctx.sampleRate));
              samples = [];
              if (segments.length >= 2) { resolve({ segments: segments, ctx: ctx, stream: stream }); }
              else setStatus("第 1 段完成，请再念一遍", "warn");
            }
          };
        });
      }).then(function (r) {
        return new Promise(function (res) { setTimeout(res, 300); }).then(function () { return r; });
      }).then(function (r) {
        src.disconnect();
        r.stream.getTracks().forEach(function (t) { t.stop(); });
        r.ctx.close();
        var body = {
          audio_samples: r.segments.map(function (seg) { return toBase64(seg); }),
          sample_uri: null
        };
        setStatus("声纹上传中…", "warn");
        return fetch("/api/v1/voice/enroll/device", {
          method: "POST",
          headers: { "Content-Type": "application/json", Authorization: "Bearer " + deviceToken },
          body: JSON.stringify(body)
        }).then(function (resp) { return resp.json().then(function (j) { return { ok: resp.ok, j: j }; }); });
      }).then(function (r) {
        if (r.ok) {
          setStatus("✅ 声纹录入成功，请说第一句话验证", "ok");
          appendLog(logReply, "[声纹] 录入成功 id=" + r.j.voiceprint_id, "r");
        } else {
          setStatus("声纹录入失败：" + (r.j.detail || JSON.stringify(r.j)), "err");
        }
      });
    }).catch(function (err) {
      setStatus("声纹录入失败：" + err.message, "err");
    });
  }

  function flatten(arrs) {
    var total = arrs.reduce(function (s, a) { return s + a.length; }, 0);
    var out = new Float32Array(total), off = 0;
    arrs.forEach(function (a) { out.set(a, off); off += a.length; });
    return out;
  }
  function downsampleToInt16(f32, fromRate) {
    var toRate = 16000, ratio = fromRate / toRate;
    var n = Math.floor(f32.length / ratio);
    var out = new Int16Array(n);
    for (var i = 0; i < n; i++) {
      var v = f32[Math.floor(i * ratio)];
      out[i] = Math.max(-32768, Math.min(32767, Math.round(v * 32767)));
    }
    return out;
  }
  function toBase64(int16arr) {
    var bytes = new Uint8Array(int16arr.buffer);
    var bin = "";
    var CHUNK = 0x8000;
    for (var i = 0; i < bytes.length; i += CHUNK) {
      bin += String.fromCharCode.apply(null, bytes.subarray(i, i + CHUNK));
    }
    return btoa(bin);
  }

  // ---------- 事件绑定 ----------
  $("btn-login").addEventListener("click", function () {
    deviceToken = $("device-token").value.trim();
    if (!deviceToken) { setStatus("请粘贴设备 Token", "warn"); return; }
    sessionStorage.setItem("kb_device_token", deviceToken);
    setStatus("连接中…", "warn");
    connectWS(deviceToken, function () {
      loginCard.style.display = "none";
      voiceCard.style.display = "";
      hideFallback();
    });
  });
  if (deviceToken) $("device-token").value = deviceToken;

  $("btn-voice").addEventListener("click", startVoice);
  $("btn-stop").addEventListener("click", stopVoice);
  $("btn-enroll").addEventListener("click", enroll);
  $("btn-text-send").addEventListener("click", function () {
    var v = $("text-input").value.trim();
    if (!v) return;
    appendLog($("log-text"), "（文字）" + v + " —— Day5 接入 LLM 后生效", "r");
    $("text-input").value = "";
  });
  $("text-input").addEventListener("keydown", function (e) {
    if (e.key === "Enter") $("btn-text-send").click();
  });
})();
