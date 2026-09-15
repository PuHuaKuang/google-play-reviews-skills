# 应用评论洞察 Skill 执行模板

模板 0 是对话层用法（怎么把活派给 Skill），模板 A~F 是命令层用法（怎么直接跑脚本）。不确定选哪个时，先用模板 0。

## 模板 0：Skill 调用用法（对话层）

### 0.1 触发语句

以下任一表述都会命中本 Skill，直接把 `<PACKAGE>` 与数据位置说清即可：

```text
帮我分析 <PACKAGE> 的 Google Play 用户评价，CSV 在 <WORKDIR>/评论 目录。
```

```text
用 app-review-insight 跑一遍 <PACKAGE> 的评论分析，输出看板和行动项。
```

```text
从 <PACKAGE> 最近的差评里推导需求 PRD，要有证据和验收指标。
```

```text
帮我配置 <PACKAGE> 评论的自动采集与每日看板，我只有协作者权限。
```

### 0.2 一次说清的四要素

信息不全时会被反问，按下面模板一次给全更省事：

```text
应用包名：<PACKAGE>
数据来源：本地 CSV / Reviews API / GCS 月报（选一个或多个）
数据位置：<WORKDIR>/评论
交付物：HTML 看板 / report.json / 行动项清单 / PRD（可多选）
```

### 0.3 按交付物选用法

| 想要的结果 | 说法模板 | 实际执行 |
| --- | --- | --- |
| 只要结论，不要文件 | `先看 <PACKAGE> 的评论问题分布，不用生成文件` | 走确定性分析，直接在对话里给结论 |
| 要可评审看板 | `生成 <PACKAGE> 的评论分析看板` | 模板 A，产出 `review_report.html` |
| 要行动项 | `按优先级给出 <PACKAGE> 的改进行动项和验收指标` | 分析后执行证据审查，输出 P0/P1/P2 |
| 要 PRD | `从这批评论推导 <PACKAGE> 的需求 PRD` | 证据审查 → `*_PRD.md` |
| 要自动化 | `把 <PACKAGE> 的评论分析做成每日自动任务` | 模板 B/F，配置调度与断档自检 |
| 要语义深挖 | `对头部差评做 LLM 打标，先 dry-run 看成本` | 模板 D，默认先 dry-run |

### 0.4 追加约束的说法

需要收窄口径时补一句即可，这些约束会直接落到分析参数上：

```text
只看最近 3 个月，残缺月不要进环比。
只分析 1-2 星差评，样本量低于 30 的主题标注不确定。
国家维度按语言代理处理，不要写成真实国家归属。
LLM 打标只跑 60 条，先 dry-run 报成本。
```

### 0.5 不要这样提

- 不要粘贴 Service Account 私钥或 API Key 到对话；改为在环境变量中配置（见模板 B）。
- 不要只说"分析一下评论"却不给包名和数据位置，会多一轮确认。
- 不要要求把关键词命中数直接当需求结论，本 Skill 会拆成故障路径后再判断优先级。

## 命令层模板说明

以下命令默认在包含 CSV 数据的工作目录中执行。脚本也可以从 Skill 目录直接调用：

```text
<SKILL_DIR>\scripts
```

将 `<PACKAGE>` 替换为 Android applicationId，例如 `com.example.app`；将 `<WORKDIR>` 替换为自己的数据目录；`<SA_JSON>` 替换为 Service Account JSON key 的绝对路径。

模板 A、D、E 无需 Google 凭证，用 Python 3.10+ 即可。模板 B0、B 需要 `google-auth` 与 `google-api-python-client`，模板 C 额外需要 `google-cloud-storage`。建议在项目自己的隔离虚拟环境中安装依赖，避免污染系统 Python。

## 模板 A：协作者手动 CSV 全流程

适合没有 Reviews API 权限的场景。先从 Play Console 下载 CSV 放到 `<WORKDIR>\评论`：

```bash
python "<SKILL_DIR>/scripts/run_all.py" \
  --csv-dir "<WORKDIR>/评论" \
  --package "<PACKAGE>" \
  --db "<WORKDIR>/reviews.db"
```

输出：

```text
<WORKDIR>/reviews.db
<WORKDIR>/report.json
<WORKDIR>/review_report.html
```

如果只需要重新生成看板：

```bash
python "<SKILL_DIR>/scripts/make_report.py" \
  --report "<WORKDIR>/report.json" \
  --out "<WORKDIR>/review_report.html"
```

## 模板 B0：Reviews API 凭证自检（配置完先跑它）

适合刚配好 Service Account、还没接定时任务的阶段。只读，不写数据库：

```bash
python "<SKILL_DIR>/scripts/verify_credentials.py" \
  --key "<SA_JSON>" \
  --package "<PACKAGE>"
```

预期输出：

```text
[--] 凭证路径：<SA_JSON>
[OK] 凭证格式合法
     服务账号邮箱：<sa>@<project>.iam.gserviceaccount.com
     GCP 项目 ID ：<project>
[--] 正在读取 <PACKAGE> 的可见评论（只读）……
[OK] API 调用成功，本次返回 10 条可见评论
[OK] 全部检查通过。
```

返回 0 条也可能出现，属正常：窗口内暂无新评论。出现 401/403/404 时脚本会直接给出对应处置建议，不要跳过。

## 模板 B：Reviews API 自动增量

依赖（建议安装在项目自己的隔离虚拟环境中）：

```bash
python -m pip install google-auth google-api-python-client
```

先做凭证自检（模板 B0），通过后再入库。凭证优先用 `--key` 显式传入，不依赖环境变量，更适合定时任务：

```bash
python "<SKILL_DIR>/scripts/fetch_api.py" \
  --key "<SA_JSON>" \
  --package "<PACKAGE>" \
  --db "<WORKDIR>/reviews.db"
python "<SKILL_DIR>/scripts/preprocess.py" \
  --db "<WORKDIR>/reviews.db"
python "<SKILL_DIR>/scripts/analyze.py" \
  --db "<WORKDIR>/reviews.db" \
  --package "<PACKAGE>" \
  --out "<WORKDIR>/report.json"
python "<SKILL_DIR>/scripts/make_report.py" \
  --report "<WORKDIR>/report.json" \
  --out "<WORKDIR>/review_report.html"
```

也可以用环境变量代替 `--key`（PowerShell 写法，只对当前会话生效）：

```powershell
$env:GOOGLE_APPLICATION_CREDENTIALS = "<SA_JSON>"
$env:GOOGLE_PLAY_PACKAGE_NAME = "<PACKAGE>"
```

预期输出与校验点：

```text
通道 A 完成：API 返回 N 条，写入/更新 N 条
```

- 第二次运行应显示 **`写入/更新 0 条`**，这是幂等写入的正确表现，不是没采到数据。
- `API 返回 0 条` 与 `写入 0 条` 含义不同：前者是 7 天窗口内确实没有新评论，需结合发版节奏判断是否异常。

Reviews API 只能获取近约 7 天的新增或修改评论，推荐每日运行。**只跑通道 A 时样本量很小，趋势和环比结论不成立**，历史数据要用模板 C 或 A 以外的 CSV 通道补齐后再做趋势。

## 模板 C：GCS 历史报告（待配置，未在本机跑通）

启动前需先补齐三项，缺任一项都会失败：

1. **桶名**：Play Console → 下载报告 → 评价 → 「复制 Cloud Storage URI」，形如 `gs://pubsite_prod_rev_<id>/reviews`，脚本 `--bucket` 只填桶名，不带 `gs://`。
2. **IAM 权限**：给 Service Account 授予该桶的 Storage Object Viewer，同时 Play Console 侧要有批量报告下载权限。
3. **依赖**：`python -m pip install google-cloud-storage`（通道 A 的两个依赖不包含它）。

```bash
python "<SKILL_DIR>/scripts/fetch_gcs.py" \
  --bucket "<BUCKET_NAME>" \
  --package "<PACKAGE>" \
  --since "202401" \
  --db "<WORKDIR>/reviews.db"
python "<SKILL_DIR>/scripts/preprocess.py" \
  --db "<WORKDIR>/reviews.db"
python "<SKILL_DIR>/scripts/analyze.py" \
  --db "<WORKDIR>/reviews.db" \
  --package "<PACKAGE>" \
  --out "<WORKDIR>/report.json"
python "<SKILL_DIR>/scripts/make_report.py" \
  --report "<WORKDIR>/report.json" \
  --out "<WORKDIR>/review_report.html"
```

## 模板 D：可选 LLM 深度打标

在模板 A、B 或 C 完成入库和预处理后执行：

```bash
python "<SKILL_DIR>/scripts/llm_label.py" \
  --db "<WORKDIR>/reviews.db" \
  --limit 60 \
  --batch 20 \
  --app-hint "一款跨平台笔记应用"
```

`--app-hint` 注入系统提示中的应用描述，缺省为“一款移动应用”。填入真实形态（笔记、短视频、工具类等）能显著改善多语言差评的模块归类准确度。未配置任何 API Key 时脚本自动进入 dry-run，只打印 prompt 与成本预估。

只配置一个后端即可：`OPENAI_API_KEY`、`ANTHROPIC_API_KEY` 或 `GEMINI_API_KEY`。不配置 Key 时使用 `--dry-run` 预览输入和成本，不发送数据。

## 模板 E：离线自检

```bash
python "<SKILL_DIR>/scripts/selftest.py"
```

## 模板 F：Windows 任务计划程序

创建任务时：

- 程序：Python 解释器的绝对路径；
- 参数：`"<SkillDir>\\scripts\\run_all.py" --csv-dir "<WORKDIR>\\评论" --package "<PACKAGE>" --db "<WORKDIR>\\reviews.db"`；
- 起始于：`<WORKDIR>`；
- 触发器：每天一次；
- 失败操作：记录退出码并发送通知。

建议不要把 Service Account JSON 放入项目目录。CSV、数据库、报告也建议放在受控目录，并限制访问权限。

## 参数速查

| 脚本 | 用途 | 关键参数 |
| --- | --- | --- |
| `verify_credentials.py` | 通道 A 凭证自检 + 只读探测 | `--key`, `--package` |
| `fetch_api.py` | API 增量采集 | `--package`, `--key`, `--db`, `--page-size` |
| `fetch_gcs.py` | GCS 或本地 CSV | `--bucket` / `--local-csv`, `--package`, `--since`, `--db` |
| `preprocess.py` | 清洗去重 | `--db`, `--min-len` |
| `analyze.py` | 确定性分析 | `--db`, `--package`, `--out` |
| `llm_label.py` | LLM 结构化打标 | `--db`, `--limit`, `--batch`, `--model`, `--app-hint`, `--dry-run`, `--redo` |
| `make_report.py` | HTML 看板 | `--report`, `--out`, `--package` |
| `run_all.py` | 一键全流程 | `--csv-dir`, `--package`, `--db`, `--source csv\|api`, `--llm` |
| `selftest.py` | 离线回归测试 | 无 |
