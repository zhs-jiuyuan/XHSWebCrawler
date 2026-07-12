# RedisDedupHelper — 查重与断点续爬

## 概述

`RedisDedupHelper` 为 Scrapy spider 提供基于 Redis 的笔记级去重和关键词级进度追踪，支持**断点续爬**。

每个 spider 实例持有独立的 Redis 命名空间，互不干扰。

## Redis Key 结构

| Key | 类型 | 示例值 | 说明 |
|-----|------|--------|------|
| `{spider}:notes` | SET | `url\|search`、`url\|detail` | 全局去重集合，成员为 `"{url}|{data_type}"` |
| `{spider}:kw:{keyword}` | HASH | `{target: 6, done: 1}` | 单个关键词的配置与完成状态 |
| `{spider}:kw:{keyword}:cnt` | STRING | `6` | 该关键词已采集条数计数器 |

### 示例（spider name = xiaohongshu）

```
xiaohongshu:notes
│
├── https://www.xiaohongshu.com/explore/abc123|search
├── https://www.xiaohongshu.com/explore/def456|search
└── ...

xiaohongshu:kw:美食:cnt  →  "3"

xiaohongshu:kw:美食  →  {"target": "3", "done": "1"}
```

## API

```python
from src.deduplication.redis_helper import RedisDedupHelper

helper = RedisDedupHelper(redis_url="redis://:pwd@localhost:6379/0", spider_name="xiaohongshu")
```

### 关键词管理

| 方法 | 参数 | 返回值 | 说明 |
|------|------|--------|------|
| `register_keyword(keyword, target)` | keyword: str, target: int | None | 注册关键词并设置目标条数，同时将 `done` 重置为 `"0"` |
| `is_keyword_done(keyword)` | keyword: str | bool | 该关键词是否已完成 |
| `mark_keyword_done(keyword)` | keyword: str | None | 标记关键词完成 |
| `get_collected(keyword)` | keyword: str | int | 该关键词已采集条数（不存在时返回 0） |

### 笔记去重

| 方法 | 参数 | 返回值 | 说明 |
|------|------|--------|------|
| `is_note_seen(url, data_type)` | url: str, data_type: str | bool | 该笔记是否已采集过 |
| `mark_note_collected(url, data_type)` | url: str, data_type: str | None | 标记笔记已采集（加入去重集合） |

### 计数

| 方法 | 参数 | 返回值 | 说明 |
|------|------|--------|------|
| `incr(keyword)` | keyword: str | int | 原子递增并返回新值 |

### 命名空间隔离

其他 spider 只需传入自己的 `spider_name` 即可获得独立命名空间：

```python
# 小红书 spider
helper_xhs = RedisDedupHelper(REDIS_URL, "xiaohongshu")

# 其他 spider
helper_other = RedisDedupHelper(REDIS_URL, "weibo")
```

两个 helper 的 Redis key 前缀不同，集合和计数器完全隔离。

## Spider 集成流程

### 1. 启动阶段 — `start()`

```
遍历 keywords 列表
│
├─ is_keyword_done(kw) == True ──→ 跳过（日志：already done, skip）
│
├─ 未完成 → register_keyword(kw, target)  写入 Redis: {target: N, done: 0}
│              get_collected(kw)            读取已采集条数
│              collected >= target ──→ mark_keyword_done(kw) 并跳过
│
└─ 开始采集 → yield search_request(page=1, keyword=kw)
```

### 2. 搜索解析 — `parse_search_results()`

```
收到搜索结果页
│
├─ 遍历每条笔记
│    ├─ 本地 _scheduled >= num_limit ──→ 停止 yield（防止超发）
│    ├─ is_note_seen(url, "search") == True ──→ 跳过（日志：dup skip）
│    └─ 未见过 → _scheduled += 1 → yield detail_request
│
└─ has_more && _scheduled < num_limit → 请求下一页
```

> **`_scheduled` 计数器说明**：搜索结果页可能一次返回 20 条笔记，`parse_search_results` 在收到响应后立即遍历并 yield 所有详情请求。但这些请求是异步的——全部 yield 之后才会陆续回来。如果只用 `helper.get_collected()`（Redis 计数器）来判断是否达到上限，遍历时计数为 0，20 条请求全部发出，实际会采集远超目标。`_scheduled` 是一个本地 Python dict，在 yield 前递增，确保不会超发。

### 3. 笔记详情解析 — `parse_note_detail()`

```
收到详情响应（状态码 200，JSON 成功解析）
│
├─ mark_note_collected(url, "search")   去重集合 SADD
├─ new_cnt = incr(keyword)              计数器 INCR（原子操作）
│
├─ new_cnt >= num_limit ──→ mark_keyword_done(keyword)
│                            日志：kw=xx done, collected=N
│
└─ yield create_item(...)
```

## 断点续爬流程

```
启动 spider（第 N 次运行）
│
├─ keyword "美食": is_keyword_done → True → 跳过
├─ keyword "旅游": is_keyword_done → False
│    ├─ get_collected("旅游") → 5
│    ├─ collected(5) < target(10) → 继续采集
│    └─ 从 page 1 开始搜索 "旅游"
│         │
│         ├─ 前 5 条 → is_note_seen → True → dup skip（不请求详情）
│         └─ 第 6 条起 → is_note_seen → False → 正常采集
│              └─ ... 直到 collected >= 10 → mark_keyword_done("旅游")
│
└─ spider 正常结束
```

## 警告：`is_keyword_done` 与 `register_keyword` 调用顺序

`register_keyword` 会**无条件覆盖** `done=0`。

```python
# 正确顺序（必须先判 done，再 register）
if helper.is_keyword_done(kw):   # done=1 → 跳过，不会覆盖
    skip
helper.register_keyword(kw, N)   # 仅在 done≠1 时执行

# 错误顺序（先 register 会把 done=1 重置为 done=0，导致已完成的关键词被重新爬取）
helper.register_keyword(kw, N)   # ← 覆盖 done=0！
if helper.is_keyword_done(kw):   # 永远 False
    skip
```

> **任何人在修改 `start()` 或 `_incr_start()` 时，必须保持 `is_keyword_done` 在 `register_keyword` 之前调用。**

---

## 增量采集

详见 `src/spiders/socialmedia/xhs/incremental-design.md`。

核心设计：全量和增量**共享** notes SET 去重，keyword 追踪按 mode 分 prefix（`{xhs}:incr:kw:*`）。以**轮**为单位推进，必须全部 keyword done 才能推进下一轮。调用方式：

```bash
scrapy crawl xiaohongshu -a mode=incremental -a incre_num=5
```
