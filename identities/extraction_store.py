"""Transient storage of identity extraction results.

Extracted identity VALUES are never persisted to the database. They live in the
Django cache (Redis in production) under a TTL key and are consumed once by the
poll endpoint, after which the cache key and the job row are deleted.
"""
from django.core.cache import cache

CACHE_PREFIX = "identity:extract:"
RESULT_TTL_SECONDS = 30 * 60  # 30 minutes


def store_extraction_result(job_uuid, payload):
    cache.set(f"{CACHE_PREFIX}{job_uuid}", payload, RESULT_TTL_SECONDS)


def read_extraction_result(job_uuid):
    return cache.get(f"{CACHE_PREFIX}{job_uuid}")


def clear_extraction_result(job_uuid):
    cache.delete(f"{CACHE_PREFIX}{job_uuid}")
