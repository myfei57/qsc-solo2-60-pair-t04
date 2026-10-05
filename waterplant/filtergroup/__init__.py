"""滤池池组与分区管理。

提供池号 / 分区 / 投运顺序三列唯一对应的版本化管理、整体重排
（新旧编号并存、可恢复、幂等）、不可变值班记录与审计。
"""

from .manager import GroupManager, apply_conflict_only
from .model import (
    CONFLICT_POLICIES,
    POLICY_APPLY_CONFLICT_ONLY,
    POLICY_REJECT_BATCH,
    SWITCH_IMMEDIATE,
    SWITCH_MODES,
    SWITCH_SHIFT_END,
    Assignment,
    ConflictError,
    GroupError,
    PlanStateError,
    build_codemap,
    detect_conflicts,
    verify_codemap,
)

__all__ = [
    "Assignment",
    "CONFLICT_POLICIES",
    "ConflictError",
    "GroupError",
    "GroupManager",
    "PlanStateError",
    "POLICY_APPLY_CONFLICT_ONLY",
    "POLICY_REJECT_BATCH",
    "SWITCH_IMMEDIATE",
    "SWITCH_MODES",
    "SWITCH_SHIFT_END",
    "apply_conflict_only",
    "build_codemap",
    "detect_conflicts",
    "verify_codemap",
]
