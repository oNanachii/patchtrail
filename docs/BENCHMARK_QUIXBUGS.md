# QuixBugs Python 公开小基准

运行开始（UTC）：2026-11-01T17:09:23.565275+00:00；状态：`completed`。
数据：[`benchmark-quixbugs-20261101T170923Z-99c250d0.json`](../artifacts/benchmark-quixbugs-20261101T170923Z-99c250d0.json)；逐次请求、响应及费用：[`benchmark-quixbugs-20261101T170923Z-99c250d0-calls.json`](../artifacts/benchmark-quixbugs-20261101T170923Z-99c250d0-calls.json)。

## 范围与许可

QuixBugs 上游 LICENSE 为 MIT；上游 legal_notes.txt 另有 Quixey 历史权属说明。数据集目录由固定版本下载与转换脚本生成，不直接提交。公开请求/响应记录含上游源码与测试片段，附带[上游完整许可与说明](QUIXBUGS_NOTICE.txt)；下载目录也保留两份说明。
上游固定版本、归档摘要及转换规则见工件 `dataset`；[上游许可证](https://github.com/jkoppel/QuixBugs/blob/4257f44b0ff1181dedaedee6a447e133219fcebf/LICENSE)。

## 可复现命令

```sh
python scripts/benchmark_quixbugs.py run --data-dir var/benchmark/quixbugs-new \
  --container-bin podman --pytest-image localhost/patchtrail-pytest:prepared \
  --env-file /path/to/private.env --cap-yuan 8
```

先安装 requirements.lock.txt，并按 [pytest 镜像说明](../deploy/pytest/README.md)构建 `localhost/patchtrail-pytest:prepared`。命令会将标签解析为不可变镜像 ID 并记录；本轮实际 ID 为 `6896b73045bc7adf6dc64c94d6932aff6a14e20e4d6a8b79b392a801bd81d4f7`，容器内 Python 3.12.16、pytest 9.0.3。Key 只从私有环境文件或环境变量读取。
`dry-run` 子命令只下载、转换、容器预检及估价；`run` 必须先完成同样的 dry-run，再开始付费。`report --artifact 文件` 可离线重算基础报告；本文件另附本轮结果核查说明，重算基础报告不会包含这些补充说明。离线核对全部数字：

```sh
python scripts/check_quixbugs_benchmark.py artifacts/benchmark-quixbugs-20261101T170923Z-99c250d0.json
```

每次正式评测使用新数据目录；已启动目录拒绝重跑，不自动恢复或重试未知调用。基准进程直接使用独立 RunService 与本地 ASGI 确认接口，不连接线上服务；运行不会进入公开演示列表。

## 比较方法

全部 40 道 Python 题按名称排序预检。仅保留固定 pytest 容器中完整运行且确有测试失败的题；依赖缺失、无法收集、超时、原测试通过等全部列入排除记录。
测试转换只去掉正确实现分支、调整本地辅助模块导入；保留上游默认慢测试跳过。正确实现不进入模型上下文。源码可改，测试和数据只读，任务快照绑定内容哈希。
基线一次性接收源码与失败测试输出，直接返回完整文件；没有工具、没有迭代。Agent 使用产品原有提示和五个工具，可读取测试，预算 8 次调用 / 12 步 / 120 秒。两种方式使用相同模型、4096 输出 token 上限、相同镜像和原测试验证；不改变产品提示。
题目顺序固定，奇偶题交替两种方式的先后顺序；每题每种方式一次，不挑选结果。Agent 每个合法待审补丁经准确 digest/version 的 HTTP 确认接口，由**脚本自动批准**，不是人工审查。
耗时从该方式开始到最终验证结束，含模型、工具、确认和最终容器验证；共同的筛选预检排除在外。平均调用/token/耗时以实际尝试数为分母，修复率以全部符合条件题数为分母；未运行单独列出。

## 开跑前估价

预检符合条件 34 题。按每题一次基线、四次 Agent 调用，后续上下文为初始两倍、每次输出 512 token、无缓存折扣估算：627,520 token，最高时段价格约 ¥1.7773。输入 token 用 UTF-8 请求字节数/4 估计，不是精确分词。
若每次都用满配置，极保守的整批上界约 ¥158.82，超过预算；因此不预付整个配置上界，实际逐次预留并在下一次请求可能超过 ¥8 时停止。逐题估算和估价完成时间保存在 dry_run，早于首个付费派发。

## 结果

这里的“修复”只指全部原上游测试通过，未做语义正确性独立认证。两种方式的信息访问不同：基线只得到源码与失败输出，Agent 可通过工具读取完整测试，因此差距属于两套流程的比较，不能单独归因于工具本身。

| 方式 | 原测试全通过/符合条件 | 修复率（按原测试） | 尝试/未运行 | 平均调用 | 平均 token | 总 token | 总费用（元） | 平均耗时（秒） |
| --- | --- | --- | --- | --- | --- | --- | --- | --- |
| 单次基线 | 31/34 | 91.18% | 34/0 | 1.00 | 1608.56 | 54691 | 0.080593 | 7.70 |
| PatchTrail Agent | 34/34 | 100.00% | 34/0 | 2.29 | 6552.50 | 222785 | 0.146768 | 12.05 |

两种方式合计 **277,476 token、¥0.22736064**（用量计价），无未知调用；最高费率口径的累计费用为 ¥0.45472128，低于 ¥8 上限。

逐题配对：`{"tasks": 34, "both_fixed": 31, "baseline_only": 0, "agent_only": 3, "neither_fixed": 0}`。

费用按 API usage 与 [DeepSeek 官方人民币费率](https://api-docs.deepseek.com/zh-cn/quick_start/pricing/)计算，属于用量计价，非独立账单。缓存命中与未命中分别计价；峰时判断及逐次费率见请求账本。存在未知调用时，总 token 和费用只含已确认部分，token 均值留空，未确认费用预留见 cost。
每次请求先按全部 UTF-8 请求字节＋4096 token 余量、最大输出及最高费率预留；已确认峰值费用与未确认预留累计不超过 ¥8。未知调用保留预留并停止全批次，不自动重试。

## 逐题结果与排除原因

| 题目 | 预检/原因 | 单次基线 | Agent |
| --- | --- | --- | --- |
| bitcount | timed_out | 未运行 | 未运行 |
| breadth_first_search | ready | 通过 | 通过 |
| bucketsort | ready | 通过 | 通过 |
| depth_first_search | ready | 通过 | 通过 |
| detect_cycle | ready | 通过 | 通过 |
| find_first_in_sorted | timed_out | 未运行 | 未运行 |
| find_in_sorted | ready | 通过 | 通过 |
| flatten | ready | 通过 | 通过 |
| gcd | ready | 通过 | 通过 |
| get_factors | ready | 通过 | 通过 |
| hanoi | ready | 通过 | 通过 |
| is_valid_parenthesization | ready | 通过 | 通过 |
| kheapsort | ready | 通过 | 通过 |
| knapsack | ready | 通过 | 通过 |
| kth | ready | 通过 | 通过 |
| lcs_length | ready | 通过 | 通过 |
| levenshtein | timed_out | 未运行 | 未运行 |
| lis | ready | 通过 | 通过 |
| longest_common_subsequence | ready | 通过 | 通过 |
| max_sublist_sum | ready | 通过 | 通过 |
| mergesort | timed_out | 未运行 | 未运行 |
| minimum_spanning_tree | ready | patch_failed_tests | 通过 |
| next_palindrome | ready | 通过 | 通过 |
| next_permutation | ready | 通过 | 通过 |
| pascal | ready | 通过 | 通过 |
| possible_change | unconfirmed（输出截断） | 未运行 | 未运行 |
| powerset | ready | 通过 | 通过 |
| quicksort | ready | 通过 | 通过 |
| reverse_linked_list | ready | 通过 | 通过 |
| rpn_eval | ready | 通过 | 通过 |
| shortest_path_length | ready | 通过 | 通过 |
| shortest_path_lengths | ready | 通过 | 通过 |
| shortest_paths | ready | 通过 | 通过 |
| shunting_yard | ready | 通过 | 通过 |
| sieve | ready | 通过 | 通过 |
| sqrt | timed_out | 未运行 | 未运行 |
| subsequences | ready | 通过 | 通过 |
| to_base | ready | 通过 | 通过 |
| topological_ordering | ready | patch_failed_tests | 通过 |
| wrap | ready | patch_failed_tests | 通过 |

## 失败分类

- baseline：`{"patch_failed_tests": 3}`。
- agent：`{}`。

## 逐项核查与准备记录

- `minimum_spanning_tree`：基线返回多余边，3 条原测试中 2 条失败；Agent 同 3 条测试通过。
- `topological_ordering`：基线 3 条原测试均因输出顺序不同失败。静态核对前两张图，基线输出的替代顺序仍满足拓扑依赖；上游断言却要求特定列表。因此这项是严格测试预言机的差距，不能据此判定基线语义上没修好。
- `wrap`：基线 5 条原测试均失败，差异涉及行尾空格和切分位置，且补丁在部分宽度边界多保留一个空格，使行宽超限；Agent 同 5 条测试通过。评分沿用上游的精确字符串比较。
- 最终筛选排除 5 道超时题和 `possible_change`（输出截断，结果不完整）；没有第三方依赖缺失。`levenshtein` 在较早预检曾完成、本轮触发时限，说明筛选也受机器负载和时限影响。
- [首次 dry-run](../artifacts/benchmark-quixbugs-20261101T165647Z-c325006f.json)和[零调用准备失败](../artifacts/benchmark-quixbugs-20261101T170130Z-ad60a749.json)均保留。后者是独立副本缺少 `web/`，确认接口初始化失败；核对请求账本尚未创建、独立运行数为 0 后，保留失败标记并补齐文件。正式成绩只来自本报告开头的已完成工件；准备记录不计入模型尝试。dry-run 和该失败记录中的 `calls_artifact` 是尚未创建的计划路径。
- [独立离线核对](../artifacts/benchmark-quixbugs-20261101T170923Z-99c250d0-audit.json)：40 题、34 对配对、112 次调用的归属、usage、缓存计价、审批 digest/version 与结果一致；真实 Key 扫描通过。170 项本地 Python 检查通过；原有两份真实模型验收工件 SHA-256 均不变。这些内部核对与模型成绩分别判断。

## 结论与边界

本轮 Agent 相对单次基线多通过 3 题原测试，原测试通过率相差 8.82 个百分点；其中拓扑排序包含严格输出顺序造成的差距。调用、token、费用和耗时差距见同表；这是两套给定流程在同一小基准上的相对比较。
公开基准可能出现在模型训练数据里，不代表通用修复率。模型服务使用可变别名；实际返回标识保留在逐次响应中，不声称固定权重快照；复现指流程和核验步骤，再次调用不保证输出或筛选名单完全相同。测试通过只代表原有测试通过，未增加隐藏测试或外部人工正确性审查。
已有自编回归、自有源码验收记录原样保留，本轮结果单独报告。
