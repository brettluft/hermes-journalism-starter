import importlib.util
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = ROOT / "skills/community-skill-sharing/scripts"
SCRIPT = SCRIPTS / "package_skill.py"


def load_module():
    spec = importlib.util.spec_from_file_location("package_skill_under_test", SCRIPT)
    if spec is None or spec.loader is None:
        raise ImportError("package skill module is absent")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


pkg = load_module()


class PackageSkillTests(unittest.TestCase):
    def test_parse_frontmatter_valid(self):
        content = "---\nname: my-skill\ndescription: Test description\n---\n# Body\n"
        meta, body = pkg.parse_frontmatter(content)
        self.assertIsNotNone(meta)
        self.assertEqual(meta["name"], "my-skill")
        self.assertEqual(meta["description"], "Test description")
        self.assertIn("# Body", body)

    def test_parse_frontmatter_invalid(self):
        content = "No frontmatter here\n"
        meta, body = pkg.parse_frontmatter(content)
        self.assertIsNone(meta)
        self.assertEqual(body, content)

    def test_sanitize_text_normalizes_paths(self):
        text = "Files live in /opt/data/newsroom/sources.json and /opt/data/skills/my-skill"
        sanitized = pkg.sanitize_text(text)
        self.assertIn("$HERMES_HOME/newsroom/sources.json", sanitized)
        self.assertIn("$HERMES_HOME/skills/my-skill", sanitized)
        self.assertNotIn("/opt/data", sanitized)

    def test_scan_for_secrets_detects_keys_and_tokens(self):
        clean_text = "This is a clean file without credentials."
        self.assertEqual(pkg.scan_for_secrets(clean_text), [])

        dirty_text = "Here is my key: sk-abcdefghijklmnopqrstuvwxyz123456"
        findings = pkg.scan_for_secrets(dirty_text)
        self.assertTrue(any("OpenAI/API Key" in f for f in findings))

    def test_validate_and_package_valid_skill(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            skill_dir = Path(temp_dir) / "test-skill"
            skill_dir.mkdir()
            (skill_dir / "SKILL.md").write_text(
                "---\nname: test-skill\ndescription: A test skill for journalists\n---\n# Test Skill\n"
            )
            scripts_dir = skill_dir / "scripts"
            scripts_dir.mkdir()
            (scripts_dir / "helper.py").write_text("def hello():\n    return 'world'\n")

            result = pkg.validate_and_package_skill(skill_dir, author="@reporter")
            self.assertEqual(result["status"], "ok")
            self.assertEqual(result["skill_name"], "test-skill")
            self.assertIn("SKILL.md", result["files_included"])
            self.assertIn("scripts/helper.py", result["files_included"])
            self.assertIn("https://github.com/brettluft/hermes-journalism-starter/issues/new?", result["submission_url"])

    def test_validate_and_package_fails_on_secrets(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            skill_dir = Path(temp_dir) / "leaky-skill"
            skill_dir.mkdir()
            (skill_dir / "SKILL.md").write_text(
                "---\nname: leaky-skill\ndescription: Has a secret\n---\nUse key sk-abcdefghijklmnopqrstuvwxyz123456\n"
            )
            result = pkg.validate_and_package_skill(skill_dir)
            self.assertEqual(result["status"], "error")
            self.assertEqual(result["code"], "VALIDATION_FAILED")
            self.assertTrue(any("OpenAI/API Key" in d for d in result.get("details", [])))

    def test_validate_and_package_fails_on_broken_python_syntax(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            skill_dir = Path(temp_dir) / "broken-skill"
            skill_dir.mkdir()
            (skill_dir / "SKILL.md").write_text(
                "---\nname: broken-skill\ndescription: Has syntax error\n---\n"
            )
            scripts_dir = skill_dir / "scripts"
            scripts_dir.mkdir()
            (scripts_dir / "bad.py").write_text("def broken_syntax(:\n")

            result = pkg.validate_and_package_skill(skill_dir)
            self.assertEqual(result["status"], "error")
            self.assertEqual(result["code"], "VALIDATION_FAILED")

    def test_cli_execution_with_json_flag(self):
        cli_path = ROOT / "skills/community-skill-sharing/scripts/package_skill.py"
        target_skill = ROOT / "skills/government-records-research"

        proc = subprocess.run(
            [sys.executable, str(cli_path), "--skill", str(target_skill), "--author", "Test Author", "--json"],
            capture_output=True,
            text=True,
            check=False,
        )
        self.assertEqual(proc.returncode, 0, proc.stderr)
        data = json.loads(proc.stdout)
        self.assertEqual(data["status"], "ok")
        self.assertEqual(data["skill_name"], "government-records-research")
        self.assertIn("SKILL.md", data["files_included"])


if __name__ == "__main__":
    unittest.main()
