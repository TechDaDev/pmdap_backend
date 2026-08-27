"""Tests for the safe purge_stale_celery_results management command.

Covers: dry-run default (no deletion), execute deletes only stale
celery-task-meta keys, fresh keys kept, protected task IDs kept,
persistent (no-TTL) keys kept unless --include-persistent, other
prefixes never touched.
"""
import time

import pytest
from django.core.management import call_command

from opsconsole.management.commands import purge_stale_celery_results as mod

OLD_TTL = 3600  # 1h remaining -> created ~23h ago (stale)
FRESH_TTL = 23 * 3600  # 23h remaining -> created ~1h ago (fresh)


class FakePipeline:
    def __init__(self, client):
        self._client = client
        self._cmds = []

    def ttl(self, k):
        self._cmds.append(("ttl", k))
        return self

    def execute(self):
        return [self._client.data[k]["ttl"] for (_cmd, k) in self._cmds]


class FakeRedis:
    """Minimal SCAN/TTL/UNLINK fake over a dict of keys."""

    def __init__(self, keys):
        # keys: {name: {"ttl": int}}
        self.data = dict(keys)
        self.unlinked = []

    @property
    def connection_pool(self):
        class _cp:
            connection_kwargs = {"db": 1}

        return _cp()

    def scan(self, cursor=0, match=None, count=None):
        prefix = (match or "").rstrip("*")
        names = [n for n in self.data if n.startswith(prefix)]
        return 0, names

    def pipeline(self, transaction=False):
        return FakePipeline(self)

    def unlink(self, *keys):
        for k in keys:
            if k in self.data:
                del self.data[k]
                self.unlinked.append(k)
        return len(keys)

    def dbsize(self):
        return len(self.data)


def build_cmd(monkeypatch, keys):
    fake = FakeRedis(keys)
    # Patch only from_url on the real class; the command calls Redis.from_url.
    monkeypatch.setattr(
        mod.redis_lib.Redis, "from_url", staticmethod(lambda *a, **kw: fake)
    )
    return fake


def make_key(name, ttl):
    return {f"celery-task-meta-{name}": {"ttl": ttl}}


def test_dry_run_does_not_delete(monkeypatch):
    fake = build_cmd(
        monkeypatch,
        {**make_key("a", OLD_TTL), **make_key("b", FRESH_TTL)},
    )
    call_command("purge_stale_celery_results", older_than_hours=1)
    assert fake.unlinked == []
    assert len(fake.data) == 2


def test_execute_deletes_only_stale(monkeypatch):
    fake = build_cmd(
        monkeypatch,
        {
            **make_key("stale1", OLD_TTL),
            **make_key("stale2", OLD_TTL),
            **make_key("fresh1", FRESH_TTL),
            **make_key("fresh2", FRESH_TTL),
        },
    )
    call_command("purge_stale_celery_results", older_than_hours=1, execute=True)
    assert sorted(fake.unlinked) == sorted(["celery-task-meta-stale1",
                                            "celery-task-meta-stale2"])
    assert set(fake.data) == {"celery-task-meta-fresh1", "celery-task-meta-fresh2"}


def test_protected_task_ids_never_deleted(monkeypatch, tmp_path):
    fake = build_cmd(
        monkeypatch,
        {
            **make_key("protected", OLD_TTL),
            **make_key("regular", OLD_TTL),
        },
    )
    # Without a protected list the stale key is deleted.
    call_command(
        "purge_stale_celery_results",
        older_than_hours=1,
        execute=True,
        protected_task_ids_file="",
    )
    assert "celery-task-meta-protected" in fake.unlinked
    # With the protected list the key is kept.
    fake2 = build_cmd(
        monkeypatch,
        {
            **make_key("protected", OLD_TTL),
            **make_key("regular", OLD_TTL),
        },
    )
    p = tmp_path / "prot.txt"
    p.write_text("protected\n")
    call_command(
        "purge_stale_celery_results",
        older_than_hours=1,
        execute=True,
        protected_task_ids_file=str(p),
    )
    assert "celery-task-meta-protected" not in fake2.unlinked
    assert "celery-task-meta-regular" in fake2.unlinked
    assert "celery-task-meta-protected" in fake2.data


def test_persistent_keys_kept_unless_flag(monkeypatch):
    persistent = {"celery-task-meta-keep": {"ttl": -1}}
    fake = build_cmd(monkeypatch, dict(persistent))
    call_command("purge_stale_celery_results", older_than_hours=1, execute=True)
    assert fake.unlinked == []
    # With the flag it is deleted.
    fake2 = build_cmd(monkeypatch, dict(persistent))
    call_command(
        "purge_stale_celery_results",
        older_than_hours=1,
        execute=True,
        include_persistent=True,
    )
    assert fake2.unlinked == ["celery-task-meta-keep"]


def test_other_prefixes_never_touched(monkeypatch):
    fake = build_cmd(
        monkeypatch,
        {
            "pmdap:ops:railway:collector": {"ttl": OLD_TTL},
            "identity:extract:abc": {"ttl": OLD_TTL},
        },
    )
    call_command("purge_stale_celery_results", older_than_hours=1, execute=True)
    assert fake.unlinked == []
    assert len(fake.data) == 2
