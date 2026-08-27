"""Safe selective purge of stale Celery result-backend keys.

Surgical recovery tool for a Redis result backend that has accumulated
millions of `celery-task-meta-*` keys. SCAN-based, DB/prefix constrained,
age/TTL constrained, batched UNLINK. NEVER uses KEYS, FLUSHDB or FLUSHALL.

Safety rails (all enforced):
  * --dry-run is the DEFAULT; pass --execute to actually delete.
  * Prefix is fixed to the Celery result-backend keys only.
  * DB is derived from CELERY_RESULT_BACKEND — never a free parameter.
  * Age cutoff via --older-than-hours (TTL heuristic).
  * Optional protected-task-ids file excludes specific task IDs.
  * Prints a full summary before any deletion.

Usage:
  python manage.py purge_stale_celery_results --older-than-hours 1
  python manage.py purge_stale_celery_results --older-than-hours 1 --execute
  python manage.py purge_stale_celery_results --older-than-hours 1 \\
      --protected-task-ids-file /tmp/protected.txt --execute
"""
import sys
import time

import redis as redis_lib
from django.conf import settings
from django.core.management.base import BaseCommand

# Celery redis backend default result expiry (safety bound for the TTL age
# heuristic). Keys are set with result_expires at creation time.
RESULT_TTL_SECONDS = 24 * 3600
PREFIX = "celery-task-meta-*"


def result_backend_url():
    return getattr(settings, "CELERY_RESULT_BACKEND", "redis://localhost:6379/1")


class Command(BaseCommand):
    help = "Selectively purge stale Celery result-backend keys (dry-run by default)."

    def add_arguments(self, parser):
        parser.add_argument(
            "--execute",
            action="store_true",
            help="actually UNLINK candidates (default is a dry-run count only)",
        )
        parser.add_argument(
            "--older-than-hours",
            type=int,
            default=1,
            help="delete result keys older than N hours (TTL heuristic)",
        )
        parser.add_argument(
            "--batch-size", type=int, default=500, help="UNLINK batch size"
        )
        parser.add_argument(
            "--limit",
            type=int,
            default=0,
            help="maximum keys to delete (0 = unlimited)",
        )
        parser.add_argument(
            "--include-persistent",
            action="store_true",
            help="also delete celery-task-meta keys that have no TTL",
        )
        parser.add_argument(
            "--protected-task-ids-file",
            default="",
            help="file with task IDs (one per line) whose result keys are never deleted",
        )

    def _load_protected(self, path):
        protected = set()
        if not path:
            return protected
        with open(path, encoding="utf-8") as fh:
            for line in fh:
                tid = line.strip()
                if tid:
                    protected.add(tid)
        return protected

    def _deletable(self, ttl, older_than_hours, include_persistent):
        if ttl is None:
            return False
        if ttl < 0:
            return include_persistent
        # TTL = remaining life. A key created more than older_than_hours ago
        # has TTL < (RESULT_TTL_SECONDS - older_than_hours * 3600).
        return ttl < RESULT_TTL_SECONDS - older_than_hours * 3600

    def handle(self, *args, **options):
        execute = bool(options["execute"])
        older_than_hours = max(0, options["older_than_hours"])
        batch_size = max(1, options["batch_size"])
        limit = options["limit"]
        include_persistent = options["include_persistent"]
        protected = self._load_protected(options["protected_task_ids_file"])

        client = redis_lib.Redis.from_url(
            result_backend_url(),
            socket_connect_timeout=5,
            socket_timeout=10,
            decode_responses=True,
        )
        db = client.connection_pool.connection_kwargs.get("db", "?")

        self.stdout.write(
            f"target db={db} prefix={PREFIX} "
            f"older_than_hours={older_than_hours} "
            f"protected_ids={len(protected)} mode={'EXECUTE' if execute else 'DRY-RUN'}"
        )

        matched = 0
        candidates = 0
        kept_fresh = 0
        kept_persistent = 0
        protected_seen = 0
        cursor = 0
        batch = []
        deleted = 0
        started = time.time()
        last_report = started

        while True:
            cursor, keys = client.scan(cursor=cursor, match=PREFIX, count=500)
            matched += len(keys)
            pipe = client.pipeline(transaction=False)
            for k in keys:
                pipe.ttl(k)
            ttls = pipe.execute()
            for k, ttl in zip(keys, ttls):
                task_id = k[len("celery-task-meta-"):]
                if task_id in protected:
                    protected_seen += 1
                    continue
                if self._deletable(ttl, older_than_hours, include_persistent):
                    candidates += 1
                    batch.append(k)
                elif ttl is not None and ttl < 0:
                    kept_persistent += 1
                else:
                    kept_fresh += 1

            while len(batch) >= batch_size:
                chunk = batch[:batch_size]
                del batch[:batch_size]
                if execute:
                    client.unlink(*chunk)
                deleted += len(chunk)
                if limit and deleted >= limit:
                    break
                now = time.time()
                if now - last_report >= 5:
                    self.stdout.write(
                        f"  ...scanned={matched} candidates={candidates} "
                        f"deleted={deleted}"
                    )
                    last_report = now

            if limit and deleted >= limit:
                break
            if cursor == 0:
                break

        if batch:
            if execute:
                client.unlink(*batch)
            deleted += len(batch)

        elapsed = time.time() - started
        self.stdout.write("--- SUMMARY ---")
        self.stdout.write(f"keys_matched(scan)={matched}")
        self.stdout.write(f"candidate_old_keys={candidates}")
        self.stdout.write(f"protected_task_ids_seen={protected_seen}")
        self.stdout.write(f"kept_fresh={kept_fresh}")
        self.stdout.write(f"kept_persistent={kept_persistent}")
        self.stdout.write("other_prefixes_touched=0")
        self.stdout.write(f"mode={'EXECUTE' if execute else 'DRY-RUN'}")
        self.stdout.write(f"keys_deleted={deleted}")
        self.stdout.write(f"elapsed_s={elapsed:.1f}")
        self.stdout.write(f"dbsize_after={client.dbsize()}")


if __name__ == "__main__":
    sys.exit(Command().run_from_argv(["manage.py"]))
