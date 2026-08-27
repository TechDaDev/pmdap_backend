"""Safe Redis health snapshot for PMDAP operations.

Reads only INFO sections + per-DB DBSIZE. Never SCANs keys, never reads or
prints key payloads, credentials or medical data. Safe to run on demand or on
a schedule (Railway cron / celery beat).

Usage:
  python manage.py ops_redis_health
  python manage.py ops_redis_health --json
"""
import json
import time

import redis as redis_lib
from django.conf import settings
from django.core.management.base import BaseCommand


def _decode(value, default=None):
    if isinstance(value, bytes):
        return value.decode()
    return value


class Command(BaseCommand):
    help = "Snapshot Redis health (memory, RDB persistence, per-DB sizes)."

    def add_arguments(self, parser):
        parser.add_argument("--json", action="store_true", help="emit JSON")

    def _client(self, url):
        return redis_lib.Redis.from_url(
            url,
            socket_connect_timeout=3,
            socket_timeout=5,
            decode_responses=False,
        )

    def handle(self, *args, **options):
        cache_url = getattr(
            settings, "DJANGO_CACHE_URL", "redis://localhost:6379/2"
        )
        result_url = getattr(
            settings, "CELERY_RESULT_BACKEND", "redis://localhost:6379/1"
        )
        broker_url = getattr(
            settings, "CELERY_BROKER_URL", "redis://localhost:6379/0"
        )

        now = int(time.time())
        try:
            cache = self._client(cache_url)
            info = cache.info()
        except Exception as exc:
            self.stdout.write(json.dumps({"error": str(exc)}))
            return

        persistence = info.get("persistence", {})
        stats = info.get("stats", {})
        memory = info.get("memory", {})
        last_save = int(_decode(persistence.get("rdb_last_save_time"), 0) or 0)
        snapshot = {
            "now": now,
            "redis_version": _decode(info.get("server", {}).get("redis_version")),
            "used_memory_bytes": memory.get("used_memory", 0),
            "used_memory_rss_bytes": memory.get("used_memory_rss", 0),
            "rdb_last_bgsave_status": _decode(
                persistence.get("rdb_last_bgsave_status")
            ),
            "rdb_bgsave_in_progress": int(
                persistence.get("rdb_bgsave_in_progress", 0)
            ),
            "rdb_last_save_age_seconds": max(0, now - last_save),
            "rdb_saves": persistence.get("rdb_saves", 0),
            "expired_keys": stats.get("expired_keys", 0),
            "evicted_keys": stats.get("evicted_keys", 0),
            "db_sizes": {},
        }
        for label, url in (
            ("broker", broker_url),
            ("result", result_url),
            ("cache", cache_url),
        ):
            try:
                snapshot["db_sizes"][label] = self._client(url).dbsize()
            except Exception:
                snapshot["db_sizes"][label] = -1

        if options["json"]:
            self.stdout.write(json.dumps(snapshot))
            return
        self.stdout.write(f"redis_version      : {snapshot['redis_version']}")
        self.stdout.write(
            f"used_memory        : {snapshot['used_memory_bytes']/1e6:.0f} MB"
        )
        self.stdout.write(
            f"rdb_last_save_age_s: {snapshot['rdb_last_save_age_seconds']}"
        )
        self.stdout.write(
            f"rdb_status         : {snapshot['rdb_last_bgsave_status']} "
            f"(bgsave_in_progress={snapshot['rdb_bgsave_in_progress']})"
        )
        self.stdout.write(f"expired_keys       : {snapshot['expired_keys']}")
        self.stdout.write(f"evicted_keys       : {snapshot['evicted_keys']}")
        self.stdout.write(f"db_sizes           : {snapshot['db_sizes']}")
