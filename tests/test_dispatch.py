"""跨片区调度方案：预演、调整、版本核对、确认幂等与持久化测试。"""
from __future__ import annotations

import json
import os
import tempfile
import unittest

from urban_network.models import Reading, Segment
from urban_network.planning import DispatchConflict, build_plan, digest
from urban_network.service import NetworkService


def _work_order(service, token, sid, rid, flow=250, acoustic=90, priority=1):
    reading = service.ingest_reading(
        token, Reading(rid, sid, "sensor", 100, flow, acoustic, "2026-09-24T10:00:00+00:00")
    )
    return service.create_work_order(token, sid, reading["alert_id"], "crew", priority)["work_order_id"]


class DispatchServiceTests(unittest.TestCase):
    def setUp(self):
        self.s = NetworkService()
        self.s.bootstrap()
        self.admin = self.s.auth.login("admin", "network-admin")
        self.operator = self.s.auth.login("operator", "network-operator")
        self.dispatcher = self.s.auth.login("dispatcher", "network-dispatcher")
        self.s.register_segment(self.admin, Segment("S1", "north", "drainage", 100, 5))
        self.s.register_segment(self.admin, Segment("S2", "east", "drainage", 100, 3))
        self.s.register_segment(self.admin, Segment("S3", "west", "water", 100, 4))
        self.wo_n = _work_order(self.s, self.admin, "S1", "R1", priority=1)
        self.wo_e = _work_order(self.s, self.admin, "S2", "R2", flow=200, priority=3)
        self.wo_w = _work_order(self.s, self.admin, "S3", "R3", priority=2)
        for rid, kind, district, cap in (
            ("PUMP-N", "mobile-pump", "north", 1),
            ("PUMP-W", "mobile-pump", "west", 3),
            ("BAR-E", "barrier", "east", 4),
        ):
            self.s.add_resource(self.admin, rid, kind, district, cap)
        self.s.set_district_reserve(self.admin, "mobile-pump", "west", 1)
        for f, t, m in (("west", "north", 35), ("west", "east", 50), ("east", "north", 20)):
            self.s.set_travel_time(self.admin, f, t, m)

    def _demands(self):
        return [
            {"work_order_id": self.wo_n, "kind": "mobile-pump", "quantity": 2, "max_minutes": 60},
            {"work_order_id": self.wo_w, "kind": "barrier", "quantity": 1, "max_minutes": 60},
        ]

    def test_preview_does_not_deduct(self):
        before = self.s.resource(self.admin, "PUMP-W")["available"]
        plan = self.s.create_dispatch_plan(self.operator, self._demands())
        self.assertEqual(plan["state"], "preview")
        self.assertEqual(self.s.resource(self.admin, "PUMP-W")["available"], before)
        self.assertEqual(len(self.s.dispatch_plan(self.dispatcher, plan["plan_id"])["final_allocations"]), 0)

    def test_reserve_and_local_priority(self):
        plan = self.s.create_dispatch_plan(self.dispatcher, [
            {"work_order_id": self.wo_n, "kind": "mobile-pump", "quantity": 3, "max_minutes": 60}
        ])
        # west 有 3 台、保有量 1 台，故最多外借 2 台；north 本地 1 台，共 3 台。
        pumps = {(a["resource_id"]): a["quantity"] for a in plan["allocations"]}
        self.assertEqual(pumps.get("PUMP-N"), 1)
        self.assertEqual(pumps.get("PUMP-W"), 2)
        self.assertEqual(self.s.resource(self.admin, "PUMP-W")["available"], 3)

    def test_unmet_reasons(self):
        # 没有任何 east->west 道路时间，west 围挡工单只能拿到 unknown-road 的未满足说明。
        plan = self.s.create_dispatch_plan(self.dispatcher, [
            {"work_order_id": self.wo_w, "kind": "barrier", "quantity": 2, "max_minutes": 60}
        ])
        self.assertEqual(len(plan["allocations"]), 0)
        unmet = plan["unmet"][0]
        self.assertEqual(unmet["shortage"], 2)
        self.assertIn("road-time-unknown", unmet["reasons"])

    def test_travel_window_excludes_slow_routes(self):
        plan = self.s.create_dispatch_plan(self.dispatcher, [
            {"work_order_id": self.wo_e, "kind": "mobile-pump", "quantity": 2, "max_minutes": 30}
        ])
        # west->east 50 分钟超出 30 分钟窗口，east 本地无泵 -> 全部未满足。
        self.assertEqual(plan["allocations"], [])
        self.assertIn("beyond-max-minutes", plan["unmet"][0]["reasons"])

    def test_operator_cannot_confirm_cross_district(self):
        plan = self.s.create_dispatch_plan(self.operator, self._demands())
        self.assertTrue(plan["cross_district"])
        with self.assertRaises(PermissionError):
            self.s.confirm_dispatch_plan(self.operator, plan["plan_id"])
        # 被拒绝后资源未扣减、方案仍停留在预演。
        self.assertEqual(self.s.resource(self.admin, "PUMP-W")["available"], 3)
        self.assertEqual(self.s.dispatch_plan(self.dispatcher, plan["plan_id"])["state"], "preview")

    def test_dispatcher_confirm_and_idempotent_replay(self):
        plan = self.s.create_dispatch_plan(self.operator, self._demands())
        confirmed = self.s.confirm_dispatch_plan(self.dispatcher, plan["plan_id"])
        self.assertEqual(confirmed["state"], "confirmed")
        first_ids = [a["allocation_id"] for a in confirmed["allocations"]]
        replay = self.s.confirm_dispatch_plan(self.dispatcher, plan["plan_id"])
        self.assertTrue(replay["replayed"])
        self.assertEqual([a["allocation_id"] for a in replay["allocations"]], first_ids)
        # 只扣减一次。
        self.assertEqual(self.s.resource(self.admin, "PUMP-W")["available"], 2)
        self.assertEqual(self.s.resource(self.admin, "PUMP-N")["available"], 0)

    def test_version_mismatch_fails_without_partial_deduction(self):
        plan = self.s.create_dispatch_plan(self.dispatcher, [
            {"work_order_id": self.wo_n, "kind": "mobile-pump", "quantity": 1, "max_minutes": 60}
        ])
        self.s.add_resource(self.admin, "PUMP-X", "mobile-pump", "north", 2)  # 版本漂移
        with self.assertRaises(DispatchConflict):
            self.s.confirm_dispatch_plan(self.dispatcher, plan["plan_id"])
        self.assertEqual(self.s.resource(self.admin, "PUMP-N")["available"], 1)
        view = self.s.dispatch_plan(self.dispatcher, plan["plan_id"])
        self.assertEqual(view["state"], "failed")
        self.assertIn("资源版本", view["failure_reason"])
        # 失败方案不能再确认。
        with self.assertRaises(DispatchConflict):
            self.s.confirm_dispatch_plan(self.dispatcher, plan["plan_id"])

    def test_adjustment_requires_reason_and_keeps_history(self):
        plan = self.s.create_dispatch_plan(self.dispatcher, self._demands())
        with self.assertRaises(ValueError):
            self.s.adjust_dispatch_plan(self.dispatcher, plan["plan_id"], {"BAR-E": {self.wo_w: 0}}, "  ")
        # 道路不通时调整非法。
        with self.assertRaises(ValueError):
            self.s.adjust_dispatch_plan(self.dispatcher, plan["plan_id"], {"BAR-E": {self.wo_w: 1}}, "试调")
        # 登记道路后资源版本变化，调整基于新快照重算并记录修订。
        self.s.set_travel_time(self.admin, "east", "west", 25)
        current = self.s.create_dispatch_plan(self.dispatcher, self._demands())
        adjusted = self.s.adjust_dispatch_plan(
            self.dispatcher, plan["plan_id"], {"BAR-E": {self.wo_w: 1}}, "优先调用 east 围挡"
        )
        self.assertEqual(adjusted["revision"], 2)
        self.assertEqual(adjusted["resource_version"], current["resource_version"])
        view = self.s.dispatch_plan(self.dispatcher, plan["plan_id"])
        self.assertEqual(view["revisions"][0]["revision"], 1)
        self.assertTrue(view["revisions"][0]["reason"].startswith("优先调用"))
        self.assertEqual(view["resource_version"], current["resource_version"])

    def test_incompatible_adjustment_rejected(self):
        plan = self.s.create_dispatch_plan(self.dispatcher, self._demands())
        with self.assertRaises(ValueError):
            self.s.adjust_dispatch_plan(
                self.dispatcher, plan["plan_id"], {"PUMP-N": {self.wo_w: 1}}, "泵代围挡"
            )

    def test_compatibility_mapping(self):
        self.s.add_resource(self.admin, "FP-W", "flood-pump", "west", 2)
        self.s.set_compatibility(self.admin, "flood-pump", "mobile-pump", True)
        self.s.set_travel_time(self.admin, "west", "east", 20)
        plan = self.s.create_dispatch_plan(self.dispatcher, [
            {"work_order_id": self.wo_e, "kind": "mobile-pump", "quantity": 1, "max_minutes": 30}
        ])
        self.assertEqual(plan["allocations"][0]["resource_id"], "FP-W")

    def test_persistence_after_restart(self):
        with tempfile.TemporaryDirectory() as directory:
            db = os.path.join(directory, "network.sqlite3")
            service = NetworkService(db)
            service.bootstrap()
            admin = service.auth.login("admin", "network-admin")
            dispatcher = service.auth.login("dispatcher", "network-dispatcher")
            service.register_segment(admin, Segment("S1", "north", "drainage", 100, 5))
            wo = _work_order(service, admin, "S1", "R1")
            service.add_resource(admin, "PUMP-N", "mobile-pump", "north", 2)
            plan = service.create_dispatch_plan(dispatcher, [
                {"work_order_id": wo, "kind": "mobile-pump", "quantity": 1}
            ])
            service.confirm_dispatch_plan(dispatcher, plan["plan_id"])
            plan_id = plan["plan_id"]
            del service
            revived = NetworkService(db)
            token = revived.auth.login("dispatcher", "network-dispatcher")
            view = revived.dispatch_plan(token, plan_id)
            self.assertEqual(view["state"], "confirmed")
            self.assertEqual(len(view["final_allocations"]), 1)
            self.assertEqual(view["final_allocations"][0]["quantity"], 1)

    def test_deterministic_same_snapshot(self):
        a = self.s.create_dispatch_plan(self.dispatcher, self._demands())
        b = self.s.create_dispatch_plan(self.dispatcher, self._demands())
        strip = lambda p: [ {k: v for k, v in x.items() if k != "allocation_id"} for x in p["allocations"] ]
        self.assertEqual(strip(a), strip(b))
        self.assertEqual(a["resource_version"], b["resource_version"])
        self.assertEqual(a["unmet"], b["unmet"])


class PlannerUnitTests(unittest.TestCase):
    def test_deterministic_ordering_by_risk(self):
        resources = [
            {"resource_id": "P", "kind": "pump", "district": "x", "capacity": 1, "available": 1},
        ]
        reserves = {}
        travel = {}
        compat = {}
        orders = {
            "hi": {"district": "x", "severity": "critical", "score": 90.0, "priority": 3, "created_at": "t2"},
            "lo": {"district": "x", "severity": "low", "score": 1.0, "priority": 1, "created_at": "t1"},
        }
        demands = [
            {"work_order_id": "lo", "kind": "pump", "quantity": 1},
            {"work_order_id": "hi", "kind": "pump", "quantity": 1},
        ]
        result = build_plan(demands, resources, reserves, travel, compat, orders)
        self.assertEqual(result["allocations"][0]["work_order_id"], "hi")
        self.assertEqual(result["unmet"][0]["work_order_id"], "lo")


if __name__ == "__main__":
    unittest.main()
