"""指标异动归因业务规则：状态流转、字段校验与筛选口径都收在这里。

异动记录有两个互不覆盖的状态维度：
- ``status``：归因分析工作流（待分析 -> 分析中 -> 已归因 -> 已关闭）；
- ``告警状态``：由分析告警规则写入的触发/静默状态（已触发 / 静默中 等）。
旧实现把两者混用，导致详情页出现状态错位。
"""
from __future__ import annotations

import math
from datetime import datetime, timedelta, timezone
from typing import Any

from app.store import store

MODULE = "anomaly"
REQUIRED_FIELDS = ["记录编号", "指标名称", "异动方向"]
STATUS_ORDER = ["待分析", "分析中", "已归因", "已关闭"]
# 动作 -> 目标状态；旧实现写成了上一档状态，动作点下去等于原地踏步。
ACTION_RULES = {"开始分析": "分析中", "确认归因": "已归因", "关闭异动": "已关闭"}
ACTION_LABELS = {"开始分析": "开始分析", "确认归因": "确认归因", "关闭异动": "关闭异动"}
ALLOWED_SOURCES = {
    "开始分析": {"待分析", "分析中"},
    "确认归因": {"分析中", "已归因"},
    "关闭异动": {"已归因", "已关闭"},
}
NEGATIVE_ACTIONS: list[str] = []


def _now() -> datetime:
    return datetime.now(timezone.utc).replace(microsecond=0)


def _parse_dt(value: Any) -> datetime | None:
    if value in (None, ""):
        return None
    text = str(value).strip()
    if not text:
        return None
    try:
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


class AnomalyService:
    def list_entries(
        self,
        *,
        keyword: str | None = None,
        status: str | None = None,
        page: int = 1,
        size: int = 20,
    ) -> tuple[list[dict[str, Any]], int]:
        # 先沿调用链求值，异动页与告警规则页共享同一份触发结果。
        self.evaluate_alarm_rules()
        rows = store.rows(MODULE)
        # 列表读取时刷新静默倒计时与静默状态，保证与告警规则列表口径一致。
        for row in rows:
            self._refresh_silence(row)
        if keyword:
            rows = [row for row in rows if keyword in str(row.get("记录编号", ""))]
        if status:
            rows = [row for row in rows if row.get("status") == status]
        total = len(rows)
        start = max(page - 1, 0) * size
        return rows[start:start + size], total

    def get_entry(self, entry_id: int) -> dict[str, Any] | None:
        self.evaluate_alarm_rules()
        entry = store.find(MODULE, entry_id)
        if entry is not None:
            self._refresh_silence(entry)
        return entry

    def create_entry(self, values: dict[str, Any]) -> tuple[dict[str, Any] | None, list[str]]:
        missing = [field for field in REQUIRED_FIELDS if not str(values.get(field) or "").strip()]
        if missing:
            return None, missing
        rows = store.rows(MODULE)
        entry = {"id": max((int(row.get("id", 0)) for row in rows), default=0) + 1}
        entry.update({field: values.get(field) for field in REQUIRED_FIELDS})
        entry["status"] = STATUS_ORDER[0]
        entry["pending"] = True
        entry["abnormal"] = False
        entry["告警状态"] = "未触发"
        entry["静默状态"] = "不适用"
        rows.append(entry)
        # 新观测入库后按监控指标重新求值：同一指标的触发结果在规则页与异动页保持一致。
        self.sync_metric(str(entry.get("指标名称") or ""))
        return entry, []

    def run_action(self, entry_id: int, action: str) -> tuple[dict[str, Any] | None, str]:
        entry = store.find(MODULE, entry_id)
        if entry is None:
            return None, f"异动记录 {entry_id} 不存在或已归档"
        if action not in ACTION_RULES:
            return None, f"动作「{action}」不属于指标异动归因可执行范围"
        current = str(entry.get("status") or STATUS_ORDER[0])
        if current not in ALLOWED_SOURCES[action]:
            return None, f"异动记录当前为「{current}」，不能执行「{action}」"
        target = ACTION_RULES[action]
        if target not in STATUS_ORDER:
            return None, f"目标状态「{target}」不在允许的状态序列里"
        entry["status"] = target
        entry["pending"] = target != STATUS_ORDER[-1]
        entry["abnormal"] = action in NEGATIVE_ACTIONS
        return entry, f"异动记录已{ACTION_LABELS[action]}"

    def evaluate_alarm_rules(self) -> list[dict[str, Any]]:
        """读取异动记录前，沿调用链对已启用规则做一次条件求值与静默判断。

        这样值班同事先打开哪个页面都能看到一致的触发结果：规则页与异动页
        共用同一份触发状态，不会出现“规则已启用但异动页没有新告警”。
        """
        return self._alarm_service().evaluate_all(materialize=True)

    def sync_metric(self, metric: str) -> list[dict[str, Any]]:
        """异动观测更新后，同步刷新同指标规则的触发结果。"""
        return self._alarm_service().evaluate_metric(metric, materialize=True)

    # ------------------------------------------------------------------ 内部
    @staticmethod
    def _alarm_service():
        from app.services.alarm import AlarmService  # 延迟导入，避免模块循环依赖

        return AlarmService()

    def _refresh_silence(self, row: dict[str, Any]) -> None:
        """按异动记录自带的静默截止时间刷新倒计时，不改动归因工作流状态。"""
        expiry = _parse_dt(row.get("静默截止时间"))
        if expiry is None:
            return
        remaining = max(0, int(math.ceil((expiry - _now()).total_seconds() / 60)))
        row["静默剩余分钟"] = remaining
        if remaining > 0:
            row["静默状态"] = "静默中"
            if row.get("告警状态") == "已触发":
                row["告警状态"] = "静默中"
        else:
            row["静默状态"] = "静默结束"
            row["静默结束时间"] = expiry.isoformat()
            if row.get("告警状态") == "静默中":
                row["告警状态"] = "已触发"
