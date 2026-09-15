"""第 3 步（A 部分）：确定性分析。不调用任何 LLM，零成本、可复现。

先做这一步的理由：LLM 打标是按 token 计费的，5000 条评论跑一轮聚类+打标
有实际成本。而下面这些结论纯 SQL 就能算出来，且每次结果完全一致，
适合做每日看板的基础指标。LLM 应该留给"读懂差评在说什么"这类真正需要
语义理解的活。

产出：
  - 控制台摘要
  - report.json（结构化结果，供后续 LLM 步骤或看板消费）

用法：
  python analyze.py --db reviews.db --out report.json
"""

from __future__ import annotations

import argparse
import json
import re
import sqlite3
from collections import Counter, defaultdict

# 主题词典：按业务模块归类，命中即计数。
# 这是"冷启动"方案——在没跑 embedding 聚类前先用它拿到 80% 的洞察。
# 每个主题下混排多语言关键词，因为海外评论英语占比通常不足 25%。
TOPICS: dict[str, list[str]] = {
    "闪退崩溃": [
        "crash", "crashing", "closes by itself", "shuts down", "force close",
        "se cierra", "se cierra sola", "fecha sozinho", "plante", "se ferme seul",
        "падает", "закрывается сам", "kapanıyor", "يغلق", "تتوقف", "tự tắt",
        "bị tắt", "keluar sendiri",
    ],
    "卡顿性能": [
        "slow", "lag", "laggy", "freeze", "stuck", "hang", "not responding",
        "lento", "lenta", "trava", "travando", "lent", "медленн", "зависает",
        "yavaş", "donuyor", "بطيء", "chậm", "treo", "lambat", "berat",
    ],
    "广告过多": [
        "ads", "too many ads", "advertisement", "popup", "pop-up", "pop up",
        "anuncios", "publicidad", "propaganda", "publicité", "реклам", "reklam",
        "إعلان", "quảng cáo", "iklan",
    ],
    "视频播放": [
        "buffering", "no sound", "black screen", "cannot play", "won't play",
        "not playing", "video", "subtitle", "no se reproduce", "sin sonido",
        "pantalla negra", "не воспроизв", "нет звука", "ses yok", "siyah ekran",
        "không phát", "không có tiếng", "phụ đề",
    ],
    "登录账号": [
        "login", "log in", "sign in", "cannot login", "account", "password",
        "no puedo iniciar", "cuenta", "не могу войти", "аккаунт", "giriş",
        "hesap", "تسجيل الدخول", "حساب", "đăng nhập", "tài khoản", "masuk",
    ],
    "更新安装": [
        "update", r"\bupdate", r"\binstall", "download", "actualiz", "instalar",
        "descargar", "обнов", "устано", "güncelle", "yükle", "تحديث", "تثبيت",
        "cập nhật", "cài đặt", "unduh",
    ],
    "耗电发热": [
        "battery", "drain", "overheat", "heats up", "batería", "consume batería",
        "bateria", "batterie", "chauffe", "батаре", "греется", "pil", "ısınıyor",
        "بطارية", "حرارة", "pin", "nóng", "panas", "baterai",
    ],
    "网络下载": [
        "network", "no internet", "connection", "wifi", "download speed",
        "sin conexión", "conexión", "conexão", "connexion", "интернет",
        "соединение", "bağlantı", "اتصال", "kết nối", "mạng", "koneksi",
    ],
    "界面易用": [
        "ui", "interface", "design", "confusing", "hard to use", "button",
        "interfaz", "diseño", "difícil de usar", "interface", "интерфейс",
        "неудобн", "arayüz", "kullanımı zor", "واجهة", "giao diện", "khó dùng",
        "antarmuka",
    ],
    "付费订阅": [
        "premium", "subscription", "pay", "paid", "refund", "price", "expensive",
        "suscripción", "pagar", "reembolso", "caro", "assinatura", "abonnement",
        "подписк", "оплат", "дорого", "abonelik", "ücret", "pahalı", "اشتراك",
        "مدفوع", "đăng ký", "trả phí", "đắt", "langganan", "bayar",
    ],
}
TOPIC_RE = {
    # 允许关键词自带正则片段（如 \binstall），避免 "uninstall" 被 "install" 误命中。
    name: re.compile("|".join(w if "\\" in w else re.escape(w) for w in words), re.IGNORECASE)
    for name, words in TOPICS.items()
}

# 核心路径阻塞：这些问题一旦成立，用户无法完成"打开网页/看视频"这个主任务，
# 因此优先级高于"体验不好"类的主题，且不受提及数排名的支配。
CORE_BLOCKER_TOPICS = {"闪退崩溃", "视频播放", "登录账号", "更新安装", "网络下载"}
CORE_BLOCKERS: dict[str, list[str]] = {
    "链接/交互不可点击": [
        "hyperlink", "links worked", "link", "enlace", "click", "clic",
        "no me deja", "não consigo clicar", "不能点", "点不了",
    ],
    "输入与焦点响应": [
        "typing", "keyboard", "teclado", "mouse", "souris", "ratón", "slowing",
        "lenta", "demais", "trava", "se pega", "bolinha", "lag", "freeze",
    ],
    "播放失败/闪退": [
        "video", "videos", "play", "gris", "black screen", "se sale", "se cierra",
        "crash", "closes", "pantalla", "播放", "闪退", "崩溃",
    ],
    "核心功能失效": [
        "no funciona", "not work", "doesn't work", "no sirve", "não funciona",
        "no puedo", "instant uninstall", "useless", "inútil",
    ],
}


def verification_pool(report_topics: list[dict]) -> list[dict]:
    """样本不足以支撑结论的主题：进验证池，不升级为 P0/P1。"""
    return [{
        "topic": t["topic"], "mentions": t["mentions"],
        "negative_mentions": t["negative_mentions"],
        "negative_ratio": t["negative_ratio"],
        "reason": "差评样本 < 3 条，可能是个体问题或分词口径，需人工复核后再定级",
        "next_step": "抽取原文确认是否为同一根因；若 30 天内累积到 3 条以上再启动立项",
    } for t in report_topics if t["negative_mentions"] < 3]


def q(conn, sql, *args):
    return conn.execute(sql, args).fetchall()


def overall(conn) -> dict:
    row = q(conn, """SELECT COUNT(*) n, ROUND(AVG(star_rating),2) avg,
                            SUM(is_negative) neg, SUM(has_tech_hint) tech
                     FROM reviews_clean""")[0]
    raw = q(conn, """SELECT COUNT(*) n, ROUND(AVG(star_rating),2) avg,
                            SUM(CASE WHEN star_rating<=2 THEN 1 ELSE 0 END) neg
                     FROM reviews""")[0]
    return {
        "raw_total": raw["n"],
        "raw_avg_rating": raw["avg"],
        "raw_negative": raw["neg"],
        "raw_negative_rate": round(raw["neg"] / raw["n"], 4) if raw["n"] else 0,
        "text_total": row["n"],
        "text_avg_rating": row["avg"],
        "text_negative": row["neg"],
        "text_with_tech_hint": row["tech"],
    }


def monthly_trend(conn) -> list[dict]:
    """按月看口碑走势。用原始表（含纯星级）才能反映真实评分。

    注意 avg_rating / negative_rate 是比率，不受月份天数影响，可直接比较；
    但 count 受影响，所以对残缺月标记 partial=True 并给出日均值。
    """
    complete = set(_covered_months(conn))
    rows = q(conn, """SELECT substr(submitted_at,1,7) m, COUNT(*) n,
                             ROUND(AVG(star_rating),3) avg,
                             SUM(CASE WHEN star_rating<=2 THEN 1 ELSE 0 END) neg,
                             MAX(substr(submitted_at,9,2)) last_day
                      FROM reviews
                      WHERE submitted_at IS NOT NULL AND submitted_at <> ''
                      GROUP BY m ORDER BY m""")
    floor = _month_floor(conn)
    out = []
    for r in rows:
        days = int(r["last_day"] or 0) or 1
        in_scope = r["m"] in complete
        # 区分两种被排除原因，避免把"样本太少的历史残留月"误标成"残缺月"：
        #   truncated = 月份本身覆盖天数不足（例如当月 CSV 只下载到 22 日）
        #   low_sample = 样本量远低于数据主体（通常是用户修改旧评论产生的残留）
        low_sample = (not in_scope) and r["n"] < floor
        truncated = (not in_scope) and (not low_sample)
        out.append({
            "month": r["m"], "count": r["n"], "avg_rating": r["avg"],
            "negative": r["neg"],
            "negative_rate": round(r["neg"] / r["n"], 4) if r["n"] else 0,
            "partial": not in_scope,
            "truncated": truncated,
            "low_sample": low_sample,
            "covered_days": days,
            "daily_avg_count": round(r["n"] / days, 1),
        })
    return out


def _month_floor(conn) -> int:
    """计算进入趋势分析的最低月样本量。"""
    rows = q(conn, """SELECT substr(submitted_at,1,7) m, COUNT(*) n
                      FROM reviews
                      WHERE submitted_at IS NOT NULL AND submitted_at <> ''
                      GROUP BY m""")
    counts = [int(r["n"]) for r in rows]
    return min(200, max(20, int(max(counts) * 0.05))) if counts else 20


def _covered_months(conn) -> list[str]:
    """报告实际覆盖的月份，并识别最后一个月是否残缺。

    为什么必须做这件事：手动下载的当月 CSV 通常只到下载日（本例止于 8-22）。
    拿一个 22 天的月份去和 31 天的月份比提及数，环比必然全线为负，
    看起来像"所有问题都在好转"，实际是纯统计假象。这个坑会直接误导决策。
    """
    rows = q(conn, """SELECT substr(submitted_at,1,7) m,
                             MAX(substr(submitted_at,9,2)) last_day,
                             COUNT(*) n
                      FROM reviews GROUP BY m ORDER BY m""")
    floor = _month_floor(conn)
    months = [(r["m"], int(r["last_day"] or 0)) for r in rows if r["n"] >= floor]
    if not months:
        return []
    tail_m, tail_day = months[-1]
    # 末月覆盖天数不足 27 天视为残缺，剔除后再算环比
    if tail_day < 27:
        return [m for m, _ in months[:-1]]
    return [m for m, _ in months]


def _momentum(by_month: dict[str, int], scope: list[str]) -> float | None:
    """最后一个完整月 vs 之前月份均值。scope 已排除残缺月。"""
    vals = [by_month.get(m, 0) for m in scope]
    if len(vals) < 3:
        return None
    recent, prior = vals[-1], vals[:-1]
    base = sum(prior) / len(prior)
    if base <= 0:
        return None
    return round((recent - base) / base, 3)


def topic_hits(conn) -> list[dict]:
    """主题命中统计。同一条评论可命中多个主题（真实评论常抱怨多件事）。"""
    self_months = _covered_months(conn)
    rows = q(conn, """SELECT review_id, review_text, star_rating, is_negative,
                             month_bucket, device, lang_base
                      FROM reviews_clean""")
    counts: dict[str, Counter] = defaultdict(Counter)
    samples: dict[str, list] = defaultdict(list)
    by_month: dict[str, Counter] = defaultdict(Counter)

    for r in rows:
        text = r["review_text"] or ""
        for name, rx in TOPIC_RE.items():
            if not rx.search(text):
                continue
            counts[name]["total"] += 1
            counts[name]["neg"] += r["is_negative"]
            by_month[name][r["month_bucket"]] += 1
            if r["is_negative"] and len(samples[name]) < 5:
                samples[name].append({
                    "text": text[:220], "star": r["star_rating"],
                    "device": r["device"], "lang": r["lang_base"],
                })

    result = []
    for name in TOPICS:
        c = counts[name]
        if not c["total"]:
            continue
        months = dict(sorted(by_month[name].items()))
        trend = _momentum(months, self_months)
        result.append({
            "topic": name,
            "mentions": c["total"],
            "negative_mentions": c["neg"],
            "negative_ratio": round(c["neg"] / c["total"], 3),
            "by_month": months,
            "mom_vs_baseline": trend,
            "samples": samples[name],
        })
    return sorted(result, key=lambda x: x["negative_mentions"], reverse=True)


def device_risk(conn, min_n: int = 15) -> list[dict]:
    """机型维度差评率。这是 CSV 报告最有价值的字段之一——
    定位到某几个机型集中差评，通常就是兼容性问题而非功能问题。"""
    rows = q(conn, """SELECT device, COUNT(*) n,
                             ROUND(AVG(star_rating),2) avg,
                             SUM(CASE WHEN star_rating<=2 THEN 1 ELSE 0 END) neg
                      FROM reviews
                      WHERE device IS NOT NULL AND device <> ''
                      GROUP BY device HAVING n >= ?
                      ORDER BY 1.0*neg/n DESC, n DESC LIMIT 15""", min_n)
    return [{
        "device": r["device"], "count": r["n"], "avg_rating": r["avg"],
        "negative": r["neg"], "negative_rate": round(r["neg"] / r["n"], 3),
    } for r in rows]


def version_risk(conn, min_n: int = 20) -> list[dict]:
    rows = q(conn, """SELECT app_version_name v, COUNT(*) n,
                             ROUND(AVG(star_rating),2) avg,
                             SUM(CASE WHEN star_rating<=2 THEN 1 ELSE 0 END) neg
                      FROM reviews
                      WHERE app_version_name IS NOT NULL AND app_version_name <> ''
                      GROUP BY v HAVING n >= ?
                      ORDER BY 1.0*neg/n DESC LIMIT 15""", min_n)
    return [{
        "version": r["v"], "count": r["n"], "avg_rating": r["avg"],
        "negative": r["neg"], "negative_rate": round(r["neg"] / r["n"], 3),
    } for r in rows]


def country_market(lang: str | None) -> str:
    """根据 Play CSV 的 Reviewer Language 推断国家/市场。

    CSV 没有国家字段，因此这里输出的是可行动的语言市场代理，不是真实国家。
    对多国共用语言保留“语种市场”称谓，避免制造虚假的国家精度。
    """
    code = (lang or "unknown").lower()
    mapping = {
        "en": "英语市场（国家待补充）", "es": "西语市场（国家待补充）",
        "ar": "阿拉伯语市场（国家待补充）", "vi": "越南",
        "pt": "葡萄牙语市场（国家待补充）", "fr": "法语市场（国家待补充）",
        "ru": "俄罗斯/俄语市场", "tr": "土耳其", "id": "印度尼西亚",
        "fa": "伊朗/波斯语市场", "th": "泰国", "it": "意大利",
        "pl": "波兰", "uk": "乌克兰", "de": "德国/德语市场",
        "ro": "罗马尼亚", "ko": "韩国", "iw": "以色列",
        "zh-hant": "中国台湾/繁体中文市场", "zh-hans": "中国大陆/简体中文市场",
        "hu": "匈牙利", "el": "希腊", "cs": "捷克",
        "mn": "蒙古", "sk": "斯洛伐克", "sr": "塞尔维亚",
        "nl": "荷兰/荷语市场", "uz": "乌兹别克斯坦", "ja": "日本",
        "bn": "孟加拉国", "hr": "克罗地亚", "ms": "马来西亚/马来语市场",
        "sv": "瑞典", "az": "阿塞拜疆", "no": "挪威", "bs": "波黑",
        "my": "缅甸", "lt": "立陶宛", "hi": "印度/印地语市场",
        "sq": "阿尔巴尼亚", "ka": "格鲁吉亚", "da": "丹麦",
        "mk": "北马其顿", "fi": "芬兰", "af": "南非/南非荷兰语市场",
        "lv": "拉脱维亚", "sl": "斯洛文尼亚", "et": "爱沙尼亚",
        "km": "柬埔寨", "fil": "菲律宾", "is": "冰岛", "ca": "加泰罗尼亚语市场",
    }
    return mapping.get(code, f"其他市场（{lang or '未知语言'}）")


def country_dist(conn) -> list[dict]:
    """按语言推断的国家/市场分布；真实国家需接入带 country 字段的数据源。"""
    rows = q(conn, """SELECT reviewer_lang l, COUNT(*) n,
                             ROUND(AVG(star_rating),2) avg,
                             SUM(is_negative) neg
                      FROM reviews_clean GROUP BY reviewer_lang
                      ORDER BY n DESC""")
    grouped: dict[str, dict] = {}
    for r in rows:
        market = country_market(r["l"])
        item = grouped.setdefault(market, {"country": market, "count": 0,
                                           "negative": 0, "weighted_rating": 0,
                                           "languages": []})
        item["count"] += r["n"]
        item["negative"] += r["neg"]
        item["weighted_rating"] += (r["avg"] or 0) * r["n"]
        item["languages"].append(r["l"])
    out = []
    for item in grouped.values():
        item["avg_rating"] = round(item.pop("weighted_rating") / item["count"], 2)
        item["negative_rate"] = round(item["negative"] / item["count"], 3)
        out.append(item)
    return sorted(out, key=lambda x: (-x["negative_rate"], -x["count"]))[:20]


def _hit(text: str, patterns: list[str]) -> bool:
    t = (text or "").lower()
    return any(p in t for p in patterns)


def device_hotspots(conn) -> list[dict]:
    """同设备 + 同版本的聚集性差评，且来自 >=2 位独立作者。

    7 天窗口下单个主题的差评数往往只有 1-3 条，主题阈值会把真正的
    核心路径阻塞滤掉。但"同一台设备、同一个版本、两个不同用户说同一件事"
    是可复现信号，证据强度高于单纯的主题计数，必须单独成项。
    """
    rows = q(conn, """
        SELECT c.device device, COALESCE(c.app_version,'(版本缺失)') ver,
               COUNT(DISTINCT COALESCE(r.author_name, c.review_id)) authors,
               COUNT(*) neg
        FROM reviews_clean c
        JOIN reviews r USING(review_id)
        WHERE c.is_negative = 1
          AND c.device IS NOT NULL AND c.device <> ''
        GROUP BY c.device, ver
        HAVING authors >= 2
        ORDER BY authors DESC, neg DESC LIMIT 10""")
    out = []
    for r in rows:
        samples = q(conn, """
            SELECT c.review_text t FROM reviews_clean c
            JOIN reviews r USING(review_id)
            WHERE c.is_negative = 1 AND c.device = ?
              AND COALESCE(c.app_version,'(版本缺失)') = ?
            LIMIT 3""", r["device"], r["ver"])
        texts = [s["t"] for s in samples if s["t"]]
        blockers = [name for name, pats in CORE_BLOCKERS.items() if any(_hit(t, pats) for t in texts)]
        out.append({
            "device": r["device"], "version": r["ver"],
            "authors": r["authors"], "negative": r["neg"],
            "blockers": blockers,
            "samples": [t[:160] for t in texts],
        })
    return out


def action_items(conn, report_topics: list[dict], countries: list[dict]) -> list[dict]:
    """把统计信号转成可执行行动项，供产品/研发直接认领。

    优先级不看主题排名，只看三件事：是否阻塞核心任务、证据是否可复现
    （独立作者数/同设备同版本聚集）、差评强度和样本是否够。样本不足的
    一律进 verification_pool，不因为差评率 100% 就升 P0。
    """
    owners = {
        "闪退崩溃": ("客户端研发 + QA", "拉取崩溃堆栈，按版本和机型复现；将 P0/P1 集群接入发布门禁", "Crash-free users / 崩溃率"),
        "卡顿性能": ("客户端研发 + 性能专项", "针对高风险机型采集主线程耗时、启动耗时和卡顿 trace，优先做回归测试", "ANR率 / 启动耗时 / 卡顿率"),
        "广告过多": ("商业化产品", "复核高差评市场的广告频次、插屏时机和遮挡面积，先做频控 A/B", "广告相关差评率 / 留存"),
        "视频播放": ("播放器研发 + 内容支持", "按国家/市场、网络和机型拆分播放失败，补充 CDN/编解码降级策略", "播放成功率 / 播放类差评率"),
        "登录账号": ("账号研发", "核对登录失败链路、错误码和验证码成功率，补齐可诊断日志", "登录成功率 / 账号类差评率"),
        "更新安装": ("发布工程 + 客户端研发", "核查受影响版本的升级路径、下载失败和安装包兼容性", "升级成功率 / 更新类差评率"),
        "耗电发热": ("客户端研发", "用 Battery Historian/Profiler 对后台任务、视频和广告 SDK 分段定位", "后台耗电 / 发热类差评率"),
        "网络下载": ("网络/播放器研发", "按市场和运营商验证 DNS、TLS、CDN 与弱网重试策略", "网络失败率 / 下载类差评率"),
        "界面易用": ("产品设计 + 客户端研发", "从代表性差评提炼 3 个最高频操作阻塞，做可用性走查和小流量验证", "任务完成率 / UI类差评率"),
        "付费订阅": ("商业化产品 + 客服", "检查价格、订阅说明、退款入口和扣费争议的引导文案", "付费类差评率 / 退款咨询量"),
    }
    items = []
    for t in report_topics:
        if t["negative_mentions"] < 3:
            continue
        owner, action, metric = owners.get(t["topic"], ("产品负责人", "抽样核查代表性差评并建立问题单", "主题差评率"))
        score = t["negative_mentions"] * (1 + max(0, t["negative_ratio"] - 0.25)) * (1 + max(0, t["mom_vs_baseline"] or 0))
        # 优先级：核心阻塞主题 + 差评占比高 + 样本够，才够格 P0；
        # 3-4 条差评只到 P1，避免 7 天窗口下的低样本把个体问题放大成 P0。
        if t["topic"] in CORE_BLOCKER_TOPICS and t["negative_ratio"] >= 0.5 and t["negative_mentions"] >= 5:
            prio = "P0"
        elif t["topic"] in CORE_BLOCKER_TOPICS or t["negative_ratio"] >= 0.4:
            prio = "P1"
        else:
            prio = "P2"
        low = "" if t["negative_mentions"] >= 5 else "（样本偏低，需 30 天窗口累积再定级）"
        items.append({"priority": prio, "topic": t["topic"], "score": round(score, 1),
                      "evidence": f"{t['negative_mentions']} 条差评提及，差评占比 {t['negative_ratio']:.1%}{low}",
                      "owner": owner, "action": action, "verify": metric})

    # 同设备 + 同版本 + 多位独立作者的聚集性差评：可复现信号，单独成项。
    for h in device_hotspots(conn):
        blocker_note = "、".join(h["blockers"]) if h["blockers"] else "未命中核心阻塞关键词"
        prio = "P1" if h["blockers"] else "P2"
        sample = h["samples"][0] if h["samples"] else ""
        items.append({
            "priority": prio, "topic": f"{h['device']} / {h['version']} 聚集性差评",
            "score": round(h["authors"] * (2 if h["blockers"] else 1), 1),
            "evidence": f"{h['authors']} 位独立作者、{h['negative']} 条差评集中在同设备同版本；疑似阻塞：{blocker_note}；样例：{sample}",
            "owner": "客户端研发 + QA" if h["blockers"] else "产品负责人",
            "action": "按该设备与版本复现问题，核对适配层（输入/焦点/渲染）与发布通道，确认是否为版本回归",
            "verify": "该设备版本的差评率 / 崩溃 ANR 率 / 复现结论",
        })
    for c in countries[:3]:
        if c["negative_rate"] >= 0.2 and c["count"] >= 30:
            items.append({"priority": "P1", "topic": f"{c['country']}专项",
                          "score": round(c["negative_rate"] * 100, 1),
                          "evidence": f"{c['count']} 条有效语料，差评率 {c['negative_rate']:.1%}，语言代码：{','.join(c['languages'])}",
                          "owner": "本地化/区域运营 + 产品", "action": "抽取该市场差评原文做人工复核，确认翻译、广告、网络或内容供给的区域性原因",
                          "verify": "该市场差评率 / 评分"})
    return sorted(items, key=lambda x: (x["priority"], -x["score"]))


def lang_dist(conn) -> list[dict]:
    rows = q(conn, """SELECT lang_base l, COUNT(*) n,
                             ROUND(AVG(star_rating),2) avg,
                             SUM(is_negative) neg
                      FROM reviews_clean GROUP BY l
                      ORDER BY n DESC LIMIT 15""")
    return [{
        "lang": r["l"], "count": r["n"], "avg_rating": r["avg"],
        "negative": r["neg"], "negative_rate": round(r["neg"] / r["n"], 3),
        "country_inferred": country_market(r["l"]),
    } for r in rows]


def spam_signals(conn) -> dict:
    """重复文本 TOP。高频重复往往是刷评或模板化投诉，两种都值得单独看。"""
    rows = q(conn, """SELECT norm_text, COUNT(*) n,
                             ROUND(AVG(star_rating),2) avg,
                             MIN(review_text) sample
                      FROM reviews_clean
                      GROUP BY norm_text HAVING n > 2
                      ORDER BY n DESC LIMIT 12""")
    return {
        "duplicate_clusters": [{
            "count": r["n"], "avg_rating": r["avg"], "sample": (r["sample"] or "")[:120],
        } for r in rows]
    }


def negative_backlog(conn, limit: int = 40) -> list[dict]:
    """待 LLM 深入分析的差评队列：优先长文本 + 含技术线索的差评。
    这是控制 LLM 成本的关键——只把信息密度最高的评论送去打标。"""
    rows = q(conn, """SELECT review_id, star_rating, lang_base, device,
                             app_version, submitted_at, review_text, text_len
                      FROM reviews_clean
                      WHERE is_negative = 1
                      ORDER BY has_tech_hint DESC, text_len DESC
                      LIMIT ?""", limit)
    return [dict(r) for r in rows]


def main() -> int:
    ap = argparse.ArgumentParser(description="确定性分析（第 3 步 A 部分）")
    ap.add_argument("--db", default="reviews.db")
    ap.add_argument("--out", default="report.json")
    ap.add_argument("--package", default="", help="应用包名，写入报告元数据")
    args = ap.parse_args()

    conn = sqlite3.connect(args.db)
    conn.row_factory = sqlite3.Row

    topics = topic_hits(conn)
    countries = country_dist(conn)
    report = {
        "meta": {
            "package": args.package,
            "complete_months": _covered_months(conn),
            "note": "环比仅基于完整月计算，残缺月已排除",
            "country_note": "CSV 不含国家字段；国家/市场为根据 Reviewer Language 推断的市场代理，不代表真实国家归属",
        },
        "overall": overall(conn),
        "monthly_trend": monthly_trend(conn),
        "topics": topics,
        "device_risk": device_risk(conn),
        "version_risk": version_risk(conn),
        "lang_dist": lang_dist(conn),
        "country_dist": countries,
        "action_items": action_items(conn, topics, countries),
        "verification_pool": verification_pool(topics),
        "device_hotspots": device_hotspots(conn),
        "spam": spam_signals(conn),
        "llm_backlog": negative_backlog(conn),
    }
    conn.close()

    with open(args.out, "w", encoding="utf-8") as fh:
        json.dump(report, fh, ensure_ascii=False, indent=2)

    o = report["overall"]
    print(f"总评论 {o['raw_total']}  均分 {o['raw_avg_rating']}  "
          f"差评率 {o['raw_negative_rate']:.2%}")
    print(f"有正文 {o['text_total']}  其中差评 {o['text_negative']}  "
          f"含技术线索 {o['text_with_tech_hint']}")
    print("\n主题排行（按差评提及数）")
    print(f"  {'主题':<10} {'提及':>5} {'差评':>5} {'差评占比':>8} {'环比':>8}")
    for t in report["topics"]:
        mom = f"{t['mom_vs_baseline']:+.0%}" if t["mom_vs_baseline"] is not None else "-"
        print(f"  {t['topic']:<10} {t['mentions']:>5} {t['negative_mentions']:>5} "
              f"{t['negative_ratio']:>7.1%} {mom:>8}")
    print("\n高差评率机型 TOP5")
    for d in report["device_risk"][:5]:
        print(f"  {d['device']:<18} n={d['count']:<5} 均分={d['avg_rating']:<5} "
              f"差评率={d['negative_rate']:.1%}")
    print(f"\n已写出 {args.out}（含 {len(report['llm_backlog'])} 条待 LLM 分析队列）")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
