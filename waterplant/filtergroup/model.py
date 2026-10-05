"""滤组分区管理的领域类型与校验。

术语约定
========

池号 (code)
    贴在滤池现场的标牌编号，重排时会变，例如 ``1#``、``12#``。
分区 (zone)
    滤池在池组中的物理分区序号，重排时会变。
投运顺序 (sequence)
    该池投入运行/冲洗轮换的次序，重排时会变。
池标识 (pool_id)
    每座滤池的永久身份，终身不变（资产台账号）。所有历史记录都按它关联。

在同一个生效版本内，池号、分区、投运顺序三者一一对应（双射）：
任意两个池不能占用同一个分区、同一个投运顺序或同一个池号。
"""

from __future__ import annotations

from dataclasses import dataclass

# 切换时机：立即切换 / 等本班结束再切
SWITCH_IMMEDIATE = "IMMEDIATE"
SWITCH_SHIFT_END = "SHIFT_END"
SWITCH_MODES = (SWITCH_IMMEDIATE, SWITCH_SHIFT_END)

# 冲突策略：整批拒绝 / 只改不冲突项（冲突项回退到原编号）
POLICY_REJECT_BATCH = "REJECT_BATCH"
POLICY_APPLY_CONFLICT_ONLY = "APPLY_CONFLICT_ONLY"
CONFLICT_POLICIES = (POLICY_REJECT_BATCH, POLICY_APPLY_CONFLICT_ONLY)

# 重排方案状态
PLAN_DRAFT = "DRAFT"          # 方案已登记，尚未通过校验
PLAN_REJECTED = "REJECTED"    # 冲突策略为整批拒绝，本方案作废
PLAN_ARMED = "ARMED"          # 已校验，等待激活（立即模式下瞬间经过）
PLAN_EFFECTIVE = "EFFECTIVE"  # 已切换生效
PLAN_FAILED = "FAILED"        # 执行中途部分失败，可恢复
PLAN_CANCELLED = "CANCELLED"  # 待激活方案被撤销

# 激活重排的步骤（每步独立落盘，可断点恢复）
STEP_VALIDATE = "VALIDATE"        # 登记目标版本的分配快照
STEP_CODEMAP = "BUILD_CODEMAP"    # 生成新旧对照边并做一致性校验
STEP_CUTOVER = "CUTOVER"          # 切换生效指针
STEP_AUDIT = "AUDIT"              # 补写审计
PLAN_STEPS = (STEP_VALIDATE, STEP_CODEMAP, STEP_CUTOVER, STEP_AUDIT)


class GroupError(ValueError):
    """池组管理领域错误的基类（经控制台映射为 400）。"""


class ConflictError(GroupError):
    """重排方案存在冲突。携带完整冲突明细，便于控制台展示。"""

    def __init__(self, message: str, conflicts: list[dict[str, object]] | None = None) -> None:
        super().__init__(message)
        self.conflicts = conflicts or []


class PlanStateError(GroupError):
    """方案当前状态不允许该操作（例如对已生效方案再次激活）。"""


@dataclass(frozen=True)
class Assignment:
    """一条分配：一座池在某个版本内的 (池号, 分区, 投运顺序)。"""

    pool_id: str
    code: str
    zone: int
    sequence: int

    def as_dict(self) -> dict[str, object]:
        return {
            "pool_id": self.pool_id,
            "code": self.code,
            "zone": self.zone,
            "sequence": self.sequence,
        }

    @classmethod
    def from_dict(cls, payload: dict[str, object]) -> "Assignment":
        return cls(
            pool_id=str(payload["pool_id"]),
            code=str(payload["code"]),
            zone=int(payload["zone"]),
            sequence=int(payload["sequence"]),
        )


def validate_assignment_rows(rows: list[Assignment]) -> None:
    """校验单个版本内部的字段合法性与三列唯一性。

    同一批内池号、分区、投运顺序都不允许重复；池标识也不允许重复。
    """

    seen_pools: set[str] = set()
    seen_codes: set[str] = set()
    seen_zones: set[int] = set()
    seen_sequences: set[int] = set()
    for row in rows:
        if not row.pool_id.strip():
            raise GroupError("pool_id 不能为空")
        if not row.code.strip():
            raise GroupError(f"池 {row.pool_id} 的池号不能为空")
        if row.zone < 1:
            raise GroupError(f"池 {row.pool_id} 的分区必须为正整数")
        if row.sequence < 1:
            raise GroupError(f"池 {row.pool_id} 的投运顺序必须为正整数")
        if row.pool_id in seen_pools:
            raise ConflictError(
                f"池标识 {row.pool_id} 在同一版本内出现多次",
                [{"type": "DUPLICATE_POOL", "pool_id": row.pool_id}],
            )
        if row.code in seen_codes:
            raise ConflictError(
                f"池号 {row.code} 被多个池占用",
                [{"type": "DUP_CODE", "code": row.code}],
            )
        if row.zone in seen_zones:
            raise ConflictError(
                f"分区 {row.zone} 被两个池占用",
                [{"type": "DUP_ZONE", "zone": row.zone}],
            )
        if row.sequence in seen_sequences:
            raise ConflictError(
                f"投运顺序 {row.sequence} 被两个池占用",
                [{"type": "DUP_SEQUENCE", "sequence": row.sequence}],
            )
        seen_pools.add(row.pool_id)
        seen_codes.add(row.code)
        seen_zones.add(row.zone)
        seen_sequences.add(row.sequence)


def index_assignments(rows: list[Assignment]) -> dict[str, Assignment]:
    """按池标识索引一个版本的全部分配。"""

    return {row.pool_id: row for row in rows}


def detect_conflicts(
    base: dict[str, Assignment], proposed: dict[str, Assignment]
) -> list[dict[str, object]]:
    """找出拟议方案相对基线版本的全部冲突。

    冲突分两类：

    结构性冲突（任何策略都无法调和，必整批拒绝）
        - MISSING_POOL：基线里的池没有出现在方案中
        - EXTRA_POOL：方案里出现基线没有的池（新池应走资产登记）
        - DUPLICATE_POOL：同一池在方案中出现多次

    占用冲突（可由冲突策略决定怎么处理）
        - DUP_CODE / DUP_ZONE / DUP_SEQUENCE：方案内部两池抢占同一格
        - COLLIDE_*：方案内部不重复，但两个池的目标格互相打架（即
          多对一：不同的池指向同一个池号/分区/顺序）。重复与抢占在
          方案内部其实等价，这里统一以"目标格被多个池指向"上报。
    """

    conflicts: list[dict[str, object]] = []

    base_pools = set(base)
    proposed_pools = set(proposed)
    for pool_id in sorted(base_pools - proposed_pools):
        conflicts.append({"type": "MISSING_POOL", "pool_id": pool_id})
    for pool_id in sorted(proposed_pools - base_pools):
        conflicts.append({"type": "EXTRA_POOL", "pool_id": pool_id})

    buckets = {"code": {}, "zone": {}, "sequence": {}}
    labels = {"code": "DUP_CODE", "zone": "DUP_ZONE", "sequence": "DUP_SEQUENCE"}
    keys = {"code": "code", "zone": "zone", "sequence": "sequence"}
    for pool_id in sorted(proposed):
        row = proposed[pool_id]
        for attr, value in (("code", row.code), ("zone", row.zone), ("sequence", row.sequence)):
            buckets[attr].setdefault(value, []).append(pool_id)
    for attr in ("code", "zone", "sequence"):
        for value, owners in sorted(buckets[attr].items(), key=lambda item: repr(item[0])):
            if len(owners) > 1:
                conflicts.append(
                    {
                        "type": labels[attr],
                        keys[attr]: value,
                        "pools": sorted(owners),
                    }
                )
    return conflicts


def structural_conflicts(conflicts: list[dict[str, object]]) -> list[dict[str, object]]:
    return [item for item in conflicts if item["type"] in ("MISSING_POOL", "EXTRA_POOL")]


def build_codemap(
    old: dict[str, Assignment], new: dict[str, Assignment]
) -> list[dict[str, object]]:
    """生成相邻两个版本之间的逐池对照边。

    每条边记录同一座永久池的新旧 (池号, 分区, 投运顺序)，这是
    "重排前后对照关系保证一致性"的凭据：重放这些边必须能从旧版本
    唯一到达新版本。
    """

    edges: list[dict[str, object]] = []
    for pool_id in sorted(old):
        if pool_id not in new:
            continue
        before, after = old[pool_id], new[pool_id]
        edges.append(
            {
                "pool_id": pool_id,
                "old_code": before.code,
                "new_code": after.code,
                "old_zone": before.zone,
                "new_zone": after.zone,
                "old_sequence": before.sequence,
                "new_sequence": after.sequence,
                "changed": not (
                    before.code == after.code
                    and before.zone == after.zone
                    and before.sequence == after.sequence
                ),
            }
        )
    return edges


def verify_codemap(
    old: dict[str, Assignment],
    new: dict[str, Assignment],
    edges: list[dict[str, object]],
) -> None:
    """对照一致性校验：边集合必须与两个版本完全吻合。

    - 每个池恰好一条边；
    - 边里的旧值必须等于旧版本、新值必须等于新版本；
    - 映射可逆：新池号/分区/顺序在边中同样唯一。
    """

    if {edge["pool_id"] for edge in edges} != set(old) or set(old) != set(new):
        raise GroupError("对照关系与版本池集合不一致")
    for edge in edges:
        before, after = old[edge["pool_id"]], new[edge["pool_id"]]
        if (
            edge["old_code"] != before.code
            or edge["old_zone"] != before.zone
            or edge["old_sequence"] != before.sequence
            or edge["new_code"] != after.code
            or edge["new_zone"] != after.zone
            or edge["new_sequence"] != after.sequence
        ):
            raise GroupError(f"池 {edge['pool_id']} 的对照边与版本快照不符")
    for attr in ("new_code", "new_zone", "new_sequence"):
        values = [edge[attr] for edge in edges]
        if len(values) != len(set(values)):
            raise GroupError(f"对照关系在 {attr} 上不可逆，重排不唯一")
