"""依赖标准库的 JSON HTTP API。"""
from __future__ import annotations
import argparse,json
from http.server import BaseHTTPRequestHandler,ThreadingHTTPServer
from .models import Reading,Segment
from .planning import DispatchConflict
from .service import NetworkService
class Handler(BaseHTTPRequestHandler):
    service=NetworkService()
    def _send(self,status,payload):
        data=json.dumps(payload,ensure_ascii=False).encode(); self.send_response(status); self.send_header("Content-Type","application/json"); self.send_header("Content-Length",str(len(data))); self.end_headers(); self.wfile.write(data)
    def _token(self):return self.headers.get("Authorization","").removeprefix("Bearer ")
    def do_GET(self):
        try:
            if self.path=="/health":return self._send(200,{"status":"ok","service":"urban-network"})
            if self.path.startswith("/dispatch-plans/"):return self._send(200,self.service.dispatch_plan(self._token(),self.path.split("/")[2]))
            if self.path.startswith("/segments/") and self.path.endswith("/risk"):return self._send(200,self.service.risk_report(self._token(),self.path.split("/")[2]))
            if self.path.startswith("/segments/"):return self._send(200,self.service.segment(self._token(),self.path.split("/",2)[2]))
            return self._send(404,{"error":"not found"})
        except PermissionError as e:return self._send(403,{"error":str(e)})
        except Exception as e:return self._send(400,{"error":str(e)})
    def do_POST(self):
        try:
            body=json.loads(self.rfile.read(int(self.headers.get("Content-Length","0"))) or b"{}")
            if self.path=="/login":return self._send(200,{"token":self.service.auth.login(body["user_id"],body["password"])})
            token=self._token()
            if self.path=="/segments":return self._send(201,self.service.register_segment(token,Segment(body["segment_id"],body["district"],body["network_type"],body["length_m"],body["criticality"])))
            if self.path.startswith("/segments/") and self.path.endswith("/readings"):
                sid=self.path.split("/")[2]; r=Reading(body["reading_id"],sid,body["sensor_id"],body["pressure_kpa"],body["flow_lps"],body["acoustic_db"],body["observed_at"]); return self._send(201,self.service.ingest_reading(token,r))
            if self.path.startswith("/segments/") and self.path.endswith("/work-orders"):
                return self._send(201,self.service.create_work_order(token,self.path.split("/")[2],body["alert_id"],body["assignee"],body.get("priority",3)))
            if self.path=="/districts/reserves":return self._send(200,self.service.set_district_reserve(token,body["kind"],body["district"],body["minimum"]))
            if self.path=="/roads/travel-times":return self._send(200,self.service.set_travel_time(token,body["from_district"],body["to_district"],body["minutes"]))
            if self.path=="/resources/compatibility":return self._send(200,self.service.set_compatibility(token,body["resource_kind"],body["demand_kind"],body["compatible"]))
            if self.path=="/dispatch-plans":return self._send(201,self.service.create_dispatch_plan(token,body["demands"]))
            if self.path.startswith("/dispatch-plans/"):
                parts=self.path.strip("/").split("/"); plan_id=parts[1]
                if parts[2]=="adjustments":return self._send(200,self.service.adjust_dispatch_plan(token,plan_id,body["overrides"],body["reason"]))
                if parts[2]=="confirm":return self._send(200,self.service.confirm_dispatch_plan(token,plan_id))
            return self._send(404,{"error":"not found"})
        except PermissionError as e:return self._send(403,{"error":str(e)})
        except DispatchConflict as e:return self._send(409,{"error":str(e)})
        except Exception as e:return self._send(400,{"error":str(e)})
def main():
    p=argparse.ArgumentParser(); p.add_argument("--database",default=":memory:"); p.add_argument("--host",default="127.0.0.1"); p.add_argument("--port",type=int,default=8080); a=p.parse_args(); Handler.service=NetworkService(a.database); Handler.service.bootstrap(); ThreadingHTTPServer((a.host,a.port),Handler).serve_forever()
if __name__=="__main__":main()
