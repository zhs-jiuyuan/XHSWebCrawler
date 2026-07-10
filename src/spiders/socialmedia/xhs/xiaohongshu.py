"""
小红书爬虫 - 精简版
支持 search 模式，通过 API + xhshow 签名采集笔记
"""
import json
import os

import scrapy
from scrapy.exceptions import CloseSpider

from src.spiders.socialmedia import SocialMediaSpider
from .xhs_sign import sign_with_xhshow, generate_x_b3_traceid, generate_xray_traceid
from . import xhs_config as config
from src.items.base import BaseItem

_LOG_DIR = os.path.join(os.path.dirname(__file__), "logs")
os.makedirs(_LOG_DIR, exist_ok=True)
_LOG_FILE = os.path.join(_LOG_DIR, "scrapy.log")


class XiaohongshuSpider(SocialMediaSpider):
    name = "xiaohongshu"

    custom_settings = {
        "LOG_FILE": _LOG_FILE,
        "DOWNLOAD_DELAY": 2.0,
        "CONCURRENT_REQUESTS": 3,
        "CONCURRENT_REQUESTS_PER_DOMAIN": 3,
        "DOWNLOAD_TIMEOUT": 30,
        "DOWNLOAD_HANDLERS": {
            "https": "src.middlewares.curl_cffi_handler.CurlCffiDownloadHandler",
        },
    }

    BASE_URL = "https://edith.xiaohongshu.com"

    def __init__(self, keyword: str = None, num: int = None, *args, **kwargs):
        super().__init__(*args, **kwargs)

        self.keyword = keyword or config.KEYWORD
        self.num_limit = int(num) if num is not None else config.MAX_NOTES_COUNT
        self.items_count = 0
        self._seen_urls = set()

        self.cookies_dict, self.cookies_str = self._load_cookie()

        self.logger.info(f"[XHS] keyword={self.keyword}, num={self.num_limit}")

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

        self.logger.info(f"[XHS] 加载cookie, %d条, a1=%s...", len(cookies_dict), cookies_dict['a1'][:12])
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
        yield self._make_search_request(page=config.START_PAGE)

    def _make_search_request(self, page: int = 1):
        api = "/api/sns/web/v1/search/notes"
        data = {
            "keyword": self.keyword,
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
        self.logger.info(f"[XHS] Search page {page}: keyword={self.keyword}")
        return scrapy.Request(
            url=self.BASE_URL + api,
            method="POST", headers=headers, body=body,
            cookies=self.cookies_dict,
            callback=self.parse_search_results,
            meta={"page": page},
            dont_filter=True,
        )

    def _make_note_detail_request(self, note_id: str, xsec_token: str, xsec_source: str = "pc_search"):
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
        self.logger.debug("[XHS] Requesting detail note_id=%s", note_id)
        return scrapy.Request(
            url=self.BASE_URL + api,
            method="POST", headers=headers, body=body,
            cookies=self.cookies_dict,
            callback=self.parse_note_detail,
            meta={"note_id": note_id, "xsec_token": xsec_token, "xsec_source": xsec_source},
            dont_filter=True,
        )

    def parse_search_results(self, response):
        page = response.meta.get("page", 1)
        try:
            data = json.loads(response.text)
        except Exception as e:
            self.logger.error(
                "[XHS] Failed to parse search JSON page=%d status=%d body_preview=%s error=%s",
                page, response.status, response.text[:200], str(e),
            )
            return

        if not data.get("success"):
            self.logger.error(
                "[XHS] Search API error page=%d msg=%s code=%s",
                page, data.get("msg"), data.get("code"),
            )
            return

        items = data.get("data", {}).get("items", [])
        has_more = data.get("data", {}).get("has_more", False)
        notes = [it for it in items if it.get("model_type") == "note"]
        self.logger.info(
            "[XHS] Search page %d: total=%d notes=%d has_more=%s",
            page, len(items), len(notes), has_more,
        )

        for note in notes:
            if len(self._seen_urls) >= self.num_limit:
                self.logger.info(
                    "[XHS] Reached num_limit=%d at page=%d, stop yielding",
                    self.num_limit, page,
                )
                return
            note_id = note.get("id")
            if not note_id:
                self.logger.warning("[XHS] Note missing id, skip")
                continue
            note_url = f"https://www.xiaohongshu.com/explore/{note_id}"
            if note_url in self._seen_urls:
                self.logger.debug("[XHS] Duplicate note, skip note_id=%s", note_id)
                continue
            self._seen_urls.add(note_url)
            yield self._make_note_detail_request(
                note_id=note_id,
                xsec_token=note.get("xsec_token", ""),
            )

        if has_more and len(self._seen_urls) < self.num_limit:
            yield self._make_search_request(page=page + 1)

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
                "[XHS] Unexpected status=%d for note_id=%s, skip", response.status, note_id,
            )
            return

        try:
            data = json.loads(response.text)
        except Exception as e:
            self.logger.error(
                "[XHS] Failed to parse detail JSON note_id=%s status=%d error=%s body_preview=%s",
                note_id, response.status, str(e), response.text[:200],
            )
            return

        if not data.get("success"):
            self.logger.error(
                "[XHS] Note detail API error note_id=%s msg=%s code=%s",
                note_id, data.get("msg"), data.get("code"),
            )
            return

        items = data.get("data", {}).get("items", [])
        if not items:
            self.logger.warning("[XHS] No note_card data for note_id=%s", note_id)
            return

        note_card = items[0].get("note_card", {})
        if not note_card:
            self.logger.warning("[XHS] Empty note_card for note_id=%s", note_id)
            return

        note_info = self._extract_note_info(note_card, note_id)
        note_info["xsec_token"] = xsec_token
        note_info["url"] = f"https://www.xiaohongshu.com/explore/{note_id}"
        note_info["search_keyword"] = self.keyword

        self.items_count += 1
        self.logger.info(
            "[XHS] Note %d/%d | id=%s title=%s author=%s type=%s images=%d video=%s",
            self.items_count, self.num_limit,
            note_id,
            note_info.get("title", "")[:30],
            note_info.get("author", ""),
            note_info.get("type", ""),
            len(note_info.get("images", [])),
            "yes" if note_info.get("video") else "no",
        )

        yield self.create_item(note_info, url=note_info["url"])

    def _extract_note_info(self, note_card: dict, note_id: str) -> dict:
        info = {
            "note_id": note_id,
            "title": note_card.get("title", ""),
            "content": note_card.get("desc", ""),
            "type": note_card.get("type", ""),
            "published_at": note_card.get("time", 0),
            "last_update_time": note_card.get("last_update_time", 0),
        }
        interact_info = note_card.get("interact_info", {})
        info["liked_count"] = interact_info.get("liked_count", "0")
        info["collected_count"] = interact_info.get("collected_count", "0")
        info["comment_count"] = interact_info.get("comment_count", "0")
        info["share_count"] = interact_info.get("share_count", "0")
        user = note_card.get("user", {})
        info["user_id"] = user.get("user_id", "")
        info["author"] = user.get("nickname", "")
        info["avatar"] = user.get("avatar", "")
        image_list = note_card.get("image_list", [])
        info["images"] = []
        for img in image_list:
            img_info = {
                "url": img.get("url_default", "") or img.get("url", ""),
                "width": img.get("width", 0),
                "height": img.get("height", 0),
            }
            for img_detail in img.get("info_list", []):
                if img_detail.get("image_scene") == "WB_DFT":
                    img_info["url_no_watermark"] = img_detail.get("url", "")
                    break
            info["images"].append(img_info)
        video = note_card.get("video", {})
        if video:
            media = video.get("media", {})
            stream = media.get("stream", {})
            video_url = ""
            for quality in ["h266", "h265", "h264", "av1"]:
                streams = stream.get(quality, [])
                if streams:
                    video_url = streams[0].get("master_url", "")
                    break
            info["video"] = {"url": video_url, "duration": video.get("duration", 0)}
        tag_list = note_card.get("tag_list", [])
        info["tags"] = [tag.get("name", "") for tag in tag_list if tag.get("name")]
        topics = note_card.get("topics", [])
        info["topics"] = [topic.get("name", "") for topic in topics if topic.get("name")]
        info["ip_location"] = note_card.get("ip_location", "")
        return info

    def create_item(self, data: dict, url: str) -> BaseItem:
        item = BaseItem()
        item['spider_name'] = self.name
        item['target_type'] = self.target_type
        item['task_id'] = self.task_id
        item['url'] = url
        item['data'] = data
        return item
