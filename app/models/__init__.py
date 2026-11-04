"""ORM 模型统一加载：导入全部模型供 Alembic 收集完整元数据。"""

# Day2：身份与权限
from app.models.account import Account
from app.models.assignment import BuildingAssignment, DoctorElderAssignment, FamilyElderLink
from app.models.base import Base
from app.models.conversation import Conversation
from app.models.device_token import DeviceToken
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
    "Account",
    "BuildingAssignment",
    "Conversation",
    "Device",
    "DeviceToken",
    "DoctorElderAssignment",
    "Elder",
    "FamilyElderLink",
    "HealthRecord",
    "MediaLibrary",
    "MedicationPlan",
    "MemoryChunk",
    "Persona",
    "Room",
    "Voiceprint",
    "Workorder",
]
