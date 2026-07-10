# -*- coding: utf-8 -*-
# Copyright (c) 2025 relakkes@gmail.com
#
# This file is part of MediaCrawler project.
# Repository: https://github.com/NanmiCoder/MediaCrawler/blob/main/media_platform/xhs/playwright_sign.py
# GitHub: https://github.com/NanmiCoder
# Licensed under NON-COMMERCIAL LEARNING LICENSE 1.1

# Xiaohongshu signature generation using xhshow pure-algorithm library
#
# 致谢：本签名实现依赖 xhshow 开源库, 由 Cloxl 提供
# 仓库地址: https://github.com/Cloxl/xhshow
# 许可协议: MIT License

import hashlib
import json
import random
import time
import uuid
from typing import Any, Dict, Optional, Union
from urllib.parse import quote


def _patch_xhshow_a3_hash():
    """
    修复 xhshow 库 build_payload_array 中 a3_hash 计算的 bug。
    xhshow 原实现对所有请求使用 MD5(extract_api_path(content_string)) 计算 a3_hash,
    其中 extract_api_path 会同时去掉 "?" 后的查询参数和 "{" 后的 JSON body。
    但浏览器的实际行为是:
      - POST: a3 使用 MD5(api_path), 即去掉 JSON body 后的路径 → 原实现正确
      - GET:  a3 使用 MD5(完整 URL + 查询参数) → 原实现错误, 因为也去掉了查询参数
    修复方式: 对 GET 请求(content_string 不含 "{"), 使用完整 content_string 的 MD5;
              对 POST 请求(content_string 含 "{"), 保持原始行为。
    相关 issue: https://github.com/Cloxl/xhshow/issues/104
    """
    from xhshow.core.crypto import CryptoProcessor

    _original_build = CryptoProcessor.build_payload_array

    def _patched_build(self, hex_parameter, hex_md5_path, a1_value, app_identifier="xhs-pc-web",
                       string_param="", timestamp=None, sign_state=None):
        payload = _original_build(self, hex_parameter, hex_md5_path, a1_value,
                                  app_identifier, string_param, timestamp, sign_state)
        if "{" not in string_param:
            correct_md5_hex = hashlib.md5(string_param.encode("utf-8")).hexdigest()
            correct_md5_bytes = [int(correct_md5_hex[i:i + 2], 16) for i in range(0, 32, 2)]
            seed_byte = payload[4]
            ts_bytes = payload[8:16]
            correct_a3_hash = self._custom_hash_v2(list(ts_bytes) + correct_md5_bytes)
            for i in range(16):
                payload[128 + i] = correct_a3_hash[i] ^ seed_byte
        return payload

    CryptoProcessor.build_payload_array = _patched_build


_patch_xhshow_a3_hash()


def _build_sign_string(uri: str, data: Optional[Union[Dict, str]] = None, method: str = "POST") -> str:
    if method.upper() == "POST":
        c = uri
        if data is not None:
            if isinstance(data, dict):
                c += json.dumps(data, separators=(",", ":"), ensure_ascii=False)
            elif isinstance(data, str):
                c += data
        return c
    else:
        if not data or (isinstance(data, dict) and len(data) == 0):
            return uri
        if isinstance(data, dict):
            params = []
            for key in data.keys():
                value = data[key]
                if isinstance(value, list):
                    value_str = ",".join(str(v) for v in value)
                elif value is not None:
                    value_str = str(value)
                else:
                    value_str = ""
                value_str = quote(value_str, safe=',')
                params.append(f"{key}={value_str}")
            return f"{uri}?{'&'.join(params)}"
        elif isinstance(data, str):
            return f"{uri}?{data}"
        return uri


def get_trace_id() -> str:
    chars = "abcdef0123456789"
    return "".join(chars[random.randint(0, 15)] for _ in range(16))


def generate_x_b3_traceid(length: int = 16) -> str:
    chars = "abcdef0123456789"
    return "".join(chars[random.randint(0, 15)] for _ in range(length))


def generate_xray_traceid() -> str:
    return str(uuid.uuid4())


def sign_with_xhshow(
    uri: str,
    data: Optional[Union[Dict, str]] = None,
    cookie_str: str = "",
    method: str = "POST",
) -> Dict[str, Any]:
    from xhshow import Xhshow
    xhshow_client = Xhshow()

    is_post = method.upper() == "POST"

    if is_post:
        headers = xhshow_client.sign_headers_post(
            uri=uri,
            cookies=cookie_str,
            payload=data if isinstance(data, dict) else {},
        )
    else:
        content_string = _build_sign_string(uri, data, method)
        cookie_dict = xhshow_client._parse_cookies(cookie_str)
        a1_value = cookie_dict.get("a1", "")

        ts = time.time()
        d_value = hashlib.md5(content_string.encode("utf-8")).hexdigest()

        payload_array = xhshow_client.crypto_processor.build_payload_array(
            d_value, a1_value, "xhs-pc-web", content_string, ts
        )
        xor_result = xhshow_client.crypto_processor.bit_ops.xor_transform_array(payload_array)
        config = xhshow_client.config
        x3_b64 = xhshow_client.crypto_processor.b64encoder.encode_x3(
            xor_result[:config.PAYLOAD_LENGTH]
        )
        sig_data = config.SIGNATURE_DATA_TEMPLATE.copy()
        sig_data["x3"] = config.X3_PREFIX + x3_b64
        x_s = config.XYS_PREFIX + xhshow_client.crypto_processor.b64encoder.encode(
            json.dumps(sig_data, separators=(",", ":"), ensure_ascii=False)
        )
        headers = {
            "x-s": x_s,
            "x-s-common": xhshow_client.sign_xs_common(cookie_dict),
            "x-t": str(xhshow_client.get_x_t(ts)),
            "x-b3-traceid": xhshow_client.get_b3_trace_id(),
        }

    return {
        "x-s": headers.get("x-s", ""),
        "x-t": headers.get("x-t", ""),
        "x-s-common": headers.get("x-s-common", ""),
        "x-b3-traceid": headers.get("x-b3-traceid", get_trace_id()),
        "x-xray-traceid": headers.get("x-xray-traceid", get_trace_id()),
    }
