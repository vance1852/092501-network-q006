"""跨片区应急调度的确定性预演算法。

算法只依赖传入的快照数据（资源、片区保有量、道路时间、兼容性、工单风险），
不读取时钟或随机数，因此同一份快照必然得到同一份分配，便于预演、复核和重放。
"""
from __future__ import annotations

import hashlib
import json
from typing import Any, Mapping, Sequence

SEVERITY_RANK = {"critical": 4, "high": 3, "medium": 2, "low": 1}


class DispatchConflict(Exception):
    """方案状态或资源版本与预期不一致。"""


def canonical_json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def digest(value: Any) -> str:
    return hashlib.sha256(canonical_json(value).encode("utf-8")).hexdigest()


def allocation_id(plan_id: str, resource_id: str, work_order_id: str) -> str:
    raw = f"{plan_id}|{resource_id}|{work_order_id}"
    return "dalloc-" + hashlib.sha1(raw.encode("utf-8")).hexdigest()[:16]


def _serves(compatibility: Mapping[str, set[str]], resource_kind: str, demand_kind: str) -> bool:
    return resource_kind == demand_kind or demand_kind in compatibility.get(resource_kind, set())


def _minutes(travel: Mapping[tuple[str, str], int], origin: str, target: str) -> int | None:
    if origin == target:
        return 0
    return travel.get((origin, target))


def _demand_order(demands: Sequence[Mapping[str, Any]], work_orders: Mapping[str, Mapping[str, Any]]):
    def key(demand: Mapping[str, Any]):
        wo = work_orders[demand["work_order_id"]]
        return (
            -SEVERITY_RANK.get(wo.get("severity") or "low", 1),
            -float(wo.get("score") or 0.0),
            int(wo["priority"]),
            wo["created_at"],
            demand["work_order_id"],
            demand["kind"],
        )

    return sorted(demands, key=key)


def build_plan(
    demands: Sequence[Mapping[str, Any]],
    resources: Sequence[Mapping[str, Any]],
    reserves: Mapping[tuple[str, str], int],
    travel: Mapping[tuple[str, str], int],
    compatibility: Mapping[str, set[str]],
    work_orders: Mapping[str, Mapping[str, Any]],
    overrides: Mapping[tuple[str, str], int] | None = None,
) -> dict[str, Any]:
    """按风险顺序贪心分配，返回分配明细、未满足原因和跨片区标记。

    每个工单需求依次受三类约束：资源兼容性、道路到达时间（含 max_minutes 窗口）、
    片区最低保有量（可外借量 = 可用量 - 最低保有量 - 本方案已占用量）。
    人工调整（overrides）优先占用资源，其余需求再按确定性顺序自动补齐。
    """
    overrides = overrides or {}
    resources_by_id = {r["resource_id"]: dict(r) for r in resources}
    committed: dict[str, int] = {rid: 0 for rid in resources_by_id}
    # (resource_id, work_order_id) -> (demand_kind, quantity)
    placed: dict[tuple[str, str], tuple[str, int]] = {}
    override_totals: dict[tuple[str, str], int] = {}

    def demands_for(work_order_id: str):
        return [d for d in demands if d["work_order_id"] == work_order_id]

    # 先校验并落位人工调整：资源必须与该工单的唯一兼容需求对应。
    for (resource_id, work_order_id), quantity in sorted(overrides.items()):
        resource = resources_by_id.get(resource_id)
        if resource is None:
            raise ValueError(f"调整引用的资源 {resource_id} 不属于本方案")
        matches = [d for d in demands_for(work_order_id) if _serves(compatibility, resource["kind"], d["kind"])]
        if not matches:
            raise ValueError(f"资源 {resource_id} 与工单 {work_order_id} 的需求不兼容")
        if len(matches) > 1:
            raise ValueError(f"资源 {resource_id} 可满足工单 {work_order_id} 的多种需求，调整需先拆分")
        demand = matches[0]
        wo = work_orders[work_order_id]
        minutes = _minutes(travel, resource["district"], wo["district"])
        if minutes is None:
            raise ValueError(f"资源 {resource_id} 到片区 {wo['district']} 缺少道路到达时间")
        if minutes > int(demand.get("max_minutes", 10**9)):
            raise ValueError(f"资源 {resource_id} 到达时间 {minutes} 分钟超过时限")
        if not 0 <= quantity <= int(demand["quantity"]):
            raise ValueError("调整数量必须在 0 到工单需求量之间")
        key = (work_order_id, demand["kind"])
        override_totals[key] = override_totals.get(key, 0) + quantity
        if override_totals[key] > int(demand["quantity"]):
            raise ValueError(f"工单 {work_order_id} 的 {demand['kind']} 人工调整总量超过需求量")
        if quantity > 0:
            committed[resource_id] += quantity
        # quantity=0 表示明确不使用该资源：登记后自动补齐会跳过它。
        placed[(resource_id, work_order_id)] = (demand["kind"], quantity)

    def lendable(resource: Mapping[str, Any]) -> int:
        return (
            int(resource["available"])
            - reserves.get((resource["kind"], resource["district"]), 0)
            - committed[resource["resource_id"]]
        )

    lines: list[dict[str, Any]] = []

    for demand in _demand_order(demands, work_orders):
        wo = work_orders[demand["work_order_id"]]
        remaining = int(demand["quantity"])
        max_minutes = int(demand.get("max_minutes", 10**9))

        # 该工单该需求类型上的人工调整直接成为分配行。
        for (resource_id, work_order_id), (kind, quantity) in sorted(placed.items()):
            if work_order_id != demand["work_order_id"] or kind != demand["kind"] or quantity <= 0:
                continue
            resource = resources_by_id[resource_id]
            minutes = _minutes(travel, resource["district"], wo["district"]) or 0
            lines.append(_line(demand, wo, resource, quantity, minutes))
            remaining -= quantity

        compatible = [r for r in resources_by_id.values() if _serves(compatibility, r["kind"], demand["kind"])]
        while remaining > 0:
            candidates = []
            for resource in compatible:
                # 同一资源对该工单已有人工调整行时不再自动追加，避免重复明细编号。
                if (resource["resource_id"], demand["work_order_id"]) in placed:
                    continue
                minutes = _minutes(travel, resource["district"], wo["district"])
                if minutes is None or minutes > max_minutes or lendable(resource) <= 0:
                    continue
                candidates.append(
                    (0 if resource["district"] == wo["district"] else 1, minutes, -lendable(resource), resource["resource_id"], resource)
                )
            if not candidates:
                break
            *_, resource = sorted(candidates, key=lambda c: c[:4])[0]
            take = min(remaining, lendable(resource))
            committed[resource["resource_id"]] += take
            remaining -= take
            minutes = _minutes(travel, resource["district"], wo["district"]) or 0
            lines.append(_line(demand, wo, resource, take, minutes))

    lines.sort(key=lambda line: (line["work_order_id"], line["from_district"], line["resource_id"]))

    unmet = []
    for demand in demands:
        wo = work_orders[demand["work_order_id"]]
        satisfied = sum(
            line["quantity"]
            for line in lines
            if line["work_order_id"] == demand["work_order_id"] and line["demand_kind"] == demand["kind"]
        )
        shortage = int(demand["quantity"]) - satisfied
        if shortage > 0:
            unmet.append(_unmet(demand, wo, shortage, satisfied, resources_by_id, reserves, travel, compatibility, committed))

    cross_district = any(line["cross_district"] for line in lines)
    return {"allocations": lines, "unmet": unmet, "cross_district": cross_district}


def _line(demand, wo, resource, quantity, minutes):
    return {
        "resource_id": resource["resource_id"],
        "kind": resource["kind"],
        "demand_kind": demand["kind"],
        "work_order_id": demand["work_order_id"],
        "from_district": resource["district"],
        "to_district": wo["district"],
        "quantity": quantity,
        "travel_minutes": minutes,
        "cross_district": resource["district"] != wo["district"],
    }


def _unmet(demand, wo, shortage, satisfied, resources_by_id, reserves, travel, compatibility, committed):
    max_minutes = int(demand.get("max_minutes", 10**9))
    compatible = [r for r in resources_by_id.values() if _serves(compatibility, r["kind"], demand["kind"])]
    reasons: list[str] = []
    detail: dict[str, Any] = {"shortage": shortage}
    if not compatible:
        reasons.append("no-compatible-resource")
    else:
        unknown_road = [r for r in compatible if _minutes(travel, r["district"], wo["district"]) is None]
        timed = [(r, _minutes(travel, r["district"], wo["district"])) for r in compatible if _minutes(travel, r["district"], wo["district"]) is not None]
        beyond = [(r, m) for r, m in timed if m > max_minutes]
        reachable = [(r, m) for r, m in timed if m <= max_minutes]
        if unknown_road:
            reasons.append("road-time-unknown")
            detail["districts_without_road_time"] = sorted({r["district"] for r in unknown_road})
        if beyond:
            reasons.append("beyond-max-minutes")
            detail["nearest_minutes"] = min(m for _, m in beyond)
        if reachable:
            # 保有量和高优先级工单已占用后的真实可借余量。
            spare = sum(
                max(0, int(r["available"]) - reserves.get((r["kind"], r["district"]), 0) - committed.get(r["resource_id"], 0))
                for r, _ in reachable
            )
            reserve_only = all(
                int(r["available"]) - reserves.get((r["kind"], r["district"]), 0) <= 0 for r, _ in reachable
            )
            if spare <= 0 and reserve_only:
                reasons.append("district-reserve-held")
            elif spare <= 0:
                reasons.append("higher-risk-orders-first")
            else:
                reasons.append("insufficient-capacity")
            detail["reachable_spare_after_reserve"] = spare
    return {
        "work_order_id": demand["work_order_id"],
        "district": wo["district"],
        "kind": demand["kind"],
        "requested": int(demand["quantity"]),
        "satisfied": satisfied,
        "shortage": shortage,
        "reasons": reasons,
        "detail": detail,
    }


def render_lines(plan_id: str, lines: Sequence[Mapping[str, Any]]) -> list[dict[str, Any]]:
    """用真实方案编号补全确定性的分配明细编号。"""
    rendered = []
    for line in lines:
        item = dict(line)
        item["allocation_id"] = allocation_id(plan_id, line["resource_id"], line["work_order_id"])
        rendered.append(item)
    return rendered
