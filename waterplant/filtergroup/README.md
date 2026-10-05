# 滤池池组与分区管理

解决"池号是一张人工维护的列表，重新编号之后备份、现场标牌和值班记录
对不上"的问题。核心做法：给每座滤池一个终身不变的池标识 `pool_id`
（资产台账号），把会变的**池号 / 分区 / 投运顺序**作为可版本化的
分配来管理，所有变更走 append-only 事件日志。

## 不变量

1. 同一版本内，池号、分区、投运顺序三列两两一一对应（双射）：
   任意两个池不能占同一个池号、分区或投运顺序。建账、提交、恢复的
   每一步都会重新校验。
2. 池标识 `pool_id` 终身不变；版本快照只追加，不修改不删除。
3. 值班记录只追加，永远保存**书写当时的池号原文和版本号**，
   重排不会回改任何历史记录。

## 一次重排的生命周期

提交时两项必须显式指定，系统不给默认值：

| 参数 | 取值 | 含义 |
| --- | --- | --- |
| `switch` | `IMMEDIATE` | 立即切换，提交调用内一路推到生效 |
| | `SHIFT_END` | 等本班结束再切，期间新旧编号并存 |
| `policy` | `REJECT_BATCH` | 发现占用冲突拒绝整批 |
| | `APPLY_CONFLICT_ONLY` | 只改不冲突项，冲突池确定性地保留原编号 |

结构性冲突（方案漏了池、多了池、同一池出现多次）在任何策略下都
整批拒绝。

切换按固定四步推进，每步独立落盘，可断点恢复：

```
VALIDATE  -> BUILD_CODEMAP -> CUTOVER -> AUDIT
登记目标版本   生成并校验新旧对照   切生效指针   补审计
```

* **部分失败与恢复**：任一步崩溃后方案为 `FAILED`，`recover` 从断点
  前推，已完成步骤跳过，重复调用结果完全一致（幂等）。
* SHIFT_END 方案在登记阶段失败时，`recover` 只把它恢复到"待激活"，
  不会越过班末替值班员切换。
* **对照一致性**：`BUILD_CODEMAP` 生成逐池对照边并强校验——每池恰好
  一条边、边的旧值等于旧版本、新值等于新版本、新三元组可逆唯一；
  不通过则不切换。

## 新旧编号并存（兼容读方）

SHIFT_END 方案进入待激活后，生效版本与目标版本同时可被读方识别：

* `resolve`（按池号或分区）同时在两个版本里查找；
* 若新旧版本复用了同一号码指代不同的池，返回 `ambiguous=true` 和全部
  候选，不静默挑一个；
* 读历史记录时用记录上的版本号调 `translate`，沿池标识把旧号翻译到
  任意版本（"这条旧记录说的是今天的哪座池")。

## HTTP 接口

| 方法 | 路径 | 说明 |
| --- | --- | --- |
| POST | `/filtergroup/bootstrap` | 初始建账（只允许一次） |
| GET | `/filtergroup/layout` | 当前布局；并存期含待激活版本与对照 |
| POST | `/filtergroup/plan` | 提交重排方案 |
| GET | `/filtergroup/plans` `/filtergroup/plan` | 方案进度（四步完成情况） |
| POST | `/filtergroup/activate` | 班末激活 SHIFT_END 方案 |
| POST | `/filtergroup/cancel` | 撤销待激活方案（幂等） |
| POST | `/filtergroup/recover` | 部分失败后恢复（幂等） |
| POST | `/filtergroup/resolve` | 新旧池号/分区同时反查 |
| POST | `/filtergroup/translate` | 按记录版本翻译旧池号 |
| GET/POST | `/filtergroup/duty` | 值班记录（不可变列表 / 追加） |
| GET | `/filtergroup/audit` | 池组变动审计 |

整批被冲突拒绝时返回 `409`，body 内带完整 `conflicts` 明细供控制台
展示；参数缺失或非法返回 `400`。所有变动（建账、提交、拒绝、激活、
撤销、恢复、值班记录）都落审计。

## 存储键

* `filtergroup:events` — append-only 领域事件（版本快照、方案、步骤、对照边）
* `filtergroup:state` — 唯一可改写的指针（当前生效版本、待激活方案）
* `filtergroup:duty` — append-only 值班记录
* `filtergroup:audit` — append-only 审计
