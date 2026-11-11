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
      document.body.classList.remove("ws-connected"); // 排版升级：连接断开→隐藏对话记录条
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
        document.body.classList.add("ws-connected"); // 排版升级：连接成功→展示对话记录条
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
        renderWeatherMini(ev.data); // 排版升级步骤 5：左下弹出天气小模块（形态 B）
        break;
      case "medication_reminder":
        // Day7 步7：定时用药提醒下行（D1 口径：元数据先到，TTS 音频经既有 tts
        // 事件自然跟进播报——数字人开口）；仅追加展示分支，不碰既有链路
        appendLog(logReply, "💊 " + (ev.text || (ev.drug_name + " " + (ev.dose || ""))), "r");
        setStatus(ev.slot + "用药提醒：" + ev.drug_name, "ok");
        break;
      case "health_checkin":
        // Day8 步4：晚间健康外呼下行（逐问 TTS 自然跟进；答案随 ASR 上行走采集状态机）
        appendLog(logReply, "🩺 " + (ev.text || ""), "r");
        setStatus("晚间健康询问：" + ((ev.questions || []).length) + " 问", "ok");
        break;
      case "media_play":
        // Day7 步8：戏曲/影视点播（版权授权过滤在后端；voice_rule 已服务端留痕）
        playMedia(ev);
        renderMusicMini(ev); // 步骤 6：左下音乐迷你条同步弹出（与弹窗双向同步）
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
    musicMiniSyncTrack(it); // 步骤 6：迷你条曲名/进度同步（与弹窗双向）
    var audio = $("player-audio");
    mediaAudio = audio;
    var errored = false; // onerror 与 play() 拒绝可能同源（如 404），只报一次真实原因
    audio.onended = function () {
      if (mediaAudio === audio) mediaAudio = null;
      setPlayerMicMute(false);
      playerToggleBtn.textContent = "▶ 播放";
      // 用户拍板（2026-11-11 三轮）：默认一首播完=停播+收左下迷你条；
      // 最后 15s 警告里点过「连续播放」（会话级）才自动切下一首
      if (miniAutoContinue && playerItems.length > 1) {
        playerStep(1); // 播完自动下一首
      } else {
        miniEndClose();
      }
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

  // Day7 步8：媒体点播事件 → 定位播放。用户拍板（2026-11-11 二轮）：
  // 语音点播不再自动弹曲库弹窗，直接由左下音乐迷你条承接；播放列表只在
  // 点「🎵 曲库」按钮或点迷你条本体时打开（openPlayer 独立入口）
  function playMedia(ev) {
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

  // ---------- 排版升级步骤 4：形态转换编排（A↔B）----------
  // 形态 B：3D 数字人丝滑缩小至左上小格 + 左下弹出模块同时入场（stagger rise）；
  // 收起反向恢复。动画纪律：只动 transform/opacity（FLIP 技法，单一缓动曲线
  // 读 voice-stage.css --stage-curve，拍板定稿：弹性回弹）；动效开关 off
  // （motion.js 统一写 html[data-motion]）→ 直接切换布局无动画。
  // 布局切换与 WS/音频链路完全解耦（只动视觉容器，不碰任何业务元素）。
  var stageBottom = $("stage-bottom");

  function stageMotionOn() {
    return document.documentElement.getAttribute("data-motion") !== "off";
  }
  function stageCurveParams() {
    var cs = getComputedStyle(document.body);
    return {
      dur: parseFloat(cs.getPropertyValue("--stage-dur")) || 0.45,
      curve: (cs.getPropertyValue("--stage-curve") || "").trim() || "ease-out"
    };
  }
  // FLIP：First 记录旧位 → 切布局 → Last 测新位 → Invert 反演 → Play 归位。
  // 只改 transform/transition（合成层），canvas 尺寸变化由引擎 ResizeObserver 重适配。
  function stageFlip(card, applyLayout) {
    var first = card.getBoundingClientRect();
    applyLayout();
    var last = card.getBoundingClientRect();
    var dx = first.left - last.left, dy = first.top - last.top;
    var sx = first.width / last.width, sy = first.height / last.height;
    var p = stageCurveParams();
    card.style.transition = "none";
    card.style.transformOrigin = "top left";
    card.style.transform = "translate(" + dx + "px," + dy + "px) scale(" + sx + "," + sy + ")";
    void card.offsetWidth; // 强制回流，确保起始帧生效
    card.style.transition = "transform " + p.dur + "s " + p.curve;
    card.style.transform = "";
    setTimeout(function () {
      card.style.transition = "";
      card.style.transformOrigin = "";
    }, p.dur * 1000 + 60);
  }
  function setStageForm(toB) {
    if (toB === document.body.classList.contains("stage-b")) return;
    var avatar = $("avatar-card");
    if (!stageMotionOn()) { // 动效 off：无动画直接切换
      document.body.classList.toggle("stage-b", toB);
      stageBottom.style.display = toB ? "" : "none";
      return;
    }
    if (toB) {
      stageBottom.classList.remove("stage-bottom-leaving");
      stageFlip(avatar, function () {
        stageBottom.style.display = "";       // rise 动画由 CSS 类驱动（0.12s stagger）
        document.body.classList.add("stage-b");
      });
    } else {
      // 退场轻微衔接：模块显隐依赖 .stage-b，摘类即 display:none 会掐断 sink
      // 动画——先让模块在原位下沉退场（0.3s），再 FLIP 恢复 3D 大形态
      stageBottom.style.display = "block";
      stageBottom.classList.add("stage-bottom-leaving");
      setTimeout(function () {
        stageFlip(avatar, function () {
          document.body.classList.remove("stage-b");
        });
        setTimeout(function () {
          stageBottom.style.display = "";
          stageBottom.classList.remove("stage-bottom-leaving");
        }, stageCurveParams().dur * 1000 + 60);
      }, 300);
    }
  }
  // 步骤 5/6 的触发入口：weather_result → enter()；收起/播完 → exit()
  window.KBStage = {
    enter: function () { setStageForm(true); },
    exit: function () { setStageForm(false); },
    isB: function () { return document.body.classList.contains("stage-b"); }
  };

  // ---------- 排版升级步骤 5：天气小模块（左下弹出，形态 B）----------
  // 触发：WS weather 事件（复用既有）→ KBStage.enter() 进形态 B + 渲染小模块。
  // 数据口径：ev.data 与 GET /api/v1/weather/now 同构（city/temp_c/high_c/low_c/
  // summary/hourly[8点]/degraded…）。小模块：右上角日期时间（每 30s 刷新）+
  // 2 分钟自动收起倒计时（最后 30s 高亮，D3）+ × 手动收起 + 点击放大。
  // 放大视图：今天/明天/后天 tab（后两天按需拉 /weather/now，设备 Token）+
  // 逐 3h 温度 SVG 双线（实际/体感，hover 数值）+ 湿度/风速/降水概率 + 三天预报。
  var weatherMini = $("weather-mini");
  var musicMini = $("music-mini");
  var wxCountdownEl = $("wx-countdown");
  var wxTimer = null;       // 2 分钟自动收起倒计时（D3）
  var wxRemain = 120;
  var wxDayData = { today: null, tomorrow: null, day_after: null }; // 放大视图缓存

  function weatherSkyType(summary) {
    var s = summary || "";
    if (s.indexOf("雨") >= 0 || s.indexOf("雷") >= 0) return "sky-rain";
    if (s.indexOf("雪") >= 0) return "sky-snow";
    if (s.indexOf("晴") >= 0) return "sky-sunny";
    return "sky-overcast"; // 阴/多云/雾 → 云层缓移
  }

  function weatherIcon(code) {
    if (code === 0 || code === 1) return "☀️";
    if (code === 2) return "⛅";
    if (code === 3 || code === 45 || code === 48) return "☁️";
    if ((code >= 71 && code <= 77) || code === 85 || code === 86) return "❄️";
    if (code >= 95) return "⛈️";
    if (code >= 51) return "🌧️";
    return "☁️";
  }

  // 右上角「当前查询时间：11月11日 14:32」——每次查询渲染时刻静态标注，
  // 不做分钟跳动（语义=本次查询发生的时间，用户拍板文案）
  function wxTick() {
    if (weatherMini.style.display === "none") return;
    var d = new Date();
    $("weather-datetime").textContent =
      "当前查询时间：" + (d.getMonth() + 1) + "月" + d.getDate() + "日 " +
      ("0" + d.getHours()).slice(-2) + ":" + ("0" + d.getMinutes()).slice(-2);
  }

  // 2 分钟自动收起（D3）：倒计时可见、最后 30s 高亮；再问天气重新弹出即重置
  function wxCountdownStart() {
    wxCountdownStop();
    wxRemain = 120;
    wxCountdownRender();
    wxTimer = setInterval(function () {
      wxRemain -= 1;
      wxCountdownRender();
      if (wxRemain <= 0) wxCollapse();
    }, 1000);
  }
  function wxCountdownStop() {
    if (wxTimer) { clearInterval(wxTimer); wxTimer = null; }
  }
  function wxCountdownRender() {
    var m = Math.floor(wxRemain / 60), s = wxRemain % 60;
    wxCountdownEl.textContent =
      "倒计时：" + ("0" + m).slice(-2) + ":" + ("0" + s).slice(-2) +
      "后消失，可再次询问天气召出";
    wxCountdownEl.classList.toggle("wx-urgent", wxRemain <= 30);
  }
  // 双模块并存仲裁（用户拍板 2026-11-11）：天气与音乐同时激活时，左下模块
  // 切割成等比等宽的两块分别展示；任一激活即保持形态 B，全部收起才退回形态 A
  function updateStageModules() {
    var w = weatherMini.style.display !== "none";
    var m = musicMini.style.display !== "none";
    stageBottom.classList.toggle("split", w && m);
    if (w || m) {
      $("stage-bottom-placeholder").style.display = "none";
      window.KBStage.enter();
    } else {
      $("stage-bottom-placeholder").style.display = "";
      window.KBStage.exit();
    }
  }

  // 收起（到点/×）：只收天气模块——音乐迷你条若在展示则保持形态 B
  function wxCollapse() {
    wxCountdownStop();
    weatherMini.style.display = "none";
    updateStageModules();
  }

  function renderWeatherMini(data) {
    if (!data) return;
    wxDayData.today = data; // 放大视图「今天」初始缓存（与语音播报同源）
    // 天空态切换（同态不摘类，动画不闪断）
    var sky = weatherSkyType(data.summary);
    if (!weatherMini.classList.contains(sky)) {
      weatherMini.classList.remove("sky-sunny", "sky-rain", "sky-snow", "sky-overcast");
      weatherMini.classList.add(sky);
    }
    $("weather-city").textContent = data.city || "—";
    $("weather-hl").textContent = "最高温/最低温：" +
      (data.high_c != null ? Math.round(data.high_c) : "—") + "℃ / " +
      (data.low_c != null ? Math.round(data.low_c) : "—") + "℃";
    $("weather-temp").textContent =
      data.temp_c != null ? Math.round(data.temp_c) + "°" : "—";
    $("weather-desc").textContent = data.summary || "—";
    $("weather-mock").style.display = data.degraded ? "" : "none";
    // 底排逐 3h 预报：取前 5 点（时间/图标/温度）
    var hourlyEl = $("weather-hourly");
    hourlyEl.innerHTML = "";
    var pts = (data.hourly || []).slice(0, 5);
    pts.forEach(function (p) {
      var col = document.createElement("div");
      col.className = "wh-col";
      var t = document.createElement("span");
      t.textContent = (p.time || "").length >= 13 ? p.time.slice(11, 13) + "时" : "—";
      var icon = document.createElement("span");
      icon.className = "wh-icon";
      icon.textContent = weatherIcon(p.code);
      var v = document.createElement("span");
      v.className = "wh-temp";
      v.textContent = p.temp_c != null ? Math.round(p.temp_c) + "°" : "—";
      col.appendChild(t);
      col.appendChild(icon);
      col.appendChild(v);
      hourlyEl.appendChild(col);
    });
    hourlyEl.style.display = pts.length ? "" : "none";
    // 占位让位 + 进形态 B（3D 缩小与模块弹出同时编排，动效 off 直切）+ 重置倒计时；
    // 天气+音乐并存时自动切分割布局（updateStageModules），无音乐则整块
    $("stage-bottom-placeholder").style.display = "none";
    weatherMini.style.display = "";
    wxTick();
    updateStageModules();
    wxCountdownStart();
  }

  // ---------- 放大视图（点击小模块弹出）：昨天/今天/明天/后天 tab + SVG 双线曲线 ----------
  var WX_TABS = [["yesterday", "昨天"], ["today", "今天"], ["tomorrow", "明天"], ["day_after", "后天"]];
  var wxModalDay = "today";
  var wxFetchErr = ""; // 最近一次拉取失败的具体原因（可见降级，不静默）

  function wxFetchDay(date) {
    if (wxDayData[date]) return Promise.resolve(wxDayData[date]);
    if (!deviceToken) {
      wxFetchErr = "尚未连接语音设备（无设备凭证），无法查询其他日期；请先在右侧登录连接";
      return Promise.resolve(null);
    }
    wxFetchErr = "";
    return fetch("/api/v1/weather/now?date=" + date, {
      headers: { Authorization: "Bearer " + deviceToken }
    }).then(function (resp) {
      if (!resp.ok) throw new Error("HTTP " + resp.status);
      return resp.json();
    }).then(function (data) {
      wxDayData[date] = data;
      return data;
    }).catch(function (err) {
      wxFetchErr = "该日天气拉取失败（" + (err && err.message ? err.message : "网络异常") + "），请稍后重试";
      return null; // 拉取失败：弹窗内可见提示，不阻塞已缓存的天
    });
  }

  function wxModalMsg(text) {
    var el = $("wx-modal-msg");
    if (!text) { el.style.display = "none"; return; }
    el.textContent = text;
    el.style.display = "";
  }

  // 逐 3h 温度曲线（纯 SVG 手写，禁 Chart.js）：实际温度 + 体感双线，
  // 每点带 <title> hover 数值；体感缺失的点如实跳过连线断口
  function wxCurveSvg(data) {
    var pts = (data.hourly || []).filter(function (p) { return p.temp_c != null; });
    if (pts.length < 2) return "";
    var W = 640, H = 230, L = 44, R = 16, T = 26, B = 40; // 四边留白（轴/标签）
    var vals = [];
    pts.forEach(function (p) {
      if (p.temp_c != null) vals.push(p.temp_c);
      if (p.apparent_c != null) vals.push(p.apparent_c);
    });
    var lo = Math.min.apply(null, vals), hi = Math.max.apply(null, vals);
    if (hi - lo < 4) { hi += 2; lo -= 2; } // 温差过小平线会贴边，扩窗
    var span = hi - lo;
    function x(i) { return L + (W - L - R) * i / (pts.length - 1); }
    function y(v) { return T + (H - T - B) * (1 - (v - lo) / span); }
    var svg = ['<svg class="wx-curve" viewBox="0 0 ' + W + " " + H + '" role="img" aria-label="逐小时温度曲线">'];
    // 网格基线 + y 轴刻度（两行）
    [lo, (lo + hi) / 2, hi].forEach(function (v) {
      svg.push('<line x1="' + L + '" y1="' + y(v) + '" x2="' + (W - R) + '" y2="' + y(v) +
        '" stroke="rgba(127,118,104,.2)" stroke-width="1"/>');
      svg.push('<text x="' + (L - 6) + '" y="' + (y(v) + 4) + '" font-size="12" fill="var(--ink-faint,#7b7166)" text-anchor="end">' + Math.round(v) + '°</text>');
    });
    // x 轴时刻标签
    pts.forEach(function (p, i) {
      svg.push('<text x="' + x(i) + '" y="' + (H - B + 18) + '" font-size="12" fill="var(--ink-faint,#7b7166)" text-anchor="middle">' +
        ((p.time || "").slice(11, 13) || "—") + '时</text>');
    });
    // 双线（体感=虚线灰）
    [["apparent_c", "rgba(127,118,104,.75)", "6 4", "体感"], ["temp_c", "var(--clay,#b4653f)", "", "实际"]].forEach(function (cfg) {
      var key = cfg[0], seg = [];
      pts.forEach(function (p, i) {
        if (p[key] != null) seg.push(x(i) + "," + y(p[key]));
      });
      if (seg.length > 1) {
        svg.push('<polyline points="' + seg.join(" ") + '" fill="none" stroke="' + cfg[1] +
          '" stroke-width="2.5"' + (cfg[2] ? ' stroke-dasharray="' + cfg[2] + '"' : "") + ' stroke-linecap="round"/>');
      }
      pts.forEach(function (p, i) {
        if (p[key] == null) return;
        svg.push('<circle cx="' + x(i) + '" cy="' + y(p[key]) + '" r="4" fill="' + cfg[1] + '"><title>' +
          (p.time || "").slice(11, 13) + "时 " + cfg[3] + " " + Math.round(p[key]) + "°" +
          (key === "temp_c" && p.apparent_c != null ? "（体感 " + Math.round(p.apparent_c) + "°）" : "") +
          (p.precip_prob != null ? " · 降水概率 " + p.precip_prob + "%" : "") + "</title></circle>");
      });
    });
    // 图例
    svg.push('<line x1="' + L + '" y1="10" x2="' + (L + 26) + '" y2="10" stroke="var(--clay,#b4653f)" stroke-width="2.5"/>');
    svg.push('<text x="' + (L + 32) + '" y="14" font-size="12" fill="var(--ink-soft,#6b6459)">实际温度</text>');
    svg.push('<line x1="' + (L + 110) + '" y1="10" x2="' + (L + 136) + '" y2="10" stroke="rgba(127,118,104,.75)" stroke-width="2.5" stroke-dasharray="6 4"/>');
    svg.push('<text x="' + (L + 142) + '" y="14" font-size="12" fill="var(--ink-soft,#6b6459)">体感温度</text>');
    svg.push("</svg>");
    return svg.join("");
  }

  function wxCell(label, value) {
    return '<div class="wx-cell">' + label + "<b>" + value + "</b></div>";
  }

  function wxRenderModal() {
    var data = wxDayData[wxModalDay];
    var body = $("wx-modal-body");
    var html = "";
    if (data) {
      wxModalMsg(data.note || ""); // mock 降级可见标注透传
      $("wx-modal-city").textContent = (data.date_label || "") + " · " + (data.city || "—");
      var isToday = wxModalDay === "today";
      var wind = data.wind_kmh != null
        ? Math.round(data.wind_kmh) + " km/h " + (data.wind_dir || "") : "—";
      html =
        '<div class="wx-nowgrid">' +
        wxCell(isToday ? "当前温度" : "平均温度",
          data.temp_c != null ? Math.round(data.temp_c) + "°" : "—") +
        wxCell(isToday ? "体感" : "体感（14时）",
          data.apparent_c != null ? Math.round(data.apparent_c) + "°" : "—") +
        wxCell(isToday ? "湿度" : "平均湿度",
          data.humidity != null ? data.humidity + "%" : "—") +
        wxCell(isToday ? "风速风向" : "风速（14时）", wind) +
        wxCell("降水概率", (data.hourly || []).some(function (p) { return p.precip_prob != null; })
          ? Math.max.apply(null, (data.hourly || []).map(function (p) { return p.precip_prob || 0; })) + "%" : "—") +
        wxCell(isToday ? "当前降水量" : "全天降水量",
          data.precipitation_mm != null ? data.precipitation_mm + " mm" : "—") +
        wxCell("最高温", data.high_c != null ? Math.round(data.high_c) + "℃" : "—") +
        wxCell("最低温", data.low_c != null ? Math.round(data.low_c) + "℃" : "—") +
        "</div>" + wxCurveSvg(data);
    } else {
      wxModalMsg(wxFetchErr || "该日天气数据暂不可用，请稍后重试");
    }
    // 四天预报摘要（昨天/今天/明天/后天；有数据才渲染，拿不到的天如实显示「—」）
    html += '<div class="wx-days">';
    WX_TABS.forEach(function (pair) {
      var d = wxDayData[pair[0]];
      html += '<div class="wx-day"><div>' + pair[1] + "</div>" +
        '<div class="wd-icon">' + (d ? weatherIcon((d.hourly || [])[0] ? d.hourly[0].code : 3) : "—") + "</div>" +
        "<div>" + (d ? d.summary : "—") + "</div>" +
        '<div class="wd-hl">' + (d && d.high_c != null ? Math.round(d.high_c) + "° / " + Math.round(d.low_c) + "°" : "—") + "</div></div>";
    });
    html += "</div>";
    body.innerHTML = html;
  }

  function wxRenderTabs() {
    var tabs = $("wx-modal-daytabs");
    tabs.innerHTML = "";
    tabs.className = "wx-tabs";
    WX_TABS.forEach(function (pair) {
      var b = document.createElement("button");
      b.type = "button";
      b.className = "wx-tab" + (pair[0] === wxModalDay ? " active" : "");
      b.textContent = pair[1];
      b.onclick = function () {
        wxModalDay = pair[0];
        wxRenderTabs();
        if (wxDayData[pair[0]]) { wxRenderModal(); }
        else {
          $("wx-modal-body").innerHTML = "";
          wxModalMsg("加载中…");
          wxFetchDay(pair[0]).then(wxRenderModal);
        }
      };
      tabs.appendChild(b);
    });
  }

  function wxModalOpen() {
    wxModalDay = "today";
    $("weather-modal").style.display = "flex";
    wxCountdownStop(); // 放大期间暂停自动收起，关闭弹窗后重新计时
    wxRenderTabs();
    wxFetchDay("tomorrow");
    wxFetchDay("day_after").then(function () {
      if ($("weather-modal").style.display !== "none") wxRenderModal(); // 补渲染三天摘要
    });
    wxRenderModal();
  }
  function wxModalClose() {
    $("weather-modal").style.display = "none";
    if (weatherMini.style.display !== "none") wxCountdownStart(); // 收起弹窗恢复倒计时
  }

  // 天气模块交互绑定：×收起（阻止冒泡避免误触放大）、点卡放大、弹窗关闭
  $("wx-close").addEventListener("click", function (e) {
    e.stopPropagation();
    wxCollapse();
  });
  weatherMini.addEventListener("click", wxModalOpen);
  $("wx-modal-close").addEventListener("click", wxModalClose);
  $("weather-modal").addEventListener("click", function (e) {
    if (e.target === this) wxModalClose(); // 点遮罩关闭
  });

  // ---------- 音乐迷你条（步骤 6，用户拍板 2026-11-11）----------
  // 语音点播（media_play）弹出；与天气双模块并存时左下等比等宽分割两块
  // （updateStageModules）。曲名/进度/暂停态与曲库播放器弹窗双向同步：
  // 进度与播放态由 <audio> 事件驱动（零轮询），点播走 playerStep/
  // playerTogglePlay 同一函数；×只收模块不停播（与弹窗「关闭不停播」同口径）。
  function renderMusicMini(ev) {
    $("music-mini-title").textContent = ev.title || "—";
    $("music-mini-fill").style.width = "0%";
    $("music-mini-cur").textContent = "0:00";
    $("music-mini-dur").textContent = "0:00";
    $("music-mini-toggle").textContent = "⏸";
    miniAutoContinue = false; // 新语音点播=新一轮收尾选择（重置连播会话）
    miniEndWarnHide();
    musicMini.style.display = "";
    updateStageModules();
  }

  // playTrackAt 每次定位曲目时同步曲名/进度（弹窗手动点播、自动下一首同源）
  function musicMiniSyncTrack(it) {
    if (musicMini.style.display === "none" || !it) return;
    $("music-mini-title").textContent = it.title || "—";
    $("music-mini-fill").style.width = "0%";
    $("music-mini-cur").textContent = "0:00";
  }

  var miniAudio = $("player-audio");
  miniAudio.addEventListener("timeupdate", function () {
    if (musicMini.style.display === "none") return;
    if (isFinite(miniAudio.duration) && miniAudio.duration > 0) {
      $("music-mini-fill").style.width =
        Math.round((miniAudio.currentTime / miniAudio.duration) * 100) + "%";
      // 最后 15s 收尾警告（用户拍板 2026-11-11 三轮）：已选连播不提示；
      // 时长 ≤20s 的短素材警告无意义，也不提示
      var remain = miniAudio.duration - miniAudio.currentTime;
      if (!miniAutoContinue && remain > 0 && remain <= 15 && miniAudio.duration > 20) {
        miniEndWarnText.textContent = "⏱ " + Math.ceil(remain) + "s 后将关闭音乐模块";
        miniEndWarn.style.display = "";
      } else {
        miniEndWarnHide();
      }
    }
    $("music-mini-cur").textContent = fmtTime(miniAudio.currentTime);
  });
  miniAudio.addEventListener("loadedmetadata", function () {
    if (musicMini.style.display !== "none") {
      $("music-mini-dur").textContent = fmtTime(miniAudio.duration);
    }
  });
  function miniSyncPlayState() {
    if (musicMini.style.display === "none") return;
    $("music-mini-toggle").textContent = miniAudio.paused ? "▶" : "⏸";
    if (!miniAudio.paused) {
      $("music-mini-dur").textContent = fmtTime(miniAudio.duration);
    }
  }
  miniAudio.addEventListener("play", miniSyncPlayState);
  miniAudio.addEventListener("pause", miniSyncPlayState);

  $("music-mini-toggle").addEventListener("click", function (e) {
    e.stopPropagation(); // 防误触天气放大（双模块分割时相邻）
    playerTogglePlay();  // 与弹窗同一播放/暂停入口（双向同步）
  });
  $("music-mini-prev").addEventListener("click", function (e) {
    e.stopPropagation();
    playerStep(-1);
  });
  $("music-mini-next").addEventListener("click", function (e) {
    e.stopPropagation();
    playerStep(1);
  });
  $("music-mini-close").addEventListener("click", function (e) {
    e.stopPropagation();
    musicMini.style.display = "none";
    miniAutoContinue = false; // 手动收窗=结束连播会话（用户拍板口径）
    miniEndWarnHide();
    updateStageModules(); // 音乐继续播（不停播），仅收模块；全部收起才退形态 A
  });

  // ---------- 播完收尾（用户拍板 2026-11-11 三轮）：默认一首播完=停播+收窗；
  // 最后 15s 迷你条内出现倒计时警告，「连续播放」=本次会话一直连播
  // （直到手动收窗 / 新语音点播重置）。详见 playTrackAt 的 onended 分流 ----------
  var miniEndWarn = $("music-endwarn");
  var miniEndWarnText = $("music-endwarn-text");
  var miniAutoContinue = false;

  function miniEndWarnHide() { miniEndWarn.style.display = "none"; }

  $("music-endwarn-keep").addEventListener("click", function (e) {
    e.stopPropagation(); // 防误触「点卡开曲库」
    miniAutoContinue = true;
    miniEndWarnHide();
    setStatus("已选择连续播放，播完自动下一首", "ok");
  });

  function miniEndClose() { // 一首播完（未选连播）：停播 + 收迷你条
    miniEndWarnHide();
    musicMini.style.display = "none";
    updateStageModules(); // 天气还开着则保持形态 B，否则退回形态 A
  }

  // 用户拍板（2026-11-11 二轮）：点迷你条本体 = 打开曲库播放列表
  //（内部按钮均已 stopPropagation，不会冒泡误触）
  musicMini.addEventListener("click", function () {
    openPlayer();
  });

  // 迷你条进度条点击跳播（命中区含上下 7px 扩展，见 voice-stage.css ::before）；
  // stopPropagation 防误触「点卡开曲库」；时长未知时忽略
  var miniBar = musicMini.querySelector(".music-bar");
  miniBar.addEventListener("click", function (e) {
    e.stopPropagation();
    if (!isFinite(miniAudio.duration) || miniAudio.duration <= 0) return;
    var rect = miniBar.getBoundingClientRect();
    var ratio = Math.min(1, Math.max(0, (e.clientX - rect.left) / rect.width));
    miniAudio.currentTime = ratio * miniAudio.duration;
    $("music-mini-fill").style.width = Math.round(ratio * 100) + "%";
  });

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
