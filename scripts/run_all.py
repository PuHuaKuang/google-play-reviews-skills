"""一键串联：入库 -> 预处理 -> 分析 -> 出报告。

用法：
  python run_all.py --csv-dir 评论 --package com.example.app
  python run_all.py --csv-dir 评论 --package com.example.app --llm   # 追加 LLM 打标
  python run_all.py --source api --package com.example.app           # Reviews API 直采
"""

from __future__ import annotations

import argparse
import subprocess
import sys
import time
from pathlib import Path


SCRIPT_DIR = Path(__file__).resolve().parent


def step(title: str, argv: list[str]) -> None:
    print(f"\n{'=' * 62}\n▶ {title}\n{'=' * 62}")
    t0 = time.time()
    r = subprocess.run([sys.executable, *argv])
    if r.returncode != 0:
        raise SystemExit(f"步骤失败：{title}（退出码 {r.returncode}）")
    print(f"  ({time.time() - t0:.1f}s)")


def main() -> int:
    ap = argparse.ArgumentParser(description="评论分析全流程")
    ap.add_argument("--csv-dir", default="评论", help="手动下载的 CSV 目录")
    ap.add_argument("--package", required=True)
    ap.add_argument("--db", default="reviews.db")
    ap.add_argument("--llm", action="store_true", help="追加 LLM 打标（需配 API Key）")
    ap.add_argument("--llm-limit", type=int, default=60)
    ap.add_argument("--source", choices=["csv", "api"], default="csv", help="采集来源")
    args = ap.parse_args()
    work_dir = Path(args.db).resolve().parent
    work_dir.mkdir(parents=True, exist_ok=True)
    db_path = str(Path(args.db).resolve())
    csv_dir = str(Path(args.csv_dir).resolve())
    report_path = str(work_dir / "report.json")
    html_path = str(work_dir / "review_report.html")

    if args.source == "api":
        step("第 1 步：Reviews API 增量采集", [
            str(SCRIPT_DIR / "fetch_api.py"),
            "--package", args.package, "--db", db_path,
        ])
    else:
        step("第 1 步：CSV 入库", [
            str(SCRIPT_DIR / "fetch_gcs.py"), "--local-csv", csv_dir,
            "--package", args.package, "--db", db_path,
        ])
    step("第 2 步：预处理", [str(SCRIPT_DIR / "preprocess.py"), "--db", db_path])
    step("第 3 步：确定性分析", [str(SCRIPT_DIR / "analyze.py"), "--db", db_path, "--package", args.package, "--out", report_path])
    if args.llm:
        step("第 3 步 B：LLM 打标", [
            str(SCRIPT_DIR / "llm_label.py"), "--db", db_path, "--limit", str(args.llm_limit),
        ])
    step("第 4 步：生成看板", [
        str(SCRIPT_DIR / "make_report.py"), "--report", report_path, "--out", html_path,
    ])
    print(f"\n完成。打开 {html_path} 查看看板。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
