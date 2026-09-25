"""协调管网监测、告警、工单和应急资源分配的应用服务。"""
from __future__ import annotations
import hashlib,json,sqlite3,uuid
from .auth import Auth
from .dispatch import canonical_json, digest, plan_dispatch
from .errors import PlanStateError, VersionConflict
from .models import Reading,Segment,as_dict,utcnow
from .risk import leak_probability,score_reading
from .storage import audit,connect,rows,transaction
class NetworkService:
    def __init__(self,database=":memory:"): self.db=connect(database); self.auth=Auth(self.db)
    def bootstrap(self):
        for uid,pwd,role in (("admin","network-admin","admin"),("operator","network-operator","operator"),("dispatcher","city-dispatch","dispatcher")):
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

    # ---- 跨片区调度：保有量、兼容性、道路与需求配置 ----
    def set_district_reserve(self,token,district,resource_kind,minimum):
        actor=self.auth.require(token,"admin")
        if not district.strip() or not resource_kind.strip() or minimum<0: raise ValueError("district reserve fields are invalid")
        now=utcnow()
        with transaction(self.db):
            self.db.execute("INSERT INTO district_reserves VALUES(?,?,?,?) ON CONFLICT(district,resource_kind) DO UPDATE SET minimum=excluded.minimum,updated_at=excluded.updated_at",(district,resource_kind,minimum,now))
            audit(self.db,"district_reserve",f"{district}:{resource_kind}","configured",actor.user_id,{"minimum":minimum})
        return {"district":district,"resource_kind":resource_kind,"minimum":minimum}

    def set_compatibility(self,token,need_kind,resource_kinds):
        actor=self.auth.require(token,"admin")
        kinds=sorted({k.strip() for k in resource_kinds if k.strip()})
        if not need_kind.strip() or not kinds: raise ValueError("compatibility fields are invalid")
        with transaction(self.db):
            self.db.execute("DELETE FROM resource_compatibility WHERE need_kind=?",(need_kind,))
            self.db.executemany("INSERT INTO resource_compatibility VALUES(?,?)",[(need_kind,k) for k in kinds])
            audit(self.db,"compatibility",need_kind,"configured",actor.user_id,{"resource_kinds":kinds})
        return {"need_kind":need_kind,"resource_kinds":kinds}

    def set_road_access(self,token,resource_id,work_order_id,travel_minutes,reachable=True):
        actor=self.auth.require(token,"work_order")
        if travel_minutes<0: raise ValueError("travel minutes cannot be negative")
        if not self.db.execute("SELECT 1 FROM resources WHERE resource_id=?",(resource_id,)).fetchone(): raise KeyError(resource_id)
        if not self.db.execute("SELECT 1 FROM work_orders WHERE work_order_id=?",(work_order_id,)).fetchone(): raise KeyError(work_order_id)
        now=utcnow(); flag=1 if reachable else 0
        with transaction(self.db):
            self.db.execute("INSERT INTO road_access VALUES(?,?,?,?,?) ON CONFLICT(resource_id,work_order_id) DO UPDATE SET travel_minutes=excluded.travel_minutes,reachable=excluded.reachable,updated_at=excluded.updated_at",(resource_id,work_order_id,travel_minutes,flag,now))
            audit(self.db,"road_access",f"{resource_id}:{work_order_id}","configured",actor.user_id,{"travel_minutes":travel_minutes,"reachable":bool(flag)})
        return {"resource_id":resource_id,"work_order_id":work_order_id,"travel_minutes":travel_minutes,"reachable":bool(flag)}

    def declare_demand(self,token,work_order_id,need_kind,quantity):
        actor=self.auth.require(token,"work_order")
        if not need_kind.strip() or quantity<=0: raise ValueError("demand fields are invalid")
        if not self.db.execute("SELECT 1 FROM work_orders WHERE work_order_id=?",(work_order_id,)).fetchone(): raise KeyError(work_order_id)
        now=utcnow()
        with transaction(self.db):
            self.db.execute("INSERT INTO dispatch_demands VALUES(?,?,?,?) ON CONFLICT(work_order_id) DO UPDATE SET need_kind=excluded.need_kind,quantity=excluded.quantity,created_at=excluded.created_at",(work_order_id,need_kind,quantity,now))
            audit(self.db,"dispatch_demand",work_order_id,"declared",actor.user_id,{"need_kind":need_kind,"quantity":quantity})
        return {"work_order_id":work_order_id,"need_kind":need_kind,"quantity":quantity}

    # ---- 预演 / 调整 / 确认 ----
    def _snapshot(self,work_order_ids):
        ids=sorted(set(work_order_ids))
        if not ids: raise ValueError("work_order_ids are required")
        placeholders=",".join("?"*len(ids))
        order_rows=self.db.execute(
            "SELECT d.work_order_id,d.need_kind,d.quantity AS demand,w.priority,s.district,"
            "COALESCE(a.score,0.0) AS risk_score "
            "FROM dispatch_demands d JOIN work_orders w ON w.work_order_id=d.work_order_id "
            "JOIN segments s ON s.segment_id=w.segment_id "
            "LEFT JOIN alerts a ON a.alert_id=w.alert_id "
            f"WHERE d.work_order_id IN ({placeholders}) ORDER BY d.work_order_id",ids).fetchall()
        found={r["work_order_id"] for r in order_rows}; missing=[i for i in ids if i not in found]
        if missing: raise KeyError(missing[0])
        resources=rows(self.db,"SELECT resource_id,kind,district,available FROM resources ORDER BY resource_id")
        reserves=rows(self.db,"SELECT district,resource_kind,minimum FROM district_reserves ORDER BY district,resource_kind")
        reserve_map={(r["district"],r["resource_kind"]):r["minimum"] for r in reserves}
        for r in resources: r["reserve"]=reserve_map.get((r["district"],r["kind"]),0)
        access=rows(self.db,f"SELECT resource_id,work_order_id,travel_minutes,reachable FROM road_access WHERE work_order_id IN ({placeholders}) ORDER BY resource_id",ids)
        for a in access: a["reachable"]=bool(a["reachable"])
        compatibility=[]
        for n in rows(self.db,"SELECT DISTINCT need_kind FROM resource_compatibility ORDER BY need_kind"):
            kinds=[r["resource_kind"] for r in rows(self.db,"SELECT resource_kind FROM resource_compatibility WHERE need_kind=? ORDER BY resource_kind",(n["need_kind"],))]
            compatibility.append({"need_kind":n["need_kind"],"resource_kinds":kinds})
        existing=rows(self.db,f"SELECT resource_id,work_order_id FROM allocations WHERE work_order_id IN ({placeholders}) ORDER BY resource_id",ids)
        orders=[]
        for o in order_rows:
            home=min([a["travel_minutes"] for a in access if a["work_order_id"]==o["work_order_id"] and a["reachable"] and any(r["resource_id"]==a["resource_id"] and r["district"]==o["district"] for r in resources)],default=0.0)
            orders.append({"work_order_id":o["work_order_id"],"district":o["district"],"need_kind":o["need_kind"],"demand":o["demand"],"priority":o["priority"],"risk_score":o["risk_score"],"home_travel_minutes":home})
        snapshot={"work_orders":orders,"resources":resources,"access":access,"compatibility":compatibility,"existing_pairs":existing}
        version=digest(snapshot)
        snapshot["resource_version"]=version
        return snapshot

    def preview_dispatch(self,token,work_order_ids,idempotency_key=None):
        actor=self.auth.require(token,"dispatch_plan")
        with transaction(self.db):
            snapshot=self._snapshot(work_order_ids)
            result=plan_dispatch(snapshot)
            work_ids=[o["work_order_id"] for o in snapshot["work_orders"]]
            plan_content={"work_order_ids":work_ids,"assignments":result["assignments"],"unmet":result["unmet"],"cross_district":result["cross_district"],"manual":False}
            content_sha=digest(plan_content); version=snapshot["resource_version"]
            same=self.db.execute("SELECT plan_id FROM dispatch_plans WHERE plan_sha256=?",(content_sha,)).fetchone()
            if same:
                row=self.db.execute("SELECT * FROM dispatch_plans WHERE plan_id=?",(same["plan_id"],)).fetchone()
                return self._plan_view(dict(row),replayed=True)
            if idempotency_key:
                held=self.db.execute("SELECT plan_id,plan_sha256 FROM dispatch_plans WHERE idempotency_key=?",(idempotency_key,)).fetchone()
                if held:
                    if held["plan_sha256"]!=content_sha: raise ValueError("idempotency key maps to a different plan")
                    row=self.db.execute("SELECT * FROM dispatch_plans WHERE plan_id=?",(held["plan_id"],)).fetchone()
                    return self._plan_view(dict(row),replayed=True)
            plan_id="plan-"+content_sha[:16]; now=utcnow()
            self.db.execute("INSERT INTO dispatch_plans(plan_id,idempotency_key,plan_sha256,resource_version_sha256,status,snapshot_json,plan_json,created_by,created_at) VALUES(?,?,?,?,?,?,?,?,?)",(plan_id,idempotency_key,content_sha,version,"previewed",json.dumps(snapshot,ensure_ascii=False,sort_keys=True),json.dumps(plan_content,ensure_ascii=False,sort_keys=True),actor.user_id,now))
            audit(self.db,"dispatch_plan",plan_id,"previewed",actor.user_id,{"work_orders":sorted(set(work_order_ids)),"resource_version":version,"cross_district":result["cross_district"]})
            row=self.db.execute("SELECT * FROM dispatch_plans WHERE plan_id=?",(plan_id,)).fetchone()
        return self._plan_view(dict(row),replayed=False)

    def adjust_plan(self,token,plan_id,overrides,reason):
        actor=self.auth.require(token,"dispatch_plan")
        if not reason or not reason.strip(): raise ValueError("adjustment reason is required")
        with transaction(self.db):
            row=self.db.execute("SELECT * FROM dispatch_plans WHERE plan_id=?",(plan_id,)).fetchone()
            if not row: raise KeyError(plan_id)
            if row["status"]!="previewed": raise PlanStateError("only previewed plans can be adjusted")
            snapshot=json.loads(row["snapshot_json"]); resources={r["resource_id"]:r for r in snapshot["resources"]}
            access={(a["resource_id"],a["work_order_id"]):a for a in snapshot["access"]}
            orders_by_id={o["work_order_id"]:o for o in snapshot["work_orders"]}
            allowed={rule["need_kind"]:set(rule["resource_kinds"]) for rule in snapshot["compatibility"]}
            demand_total={o["work_order_id"]:int(o["demand"]) for o in snapshot["work_orders"]}
            existing_pairs={(p["resource_id"],p["work_order_id"]) for p in snapshot["existing_pairs"]}
            assignments=[]; seen=set(); per_order={wid:0 for wid in demand_total}; per_resource={rid:0 for rid in resources}
            for item in overrides.get("assignments",[]):
                wid,rid,qty=item["work_order_id"],item["resource_id"],int(item["quantity"])
                if wid not in demand_total: raise ValueError("adjustment references unknown work order")
                if rid not in resources: raise ValueError("adjustment references unknown resource")
                if (rid,wid) in seen: raise ValueError("duplicate assignment in adjustment")
                seen.add((rid,wid))
                if qty<0: raise ValueError("quantity cannot be negative")
                if qty==0: continue
                need=orders_by_id[wid]["need_kind"]
                if resources[rid]["kind"] not in allowed.get(need,set()): raise ValueError(f"{rid} is incompatible with {need}")
                route=access.get((rid,wid))
                if route is None or not route["reachable"]: raise ValueError(f"{rid} cannot reach {wid}")
                if (rid,wid) in existing_pairs: raise ValueError(f"{rid} already allocated to {wid}")
                per_order[wid]+=qty
                if per_order[wid]>demand_total[wid]: raise ValueError("adjustment exceeds declared demand")
                per_resource[rid]+=qty
                if per_resource[rid]>resources[rid]["available"]-resources[rid]["reserve"]: raise ValueError(f"{rid} exceeds amount exportable beyond district reserve")
                assignments.append({"work_order_id":wid,"resource_id":rid,"quantity":qty,"from_district":resources[rid]["district"],"to_district":orders_by_id[wid]["district"],"travel_minutes":float(route["travel_minutes"]),"cross_district":resources[rid]["district"]!=orders_by_id[wid]["district"]})
            assignments.sort(key=lambda a:(a["work_order_id"],a["resource_id"]))
            unmet=[]
            for wid,total in sorted(per_order.items()):
                if total<demand_total[wid]:
                    unmet.append({"work_order_id":wid,"need_kind":orders_by_id[wid]["need_kind"],"requested":demand_total[wid],"unmet":demand_total[wid]-total,"reasons":["manually-adjusted"],"reserve_held":0})
            plan_content={"work_order_ids":sorted(demand_total),"assignments":assignments,"unmet":unmet,"cross_district":any(a["cross_district"] for a in assignments),"manual":True}
            content_sha=digest(plan_content); now=utcnow()
            self.db.execute("UPDATE dispatch_plans SET plan_json=?,plan_sha256=?,adjustment_reason=?,adjustment_by=?,adjustment_at=? WHERE plan_id=?",(json.dumps(plan_content,ensure_ascii=False,sort_keys=True),content_sha,reason.strip(),actor.user_id,now,plan_id))
            audit(self.db,"dispatch_plan",plan_id,"adjusted",actor.user_id,{"reason":reason.strip(),"assignments":len(assignments)})
            row=self.db.execute("SELECT * FROM dispatch_plans WHERE plan_id=?",(plan_id,)).fetchone()
        return self._plan_view(dict(row),replayed=False)

    def confirm_plan(self,token,plan_id):
        actor=self.auth.require(token,"dispatch_plan")
        try:
            with transaction(self.db):
                row=self.db.execute("SELECT * FROM dispatch_plans WHERE plan_id=?",(plan_id,)).fetchone()
                if not row: raise KeyError(plan_id)
                plan=json.loads(row["plan_json"]); snapshot=json.loads(row["snapshot_json"])
                if row["status"]=="confirmed":
                    return self._plan_view(dict(row),replayed=True)
                if row["status"]!="previewed": raise PlanStateError(f"plan is {row['status']}")
                if plan["cross_district"] and "dispatch_confirm" not in self._permissions(actor):
                    raise PermissionError("cross-district dispatch requires city dispatcher")
                # 在同一写事务内重新抓取快照并核对资源版本，不一致则整体回滚。
                current=self._snapshot([o["work_order_id"] for o in snapshot["work_orders"]])
                if current["resource_version"]!=row["resource_version_sha256"]:
                    raise VersionConflict("resource version changed since preview; re-plan before confirming")
                now=utcnow(); allocation_ids=[]
                for a in plan["assignments"]:
                    aid="alloc-"+uuid.uuid4().hex[:16]
                    self.db.execute("INSERT INTO allocations VALUES(?,?,?,?,?)",(aid,a["resource_id"],a["work_order_id"],a["quantity"],now))
                    cur=self.db.execute("UPDATE resources SET available=available-? WHERE resource_id=? AND available>=?",(a["quantity"],a["resource_id"],a["quantity"]))
                    if cur.rowcount!=1: raise sqlite3.IntegrityError(f"resource {a['resource_id']} cannot cover confirmed quantity")
                    allocation_ids.append(aid)
                self.db.execute("UPDATE dispatch_plans SET status='confirmed',confirmed_by=?,confirmed_at=?,applied_version_sha256=?,allocation_ids_json=? WHERE plan_id=?",(actor.user_id,now,current["resource_version"],json.dumps(allocation_ids,ensure_ascii=False),plan_id))
                audit(self.db,"dispatch_plan",plan_id,"confirmed",actor.user_id,{"resource_version":current["resource_version"],"allocations":allocation_ids,"cross_district":plan["cross_district"]})
                final=self.db.execute("SELECT * FROM dispatch_plans WHERE plan_id=?",(plan_id,)).fetchone()
                view=self._plan_view(dict(final),replayed=False)
        except Exception as exc:
            try:
                with transaction(self.db):
                    audit(self.db,"dispatch_plan",plan_id,"confirm_failed",actor.user_id,{"error":type(exc).__name__,"detail":str(exc)})
            except Exception: pass
            raise
        return view

    def _permissions(self,principal):
        from .auth import PERMISSIONS
        return PERMISSIONS.get(principal.role,set())

    def plan(self,token,plan_id):
        self.auth.require(token,"read")
        row=self.db.execute("SELECT * FROM dispatch_plans WHERE plan_id=?",(plan_id,)).fetchone()
        if not row: raise KeyError(plan_id)
        return self._plan_view(dict(row))

    def list_plans(self,token,status=None):
        self.auth.require(token,"read")
        if status:
            found=rows(self.db,"SELECT plan_id,status,created_at,confirmed_at FROM dispatch_plans WHERE status=? ORDER BY created_at",(status,))
        else:
            found=rows(self.db,"SELECT plan_id,status,created_at,confirmed_at FROM dispatch_plans ORDER BY created_at")
        return {"plans":found}

    @staticmethod
    def _plan_view(row,replayed=None):
        plan=json.loads(row["plan_json"]); snapshot=json.loads(row["snapshot_json"])
        view={"plan_id":row["plan_id"],"status":row["status"],"resource_version":row["resource_version_sha256"],"assignments":plan["assignments"],"unmet":plan["unmet"],"cross_district":plan["cross_district"],"manual":plan.get("manual",False),"work_orders":[o["work_order_id"] for o in snapshot["work_orders"]],"created_by":row["created_by"],"created_at":row["created_at"],"confirmed_by":row["confirmed_by"],"confirmed_at":row["confirmed_at"],"adjustment_reason":row["adjustment_reason"],"applied_resource_version":row["applied_version_sha256"],"allocations":json.loads(row["allocation_ids_json"]) if row["allocation_ids_json"] else []}
        if replayed is not None: view["replayed"]=replayed
        return view
