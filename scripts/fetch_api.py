"""通道 A：通过 Google Play Developer API 增量拉取评论。

前置条件（在 Play Console / GCP 完成，见 README）：
  1. GCP 项目启用 androidpublisher.googleapis.com
  2. 创建 Service Account 并下载 JSON key
  3. Play Console -> 用户和权限，邀请该 SA 邮箱，勾选
     「查看应用信息」+「回复评论」
  4. 凭证二选一（优先级：--key > 环境变量）：
       --key <sa.json 绝对路径>
       GOOGLE_APPLICATION_CREDENTIALS=<sa.json 绝对路径>
     包名二选一：--package 或 GOOGLE_PLAY_PACKAGE_NAME

关键限制：reviews.list 只返回最近约 7 天内提交或被修改的评论。
因此该脚本必须每天定时跑，历史数据靠通道 B（GCS 报告）回溯。

依赖：pip install google-api-python-client google-auth

用法：
  python fetch_api.py --package com.example.app --db reviews.db
  python fetch_api.py --key /secure/sa.json --package com.example.app --db reviews.db

调试顺序：先跑 verify_credentials.py 确认凭证链路，再跑本脚本，最后接定时任务。
"""

from __future__ import annotations

import argparse
import os
import time
from datetime import datetime, timezone
from typing import Any, Iterator

import storage

SCOPES = ["https://www.googleapis.com/auth/androidpublisher"]
SOURCE = "reviews_api"


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _ts_to_iso(ts: dict[str, Any] | None) -> str | None:
    """Play API 的时间戳形如 {'seconds': '1712900000', 'nanos': 0}。"""
    if not ts or "seconds" not in ts:
        return None
    return datetime.fromtimestamp(int(ts["seconds"]), tz=timezone.utc).isoformat()


def build_service(key_path: str | None = None):
    """构建 androidpublisher 客户端。

    凭证解析优先级：显式 --key > 环境变量 GOOGLE_APPLICATION_CREDENTIALS，
    与 verify_credentials.py 保持一致，避免两个脚本报不同的错。
    """
    from google.oauth2 import service_account
    from googleapiclient.discovery import build

    key_path = key_path or os.environ.get("GOOGLE_APPLICATION_CREDENTIALS")
    if not key_path or not os.path.exists(key_path):
        raise RuntimeError(
            "未找到 Service Account 凭证：请传入 --key，或设置环境变量 "
            "GOOGLE_APPLICATION_CREDENTIALS 指向 JSON key"
        )
    creds = service_account.Credentials.from_service_account_file(
        key_path, scopes=SCOPES
    )
    return build("androidpublisher", "v3", credentials=creds, cache_discovery=False)


def iter_reviews(service, package_name: str, page_size: int = 100) -> Iterator[dict]:
    """翻页遍历所有可见评论，内置 429/5xx 退避重试。"""
    token: str | None = None
    attempt = 0
    while True:
        req = service.reviews().list(
            packageName=package_name, maxResults=page_size, token=token
        )
        try:
            resp = req.execute()
            attempt = 0
        except Exception as exc:  # googleapiclient.errors.HttpError 及网络异常
            status = getattr(getattr(exc, "resp", None), "status", None)
            if status in (429, 500, 502, 503) and attempt < 5:
                sleep = 2**attempt
                print(f"  遇到 {status}，{sleep}s 后重试")
                time.sleep(sleep)
                attempt += 1
                continue
            raise

        for review in resp.get("reviews", []):
            yield review

        token = resp.get("tokenPagination", {}).get("nextPageToken")
        if not token:
            return


def flatten(review: dict[str, Any]) -> dict[str, Any] | None:
    """把嵌套的 API 结构拍平成一行记录。

    每条 review 的 comments 数组里，userComment 是用户评论，
    developerComment 是开发者回复。取最新一组即可。
    """
    comments = review.get("comments") or []
    user_comment: dict[str, Any] = {}
    dev_comment: dict[str, Any] = {}
    for c in comments:
        if "userComment" in c:
            user_comment = c["userComment"]
        if "developerComment" in c:
            dev_comment = c["developerComment"]

    if not user_comment:
        return None

    device_meta = user_comment.get("deviceMetadata") or {}
    return {
        "review_id": review["reviewId"],
        "author_name": review.get("authorName"),
        "star_rating": user_comment.get("starRating"),
        "review_text": (user_comment.get("text") or "").strip(),
        "reviewer_lang": user_comment.get("reviewerLanguage"),
        "device": device_meta.get("productName") or user_comment.get("device"),
        "android_version": user_comment.get("androidOsVersion"),
        "app_version_code": user_comment.get("appVersionCode"),
        "app_version_name": user_comment.get("appVersionName"),
        "submitted_at": _ts_to_iso(user_comment.get("lastModified")),
        "last_modified": _ts_to_iso(user_comment.get("lastModified")),
        "dev_reply_text": (dev_comment.get("text") or "").strip() or None,
        "dev_replied_at": _ts_to_iso(dev_comment.get("lastModified")),
        "source": SOURCE,
        "fetched_at": _now_iso(),
    }


def main() -> int:
    ap = argparse.ArgumentParser(description="拉取 Google Play 评论（通道 A）")
    ap.add_argument("--db", default="reviews.db", help="SQLite 路径")
    ap.add_argument("--package", default=os.environ.get("GOOGLE_PLAY_PACKAGE_NAME"))
    ap.add_argument("--key", default=os.environ.get("GOOGLE_APPLICATION_CREDENTIALS"),
                    help="Service Account JSON 路径，默认取环境变量")
    ap.add_argument("--page-size", type=int, default=100)
    args = ap.parse_args()

    if not args.package:
        ap.error("需要 --package 或环境变量 GOOGLE_PLAY_PACKAGE_NAME")

    service = build_service(args.key)
    seen = kept = 0
    batch: list[dict] = []

    with storage.connect(args.db) as conn:
        log_id = storage.start_sync(conn, SOURCE, _now_iso())
        try:
            for review in iter_reviews(service, args.package, args.page_size):
                seen += 1
                row = flatten(review)
                if row:
                    batch.append(row)
                if len(batch) >= 200:
                    kept += storage.upsert_reviews(conn, batch)
                    batch.clear()
            kept += storage.upsert_reviews(conn, batch)
            storage.finish_sync(conn, log_id, _now_iso(), seen, kept, "ok")
        except Exception as exc:
            storage.finish_sync(conn, log_id, _now_iso(), seen, kept, "failed", str(exc))
            raise

        print(f"通道 A 完成：API 返回 {seen} 条，写入/更新 {kept} 条")
        print("评论库概览：", storage.stats(conn))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
