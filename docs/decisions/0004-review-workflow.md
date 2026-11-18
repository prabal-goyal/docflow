# 0004: Review workflow, separation of duties and audit log (M3)

Date: 2026-11-18. Status: accepted.

## State machine

One pure function, `workflow.transition(state, action, actor, stages)`, decides every change of
status. It has no I/O, and a table test covers every status x action x role x ownership
combination: allowed transitions are listed explicitly and everything else must raise.
State errors map to 409, permission errors to 403, and permission is checked before input
validation, so an unauthorised caller never learns which input would have worked.

```
IN_REVIEW(n) --approve--> IN_REVIEW(n+1) ... --approve--> APPROVED
IN_REVIEW(n) --raise query--> QUERY_RAISED(n) --submitter answers--> IN_REVIEW(n)   (same stage)
IN_REVIEW(n) / QUERY_RAISED(n) --reject (reason required)--> REJECTED   (final)
EXTRACTION_FAILED / UPLOADED older than 10 min --admin retry--> UPLOADED
```

The role for each stage comes from settings (`approval_stages`, default reviewer then approver),
so a longer approval chain is configuration, not code.

## Separation of duties

- One role per user and one role per stage, so different people act at each stage.
- Settings validation rejects submitter or admin as a stage role, and a role used twice.
- Admins manage users and recover failed extractions; they cannot approve or reject.
- The owner of a submission can never act on it as a reviewer, and only an owner with an
  uploading role can answer its queries. Neither can happen with today's roles; the rules are
  defence in depth in case roles change.

## Human overrides

- Approving a submission that still has flags requires a comment, recorded with the number of
  flags acknowledged.
- Reviewer edits and submitter corrections re-run all checks. A value entered by a person has no
  source quote, so grounding skips it (`human_edited_fields`); format and arithmetic still apply.
- A submitter answers all open queries at once; a correction is optional.

## Concurrency

Every operation locks the submission row (`SELECT ... FOR UPDATE`), re-validates the state, and
commits the change and its audit event together. Two simultaneous approvals: one wins, the other
gets 409. Not handled: an edit made from a stale screen (no ETag / If-Match); the audit log still
records who changed what.

## Audit log

- `audit_events` is append-only, enforced by a Postgres trigger that rejects `UPDATE`, `DELETE` and
  `TRUNCATE`, so neither a bug nor a direct SQL session can rewrite history. The foreign key does
  not cascade, so a submission with history cannot be deleted.
- Ordering uses a database-generated identity column (`seq`). Timestamps use `clock_timestamp()`:
  Postgres `now()` returns the transaction start, so several events in one transaction would tie.
- Events: uploaded, extracted, extraction_failed, extraction_retried, field_edited, query_raised,
  query_answered, stage_approved, approved, rejected. M4 webhooks build on these.
