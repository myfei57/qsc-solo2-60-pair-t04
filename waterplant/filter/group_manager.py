"""Persistent, audited management of filter groups and bed partitions."""

from __future__ import annotations

import copy
import hashlib
import json
import threading
import time
import uuid
from typing import TYPE_CHECKING

from waterplant.filter.group_report import (
    DEFAULT_GROUP_ID,
    PHASE_ACTIVATING,
    PHASE_ACTIVE,
    PHASE_APPLYING,
    PHASE_BASELINE,
    PHASE_CANCELED,
    PHASE_ROLLED_BACK,
    PHASE_ROLLING_BACK,
    PHASE_STAGED,
    PHASE_SUPERSEDED,
    POLICY_APPLY_NON_CONFLICTING,
    POLICY_REJECT_BATCH,
    SWITCH_IMMEDIATE,
    Assignment,
    Conflict,
    GroupConflictError,
    GROUP_STATE_KEY,
    validate_conflict_policy,
    validate_identifier,
    validate_sequence,
    validate_switch_mode,
)
from waterplant.filter.report import validate_zone
from waterplant.store.store import Store

if TYPE_CHECKING:
    from waterplant.filter.bank import Bank


class FilterGroupManager:
    """Authoritative store for bed code, zone and commissioning sequence.

    The stable ``bed_id`` identifies the physical filter and never changes.
    Codes, zones and sequences change only through immutable revisions. Every
    revision keeps the previous and next assignment so historical records can
    continue to use identifiers that were valid when they were written.
    """

    def __init__(self, store: Store, bank: "Bank | None" = None) -> None:
        self._store = store
        self._bank = bank
        self._lock = threading.RLock()
        self._state = self._load()
        self.ensure_group(
            DEFAULT_GROUP_ID,
            "default",
            switch_mode=SWITCH_IMMEDIATE,
            conflict_policy=POLICY_REJECT_BATCH,
        )
        self.recover()
        if self._bank is not None:
            self._synchronize_bank()

    def create_group(
        self,
        group_id: str,
        name: str = "",
        switch_mode: str = SWITCH_IMMEDIATE,
        conflict_policy: str = POLICY_REJECT_BATCH,
    ) -> dict[str, object]:
        """Create a distinct filter group."""

        group_id = group_id.strip()
        name = name.strip() or group_id
        validate_identifier(group_id, "group id")
        validate_identifier(name, "group name")
        validate_switch_mode(switch_mode)
        validate_conflict_policy(conflict_policy)
        with self._lock:
            if self._group_locked(group_id) is not None:
                raise ValueError(f"filter group {group_id} already exists")
            group = {
                "id": group_id,
                "name": name,
                "switch_mode": switch_mode,
                "conflict_policy": conflict_policy,
                "active_revision": 0,
                "staged_revision": None,
            }
            revision = self._revision_record(
                group_id=group_id,
                parent_id=None,
                phase=PHASE_BASELINE,
                assignments={},
                reason="group created",
                operator="system",
            )
            self._state["groups"].append(group)
            self._revisions_locked(group_id).append(revision)
            self._append_event_locked(
                group_id, revision["id"], "group_created", {"policy": group}
            )
            self._save_locked()
            return self.group_state_locked(group_id)

    def ensure_group(
        self,
        group_id: str = DEFAULT_GROUP_ID,
        name: str = "default",
        switch_mode: str = SWITCH_IMMEDIATE,
        conflict_policy: str = POLICY_REJECT_BATCH,
    ) -> dict[str, object]:
        """Fetch a group, creating it once with fixed default policies."""

        with self._lock:
            if self._group_locked(group_id) is None:
                return self.create_group(
                    group_id,
                    name=name,
                    switch_mode=switch_mode,
                    conflict_policy=conflict_policy,
                )
            return self.group_state_locked(group_id)

    def set_policy(
        self,
        group_id: str,
        switch_mode: str | None = None,
        conflict_policy: str | None = None,
        operator: str = "operator",
    ) -> dict[str, object]:
        """Change the standing operating policy for a group."""

        with self._lock:
            group = self._require_group_locked(group_id)
            self._require_no_staged_locked(group)
            if switch_mode is not None:
                validate_switch_mode(switch_mode)
                group["switch_mode"] = switch_mode
            if conflict_policy is not None:
                validate_conflict_policy(conflict_policy)
                group["conflict_policy"] = conflict_policy
            self._append_event_locked(
                group_id,
                group["active_revision"],
                "policy_updated",
                {
                    "switch_mode": group["switch_mode"],
                    "conflict_policy": group["conflict_policy"],
                    "operator": operator,
                },
            )
            self._save_locked()
            return self.group_state_locked(group_id)

    def commission(
        self,
        group_id: str,
        bed_id: str,
        code: str,
        zone: int,
        sequence: int,
        reason: str = "commission",
        operator: str = "operator",
        switch_mode: str | None = None,
    ) -> dict[str, object]:
        """Add one physical bed with its unique code, zone and sequence."""

        assignment = self._valid_assignment(bed_id, code, zone, sequence)
        with self._lock:
            self._require_physical_bed_absent_locked(assignment.bed_id)
            group = self._require_group_locked(group_id)
            self._require_no_staged_locked(group)
            switch_mode = switch_mode or group["switch_mode"]
            current = self._active_assignments_locked(group_id)
            self._reject_assignment_conflicts_locked(group_id, assignment, current)
            self._reject_global_conflicts_locked(group_id, [assignment])
            target = dict(current)
            target[assignment.bed_id] = assignment
            return self._create_revision(
                group_id,
                current,
                target,
                reason=reason,
                operator=operator,
                switch_mode=switch_mode,
                action="commission",
            )

    def decommission(
        self,
        group_id: str,
        bed_id: str,
        reason: str = "decommission",
        operator: str = "operator",
        switch_mode: str | None = None,
    ) -> dict[str, object]:
        """Remove a physical bed without erasing its historical revisions."""

        with self._lock:
            group = self._require_group_locked(group_id)
            self._require_no_staged_locked(group)
            current = self._active_assignments_locked(group_id)
            if bed_id not in current:
                raise ValueError(f"filter bed {bed_id} is not active in {group_id}")
            target = {key: value for key, value in current.items() if key != bed_id}
            switch_mode = switch_mode or group["switch_mode"]
            return self._create_revision(
                group_id,
                current,
                target,
                reason=reason,
                operator=operator,
                switch_mode=switch_mode,
                action="decommission",
            )

    def rezone(
        self,
        group_id: str,
        bed_id: str,
        zone: int,
        operator: str = "operator",
        switch_mode: str | None = None,
    ) -> dict[str, object]:
        """Move one bed, rejecting the request when its new zone is occupied."""

        validate_zone(zone)
        with self._lock:
            group = self._require_group_locked(group_id)
            self._require_no_staged_locked(group)
            current = self._active_assignments_locked(group_id)
            if bed_id not in current:
                raise ValueError(f"filter bed {bed_id} is not active in {group_id}")
            old = current[bed_id]
            assignment = Assignment(
                bed_id=bed_id, code=old.code, zone=zone, sequence=old.sequence
            )
            self._reject_assignment_conflicts_locked(group_id, assignment, current)
            self._reject_global_conflicts_locked(group_id, [assignment])
            target = dict(current)
            target[bed_id] = assignment
            return self._create_revision(
                group_id,
                current,
                target,
                reason=f"rezone {old.code}",
                operator=operator,
                switch_mode=switch_mode or group["switch_mode"],
                action="rezone",
            )

    def reorder(
        self,
        group_id: str,
        assignments: list[Assignment | dict[str, object]],
        complete: bool = True,
        reason: str = "reorder",
        operator: str = "operator",
        switch_mode: str | None = None,
        conflict_policy: str | None = None,
    ) -> dict[str, object]:
        """Apply a full rearrangement or an explicit subset change.

        ``complete=True`` is an all-or-nothing bijection over every active bed
        in the group. Partial requests use the group's fixed conflict policy
        unless the caller explicitly names the same policy allowed for that
        group.
        """

        requested = [self._assignment_from(item) for item in assignments]
        with self._lock:
            group = self._require_group_locked(group_id)
            self._require_no_staged_locked(group)
            switch_mode = switch_mode or group["switch_mode"]
            validate_switch_mode(switch_mode)
            conflict_policy = conflict_policy or group["conflict_policy"]
            validate_conflict_policy(conflict_policy)
            if conflict_policy != group["conflict_policy"]:
                raise ValueError(
                    f"group conflict policy is fixed to {group['conflict_policy']}"
                )
            current = self._active_assignments_locked(group_id)
            if complete:
                target = self._complete_target_locked(group_id, current, requested)
                self._reject_global_conflicts_locked(group_id, list(target.values()))
                result = self._create_revision(
                    group_id,
                    current,
                    target,
                    reason=reason,
                    operator=operator,
                    switch_mode=switch_mode,
                    action="reorder",
                )
                result["conflicts"] = []
                result["accepted"] = [item.bed_id for item in requested]
                return result

            accepted, conflicts = self._accepted_partial_locked(
                group_id, current, requested, conflict_policy
            )
            if conflicts and conflict_policy == POLICY_REJECT_BATCH:
                raise GroupConflictError(
                    "reorder rejected: "
                    + ", ".join(f"{item.bed_id}:{item.reason}" for item in conflicts)
                )
            target = dict(current)
            for assignment in accepted:
                target[assignment.bed_id] = assignment
            result = self._create_revision(
                group_id,
                current,
                target,
                reason=reason,
                operator=operator,
                switch_mode=switch_mode,
                action="partial_reorder",
            )
            result["accepted"] = [item.bed_id for item in accepted]
            result["conflicts"] = [item.as_dict() for item in conflicts]
            return result

    def activate(self, group_id: str, revision_id: int | None = None) -> dict[str, object]:
        """Switch a staged revision into service."""

        with self._lock:
            group = self._require_group_locked(group_id)
            staged_id = group.get("staged_revision")
            if staged_id is None:
                raise ValueError(f"group {group_id} has no staged revision")
            if revision_id is not None and int(revision_id) != int(staged_id):
                raise ValueError("requested revision is not the staged revision")
            revision = self._revision_locked(group_id, int(staged_id))
            if revision["phase"] != PHASE_STAGED:
                raise ValueError("staged revision is not ready to activate")
            parent = self._revision_locked(group_id, int(group["active_revision"]))
            self._validate_assignment_list_locked(
                list(self._revision_assignments(revision).values())
            )
            self._reject_global_conflicts_locked(
                group_id, list(self._revision_assignments(revision).values())
            )
            self._set_phase_locked(revision, PHASE_ACTIVATING)
            self._save_locked()
            prior = self._revision_assignments(parent)
            target = self._revision_assignments(revision)
            try:
                self._apply_to_bank_locked(group_id, prior, target)
            except Exception:
                self._finish_failed_revision_locked(group, parent, revision, prior)
                raise
            parent["phase"] = PHASE_SUPERSEDED
            revision["phase"] = PHASE_ACTIVE
            revision["completed_at"] = self._now()
            group["active_revision"] = revision["id"]
            group["staged_revision"] = None
            self._append_event_locked(
                group_id, revision["id"], "revision_activated", revision
            )
            self._save_locked()
            return self.revision_state_locked(group_id, revision["id"])

    def cancel_staged(self, group_id: str, revision_id: int | None = None) -> dict[str, object]:
        """Cancel a staged rearrangement before it changes live beds."""

        with self._lock:
            group = self._require_group_locked(group_id)
            staged_id = group.get("staged_revision")
            if staged_id is None:
                raise ValueError(f"group {group_id} has no staged revision")
            if revision_id is not None and int(revision_id) != int(staged_id):
                raise ValueError("requested revision is not the staged revision")
            revision = self._revision_locked(group_id, int(staged_id))
            if revision["phase"] != PHASE_STAGED:
                raise ValueError("revision is not staged")
            revision["phase"] = PHASE_CANCELED
            revision["completed_at"] = self._now()
            group["staged_revision"] = None
            self._append_event_locked(
                group_id, revision["id"], "revision_canceled", revision
            )
            self._save_locked()
            return self.revision_state_locked(group_id, revision["id"])

    def rollback(self, group_id: str, revision_id: int | None = None) -> dict[str, object]:
        """Undo the latest active revision; repeated calls are idempotent."""

        with self._lock:
            group = self._require_group_locked(group_id)
            if revision_id is None:
                revision_id = int(group["active_revision"])
            revision = self._revision_locked(group_id, int(revision_id))
            if revision["phase"] in (PHASE_BASELINE, PHASE_ROLLED_BACK):
                return {
                    "rolled_back": False,
                    "reason": (
                        "revision already rolled back"
                        if revision["phase"] == PHASE_ROLLED_BACK
                        else "baseline cannot be rolled back"
                    ),
                    "revision": self.revision_state_locked(group_id, revision["id"]),
                }
            if revision["phase"] != PHASE_ACTIVE:
                raise ValueError("only an active revision can be rolled back")
            if revision["id"] != int(group["active_revision"]):
                raise ValueError("only the latest active revision can be rolled back")
            parent = self._revision_locked(group_id, int(revision["parent_id"]))
            current = self._revision_assignments(revision)
            target = self._revision_assignments(parent)
            self._set_phase_locked(revision, PHASE_ROLLING_BACK)
            self._save_locked()
            try:
                self._apply_to_bank_locked(group_id, current, target)
            except Exception:
                self._finish_failed_revision_locked(group, revision, parent, target, recovering=True)
                raise
            revision["phase"] = PHASE_ROLLED_BACK
            revision["completed_at"] = self._now()
            parent["phase"] = PHASE_ACTIVE
            group["active_revision"] = parent["id"]
            self._append_event_locked(
                group_id, revision["id"], "revision_rolled_back", revision
            )
            self._save_locked()
            return {
                "rolled_back": True,
                "revision": self.revision_state_locked(group_id, revision["id"]),
            }

    def recover(self, group_id: str | None = None) -> list[dict[str, object]]:
        """Restore consistency after an interrupted partial application.

        Applying/activating revisions are rolled back to their parent. A
        revision already marked rolled back is reconciled and left untouched,
        so invoking recovery repeatedly produces the same final state.
        """

        recovered: list[dict[str, object]] = []
        with self._lock:
            groups = [self._require_group_locked(group_id)] if group_id else list(self._groups_locked())
            changed = False
            for group in groups:
                for revision in self._revisions_locked(str(group["id"])):
                    phase = revision["phase"]
                    if revision["parent_id"] is None:
                        continue
                    parent = self._revision_locked(group_id=str(group["id"]), revision_id=int(revision["parent_id"]))
                    current_active_id = int(group["active_revision"])
                    if phase in (PHASE_APPLYING, PHASE_ACTIVATING, PHASE_ROLLING_BACK):
                        desired = self._revision_assignments(parent)
                        self._apply_to_bank_locked(str(group["id"]), self._revision_assignments(revision), desired)
                        if phase == PHASE_ROLLING_BACK:
                            revision["phase"] = PHASE_ROLLED_BACK
                            parent["phase"] = PHASE_ACTIVE
                            group["active_revision"] = parent["id"]
                        else:
                            revision["phase"] = PHASE_ROLLED_BACK
                            parent["phase"] = PHASE_ACTIVE
                            group["active_revision"] = parent["id"]
                        if group.get("staged_revision") == revision["id"]:
                            group["staged_revision"] = None
                        revision["completed_at"] = self._now()
                        self._append_event_locked(
                            str(group["id"]),
                            revision["id"],
                            "revision_recovered",
                            {"phase": phase},
                        )
                        recovered.append(
                            self.revision_state_locked(str(group["id"]), revision["id"])
                        )
                        changed = True
                    elif phase == PHASE_ROLLED_BACK and current_active_id == revision["id"]:
                        self._apply_to_bank_locked(
                            str(group["id"]),
                            self._revision_assignments(revision),
                            self._revision_assignments(parent),
                        )
                        parent["phase"] = PHASE_ACTIVE
                        group["active_revision"] = parent["id"]
                        changed = True
                    elif phase == PHASE_STAGED and group.get("staged_revision") != revision["id"]:
                        group["staged_revision"] = revision["id"]
                        changed = True
            if changed:
                self._save_locked()
            if self._bank is not None:
                self._synchronize_bank()
            return recovered

    def verify_group(self, group_id: str) -> dict[str, object]:
        """Check uniqueness and recompute the stored rearrangement checksum."""

        with self._lock:
            group = self._require_group_locked(group_id)
            revision = self._revision_locked(group_id, int(group["active_revision"]))
            assignments = list(self._revision_assignments(revision).values())
            self._validate_assignment_list_locked(assignments)
            self._reject_global_conflicts_locked(group_id, assignments)
            crosswalk = self._crosswalk(
                self._revision_assignments(
                    self._revision_locked(group_id, int(revision["parent_id"]))
                )
                if revision["parent_id"] is not None
                else {},
                self._revision_assignments(revision),
            )
            checksum = self._checksum(crosswalk)
            consistent = revision["checksum"] == checksum
            if not consistent:
                raise GroupConflictError("crosswalk checksum does not match revision data")
            return {
                "consistent": True,
                "group_id": group_id,
                "revision": revision["id"],
                "checksum": checksum,
                "active_beds": len(assignments),
            }

    def crosswalk(self, group_id: str, revision_id: int | None = None) -> list[dict[str, object]]:
        with self._lock:
            if revision_id is None:
                revision_id = int(self._require_group_locked(group_id)["active_revision"])
            revision = self._revision_locked(group_id, int(revision_id))
            return [dict(row) for row in revision["crosswalk"]]

    def resolve(
        self,
        identifier: str,
        group_id: str | None = None,
        code: str | None = None,
        zone: int | None = None,
        sequence: int | None = None,
        revision: int | None = None,
    ) -> dict[str, object]:
        """Resolve old and new identifiers while a staged renumber is pending."""

        with self._lock:
            if identifier in self._all_known_bed_ids_locked():
                return {
                    "resolved": True,
                    "bed_id": identifier,
                    "identifier": identifier,
                    "candidates": self._candidates_for_bed_locked(identifier, group_id, revision),
                }
            candidates = self._alias_candidates_locked(group_id, revision)
            if revision is not None and not candidates:
                raise ValueError(f"revision {revision} has no matching candidates")
            matches: list[dict[str, object]] = []
            for candidate in candidates:
                if code is not None and candidate["code"] != code:
                    continue
                if zone is not None and candidate["zone"] != zone:
                    continue
                if sequence is not None and candidate["sequence"] != sequence:
                    continue
                if code is None and zone is None and sequence is None:
                    numeric = _as_int(identifier)
                    if candidate["code"] != identifier and candidate["zone"] != numeric and candidate["sequence"] != numeric:
                        continue
                matches.append(candidate)
            bed_ids = {item["bed_id"] for item in matches}
            resolved = len(matches) >= 1 and len(bed_ids) == 1
            return {
                "resolved": resolved,
                "bed_id": next(iter(bed_ids)) if resolved else "",
                "identifier": identifier,
                "ambiguous": len(bed_ids) > 1,
                "candidates": matches,
            }

    def groups(self) -> list[dict[str, object]]:
        with self._lock:
            return [self.group_state_locked(str(group["id"])) for group in self._groups_locked()]

    def group(self, group_id: str = DEFAULT_GROUP_ID) -> dict[str, object]:
        with self._lock:
            return self.group_state_locked(group_id)

    def revision(self, group_id: str, revision_id: int) -> dict[str, object]:
        with self._lock:
            return self.revision_state_locked(group_id, revision_id)

    def revisions(self, group_id: str) -> list[dict[str, object]]:
        with self._lock:
            return [
                self.revision_state_locked(group_id, int(item["id"]))
                for item in self._revisions_locked(group_id)
            ]

    def history(self, group_id: str | None = None) -> list[dict[str, object]]:
        with self._lock:
            events = copy.deepcopy(self._state["events"])
            if group_id is not None:
                events = [event for event in events if event["group_id"] == group_id]
            return events

    def active_assignments(self, group_id: str = DEFAULT_GROUP_ID) -> dict[str, Assignment]:
        with self._lock:
            return self._active_assignments_locked(group_id)

    def state(self) -> dict[str, object]:
        with self._lock:
            return {
                "schema": self._state["schema"],
                "groups": self.groups(),
                "events": self.history(),
            }

    def describe(self) -> str:
        with self._lock:
            beds = sum(len(group["assignments"]) for group in self.groups())
            return f"filter groups={len(self._state['groups'])} beds={beds}"

    def group_state_locked(self, group_id: str) -> dict[str, object]:
        group = self._require_group_locked(group_id)
        active = self._revision_locked(group_id, int(group["active_revision"]))
        staged = None
        if group.get("staged_revision") is not None:
            staged_revision = self._revision_locked(group_id, int(group["staged_revision"]))
            staged = self.revision_state_from_record_locked(staged_revision)
        return {
            "id": group["id"],
            "name": group["name"],
            "switch_mode": group["switch_mode"],
            "conflict_policy": group["conflict_policy"],
            "active_revision": group["active_revision"],
            "staged_revision": group.get("staged_revision"),
            "assignments": [
                assignment.as_dict()
                for assignment in sorted(
                    self._revision_assignments(active).values(),
                    key=lambda item: (item.sequence, item.zone, item.code),
                )
            ],
            "staged": staged,
        }

    def revision_state_locked(self, group_id: str, revision_id: int) -> dict[str, object]:
        return self.revision_state_from_record_locked(
            self._revision_locked(group_id, revision_id)
        )

    def revision_state_from_record_locked(self, revision: dict[str, object]) -> dict[str, object]:
        assignments = sorted(
            self._revision_assignments(revision).values(),
            key=lambda item: (item.sequence, item.zone, item.code),
        )
        return {
            "id": revision["id"],
            "group_id": revision["group_id"],
            "parent_id": revision["parent_id"],
            "phase": revision["phase"],
            "reason": revision["reason"],
            "operator": revision["operator"],
            "created_at": revision["created_at"],
            "completed_at": revision["completed_at"],
            "checksum": revision["checksum"],
            "assignments": [item.as_dict() for item in assignments],
            "crosswalk": [dict(row) for row in revision["crosswalk"]],
        }

    def _create_revision(
        self,
        group_id: str,
        prior: dict[str, Assignment],
        target: dict[str, Assignment],
        reason: str,
        operator: str,
        switch_mode: str,
        action: str,
    ) -> dict[str, object]:
        group = self._require_group_locked(group_id)
        validate_switch_mode(switch_mode)
        if switch_mode == PHASE_STAGED:
            raise ValueError("invalid switch mode")
        revision = self._revision_record(
            group_id=group_id,
            parent_id=int(group["active_revision"]),
            phase=PHASE_APPLYING if switch_mode == SWITCH_IMMEDIATE else PHASE_STAGED,
            assignments=target,
            prior=prior,
            reason=reason,
            operator=operator,
        )
        self._revisions_locked(group_id).append(revision)
        if switch_mode == SWITCH_IMMEDIATE:
            self._save_locked()
            try:
                self._apply_to_bank_locked(group_id, prior, target)
            except Exception:
                parent = self._revision_locked(group_id, int(group["active_revision"]))
                self._finish_failed_revision_locked(group, parent, revision, prior)
                raise
            parent = self._revision_locked(group_id, int(group["active_revision"]))
            parent["phase"] = PHASE_SUPERSEDED
            revision["phase"] = PHASE_ACTIVE
            revision["completed_at"] = self._now()
            group["active_revision"] = revision["id"]
        else:
            group["staged_revision"] = revision["id"]
        self._append_event_locked(group_id, revision["id"], action, revision)
        self._save_locked()
        return self.revision_state_locked(group_id, revision["id"])

    def _finish_failed_revision_locked(
        self,
        group: dict[str, object],
        parent: dict[str, object],
        revision: dict[str, object],
        desired: dict[str, Assignment],
        recovering: bool = False,
    ) -> None:
        try:
            self._apply_to_bank_locked(
                str(group["id"]), self._revision_assignments(revision), desired
            )
        finally:
            revision["phase"] = PHASE_ROLLED_BACK
            revision["completed_at"] = self._now()
            if group.get("staged_revision") == revision["id"]:
                group["staged_revision"] = None
            if int(group["active_revision"]) == revision["id"]:
                parent["phase"] = PHASE_ACTIVE
                group["active_revision"] = parent["id"]
            self._append_event_locked(
                str(group["id"]),
                revision["id"],
                "revision_rollback_failed" if not recovering else "revision_rolled_back",
                revision,
            )
            self._save_locked()

    def _complete_target_locked(
        self,
        group_id: str,
        current: dict[str, Assignment],
        requested: list[Assignment],
    ) -> dict[str, Assignment]:
        if not requested:
            raise ValueError("a complete reorder must include every active bed")
        by_bed = {item.bed_id: item for item in requested}
        if set(by_bed) != set(current):
            missing = sorted(set(current) - set(by_bed))
            unknown = sorted(set(by_bed) - set(current))
            raise ValueError(
                f"complete reorder bed set mismatch missing={missing} unknown={unknown}"
            )
        self._validate_assignment_list_locked(requested)
        self._reject_internal_duplicates_locked(requested)
        return dict(by_bed)

    def _accepted_partial_locked(
        self,
        group_id: str,
        current: dict[str, Assignment],
        requested: list[Assignment],
        policy: str,
    ) -> tuple[list[Assignment], list[Conflict]]:
        if not requested:
            raise ValueError("at least one assignment is required")
        conflicts: list[Conflict] = []
        internal_conflict_beds = self._internal_conflict_beds(requested)
        valid: list[Assignment] = []
        for assignment in requested:
            if assignment.bed_id not in current:
                conflicts.append(self._conflict(assignment, "bed is not active in group"))
            elif assignment.bed_id in internal_conflict_beds:
                conflicts.append(self._conflict(assignment, "duplicate assignment within batch"))
            else:
                self._validate_assignment(assignment)
                valid.append(assignment)
        if conflicts and policy == POLICY_REJECT_BATCH:
            return [], conflicts

        accepted: list[Assignment] = []
        target = dict(current)
        for assignment in valid:
            try:
                self._reject_assignment_conflicts_locked(group_id, assignment, target)
                self._reject_global_conflicts_locked(group_id, [assignment])
            except GroupConflictError as exc:
                conflicts.append(self._conflict(assignment, str(exc)))
                continue
            target[assignment.bed_id] = assignment
            accepted.append(assignment)
        return accepted, conflicts

    def _internal_conflict_beds(self, requested: list[Assignment]) -> set[str]:
        key_owners: dict[tuple[str, object], set[str]] = {}
        for assignment in requested:
            for key in (
                ("code", assignment.code),
                ("zone", assignment.zone),
                ("sequence", assignment.sequence),
            ):
                key_owners.setdefault(key, set()).add(assignment.bed_id)
        return {
            bed_id
            for owners in key_owners.values()
            if len(owners) > 1
            for bed_id in owners
        }

    def _reject_internal_duplicates_locked(self, assignments: list[Assignment]) -> None:
        seen: dict[tuple[str, object], str] = {}
        for assignment in assignments:
            for key in (
                ("code", assignment.code),
                ("zone", assignment.zone),
                ("sequence", assignment.sequence),
            ):
                owner = seen.get(key)
                if owner is not None and owner != assignment.bed_id:
                    raise GroupConflictError(
                        f"duplicate {key[0]} {key[1]} used by {owner} and {assignment.bed_id}"
                    )
                seen[key] = assignment.bed_id

    def _validate_assignment_list_locked(self, assignments: list[Assignment]) -> None:
        for assignment in assignments:
            self._validate_assignment(assignment)
        self._reject_internal_duplicates_locked(assignments)

    def _reject_global_conflicts_locked(
        self, group_id: str, assignments: list[Assignment]
    ) -> None:
        current = {
            bed_id: assignment
            for other in self._groups_locked()
            for bed_id, assignment in self._active_assignments_locked(str(other["id"])).items()
            if str(other["id"]) != group_id
        }
        for assignment in assignments:
            self._reject_assignment_conflicts_locked(group_id, assignment, current)

    def _reject_assignment_conflicts_locked(
        self,
        group_id: str,
        assignment: Assignment,
        target: dict[str, Assignment],
    ) -> None:
        for bed_id, existing in target.items():
            if bed_id == assignment.bed_id:
                continue
            if existing.code == assignment.code:
                raise GroupConflictError(
                    f"code {assignment.code} is already used by {bed_id}"
                )
            if existing.zone == assignment.zone:
                raise GroupConflictError(
                    f"zone {assignment.zone} is already occupied by {bed_id}"
                )
            if existing.sequence == assignment.sequence:
                raise GroupConflictError(
                    f"commissioning sequence {assignment.sequence} is already used by {bed_id}"
                )

    def _apply_to_bank_locked(
        self,
        group_id: str,
        prior: dict[str, Assignment],
        target: dict[str, Assignment],
    ) -> None:
        if self._bank is None:
            return
        prior_ids = set(prior)
        temporary_zone = -1
        for bed_id, assignment in target.items():
            if self._bank.bed(bed_id) is None:
                while any(bed.zone == temporary_zone for bed in self._bank.beds()):
                    temporary_zone -= 1
                self._bank.add_bed(bed_id, temporary_zone)
                temporary_zone -= 1
        self._bank.apply_zone_map(
            {bed_id: assignment.zone for bed_id, assignment in target.items()}
        )
        for bed_id in sorted(prior_ids - set(target)):
            if self._bank.bed(bed_id) is not None:
                self._bank.remove_bed(bed_id)

    def _synchronize_bank(self) -> None:
        if self._bank is None:
            return
        active = {
            bed_id: assignment
            for group in self._groups_locked()
            for bed_id, assignment in self._active_assignments_locked(str(group["id"])).items()
        }
        temporary_zone = -1
        for bed_id in sorted(active):
            if self._bank.bed(bed_id) is None:
                while any(bed.zone == temporary_zone for bed in self._bank.beds()):
                    temporary_zone -= 1
                self._bank.add_bed(bed_id, temporary_zone)
                temporary_zone -= 1
        self._bank.apply_zone_map(
            {bed_id: assignment.zone for bed_id, assignment in active.items()}
        )
        for bed_id in list(self._bank.bed_ids()):
            if bed_id not in active and self._bank.bed(bed_id) is not None:
                self._bank.remove_bed(bed_id)

    def _require_physical_bed_absent_locked(self, bed_id: str) -> None:
        for group in self._groups_locked():
            revisions = self._revisions_locked(str(group["id"]))
            for revision in revisions:
                if revision["phase"] == PHASE_CANCELED:
                    continue
                if bed_id in self._revision_assignments(revision):
                    raise ValueError(f"filter bed {bed_id} already exists")

    def _require_no_staged_locked(self, group: dict[str, object]) -> None:
        if group.get("staged_revision") is not None:
            raise ValueError(
                f"group {group['id']} has staged revision {group['staged_revision']}"
            )

    def _alias_candidates_locked(
        self, group_id: str | None, revision_id: int | None = None
    ) -> list[dict[str, object]]:
        candidates: list[dict[str, object]] = []
        for group in self._groups_locked():
            if group_id is not None and str(group["id"]) != group_id:
                continue
            gid = str(group["id"])
            active = self._revision_locked(gid, int(group["active_revision"]))
            revisions = [active]
            if revision_id is not None:
                selected = self._revision_locked(gid, revision_id)
                revisions = [selected]
            else:
                if group.get("staged_revision") is not None:
                    revisions.append(self._revision_locked(gid, int(group["staged_revision"])))
                if active["parent_id"] is not None:
                    parent = self._revision_locked(gid, int(active["parent_id"]))
                    if parent["phase"] == PHASE_SUPERSEDED:
                        revisions.append(parent)
            for revision in revisions:
                if revision["phase"] == PHASE_CANCELED:
                    continue
                label = "previous" if revision["phase"] == PHASE_SUPERSEDED else revision["phase"]
                for assignment in self._revision_assignments(revision).values():
                    candidate = assignment.as_dict()
                    candidate.update(
                        {
                            "group_id": gid,
                            "revision": revision["id"],
                            "label": label,
                        }
                    )
                    candidates.append(candidate)
        return candidates

    def _candidates_for_bed_locked(
        self, bed_id: str, group_id: str | None, revision: int | None = None
    ) -> list[dict[str, object]]:
        return [
            candidate
            for candidate in self._alias_candidates_locked(group_id, revision)
            if candidate["bed_id"] == bed_id
        ]

    def _all_known_bed_ids_locked(self) -> set[str]:
        return {
            bed_id
            for group in self._groups_locked()
            for revision in self._revisions_locked(str(group["id"]))
            if revision["phase"] != PHASE_CANCELED
            for bed_id in self._revision_assignments(revision)
        }

    def _groups_locked(self) -> list[dict[str, object]]:
        return list(self._state["groups"])

    def _group_locked(self, group_id: str) -> dict[str, object] | None:
        for group in self._state["groups"]:
            if str(group["id"]) == group_id:
                return group
        return None

    def _require_group_locked(self, group_id: str) -> dict[str, object]:
        group = self._group_locked(group_id)
        if group is None:
            raise ValueError(f"filter group {group_id} not found")
        return group

    def _revisions_locked(self, group_id: str) -> list[dict[str, object]]:
        return self._state["revisions"].setdefault(group_id, [])

    def _revision_locked(self, group_id: str, revision_id: int) -> dict[str, object]:
        self._require_group_locked(group_id)
        for revision in self._revisions_locked(group_id):
            if int(revision["id"]) == int(revision_id):
                return revision
        raise ValueError(f"revision {revision_id} not found in group {group_id}")

    def _active_assignments_locked(self, group_id: str) -> dict[str, Assignment]:
        group = self._require_group_locked(group_id)
        revision = self._revision_locked(group_id, int(group["active_revision"]))
        return self._revision_assignments(revision)

    def _revision_record(
        self,
        group_id: str,
        parent_id: int | None,
        phase: str,
        assignments: dict[str, Assignment],
        reason: str,
        operator: str,
        prior: dict[str, Assignment] | None = None,
    ) -> dict[str, object]:
        revisions = self._revisions_locked(group_id)
        revision_id = 0 if not revisions else max(int(item["id"]) for item in revisions) + 1
        if prior is None:
            prior = {}
        crosswalk = self._crosswalk(prior, assignments)
        return {
            "id": revision_id,
            "group_id": group_id,
            "parent_id": parent_id,
            "phase": phase,
            "reason": reason,
            "operator": operator,
            "created_at": self._now(),
            "completed_at": None,
            "assignments": {
                bed_id: assignment.as_dict()
                for bed_id, assignment in assignments.items()
            },
            "crosswalk": crosswalk,
            "checksum": self._checksum(crosswalk),
        }

    def _set_phase_locked(self, revision: dict[str, object], phase: str) -> None:
        revision["phase"] = phase

    def _append_event_locked(
        self,
        group_id: str,
        revision_id: int,
        action: str,
        detail: object,
    ) -> None:
        self._state["events"].append(
            {
                "id": uuid.uuid4().hex,
                "at": self._now(),
                "group_id": group_id,
                "revision": revision_id,
                "action": action,
                "detail": detail,
            }
        )

    def _save_locked(self) -> None:
        self._store.put(GROUP_STATE_KEY, json.dumps(self._state, ensure_ascii=False, sort_keys=True))

    def _load(self) -> dict[str, object]:
        raw, present = self._store.get(GROUP_STATE_KEY)
        if not present:
            return {"schema": 1, "groups": [], "revisions": {}, "events": []}
        loaded = json.loads(raw)
        if not isinstance(loaded, dict) or not isinstance(loaded.get("groups"), list):
            raise ValueError("invalid filter group document")
        loaded.setdefault("schema", 1)
        loaded.setdefault("revisions", {})
        loaded.setdefault("events", [])
        return loaded

    @staticmethod
    def _revision_assignments(revision: dict[str, object]) -> dict[str, Assignment]:
        raw = revision.get("assignments", {})
        if not isinstance(raw, dict):
            raise ValueError("revision assignments must be an object")
        return {
            str(bed_id): Assignment.from_payload(dict(payload, bed_id=bed_id))
            for bed_id, payload in raw.items()
            if isinstance(payload, dict)
        }

    @staticmethod
    def _valid_assignment(
        bed_id: str, code: str, zone: int, sequence: int
    ) -> Assignment:
        assignment = Assignment(
            bed_id=bed_id.strip(), code=code.strip(), zone=int(zone), sequence=int(sequence)
        )
        FilterGroupManager._validate_assignment(assignment)
        return assignment

    @staticmethod
    def _validate_assignment(assignment: Assignment) -> None:
        validate_identifier(assignment.bed_id, "bed id")
        validate_identifier(assignment.code, "bed code")
        validate_zone(assignment.zone)
        validate_sequence(assignment.sequence)

    @staticmethod
    def _assignment_from(value: Assignment | dict[str, object]) -> Assignment:
        if isinstance(value, Assignment):
            FilterGroupManager._validate_assignment(value)
            return value
        return FilterGroupManager._valid_assignment(
            str(value.get("bed_id", "")),
            str(value.get("code", "")),
            int(value.get("zone", 0)),
            int(value.get("sequence", 0)),
        )

    @staticmethod
    def _conflict(assignment: Assignment, reason: str) -> Conflict:
        return Conflict(
            bed_id=assignment.bed_id,
            code=assignment.code,
            zone=assignment.zone,
            sequence=assignment.sequence,
            reason=reason,
        )

    @staticmethod
    def _crosswalk(
        prior: dict[str, Assignment], target: dict[str, Assignment]
    ) -> list[dict[str, object]]:
        rows: list[dict[str, object]] = []
        for bed_id in sorted(set(prior) | set(target)):
            old = prior.get(bed_id)
            new = target.get(bed_id)
            rows.append(
                {
                    "bed_id": bed_id,
                    "old_code": "" if old is None else old.code,
                    "old_zone": None if old is None else old.zone,
                    "old_sequence": None if old is None else old.sequence,
                    "new_code": "" if new is None else new.code,
                    "new_zone": None if new is None else new.zone,
                    "new_sequence": None if new is None else new.sequence,
                }
            )
        return rows

    @staticmethod
    def _checksum(crosswalk: list[dict[str, object]]) -> str:
        body = json.dumps(crosswalk, sort_keys=True, separators=(",", ":"))
        return hashlib.sha256(body.encode("utf-8")).hexdigest()

    @staticmethod
    def _now() -> str:
        return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


def _as_int(value: str) -> int | None:
    try:
        return int(value)
    except (TypeError, ValueError):
        return None
