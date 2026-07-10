import os
import re
import traceback
import warnings
import yaml

from pathlib import Path
from dotenv import load_dotenv

PROJECT_ROOT = Path(__file__).resolve().parent.parent
load_dotenv(PROJECT_ROOT / ".env")


try:
    from scrapy.exceptions import ScrapyDeprecationWarning
    warnings.filterwarnings(action="ignore", category=ScrapyDeprecationWarning)
except ImportError:
    pass


def load_yaml_config(config_name="base"):
    config_dir = Path(__file__).parent
    config_file = config_dir / f"{config_name}.yaml"

    if not config_file.exists():
        raise FileNotFoundError(f"Config file not found: '{config_file}'.")

    with open(config_file, "r", encoding="utf-8") as f:
        config = yaml.safe_load(f)

    config = substitute_env_vars(config)

    return config


def substitute_env_vars(obj):
    if isinstance(obj, dict):
        return {key: substitute_env_vars(value) for key, value in obj.items()}
    elif isinstance(obj, list):
        return [substitute_env_vars(item) for item in obj]
    elif isinstance(obj, str):
        pattern = r'\$\{([^}:]+)(?::([^}]*))?\}'

        def replace_var(match):
            var_name = match.group(1)
            default_value = match.group(2) or ""
            return os.getenv(var_name, default_value)


        return re.sub(pattern, replace_var, obj)
    else:
        return obj


SECTION_MAP = {
    "scrapy": {
        "bot_name": "BOT_NAME",
        "spider_modules": "SPIDER_MODULES",
        "newspider_module": "NEWSPIDER_MODULE",
        "robotstxt_obey": "ROBOTSTXT_OBEY",
    },
    "concurrency": {
        "concurrent_requests": "CONCURRENT_REQUESTS",
        "concurrent_requests_per_domain": "CONCURRENT_REQUESTS_PER_DOMAIN",
        "download_delay": "DOWNLOAD_DELAY",
        "randomize_download_delay": "RANDOMIZE_DOWNLOAD_DELAY",
    },
    "timeout": {
        "download_timeout": "DOWNLOAD_TIMEOUT",
        "retry_times": "RETRY_TIMES",
        "retry_http_codes": "RETRY_HTTP_CODES",
    },
    "logging": {
        "level": "LOG_LEVEL",
        "file": "LOG_FILE",
    },
    "autothrottle": {
        "enabled": "AUTOTHROTTLE_ENABLED",
        "start_delay": "AUTOTHROTTLE_START_DELAY",
        "max_delay": "AUTOTHROTTLE_MAX_DELAY",
        "target_concurrency": "AUTOTHROTTLE_TARGET_CONCURRENCY",
        "debug": "AUTOTHROTTLE_DEBUG",
    },
    "httpcache": {
        "enabled": "HTTPCACHE_ENABLED",
        "expiration_secs": "HTTPCACHE_EXPIRATION_SECS",
        "dir": "HTTPCACHE_DIR",
    },
    "task": {
        "id_format": "TASK_ID_FORMAT",
        "batch_size": "BATCH_SIZE",
    },
    "postgres": {
        "url": "POSTGRES_URL",
    },
}

DEFAULTS = {
    "BOT_NAME": "commonspider",
    "SPIDER_MODULES": ["src.spiders"],
    "NEWSPIDER_MODULE": "src.spiders",
    "ROBOTSTXT_OBEY": False,
    "CONCURRENT_REQUESTS": 32,
    "CONCURRENT_REQUESTS_PER_DOMAIN": 16,
    "DOWNLOAD_DELAY": 2,
    "RANDOMIZE_DOWNLOAD_DELAY": 1,
    "DOWNLOAD_TIMEOUT": 30,
    "RETRY_TIMES": 3,
    "RETRY_HTTP_CODES": [500, 502, 503, 504, 408, 429],
    "LOG_LEVEL": "INFO",
    "LOG_FILE": "logs/scrapy.log",
    "AUTOTHROTTLE_ENABLED": True,
    "AUTOTHROTTLE_START_DELAY": 1,
    "AUTOTHROTTLE_MAX_DELAY": 60,
    "AUTOTHROTTLE_TARGET_CONCURRENCY": 2.0,
    "AUTOTHROTTLE_DEBUG": False,
    "HTTPCACHE_ENABLED": False,
    "HTTPCACHE_EXPIRATION_SECS": 3600,
    "HTTPCACHE_DIR": "httpcache",
    "TASK_ID_FORMAT": "{spider_name}_{date}_{timestamp}",
    "BATCH_SIZE": 1000,
    "POSTGRES_URL": "postgresql://localhost/commonspider",
}


def yaml_to_scrapy_settings(config):
    settings = {}

    for section_name, field_map in SECTION_MAP.items():
        if section_name in config:
            section = config[section_name]
            if section is None:
                continue
            for yaml_key, scrapy_key in field_map.items():
                settings[scrapy_key] = section.get(yaml_key, DEFAULTS.get(scrapy_key))

    if "middlewares" in config:
        middlewares = config["middlewares"]
        if middlewares is not None and "downloader" in middlewares:
            settings["DOWNLOAD_MIDDLEWARES"] = middlewares["downloader"]

    if "pipelines" in config:
        pipelines = config["pipelines"]
        if pipelines is not None and "item" in pipelines:
            settings["ITEM_PIPELINES"] = pipelines["item"]

    if "redis" in config:
        redis_config = config["redis"]
        if redis_config is not None:
            settings["REDIS_URL"] = redis_config.get("url", "redis://localhost:6379")

            if redis_config.get("dupefilter_class"):
                settings["DUPEFILTER_CLASS"] = redis_config.get("dupefilter_class")
            if redis_config.get("scheduler"):
                settings["SCHEDULER"] = redis_config.get("scheduler")
                settings["SCHEDULER_PERSIST"] = redis_config.get("scheduler_persist", True)

                if "bloom_params" in redis_config:
                    bloom_params = redis_config["bloom_params"]
                    settings["REDIS_BLOOM_PARAMS"] = {
                            "redis_url": settings["REDIS_URL"],
                            "hash_number": bloom_params.get("hash_number", 6),
                            "bit": bloom_params.get("bit", 30),
                            }

    if "proxy" in config:
        proxy_config = config["proxy"]
        if proxy_config is not None:
            settings["PROXY_API_URL"] = proxy_config.get("api_url", "http://localhost:5010/get")
            settings["PROXY_ENABLED"] = str(proxy_config.get("enabled", True)).lower() == "true"

    if "monitoring" in config:
        monitoring = config["monitoring"]
        if monitoring is not None:
            if monitoring.get("stats_class"):
                settings["STATS_CLASS"] = monitoring.get("stats_class")
            settings["TELNETCONSOLE_ENABLED"] = monitoring.get("telnetconsole_enabled", False)

    settings["REQUEST_FINGERPRINTER_IMPLEMENTATION"] = config.get("request_fingerprinter_implementation", "2.7")

    return settings


ENV = os.getenv("SCRAPY_ENV", "base")

try:
    config = load_yaml_config(ENV)
    scrapy_settings = yaml_to_scrapy_settings(config)

    globals().update(scrapy_settings)

except Exception as e:
    traceback.print_exc()
    print(f"Error loading YAML config: {e}, fallback to default settings.")

    BOT_NAME = "commonspider"
    SPIDER_MODULES = ["src.spiders"]


