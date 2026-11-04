"""字符串枚举定义：数据库使用 VARCHAR + CHECK，不使用 PostgreSQL 原生枚举。"""

from enum import StrEnum


class RoomStatus(StrEnum):
    ACTIVE = "active"
    MAINTENANCE = "maintenance"
    INACTIVE = "inactive"


class DeviceStatus(StrEnum):
    PENDING = "pending"
    ACTIVE = "active"
    DISABLED = "disabled"
    RETIRED = "retired"


class DeviceType(StrEnum):
    TABLET = "tablet"


class ElderStatus(StrEnum):
    ACTIVE = "active"
    PAUSED = "paused"
    DISCHARGED = "discharged"


class Gender(StrEnum):
    FEMALE = "female"
    MALE = "male"
    OTHER = "other"
    UNKNOWN = "unknown"


class MemoryCategory(StrEnum):
    PREFERENCE = "preference"
    HEALTH = "health"
    EVENT = "event"
    RELATIONSHIP = "relationship"
    ROUTINE = "routine"
    OTHER = "other"


class MemoryVisibility(StrEnum):
    PRIVATE = "private"
    CARE_TEAM = "care_team"


class MemoryStatus(StrEnum):
    ACTIVE = "active"
    SUPERSEDED = "superseded"
    SUPPRESSED = "suppressed"
    DELETED = "deleted"


class VoiceprintStatus(StrEnum):
    ACTIVE = "active"
    REVOKED = "revoked"
    EXPIRED = "expired"


class MedicationPlanStatus(StrEnum):
    PLANNED = "planned"
    ACTIVE = "active"
    PAUSED = "paused"
    COMPLETED = "completed"
    CANCELLED = "cancelled"


class MediaType(StrEnum):
    OPERA = "opera"
    MOVIE = "movie"


class MediaCopyrightStatus(StrEnum):
    UNKNOWN = "unknown"
    LICENSED = "licensed"
    TEACHING = "teaching"
    PUBLIC_DOMAIN = "public_domain"
    RESTRICTED = "restricted"
    EXPIRED = "expired"


class MediaStatus(StrEnum):
    ACTIVE = "active"
    HIDDEN = "hidden"


class WorkorderStatus(StrEnum):
    OPEN = "open"
    ACKNOWLEDGED = "acknowledged"
    IN_PROGRESS = "in_progress"
    RESOLVED = "resolved"
    CANCELLED = "cancelled"


class WorkorderPriority(StrEnum):
    LOW = "low"
    NORMAL = "normal"
    HIGH = "high"
    URGENT = "urgent"


class IdentitySource(StrEnum):
    DEVICE_ROOM = "device_room"
    VOICEPRINT = "voiceprint"
    EXPLICIT_CONFIRMATION = "explicit_confirmation"
    STAFF_TOKEN = "staff_token"
    ANONYMOUS = "anonymous"


class ConversationStatus(StrEnum):
    ACTIVE = "active"
    ENDED = "ended"
    ABANDONED = "abandoned"


# ---- Day2：身份与权限 ----


class AccountRole(StrEnum):
    NURSE = "nurse"
    DOCTOR = "doctor"
    FAMILY = "family"
    ADMIN = "admin"


class AccountStatus(StrEnum):
    ACTIVE = "active"
    DISABLED = "disabled"
    LOCKED = "locked"


class DeviceTokenKind(StrEnum):
    REGISTER = "register"
    ACCESS = "access"


class AssignmentStatus(StrEnum):
    ACTIVE = "active"
    SUSPENDED = "suspended"
