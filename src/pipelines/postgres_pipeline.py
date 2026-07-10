"""
通用 PostgreSQL 存储管道 — 将 item 字段按列名直接映射写入 public.social_media。

每个 spider 需在 item 中自行完成数据清洗（类型转换、中文数字归一化等），
pipeline 仅做透传存储；字段类型不匹配时记录错误。
"""
import json
from datetime import datetime, timezone

import psycopg2
from itemadapter import ItemAdapter

_INSERT_SQL = """
INSERT INTO public.social_media
    (platform, data_type, item_id, task_id, title, content, author, author_id,
     url, published_at, like_count, comment_count, share_count, crawl_time, raw_data)
VALUES (%(platform)s, %(data_type)s, %(item_id)s, %(task_id)s, %(title)s, %(content)s,
        %(author)s, %(author_id)s, %(url)s, %(published_at)s, %(like_count)s,
        %(comment_count)s, %(share_count)s, %(crawl_time)s, %(raw_data)s)
ON CONFLICT (platform, data_type, item_id) DO NOTHING
RETURNING id;
"""


class PostgresPipeline:

    def __init__(self, dsn: str):
        self.dsn = dsn
        self.conn = None
        self.item_count = 0

    @classmethod
    def from_crawler(cls, crawler):
        dsn = crawler.settings.get("POSTGRES_URL")
        if not dsn:
            raise ValueError("POSTGRES_URL not configured")
        return cls(dsn)

    def open_spider(self, spider):
        self.conn = psycopg2.connect(self.dsn)
        self.conn.set_session(autocommit=False)
        spider.logger.info("[PostgresPipeline] opened")

    def process_item(self, item, spider):
        adapter = ItemAdapter(item)

        try:
            crawled = adapter.get("crawl_time")
            if isinstance(crawled, str):
                crawled = datetime.fromisoformat(crawled)
            if crawled is None:
                crawled = datetime.now(timezone.utc)

            pub = adapter.get("published_at")
            if isinstance(pub, str):
                pub = datetime.fromisoformat(pub)

            raw = adapter.get("raw_data")
            if isinstance(raw, dict):
                raw = json.dumps(raw, ensure_ascii=False, default=str)

            row = {
                "platform": adapter.get("platform") or spider.name,
                "data_type": adapter.get("data_type") or "",
                "item_id": adapter.get("item_id") or "",
                "task_id": adapter.get("task_id"),
                "title": adapter.get("title") or "",
                "content": adapter.get("content") or "",
                "author": adapter.get("author") or "",
                "author_id": adapter.get("author_id") or "",
                "url": adapter.get("url"),
                "published_at": pub,
                "like_count": int(adapter.get("like_count", 0)),
                "comment_count": int(adapter.get("comment_count", 0)),
                "share_count": int(adapter.get("share_count", 0)),
                "crawl_time": crawled,
                "raw_data": raw,
            }

            with self.conn.cursor() as cur:
                cur.execute(_INSERT_SQL, row)
                result = cur.fetchone()
                self.conn.commit()
                if result and result[0]:
                    self.item_count += 1
                    spider.logger.debug("[PostgresPipeline] inserted id=%d url=%s", result[0], row["url"])
                else:
                    spider.logger.debug("[PostgresPipeline] skipped (duplicate) url=%s", row["url"])
        except Exception as e:
            self.conn.rollback()
            spider.logger.error(
                "[PostgresPipeline] insert failed, check data/column mismatch and fix before re-running."
                " url=%s error=%s", adapter.get("url"), e,
            )
            raise

        return item

    def close_spider(self, spider):
        if self.conn:
            self.conn.close()
        spider.logger.info("[PostgresPipeline] closed | inserted=%d", self.item_count)
