"""Backwash controller.

Every backwash follows the same sequence: the bed is closed first, then the
drain is opened for it. The command queue is persisted so an interrupted
console can replay or discard pending work.
"""

from __future__ import annotations

from waterplant.filter import Bank
from waterplant.store.commands import append_command, clear_commands, load_commands
from waterplant.store.store import Store

from .report import BackwashState
from .schedule import now_unix

COMMAND_KEY = "backwash:commands"
DRAIN_KEY_PREFIX = "backwash:drain:"
SPILL_KEY = "backwash:spill"


class Controller:
    """Sequences backwash commands against the filter bank."""

    def __init__(self, bank: Bank, store: Store, group_manager=None) -> None:
        self._bank = bank
        self._store = store
        self._group_manager = group_manager

    def start(self, bed_id: str) -> None:
        """Close the bed and only then open its drain."""

        self._bank.close(bed_id)
        self._drain(bed_id)

    def _drain(self, bed_id: str) -> None:
        if not self._bank.is_closed(bed_id):
            self._store.put(SPILL_KEY, "true")
        self._store.put(f"{DRAIN_KEY_PREFIX}{bed_id}", str(now_unix()))

    def enqueue(self, bed_id: str) -> None:
        append_command(self._store, COMMAND_KEY, bed_id)

    def replay(self) -> list[str]:
        """Re-run every queued command in the order it was written."""

        commands = self.command_list()
        for bed_id in commands:
            self.start(bed_id)
        return commands

    def recover(self) -> None:
        """Discard commands left over from a filter that already recovered."""

        clear_commands(self._store, COMMAND_KEY)

    def command_list(self) -> list[str]:
        return load_commands(self._store, COMMAND_KEY)

    def pending_count(self) -> int:
        return len(self.command_list())

    def select(self, zone: int, group_id: str = "default") -> str:
        """Resolve the bed that currently occupies a zone within one group."""

        if self._group_manager is None:
            return self._select_bank_zone(zone)
        for bed_id, assignment in self._group_manager.active_assignments(group_id).items():
            if assignment.zone == zone:
                return bed_id
        raise ValueError(f"no filter bed for zone {zone} in group {group_id}")

    def _select_bank_zone(self, zone: int) -> str:
        mapping = self._bank.mapping()
        if zone not in mapping:
            raise ValueError(f"no filter bed for zone {zone}")
        return mapping[zone]

    def order_rotation(self) -> list[str]:
        """Rotate duty to the dirtiest bed first."""

        order = [bed.id for bed in sorted(self._bank.beds(), key=lambda bed: -bed.load)]
        if order:
            self._bank.rotate(order[0])
        return order

    def eligible(self) -> list[str]:
        return [bed.id for bed in self._bank.beds() if not bed.closed]

    def drains(self) -> list[str]:
        drains: list[str] = []
        for key in self._store.keys():
            if key.startswith(DRAIN_KEY_PREFIX):
                drains.append(key[len(DRAIN_KEY_PREFIX) :])
        return drains

    def drain_count(self) -> int:
        return len(self.drains())

    def last_drain(self) -> str | None:
        drains = self.drains()
        if not drains:
            return None
        return drains[-1]

    def spilled(self) -> bool:
        return self._store.get(SPILL_KEY)[1]

    def state(self) -> BackwashState:
        return BackwashState(commands=self.command_list(), drains=self.drains())

    def describe(self) -> str:
        state = self.state()
        return f"backwash commands={len(state.commands)} drains={len(state.drains)}"
