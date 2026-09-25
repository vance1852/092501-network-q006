# 城市地下管网安全监测与应急调度服务

本项目为城市供水、排水和燃气管网提供离线后台服务，保存管段、传感器读数、泄漏告警、巡检工单、维修审批和应急资源分配。系统使用确定性的风险评分帮助值班人员优先处理高风险管段，账号按角色授予读取、处置和审批权限，状态变化写入 SQLite 审计表。

## 目录

- `src/urban_network/`：管网领域服务、风险计算、权限、SQLite 存储和 JSON API；
- `src/power_dispatch/`：应急泵站资源分配使用的计划与容量计算组件；
- `src/plant_science/`：传感器校准与统计分析组件；
- `tests/`：领域规则、存储事务和 API 测试。

## 环境

- Linux
- Python 3.11 或更高版本
- 运行时仅使用 Python 标准库和 SQLite

## 测试

```bash
PYTHONPATH=src python3 -m unittest discover -s tests -q
```

## 构建检查

```bash
python3 -m compileall -q src tests
```

## 离线验收

```bash
PYTHONPATH=src python3 -m urban_network.acceptance --workspace .
```

验收命令会创建演示管段、导入传感器读数、计算泄漏风险、生成巡检工单并输出 JSON。它不访问外部网络，也不要求常驻的数据库、队列或其他服务。

## HTTP API

```bash
PYTHONPATH=src python3 -m urban_network.api --database network.sqlite3 --host 127.0.0.1 --port 8080
```

`GET /health` 返回服务状态，其余接口使用 JSON 和 `Authorization: Bearer <token>` 会话，支持管段登记、读数上报、风险查询、工单创建和应急资源分配。

## 暴雨跨片区调度（预演 → 调整 → 确认）

当暴雨同时触发多个积水（移动泵）和管涌（围挡、抢修队）工单时，系统支持先预演再确认的调度方案：

1. 管理员维护调度基础数据：片区最低保有量 `POST /districts/reserves`、道路到达时间 `POST /roads/travel-times`、资源兼容性 `POST /resources/compatibility`（例如大流量泵车可代作移动泵）。
2. 具备 `dispatch_plan` 权限的角色（操作员、工程师、市级调度员等）调用 `POST /dispatch-plans` 提交多个工单需求进行预演。预演**不扣减任何资源**，按工单风险（告警等级、评分、优先级）排序，综合道路到达时间窗口、片区最低保有量、资源兼容性和本片区优先原则给出确定性分配；每条未满足需求都附带原因（无兼容资源、道路时间缺失、超出到达时限、片区保有量占用、余量已优先保障更高风险工单等）。
3. `POST /dispatch-plans/{id}/adjustments` 可记录人工调整（指定资源给某工单的数量，0 表示明确不使用），必须填写理由；每次调整保留修订历史并基于当前资源版本重算。
4. `POST /dispatch-plans/{id}/confirm` 确认方案：
   - 确认时重新计算资源版本（资源余量、保有量、道路时间、兼容性的 SHA-256），与预演依据不一致则方案置为 `failed` 并说明差异，**不发生任何扣减**；
   - 版本一致时全部扣减、调度明细落库和确认标记在同一 SQLite 事务内提交，任何失败整体回滚，不会留下部分扣减；
   - 重复确认幂等返回同一方案和同一分配编号，不重复扣减；
   - **含跨片区调动的方案必须由市级调度员（`dispatcher` 角色，`dispatch_confirm` 权限）确认，普通操作员越权确认返回 403**；纯本片区方案操作员可自行确认。
5. `GET /dispatch-plans/{id}` 查询方案、修订历史（含人工调整理由）和最终分配；数据持久化在 SQLite 中，服务重启后仍可查询。

内置账号：`admin/network-admin`、`operator/network-operator`、`dispatcher/network-dispatcher`。确认冲突返回 HTTP 409。
