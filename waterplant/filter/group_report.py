"""Read models and validation for filter groups and bed partitions."""

from __future__ import annotations

from dataclasses import dataclass

DEFAULT_GROUP_ID = "default"

SWITCH_IMMEDIATE = "immediate"
SWITCH_SHIFT_END = "shift_end"
SWITCH_MODES = (SWITCH_IMMEDIATE, SWITCH_SHIFT_END)

POLICY_REJECT_BATCH = "reject_batch"
POLICY_APPLY_NON_CONFLICTING = "apply_non_conflicting"
CONFLICT_POLICIES = (POLICY_REJECT_BATCH, POLICY_APPLY_NON_CONFLICTING)

PHASE_BASELINE = "baseline"
PHASE_STAGED = "staged"
PHASE_APPLYING = "applying"
PHASE_ACTIVATING = "activating"
PHASE_ACTIVE = "active"
PHASE_ROLLING_BACK = "rolling_back"
PHASE_SUPERSEDED = "superseded"
PHASE_ROLLED_BACK = "rolled_back"
PHASE_CANCELED = "canceled"

GROUP_STATE_KEY = "filter:groups"


class GroupConflictError(ValueError):
    """A request conflicts with the current group or partition state."""


@dataclass(frozen=True)
class Assignment:
    """The immutable triple associated with one physical bed in a revision."""

    bed_id: str
    code: str
    zone: int
    sequence: int

    def as_dict(self) -> dict[str, object]:
        return {
            "bed_id": self.bed_id,
            "code": self.code,
            "zone": self.zone,
            "sequence": self.sequence,
        }

    @classmethod
    def from_payload(cls, payload: dict[str, object]) -> "Assignment":
        try:
            zone = int(payload["zone"])
            sequence = int(payload["sequence"])
        except (KeyError, TypeError, ValueError) as exc:
            raise ValueError("zone and sequence must be integers") from exc
        bed_id = str(payload.get("bed_id", "")).strip()
        code = str(payload.get("code", "")).strip()
        if not bed_id:
            raise ValueError("bed_id is required")
        if not code:
            raise ValueError("code is required")
        return cls(bed_id=bed_id, code=code, zone=zone, sequence=sequence)


@dataclass(frozen=True)
class Conflict:
    """One requested assignment that could not be applied."""

    bed_id: str
    code: str
    zone: int
    sequence: int
    reason: str

    def as_dict(self) -> dict[str, object]:
        return {
            "bed_id": self.bed_id,
            "code": self.code,
            "zone": self.zone,
            "sequence": self.sequence,
            "reason": self.reason,
        }


def validate_switch_mode(value: str) -> None:
    if value not in SWITCH_MODES:
        raise ValueError(f"switch mode must be one of {', '.join(SWITCH_MODES)}")


def validate_conflict_policy(value: str) -> None:
    if value not in CONFLICT_POLICIES:
        raise ValueError(
            f"conflict policy must be one of {', '.join(CONFLICT_POLICIES)}"
        )


def validate_sequence(sequence: int) -> None:
    if sequence <= 0:
        raise ValueError("commissioning sequence must be positive")
    if sequence > 100_000:
        raise ValueError("commissioning sequence exceeds the supported range")


def validate_identifier(value: str, field: str) -> None:
    value = value.strip()
    if not value:
        raise ValueError(f"{field} is required")
    if len(value) > 64:
        raise ValueError(f"{field} must not exceed 64 characters")
    if "|" in value:
        raise ValueError(f"{field} must not contain '|'")
