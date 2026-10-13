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
    "mairui": {
        # 麦蕊智数证书（licence）：https://www.mairui.club 免费领取；
        # 也可用环境变量 MAIRUI_LICENCE / EMSCAN_LICENCE 覆盖，避免把证书写进仓库
        "licence": "",
        "base_url": "https://api.mairuiapi.com",
        # 并发线程数：0=按限频自动推算（推荐）；手动填也别超过"每分钟次数/60 × 单个请求耗时"太多
        "workers": 0,
        "timeout": 15.0,
        "retries": 3,
        "sleep": 0.05,
        # 证书限流（次/分钟）：按文档填 300（基础）/1000（包月）/3000（包年）/6000（钻石）
        "rate_limit_per_min": 300,
        # 快照取数方式：
        #   auto（默认）= 多股(≤20 只/次) → 逐只；不试全市场接口（那个要 Pro/包年档）
        #   all = hsrl/real/all 一次拿全市场（仅证书含"全市场"权限时可用）
        "spot_mode": "auto",
        # 多股快照一次最多 20 只（接口上限）
        "multi_batch": 20,
        # 日线参数：d=日线；除权方式 n=不复权 f=前复权 b=后复权 fr/br=等比
        "kline_level": "d",
        "kline_adjust": "f",
        # 回补时每批处理多少只股票：批越大越快，但内存占用越高（400 只 ≈ 30 万行 ≈ 150MB）
        "backfill_batch": 400,
        # 没有流通股本时，是否逐只抓《股票基础信息》补 fv（首次全市场会多花十几分钟）
        "fetch_instrument": False,
    },
    "universe": {
        "boards": ["sh_main", "sh_star", "sz_main", "sz_gem"],
        "exclude_st": True,
        "exclude_suspended_today": True,
        "min_bars": 25,
    },
    # 自愈同步：脚本自己判断"缺了什么"，窗口决定往前比对多少天
    "reconcile_calendar_days": 45,
    "sync": {
        # 首次回补只回补这个日期之后的日线（留空则用下面的 history_years）
        "history_start": "",
        # 回补几年历史（默认 3 年：MA60/BOLL/量能基准都够；0=从头全量，很慢）
        "history_years": 3,
        # 单次运行最多处理多少只股票（0=不限）。限流小或想快速收敛时可设 800
        "max_symbols_per_run": 0,
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
        # 扫描时需要读回多少个自然日的行情：60 个交易日基准 ≈ 90 自然日，
        # 留出春节/国庆等长假冗余，140 足够，且能显著降低内存占用
        "lookback_calendar_days": 140,
        "top_n": 50,
    },
    "report": {
        "formats": ["console", "csv", "xlsx"],
        "title": "A股日频放量扫描",
    },
    "indicators": {
        "enabled": True,
        "kinds": ["macd", "kdj", "boll"],
        # 指标来源：local=本地按日线算（默认，零接口请求、毫秒级、可随时重算）
        #           api  =拉麦蕊指标接口（口径以券商为准，但每天 股票数×种类 次请求）
        "source": "local",
        # 本地计算时，一次处理多少只股票（控制内存；2G 内存保持 300~500）
        "local_chunk": 400,
        # 本地计算时，每天都重算最近几个交易日（判金叉/触轨只用得到今天和昨天）
        "local_days": 5,
        # 日更时只取最近 N 条（0=全部历史）
        "limit": 10,
        # 当日指标几点发布：麦蕊标注日线指标 15:35 更新。早于这个时间运行就跳过当天
        # （否则会为全市场白跑一遍请求），当天稍后/第二天再补。
        "publish_time": "15:35",
    },
    "signals": {
        "enabled": True,
        "title": "A股日频指标信号",
        "match": "any",
        # 默认只报"四个主信号"里的三个指标信号（放量由 scan 负责）：
        # MACD 金叉 / KDJ 金叉 / BOLL 下轨突破
        "rules": ["macd_golden", "kdj_golden", "boll_break_lower"],
        "min_amount": 0,
        "lookback_calendar_days": 30,
        "top_n": 200,
        # 当天指标还没发布时，是否允许退回用最近一个交易日出报告（默认允许，日志会写明）
        "require_trade_date": False,
    },
    "web": {
        # 网页端：python3 -m emscan web
        "host": "127.0.0.1",  # 只本机可访问；要对外就改 0.0.0.0（记得配 token / 反代认证）
        "port": 8080,
        "title": "A股日频信号",
        "limit": 300,  # 单页最多显示多少行
        # 访问口令：设置后浏览器首次访问会跳登录页（Cookie 记住若干天），
        # 脚本/接口也可用 HTTP Basic（curl -u ":$口令"）。留空=开放访问（只适合本机）。
        "password": "",
        "password_env": "EMSCAN_WEB_PASSWORD",
        "session_days": 7,
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
        # 证书（licence）优先读环境变量：避免把证书写进仓库/镜像
        env_licence = os.environ.get("MAIRUI_LICENCE") or os.environ.get("EMSCAN_LICENCE")
        if env_licence:
            data["mairui"]["licence"] = env_licence
        # 网页端的监听/口令也支持环境变量（systemd EnvironmentFile 里放这些最方便）
        env_web = {
            "EMSCAN_WEB_HOST": "host",
            "EMSCAN_WEB_PORT": "port",
            "EMSCAN_WEB_PASSWORD": "password",
            "EMSCAN_WEB_TOKEN": "password",  # 旧名字，兼容已有部署
        }
        for env_key, cfg_key in env_web.items():
            value = os.environ.get(env_key)
            if value:
                data["web"][cfg_key] = int(value) if cfg_key == "port" else value
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
