# Redis Operations Alerting — PMDAP

Applies to the Railway-managed Redis service (volume NOT expanded, remains 5 GB
until a deliberate, separate capacity decision).

Source of truth for these numbers: `python manage.py ops_redis_health`.

## Suggested thresholds (conservative for a 5 GB volume)

| Metric | Warning | Critical | Rationale |
|--------|---------|----------|-----------|
| Volume used | 65–70% | 80% | RDB bgsave writes a full snapshot; needs headroom on top of live data |
| RDB last bgsave status | — | `err` (immediate) | Persistence broken → MISCONF blocks writes |
| Last successful save age | > 30 min | > 1 hour | `save 60 1` policy ⇒ a healthy instance saves within ~1 min of changes |
| Celery result DB (db1) key count | rapid growth (>1 h) | sustained growth | Signals result accumulation (check `ignore_result` / expiry) |
| used_memory vs RSS | fragmentation > 1.5× | — | Memory bloat |
| evicted_keys | — | > 0 sustained | `noeviction` policy ⇒ eviction means writes would fail on maxmemory |

## Response playbook

1. **RDB save failure + MISCONF** (this incident class):
   - Do NOT expand the volume blindly, wipe Redis, or FLUSHALL.
   - Check `db1` is Celery result keys only (100% prefix `celery-task-meta-*`).
   - Purge with the safe command (dry-run first):
     ```
     python manage.py purge_stale_celery_results --older-than-hours 1
     python manage.py purge_stale_celery_results --older-than-hours 1 --execute
     ```
   - If writes are blocked by MISCONF, a temporary `CONFIG SET save ""` may be
     required to unblock writes for the purge — restore the original save
     policy and complete a real BGSAVE immediately after. Never leave
     persistence disabled.
2. **Result-key growth** (not disk-full):
   - Confirm `CELERY_TASK_IGNORE_RESULT=True` and `CELERY_RESULT_EXPIRES` set.
   - Verify the metrics collector schedules exactly one successor per run
     (regression: `test_backoff_early_return_does_not_double_schedule`).
3. **Worker crash on MISCONF**: recover Redis persistence first; the worker
   reconnects once writes are allowed again.

## Long-term capacity

With the result backend no longer accumulating keys, the live dataset is small
(few KB) and the 5 GB volume is comfortable today. Expansion is NOT currently
necessary, but revisit whenever a new Redis write-heavy workload is added.
