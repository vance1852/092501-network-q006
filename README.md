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

## 暴雨跨片区调度

当多个积水、管涌工单同时触发时，可以先**预演**再**确认**调度方案，而不是直接把固定数量扣给单个工单：

- `POST /dispatch/compatibility`：登记需求种类与可兼容资源种类（管理员）；
- `POST /dispatch/reserves`：设置片区对某类资源的最低保有量（管理员）；
- `POST /work-orders/{id}/road-access`：登记资源到工单的道路到达时间与是否可达；
- `POST /work-orders/{id}/demand`：声明工单需要的资源种类和数量；
- `POST /dispatch/plans`：预演，返回确定性方案（`assignments`）和未满足需求（`unmet`，含原因）；
- `POST /dispatch/plans/{id}/adjust`：人工调整，必须填写理由；
- `POST /dispatch/plans/{id}/confirm`：核对资源版本后一次性落库；
- `GET /dispatch/plans`、`GET /dispatch/plans/{id}`：方案与最终分配重启后仍可查询。

确定性规则：工单按风险分降序、优先级、到达时间排序；同工单内本片区资源优先，跨片区只能动用超出最低保有量的部分，资源种类必须兼容且道路可达。同一输入预演返回同一方案；确认时会在写事务内重新计算资源版本，版本变化返回 `409` 且不产生任何扣减；重复确认幂等返回原方案。方案含跨片区分配时需要市级调度员（`dispatcher`，内置账号 `dispatcher/city-dispatch`）或管理员确认，普通操作员确认会得到 `403`。
