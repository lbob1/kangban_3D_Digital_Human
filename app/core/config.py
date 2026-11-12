"""应用配置：Pydantic Settings 统一加载 .env 与进程环境变量。

Day1 基础配置启动必需；后续能力字段（LLM/ASR/TTS/JWT 等）声明为 Optional，
空占位不影响 Day1 启动，进入对应功能阶段后再做必填校验（ADR-012）。
"""

from functools import lru_cache
from typing import Any

from pydantic import model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    # ---- Day1 基础配置 ----
    app_name: str = "康伴 3D 数字人康养陪伴系统"
    app_env: str = "local"
    app_host: str = "127.0.0.1"
    app_port: int = 8000
    app_timezone: str = "Asia/Shanghai"
    log_level: str = "INFO"
    database_url: str = "postgresql+asyncpg://kangban_dev:CHANGE_ME_LOCAL_ONLY@127.0.0.1:5432/kangban_dev"
    # pytest 专用独立测试库；未配置时测试套件在收集阶段报错并提示创建
    database_url_test: str | None = None
    redis_url: str = "redis://127.0.0.1:6379/0"
    sql_echo: bool = False

    # ---- System 2 大模型（后续 Optional）----
    llm_provider: str | None = None
    llm_api_key: str | None = None
    llm_base_url: str | None = None
    llm_model: str | None = None
    llm_timeout_seconds: int = 30
    llm_max_retries: int = 1

    # ---- Day5 超时熔断三档（大纲 5.2；默认即大纲值，.env 可选覆盖）----
    asr_prompt_seconds: float = 2.0      # 一档：终句等待超时 → 提示「没听清再说一遍」
    llm_placeholder_seconds: float = 3.0  # 二档：LLM 首 token 超时 → 播「我想一想」占位
    tts_slow_seconds: float = 1.0        # 三档：首句 TTS 首包超时 → 文字先行提示

    # ---- Embedding（后续 Optional）----
    embedding_provider: str | None = None
    embedding_model: str | None = None
    embedding_base_url: str | None = None
    embedding_api_key: str | None = None
    # Day7：向量维度必须与模型实际输出一致（qwen3.7-text-embedding-flash 默认 1024），
    # 0012 迁移已按此值固定 memory_chunks.embedding 为 vector(1024)，改动须三处同步
    embedding_dimensions: int = 1024

    # ---- System One（后续 Optional）----
    system_one_provider: str | None = None
    system_one_model_path: str | None = None
    system_one_confidence_threshold: float = 0.85
    system_one_timeout_ms: int = 200

    # ---- Day6 MCP 天气 Tool（Optional：未配置走缺省坐标与默认启动命令）----
    weather_lat: float = 39.9093  # 社区缺省坐标（北京），长者档案无城市字段，不做定位
    weather_lon: float = 116.3973
    weather_city_name: str = "北京"  # 老人端天气模块展示名（.env WEATHER_CITY_NAME 可覆盖）
    weather_mcp_command: str | None = None  # 缺省 [当前解释器, mcp-servers/weather/server.py]

    # ---- Day9 MCP 多工具扩展（Optional；false=直走本地降级路径）----
    workorder_mcp_enabled: bool = True  # false=emergency_report 直走本地 ORM（降级测试用）
    workorder_mcp_command: str | None = None  # 缺省 [当前解释器, mcp-servers/workorder/server.py]
    family_notify_mcp_enabled: bool = True  # false=家属消息仅结构化日志
    # 缺省 [当前解释器, mcp-servers/family-notify/server.py]
    family_notify_mcp_command: str | None = None

    # ---- Day9 通知实时化（Optional；false=仅结构化日志，回 Day8 行为）----
    notify_ws_enabled: bool = True

    # ---- ASR / TTS（后续 Optional）----
    asr_provider: str | None = None
    asr_api_key: str | None = None
    asr_base_url: str | None = None
    asr_model: str | None = None
    asr_dialect: str | None = None
    tts_provider: str | None = None
    tts_api_key: str | None = None
    tts_base_url: str | None = None
    tts_voice_id: str | None = None
    tts_dialect: str | None = None

    # ---- 方言规范化（后续 Optional）----
    dialect_normalization_enabled: bool = False
    dialect_normalization_model: str | None = None
    dialect_min_confidence: float | None = None

    # ---- 声纹（后续 Optional）----
    voiceprint_model_path: str | None = None
    # Day3：CAMPPlus 余弦相似度阈值。生产默认维持 POC-04 真人基线 0.55（Day8 步9 合成
    # 扫描零误识区 ≥0.65 建立在合成同人分布偏高之上，真人复调 Day10 再调；环境变量可调）
    voiceprint_threshold: float = 0.55
    voiceprint_encryption_key: str | None = None

    # ---- Day3 实时语音（全部 Optional，缺省可启动；FunASR/模型未配置时旧接口不受影响）----
    # FunASR 本地 Docker WSS 地址（compose.funasr.yaml 暴露 10096）
    funasr_url: str | None = None
    # 自签名证书跳过校验（仅本地）
    funasr_insecure: bool = False
    # Edge-TTS 朗读音色（适老偏温和女声）
    edge_tts_voice: str = "zh-CN-XiaoxiaoNeural"
    # 声纹验证通过后临时 Token 有效期（秒，仅内存不落库）
    voiceprint_token_ttl: int = 300
    # sherpa-onnx 本地模型目录（CAMPPlus / ten-vad，不入 Git）
    sherpa_model_dir: str | None = None
    # 生物识别演示登录开关（登录页"老人生物识别进入"）：指纹点击即进 = 登录后门，
    # 仅教学演示用；置 0 后前端按钮失效、后端接口 403，恢复严格设备 Token 鉴权
    biometric_demo_enabled: bool = True
    # STUN/TURN JSON 数组字符串，如 [{"urls":"turn:x.x.x.x:3478","username":"u","credential":"p"}]；
    # 空=本地 host 候选。Day1 预留的 WEBRTC_STUN/TURN_* 字段保留，正式部署择一使用
    rtc_ice_servers: str | None = None

    # ---- WebRTC（后续 Optional）----
    webrtc_stun_servers: str | None = None
    webrtc_turn_servers: str | None = None
    webrtc_turn_username: str | None = None
    webrtc_turn_credential: str | None = None

    # ---- Day7 用药确认图（SQLite checkpointer，大纲 7.2）----
    medication_checkpoint_db: str = "./data/med_checkpoints.sqlite"

    # ---- Day7 用药提醒调度（大纲 7.2/4.2：定时与对话共用一图）----
    medication_scheduler_enabled: bool = True
    medication_scan_interval_seconds: int = 30  # 到点扫描粒度
    medication_slot_window_seconds: int = 120  # slot 时刻起多久内算「到点」
    medication_wait_timeout_seconds: int = 120  # waiting_confirm 超时判据（updated_at 起）
    medication_offline_backoff_seconds: int = 60  # 离线退避间隔（recall 再呼）

    # ---- Day8 健康决策图（interrupt HITL，大纲 8.1）----
    health_call_enabled: bool = True  # 每晚健康外呼开关
    health_call_time: str = "20:00"  # 外呼时刻（HH:MM）
    # 健康图 checkpointer（独立于用药图文件）
    health_checkpoint_db: str = "./data/health_checkpoints.sqlite"
    hospital_mock_delay_ms: int = 800  # 医院对接 mock 模拟延迟（演示真实感）

    # ---- JWT / 设备 Token（Day2 启用；密钥为空时签发/注册接口在运行期报错）----
    jwt_secret_key: str | None = None
    jwt_access_token_expire_minutes: int = 60
    device_token_expire_days: int = 90
    device_registration_token_hours: int = 24

    # ---- 数据保留 / 观测（后续 Optional）----
    audio_retention_hours: int = 72
    langfuse_host: str | None = None
    langfuse_public_key: str | None = None
    langfuse_secret_key: str | None = None
    # Day10 步骤 3：/metrics 端点开关（false 时端点 404，中间件停用）
    metrics_enabled: bool = True

    @model_validator(mode="before")
    @classmethod
    def _empty_str_to_none(cls, data: Any) -> Any:
        """空字符串占位（如 JWT_ACCESS_TOKEN_EXPIRE_MINUTES=）按未配置处理，走字段默认值。"""
        if isinstance(data, dict):
            return {k: v for k, v in data.items() if v != ""}
        return data


@lru_cache
def get_settings() -> Settings:
    """返回缓存的设置实例。"""
    return Settings()
