# Configuration contract

The setup CLI owns two files under `$HERMES_HOME/newsroom/`:

- `newsroom.json` holds editorial scope, report languages, and evidence preferences.
- `sources.json` holds source authority, approval state, record-type checks, languages, and validation times.

`schema_version` is `1`. `revision` is a nonnegative integer managed by the CLI. Newsroom setup status is `draft`, `partial`, or `complete`:

- `draft`: the interview or source discovery is not approved.
- `partial`: configuration is usable, but source coverage or test gaps remain; it is unfinished.
- `complete`: the editor approved the scope and gaps, the first live check completed, and at least one active validated official or official_mirror source exists.

Source status is `candidate`, `active`, or `inactive`. Authority status is `official`, `official_mirror`, or `non_official`. Record-type availability is `verified`, `partial`, `unavailable`, or `unknown`.

An active official source needs an RFC3339 `last_validated_at` value. URLs must use HTTPS. Source IDs must be unique safe identifiers. Language values use BCP 47 style tags. The CLI rejects fields named `api_key`, `password`, `secret`, or `token` at any depth. Configuration must never contain credentials.

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
