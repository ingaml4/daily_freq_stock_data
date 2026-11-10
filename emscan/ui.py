"""同源前端的静态资源与只读页面数据。"""

import mimetypes
from pathlib import Path
from urllib.parse import unquote


def asset(dist, url_path):
    if dist is None:
        return None
    root = Path(dist).resolve()
    relative = unquote(url_path.removeprefix("/ui/"))
    try:
        target = (root / relative).resolve()
        valid = root in target.parents and target.is_file()
    except (ValueError, OSError):
        valid = False
    if not valid:
        return None
    mime = mimetypes.guess_type(target.name)[0] or "application/octet-stream"
    return target.read_bytes(), mime


def summary(store, cfg, requested_date=None):
    from .readiness import report

    dates = [r[0] for r in store.conn.execute("SELECT date FROM daily_summary ORDER BY date DESC")]
    day = requested_date or (dates[0] if dates else None)
    record = store.get_summary(day) if day else None
    result = {key: (record or {}).get(key) for key in ("date", "engine", "created_at", "content")}
    result.update(date=day, dates=dates, missing=not bool(record), content=result["content"] or "",
                  publication=store.publication_status(day),
                  coverage_state=report(store, cfg, day)["state"] if day else "waiting")
    return result
