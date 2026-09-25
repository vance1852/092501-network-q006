"""SQLite 结构、事务和审计事件辅助函数。"""
from __future__ import annotations
import json, sqlite3
from contextlib import contextmanager
from datetime import datetime, timezone
from typing import Iterator

SCHEMA = """
CREATE TABLE IF NOT EXISTS users(user_id TEXT PRIMARY KEY,role TEXT NOT NULL,salt TEXT NOT NULL,password_hash TEXT NOT NULL,active INTEGER NOT NULL DEFAULT 1,created_at TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS sessions(token TEXT PRIMARY KEY,user_id TEXT NOT NULL,expires_at TEXT NOT NULL,active INTEGER NOT NULL DEFAULT 1);
CREATE TABLE IF NOT EXISTS segments(segment_id TEXT PRIMARY KEY,district TEXT NOT NULL,network_type TEXT NOT NULL,length_m REAL NOT NULL,criticality INTEGER NOT NULL,status TEXT NOT NULL,created_at TEXT NOT NULL,updated_at TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS readings(reading_id TEXT PRIMARY KEY,segment_id TEXT NOT NULL REFERENCES segments(segment_id),sensor_id TEXT NOT NULL,pressure_kpa REAL NOT NULL,flow_lps REAL NOT NULL,acoustic_db REAL NOT NULL,observed_at TEXT NOT NULL,UNIQUE(segment_id,sensor_id,observed_at));
CREATE TABLE IF NOT EXISTS alerts(alert_id TEXT PRIMARY KEY,segment_id TEXT NOT NULL REFERENCES segments(segment_id),fingerprint TEXT NOT NULL UNIQUE,severity TEXT NOT NULL,score REAL NOT NULL,status TEXT NOT NULL,created_at TEXT NOT NULL,resolved_at TEXT);
CREATE TABLE IF NOT EXISTS work_orders(work_order_id TEXT PRIMARY KEY,segment_id TEXT NOT NULL,alert_id TEXT NOT NULL,assignee TEXT NOT NULL,status TEXT NOT NULL,priority INTEGER NOT NULL,created_at TEXT NOT NULL,updated_at TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS resources(resource_id TEXT PRIMARY KEY,kind TEXT NOT NULL,district TEXT NOT NULL,capacity INTEGER NOT NULL,available INTEGER NOT NULL);
CREATE TABLE IF NOT EXISTS allocations(allocation_id TEXT PRIMARY KEY,resource_id TEXT NOT NULL,work_order_id TEXT NOT NULL,quantity INTEGER NOT NULL,created_at TEXT NOT NULL,UNIQUE(resource_id,work_order_id));
CREATE TABLE IF NOT EXISTS district_reserves(kind TEXT NOT NULL,district TEXT NOT NULL,minimum INTEGER NOT NULL,updated_at TEXT NOT NULL,PRIMARY KEY(kind,district));
CREATE TABLE IF NOT EXISTS dispatch_compatibility(resource_kind TEXT NOT NULL,demand_kind TEXT NOT NULL,PRIMARY KEY(resource_kind,demand_kind));
CREATE TABLE IF NOT EXISTS road_travel_times(from_district TEXT NOT NULL,to_district TEXT NOT NULL,minutes INTEGER NOT NULL CHECK(minutes>=0),updated_at TEXT NOT NULL,PRIMARY KEY(from_district,to_district));
CREATE TABLE IF NOT EXISTS dispatch_plans(plan_id TEXT PRIMARY KEY,state TEXT NOT NULL CHECK(state IN ('preview','confirmed','failed')),resource_version TEXT NOT NULL,snapshot_json TEXT NOT NULL,demands_json TEXT NOT NULL,compatibility_json TEXT NOT NULL,result_json TEXT NOT NULL,overrides_json TEXT NOT NULL,cross_district INTEGER NOT NULL,revision INTEGER NOT NULL DEFAULT 1,created_by TEXT NOT NULL,created_at TEXT NOT NULL,confirmed_by TEXT,confirmed_at TEXT,confirm_resource_version TEXT,failure_reason TEXT);
CREATE TABLE IF NOT EXISTS dispatch_plan_revisions(revision_id INTEGER PRIMARY KEY AUTOINCREMENT,plan_id TEXT NOT NULL,revision INTEGER NOT NULL,resource_version TEXT NOT NULL,result_json TEXT NOT NULL,overrides_json TEXT NOT NULL,reason TEXT NOT NULL,created_by TEXT NOT NULL,created_at TEXT NOT NULL,UNIQUE(plan_id,revision));
CREATE TABLE IF NOT EXISTS dispatch_allocations(allocation_id TEXT PRIMARY KEY,plan_id TEXT NOT NULL,resource_id TEXT NOT NULL,work_order_id TEXT NOT NULL,kind TEXT NOT NULL,from_district TEXT NOT NULL,to_district TEXT NOT NULL,quantity INTEGER NOT NULL,travel_minutes INTEGER NOT NULL,cross_district INTEGER NOT NULL,created_at TEXT NOT NULL);
CREATE INDEX IF NOT EXISTS idx_dispatch_allocations_plan ON dispatch_allocations(plan_id);
CREATE TABLE IF NOT EXISTS audit_events(event_id INTEGER PRIMARY KEY AUTOINCREMENT,entity_type TEXT NOT NULL,entity_id TEXT NOT NULL,action TEXT NOT NULL,actor TEXT NOT NULL,payload TEXT NOT NULL,created_at TEXT NOT NULL);
"""
def utcnow() -> str: return datetime.now(timezone.utc).isoformat()
def connect(path: str = ":memory:") -> sqlite3.Connection:
    db=sqlite3.connect(path,timeout=10,check_same_thread=False); db.row_factory=sqlite3.Row; db.execute("PRAGMA foreign_keys=ON"); db.execute("PRAGMA journal_mode=WAL"); db.executescript(SCHEMA); db.commit(); return db
@contextmanager
def transaction(db: sqlite3.Connection) -> Iterator[sqlite3.Connection]:
    try: db.execute("BEGIN IMMEDIATE"); yield db; db.commit()
    except Exception: db.rollback(); raise
def audit(db, entity_type, entity_id, action, actor, payload):
    db.execute("INSERT INTO audit_events(entity_type,entity_id,action,actor,payload,created_at) VALUES(?,?,?,?,?,?)",(entity_type,entity_id,action,actor,json.dumps(payload,ensure_ascii=False,sort_keys=True),utcnow()))
def rows(db, query, args=()): return [dict(r) for r in db.execute(query,args).fetchall()]
