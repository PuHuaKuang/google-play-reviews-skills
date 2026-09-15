"""第 3 步（B 部分）：LLM 结构化打标与聚类摘要。

只处理 analyze.py 筛出的高价值队列（差评 + 含技术线索 + 长文本），
而不是把全量评论都送入 LLM。按信息密度排序后取头部，能够控制调用成本，
并优先覆盖更具分析价值的反馈。

支持三种后端，按环境变量自动选择：
  OPENAI_API_KEY   -> OpenAI 兼容接口（含 DeepSeek/Kimi/通义等，配 OPENAI_BASE_URL）
  ANTHROPIC_API_KEY-> Claude
  GEMINI_API_KEY   -> Gemini
都没配时进入 --dry-run，只打印将要发送的 prompt 和预估成本，不产生调用。

用法：
  python llm_label.py --db reviews.db --limit 60           # 真实调用
  python llm_label.py --db reviews.db --limit 60 --dry-run # 只看 prompt 和成本
"""

from __future__ import annotations

import argparse
import json
import os
import sqlite3
import time
import urllib.error
import urllib.request

LABEL_DDL = """
CREATE TABLE IF NOT EXISTS review_labels (
    review_id   TEXT PRIMARY KEY,
    category    TEXT,
    sub_issue   TEXT,
    sentiment   TEXT,
    severity    INTEGER,
    module      TEXT,
    is_bug      INTEGER,
    summary_zh  TEXT,
    confidence  REAL,
    model       TEXT,
    labeled_at  TEXT
);
"""

CATEGORIES = [
    "功能缺陷", "性能卡顿", "崩溃闪退", "广告体验", "视频播放",
    "界面易用", "登录账号", "更新安装", "付费订阅", "网络下载", "其他",
]

APP_HINT_DEFAULT = "一款移动应用"

SYSTEM_PROMPT_TMPL = """你是移动应用质量分析师，分析{app_hint}的用户评论。

任务：为每条评论输出结构化标签。评论是多语言的（西语、俄语、阿语、越南语等占多数），先理解含义再打标。

严格要求：
1. 只依据评论原文下结论，不要推测原文没提到的内容。
2. summary_zh 用一句中文概括用户遇到的具体问题，不要复述"用户不满意"这类空话。
3. sub_issue 要具体到可排查的现象，例如"更新后视频全屏按钮常驻遮挡画面"而非"UI问题"。
4. 无法判断时 category 填"其他"，confidence 给低分，不要硬凑。
5. severity: 5=完全无法使用/数据丢失, 4=核心功能失效, 3=频繁影响体验, 2=偶发或次要, 1=主观偏好。
6. is_bug: 1 表示是技术缺陷（可提工单），0 表示是产品策略抱怨（如嫌广告多、嫌收费）。

category 只能从这个列表选：""" + "、".join(CATEGORIES) + """

输出 JSON 数组，每个元素对应一条输入评论，id 原样回填输入里的 R 编号，字段：
{"id":"R1","category":"","sub_issue":"","sentiment":"负面/中性/正面","severity":1-5,"module":"涉及模块","is_bug":0或1,"summary_zh":"","confidence":0.0-1.0}
必须为每条输入都输出一个元素，数量与输入一致。
只输出 JSON，不要 markdown 代码块，不要解释。"""


def build_system_prompt(app_hint: str | None = None) -> str:
    """把应用描述注入系统提示。

    用 replace 而不是 str.format：模板末尾的 JSON 示例里有大量花括号，
    format 会把它们当占位符并抛 KeyError。
    """
    hint = (app_hint or "").strip() or APP_HINT_DEFAULT
    return SYSTEM_PROMPT_TMPL.replace("{app_hint}", hint)


def build_batch_prompt(rows: list[dict]) -> tuple[str, dict[str, str]]:
    """构造批量 prompt，并返回短序号到真实 review_id 的映射。

    为什么不直接把 review_id 发给模型：CSV 报告里的 review_id 是整条
    Play Console 后台 URL（190+ 字符），实测占了输入 token 的近 40%，
    而且长 ID 容易被模型复制出错。改用 R1/R2 这类短号，回来再映射。
    """
    lines, idmap = [], {}
    for i, r in enumerate(rows, 1):
        tag = f"R{i}"
        idmap[tag] = r["review_id"]
        text = (r["review_text"] or "").replace("\n", " ")[:500]
        meta = (f"{r['star_rating']}星 lang={r['lang_base']} "
                f"device={r['device'] or '-'} ver={r['app_version'] or '-'}")
        lines.append(f"{tag} [{meta}]\n{text}")
    prompt = "待分析评论共 %d 条：\n\n%s" % (len(rows), "\n\n".join(lines))
    return prompt, idmap


# ---------- 后端适配 ----------

def _post(url: str, payload: dict, headers: dict, timeout: int = 180) -> dict:
    req = urllib.request.Request(
        url, data=json.dumps(payload).encode("utf-8"),
        headers={"Content-Type": "application/json", **headers},
    )
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return json.loads(resp.read().decode("utf-8"))


def call_openai(system: str, user: str, model: str) -> str:
    base = os.environ.get("OPENAI_BASE_URL", "https://api.openai.com/v1").rstrip("/")
    data = _post(
        f"{base}/chat/completions",
        {
            "model": model,
            "messages": [
                {"role": "system", "content": system},
                {"role": "user", "content": user},
            ],
            "temperature": 0,
        },
        {"Authorization": f"Bearer {os.environ['OPENAI_API_KEY']}"},
    )
    return data["choices"][0]["message"]["content"]


def call_anthropic(system: str, user: str, model: str) -> str:
    data = _post(
        "https://api.anthropic.com/v1/messages",
        {
            "model": model, "max_tokens": 8000, "temperature": 0,
            "system": system,
            "messages": [{"role": "user", "content": user}],
        },
        {
            "x-api-key": os.environ["ANTHROPIC_API_KEY"],
            "anthropic-version": "2023-06-01",
        },
    )
    return "".join(b.get("text", "") for b in data["content"])


def call_gemini(system: str, user: str, model: str) -> str:
    key = os.environ["GEMINI_API_KEY"]
    data = _post(
        f"https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent?key={key}",
        {
            "systemInstruction": {"parts": [{"text": system}]},
            "contents": [{"parts": [{"text": user}]}],
            "generationConfig": {"temperature": 0, "responseMimeType": "application/json"},
        },
        {},
    )
    return data["candidates"][0]["content"]["parts"][0]["text"]


def pick_backend(model_override: str | None):
    """按已配置的环境变量自动选后端，避免用户改代码。"""
    if os.environ.get("OPENAI_API_KEY"):
        return "openai", model_override or os.environ.get("OPENAI_MODEL", "gpt-4o-mini"), call_openai
    if os.environ.get("ANTHROPIC_API_KEY"):
        return "anthropic", model_override or "claude-sonnet-4-20250514", call_anthropic
    if os.environ.get("GEMINI_API_KEY"):
        return "gemini", model_override or "gemini-2.0-flash", call_gemini
    return None, None, None


def parse_json_array(raw: str) -> list[dict]:
    """LLM 偶尔会包 markdown 代码块或加前言，做容错提取。"""
    s = raw.strip()
    if s.startswith("```"):
        s = s.split("```", 2)[1]
        if s.lstrip().lower().startswith("json"):
            s = s.lstrip()[4:]
    start, end = s.find("["), s.rfind("]")
    if start == -1 or end == -1:
        raise ValueError(f"响应中未找到 JSON 数组：{raw[:200]}")
    return json.loads(s[start:end + 1])


def fetch_queue(conn, limit: int, only_unlabeled: bool) -> list[dict]:
    conn.executescript(LABEL_DDL)
    where = "WHERE c.is_negative = 1"
    if only_unlabeled:
        where += " AND l.review_id IS NULL"
    rows = conn.execute(f"""
        SELECT c.review_id, c.star_rating, c.lang_base, c.device, c.app_version,
               c.review_text, c.has_tech_hint, c.text_len
        FROM reviews_clean c
        LEFT JOIN review_labels l ON l.review_id = c.review_id
        {where}
        ORDER BY c.has_tech_hint DESC, c.text_len DESC
        LIMIT ?""", (limit,)).fetchall()
    return [dict(r) for r in rows]


def save_labels(conn, items: list[dict], model: str, idmap: dict[str, str]) -> int:
    now = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
    payload = []
    for it in items:
        tag = str(it.get("id") or it.get("review_id") or "").strip()
        # 短号映射回真实 ID；模型若原样吐出真实 ID 也兼容
        rid = idmap.get(tag) or (tag if tag in idmap.values() else None)
        if not rid:
            continue
        cat = it.get("category")
        payload.append((
            rid,
            cat if cat in CATEGORIES else "其他",
            it.get("sub_issue"), it.get("sentiment"),
            int(it["severity"]) if str(it.get("severity", "")).isdigit() else None,
            it.get("module"),
            1 if it.get("is_bug") in (1, True, "1") else 0,
            it.get("summary_zh"),
            float(it["confidence"]) if it.get("confidence") is not None else None,
            model, now,
        ))
    conn.executemany(
        """INSERT INTO review_labels
           (review_id, category, sub_issue, sentiment, severity, module,
            is_bug, summary_zh, confidence, model, labeled_at)
           VALUES (?,?,?,?,?,?,?,?,?,?,?)
           ON CONFLICT(review_id) DO UPDATE SET
             category=excluded.category, sub_issue=excluded.sub_issue,
             sentiment=excluded.sentiment, severity=excluded.severity,
             module=excluded.module, is_bug=excluded.is_bug,
             summary_zh=excluded.summary_zh, confidence=excluded.confidence,
             model=excluded.model, labeled_at=excluded.labeled_at""",
        payload,
    )
    conn.commit()
    return len(payload)


def main() -> int:
    ap = argparse.ArgumentParser(description="LLM 结构化打标（第 3 步 B 部分）")
    ap.add_argument("--db", default="reviews.db")
    ap.add_argument("--limit", type=int, default=60, help="本次处理的差评条数")
    ap.add_argument("--batch", type=int, default=20, help="每次请求携带的评论数")
    ap.add_argument("--model", help="覆盖默认模型名")
    ap.add_argument("--app-hint", default=APP_HINT_DEFAULT,
                    help="注入系统提示的应用描述")
    ap.add_argument("--dry-run", action="store_true", help="只打印 prompt 与成本预估")
    ap.add_argument("--redo", action="store_true", help="重新打标已处理过的评论")
    args = ap.parse_args()

    conn = sqlite3.connect(args.db)
    conn.row_factory = sqlite3.Row
    queue = fetch_queue(conn, args.limit, only_unlabeled=not args.redo)
    if not queue:
        print("队列为空。可能所有差评都已打标，加 --redo 可重跑。")
        return 0

    backend, model, caller = pick_backend(args.model)
    batches = [queue[i:i + args.batch] for i in range(0, len(queue), args.batch)]
    system_prompt = build_system_prompt(args.app_hint)

    if args.dry_run or not backend:
        if not backend:
            print("未检测到任何 API Key，进入 dry-run。")
            print("设置其中之一即可真实调用："
                  "OPENAI_API_KEY / ANTHROPIC_API_KEY / GEMINI_API_KEY\n")
        sample, _ = build_batch_prompt(batches[0])
        chars = len(system_prompt) + sum(len(build_batch_prompt(b)[0]) for b in batches)
        # 多语言文本约 2 字符/token，输出约占输入的 40%
        tok_in = chars // 2
        tok_out = int(tok_in * 0.4)
        print(f"待打标 {len(queue)} 条，分 {len(batches)} 批（每批 {args.batch} 条）")
        print(f"预估输入 ~{tok_in:,} tokens，输出 ~{tok_out:,} tokens")
        print(f"按 gpt-4o-mini 价格（$0.15/$0.60 per 1M）约 "
              f"${tok_in/1e6*0.15 + tok_out/1e6*0.6:.4f}")
        print("\n--- 首批 prompt 预览（前 1200 字符）---")
        print(sample[:1200])
        return 0

    print(f"后端 {backend} / 模型 {model}，共 {len(batches)} 批")
    saved = 0
    for i, batch in enumerate(batches, 1):
        user, idmap = build_batch_prompt(batch)
        for attempt in range(3):
            try:
                raw = caller(system_prompt, user, model)
                items = parse_json_array(raw)
                n = save_labels(conn, items, model, idmap)
                saved += n
                miss = len(batch) - n
                warn = f"，{miss} 条未回填" if miss > 0 else ""
                print(f"  批次 {i}/{len(batches)}：解析 {len(items)} 条，"
                      f"入库 {n} 条{warn}，累计 {saved}")
                break
            except (urllib.error.HTTPError, urllib.error.URLError, ValueError,
                    json.JSONDecodeError, KeyError) as exc:
                wait = 2 ** attempt
                print(f"  批次 {i} 第 {attempt+1} 次失败：{type(exc).__name__} {exc}，{wait}s 后重试")
                time.sleep(wait)
        else:
            print(f"  批次 {i} 三次均失败，跳过")

    rows = conn.execute("""
        SELECT category, COUNT(*) n, ROUND(AVG(severity),2) sev, SUM(is_bug) bugs
        FROM review_labels GROUP BY category ORDER BY n DESC""").fetchall()
    print("\n打标结果分布")
    for r in rows:
        print(f"  {r['category']:<8} {r['n']:>4} 条  平均严重度 {r['sev']}  技术缺陷 {r['bugs']}")
    conn.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
