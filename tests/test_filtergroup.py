"""滤池池组与分区管理的行为测试。"""

from __future__ import annotations

import json
import tempfile
import unittest

from waterplant.filtergroup import (
    POLICY_APPLY_CONFLICT_ONLY,
    POLICY_REJECT_BATCH,
    SWITCH_IMMEDIATE,
    SWITCH_SHIFT_END,
    Assignment,
    ConflictError,
    GroupManager,
    PlanStateError,
)
from waterplant.filtergroup.manager import apply_conflict_only
from waterplant.filtergroup.model import (
    STEP_AUDIT,
    STEP_CODEMAP,
    STEP_CUTOVER,
    STEP_VALIDATE,
    detect_conflicts,
    index_assignments,
)
from waterplant.store.commands import load_commands
from waterplant.filtergroup import events as fevents
from waterplant.store import Store


def initial_rows() -> list[Assignment]:
    # V1：池号/分区/投运顺序三列一一对应，同序同值便于阅读。
    return [
        Assignment("P-A", "1#", 1, 1),
        Assignment("P-B", "2#", 2, 2),
        Assignment("P-C", "3#", 3, 3),
        Assignment("P-D", "4#", 4, 4),
    ]


def new_manager(tmp: str) -> GroupManager:
    return GroupManager(Store.open(f"{tmp}/state.json"))


class UniquenessCase(unittest.TestCase):
    def test_bootstrap_rejects_duplicate_zone(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            manager = new_manager(tmp)
            rows = [
                Assignment("P-A", "1#", 1, 1),
                Assignment("P-B", "2#", 1, 2),  # 同分区
            ]
            with self.assertRaises(ConflictError):
                manager.bootstrap(rows)

    def test_bootstrap_rejects_duplicate_sequence(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            manager = new_manager(tmp)
            with self.assertRaises(ConflictError):
                manager.bootstrap(
                    [Assignment("P-A", "1#", 1, 1), Assignment("P-B", "2#", 2, 1)]
                )

    def test_layout_is_unique_triplet_mapping(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            manager = new_manager(tmp)
            manager.bootstrap(initial_rows())
            layout = manager.layout()
            rows = layout["rows"]
            self.assertEqual(len({r["zone"] for r in rows}), 4)
            self.assertEqual(len({r["sequence"] for r in rows}), 4)
            self.assertEqual(len({r["code"] for r in rows}), 4)
            self.assertEqual(layout["effective_version"], "V1")

    def test_bootstrap_is_once_only(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            manager = new_manager(tmp)
            manager.bootstrap(initial_rows())
            with self.assertRaises(ValueError):
                manager.bootstrap(initial_rows())


class PlanValidationCase(unittest.TestCase):
    def test_switch_and_policy_must_be_explicit(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            manager = new_manager(tmp)
            manager.bootstrap(initial_rows())
            with self.assertRaises(ValueError):
                manager.submit_plan(initial_rows(), switch="", policy=POLICY_REJECT_BATCH)
            with self.assertRaises(ValueError):
                manager.submit_plan(initial_rows(), switch=SWITCH_IMMEDIATE, policy="")

    def test_structural_conflict_always_rejects_whole_batch(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            manager = new_manager(tmp)
            manager.bootstrap(initial_rows())
            # 少了 P-D，即便策略是"只改冲突项"也必须整批拒绝。
            rows = [
                Assignment("P-A", "2#", 2, 2),
                Assignment("P-B", "3#", 3, 3),
                Assignment("P-C", "4#", 4, 4),
            ]
            with self.assertRaises(ConflictError) as caught:
                manager.submit_plan(
                    rows, SWITCH_IMMEDIATE, POLICY_APPLY_CONFLICT_ONLY
                )
            self.assertEqual(caught.exception.conflicts[0]["type"], "MISSING_POOL")
            # 被拒绝不得改变生效版本。
            self.assertEqual(manager.layout()["effective_version"], "V1")

    def test_duplicate_pool_in_payload_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            manager = new_manager(tmp)
            manager.bootstrap(initial_rows())
            rows = initial_rows() + [Assignment("P-A", "9#", 9, 9)]
            with self.assertRaises(ConflictError) as caught:
                manager.submit_plan(rows, SWITCH_IMMEDIATE, POLICY_APPLY_CONFLICT_ONLY)
            self.assertEqual(caught.exception.conflicts[0]["type"], "DUPLICATE_POOL")


class ConflictPolicyCase(unittest.TestCase):
    def test_reject_batch_refuses_on_occupancy(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            manager = new_manager(tmp)
            manager.bootstrap(initial_rows())
            # A、B 都想占 5#/5/5：整批拒绝。
            rows = [
                Assignment("P-A", "5#", 5, 5),
                Assignment("P-B", "5#", 5, 5),
                Assignment("P-C", "3#", 3, 3),
                Assignment("P-D", "4#", 4, 4),
            ]
            with self.assertRaises(ConflictError):
                manager.submit_plan(rows, SWITCH_IMMEDIATE, POLICY_REJECT_BATCH)
            self.assertEqual(manager.layout()["effective_version"], "V1")

    def test_apply_conflict_only_keeps_non_conflicting_moves(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            manager = new_manager(tmp)
            manager.bootstrap(initial_rows())
            # A、B 申请完全相同的目标格 5/5/5（两个池抢一格）：
            # 只有池标识靠前的一个能落地，另一个回退原位；C 无关移动保留。
            rows = [
                Assignment("P-A", "5#", 5, 5),
                Assignment("P-B", "5#", 5, 5),
                Assignment("P-C", "6#", 6, 6),
                Assignment("P-D", "4#", 4, 4),
            ]
            plan = manager.submit_plan(
                rows, SWITCH_IMMEDIATE, POLICY_APPLY_CONFLICT_ONLY
            )
            self.assertEqual(plan["status"], "EFFECTIVE")
            layout = {r["pool_id"]: r for r in manager.layout()["rows"]}
            winners = {layout["P-A"]["code"], layout["P-B"]["code"]}
            # 池标识字典序靠前的 P-A 确定性抢到 5#，P-B 回退原号 2#。
            self.assertEqual(layout["P-A"]["code"], "5#")
            self.assertEqual(layout["P-B"]["code"], "2#")
            self.assertEqual(layout["P-C"]["code"], "6#")
            self.assertEqual(layout["P-D"]["code"], "4#")
            codes = [r["code"] for r in manager.layout()["rows"]]
            self.assertEqual(len(codes), len(set(codes)))

    def test_apply_conflict_only_cycle_terminates(self) -> None:
        # 经典轮转 A->2, B->3, C->1 无冲突；再构造一个钻进静池格子的
        # 对抗场景，验证淘汰必然收敛为合法双射。
        base = index_assignments(initial_rows())
        rows = [
            Assignment("P-A", "2#", 2, 2),  # 钻进 B 的原格
            Assignment("P-B", "5#", 5, 5),
            Assignment("P-C", "6#", 6, 6),
            Assignment("P-D", "4#", 4, 4),  # 静止
        ]
        final, reverted = apply_conflict_only(base, rows)
        zones = sorted(r.zone for r in final)
        seqs = sorted(r.sequence for r in final)
        codes = sorted(r.code for r in final)
        # 结果必须是合法双射。
        self.assertEqual(len(set(zones)), 4)
        self.assertEqual(len(set(seqs)), 4)
        self.assertEqual(len(set(codes)), 4)
        pool_map = {r.pool_id: r for r in final}
        # P-B、P-C 的无冲突移动保留；P-A 钻进 B 原格的抢占被确定性消解。
        self.assertEqual(pool_map["P-B"].code, "5#")
        self.assertEqual(pool_map["P-C"].code, "6#")
        self.assertEqual(pool_map["P-D"].code, "4#")
        # 被回退的池一定是申请移动者，静止的 P-D 永不在回退名单里。
        self.assertNotIn("P-D", {item["pool_id"] for item in reverted})


class ImmediateCutoverCase(unittest.TestCase):
    def test_immediate_switch_creates_version_and_codemap(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            manager = new_manager(tmp)
            manager.bootstrap(initial_rows())
            rows = [
                Assignment("P-A", "2#", 2, 2),
                Assignment("P-B", "1#", 1, 1),
                Assignment("P-C", "3#", 3, 3),
                Assignment("P-D", "4#", 4, 4),
            ]
            plan = manager.submit_plan(rows, SWITCH_IMMEDIATE, POLICY_REJECT_BATCH)
            self.assertEqual(plan["status"], "EFFECTIVE")
            self.assertEqual(plan["target_version"], "V2")
            self.assertEqual(manager.layout()["effective_version"], "V2")
            # 版本链与对照边
            versions = manager.versions()
            self.assertEqual([v["version_id"] for v in versions], ["V1", "V2"])
            # A 在 V1 是 1#，在 V2 是 2#；translate 沿 pool_id 追踪。
            moved = manager.translate("1#", "V1", "V2")
            self.assertEqual(moved["pool_id"], "P-A")
            self.assertEqual(moved["code_at_target"], "2#")


class ShiftEndCase(unittest.TestCase):
    def _pending_rows(self) -> list[Assignment]:
        return [
            Assignment("P-A", "2#", 2, 2),
            Assignment("P-B", "1#", 1, 1),
            Assignment("P-C", "3#", 3, 3),
            Assignment("P-D", "4#", 4, 4),
        ]

    def test_shift_end_keeps_both_versions_readable(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            manager = new_manager(tmp)
            manager.bootstrap(initial_rows())
            plan = manager.submit_plan(
                self._pending_rows(), SWITCH_SHIFT_END, POLICY_REJECT_BATCH
            )
            self.assertEqual(plan["status"], "ARMED")
            layout = manager.layout()
            self.assertEqual(layout["effective_version"], "V1")
            self.assertIsNotNone(layout["pending"])
            # 新旧号并存：新版本未复用的旧号 3# 仍唯一指 P-C；
            # 而 2# 在旧版本指 P-B、新版本指 P-A => 歧义如实上报。
            old_hit = manager.resolve_code("3#")
            self.assertEqual(old_hit["pool_id"], "P-C")
            self.assertFalse(old_hit["ambiguous"])
            new_hit = manager.resolve_code("2#")
            self.assertTrue(new_hit["ambiguous"])
            self.assertIsNone(new_hit["pool_id"])
            roles = {m["role"] for m in new_hit["matches"]}
            self.assertEqual(roles, {"EFFECTIVE", "PENDING"})

    def test_activate_at_shift_change(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            manager = new_manager(tmp)
            manager.bootstrap(initial_rows())
            plan = manager.submit_plan(
                self._pending_rows(), SWITCH_SHIFT_END, POLICY_REJECT_BATCH
            )
            # 激活前不能再提新方案。
            with self.assertRaises(PlanStateError):
                manager.submit_plan(
                    self._pending_rows(), SWITCH_IMMEDIATE, POLICY_REJECT_BATCH
                )
            done = manager.activate(plan["plan_id"])
            self.assertEqual(done["status"], "EFFECTIVE")
            self.assertEqual(manager.layout()["effective_version"], "V2")
            self.assertIsNone(manager.layout()["pending"])

    def test_cancel_before_shift_change_is_idempotent(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            manager = new_manager(tmp)
            manager.bootstrap(initial_rows())
            plan = manager.submit_plan(
                self._pending_rows(), SWITCH_SHIFT_END, POLICY_REJECT_BATCH
            )
            manager.cancel(plan["plan_id"])
            manager.cancel(plan["plan_id"])  # 重复撤销幂等
            self.assertIsNone(manager.layout()["pending"])
            # 生效版本未动，且可以重新提方案。
            self.assertEqual(manager.layout()["effective_version"], "V1")
            again = manager.submit_plan(
                self._pending_rows(), SWITCH_SHIFT_END, POLICY_REJECT_BATCH
            )
            self.assertEqual(again["status"], "ARMED")


class RecoveryCase(unittest.TestCase):
    def _renumber(self) -> list[Assignment]:
        return [
            Assignment("P-A", "2#", 2, 2),
            Assignment("P-B", "3#", 3, 3),
            Assignment("P-C", "4#", 4, 4),
            Assignment("P-D", "1#", 1, 1),
        ]

    def test_fail_and_recover_at_each_step_is_idempotent(self) -> None:
        for step in (STEP_VALIDATE, STEP_CODEMAP, STEP_CUTOVER, STEP_AUDIT):
            with self.subTest(step=step), tempfile.TemporaryDirectory() as tmp:
                manager = new_manager(tmp)
                manager.bootstrap(initial_rows())
                manager.inject_failure(step)
                with self.assertRaises(RuntimeError):
                    manager.submit_plan(
                        self._renumber(), SWITCH_IMMEDIATE, POLICY_REJECT_BATCH
                    )
                plan_id = manager.plans()[-1]["plan_id"]
                # 第一次恢复前推；再多调两次，结果必须完全一致且无副作用。
                recovered = manager.recover(plan_id)
                again_1 = manager.recover(plan_id)
                again_2 = manager.recover(plan_id)
                self.assertEqual(recovered["status"], "EFFECTIVE")
                self.assertEqual(again_1, recovered)
                self.assertEqual(again_2, recovered)
                self.assertTrue(all(s["done"] for s in recovered["steps"]))
                self.assertEqual(manager.layout()["effective_version"], "V2")
                # 对照一致性：最终布局与 V1 经 pool_id 一一对应。
                t1 = manager.translate("1#", "V1", "V2")
                self.assertEqual(t1["code_at_target"], "2#")
                t4 = manager.translate("4#", "V1", "V2")
                self.assertEqual(t4["code_at_target"], "1#")
                self._assert_event_invariants(tmp)

    def test_recover_shift_end_failure_does_not_early_activate(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            manager = new_manager(tmp)
            manager.bootstrap(initial_rows())
            manager.inject_failure(STEP_VALIDATE)
            with self.assertRaises(RuntimeError):
                manager.submit_plan(
                    self._renumber(), SWITCH_SHIFT_END, POLICY_REJECT_BATCH
                )
            plan_id = manager.plans()[-1]["plan_id"]
            recovered = manager.recover(plan_id)
            # 恢复只回到待激活，不能替值班员做班末切换决定。
            self.assertEqual(recovered["status"], "ARMED")
            self.assertEqual(manager.layout()["effective_version"], "V1")
            self.assertIsNotNone(manager.layout()["pending"])
            # 重复恢复保持挂起，不激活。
            manager.recover(plan_id)
            self.assertEqual(manager.layout()["effective_version"], "V1")
            done = manager.activate(plan_id)
            self.assertEqual(done["status"], "EFFECTIVE")

    def test_recover_after_cutover_crash_completes_audit_once(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            manager = new_manager(tmp)
            manager.bootstrap(initial_rows())
            manager.inject_failure(STEP_AUDIT)
            with self.assertRaises(RuntimeError):
                manager.submit_plan(
                    self._renumber(), SWITCH_IMMEDIATE, POLICY_REJECT_BATCH
                )
            plan_id = manager.plans()[-1]["plan_id"]
            manager.recover(plan_id)
            manager.recover(plan_id)
            audits = manager.audit_entries()
            effective_audits = [e for e in audits if e["action"] == "PLAN_EFFECTIVE"]
            self.assertEqual(len(effective_audits), 1)

    def _assert_event_invariants(self, tmp: str) -> None:
        # 版本快照只追加，不允许同一版本两条提交。
        store = Store.open(f"{tmp}/state.json")
        events = fevents.load_events(store)
        committed = [e for e in events if e["kind"] == "VERSION_COMMITTED"]
        self.assertEqual(len(committed), len({e["version_id"] for e in committed}))
        # 每个步骤事件至多一条/方案。
        done = [e for e in events if e["kind"] == "PLAN_STEP_DONE"]
        keys = [(e["plan_id"], e["step"]) for e in done]
        self.assertEqual(len(keys), len(set(keys)))


class DutyHistoryCase(unittest.TestCase):
    def test_duty_records_are_immutable_across_renumber(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            manager = new_manager(tmp)
            manager.bootstrap(initial_rows())
            manager.record_duty("1#", "BACKWASH", note="V1 期间冲洗")
            # 重排：1# 在新版本改由 P-D 占用（P-A 变 2#）。
            rows = [
                Assignment("P-A", "2#", 2, 2),
                Assignment("P-B", "3#", 3, 3),
                Assignment("P-C", "4#", 4, 4),
                Assignment("P-D", "1#", 1, 1),
            ]
            manager.submit_plan(rows, SWITCH_IMMEDIATE, POLICY_REJECT_BATCH)
            records = manager.duty_records()
            self.assertEqual(len(records), 1)
            old = records[0]
            self.assertEqual(old["code_raw"], "1#")
            self.assertEqual(old["version"], "V1")
            self.assertEqual(old["pool_id"], "P-A")
            # 用记录上的版本号翻译：那条旧记录 = 今天的 2#。
            translated = manager.translate("1#", old["version"], "V2")
            self.assertEqual(translated["pool_id"], "P-A")
            self.assertEqual(translated["code_at_target"], "2#")
            # append-only：原始列表内容字节层面未被回改。
            raw = load_commands(manager._store, fevents.DUTY_KEY)  # noqa: SLF001
            self.assertIn("V1 期间冲洗", raw[0])


class PersistenceCase(unittest.TestCase):
    def test_state_survives_reopen_during_coexistence(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            manager = new_manager(tmp)
            manager.bootstrap(initial_rows())
            rows = [
                Assignment("P-A", "2#", 2, 2),
                Assignment("P-B", "1#", 1, 1),
                Assignment("P-C", "3#", 3, 3),
                Assignment("P-D", "4#", 4, 4),
            ]
            plan = manager.submit_plan(
                rows, SWITCH_SHIFT_END, POLICY_REJECT_BATCH
            )
            manager = new_manager(tmp)  # 重开
            layout = manager.layout()
            self.assertEqual(layout["effective_version"], "V1")
            self.assertEqual(layout["pending"]["plan_id"], plan["plan_id"])
            manager.activate(plan["plan_id"])
            self.assertEqual(manager.layout()["effective_version"], "V2")

    def test_conflicts_reported_with_detail(self) -> None:
        base = index_assignments(initial_rows())
        proposed = index_assignments(
            [
                Assignment("P-A", "9#", 9, 9),
                Assignment("P-B", "2#", 2, 2),
                Assignment("P-C", "3#", 3, 3),
                # 缺 P-D
            ]
        )
        conflicts = detect_conflicts(base, proposed)
        kinds = {c["type"] for c in conflicts}
        self.assertIn("MISSING_POOL", kinds)


if __name__ == "__main__":
    unittest.main()
