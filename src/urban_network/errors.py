"""跨片区调度用例向 API 暴露的错误。"""
from __future__ import annotations


class DispatchError(RuntimeError):
    status = 400


class VersionConflict(DispatchError):
    """确认时资源版本与预演依据不一致。"""
    status = 409


class PlanStateError(DispatchError):
    """方案状态不允许该操作（例如已确认后再调整）。"""
    status = 409
