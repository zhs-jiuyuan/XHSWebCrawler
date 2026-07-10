"""
小红书爬虫配置

所有爬取参数集中在此文件管理，爬虫通过 `from . import xhs_config as config` 引用。
"""

# ==================== 爬取配置 ====================
CRAWLER_TYPE = "search"     # search
KEYWORD = "美食"             # 搜索关键词
START_PAGE = 1              # 起始页码
MAX_NOTES_COUNT = 3         # 最大笔记数

# ==================== 排序和类型 ====================
SORT_TYPE = "general"       # general | popularity_descending | time_descending
NOTE_TYPE = 0               # 0=不限 1=视频 2=图文

# ==================== Cookie ====================
COOKIE_FILE = "xhs_cookies.json"
