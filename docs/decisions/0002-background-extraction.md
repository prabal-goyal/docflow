# 0002: Background extraction pipeline (M2a)

Date: 2026-11-17. Status: accepted.

## Context

Extraction calls an LLM and parses an untrusted PDF. Both are slow and can fail, so they run in a
Celery worker, not in the API request. Celery with a Redis broker delivers tasks **at least once**:
a task can run twice, or be redelivered after a worker crash.

## Decisions

- **The API never parses the PDF.** It only checks the `%PDF-` signature, the size and the hash.
  Parsing happens in the worker, under Celery's time limits, so a malicious file cannot hang or
  crash the process serving users.
- **Commit, then enqueue.** The row is committed before the task is sent, so a worker can never
  look for a submission that does not exist yet.
- **Atomic claim with a lease.** A worker takes a submission with one conditional `UPDATE`
  (`UPLOADED -> EXTRACTING`, setting `claimed_at`). A duplicate delivery finds nothing to claim.
  If a worker dies mid-task, its claim expires after the task time limit plus a margin, and a
  redelivered task takes over. A delivery that finds a live claim retries after the lease.
- **Transient vs permanent failures.** Storage outages and (in M2b) rate limits or provider
  outages release the claim and retry with exponential backoff (30s, 60s, 120s), then mark the
  submission failed. Bad PDFs and permanent provider errors fail immediately with a reason.
- **Idempotent uploads.** An optional `Idempotency-Key` header, unique per submitter, makes client
  retries safe. A reused key with a different file is a 409.
- **Local object storage is SeaweedFS.** MinIO stopped publishing free Docker images in 2025.
  The application only uses the S3 API through boto3, so production uses AWS S3 unchanged.

## Known gaps and upgrade paths

| Gap | Effect | Upgrade path |
|---|---|---|
| Enqueue can fail after the commit (broker down) | Submission stays `UPLOADED`; the upload still returns 202 | Transactional outbox, or a periodic sweeper that re-enqueues old `UPLOADED` rows; admin retry endpoint in M3 |
| Storage write succeeds, then the commit fails | An orphaned object in the bucket | Bucket lifecycle rule, or a cleanup job |
| Request body size is checked after Starlette spools it to a temp file | Large bodies use disk, not memory | Body size limit in the reverse proxy (M6) |
