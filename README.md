# 阶梯报价与汇率联动下的毛利可行区域资料台

本项目保存业务资料的修订版本，提供资料校验、原始记录查询和现有口径的概览核算。操作人员通过本地 HTTP 接口导入资料，分析客户端按版本读取，避免后来的补录覆盖先前资料。

## 环境与运行

使用 Python 3.11，数值计算依赖固定在 requirements.txt。容器使用固定摘要的基础镜像。准备阶段可安装依赖，准备完成后测试与演示只访问本机；无需数据库服务或业务账号。

```sh
docker build -t quote_contours:local .
docker run --rm --network none quote_contours:local python -m unittest discover -s tests -v
docker run --rm --network none quote_contours:local python -m quote_contours demo
```

demo 同时输出 /summary 概览和固定 revision 的毛利情景分析；可用 `python -m quote_contours analyze --database ... --revision ...` 对已导入资料重复运行分析。

也可在 Python 3.11 虚拟环境中安装 requirements.txt 后运行相同的测试和模块命令。demo 在临时目录建库，启动真实本地 HTTP 服务，导入资料，再通过客户端读取结果，退出时关闭服务。运行过程没有固定等待或远程请求。

## 常驻资料接口

```sh
python -m quote_contours serve --database /tmp/quote_contours.sqlite3 --load fixtures/demo.json
```

默认监听 127.0.0.1:8080；--host 和 --port 可调整。服务用于可信本地环境，当前不提供公网认证与授权。数据保存在指定 SQLite 文件，重启后仍可按 revision 取回。API 格式见 docs/interfaces.md，业务数据语义见 docs/data-contract.md。

## 目录与现有能力

`quote_contours/domain.py` 负责资料规则和已有概览；`annual.py` 负责整条年度履约路径的精确核算；`analysis.py` 负责固定 revision 的情景、可行区域、临界点与方案比较；`repository.py` 负责不可变资料版本与导入回执；`service.py` 是服务边界；`client.py` 是独立 HTTP 消费者。fixtures/demo.json 是便于逐项核对的小样本，fixtures/history.json 是同口径的本地合成历史资料。测试覆盖领域规则、年度核算、可行区域、覆盖项、版本稳定性及真实 HTTP 往返。

## 毛利情景分析

分析固定在一个 revision 上运行（`POST /analysis` 或 `python -m quote_contours analyze`），不会在读取时换成最新资料。三种结论分开陈述：

- `exact`：单个情景的年度精确核算，金额构成的明细行可逐项对账到毛利（`net_revenue` 是小计行）；退单按原成交价和原汇率冲回，年度返利与折扣在年末一次性定档。
- `feasible_regions`：在该情景声明的联合条件下，对年度总量做精确整数枚举，给出满足公司毛利率底线的区间，并列出进入/离开区间的临界点由哪条合同条款触发（数量折扣档位、年度返利档位或最低运费）；“多卖反而少赚”的离散下降单独列在 `cliffs`。
- `risk`：只总结已声明有限情景中的观察结果。它不提供概率、置信区间、未声明情景插值，也不会把不同情景的汇率、成本或运费拼成最坏情况。

`overrides` 让同事调整销量、结算币种和运费条件；覆盖一致作用于整条月度路径，并回显在 `applied_overrides`。每个结果都带 `data_digest`、合同摘要和情景摘要，合同修改后仍能判断旧结论适用于哪个版本。新增接口保留 /records、/summary 既有行为。
