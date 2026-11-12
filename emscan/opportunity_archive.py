"""Archive opportunity preparations as observed, never backdate a recomputation."""
import json
import os
import tempfile
from datetime import date
from pathlib import Path
from uuid import uuid4

from .personal_opportunity import build_personal_opportunity_payload
from .readiness import code_version
from .tzutil import now


def _day(day):
    if not isinstance(day, str) or date.fromisoformat(day).isoformat() != day:
        raise ValueError('date must be YYYY-MM-DD')
    return day


def _directory(store):
    return Path(store.path).parent / 'personal-opportunities'


def _payload(store, day, mode):
    result = build_personal_opportunity_payload(store.conn, day)
    generated = now().isoformat(timespec='microseconds')
    result.update(generated_at=generated, code_version=code_version(), provenance={
        'mode': mode,
        'description': ('保存生成时实际计算的结果；生成时间可能晚于行情日期，不能据此认定为历史原始时点快照。'
                        if mode == 'archived' else
                        '按当前数据库重算，非历史时点实收归档；历史行情和证券资料可能已被覆盖。'),
    })
    return result


def _atomic_write(path, content):
    fd, name = tempfile.mkstemp(prefix='.opportunity-', suffix='.tmp', dir=path.parent)
    try:
        with os.fdopen(fd, 'w', encoding='utf-8') as output:
            output.write(content)
            output.flush()
            os.fsync(output.fileno())
        os.replace(name, path)
    finally:
        if os.path.exists(name):
            os.unlink(name)


def materialize(store, day):
    """Save a version and atomically update the day file; returns saved payload."""
    day = _day(day)
    result = _payload(store, day, 'archived')
    directory = _directory(store)
    directory.mkdir(parents=True, exist_ok=True)
    encoded = json.dumps(result, ensure_ascii=False, allow_nan=False, indent=2) + '\n'
    stamp = result['generated_at'].replace(':', '').replace('-', '').replace('.', '')
    version = directory / f'{day}.{stamp}.{uuid4().hex}.json'
    _atomic_write(version, encoded)
    _atomic_write(directory / f'{day}.json', encoded)
    return result


def report(store, day):
    """Read a valid day archive or disclose that the result was recomputed."""
    day = _day(day)
    path = _directory(store) / f'{day}.json'
    try:
        with path.open(encoding='utf-8') as source:
            result = json.load(source)
        if (isinstance(result, dict) and result.get('date') == day
                and result.get('schema_version') == '1.0'
                and isinstance(result.get('candidates'), list)
                and isinstance(result.get('coverage'), dict)
                and isinstance(result.get('mainline'), dict)
                and isinstance(result.get('generated_at'), str)
                and isinstance(result.get('code_version'), str)
                and isinstance(result.get('provenance'), dict)
                and result['provenance'].get('mode') == 'archived'):
            return result
    except (OSError, ValueError, TypeError):
        pass
    return _payload(store, day, 'recomputed')
