"""离线自检：用模拟数据验证解析与落库逻辑，不需要真实 Play 凭证。

跑这个脚本可以确认：
  - API 响应结构的拍平逻辑正确
  - CSV（UTF-16）解码与列名兜底正确
  - review_id 去重与「修改后覆盖」逻辑正确
"""

from __future__ import annotations

import io
import tempfile
from pathlib import Path

import fetch_api
import fetch_gcs
import storage

API_SAMPLE = {
    "reviews": [
        {
            "reviewId": "gp:AOqpTOaaa111",
            "authorName": "张三",
            "comments": [
                {
                    "userComment": {
                        "text": "更新后一直闪退，打不开了",
                        "lastModified": {"seconds": "1755000000", "nanos": 0},
                        "starRating": 1,
                        "reviewerLanguage": "zh-CN",
                        "device": "OP5929L1",
                        "androidOsVersion": 34,
                        "appVersionCode": 812,
                        "appVersionName": "8.1.2",
                        "deviceMetadata": {"productName": "OnePlus Ace 3"},
                    }
                },
                {
                    "developerComment": {
                        "text": "已定位问题，8.1.3 修复",
                        "lastModified": {"seconds": "1755100000", "nanos": 0},
                    }
                },
            ],
        },
        {
            "reviewId": "gp:AOqpTObbb222",
            "authorName": "Priya",
            "comments": [
                {
                    "userComment": {
                        "text": "Battery drain is terrible after update",
                        "lastModified": {"seconds": "1755200000", "nanos": 0},
                        "starRating": 2,
                        "reviewerLanguage": "en",
                        "androidOsVersion": 33,
                        "appVersionCode": 812,
                        "appVersionName": "8.1.2",
                        "deviceMetadata": {"productName": "Pixel 7a"},
                    }
                }
            ],
        },
        {
            "reviewId": "gp:AOqpTOccc333",
            "authorName": "只打星不写字",
            "comments": [{"userComment": {"starRating": 5, "lastModified": {"seconds": "1755300000"}}}],
        },
    ]
}

CSV_HEADER = (
    "Package Name,App Version Code,Reviewer Language,Device,"
    "Review Submit Date and Time,Review Submit Millis Since Epoch,"
    "Review Last Update Date and Time,Review Last Update Millis Since Epoch,"
    "Star Rating,Review Title,Review Text,Developer Reply Date and Time,"
    "Developer Reply Text,Review Link\n"
)
CSV_ROWS = (
    "com.example.app,780,zh-CN,Redmi Note 12,2026-03-02T04:11:00Z,1740000000000,"
    "2026-03-02T04:11:00Z,1740000000000,1,登录失败,一直提示网络错误无法登录,,,"
    "https://play.google.com/console/review/hist001\n"
    "com.example.app,795,en,Galaxy A54,2026-04-18T09:20:00Z,1744000000000,"
    "2026-04-18T09:20:00Z,1744000000000,4,Good,Works well but slow on startup,,,"
    "https://play.google.com/console/review/hist002\n"
)


def test_api_flatten() -> None:
    rows = [r for r in (fetch_api.flatten(x) for x in API_SAMPLE["reviews"]) if r]
    assert len(rows) == 3, f"应拍平 3 条，实际 {len(rows)}"

    first = rows[0]
    assert first["review_id"] == "gp:AOqpTOaaa111"
    assert first["star_rating"] == 1
    assert first["device"] == "OnePlus Ace 3", "应优先取 deviceMetadata.productName"
    assert first["dev_reply_text"] == "已定位问题，8.1.3 修复"
    assert first["submitted_at"].startswith("2025-"), first["submitted_at"]

    third = rows[2]
    assert third["review_text"] == "", "纯星级评论正文应为空字符串而非 None"
    print("  [ok] API 响应拍平：3 条，含开发者回复与设备名解析")


def test_csv_utf16() -> None:
    raw = (CSV_HEADER + CSV_ROWS).encode("utf-16")
    text = fetch_gcs.decode_csv(raw)
    rows = list(fetch_gcs.parse_rows(text, "com.example.app"))
    assert len(rows) == 2, f"应解析 2 条，实际 {len(rows)}"
    assert rows[0]["star_rating"] == 1
    assert rows[0]["review_text"] == "登录失败 一直提示网络错误无法登录"
    assert rows[0]["device"] == "Redmi Note 12"
    assert rows[0]["source"] == "gcs_report"
    print("  [ok] UTF-16 CSV 解码与列名兜底：2 条，标题与正文已拼接")


def test_dedupe_and_update() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        db = Path(tmp) / "t.db"
        with storage.connect(db) as conn:
            api_rows = [r for r in (fetch_api.flatten(x) for x in API_SAMPLE["reviews"]) if r]
            n1 = storage.upsert_reviews(conn, api_rows)
            assert n1 == 3, n1

            # 同批数据重放，last_modified 未变 -> 不应新增
            n2 = storage.upsert_reviews(conn, api_rows)
            total = conn.execute("SELECT COUNT(*) FROM reviews").fetchone()[0]
            assert total == 3, f"重放后应仍为 3 条，实际 {total}"

            # 用户修改了评论：last_modified 变新 -> 应覆盖正文
            edited = dict(api_rows[0])
            edited["review_text"] = "8.1.3 修好了，改成五星"
            edited["star_rating"] = 5
            edited["last_modified"] = "2026-08-01T00:00:00+00:00"
            storage.upsert_reviews(conn, [edited])

            row = conn.execute(
                "SELECT review_text, star_rating FROM reviews WHERE review_id = ?",
                ("gp:AOqpTOaaa111",),
            ).fetchone()
            assert row["star_rating"] == 5, "修改后的评论应被覆盖更新"
            assert "改成五星" in row["review_text"]

            csv_rows = list(
                fetch_gcs.parse_rows(
                    fetch_gcs.decode_csv((CSV_HEADER + CSV_ROWS).encode("utf-16")),
                    "com.example.app",
                )
            )
            storage.upsert_reviews(conn, csv_rows)
            s = storage.stats(conn)
            assert s["total"] == 5, s
            assert s["by_source"] == {"gcs_report": 2, "reviews_api": 3}, s["by_source"]
            print(f"  [ok] 去重与覆盖更新：总 {s['total']} 条，来源分布 {s['by_source']}")
            print(f"  [ok] 负面评论(<=2星) {s['negative']} 条，均分 {s['avg_rating']}")


def test_local_csv_mode() -> None:
    """零权限兜底：手动下载的 CSV 目录能被正确识别（含被重命名的文件）。"""
    with tempfile.TemporaryDirectory() as tmp:
        d = Path(tmp)
        raw = (CSV_HEADER + CSV_ROWS).encode("utf-16")
        (d / "reviews_com.example.app_202603.csv").write_bytes(raw)
        (d / "reviews_com.other.app_202603.csv").write_bytes(raw)
        (d / "notes.txt").write_text("ignore me", encoding="utf-8")

        items = fetch_gcs.list_local_csv(str(d), "com.example.app", None)
        assert len(items) == 1, f"应只匹配本包 1 个文件，实际 {len(items)}"
        assert items[0][0] == "202603", items[0][0]

        # since 过滤生效
        assert fetch_gcs.list_local_csv(str(d), "com.example.app", "202604") == []

        # 文件被用户重命名过时，降级接受目录下所有 CSV
        d2 = Path(tmp) / "renamed"
        d2.mkdir()
        (d2 / "我下载的评论.csv").write_bytes(raw)
        fb = fetch_gcs.list_local_csv(str(d2), "com.example.app", None)
        assert len(fb) == 1 and fb[0][0] == "local", fb

        text = fetch_gcs.decode_csv(fetch_gcs._read_local(items[0][1]))
        rows = list(fetch_gcs.parse_rows(text, "com.example.app"))
        assert len(rows) == 2, rows
        print(f"  [ok] 本地 CSV 兜底模式：匹配 1 个文件，解析 {len(rows)} 条，重命名降级正常")


def test_real_filename_variants() -> None:
    """回归：Play Console 手动下载的真实文件名有两处与文档不同——
    前缀是 reviews_reviews_（重复一次），浏览器重复下载还会加 " (3)" 后缀。
    这两点都曾导致一个文件都匹配不上。"""
    cases = [
        ("reviews_reviews_com.example.reader_202601 (3).csv", "com.example.reader", "202601"),
        ("reviews_reviews_com.example.reader_202604.csv", "com.example.reader", "202604"),
        ("reviews_com.example.app_202401.csv", "com.example.app", "202401"),
    ]
    for name, pkg, ym in cases:
        m = fetch_gcs.FNAME_RE.search(name)
        assert m, f"未能识别文件名：{name}"
        assert m.group("pkg") == pkg, (name, m.group("pkg"))
        assert m.group("ym") == ym, (name, m.group("ym"))
    assert not fetch_gcs.FNAME_RE.search("random.csv")
    print(f"  [ok] 真实文件名变体识别：{len(cases)} 种命名全部通过")


def test_synthetic_id_is_stable() -> None:
    """回归：纯星级评论（无 Review Link，实测占 89%）依赖合成 ID 去重。
    早期用 Python 内置 hash()，带进程级随机盐，每次运行 ID 都不同，
    重跑会把整库再插一遍。这里验证同输入必得同 ID。"""
    header = (
        "Package Name,App Version Code,App Version Name,Reviewer Language,Device,"
        "Review Submit Date and Time,Review Submit Millis Since Epoch,"
        "Review Last Update Date and Time,Review Last Update Millis Since Epoch,"
        "Star Rating,Review Title,Review Text,Developer Reply Date and Time,"
        "Developer Reply Millis Since Epoch,Developer Reply Text,Review Link"
    )
    body = "com.example.app,,,ar,G07,2026-01-01T00:02:48Z,1767225768053,2026-01-01T00:02:48Z,1767225768053,5,,,,,,"
    text = header + "\r\n" + body + "\r\n"

    ids = []
    for _ in range(2):
        rows = list(fetch_gcs.parse_rows(text, "com.example.app"))
        assert len(rows) == 1, rows
        assert rows[0]["review_id"].startswith("syn:"), rows[0]["review_id"]
        ids.append(rows[0]["review_id"])
    assert ids[0] == ids[1], f"合成 ID 不稳定：{ids}"

    # 幂等性：同一份数据入库两次，第二次写入应为 0
    with tempfile.TemporaryDirectory() as tmp:
        db = str(Path(tmp) / "t.db")
        with storage.connect(db) as conn:
            first = storage.upsert_reviews(conn, list(fetch_gcs.parse_rows(text, "com.example.app")))
            second = storage.upsert_reviews(conn, list(fetch_gcs.parse_rows(text, "com.example.app")))
            assert first == 1 and second == 0, (first, second)
            assert storage.stats(conn)["total"] == 1
    print("  [ok] 无正文评论合成 ID 稳定，重复入库幂等")


def test_partial_month_excluded() -> None:
    """回归：手动下载的当月 CSV 通常只到下载日。若拿残缺月直接算环比，
    所有主题都会显示为下降，制造"问题都在好转"的假象。"""
    import sqlite3

    import analyze

    tmp = tempfile.mkdtemp()
    db = str(Path(tmp) / "t.db")
    try:
        with storage.connect(db) as conn:
            pass
        conn = sqlite3.connect(db)
        conn.row_factory = sqlite3.Row
        # 三个完整月 + 一个只到 22 日的残缺月，每月 300 条以上
        rows = []
        for month, last_day in [("01", 31), ("02", 28), ("03", 31), ("04", 22)]:
            for i in range(300):
                day = f"{min(i % last_day + 1, last_day):02d}"
                rows.append((f"r{month}{i}", 5, f"2026-{month}-{day}T00:00:00Z"))
        conn.executemany(
            """INSERT INTO reviews (review_id, star_rating, submitted_at, source, fetched_at)
               VALUES (?,?,?,'gcs_report','2026-08-28T00:00:00Z')""",
            rows,
        )
        conn.commit()

        complete = analyze._covered_months(conn)
        assert complete == ["2026-01", "2026-02", "2026-03"], complete

        trend = analyze.monthly_trend(conn)
        tail = [t for t in trend if t["month"] == "2026-04"][0]
        assert tail["partial"] is True and tail["covered_days"] == 22, tail

        # 提及数逐月持平时，环比应约为 0，而不是因残缺月被拉成负数
        by_month = {"2026-01": 40, "2026-02": 40, "2026-03": 40, "2026-04": 25}
        mom = analyze._momentum(by_month, complete)
        assert mom == 0.0, f"完整月持平时环比应为 0，实际 {mom}"
        conn.close()
    finally:
        # Windows 下 SQLite 连接未关时无法删除文件，忽略清理失败
        import shutil

        shutil.rmtree(tmp, ignore_errors=True)
    print("  [ok] 残缺月已排除，环比不再被下载日截断污染")


if __name__ == "__main__":
    print("离线自检开始（无需 Play 凭证）")
    test_api_flatten()
    test_csv_utf16()
    test_dedupe_and_update()
    test_local_csv_mode()
    test_real_filename_variants()
    test_synthetic_id_is_stable()
    test_partial_month_excluded()
    print("全部通过")
