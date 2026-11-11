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
  var stateBadge = $("avatar-state-badge"); // Day5 优化③：数字人状态徽章
  var logAsr = $("log-asr"), logReply = $("log-reply");
  var loginCard = $("login-card"), voiceCard = $("voice-card"), fallbackCard = $("fallback-card");

  // ---------- 状态 ----------
  var deviceToken = sessionStorage.getItem("kb_device_token") || "";
  var ws = null, pc = null, micStream = null;
  var lastVoiceToken = null; // verification 通过后下发，重连时 resume 免验
  var audioCtx = null, ttsQueue = [], ttsPlaying = false;
  var ttsGain = null, currentSrc = null; // Day6：volume/stop_playback 下行事件控制
  var avatarDest = null; // Day4 口型桥：TTS → MediaStreamDestination → 3D 数字人
  var pendingViseme = null; // Day5：viseme 时间轴（先于同句 tts 事件到达，播放起点生效）
  var connected = false;

  function setStatus(text, cls) {
    statusEl.textContent = text;
    statusEl.className = "voice-status" + (cls ? " " + cls : "");
    // 动效类名钩子（前端交互升级 §4.4）：验证通过(cls=ok)时给对话卡加 ring-pass，
    // 触发装饰环加速旋转一圈；仅改类名，无任何业务逻辑
    var passCard = document.getElementById("voice-card");
    if (passCard) passCard.classList.toggle("ring-pass", cls === "ok");
  }
  function setBadge(text, ok) {
    badge.innerHTML = '<span class="dot"></span>' + text;
    badge.style.borderColor = ok === true ? "#2e7d32" : ok === false ? "#c62828" : "#ccc";
  }

  // ---------- 数字人状态徽章（Day5 优化③：单一漏斗，事件驱动防卡状态）----------
  // idle 待机灰 / listening 聆听蓝 / thinking 思考橙（律动）/ speaking 回复绿（律动）。
  // 铁律：所有状态变更只经 setAvatarState，事件映射集中在 handleEvent/startVoice/
  // stopVoice/unmute 五处，禁止散落改 className；replyPending 保证占位语朗读
  // 期间不会把「思考中」误降级为「聆听中」。
  var avatarState = "idle";
  var replyPending = false; // 终句已收到、回复尚未播完（含占位语间隙）
  function setAvatarState(next) {
    if (avatarState === next) return;
    avatarState = next;
    var map = {
      idle: ["待机", "idle"],
      listening: ["聆听中", "listening"],
      thinking: ["思考中", "thinking"],
      speaking: ["回复中", "speaking"]
    };
    var s = map[next] || map.idle;
    if (stateBadge) {
      stateBadge.textContent = s[0];
      stateBadge.className = "avatar-state " + s[1];
    }
  }
  function restState() { // 一轮回合结束后的落点：在通话=聆听，否则=待机
    return pc ? "listening" : "idle";
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
      replyPending = false;
      setAvatarState("idle"); // Day5 优化③：连接关闭=待机
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
        setAvatarState("idle"); // 已连接但未开始对话
        if (window.KangbanAvatar) KangbanAvatar.setState("in_call");
        if (lastVoiceToken) ws.send(JSON.stringify({ type: "resume", token: lastVoiceToken }));
        if (onReady) onReady();
        break;
      case "verification":
        if (ev.accepted) {
          lastVoiceToken = ev.token || lastVoiceToken;
          scoreEl.textContent = "声纹验证通过 · 相似度 " + ev.score;
          setStatus("声纹通过！请继续说话", "ok");
          unlockHistory(); // Day6+ 批4：声纹验证通过后解锁左侧对话记录面板
        } else if (ev.score == null && ev.reason) {
          // Provider 侧异常（如 TEN-VAD 未检测到语音）：如实提示原因，不算"未通过"
          scoreEl.textContent = "本次未采集到有效语音";
          setStatus(ev.reason + "——请靠近设备、开口说一句话", "warn");
        } else {
          scoreEl.textContent = "声纹未通过" + (ev.score != null ? " · 相似度 " + ev.score : "");
          setStatus("声纹未通过，请再说一次（靠近设备）", "warn");
        }
        break;
      case "state":
        if (ev.state === "verified") { setStatus("身份已确认，请说话", "ok"); unlockHistory(); }
        if (ev.state === "awaiting_voiceprint") setStatus("免验已过期，请再说一句完成验证", "warn");
        break;
      case "transcript":
        // 该 FunASR 镜像不返回置信度（恒为 0）——显示 "—" 而非误导性的 0
        appendLog(logAsr, (ev.is_final ? "【终】" : "【部分】") + ev.text +
          " (" + ev.mode + (ev.confidence > 0 ? " 置信度 " + ev.confidence : "") + ")",
          ev.is_final ? "t" : "");
        // Day5 优化③：部分转写=老人还在说（聆听）；终句=该模型接话（思考）
        if (ev.is_final) { replyPending = true; setAvatarState("thinking"); }
        else if (pc) setAvatarState("listening");
        break;
      case "action":
        // Day4：动作标记事件（服务端剥净后单独下发，不朗读不进字幕正文）
        appendLog(logReply, "🎬 动作 " + ev.action + (ev.param ? " ×" + ev.param : ""), "r");
        if (window.KangbanAvatar && KangbanAvatar.ready) {
          KangbanAvatar.playAction(ev.action, ev.param || null);
        } else {
          appendLog(logReply, "（3D 未就绪，动作跳过——语音链路不受影响）", "");
        }
        break;
      case "viseme":
        // Day5：口型时间轴（先于同句 tts 到达）；引擎未就绪则丢弃，回落能量口型
        pendingViseme = Array.isArray(ev.timeline) && ev.timeline.length ? ev.timeline : null;
        break;
      case "hint":
        // Day5 熔断提示：可见降级，不阻塞主链路
        appendLog(logReply, "💡 " + (ev.text || ev.code), "s");
        break;
      case "volume":
        // Day6：级联规则层音量指令（「大声一点/小声点」零模型直达）
        adjustVolume(ev.direction);
        appendLog(logReply, "🔊 音量" + (ev.direction === "up" ? "调大" : "调小"), "r");
        break;
      case "stop_playback":
        // Day6：级联规则层停播指令（「停」零模型直达）：清队列 + 停当前句
        stopPlayback();
        appendLog(logReply, "⏹ 已停止播放", "r");
        setStatus("已停止播放", "ok");
        break;
      case "weather":
        // Day6+：天气全量数据卡（此前只在后端日志，前端无展示——用户反馈补齐）
        appendLog(logReply, "🌤 " + (ev.full_text || JSON.stringify(ev.data || {})), "r");
        break;
      case "medication_reminder":
        // Day7 步7：定时用药提醒下行（D1 口径：元数据先到，TTS 音频经既有 tts
        // 事件自然跟进播报——数字人开口）；仅追加展示分支，不碰既有链路
        appendLog(logReply, "💊 " + (ev.text || (ev.drug_name + " " + (ev.dose || ""))), "r");
        setStatus(ev.slot + "用药提醒：" + ev.drug_name, "ok");
        break;
      case "media_play":
        // Day7 步8：戏曲/影视点播（版权授权过滤在后端；voice_rule 已服务端留痕）
        playMedia(ev);
        break;
      case "tts":
        appendLog(logReply, "🔊 " + ev.text, "r");
        setAvatarState("speaking"); // Day5 优化③：朗读期间=回复中（含占位语/提示语）
        playTts(ev.audio, ev.sample_rate);
        break;
      case "done":
        replyPending = false; // 回合闭合：占位语间隙也不会再误显思考中
        if (!ev.text) { setStatus("没有听清，请再说一次", "warn"); }
        else setStatus("本轮完成，请继续说话", "ok");
        setAvatarState(restState());
        if (historyUnlocked) loadHistory(); // 批4：每轮完成自动刷新左侧对话记录
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
        appendLog(logReply, "[降级] " + ev.message + (ev.error ? "（" + ev.error + "）" : ""), "e");
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

  // Day6：音量调节（volume 事件，0.2~1.0 夹紧，步进 25%）
  function adjustVolume(direction) {
    audioCtx = audioCtx || new (window.AudioContext || window.webkitAudioContext)();
    if (!ttsGain) ttsGain = audioCtx.createGain();
    var cur = ttsGain.gain.value;
    var next = Math.min(1, Math.max(0.2, direction === "up" ? cur + 0.25 : cur - 0.25));
    ttsGain.gain.value = next;
    setStatus("音量已调至 " + Math.round(next * 100) + "%", "ok");
  }

  // Day6：停止播放（stop_playback 事件）：清空队列 + 停当前句；
  // currentSrc.onended/unmute 兜底会立即恢复麦克风与状态，链路不卡死。
  function stopPlayback() {
    ttsQueue.length = 0;
    if (currentSrc) {
      try { currentSrc.stop(); } catch (e) { /* 已结束则忽略 */ }
      currentSrc = null;
    }
    // Day7 步8：「停」同时停媒体点播（对 TTS 链路零改动，仅追加媒体分支）；
    // Day7+ 播放器化后 mediaAudio 即弹窗播放器 <audio>，一并暂停并恢复麦克风
    if (mediaAudio) {
      try { mediaAudio.pause(); } catch (e) { /* 已结束则忽略 */ }
      mediaAudio = null;
      setPlayerMicMute(false);
      playerToggleBtn.textContent = "▶ 播放";
    }
  }

  // ---------- 曲库播放器弹窗（Day7+：语音点播自动弹出 / 「曲库」按钮手动打开）----------
  // 播放本体为弹窗内 <audio>（mediaAudio 指向它，「停」规则照常生效）；voice_rule
  // 留痕在后端，UI 点击走 POST /media/history source=ui——双来源留痕口径不变。
  var mediaAudio = null;
  var playerItems = [];        // 曲库列表 {media_id,title,artist,type,media_uri}
  var playerIdx = -1;
  var playerSeeking = false;   // 拖动进度条期间不被 timeupdate 抢写
  var playerMicMuted = false;  // 音乐播放期间静音麦克（防回声转写），暂停/结束恢复
  var playerToggleBtn = $("player-toggle");

  function fmtTime(s) {
    if (!isFinite(s) || s < 0) s = 0;
    var m = Math.floor(s / 60), r = Math.floor(s % 60);
    return m + ":" + (r < 10 ? "0" : "") + r;
  }
  function setPlayerMicMute(mute) {
    if (mute === playerMicMuted) return;
    playerMicMuted = mute;
    if (micStream) micStream.getAudioTracks().forEach(function (t) { t.enabled = !mute; });
  }
  function playerShowMsg(text) {
    var el = $("player-msg");
    el.textContent = text;
    el.style.display = text ? "" : "none";
  }
  function renderPlayerList() {
    var list = $("player-list");
    list.innerHTML = "";
    playerItems.forEach(function (it, i) {
      var row = document.createElement("div");
      row.style.cssText = "display:flex; justify-content:space-between; align-items:center;" +
        " padding:9px 12px; border-radius:8px; cursor:pointer; margin-bottom:4px; font-size:17px;" +
        (i === playerIdx ? " background:#e8f5e9; font-weight:700;" : "");
      var left = document.createElement("div");
      left.style.cssText = "min-width:0; overflow:hidden; text-overflow:ellipsis; white-space:nowrap;";
      left.textContent = (i + 1) + ". " + it.title;
      var tag = document.createElement("span");
      tag.style.cssText = "flex-shrink:0; margin-left:8px; font-size:13px; padding:2px 10px;" +
        " border-radius:999px; background:#eef4ff; color:#1565c0;";
      tag.textContent = it.type === "music" ? "音乐" : it.type === "opera" ? "戏曲" : "影视";
      row.appendChild(left);
      row.appendChild(tag);
      row.onclick = function () { playTrackAt(i, true); };
      list.appendChild(row);
    });
  }
  function loadLibrary() {
    if (!deviceToken) { playerShowMsg("请先连接语音（设备 Token）"); return Promise.resolve(); }
    return fetch("/api/v1/media/library", {
      headers: { Authorization: "Bearer " + deviceToken }
    }).then(function (resp) {
      if (!resp.ok) throw new Error("HTTP " + resp.status);
      return resp.json();
    }).then(function (data) {
      playerItems = (data || []).filter(function (m) { return m.media_uri; });
      renderPlayerList();
      if (playerItems.length === 0) {
        playerShowMsg("曲库为空：请先运行 scripts/import_user_media.py 导入 music/yuejv 文件夹的 mp3");
      }
    }).catch(function (err) {
      playerShowMsg("曲库加载失败：" + (err.message || err));
    });
  }
  function openPlayer() {
    $("player-modal").style.display = "flex";
    if (playerItems.length === 0) loadLibrary();
  }
  function playTrackAt(idx, fromUI) {
    if (idx < 0 || idx >= playerItems.length) return;
    playerIdx = idx;
    var it = playerItems[idx];
    $("player-now").textContent = it.title;
    $("player-artist").textContent = it.artist || (it.type === "opera" ? "戏曲" : "音乐");
    var audio = $("player-audio");
    mediaAudio = audio;
    var errored = false; // onerror 与 play() 拒绝可能同源（如 404），只报一次真实原因
    audio.onended = function () {
      if (mediaAudio === audio) mediaAudio = null;
      setPlayerMicMute(false);
      playerToggleBtn.textContent = "▶ 播放";
      if (playerItems.length > 1) playerStep(1); // 播完自动下一首
    };
    audio.onerror = function () {
      errored = true;
      setPlayerMicMute(false);
      playerShowMsg("素材加载失败：" + it.media_uri + "（请运行 scripts/import_user_media.py）");
      appendLog(logReply, "⚠️ 播放失败：" + it.title + "（素材缺失：" + it.media_uri + "）", "e");
      setStatus("播放失败", "err");
    };
    audio.src = it.media_uri;
    audio.play().then(function () {
      playerShowMsg("");
      playerToggleBtn.textContent = "⏸ 暂停";
      // 音乐较大声且持续：播放期间静音麦克，防止被收录成回声转写（暂停/结束恢复）
      setPlayerMicMute(true);
      appendLog(logReply, "🎵 正在播放：" + it.title + "（播放期间麦克风已静音，弹窗暂停后可继续对话）", "r");
      setStatus("正在播放 " + it.title, "ok");
    }).catch(function (err) {
      if (errored) return;
      setPlayerMicMute(false);
      if (err && err.name === "NotAllowedError") {
        playerShowMsg("浏览器拦截了自动播放：请先在页面任意处点击一下，再重试");
      } else {
        playerShowMsg("播放失败：" + ((err && err.name) || "未知错误"));
      }
    });
    renderPlayerList();
    // UI 点击播放上报留痕（voice_rule 语音点播已由后端记录，不重复上报）
    if (fromUI && deviceToken) {
      fetch("/api/v1/media/history", {
        method: "POST",
        headers: { "Content-Type": "application/json", Authorization: "Bearer " + deviceToken },
        body: JSON.stringify({ media_id: it.media_id, duration_seconds: null, source: "ui" })
      }).catch(function () { /* 留痕失败不阻断播放 */ });
    }
  }
  function playerStep(delta) {
    if (playerItems.length === 0) return;
    playTrackAt((playerIdx + delta + playerItems.length) % playerItems.length, true);
  }
  function playerTogglePlay() {
    if (playerIdx < 0) {
      if (playerItems.length) playTrackAt(0, true);
      return;
    }
    var audio = $("player-audio");
    if (audio.paused) audio.play().catch(function () { /* onerror 已报过 */ });
    else { audio.pause(); setPlayerMicMute(false); playerToggleBtn.textContent = "▶ 播放"; }
  }

  // Day7 步8：媒体点播事件 → 打开弹窗并定位播放（原 playMedia 播放器化重写）
  function playMedia(ev) {
    openPlayer();
    var start = function () {
      var idx = -1;
      for (var i = 0; i < playerItems.length; i++) {
        if (playerItems[i].media_id === ev.media_id) { idx = i; break; }
      }
      if (idx >= 0) playTrackAt(idx, false);
      else playerShowMsg("「" + ev.title + "」不在曲库列表中（曲库为空请先运行 import_user_media.py）");
    };
    if (playerItems.length === 0) loadLibrary().then(start);
    else start();
  }

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
    if (audioCtx.resume) audioCtx.resume(); // 自动播放策略可能挂起上下文→onended 永不触发
    var buf = audioCtx.createBuffer(1, item.bytes.length / 2, item.rate);
    var ch = buf.getChannelData(0);
    var view = new DataView(item.bytes.buffer);
    for (var i = 0; i < ch.length; i++) ch[i] = view.getInt16(i * 2, true) / 32768;
    var src = audioCtx.createBufferSource();
    src.buffer = buf;
    // Day6：扬声器出声统一走 GainNode（volume 下行事件可调）；口型桥直连 src
    //（口型能量不随音量变化，调小音量不糊嘴型）
    if (!ttsGain) ttsGain = audioCtx.createGain();
    src.connect(ttsGain);
    ttsGain.connect(audioCtx.destination);
    currentSrc = src;
    // Day4 口型桥：老师引擎吃 WebRTC 远端流（AnalyserNode），本项目 TTS 走本地
    // AudioContext——用 MediaStreamDestination 把播放流喂给引擎（与扬声出声并行）。
    // 引擎未就绪则跳过，不影响出声（降级底线）。
    if (window.KangbanAvatar && KangbanAvatar.ready) {
      if (!avatarDest) {
        avatarDest = audioCtx.createMediaStreamDestination();
        KangbanAvatar.attachStream(avatarDest.stream);
      }
      src.connect(avatarDest);
    }
    // 播放期间静音麦克上行，防止扬声器朗读被麦克风收录成"回声转写"
    // ⚠️ onended 在浏览器上不可靠（上下文挂起时不触发）→ 若只依赖它恢复，
    // 麦克风会永久静音（"后面说话没反应"）。必须加超时兜底，两者先到先用。
    var unmuted = false;
    function unmute() {
      if (unmuted) return;
      unmuted = true;
      if (micStream) micStream.getAudioTracks().forEach(function (t) { t.enabled = true; });
      ttsPlaying = false;
      currentSrc = null;
      drainTts();
      // Day5 优化③：队列播完才回落；回复回合未闭合（如占位语间隙）保持思考中
      if (!ttsPlaying && ttsQueue.length === 0) {
        setAvatarState(replyPending ? "thinking" : restState());
      }
    }
    if (micStream) micStream.getAudioTracks().forEach(function (t) { t.enabled = false; });
    src.onended = unmute;
    setTimeout(unmute, (ch.length / item.rate) * 1000 + 800); // 播放时长+800ms 兜底
    // Day5 viseme：以播放起点为 t0 调度口型时间轴；未就绪/无时间轴时静默回落能量口型
    if (pendingViseme && window.KangbanAvatar && KangbanAvatar.ready) {
      KangbanAvatar.playViseme(pendingViseme);
    }
    pendingViseme = null;
    src.start();
  }

  // ---------- WebRTC 音频上行 ----------
  function startVoice() {
    if (!connected) { setStatus("请先连接语音", "warn"); return; }
    if (pc) {
      // Day7+ 修复「必须点两次才能对话」：通道已死（failed/disconnected/closed）时
      // 自动拆旧重建——一次点击即可恢复；通道健康时新开对话块，不再是无操作提示
      var st = pc.connectionState;
      if (st && st !== "connected" && st !== "new") {
        appendLog(logAsr, "[通道] 语音通道已断开（" + st + "），自动重建…", "e");
        stopVoice();
      } else {
        if (ws && ws.readyState === 1) ws.send(JSON.stringify({ type: "start_session" }));
        setStatus("对话进行中（新对话块已开启），请说话", "ok");
        return;
      }
    }
    navigator.mediaDevices.getUserMedia({
      audio: { echoCancellation: true, noiseSuppression: true, autoGainControl: true }
    }).then(function (stream) {
      micStream = stream;
      pc = new RTCPeerConnection(); // 本地 host 候选；跨机器时由服务端注入 ICE
      stream.getTracks().forEach(function (t) { pc.addTrack(t, stream); });
      // Day7+：通道状态可视化——断链时状态条立刻如实显示，不再假装「聆听中」
      pc.onconnectionstatechange = function () {
        if (!pc) return;
        if (pc.connectionState === "failed" || pc.connectionState === "disconnected") {
          setStatus("语音通道已断开，点击「开始对话」一键恢复", "err");
          appendLog(logAsr, "[通道] 连接异常：" + pc.connectionState, "e");
        }
      };
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
        setStatus("🎤 正在聆听（首句将做声纹验证）…", "warn listening");
        setAvatarState("listening"); // Day5 优化③：上行已建立=聆听中
        // Day6+ 批4：每次点击「开始对话」新开一个对话块（后端收口当前 ACTIVE 块）
        if (ws && ws.readyState === 1) ws.send(JSON.stringify({ type: "start_session" }));
      });
    }).catch(function (err) {
      setStatus("语音启动失败：" + err.message, "err");
      stopVoice();
    });
  }

  function stopVoice() {
    if (pc) { pc.close(); pc = null; }
    if (micStream) { micStream.getTracks().forEach(function (t) { t.stop(); }); micStream = null; }
    replyPending = false;
    setAvatarState("idle"); // Day5 优化③：停止=待机
    setStatus("已停止聆听", "");
  }

  // ---------- 声纹录入（AudioWorklet 采集 2 段 × 5 秒，带倒计时）----------
  // channel: "device"（单人间自动锁定）| "admin"（双人间：管理员指定 elder_id，ADR-005）
  function enroll(channel) {
    if (!deviceToken) { setStatus("请先设备登录", "warn"); return; }
    var adminToken = "", elderId = "";
    if (channel === "admin") {
      adminToken = ($("admin-token") || {}).value || "";
      elderId = (($("admin-elder") || {}).value || "").trim();
      if (!adminToken || !elderId) {
        setStatus("管理员通道：请填写管理员 JWT 和长者 elder_id", "err"); return;
      }
    }
    var SEG_SAMPLES = 48000 * 5; // 每段 5 秒 @48kHz：给慢速朗读留余量
    setStatus("声纹录入：点击后请【立刻开始念】「今天天气真好，我很开心」，持续念满 5 秒（可重复念），共 2 段", "warn");
    navigator.mediaDevices.getUserMedia({ audio: true }).then(function (stream) {
      var ctx = new (window.AudioContext || window.webkitAudioContext)();
      var src = null; // 提升到外层作用域：后面的 .then 链也要用（src.disconnect）
      return ctx.audioWorklet.addModule("/js/audio-worklet.js").then(function () {
        src = ctx.createMediaStreamSource(stream);
        var node = new AudioWorkletNode(ctx, "capture-processor");
        src.connect(node); // 不连 destination，避免回放啸叫
        var samples = [];
        var segments = [];
        var lastRemain = -1; // 倒计时去重：只在整秒变化时刷新状态条
        return new Promise(function (resolve) {
          node.port.onmessage = function (e) {
            samples.push(e.data);
            var total = samples.reduce(function (s, a) { return s + a.length; }, 0);
            if (total >= SEG_SAMPLES) {
              segments.push(downsampleToInt16(flatten(samples), ctx.sampleRate));
              samples = [];
              lastRemain = -1;
              if (segments.length >= 2) { resolve({ segments: segments, ctx: ctx, stream: stream }); }
              else setStatus("✅ 第 1 段完成！请再念一遍「今天天气真好，我很开心」", "warn");
            } else {
              var remain = Math.ceil((SEG_SAMPLES - total) / 48000);
              if (remain !== lastRemain) {
                lastRemain = remain;
                setStatus("第 " + (segments.length + 1) + " 段录制中，请持续念… 剩 " + remain + " 秒", "warn");
              }
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
        var url, headers;
        if (channel === "admin") {
          url = "/api/v1/voice/enroll/admin";
          headers = { "Content-Type": "application/json", Authorization: "Bearer " + adminToken };
          body.elder_id = elderId;
        } else {
          url = "/api/v1/voice/enroll/device";
          headers = { "Content-Type": "application/json", Authorization: "Bearer " + deviceToken };
        }
        setStatus("声纹上传中…", "warn");
        return fetch(url, {
          method: "POST",
          headers: headers,
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

  // ---------- 对话记录（Day6+ 批4：左侧固定面板，分层对话块结构）----------
  // 可见性铁律在服务端：elder 由设备 Token 定位活跃会话且必须已声纹验证，
  // 未验证/会话不存在一律 403——面板显隐只是体验层，不是安全边界。
  var historyUnlocked = false;

  function unlockHistory() {
    if (historyUnlocked) { loadHistory(); return; }
    historyUnlocked = true;
    $("history-locked").style.display = "none";
    $("history-list").style.display = "";
    $("history-identity").style.display = "";
    loadHistory();
  }

  function fmtBj(iso) { return (iso || "").replace("T", " ").slice(5, 16); }

  function loadHistory() {
    if (!deviceToken || !historyUnlocked) return;
    fetch("/api/v1/voice/conversations/recent?limit=20", {
      headers: { Authorization: "Bearer " + deviceToken }
    }).then(function (resp) {
      if (resp.status === 403) throw new Error("声纹未验证或会话已结束");
      if (!resp.ok) throw new Error("加载失败（HTTP " + resp.status + "）");
      return resp.json();
    }).then(function (data) {
      $("history-identity").textContent = "当前识别到：" + (data.elder_name || "未知长者");
      var list = $("history-list");
      var blocks = data.blocks || [];
      if (!blocks.length) {
        list.innerHTML = "<div style='text-align:center;color:#999;padding:20px 8px;font-size:18px;'>还没有对话块，点「开始对话」说句话吧</div>";
        return;
      }
      // 分层渲染（时间倒序，最新块/轮在最前）：对话块（块ID+块序+工单标记）→ 子对话（轮ID）
      // 块头点击可收起/展开（Day7+ 2026-11-10 用户反馈：默认收起，仅最新一块展开）
      list.innerHTML = blocks.map(function (b, i) {
        var wo = b.has_workorder ? " <span style='color:#c62828;font-size:14px;'>🚨 已创建工单</span>" : "";
        var rounds = (b.rounds || []).map(function (r) {
          var rwo = r.has_workorder ? " <span style='color:#c62828;' title='本轮创建了工单'>🚨</span>" : "";
          return "<div style='border-top:1px dashed #ddd;padding:8px 2px;'>" +
            "<div style='font-size:13px;color:#999;'>子对话 " + KB.esc(r.round_id || "") + " · " + fmtBj(r.asked_at) + rwo + "</div>" +
            "<div style='font-size:19px;font-weight:700;margin-top:2px;'>👴 " + KB.esc(r.question || "") + "</div>" +
            "<div style='font-size:19px;color:#1565c0;margin-top:2px;'>🤖 " + KB.esc(r.answer || "（未回答）") + "</div>" +
            "</div>";
        }).join("");
        var collapsed = i > 0; // 时间倒序：仅第 0 块（最新）默认展开
        return "<div style='border:1px solid #e3e9f5;border-radius:10px;padding:8px 10px;margin-bottom:10px;'>" +
          "<div class='blk-head' style='font-weight:600;color:#444;font-size:15px;cursor:pointer;user-select:none;'>" +
          "<span class='blk-arrow' style='display:inline-block;width:14px;color:#888;'>" + (collapsed ? "▸" : "▾") + "</span> 对话块 " +
          (b.block_no != null ? "#" + KB.esc(String(b.block_no)) : "") +
          " · " + fmtBj(b.started_at) + wo +
          "<span style='font-size:12px;color:#bbb;font-weight:400;'>（块ID " + KB.esc(b.block_id || "") +
          " · " + (b.rounds || []).length + " 轮 · 点击块头收起/展开）</span></div>" +
          "<div class='blk-rounds'" + (collapsed ? " style='display:none;'" : "") + ">" + rounds + "</div>" +
          "</div>";
      }).join("");
      // 块头点击切换收起/展开
      Array.prototype.forEach.call(list.querySelectorAll(".blk-head"), function (h) {
        h.addEventListener("click", function () {
          var body = h.nextElementSibling;
          if (!body) return;
          var open = body.style.display !== "none";
          body.style.display = open ? "none" : "";
          var arrow = h.querySelector(".blk-arrow");
          if (arrow) arrow.textContent = open ? "▸" : "▾";
        });
      });
    }).catch(function (e) {
      $("history-list").innerHTML = "<div style='color:#c62828;text-align:center;padding:12px;font-size:16px;'>" +
        KB.esc(e.message || String(e)) + "</div>";
    });
  }

  // ---------- 排版升级步骤 3：对话记录折叠抽屉（D1）+ 比例样张 ----------
  // 抽屉默认收起仅显示抽屉头，点击开合；展开时共享右区下半（voice-stage.css）。
  // 数据/鉴权逻辑零改动：loadHistory 仍由 unlockHistory/done/刷新按钮触发。
  var drawerPanel = $("history-panel");
  var drawerHead = $("history-drawer-head");
  if (drawerPanel && drawerHead) {
    drawerHead.addEventListener("click", function () {
      var open = drawerPanel.classList.toggle("open");
      drawerHead.setAttribute("aria-expanded", open ? "true" : "false");
      var arrow = drawerHead.querySelector(".stage-drawer-arrow");
      if (arrow) arrow.textContent = open ? "▾" : "▸";
      // 展开时若已解锁且无数据，补一次加载（锁定占位不受影响）
      if (open && historyUnlocked && !$("history-list").hasChildNodes()) loadHistory();
    });
  }

  // 左右比例样张（拍板用，拍板后写死删除）：?ratio=6040 → 60:40 版，默认 55:45
  var ratioParam = new URLSearchParams(location.search).get("ratio");
  if (ratioParam === "6040") document.body.classList.add("conv-ratio-6040");

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
  $("btn-enroll").addEventListener("click", function () { enroll("device"); });
  $("btn-enroll-admin").addEventListener("click", function () { enroll("admin"); });
  // ---------- 曲库播放器控件（Day7+）----------
  $("btn-player").addEventListener("click", openPlayer);
  $("player-close").addEventListener("click", function () {
    $("player-modal").style.display = "none"; // 关闭弹窗不停播
  });
  $("player-prev").addEventListener("click", function () { playerStep(-1); });
  $("player-next").addEventListener("click", function () { playerStep(1); });
  playerToggleBtn.addEventListener("click", playerTogglePlay);
  var playerSeek = $("player-seek");
  var playerAudioEl = $("player-audio");
  playerAudioEl.addEventListener("timeupdate", function () {
    if (playerSeeking || !isFinite(playerAudioEl.duration)) return;
    playerSeek.value = Math.round((playerAudioEl.currentTime / playerAudioEl.duration) * 1000);
    $("player-cur").textContent = fmtTime(playerAudioEl.currentTime);
    $("player-dur").textContent = fmtTime(playerAudioEl.duration);
  });
  playerSeek.addEventListener("input", function () {
    playerSeeking = true;
    if (isFinite(playerAudioEl.duration)) {
      $("player-cur").textContent = fmtTime((playerSeek.value / 1000) * playerAudioEl.duration);
    }
  });
  playerSeek.addEventListener("change", function () {
    if (isFinite(playerAudioEl.duration)) {
      playerAudioEl.currentTime = (playerSeek.value / 1000) * playerAudioEl.duration;
    }
    playerSeeking = false;
  });
  playerAudioEl.addEventListener("pause", function () {
    setPlayerMicMute(false);
    playerToggleBtn.textContent = "▶ 播放";
  });
  playerAudioEl.addEventListener("play", function () {
    playerToggleBtn.textContent = "⏸ 暂停";
  });
  $("btn-history-refresh").addEventListener("click", function () {
    if (!historyUnlocked) { setStatus("声纹验证通过后才能查看对话记录", "warn"); return; }
    loadHistory();
  });
  $("btn-text-send").addEventListener("click", function () {
    var v = $("text-input").value.trim();
    if (!v) return;
    if (!ws || ws.readyState !== 1) {
      appendLog($("log-text"), "（文字）未连接，请先完成登录", "w");
      return;
    }
    if (v.length > 500) {
      appendLog($("log-text"), "（文字）内容超过 500 字，请精简后重试", "w");
      return;
    }
    // Day5：文字通道与语音轮同一管线（LLM → 动作 → TTS → 落库）
    ws.send(JSON.stringify({ type: "text", text: v }));
    appendLog($("log-text"), "（文字）" + v, "r");
    $("text-input").value = "";
  });
  $("text-input").addEventListener("keydown", function (e) {
    if (e.key === "Enter") $("btn-text-send").click();
  });
})();
