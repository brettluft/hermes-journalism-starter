import hashlib
import hmac
import importlib.util
import json
import math
import os
from pathlib import Path
import stat
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from unittest import mock


ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = ROOT / "skills/draft-publishing/scripts"
SCRIPT = SCRIPTS / "draft_publish.py"


def load_module():
    sys.path.insert(0, str(SCRIPTS))
    try:
        spec = importlib.util.spec_from_file_location("draft_publish_under_test", SCRIPT)
        if spec is None or spec.loader is None:
            raise ImportError("draft publish module is absent")
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        return module
    finally:
        sys.path.pop(0)


class DraftPublishTests(unittest.TestCase):
    def setUp(self):
        self.module = load_module()
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        base = Path(self.temporary.name)
        self.key_path = base / "draft-library/hmac.key"
        self.config_path = base / "newsroom.json"
        self.draft_root = base / "drafts"
        self.config_path.write_text(json.dumps(self.valid_newsroom()))
        self.publisher = self.module.DraftPublisher(
            draft_root=self.draft_root, key_path=self.key_path,
            config_path=self.config_path, clock=lambda: 2_000_000_000,
            environ={"DRAFT_LIBRARY_BASE_URL": "https://drafts.example.test"})

    @staticmethod
    def valid_newsroom():
        return {
            "schema_version": 1, "revision": 0, "setup_status": "draft",
            "newsroom": {"name": "Test newsroom", "timezone": "Etc/UTC",
                         "report_languages": ["en"],
                         "editorial_style": {
                             "guide": "Canadian Press",
                             "house_rules": ["Use the outlet's local dateline format."],
                             "confirmed_by_editor": True,
                         }},
            "coverage": {"places": [], "public_bodies": [], "beats": []},
            "drafts": {"destination": "library", "publishing_policy": "ask_each_time",
                       "spacefast_setup_status": "not_configured"},
            "preferences": {"preserve_original_language": True,
                            "require_primary_source_citations": True,
                            "allow_unofficial_fallbacks": False},
        }

    def test_editorial_style_preflight_blocks_save_link_and_publish(self):
        source = Path(self.temporary.name) / "source.md"
        source.write_text("Draft copy", encoding="utf-8")
        source.chmod(0o600)
        self.publisher.store.save("one", "title", "source")
        self.publisher.initialize()

        for style in (
            None,
            {"guide": "undetermined", "house_rules": [],
             "confirmed_by_editor": False},
        ):
            document = self.valid_newsroom()
            if style is None:
                del document["newsroom"]["editorial_style"]
            else:
                document["newsroom"]["editorial_style"] = style
            self.config_path.write_text(json.dumps(document))
            for operation in (
                lambda: self.publisher.save_file("blocked", "title", source),
                lambda: self.publisher.link("one", 60, editor_approved=True),
                lambda: self.publisher.publish("one", editor_approved=True),
            ):
                with self.subTest(style=style, operation=operation), self.assertRaisesRegex(
                    self.module.PublishError, "^editorial_style_unconfirmed$"
                ):
                    operation()
            self.assertFalse(self.draft_root.joinpath("blocked").exists())

    def test_publish_fails_fast_if_style_is_revoked_during_authorization(self):
        self.publisher.store.save("one", "title", "source")
        self.publisher.initialize()
        factory = mock.Mock()
        self.publisher._spacefast_factory = factory

        for destination in ("both", "spacefast"):
            config = {
                "destination": destination,
                "publishing_policy": "ask_each_time",
                "spacefast_setup_status": "configured",
            }
            revoked = self.module.PublishError("editorial_style_unconfirmed")
            with self.subTest(destination=destination), mock.patch.object(
                self.publisher, "load_draft_config", side_effect=[config, revoked]
            ):
                with self.assertRaisesRegex(
                    self.module.PublishError, "^editorial_style_unconfirmed$"
                ):
                    self.publisher.publish("one", editor_approved=True)
            factory.assert_not_called()

    def test_production_paths_are_fixed_and_parser_has_no_relocation_or_secret_options(self):
        self.assertEqual(self.module.PRODUCTION_KEY_PATH,
                         Path("/opt/data/newsroom/draft-library/hmac.key"))
        self.assertEqual(self.module.PRODUCTION_CONFIG_PATH, Path("/opt/data/newsroom/newsroom.json"))
        parser = self.module.build_parser()
        help_text = parser.format_help() + " ".join(
            action.dest for action in parser._actions)
        for forbidden in ("--home", "--root", "--output", "--key", "--token", "--base-url"):
            self.assertNotIn(forbidden, help_text)

    def test_import_status_save_and_link_do_not_create_key_or_key_parent(self):
        self.assertFalse(self.key_path.parent.exists())
        status = self.publisher.status()
        self.assertFalse(status["library_initialized"])
        self.publisher.store.save("one", "title", "source")
        with self.assertRaises(self.module.PublishError):
            self.publisher.link("one", 60, editor_approved=True)
        self.assertFalse(self.key_path.parent.exists())

    def test_explicit_init_only_creates_secure_random_key_and_repeat_preserves(self):
        random_calls = []
        def random_bytes(size):
            random_calls.append(size)
            return bytes(range(size))
        result = self.publisher.initialize(random_bytes=random_bytes)
        self.assertEqual(result, {"initialized": True})
        original = self.key_path.read_bytes()
        self.assertEqual(len(original), self.module.KEY_BYTES)
        self.assertEqual(stat.S_IMODE(self.key_path.stat().st_mode), 0o600)
        self.assertEqual(stat.S_IMODE(self.key_path.parent.stat().st_mode), 0o700)
        result = self.publisher.initialize(random_bytes=lambda _: b"z" * self.module.KEY_BYTES)
        self.assertEqual(result, {"initialized": False})
        self.assertEqual(self.key_path.read_bytes(), original)
        self.assertEqual(random_calls, [self.module.KEY_BYTES])

    def test_init_and_reads_reject_linked_permissive_malformed_key(self):
        self.publisher.store.save("one", "title", "source")
        self.key_path.parent.mkdir(mode=0o700)
        outside = Path(self.temporary.name) / "outside-key"
        outside.write_bytes(b"x" * self.module.KEY_BYTES)
        self.key_path.symlink_to(outside)
        with self.assertRaises(self.module.PublishError):
            self.publisher.initialize()
        self.key_path.unlink()
        self.key_path.write_bytes(b"x" * self.module.KEY_BYTES)
        self.key_path.chmod(0o644)
        with self.assertRaises(self.module.PublishError):
            self.publisher.status()
        self.key_path.chmod(0o600)
        self.key_path.write_bytes(b"short")
        with self.assertRaises(self.module.PublishError):
            self.publisher.link("one", 60, editor_approved=True)
        self.key_path.unlink()
        os.link(outside, self.key_path)
        self.key_path.chmod(0o600)
        with self.assertRaises(self.module.PublishError):
            self.publisher.status()

    def test_signing_is_deterministic_canonical_and_key_is_reread(self):
        self.publisher.store.save("story-1", "title", "source")
        self.publisher.initialize(random_bytes=lambda _: b"a" * self.module.KEY_BYTES)
        link = self.publisher.link("story-1", 90, editor_approved=True)
        expires = 2_000_000_090
        payload = b"v1\nGET\n/draft/story-1\n2000000090"
        signature = hmac.new(b"a" * self.module.KEY_BYTES, payload, hashlib.sha256).hexdigest()
        self.assertEqual(link["url"],
                         f"https://drafts.example.test/draft/story-1?expires={expires}&sig={signature}")
        self.key_path.write_bytes(b"b" * self.module.KEY_BYTES)
        rotated = self.publisher.link("story-1", 90, editor_approved=True)
        self.assertNotEqual(rotated["url"], link["url"])
        self.assertRegex(rotated["url"].split("sig=")[1], r"^[0-9a-f]{64}$")

    def test_ttl_bounds(self):
        self.publisher.store.save("one", "title", "source")
        self.publisher.initialize()
        for ttl in (0, -1, 86401, True, "60"):
            with self.subTest(ttl=ttl), self.assertRaises(self.module.PublishError):
                self.publisher.link("one", ttl, editor_approved=True)
        self.publisher.link("one", 1, editor_approved=True)
        self.publisher.link("one", 86400, editor_approved=True)

    def test_base_url_precedence_and_validation(self):
        explicit = self.module.resolve_base_url({
            "DRAFT_LIBRARY_BASE_URL": "https://drafts.example.test",
            "RAILWAY_PUBLIC_DOMAIN": "fallback.example.test"})
        self.assertEqual(explicit, "https://drafts.example.test")
        self.assertEqual(
            self.module.resolve_base_url(
                {"DRAFT_LIBRARY_BASE_URL": "https://Drafts.Example.Test.:443/"}),
            "https://drafts.example.test:443",
        )
        self.assertEqual(self.module.resolve_base_url({"RAILWAY_PUBLIC_DOMAIN": "app.example.test"}),
                         "https://app.example.test")
        invalid_explicit = ("http://x.test", "https://user@x.test", "https://x.test/path",
                            "https://x.test?x=1", "https://x.test/#x", "https://localhost",
                            "https://127.0.0.1", " https://x.test")
        for value in invalid_explicit:
            with self.subTest(value=value), self.assertRaises(self.module.PublishError):
                self.module.resolve_base_url({"DRAFT_LIBRARY_BASE_URL": value})
        invalid_domains = ("localhost", "127.0.0.1", "https://x.test", "x.test:443",
                           "x.test/path", "*.x.test", "x test", "x")
        for value in invalid_domains:
            with self.subTest(value=value), self.assertRaises(self.module.PublishError):
                self.module.resolve_base_url({"RAILWAY_PUBLIC_DOMAIN": value})

    def test_clock_must_be_finite_numeric_and_in_timestamp_range(self):
        self.publisher.store.save("one", "title", "source")
        self.publisher.initialize()
        invalid = (None, "2000000000", True, math.nan, math.inf, -math.inf,
                   -1, -0.1, -0.999999, 2**63)
        for value in invalid:
            with self.subTest(value=value):
                publisher = self.module.DraftPublisher(
                    draft_root=self.draft_root, key_path=self.key_path,
                    config_path=self.config_path, clock=lambda value=value: value,
                    environ={"DRAFT_LIBRARY_BASE_URL": "https://drafts.example.test"})
                with self.assertRaisesRegex(self.module.PublishError, "^invalid_clock$"):
                    publisher.link("one", 60, editor_approved=True)

        def broken_clock():
            raise RuntimeError("private clock failure /secret/path")

        self.publisher.clock = broken_clock
        with self.assertRaisesRegex(self.module.PublishError, "^invalid_clock$"):
            self.publisher.link("one", 60, editor_approved=True)

    def test_policy_matrix_for_link(self):
        self.publisher.store.save("one", "title", "source")
        self.publisher.initialize()
        for policy, approved, allowed in (
            ("never", False, False), ("never", True, False),
            ("ask_each_time", False, False), ("ask_each_time", True, True),
            ("auto_private", False, True), ("auto_private", True, True)):
            document = json.loads(self.config_path.read_text())
            document["drafts"]["publishing_policy"] = policy
            self.config_path.write_text(json.dumps(document))
            with self.subTest(policy=policy, approved=approved):
                if allowed:
                    self.publisher.link("one", 60, editor_approved=approved)
                else:
                    with self.assertRaises(self.module.PublishError):
                        self.publisher.link("one", 60, editor_approved=approved)

    def test_link_authorizes_destination_and_validated_snapshot_before_signing(self):
        self.publisher.initialize()
        document = self.valid_newsroom()
        document["drafts"].update(destination="spacefast", publishing_policy="auto_private",
                                  spacefast_setup_status="configured")
        self.config_path.write_text(json.dumps(document))
        with mock.patch.object(self.module, "_read_key") as read_key:
            with self.assertRaisesRegex(self.module.PublishError, "^library_destination_disabled$"):
                self.publisher.link("missing", 60)
            read_key.assert_not_called()

        for destination in ("library", "both"):
            document["drafts"]["destination"] = destination
            self.config_path.write_text(json.dumps(document))
            with mock.patch.object(self.module, "_read_key") as read_key:
                with self.assertRaisesRegex(self.module.DraftError, "^unsafe_storage$"):
                    self.publisher.link("missing", 60)
                read_key.assert_not_called()

        self.publisher.store.save("one", "title", "source")
        for destination in ("library", "both"):
            document["drafts"]["destination"] = destination
            self.config_path.write_text(json.dumps(document))
            self.assertIn("url", self.publisher.link("one", 60))

    def test_publish_policy_never_and_spacefast_placeholder(self):
        self.publisher.store.save("one", "title", "source")
        self.publisher.initialize()
        document = json.loads(self.config_path.read_text())
        document["drafts"].update(destination="both", publishing_policy="auto_private",
                                  spacefast_setup_status="configured")
        self.config_path.write_text(json.dumps(document))
        result = self.publisher.publish("one", editor_approved=False)
        self.assertEqual(result["library"]["status"], "published")
        self.assertEqual(result["spacefast"]["status"], "approval_required")
        approved = self.publisher.publish("one", editor_approved=True)
        self.assertEqual(approved["spacefast"]["status"], "spacefast_credentials_missing")
        document["drafts"]["publishing_policy"] = "never"
        self.config_path.write_text(json.dumps(document))
        with self.assertRaises(self.module.PublishError):
            self.publisher.publish("one", editor_approved=True)

    def test_spacefast_create_then_update_and_partial_results(self):
        self.publisher.store.save("one", "Title", "ONLY RENDITION LEAVES")
        self.publisher.initialize()
        document = self.valid_newsroom()
        document["drafts"].update(destination="both", publishing_policy="ask_each_time",
                                  spacefast_setup_status="configured")
        self.config_path.write_text(json.dumps(document))
        calls = []

        class Client:
            def publish(inner, title, rendition, digest, *, draft_id, space_id=None):
                calls.append((title, rendition, digest, draft_id, space_id))
                return {"space_id": "spc_0123456789abcdef",
                        "live_url": "https://news.spacefast.app/",
                        "immutable_url": "https://version.spacefast.app/"}

        self.publisher._spacefast_factory = lambda environ: Client()
        first = self.publisher.publish("one", editor_approved=True)
        self.assertEqual(first["library"]["status"], "published")
        self.assertEqual(first["spacefast"]["status"], "published")
        self.assertIsNone(calls[0][4])
        self.publisher.publish("one", editor_approved=True)
        self.assertEqual(calls[1][4], "spc_0123456789abcdef")
        metadata = json.loads((self.draft_root / "one/publication.json").read_text())
        self.assertEqual(metadata["space_id"], "spc_0123456789abcdef")

        class Failure:
            def publish(inner, *_args, **_kwargs):
                raise self.module.SpacefastError("spacefast_timeout")

        self.publisher._spacefast_factory = lambda environ: Failure()
        before = {name: (self.draft_root / "one" / name).read_bytes()
                  for name in ("source.txt", "rendition.html", "publication.json")}
        partial = self.publisher.publish("one", editor_approved=True)
        self.assertEqual(partial["library"]["status"], "published")
        self.assertEqual(partial["spacefast"]["status"], "spacefast_timeout")
        self.assertEqual({name: (self.draft_root / "one" / name).read_bytes() for name in before}, before)

    def test_simultaneous_first_publishes_are_one_create_then_one_update(self):
        self.publisher.store.save("one", "Title", "RENDITION")
        document = self.valid_newsroom()
        document["drafts"].update(destination="spacefast", publishing_policy="ask_each_time",
                                  spacefast_setup_status="configured")
        self.config_path.write_text(json.dumps(document))
        clock_values = iter((2_000_000_000, 2_000_000_001))
        self.publisher.clock = lambda: next(clock_values)
        first_remote_entered = threading.Event()
        release_first_remote = threading.Event()
        calls = []
        calls_lock = threading.Lock()

        class Client:
            def publish(inner, title, rendition, digest, *, draft_id, space_id=None):
                del title, rendition, digest, draft_id
                with calls_lock:
                    calls.append(space_id)
                    call_number = len(calls)
                if call_number == 1:
                    first_remote_entered.set()
                    self.assertTrue(release_first_remote.wait(5))
                return {"space_id": "spc_0123456789abcdef",
                        "live_url": "https://news.spacefast.app/",
                        "immutable_url": "https://version.spacefast.app/"}

        self.publisher._spacefast_factory = lambda _environ: Client()
        barrier = threading.Barrier(3)
        results = []
        errors = []

        def worker():
            try:
                barrier.wait()
                results.append(self.publisher.publish("one", editor_approved=True))
            except Exception as error:
                errors.append(error)

        threads = [threading.Thread(target=worker), threading.Thread(target=worker)]
        for thread in threads:
            thread.start()
        barrier.wait()
        self.assertTrue(first_remote_entered.wait(5))
        time.sleep(0.2)
        self.assertEqual(calls, [None], "second transport entered instead of blocking on draft lock")
        release_first_remote.set()
        for thread in threads:
            thread.join(5)
        self.assertEqual(errors, [])
        self.assertEqual(calls, [None, "spc_0123456789abcdef"])
        self.assertEqual(sorted(item["spacefast"]["status"] for item in results),
                         ["published", "published"])
        metadata = json.loads((self.draft_root / "one/publication.json").read_text())
        self.assertEqual(metadata["spacefast"]["published_at"], 2_000_000_001)
        self.assertEqual(metadata["space_id"], "spc_0123456789abcdef")

    def test_concurrent_save_cannot_change_outbound_rendition_before_commit(self):
        self.publisher.store.save("one", "Old", "OLD RENDITION")
        document = self.valid_newsroom()
        document["drafts"].update(destination="spacefast", publishing_policy="ask_each_time",
                                  spacefast_setup_status="configured")
        self.config_path.write_text(json.dumps(document))
        remote_entered = threading.Event()
        release_remote = threading.Event()
        observed = []

        class Client:
            def publish(inner, title, rendition, digest, *, draft_id, space_id=None):
                del title, digest, draft_id, space_id
                observed.append(rendition)
                remote_entered.set()
                self.assertTrue(release_remote.wait(5))
                return {"space_id": "spc_0123456789abcdef",
                        "live_url": "https://news.spacefast.app/",
                        "immutable_url": "https://version.spacefast.app/"}

        self.publisher._spacefast_factory = lambda _environ: Client()
        results = []
        publish_thread = threading.Thread(
            target=lambda: results.append(self.publisher.publish("one", editor_approved=True)))
        publish_thread.start()
        self.assertTrue(remote_entered.wait(5))
        save_thread = threading.Thread(
            target=lambda: self.publisher.store.save("one", "New", "NEW RENDITION"))
        save_thread.start()
        save_thread.join(0.2)
        self.assertTrue(save_thread.is_alive())
        self.assertEqual((self.draft_root / "one/source.txt").read_text(), "OLD RENDITION")
        release_remote.set()
        publish_thread.join(5)
        save_thread.join(5)
        self.assertIn(b"OLD RENDITION", observed[0])
        self.assertEqual(results[0]["spacefast"]["status"], "published")
        metadata = json.loads((self.draft_root / "one/publication.json").read_text())
        self.assertEqual(metadata["space_id"], "spc_0123456789abcdef")
        self.assertEqual((self.draft_root / "one/source.txt").read_text(), "NEW RENDITION")

    def test_visible_post_replace_fsync_failure_reports_published_but_uncertain(self):
        self.publisher.store.save("one", "Title", "RENDITION")
        document = self.valid_newsroom()
        document["drafts"].update(destination="spacefast", publishing_policy="ask_each_time",
                                  spacefast_setup_status="configured")
        self.config_path.write_text(json.dumps(document))

        calls = []

        class Client:
            def publish(inner, *_args, space_id=None, **_kwargs):
                calls.append(space_id)
                return {"space_id": "spc_0123456789abcdef",
                        "live_url": "https://news.spacefast.app/",
                        "immutable_url": "https://version.spacefast.app/"}

        def failpoint(stage):
            if stage == "publication_directory_fsync":
                raise OSError("SECRET-FSYNC-DETAIL")

        self.publisher._spacefast_factory = lambda _environ: Client()
        self.publisher.store = self.module.DraftStore(self.draft_root, failpoint=failpoint)
        result = self.publisher.publish("one", editor_approved=True)
        self.assertEqual(result["spacefast"]["status"], "published")
        self.assertEqual(result["spacefast"]["durability"], "uncertain")
        self.assertNotIn("SECRET", json.dumps(result))
        metadata = json.loads((self.draft_root / "one/publication.json").read_text())
        self.assertEqual(metadata["space_id"], "spc_0123456789abcdef")
        self.publisher.store = self.module.DraftStore(self.draft_root)
        retry = self.publisher.publish("one", editor_approved=True)
        self.assertEqual(retry["spacefast"]["status"], "published")
        self.assertEqual(calls, [None, "spc_0123456789abcdef"])

    def test_legacy_defaults_and_strict_config_are_read_without_rewrite(self):
        legacy = self.valid_newsroom()
        del legacy["drafts"]
        self.config_path.write_text(json.dumps(legacy))
        before = self.config_path.read_bytes()
        self.assertEqual(self.publisher.load_draft_config(), {
            "destination": "library", "publishing_policy": "ask_each_time",
            "spacefast_setup_status": "not_configured"})
        self.assertEqual(self.config_path.read_bytes(), before)
        for drafts in (None, {}, {"destination": "library", "publishing_policy": "never",
                                  "spacefast_setup_status": "not_configured", "extra": "x"}):
            document = self.valid_newsroom()
            document["drafts"] = drafts
            self.config_path.write_text(json.dumps(document))
            with self.assertRaises(self.module.PublishError):
                self.publisher.load_draft_config()

    def test_complete_authoritative_newsroom_validation_precedes_link_and_publish(self):
        invalid_documents = []
        for field, value in (("revision", -1), ("setup_status", "ready")):
            document = self.valid_newsroom()
            document[field] = value
            invalid_documents.append(document)
        missing_structure = self.valid_newsroom()
        del missing_structure["coverage"]
        invalid_documents.append(missing_structure)
        credential = self.valid_newsroom()
        credential["newsroom"]["token"] = "DO-NOT-LEAK"
        invalid_documents.append(credential)

        for document in invalid_documents:
            with self.subTest(document=document):
                self.config_path.write_text(json.dumps(document))
                for operation in (
                    lambda: self.publisher.load_draft_config(),
                    lambda: self.publisher.link("one", 60, editor_approved=True),
                    lambda: self.publisher.publish("one", editor_approved=True),
                ):
                    with self.assertRaisesRegex(self.module.PublishError, "^invalid_config$"):
                        operation()
        self.config_path.write_text('{"schema_version":')
        with self.assertRaisesRegex(self.module.PublishError, "^invalid_config$"):
            self.publisher.link("one", 60, editor_approved=True)

    def test_config_file_rejects_group_or_world_writable_mode(self):
        self.config_path.chmod(0o666)
        with self.assertRaisesRegex(self.module.PublishError, "^invalid_config$"):
            self.publisher.load_draft_config()

    def test_validator_is_lazy_and_loader_failures_are_sanitized(self):
        def broken_loader():
            raise ImportError("private missing validator path")

        publisher = self.module.DraftPublisher(
            draft_root=self.draft_root, key_path=self.key_path,
            config_path=self.config_path, validator_loader=broken_loader)
        # Construction and operations unrelated to config do not import the validator.
        self.assertEqual(publisher.status(), {"library_initialized": False})
        with self.assertRaisesRegex(self.module.PublishError, "^invalid_config$"):
            publisher.load_draft_config()

        malformed_loaders = (
            lambda: object(),
            lambda: type("BadValidator", (), {
                "strict_json_loads": staticmethod(lambda _text: (_ for _ in ()).throw(
                    RuntimeError("private validator runtime failure")))})(),
        )
        for loader in malformed_loaders:
            publisher._validator_loader = loader
            with self.assertRaisesRegex(self.module.PublishError, "^invalid_config$"):
                publisher.load_draft_config()

    def test_cli_save_commit_uncertain_is_one_sanitized_json_error(self):
        publisher = mock.Mock()
        publisher.save_file.side_effect = self.module.SaveCommitUncertain()
        with mock.patch.object(self.module, "DraftPublisher", return_value=publisher), \
                mock.patch("builtins.print") as output:
            code = self.module.main([
                "save", "--id", "one", "--title", "SECRET TITLE",
                "--input", "/private/source/path",
            ])
        self.assertEqual(code, 1)
        output.assert_called_once_with(
            '{"error":"save_commit_uncertain","ok":false}')

    def test_cli_missing_bundled_validator_emits_one_sanitized_json_error(self):
        isolated = Path(self.temporary.name) / "isolated/scripts"
        isolated.mkdir(parents=True)
        for name in ("draft_publish.py", "draft_store.py", "spacefast_client.py"):
            (isolated / name).write_bytes((SCRIPTS / name).read_bytes())
        result = subprocess.run(
            [sys.executable, str(isolated / "draft_publish.py"), "link",
             "--id", "one", "--ttl-seconds", "60", "--editor-approved"],
            text=True, capture_output=True, check=False,
        )
        self.assertNotEqual(result.returncode, 0)
        self.assertEqual(result.stderr, "")
        self.assertEqual(result.stdout.count("\n"), 1)
        self.assertEqual(json.loads(result.stdout), {"error": "invalid_config", "ok": False})
        self.assertNotIn("Traceback", result.stdout + result.stderr)
        self.assertNotIn(str(isolated), result.stdout + result.stderr)

    def test_cli_always_emits_one_sanitized_json_object(self):
        source = Path(self.temporary.name) / "private-title-and-source.txt"
        source.write_text("SECRET-SOURCE")
        commands = (
            (), ("save", "--id", "BAD/ID", "--title", "SECRET-TITLE", "--input", str(source)),
            ("link", "--id", "one", "--ttl-seconds", "bad"),
            ("status", "--root", str(self.draft_root)),
        )
        for arguments in commands:
            with self.subTest(arguments=arguments):
                result = subprocess.run([sys.executable, str(SCRIPT), *arguments],
                                        text=True, capture_output=True, check=False,
                                        env={**os.environ, "SECRET_ENV_MARKER": "ENV-SECRET"})
                payload = json.loads(result.stdout)
                self.assertIsInstance(payload, dict)
                self.assertEqual(len(result.stdout.strip().splitlines()), 1)
                combined = result.stdout + result.stderr
                for secret in (str(source), "SECRET-SOURCE", "SECRET-TITLE", "ENV-SECRET",
                               "/opt/data/newsroom", "Traceback"):
                    self.assertNotIn(secret, combined)
                self.assertNotEqual(result.returncode, 0)

    def test_cli_help_is_one_json_object_for_root_and_every_command(self):
        invocations = [(flag,) for flag in ("-h", "--help")]
        invocations += [
            (command, flag)
            for command in ("init", "status", "save", "link", "publish")
            for flag in ("-h", "--help")
        ]
        for arguments in invocations:
            with self.subTest(arguments=arguments):
                result = subprocess.run(
                    [sys.executable, str(SCRIPT), *arguments],
                    text=True, capture_output=True, check=False,
                    env={**os.environ, "SECRET_ENV_MARKER": "ENV-SECRET"},
                )
                self.assertEqual(result.returncode, 0)
                self.assertEqual(result.stderr, "")
                self.assertEqual(len(result.stdout.strip().splitlines()), 1)
                payload = json.loads(result.stdout)
                self.assertIsInstance(payload, dict)
                self.assertTrue(payload["ok"])
                self.assertIn("usage", payload)
                self.assertNotIn("ENV-SECRET", result.stdout)


if __name__ == "__main__":
    unittest.main()
