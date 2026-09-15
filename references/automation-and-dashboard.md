# 自动采集、自动分析与看板运行手册

本文件说明本 Skill 在“自动获取评论数据 → 自动分析 → 看板呈现”这条链路上的具体实现方式、权限前提和降级策略。

## 一、三条采集通道

| 通道 | 覆盖范围 | 是否可无人值守 | 权限要求 | 验证方式 |
| --- | --- | --- | --- | --- |
| A. Reviews API | 近 7 天内提交或修改的评论 | 是，需每日定时 | Service Account + Play Console 用户授权 | 先运行 `verify_credentials.py`，再检查重复同步是否幂等 |
| B. GCS 月度报告 | 应用全生命周期历史 | 是，按月定时 | Service Account + Storage Object Viewer + 下载批量报告权限 | 检查月份覆盖、编码解析和重复导入结果 |
| C. 手动 CSV 目录 | 已下载的月份 | 否，需人工下载 | 仅 Play Console 登录账号 | 运行 `selftest.py` 后使用脱敏样例做端到端验证 |

采集策略：**B 灌历史基线，A 做每日增量，C 作为零权限兜底。** 三者写入同一张 `reviews` 表，靠 `review_id` 主键天然去重，重复运行不会翻倍。

### 通道 A：Reviews API 每日增量

关键限制：`androidpublisher.reviews.list` 只返回最近约 7 天内提交或被修改的评论。**必须每日运行**，否则超出窗口的数据永久丢失，只能等次月报告补。

### 通道 A 上线检查清单

按顺序做，每步都有明确成功信号：

1. **启用 API**：GCP 项目启用 `androidpublisher.googleapis.com`。
2. **建服务账号**：创建 Service Account 并下载 JSON key，放到受控目录（不要放项目目录，不要进 Git）。
3. **装依赖**：`python -m pip install google-auth google-api-python-client`（GCS 另需 `google-cloud-storage`）。
4. **Console 授权**：Play Console → 用户和权限 → 邀请 SA 邮箱 → 勾选「查看应用信息」。邀请生效需 1-5 分钟，立刻跑会拿到 403。
5. **凭证自检**（再写定时任务之前必做）：

   ```bash
   python verify_credentials.py --key "<SA_JSON>" --package "<PACKAGE>"
   ```

   成功标志：`[OK] 凭证格式合法` → `[OK] API 调用成功，本次返回 N 条可见评论` → `[OK] 全部检查通过`。
   返回 0 条不是失败：窗口内无新评论即为 0。

6. **增量入库 + 分析链路**：

   ```bash
   python fetch_api.py     --key "<SA_JSON>" --package "<PACKAGE>" --db "<WORKDIR>/reviews.db"
   python preprocess.py    --db "<WORKDIR>/reviews.db"
   python analyze.py       --db "<WORKDIR>/reviews.db" --package "<PACKAGE>" --out "<WORKDIR>/report.json"
   python make_report.py   --report "<WORKDIR>/report.json" --out "<WORKDIR>/review_report.html"
   ```

   成功标志：`通道 A 完成：API 返回 N 条，写入/更新 N 条`，随后产出 report.json 与 HTML。
   **再跑一次第二条应显示 `写入/更新 0 条`**——这是幂等写入的正常表现，不是没采到数据。

异常码处置（脚本会直接打印，这里是留档）：

| 状态码 | 含义 | 处置 |
| --- | --- | --- |
| 401 | 密钥无效或本机时间偏差过大 | 重新生成 JSON key，校准系统时间 |
| 403 | 服务账号未被授权 | 检查 API 是否启用、Play Console 是否邀请并勾选权限；刚邀请完等待 1-5 分钟 |
| 404 | 包名找不到或该应用未授权给 SA | 核对包名，确认 SA 有该应用访问权 |
| 429 / 5xx | 配额或服务端错误 | 脚本内置指数退避重试，仍失败则降低频率后重试 |

实现要点：

- 翻页用 `tokenPagination.nextPageToken`，对 429/500/502/503 做指数退避重试（最多 5 次）。
- 一条 review 的 `comments[]` 里 `userComment` 是用户评论、`developerComment` 是开发者回复，取最新一组拍平成一行。
- 设备优先取 `deviceMetadata.productName`，回退 `device`。
- 每次同步写 `sync_log`，失败也要落 `status=failed` 与错误详情，便于排查断档。

### 通道 B：GCS 月度报告回溯

桶地址获取：Play Console → 下载报告 → 评价 → 复制 Cloud Storage URI，形如 `gs://pubsite_prod_rev_<id>/reviews`。

- 文件命名：`reviews/reviews_<package>_YYYYMM.csv`。
- 编码为 UTF-16LE 带 BOM，必须显式解码，否则整表乱码。
- 按包名过滤，避免同桶多应用串数据。

### 通道 C：手动 CSV 兜底

被邀请的协作者通常看不到「API 访问」页面，这不是采集失败。此时直接手动下载 CSV，放入一个目录后按本地模式入库，解码、去重、字段映射与自动通道完全一致。

文件名兼容三种形态：官方 `reviews_<pkg>_YYYYMM.csv`、Console 下载的 `reviews_reviews_<pkg>_YYYYMM.csv`、浏览器重复下载的 ` (1)` ` (2)` 后缀。

## 二、无人值守调度

### 推荐调度节奏

- 每日 09:00：通道 A 增量 → 预处理 → 分析 → 重建看板。
- 每月 3 日：通道 B 回溯上月完整报告，补齐 API 窗口外的遗漏。
- 每次发版后 72 小时内：加跑一次版本风险对比，用于升级回归判断。

### 落地方式

优先使用平台的定时任务能力创建自动化（推荐），无需依赖本机 cron：

- 任务内容：执行采集 → 分析 → 看板重建，并汇报差评率、P0 变化和新增高风险版本/机型。
- 一次性提醒用 `once`，日常巡检用 `recurring`。

若要在服务器上跑，用系统级调度器：

```bash
# Linux/macOS crontab
0 9 * * * cd /path/pipeline && python fetch_api.py --db reviews.db && python preprocess.py --db reviews.db && python analyze.py --db reviews.db --out report.json && python make_report.py --report report.json --out review_report.html
```

Windows 可用任务计划程序调用同一串命令。Python 解释器应使用项目隔离虚拟环境中的绝对路径。

定时任务里要使用绝对路径，且不能依赖交互式 shell 的环境变量，凭证用 `--key` 显式传入更稳：

```bash
"<PY>" "<SkillDir>/scripts/fetch_api.py" --key "<SA_JSON>" --package "<PACKAGE>" --db "<WORKDIR>/reviews.db"
"<PY>" "<SkillDir>/scripts/preprocess.py"  --db "<WORKDIR>/reviews.db"
"<PY>" "<SkillDir>/scripts/analyze.py"     --db "<WORKDIR>/reviews.db" --package "<PACKAGE>" --out "<WORKDIR>/report.json"
"<PY>" "<SkillDir>/scripts/make_report.py" --report "<WORKDIR>/report.json" --out "<WORKDIR>/review_report.html"
```

### 断档自检

每次运行后检查：

- `sync_log` 最近一次 `status` 是否为 `ok`。
- `MAX(submitted_at)` 距今是否超过 48 小时（超过说明采集断了）。
- 本次 `rows_seen` 是否异常为 0。
- 当月评论量与上月同期偏差是否超过 50%。

```sql
SELECT status, started_at, finished_at, rows_seen, rows_upsert, detail
FROM sync_log ORDER BY id DESC LIMIT 1;

SELECT MAX(submitted_at) AS latest FROM reviews;
```

任一异常都要在报告里显式告警，不要静默出图。

注意区分两种"零"：

- `写入/更新 0 条`：重复运行的正常幂等结果。
- `API 返回 0 条`：7 天窗口内确实没有新评论或被修改的评论，需结合发布节奏判断是否为异常。

## 三、自动分析层

固定顺序，每步都可单独重跑：

1. **入库**：UPSERT，按 `last_modified` 覆盖用户修改后的评论。
2. **预处理**：NFKC 归一化、重复字符压缩、空正文过滤、近似去重、语言基码、差评标记、技术线索标记，产出 `reviews_clean`。
3. **确定性分析**：总量/均分/差评率、月度趋势、多语言关键词主题、设备风险、版本风险、语言与市场代理分布、重复文本聚集、差评队列、行动项。

`report.json` 还会额外输出两个给 PRD 环节用的字段，看板暂不渲染，需直接读取 JSON：

- `verification_pool`：样本不足、根因待定的主题，写 PRD 时应放在"暂不立项"而不是 P0/P1。
- `device_hotspots`：机型 × 主题的热点组合，用于在写兼容类需求时定位优先机型。
4. **语义分析（可选）**：仅对高信息密度差评调用 LLM 做结构化打标，用短 ID 映射降低 Token。
5. **报告渲染**：`report.json` → 单页 HTML 看板。

分析层的硬约束：

- 残缺月份标记 `partial=true`，**不参与计数类环比**，否则趋势会整体反向。
- 语言字段被用作国家/市场时必须标注为代理，多国共用语言不映射到单一国家。
- 小样本主题必须同时展示样本量，禁止仅凭差评率排优先级。

## 四、看板呈现

单页 HTML，无外部依赖，可直接分发或嵌入评审。默认版块顺序按“先决策后证据”排列：

1. **KPI 行**：评论总量、平均星级、差评率、有效语料占比。
2. **行动清单**：优先级、问题证据、建议负责人、下一步行动、验证指标——放在最上方，看板首屏必须是决策而不是统计。
3. **月度口碑走势**：残缺月显式标注。
4. **国家/市场差评分布**：附带语言代码与数据边界说明。
5. **问题主题排行**：提及、差评、差评占比、完整月环比、月度分布迷你柱。
6. **高风险机型 / 高风险版本 TOP10**：始终带样本量。
7. **代表性差评原声**：可回溯到原文，含星级、语言、机型。
8. **重复文本聚集**：区分自然好评套话与异常刷评。

视觉规范：深灰底 + 翠绿主色的 BI 终端风格；危险用红、警告用黄。差评率、比率类指标统一右对齐并使用等宽数字。

看板质量门槛：

- 每个 P0/P1 都能点回原文证据。
- 任何比率都伴随分母。
- 残缺月、语言代理、小样本三类边界在页面内可见，而不是只写在文档里。
