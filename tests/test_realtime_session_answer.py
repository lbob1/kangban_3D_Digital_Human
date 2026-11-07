"""Day5 补测：VoiceRealtimeSession.accept_offer（audio sendrecv offer → recvonly answer）。

用最小合法 offer SDP（与 .cache/ice_bench.py 同源）驱动真实 aiortc 流程；
不做真实 ICE 连通（那是端到端演示环节），只锁定 answer 生成行为。
"""

from app.realtime.session import VoiceRealtimeSession

FINGERPRINT = (
    "D2:FA:0E:C3:22:59:5E:14:95:69:92:3D:13:B4:84:24"
    ":2C:C2:A2:C0:3E:FD:34:8E:5E:EA:6F:AF:52:CE:E6:0F"
)

# 最小合法 Chrome 风格 audio-only sendrecv offer
OFFER_SDP = """v=0
o=- 4611731400430051336 2 IN IP4 127.0.0.1
s=-
t=0 0
a=group:BUNDLE 0
a=msid-semantic: WMS stream
m=audio 9 UDP/TLS/RTP/SAVPF 111
c=IN IP4 0.0.0.0
a=rtcp:9 IN IP4 0.0.0.0
a=ice-ufrag:EsAw
a=ice-pwd:P2uYro0UCOQ4zxjKXaWCBui1
""" + f"a=fingerprint:sha-256 {FINGERPRINT}\n" + """a=setup:actpass
a=mid:0
a=rtcp-mux
a=sendrecv
a=msid:stream audio
a=rtpmap:111 opus/48000/2
a=fmtp:111 minptime=10;useinbandfec=1
a=ssrc:1001 cname:cov
""".replace("\n", "\r\n")


async def test_accept_offer_returns_recvonly_answer():
    session = VoiceRealtimeSession(device_id="cov-accept")
    try:
        out = await session.accept_offer(OFFER_SDP, "offer")
    finally:
        await session.close()
    assert out.type == "answer"
    assert "recvonly" in out.sdp  # 显式收方向，杜绝意外发送
    assert "opus" in out.sdp


async def test_accept_offer_twice_same_session():
    session = VoiceRealtimeSession(device_id="cov-accept2")
    try:
        first = await session.accept_offer(OFFER_SDP, "offer")
        second = await session.accept_offer(OFFER_SDP, "offer")
        assert first.type == second.type == "answer"
    finally:
        await session.close()
