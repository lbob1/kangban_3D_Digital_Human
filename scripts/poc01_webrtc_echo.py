"""POC-01：aiortc 本地单机 WebRTC 音频回声最小验证（Day3 步骤 1）。

参考来源：教师样板 C:/Users/yang/Desktop/asr 的 aiortc 用法（协议依据），
本脚本为一次性 POC 代码，不进入 app/ 正式模块。

验证目标：
1. 浏览器 getUserMedia 采集麦克风 → SDP offer → 服务端 aiortc answer；
2. 服务端把收到的音频轨原样 addTrack 回传 → 浏览器扬声器播放（听到自己回声）；
3. 记录"offer 到达 → 服务端收到首个音频帧"的首包延迟，供回填 POC 文档。

运行：uv run python scripts/poc01_webrtc_echo.py
访问：http://127.0.0.1:8601/  （localhost 属安全上下文，允许 getUserMedia）
"""

import time

from aiortc import RTCPeerConnection, RTCSessionDescription
from aiortc.mediastreams import MediaStreamTrack
from fastapi import FastAPI
from fastapi.responses import HTMLResponse
from pydantic import BaseModel

app = FastAPI(title="POC-01 WebRTC Echo")

pcs: set[RTCPeerConnection] = set()


class OfferIn(BaseModel):
    sdp: str
    type: str


class AnswerOut(BaseModel):
    sdp: str
    type: str
    first_frame_ms: float | None = None


@app.post("/offer", response_model=AnswerOut)
async def offer(body: OfferIn) -> AnswerOut:
    """接收 SDP offer，创建回声 PC：收到的音频轨原样回传。"""
    t0 = time.perf_counter()
    first_frame_ms: float | None = None
    pc = RTCPeerConnection()
    pcs.add(pc)
    offer_desc = RTCSessionDescription(sdp=body.sdp, type=body.type)

    @pc.on("track")
    async def on_track(track: MediaStreamTrack) -> None:
        nonlocal first_frame_ms
        print(f"[POC-01] 收到轨道: kind={track.kind} id={track.id}")
        # 回声核心：把浏览器上来的音频轨原样加回发送方向
        pc.addTrack(track)

        @track.on("frame")
        def on_frame(frame) -> None:
            nonlocal first_frame_ms
            if first_frame_ms is None:
                first_frame_ms = (time.perf_counter() - t0) * 1000
                print(f"[POC-01] 首个音频帧到达，offer→首帧 = {first_frame_ms:.0f} ms")
            # 每约 100 帧打印一次心跳，证明音频流在持续流动
            if frame.pts is not None and frame.pts % 1600 == 0:
                print(f"[POC-01] 音频帧流动中: pts={frame.pts}")

    @pc.on("connectionstatechange")
    async def on_state() -> None:
        print(f"[POC-01] 连接状态: {pc.connectionState}")
        if pc.connectionState in ("failed", "closed"):
            await pc.close()
            pcs.discard(pc)

    await pc.setRemoteDescription(offer_desc)
    answer = await pc.createAnswer()
    await pc.setLocalDescription(answer)
    print(f"[POC-01] answer 已生成，耗时 {(time.perf_counter() - t0) * 1000:.0f} ms")
    return AnswerOut(
        sdp=pc.localDescription.sdp,
        type=pc.localDescription.type,
        first_frame_ms=first_frame_ms,
    )


@app.get("/", response_class=HTMLResponse)
async def index() -> str:
    return PAGE


PAGE = """<!DOCTYPE html>
<html lang="zh">
<head>
<meta charset="utf-8">
<title>POC-01 WebRTC 回声</title>
<style>
  body { font-family: "Microsoft YaHei", sans-serif; max-width: 640px; margin: 40px auto; }
  button { font-size: 20px; padding: 12px 32px; margin-right: 12px; }
  #status { font-size: 22px; font-weight: bold; margin: 20px 0; padding: 12px;
            border: 2px solid #ccc; border-radius: 8px; }
  #log { font-size: 14px; color: #555; white-space: pre-wrap; }
  audio { width: 100%; margin-top: 16px; }
</style>
</head>
<body>
  <h2>POC-01：WebRTC 音频回声（本地单机）</h2>
  <div>请佩戴耳机避免啸叫；点击"开始回声"后说话，能从扬声器听到自己的声音即为通过。</div>
  <div id="status">待开始</div>
  <button id="start" onclick="startEcho()">开始回声</button>
  <button id="stop" onclick="stopEcho()">停止</button>
  <audio id="remote" autoplay></audio>
  <div id="log"></div>
<script>
var pc = null, localStream = null;
var statusEl = document.getElementById("status");
var logEl = document.getElementById("log");
function log(msg) { logEl.textContent += msg + "\\n"; }

async function startEcho() {
  try {
    statusEl.textContent = "正在请求麦克风…";
    localStream = await navigator.mediaDevices.getUserMedia({
      audio: { echoCancellation: false, noiseSuppression: false, autoGainControl: false }
    });
    log("麦克风已开启: " + localStream.getAudioTracks()[0].label);

    pc = new RTCPeerConnection();
    localStream.getTracks().forEach(function (t) { pc.addTrack(t, localStream); });

    // 关键：只收不发也会带 sendrecv，这里显式收下回传轨道并播放
    pc.ontrack = function (e) {
      log("收到回传轨道: " + e.track.kind);
      document.getElementById("remote").srcObject = e.streams[0];
      statusEl.textContent = "回声进行中——请说话，戴耳机听";
      statusEl.style.borderColor = "#2e7d32";
    };
    pc.onconnectionstatechange = function () {
      log("PC 状态: " + pc.connectionState);
      if (pc.connectionState === "failed") {
        statusEl.textContent = "连接失败";
        statusEl.style.borderColor = "#c62828";
      }
    };

    var offer = await pc.createOffer();
    await pc.setLocalDescription(offer);
    var t0 = performance.now();
    var resp = await fetch("/offer", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ sdp: pc.localDescription.sdp, type: pc.localDescription.type })
    });
    var answer = await resp.json();
    log("信令往返耗时: " + (performance.now() - t0).toFixed(0) + " ms" +
        (answer.first_frame_ms ? ("；服务端 offer→首帧: " + answer.first_frame_ms.toFixed(0) + " ms") : ""));
    await pc.setRemoteDescription({ sdp: answer.sdp, type: answer.type });
  } catch (err) {
    statusEl.textContent = "出错: " + err.message;
    statusEl.style.borderColor = "#c62828";
    log("错误: " + err.message);
  }
}

function stopEcho() {
  if (pc) { pc.close(); pc = null; }
  if (localStream) { localStream.getTracks().forEach(function (t) { t.stop(); }); localStream = null; }
  statusEl.textContent = "已停止";
  statusEl.style.borderColor = "#ccc";
  log("已停止");
}
</script>
</body>
</html>"""


if __name__ == "__main__":
    import uvicorn

    uvicorn.run(app, host="127.0.0.1", port=8601, log_level="info")
