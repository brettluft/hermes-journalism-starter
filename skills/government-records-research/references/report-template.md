# Government records report template

## Scope

- Question:
- Public body and confirmed place:
- Date range:
- Record types:
- Coverage warning, if any:

## Findings

For each material claim:

**Claim:** Concise statement no broader than the artifact.

**Artifact:** `[title in original language](URL)`, artifact type, publisher/body, official or local calendar and displayed date, any normalized Gregorian or ISO date, retrieved `RFC3339 time`, page/section/item locator. Use an exact quote only when useful and directly present. Put any labeled translation after it. For a video, image, scan, or table without reliable text, precisely describe observable evidence with a timestamp, page, row, or item locator. Never invent a transcription. Label each calendar conversion and its conversion uncertainty.

**Proves:** What this artifact establishes.

**Does not prove:** Relevant limit.

## Checked sources and failures

| Source | Official and approval status | Record types checked | Result | Failure or gap |
|---|---|---|---|---|

List every relevant active source, including unsuccessful checks. List unofficial fallback evidence separately. Candidate sources are discovery leads, not evidence. Exclude inactive sources from this evidence table. If useful, list inactive sources separately as coverage diagnostics or history and state that inactive sources cannot support claims.

## Limits

State archive, extraction, date, language, locator, and source-coverage limits. Preserve each official or local calendar and displayed date alongside any normalized Gregorian or ISO date. Identify every calendar conversion and conversion uncertainty. Note relevant inactive sources only as coverage diagnostics or history. A negative search result does not establish that an event did not happen.

Draft and partial setup states are unfinished and force `search incomplete`.

End with exactly one status label and nothing after it. Apply this precedence in order:

1. `search incomplete`: use this if any relevant configured source or check failed, setup is unfinished, coverage is insufficient, scope could not be completed, or evidence is only unofficial. Summarize responsive verified records in Findings, but incomplete wins even if responsive records were found.
2. `verified records found`: use this only if the first rule does not apply and one or more responsive verified artifacts were found.
3. `no matching records found in all successfully checked sources`: use this only if the first two rules do not apply, no responsive artifact was found, and every scoped source and check succeeded.
