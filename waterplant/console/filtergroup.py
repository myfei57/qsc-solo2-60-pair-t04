"""池组分区管理的控制台端点。

路由
----

GET  /filtergroup/layout        当前布局（并存期含待激活版本与对照）
GET  /filtergroup/plans         全部重排方案与步骤进度
GET  /filtergroup/plan          查询单个方案
POST /filtergroup/bootstrap     初始建账
POST /filtergroup/plan          提交重排方案
POST /filtergroup/activate      班末激活 SHIFT_END 方案
POST /filtergroup/cancel        撤销待激活方案
POST /filtergroup/recover       部分失败后恢复（幂等）
POST /filtergroup/resolve       新旧池号/分区同时反查
POST /filtergroup/translate     按记录版本把旧池号翻译到现版本
GET  /filtergroup/duty          值班记录（不可变列表）
POST /filtergroup/duty          追加一条值班记录
GET  /filtergroup/audit         池组变动审计
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from waterplant.filtergroup import Assignment, ConflictError, GroupManager
from waterplant.filtergroup.model import CONFLICT_POLICIES, SWITCH_MODES

from .http import Request, RequestError, json_response

if TYPE_CHECKING:  # pragma: no cover - imported only for type checkers
    from .server import Server


def _rows_from_payload(request: Request) -> list[Assignment]:
    raw = request.payload.get("rows")
    if not isinstance(raw, list) or not raw:
        raise RequestError(400, "field rows 必须是非空列表")
    rows: list[Assignment] = []
    for index, item in enumerate(raw):
        if not isinstance(item, dict):
            raise RequestError(400, f"rows[{index}] 必须是对象")
        try:
            rows.append(
                Assignment(
                    pool_id=str(item["pool_id"]),
                    code=str(item["code"]),
                    zone=int(item["zone"]),
                    sequence=int(item["sequence"]),
                )
            )
        except KeyError as exc:
            raise RequestError(400, f"rows[{index}] 缺少字段 {exc.args[0]}") from exc
        except (TypeError, ValueError) as exc:
            raise RequestError(400, f"rows[{index}] 字段类型错误: {exc}") from exc
    return rows


def _manager(server: "Server") -> GroupManager:
    return server.runtime.filtergroup


def filtergroup_layout(server: "Server", request: Request) -> object:
    return json_response(_manager(server).layout())


def filtergroup_plans(server: "Server", request: Request) -> object:
    return json_response({"plans": _manager(server).plans()})


def filtergroup_plan(server: "Server", request: Request) -> object:
    plan_id = ""
    if request.query:
        plan_id = request.query.get("plan_id", [""])[0]
    if not plan_id:
        plan_id = request.str_field("plan_id")
    if not plan_id:
        raise RequestError(400, "plan_id 必填")
    try:
        return json_response(_manager(server).plan(plan_id))
    except ValueError as exc:
        raise RequestError(404, str(exc)) from exc


def filtergroup_bootstrap(server: "Server", request: Request) -> object:
    rows = _rows_from_payload(request)
    manager = _manager(server)
    return json_response(
        manager.bootstrap(
            rows,
            note=request.str_field("note", "初始建账"),
            actor=request.str_field("actor", "system"),
        )
    )


def filtergroup_plan_submit(server: "Server", request: Request) -> object:
    rows = _rows_from_payload(request)
    switch = request.str_field("switch")
    policy = request.str_field("policy")
    if switch not in SWITCH_MODES:
        raise RequestError(400, f"switch 必须显式指定为 {SWITCH_MODES} 之一，不设默认值")
    if policy not in CONFLICT_POLICIES:
        raise RequestError(400, f"policy 必须显式指定为 {CONFLICT_POLICIES} 之一，不设默认值")
    manager = _manager(server)
    try:
        plan = manager.submit_plan(
            rows,
            switch=switch,
            policy=policy,
            note=request.str_field("note", ""),
            actor=request.str_field("actor", "operator"),
        )
    except ConflictError as exc:
        # 冲突拒绝不是调用方格式错误：409 + 完整冲突明细，供控制台展示。
        return json_response(
            {
                "error": str(exc),
                "rejected": True,
                "conflicts": exc.conflicts,
            },
            status=409,
        )
    return json_response(plan)


def filtergroup_activate(server: "Server", request: Request) -> object:
    plan_id = request.str_field("plan_id")
    return json_response(
        _manager(server).activate(plan_id, actor=request.str_field("actor", "shift-lead"))
    )


def filtergroup_cancel(server: "Server", request: Request) -> object:
    plan_id = request.str_field("plan_id")
    return json_response(
        _manager(server).cancel(plan_id, actor=request.str_field("actor", "shift-lead"))
    )


def filtergroup_recover(server: "Server", request: Request) -> object:
    plan_id = request.str_field("plan_id")
    return json_response(
        _manager(server).recover(plan_id, actor=request.str_field("actor", "operator"))
    )


def filtergroup_resolve(server: "Server", request: Request) -> object:
    manager = _manager(server)
    code = request.str_field("code", "")
    zone = request.payload.get("zone")
    if code:
        return json_response(manager.resolve_code(code))
    if zone is not None:
        try:
            zone_int = int(zone)  # type: ignore[arg-type]
        except (TypeError, ValueError) as exc:
            raise RequestError(400, "zone 必须是整数") from exc
        return json_response(manager.resolve_zone(zone_int))
    raise RequestError(400, "必须提供 code 或 zone")


def filtergroup_translate(server: "Server", request: Request) -> object:
    return json_response(
        _manager(server)
        .translate(
            request.str_field("code"),
            request.str_field("from_version"),
            request.str_field("to_version"),
        )
    )


def filtergroup_duty_list(server: "Server", request: Request) -> object:
    return json_response({"records": _manager(server).duty_records()})


def filtergroup_duty_append(server: "Server", request: Request) -> object:
    code = request.str_field("code")
    action = request.str_field("action")
    if not code or not action:
        raise RequestError(400, "code 和 action 必填")
    record = _manager(server).record_duty(
        code,
        action,
        actor=request.str_field("actor", "operator"),
        note=request.str_field("note", ""),
    )
    return json_response(record)


def filtergroup_audit(server: "Server", request: Request) -> object:
    return json_response({"entries": _manager(server).audit_entries()})
