"""离线命令行验收入口。"""
from __future__ import annotations
import argparse,json
from .models import Reading,Segment
from .service import NetworkService
def run():
    s=NetworkService(); s.bootstrap(); t=s.auth.login("admin","network-admin"); dt=s.auth.login("dispatcher","city-dispatch")
    s.register_segment(t,Segment("SEG-DEMO","north","water",680,5)); r=s.ingest_reading(t,Reading("RD-DEMO","SEG-DEMO","sensor-01",160,230,88,"2026-09-24T10:00:00+00:00")); report=s.risk_report(t,"SEG-DEMO"); order=s.create_work_order(t,"SEG-DEMO",r["alert_id"],"crew-north",1); s.add_resource(t,"PUMP-01","mobile-pump","north",2); allocation=s.allocate(t,"PUMP-01",order["work_order_id"],1)
    # 暴雨跨片区预演：第二个片区工单，本片区泵受保有量保护，需市级调度员确认外拨。
    s.register_segment(t,Segment("SEG-SOUTH","south","drainage",420,4)); r2=s.ingest_reading(t,Reading("RD-SOUTH","SEG-SOUTH","sensor-02",120,250,90,"2026-09-24T10:05:00+00:00"))
    o2=s.create_work_order(t,"SEG-SOUTH",r2["alert_id"],"crew-south",2); s.add_resource(t,"PUMP-02","mobile-pump","south",3)
    s.set_compatibility(t,"pump",["mobile-pump"]); s.set_district_reserve(t,"south","mobile-pump",2)
    s.set_road_access(t,"PUMP-02",o2["work_order_id"],9); s.declare_demand(t,o2["work_order_id"],"pump",2)
    plan=s.preview_dispatch(dt,[o2["work_order_id"]]); confirmed=s.confirm_plan(dt,plan["plan_id"])
    return {"status":"ok","segment":"SEG-DEMO","severity":r["risk"]["severity"],"probability":report["leak_probability"],"allocation":allocation["allocation_id"],"dispatch_plan":confirmed["plan_id"],"dispatch_status":confirmed["status"],"dispatch_unmet":confirmed["unmet"]}
def main():
    parser=argparse.ArgumentParser(); parser.add_argument("--workspace",default="."); parser.parse_args(); print(json.dumps(run(),ensure_ascii=False))
if __name__=="__main__":main()
