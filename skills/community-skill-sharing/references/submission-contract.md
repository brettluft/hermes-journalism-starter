# Community Skill Submission Contract

## Purpose
The community skill sharing workflow allows newsrooms, investigative reporters, and civic technologists to contribute reusable workflows (e.g. state FOIA trackers, court docket scrapers, SEC filing analyzers, public data decoders) back to the open-source Hermes Journalism Starter repository.

## Invariants and Safety Rules

1. **Zero Newsroom Credentials:**
   - Skills shared upstream must never contain live API keys, Bearer tokens, Discord bot tokens, webhook secrets, or private certificates.
   - Any skill containing private secrets is rejected before a link is generated.

2. **Path Normalization:**
   - Local newsroom paths (such as `/opt/data/newsroom/`, `/home/user/...`) are automatically sanitized to standard `$HERMES_HOME` relative references.

3. **No Unapproved Side Effects:**
   - `package_skill.py` operates 100% locally. It does not initiate background network connections or commit to remotes.
   - The user receives a pre-formatted GitHub issue URL which they must review and submit themselves in their browser.

4. **File Structure Standards:**
   - Every submitted skill must have a valid `SKILL.md` with YAML frontmatter containing `name` and `description`.
   - Supporting scripts in `scripts/` must be valid Python (syntactically compilable).
   - Templates in `templates/` and configuration files must parse as valid JSON/YAML.
