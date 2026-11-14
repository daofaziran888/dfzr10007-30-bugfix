"""分析告警规则接口：维护告警规则，覆盖启用规则、暂停规则、关闭规则等动作。

调用链：规则配置（含通知字段）-> 条件求值 -> 静默判断 -> 异动记录落库。
"""
from __future__ import annotations

from datetime import datetime
from typing import Any

from fastapi import APIRouter, HTTPException, Query

from app.schemas import ActionResult, EntryPayload, PageResult
from app.services.alarm import AlarmService
from app.store import store

router = APIRouter(prefix="/api/alarm", tags=["分析告警规则"])

service = AlarmService()

LIST_FIELDS = [
    "规则编号", "监控指标", "告警条件", "告警级别", "通知渠道", "静默周期",
    "生效时段", "规则状态", "触发结果", "静默状态", "静默剩余分钟", "最近触发时间",
]
STATUSES = ["草稿", "已启用", "已暂停", "已关闭"]


@router.get("/export")
def export_entries() -> dict[str, Any]:
    """导出分析告警规则清单：返回当前过滤条件下的全量数据。"""
    service.evaluate_all(materialize=True)
    items, total = service.list_entries(page=1, size=10000, evaluate=False)
    return {"module": "alarm", "total": total, "items": items}


@router.get("/{entry_id}/triggers")
def list_triggers(entry_id: int) -> dict[str, Any]:
    """读取某条告警规则落到异动记录里的触发明细，便于核对通知字段。"""
    entry = service.get_entry(entry_id)
    if entry is None:
        raise HTTPException(status_code=404, detail=f"告警规则 {entry_id} 不存在或已归档")
    rows = [
        row for row in store.rows("anomaly")
        if str(row.get("规则编号") or "") == str(entry.get("规则编号") or "")
    ]
    return {"rule": entry.get("规则编号"), "total": len(rows), "items": rows}


@router.get("", response_model=PageResult[dict])
def list_entries(
    keyword: str | None = Query(default=None, description="按规则编号检索"),
    status: str | None = Query(default=None, description="草稿、已启用、已暂停、已关闭"),
    page: int = 1,
    size: int = 20,
) -> PageResult[dict]:
    """按规则编号与状态过滤分析告警规则列表；读取即沿调用链完成一次触发布尔判断。"""
    if size > 200:
        raise HTTPException(status_code=400, detail="每页最多 200 条，请缩小分页范围")
    items, total = service.list_entries(keyword=keyword, status=status, page=page, size=size)
    return PageResult(items=items, total=total, page=page, size=size)


@router.get("/{entry_id}", response_model=dict)
def get_entry(entry_id: int) -> dict:
    """读取单条告警规则明细；不存在时给出可读的错误说明。"""
    entry = service.get_entry(entry_id)
    if entry is None:
        raise HTTPException(status_code=404, detail=f"告警规则 {entry_id} 不存在或已归档")
    return entry


@router.post("", response_model=ActionResult)
def create_entry(payload: EntryPayload) -> ActionResult:
    """登记一条告警规则；监控字段与通知字段一起保存，缺字段时说明原因。"""
    entry, missing = service.create_entry(payload.values)
    if missing:
        return ActionResult(ok=False, message=f"缺少必填字段：{'、'.join(missing)}")
    return ActionResult(ok=True, message="告警规则已登记（通知字段已随规则一起保存）", entry=entry)


@router.put("/{entry_id}", response_model=ActionResult)
def update_entry(entry_id: int, payload: EntryPayload) -> ActionResult:
    """修改规则配置：规则字段与通知渠道、静默周期必须在同一次保存里落库。"""
    entry, message = service.update_entry(entry_id, payload.values)
    if entry is None:
        return ActionResult(ok=False, message=message)
    return ActionResult(ok=True, message=message, entry=entry)


@router.post("/{entry_id}/evaluate", response_model=ActionResult)
def evaluate_entry(entry_id: int, payload: EntryPayload | None = None) -> ActionResult:
    """手动执行一次调用链求值；可通过 at 传入模拟时间，便于核对静默窗口。"""
    at_value = (payload.values if payload else {}).get("at")
    now = None
    if at_value:
        try:
            now = datetime.fromisoformat(str(at_value).replace("Z", "+00:00"))
        except ValueError:
            return ActionResult(ok=False, message=f"时间「{at_value}」无法解析，请使用 ISO 格式")
    entry = store.find("alarm", entry_id)
    if entry is None:
        return ActionResult(ok=False, message=f"告警规则 {entry_id} 不存在或已归档")
    summary = service.evaluate_rule(entry, materialize=True, now=now)
    return ActionResult(
        ok=True,
        message=f"规则 {summary.get('规则编号')}：{summary.get('触发结果')}",
        entry=entry,
    )


@router.post("/{entry_id}/actions", response_model=ActionResult)
def run_action(entry_id: int, payload: EntryPayload) -> ActionResult:
    """对单条告警规则执行启用规则、暂停规则、关闭规则；不允许的动作会被拦下并说明原因。"""
    action = str(payload.action or payload.values.get("action") or "").strip()
    entry, message = service.run_action(entry_id, action)
    if entry is None:
        return ActionResult(ok=False, message=message)
    return ActionResult(ok=True, message=message, entry=entry)
