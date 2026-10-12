"""配置加载：默认值 + config.yaml 深合并，并提供点号取值与路径解析。"""

from __future__ import annotations

import copy
import os
from pathlib import Path
from typing import Any, Dict, Optional

import yaml

DEFAULTS: Dict[str, Any] = {
    "store": {
        "path": "data/market.sqlite",
        "reports_dir": "data/reports",
        "logs_dir": "logs",
    },
    "eastmoney": {
        "workers": 8,
        "timeout": 15.0,
        "retries": 3,
        "sleep": 0.05,
        "kline_fqt": 1,
        "spot_page_size": 100,
    },
    "universe": {
        "boards": ["sh_main", "sh_star", "sz_main", "sz_gem"],
        "exclude_st": True,
        "exclude_suspended_today": True,
        "min_bars": 25,
    },
    "scan": {
        "windows": [5, 10, 20, 60],
        "baseline_window": 60,
        "min_amount": 50_000_000,
        "min_turnover": 0.5,
        "vol_ratio_5_min": 2.0,
        "vol_ratio_20_min": 1.5,
        "z_min": 3.0,
        "min_valid_in_window": 2,
        "top_n": 50,
    },
    "report": {
        "formats": ["console", "csv", "xlsx"],
        "title": "A股日频放量扫描",
    },
    "notify": {
        "enabled": False,
        "webhook": {"type": "feishu", "url": ""},
        "email": {
            "enabled": False,
            "host": "",
            "port": 465,
            "ssl": True,
            "user": "",
            "password_env": "EMSCAN_SMTP_PASSWORD",
            "sender": "",
            "recipients": [],
        },
        "max_items": 20,
    },
}


def deep_merge(base: Dict[str, Any], override: Dict[str, Any]) -> Dict[str, Any]:
    """把 override 合并进 base（递归覆盖，返回新对象）。"""
    out = copy.deepcopy(base)
    for key, value in (override or {}).items():
        if isinstance(value, dict) and isinstance(out.get(key), dict):
            out[key] = deep_merge(out[key], value)
        else:
            out[key] = copy.deepcopy(value)
    return out


class Config:
    """配置访问器。

    支持 ``cfg.get("scan.min_amount")`` 点号取值，以及 ``cfg.path("store.path")``
    返回基于配置文件目录解析后的绝对路径。
    """

    def __init__(self, data: Dict[str, Any], root: Path, source: Optional[Path] = None):
        self._data = data
        self.root = Path(root)
        self.source = source

    # ------------------------------------------------------------------ 加载
    @classmethod
    def load(cls, path: Optional[str] = None) -> "Config":
        candidate = Path(path).expanduser().resolve() if path else Path.cwd() / "config.yaml"
        user: Dict[str, Any] = {}
        source: Optional[Path] = None
        if candidate.exists():
            with open(candidate, "r", encoding="utf-8") as fh:
                user = yaml.safe_load(fh) or {}
            source = candidate
        data = deep_merge(DEFAULTS, user)
        root = candidate.parent if candidate.exists() else Path.cwd()
        # 允许用环境变量覆盖行情库位置，便于在别的机器/容器里跑
        env_store = os.environ.get("EMSCAN_STORE_PATH")
        if env_store:
            data["store"]["path"] = env_store
        return cls(data, root=root, source=source)

    # ------------------------------------------------------------------ 取值
    def get(self, dotted: str, default: Any = None) -> Any:
        node: Any = self._data
        for part in dotted.split("."):
            if not isinstance(node, dict) or part not in node:
                return default
            node = node[part]
        return node

    def __getitem__(self, key: str) -> Any:
        return self._data[key]

    def as_dict(self) -> Dict[str, Any]:
        return copy.deepcopy(self._data)

    # ------------------------------------------------------------------ 路径
    def path(self, dotted: str, default: Optional[str] = None) -> Path:
        raw = self.get(dotted, default)
        if raw is None:
            raise KeyError(f"配置项 {dotted} 不存在且没有默认值")
        p = Path(str(raw)).expanduser()
        return p if p.is_absolute() else (self.root / p)

    def ensure_dirs(self) -> None:
        for key in ("store.path", "store.reports_dir", "store.logs_dir"):
            target = self.path(key)
            (target if target.suffix == "" else target.parent).mkdir(parents=True, exist_ok=True)
