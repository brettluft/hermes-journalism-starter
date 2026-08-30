import os
from pathlib import Path
import re
import unittest


ROOT = Path(__file__).resolve().parents[1]


class ContainerServiceTests(unittest.TestCase):
    def test_draft_library_run_script_is_exact_write_free_exec(self):
        run = ROOT / "docker/services.d/draft-library/run"
        self.assertTrue(run.is_file())
        self.assertTrue(run.stat().st_mode & 0o111)
        text = run.read_text()
        self.assertEqual(text, (
            "#!/usr/bin/env bash\n"
            "set -euo pipefail\n"
            "unset SPACEFAST_TOKEN SPACEFAST_TEAM_ID\n"
            "exec python3 /opt/hermes/skills/draft-publishing/scripts/"
            "draft_library_server.py\n"
        ))
        for forbidden in ("mkdir", "touch", "install", "chmod", "chown", "init", "hmac.key"):
            with self.subTest(forbidden=forbidden):
                self.assertNotIn(forbidden, text)
        self.assertNotRegex(text, r"(?:^|\s)&(?:\s|$)")

    def test_dockerfile_installs_service_and_preserves_upstream_start_contract(self):
        text = (ROOT / "Dockerfile").read_text()
        self.assertIn(
            "COPY --chmod=0755 docker/services.d/draft-library/run "
            "/etc/services.d/draft-library/run", text)
        self.assertIn("COPY skills/ /opt/hermes/skills/", text)
        self.assertNotIn("ENTRYPOINT", text)
        self.assertEqual(text.count('CMD ["gateway", "run"]'), 1)
        self.assertLess(text.index("00-journalism-bootstrap"), text.index("COPY skills/"))

    def test_ci_runs_full_tests_compiles_three_scripts_checks_both_shell_scripts_and_builds(self):
        text = (ROOT / ".github/workflows/validate.yml").read_text()
        self.assertIn("python -m unittest discover -s tests -v", text)
        for script in ("draft_store.py", "draft_publish.py", "draft_library_server.py"):
            with self.subTest(script=script):
                self.assertIn(f"skills/draft-publishing/scripts/{script}", text)
        for script in ("docker/cont-init.d/00-journalism-bootstrap",
                       "docker/services.d/draft-library/run"):
            with self.subTest(script=script):
                self.assertRegex(text, rf"bash -n [^\n]*{re.escape(script)}")
        self.assertRegex(text, r"docker build [^\n]*\.")

    def test_optional_spacefast_variables_are_not_bootstrap_requirements(self):
        bootstrap = (ROOT / "docker/cont-init.d/00-journalism-bootstrap").read_text()
        self.assertNotIn("SPACEFAST_TOKEN", bootstrap)
        self.assertNotIn("SPACEFAST_TEAM_ID", bootstrap)

    def test_operator_documents_public_service_smoke_sequence(self):
        readme = (ROOT / "README.md").read_text().lower()
        health = readme.index("curl -i http://127.0.0.1:8080/healthz")
        unavailable = readme.index("503", health)
        initialize = readme.index("draft_publish.py init", unavailable)
        mode = readme.index("stat -c '%a'", initialize)
        self.assertLess(health, unavailable)
        self.assertLess(unavailable, initialize)
        self.assertLess(initialize, mode)
        self.assertIn("600", readme[mode:mode + 300])


if __name__ == "__main__":
    unittest.main()
