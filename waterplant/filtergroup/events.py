"""滤组模块的持久化：一条 append-only 事件日志 + 一个可改写的生效指针。

设计要点
========

* 所有"发生过的事实"（建账、版本快照、新旧对照、方案状态、步骤完成）
  都以事件形式追加到 ``filtergroup:events``，永不修改、永不删除；
* 只有"当前指向哪个版本"这类指针允许就地更新
  （``filtergroup:state``），指针切换天然幂等；
* 值班记录单独追加到 ``filtergroup:duty``，同样不可变；
* 审计条目追加到 ``filtergroup:audit``。
"""

from __future__ import annotations

import json
from typing import Any

from waterplant.store.commands import append_command, load_commands
from waterplant.store.store import Store

EVENT_KEY = "filtergroup:events"
STATE_KEY = "filtergroup:state"
DUTY_KEY = "filtergroup:duty"
AUDIT_KEY = "filtergroup:audit"


def append_event(store: Store, kind: str, **payload: Any) -> dict[str, Any]:
    """追加一条领域事件。"""

    event = {"kind": kind, **payload}
    append_command(store, EVENT_KEY, json.dumps(event, ensure_ascii=False))
    return event


def load_events(store: Store) -> list[dict[str, Any]]:
    """读出全部领域事件，跳过损坏条目。"""

    events: list[dict[str, Any]] = []
    for raw in load_commands(store, EVENT_KEY):
        try:
            payload = json.loads(raw)
        except ValueError:
            continue
        if isinstance(payload, dict) and "kind" in payload:
            events.append(payload)
    return events


def load_state(store: Store) -> dict[str, Any]:
    """读生效指针；不存在时返回空指针。"""

    raw, present = store.get(STATE_KEY)
    if not present:
        return {"effective_version": None, "armed_plan": None}
    try:
        payload = json.loads(raw)
    except ValueError:
        return {"effective_version": None, "armed_plan": None}
    if not isinstance(payload, dict):
        return {"effective_version": None, "armed_plan": None}
    return {
        "effective_version": payload.get("effective_version"),
        "armed_plan": payload.get("armed_plan"),
    }


def save_state(store: Store, state: dict[str, Any]) -> None:
    store.put(STATE_KEY, json.dumps(state, ensure_ascii=False))


def append_audit(store: Store, action: str, detail: dict[str, Any]) -> dict[str, Any]:
    """追加一条结构化审计条目。"""

    import time
    import uuid

    entry = {
        "id": uuid.uuid4().hex,
        "at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "action": action,
        "detail": detail,
    }
    append_command(store, AUDIT_KEY, json.dumps(entry, ensure_ascii=False))
    return entry


def load_audit(store: Store) -> list[dict[str, Any]]:
    entries: list[dict[str, Any]] = []
    for raw in load_commands(store, AUDIT_KEY):
        try:
            payload = json.loads(raw)
        except ValueError:
            continue
        if isinstance(payload, dict):
            entries.append(payload)
    return entries


def append_duty(store: Store, record: dict[str, Any]) -> None:
    append_command(store, DUTY_KEY, json.dumps(record, ensure_ascii=False))


def load_duty(store: Store) -> list[dict[str, Any]]:
    records: list[dict[str, Any]] = []
    for raw in load_commands(store, DUTY_KEY):
        try:
            payload = json.loads(raw)
        except ValueError:
            continue
        if isinstance(payload, dict):
            records.append(payload)
    return records
