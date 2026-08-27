"""Tests for the ops_redis_health management command (safe INFO/DBSIZE snapshot)."""
from io import StringIO

from django.core.management import call_command

from opsconsole.management.commands import ops_redis_health as mod


class FakeClient:
    def __init__(self, info):
        self._info = info

    def info(self):
        return self._info

    def dbsize(self):
        return 7


BASE_INFO = {
    "server": {"redis_version": "8.2.1"},
    "memory": {"used_memory": 1000, "used_memory_rss": 1200},
    "persistence": {
        "rdb_last_bgsave_status": "ok",
        "rdb_bgsave_in_progress": 0,
        "rdb_last_save_time": 100,
        "rdb_saves": 5,
    },
    "stats": {"expired_keys": 3, "evicted_keys": 0},
}


def test_ops_redis_health_json(monkeypatch):
    fake = FakeClient(BASE_INFO)
    monkeypatch.setattr(
        mod.redis_lib.Redis, "from_url", staticmethod(lambda *a, **kw: fake)
    )
    out = StringIO()
    call_command("ops_redis_health", "--json", stdout=out)
    out = out.getvalue()
    assert '"used_memory_bytes": 1000' in out
    assert '"rdb_last_bgsave_status": "ok"' in out
    assert '"db_sizes"' in out
    assert 'result' in out


def test_ops_redis_health_text(monkeypatch):
    fake = FakeClient(BASE_INFO)
    monkeypatch.setattr(
        mod.redis_lib.Redis, "from_url", staticmethod(lambda *a, **kw: fake)
    )
    out = StringIO()
    call_command("ops_redis_health", stdout=out)
    out = out.getvalue()
    assert "rdb_status" in out
    assert "db_sizes" in out


def test_ops_redis_health_redis_down(monkeypatch):
    def boom(*a, **kw):
        raise RuntimeError("redis down")

    monkeypatch.setattr(mod.redis_lib.Redis, "from_url", boom)
    out = StringIO()
    call_command("ops_redis_health", "--json", stdout=out)
    out = out.getvalue()
    assert '"error"' in out
