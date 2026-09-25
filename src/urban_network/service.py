"""协调管网监测、告警、工单和应急资源分配的应用服务。"""
from __future__ import annotations
import hashlib,json,uuid
from .auth import Auth
from .models import Reading,Segment,as_dict,utcnow
from .planning import DispatchConflict, build_plan, canonical_json, digest, render_lines
from .risk import leak_probability,score_reading
from .storage import audit,connect,rows,transaction
class NetworkService:
    def __init__(self,database=":memory:"): self.db=connect(database); self.auth=Auth(self.db)
    def bootstrap(self):
        for uid,pwd,role in (("admin","network-admin","admin"),("operator","network-operator","operator"),("dispatcher","network-dispatcher","dispatcher")):
            try:self.auth.create_user(uid,pwd,role)
            except Exception:pass
    def register_segment(self,token,segment):
        actor=self.auth.require(token,"admin"); segment.validate(); now=utcnow()
        with transaction(self.db):
            self.db.execute("INSERT INTO segments VALUES(?,?,?,?,?,?,?,?)",(segment.segment_id,segment.district,segment.network_type,segment.length_m,segment.criticality,segment.status,now,now)); audit(self.db,"segment",segment.segment_id,"created",actor.user_id,as_dict(segment))
        return self.segment(token,segment.segment_id)
    def segment(self,token,segment_id):
        self.auth.require(token,"read"); row=self.db.execute("SELECT * FROM segments WHERE segment_id=?",(segment_id,)).fetchone()
        if not row:raise KeyError(segment_id)
        return dict(row)
    def ingest_reading(self,token,reading):
        actor=self.auth.require(token,"measure"); reading.validate(); seg=self.db.execute("SELECT criticality FROM segments WHERE segment_id=?",(reading.segment_id,)).fetchone()
        if not seg:raise KeyError(reading.segment_id)
        risk=score_reading(reading.pressure_kpa,reading.flow_lps,reading.acoustic_db,seg[0]); fingerprint=hashlib.sha256(f"{reading.segment_id}|{reading.sensor_id}|{reading.observed_at}".encode()).hexdigest()
        with transaction(self.db):
            if self.db.execute("SELECT reading_id FROM readings WHERE reading_id=?",(reading.reading_id,)).fetchone(): return {"reading_id":reading.reading_id,"duplicate":True,"risk":as_dict(risk)}
            self.db.execute("INSERT INTO readings VALUES(?,?,?,?,?,?,?)",(reading.reading_id,reading.segment_id,reading.sensor_id,reading.pressure_kpa,reading.flow_lps,reading.acoustic_db,reading.observed_at)); alert_id=None
            if risk.severity in {"high","critical"}:
                alert_id="alert-"+fingerprint[:18]; self.db.execute("INSERT OR IGNORE INTO alerts VALUES(?,?,?,?,?,?,?,?)",(alert_id,reading.segment_id,fingerprint,risk.severity,risk.score,"open",utcnow(),None))
            audit(self.db,"reading",reading.reading_id,"ingested",actor.user_id,{"risk":as_dict(risk),"alert_id":alert_id})
        return {"reading_id":reading.reading_id,"duplicate":False,"risk":as_dict(risk),"alert_id":alert_id}
    def risk_report(self,token,segment_id):
        self.auth.require(token,"analyze"); readings=rows(self.db,"SELECT * FROM readings WHERE segment_id=? ORDER BY observed_at",(segment_id,)); alerts=rows(self.db,"SELECT * FROM alerts WHERE segment_id=? ORDER BY created_at",(segment_id,)); return {"segment_id":segment_id,"readings":len(readings),"alerts":alerts,"leak_probability":leak_probability(alerts)}
    def create_work_order(self,token,segment_id,alert_id,assignee,priority=3):
        actor=self.auth.require(token,"work_order")
        if not assignee.strip() or not 1<=priority<=5:raise ValueError("assignee and priority are invalid")
        if not self.db.execute("SELECT 1 FROM alerts WHERE alert_id=? AND segment_id=?",(alert_id,segment_id)).fetchone():raise KeyError(alert_id)
        wid="wo-"+uuid.uuid4().hex[:16]
        with transaction(self.db): self.db.execute("INSERT INTO work_orders VALUES(?,?,?,?,?,?,?,?)",(wid,segment_id,alert_id,assignee,"open",priority,utcnow(),utcnow())); audit(self.db,"work_order",wid,"created",actor.user_id,{"segment_id":segment_id,"alert_id":alert_id})
        return self.work_order(token,wid)
    def work_order(self,token,work_order_id):
        self.auth.require(token,"read"); row=self.db.execute("SELECT * FROM work_orders WHERE work_order_id=?",(work_order_id,)).fetchone()
        if not row:raise KeyError(work_order_id)
        return dict(row)
    def transition_work_order(self,token,work_order_id,target,reason):
        actor=self.auth.require(token,"work_order"); allowed={"open":{"assigned","cancelled"},"assigned":{"in_progress","cancelled"},"in_progress":{"completed","blocked"},"blocked":{"in_progress","cancelled"},"completed":set(),"cancelled":set()}
        if not reason.strip():raise ValueError("transition reason is required")
        with transaction(self.db):
            row=self.db.execute("SELECT status FROM work_orders WHERE work_order_id=?",(work_order_id,)).fetchone()
            if not row:raise KeyError(work_order_id)
            if target not in allowed.get(row[0],set()):raise ValueError("invalid work order transition")
            self.db.execute("UPDATE work_orders SET status=?,updated_at=? WHERE work_order_id=?",(target,utcnow(),work_order_id)); audit(self.db,"work_order",work_order_id,"transition",actor.user_id,{"from":row[0],"to":target,"reason":reason})
        return self.work_order(token,work_order_id)
    def add_resource(self,token,resource_id,kind,district,capacity):
        actor=self.auth.require(token,"admin")
        if capacity<=0 or not kind.strip() or not district.strip():raise ValueError("resource fields are invalid")
        with transaction(self.db):self.db.execute("INSERT INTO resources VALUES(?,?,?,?,?)",(resource_id,kind,district,capacity,capacity)); audit(self.db,"resource",resource_id,"created",actor.user_id,{"kind":kind,"district":district,"capacity":capacity})
        return self.resource(token,resource_id)
    def resource(self,token,resource_id):
        self.auth.require(token,"read"); row=self.db.execute("SELECT * FROM resources WHERE resource_id=?",(resource_id,)).fetchone()
        if not row:raise KeyError(resource_id)
        return dict(row)
    def allocate(self,token,resource_id,work_order_id,quantity):
        actor=self.auth.require(token,"allocate")
        if quantity<=0:raise ValueError("quantity must be positive")
        aid="alloc-"+uuid.uuid4().hex[:16]
        with transaction(self.db):
            resource=self.db.execute("SELECT available FROM resources WHERE resource_id=?",(resource_id,)).fetchone()
            if not resource:raise KeyError(resource_id)
            if not self.db.execute("SELECT 1 FROM work_orders WHERE work_order_id=?",(work_order_id,)).fetchone():raise KeyError(work_order_id)
            if resource[0]<quantity:raise ValueError("resource capacity exceeded")
            old=self.db.execute("SELECT allocation_id FROM allocations WHERE resource_id=? AND work_order_id=?",(resource_id,work_order_id)).fetchone()
            if old:return {"allocation_id":old[0],"duplicate":True}
            self.db.execute("INSERT INTO allocations VALUES(?,?,?,?,?)",(aid,resource_id,work_order_id,quantity,utcnow())); self.db.execute("UPDATE resources SET available=available-? WHERE resource_id=?",(quantity,resource_id)); audit(self.db,"resource",resource_id,"allocated",actor.user_id,{"work_order_id":work_order_id,"quantity":quantity})
        return {"allocation_id":aid,"duplicate":False,"resource_id":resource_id,"quantity":quantity}
    def audit_events(self,token,entity_type,entity_id): self.auth.require(token,"read"); return rows(self.db,"SELECT * FROM audit_events WHERE entity_type=? AND entity_id=? ORDER BY event_id",(entity_type,entity_id))

    # ---- 调度基础数据：片区保有量、道路时间、资源兼容性 ----
    def set_district_reserve(self,token,kind,district,minimum):
        actor=self.auth.require(token,"admin")
        if not kind.strip() or not district.strip() or minimum<0:raise ValueError("reserve fields are invalid")
        with transaction(self.db):
            self.db.execute("INSERT INTO district_reserves VALUES(?,?,?,?) ON CONFLICT(kind,district) DO UPDATE SET minimum=excluded.minimum,updated_at=excluded.updated_at",(kind,district,minimum,utcnow()))
            audit(self.db,"district_reserve",f"{kind}|{district}","configured",actor.user_id,{"minimum":minimum})
        return {"kind":kind,"district":district,"minimum":minimum}

    def set_travel_time(self,token,from_district,to_district,minutes):
        actor=self.auth.require(token,"admin")
        if not from_district.strip() or not to_district.strip() or minutes<0:raise ValueError("travel time fields are invalid")
        with transaction(self.db):
            self.db.execute("INSERT INTO road_travel_times VALUES(?,?,?,?) ON CONFLICT(from_district,to_district) DO UPDATE SET minutes=excluded.minutes,updated_at=excluded.updated_at",(from_district,to_district,minutes,utcnow()))
            audit(self.db,"road_travel_time",f"{from_district}>{to_district}","configured",actor.user_id,{"minutes":minutes})
        return {"from_district":from_district,"to_district":to_district,"minutes":minutes}

    def set_compatibility(self,token,resource_kind,demand_kind,compatible):
        actor=self.auth.require(token,"admin")
        if not resource_kind.strip() or not demand_kind.strip():raise ValueError("compatibility fields are invalid")
        with transaction(self.db):
            if compatible:
                self.db.execute("INSERT OR IGNORE INTO dispatch_compatibility VALUES(?,?)",(resource_kind,demand_kind))
            else:
                self.db.execute("DELETE FROM dispatch_compatibility WHERE resource_kind=? AND demand_kind=?",(resource_kind,demand_kind))
            audit(self.db,"compatibility",f"{resource_kind}>{demand_kind}","configured",actor.user_id,{"compatible":bool(compatible)})
        return {"resource_kind":resource_kind,"demand_kind":demand_kind,"compatible":bool(compatible)}

    def _load_work_orders(self,work_order_ids):
        result={}
        for wid in work_order_ids:
            row=self.db.execute("SELECT w.*,s.district,a.severity,a.score FROM work_orders w JOIN segments s ON s.segment_id=w.segment_id LEFT JOIN alerts a ON a.alert_id=w.alert_id WHERE w.work_order_id=?",(wid,)).fetchone()
            if not row:raise KeyError(wid)
            item=dict(row); result[wid]={"district":item["district"],"severity":item["severity"] or "low","score":float(item["score"] or 0.0),"priority":item["priority"],"created_at":item["created_at"]}
        return result

    def _load_snapshot(self):
        resources=rows(self.db,"SELECT resource_id,kind,district,capacity,available FROM resources ORDER BY resource_id")
        reserve_rows=rows(self.db,"SELECT kind,district,minimum FROM district_reserves ORDER BY kind,district")
        travel_rows=rows(self.db,"SELECT from_district,to_district,minutes FROM road_travel_times ORDER BY from_district,to_district")
        compat_rows=rows(self.db,"SELECT resource_kind,demand_kind FROM dispatch_compatibility ORDER BY resource_kind,demand_kind")
        reserves={(r["kind"],r["district"]):int(r["minimum"]) for r in reserve_rows}
        travel={(r["from_district"],r["to_district"]):int(r["minutes"]) for r in travel_rows}
        compatibility={}
        for r in compat_rows: compatibility.setdefault(r["resource_kind"],set()).add(r["demand_kind"])
        return resources,reserves,travel,compatibility

    def _resource_version(self,resources,reserves,travel,compatibility):
        basis={"resources":[{"resource_id":r["resource_id"],"kind":r["kind"],"district":r["district"],"available":int(r["available"])} for r in resources],"reserves":[{"kind":k[0],"district":k[1],"minimum":v} for k,v in sorted(reserves.items())],"travel":[{"from_district":k[0],"to_district":k[1],"minutes":v} for k,v in sorted(travel.items())],"compatibility":sorted((rk,dk) for rk,dks in compatibility.items() for dk in dks)}
        return digest(basis)

    @staticmethod
    def _validate_demands(demands):
        if not isinstance(demands,list) or not demands:raise ValueError("demands must be a non-empty list")
        normalized=[]
        seen=set()
        for d in demands:
            wid=str(d.get("work_order_id","")).strip(); kind=str(d.get("kind","")).strip(); quantity=d.get("quantity")
            if not wid or not kind:raise ValueError("demand work_order_id and kind are required")
            if isinstance(quantity,bool) or not isinstance(quantity,int) or quantity<=0:raise ValueError("demand quantity must be a positive integer")
            max_minutes=d.get("max_minutes")
            if max_minutes is not None and (isinstance(max_minutes,bool) or not isinstance(max_minutes,int) or max_minutes<=0):raise ValueError("max_minutes must be a positive integer")
            if (wid,kind) in seen:raise ValueError("duplicate demand for the same work order and kind")
            seen.add((wid,kind))
            item={"work_order_id":wid,"kind":kind,"quantity":quantity}
            if max_minutes is not None:item["max_minutes"]=max_minutes
            normalized.append(item)
        return normalized

    def create_dispatch_plan(self,token,demands):
        """预演跨片区调度：不扣减任何资源，只生成可复核的确定性方案。"""
        actor=self.auth.require(token,"dispatch_plan")
        demands=self._validate_demands(demands)
        work_order_ids=sorted({d["work_order_id"] for d in demands})
        work_orders=self._load_work_orders(work_order_ids)
        resources,reserves,travel,compatibility=self._load_snapshot()
        version=self._resource_version(resources,reserves,travel,compatibility)
        result=build_plan(demands,resources,reserves,travel,compatibility,work_orders)
        plan_id="plan-"+uuid.uuid4().hex[:16]
        result["allocations"]=render_lines(plan_id,result["allocations"])
        record={"plan_id":plan_id,"state":"preview","resource_version":version,"demands":demands,"allocations":result["allocations"],"unmet":result["unmet"],"cross_district":result["cross_district"],"revision":1}
        with transaction(self.db):
            self.db.execute("INSERT INTO dispatch_plans(plan_id,state,resource_version,snapshot_json,demands_json,compatibility_json,result_json,overrides_json,cross_district,revision,created_by,created_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",(plan_id,"preview",version,canonical_json({"resources":resources}),canonical_json(demands),canonical_json({k:sorted(v) for k,v in compatibility.items()}),canonical_json(record),canonical_json({}),1 if result["cross_district"] else 0,1,actor.user_id,utcnow()))
            audit(self.db,"dispatch_plan",plan_id,"previewed",actor.user_id,{"resource_version":version,"cross_district":result["cross_district"],"work_orders":work_order_ids})
        return record

    def adjust_dispatch_plan(self,token,plan_id,overrides,reason):
        """记录人工调整理由并重算方案；原版本保留在修订历史中。"""
        actor=self.auth.require(token,"dispatch_plan")
        if not reason.strip():raise ValueError("adjustment reason is required")
        if not isinstance(overrides,dict):raise ValueError("overrides must be a mapping of resource_id: {work_order_id: quantity}")
        row=self.db.execute("SELECT * FROM dispatch_plans WHERE plan_id=?",(plan_id,)).fetchone()
        if not row:raise KeyError(plan_id)
        if row["state"]!="preview":raise DispatchConflict("只有预演中的方案可以调整")
        demands=json.loads(row["demands_json"])
        parsed={}
        for resource_id,mapping in overrides.items():
            if not isinstance(mapping,dict):raise ValueError("overrides values must be mappings")
            for work_order_id,quantity in mapping.items():
                if isinstance(quantity,bool) or not isinstance(quantity,int) or quantity<0:raise ValueError("override quantity must be a non-negative integer")
                parsed[(str(resource_id),str(work_order_id))]=quantity
        work_orders=self._load_work_orders(sorted({d["work_order_id"] for d in demands}))
        resources,reserves,travel,compatibility=self._load_snapshot()
        version=self._resource_version(resources,reserves,travel,compatibility)
        result=build_plan(demands,resources,reserves,travel,compatibility,work_orders,parsed)
        next_revision=int(row["revision"])+1
        result["allocations"]=render_lines(plan_id,result["allocations"])
        record={"plan_id":plan_id,"state":"preview","resource_version":version,"demands":demands,"allocations":result["allocations"],"unmet":result["unmet"],"cross_district":result["cross_district"],"revision":next_revision}
        overrides_json=canonical_json([{"resource_id":k[0],"work_order_id":k[1],"quantity":v} for k,v in sorted(parsed.items())])
        with transaction(self.db):
            self.db.execute("INSERT INTO dispatch_plan_revisions(plan_id,revision,resource_version,result_json,overrides_json,reason,created_by,created_at) VALUES(?,?,?,?,?,?,?,?)",(plan_id,row["revision"],row["resource_version"],row["result_json"],row["overrides_json"],reason,actor.user_id,utcnow()))
            self.db.execute("UPDATE dispatch_plans SET resource_version=?,snapshot_json=?,compatibility_json=?,result_json=?,overrides_json=?,cross_district=?,revision=? WHERE plan_id=?",(version,canonical_json({"resources":resources}),canonical_json({k:sorted(v) for k,v in compatibility.items()}),canonical_json(record),overrides_json,1 if result["cross_district"] else 0,next_revision,plan_id))
            audit(self.db,"dispatch_plan",plan_id,"adjusted",actor.user_id,{"revision":next_revision,"reason":reason,"resource_version":version})
        return record

    def dispatch_plan(self,token,plan_id):
        self.auth.require(token,"read")
        row=self.db.execute("SELECT * FROM dispatch_plans WHERE plan_id=?",(plan_id,)).fetchone()
        if not row:raise KeyError(plan_id)
        record=json.loads(row["result_json"])
        record.update({"state":row["state"],"failure_reason":row["failure_reason"],"created_by":row["created_by"],"created_at":row["created_at"],"confirmed_by":row["confirmed_by"],"confirmed_at":row["confirmed_at"],"confirm_resource_version":row["confirm_resource_version"]})
        record["revisions"]=rows(self.db,"SELECT revision,resource_version,reason,created_by,created_at FROM dispatch_plan_revisions WHERE plan_id=? ORDER BY revision",(plan_id,))
        record["final_allocations"]=rows(self.db,"SELECT * FROM dispatch_allocations WHERE plan_id=? ORDER BY work_order_id,from_district,resource_id",(plan_id,))
        return record

    def confirm_dispatch_plan(self,token,plan_id):
        """确认方案：核对资源版本，整事务扣减；重复确认返回同一方案。"""
        row=self.db.execute("SELECT * FROM dispatch_plans WHERE plan_id=?",(plan_id,)).fetchone()
        if not row:raise KeyError(plan_id)
        record=json.loads(row["result_json"])
        # 跨片区确认需要市级调度员权限；普通操作员不可越权。
        actor=self.auth.require(token,"dispatch_confirm" if record["cross_district"] else "dispatch_plan")
        if row["state"]=="confirmed":
            record.update({"state":"confirmed","replayed":True,"confirmed_by":row["confirmed_by"],"confirmed_at":row["confirmed_at"],"confirm_resource_version":row["confirm_resource_version"]})
            return record
        if row["state"]=="failed":raise DispatchConflict(f"方案已失败：{row['failure_reason']}，请重新预演")
        now=utcnow(); outcome={}
        # 全部判定和扣减在同一写事务内完成，并以事务内读到的方案状态为准，
        # 避免并发重复确认；版本不符时只提交 failed 标记，绝不留下部分扣减。
        with transaction(self.db):
            live_row=self.db.execute("SELECT state FROM dispatch_plans WHERE plan_id=?",(plan_id,)).fetchone()
            if live_row is None:
                raise KeyError(plan_id)
            if live_row["state"]=="confirmed":
                outcome["replayed"]=True
            elif live_row["state"]=="failed":
                fresh=self.db.execute("SELECT failure_reason FROM dispatch_plans WHERE plan_id=?",(plan_id,)).fetchone()
                raise DispatchConflict(f"方案已失败：{fresh['failure_reason']}，请重新预演")
            else:
                resources,reserves,travel,compatibility=self._load_snapshot()
                live_version=self._resource_version(reserves=reserves,resources=resources,travel=travel,compatibility=compatibility)
                reason=None
                if live_version!=row["resource_version"]:
                    reason=f"资源版本已变化（预演依据 {row['resource_version'][:12]}，当前 {live_version[:12]}），确认被拒绝"
                else:
                    totals={}
                    for line in record["allocations"]:
                        totals[line["resource_id"]]=totals.get(line["resource_id"],0)+line["quantity"]
                    available={r["resource_id"]:int(r["available"]) for r in resources}
                    missing=next((rid for rid in totals if rid not in available),None)
                    short=None if missing is not None else next((rid for rid,q in totals.items() if available[rid]<q),None)
                    if missing is not None:
                        reason=f"资源 {missing} 已不存在，确认被拒绝"
                    elif short is not None:
                        reason=f"资源 {short} 可用量不足（需 {totals[short]}，余 {available[short]}），确认被拒绝"
                if reason:
                    self.db.execute("UPDATE dispatch_plans SET state='failed',failure_reason=? WHERE plan_id=?",(reason,plan_id))
                    audit(self.db,"dispatch_plan",plan_id,"confirm-failed",actor.user_id,{"reason":reason})
                    outcome["conflict"]=reason
                else:
                    for line in record["allocations"]:
                        self.db.execute("UPDATE resources SET available=available-? WHERE resource_id=?",(line["quantity"],line["resource_id"]))
                        self.db.execute("INSERT INTO dispatch_allocations VALUES(?,?,?,?,?,?,?,?,?,?,?)",(line["allocation_id"],plan_id,line["resource_id"],line["work_order_id"],line["kind"],line["from_district"],line["to_district"],line["quantity"],line["travel_minutes"],1 if line["cross_district"] else 0,now))
                        audit(self.db,"resource",line["resource_id"],"dispatched",actor.user_id,{"plan_id":plan_id,"work_order_id":line["work_order_id"],"quantity":line["quantity"],"cross_district":line["cross_district"]})
                    self.db.execute("UPDATE dispatch_plans SET state='confirmed',confirmed_by=?,confirmed_at=?,confirm_resource_version=? WHERE plan_id=?",(actor.user_id,now,live_version,plan_id))
                    audit(self.db,"dispatch_plan",plan_id,"confirmed",actor.user_id,{"resource_version":live_version,"allocations":len(record["allocations"]),"unmet":len(record["unmet"])})
                    outcome.update({"replayed":False,"confirmed_at":now,"confirm_resource_version":live_version,"confirmed_by":actor.user_id})
        if "conflict" in outcome:raise DispatchConflict(outcome["conflict"])
        if outcome.get("replayed"):
            confirmed_row=self.db.execute("SELECT confirmed_by,confirmed_at,confirm_resource_version FROM dispatch_plans WHERE plan_id=?",(plan_id,)).fetchone()
            record.update({"state":"confirmed","replayed":True,"confirmed_by":confirmed_row["confirmed_by"],"confirmed_at":confirmed_row["confirmed_at"],"confirm_resource_version":confirmed_row["confirm_resource_version"]})
        else:
            record.update({"state":"confirmed","replayed":False,"confirmed_by":outcome["confirmed_by"],"confirmed_at":outcome["confirmed_at"],"confirm_resource_version":outcome["confirm_resource_version"]})
        return record
