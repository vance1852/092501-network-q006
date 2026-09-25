"""离线命令行验收入口。"""
from __future__ import annotations
import argparse,json,tempfile,os
from .models import Reading,Segment
from .planning import DispatchConflict
from .service import NetworkService

def _storm_ticket(s,token,segment_id,reading_id,district,flow,acoustic,priority):
    r=s.ingest_reading(token,Reading(reading_id,segment_id,"sensor",100,flow,acoustic,"2026-09-24T10:00:00+00:00"))
    return s.create_work_order(token,segment_id,r["alert_id"],f"crew-{district}",priority)["work_order_id"]

def run():
    workspace_dir=tempfile.mkdtemp(prefix="urban-acceptance-")
    db_path=os.path.join(workspace_dir,"network.sqlite3")
    s=NetworkService(db_path); s.bootstrap()
    admin=s.auth.login("admin","network-admin")
    operator=s.auth.login("operator","network-operator")
    dispatcher=s.auth.login("dispatcher","network-dispatcher")

    s.register_segment(admin,Segment("SEG-N","north","drainage",680,5))
    s.register_segment(admin,Segment("SEG-E","east","drainage",420,4))
    s.register_segment(admin,Segment("SEG-W","west","water",510,3))
    # 暴雨同时触发积水（移动泵）和管涌（围挡）工单。
    wo_n=_storm_ticket(s,admin,"SEG-N","RD-N","north",250,90,1)
    wo_e=_storm_ticket(s,admin,"SEG-E","RD-E","east",235,82,2)
    wo_w=_storm_ticket(s,admin,"SEG-W","RD-W","west",240,88,1)

    # 资源：本片区泵不足，需从 west 跨片区调泵；east 有富余围挡。
    s.add_resource(admin,"PUMP-N","mobile-pump","north",1)
    s.add_resource(admin,"PUMP-W","mobile-pump","west",3)
    s.add_resource(admin,"BARRIER-E","barrier","east",4)
    s.add_resource(admin,"BARRIER-W","barrier","west",2)
    s.add_resource(admin,"CREW-W","repair-crew","west",2)

    # 片区最低保有量：west 的泵至少留 1 台。
    s.set_district_reserve(admin,"mobile-pump","west",1)
    # 道路到达时间（分钟）；north<->east 未登记，无法跨片区。
    s.set_travel_time(admin,"west","north",35)
    s.set_travel_time(admin,"west","east",50)
    s.set_travel_time(admin,"east","west",45)
    s.set_travel_time(admin,"east","north",20)
    # 兼容性：大流量泵车可代作移动泵。
    s.set_compatibility(admin,"mobile-pump","flood-pump",True)

    demands=[{"work_order_id":wo_n,"kind":"mobile-pump","quantity":2,"max_minutes":60},
             {"work_order_id":wo_w,"kind":"mobile-pump","quantity":2,"max_minutes":40},
             {"work_order_id":wo_w,"kind":"barrier","quantity":3,"max_minutes":60},
             {"work_order_id":wo_e,"kind":"repair-crew","quantity":1,"max_minutes":60}]
    plan=s.create_dispatch_plan(operator,demands)
    assert plan["state"]=="preview"
    # 普通操作员不能越权确认跨片区方案。
    try:
        s.confirm_dispatch_plan(operator,plan["plan_id"]); raise AssertionError("operator must not confirm cross-district plan")
    except PermissionError:
        pass
    # 人工调整：围挡优先由本片区 east 保障 west 工单，并留存理由。
    adjusted=s.adjust_dispatch_plan(dispatcher,plan["plan_id"],{"BARRIER-E":{wo_w:2}},"优先调用 east 富余围挡，保留 west 抢修力量")
    assert adjusted["revision"]==2
    confirmed=s.confirm_dispatch_plan(dispatcher,adjusted["plan_id"])
    assert confirmed["state"]=="confirmed"
    # 重复确认返回同一方案、同一分配编号，且不再扣减。
    replay=s.confirm_dispatch_plan(dispatcher,confirmed["plan_id"])
    assert replay["replayed"] is True and replay["allocations"]==confirmed["allocations"]

    # 重启后方案、人工调整理由和最终分配仍可查询。
    restarted=NetworkService(db_path)
    view=restarted.dispatch_plan(restarted.auth.login("dispatcher","network-dispatcher"),plan["plan_id"])
    assert view["state"]=="confirmed" and len(view["final_allocations"])==len(confirmed["allocations"])
    assert view["revisions"] and view["revisions"][0]["reason"].startswith("优先调用")

    # 版本漂移：资源变动后未重预演的旧方案确认必须失败且零扣减。
    stale=s.create_dispatch_plan(dispatcher,[{"work_order_id":wo_e,"kind":"mobile-pump","quantity":1,"max_minutes":60}])
    before=s.resource(admin,"PUMP-W")["available"]
    s.add_resource(admin,"PUMP-X","mobile-pump","east",1)  # 改变资源版本
    try:
        s.confirm_dispatch_plan(dispatcher,stale["plan_id"]); raise AssertionError("stale plan must fail")
    except DispatchConflict:
        pass
    assert s.resource(admin,"PUMP-W")["available"]==before

    return {"status":"ok","plan_id":plan["plan_id"],"resource_version":adjusted["resource_version"],"cross_district":adjusted["cross_district"],"allocations":len(confirmed["allocations"]),"unmet":adjusted["unmet"],"revision":2,"persisted_after_restart":True}

def main():
    parser=argparse.ArgumentParser(); parser.add_argument("--workspace",default="."); parser.parse_args(); print(json.dumps(run(),ensure_ascii=False))
if __name__=="__main__":main()
