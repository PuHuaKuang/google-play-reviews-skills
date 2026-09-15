"""评论库：SQLite 落地层。

设计要点：
1. review_id 作为主键，天然去重（Reviews API 与 GCS 报告会重叠）。
2. last_modified 用于识别用户「修改过的评论」——同一 review_id 内容可能变化，
   需要覆盖更新而非跳过。
3. source 字段标记数据来自哪个通道，便于排查数据质量问题。
"""

from __future__ import annotations

import sqlite3
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Iterable

DDL = """
CREATE TABLE IF NOT EXISTS reviews (
    review_id       TEXT PRIMARY KEY,
    author_name     TEXT,
    star_rating     INTEGER,
    review_text     TEXT,
    reviewer_lang   TEXT,
    device          TEXT,
    android_version INTEGER,
    app_version_code INTEGER,
    app_version_name TEXT,
    submitted_at    TEXT,
    last_modified   TEXT,
    dev_reply_text  TEXT,
    dev_replied_at  TEXT,
    source          TEXT NOT NULL,
    fetched_at      TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_reviews_submitted ON reviews(submitted_at);
CREATE INDEX IF NOT EXISTS idx_reviews_version   ON reviews(app_version_name);
CREATE INDEX IF NOT EXISTS idx_reviews_rating    ON reviews(star_rating);

CREATE TABLE IF NOT EXISTS sync_log (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    source      TEXT NOT NULL,
    started_at  TEXT NOT NULL,
    finished_at TEXT,
    rows_seen   INTEGER DEFAULT 0,
    rows_upsert INTEGER DEFAULT 0,
    status      TEXT,
    detail      TEXT
);
"""

UPSERT = """
INSERT INTO reviews (
    review_id, author_name, star_rating, review_text, reviewer_lang,
    device, android_version, app_version_code, app_version_name,
    submitted_at, last_modified, dev_reply_text, dev_replied_at,
    source, fetched_at
) VALUES (
    :review_id, :author_name, :star_rating, :review_text, :reviewer_lang,
    :device, :android_version, :app_version_code, :app_version_name,
    :submitted_at, :last_modified, :dev_reply_text, :dev_replied_at,
    :source, :fetched_at
)
ON CONFLICT(review_id) DO UPDATE SET
    review_text     = excluded.review_text,
    star_rating     = excluded.star_rating,
    last_modified   = excluded.last_modified,
    dev_reply_text  = excluded.dev_reply_text,
    dev_replied_at  = excluded.dev_replied_at,
    fetched_at      = excluded.fetched_at
WHERE excluded.last_modified > reviews.last_modified
   OR reviews.last_modified IS NULL;
"""


@contextmanager
def connect(db_path: str | Path):
    """打开数据库连接并确保表结构存在。"""
    path = Path(db_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(path))
    conn.row_factory = sqlite3.Row
    try:
        conn.executescript(DDL)
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def upsert_reviews(conn: sqlite3.Connection, rows: Iterable[dict[str, Any]]) -> int:
    """批量写入评论，返回实际影响的行数。"""
    rows = list(rows)
    if not rows:
        return 0
    before = conn.total_changes
    conn.executemany(UPSERT, rows)
    return conn.total_changes - before


def start_sync(conn: sqlite3.Connection, source: str, started_at: str) -> int:
    cur = conn.execute(
        "INSERT INTO sync_log (source, started_at, status) VALUES (?, ?, 'running')",
        (source, started_at),
    )
    return int(cur.lastrowid)


def finish_sync(
    conn: sqlite3.Connection,
    log_id: int,
    finished_at: str,
    rows_seen: int,
    rows_upsert: int,
    status: str,
    detail: str = "",
) -> None:
    conn.execute(
        """UPDATE sync_log
           SET finished_at = ?, rows_seen = ?, rows_upsert = ?, status = ?, detail = ?
           WHERE id = ?""",
        (finished_at, rows_seen, rows_upsert, status, detail, log_id),
    )


def stats(conn: sqlite3.Connection) -> dict[str, Any]:
    """返回评论库概览，用于校验采集结果。"""
    row = conn.execute(
        """SELECT COUNT(*) AS total,
                  MIN(submitted_at) AS earliest,
                  MAX(submitted_at) AS latest,
                  ROUND(AVG(star_rating), 2) AS avg_rating,
                  SUM(CASE WHEN star_rating <= 2 THEN 1 ELSE 0 END) AS negative
           FROM reviews"""
    ).fetchone()
    by_source = conn.execute(
        "SELECT source, COUNT(*) AS n FROM reviews GROUP BY source"
    ).fetchall()
    return {
        **dict(row),
        "by_source": {r["source"]: r["n"] for r in by_source},
    }
