---
name: app-review-insight
description: This skill should be used when analyzing user reviews from app stores or other structured feedback sources, including ingestion, cleaning, deduplication, multilingual topic analysis, version/device/market risk analysis, evidence-backed action items, dashboards, and PRD derivation. It supports the bundled Google Play adapter and can be extended with additional source adapters.
agent_created: true
---

# 通用应用评论洞察

## 适用场景

在以下场景触发本 Skill：

- 用户提供应用商店评论 CSV、API JSON、SQLite 评论库或导出的反馈数据，要求统计、归因、找问题或生成报告。
- 用户要求分析差评、版本风险、设备兼容性、语言/市场风险、主题趋势或用户反馈优先级。
- 用户要求从评论证据推导产品需求、研发工单、行动项、看板或 PRD。
- 用户要求建立评论采集、清洗、分析和定期巡检流程。

默认将“评论”理解为移动应用商店评价，但不把任何平台字段、权限或采集方式硬编码为通用结论。具体平台能力由数据源适配器决定；当前仓库内置 Google Play 适配器。

## 快速用法

用户只给出模糊请求时，先补齐四要素：**应用标识、数据来源、数据位置、期望交付物**。可复制的对话模板和命令示例见 `references/execution-templates.md`。

本地 CSV 最短路径：

```bash
python <SKILL_DIR>/scripts/run_all.py \
  --csv-dir <WORKDIR>/reviews \
  --package <APP_ID> \
  --db <WORKDIR>/reviews.db
```

其中 `--package` 是兼容现有 Google Play 适配器的应用标识；接入其他商店时，应先把数据转换为统一字段，或实现新的 source adapter。

## 核心目标

将原始反馈转化为可验证的产品和工程决策：

`采集/导入 → 统一字段 → 解码与落库 → 清洗去重 → 确定性分析 → 主题/风险分析 → 证据审查 → 行动项/PRD → 报告`

禁止把关键词命中次数直接当作需求结论。必须区分：评论事实、问题模式、根因假设、验证状态和正式需求。

## 输入与输出

### 支持输入

- 商店导出的评论 CSV，包含标题、正文、星级、时间、语言、版本、设备等字段的任意子集。
- 评论 API 的 JSON 增量数据。
- 已有 SQLite 评论库、JSON 报告或 HTML 看板。
- 可选的版本发布记录、崩溃/ANR 数据、设备清单、地区字段或经授权的业务数据。

### 统一记录建议

适配器尽量映射到以下字段：

- `review_id`：稳定主键；缺失时使用稳定指纹生成，不使用 Python 内置 `hash()`。
- `app_id`、`source`、`star_rating`、`review_text`、`submitted_at`、`last_modified`。
- `reviewer_lang`、`country_or_market`、`device`、`os_version`。
- `app_version_code`、`app_version_name`、`developer_reply_text`、`developer_replied_at`。

缺失字段保持为空，不用推测值伪造完整性。国家/市场只在存在可信字段时作为事实；从语言推断时必须标记为“语言市场代理”。

### 默认输出

根据任务选择一项或多项：

- `reviews.db`：原始记录、清洗字段和同步日志。
- `report.json`：统计、主题、趋势、版本/设备/市场风险和行动项。
- `review_report.html`：适合评审的单页看板。
- `*_PRD.md`：带证据、验收、埋点、灰度和回滚的产品文档。
- 只要结论时，直接在对话中输出并保留证据边界，不擅自生成文件。

## 标准工作流

### 1. 识别来源与权限

优先使用用户已有的本地导出文件；需要外部 API 时，先说明权限、时间窗口、速率限制和凭证放置方式。不要要求用户把私钥、API Key 或密码粘贴到对话中。

采集通道应分为：历史基线、增量同步和手动导入。不同通道写入同一张评论表，以稳定 `review_id` 去重，并记录同步状态、时间和错误。

### 2. 建立可重复的数据底座

- 使用稳定主键和幂等 UPSERT。
- 正确处理 CSV 编码、引号和正文内换行。
- 分开保存版本 code/name、提交时间和最后修改时间。
- 保留纯星级评论用于评分和趋势；无正文记录不送入语义分析。
- 原始文件只读，产物写入用户指定的工作目录。

### 3. 清洗与质量检查

执行 Unicode NFKC 归一化、空正文过滤、重复/近似重复识别、基础语言标识和技术线索标记。报告至少包含：原始量、有正文量、有效语料量、纯星级占比、重复率、时间覆盖、残缺周期、版本/设备字段覆盖率。

### 4. 先确定性分析，再语义分析

先用可复现的规则和统计建立基线，再用 LLM/embedding 深挖。至少分析：总量、均分、低星率、时间趋势、主题提及与低星占比、版本/设备风险、语言/市场分布和待处理队列。低样本必须同时展示样本量和不确定性；残缺周期不得污染环比。

### 5. 证据审查

每个候选问题建立完整链路：

1. 原文样本：星级、时间、语言、版本、设备和正文。
2. 问题模式：把混合主题拆成具体故障路径。
3. 影响判断：受影响功能、版本、设备、市场、规模和趋势。
4. 根因状态：已复现、日志支持、待验证或仅用户感知。
5. 立项判断：P0/P1/P2 或验证池。
6. 验收指标：由埋点、测试或线上数据判断是否完成。

**口径提示（易错点）**：有正文评论通常只占全量的一小部分（部分应用仅约一成），且愿意写长文的用户更倾向表达不满，因此「有正文样本的差评率」会系统性地高于「全量差评率」，两者可能相差数倍。凡基于有正文样本得出的语言、主题、版本结论，必须标注样本口径；判断市场规模与整体质量风险时以全量口径为准，再用有正文样本解释成因。同一语言在两种口径下排名可能完全反转。

### 6. 生成行动项或 PRD

行动项至少包含：优先级、问题证据、负责人、下一步、验证指标、依赖和回滚方式。

正式 PRD 至少包含：问题定义、证据口径、目标/非目标、用户故事、功能与异常条件、埋点监控、验收阈值、灰度/熔断/回滚、责任团队、依赖和待评审事项。低样本问题先进入验证池，不因高差评率直接升级为 P0。

## 优先级建议

综合影响规模、严重度、证据强度和止损成本：

- **P0**：核心路径不可用、更新后大范围退化、崩溃/ANR、支付或登录等关键能力失效。
- **P1**：高频体验问题、特定版本/设备/市场显著风险、可通过规则或远端策略治理的问题。
- **P2**：低样本、根因不明或需要额外数据的问题，进入验证池。

不要只按主题提及数排序，也不要只因平均评分低就跳过复现和安全检查。

## 安全与隐私

- 凭证只通过受控文件或环境变量提供，不入库、不入包、不进对话。
- 用户标识、URL、账号、内容标题进入报表或埋点前脱敏/哈希。
- 不把评论中的猜测写成事实。
- 不通过关闭 TLS 校验等方式规避安全问题。

## 脚本与扩展

`scripts/` 是可复现执行实现，当前包含：

- `storage.py`：SQLite 表结构、幂等 UPSERT、同步记录。
- `fetch_gcs.py`：Google Play CSV/GCS 历史报告适配器，也支持本地 CSV 兜底。
- `fetch_api.py`、`verify_credentials.py`：Google Play Reviews API 增量采集和凭证自检。
- `preprocess.py`、`analyze.py`：清洗、确定性统计、主题和风险分析。
- `llm_label.py`：可选结构化 LLM 打标，支持 dry-run。
- `make_report.py`：生成 HTML 看板。
- `run_all.py`：串联导入、清洗、分析和报告。
- `selftest.py`：无需外部凭证的离线回归自检。

要接入其他平台，新增一个只负责“平台字段 → 统一记录”的适配器，并复用 `storage.py`、`preprocess.py`、`analyze.py` 和 `make_report.py`；不要在核心分析逻辑中散落平台判断。新增适配器时同步补充离线样例、自检和 README 配置说明。

交付前确认：重复导入不翻倍；主题可追溯到原文；趋势排除了残缺周期；市场结论没有超过字段能力；每个 P0/P1 有负责人、验收指标和回滚路径；低样本问题标注不确定性。
