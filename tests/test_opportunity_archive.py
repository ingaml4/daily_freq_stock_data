import json
from types import SimpleNamespace

import pytest

from emscan import opportunity_archive as archive


@pytest.fixture
def setup(tmp_path, monkeypatch):
    store = SimpleNamespace(path=tmp_path / 'stock.sqlite', conn=object())
    calls = []
    def build(conn, day):
        calls.append(day)
        return {'schema_version': '1.0', 'date': day, 'candidates': [],
                'coverage': {}, 'mainline': {}, 'value': len(calls)}
    monkeypatch.setattr(archive, 'build_personal_opportunity_payload', build)
    monkeypatch.setattr(archive, 'code_version', lambda: 'test-revision')
    return store, calls


def test_missing_archive_is_explicit_recomputation(setup):
    store, calls = setup
    result = archive.report(store, '2026-11-11')
    assert result['provenance']['mode'] == 'recomputed'
    assert '非历史时点实收' in result['provenance']['description']
    assert result['generated_at'] and result['code_version'] == 'test-revision'
    assert calls == ['2026-11-11']


def test_materialize_keeps_versions_and_read_uses_saved_content(setup):
    store, calls = setup
    first = archive.materialize(store, '2026-11-11')
    second = archive.materialize(store, '2026-11-11')
    folder = store.path.parent / 'personal-opportunities'
    versions = list(folder.glob('2026-11-11.*.json'))
    assert len(versions) == 2
    assert {json.loads(p.read_text(encoding='utf-8'))['value'] for p in versions} == {1, 2}
    assert first['provenance']['mode'] == 'archived'
    assert '不能据此认定为历史原始时点快照' in first['provenance']['description']
    assert archive.report(store, '2026-11-11') == second
    assert len(calls) == 2


def test_bad_archive_does_not_impersonate_valid_snapshot(setup):
    store, calls = setup
    folder = store.path.parent / 'personal-opportunities'
    folder.mkdir()
    (folder / '2026-11-11.json').write_text('{bad', encoding='utf-8')
    assert archive.report(store, '2026-11-11')['provenance']['mode'] == 'recomputed'
    (folder / '2026-11-11.json').write_text('{"date":"2026-11-11"}', encoding='utf-8')
    assert archive.report(store, '2026-11-11')['provenance']['mode'] == 'recomputed'
    assert len(calls) == 2


def test_date_validation_prevents_path_traversal(setup):
    store, _ = setup
    with pytest.raises(ValueError):
        archive.materialize(store, '../unsafe')
    with pytest.raises(ValueError):
        archive.report(store, '2026-1-1')
