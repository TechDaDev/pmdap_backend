# Phase-1 Release Candidate — Release Readiness

Status as of M17 (commit referenced in the M17 report).

## Release classification

```
PHASE1_RC_READY
```

The application/API contract is stable and frozen at `/api/v1/` (M17). It is
**not** `PRODUCTION_READY`: documented deployment blockers below remain
unresolved by design at M17.

## Application readiness vs deployment readiness

| Dimension | Status |
|---|---|
| APPLICATION READINESS | API contract frozen, full test matrix green, coverage 94.55% |
| PRODUCTION DEPLOYMENT READINESS | Blocked by listed items; requires owner decisions |

The backend may be application-complete while not deployment-ready. These two
are separate; do not conflate them.

## API deprecation policy (Phase-1 rule)

`/api/v1/` is frozen after M17. Future breaking changes must use `/api/v2/` or
an explicit deprecation period. No deprecation framework is built at M17; this
is a policy statement.

## Release-readiness checklist

- [x] All product APIs under `/api/v1/` — verified
- [x] OpenAPI schema generated, validates, 48 paths / 58 ops
- [x] Runtime-vs-OpenAPI drift tests (`tests/test_openapi_runtime_drift.py`)
- [x] Error envelope frozen and stable across domains
- [x] Pagination contract frozen (`count/next/previous/results` under `data`)
- [x] Deterministic ordering on all paginated endpoints
- [x] Mass-assignment sweeps green (`tests/test_audit_mass_assignment.py`)
- [x] IDOR sweeps green (`tests/test_audit_idor_sweep.py`)
- [x] Error-leakage sweeps green (`tests/test_audit_*`)
- [x] Logging/privacy sweeps green (`tests/test_audit_logging.py`)
- [x] Secret scan clean
- [x] Full SQLite lane green (775 passed)
- [x] PostgreSQL lane (see M17 report)
- [x] Coverage 94.55% ≥ 90%
- [x] Static checks (ruff, `manage.py check`, `makemigrations --check`, pip check)
- [x] Docker Compose config + startup validated
- [x] Celery/Redis smoke (see M17 report)
- [ ] Production object storage (external, not in app)
- [ ] Malware scanning (external policy/tool)
- [ ] Backups / restore test / PITR (external)
- [ ] Real Arabic OCR validation on real scans
- [ ] Audit retention/legal policy (owner decision)
- [ ] PyMuPDF licensing resolution (vendor decision)

## Known production blockers (must not be hidden)

1. **PyMuPDF licensing** — unresolved for production. The dependency is used for
   PDF text extraction. Do not claim full production readiness while unresolved.
2. **Malware scanning** — `malware engine = NOT_CONFIGURED`. Identity and
   medical uploads have no production malware scanning. This is a deployment
   blocker/policy decision, not implemented at M17.
3. **Production private object storage** — current private local storage is
   acceptable for development. Production needs secured durable object/private
   storage. Storage architecture not migrated at M17.
4. **Backup / PITR** — production PostgreSQL requires backups, a restore test,
   and PITR where required. Not implemented in the app at M17.
5. **TLS / reverse proxy / secrets** — must be configured externally
   (production settings expect HTTPS proxy headers and fail closed without
   `DJANGO_ALLOWED_HOSTS` or `DJANGO_SECRET_KEY`).
6. **Audit retention / legal policy** — unresolved; audit records are retained
   indefinitely. Owner decision required.
7. **Real Arabic OCR validation** — real Arabic medical scan evaluation is still
   required before claiming Arabic OCR/date-extraction production readiness.

## Known limitations

- CORS is not configured. Phase-1 clients are native/mobile; browser CORS is not
  required. If a browser client is introduced, restrict origins via environment.
- There is no `/api/v1/` root resource; clients discover routes via OpenAPI.
- Audit immutability: records are append-only through application APIs. Raw SQL
  or privileged DB access can alter them; there is no DB trigger and records are
  not cryptographically immutable. Do not overclaim.
- Health endpoint reports only liveness (`{"status": "ok"}`); it does not check
  DB/Redis and exposes no credentials, URLs, paths, or package inventory.
- Search is lexical (`simple` PostgreSQL config), not semantic; no text snippets
  are returned. Query length is capped at 200 chars.
- Date candidates expose only safe metadata (date, type, score, context, page);
  scoring internals and raw document text are not exposed.
- Identity/medical uploads reject unsupported input modes and validate content,
  size, and integrity at upload.
- OCR is CPU-only with `enable_mkldnn=False`. Real Arabic scan benchmark is
  pending (see `benchmarks/ocr/`).

## Environment and deployment notes

- `.env.example` documents all required environment variables with no secrets.
- Compose keeps PostgreSQL on a persistent named volume
  (`pmdap_backend_postgres_data`); normal startup never deletes the developer DB.
- Production settings are fail-closed: `DEBUG=False`, `ALLOWED_HOSTS` required,
  HTTPS proxy header (`HTTP_X_FORWARDED_PROTO`), secure cookies, HSTS.
- Configuration summary: see `docs/architecture/` ADRs and the README.
