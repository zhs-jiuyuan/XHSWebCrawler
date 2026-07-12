# 增量爬取方案设计 — 共享去重 + mode 拆分 + 轮级门控

## 1. Redis Key 结构

全量和增量**共享** notes SET，keyword 追踪按 mode 分 prefix：

```
                          ┌─── 共享 ───┐
{xhs}:notes               SET    "url|search"         全局去重，两个 mode 共用

{xhs}:kw:{keyword}        HASH   {target, done}       全量 keyword 进度（不变）
{xhs}:kw:{keyword}:cnt    STRING int                   全量计数

{xhs}:incr:kw:{keyword}   HASH   {target, done}       增量 keyword 进度
{xhs}:incr:kw:{keyword}:cnt STRING int                 增量计数
```

**关键点**: `mark_note_collected()` 永远写同一个 `{xhs}:notes` SET，增量全量天然互不重复采集。

target 值自带轮次含义：`target=5` 是第 1 轮，`target=10` 是第 2 轮。

---

## 2. 轮次门控模型

增量爬取以**轮**为单位推进：一轮 = 所有 keyword 各自采集 `incre_num` 条新笔记。

```
keyword_A ──[采5条]──→ done=1 ┐
keyword_B ──[采5条]──→ done=1 ├──→ 全部 done → 下一轮
keyword_C ──[采5条]──→ done=1 ┘
```

**门控规则**（`start()` 入口判断）：

| 增量 key 状态 | 判定 | 行为 |
|--------------|------|------|
| 全部 key 不存在 | 第 1 轮首次 | `register_keyword` 创建，`target=incre_num, done=0` |
| 全部 `done=1` | 上轮全部完成 | `advance_round` → `target += incre_num, done=0`，开下一轮 |
| 混合状态（部分 done=1，部分 done=0） | 上轮中断 | **不动任何 target**，只处理 `done=0` 的 keyword 补缺 |
| 全部 `done=0` 且 key 存在 | 上一轮全中断 | 不动 target，所有 keyword 继续补缺 |

核心原则：**必须全部 keyword done 才能推进到下一轮**，任何 keyword 中断都不累加 target。

---

## 3. 调用方式

```bash
# 全量爬取（默认）
scrapy crawl xiaohongshu

# 增量爬取 - 第 N 轮 / 断点续爬
scrapy crawl xiaohongshu -a mode=incremental -a incre_num=5
```

- 不传 `mode`：全量模式，读取 `xhs_config` 配置
- `mode=incremental` 但不传 `incre_num`：降级为全量行为（使用 `config.MAX_NOTES_COUNT`）
- `incre_num` 仅在 `mode=incremental` 时生效
- 参数可叠加：`-a mode=incremental -a incre_num=5 -a keyword=美食`

---

## 4. RedisDedupHelper 改造

### 4a. `__init__` 加 `mode` 参数

```python
def __init__(self, redis_url: str, spider_name: str, mode: str = "full"):
    self._r = redis.Redis.from_url(redis_url, decode_responses=True)
    self._prefix = spider_name
    self._mode = mode                    # "full" | "incremental"
```

### 4b. Key 路由

```python
def _notes_key(self) -> str:
    return f"{self._prefix}:notes"       # 不论 mode，永远同一个

def _kw_key(self, keyword: str) -> str:
    if self._mode == "incremental":
        return f"{self._prefix}:incr:kw:{keyword}"
    return f"{self._prefix}:kw:{keyword}"

def _cnt_key(self, keyword: str) -> str:
    if self._mode == "incremental":
        return f"{self._prefix}:incr:kw:{keyword}:cnt"
    return f"{self._prefix}:kw:{keyword}:cnt"
```

### 4c. 新增方法

```python
def register_keyword(self, keyword: str, target: int) -> None:
    self._r.hset(self._kw_key(keyword), mapping={"target": target, "done": "0"})

def advance_round(self, keyword: str, incre: int) -> None:
    self._r.hincrby(self._kw_key(keyword), "target", incre)
    self._r.hset(self._kw_key(keyword), "done", "0")

def get_target(self, keyword: str) -> int:
    val = self._r.hget(self._kw_key(keyword), "target")
    return int(val) if val is not None else 0
```

### 4d. 不变的方法

`is_keyword_done`, `mark_keyword_done`, `get_collected`, `incr`, `is_note_seen`, `mark_note_collected` 无需改动——内部通过 `_kw_key` / `_cnt_key` / `_notes_key` 自动路由。

---

## 5. Spider 改造

### 5a. `__init__` 接收 CLI 参数

```python
def __init__(self, keyword=None, num=None, mode=None, incre_num=None, *args, **kwargs):
    super().__init__(*args, **kwargs)
    # ... keyword, cookie 加载不变 ...

    if mode == "incremental" and incre_num:
        self.num_limit = int(incre_num)
    else:
        self.num_limit = int(num) if num else config.MAX_NOTES_COUNT

    self.mode = mode
    self._scheduled = {}
```

### 5b. `from_crawler` 传 mode

```python
@classmethod
def from_crawler(cls, crawler, *args, **kwargs):
    spider = super().from_crawler(crawler, *args, **kwargs)
    mode = getattr(spider, 'mode', None)
    spider.helper = RedisDedupHelper(
        crawler.settings.get("REDIS_URL"),
        spider.name,
        mode=mode or "full",
    )
    return spider
```

### 5c. `start()` — 轮级门控

```python
async def start(self):
    if self.mode == "incremental":
        for request in self._incr_start():
            yield request
        return

    # 全量模式（不变）
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
        self.logger.info("[XHS] keyword=%s collected=%d remaining=%d",
                         keyword, collected, remaining)
        yield self._make_search_request(keyword, page=config.START_PAGE, remaining=remaining)

def _incr_start(self):
    keywords = self.keywords
    h = self.helper

    all_exist = all(h._r.exists(h._kw_key(k)) for k in keywords)
    all_done  = all(h.is_keyword_done(k) for k in keywords)

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
        remaining = target - collected
        self.logger.info("[XHS:incr] keyword=%s target=%d collected=%d remaining=%d",
                         k, target, collected, remaining)
        yield self._make_search_request(k, page=config.START_PAGE, remaining=remaining)
```

### 5d. `parse_search_results()` 不变

`is_note_seen` 查共享 SET，`_scheduled` 本地防超发。

### 5e. `parse_note_detail()` 不变

`mark_note_collected` 写共享 SET，`incr` / `mark_keyword_done` 写 mode 专属计数。

---

## 6. 完整流程示例

### 6a. 第 1 轮增量

```
$ scrapy crawl xiaohongshu -a mode=incremental -a incre_num=5

--- _incr_start() ---
all_exist: False (所有 incr:kw:* key 不存在)
→ register_keyword 全部创建 {target:5, done:0}
→ 依次 yield 搜索请求

--- parse_search_results ---
美食: 搜索结果 20 条
  5条新笔记 is_note_seen=False → 调度
  15条全量已采 is_note_seen=True → dup skip
  采满5条 → 美食 done=1

旅游: 采满5条 → done=1
摄影: 采满5条 → done=1

--- 结果 ---
全部 done=1，第1轮完成
```

### 6b. 第 1 轮中断 → 续爬

```
--- 第1轮中断时状态 ---
美食 done=1, cnt=5
旅游 done=0, cnt=2  ← 采到第2条中断
摄影 done=0, cnt=4  ← 采到第4条中断

$ scrapy crawl xiaohongshu -a mode=incremental -a incre_num=5

--- _incr_start() ---
all_exist: True
all_done: False (旅游、摄影 done=0)
→ 不动任何 target
→ 美食: done=1 → skip

旅游: target=5, collected=2, remaining=3 → yield search_request
摄影: target=5, collected=4, remaining=1 → yield search_request

--- parse_search_results ---
旅游: 全量已采的跳过，补采3条新笔记 → done=1
摄影: 补采1条 → done=1

--- 结果 ---
全部 done=1，第1轮补齐
```

### 6c. 推进到第 2 轮

```
$ scrapy crawl xiaohongshu -a mode=incremental -a incre_num=5

--- _incr_start() ---
all_exist: True
all_done: True
→ advance_round: 全部 target 5→10, done=0

美食: target=10, collected=5, remaining=5
旅游: target=10, collected=5, remaining=5
摄影: target=10, collected=5, remaining=5

--- 再采5条新笔记 per keyword ---
全部 done=1 → 第2轮完成
```

### 6d. 第 2 轮中断 → 续爬

```
--- 第2轮中断时状态 ---
美食 target=10, cnt=5, done=1  ← 仍是上轮的值（本次未开始）
旅游 target=10, cnt=8, done=0  ← 本次采了3条后中断
摄影 target=10, cnt=10, done=1 ← 本次已完成

$ scrapy crawl xiaohongshu -a mode=incremental -a incre_num=5

--- _incr_start() ---
all_done: False → 不动 target
美食: done=1 → skip
旅游: target=10, collected=8, remaining=2 → 补缺
摄影: done=1 → skip

→ 旅游补采2条 → 全部 done=1 → 第2轮完成
```

---

## 7. 边界情况

| 场景 | 行为 |
|------|------|
| 全新 keyword 列表（从未增量） | `all_exist=False` → `register_keyword` 创建，开第 1 轮 |
| 全部 done=1，再跑 | `advance_round` 推进到下一轮 |
| 部分 done=1 部分 done=0 | 不动 target，补缺 undone 的 keyword |
| 没有新笔记可采（翻页到底） | counter 不动，keyword 保持 done=0，不死锁——下次仍会尝试 |
| 增量和全量同时运行 | Redis 原子操作，两条独立推进，共享 notes SET 防重复 |
| `mode=incremental` 不给 `incre_num` | 降级为全量行为 |
| keyword 列表变化（新增 keyword） | `all_exist=False` → 全部重新注册，等于从头开始——**不建议中途改 keyword 列表** |

---

## 8. 已知不足：翻页浪费

增量模式下，大量已采集笔记会被 `is_note_seen` 跳过但仍产生搜索请求。当前阶段可接受，后续可叠加时间截断优化。

---

## 9. 变更文件清单

| 文件 | 改动 |
|------|------|
| `src/deduplication/redis_helper.py` | `__init__` 加 `mode` 参数；`_kw_key` / `_cnt_key` 按 mode 路由；新增 `advance_round`、`get_target`；`register_keyword` 回归简单模式 |
| `src/spiders/socialmedia/xhs/xiaohongshu.py` | `__init__` 加 `mode` / `incre_num` 参数；`from_crawler` 传 mode；`start()` 分流全量/增量；新增 `_incr_start()` 轮级门控 |
