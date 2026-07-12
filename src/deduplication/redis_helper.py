"""
Redis-based note deduplication and keyword progress tracking.
"""
import redis


class RedisDedupHelper:
    """Per-spider dedup + multi-keyword progress tracker backed by Redis.

    Keys::

        {spider}:notes              SET    "url|data_type"           (shared)
        {spider}:kw:{keyword}       HASH   {target, done}            (full)
        {spider}:kw:{keyword}:cnt   STRING int                       (full)
        {spider}:incr:kw:{keyword}  HASH   {target, done}            (incr)
        {spider}:incr:kw:{keyword}:cnt STRING int                    (incr)
    """

    def __init__(self, redis_url: str, spider_name: str, mode: str = "full"):
        self._r = redis.Redis.from_url(redis_url, decode_responses=True)
        self._prefix = spider_name
        self._mode = mode

    def _notes_key(self) -> str:
        return f"{self._prefix}:notes"

    def _kw_key(self, keyword: str) -> str:
        if self._mode == "incremental":
            return f"{self._prefix}:incr:kw:{keyword}"
        return f"{self._prefix}:kw:{keyword}"

    def _cnt_key(self, keyword: str) -> str:
        if self._mode == "incremental":
            return f"{self._prefix}:incr:kw:{keyword}:cnt"
        return f"{self._prefix}:kw:{keyword}:cnt"

    # ---- keyword management -------------------------------------------

    def register_keyword(self, keyword: str, target: int) -> None:
        self._r.hset(self._kw_key(keyword), mapping={"target": target, "done": "0"})

    def is_keyword_done(self, keyword: str) -> bool:
        return self._r.hget(self._kw_key(keyword), "done") == "1"

    def mark_keyword_done(self, keyword: str) -> None:
        self._r.hset(self._kw_key(keyword), "done", "1")

    def get_collected(self, keyword: str) -> int:
        val = self._r.get(self._cnt_key(keyword))
        return int(val) if val is not None else 0

    # ---- note deduplication -------------------------------------------

    def is_note_seen(self, url: str, data_type: str) -> bool:
        return bool(self._r.sismember(self._notes_key(), f"{url}|{data_type}"))

    def mark_note_collected(self, url: str, data_type: str) -> None:
        self._r.sadd(self._notes_key(), f"{url}|{data_type}")

    # ---- counter ------------------------------------------------------

    def incr(self, keyword: str) -> int:
        return int(self._r.incr(self._cnt_key(keyword)))

    def advance_round(self, keyword: str, incre: int) -> None:
        self._r.hincrby(self._kw_key(keyword), "target", incre)
        self._r.hset(self._kw_key(keyword), "done", "0")

    def get_target(self, keyword: str) -> int:
        val = self._r.hget(self._kw_key(keyword), "target")
        return int(val) if val is not None else 0

    def keyword_has_full_record(self, keyword: str) -> bool:
        full_key = f"{self._prefix}:kw:{keyword}"
        return bool(self._r.exists(full_key))
