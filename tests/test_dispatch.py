import os, tempfile, unittest

from urban_network.models import Reading, Segment
from urban_network.service import NetworkService
from urban_network.errors import VersionConflict


class DispatchTestBase(unittest.TestCase):
    def setUp(self):
        self.s = NetworkService()
        self.s.bootstrap()
        self.admin = self.s.auth.login("admin", "network-admin")
        self.operator = self.s.auth.login("operator", "network-operator")
        self.city = self.s.auth.login("dispatcher", "city-dispatch")
        # 两个片区、两个积水/管涌工单。
        self.s.register_segment(self.admin, Segment("SEG-N", "north", "drainage", 300, 5))
        self.s.register_segment(self.admin, Segment("SEG-S", "south", "drainage", 200, 3))
        rn = self.s.ingest_reading(self.admin, Reading("RN", "SEG-N", "sn", 120, 250, 90, "2026-09-25T01:00:00+00:00"))
        rs = self.s.ingest_reading(self.admin, Reading("RS", "SEG-S", "ss", 160, 120, 50, "2026-09-25T01:00:00+00:00"))
        self.wo_n = self.s.create_work_order(self.admin, "SEG-N", rn["alert_id"], "crew-n", priority=1)["work_order_id"]
        self.wo_s = self.s.create_work_order(self.admin, "SEG-S", rs["alert_id"], "crew-s", priority=3)["work_order_id"]
        # 移动泵：north 2 台、south 4 台；围挡 north 1。
        self.s.add_resource(self.admin, "PUMP-N", "mobile-pump", "north", 2)
        self.s.add_resource(self.admin, "PUMP-S", "mobile-pump", "south", 4)
        self.s.add_resource(self.admin, "BARRIER-N", "barrier", "north", 1)
        # 兼容关系：排水需求需要移动泵；围挡需求需要围挡。
        self.s.set_compatibility(self.admin, "pump", ["mobile-pump"])
        self.s.set_compatibility(self.admin, "barrier", ["barrier"])
        # 片区最低保有量：south 必须留 3 台泵。
        self.s.set_district_reserve(self.admin, "south", "mobile-pump", 3)
        # 道路到达时间。
        for rid, wid, minutes in (
            ("PUMP-N", self.wo_n, 8), ("PUMP-S", self.wo_n, 35),
            ("PUMP-N", self.wo_s, 40), ("PUMP-S", self.wo_s, 10),
            ("BARRIER-N", self.wo_n, 6),
        ):
            self.s.set_road_access(self.admin, rid, wid, minutes)
        self.s.declare_demand(self.admin, self.wo_n, "pump", 3)
        self.s.declare_demand(self.admin, self.wo_s, "pump", 1)

    def _assignments(self, plan):
        return {(a["work_order_id"], a["resource_id"]): a["quantity"] for a in plan["assignments"]}


class DeterministicPlanTests(DispatchTestBase):
    def test_high_risk_home_first_then_cross_district_beyond_reserve(self):
        plan = self.s.preview_dispatch(self.city, [self.wo_s, self.wo_n])
        a = self._assignments(plan)
        # 高风险 north 工单先拿到本片区 2 台，再跨片区拿到 south 超出保有量的 1 台。
        self.assertEqual(a[(self.wo_n, "PUMP-N")], 2)
        self.assertEqual(a[(self.wo_n, "PUMP-S")], 1)
        # south 工单只能拿到剩余 0 台：4 - 保有量3 - 外拨1 = 0。
        unmet = {u["work_order_id"]: u for u in plan["unmet"]}
        self.assertIn(self.wo_s, unmet)
        self.assertEqual(unmet[self.wo_s]["unmet"], 1)
        self.assertIn("district-reserve-protected", unmet[self.wo_s]["reasons"])
        self.assertTrue(plan["cross_district"])
        # south 可用量在预演阶段不变。
        self.assertEqual(self.s.resource(self.city, "PUMP-S")["available"], 4)

    def test_preview_is_deterministic_and_deduped(self):
        p1 = self.s.preview_dispatch(self.city, [self.wo_n, self.wo_s])
        p2 = self.s.preview_dispatch(self.city, [self.wo_n, self.wo_s])
        self.assertEqual(p1["plan_id"], p2["plan_id"])
        self.assertTrue(p2["replayed"])
        self.assertEqual(p1["resource_version"], p2["resource_version"])

    def test_blocked_road_is_reported_as_unmet_reason(self):
        self.s.set_road_access(self.admin, "PUMP-S", self.wo_n, 35, reachable=False)
        plan = self.s.preview_dispatch(self.city, [self.wo_n])
        unmet = plan["unmet"][0]
        self.assertEqual(unmet["unmet"], 1)
        self.assertIn("road-unreachable", unmet["reasons"])
        self.assertFalse(any(x["resource_id"] == "PUMP-S" for x in plan["assignments"]))

    def test_incompatible_kind_not_assigned(self):
        # 抢修队需求没有登记任何兼容资源，不能拿泵或围挡顶替。
        self.s.declare_demand(self.admin, self.wo_n, "repair-crew", 1)
        plan = self.s.preview_dispatch(self.city, [self.wo_n])
        self.assertEqual(plan["assignments"], [])
        self.assertIn("no-compatible-resource-kind", plan["unmet"][0]["reasons"])

    def test_manual_adjustment_requires_reason_and_reserves_reserve(self):
        plan = self.s.preview_dispatch(self.city, [self.wo_n])
        with self.assertRaises(ValueError):
            self.s.adjust_plan(self.city, plan["plan_id"], {"assignments": []}, "  ")
        # 人工只给本片区 1 台，放弃跨片区外拨；不得越过 south 保有量。
        adjusted = self.s.adjust_plan(self.city, plan["plan_id"], {
            "assignments": [{"work_order_id": self.wo_n, "resource_id": "PUMP-N", "quantity": 1}]
        }, "south road flooded, hold reserve")
        self.assertTrue(adjusted["manual"])
        self.assertEqual(adjusted["adjustment_reason"], "south road flooded, hold reserve")
        self.assertEqual(len(adjusted["assignments"]), 1)
        self.assertEqual(adjusted["unmet"][0]["reasons"], ["manually-adjusted"])
        # 越过保有量的人工调整被拒绝。
        with self.assertRaises(ValueError):
            self.s.adjust_plan(self.city, plan["plan_id"], {
                "assignments": [{"work_order_id": self.wo_n, "resource_id": "PUMP-S", "quantity": 2}]
            }, "try to breach reserve")


class ConfirmationTests(DispatchTestBase):
    def test_confirm_checks_version_and_applies_atomically(self):
        plan = self.s.preview_dispatch(self.city, [self.wo_n, self.wo_s])
        # 预演后资源发生变化：版本核对必须失败，且不能留下部分扣减。
        self.s.add_resource(self.admin, "PUMP-E", "mobile-pump", "east", 5)
        self.s.set_road_access(self.admin, "PUMP-E", self.wo_n, 20)
        with self.assertRaises(VersionConflict):
            self.s.confirm_plan(self.city, plan["plan_id"])
        self.assertEqual(self.s.resource(self.city, "PUMP-N")["available"], 2)
        self.assertEqual(self.s.resource(self.city, "PUMP-S")["available"], 4)
        failed = [e for e in self.s.audit_events(self.city, "dispatch_plan", plan["plan_id"]) if e["action"] == "confirm_failed"]
        self.assertTrue(failed)
        # 重新预演后确认成功，扣减一次完成。
        plan2 = self.s.preview_dispatch(self.city, [self.wo_n, self.wo_s])
        confirmed = self.s.confirm_plan(self.city, plan2["plan_id"])
        self.assertEqual(confirmed["status"], "confirmed")
        self.assertEqual(confirmed["applied_resource_version"], plan2["resource_version"])
        self.assertEqual(len(confirmed["allocations"]), len(confirmed["assignments"]))
        self.assertEqual(self.s.resource(self.city, "PUMP-N")["available"], 0)
        self.assertEqual(self.s.resource(self.city, "PUMP-S")["available"], 3)  # 保有量守住

    def test_repeated_confirm_returns_same_plan(self):
        plan = self.s.preview_dispatch(self.city, [self.wo_s])
        first = self.s.confirm_plan(self.city, plan["plan_id"])
        again = self.s.confirm_plan(self.city, plan["plan_id"])
        self.assertEqual(first["plan_id"], again["plan_id"])
        self.assertEqual(first["allocations"], again["allocations"])
        self.assertTrue(again["replayed"])
        self.assertEqual(self.s.resource(self.city, "PUMP-S")["available"], 3)

    def test_operator_cannot_confirm_cross_district_plan(self):
        plan = self.s.preview_dispatch(self.operator, [self.wo_n, self.wo_s])
        self.assertTrue(plan["cross_district"])
        with self.assertRaises(PermissionError):
            self.s.confirm_plan(self.operator, plan["plan_id"])
        # 资源未被扣减。
        self.assertEqual(self.s.resource(self.operator, "PUMP-S")["available"], 4)
        # 市级调度员可以确认。
        ok = self.s.confirm_plan(self.city, plan["plan_id"])
        self.assertEqual(ok["status"], "confirmed")

    def test_operator_can_confirm_within_district_plan(self):
        # south 本片区 1 台需求不跨片区，操作员可确认。
        plan = self.s.preview_dispatch(self.operator, [self.wo_s])
        self.assertFalse(plan["cross_district"])
        ok = self.s.confirm_plan(self.operator, plan["plan_id"])
        self.assertEqual(ok["status"], "confirmed")

    def test_plans_and_final_allocation_survive_restart(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "n2.sqlite3")
            svc = NetworkService(path)
            svc.bootstrap()
            t = svc.auth.login("admin", "network-admin")
            seg = svc.register_segment(t, Segment("S1", "north", "drainage", 100, 5))
            r = svc.ingest_reading(t, Reading("R1", "S1", "x", 120, 250, 90, "2026-09-25T02:00:00+00:00"))
            wo = svc.create_work_order(t, "S1", r["alert_id"], "c", 1)["work_order_id"]
            svc.add_resource(t, "P", "mobile-pump", "north", 2)
            svc.set_compatibility(t, "pump", ["mobile-pump"])
            svc.set_road_access(t, "P", wo, 5)
            svc.declare_demand(t, wo, "pump", 1)
            p = svc.preview_dispatch(t, [wo])
            svc.adjust_plan(t, p["plan_id"], {"assignments": [{"work_order_id": wo, "resource_id": "P", "quantity": 1}]}, "on-site call")
            svc.confirm_plan(t, p["plan_id"])
            plan_id = p["plan_id"]
            svc.db.close()
            restarted = NetworkService(path)
            rt = restarted.auth.login("dispatcher", "city-dispatch")
            view = restarted.plan(rt, plan_id)
            self.assertEqual(view["status"], "confirmed")
            self.assertTrue(view["manual"])
            self.assertEqual(view["adjustment_reason"], "on-site call")
            self.assertEqual(view["assignments"][0]["quantity"], 1)
            self.assertEqual(restarted.resource(rt, "P")["available"], 1)
            self.assertEqual(len(restarted.list_plans(rt)["plans"]), 1)


if __name__ == "__main__":
    unittest.main()
