"""
小红书爬虫 - 精简版
支持 search 模式，通过 API + xhshow 签名采集笔记
"""
import json
import os
from datetime import datetime, timezone

import scrapy
from scrapy.exceptions import CloseSpider

from src.spiders.socialmedia import SocialMediaSpider
from src.deduplication.redis_helper import RedisDedupHelper
from .xhs_sign import sign_with_xhshow, generate_x_b3_traceid, generate_xray_traceid
from . import xhs_config as config

_LOG_DIR = os.path.join(os.path.dirname(__file__), "logs")
os.makedirs(_LOG_DIR, exist_ok=True)
_LOG_FILE = os.path.join(_LOG_DIR, "scrapy.log")


class XiaohongshuSpider(SocialMediaSpider):
    name = "xiaohongshu"
    allowed_domains = ["xiaohongshu.com"]

    custom_settings = {
        "LOG_FILE": _LOG_FILE,
        "DOWNLOAD_DELAY": 2.0,
        "CONCURRENT_REQUESTS": 3,
        "CONCURRENT_REQUESTS_PER_DOMAIN": 3,
        "DOWNLOAD_TIMEOUT": 30,
        "DOWNLOAD_HANDLERS": {
            "https": "src.middlewares.curl_cffi_handler.CurlCffiDownloadHandler",
        },
        "ITEM_PIPELINES": {
            "src.pipelines.postgres_pipeline.PostgresPipeline": 100,
        },
    }

    BASE_URL = "https://edith.xiaohongshu.com"

    def __init__(self, keyword: str = None, num: int = None,
                 mode: str = None, incre_num: int = None, *args, **kwargs):
        super().__init__(*args, **kwargs)

        if keyword:
            self.keywords = [keyword]
        elif isinstance(config.KEYWORD, list):
            self.keywords = config.KEYWORD
        else:
            self.keywords = [config.KEYWORD]

        if mode == "incremental" and incre_num:
            self.num_limit = int(incre_num)
            self.mode = "incremental"
        else:
            self.num_limit = int(num) if num is not None else config.MAX_NOTES_COUNT
            self.mode = None

        self.cookies_dict, self.cookies_str = self._load_cookie()
        self._scheduled = {}

        self.logger.info("[XHS] keywords=%s num=%d mode=%s",
                         self.keywords, self.num_limit, self.mode or "full")

    @classmethod
    def from_crawler(cls, crawler, *args, **kwargs):
        spider = super().from_crawler(crawler, *args, **kwargs)
        spider.helper = RedisDedupHelper(
            crawler.settings.get("REDIS_URL"),
            spider.name,
            mode=spider.mode or "full",
        )
        return spider

    def _load_cookie(self):
        cookie_path = os.path.join(os.path.dirname(__file__), "xhs_cookies.json")
        if not os.path.exists(cookie_path):
            raise FileNotFoundError(f"Cookie file not found: {cookie_path}")

        with open(cookie_path, "r", encoding="utf-8") as f:
            entries = json.load(f)

        pairs = []
        cookies_dict = {}
        for entry in entries:
            name = entry.get("name", "")
            value = entry.get("value", "")
            if name and value:
                pairs.append(f"{name}={value}")
                cookies_dict[name] = value

        if not cookies_dict.get("a1"):
            raise ValueError("Cookie file missing required 'a1' field")

        self.logger.info(f"[XHS] cookie loaded, %d pairs, a1=%s...", len(cookies_dict), cookies_dict['a1'][:12])
        return cookies_dict, "; ".join(pairs)

    def _build_headers(self, api: str, data: dict = None, method: str = "POST") -> dict:
        signs = sign_with_xhshow(api, data, self.cookies_str, method)
        return {
            "authority": "edith.xiaohongshu.com",
            "accept": "application/json, text/plain, */*",
            "accept-language": "zh-CN,zh;q=0.9,en;q=0.8,en-GB;q=0.7,en-US;q=0.6",
            "cache-control": "no-cache",
            "content-type": "application/json;charset=UTF-8",
            "origin": "https://www.xiaohongshu.com",
            "pragma": "no-cache",
            "referer": "https://www.xiaohongshu.com/",
            "sec-ch-ua": '"Not A(Brand";v="99", "Microsoft Edge";v="121", "Chromium";v="121"',
            "sec-ch-ua-mobile": "?0",
            "sec-ch-ua-platform": '"Windows"',
            "sec-fetch-dest": "empty",
            "sec-fetch-mode": "cors",
            "sec-fetch-site": "same-site",
            "user-agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/121.0.0.0 Safari/537.36 Edg/121.0.0.0",
            "x-s": signs["x-s"],
            "x-t": signs["x-t"],
            "x-s-common": signs["x-s-common"],
            "x-b3-traceid": signs["x-b3-traceid"],
            "x-xray-traceid": generate_xray_traceid(),
        }

    async def start(self):
        if self.mode == "incremental":
            for request in self._incr_start():
                yield request
            return

        for keyword in self.keywords:
            if self.helper.is_keyword_done(keyword):
                self.logger.info("[XHS] keyword=%s already done, skip", keyword)
                continue
            self.helper.register_keyword(keyword, self.num_limit)
            collected = self.helper.get_collected(keyword)
            if collected >= self.num_limit:
                self.helper.mark_keyword_done(keyword)
                self.logger.info("[XHS] keyword=%s already reached target, skip", keyword)
                continue
            remaining = self.num_limit - collected
            self.logger.info("[XHS] keyword=%s collected=%d remaining=%d, start crawling",
                             keyword, collected, remaining)
            yield self._make_search_request(keyword, page=config.START_PAGE, remaining=remaining)

    def _incr_start(self):
        h = self.helper
        keywords = self.keywords

        new_kws = [k for k in keywords if not h.keyword_has_full_record(k)]
        if new_kws:
            self.logger.warning(
                "[XHS:incr] new keyword(s) detected without full-crawl record: %s. "
                "Run full crawl first for these keywords, then enable incremental mode.",
                new_kws,
            )
            raise CloseSpider("New keywords without full-crawl record detected")

        all_exist = all(h._r.exists(h._kw_key(k)) for k in keywords)
        all_done = all(h.is_keyword_done(k) for k in keywords)

        if not all_exist:
            self.logger.info("[XHS:incr] first round — init all keywords")
            for k in keywords:
                h.register_keyword(k, self.num_limit)
        elif all_done:
            self.logger.info("[XHS:incr] all keywords done — advancing to next round")
            for k in keywords:
                h.advance_round(k, self.num_limit)
        else:
            undone = [k for k in keywords if not h.is_keyword_done(k)]
            self.logger.info("[XHS:incr] resuming interrupted round, undone=%s", undone)

        for k in keywords:
            if h.is_keyword_done(k):
                self.logger.info("[XHS:incr] keyword=%s done, wait for others", k)
                continue
            target = h.get_target(k)
            collected = h.get_collected(k)
            if collected >= target:
                h.mark_keyword_done(k)
                self.logger.info("[XHS:incr] keyword=%s already reached target=%d collected=%d, mark done",
                                 k, target, collected)
                continue
            remaining = target - collected
            self.logger.info("[XHS:incr] keyword=%s target=%d collected=%d remaining=%d",
                             k, target, collected, remaining)
            yield self._make_search_request(k, page=config.START_PAGE, remaining=remaining)

    def _make_search_request(self, keyword: str, page: int = 1, remaining: int = 0):
        api = "/api/sns/web/v1/search/notes"
        data = {
            "keyword": keyword,
            "page": page,
            "page_size": 20,
            "search_id": generate_x_b3_traceid(21),
            "sort": config.SORT_TYPE,
            "note_type": config.NOTE_TYPE,
            "ext_flags": [],
            "filters": [
                {"tags": ["general"], "type": "sort_type"},
                {"tags": ["不限"], "type": "filter_note_type"},
                {"tags": ["不限"], "type": "filter_note_time"},
                {"tags": ["不限"], "type": "filter_note_range"},
                {"tags": ["不限"], "type": "filter_pos_distance"},
            ],
            "image_formats": ["jpg", "webp", "avif"],
        }
        headers = self._build_headers(api, data)
        body = json.dumps(data, separators=(",", ":"), ensure_ascii=False)
        self.logger.info("[XHS] search page=%d keyword=%s", page, keyword)
        return scrapy.Request(
            url=self.BASE_URL + api,
            method="POST", headers=headers, body=body,
            cookies=self.cookies_dict,
            callback=self.parse_search_results,
            meta={"page": page, "keyword": keyword, "remaining": remaining},
            dont_filter=True,
        )

    def _make_note_detail_request(self, note_id: str, xsec_token: str, keyword: str = "",
                                   xsec_source: str = "pc_search"):
        api = "/api/sns/web/v1/feed"
        data = {
            "source_note_id": note_id,
            "image_formats": ["jpg", "webp", "avif"],
            "extra": {"need_body_topic": "1"},
            "xsec_source": xsec_source,
            "xsec_token": xsec_token,
        }
        headers = self._build_headers(api, data)
        body = json.dumps(data, separators=(",", ":"), ensure_ascii=False)
        self.logger.debug("[XHS] requesting detail note_id=%s", note_id)
        return scrapy.Request(
            url=self.BASE_URL + api,
            method="POST", headers=headers, body=body,
            cookies=self.cookies_dict,
            callback=self.parse_note_detail,
            meta={"note_id": note_id, "xsec_token": xsec_token, "xsec_source": xsec_source,
                  "keyword": keyword},
            dont_filter=True,
        )

    def parse_search_results(self, response):
        page = response.meta.get("page", 1)
        keyword = response.meta["keyword"]
        remaining = response.meta.get("remaining", self.num_limit)
        try:
            data = json.loads(response.text)
        except Exception as e:
            self.logger.error(
                "[XHS] failed to parse search JSON page=%d status=%d body_preview=%s error=%s",
                page, response.status, response.text[:200], str(e),
            )
            return

        if not data.get("success"):
            msg = str(data.get("msg") or "")
            code = data.get("code")
            self.logger.error(
                "[XHS] search API error page=%d msg=%s code=%s",
                page, msg, code,
            )
            if _is_auth_error(msg):
                self.logger.warning("[XHS] login expired — closing spider, msg=%s", msg)
                raise CloseSpider("Login expired")
            return

        items = data.get("data", {}).get("items", [])
        has_more = data.get("data", {}).get("has_more", False)
        notes = [it for it in items if it.get("model_type") == "note"]
        self.logger.info(
            "[XHS] page=%d total=%d notes=%d has_more=%s kw=%s",
            page, len(items), len(notes), has_more, keyword,
        )

        for note in notes:
            scheduled = self._scheduled.get(keyword, 0)
            if scheduled >= remaining:
                self.logger.info(
                    "[XHS] remaining=%d reached, kw=%s, stop yielding",
                    remaining, keyword,
                )
                return

            note_id = note.get("id")
            if not note_id:
                self.logger.warning("[XHS] note missing id, skip")
                continue

            note_url = f"https://www.xiaohongshu.com/explore/{note_id}"
            if self.helper.is_note_seen(note_url, "search"):
                self.logger.info("[XHS] dup skip | note_id=%s url=%s", note_id, note_url)
                continue

            self._scheduled[keyword] = scheduled + 1
            yield self._make_note_detail_request(
                note_id=note_id,
                xsec_token=note.get("xsec_token", ""),
                keyword=keyword,
            )

        if has_more and self._scheduled.get(keyword, 0) < remaining:
            yield self._make_search_request(keyword, page=page + 1, remaining=remaining)

    def parse_note_detail(self, response):
        note_id = response.meta["note_id"]
        xsec_token = response.meta.get("xsec_token", "")

        if response.status == 461:
            self.logger.warning(
                "[XHS] 461 anti-bot for note_id=%s, closing spider", note_id,
            )
            raise CloseSpider("461 anti-bot detected")

        if response.status != 200:
            self.logger.warning(
                "[XHS] unexpected status=%d for note_id=%s, skip", response.status, note_id,
            )
            return

        try:
            data = json.loads(response.text)
        except Exception as e:
            self.logger.error(
                "[XHS] failed to parse detail JSON note_id=%s status=%d error=%s body_preview=%s",
                note_id, response.status, str(e), response.text[:200],
            )
            return

        if not data.get("success"):
            msg = str(data.get("msg") or "")
            code = data.get("code")
            self.logger.error(
                "[XHS] note detail API error note_id=%s msg=%s code=%s",
                note_id, msg, code,
            )
            if _is_auth_error(msg):
                self.logger.warning("[XHS] login expired — closing spider, msg=%s", msg)
                raise CloseSpider("Login expired")
            return

        items = data.get("data", {}).get("items", [])
        if not items:
            self.logger.warning("[XHS] no note_card data for note_id=%s", note_id)
            return

        note_card = items[0].get("note_card", {})
        if not note_card:
            self.logger.warning("[XHS] empty note_card for note_id=%s", note_id)
            return

        note_url = f"https://www.xiaohongshu.com/explore/{note_id}"

        self.helper.mark_note_collected(note_url, "search")
        new_cnt = self.helper.incr(self._active_keyword(response))

        note_info = note_card
        note_info["note_id"] = note_id
        note_info["xsec_token"] = xsec_token
        note_info["url"] = note_url
        note_info["search_keyword"] = self._active_keyword(response)

        kw = self._active_keyword(response)
        target = self.helper.get_target(kw) or self.num_limit

        self.logger.info(
            "[XHS] note %d/%d | id=%s title=%s author=%s type=%s images=%d video=%s",
            new_cnt, target,
            note_id,
            note_info.get("title", "")[:30],
            note_info.get("user", {}).get("nickname", ""),
            note_info.get("type", ""),
            len(note_info.get("image_list", [])),
            "yes" if note_info.get("video") else "no",
        )

        user = note_card.get("user", {})
        interact = note_card.get("interact_info", {})
        pub_ms = note_card.get("time")
        published_at = None
        if pub_ms and isinstance(pub_ms, (int, float)):
            published_at = datetime.fromtimestamp(pub_ms / 1000, tz=timezone.utc)

        if new_cnt >= target:
            self.helper.mark_keyword_done(kw)
            self.logger.info("[XHS] kw=%s done, collected=%d", kw, new_cnt)

        yield self.create_item(
            platform=self.name,
            data_type="search",
            item_id=note_id,
            task_id=self.task_id,
            title=note_card.get("title"),
            content=note_card.get("desc"),
            author=user.get("nickname"),
            author_id=user.get("user_id"),
            url=note_url,
            published_at=published_at.isoformat() if published_at else None,
            like_count=_parse_count(interact.get("liked_count", 0)),
            comment_count=_parse_count(interact.get("comment_count", 0)),
            share_count=_parse_count(interact.get("share_count", 0)),
            crawl_time=datetime.now(timezone.utc).isoformat(),
            raw_data=note_info,
        )

    def _active_keyword(self, response) -> str:
        return response.meta.get("keyword", "")


def _parse_count(value) -> int:
    """'1万' / '1.3万' / '999' -> int"""
    if isinstance(value, (int, float)):
        return int(value)
    if isinstance(value, str):
        value = value.strip().replace(",", "")
        if value.endswith("万"):
            try:
                return int(float(value[:-1]) * 10000)
            except ValueError:
                return 0
        try:
            return int(float(value))
        except ValueError:
            return 0
    return 0


def _is_auth_error(msg: str) -> bool:
    """Check if API error message indicates authentication failure."""
    keywords = ["登录", "login", "auth", "认证", "expired", "过期", "失效", "未登录"]
    msg_lower = msg.lower()
    return any(kw in msg_lower for kw in keywords)
