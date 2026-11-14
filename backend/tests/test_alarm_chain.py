"""告警规则 -> 异动记录调用链回归测试。

覆盖三个调用点：配置读取（通知字段一起保存）、条件求值、静默判断，
并用 ALAR-0001 / ALAR-0003 核对修复前后的行为。

运行：cd backend && .venv/bin/python -m unittest discover -s tests -v
"""
from __future__ import annotations

import unittest
from datetime import datetime, timedelta, timezone

from app.seed import SEED_ROWS
from app.services.alarm import AlarmService, evaluate_condition, parse_silence_minutes
from app.services.anomaly import AnomalyService
from app.store import Store, store

UTC = timezone.utc


class ChainTestBase(unittest.TestCase):
    def setUp(self) -> None:
        store._tables = {name: [dict(row) for row in rows] for name, rows in SEED_ROWS.items()}
        self.alarm = AlarmService()
        self.anomaly = AnomalyService()

    def rule(self, code: str) -> dict:
        for row in store.rows("alarm"):
            if row.get("规则编号") == code:
                return row
        raise AssertionError(f"缺少规则 {code}")

    def anomalies_for(self, code: str) -> list[dict]:
        return [row for row in store.rows("anomaly") if row.get("规则编号") == code]


class ConfigReadTest(ChainTestBase):
    def test_notification_fields_saved_with_rule(self) -> None:
        entry, missing = self.alarm.create_entry({
            "规则编号": "ALAR-9001",
            "监控指标": "支付成功率",
            "告警条件": "异动幅度 > 3%",
            "告警级别": "严重",
            "通知渠道": "值班群",
            "静默周期": "30 分钟",
            "生效时段": "全天",
        })
        self.assertEqual(missing, [])
        assert entry is not None
        self.assertEqual(entry["通知渠道"], "值班群")
        self.assertEqual(entry["静默周期"], "30 分钟")
        self.assertEqual(entry["规则状态"], "草稿")

    def test_update_keeps_rule_and_notification_fields_together(self) -> None:
        entry, message = self.alarm.update_entry(1, {"通知渠道": "值班群", "静默周期": "15 分钟"})
        assert entry is not None
        self.assertIn("一起保存", message)
        self.assertEqual(entry["通知渠道"], "值班群")
        self.assertEqual(entry["静默周期"], "15 分钟")

    def test_missing_notification_config_blocks_trigger(self) -> None:
        rule = self.rule("ALAR-0002")
        rule["通知渠道"] = ""
        summary = self.alarm.evaluate_rule(rule, materialize=True)
        self.assertIn("通知配置不完整", str(summary["触发结果"]))
        self.assertEqual(self.anomalies_for("ALAR-0002"), [])


class ConditionTest(unittest.TestCase):
    def test_threshold_expression(self) -> None:
        satisfied, reason = evaluate_condition("异动幅度 > 20%", {"异动幅度": "25%"})
        self.assertTrue(satisfied)
        self.assertIn("条件成立", reason)
        satisfied, _ = evaluate_condition("异动幅度 > 20%", {"异动幅度": "18%"})
        self.assertFalse(satisfied)

    def test_missing_observation(self) -> None:
        satisfied, reason = evaluate_condition("异动幅度 > 20%", None)
        self.assertFalse(satisfied)
        self.assertIn("无法求值", reason)

    def test_silence_parsing(self) -> None:
        self.assertEqual(parse_silence_minutes("30 分钟"), 30)
        self.assertEqual(parse_silence_minutes("1 小时"), 60)
        self.assertIsNone(parse_silence_minutes("尽快"))


class SilenceTest(ChainTestBase):
    def test_first_read_triggers_then_silences(self) -> None:
        rule = self.rule("ALAR-0002")
        t0 = datetime(2026, 11, 14, 10, 0, tzinfo=UTC)
        first = self.alarm.evaluate_rule(rule, materialize=True, now=t0)
        self.assertEqual(first["触发结果"], "已触发")
        self.assertEqual(len(self.anomalies_for("ALAR-0002")), 1)

        within = self.alarm.evaluate_rule(rule, materialize=True, now=t0 + timedelta(minutes=10))
        self.assertEqual(within["触发结果"], "静默中")
        self.assertEqual(within["静默剩余分钟"], 20)
        self.assertEqual(len(self.anomalies_for("ALAR-0002")), 1)

        after = self.alarm.evaluate_rule(rule, materialize=True, now=t0 + timedelta(minutes=31))
        self.assertEqual(after["触发结果"], "已触发")
        self.assertEqual(len(self.anomalies_for("ALAR-0002")), 2)

    def test_trigger_record_carries_notification_fields(self) -> None:
        rule = self.rule("ALAR-0002")
        self.alarm.evaluate_rule(rule, materialize=True)
        record = self.anomalies_for("ALAR-0002")[0]
        self.assertEqual(record["通知渠道"], "值班群")
        self.assertEqual(record["静默周期"], "30 分钟")
        self.assertEqual(record["status"], "待分析")
        self.assertEqual(record["告警状态"], "静默中")


class StatusTransitionTest(ChainTestBase):
    def test_enable_moves_forward_not_backwards(self) -> None:
        entry, message = self.alarm.run_action(1, "启用规则")
        assert entry is not None
        self.assertEqual(entry["status"], "已启用")
        self.assertEqual(entry["规则状态"], "已启用")
        self.assertIn("启用", message)

    def test_alarm_0001_enable_triggers_with_channel(self) -> None:
        entry, _ = self.alarm.run_action(1, "启用规则")
        assert entry is not None
        self.assertEqual(entry["触发结果"], "已触发")
        records = self.anomalies_for("ALAR-0001")
        self.assertEqual(len(records), 1)
        self.assertEqual(records[0]["通知渠道"], "值班群")
        self.assertEqual(records[0]["指标名称"], "支付成功率")

    def test_alarm_0003_stays_silent_until_enabled(self) -> None:
        paused = self.rule("ALAR-0003")
        summary = self.alarm.evaluate_rule(paused, materialize=True)
        self.assertIn("规则未启用", str(summary["触发结果"]))
        self.assertEqual(self.anomalies_for("ALAR-0003"), [])

        entry, _ = self.alarm.run_action(3, "启用规则")
        assert entry is not None
        self.assertEqual(entry["触发结果"], "已触发")
        self.assertEqual(len(self.anomalies_for("ALAR-0003")), 1)

    def test_anomaly_workflow_and_alarm_state_are_separate(self) -> None:
        self.alarm.evaluate_rule(self.rule("ALAR-0002"), materialize=True)
        record = self.anomalies_for("ALAR-0002")[0]
        updated, _ = self.anomaly.run_action(int(record["id"]), "开始分析")
        assert updated is not None
        self.assertEqual(updated["status"], "分析中")
        self.assertEqual(updated["告警状态"], "静默中")

        closed, _ = self.anomaly.run_action(int(record["id"]), "确认归因")
        assert closed is not None
        self.assertEqual(closed["status"], "已归因")

    def test_illegal_transition_is_rejected(self) -> None:
        entry, message = self.anomaly.run_action(1, "关闭异动")
        self.assertIsNone(entry)
        self.assertIn("不能执行", message)

    def test_list_reads_are_idempotent(self) -> None:
        self.alarm.list_entries()
        self.anomaly.list_entries()
        self.anomaly.list_entries()
        self.assertEqual(len(self.anomalies_for("ALAR-0002")), 1)


class StoreIsolationTest(unittest.TestCase):
    def test_store_can_be_reset(self) -> None:
        fresh = Store()
        self.assertEqual(len(fresh.rows("alarm")), 3)


if __name__ == "__main__":
    unittest.main()
