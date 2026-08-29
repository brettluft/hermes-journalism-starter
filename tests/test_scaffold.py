import json
import os
from pathlib import Path
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
        self.assertIn("/opt/hermes/skills/", dockerfile)
        self.assertIn('CMD ["gateway", "run"]', dockerfile)

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

    def test_bootstrap_only_validates_and_never_writes_persistent_state(self):
        script = (ROOT / "docker/cont-init.d/00-journalism-bootstrap").read_text()
        for forbidden in ("HERMES_HOME", "mkdir", "install", "cp -", "mv ", "rm "):
            with self.subTest(forbidden=forbidden):
                self.assertNotIn(forbidden, script)

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


if __name__ == "__main__":
    unittest.main()
