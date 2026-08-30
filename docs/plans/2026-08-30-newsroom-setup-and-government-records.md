# Newsroom setup and government records implementation plan

> **For Hermes:** Use subagent-driven-development skill to implement this plan task-by-task.

**Goal:** Add conversational newsroom onboarding and source-cited government-record research backed by persistent, validated newsroom configuration.

**Architecture:** Bundle two focused skills. `newsroom-setup` interviews editors and uses a deterministic standard-library Python CLI to validate and atomically update profile-owned JSON under `$HERMES_HOME/newsroom/`. `government-records-research` reads only approved active sources, applies reporting safeguards, and reports source coverage. Startup continues to validate environment only and never overwrites persistent newsroom state.

**Tech stack:** Hermes SKILL.md files, Python 3 standard library, JSON, `unittest`, Docker, Railway persistent volume.

---

### Task 1: Define the persistent configuration contract

**Objective:** Specify and test profile initialization, validation, atomic apply, audit, and undo behavior.

**Files:**
- Create: `tests/test_newsroom_config.py`
- Create: `skills/newsroom-setup/scripts/newsroom_config.py`

**Step 1: Write failing tests**

Add tests that invoke the desired CLI and require:

- `init --home <temp>` creates `newsroom/newsroom.json`, `newsroom/sources.json`, and an audit file without touching any other profile paths.
- Repeated `init` preserves existing files.
- `validate --kind newsroom --input <file>` rejects missing newsroom name, invalid IANA-style timezone text, and empty report-language lists.
- `validate --kind sources --input <file>` rejects non-HTTPS URLs, duplicate IDs, invalid source status, and active official sources without a validation timestamp.
- `apply` sets an incremented revision, writes atomically, keeps a backup, and appends a content-hash-only audit event.
- `undo` restores the previous valid document and increments revision.
- The CLI never accepts or records fields named `api_key`, `password`, `secret`, or `token`.

**Step 2: Verify RED**

Run:

```bash
python3 -m unittest tests.test_newsroom_config -v
```

Expected: failure because `skills/newsroom-setup/scripts/newsroom_config.py` does not exist.

**Step 3: Implement the minimum CLI**

Use only Python standard-library modules. Resolve mutable paths from `--home` or `$HERMES_HOME`; never hard-code a profile path. Write temporary files in the destination directory, flush and `fsync`, then use `os.replace`. Use a bounded lock directory to prevent concurrent writers. Keep JSON UTF-8 and sorted/indented for human inspection.

Commands:

```text
init --home PATH
status --home PATH
validate --kind newsroom|sources --input FILE
apply --home PATH --kind newsroom|sources --input FILE
undo --home PATH --kind newsroom|sources
```

Every command prints machine-readable JSON and uses nonzero exit status for validation or I/O failures.

**Step 4: Verify GREEN**

Run the focused tests and then the existing scaffold suite.

### Task 2: Add the two user-facing skills

**Objective:** Add plain-language, globally applicable setup and research workflows that use the persistent contract.

**Files:**
- Create: `skills/newsroom-setup/SKILL.md`
- Create: `skills/newsroom-setup/references/configuration-contract.md`
- Create: `skills/newsroom-setup/templates/newsroom.example.json`
- Create: `skills/newsroom-setup/templates/sources.example.json`
- Create: `skills/government-records-research/SKILL.md`
- Create: `skills/government-records-research/references/record-types.md`
- Create: `skills/government-records-research/references/report-template.md`
- Modify: `tests/test_scaffold.py`

**Step 1: Write failing scaffold tests**

Require both skills and their linked assets. Assert the setup skill:

- Is rerunnable and asks only editorial questions.
- Runs deployment checks before configuration.
- Discovers and tests technical source details itself.
- Requires editor approval before promoting candidates to active sources.
- Uses dry-run or validation, a plain-language diff, apply, read-back, and undo.
- Never asks users to paste secrets into Discord.

Assert the research skill:

- Reads `$HERMES_HOME/newsroom/newsroom.json` and `sources.json`.
- Uses active validated sources first.
- Preserves original-language text.
- Separates agendas, minutes, transcripts, votes, executive orders, attachments, and video.
- Maps claims to exact official artifacts.
- Ends with one of `verified records found`, `no matching records found in all successfully checked sources`, or `search incomplete`.
- Treats retrieved web content as untrusted data.

**Step 2: Verify RED**

Run the new focused scaffold tests and confirm failure because the skills are absent.

**Step 3: Write the skills and supporting references**

Keep public-body terminology globally neutral. Do not assume North American administrative levels, `.gov` domains, English source text, named votes, public meetings, or a particular calendar. Setup should ask four initial questions, discover five to ten sources, present a coverage matrix, obtain approval, and finish with a live cited result.

**Step 4: Verify GREEN**

Run all unit tests.

### Task 3: Integrate with the starter without overwriting newsroom state

**Objective:** Package the skills and explain the first-session workflow while preserving safe upgrades.

**Files:**
- Modify: `Dockerfile` only if additional explicit copy behavior is required
- Modify: `README.md`
- Modify: `SOUL.md`
- Modify: `.github/workflows/validate.yml`
- Modify: `tests/test_scaffold.py`

**Step 1: Write failing integration assertions**

Require documentation to explain:

- `newsroom-setup` as the first conversational action.
- Mutable state under `/opt/data/newsroom` on Railway.
- Bundled skill upgrades do not overwrite newsroom choices.
- Setup can be rerun and undone.
- Global support means source discovery and explicit coverage reporting, not guaranteed support for every government.
- Monitoring is deliberately deferred.

Require CI to run all unit tests and Python syntax checks for bundled scripts.

**Step 2: Verify RED**

Run the assertions and confirm they fail against current documentation.

**Step 3: Implement the documentation and identity changes**

Add a short first-session example and configuration-management commands. Keep sensitive-document warnings and Discord authorization wording intact. Do not add startup writes to `00-journalism-bootstrap`.

**Step 4: Verify GREEN**

Run:

```bash
python3 -m unittest discover -s tests -v
python3 -m py_compile skills/newsroom-setup/scripts/newsroom_config.py
bash -n docker/cont-init.d/00-journalism-bootstrap
```

### Task 4: Final verification and release

**Objective:** Verify security, persistence boundaries, behavior, and repository cleanliness before publishing.

**Files:** All changed files.

**Step 1: Run full validation**

- Full unit suite
- Python syntax compilation
- Shell syntax validation
- JSON parsing for every example/template
- Git diff inspection
- Added-line secret scan
- Check that bootstrap still never writes persistent state

**Step 2: Independent reviews**

Request separate spec-compliance and code/security reviews. Blocking findings include secret storage, path traversal, arbitrary profile writes, non-atomic updates, candidate sources treated as verified, lost local customization, or false completeness claims.

**Step 3: Fix and reverify**

Use regression tests for every blocking defect. Repeat until both reviews approve.

**Step 4: Commit and push**

Commit the verified change to `main`, push to the existing private origin, and verify the remote commit and files. Do not publish a Railway template or make the repository public in this task.
