"""暴雨跨片区调度的确定性预演算法。

输入全部来自调用方在单个事务里抓取的快照（工单、资源、可达时间、片区
保有量约束、资源兼容矩阵），模块本身不接触数据库或时钟，因此同一份输入
永远得到同一份方案，便于预演、复核与重启后重放。
"""
from __future__ import annotations

import hashlib
import json
from typing import Any, Mapping


def canonical_json(value: object) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def digest(value: object) -> str:
    return hashlib.sha256(canonical_json(value).encode("utf-8")).hexdigest()


def plan_dispatch(snapshot: Mapping[str, Any]) -> dict[str, Any]:
    """根据快照生成确定性调度方案。

    工单顺序：风险分降序、优先级升序（1 最高）、本片区最近到达时间升序、
    工单编号升序。工单内候选资源顺序：本片区优先，其次到达时间升序、
    资源编号升序。资源对本片区始终保留最低保有量，跨片区支援只能动用
    超出保有量的部分，且资源种类必须出现在兼容矩阵中，道路必须可达。
    """
    orders = sorted(
        snapshot["work_orders"],
        key=lambda o: (
            -float(o["risk_score"]),
            int(o["priority"]),
            float(o.get("home_travel_minutes", 0.0)),
            o["work_order_id"],
        ),
    )
    resources = {r["resource_id"]: dict(r) for r in snapshot["resources"]}
    # 可外拨余量 = 现有可用量 - 本片区最低保有量，且不为负。
    for resource in resources.values():
        reserve = int(resource.get("reserve", 0))
        resource["remaining"] = max(0, int(resource["available"]) - reserve)
        resource["reserve"] = reserve
    access = {
        (a["resource_id"], a["work_order_id"]): a
        for a in snapshot.get("access", [])
    }
    excluded_pairs = {
        (p["resource_id"], p["work_order_id"])
        for p in snapshot.get("existing_pairs", [])
    }
    compatible_kinds: dict[str, set[str]] = {}
    for rule in snapshot.get("compatibility", []):
        compatible_kinds.setdefault(rule["need_kind"], set()).update(rule["resource_kinds"])

    assignments: list[dict[str, Any]] = []
    unmet: list[dict[str, Any]] = []
    for order in orders:
        need_kind = order["need_kind"]
        demand_total = int(order["demand"])
        if demand_total <= 0:
            continue
        kinds = compatible_kinds.get(need_kind, set())
        if not kinds or not any(r["kind"] in kinds for r in resources.values()):
            unmet.append({
                "work_order_id": order["work_order_id"],
                "need_kind": need_kind,
                "requested": demand_total,
                "unmet": demand_total,
                "reasons": ["no-compatible-resource-kind"],
                "reserve_held": 0,
            })
            continue
        demand_left = demand_total
        candidates: list[tuple[int, float, str]] = []
        blocked: dict[str, list[str]] = {}
        for resource_id, resource in resources.items():
            if resource["kind"] not in kinds:
                continue
            reasons: list[str] = []
            if (resource_id, order["work_order_id"]) in excluded_pairs:
                reasons.append("already-allocated")
            route = access.get((resource_id, order["work_order_id"]))
            if route is None or not bool(route.get("reachable", True)):
                reasons.append("road-unreachable")
            if resource["remaining"] <= 0:
                reasons.append(
                    "district-reserve-protected"
                    if int(resource["available"]) > 0
                    else "no-availability"
                )
            if reasons:
                blocked[resource_id] = reasons
                continue
            travel = float(route["travel_minutes"])
            same_district = resource["district"] == order["district"]
            candidates.append((0 if same_district else 1, travel, resource_id))
        candidates.sort()
        for _, _, resource_id in candidates:
            if demand_left == 0:
                break
            resource = resources[resource_id]
            take = min(demand_left, resource["remaining"])
            if take <= 0:
                continue
            route = access[(resource_id, order["work_order_id"])]
            cross_district = resource["district"] != order["district"]
            assignments.append({
                "work_order_id": order["work_order_id"],
                "resource_id": resource_id,
                "quantity": take,
                "from_district": resource["district"],
                "to_district": order["district"],
                "travel_minutes": float(route["travel_minutes"]),
                "cross_district": cross_district,
            })
            resource["remaining"] -= take
            demand_left -= take
        if demand_left > 0:
            reason_set = {reason for rs in blocked.values() for reason in rs}
            reserve_held = sum(
                min(int(r["available"]), int(r["reserve"]))
                for r in resources.values()
                if r["kind"] in kinds
            )
            if reserve_held > 0:
                reason_set.add("district-reserve-protected")
            if not reason_set:
                reason_set.add("insufficient-capacity")
            unmet.append({
                "work_order_id": order["work_order_id"],
                "need_kind": need_kind,
                "requested": demand_total,
                "unmet": demand_left,
                "reasons": sorted(reason_set),
                "reserve_held": reserve_held,
            })

    assignments.sort(key=lambda a: (a["work_order_id"], a["resource_id"]))
    unmet.sort(key=lambda u: u["work_order_id"])
    return {
        "assignments": assignments,
        "unmet": unmet,
        "work_order_count": len(orders),
        "cross_district": any(a["cross_district"] for a in assignments),
    }
