"""池组与分区管理的核心服务。

重排（整体重编号）的生命周期
============================

1. ``submit_plan`` 登记重排方案，切换时机与冲突策略都必须显式指定，
   系统不提供默认值：

   * 切换时机 ``switch``：IMMEDIATE（立即切换）或 SHIFT_END（等本班
     结束再切）——按运行安排定；
   * 冲突策略 ``policy``：REJECT_BATCH（拒绝整批）或
     APPLY_CONFLICT_ONLY（只改不冲突项，冲突项保留原编号）。

   结构性冲突（池集合与当前版本不一致、同一池在方案里出现多次）在
   任何策略下都整批拒绝；编号占用冲突才按既定策略处理。

2. 方案按固定步骤推进，每步独立落盘：

   VALIDATE → BUILD_CODEMAP → CUTOVER → AUDIT

   中途崩溃只留下部分状态（部分失败），``recover`` 从断点继续前推，
   重复调用幂等：每个步骤先判断"成果是否已存在"，已完成就跳过，
   未完成才追加事件，任何一步的事件都只写一次。

3. IMMEDIATE 在提交同一调用里一路推到生效；SHIFT_END 只完成
   VALIDATE 后停在 ARMED，新旧编号并存，直到本班结束由 ``activate``
   切换，或在切换前 ``cancel`` 撤销。SHIFT_END 在 VALIDATE 阶段
   崩溃时，``recover`` 只把方案恢复到待激活状态，不会越过班末。

4. 值班记录只追加、永不回改，且永远记下书写当时的版本与池号原文；
   读方通过 ``resolve_code`` 在"生效 + 待激活"两个版本里同时识别
   新旧号；新旧版本复用同一池号指代不同池时按歧义上报，不静默选择。
"""

from __future__ import annotations

import threading
import time
import uuid
from typing import Any

from waterplant.store.store import Store

from . import events as ev
from .model import (
    PLAN_ARMED,
    PLAN_CANCELLED,
    PLAN_DRAFT,
    PLAN_EFFECTIVE,
    PLAN_FAILED,
    PLAN_REJECTED,
    PLAN_STEPS,
    POLICY_APPLY_CONFLICT_ONLY,
    POLICY_REJECT_BATCH,
    STEP_AUDIT,
    STEP_CODEMAP,
    STEP_CUTOVER,
    STEP_VALIDATE,
    SWITCH_IMMEDIATE,
    SWITCH_MODES,
    SWITCH_SHIFT_END,
    Assignment,
    ConflictError,
    GroupError,
    PlanStateError,
    build_codemap,
    detect_conflicts,
    index_assignments,
    structural_conflicts,
    validate_assignment_rows,
    verify_codemap,
)


def _now() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


def _validate_fields(rows: list[Assignment]) -> None:
    """只校验字段本身，不校验批内唯一——唯一性属于冲突，要留给策略。"""

    for row in rows:
        if not row.pool_id.strip():
            raise GroupError("pool_id 不能为空")
        if not row.code.strip():
            raise GroupError(f"池 {row.pool_id} 的池号不能为空")
        if row.zone < 1:
            raise GroupError(f"池 {row.pool_id} 的分区必须为正整数")
        if row.sequence < 1:
            raise GroupError(f"池 {row.pool_id} 的投运顺序必须为正整数")


def apply_conflict_only(
    base: dict[str, Assignment], rows: list[Assignment]
) -> tuple[list[Assignment], list[dict[str, object]]]:
    """只改不冲突项：单调淘汰，直到结果为合法双射。

    先假设所有池都按方案移动，逐轮检查合成布局；一旦发现两个池抢占
    同一池号/分区/顺序，就确定性地淘汰其中优先级较低的移动（池标识
    字典序靠后者），该池退回原编号。每轮至少淘汰一个移动、只减不
    增，必然终止；全退时退回基线，而基线本身是合法双射。最终结果
    对同一输入恒定，且非冲突项一律按方案落地。
    """

    proposed = index_assignments(rows)
    movers = set(proposed)

    def layout() -> dict[str, Assignment]:
        return {
            pool_id: (proposed[pool_id] if pool_id in movers else base[pool_id])
            for pool_id in base
        }

    def collisions(current: dict[str, Assignment]) -> list[tuple[str, object, list[str]]]:
        # 返回 (属性, 值, 该处冲突涉及的池标识)。基线本身是双射，因此
        # 任何一处冲突里必然至少有一个主动移动方。
        found: list[tuple[str, object, list[str]]] = []
        for attr in ("code", "zone", "sequence"):
            owners: dict[object, list[str]] = {}
            for pool_id in sorted(current):
                owners.setdefault(getattr(current[pool_id], attr), []).append(pool_id)
            for value, pools in owners.items():
                if len(pools) > 1:
                    found.append((attr, value, sorted(pools)))
        return found

    def losing_movers(current: dict[str, Assignment]) -> list[str]:
        """逐格判定本轮输掉争抢的移动池。

        对 code/zone/sequence 每一类的每个目标值：

        * 若该值被某个静止池（已退回基线者）占据，所有申请它的移动
          池都输掉——不能占别人的原位；
        * 否则申请者里池标识最靠前者保留，其余输掉。

        一个移动池只要在任意一格上输掉，整个移动作废、退回原号。
        """

        losers: set[str] = set()
        stationary = {pool_id for pool_id in base if pool_id not in movers}
        for attr in ("code", "zone", "sequence"):
            occupants: dict[object, list[str]] = {}
            for pool_id in sorted(current):
                occupants.setdefault(getattr(current[pool_id], attr), []).append(pool_id)
            for value, pools in occupants.items():
                if len(pools) <= 1:
                    continue
                holder = next((p for p in pools if p in stationary), None)
                if holder is not None:
                    losers.update(p for p in pools if p in movers)
                else:
                    contenders = sorted(p for p in pools if p in movers)
                    losers.update(contenders[1:])  # 最靠前的保留
        return sorted(losers)

    while movers:
        current = layout()
        if not collisions(current):
            break
        losers = losing_movers(current)
        if not losers:  # 理论不可达，保底保证严格收敛。
            losers = [max(movers)]
        for loser in losers:
            movers.discard(loser)

    final_map = layout()
    reverted: list[dict[str, object]] = []
    for pool_id in sorted(base):
        new, old = proposed.get(pool_id), base[pool_id]
        if new is not None and new != old and pool_id not in movers:
            reverted.append(
                {
                    "pool_id": pool_id,
                    "requested_code": new.code,
                    "requested_zone": new.zone,
                    "requested_sequence": new.sequence,
                    "kept_code": old.code,
                    "kept_zone": old.zone,
                    "kept_sequence": old.sequence,
                }
            )
    final_rows = [final_map[pool_id] for pool_id in sorted(final_map)]
    validate_assignment_rows(final_rows)
    return final_rows, reverted


class GroupManager:
    """基于事件日志的池组分区管理器，线程安全。"""

    def __init__(self, store: Store) -> None:
        self._store = store
        self._lock = threading.RLock()
        # 故障注入：{步骤名: 剩余触发次数}，供测试模拟部分失败。
        self._failpoints: dict[str, int] = {}

    # ------------------------------------------------------------------
    # 故障注入（仅测试/演练使用）：在步骤的指定点抛出，模拟进程崩溃。
    # ------------------------------------------------------------------
    def inject_failure(self, step: str, times: int = 1) -> None:
        self._failpoints[step] = self._failpoints.get(step, 0) + times

    def _fail_if_armed(self, step: str) -> None:
        remaining = self._failpoints.get(step, 0)
        if remaining > 0:
            self._failpoints[step] = remaining - 1
            raise RuntimeError(f"injected failure at step {step}")

    # ==================================================================
    # 投影：从 append-only 事件日志重建读模型
    # ==================================================================
    def _fold(self) -> dict[str, Any]:
        pools: dict[str, dict[str, Any]] = {}
        versions: dict[str, dict[str, Any]] = {}
        plans: dict[str, dict[str, Any]] = {}
        completed_steps: set[tuple[str, str]] = set()
        reserved: dict[str, str] = {}
        failed: set[str] = set()
        plan_audits: dict[str, dict[str, Any]] = {}
        for event in ev.load_events(self._store):
            kind = event["kind"]
            if kind == "POOL_REGISTERED":
                pools[event["pool_id"]] = {
                    "pool_id": event["pool_id"],
                    "commissioned_at": event.get("at", ""),
                }
            elif kind == "VERSION_COMMITTED":
                versions[event["version_id"]] = {
                    "version_id": event["version_id"],
                    "seq": int(event["seq"]),
                    "basis": event.get("basis"),
                    "at": event.get("at", ""),
                    "rows": [Assignment.from_dict(row) for row in event["rows"]],
                }
            elif kind == "PLAN_SUBMITTED":
                plans[event["plan_id"]] = {
                    "plan_id": event["plan_id"],
                    "base_version": event["base_version"],
                    "target_version": None,
                    "switch": event["switch"],
                    "policy": event["policy"],
                    "status": PLAN_DRAFT,
                    "at": event.get("at", ""),
                    "note": event.get("note", ""),
                    "proposed": [Assignment.from_dict(row) for row in event["rows"]],
                }
            elif kind == "PLAN_REJECTED":
                plans[event["plan_id"]]["status"] = PLAN_REJECTED
            elif kind == "PLAN_TARGET_RESERVED":
                reserved[event["plan_id"]] = event["version_id"]
                plans[event["plan_id"]]["target_version"] = event["version_id"]
            elif kind == "PLAN_STEP_DONE":
                plan = plans[event["plan_id"]]
                completed_steps.add((event["plan_id"], event["step"]))
                if event.get("version_id"):
                    plan["target_version"] = event["version_id"]
                if event["step"] == STEP_AUDIT and event.get("audit_entry"):
                    plan_audits[event["plan_id"]] = event["audit_entry"]
                if (
                    event["step"] not in (STEP_CUTOVER, STEP_AUDIT)
                    and plan["status"] != PLAN_EFFECTIVE
                ):
                    plan["status"] = PLAN_ARMED
            elif kind == "PLAN_EFFECTIVE":
                plans[event["plan_id"]]["status"] = PLAN_EFFECTIVE
            elif kind == "PLAN_FAILED":
                failed.add(event["plan_id"])
                # 失败标记只对尚未切换的方案有效；CUTOVER 已落盘的方案
                # 事实上已生效，后到的 FAILED 事件不得回改其状态。
                if (
                    event["plan_id"] in plans
                    and (event["plan_id"], STEP_CUTOVER) not in completed_steps
                    and plans[event["plan_id"]]["status"] != PLAN_EFFECTIVE
                ):
                    plans[event["plan_id"]]["status"] = PLAN_FAILED
            elif kind == "PLAN_REARMED":
                failed.discard(event["plan_id"])
                # 只清除失败标记；最终状态以已完成步骤为准
                # （CUTOVER 已完成即 EFFECTIVE，否则为 ARMED）。
                if event["plan_id"] in plans:
                    if (event["plan_id"], STEP_CUTOVER) in completed_steps:
                        plans[event["plan_id"]]["status"] = PLAN_EFFECTIVE
                    elif plans[event["plan_id"]]["status"] != PLAN_REJECTED:
                        plans[event["plan_id"]]["status"] = PLAN_ARMED
            elif kind == "PLAN_CANCELLED":
                plans[event["plan_id"]]["status"] = PLAN_CANCELLED
        state = ev.load_state(self._store)
        return {
            "pools": pools,
            "versions": versions,
            "plans": plans,
            "steps": completed_steps,
            "reserved": reserved,
            "failed": failed,
            "plan_audits": plan_audits,
            "state": state,
        }

    def _assignments_of(self, model: dict[str, Any], version_id: str) -> dict[str, Assignment]:
        version = model["versions"].get(version_id)
        if version is None:
            raise GroupError(f"版本 {version_id} 不存在")
        return index_assignments(version["rows"])

    def _armed_plan(self, model: dict[str, Any]) -> dict[str, Any] | None:
        plan_id = model["state"].get("armed_plan")
        return model["plans"].get(plan_id) if plan_id else None

    # ==================================================================
    # 建账：初始池清单
    # ==================================================================
    def bootstrap(
        self, rows: list[Assignment], note: str = "初始建账", actor: str = "system"
    ) -> dict[str, Any]:
        """建立第一版池组清单。仅允许在从未建账时执行一次。"""

        validate_assignment_rows(rows)
        with self._lock:
            model = self._fold()
            if model["versions"]:
                raise GroupError("池组已建账，初始建账只能执行一次")
            at = _now()
            for row in rows:
                ev.append_event(
                    self._store, "POOL_REGISTERED", pool_id=row.pool_id, at=at
                )
            version_id = self._commit_version(model, rows, basis=None, note=note)
            ev.append_audit(
                self._store,
                "BOOTSTRAP",
                {"actor": actor, "version": version_id, "pools": len(rows), "note": note},
            )
            return self.layout()

    def _next_version_seq(self, model: dict[str, Any]) -> int:
        if not model["versions"]:
            return 1
        return max(version["seq"] for version in model["versions"].values()) + 1

    def _commit_version(
        self,
        model: dict[str, Any],
        rows: list[Assignment],
        basis: str | None,
        note: str = "",
        explicit_id: str | None = None,
    ) -> str:
        """校验后提交一个版本快照（append-only 事件），返回版本号。"""

        validate_assignment_rows(rows)
        version_id = explicit_id or f"V{self._next_version_seq(model)}"
        if version_id in model["versions"]:
            return version_id  # 幂等：崩溃恢复时版本已落盘。
        seq = int(version_id[1:])
        ev.append_event(
            self._store,
            "VERSION_COMMITTED",
            version_id=version_id,
            seq=seq,
            basis=basis,
            note=note,
            at=_now(),
            rows=[row.as_dict() for row in rows],
        )
        model["versions"][version_id] = {
            "version_id": version_id,
            "seq": seq,
            "basis": basis,
            "at": _now(),
            "rows": rows,
        }
        if basis is None:
            state = model["state"]
            state["effective_version"] = version_id
            ev.save_state(self._store, state)
        return version_id

    # ==================================================================
    # 提交重排方案
    # ==================================================================
    def submit_plan(
        self,
        rows: list[Assignment],
        switch: str,
        policy: str,
        note: str = "",
        actor: str = "operator",
    ) -> dict[str, Any]:
        """登记并（按切换时机）推进一个重排方案。"""

        if switch not in SWITCH_MODES:
            raise GroupError(f"切换时机必须是 {SWITCH_MODES} 之一，必须显式指定，不设默认")
        if policy not in (POLICY_REJECT_BATCH, POLICY_APPLY_CONFLICT_ONLY):
            raise GroupError(
                "冲突策略必须是 REJECT_BATCH 或 APPLY_CONFLICT_ONLY，必须显式指定，不设默认"
            )
        _validate_fields(rows)

        with self._lock:
            model = self._fold()
            base_id = model["state"].get("effective_version")
            if not base_id:
                raise GroupError("尚未建账，无法重排")
            if self._armed_plan(model) is not None:
                raise PlanStateError("已有待激活的重排方案，请先激活或撤销")
            for plan in model["plans"].values():
                if plan["status"] == PLAN_FAILED:
                    raise PlanStateError(
                        f"方案 {plan['plan_id']} 处于部分失败状态，请先 recover 恢复"
                    )

            base = self._assignments_of(model, base_id)

            # 同一方案里同一池出现多次：批次形状错误，任何策略都拒绝。
            payload_pools = [row.pool_id for row in rows]
            duplicate_pools = sorted(
                {pool_id for pool_id in payload_pools if payload_pools.count(pool_id) > 1}
            )
            if duplicate_pools:
                conflicts = [
                    {"type": "DUPLICATE_POOL", "pool_id": pool_id}
                    for pool_id in duplicate_pools
                ]
                plan_id = self._register_plan(model, rows, switch, policy, note)
                self._mark_rejected(model, plan_id, "MALFORMED", conflicts, actor)
                raise ConflictError("方案中同一池出现多次，整批拒绝", conflicts)

            proposed = index_assignments(rows)
            conflicts = detect_conflicts(base, proposed)
            structural = structural_conflicts(conflicts)
            if structural:
                plan_id = self._register_plan(model, rows, switch, policy, note)
                self._mark_rejected(model, plan_id, "STRUCTURAL", structural, actor)
                raise ConflictError("池集合与当前版本不一致，整批拒绝", structural)

            occupancy = [item for item in conflicts if item["type"].startswith("DUP_")]
            reverted: list[dict[str, object]] = []
            if occupancy:
                if policy == POLICY_REJECT_BATCH:
                    plan_id = self._register_plan(model, rows, switch, policy, note)
                    self._mark_rejected(model, plan_id, "OCCUPANCY", occupancy, actor)
                    raise ConflictError("发现编号占用冲突，按 REJECT_BATCH 整批拒绝", occupancy)
                rows, reverted = apply_conflict_only(base, rows)

            plan_id = self._register_plan(model, rows, switch, policy, note)
            ev.append_audit(
                self._store,
                "PLAN_SUBMIT",
                {
                    "actor": actor,
                    "plan": plan_id,
                    "base": base_id,
                    "switch": switch,
                    "policy": policy,
                    "conflicts": occupancy,
                    "reverted": reverted,
                    "note": note,
                },
            )

            if switch == SWITCH_IMMEDIATE:
                self._advance(model, plan_id, actor)
            else:
                # SHIFT_END：只登记目标版本快照并进入并存期，不切换。
                try:
                    self._run_step(model, plan_id, STEP_VALIDATE)
                    state = model["state"]
                    state["armed_plan"] = plan_id
                    ev.save_state(self._store, state)
                except Exception:
                    self._mark_failed(model, plan_id)
                    raise
            return self.plan(plan_id)

    def _register_plan(
        self,
        model: dict[str, Any],
        rows: list[Assignment],
        switch: str,
        policy: str,
        note: str,
    ) -> str:
        plan_id = f"P{uuid.uuid4().hex[:8]}"
        base_id = model["state"]["effective_version"]
        ev.append_event(
            self._store,
            "PLAN_SUBMITTED",
            plan_id=plan_id,
            base_version=base_id,
            switch=switch,
            policy=policy,
            note=note,
            at=_now(),
            rows=[row.as_dict() for row in rows],
        )
        model["plans"][plan_id] = {
            "plan_id": plan_id,
            "base_version": base_id,
            "target_version": None,
            "switch": switch,
            "policy": policy,
            "status": PLAN_DRAFT,
            "at": _now(),
            "note": note,
            "proposed": rows,
        }
        return plan_id

    def _mark_rejected(
        self,
        model: dict[str, Any],
        plan_id: str,
        reason: str,
        conflicts: list[dict[str, object]],
        actor: str,
    ) -> None:
        ev.append_event(
            self._store,
            "PLAN_REJECTED",
            plan_id=plan_id,
            reason=reason,
            conflicts=conflicts,
            at=_now(),
        )
        model["plans"][plan_id]["status"] = PLAN_REJECTED
        ev.append_audit(
            self._store,
            "PLAN_REJECT",
            {"actor": actor, "plan": plan_id, "reason": reason, "conflicts": conflicts},
        )

    def _mark_failed(self, model: dict[str, Any], plan_id: str) -> None:
        """部分失败标记，只追加一次（幂等）。"""

        plan = model["plans"][plan_id]
        if plan_id in model["failed"] or plan["status"] == PLAN_EFFECTIVE:
            return
        ev.append_event(self._store, "PLAN_FAILED", plan_id=plan_id, at=_now())
        model["failed"].add(plan_id)
        if plan["status"] != PLAN_EFFECTIVE:
            plan["status"] = PLAN_FAILED

    # ==================================================================
    # 步骤推进与恢复
    # ==================================================================
    def _advance(self, model: dict[str, Any], plan_id: str, actor: str) -> None:
        try:
            self._run_step(model, plan_id, STEP_VALIDATE)
            self._run_step(model, plan_id, STEP_CODEMAP)
            self._run_step(model, plan_id, STEP_CUTOVER)
            self._run_step(model, plan_id, STEP_AUDIT, actor=actor)
        except Exception:
            self._mark_failed(model, plan_id)
            raise

    def _run_step(
        self, model: dict[str, Any], plan_id: str, step: str, actor: str = "system"
    ) -> None:
        if (plan_id, step) in model["steps"]:
            return  # 幂等：步骤已完成，直接跳过。
        plan = model["plans"][plan_id]
        base = self._assignments_of(model, plan["base_version"])

        if step == STEP_VALIDATE:
            # 预留目标版本号，使"版本快照已写、步骤未完成"的崩溃可重放。
            target_id = model["reserved"].get(plan_id)
            if target_id is None:
                target_id = f"V{self._next_version_seq(model)}"
                ev.append_event(
                    self._store,
                    "PLAN_TARGET_RESERVED",
                    plan_id=plan_id,
                    version_id=target_id,
                    at=_now(),
                )
                model["reserved"][plan_id] = target_id
            self._fail_if_armed(step)
            validate_assignment_rows(plan["proposed"])
            self._commit_version(
                model,
                plan["proposed"],
                basis=plan["base_version"],
                note=f"重排 {plan_id} 目标版本",
                explicit_id=target_id,
            )
            plan["target_version"] = target_id

        elif step == STEP_CODEMAP:
            target_id = plan["target_version"]
            if not target_id:
                raise GroupError("CODEMAP 早于 VALIDATE，方案状态损坏")
            target = self._assignments_of(model, target_id)
            edges = build_codemap(base, target)
            verify_codemap(base, target, edges)
            self._fail_if_armed(step)
            ev.append_event(
                self._store,
                "CODEMAP_BUILT",
                plan_id=plan_id,
                from_version=plan["base_version"],
                to_version=target_id,
                edges=edges,
                at=_now(),
            )

        elif step == STEP_CUTOVER:
            target_id = plan["target_version"]
            state = model["state"]
            # 先切指针（可重复写同一个值），再发事件：
            # 若崩在两者之间，重放时指针已到位，只补事件。
            if state.get("effective_version") != target_id or state.get("armed_plan"):
                state["effective_version"] = target_id
                state["armed_plan"] = None
                ev.save_state(self._store, state)
            self._fail_if_armed(step)
            ev.append_event(
                self._store,
                "PLAN_EFFECTIVE",
                plan_id=plan_id,
                version_id=target_id,
                at=_now(),
            )
            plan["status"] = PLAN_EFFECTIVE

        elif step == STEP_AUDIT:
            # 审计内容与步骤完成合并为同一个事件，杜绝重复审计。
            entry = {
                "id": uuid.uuid4().hex,
                "at": _now(),
                "action": "PLAN_EFFECTIVE",
                "detail": {
                    "actor": actor,
                    "plan": plan_id,
                    "from": plan["base_version"],
                    "to": plan["target_version"],
                    "switch": plan["switch"],
                    "policy": plan["policy"],
                },
            }
            self._fail_if_armed(step)
            ev.append_event(
                self._store,
                "PLAN_STEP_DONE",
                plan_id=plan_id,
                step=step,
                version_id=plan["target_version"],
                audit_entry=entry,
                at=_now(),
            )
            model["steps"].add((plan_id, step))
            model["plan_audits"][plan_id] = entry
            return

        else:  # pragma: no cover - 防御
            raise GroupError(f"未知步骤 {step}")

        ev.append_event(
            self._store,
            "PLAN_STEP_DONE",
            plan_id=plan_id,
            step=step,
            version_id=plan["target_version"],
            at=_now(),
        )
        model["steps"].add((plan_id, step))
        if step != STEP_CUTOVER and plan["status"] != PLAN_EFFECTIVE:
            plan["status"] = PLAN_ARMED

    def recover(self, plan_id: str, actor: str = "operator") -> dict[str, Any]:
        """从部分失败中恢复。重复调用安全且幂等。

        SHIFT_END 方案若在"提交登记（VALIDATE）"阶段失败，恢复只把它
        送回待激活状态，仍等班末激活；若失败发生在班末激活过程中，
        则继续把激活流程推完。
        """

        with self._lock:
            model = self._fold()
            plan = model["plans"].get(plan_id)
            if plan is None:
                raise GroupError(f"方案 {plan_id} 不存在")
            # 以步骤为真实凭据归一化状态：CUTOVER 既已完成就是已生效，
            # FAILED 只是给操作员看的标记，不能盖过已落盘的切换事实。
            if (plan_id, STEP_CUTOVER) in model["steps"]:
                plan["status"] = PLAN_EFFECTIVE
            if plan["status"] in (PLAN_REJECTED, PLAN_CANCELLED):
                return self.plan(plan_id)
            all_done = all((plan_id, step) in model["steps"] for step in PLAN_STEPS)
            if plan["status"] == PLAN_EFFECTIVE and all_done:
                return self.plan(plan_id)

            parked = (
                plan["switch"] == SWITCH_SHIFT_END
                and (plan_id, STEP_CUTOVER) not in model["steps"]
                and model["state"].get("armed_plan") == plan_id
                and plan_id not in model["failed"]
            )
            if parked:
                # 正常挂起等班末的方案，重复恢复不应改变任何状态。
                return self.plan(plan_id)

            if (
                plan["switch"] == SWITCH_SHIFT_END
                and (plan_id, STEP_CODEMAP) not in model["steps"]
                and model["state"].get("armed_plan") != plan_id
            ):
                # 登记阶段（VALIDATE）部分失败：只恢复到待激活，仍等班末。
                self._run_step(model, plan_id, STEP_VALIDATE)
                state = model["state"]
                state["armed_plan"] = plan_id
                ev.save_state(self._store, state)
                if plan_id in model["failed"]:
                    ev.append_event(
                        self._store, "PLAN_REARMED", plan_id=plan_id, at=_now()
                    )
                    model["failed"].discard(plan_id)
                plan["status"] = PLAN_ARMED
                ev.append_audit(
                    self._store,
                    "PLAN_RECOVER",
                    {"actor": actor, "plan": plan_id, "resumed_to": "ARMED"},
                )
                return self.plan(plan_id)

            self._advance(model, plan_id, actor)
            if plan_id in model["failed"] and plan["status"] == PLAN_EFFECTIVE:
                ev.append_event(self._store, "PLAN_REARMED", plan_id=plan_id, at=_now())
                model["failed"].discard(plan_id)
            ev.append_audit(
                self._store,
                "PLAN_RECOVER",
                {"actor": actor, "plan": plan_id, "resumed_to": plan["status"]},
            )
            return self.plan(plan_id)

    def activate(self, plan_id: str, actor: str = "shift-lead") -> dict[str, Any]:
        """本班结束：激活 SHIFT_END 待激活方案。"""

        with self._lock:
            model = self._fold()
            plan = model["plans"].get(plan_id)
            if plan is None:
                raise GroupError(f"方案 {plan_id} 不存在")
            armed = self._armed_plan(model)
            if armed is None or armed["plan_id"] != plan_id:
                raise PlanStateError(f"方案 {plan_id} 不是当前待激活方案")
            if plan["switch"] != SWITCH_SHIFT_END:
                raise PlanStateError("只有 SHIFT_END 方案需要在班末激活")
            self._advance(model, plan_id, actor)
            return self.plan(plan_id)

    def cancel(self, plan_id: str, actor: str = "shift-lead") -> dict[str, Any]:
        """撤销待激活方案（仅班末切换前允许）。重复调用幂等。"""

        with self._lock:
            model = self._fold()
            plan = model["plans"].get(plan_id)
            if plan is None:
                raise GroupError(f"方案 {plan_id} 不存在")
            if plan["status"] == PLAN_CANCELLED:
                return self.plan(plan_id)
            armed = self._armed_plan(model)
            if armed is None or armed["plan_id"] != plan_id:
                raise PlanStateError("只能撤销处于待激活状态的方案")
            state = model["state"]
            state["armed_plan"] = None
            ev.save_state(self._store, state)
            ev.append_event(self._store, "PLAN_CANCELLED", plan_id=plan_id, at=_now())
            plan["status"] = PLAN_CANCELLED
            ev.append_audit(
                self._store, "PLAN_CANCEL", {"actor": actor, "plan": plan_id}
            )
            return self.plan(plan_id)

    # ==================================================================
    # 读侧：新旧编号并存识别
    # ==================================================================
    def _readable_versions(self, model: dict[str, Any]) -> list[tuple[str, dict[str, Assignment]]]:
        """读方当前能认的版本：生效版本 + 待激活目标版本（并存期）。"""

        result: list[tuple[str, dict[str, Assignment]]] = []
        effective_id = model["state"].get("effective_version")
        if effective_id:
            result.append((effective_id, self._assignments_of(model, effective_id)))
        armed = self._armed_plan(model)
        if armed and armed.get("target_version"):
            target_id = armed["target_version"]
            if target_id != effective_id:
                result.append((target_id, self._assignments_of(model, target_id)))
        return result

    def resolve_code(self, code: str) -> dict[str, Any]:
        """用现场标牌池号反查池身份，新旧号同时识别。

        并存期内若新旧版本恰好复用了同一池号指代不同的池，会出现多
        个候选并置 ``ambiguous=True``，由值班员结合版本消歧，不静默
        选择。
        """

        with self._lock:
            model = self._fold()
            matches: list[dict[str, Any]] = []
            for version_id, rows in self._readable_versions(model):
                for pool_id in sorted(rows):
                    row = rows[pool_id]
                    if row.code == code:
                        matches.append(
                            {
                                "version": version_id,
                                "pool_id": row.pool_id,
                                "zone": row.zone,
                                "sequence": row.sequence,
                                "role": self._version_role(model, version_id),
                            }
                        )
            unique_pools = {item["pool_id"] for item in matches}
            return {
                "code": code,
                "matches": matches,
                "ambiguous": len(unique_pools) > 1,
                "pool_id": next(iter(unique_pools)) if len(unique_pools) == 1 else None,
            }

    def resolve_zone(self, zone: int) -> dict[str, Any]:
        """按分区反查，规则同 :meth:`resolve_code`。"""

        with self._lock:
            model = self._fold()
            matches: list[dict[str, Any]] = []
            for version_id, rows in self._readable_versions(model):
                for pool_id in sorted(rows):
                    row = rows[pool_id]
                    if row.zone == zone:
                        matches.append(
                            {
                                "version": version_id,
                                "pool_id": row.pool_id,
                                "code": row.code,
                                "sequence": row.sequence,
                                "role": self._version_role(model, version_id),
                            }
                        )
            unique_pools = {item["pool_id"] for item in matches}
            return {
                "zone": zone,
                "matches": matches,
                "ambiguous": len(unique_pools) > 1,
                "pool_id": next(iter(unique_pools)) if len(unique_pools) == 1 else None,
            }

    def _version_role(self, model: dict[str, Any], version_id: str) -> str:
        if model["state"].get("effective_version") == version_id:
            return "EFFECTIVE"
        armed = self._armed_plan(model)
        if armed and armed.get("target_version") == version_id:
            return "PENDING"
        return "HISTORY"

    def translate(self, code: str, from_version: str, to_version: str) -> dict[str, Any]:
        """把池号从任意历史版本翻译到另一版本。

        池标识终身不变，是天然的对照链：先在旧版本用池号找到池，再
        到目标版本读它当时的编号。历史记录按写下时的版本号调用本
        方法即可还原"那条记录说的是今天的哪座池"。
        """

        with self._lock:
            model = self._fold()
            if from_version not in model["versions"] or to_version not in model["versions"]:
                raise GroupError("翻译的起点或终点版本不存在")
            source = self._assignments_of(model, from_version)
            pool_id = next(
                (row.pool_id for row in source.values() if row.code == code), None
            )
            if pool_id is None:
                return {"code": code, "from": from_version, "to": to_version, "found": False}
            target = self._assignments_of(model, to_version)
            row = target.get(pool_id)
            return {
                "code": code,
                "from": from_version,
                "to": to_version,
                "found": row is not None,
                "pool_id": pool_id,
                "code_at_target": None if row is None else row.code,
                "zone_at_target": None if row is None else row.zone,
                "sequence_at_target": None if row is None else row.sequence,
            }

    # ==================================================================
    # 值班记录：只追加、不可变
    # ==================================================================
    def record_duty(
        self,
        code: str,
        action: str,
        actor: str = "operator",
        note: str = "",
    ) -> dict[str, Any]:
        """记一条值班记录。

        记录永久保存书写当时的池号原文与版本号，后续任何重排都不会
        回改；``pool_id`` 仅作解析辅助，历史不可变由 append-only 存储
        保证（没有任何代码路径会更新 duty 列表）。
        """

        with self._lock:
            model = self._fold()
            version_id = model["state"].get("effective_version")
            if not version_id:
                raise GroupError("尚未建账")
            resolved = self.resolve_code(code)
            record = {
                "id": uuid.uuid4().hex,
                "at": _now(),
                "version": version_id,
                "code_raw": code,
                "pool_id": resolved["pool_id"],
                "ambiguous_then": resolved["ambiguous"],
                "action": action,
                "actor": actor,
                "note": note,
            }
            ev.append_duty(self._store, record)
            ev.append_audit(
                self._store,
                "DUTY_RECORD",
                {
                    "actor": actor,
                    "record": record["id"],
                    "version": version_id,
                    "code": code,
                    "pool_id": resolved["pool_id"],
                },
            )
            return record

    def duty_records(self) -> list[dict[str, Any]]:
        return ev.load_duty(self._store)

    # ==================================================================
    # 控制台展示视图
    # ==================================================================
    def layout(self) -> dict[str, Any]:
        """当前池组布局；并存期附带待激活版本与新旧对照。"""

        with self._lock:
            model = self._fold()
            effective_id = model["state"].get("effective_version")
            payload: dict[str, Any] = {
                "effective_version": effective_id,
                "rows": [],
                "pending": None,
            }
            if effective_id:
                rows = self._assignments_of(model, effective_id)
                payload["rows"] = [
                    rows[pool].as_dict()
                    for pool in sorted(rows, key=lambda pid: rows[pid].zone)
                ]
            armed = self._armed_plan(model)
            if armed and armed.get("target_version"):
                new_rows = self._assignments_of(model, armed["target_version"])
                old_rows = self._assignments_of(model, armed["base_version"])
                edges = build_codemap(old_rows, new_rows)
                payload["pending"] = {
                    "plan_id": armed["plan_id"],
                    "target_version": armed["target_version"],
                    "switch": armed["switch"],
                    "policy": armed["policy"],
                    "codemap": [edge for edge in edges if edge["changed"]],
                }
            return payload

    def plan(self, plan_id: str) -> dict[str, Any]:
        with self._lock:
            model = self._fold()
            plan = model["plans"].get(plan_id)
            if plan is None:
                raise GroupError(f"方案 {plan_id} 不存在")
            done = {step for pid, step in model["steps"] if pid == plan_id}
            return {
                "plan_id": plan["plan_id"],
                "base_version": plan["base_version"],
                "target_version": plan["target_version"],
                "switch": plan["switch"],
                "policy": plan["policy"],
                "status": plan["status"],
                "at": plan["at"],
                "note": plan["note"],
                "steps": [{"step": step, "done": step in done} for step in PLAN_STEPS],
                "proposed": [row.as_dict() for row in plan["proposed"]],
            }

    def plans(self) -> list[dict[str, Any]]:
        with self._lock:
            model = self._fold()
            return [self.plan(plan_id) for plan_id in sorted(model["plans"])]

    def versions(self) -> list[dict[str, Any]]:
        with self._lock:
            model = self._fold()
            return [
                {
                    "version_id": vid,
                    "seq": version["seq"],
                    "basis": version["basis"],
                    "at": version["at"],
                    "pools": len(version["rows"]),
                    "role": self._version_role(model, vid),
                }
                for vid, version in sorted(
                    model["versions"].items(), key=lambda item: item[1]["seq"]
                )
            ]

    def audit_entries(self) -> list[dict[str, Any]]:
        """全部审计：显式审计流 + 生效步骤内嵌审计，按时间归并。"""

        with self._lock:
            model = self._fold()
            entries = ev.load_audit(self._store)
            entries.extend(model["plan_audits"].values())
            return sorted(entries, key=lambda entry: (entry.get("at", ""), entry.get("id", "")))
