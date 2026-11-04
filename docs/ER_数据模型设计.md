# ER 数据模型设计（Day1 · 11 张基础表）

> 版本：Day1 v1.0
> 范围：Day1 基线 11 表；后续业务表（TDD 6.3）仅列为候选，经评审后通过新迁移加入（ADR-013）。

## 1. 通用规范

- 数据库：PostgreSQL + pgvector；主键 UUID（`gen_random_uuid()` 数据库端生成）。
- 时间：`TIMESTAMPTZ`，统一 UTC 存储；`created_at` 非空且数据库默认生成；发生更新的表另有 `updated_at`（`onupdate`）。
- 状态/业务枚举：`VARCHAR + CHECK`，不使用 PostgreSQL 原生枚举。
- 个人数据默认 `RESTRICT` 删除；退房经显式匿名化/删除流程处理（后续阶段实现）。
- 向量列：`memory_chunks.embedding`、`voiceprints.embedding` 使用 pgvector `vector` 类型，**Day1 不指定维度、不建 ANN 索引**；Embedding 模型与维度确定后再由新迁移固定维度并补索引。

## 2. 表关系总览

```
rooms 1 ──< devices（room_id 非空）
rooms 1 ──< elders（room_id 可空，与 bed_no 成对）
personas 1 ──< elders（persona_id 非空）
elders 1 ──< memory_chunks / medication_plans / health_records / voiceprints / workorders（均非空 FK）
elders 0..1 ──< conversations（elder_id 可空，匿名会话允许空）
devices 0..1 ──< conversations（device_id 可空）
```

| 表 | 职责 |
| --- | --- |
| `rooms` | 房间（单人间/双人间），被 devices、elders 引用 |
| `devices` | 平板等设备，必属一个房间；Day1 不存 JWT |
| `elders` | 长者档案；room_id+bed_no 表达住户床位；引用 personas |
| `personas` | 数字人人设模板；可被多个 elders 引用 |
| `memory_chunks` | 长期记忆块；必属一个 elder；含向量列 |
| `medication_plans` | 用药计划；必属一个 elder |
| `media_library` | 可授权媒体目录；Day1 只建结构，不导入媒体 |
| `health_records` | 健康采集记录；必属一个 elder |
| `voiceprints` | 声纹向量记录；必属一个 elder |
| `workorders` | 护工工单；必属一个 elder；无 staff 表，assignee 暂存外部引用 |
| `conversations` | 会话索引（对应原 conversation_threads 语义） |

命名映射：`conversation_threads → conversations`、`work_orders → workorders`。

## 3. 逐表字段说明

### 3.1 rooms（TimestampMixin：created_at! / updated_at!）

| 字段 | 类型 | 空 | 说明 |
| --- | --- | --- | --- |
| room_id | UUID PK | 否 | 数据库默认生成 |
| building_code | VARCHAR(32) | 否 | 楼栋编码 |
| floor | SMALLINT | 否 | 楼层 |
| room_number | VARCHAR(32) | 否 | 房号 |
| bed_count | SMALLINT | 否 | 床位数，CHECK ∈ (1,2) |
| status | VARCHAR(20) | 否 | CHECK ∈ active/maintenance/inactive，默认 active |

约束：唯一 `(building_code, floor, room_number)`；索引 `status`。

### 3.2 personas（created_at! / updated_at!）

| 字段 | 类型 | 空 | 说明 |
| --- | --- | --- | --- |
| persona_id | UUID PK | 否 | |
| code | VARCHAR(64) | 否 | 唯一 |
| name | VARCHAR(100) | 否 | |
| avatar_url | VARCHAR(500) | 是 | 形象地址 |
| personality | TEXT | 是 | 性格描述 |
| greeting | TEXT | 是 | 问候语 |
| voice_id | VARCHAR(100) | 是 | 音色 |
| is_active | BOOLEAN | 否 | 默认 true |

索引：`(is_active, name)`。

### 3.3 devices（created_at! / updated_at! / last_seen_at?）

| 字段 | 类型 | 空 | 说明 |
| --- | --- | --- | --- |
| device_id | UUID PK | 否 | |
| device_code | VARCHAR(80) | 否 | 唯一 |
| room_id | UUID FK→rooms | 否 | 必属一个房间 |
| bed_no | SMALLINT | 是 | NULL 或 1/2；NULL 表示房间级设备 |
| device_type | VARCHAR(24) | 否 | CHECK 当前含 tablet，默认 tablet |
| status | VARCHAR(20) | 否 | CHECK ∈ pending/active/disabled/retired，默认 pending |
| last_seen_at | TIMESTAMPTZ | 是 | 最近在线时间 |

索引：`(room_id, status)`。

### 3.4 elders（created_at! / updated_at!）

| 字段 | 类型 | 空 | 说明 |
| --- | --- | --- | --- |
| elder_id | UUID PK | 否 | |
| name | VARCHAR(100) | 否 | |
| birth_date | DATE | 是 | |
| gender | VARCHAR(20) | 是 | CHECK ∈ female/male/other/unknown |
| room_id | UUID FK→rooms | 是 | 与 bed_no 成对：同空或同有（CHECK） |
| bed_no | SMALLINT | 是 | NULL 或 1/2 |
| persona_id | UUID FK→personas | 否 | |
| status | VARCHAR(20) | 否 | CHECK ∈ active/paused/discharged，默认 active |

约束：部分唯一 `(room_id, bed_no) WHERE room_id IS NOT NULL AND bed_no IS NOT NULL`；索引 `(room_id, status)`。

**单/双人间床位模型**：住户按行表达——单人间最多一条 `(room_id, bed_no=1)`；双人间分别为床位 1、2。
部分唯一约束保证每个床位最多一位长者。数据库 CHECK 无法校验 `bed_no <= rooms.bed_count` 的跨表规则，
该规则由后续写入服务校验。

### 3.5 memory_chunks（created_at! / updated_at!）

| 字段 | 类型 | 空 | 说明 |
| --- | --- | --- | --- |
| chunk_id | UUID PK | 否 | |
| elder_id | UUID FK→elders | 否 | 记忆隔离依据 |
| content | TEXT | 否 | 记忆内容 |
| embedding | VECTOR（无维度） | 否 | pgvector；维度待模型确定 |
| embedding_model | VARCHAR(80) | 是 | 模型版本 |
| category | VARCHAR(24) | 否 | CHECK ∈ preference/health/event/relationship/routine/other |
| visibility | VARCHAR(20) | 否 | CHECK ∈ private/care_team（本人私有/医护共享），默认 private |
| status | VARCHAR(20) | 否 | CHECK ∈ active/superseded/suppressed/deleted，默认 active；支持冲突记忆替代 |
| expires_at | TIMESTAMPTZ | 是 | 过期时间 |

索引：`(elder_id, status, category, created_at)`、`(elder_id, visibility)`。
**记忆可见性边界**：所有召回强制 `elder_id + visibility + status` 过滤；匿名模式不得访问任何个人记忆。

### 3.6 medication_plans（created_at! / updated_at!）

| 字段 | 类型 | 空 | 说明 |
| --- | --- | --- | --- |
| plan_id | UUID PK | 否 | |
| elder_id | UUID FK→elders | 否 | |
| drug_name | VARCHAR(160) | 否 | |
| dose | VARCHAR(100) | 否 | |
| schedule | JSONB | 否 | 服药时间表 |
| instructions | TEXT | 是 | |
| start_date | DATE | 否 | |
| end_date | DATE | 是 | CHECK 为空或 ≥ start_date |
| status | VARCHAR(20) | 否 | CHECK ∈ planned/active/paused/completed/cancelled，默认 planned |

索引：`(elder_id, status)`。

### 3.7 media_library（created_at! / updated_at!）

| 字段 | 类型 | 空 | 说明 |
| --- | --- | --- | --- |
| media_id | UUID PK | 否 | |
| type | VARCHAR(20) | 否 | CHECK ∈ opera/movie |
| title | VARCHAR(240) | 否 | |
| genre | VARCHAR(80) | 是 | 剧种/题材 |
| era | VARCHAR(80) | 是 | 年代 |
| artist | VARCHAR(160) | 是 | |
| media_uri | VARCHAR(500) | 是 | 未授权媒体不填地址 |
| duration_seconds | INTEGER | 是 | |
| copyright_status | VARCHAR(20) | 否 | CHECK ∈ unknown/licensed/teaching/public_domain/restricted/expired，默认 unknown |
| status | VARCHAR(20) | 否 | CHECK ∈ active/hidden，默认 hidden |

约束：**版权边界** CHECK——仅 `copyright_status ∈ (licensed, teaching, public_domain)` 时才允许 `status = active`。
索引：`(type, status)`、`title`。Day1 不导入、不链接任何未授权媒体。

### 3.8 health_records（仅 created_at!，无 updated_at）

| 字段 | 类型 | 空 | 说明 |
| --- | --- | --- | --- |
| record_id | UUID PK | 否 | |
| elder_id | UUID FK→elders | 否 | |
| recorded_at | TIMESTAMPTZ | 否 | 采集时间 |
| bp_systolic | SMALLINT | 是 | CHECK NULL 或 50–250 |
| bp_diastolic | SMALLINT | 是 | CHECK NULL 或 30–150 |
| sleep_minutes | INTEGER | 是 | CHECK NULL 或 0–1440 |
| mood | VARCHAR(24) | 是 | |
| chief_complaint | TEXT | 是 | 主诉 |
| source | VARCHAR(24) | 否 | 采集来源 |

索引：`(elder_id, recorded_at)`。

### 3.9 voiceprints（created_at! / expires_at?，无 updated_at）

| 字段 | 类型 | 空 | 说明 |
| --- | --- | --- | --- |
| voiceprint_id | UUID PK | 否 | |
| elder_id | UUID FK→elders | 否 | |
| embedding | VECTOR（无维度） | 否 | 加密方案待 POC，Day1 不实现 |
| embedding_model | VARCHAR(80) | 是 | |
| sample_uri | VARCHAR(500) | 是 | 临时样本引用，非永久资源 |
| status | VARCHAR(20) | 否 | CHECK ∈ active/revoked/expired，默认 active |
| expires_at | TIMESTAMPTZ | 是 | |

索引：`(elder_id, status)`。

### 3.10 workorders（created_at! / updated_at! / resolved_at?）

| 字段 | 类型 | 空 | 说明 |
| --- | --- | --- | --- |
| workorder_id | UUID PK | 否 | |
| elder_id | UUID FK→elders | 否 | |
| type | VARCHAR(32) | 否 | 工单类型（如漏服提醒）；Day1 不设枚举 CHECK |
| status | VARCHAR(20) | 否 | CHECK ∈ open/acknowledged/in_progress/resolved/cancelled，默认 open |
| priority | VARCHAR(16) | 否 | CHECK ∈ low/normal/high/urgent，默认 normal |
| assignee_ref | VARCHAR(120) | 是 | 处理人外部引用；无 staff 表，不伪造外键 |
| title | VARCHAR(200) | 否 | |
| description | TEXT | 是 | |
| resolved_at | TIMESTAMPTZ | 是 | |

索引：`(status, created_at)`、`(elder_id, status)`。

### 3.11 conversations（started_at! / ended_at?，无 created_at/updated_at）

| 字段 | 类型 | 空 | 说明 |
| --- | --- | --- | --- |
| thread_id | UUID PK | 否 | 会话主键与外部追踪标识 |
| elder_id | UUID FK→elders | 是 | 匿名通用模式允许为空，但不得访问个人记忆 |
| device_id | UUID FK→devices | 是 | |
| session_seq | BIGINT | 否 | 该长者的会话序号 |
| identity_source | VARCHAR(24) | 否 | CHECK ∈ device_room/voiceprint/explicit_confirmation/staff_token/anonymous |
| status | VARCHAR(20) | 否 | CHECK ∈ active/ended/abandoned，默认 active |
| started_at | TIMESTAMPTZ | 否 | 默认 now() |
| ended_at | TIMESTAMPTZ | 是 | CHECK 为空或 ≥ started_at |

约束：唯一 `(elder_id, session_seq)`（elder_id 为 NULL 时 PostgreSQL 唯一约束不拦截多行，匿名会话不占用序号语义）；
索引 `(elder_id, started_at)`、`(device_id, started_at)`。
**会话序号语义**：对已识别长者，`(elder_id, session_seq)` 唯一约束落实会话生成语义；分配逻辑由后续服务实现。

## 4. 迁移组织

1. `0001_facility_core`：`CREATE EXTENSION IF NOT EXISTS vector`（及 pgcrypto）→ rooms → personas → devices → elders。
2. `0002_domain_tables`：conversations、medication_plans、media_library、health_records、memory_chunks、voiceprints、workorders。
3. 降级业务表时不删除扩展，避免误删共享扩展。
4. 每次 `alembic upgrade head` 前人工审查迁移文件（外键、可空性、约束、向量类型、downgrade）。

## 5. 后续候选表清单（不在 Day1 范围，仅记录）

`accounts`、授权关联表、`device_tokens`/设备凭证、`staff_tokens`、`medication_events`、`decision_logs`、
`media_history`、`health_events`、`family_reports`、`family_checkins`、`family_care_activities`、
`reward_rules`、`reward_grants`、`audit_logs`、`metrics`。
以上表必须经评审后通过新迁移加入，不回改已执行的 Day1 主迁移（ADR-013、ADR-016）。
