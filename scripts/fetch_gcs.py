"""通道 B：从 Play Console 的 Cloud Storage 桶回溯全部历史评论。

为什么需要它：Reviews API 只给近 7 天。Play Console 会把评论按月导出成 CSV，
存在一个专属 GCS 桶里，覆盖应用全生命周期。首次搭建流水线时用它灌一次基线，
之后交给通道 A 做增量。

获取桶地址：
  Play Console -> 下载报告 -> 评价 -> 右上角「复制 Cloud Storage URI」
  形如 gs://pubsite_prod_rev_01234567890123456789/reviews

权限：给 Service Account 在该 GCP 项目授予 Storage Object Viewer，
      同时 Play Console 侧需有「查看应用信息并下载批量报告」权限。

文件命名规律：reviews/reviews_<package>_YYYYMM.csv
CSV 为 UTF-16LE 编码带 BOM（Google 的历史遗留），必须显式指定解码方式，
否则会读成乱码——这是最容易踩的坑。

依赖：pip install google-cloud-storage
用法：
  python fetch_gcs.py --bucket pubsite_prod_rev_0123456789 \
      --package com.example.app --since 202401

零权限兜底（--local-csv）：
  如果暂时拿不到任何 API/GCS 权限（例如你不是 Play Console 账号所有者），
  可以在 Play Console 界面手动下载评论 CSV，再用本地目录模式入库，
  解码与去重逻辑与自动通道完全一致：
    python fetch_gcs.py --local-csv ./downloads --package com.example.app
  这条路不需要 Service Account、不需要 GCP 项目、不需要链接任何东西。
"""

from __future__ import annotations

import argparse
import csv
import hashlib
import io
import os
import re
from datetime import datetime, timezone
from typing import Any, Iterator

import storage

SOURCE = "gcs_report"
# Play Console 手动下载得到的文件名实际是 reviews_reviews_<pkg>_YYYYMM.csv
# （"reviews_" 前缀出现两次，GCS 桶内则只有一次），且浏览器重复下载会追加
# " (1)"、" (2)" 这类后缀。三种情况都要能识别，否则一个文件都匹配不上。
FNAME_RE = re.compile(
    r"reviews_(?:reviews_)?(?P<pkg>.+?)_(?P<ym>\d{6})(?:\s*\(\d+\))?\.csv$"
)


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def decode_csv(raw: bytes) -> str:
    """Play 的评论 CSV 是 UTF-16 带 BOM，少数新报告为 UTF-8，做兼容探测。"""
    if raw[:2] in (b"\xff\xfe", b"\xfe\xff"):
        return raw.decode("utf-16")
    try:
        return raw.decode("utf-8-sig")
    except UnicodeDecodeError:
        return raw.decode("utf-16", errors="replace")


def _pick(row: dict[str, str], *candidates: str) -> str | None:
    """报告列名随时间变化过，做多别名兜底。"""
    for key in candidates:
        for actual in row:
            if actual and actual.strip().lower() == key.lower():
                value = (row[actual] or "").strip()
                return value or None
    return None


def parse_rows(text: str, package: str) -> Iterator[dict[str, Any]]:
    """把一个月的 CSV 解析成统一记录格式。

    newline="" 是必须的：评论正文里含真实换行符，交给 csv 模块自己处理引号内
    的换行，否则一条多行评论会被切成多行残缺记录。
    """
    reader = csv.DictReader(io.StringIO(text, newline=""))
    for row in reader:
        # 同一目录混放多个应用的报告时，按包名过滤，避免串数据
        row_pkg = _pick(row, "Package Name")
        if row_pkg and row_pkg != package:
            continue
        review_id = _pick(row, "Review Link", "Review ID")
        submitted = _pick(row, "Review Submit Date and Time", "Review Submit Millis Since Epoch")
        modified = _pick(row, "Review Last Update Date and Time", "Review Last Update Millis Since Epoch")
        text_body = " ".join(
            filter(None, [_pick(row, "Review Title"), _pick(row, "Review Text")])
        ).strip()
        rating = _pick(row, "Star Rating")
        submit_millis = _pick(row, "Review Submit Millis Since Epoch")
        version_code = _pick(row, "App Version Code")
        version_name = _pick(row, "App Version Name")
        if not review_id:
            # 纯星级评论（无正文）不带 Review Link，实测占比约 89%。
            # 必须用稳定哈希：Python 内置 hash() 对 str 带进程级随机盐，
            # 每次运行结果不同，会导致同一条评论被反复插入。
            fingerprint = "|".join(
                [package, submit_millis or submitted or "", rating or "",
                 _pick(row, "Device") or "", _pick(row, "Reviewer Language") or "",
                 text_body]
            )
            digest = hashlib.sha1(fingerprint.encode("utf-8")).hexdigest()[:16]
            review_id = f"syn:{package}:{digest}"

        yield {
            "review_id": review_id,
            "author_name": None,  # 批量报告不含作者名
            "star_rating": int(rating) if rating and rating.isdigit() else None,
            "review_text": text_body,
            "reviewer_lang": _pick(row, "Reviewer Language"),
            "device": _pick(row, "Device"),
            "android_version": None,
            "app_version_code": int(version_code) if version_code and version_code.isdigit() else None,
            "app_version_name": version_name,
            "submitted_at": submitted,
            "last_modified": modified or submitted,
            "dev_reply_text": _pick(row, "Developer Reply Text"),
            "dev_replied_at": _pick(row, "Developer Reply Date and Time"),
            "source": SOURCE,
            "fetched_at": _now_iso(),
        }


def list_report_blobs(bucket_name: str, package: str, since: str | None):
    """列出目标包名的月度报告，按年月排序。"""
    from google.cloud import storage as gcs

    client = gcs.Client()
    bucket = client.bucket(bucket_name)
    picked = []
    for blob in client.list_blobs(bucket, prefix="reviews/"):
        m = FNAME_RE.search(blob.name)
        if not m or m.group("pkg") != package:
            continue
        if since and m.group("ym") < since:
            continue
        picked.append((m.group("ym"), blob))
    return sorted(picked, key=lambda x: x[0])


def list_local_csv(dir_path: str, package: str, since: str | None):
    """零权限兜底：扫描手动下载到本地的评论 CSV。

    文件名保持 Play Console 下载时的原名即可（reviews_<package>_YYYYMM.csv）；
    若被重命名过，则接受目录下所有 .csv，年月标记为 'local'。
    """
    matched, fallback = [], []
    recognized = False  # 是否出现过符合官方命名且属于本包的文件
    for name in sorted(os.listdir(dir_path)):
        if not name.lower().endswith(".csv"):
            continue
        full = os.path.join(dir_path, name)
        m = FNAME_RE.search(name)
        if m:
            # 官方命名的文件：不属于本包就直接丢弃，不能进兜底
            if m.group("pkg") != package:
                continue
            recognized = True
            if since and m.group("ym") < since:
                continue
            matched.append((m.group("ym"), full))
        else:
            fallback.append(("local", full))
    if matched:
        return sorted(matched, key=lambda x: x[0])
    # 只有在完全没识别出官方命名文件时才降级；否则说明是 since 过滤掉了，应返回空
    return [] if recognized else fallback


def _read_local(path: str) -> bytes:
    with open(path, "rb") as fh:
        return fh.read()


def main() -> int:
    ap = argparse.ArgumentParser(description="回溯 Play 历史评论（通道 B）")
    ap.add_argument("--db", default="reviews.db")
    ap.add_argument("--bucket", help="不含 gs:// 前缀的桶名（自动模式）")
    ap.add_argument("--local-csv", help="手动下载的 CSV 所在目录（零权限兜底模式）")
    ap.add_argument("--package", default=os.environ.get("GOOGLE_PLAY_PACKAGE_NAME"))
    ap.add_argument("--since", help="起始年月，如 202401，留空则全量")
    args = ap.parse_args()

    if not args.package:
        ap.error("需要 --package 或环境变量 GOOGLE_PLAY_PACKAGE_NAME")
    if bool(args.bucket) == bool(args.local_csv):
        ap.error("--bucket 与 --local-csv 二选一")

    if args.local_csv:
        items = list_local_csv(args.local_csv, args.package, args.since)
        loader = _read_local
        hint = "请确认目录下存在 Play Console 导出的评论 CSV"
    else:
        items = list_report_blobs(args.bucket, args.package, args.since)
        loader = lambda blob: blob.download_as_bytes()
        hint = "请检查桶名与包名"

    if not items:
        print(f"未匹配到任何报告文件，{hint}")
        return 1

    total_seen = total_kept = 0
    with storage.connect(args.db) as conn:
        log_id = storage.start_sync(conn, SOURCE, _now_iso())
        try:
            for ym, item in items:
                text = decode_csv(loader(item))
                rows = list(parse_rows(text, args.package))
                kept = storage.upsert_reviews(conn, rows)
                total_seen += len(rows)
                total_kept += kept
                print(f"  {ym}: 解析 {len(rows):>5} 条，写入 {kept:>5} 条")
            storage.finish_sync(conn, log_id, _now_iso(), total_seen, total_kept, "ok")
        except Exception as exc:
            storage.finish_sync(
                conn, log_id, _now_iso(), total_seen, total_kept, "failed", str(exc)
            )
            raise

        print(f"通道 B 完成：共 {total_seen} 条，写入/更新 {total_kept} 条")
        print("评论库概览：", storage.stats(conn))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
