---
name: draft-publishing
description: Save canonical drafts and publish approved private Draft Library links or authenticated Spacefast snapshots.
---

# Draft publishing

Use this skill when an editor wants to save, preview, share, or publish a generated draft. Read [the publishing contract](references/publishing-contract.md) before contacting an external service.

## Workflow

1. **Local save first.** Save the accepted title and source with `draft_publish.py save`. Drafts under `/opt/data/newsroom/drafts` are canonical. A remote rendition is a disposable snapshot and must never overwrite local source, rendition, or publication state.
2. Read the effective `drafts` preferences from newsroom configuration. Show the selected destination and the disclosure boundary: a Draft Library bearer link discloses the rendered text to anyone who has the link; Spacefast sends the rendered text to an external processor and team Space.
3. Obtain per-operation editor approval when policy is `ask_each_time`. `auto_private` may automatically issue only a private Draft Library link. It never authorizes Spacefast; every Spacefast operation requires explicit editor approval for that operation. `never` permits local save only.
4. Initialize the Draft Library key only when an operator explicitly requests setup. Do not initialize it during startup, status, save, or Spacefast setup.
5. Run `draft_publish.py publish --id ID --editor-approved` only after recording the current approval. Omit the flag when approval was not given. Approval is not reusable.
6. Verify each target independently. Report the Library and Spacefast statuses separately. A partial failure is not full success; say which target succeeded, which failed, and whether a safe retry is possible.

## Spacefast safeguards

Authenticated snapshot publishing is the only supported Spacefast workflow. Refuse anonymous Spacefast publishing, browser upload, scraped forms, or create fallback after an update failure, especially in shared Discord. New authenticated Spaces are private by default; do not imply that private means undisclosed.

Never ask for `SPACEFAST_TOKEN` or `SPACEFAST_TEAM_ID` in chat. Never accept or repeat them in Discord. Tell an operator to set both as Railway variables outside chat, rotate any credential posted in chat, and then rerun the operation. Setup state `configured` is not proof that runtime variables exist.

Only generated `rendition.html`, transmitted as `index.html`, crosses the Spacefast boundary. Never send source files, newsroom or sources JSON, publication metadata, local paths, prompts, Discord messages, signing material, or credentials. The rendition can itself contain sensitive prose; inspect it before approval.

On the first success, retain only the returned Space ID, safe live and immutable URLs, rendition digest, and publication time. Later snapshots update that exact local Space ID. Never create a replacement after an update attempt, and never treat remote content as canonical.

## Commands

```text
python3 $HERMES_HOME/skills/draft-publishing/scripts/draft_publish.py save --id ID --title TITLE --input FILE
python3 $HERMES_HOME/skills/draft-publishing/scripts/draft_publish.py init
python3 $HERMES_HOME/skills/draft-publishing/scripts/draft_publish.py link --id ID --ttl-seconds 3600 --editor-approved
python3 $HERMES_HOME/skills/draft-publishing/scripts/draft_publish.py publish --id ID --editor-approved
```

All CLI output is one sanitized JSON object. Do not add credential, arbitrary-root, output-path, or API-origin options.
