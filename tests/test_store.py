import sqlite3

import pytest


def test_init_keeps_sqlite_state_and_uses_wal(tmp_path):
    from agy_pool.store import PoolStore

    store = PoolStore(tmp_path / 'pool.db')
    store.init_db()
    store.record_event('example', detail={'value': 'kept'})
    store.init_db()
    with store.transaction(write=False) as tx:
        assert tx.execute('PRAGMA journal_mode').fetchone()[0] == 'wal'
        assert tx.execute('SELECT event_type FROM events').fetchone()[0] == 'example'
        assert tx.execute('PRAGMA busy_timeout').fetchone()[0] == 5000


def test_rollback_prevents_partial_writes(tmp_path):
    from agy_pool.store import PoolStore

    store = PoolStore(tmp_path / 'pool.db')
    store.init_db()
    with pytest.raises(RuntimeError), store.transaction() as tx:
        tx.execute('INSERT INTO accounts(account_id,status) VALUES (?,?)', ('main', 'ready'))
        raise RuntimeError('fixture')
    assert store.get_account('main') is None


@pytest.mark.parametrize('detail', [{'nested': [{'api_key': 'value'}]}, {'note': 'demo@example.test'}, {'note': 'Bearer fake-secret'}])
def test_event_refuses_sensitive_input(tmp_path, detail):
    from agy_pool.store import PoolStore, SensitiveData

    store = PoolStore(tmp_path / 'pool.db')
    store.init_db()
    with pytest.raises(SensitiveData):
        store.record_event('example', detail=detail)
    with store.transaction(write=False) as tx:
        assert tx.execute('SELECT COUNT(*) FROM events').fetchone()[0] == 0


def test_two_databases_in_same_home_get_distinct_artifacts(tmp_path):
    from agy_pool.store import PoolStore

    a = PoolStore(tmp_path / 'a.db', tmp_path)
    b = PoolStore(tmp_path / 'b.db', tmp_path)
    for store in (a, b):
        store.init_db()
    a.artifact('anchors/T1.md').write_text('ONLY_A')
    b.artifact('anchors/T1.md').write_text('ONLY_B')
    assert a.artifact('anchors/T1.md').read_text() == 'ONLY_A'
    assert a.artifact('probes/seg-T1-seg1.json') != b.artifact('probes/seg-T1-seg1.json')
    with pytest.raises(ValueError):
        a.artifact('../escape')


def test_migrate_v0_creates_consistent_backup(tmp_path):
    from contextlib import closing

    from agy_pool.store import PoolStore

    db = tmp_path / 'pool.db'
    with closing(sqlite3.connect(db)) as old:
        old.execute('PRAGMA journal_mode=WAL')
        old.execute('CREATE TABLE meta(key TEXT PRIMARY KEY, value TEXT NOT NULL)')
        old.executemany('INSERT INTO meta VALUES (?,?)', [('schema_version', '0'), ('next_thread_seq', '17')])
        old.commit()
        PoolStore(db).init_db()
        with closing(sqlite3.connect(tmp_path / 'pool.db.v0.bak')) as backup:
            assert dict(backup.execute('SELECT key,value FROM meta')) == {'schema_version': '0', 'next_thread_seq': '17'}
    with PoolStore(db).transaction(write=False) as tx:
        assert tx.get_meta('schema_version') == '2'
        assert tx.get_meta('next_thread_seq') == '17'


def test_future_schema_is_rejected_without_modification(tmp_path):
    from agy_pool.store import PoolStore, StoreError

    db = tmp_path / 'pool.db'
    with sqlite3.connect(db) as conn:
        conn.execute('CREATE TABLE meta(key, value)')
        conn.execute("INSERT INTO meta VALUES ('schema_version', '99')")
    with pytest.raises(StoreError):
        PoolStore(db).init_db()
    with sqlite3.connect(db) as conn:
        assert conn.execute("SELECT value FROM meta WHERE key='schema_version'").fetchone()[0] == '99'
