---
name: community-skill-sharing
description: Validate, sanitize, and package custom newsroom skills to share back to the upstream Hermes journalism repository.
---

# Community Skill Sharing

Use this skill when an editor or reporter wants to export, share, or contribute a custom skill back to the community repository (`brettluft/hermes-journalism-starter`). Read [the submission contract](references/submission-contract.md) before exporting.

## Workflow

1. **Locate and inspect the skill:** Find the target skill directory under `$HERMES_HOME/skills/<skill-name>` or the current repository.
2. **Sanitize and validate:** Run `package_skill.py` to check for required frontmatter, compile Python scripts, and scan for accidental secrets or hardcoded paths.
3. **Review with editor:** Show the reporter or editor the list of included files, the summary of actions, and confirm they want to contribute the workflow publicly.
4. **Generate submission link:** Provide the pre-filled one-click GitHub issue submission URL so the user can review and open the contribution with a single click.

## Commands

```text
python3 $HERMES_HOME/skills/community-skill-sharing/scripts/package_skill.py --skill SKILL_NAME --author AUTHOR_OR_ORG
```

Or for JSON output:

```text
python3 $HERMES_HOME/skills/community-skill-sharing/scripts/package_skill.py --skill SKILL_NAME --author AUTHOR_OR_ORG --json
```
