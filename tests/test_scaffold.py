import json
import hashlib
import os
from pathlib import Path
import re
import shutil
import subprocess
import tempfile
import unittest


ROOT = Path(__file__).resolve().parents[1]


class ScaffoldTests(unittest.TestCase):
    def test_required_project_files_exist(self):
        required = [
            "Dockerfile",
            "railway.json",
            "docker/cont-init.d/00-journalism-bootstrap",
            "config/config.template.yaml",
            "README.md",
            "LICENSE",
            "SECURITY.md",
            "setup/discord-bot-setup.html",
            "skills/community-skill-sharing/SKILL.md",
            "skills/community-skill-sharing/references/submission-contract.md",
            "skills/community-skill-sharing/scripts/package_skill.py",
            ".github/ISSUE_TEMPLATE/community_skill_submission.yml",
            "skills/unslop/SKILL.md",
            "skills/unslop/LICENSE",
            "skills/unslop/SOURCE.md",
        ]
        for relative_path in required:
            with self.subTest(path=relative_path):
                self.assertTrue((ROOT / relative_path).is_file())

    def test_dockerfile_extends_official_image_and_preserves_entrypoint(self):
        dockerfile = (ROOT / "Dockerfile").read_text()
        self.assertIn("FROM nousresearch/hermes-agent:", dockerfile)
        self.assertNotIn("ENTRYPOINT", dockerfile)
        self.assertIn("/etc/cont-init.d/00-journalism-bootstrap", dockerfile)
        self.assertIn("/opt/hermes/cli-config.yaml.example", dockerfile)
        self.assertIn("/opt/hermes/docker/SOUL.md", dockerfile)
        self.assertIn("COPY skills/ /opt/hermes/skills/", dockerfile)
        self.assertIn('CMD ["gateway", "run"]', dockerfile)

    def test_unslop_skill_is_bundled_with_upstream_license(self):
        skill = (ROOT / "skills/unslop/SKILL.md").read_text()
        license_text = (ROOT / "skills/unslop/LICENSE").read_text()
        self.assertTrue(skill.startswith("---\nname: unslop\n"))
        self.assertIn("description: Cut AI tells from any writing. Must always apply.", skill)
        self.assertIn("## Patterns to detect and fix", skill)
        self.assertEqual(
            hashlib.sha256(skill.encode()).hexdigest(),
            "181883e539caec8258ec9129e3ba5f133409144a2cbf2aa361158ab94cfc3441",
        )
        self.assertIn("MIT License", license_text)
        self.assertIn("Copyright (c) 2026 Lauren Tan", license_text)
        source = (ROOT / "skills/unslop/SOURCE.md").read_text()
        self.assertIn("2a93c06bbe54fde89a36c88e63ef07477da323d4", source)

    def test_discord_defaults_are_channel_scoped_mention_free_and_unthreaded(self):
        dockerfile = (ROOT / "Dockerfile").read_text()
        self.assertIn("DISCORD_REQUIRE_MENTION=false", dockerfile)
        self.assertIn("DISCORD_AUTO_THREAD=false", dockerfile)
        self.assertNotIn("DISCORD_ALLOW_ALL_USERS=true", dockerfile)

    def test_railway_config_preserves_image_entrypoint_and_uses_restart_policy(self):
        config = json.loads((ROOT / "railway.json").read_text())
        deploy = config["deploy"]
        self.assertNotIn("startCommand", deploy)
        self.assertEqual(deploy["restartPolicyType"], "ON_FAILURE")
        self.assertGreaterEqual(deploy["restartPolicyMaxRetries"], 3)

    def test_bootstrap_runs_before_upstream_setup(self):
        bootstrap_names = sorted(
            path.name
            for path in (ROOT / "docker/cont-init.d").glob("*-journalism-bootstrap")
        )
        self.assertEqual(bootstrap_names, ["00-journalism-bootstrap"])
        self.assertLess(bootstrap_names[0], "01-hermes-setup")

    def test_bootstrap_does_not_change_ownership(self):
        script = (ROOT / "docker/cont-init.d/00-journalism-bootstrap").read_text()
        self.assertNotIn("chown", script)
        self.assertNotRegex(script, r"\b(?:chown|chmod)\s+(?:-[^\s]*R[^\s]*|--recursive)\b")

    def test_bootstrap_only_validates_without_startup_writes(self):
        script = (ROOT / "docker/cont-init.d/00-journalism-bootstrap").read_text()
        for forbidden in (
            "HERMES_HOME",
            "newsroom_config.py",
            "chmod",
            "mkdir",
            "cp -",
            "mv ",
            "rm ",
        ):
            with self.subTest(forbidden=forbidden):
                self.assertNotIn(forbidden, script)
        self.assertNotRegex(script, r"(?m)^\s*install\s")

    def test_newsroom_cli_source_and_fresh_copy_are_executable(self):
        source = ROOT / "skills/newsroom-setup/scripts/newsroom_config.py"
        self.assertTrue(source.stat().st_mode & 0o111)
        with tempfile.TemporaryDirectory() as temp_dir:
            installed_skill = Path(temp_dir) / "skills/newsroom-setup"
            shutil.copytree(source.parents[1], installed_skill, copy_function=shutil.copy2)
            installed_cli = installed_skill / "scripts/newsroom_config.py"
            self.assertTrue(installed_cli.stat().st_mode & 0o111)

    def test_config_uses_key_environment_name_instead_of_a_key(self):
        template = (ROOT / "config/config.template.yaml").read_text()
        self.assertIn("key_env: BASETEN_API_KEY", template)
        self.assertIn("api: https://inference.baseten.co/v1", template)
        self.assertIn("provider: custom:baseten", template)
        self.assertNotRegex(template, r"api_key:\s+[A-Za-z0-9_.-]{16,}")

    def test_bootstrap_rejects_missing_required_variables(self):
        script = ROOT / "docker/cont-init.d/00-journalism-bootstrap"
        env = os.environ.copy()
        env.pop("BASETEN_API_KEY", None)
        env.pop("DISCORD_BOT_TOKEN", None)
        env.pop("DISCORD_ALLOWED_USERS", None)
        env.pop("DISCORD_ALLOWED_ROLES", None)
        env.pop("DISCORD_ALLOWED_CHANNELS", None)
        result = subprocess.run(
            ["bash", str(script)],
            env=env,
            text=True,
            capture_output=True,
            check=False,
        )
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("BASETEN_API_KEY", result.stderr)

    def test_bootstrap_accepts_channel_allowlist_without_user_allowlist(self):
        script = ROOT / "docker/cont-init.d/00-journalism-bootstrap"
        env = os.environ.copy()
        env.update(
            {
                "BASETEN_API_KEY": "test-key-not-a-secret",
                "DISCORD_BOT_TOKEN": "test-discord-token",
                "DISCORD_ALLOWED_CHANNELS": "987654321",
            }
        )
        env.pop("DISCORD_ALLOWED_USERS", None)
        env.pop("DISCORD_ALLOWED_ROLES", None)
        result = subprocess.run(
            ["bash", str(script)], env=env, text=True, capture_output=True, check=False
        )
        self.assertEqual(result.returncode, 0, result.stderr)

    def test_bootstrap_accepts_required_variables_without_writing_state(self):
        script = ROOT / "docker/cont-init.d/00-journalism-bootstrap"
        with tempfile.TemporaryDirectory() as temp_dir:
            home = Path(temp_dir) / "must-remain-empty"
            home.mkdir()
            env = os.environ.copy()
            env.update(
                {
                    "HERMES_HOME": str(home),
                    "BASETEN_API_KEY": "test-key-not-a-secret",
                    "DISCORD_BOT_TOKEN": "test-discord-token",
                    "DISCORD_ALLOWED_CHANNELS": "987654321",
                }
            )
            env.pop("DISCORD_ALLOWED_USERS", None)
            env.pop("DISCORD_ALLOWED_ROLES", None)
            result = subprocess.run(
                ["bash", str(script)], env=env, text=True, capture_output=True, check=False
            )
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertEqual(list(home.iterdir()), [])

    def test_bootstrap_rerun_does_not_modify_an_installed_newsroom_cli(self):
        script = ROOT / "docker/cont-init.d/00-journalism-bootstrap"
        with tempfile.TemporaryDirectory() as temp_dir:
            home = Path(temp_dir) / "hermes"
            installed_cli = home / "skills/newsroom-setup/scripts/newsroom_config.py"
            installed_cli.parent.mkdir(parents=True)
            installed_cli.write_text("#!/usr/bin/env python3\n")
            installed_cli.chmod(0o640)
            before = (installed_cli.read_bytes(), installed_cli.stat().st_mode)
            env = os.environ.copy()
            env.update(
                {
                    "HERMES_HOME": str(home),
                    "BASETEN_API_KEY": "test-key-not-a-secret",
                    "DISCORD_BOT_TOKEN": "test-discord-token",
                    "DISCORD_ALLOWED_CHANNELS": "987654321",
                }
            )
            result = subprocess.run(
                ["bash", str(script)], env=env, text=True, capture_output=True, check=False
            )
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertEqual((installed_cli.read_bytes(), installed_cli.stat().st_mode), before)
            self.assertFalse((home / "newsroom").exists())
            self.assertEqual(
                sorted(path.relative_to(home).as_posix() for path in home.rglob("*")),
                [
                    "skills",
                    "skills/newsroom-setup",
                    "skills/newsroom-setup/scripts",
                    "skills/newsroom-setup/scripts/newsroom_config.py",
                ],
            )

    def test_bootstrap_leaves_an_installed_cli_symlink_untouched(self):
        script = ROOT / "docker/cont-init.d/00-journalism-bootstrap"
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            home = root / "hermes"
            installed_cli = home / "skills/newsroom-setup/scripts/newsroom_config.py"
            installed_cli.parent.mkdir(parents=True)
            target = root / "outside.py"
            target.write_text("#!/usr/bin/env python3\n")
            target.chmod(0o640)
            installed_cli.symlink_to(target)
            before = (installed_cli.readlink(), target.read_bytes(), target.stat().st_mode)
            env = os.environ.copy()
            env.update(
                {
                    "HERMES_HOME": str(home),
                    "BASETEN_API_KEY": "test-key-not-a-secret",
                    "DISCORD_BOT_TOKEN": "test-discord-token",
                    "DISCORD_ALLOWED_CHANNELS": "987654321",
                }
            )
            result = subprocess.run(
                ["bash", str(script)], env=env, text=True, capture_output=True, check=False
            )
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertEqual(
                (installed_cli.readlink(), target.read_bytes(), target.stat().st_mode), before
            )

    def test_ci_explicitly_compiles_the_newsroom_cli(self):
        workflow = (ROOT / ".github/workflows/validate.yml").read_text()
        self.assertIn(
            "python -m py_compile skills/newsroom-setup/scripts/newsroom_config.py",
            workflow,
        )

    def test_readme_documents_newsroom_workflow_persistence_and_limits(self):
        readme = (ROOT / "README.md").read_text()
        lower = readme.lower()
        for skill_name in ("`newsroom-setup`", "`government-records-research`"):
            self.assertIn(skill_name, readme)
        for phrase in (
            "names the coverage",
            "approves the discovered source pack",
            "one live, cited result",
            "$hermes_home/newsroom/newsroom.json",
            "$hermes_home/newsroom/sources.json",
            "$hermes_home/newsroom/newsroom.json.previous",
            "$hermes_home/newsroom/sources.json.previous",
            "$hermes_home/newsroom/audit/config-events.jsonl",
            "set up coverage",
            "add a place",
            "stop following a beat",
            "show sources",
            "test sources",
            "undo",
            "on-demand government records",
            "monitoring is deferred",
            "never scheduled automatically",
            "persistent volume",
            "redeploy",
            "newsroom_config.py status",
            "python3 -m unittest discover -s tests -v",
        ):
            with self.subTest(phrase=phrase):
                self.assertIn(phrase, lower)
        self.assertIn("/opt/data/newsroom", lower)
        self.assertIn("do not overwrite newsroom choices", lower)
        self.assertIn("not guaranteed support for every government", lower)

    def test_readme_documents_the_safe_cli_workflow(self):
        readme = (ROOT / "README.md").read_text()
        for command in (
            'newsroom_config.py init --home "$HERMES_HOME"',
            'newsroom_config.py status --home "$HERMES_HOME"',
            'newsroom_config.py validate --kind sources --input "$DRAFT_DIR/sources.json"',
            'newsroom_config.py apply --home "$HERMES_HOME" --kind sources --input "$DRAFT_DIR/sources.json"',
            'newsroom_config.py undo --home "$HERMES_HOME" --kind sources',
        ):
            with self.subTest(command=command):
                self.assertIn(command, readme)
        lower = readme.lower()
        self.assertIn("one document at a time", lower)
        self.assertIn("apply sources first", lower)
        self.assertIn("mark newsroom complete last", lower)
        self.assertIn("do not edit", lower)
        self.assertIn("authoritative", lower)

    def test_operator_docs_cover_draft_library_setup_policy_and_access_boundary(self):
        readme = (ROOT / "README.md").read_text()
        lower = readme.lower()
        for phrase in (
            "/opt/data/newsroom/drafts", "canonical", "volume mounted at `/opt/data`",
            "draft_publish.py init", "do not create the key or draft state",
            "`destination`: `library`", "`publishing_policy`: `ask_each_time`",
            "`spacefast_setup_status`: `not_configured`", "`auto_private`",
            "draft library only", "never spacefast", "draft_library_base_url",
            "railway_public_domain", "86400 seconds", "forwarded valid link",
            "no per-user identity", "without a restart", "`/healthz`", "503",
        ):
            with self.subTest(phrase=phrase):
                self.assertIn(phrase, lower)
        self.assertLess(
            lower.index("draft_library_base_url"),
            lower.index("railway_public_domain", lower.index("draft_library_base_url")),
        )
        for policy in ("ask_each_time", "auto_private", "never"):
            self.assertRegex(lower, rf"(?m)^\| `{policy}` \|")

    def test_operator_docs_cover_spacefast_disclosure_and_upload_boundary(self):
        combined = (
            (ROOT / "README.md").read_text() + "\n" +
            (ROOT / "SECURITY.md").read_text()
        ).lower()
        for phrase in (
            "spacefast_token", "spacefast_team_id", "railway variables",
            "authenticated rest", "never ask", "never post", "discord",
            "no anonymous", "shared discord", "each spacefast publish",
            "approval", "external disclosure", "local remains canonical",
            "static rendition", "raw source", "newsroom config",
            "publication metadata", "hmac", "sensitive text",
            "source-protection", "confidential identities",
        ):
            with self.subTest(phrase=phrase):
                self.assertIn(phrase, combined)

    def test_operator_docs_include_exact_validation_and_smoke_commands(self):
        readme = (ROOT / "README.md").read_text()
        for command in (
            "python3 -m unittest discover -s tests -v",
            "python3 -m py_compile skills/newsroom-setup/scripts/newsroom_config.py skills/draft-publishing/scripts/draft_store.py skills/draft-publishing/scripts/spacefast_client.py skills/draft-publishing/scripts/draft_publish.py skills/draft-publishing/scripts/draft_library_server.py skills/community-skill-sharing/scripts/package_skill.py",
            "bash -n docker/cont-init.d/00-journalism-bootstrap",
            "bash -n docker/services.d/draft-library/run",
            "python3 -m json.tool skills/newsroom-setup/templates/newsroom.example.json >/dev/null",
            "python3 -m json.tool skills/newsroom-setup/templates/sources.example.json >/dev/null",
            "docker build -t hermes-journalism-starter .",
            "curl -i http://127.0.0.1:8080/healthz",
            "stat -c '%a' /opt/data/newsroom/draft-library/hmac.key",
            "python3 -m unittest tests.test_spacefast_client -v",
        ):
            with self.subTest(command=command):
                self.assertIn(command, readme)
        self.assertIn("fake spacefast", readme.lower())
        self.assertIn("no live spacefast", readme.lower())

    def test_env_example_documents_optional_publishing_variables_without_secrets(self):
        text = (ROOT / ".env.example").read_text()
        for assignment in (
            "# DRAFT_LIBRARY_BASE_URL=", "# RAILWAY_PUBLIC_DOMAIN=",
            "# SPACEFAST_TOKEN=", "# SPACEFAST_TEAM_ID=",
        ):
            self.assertIn(assignment, text)
        self.assertIn("DRAFT_LIBRARY_BASE_URL takes precedence", text)
        self.assertIn("Railway public domain", text)
        self.assertNotRegex(
            text,
            r"(?m)^(?:DRAFT_LIBRARY_BASE_URL|RAILWAY_PUBLIC_DOMAIN|SPACEFAST_TOKEN|SPACEFAST_TEAM_ID)=.+$",
        )

    def test_ci_validates_all_scripts_shell_json_and_image_contract(self):
        workflow = (ROOT / ".github/workflows/validate.yml").read_text()
        for script in (
            "skills/newsroom-setup/scripts/newsroom_config.py",
            "skills/draft-publishing/scripts/draft_store.py",
            "skills/draft-publishing/scripts/spacefast_client.py",
            "skills/draft-publishing/scripts/draft_publish.py",
            "skills/draft-publishing/scripts/draft_library_server.py",
        ):
            self.assertIn(script, workflow)
        for template in (
            "skills/newsroom-setup/templates/newsroom.example.json",
            "skills/newsroom-setup/templates/sources.example.json",
        ):
            self.assertIn(f"python -m json.tool {template}", workflow)
        self.assertIn("python -m unittest discover -s tests -v", workflow)
        self.assertIn("bash -n docker/cont-init.d/00-journalism-bootstrap", workflow)
        self.assertIn("bash -n docker/services.d/draft-library/run", workflow)
        self.assertIn("docker build --tag hermes-journalism-starter:test .", workflow)

    def test_soul_has_concise_newsroom_behavior_without_skill_procedure(self):
        soul = (ROOT / "SOUL.md").read_text()
        lower = soul.lower()
        for phrase in (
            "missing, draft, or partial",
            "newsroom-setup",
            "confirmed editorial style",
            "before producing editorial content",
            "configured official sources",
            "preserve original language",
            "planned",
            "decided",
            "executive",
            "legislative",
            "source failures",
            "never overstate completeness",
        ):
            with self.subTest(phrase=phrase):
                self.assertIn(phrase, lower)
        self.assertLessEqual(len(soul.splitlines()), 30)
        self.assertNotIn("newsroom_config.py", soul)

    def test_readme_and_soul_use_plain_punctuation(self):
        for relative_path in ("README.md", "SOUL.md"):
            with self.subTest(path=relative_path):
                self.assertNotIn("—", (ROOT / relative_path).read_text())

    def test_documentation_warns_against_sensitive_documents(self):
        readme = (ROOT / "README.md").read_text().lower()
        security = (ROOT / "SECURITY.md").read_text().lower()
        self.assertIn("proof of concept", readme)
        self.assertIn("sensitive", readme)
        self.assertIn("direct messages are denied", readme)
        self.assertIn("do not", security)
        self.assertIn("discord", security)
        self.assertIn("baseten", security)

    def test_discord_setup_helper_builds_least_privilege_invite(self):
        helper = (ROOT / "setup/discord-bot-setup.html").read_text()
        self.assertIn("https://discord.com/developers/applications", helper)
        self.assertIn("https://discord.com/oauth2/authorize", helper)
        self.assertIn("274878024768", helper)
        self.assertIn("applications.commands", helper)
        self.assertIn("Message Content Intent", helper)
        self.assertIn("[hidden]", helper)
        self.assertNotIn('const PERMISSIONS = "8"', helper)
        self.assertNotRegex(helper, r'type=["\']password["\']')


class SkillScaffoldTests(unittest.TestCase):
    def skill_text(self, relative_path):
        text = (ROOT / relative_path).read_text()
        self.assertRegex(text, r"\A---\nname: [a-z0-9-]+\ndescription: .+\n---\n")
        return text

    def marked_block(self, text, marker):
        start = f"<!-- {marker}_START -->"
        end = f"<!-- {marker}_END -->"
        self.assertEqual(text.count(start), 1, f"expected one {start} marker")
        self.assertEqual(text.count(end), 1, f"expected one {end} marker")
        self.assertLess(text.index(start), text.index(end))
        return text.split(start, 1)[1].split(end, 1)[0]

    def test_newsroom_setup_assets_and_workflow(self):
        required = [
            "skills/newsroom-setup/SKILL.md",
            "skills/newsroom-setup/references/configuration-contract.md",
            "skills/newsroom-setup/templates/newsroom.example.json",
            "skills/newsroom-setup/templates/sources.example.json",
        ]
        for relative_path in required:
            with self.subTest(path=relative_path):
                self.assertTrue((ROOT / relative_path).is_file())

        skill = self.skill_text("skills/newsroom-setup/SKILL.md")
        lower = skill.lower()
        for phrase in (
            "model connectivity",
            "web access",
            "intended discord destination",
            "persistent volume marker",
            "stop",
            "newsroom or team",
            "places or public bodies",
            "default report language",
            "editorial style guide",
            "house rules",
            "first job",
            "five to ten",
            "candidate",
            "editor approval",
            "plain-language diff",
            "read back",
            "one live, cited result",
            "never schedule monitoring automatically",
            "untrusted data",
        ):
            with self.subTest(phrase=phrase):
                self.assertIn(phrase, lower)
        self.assertIn("$HERMES_HOME/skills/newsroom-setup/scripts/newsroom_config.py", skill)
        for command in ("init", "status", "validate", "apply", "undo"):
            self.assertRegex(lower, rf"\b{command}\b")
        for column in (
            "official status", "scope", "record types", "archive range",
            "latest verified item", "expected delay", "languages",
            "extraction quality", "gaps", "last check",
        ):
            self.assertIn(column, lower)
        self.assertIn("rerunnable", lower)
        self.assertNotIn("one-time", lower)
        self.assertIn("never ask", lower)
        for forbidden_question in (
            "api", "css selector", "feed", "cron expression", "technical platform name",
        ):
            self.assertIn(forbidden_question, lower)
        self.assertIn("never accept secrets in discord", lower)
        self.assertIn("official cross-links", lower)
        self.assertIn("not domain suffix", lower)

    def test_draft_publishing_skill_encodes_disclosure_approval_and_partial_failure(self):
        required = ("skills/draft-publishing/SKILL.md",
                    "skills/draft-publishing/references/publishing-contract.md",
                    "skills/draft-publishing/scripts/spacefast_client.py")
        for relative in required:
            self.assertTrue((ROOT / relative).is_file(), relative)
        skill = self.skill_text("skills/draft-publishing/SKILL.md")
        contract = (ROOT / required[1]).read_text()
        lower = skill.lower()
        for phrase in ("local save first", "/opt/data/newsroom/drafts", "canonical",
                       "disclosure boundary", "editor approval", "auto_private",
                       "private draft library", "never authorizes spacefast",
                       "anonymous spacefast", "shared discord", "never ask",
                       "spacefast_token", "spacefast_team_id", "railway variables",
                       "verify each target", "partial failure"):
            with self.subTest(phrase=phrase):
                self.assertIn(phrase, lower)
        self.assertIn("https://spacefast.com/setup.md", contract)
        self.assertIn("https://api.spacefast.com/openapi.json", contract)
        self.assertIn("2026-08-30", contract)
        setup = (ROOT / "skills/newsroom-setup/SKILL.md").read_text().lower()
        self.assertIn("optional draft preference interview", setup)

    def test_newsroom_setup_has_exactly_five_initial_editorial_questions(self):
        skill = self.skill_text("skills/newsroom-setup/SKILL.md")
        questions = self.marked_block(skill, "INITIAL_EDITORIAL_QUESTIONS")
        question_lines = [line for line in questions.splitlines() if line.strip()]
        self.assertEqual(len(question_lines), 5)
        for number, line in enumerate(question_lines, start=1):
            self.assertRegex(
                line, rf"^{number}\. .+\?$", f"question {number} is not a numbered question"
            )
        all_question_lines = [line for line in skill.splitlines() if line.rstrip().endswith("?")]
        self.assertEqual(all_question_lines, question_lines)

        technical_terms = (
            "api", "css", "selector", "feed", "cron", "platform", "url",
            "endpoint", "token", "secret", "credential", "database",
        )
        for term in technical_terms:
            with self.subTest(term=term):
                self.assertNotRegex(questions.lower(), rf"\b{term}\b")

        self.assertLess(questions.lower().index("style"), questions.lower().index("first job"))

    def test_style_discovery_requires_editor_confirmation_before_content(self):
        skill = self.skill_text("skills/newsroom-setup/SKILL.md")
        lower = skill.lower()
        for phrase in (
            "canadian press",
            "associated press",
            "do not infer",
            "editor confirms",
            "do not produce editorial content",
            "house rules override",
        ):
            with self.subTest(phrase=phrase):
                self.assertIn(phrase, lower)

        route_paths = (
            "skills/government-records-research/SKILL.md",
            "skills/draft-publishing/SKILL.md",
        )
        route_texts = [("SOUL.md", (ROOT / "SOUL.md").read_text().lower())]
        route_texts.extend(
            (route_path, self.skill_text(route_path).lower()) for route_path in route_paths
        )
        for route_path, text in route_texts:
            with self.subTest(route=route_path):
                self.assertIn("current authoritative", text)
                self.assertIn("confirmed_by_editor", text)
                self.assertIn("do not", text)
        research = self.skill_text("skills/government-records-research/SKILL.md").lower()
        self.assertIn("ad hoc scope never relaxes the confirmed-style prerequisite", research)

    def test_newsroom_setup_orders_deployment_checks_before_any_mutation(self):
        skill = self.skill_text("skills/newsroom-setup/SKILL.md")
        deployment_checks = self.marked_block(skill, "DEPLOYMENT_CHECKS")
        mutation = self.marked_block(skill, "CONFIGURATION_SOURCE_MUTATION")
        self.assertLess(
            skill.index("<!-- DEPLOYMENT_CHECKS_START -->"),
            skill.index("<!-- INITIAL_EDITORIAL_QUESTIONS_START -->"),
        )
        self.assertLess(
            skill.index("<!-- DEPLOYMENT_CHECKS_END -->"),
            skill.index("<!-- CONFIGURATION_SOURCE_MUTATION_START -->"),
        )
        for check in (
            "model connectivity", "web access", "discord destination", "persistent volume",
        ):
            self.assertIn(check, deployment_checks.lower())
        for mutation_term in (" init ", " apply ", "promot", "activat", "rewrite", "modify"):
            self.assertNotIn(mutation_term, f" {deployment_checks.lower()} ")
        self.assertIn("apply", mutation.lower())

    def test_newsroom_examples_match_contract_and_are_safe(self):
        newsroom = json.loads(
            (ROOT / "skills/newsroom-setup/templates/newsroom.example.json").read_text()
        )
        sources = json.loads(
            (ROOT / "skills/newsroom-setup/templates/sources.example.json").read_text()
        )
        self.assertNotEqual(newsroom["newsroom"]["name"], "Unnamed newsroom")
        self.assertEqual({source["status"] for source in sources["sources"]}, {"candidate", "active"})
        self.assertTrue(all(source["official_url"].startswith("https://") for source in sources["sources"]))
        self.assertTrue(all("example" in source["official_url"] for source in sources["sources"]))
        self.assertTrue(any(source["authority_status"] == "official" for source in sources["sources"]))
        self.assertIn("placeholder", newsroom["newsroom"]["name"].lower())
        self.assertTrue(all("placeholder" in source["id"] for source in sources["sources"]))
        serialized = json.dumps([newsroom, sources]).lower()
        for secret_field in ('"api_key"', '"password"', '"secret"', '"token"'):
            self.assertNotIn(secret_field, serialized)

    def test_government_records_assets_and_workflow(self):
        required = [
            "skills/government-records-research/SKILL.md",
            "skills/government-records-research/references/record-types.md",
            "skills/government-records-research/references/report-template.md",
        ]
        for relative_path in required:
            with self.subTest(path=relative_path):
                self.assertTrue((ROOT / relative_path).is_file())

        skill = self.skill_text("skills/government-records-research/SKILL.md")
        lower = skill.lower()
        self.assertIn("$HERMES_HOME/newsroom/newsroom.json", skill)
        self.assertIn("$HERMES_HOME/newsroom/sources.json", skill)
        for phrase in (
            "active validated official sources first",
            "candidate sources are discovery leads",
            "ad hoc research",
            "coverage warning",
            "preserve original language",
            "label translations",
            "retrieval time",
            "page/section/item",
            "checked sources and failures",
            "untrusted data",
            "never infer",
        ):
            with self.subTest(phrase=phrase):
                self.assertIn(phrase, lower)
        for record_type in (
            "notice", "calendar", "agenda", "order paper", "minutes",
            "official journal", "transcript", "hansard", "vote", "report",
            "attachment", "video", "captions", "executive order", "bill",
            "bylaw", "legal text",
        ):
            self.assertIn(record_type, lower)
        labels = (
            "verified records found",
            "no matching records found in all successfully checked sources",
            "search incomplete",
        )
        for label in labels:
            self.assertIn(f"`{label}`", skill)
        self.assertIn("exactly one", lower)
        self.assertIn("URL, publisher/body, document date, retrieval time", skill)

    def test_research_status_values_are_exact_lowercase_literals_everywhere(self):
        paths = list((ROOT / "skills/government-records-research").glob("**/*.md"))
        labels = (
            "verified records found",
            "no matching records found in all successfully checked sources",
            "search incomplete",
        )
        status_pattern = re.compile(
            r"verified records found|no matching records found in all successfully checked sources|search incomplete",
            flags=re.IGNORECASE,
        )
        for path in paths:
            for match in status_pattern.finditer(path.read_text()):
                with self.subTest(path=path, value=match.group(0)):
                    self.assertIn(match.group(0), labels)

        skill = (ROOT / "skills/government-records-research/SKILL.md").read_text()
        template = (
            ROOT / "skills/government-records-research/references/report-template.md"
        ).read_text()
        for label in labels:
            self.assertIn(f"`{label}`", skill)
            self.assertIn(f"`{label}`", template)

    def test_research_status_precedence_is_explicit_and_ordered(self):
        skill = (ROOT / "skills/government-records-research/SKILL.md").read_text().lower()
        template = (
            ROOT / "skills/government-records-research/references/report-template.md"
        ).read_text().lower()
        for label, text in (("skill", skill), ("template", template)):
            with self.subTest(document=label):
                incomplete = text.index("1. `search incomplete`")
                found = text.index("2. `verified records found`")
                no_match = text.index(
                    "3. `no matching records found in all successfully checked sources`"
                )
                self.assertLess(incomplete, found)
                self.assertLess(found, no_match)
                self.assertIn("incomplete wins", text)
                self.assertIn("even if responsive", text)
                self.assertIn("every scoped source and check succeeded", text)

    def test_report_template_matches_unofficial_only_status_precedence(self):
        skill = (ROOT / "skills/government-records-research/SKILL.md").read_text().lower()
        template = (
            ROOT / "skills/government-records-research/references/report-template.md"
        ).read_text().lower()
        condition = "evidence is only unofficial"
        self.assertIn(condition, skill)
        self.assertIn(condition, template)
        for text in (skill, template):
            self.assertIn(condition, text[text.index("1. `search incomplete`"):])

    def test_setup_states_live_proof_and_non_text_evidence_are_unambiguous(self):
        setup = (ROOT / "skills/newsroom-setup/SKILL.md").read_text().lower()
        contract = (
            ROOT / "skills/newsroom-setup/references/configuration-contract.md"
        ).read_text().lower()
        research = (ROOT / "skills/government-records-research/SKILL.md").read_text().lower()
        report = (
            ROOT / "skills/government-records-research/references/report-template.md"
        ).read_text().lower()

        for text in (setup, contract):
            self.assertIn("interview or source discovery", text)
            self.assertIn("source coverage or test gaps remain", text)
            self.assertIn("editor approved", text)
            self.assertIn("first live check completed", text)
            self.assertIn("active validated official or official_mirror source", text)
            self.assertIn("apply sources first", text)
            self.assertIn("mark newsroom complete last", text)
            self.assertIn("mark newsroom partial first", text)
            self.assertIn("if the second operation fails", text)
            self.assertIn("do not falsely claim completion", text)

        for text in (research, report):
            self.assertIn("draft and partial", text)
            self.assertIn("unfinished", text)
            self.assertIn("exact quote only when useful and directly present", text)
            self.assertIn("video, image, scan, or table", text)
            self.assertIn("timestamp, page, row, or item locator", text)
            self.assertIn("never invent a transcription", text)

        for outcome in (
            "cited positive result",
            "cited no-match across successful checks",
            "explicit failed or incomplete check",
        ):
            self.assertIn(outcome, setup)
        self.assertIn("same final status precedence", setup)

    def test_research_safeguards_proceedings_dates_calendars_and_inactive_sources(self):
        skill = (ROOT / "skills/government-records-research/SKILL.md").read_text().lower()
        template = (
            ROOT / "skills/government-records-research/references/report-template.md"
        ).read_text().lower()

        self.assertIn(
            "never assume a proceeding is public, open, recorded, streamed, or transcribed", skill
        )
        self.assertIn("do not assume the gregorian calendar", skill)
        self.assertIn("official or local calendar", skill)
        self.assertIn("displayed date", skill)
        self.assertIn("normalized gregorian or iso date", skill)
        self.assertIn("label every calendar conversion", skill)
        self.assertIn("conversion uncertainty", skill)

        self.assertIn("inactive sources", skill)
        self.assertIn("coverage diagnostics or history", skill)
        self.assertIn("must not support claims", skill)
        self.assertIn("editor reactivates and revalidates", skill)
        self.assertIn("exclude", skill)

        for phrase in (
            "official or local calendar", "displayed date", "normalized gregorian or iso date",
            "calendar conversion", "conversion uncertainty", "inactive sources",
        ):
            with self.subTest(phrase=phrase):
                self.assertIn(phrase, template)

    def test_new_skill_files_use_plain_punctuation(self):
        paths = list((ROOT / "skills/newsroom-setup").glob("**/*.md"))
        paths += list((ROOT / "skills/government-records-research").glob("**/*.md"))
        self.assertNotEqual(paths, [])
        for path in paths:
            with self.subTest(path=path):
                self.assertNotIn("—", path.read_text())


if __name__ == "__main__":
    unittest.main()
