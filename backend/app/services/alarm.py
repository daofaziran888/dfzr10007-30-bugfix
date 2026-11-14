"""分析告警规则业务规则：配置读取、条件求值、静默判断与触发落库。

告警规则到异动记录的完整调用链收在这里：

1. 配置读取：规则状态、监控指标、告警条件、通知渠道、静默周期必须一起读出；
2. 条件求值：按“监控指标”找到同一指标的最新异动观测，用告警条件比较幅度；
3. 静默判断：最近触发时间 + 静默周期 之内不再产生新的异动记录；
4. 触发落库：新建异动记录时把通知渠道、静默周期等字段一并带过去。
"""
from __future__ import annotations

import math
import re
import threading
from datetime import datetime, timedelta, timezone
from typing import Any

from app.store import store

MODULE = "alarm"
REQUIRED_FIELDS = ["规则编号", "监控指标", "告警条件"]
# 通知策略字段：登记规则时必须与监控配置一起保存，不能在落库时被裁剪。
CONFIG_FIELDS = ["告警级别", "通知渠道", "静默周期", "生效时段"]
NOTIFICATION_FIELDS = ["通知渠道", "静默周期"]
STATUS_ORDER = ["草稿", "已启用", "已暂停", "已关闭"]
# 动作 -> 目标状态。旧实现写成了上一档状态（启用规则 -> 草稿），规则永远无法真正生效。
ACTION_RULES = {"启用规则": "已启用", "暂停规则": "已暂停", "关闭规则": "已关闭"}
ACTION_LABELS = {"启用规则": "启用", "暂停规则": "暂停", "关闭规则": "关闭"}
# 允许的状态迁移；同状态重复执行按幂等处理。
ALLOWED_SOURCES = {
    "启用规则": {"草稿", "已启用", "已暂停"},
    "暂停规则": {"已启用", "已暂停"},
    "关闭规则": {"已启用", "已暂停", "已关闭"},
}
NEGATIVE_ACTIONS: list[str] = []
ANOMALY_MODULE = "anomaly"

# 求值 + 落库必须串行：并发读取列表时不能把同一次触发写成两条异动记录。
_EVALUATE_LOCK = threading.RLock()

_CONDITION_RE = re.compile(
    r"^\s*(?P<field>[^<>=!]+?)\s*(?P<op>>=|<=|==|=|!=|>|<)\s*"
    r"(?P<threshold>-?\d+(?:\.\d+)?)\s*(?P<unit>%?)\s*$"
)
_SILENCE_RE = re.compile(
    r"^\s*(?P<value>\d+(?:\.\d+)?)\s*(?P<unit>分钟|分|min|m|小时|时|h|秒|s)?"
)


def _now() -> datetime:
    return datetime.now(timezone.utc).replace(microsecond=0)


def _iso(value: datetime | None) -> str | None:
    return value.astimezone(timezone.utc).replace(microsecond=0).isoformat() if value else None


def _parse_dt(value: Any) -> datetime | None:
    """把存量里的 ISO 字符串、日期字符串统一成带时区的 UTC 时间。"""
    if value in (None, ""):
        return None
    if isinstance(value, datetime):
        parsed = value
    else:
        text = str(value).strip()
        if not text:
            return None
        try:
            parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
        except ValueError:
            for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%d"):
                try:
                    parsed = datetime.strptime(text, fmt)
                    break
                except ValueError:
                    continue
            else:
                return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def parse_silence_minutes(value: Any) -> int | None:
    """解析静默周期，统一换算成分钟；无法识别时返回 None，交给调用方报配置错误。"""
    if value in (None, ""):
        return None
    text = str(value).strip().replace("后", "")
    match = _SILENCE_RE.match(text)
    if match is None:
        return None
    amount = float(match.group("value"))
    unit = (match.group("unit") or "分钟").lower()
    if unit in {"小时", "时", "h"}:
        amount *= 60
    elif unit in {"秒", "s"}:
        amount /= 60
    return int(math.ceil(amount))


def _numeric(value: Any) -> float | None:
    if value in (None, ""):
        return None
    text = str(value).strip().replace("%", "").replace(",", "")
    try:
        return float(text)
    except ValueError:
        return None


def evaluate_condition(condition: str, observation: dict[str, Any] | None) -> tuple[bool, str]:
    """用同指标的最新异动观测求值告警条件。

    支持 ``异动幅度 > 20%`` 这类比较式；如果条件是自然语言描述（历史样例数据），
    则退化为“存在同指标异动观测即视为命中”，仍然保持可解释的求值说明。
    """
    if observation is None:
        return False, "没有同一监控指标的异动观测，条件无法求值"
    match = _CONDITION_RE.match(str(condition or "").strip())
    if match is None:
        return True, f"条件「{condition}」不是阈值表达式，按同指标异动存在判定为命中"
    field = match.group("field").strip()
    op = match.group("op")
    threshold = float(match.group("threshold"))
    current = _numeric(observation.get(field))
    if current is None:
        return False, f"同指标观测缺少可比较字段「{field}」，条件未命中"
    satisfied = {
        ">": current > threshold,
        ">=": current >= threshold,
        "<": current < threshold,
        "<=": current <= threshold,
        "=": current == threshold,
        "==": current == threshold,
        "!=": current != threshold,
    }[op]
    unit = match.group("unit") or ""
    left = f"{field} {current:g}{unit}"
    right = f"{op} {threshold:g}{unit}"
    return satisfied, f"{left} {right}，{'条件成立' if satisfied else '条件不满足'}"


def _next_anomaly_code(rows: list[dict[str, Any]]) -> str:
    numbers: list[int] = []
    for row in rows:
        match = re.fullmatch(r"ANOM-(\d+)", str(row.get("记录编号", "")))
        if match:
            numbers.append(int(match.group(1)))
    return f"ANOM-{max(numbers, default=0) + 1:04d}"


class AlarmService:
    # ------------------------------------------------------------------ 查询
    def list_entries(
        self,
        *,
        keyword: str | None = None,
        status: str | None = None,
        page: int = 1,
        size: int = 20,
        evaluate: bool = True,
    ) -> tuple[list[dict[str, Any]], int]:
        if evaluate:
            self.evaluate_all(materialize=True)
        rows = store.rows(MODULE)
        if keyword:
            rows = [row for row in rows if keyword in str(row.get("规则编号", ""))]
        if status:
            rows = [row for row in rows if row.get("status") == status]
        total = len(rows)
        start = max(page - 1, 0) * size
        return rows[start:start + size], total

    def get_entry(self, entry_id: int, *, evaluate: bool = True) -> dict[str, Any] | None:
        entry = store.find(MODULE, entry_id)
        if entry is not None and evaluate:
            self.evaluate_rule(entry, materialize=True)
        return entry

    # ------------------------------------------------------------------ 登记
    def create_entry(self, values: dict[str, Any]) -> tuple[dict[str, Any] | None, list[str]]:
        missing = [field for field in REQUIRED_FIELDS if not str(values.get(field) or "").strip()]
        if missing:
            return None, missing
        rows = store.rows(MODULE)
        entry: dict[str, Any] = {
            "id": max((int(row.get("id", 0)) for row in rows), default=0) + 1
        }
        # 监控字段与通知字段一起落库，不再只保存 REQUIRED_FIELDS。
        for field in REQUIRED_FIELDS + CONFIG_FIELDS:
            if field in values:
                entry[field] = values.get(field)
        for field in NOTIFICATION_FIELDS:
            entry.setdefault(field, "")
        entry["status"] = STATUS_ORDER[0]
        entry["规则状态"] = STATUS_ORDER[0]
        entry["pending"] = True
        entry["abnormal"] = False
        entry["触发结果"] = "未触发（规则未启用）"
        entry["静默状态"] = "不适用"
        entry["静默剩余分钟"] = 0
        entry["触发次数"] = 0
        entry["最近触发时间"] = None
        entry["静默截止时间"] = None
        entry["最近触发记录"] = None
        entry["条件求值结果"] = "尚未求值"
        rows.append(entry)
        return entry, []

    def update_entry(self, entry_id: int, values: dict[str, Any]) -> tuple[dict[str, Any] | None, str]:
        entry = store.find(MODULE, entry_id)
        if entry is None:
            return None, f"告警规则 {entry_id} 不存在或已归档"
        dirty = False
        for field in REQUIRED_FIELDS + CONFIG_FIELDS:
            if field in values and values.get(field) is not None:
                entry[field] = values.get(field)
                dirty = True
        if not dirty:
            return None, "没有可保存的规则配置字段"
        self._normalize(entry)
        self.evaluate_rule(entry, materialize=True)
        return entry, "告警规则配置与通知字段已一起保存"

    # ------------------------------------------------------------------ 动作
    def run_action(self, entry_id: int, action: str) -> tuple[dict[str, Any] | None, str]:
        entry = store.find(MODULE, entry_id)
        if entry is None:
            return None, f"告警规则 {entry_id} 不存在或已归档"
        if action not in ACTION_RULES:
            return None, f"动作「{action}」不属于分析告警规则可执行范围"
        self._normalize(entry)
        current = str(entry.get("status") or STATUS_ORDER[0])
        if current not in ALLOWED_SOURCES[action]:
            return None, f"规则当前为「{current}」，不能执行「{action}」"
        target = ACTION_RULES[action]
        if target not in STATUS_ORDER:
            return None, f"目标状态「{target}」不在允许的状态序列里"
        entry["status"] = target
        entry["规则状态"] = target
        entry["pending"] = target != STATUS_ORDER[-1]
        entry["abnormal"] = action in NEGATIVE_ACTIONS
        if target != "已启用":
            entry["触发结果"] = "未触发（规则未启用）"
            entry["静默状态"] = "不适用"
            entry["静默剩余分钟"] = 0
        else:
            # 启用动作本身就是条件求值/静默判断的入口。
            self.evaluate_rule(entry, materialize=True)
        return entry, f"告警规则已{ACTION_LABELS[action]}"

    # ------------------------------------------------------------------ 求值
    def evaluate_all(self, *, materialize: bool = True) -> list[dict[str, Any]]:
        with _EVALUATE_LOCK:
            return [self._evaluate_rule_locked(row, materialize=materialize, now=None)
                    for row in store.rows(MODULE)]

    def evaluate_metric(self, metric: str, *, materialize: bool = True) -> list[dict[str, Any]]:
        """异动观测落库后，按监控指标求值所有规则。"""
        with _EVALUATE_LOCK:
            return [
                self._evaluate_rule_locked(row, materialize=materialize, now=None)
                for row in store.rows(MODULE)
                if str(row.get("监控指标") or "").strip() == str(metric or "").strip()
            ]

    def evaluate_entry(self, entry_id: int, *, materialize: bool = True) -> tuple[dict[str, Any] | None, str]:
        entry = store.find(MODULE, entry_id)
        if entry is None:
            return None, f"告警规则 {entry_id} 不存在或已归档"
        summary = self.evaluate_rule(entry, materialize=materialize)
        return entry, str(summary.get("触发结果") or "已求值")

    def evaluate_rule(self, entry: dict[str, Any], *, materialize: bool = True,
                      now: datetime | None = None) -> dict[str, Any]:
        """配置读取 -> 条件求值 -> 静默判断 -> 触发落库的单一入口（串行执行）。"""
        with _EVALUATE_LOCK:
            return self._evaluate_rule_locked(entry, materialize=materialize, now=now)

    def _evaluate_rule_locked(self, entry: dict[str, Any], *, materialize: bool,
                              now: datetime | None) -> dict[str, Any]:
        now = now or _now()
        self._normalize(entry)

        status = str(entry.get("status") or STATUS_ORDER[0])
        last_trigger = _parse_dt(entry.get("最近触发时间") or entry.get("last_triggered_at"))
        silence_minutes = parse_silence_minutes(entry.get("静默周期"))
        silence_expiry = (
            last_trigger + timedelta(minutes=silence_minutes)
            if last_trigger is not None and silence_minutes is not None
            else None
        )
        in_silence = bool(silence_expiry and now < silence_expiry)
        remaining = (
            max(1, int(math.ceil((silence_expiry - now).total_seconds() / 60)))
            if in_silence and silence_expiry is not None else 0
        )
        entry["静默剩余分钟"] = remaining
        entry["静默截止时间"] = _iso(silence_expiry)

        # 1) 配置读取：非启用状态不进入求值。
        if status != "已启用":
            entry["静默剩余分钟"] = 0
            entry["静默状态"] = "不适用"
            entry["条件求值结果"] = f"规则状态为「{status}」，配置读取阶段直接跳过条件求值"
            return self._finish(
                entry, "未触发（规则未启用）", "不适用", "规则状态为"
                f"「{status}」，配置读取阶段直接跳过条件求值与静默判断",
                triggered=False,
            )

        missing = [
            field for field in NOTIFICATION_FIELDS
            if not str(entry.get(field) or "").strip()
        ]
        if silence_minutes is None:
            missing.append("静默周期（格式无法识别）")
        if missing:
            entry["配置缺失字段"] = missing
            return self._finish(
                entry, "未触发（通知配置不完整）", "不适用",
                f"配置读取阶段发现缺少：{'、'.join(missing)}，为避免通知渠道丢失，拒绝触发",
                triggered=False,
            )
        entry["配置缺失字段"] = []

        # 2) 条件求值：同一监控指标的最新异动观测。
        observation = self._latest_observation(str(entry.get("监控指标") or ""))
        satisfied, reason = evaluate_condition(str(entry.get("告警条件") or ""), observation)
        entry["条件求值结果"] = reason
        if not satisfied:
            return self._finish(
                entry, "条件未满足", "静默中" if in_silence else "可触发", reason,
                triggered=False,
            )

        # 3) 静默判断：最近触发时间 + 静默周期之内跳过触发。
        if in_silence:
            return self._finish(
                entry, "静默中", "静默中",
                f"距最近触发未满静默周期 {silence_minutes} 分钟，剩余 {remaining} 分钟，跳过触发",
                triggered=False,
            )

        # 4) 触发落库。
        if not materialize:
            return self._finish(
                entry, "可触发", "可触发",
                f"{reason}；静默周期已结束，等待落库", triggered=False,
            )

        record, elapsed = self._materialize_trigger(entry, observation, now, silence_minutes)
        entry["触发次数"] = int(entry.get("触发次数") or 0) + 1
        entry["最近触发时间"] = _iso(now)
        entry["静默截止时间"] = _iso(now + timedelta(minutes=silence_minutes))
        entry["静默剩余分钟"] = silence_minutes
        entry["最近触发记录"] = record.get("记录编号")
        entry["最近触发记录ID"] = record.get("id")
        entry["abnormal"] = True
        return self._finish(
            entry, "已触发", "静默中",
            f"{reason}；距上次触发 {elapsed:g} 分钟 ≥ 静默周期 {silence_minutes} 分钟，"
            f"已写入异动记录 {record.get('记录编号')} 并通知「{entry.get('通知渠道')}」",
            triggered=True,
        )

    def _finish(self, entry: dict[str, Any], result: str, silence_state: str,
                detail: str, *, triggered: bool) -> dict[str, Any]:
        entry["触发结果"] = result
        entry["静默状态"] = silence_state
        entry["触发说明"] = detail
        entry["本次新建触发"] = triggered
        return {
            "规则编号": entry.get("规则编号"),
            "监控指标": entry.get("监控指标"),
            "规则状态": entry.get("status"),
            "通知渠道": entry.get("通知渠道"),
            "静默周期": entry.get("静默周期"),
            "触发结果": result,
            "静默状态": silence_state,
            "静默剩余分钟": entry.get("静默剩余分钟", 0),
            "静默截止时间": entry.get("静默截止时间"),
            "最近触发时间": entry.get("最近触发时间"),
            "最近触发记录": entry.get("最近触发记录"),
            "触发次数": entry.get("触发次数", 0),
            "条件求值结果": entry.get("条件求值结果"),
            "触发说明": detail,
            "本次新建触发": triggered,
        }

    # ------------------------------------------------------------------ 内部
    def _normalize(self, entry: dict[str, Any]) -> None:
        status = str(entry.get("status") or entry.get("规则状态") or STATUS_ORDER[0])
        if status not in STATUS_ORDER:
            status = STATUS_ORDER[0]
        entry["status"] = status
        # 列表里的“规则状态”与工作流状态同源，避免 seed 占位文本造成状态错位。
        entry["规则状态"] = status
        if "触发次数" not in entry:
            entry["触发次数"] = 0
        if "最近触发时间" not in entry:
            entry["最近触发时间"] = entry.get("last_triggered_at")
        if "最近触发记录" not in entry:
            entry["最近触发记录"] = None
        if "静默状态" not in entry:
            entry["静默状态"] = "不适用" if status != "已启用" else "可触发"

    @staticmethod
    def _latest_observation(metric: str) -> dict[str, Any] | None:
        """只取真实的指标观测；由告警规则自身落库的记录不能反过来喂给条件求值。"""
        if not metric:
            return None
        rows = [
            row for row in store.rows(ANOMALY_MODULE)
            if str(row.get("指标名称") or "").strip() == metric
            and not row.get("规则编号")
        ]
        if not rows:
            return None
        return max(rows, key=lambda row: int(row.get("id", 0)))

    def _materialize_trigger(self, entry: dict[str, Any], observation: dict[str, Any] | None,
                             now: datetime, silence_minutes: int) -> tuple[dict[str, Any], float]:
        rows = store.rows(ANOMALY_MODULE)
        last_trigger = _parse_dt(entry.get("最近触发时间"))
        elapsed = (
            round((now - last_trigger).total_seconds() / 60, 1)
            if last_trigger is not None else 0.0
        )
        record: dict[str, Any] = {
            "id": max((int(row.get("id", 0)) for row in rows), default=0) + 1,
            # 异动记录保留自己的分析工作流状态，告警状态单独存放，避免状态错位。
            "status": "待分析",
            "pending": True,
            "abnormal": True,
            "记录编号": _next_anomaly_code(rows),
            "指标名称": entry.get("监控指标"),
            "异动方向": (observation or {}).get("异动方向") or "未知",
            "异动幅度": (observation or {}).get("异动幅度") or "",
            "归因维度": (observation or {}).get("归因维度") or "待归因",
            "归因结论": "待分析",
            "处理建议": f"按告警规则 {entry.get('规则编号')} 排查",
            "异动时间": _iso(now),
            "规则编号": entry.get("规则编号"),
            "规则状态": entry.get("status"),
            "告警级别": entry.get("告警级别"),
            "通知渠道": entry.get("通知渠道"),
            "静默周期": entry.get("静默周期"),
            "触发来源": "分析告警规则触发",
            # 触发即进入静默期：没有静默周期时才保持“已触发”，两者口径与规则页一致。
            "告警状态": "静默中" if silence_minutes > 0 else "已触发",
            "触发时间": _iso(now),
            "静默状态": "静默中",
            "静默截止时间": _iso(now + timedelta(minutes=silence_minutes)),
            "静默剩余分钟": silence_minutes,
            "上次触发时间": _iso(last_trigger),
            "距上次触发分钟": elapsed,
            "静默校验": (
                f"距上次触发 {elapsed:g} 分钟 ≥ 静默周期 {silence_minutes} 分钟，允许触发"
                if last_trigger is not None
                else f"首次触发，进入 {silence_minutes} 分钟静默期"
            ),
        }
        rows.append(record)
        return record, elapsed
