import base64
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import signal
import subprocess
import sys
import tempfile
import unittest
from unittest import mock


ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "skills/newsroom-setup/scripts/newsroom_config.py"
SPEC = importlib.util.spec_from_file_location("newsroom_config_under_test", SCRIPT)
assert SPEC is not None and SPEC.loader is not None
CONFIG_MODULE = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(CONFIG_MODULE)


def newsroom(**overrides):
    document = {
        "schema_version": 1,
        "revision": 0,
        "setup_status": "partial",
        "newsroom": {
            "name": "The Daily Test",
            "timezone": "Europe/Paris",
            "report_languages": ["en", "fr-FR"],
        },
        "coverage": {"places": [], "public_bodies": [], "beats": []},
        "preferences": {
            "preserve_original_language": True,
            "require_primary_source_citations": True,
            "allow_unofficial_fallbacks": False,
        },
    }
    document.update(overrides)
    return document


def sources(**overrides):
    document = {
        "schema_version": 1,
        "revision": 0,
        "sources": [
            {
                "id": "city-council",
                "official_url": "https://example.test/council",
                "authority_status": "official",
                "status": "active",
                "record_types": {"minutes": "verified", "votes": "partial"},
                "languages": ["en"],
                "last_validated_at": "2026-08-30T12:30:00Z",
            }
        ],
    }
    document.update(overrides)
    return document


class NewsroomConfigTests(unittest.TestCase):
    maxDiff = None

    def run_cli(self, *arguments, env=None):
        result = subprocess.run(
            [sys.executable, str(SCRIPT), *map(str, arguments)],
            text=True,
            capture_output=True,
            check=False,
            env=env,
        )
        try:
            payload = json.loads(result.stdout)
        except json.JSONDecodeError:
            payload = None
        return result, payload

    def write_json(self, directory, name, document):
        path = Path(directory) / name
        path.write_text(json.dumps(document), encoding="utf-8")
        return path

    def init_home(self, home):
        result, payload = self.run_cli("init", "--home", home)
        self.assertEqual(result.returncode, 0, result.stderr or result.stdout)
        self.assertIsNotNone(payload, result.stdout)
        self.assertTrue(payload["ok"])
        return result, payload

    def test_init_creates_only_fixed_newsroom_files_and_is_idempotent(self):
        with tempfile.TemporaryDirectory() as temporary:
            home = Path(temporary)
            result, payload = self.init_home(home)
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertTrue(payload["ok"])
            files = sorted(
                str(path.relative_to(home)) for path in home.rglob("*") if path.is_file()
            )
            self.assertEqual(
                files,
                [
                    "newsroom/.config.lock",
                    "newsroom/audit/config-events.jsonl",
                    "newsroom/newsroom.json",
                    "newsroom/sources.json",
                ],
            )
            marker = newsroom(revision=41)
            (home / "newsroom/newsroom.json").write_text(json.dumps(marker))
            result, _ = self.init_home(home)
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertEqual(json.loads((home / "newsroom/newsroom.json").read_text()), marker)

    def test_home_may_come_from_environment_and_is_required_otherwise(self):
        with tempfile.TemporaryDirectory() as temporary:
            env = os.environ.copy()
            env["HERMES_HOME"] = temporary
            result, _ = self.run_cli("init", env=env)
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertTrue((Path(temporary) / "newsroom/newsroom.json").is_file())
        env = os.environ.copy()
        env.pop("HERMES_HOME", None)
        result, payload = self.run_cli("status", env=env)
        self.assertNotEqual(result.returncode, 0)
        self.assertFalse(payload["ok"])

    def test_status_outputs_json_and_reports_valid_documents(self):
        with tempfile.TemporaryDirectory() as temporary:
            self.init_home(temporary)
            result, payload = self.run_cli("status", "--home", temporary)
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertEqual(payload["newsroom"]["revision"], 0)
            self.assertEqual(payload["sources"]["revision"], 0)
            self.assertTrue(payload["newsroom"]["valid"])
            self.assertTrue(payload["sources"]["valid"])

    def test_status_rejects_directly_mutated_complete_profile_without_qualifying_source(self):
        active = sources()["sources"][0]
        source_sets = {
            "empty": [],
            "candidate-only": [{**active, "status": "candidate"}],
            "inactive-only": [{**active, "status": "inactive"}],
            "unofficial-only": [
                {
                    key: value
                    for key, value in {**active, "authority_status": "non_official"}.items()
                    if key != "last_validated_at"
                }
            ],
        }
        for name, source_set in source_sets.items():
            with self.subTest(name=name), tempfile.TemporaryDirectory() as temporary:
                home = Path(temporary)
                self.init_home(home)
                root = home / "newsroom"
                (root / "newsroom.json").write_text(
                    json.dumps(newsroom(setup_status="complete")), encoding="utf-8"
                )
                (root / "sources.json").write_text(
                    json.dumps(sources(sources=source_set)), encoding="utf-8"
                )

                result, payload = self.run_cli("status", "--home", home)

                self.assertNotEqual(result.returncode, 0, result.stdout)
                self.assertIsInstance(payload, dict)
                self.assertFalse(payload["ok"])
                self.assertEqual(payload["command"], "status")
                self.assertTrue(payload["newsroom"]["valid"])
                self.assertTrue(payload["sources"]["valid"])
                self.assertIn("active validated official", payload["error"])
                self.assertNotIn(str(home), payload["error"])
                self.assertNotIn("Traceback", result.stderr)

    def test_status_reports_draft_and_partial_profiles_normally_without_qualifying_sources(self):
        for setup_status in ("draft", "partial"):
            with self.subTest(setup_status=setup_status), tempfile.TemporaryDirectory() as temporary:
                home = Path(temporary)
                self.init_home(home)
                root = home / "newsroom"
                (root / "newsroom.json").write_text(
                    json.dumps(newsroom(setup_status=setup_status)), encoding="utf-8"
                )
                (root / "sources.json").write_text(
                    json.dumps(sources(sources=[])), encoding="utf-8"
                )

                result, payload = self.run_cli("status", "--home", home)

                self.assertEqual(result.returncode, 0, result.stderr or result.stdout)
                self.assertTrue(payload["ok"])
                self.assertTrue(payload["newsroom"]["valid"])
                self.assertTrue(payload["sources"]["valid"])

    def test_validate_newsroom_accepts_contract(self):
        with tempfile.TemporaryDirectory() as temporary:
            path = self.write_json(temporary, "newsroom.json", newsroom())
            result, payload = self.run_cli("validate", "--kind", "newsroom", "--input", path)
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertTrue(payload["valid"])

    def test_validate_complete_newsroom_remains_independent_of_profile_sources(self):
        with tempfile.TemporaryDirectory() as temporary:
            path = self.write_json(
                temporary, "complete-newsroom.json", newsroom(setup_status="complete")
            )
            result, payload = self.run_cli(
                "validate", "--kind", "newsroom", "--input", path
            )
            self.assertEqual(result.returncode, 0, result.stderr or result.stdout)
            self.assertTrue(payload["valid"])

    def test_apply_complete_rejects_empty_candidate_and_inactive_sources_before_mutation(self):
        active = sources()["sources"][0]
        source_sets = {
            "empty": [],
            "candidate": [{**active, "status": "candidate"}],
            "inactive": [{**active, "status": "inactive"}],
        }
        for name, source_set in source_sets.items():
            with self.subTest(name=name), tempfile.TemporaryDirectory() as temporary:
                home = Path(temporary) / "home"
                self.init_home(home)
                root = home / "newsroom"
                (root / "sources.json").write_text(
                    json.dumps(sources(sources=source_set)), encoding="utf-8"
                )
                target = root / "newsroom.json"
                audit = root / "audit/config-events.jsonl"
                before = (target.read_bytes(), audit.read_bytes())
                candidate = self.write_json(
                    temporary, "complete.json", newsroom(setup_status="complete")
                )
                result, payload = self.run_cli(
                    "apply", "--home", home, "--kind", "newsroom", "--input", candidate
                )
                self.assertNotEqual(result.returncode, 0, result.stdout)
                self.assertFalse(payload["ok"])
                self.assertIn("active validated official", payload["error"])
                self.assertEqual((target.read_bytes(), audit.read_bytes()), before)
                self.assertFalse((root / "newsroom.json.previous").exists())
                self.assertFalse((root / ".config-transaction.json").exists())

    def test_apply_sources_cannot_remove_last_active_official_source_while_complete(self):
        with tempfile.TemporaryDirectory() as temporary:
            home = Path(temporary) / "home"
            self.init_home(home)
            active_path = self.write_json(temporary, "active.json", sources())
            result, _ = self.run_cli(
                "apply", "--home", home, "--kind", "sources", "--input", active_path
            )
            self.assertEqual(result.returncode, 0, result.stdout)
            complete_path = self.write_json(
                temporary, "complete.json", newsroom(setup_status="complete")
            )
            result, _ = self.run_cli(
                "apply", "--home", home, "--kind", "newsroom", "--input", complete_path
            )
            self.assertEqual(result.returncode, 0, result.stdout)
            root = home / "newsroom"
            target = root / "sources.json"
            audit = root / "audit/config-events.jsonl"
            before = (target.read_bytes(), audit.read_bytes())
            for name, replacement_sources in (
                ("empty", []),
                ("candidate", [{**sources()["sources"][0], "status": "candidate"}]),
                ("inactive", [{**sources()["sources"][0], "status": "inactive"}]),
            ):
                with self.subTest(replacement=name):
                    path = self.write_json(
                        temporary, f"{name}.json", sources(sources=replacement_sources)
                    )
                    result, payload = self.run_cli(
                        "apply", "--home", home, "--kind", "sources", "--input", path
                    )
                    self.assertNotEqual(result.returncode, 0, result.stdout)
                    self.assertFalse(payload["ok"])
                    self.assertIn("active validated official", payload["error"])
                    self.assertEqual((target.read_bytes(), audit.read_bytes()), before)
                    self.assertFalse((root / ".config-transaction.json").exists())

    def test_undo_sources_cannot_restore_empty_set_while_newsroom_is_complete(self):
        with tempfile.TemporaryDirectory() as temporary:
            home = Path(temporary) / "home"
            self.init_home(home)
            active_path = self.write_json(temporary, "active.json", sources())
            result, _ = self.run_cli(
                "apply", "--home", home, "--kind", "sources", "--input", active_path
            )
            self.assertEqual(result.returncode, 0, result.stdout)
            complete_path = self.write_json(
                temporary, "complete.json", newsroom(setup_status="complete")
            )
            result, _ = self.run_cli(
                "apply", "--home", home, "--kind", "newsroom", "--input", complete_path
            )
            self.assertEqual(result.returncode, 0, result.stdout)
            root = home / "newsroom"
            target = root / "sources.json"
            audit = root / "audit/config-events.jsonl"
            before = (target.read_bytes(), audit.read_bytes())
            result, payload = self.run_cli("undo", "--home", home, "--kind", "sources")
            self.assertNotEqual(result.returncode, 0, result.stdout)
            self.assertFalse(payload["ok"])
            self.assertIn("active validated official", payload["error"])
            self.assertEqual((target.read_bytes(), audit.read_bytes()), before)

    def test_undo_newsroom_cannot_restore_complete_after_sources_were_removed(self):
        with tempfile.TemporaryDirectory() as temporary:
            home = Path(temporary) / "home"
            self.init_home(home)
            changes = (
                ("sources", self.write_json(temporary, "active.json", sources())),
                ("newsroom", self.write_json(
                    temporary, "complete.json", newsroom(setup_status="complete")
                )),
                ("newsroom", self.write_json(
                    temporary, "partial.json", newsroom(setup_status="partial")
                )),
                ("sources", self.write_json(temporary, "empty.json", sources(sources=[]))),
            )
            for kind, path in changes:
                result, _ = self.run_cli(
                    "apply", "--home", home, "--kind", kind, "--input", path
                )
                self.assertEqual(result.returncode, 0, result.stdout)
            root = home / "newsroom"
            target = root / "newsroom.json"
            audit = root / "audit/config-events.jsonl"
            before = (target.read_bytes(), audit.read_bytes())
            result, payload = self.run_cli("undo", "--home", home, "--kind", "newsroom")
            self.assertNotEqual(result.returncode, 0, result.stdout)
            self.assertFalse(payload["ok"])
            self.assertIn("active validated official", payload["error"])
            self.assertEqual((target.read_bytes(), audit.read_bytes()), before)

    def test_validate_newsroom_rejects_each_invalid_contract_field(self):
        valid = newsroom()
        cases = {
            "schema": {**valid, "schema_version": 2},
            "revision_bool": {**valid, "revision": True},
            "status": {**valid, "setup_status": "ready"},
            "name": {**valid, "newsroom": {**valid["newsroom"], "name": " "}},
            "timezone": {**valid, "newsroom": {**valid["newsroom"], "timezone": "UTC"}},
            "languages_empty": {**valid, "newsroom": {**valid["newsroom"], "report_languages": []}},
            "language_invalid": {**valid, "newsroom": {**valid["newsroom"], "report_languages": ["english!"]}},
            "coverage_array": {**valid, "coverage": {**valid["coverage"], "beats": "politics"}},
            "preference_boolean": {**valid, "preferences": {**valid["preferences"], "allow_unofficial_fallbacks": 0}},
        }
        with tempfile.TemporaryDirectory() as temporary:
            for name, document in cases.items():
                with self.subTest(name=name):
                    path = self.write_json(temporary, name + ".json", document)
                    result, payload = self.run_cli("validate", "--kind", "newsroom", "--input", path)
                    self.assertNotEqual(result.returncode, 0)
                    self.assertFalse(payload["valid"])

    def test_validate_newsroom_rejects_malformed_iana_timezones(self):
        invalid_timezones = (
            "Not/A Zone",
            "A//B",
            "https://example.test",
            " / ",
        )
        with tempfile.TemporaryDirectory() as temporary:
            for index, timezone_name in enumerate(invalid_timezones):
                with self.subTest(timezone=timezone_name):
                    document = newsroom(
                        newsroom={**newsroom()["newsroom"], "timezone": timezone_name}
                    )
                    path = self.write_json(temporary, f"timezone-{index}.json", document)
                    result, payload = self.run_cli(
                        "validate", "--kind", "newsroom", "--input", path
                    )
                    self.assertNotEqual(result.returncode, 0)
                    self.assertIsNotNone(payload, result.stdout)
                    self.assertFalse(payload["ok"])
                    self.assertFalse(payload["valid"])
                    self.assertIn("newsroom.timezone", payload["error"])

    def test_validate_newsroom_accepts_normal_iana_timezones(self):
        with tempfile.TemporaryDirectory() as temporary:
            for index, timezone_name in enumerate(
                ("America/Edmonton", "America/Argentina/Buenos_Aires")
            ):
                with self.subTest(timezone=timezone_name):
                    document = newsroom(
                        newsroom={**newsroom()["newsroom"], "timezone": timezone_name}
                    )
                    path = self.write_json(temporary, f"timezone-valid-{index}.json", document)
                    result, payload = self.run_cli(
                        "validate", "--kind", "newsroom", "--input", path
                    )
                    self.assertEqual(result.returncode, 0, result.stderr)
                    self.assertTrue(payload["ok"])
                    self.assertTrue(payload["valid"])

    def test_validate_sources_accepts_contract(self):
        with tempfile.TemporaryDirectory() as temporary:
            path = self.write_json(temporary, "sources.json", sources())
            result, payload = self.run_cli("validate", "--kind", "sources", "--input", path)
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertTrue(payload["valid"])

    def test_validate_sources_rejects_invalid_contract_fields(self):
        source = sources()["sources"][0]
        cases = {
            "unsafe_id": {**source, "id": "../escape"},
            "http": {**source, "official_url": "http://example.test"},
            "authority": {**source, "authority_status": "rumoured"},
            "status": {**source, "status": "verified"},
            "record_type": {**source, "record_types": {"minutes": "good"}},
            "languages": {**source, "languages": []},
            "timestamp": {key: value for key, value in source.items() if key != "last_validated_at"},
            "bad_timestamp": {**source, "last_validated_at": "yesterday"},
        }
        with tempfile.TemporaryDirectory() as temporary:
            for name, item in cases.items():
                with self.subTest(name=name):
                    path = self.write_json(temporary, name + ".json", sources(sources=[item]))
                    result, payload = self.run_cli("validate", "--kind", "sources", "--input", path)
                    self.assertNotEqual(result.returncode, 0)
                    self.assertFalse(payload["valid"])
            duplicate = sources(sources=[source, dict(source)])
            path = self.write_json(temporary, "duplicate.json", duplicate)
            result, _ = self.run_cli("validate", "--kind", "sources", "--input", path)
            self.assertNotEqual(result.returncode, 0)

    def test_validate_sources_reports_malformed_url_as_json_without_traceback(self):
        malformed = {
            **sources()["sources"][0],
            "official_url": "https://[::1",
        }
        with tempfile.TemporaryDirectory() as temporary:
            path = self.write_json(temporary, "malformed-url.json", sources(sources=[malformed]))
            result, payload = self.run_cli("validate", "--kind", "sources", "--input", path)

        self.assertNotEqual(result.returncode, 0)
        self.assertIsNotNone(payload, result.stdout)
        self.assertFalse(payload["ok"])
        self.assertFalse(payload["valid"])
        self.assertIn("official_url", payload["error"])
        self.assertNotIn("Traceback", result.stderr)

    def test_validate_sources_rejects_malformed_https_url_syntax(self):
        invalid_urls = (
            "https://example.test:bad",
            "https://exa mple.test",
            "https://example.test/%zz",
            r"https://example.test\\evil.test",
        )
        source = sources()["sources"][0]
        with tempfile.TemporaryDirectory() as temporary:
            for index, official_url in enumerate(invalid_urls):
                with self.subTest(official_url=official_url):
                    item = {**source, "official_url": official_url}
                    path = self.write_json(
                        temporary, f"url-{index}.json", sources(sources=[item])
                    )
                    result, payload = self.run_cli(
                        "validate", "--kind", "sources", "--input", path
                    )
                    self.assertNotEqual(result.returncode, 0)
                    self.assertIsNotNone(payload, result.stdout)
                    self.assertFalse(payload["ok"])
                    self.assertFalse(payload["valid"])
                    self.assertIn("official_url", payload["error"])
                    self.assertNotIn("Traceback", result.stderr)

    def test_active_non_official_source_does_not_require_timestamp(self):
        source = sources()["sources"][0]
        source = {**source, "authority_status": "non_official"}
        source.pop("last_validated_at")
        with tempfile.TemporaryDirectory() as temporary:
            path = self.write_json(temporary, "sources.json", sources(sources=[source]))
            result, _ = self.run_cli("validate", "--kind", "sources", "--input", path)
            self.assertEqual(result.returncode, 0, result.stderr)

    def test_recursively_rejects_secret_field_names_and_does_not_audit_content(self):
        with tempfile.TemporaryDirectory() as temporary:
            home = Path(temporary) / "home"
            self.init_home(home)
            for forbidden in ("api_key", "password", "secret", "token"):
                document = newsroom(metadata={"nested": {forbidden: "DO-NOT-RECORD"}})
                path = self.write_json(temporary, forbidden + ".json", document)
                result, _ = self.run_cli("apply", "--home", home, "--kind", "newsroom", "--input", path)
                self.assertNotEqual(result.returncode, 0)
            audit = (home / "newsroom/audit/config-events.jsonl").read_text()
            self.assertNotIn("DO-NOT-RECORD", audit)

    def test_apply_increments_stored_revision_keeps_one_backup_and_audits_hashes(self):
        with tempfile.TemporaryDirectory() as temporary:
            home = Path(temporary) / "home"
            self.init_home(home)
            first = newsroom(revision=999, newsroom={**newsroom()["newsroom"], "name": "First"})
            first_path = self.write_json(temporary, "first.json", first)
            result, payload = self.run_cli("apply", "--home", home, "--kind", "newsroom", "--input", first_path)
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertEqual(payload["revision"], 1)
            stored_path = home / "newsroom/newsroom.json"
            backup_path = home / "newsroom/newsroom.json.previous"
            stored = json.loads(stored_path.read_text())
            self.assertEqual(stored["revision"], 1)
            self.assertEqual(stored["newsroom"]["name"], "First")
            self.assertEqual(json.loads(backup_path.read_text())["revision"], 0)

            second = newsroom(revision=0, newsroom={**newsroom()["newsroom"], "name": "Second"})
            second_path = self.write_json(temporary, "second.json", second)
            result, _ = self.run_cli("apply", "--home", home, "--kind", "newsroom", "--input", second_path)
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertEqual(json.loads(stored_path.read_text())["revision"], 2)
            backup = json.loads(backup_path.read_text())
            self.assertEqual(backup["revision"], 1)
            self.assertEqual(backup["newsroom"]["name"], "First")
            self.assertEqual(len(list((home / "newsroom").glob("*.previous"))), 1)

            events = [json.loads(line) for line in (home / "newsroom/audit/config-events.jsonl").read_text().splitlines()]
            event = events[-1]
            self.assertEqual(event["action"], "apply")
            self.assertEqual(event["kind"], "newsroom")
            self.assertEqual(event["revision"], 2)
            self.assertRegex(event["old_sha256"], r"^[0-9a-f]{64}$")
            self.assertRegex(event["new_sha256"], r"^[0-9a-f]{64}$")
            self.assertNotIn("document", event)
            self.assertNotIn("Second", json.dumps(event))
            self.assertEqual(event["new_sha256"], hashlib.sha256(stored_path.read_bytes()).hexdigest())

    def test_undo_restores_backup_as_new_revision_and_records_event(self):
        with tempfile.TemporaryDirectory() as temporary:
            home = Path(temporary) / "home"
            self.init_home(home)
            original = newsroom(revision=12, newsroom={**newsroom()["newsroom"], "name": "Changed"})
            path = self.write_json(temporary, "change.json", original)
            self.run_cli("apply", "--home", home, "--kind", "newsroom", "--input", path)
            result, payload = self.run_cli("undo", "--home", home, "--kind", "newsroom")
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertEqual(payload["revision"], 2)
            restored = json.loads((home / "newsroom/newsroom.json").read_text())
            self.assertEqual(restored["revision"], 2)
            self.assertEqual(restored["newsroom"]["name"], "Unnamed newsroom")
            event = json.loads((home / "newsroom/audit/config-events.jsonl").read_text().splitlines()[-1])
            self.assertEqual(event["action"], "undo")
            self.assertEqual(event["revision"], 2)

    def test_undo_without_backup_fails_without_modifying_document(self):
        with tempfile.TemporaryDirectory() as temporary:
            self.init_home(temporary)
            target = Path(temporary) / "newsroom/sources.json"
            before = target.read_bytes()
            result, payload = self.run_cli("undo", "--home", temporary, "--kind", "sources")
            self.assertNotEqual(result.returncode, 0)
            self.assertFalse(payload["ok"])
            self.assertEqual(target.read_bytes(), before)

    def test_invalid_kind_and_input_directory_are_rejected(self):
        with tempfile.TemporaryDirectory() as temporary:
            result, payload = self.run_cli("undo", "--home", temporary, "--kind", "../other")
            self.assertNotEqual(result.returncode, 0)
            self.assertFalse(payload["ok"])
            result, payload = self.run_cli("validate", "--kind", "newsroom", "--input", temporary)
            self.assertNotEqual(result.returncode, 0)
            self.assertFalse(payload["ok"])

    def test_apply_rejects_invalid_existing_document_instead_of_backing_it_up(self):
        with tempfile.TemporaryDirectory() as temporary:
            home = Path(temporary) / "home"
            self.init_home(home)
            target = home / "newsroom/newsroom.json"
            target.write_text("{}")
            candidate = self.write_json(temporary, "candidate.json", newsroom())
            result, _ = self.run_cli("apply", "--home", home, "--kind", "newsroom", "--input", candidate)
            self.assertNotEqual(result.returncode, 0)
            self.assertFalse((home / "newsroom/newsroom.json.previous").exists())

    def test_apply_reports_unpaired_surrogate_as_json_without_partial_write_or_audit(self):
        with tempfile.TemporaryDirectory() as temporary:
            home = Path(temporary) / "home"
            self.init_home(home)
            target = home / "newsroom/newsroom.json"
            audit = home / "newsroom/audit/config-events.jsonl"
            before_target = target.read_bytes()
            before_audit = audit.read_bytes()
            candidate = newsroom(
                newsroom={**newsroom()["newsroom"], "name": "\ud800"},
            )
            path = self.write_json(temporary, "unpaired-surrogate.json", candidate)

            result, payload = self.run_cli(
                "apply", "--home", home, "--kind", "newsroom", "--input", path
            )

            self.assertNotEqual(result.returncode, 0)
            self.assertIsNotNone(payload, result.stdout)
            self.assertFalse(payload["ok"])
            self.assertIn("Unicode", payload["error"])
            self.assertNotIn("Traceback", result.stderr)
            self.assertEqual(target.read_bytes(), before_target)
            self.assertEqual(audit.read_bytes(), before_audit)
            self.assertFalse((home / "newsroom/newsroom.json.previous").exists())

    def test_concurrent_applies_are_serialized_by_interprocess_lock(self):
        with tempfile.TemporaryDirectory() as temporary:
            home = Path(temporary) / "home"
            self.init_home(home)
            paths = [
                self.write_json(
                    temporary,
                    f"candidate-{index}.json",
                    newsroom(newsroom={**newsroom()["newsroom"], "name": f"Candidate {index}"}),
                )
                for index in range(2)
            ]
            processes = [
                subprocess.Popen(
                    [
                        sys.executable,
                        str(SCRIPT),
                        "apply",
                        "--home",
                        str(home),
                        "--kind",
                        "newsroom",
                        "--input",
                        str(path),
                    ],
                    text=True,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.PIPE,
                )
                for path in paths
            ]
            results = [process.communicate(timeout=10) for process in processes]
            for process, (stdout, stderr) in zip(processes, results):
                self.assertEqual(process.returncode, 0, stderr)
                self.assertTrue(json.loads(stdout)["ok"])
            stored = json.loads((home / "newsroom/newsroom.json").read_text())
            self.assertEqual(stored["revision"], 2)
            events = (home / "newsroom/audit/config-events.jsonl").read_text().splitlines()
            self.assertEqual(len(events), 2)

    def test_negative_revision_is_rejected(self):
        with tempfile.TemporaryDirectory() as temporary:
            path = self.write_json(temporary, "negative.json", newsroom(revision=-1))
            result, payload = self.run_cli("validate", "--kind", "newsroom", "--input", path)
            self.assertNotEqual(result.returncode, 0)
            self.assertIn("nonnegative", payload["error"])

    def test_symlinks_at_persistence_boundaries_are_rejected(self):
        boundaries = ("root", "target", "backup", "audit_dir", "audit_file", "lock", "journal")
        for boundary in boundaries:
            with self.subTest(boundary=boundary), tempfile.TemporaryDirectory() as temporary:
                base = Path(temporary)
                home, outside = base / "home", base / "outside"
                outside.mkdir()
                candidate = self.write_json(base, "candidate.json", newsroom())
                if boundary == "root":
                    home.mkdir()
                    (home / "newsroom").symlink_to(outside, target_is_directory=True)
                    command = ("init", "--home", home)
                else:
                    self.init_home(home)
                    root = home / "newsroom"
                    if boundary == "target":
                        (root / "newsroom.json").unlink()
                        (root / "newsroom.json").symlink_to(outside / "target")
                    elif boundary == "backup":
                        (root / "newsroom.json.previous").symlink_to(outside / "backup")
                    elif boundary == "audit_dir":
                        (root / "audit/config-events.jsonl").unlink()
                        (root / "audit").rmdir()
                        (root / "audit").symlink_to(outside, target_is_directory=True)
                    elif boundary == "audit_file":
                        (root / "audit/config-events.jsonl").unlink()
                        (root / "audit/config-events.jsonl").symlink_to(outside / "audit")
                    elif boundary == "lock":
                        (root / ".config.lock").unlink()
                        (root / ".config.lock").symlink_to(outside / "lock")
                    else:
                        (root / ".config-transaction.json").symlink_to(outside / "journal")
                    command = ("apply", "--home", home, "--kind", "newsroom", "--input", candidate)
                result, payload = self.run_cli(*command)
                self.assertNotEqual(result.returncode, 0, (boundary, result.stdout))
                self.assertFalse(payload["ok"])
                self.assertEqual(list(outside.iterdir()), [])

    def test_process_death_releases_flock_without_deleting_lock_file(self):
        with tempfile.TemporaryDirectory() as temporary:
            home = Path(temporary) / "home"
            self.init_home(home)
            candidate = self.write_json(temporary, "candidate.json", newsroom())
            env = os.environ.copy()
            env["NEWSROOM_CONFIG_TEST_HOLD_LOCK"] = "1"
            process = subprocess.Popen(
                [sys.executable, str(SCRIPT), "apply", "--home", str(home),
                 "--kind", "newsroom", "--input", str(candidate)],
                stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, env=env,
            )
            lock = home / "newsroom/.config.lock"
            import time
            for _ in range(100):
                if lock.exists():
                    break
                time.sleep(0.01)
            self.assertTrue(lock.is_file())
            process.send_signal(signal.SIGKILL)
            process.communicate(timeout=5)
            result, payload = self.run_cli(
                "apply", "--home", home, "--kind", "newsroom", "--input", candidate
            )
            self.assertEqual(result.returncode, 0, result.stderr or result.stdout)
            self.assertTrue(payload["ok"])
            self.assertTrue(lock.is_file())

    def test_operation_failpoints_leave_recoverable_state_at_each_stage(self):
        failpoints = (
            "journal_write_fsync", "backup_replace", "target_replace",
            "audit_replace", "journal_unlink",
        )
        for failpoint in failpoints:
            with self.subTest(failpoint=failpoint), tempfile.TemporaryDirectory() as temporary:
                home = Path(temporary) / "home"
                self.init_home(home)
                candidate = self.write_json(
                    temporary, "candidate.json",
                    newsroom(newsroom={**newsroom()["newsroom"], "name": failpoint}),
                )
                env = os.environ.copy()
                env["NEWSROOM_CONFIG_FAILPOINT"] = failpoint
                failed, payload = self.run_cli(
                    "apply", "--home", home, "--kind", "newsroom", "--input", candidate, env=env
                )
                self.assertNotEqual(failed.returncode, 0)
                self.assertFalse(payload["ok"])
                second = self.write_json(temporary, "second.json", newsroom())
                result, payload = self.run_cli(
                    "apply", "--home", home, "--kind", "newsroom", "--input", second
                )
                self.assertEqual(result.returncode, 0, result.stderr or result.stdout)
                expected_revision = 1 if failpoint == "journal_write_fsync" else 2
                self.assertEqual(payload["revision"], expected_revision)
                events = [json.loads(line) for line in
                          (home / "newsroom/audit/config-events.jsonl").read_text().splitlines()]
                self.assertEqual(
                    [event["revision"] for event in events],
                    list(range(1, expected_revision + 1)),
                )
                self.assertEqual(len({event["transaction_id"] for event in events}), len(events))
                self.assertFalse((home / "newsroom/.config-transaction.json").exists())

    def test_hard_links_at_mutable_persistence_boundaries_are_rejected(self):
        boundaries = ("target", "sources", "backup", "audit", "lock", "journal")
        for boundary in boundaries:
            with self.subTest(boundary=boundary), tempfile.TemporaryDirectory() as temporary:
                base = Path(temporary)
                home = base / "home"
                self.init_home(home)
                root = home / "newsroom"
                outside = base / "outside"
                outside.write_bytes(b"OUTSIDE MUST NOT CHANGE\n")
                entries = {
                    "target": root / "newsroom.json", "sources": root / "sources.json",
                    "backup": root / "newsroom.json.previous",
                    "audit": root / "audit/config-events.jsonl", "lock": root / ".config.lock",
                    "journal": root / ".config-transaction.json",
                }
                entry = entries[boundary]
                entry.unlink(missing_ok=True)
                os.link(outside, entry)
                document_kind = "sources" if boundary == "sources" else "newsroom"
                candidate = self.write_json(
                    base, "candidate.json", sources() if document_kind == "sources" else newsroom()
                )
                result, payload = self.run_cli(
                    "apply", "--home", home, "--kind", document_kind, "--input", candidate
                )
                self.assertNotEqual(result.returncode, 0, (boundary, result.stdout))
                self.assertIsNotNone(payload, result.stdout)
                self.assertFalse(payload["ok"])
                self.assertEqual(outside.read_bytes(), b"OUTSIDE MUST NOT CHANGE\n")

    def test_special_persistence_entries_fail_as_json_without_hanging(self):
        boundaries = ("target", "sources", "backup", "audit", "lock", "journal")
        for boundary in boundaries:
            with self.subTest(boundary=boundary), tempfile.TemporaryDirectory() as temporary:
                base = Path(temporary)
                home = base / "home"
                self.init_home(home)
                root = home / "newsroom"
                entries = {
                    "target": root / "newsroom.json", "sources": root / "sources.json",
                    "backup": root / "newsroom.json.previous",
                    "audit": root / "audit/config-events.jsonl", "lock": root / ".config.lock",
                    "journal": root / ".config-transaction.json",
                }
                entry = entries[boundary]
                entry.unlink(missing_ok=True)
                os.mkfifo(entry)
                document_kind = "sources" if boundary == "sources" else "newsroom"
                candidate = self.write_json(
                    base, "candidate.json", sources() if document_kind == "sources" else newsroom()
                )
                command = [sys.executable, str(SCRIPT), "apply", "--home", str(home),
                           "--kind", document_kind, "--input", str(candidate)]
                result = subprocess.run(command, text=True, capture_output=True, timeout=2)
                self.assertNotEqual(result.returncode, 0, boundary)
                payload = json.loads(result.stdout)
                self.assertFalse(payload["ok"])
                self.assertNotIn("Traceback", result.stderr)

    def test_final_directory_fsync_failure_is_not_reported_as_success(self):
        with tempfile.TemporaryDirectory() as temporary:
            home = Path(temporary) / "home"
            self.init_home(home)
            candidate = self.write_json(temporary, "candidate.json", newsroom())
            env = os.environ.copy()
            env["NEWSROOM_CONFIG_FAILPOINT"] = "final_directory_fsync"
            result, payload = self.run_cli(
                "apply", "--home", home, "--kind", "newsroom", "--input", candidate, env=env
            )
            self.assertNotEqual(result.returncode, 0)
            self.assertFalse(payload["ok"])
            self.assertFalse((home / "newsroom/.config-transaction.json").exists())
            self.assertEqual(json.loads((home / "newsroom/newsroom.json").read_text())["revision"], 1)

    def test_status_recovers_a_valid_pending_transaction_before_reporting(self):
        with tempfile.TemporaryDirectory() as temporary:
            home = Path(temporary) / "home"
            self.init_home(home)
            candidate = self.write_json(temporary, "candidate.json", newsroom())
            env = os.environ.copy()
            env["NEWSROOM_CONFIG_FAILPOINT"] = "target_replace"
            failed, _ = self.run_cli(
                "apply", "--home", home, "--kind", "newsroom", "--input", candidate, env=env
            )
            self.assertNotEqual(failed.returncode, 0)
            result, payload = self.run_cli("status", "--home", home)
            self.assertEqual(result.returncode, 0, result.stdout)
            self.assertEqual(payload["newsroom"]["revision"], 1)
            self.assertFalse((home / "newsroom/.config-transaction.json").exists())

    def test_stale_valid_journal_cannot_roll_back_newer_configuration(self):
        with tempfile.TemporaryDirectory() as temporary:
            home = Path(temporary) / "home"
            self.init_home(home)
            root = home / "newsroom"
            target = root / "newsroom.json"
            backup = root / "newsroom.json.previous"
            audit = root / "audit/config-events.jsonl"
            journal = root / ".config-transaction.json"

            first = self.write_json(
                temporary, "first.json",
                newsroom(newsroom={**newsroom()["newsroom"], "name": "First"}),
            )
            env = os.environ.copy()
            env["NEWSROOM_CONFIG_FAILPOINT"] = "journal_unlink"
            failed, _ = self.run_cli(
                "apply", "--home", home, "--kind", "newsroom", "--input", first, env=env
            )
            self.assertNotEqual(failed.returncode, 0)
            stale_journal = journal.read_bytes()

            recovered, _ = self.run_cli("status", "--home", home)
            self.assertEqual(recovered.returncode, 0, recovered.stdout)
            second = self.write_json(
                temporary, "second.json",
                newsroom(newsroom={**newsroom()["newsroom"], "name": "Second"}),
            )
            applied, _ = self.run_cli(
                "apply", "--home", home, "--kind", "newsroom", "--input", second
            )
            self.assertEqual(applied.returncode, 0, applied.stdout)
            before = (target.read_bytes(), backup.read_bytes(), audit.read_bytes())
            self.assertEqual(json.loads(before[0])["revision"], 2)

            journal.write_bytes(stale_journal)
            result, payload = self.run_cli("status", "--home", home)

            self.assertNotEqual(result.returncode, 0, result.stdout)
            self.assertFalse(payload["ok"])
            self.assertEqual(payload["error"], "transaction journal does not match current target")
            self.assertEqual((target.read_bytes(), backup.read_bytes(), audit.read_bytes()), before)
            self.assertEqual(journal.read_bytes(), stale_journal)

    def test_journal_recovery_rejects_unknown_journal_and_event_fields(self):
        mutations = {
            "journal-extra": lambda tx: tx.update(document="MUST-NOT-RECOVER"),
            "journal-secret": lambda tx: tx.update(token="MUST-NOT-RECOVER"),
            "event-extra": lambda tx: tx["event"].update(document="MUST-NOT-AUDIT"),
            "event-secret": lambda tx: tx["event"].update(token="MUST-NOT-AUDIT"),
        }
        for name, mutate in mutations.items():
            with self.subTest(name=name), tempfile.TemporaryDirectory() as temporary:
                home = Path(temporary) / "home"
                self.init_home(home)
                root = home / "newsroom"
                target = root / "newsroom.json"
                audit = root / "audit/config-events.jsonl"
                journal = root / ".config-transaction.json"
                old = target.read_bytes()
                new = CONFIG_MODULE.json_bytes(newsroom(revision=1))
                transaction_id = "unknown-field-transaction"
                event = CONFIG_MODULE.make_event("apply", "newsroom", 1, old, new)
                event["transaction_id"] = transaction_id
                transaction = {
                    "version": 1, "transaction_id": transaction_id, "kind": "newsroom",
                    "old": base64.b64encode(old).decode(),
                    "new": base64.b64encode(new).decode(), "event": event,
                }
                mutate(transaction)
                before_audit = audit.read_bytes()
                journal.write_bytes(CONFIG_MODULE.json_bytes(transaction))

                result, payload = self.run_cli("status", "--home", home)

                self.assertNotEqual(result.returncode, 0, result.stdout)
                self.assertFalse(payload["ok"])
                self.assertIn("transaction journal", payload["error"])
                self.assertEqual(target.read_bytes(), old)
                self.assertEqual(audit.read_bytes(), before_audit)
                self.assertTrue(journal.exists())

    def test_corrupt_journals_are_refused_without_overwriting_configuration(self):
        mutations = {
            "version": lambda tx: tx.update(version=2),
            "action": lambda tx: tx["event"].update(action="delete"),
            "event-kind": lambda tx: tx["event"].update(kind="sources"),
            "event-revision": lambda tx: tx["event"].update(revision=99),
            "old-hash": lambda tx: tx["event"].update(old_sha256="0" * 64),
            "new-hash": lambda tx: tx["event"].update(new_sha256="0" * 64),
            "bad-old-schema": lambda tx: tx.update(old=base64.b64encode(b"{}").decode()),
            "bad-new-schema": lambda tx: tx.update(new=base64.b64encode(b"{}").decode()),
            "revision-gap": lambda tx: tx.update(new=base64.b64encode(
                CONFIG_MODULE.json_bytes(newsroom(revision=8))).decode()),
        }
        for name, mutate in mutations.items():
            with self.subTest(name=name), tempfile.TemporaryDirectory() as temporary:
                home = Path(temporary) / "home"
                self.init_home(home)
                root = home / "newsroom"
                target = root / "newsroom.json"
                before = target.read_bytes()
                old = before
                new = CONFIG_MODULE.json_bytes(newsroom(revision=1))
                transaction_id = "test-transaction"
                event = CONFIG_MODULE.make_event("apply", "newsroom", 1, old, new)
                event["transaction_id"] = transaction_id
                transaction = {
                    "version": 1, "transaction_id": transaction_id, "kind": "newsroom",
                    "old": base64.b64encode(old).decode(),
                    "new": base64.b64encode(new).decode(), "event": event,
                }
                mutate(transaction)
                (root / ".config-transaction.json").write_bytes(CONFIG_MODULE.json_bytes(transaction))
                result, payload = self.run_cli("status", "--home", home)
                self.assertNotEqual(result.returncode, 0, result.stdout)
                self.assertFalse(payload["ok"])
                self.assertIn("transaction journal", payload["error"])
                self.assertEqual(target.read_bytes(), before)

    def test_write_all_retries_short_audit_writes(self):
        read_fd, write_fd = os.pipe()
        real_write = os.write
        try:
            with mock.patch.object(
                CONFIG_MODULE.os, "write",
                side_effect=lambda descriptor, data: real_write(descriptor, bytes(data[:3])),
            ) as patched:
                CONFIG_MODULE._write_all(write_fd, b"complete audit record")
            os.close(write_fd)
            write_fd = -1
            self.assertEqual(os.read(read_fd, 100), b"complete audit record")
            self.assertGreater(patched.call_count, 1)
        finally:
            os.close(read_fd)
            if write_fd >= 0:
                os.close(write_fd)

    def test_unexpected_public_errors_are_sanitized(self):
        with tempfile.TemporaryDirectory() as temporary:
            home = Path(temporary) / "private-home"
            home.write_text("not a directory")
            result, payload = self.run_cli("status", "--home", home)
            self.assertNotEqual(result.returncode, 0)
            self.assertEqual(payload["error"], "internal configuration error")
            self.assertNotIn("private-home", result.stdout)


if __name__ == "__main__":
    unittest.main()
