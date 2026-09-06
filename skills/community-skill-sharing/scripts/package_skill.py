#!/usr/bin/env python3
"""Package, validate, sanitize, and format newsroom skills for upstream sharing."""

from __future__ import annotations

import argparse
import json
from pathlib import Path
import py_compile
import re
import sys
import urllib.parse
from typing import Any, Dict, List, Optional, Tuple

SECRET_PATTERNS = [
    (re.compile(r"sk-[a-zA-Z0-9_-]{20,}"), "OpenAI/API Key"),
    (re.compile(r"bt_[a-zA-Z0-9_-]{20,}"), "Baseten API Key"),
    (re.compile(r"ghp_[a-zA-Z0-9]{36,}"), "GitHub Personal Access Token"),
    (re.compile(r"github_pat_[a-zA-Z0-9_]{40,}"), "GitHub Fine-Grained Token"),
    (
        re.compile(r"[\w-]{24}\.[\w-]{6}\.[\w-]{27,38}"),
        "Discord Bot Token",
    ),
    (re.compile(r"-----BEGIN (?:RSA|EC|DSA|OPENSSH|PGP|PRIVATE) KEY-----"), "Private Key Header"),
]

PATH_REPLACEMENTS = [
    (re.compile(r"/opt/data/newsroom/?"), "$HERMES_HOME/newsroom/"),
    (re.compile(r"/opt/data/skills/?"), "$HERMES_HOME/skills/"),
    (re.compile(r"/opt/data/?"), "$HERMES_HOME/"),
    (re.compile(r"/home/[a-zA-Z0-9._-]+/\.hermes/?"), "$HERMES_HOME/"),
    (re.compile(r"/root/\.hermes/?"), "$HERMES_HOME/"),
]


def parse_frontmatter(content: str) -> Tuple[Optional[Dict[str, str]], str]:
    if not content.startswith("---"):
        return None, content
    parts = content.split("---", 2)
    if len(parts) < 3:
        return None, content
    raw_meta = parts[1]
    body = parts[2]
    meta: Dict[str, str] = {}
    for line in raw_meta.splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        if ":" in line:
            key, val = line.split(":", 1)
            meta[key.strip()] = val.strip().strip("'\"")
    return meta, body


def sanitize_text(text: str) -> str:
    sanitized = text
    for pattern, replacement in PATH_REPLACEMENTS:
        sanitized = pattern.sub(replacement, sanitized)
    return sanitized


def scan_for_secrets(text: str) -> List[str]:
    findings: List[str] = []
    for pattern, label in SECRET_PATTERNS:
        matches = pattern.findall(text)
        if matches:
            findings.append(f"Detected potential {label} (count: {len(matches)})")
    return findings


def validate_and_package_skill(
    skill_dir: Path,
    repo: str = "brettluft/hermes-journalism-starter",
    author: Optional[str] = None,
) -> Dict[str, Any]:
    if not skill_dir.is_dir():
        return {
            "status": "error",
            "error": f"Skill directory not found: {skill_dir}",
            "code": "DIRECTORY_NOT_FOUND",
        }

    skill_md = skill_dir / "SKILL.md"
    if not skill_md.is_file():
        return {
            "status": "error",
            "error": f"Missing required SKILL.md in {skill_dir}",
            "code": "MISSING_SKILL_MD",
        }

    raw_content = skill_md.read_text(encoding="utf-8")
    meta, _ = parse_frontmatter(raw_content)
    if not meta or "name" not in meta or "description" not in meta:
        return {
            "status": "error",
            "error": "SKILL.md must have YAML frontmatter with 'name' and 'description'",
            "code": "INVALID_FRONTMATTER",
        }

    skill_name = meta["name"]
    if not re.match(r"^[a-z0-9_-]+$", skill_name):
        return {
            "status": "error",
            "error": f"Invalid skill name '{skill_name}'. Must contain only lowercase letters, numbers, hyphens, and underscores.",
            "code": "INVALID_SKILL_NAME",
        }

    all_files: List[Path] = []
    for path in sorted(skill_dir.glob("**/*")):
        if path.is_file():
            if path.name.startswith(".") or "__pycache__" in str(path):
                continue
            all_files.append(path)

    collected_files: Dict[str, str] = {}
    validation_errors: List[str] = []

    for file_path in all_files:
        rel_path = file_path.relative_to(skill_dir).as_posix()
        try:
            content = file_path.read_text(encoding="utf-8")
        except UnicodeDecodeError:
            validation_errors.append(f"Binary or non-UTF-8 file not supported: {rel_path}")
            continue

        secrets = scan_for_secrets(content)
        if secrets:
            for s in secrets:
                validation_errors.append(f"{rel_path}: {s}")

        if rel_path.endswith(".py"):
            try:
                py_compile.compile(str(file_path), doraise=True)
            except py_compile.PyCompileError as e:
                validation_errors.append(f"{rel_path} syntax error: {e}")

        if rel_path.endswith(".json"):
            try:
                json.loads(content)
            except Exception as e:
                validation_errors.append(f"{rel_path} JSON parse error: {e}")

        sanitized_content = sanitize_text(content)
        collected_files[rel_path] = sanitized_content

    if validation_errors:
        return {
            "status": "error",
            "error": "Validation and security checks failed",
            "details": validation_errors,
            "code": "VALIDATION_FAILED",
        }

    # Format markdown bundle
    bundle_parts: List[str] = [
        f"### Community Skill Submission: `{skill_name}`",
        f"**Description:** {meta.get('description', '')}",
        f"**Author / Submitter:** {author or 'Anonymous Contributor'}",
        "\n---",
    ]

    for rel_path, content in collected_files.items():
        bundle_parts.append(f"\n#### `{rel_path}`")
        ext = Path(rel_path).suffix.lstrip(".") or "text"
        if ext == "md":
            ext = "markdown"
        bundle_parts.append(f"```{ext}\n{content}\n```")

    full_bundle = "\n".join(bundle_parts)

    issue_title = f"Add Community Skill: {skill_name}"
    issue_body = (
        f"## Skill Submission: {skill_name}\n\n"
        f"**Description:** {meta.get('description', '')}\n"
        f"**Submitted by:** {author or 'Newsroom Contributor'}\n\n"
        f"### Packaged Skill Content\n\n"
        f"{full_bundle}\n\n"
        f"### Verification Checklist\n"
        f"- [x] No private newsroom credentials or tokens included\n"
        f"- [x] Paths normalized to `$HERMES_HOME`\n"
        f"- [x] Tested with Python syntax and frontmatter checks\n"
    )

    query_params = {
        "title": issue_title,
        "body": issue_body,
        "labels": "community-skill,skill-submission",
    }
    encoded_query = urllib.parse.urlencode(query_params)
    issue_url = f"https://github.com/{repo}/issues/new?{encoded_query}"

    return {
        "status": "ok",
        "skill_name": skill_name,
        "description": meta.get("description", ""),
        "author": author or "Newsroom Contributor",
        "repo": repo,
        "files_included": list(collected_files.keys()),
        "submission_url": issue_url,
        "bundle_preview": full_bundle,
    }


def find_skill_directory(skill_name_or_path: str, search_roots: Optional[List[Path]] = None) -> Optional[Path]:
    candidate = Path(skill_name_or_path)
    if candidate.is_dir() and (candidate / "SKILL.md").is_file():
        return candidate.resolve()

    roots = search_roots or [
        Path.cwd() / "skills",
        Path("/opt/data/skills"),
        Path("/opt/hermes/skills"),
        Path.home() / ".hermes/skills",
    ]

    for root in roots:
        target = root / skill_name_or_path
        if target.is_dir() and (target / "SKILL.md").is_file():
            return target.resolve()

    return None


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Package, sanitize, and create one-click submission links for Hermes community skills."
    )
    parser.add_argument("--skill", "-s", required=True, help="Skill name or path to skill directory")
    parser.add_argument("--skills-dir", help="Optional root directory containing skills")
    parser.add_argument("--repo", default="brettluft/hermes-journalism-starter", help="Target GitHub repository")
    parser.add_argument("--author", help="Author name, handle, or newsroom credit")
    parser.add_argument("--json", action="store_true", help="Output result as JSON")

    args = parser.parse_args()

    search_roots = [Path(args.skills-dir)] if args.skills_dir else None
    skill_dir = find_skill_directory(args.skill, search_roots=search_roots)

    if not skill_dir:
        res = {
            "status": "error",
            "error": f"Could not find skill directory for '{args.skill}'",
            "code": "SKILL_NOT_FOUND",
        }
        if args.json:
            print(json.dumps(res, indent=2))
        else:
            print(f"Error: {res['error']}", file=sys.stderr)
        sys.exit(1)

    result = validate_and_package_skill(skill_dir=skill_dir, repo=args.repo, author=args.author)

    if result.get("status") != "ok":
        if args.json:
            print(json.dumps(result, indent=2))
        else:
            print(f"Error: {result.get('error')}", file=sys.stderr)
            for detail in result.get("details", []):
                print(f"  - {detail}", file=sys.stderr)
        sys.exit(1)

    if args.json:
        print(json.dumps(result, indent=2))
    else:
        print(f"Skill '{result['skill_name']}' successfully validated and packaged.")
        print(f"Files included: {', '.join(result['files_included'])}")
        print("\nOne-Click Submission URL:")
        print(result["submission_url"])


if __name__ == "__main__":
    main()
