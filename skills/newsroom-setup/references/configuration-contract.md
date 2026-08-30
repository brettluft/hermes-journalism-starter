# Configuration contract

The setup CLI owns two files under `$HERMES_HOME/newsroom/`:

- `newsroom.json` holds editorial scope, report languages, and evidence preferences.
- `sources.json` holds source authority, approval state, record-type checks, languages, and validation times.

`schema_version` is `1`. `revision` is a nonnegative integer managed by the CLI. Newsroom setup status is `draft`, `partial`, or `complete`:

- `draft`: the interview or source discovery is not approved.
- `partial`: configuration is usable, but source coverage or test gaps remain; it is unfinished.
- `complete`: the editor approved the scope and gaps, the first live check completed, and at least one active validated official or official_mirror source exists.

Source status is `candidate`, `active`, or `inactive`. Authority status is `official`, `official_mirror`, or `non_official`. Record-type availability is `verified`, `partial`, `unavailable`, or `unknown`.

An active official source needs an RFC3339 `last_validated_at` value. URLs must use HTTPS. Source IDs must be unique safe identifiers. Language values use BCP 47 style tags. At any depth, the CLI case-insensitively rejects the exact credential field names `api_key`, `password`, `secret`, `token`, `spacefast_token`, `spacefast_team_id`, `access_token`, and `client_secret`. This is an exact-name policy: ordinary editorial fields such as `story_id` and `editor_id` remain allowed. Configuration must never contain credentials.

## Draft destinations and publication policy

`newsroom.json` may contain a `drafts` object. When present, it must contain exactly these three string fields, with no missing or additional fields:

```json
{
  "drafts": {
    "destination": "library",
    "publishing_policy": "ask_each_time",
    "spacefast_setup_status": "not_configured"
  }
}
```

- `destination`: `library`, `spacefast`, or `both`.
- `publishing_policy`: `ask_each_time`, `auto_private`, or `never`.
- `spacefast_setup_status`: `not_configured` or `configured`.

For backward compatibility, a schema-version-1 document without `drafts` remains valid. Its effective values are `library`, `ask_each_time`, and `not_configured`; reading, validating, and reporting status do not add the object or rewrite the file. Legacy `apply` and `undo` operations may rewrite the selected file and increment its revision as usual, but they do not synthesize an absent `drafts` object. `apply` materializes `drafts` only when the candidate includes it; `undo` restores whether the selected backup contains it. New files created by `init` include the defaults.

Publication policy is interpreted as follows:

| Policy | Draft Library | Spacefast when selected by `destination` |
| --- | --- | --- |
| `ask_each_time` | Require explicit approval for each publication. | Require explicit approval for each publication. |
| `auto_private` | May publish automatically to the private Draft Library. | Still require explicit approval for each external publication. |
| `never` | Do not publish. | Do not publish. |

`spacefast_setup_status=configured` records editorial/setup state only; it does not prove runtime credentials are available. `SPACEFAST_TOKEN` and `SPACEFAST_TEAM_ID` are Railway variables and must never be stored in `newsroom.json` (or any other newsroom configuration, draft, audit event, or chat message). Their case-insensitive JSON field-name forms, plus `access_token` and `client_secret`, are covered by the recursive exact-name rejection policy above. URLs, team IDs, space IDs, credentials, and other deployment-specific fields are not allowed in `drafts`.

## Commands

```text
newsroom_config.py init --home PATH
newsroom_config.py status --home PATH
newsroom_config.py validate --kind newsroom|sources --input FILE
newsroom_config.py apply --home PATH --kind newsroom|sources --input FILE
newsroom_config.py undo --home PATH --kind newsroom|sources
```

Every command writes one JSON result to standard output and returns a nonzero status on validation or I/O failure.

- `init` creates missing defaults and audit storage. It preserves existing files.
- `status` validates current files and reports their revisions.
- `validate` checks a draft without changing current state.
- `apply` validates a draft, increments its revision, stores the former content as `.previous`, and records hashes in the audit log.
- `undo` restores `.previous` as a newly incremented revision.

Create drafts in a permission-restricted temporary directory outside `$HERMES_HOME/newsroom/`. Show an editor-readable diff before apply. Apply only after approval, then read the current JSON and run status. Never edit current files or audit records directly.

The CLI serializes UTF-8 JSON in sorted, indented form. Its locking, transaction journal, atomic replacement, backup, and audit behavior apply to CLI writes. `undo` is per file, so coordinated changes to both files must be explained and verified separately. During `apply` and `undo`, the CLI reads the counterpart under the same flock and rejects a mutation that would leave `setup_status=complete` with zero active validated official or official_mirror sources. The independent `validate` command checks one template without profile state and does not enforce this cross-file invariant.

Use safe two-file transition ordering. For setup or expansion, apply sources first, then mark newsroom complete last. For source removal or contraction, mark newsroom partial first, then remove or deactivate sources; only return to complete after checks. If the second operation fails, report partial and do not falsely claim completion.
