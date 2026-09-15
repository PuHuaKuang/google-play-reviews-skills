"""第 2 步：预处理。把原始评论库清洗成可分析的语料。

为什么需要单独一步：应用商店评论中常有大量纯星级评分，只有部分记录带正文。
纯星级对趋势统计有用（算平均分、算差评率），
但对语义分析毫无价值，直接喂给 LLM 是纯烧钱。所以这一步做三件事：

1. 筛出有正文的评论，写入 reviews_clean 表
2. 近似去重：同一用户重复提交、模板化刷评、表情符号灌水
3. 补充分析字段：文本长度、是否含技术线索、月份桶、语言归一

用法：
  python preprocess.py --db reviews.db
  python preprocess.py --db reviews.db --min-len 3   # 放宽短评论门槛
"""

from __future__ import annotations

import argparse
import re
import sqlite3
import unicodedata
from collections import Counter

CLEAN_DDL = """
CREATE TABLE IF NOT EXISTS reviews_clean (
    review_id      TEXT PRIMARY KEY,
    star_rating    INTEGER,
    reviewer_lang  TEXT,
    lang_base      TEXT,
    device         TEXT,
    app_version    TEXT,
    submitted_at   TEXT,
    month_bucket   TEXT,
    review_text    TEXT,
    norm_text      TEXT,
    text_len       INTEGER,
    dup_group      TEXT,
    is_negative    INTEGER,
    has_tech_hint  INTEGER
);
CREATE INDEX IF NOT EXISTS idx_clean_month ON reviews_clean(month_bucket);
CREATE INDEX IF NOT EXISTS idx_clean_neg ON reviews_clean(is_negative);
CREATE INDEX IF NOT EXISTS idx_clean_lang ON reviews_clean(lang_base);
"""

# 技术线索词：命中说明评论描述了具体故障，优先级高于泛泛的"不好用"。
# 覆盖多语言是刻意的——海外应用的差评里英语占比往往不到 25%。
TECH_HINTS = [
    # 崩溃/闪退
    "crash", "crashes", "crashing", "closes", "close by itself", "shuts down",
    "se cierra", "se cierra sola", "fecha sozinho", "plante", "se ferme",
    "падает", "закрывается", "kapanıyor", "يغلق", "تتوقف", "bị tắt", "tự tắt",
    "keluar sendiri", "tertutup",
    # 卡顿/慢
    "slow", "lag", "laggy", "freeze", "frozen", "stuck", "hang", "not responding",
    "lento", "lenta", "trava", "travando", "ralenti", "медленно", "зависает",
    "yavaş", "donuyor", "بطيء", "chậm", "treo", "lambat",
    # 广播/广告
    "ads", "advertisement", "pop-up", "popup", "anuncios", "publicidad",
    "propaganda", "publicité", "реклама", "reklam", "إعلانات", "quảng cáo",
    "iklan",
    # 播放/视频
    "buffering", "no sound", "black screen", "cannot play", "won't play",
    "no se reproduce", "sin sonido", "pantalla negra", "не воспроизводит",
    "нет звука", "ses yok", "siyah ekran", "لا يعمل", "không phát", "không có tiếng",
    # 登录/账号
    "login", "log in", "sign in", "account", "password", "no puedo iniciar",
    "не могу войти", "giriş yapamıyorum", "لا يمكنني تسجيل",
    # 更新/安装
    "update", "install", "download", "actualización", "не обновляется",
    "güncelleme", "تحديث", "cập nhật",
    # 耗电/发热
    "battery", "drain", "overheat", "hot", "batería", "batterie", "батарея",
    "pil", "بطارية", "pin", "nóng",
]
TECH_RE = re.compile("|".join(re.escape(w) for w in TECH_HINTS), re.IGNORECASE)

EMOJI_RE = re.compile(
    "[" "\U0001F300-\U0001FAFF" "\U00002600-\U000027BF" "\U0001F1E6-\U0001F1FF" "]+"
)
URL_RE = re.compile(r"https?://\S+|www\.\S+")
REPEAT_RE = re.compile(r"(.)\1{2,}")
WS_RE = re.compile(r"\s+")


def normalize(text: str) -> str:
    """归一化用于近似去重比对，不用于展示。

    做四件事：Unicode 归一（全角转半角等）、去 URL、压缩重复字符
    （"gooooood" -> "good"，刷评常见）、去标点与表情。
    """
    t = unicodedata.normalize("NFKC", text or "").lower()
    t = URL_RE.sub(" ", t)
    t = EMOJI_RE.sub(" ", t)
    t = REPEAT_RE.sub(r"\1", t)
    t = "".join(ch if (ch.isalnum() or ch.isspace()) else " " for ch in t)
    return WS_RE.sub(" ", t).strip()


def lang_base(code: str | None) -> str:
    """zh-Hant / pt-BR 归一到主语种，便于按语种聚合。"""
    if not code:
        return "unknown"
    return code.split("-")[0].lower()


def month_of(ts: str | None) -> str:
    return (ts or "")[:7] or "unknown"


def is_meaningful(norm: str, min_len: int) -> bool:
    """过滤无信息量的正文。

    纯表情、纯"good"/"ok"这类单词经归一后长度极短，
    留着只会污染聚类结果。注意 CJK 无空格，按字符数判断更合适。
    """
    if not norm:
        return False
    if len(norm) < min_len:
        return False
    # 纯数字或单字符重复
    if norm.replace(" ", "").isdigit():
        return False
    return True


def run(db: str, min_len: int) -> dict:
    conn = sqlite3.connect(db)
    conn.row_factory = sqlite3.Row
    conn.executescript(CLEAN_DDL)

    rows = conn.execute(
        """SELECT review_id, star_rating, reviewer_lang, device,
                  app_version_name, app_version_code, submitted_at, review_text
           FROM reviews
           WHERE review_text IS NOT NULL AND TRIM(review_text) <> ''"""
    ).fetchall()

    seen: dict[str, str] = {}  # norm_text -> 首个 review_id，作为 dup_group
    stats = Counter()
    payload = []

    for r in rows:
        raw = (r["review_text"] or "").strip()
        norm = normalize(raw)
        stats["with_text"] += 1

        if not is_meaningful(norm, min_len):
            stats["dropped_noise"] += 1
            continue

        # 近似去重：归一后完全相同视为同组，保留全部但标记组 ID，
        # 这样既能统计"某条模板差评被刷了多少次"，又能在聚类时只取组代表。
        group = seen.setdefault(norm, r["review_id"])
        if group != r["review_id"]:
            stats["dup"] += 1

        version = r["app_version_name"] or (
            str(r["app_version_code"]) if r["app_version_code"] else None
        )
        neg = 1 if (r["star_rating"] or 0) and r["star_rating"] <= 2 else 0
        tech = 1 if TECH_RE.search(raw) else 0
        stats["negative"] += neg
        stats["tech_hint"] += tech

        payload.append(
            (
                r["review_id"], r["star_rating"], r["reviewer_lang"],
                lang_base(r["reviewer_lang"]), r["device"], version,
                r["submitted_at"], month_of(r["submitted_at"]),
                raw, norm, len(norm), group, neg, tech,
            )
        )

    conn.execute("DELETE FROM reviews_clean")
    conn.executemany(
        """INSERT INTO reviews_clean
           (review_id, star_rating, reviewer_lang, lang_base, device, app_version,
            submitted_at, month_bucket, review_text, norm_text, text_len,
            dup_group, is_negative, has_tech_hint)
           VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
        payload,
    )
    conn.commit()
    stats["kept"] = len(payload)
    stats["unique_after_dedupe"] = len(seen)
    conn.close()
    return dict(stats)


def main() -> int:
    ap = argparse.ArgumentParser(description="预处理评论语料（第 2 步）")
    ap.add_argument("--db", default="reviews.db")
    ap.add_argument("--min-len", type=int, default=4, help="归一后最小字符数")
    args = ap.parse_args()

    s = run(args.db, args.min_len)
    print(f"有正文评论      : {s.get('with_text', 0)}")
    print(f"过滤无信息量    : {s.get('dropped_noise', 0)}")
    print(f"入库            : {s.get('kept', 0)}")
    print(f"其中重复文本    : {s.get('dup', 0)}（去重后独立文本 {s.get('unique_after_dedupe', 0)}）")
    print(f"差评(<=2星)     : {s.get('negative', 0)}")
    print(f"含技术线索      : {s.get('tech_hint', 0)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
