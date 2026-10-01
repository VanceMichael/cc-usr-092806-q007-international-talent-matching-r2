# 协同事务平台

协同事务平台为需要多机构参与的业务提供统一的机构、授权、案件、证据、审批、资源预约、资金分录、消息归并、通知、定时任务和历史回放能力。项目只使用 Python 标准库与 SQLite，适合在单个 Linux 应用容器内运行。

## 可追溯招聘流程

针对杭州国际人才交流大会后暴露的“同一岗位多版本口径、薪酬承诺被静默覆盖”问题，平台在既有组织、授权、审批和恢复能力之上提供一条可追溯招聘流水线（`civicflow.recruitment`）：

- **岗位与要求版本化**：企业可维护本方岗位（技能、语言、签证/工作许可、预算），每次修改产生新版本；岗位发布、预算调整、关闭均留痕。
- **版本冻结**：每次推荐与面试安排都冻结当时的岗位版本、要求快照和候选人证件版本；岗位事后改版不会改写已进行的流程。
- **资格例外分权审批**：企业招聘人员只能申请例外，提交者本人与企业在岗成员均不能批准，例外决定记录审批人。
- **候选人授权**：联系方式仅向获授权人员（中心人员、授权范围内或企业在岗成员且授权有效）开放；撤回授权后立即对企业遮蔽。
- **简历回执去重**：同一候选人同一岗位重复收到相同简历时保留原进度；标识相同但资格材料摘要不同时进入人工核验，不覆盖原申请。
- **截断只影响未完成环节**：撤回授权、证件到期、岗位关闭、预算变化会取消未举行的面试和未完成待办；进行中但无承诺的申请关闭，已有承诺的申请保留承诺，下一步指向撤回记录或补充协议。
- **承诺不可静默覆盖**：录用承诺带薪酬与承诺期限；变更只能以补充协议衔接（原承诺标记 `superseded` 完整保留，新版本通过 `amendment_of` 承接），撤回必须留下带原因的撤回记录；撤回授权后签补充协议须重新取得授权。
- **可恢复待办与定时任务**：证件到期、面试协调、承诺确认同时落入持久待办表与定时任务队列；任务租约过期（含进程崩溃重启）可重新认领，确定性标识保证重放不产生重复。
- **可追溯解释**：`explain` 汇总推荐人、当时使用的岗位/证件版本、逐项匹配报告、例外审批人、全部承诺版本、当前待办和哈希审计链时间线，可回答“为何被推荐、满足了哪版要求、谁作的例外、下一步由谁处理”。

## 目录

- `src/civicflow/`：领域服务、SQLite 持久化、权限和命令行入口。
- `tests/`：核心流程、边界条件和异常路径测试。
- `examples/`：本地演示输入。

## 配置

通过 `CIVICFLOW_DB` 指定 SQLite 文件路径；不设置时命令行使用当前目录下的 `civicflow.sqlite3`。所有时间使用带时区的 ISO 8601 字符串。

## 测试

```bash
PYTHONPATH=src python3 -m unittest discover -s tests -v
```

## 编译或构建

```bash
PYTHONPATH=src python3 -m compileall -q src
```

## 使用

初始化数据库并运行离线演示：

```bash
PYTHONPATH=src python3 -m civicflow.cli --db /tmp/civicflow-demo.sqlite3 demo
```

查看当前案件：

```bash
PYTHONPATH=src python3 -m civicflow.cli --db /tmp/civicflow-demo.sqlite3 list-cases
```

运行可追溯招聘端到端演示（建组织、建岗、收简历、授权、例外审批、冻结版本、两轮面试、承诺与补充协议）：

```bash
PYTHONPATH=src python3 -m civicflow.cli --db /tmp/civicflow-demo.sqlite3 --now 2026-10-01T12:00:00+08:00 rec-demo
```

查看重启后仍在的待办，以及对某份申请的完整追溯解释：

```bash
PYTHONPATH=src python3 -m civicflow.cli --db /tmp/civicflow-demo.sqlite3 rec-todos
PYTHONPATH=src python3 -m civicflow.cli --db /tmp/civicflow-demo.sqlite3 rec-explain <application_id>
```
