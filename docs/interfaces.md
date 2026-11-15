# 本地接口约定

所有接口使用 UTF-8 JSON，错误响应为带 error 字段的对象。GET /health 返回进程健康状态。POST /imports 接收 request_key 与 data，data 遵循业务资料契约；新导入返回 201，完全相同的请求重放返回 200。相同请求号对应不同资料返回 409；非法资料返回 400，不留下部分版本。

GET /revisions 列出 revision 和内容摘要。GET /records 返回业务原始记录；GET /summary 返回已有概览，两者都返回 revision、digest 和 result。可加 revision 查询参数固定历史资料，不提供时读取最新资料；资料不存在为 404。摘要对对象键顺序不敏感，对数组顺序敏感；资料内容相同可共享版本，导入请求回执相互独立。

未知路径为 404。现有客户端依赖这些路径和包装字段；后续增加业务分析接口时保留既有行为，不能把资料导入成功等同于业务分析已经形成结论。每个分析产物应声明所用数据版本，不能在客户端读取时偷偷替换成最新资料。

## POST /analysis

请求体必须显式固定 revision，不读取“最新资料”：

```json
{
  "revision": 3,
  "options": {
    "margin_floor": 0.18,
    "contracts": ["offer-a"],
    "scenarios": ["s1", "s2"],
    "search": {"min_path_total": 0, "max_path_total": 800},
    "overrides": {
      "s2": {"currency": "EUR", "freight_multiplier": 1.4},
      "s1": {"contracts": {"offer-b": {"quantity": 260}}}
    }
  }
}
```

响应外层为 `revision`、`digest`、`result`，与 /summary 一致；`result.data_digest` 必须等于该 revision 的 digest，`result.result_digest` 由数据摘要和 options 共同决定。缺少 revision、缺少 `margin_floor`、未知合同/情景、未知覆盖字段、覆盖币种未在情景中声明汇率，均返回 400；revision 不存在返回 404。

`result` 的主要字段：

- `exact`：每个（合同, 情景）一份年度精确核算。`account.composition` 的明细行（不含 `net_revenue` 小计行）相加等于 `account.margin`；`account.orders` 保留每笔交付的成交价、汇率、成本、折扣和运费来源；`account.path` 给出逐月交付流与累计跨档记录，年末数值才是最终年度档位。`contract_digest` / `scenario_digest` 标明所用合同与情景版本。
- `feasible_regions`：每个（合同, 情景）在 `search` 范围内对情景年度总量做精确整数枚举的结果。`intervals` 为满足 `margin_floor` 的连续区间；`transitions` 记录进入/离开可行区域的整单量并给出触发条款；`cliffs` 记录“多卖反而少赚”的离散下降点及触发条款；`method` 声明枚举范围、步长与联动假设。枚举点按各月声明数量的比例分配，每月汇率与运费条件保持联动。
- `region_intersections`：同一合同在所选全部情景下共同可行的总量区间；只覆盖请求中列出的情景，不外推。
- `comparison`：同一组（合同, 情景）内的毛利与毛利率、相对本情景最优的差距、`applied_overrides` 和合同/情景摘要。`scenario_wins` 统计每个合同在多少个情景内毛利最高。
- `risk`：`scope` 固定为有限已声明情景；`observations` 与 `observed_ranges` 只总结观察到的结果，`not_supported` 明确排除概率、置信区间、未声明情景插值和跨情景拼接的最坏情况。

`overrides` 必须一致应用于整个情景路径，不能把某月汇率与另一月运费拼在一起。`quantity` 覆盖按声明月份形状重新分配年度总量；`months` 覆盖逐月数量并保留该月汇率/运费；`currency` 覆盖要求该币种已在情景 `fx` 中声明，切换为本币 `CNY` 时使用恒等汇率 1 并在 `applied_overrides.implied_fx` 回显。`freight_multiplier` 覆盖整条路径，`freight_floor` / `freight_unit` 覆盖合同条款，`return_rate` / `return_month` 覆盖退单假设。所有实际生效的覆盖均回显在 `applied_overrides`；`quantity` 与 `months` 不能同时使用。`options` 只接受 `margin_floor`、`contracts`、`scenarios`、`search`、`overrides`，其他字段返回 400，避免拼写错误的覆盖被静默忽略。
