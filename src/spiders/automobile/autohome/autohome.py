"""
汽车之家品牌信息爬虫 - 测试入口地址
"""
import json
import os

import scrapy

from src.spiders.base import BaseSpider

_LOG_DIR = os.path.join(os.path.dirname(__file__), "logs")
os.makedirs(_LOG_DIR, exist_ok=True)
_LOG_FILE = os.path.join(_LOG_DIR, "scrapy.log")


class AutohomeSpider(BaseSpider):
    name = "autohome"
    target_type = "automobile"
    allowed_domains = ["autohome.com.cn"]

    custom_settings = {
        "LOG_FILE": _LOG_FILE,
        "DOWNLOAD_DELAY": 2.0,
        "CONCURRENT_REQUESTS": 1,
        "CONCURRENT_REQUESTS_PER_DOMAIN": 1,
        "DOWNLOAD_TIMEOUT": 30,
        "DOWNLOAD_HANDLERS": {
            "https": "src.middlewares.curl_cffi_handler.CurlCffiDownloadHandler",
        },
    }

    BASE_URL = "https://car.app.autohome.com.cn"

    async def start(self):
        url = f"{self.BASE_URL}/carMiddle/getBrandInfoAll?appId=pc&needhmzx=1"
        headers = {
            "accept": "*/*",
            "accept-language": "zh-CN,zh;q=0.9",
            "priority": "u=1, i",
            "sec-ch-ua": "\"Not;A=Brand\";v=\"8\", \"Chromium\";v=\"150\", \"Google Chrome\";v=\"150\"",
            "sec-ch-ua-mobile": "?0",
            "sec-ch-ua-platform": "\"Windows\"",
            "sec-fetch-dest": "empty",
            "sec-fetch-mode": "cors",
            "sec-fetch-site": "same-site",
            "Referer": "https://www.autohome.com.cn/",
        }
        self.logger.info("[Autohome] request entry URL: %s", url)
        yield scrapy.Request(
            url=url,
            method="GET",
            headers=headers,
            callback=self.parse_entry,
            dont_filter=True,
        )

    def parse_entry(self, response):
        self.logger.info("[Autohome] response status=%d", response.status)
        try:
            data = json.loads(response.text)
            body = json.dumps(data, indent=2, ensure_ascii=False)
        except json.JSONDecodeError:
            body = response.text

        self.logger.info("[Autohome] response body:\n%s", body)
        self.logger.info(
            "[Autohome] entry response parsed | status=%d length=%d",
            response.status, len(response.text),
        )
