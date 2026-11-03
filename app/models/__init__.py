"""ORM 模型统一加载：导入全部模型供 Alembic 收集完整元数据。"""

from app.models.base import Base
from app.models.conversation import Conversation
from app.models.elder import Elder
from app.models.facility import Device, Room
from app.models.health import HealthRecord
from app.models.media import MediaLibrary
from app.models.medication import MedicationPlan
from app.models.memory import MemoryChunk
from app.models.persona import Persona
from app.models.voiceprint import Voiceprint
from app.models.workorder import Workorder

__all__ = [
    "Base",
    "Conversation",
    "Device",
    "Elder",
    "HealthRecord",
    "MediaLibrary",
    "MedicationPlan",
    "MemoryChunk",
    "Persona",
    "Room",
    "Voiceprint",
    "Workorder",
]
