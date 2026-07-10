"""
Scrapy download handler using curl_cffi for TLS fingerprint impersonation.
"""
import logging
import random
import time as time_module

from curl_cffi import requests as curl_requests
from scrapy.http import HtmlResponse
from scrapy.settings import Settings
from twisted.internet import threads

logger = logging.getLogger(__name__)

_BROWSER_ALIASES = [
    "chrome120",
    "chrome123",
    "chrome124",
    "safari15_5",
    "safari17_0",
]


class CurlCffiDownloadHandler:

    def __init__(self, settings: Settings):
        self.session = curl_requests.Session()
        self.session.headers.update(
            {
                "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8",
                "Cache-Control": "no-cache",
            }
        )

    @classmethod
    def from_crawler(cls, crawler):
        return cls(crawler.settings)

    def download_request(self, request, spider):
        return threads.deferToThread(self._do_request, request, spider)

    async def close(self):
        if self.session:
            self.session.close()

    def _do_request(self, request, spider):
        method = request.method.decode() if isinstance(request.method, bytes) else request.method
        url = request.url
        headers = {k.decode(): v[0].decode() for k, v in request.headers.items()}
        body = request.body or None
        cookies = request.cookies

        alias = random.choice(_BROWSER_ALIASES)

        proxy = request.meta.get("proxy")
        req_kwargs = dict(
            method=method,
            url=url,
            headers=headers,
            data=body,
            cookies=cookies,
            impersonate=alias,
        )
        if proxy:
            req_kwargs["proxies"] = {"http": proxy, "https": proxy}

        start = time_module.time()
        logger.debug(
            "[CurlCffi] download start | url=%s method=%s alias=%s",
            url, method, alias,
        )

        try:
            resp = self.session.request(**req_kwargs)
        except Exception as e:
            elapsed = time_module.time() - start
            logger.error(
                "[CurlCffi] download failed | url=%s method=%s error=%s elapsed=%.2fs",
                url, method, str(e), elapsed,
            )
            raise

        elapsed = time_module.time() - start
        logger.info(
            "[CurlCffi] download done | url=%s status=%s size=%db elapsed=%.2fs alias=%s",
            url, resp.status_code, len(resp.content), elapsed, alias,
        )

        raw_headers = {}
        skip_keys = {"content-encoding", "content-length", "transfer-encoding"}
        for k, v in resp.headers.items():
            if k.lower() in skip_keys:
                continue
            raw_headers[k.lower()] = v.encode() if isinstance(v, str) else v

        return HtmlResponse(
            url=str(resp.url),
            status=resp.status_code,
            headers=raw_headers,
            body=resp.content,
            request=request,
        )
