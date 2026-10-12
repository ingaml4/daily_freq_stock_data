"""可选推送：webhook（飞书/企业微信/钉钉/Slack）与邮件。

设计原则：推送失败绝不能影响扫描任务本身，所以全部包在 try/except 里，
失败只记日志。
"""

from __future__ import annotations

import json
import logging
import os
import smtplib
import ssl
from email.message import EmailMessage
from typing import Any, Dict, List

import requests

from .report import build_summary, render_table
from .scan import ScanResult

log = logging.getLogger(__name__)

WEBHOOK_PAYLOAD_BUILDERS = {
    "feishu": lambda text: {"msg_type": "text", "content": {"text": text}},
    "wecom": lambda text: {"msgtype": "text", "text": {"content": text}},
    "dingtalk": lambda text: {"msgtype": "text", "text": {"content": text}},
    "slack": lambda text: {"text": text},
}


def build_text(result: ScanResult, cfg) -> str:
    """拼出推送正文：摘要 + Top N 明细。"""
    max_items = int(cfg.get("notify.max_items", 20) or 20)
    parts = [build_summary(result, cfg)]
    if len(result.hits):
        parts.append(render_table(result.hits, max_rows=max_items))
    else:
        parts.append("今日没有触发放量条件的股票。")
    return "\n".join(parts)


def send_webhook(text: str, cfg) -> bool:
    url = str(cfg.get("notify.webhook.url") or "").strip()
    if not url:
        log.warning("notify.webhook.url 为空，跳过 webhook 推送")
        return False
    kind = str(cfg.get("notify.webhook.type") or "feishu").lower()
    builder = WEBHOOK_PAYLOAD_BUILDERS.get(kind)
    if builder is None:
        log.warning("未知的 webhook 类型 %s，跳过", kind)
        return False
    try:
        resp = requests.post(
            url,
            data=json.dumps(builder(text), ensure_ascii=False).encode("utf-8"),
            headers={"Content-Type": "application/json"},
            timeout=15,
        )
        ok = resp.status_code == 200
        if not ok:
            log.warning("webhook 推送失败：HTTP %s %s", resp.status_code, resp.text[:200])
        return ok
    except Exception as exc:  # pragma: no cover - 网络异常
        log.warning("webhook 推送异常：%s", exc)
        return False


def send_email(text: str, cfg) -> bool:
    email_cfg = cfg.get("notify.email") or {}
    if not email_cfg.get("enabled"):
        return False
    host = str(email_cfg.get("host") or "").strip()
    sender = str(email_cfg.get("sender") or "").strip()
    recipients: List[str] = [str(r) for r in (email_cfg.get("recipients") or [])]
    if not (host and sender and recipients):
        log.warning("邮件配置不完整（host/sender/recipients），跳过邮件推送")
        return False
    password = os.environ.get(str(email_cfg.get("password_env") or "EMSCAN_SMTP_PASSWORD"), "")
    if not password:
        log.warning("未设置 %s 环境变量，跳过邮件推送", email_cfg.get("password_env"))
        return False

    message = EmailMessage()
    message["Subject"] = str(cfg.get("report.title", "A股日频放量扫描"))
    message["From"] = sender
    message["To"] = ", ".join(recipients)
    message.set_content(text)

    try:
        port = int(email_cfg.get("port") or 465)
        if email_cfg.get("ssl", True):
            context = ssl.create_default_context()
            with smtplib.SMTP_SSL(host, port, context=context, timeout=20) as server:
                server.login(email_cfg.get("user") or sender, password)
                server.send_message(message)
        else:
            with smtplib.SMTP(host, port, timeout=20) as server:
                server.starttls(context=ssl.create_default_context())
                server.login(email_cfg.get("user") or sender, password)
                server.send_message(message)
        log.info("邮件已发送给 %s", recipients)
        return True
    except Exception as exc:  # pragma: no cover
        log.warning("邮件推送失败：%s", exc)
        return False


def send(result: ScanResult, cfg) -> Dict[str, Any]:
    """按配置推送。返回每个通道的结果，便于日志记录。"""
    outcome: Dict[str, Any] = {"enabled": bool(cfg.get("notify.enabled", False))}
    if not outcome["enabled"]:
        return outcome
    text = build_text(result, cfg)
    outcome["webhook"] = send_webhook(text, cfg)
    outcome["email"] = send_email(text, cfg)
    return outcome
