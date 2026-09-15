"""通道 A 配置自检：校验 Service Account 凭证并做一次只读探测。

为什么需要它：fetch_api.py 的失败信息来自 Google 的原始 HTTP 响应，
403/401/404 各自的处置方式完全不同，而每日任务通常在无人值守时失败，
事后排查成本高。本脚本把「先验证凭证，再交给定时任务」变成一步可执行动作。

用法：
    python verify_credentials.py --package com.example.app
    python verify_credentials.py --key /path/sa.json --package com.example.app

凭证解析优先级：--key > 环境变量 GOOGLE_APPLICATION_CREDENTIALS。
只做只读操作，不写数据库、不改动评论数据。
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

OK = "[OK]"
BAD = "[!!]"
INFO = "[--]"

HINTS = {
    401: "密钥无效或系统时间偏差过大。重新生成 JSON 密钥，并校准本机时间。",
    403: "服务账号未被授权。请确认：(1) GCP 已启用 androidpublisher.googleapis.com；"
         "(2) Play Console「用户和权限」已邀请该邮箱并勾选「查看应用信息」；"
         "(3) 邀请生效通常需要 1-5 分钟，刚授权完请稍后重试。",
    404: "未找到该包名。请确认包名正确，且服务账号已被授予该应用的访问权限。",
    429: "触发配额限制，稍后重试。",
    500: "Google 服务端错误，稍后重试。",
}


def fail(msg: str) -> int:
    print(f"{BAD} {msg}")
    return 1


def check_key_file(key_path: Path) -> dict | None:
    if not key_path:
        return None
    print(f"{INFO} 凭证路径：{key_path}")
    if not key_path.exists():
        fail("凭证文件不存在。请将 Service Account JSON 密钥放到该路径，"
             "或用 GOOGLE_APPLICATION_CREDENTIALS 指定。")
        return None

    try:
        payload = json.loads(key_path.read_text(encoding="utf-8"))
    except Exception as exc:
        fail(f"凭证文件不是合法 JSON：{exc}")
        return None

    if payload.get("type") != "service_account":
        fail(f"凭证 type 应为 service_account，实际为 {payload.get('type')!r}")
        return None

    missing = [k for k in ("client_email", "private_key", "project_id") if not payload.get(k)]
    if missing:
        fail(f"凭证缺少必需字段：{missing}")
        return None

    print(f"{OK} 凭证格式合法")
    print(f"     服务账号邮箱：{payload['client_email']}")
    print(f"     GCP 项目 ID ：{payload['project_id']}")
    return payload


def probe(key_path: Path, package: str) -> int:
    from google.oauth2 import service_account
    from googleapiclient.discovery import build
    from googleapiclient.errors import HttpError

    scopes = ["https://www.googleapis.com/auth/androidpublisher"]
    try:
        creds = service_account.Credentials.from_service_account_file(
            str(key_path), scopes=scopes
        )
        service = build("androidpublisher", "v3", credentials=creds, cache_discovery=False)
    except Exception as exc:
        return fail(f"构建客户端失败：{exc}")

    print(f"{INFO} 正在读取 {package} 的可见评论（只读）……")
    try:
        resp = service.reviews().list(packageName=package, maxResults=10).execute()
    except HttpError as exc:
        status = getattr(exc.resp, "status", None)
        body = exc.content.decode("utf-8", "replace") if exc.content else ""
        print(f"{BAD} HTTP {status}")
        if status in HINTS:
            print(f"     可能原因：{HINTS[status]}")
        print(f"     原始响应：{body[:600]}")
        return 1
    except Exception as exc:
        return fail(f"请求异常：{exc}")

    reviews = resp.get("reviews", [])
    print(f"{OK} API 调用成功，本次返回 {len(reviews)} 条可见评论")

    if not reviews:
        print(f"{INFO} 返回 0 条属正常情况：reviews.list 只覆盖最近约 7 天内")
        print(f"     提交或被修改的评论；窗口内无新评论时即为 0。")
        return 0

    ratings: list[int] = []
    for r in reviews[:10]:
        comments = r.get("comments") or []
        uc = next((c["userComment"] for c in comments if "userComment" in c), {})
        star = uc.get("starRating")
        if isinstance(star, int):
            ratings.append(star)
        print(f"     ★{star}  lang={uc.get('reviewerLanguage')}  "
              f"ver={uc.get('appVersionName')}  {(uc.get('text') or '')[:40]!r}")
    if ratings:
        print(f"{INFO} 样本平均星级：{sum(ratings) / len(ratings):.2f}")
    print(f"{OK} 凭证链路完全打通，可执行定时增量采集。")
    return 0


def main() -> int:
    ap = argparse.ArgumentParser(description="校验通道 A（Reviews API）凭证")
    ap.add_argument("--key", default=os.environ.get("GOOGLE_APPLICATION_CREDENTIALS"),
                    help="Service Account JSON 路径，默认取环境变量")
    ap.add_argument("--package", default=os.environ.get("GOOGLE_PLAY_PACKAGE_NAME"),
                    help="应用包名，默认取环境变量")
    args = ap.parse_args()

    if not args.package:
        ap.error("需要 --package 或环境变量 GOOGLE_PLAY_PACKAGE_NAME")
    if not args.key:
        return fail("未指定凭证：请传入 --key，或设置 GOOGLE_APPLICATION_CREDENTIALS")

    payload = check_key_file(Path(args.key))
    if not payload:
        print(f"\n{BAD} 校验未通过，定时采集无法启动。")
        return 1

    code = probe(Path(args.key), args.package)
    print(f"\n{OK} 全部检查通过。" if code == 0 else f"\n{BAD} 探测失败，请按上方提示修复。")
    return code


if __name__ == "__main__":
    sys.exit(main())
