import errno
import importlib.util
import json
import os
from pathlib import Path
import stat
import tempfile
import threading
import types
import unittest
from unittest import mock


ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "skills/draft-publishing/scripts/draft_store.py"


def load_module():
    spec = importlib.util.spec_from_file_location("draft_store_under_test", SCRIPT)
    if spec is None or spec.loader is None:
        raise ImportError("draft store module is absent")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class DraftStoreTests(unittest.TestCase):
    def setUp(self):
        self.module = load_module()
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name) / "drafts"
        self.store = self.module.DraftStore(self.root)

    def test_fixed_root_and_strict_ids(self):
        self.assertEqual(self.module.PRODUCTION_DRAFT_ROOT, Path("/opt/data/newsroom/drafts"))
        good = ("a", "report-2", "a" * 64)
        bad = ("", ".", "..", "A", "-a", "a-", "a_b", "a/b", "a\\b",
               "a%2fb", "é", "а", "a\x00b", "a" * 65)
        for value in good:
            self.module.validate_draft_id(value)
        for value in bad:
            with self.subTest(value=repr(value)), self.assertRaises(self.module.DraftError):
                self.module.validate_draft_id(value)

    def test_text_validation_is_bounded_utf8_and_rejects_controls_and_surrogates(self):
        for title, source in (("x\ud800", "ok"), ("ok", "x\x00y"),
                              ("ok", "x\u202ey"), ("x\n", "ok")):
            with self.subTest(title=repr(title), source=repr(source)), self.assertRaises(self.module.DraftError):
                self.store.save("one", title, source)
        with self.assertRaises(self.module.DraftError):
            self.store.save("one", "x" * (self.module.MAX_TITLE_BYTES + 1), "ok")
        with self.assertRaises(self.module.DraftError):
            self.store.save("one", "ok", "x" * (self.module.MAX_SOURCE_BYTES + 1))
        self.assertFalse(self.root.exists())

    def test_save_layout_modes_repeat_update_and_allowlisted_metadata(self):
        first = self.store.save("city-budget", "Budget & <cuts>", "First <script>alert(1)</script>")
        draft = self.root / "city-budget"
        self.assertEqual(set(p.name for p in draft.iterdir()),
                         {"source.txt", "rendition.html", "publication.json"})
        self.assertEqual((draft / "source.txt").read_text(), "First <script>alert(1)</script>")
        html = (draft / "rendition.html").read_text()
        self.assertIn("<article>", html)
        self.assertIn("Budget &amp; &lt;cuts&gt;", html)
        self.assertIn("&lt;script&gt;alert(1)&lt;/script&gt;", html)
        self.assertNotIn("<script", html.lower())
        metadata = json.loads((draft / "publication.json").read_text())
        self.assertEqual(metadata, {"draft_id": "city-budget", "schema_version": 1, "space_id": None})
        self.assertNotIn("Budget", json.dumps(metadata))
        self.assertEqual(stat.S_IMODE(draft.stat().st_mode), 0o700)
        for name in ("source.txt", "rendition.html", "publication.json"):
            self.assertEqual(stat.S_IMODE((draft / name).stat().st_mode), 0o600)
        self.store.save("city-budget", "Revised", "Second")
        self.assertEqual((draft / "source.txt").read_text(), "Second")
        self.assertEqual(len(list(self.root.glob("city-budget*"))), 1)
        self.assertEqual(first["draft_id"], "city-budget")

    def test_publication_state_is_allowlisted_atomic_and_survives_local_save(self):
        self.store.save("one", "Old title", "OLD SOURCE")
        publication = {
            "space_id": "spc_0123456789abcdef",
            "live_url": "https://news.spacefast.app/",
            "immutable_url": "https://version.spacefast.app/",
            "rendition_sha256": "a" * 64,
            "published_at": 2_000_000_000,
        }
        self.store.update_publication("one", publication)
        metadata = json.loads((self.root / "one/publication.json").read_text())
        self.assertEqual(metadata["space_id"], publication["space_id"])
        self.assertEqual(metadata["spacefast"], publication)
        before_publication = metadata["spacefast"]
        self.store.save("one", "New title", "NEW SOURCE")
        metadata = json.loads((self.root / "one/publication.json").read_text())
        self.assertEqual(metadata["spacefast"], before_publication)
        self.assertEqual((self.root / "one/source.txt").read_text(), "NEW SOURCE")

        for bad in ({"space_id": "bad/path"}, {**publication, "token": "SECRET"},
                    {**publication, "published_at": True}):
            with self.subTest(bad=bad), self.assertRaises(self.module.DraftError):
                self.store.update_publication("one", bad)
        self.assertEqual(json.loads((self.root / "one/publication.json").read_text())["spacefast"],
                         before_publication)

    def test_update_publication_rejects_query_urls_and_preserves_prior_metadata(self):
        self.store.save("one", "Title", "SOURCE")
        publication = {
            "space_id": "spc_0123456789abcdef",
            "live_url": "https://news.spacefast.app/",
            "immutable_url": "https://version.spacefast.app/",
            "rendition_sha256": "a" * 64,
            "published_at": 2_000_000_000,
        }
        self.store.update_publication("one", publication)
        metadata_path = self.root / "one/publication.json"
        before = metadata_path.read_bytes()
        secret = "QUERY-SECRET-MARKER"
        for field in ("live_url", "immutable_url"):
            query_publication = dict(publication)
            query_publication[field] += "?claim=" + secret
            with self.subTest(field=field):
                with self.assertRaisesRegex(
                        self.module.DraftError, "^invalid_publication$") as raised:
                    self.store.update_publication("one", query_publication)
                public_error = str(raised.exception) + repr(raised.exception)
                self.assertNotIn(secret, public_error)
                self.assertNotIn("claim", public_error)
                self.assertEqual(metadata_path.read_bytes(), before)

    def test_publication_urls_reject_every_ascii_control_without_modification(self):
        self.store.save("one", "Title", "SOURCE")
        publication = {
            "space_id": "spc_0123456789abcdef",
            "live_url": "https://news.spacefast.app/",
            "immutable_url": "https://version.spacefast.app/",
            "rendition_sha256": "a" * 64,
            "published_at": 2_000_000_000,
        }
        before = (self.root / "one/publication.json").read_bytes()
        for control in tuple(chr(value) for value in range(32)) + (chr(127),):
            bad = dict(publication)
            bad["live_url"] = "https://news.spacefast.app/a" + control + "URL-SECRET-MARKER"
            with self.subTest(control=ord(control)):
                with self.assertRaisesRegex(
                        self.module.DraftError, "^invalid_publication$") as raised:
                    self.store.update_publication("one", bad)
                self.assertNotIn("URL-SECRET-MARKER",
                                 str(raised.exception) + repr(raised.exception))
                self.assertEqual((self.root / "one/publication.json").read_bytes(), before)

    def test_failed_publication_metadata_exchange_preserves_complete_snapshot(self):
        self.store.save("one", "Title", "CANONICAL SOURCE")
        before = {name: (self.root / "one" / name).read_bytes() for name in self.module.FILES}

        def failpoint(stage):
            if stage == "publication_swap":
                raise OSError("private injected failure")

        failing = self.module.DraftStore(self.root, failpoint=failpoint)
        with self.assertRaises(self.module.DraftError):
            failing.update_publication("one", {
                "space_id": "spc_0123456789abcdef", "live_url": "https://news.spacefast.app/",
                "immutable_url": "https://version.spacefast.app/", "rendition_sha256": "a" * 64,
                "published_at": 2_000_000_000,
            })
        after = {name: (self.root / "one" / name).read_bytes() for name in self.module.FILES}
        self.assertEqual(after, before)

    def test_post_replace_publication_fsync_is_distinct_and_complete(self):
        self.store.save("one", "Title", "CANONICAL SOURCE")
        before = (self.root / "one/publication.json").read_bytes()
        intended = {
            "space_id": "spc_0123456789abcdef", "live_url": "https://news.spacefast.app/",
            "immutable_url": "https://version.spacefast.app/", "rendition_sha256": "a" * 64,
            "published_at": 2_000_000_000,
        }

        def failpoint(stage):
            if stage == "publication_directory_fsync":
                raise OSError("private directory fsync failure")

        with self.assertRaisesRegex(
                self.module.DraftError, "^publication_commit_uncertain$") as raised:
            self.module.DraftStore(self.root, failpoint=failpoint).update_publication("one", intended)
        self.assertTrue(getattr(raised.exception, "intended_visible", False))
        after = (self.root / "one/publication.json").read_bytes()
        intended_bytes = json.dumps(
            {"draft_id": "one", "schema_version": 1,
             "space_id": intended["space_id"], "spacefast": intended},
            ensure_ascii=True, sort_keys=True, separators=(",", ":")).encode("ascii") + b"\n"
        self.assertIn(after, (before, intended_bytes))
        self.store.read_snapshot("one")

    def test_publication_root_replacement_before_and_after_replace_fails_closed(self):
        publication = {
            "space_id": "spc_0123456789abcdef", "live_url": "https://news.spacefast.app/",
            "immutable_url": "https://version.spacefast.app/", "rendition_sha256": "a" * 64,
            "published_at": 2_000_000_000,
        }
        for point, uncertain in (("publication_before_replace", False),
                                 ("publication_after_replace", True)):
            with self.subTest(point=point), tempfile.TemporaryDirectory() as directory:
                base = Path(directory)
                root = base / "drafts"
                self.module.DraftStore(root).save("one", "old", "OLD")
                prior = (root / "one/publication.json").read_bytes()
                attacker = base / "attacker"
                attacker.mkdir(mode=0o700)
                (attacker / "sentinel").write_text("SAFE")
                outside = base / "outside-sentinel"
                outside.write_text("OUTSIDE SAFE")
                detached = base / "detached"

                def replace_root(stage):
                    if stage == point:
                        root.rename(detached)
                        attacker.rename(root)

                expected = "publication_commit_uncertain" if uncertain else "unsafe_storage"
                with self.assertRaisesRegex(self.module.DraftError, f"^{expected}$"):
                    self.module.DraftStore(root, failpoint=replace_root).update_publication(
                        "one", publication)
                self.assertEqual((root / "sentinel").read_text(), "SAFE")
                self.assertEqual(outside.read_text(), "OUTSIDE SAFE")
                detached_metadata = (detached / "one/publication.json").read_bytes()
                if uncertain:
                    self.assertNotEqual(detached_metadata, prior)
                else:
                    self.assertEqual(detached_metadata, prior)

    def test_publication_transaction_blocks_save_and_commits_its_snapshot(self):
        self.store.save("one", "Old", "OLD")
        entered = threading.Event()
        release = threading.Event()
        errors = []

        def publish_transaction():
            try:
                with self.store.publication_transaction("one") as transaction:
                    self.assertIn(b"OLD", transaction.snapshot["rendition.html"])
                    entered.set()
                    self.assertTrue(release.wait(5))
                    transaction.update_publication({
                        "space_id": "spc_0123456789abcdef",
                        "live_url": "https://news.spacefast.app/",
                        "immutable_url": "https://version.spacefast.app/",
                        "rendition_sha256": "a" * 64,
                        "published_at": 2_000_000_000,
                    })
            except Exception as error:
                errors.append(error)

        publisher = threading.Thread(target=publish_transaction)
        publisher.start()
        self.assertTrue(entered.wait(5))
        saver = threading.Thread(target=lambda: self._capture(
            errors, lambda: self.store.save("one", "New", "NEW")))
        saver.start()
        saver.join(0.2)
        self.assertTrue(saver.is_alive(), "save did not wait for publication transaction")
        self.assertEqual((self.root / "one/source.txt").read_text(), "OLD")
        release.set()
        publisher.join(5)
        saver.join(5)
        self.assertEqual(errors, [])
        self.assertEqual((self.root / "one/source.txt").read_text(), "NEW")
        metadata = json.loads((self.root / "one/publication.json").read_text())
        self.assertEqual(metadata["space_id"], "spc_0123456789abcdef")

    def test_save_waiting_on_publication_does_not_convoy_another_draft(self):
        self.store.save("one", "One", "ONE")
        self.store.save("two", "Two", "TWO")
        publication_entered = threading.Event()
        release_publication = threading.Event()
        save_one_started = threading.Event()
        save_two_started = threading.Event()
        save_two_completed = threading.Event()
        errors = []

        def hold_publication():
            try:
                with self.store.publication_transaction("one"):
                    publication_entered.set()
                    self.assertTrue(release_publication.wait(5))
            except Exception as error:
                errors.append(error)

        def save_one():
            save_one_started.set()
            self._capture(errors, lambda: self.store.save("one", "New One", "NEW ONE"))

        def save_two():
            save_two_started.set()
            try:
                self.store.save("two", "New Two", "NEW TWO")
            except Exception as error:
                errors.append(error)
            finally:
                save_two_completed.set()

        publisher = threading.Thread(target=hold_publication)
        blocked_saver = threading.Thread(target=save_one)
        independent_saver = threading.Thread(target=save_two)
        publisher.start()
        self.assertTrue(publication_entered.wait(5))
        blocked_saver.start()
        self.assertTrue(save_one_started.wait(5))
        blocked_saver.join(0.2)
        self.assertTrue(blocked_saver.is_alive(), "save one did not wait for publication one")

        independent_saver.start()
        self.assertTrue(save_two_started.wait(5))
        completed_before_release = save_two_completed.wait(1)
        states_before_release = None
        if completed_before_release:
            states_before_release = (
                (self.root / "one/source.txt").read_text(),
                (self.root / "two/source.txt").read_text(),
            )
        release_publication.set()
        publisher.join(5)
        blocked_saver.join(5)
        independent_saver.join(5)
        self.assertTrue(completed_before_release,
                        "save two convoyed behind save one while publication one was held")
        self.assertEqual(states_before_release, ("ONE", "NEW TWO"))
        self.assertFalse(publisher.is_alive())
        self.assertFalse(blocked_saver.is_alive())
        self.assertFalse(independent_saver.is_alive())
        self.assertEqual(errors, [])
        self.assertEqual((self.root / "one/source.txt").read_text(), "NEW ONE")
        self.assertEqual((self.root / "two/source.txt").read_text(), "NEW TWO")

    def test_publication_transaction_cleanup_closes_root_after_lock_close_failure(self):
        self.store.save("one", "One", "ONE")
        transaction = self.store.publication_transaction("one")
        transaction.__enter__()
        lock_fd = transaction._lock_fd
        root_fd = transaction._root_fd
        real_close = os.close
        closed = []

        def close_then_fail_for_lock(fd):
            real_close(fd)
            closed.append(fd)
            if fd == lock_fd:
                raise OSError("injected lock close failure")

        primary = RuntimeError("primary publication failure")
        with mock.patch.object(self.module.os, "close", side_effect=close_then_fail_for_lock):
            with self.assertRaisesRegex(RuntimeError, "primary publication failure"):
                try:
                    raise primary
                finally:
                    transaction.__exit__(RuntimeError, primary, None)
        self.assertEqual(closed, [lock_fd, root_fd])
        self.assertIsNone(transaction._lock_fd)
        self.assertIsNone(transaction._root_fd)

    def test_publication_transactions_for_different_drafts_do_not_block(self):
        self.store.save("one", "One", "ONE")
        self.store.save("two", "Two", "TWO")
        entered_one = threading.Event()
        release_one = threading.Event()
        entered_two = threading.Event()
        errors = []

        def hold_one():
            try:
                with self.store.publication_transaction("one"):
                    entered_one.set()
                    release_one.wait(5)
            except Exception as error:
                errors.append(error)

        def enter_two():
            try:
                with self.store.publication_transaction("two"):
                    entered_two.set()
            except Exception as error:
                errors.append(error)

        first = threading.Thread(target=hold_one)
        first.start()
        self.assertTrue(entered_one.wait(5))
        second = threading.Thread(target=enter_two)
        second.start()
        self.assertTrue(entered_two.wait(1), "different draft reused the wrong lock scope")
        release_one.set()
        first.join(5)
        second.join(5)
        self.assertEqual(errors, [])

    def test_renderer_never_interprets_attacker_markup(self):
        payload = "\n\n".join((
            "# <img src=x onerror=alert(1)>",
            "<style>body{display:none}</style>",
            "- [click](javascript:alert(1))",
            "</p><iframe srcdoc='<script>x</script>'>",
            "<svg><a href=javascript:x>bad</a></svg>",
            "<form action=//evil><input autofocus onfocus=x>",
            "&lt;script&gt; {{constructor.constructor('x')()}}",
        ))
        rendered = self.module.render_html("</title><script>x</script>", payload)
        lowered = rendered.lower()
        for forbidden in ("<script", "<style", "<iframe", "<svg", "<form"):
            self.assertNotIn(forbidden, lowered)
        # Attribute/URL payloads remain visible text only; the renderer itself emits
        # no links, media, forms, event attributes, style blocks, or raw HTML.
        self.assertNotIn("<a ", lowered)
        self.assertNotIn("<img ", lowered)
        self.assertNotIn("<input ", lowered)
        self.assertIn("&lt;script&gt;", rendered)
        self.assertEqual(rendered, self.module.render_html("</title><script>x</script>", payload))

    def test_invalid_utf8_file_is_rejected_before_storage(self):
        source = Path(self.temporary.name) / "bad.txt"
        source.write_bytes(b"good\xffbad")
        with self.assertRaises(self.module.DraftError):
            self.store.save_file("one", "title", source)
        self.assertFalse(self.root.exists())

    def test_save_file_rejects_unsafe_inode_boundaries_before_storage_creation(self):
        source = Path(self.temporary.name) / "input.txt"
        source.write_text("SAFE SOURCE")
        source.chmod(0o600)
        hardlink = Path(self.temporary.name) / "input-hardlink.txt"
        os.link(source, hardlink)
        with self.assertRaisesRegex(self.module.DraftError, "^invalid_source$") as raised:
            self.store.save_file("one", "title", source)
        self.assertIsNone(raised.exception.__cause__)
        self.assertFalse(self.root.exists())
        hardlink.unlink()

        real_fstat = self.module.os.fstat
        def wrong_owner(fd):
            info = real_fstat(fd)
            fields = {name: getattr(info, name) for name in dir(info) if name.startswith("st_")}
            fields["st_uid"] = os.geteuid() + 1
            return types.SimpleNamespace(**fields)
        with mock.patch.object(self.module.os, "fstat", side_effect=wrong_owner):
            with self.assertRaisesRegex(self.module.DraftError, "^invalid_source$") as raised:
                self.store.save_file("one", "title", source)
        self.assertIsNone(raised.exception.__cause__)
        self.assertFalse(self.root.exists())

        for mode in (0o620, 0o602, 0o666):
            source.chmod(mode)
            with self.subTest(mode=oct(mode)), self.assertRaisesRegex(
                    self.module.DraftError, "^invalid_source$") as raised:
                self.store.save_file("one", "title", source)
            self.assertIsNone(raised.exception.__cause__)
            self.assertFalse(self.root.exists())

    def test_save_file_accepts_owned_regular_0600_and_0644_inputs(self):
        for index, mode in enumerate((0o600, 0o644)):
            source = Path(self.temporary.name) / f"safe-{index}.txt"
            source.write_text(f"SAFE {mode:o}")
            source.chmod(mode)
            self.assertEqual(self.store.save_file(f"safe-{index}", "title", source),
                             {"draft_id": f"safe-{index}", "saved": True})

    def test_failures_before_exchange_preserve_exact_old_snapshot(self):
        self.store.save("one", "old", "OLD")
        for stage in ("create", "write", "fsync", "replace", "swap"):
            with self.subTest(stage=stage):
                def failpoint(current, wanted=stage):
                    if current == wanted:
                        raise OSError("injected confidential path /outside")
                failing = self.module.DraftStore(self.root, failpoint=failpoint)
                with self.assertRaises(self.module.DraftError):
                    failing.save("one", "new", "NEW")
                source = (self.root / "one/source.txt").read_text()
                html = (self.root / "one/rendition.html").read_text()
                self.assertEqual(source, "OLD")
                self.assertIn("OLD", html)
                self.assertNotIn("NEW", html)

    def test_unsupported_atomic_exchange_fails_safely_with_old_canonical(self):
        self.store.save("one", "old", "OLD")
        with mock.patch.object(
                self.module, "_rename_exchange",
                side_effect=OSError(errno.ENOSYS, "renameat2 unavailable")):
            with self.assertRaisesRegex(self.module.DraftError, "^save_failed$"):
                self.store.save("one", "new", "NEW")
        self.assertEqual((self.root / "one/source.txt").read_text(), "OLD")
        self.assertNotIn("NEW", (self.root / "one/rendition.html").read_text())

    def test_interrupt_immediately_before_exchange_preserves_old_canonical(self):
        self.store.save("one", "old", "OLD")

        def interrupt(stage):
            if stage == "swap":
                raise KeyboardInterrupt()

        with self.assertRaises(KeyboardInterrupt):
            self.module.DraftStore(self.root, failpoint=interrupt).save(
                "one", "new", "NEW")
        self.assertEqual((self.root / "one/source.txt").read_text(), "OLD")
        self.assertIn("OLD", (self.root / "one/rendition.html").read_text())
        self.assertNotIn("NEW", (self.root / "one/rendition.html").read_text())

    def test_failure_or_interrupt_after_exchange_is_uncertain_and_preserves_old_stage(self):
        for failure in (OSError("post-exchange failure"), KeyboardInterrupt()):
            with self.subTest(failure=type(failure).__name__):
                self.store.save("one", "old", "OLD")

                def failpoint(stage):
                    if stage == "after_exchange":
                        raise failure

                with self.assertRaisesRegex(
                        self.module.DraftError, "^save_commit_uncertain$"):
                    self.module.DraftStore(self.root, failpoint=failpoint).save(
                        "one", "new", "NEW")
                self.assertEqual((self.root / "one/source.txt").read_text(), "NEW")
                self.assertIn("NEW", (self.root / "one/rendition.html").read_text())
                self.assertNotIn("OLD", (self.root / "one/rendition.html").read_text())
                stages = list(self.root.glob(".stage-*"))
                self.assertEqual(len(stages), 1)
                self.assertEqual((stages[0] / "source.txt").read_text(), "OLD")
                self.store.save("one", "later", "LATER")
                self.assertEqual(list(self.root.glob(".stage-*")), [])

    def test_cleanup_failure_may_leave_old_stage_but_canonical_is_new(self):
        self.store.save("one", "old", "OLD")
        with mock.patch.object(self.module, "_remove_snapshot",
                               side_effect=OSError("cleanup interrupted")):
            self.assertEqual(self.store.save("one", "new", "NEW"),
                             {"draft_id": "one", "saved": True})
        self.assertEqual((self.root / "one/source.txt").read_text(), "NEW")
        stages = list(self.root.glob(".stage-*"))
        self.assertEqual(len(stages), 1)
        self.assertEqual((stages[0] / "source.txt").read_text(), "OLD")

    def test_next_explicit_save_reclaims_valid_abandoned_stage_only(self):
        self.store.save("one", "old", "OLD")
        with mock.patch.object(self.module, "_remove_snapshot",
                               side_effect=OSError("cleanup interrupted")):
            self.store.save("one", "new", "NEW")
        abandoned = next(self.root.glob(".stage-*"))

        # Construction and supported reads are intentionally side-effect free.
        fresh = self.module.DraftStore(self.root)
        self.assertEqual(fresh.read_snapshot("one")["source.txt"], b"NEW")
        self.assertTrue(abandoned.exists())
        lookalike = self.root / ".stage-not-an-internal-uuid"
        lookalike.mkdir()
        (lookalike / "sentinel").write_text("SAFE")

        fresh.save("two", "other", "OTHER")
        self.assertFalse(abandoned.exists())
        self.assertEqual((lookalike / "sentinel").read_text(), "SAFE")
        self.assertEqual((self.root / "one/source.txt").read_text(), "NEW")

    def test_scavenger_refuses_malformed_linked_and_special_stages_without_external_damage(self):
        self.store.save("one", "title", "SOURCE")
        outside = Path(self.temporary.name) / "outside-stage"
        outside.mkdir()
        sentinel = outside / "sentinel"
        sentinel.write_text("SAFE")

        cases = ("unexpected", "symlink", "hardlink", "fifo")
        for index, kind in enumerate(cases):
            stage = self.root / f".stage-{index:032x}"
            stage.mkdir(mode=0o700)
            if kind == "unexpected":
                (stage / "unexpected").write_text("hostile")
            elif kind == "symlink":
                (stage / "source.txt").symlink_to(sentinel)
            elif kind == "hardlink":
                os.link(sentinel, stage / "source.txt")
            else:
                os.mkfifo(stage / "source.txt")
            with self.subTest(kind=kind), self.assertRaisesRegex(
                    self.module.DraftError, "^unsafe_draft_state$"):
                self.store.save("two", "title", "source")
            self.assertTrue(stage.exists())
            self.assertEqual(sentinel.read_text(), "SAFE")
            for child in stage.iterdir():
                child.unlink()
            stage.rmdir()

    def test_scavenger_candidate_count_is_bounded_before_removal(self):
        self.store.save("one", "title", "SOURCE")
        stages = []
        for index in range(self.module.MAX_STAGE_CANDIDATES + 1):
            stage = self.root / f".stage-{index:032x}"
            stage.mkdir(mode=0o700)
            stages.append(stage)
        with self.assertRaisesRegex(self.module.DraftError, "^unsafe_draft_state$"):
            self.store.save("two", "title", "source")
        self.assertTrue(all(stage.exists() for stage in stages))

    def test_scavenger_requires_strict_consistent_snapshot_content(self):
        corruptions = {
            "publication.json": b'{"draft_id":"one","schema_version":1,"space_id":null} ',
            "source.txt": b"SOURCE\x00",
            "rendition.html": b"<!doctype html>\nnot the stored source\n",
        }
        for filename, replacement in corruptions.items():
            with self.subTest(filename=filename), tempfile.TemporaryDirectory() as directory:
                root = Path(directory) / "drafts"
                store = self.module.DraftStore(root)
                store.save("one", "old", "SOURCE")
                with mock.patch.object(self.module, "_remove_snapshot",
                                       side_effect=OSError("leave stage")):
                    store.save("one", "new", "NEW")
                stage = next(root.glob(".stage-*"))
                (stage / filename).write_bytes(replacement)
                with self.assertRaisesRegex(
                        self.module.DraftError, "^unsafe_draft_state$"):
                    store.save("two", "title", "source")
                self.assertTrue(stage.exists())

    def test_post_exchange_directory_fsync_failure_keeps_new_canonical(self):
        self.store.save("one", "old", "OLD")

        def failpoint(stage):
            if stage == "root_fsync":
                raise OSError("fsync interrupted")

        with self.assertRaisesRegex(self.module.DraftError, "^save_commit_uncertain$"):
            self.module.DraftStore(self.root, failpoint=failpoint).save(
                "one", "new", "NEW")
        self.assertEqual((self.root / "one/source.txt").read_text(), "NEW")
        stage = next(self.root.glob(".stage-*"))
        self.assertEqual((stage / "source.txt").read_text(), "OLD")

    def test_exchange_never_exposes_a_missing_canonical_to_unlocked_readers(self):
        self.store.save("one", "old", "OLD")
        observed = []
        real_exchange = self.module._rename_exchange

        def observing_exchange(parent_fd, left, right):
            observed.append((self.root / "one/source.txt").read_text())
            real_exchange(parent_fd, left, right)
            observed.append((self.root / "one/source.txt").read_text())

        with mock.patch.object(self.module, "_rename_exchange", side_effect=observing_exchange):
            self.store.save("one", "new", "NEW")
        self.assertEqual(observed, ["OLD", "NEW"])
        self.assertTrue((self.root / "one").is_dir())

    def test_linked_or_special_boundaries_are_refused_without_touching_sentinel(self):
        outside = Path(self.temporary.name) / "outside"
        outside.mkdir()
        sentinel = outside / "sentinel"
        sentinel.write_text("safe")
        self.root.mkdir()
        (self.root / "linked").symlink_to(outside, target_is_directory=True)
        with self.assertRaises(self.module.DraftError):
            self.store.save("linked", "title", "source")
        self.assertEqual(sentinel.read_text(), "safe")

        regular = self.root / "hard"
        regular.mkdir()
        victim = regular / "source.txt"
        victim.write_text("victim")
        os.link(victim, outside / "hardlink")
        with self.assertRaises(self.module.DraftError):
            self.store.save("hard", "title", "source")
        self.assertEqual((outside / "hardlink").read_text(), "victim")

        fifo_dir = self.root / "fifo"
        fifo_dir.mkdir()
        os.mkfifo(fifo_dir / "source.txt")
        with self.assertRaises(self.module.DraftError):
            self.store.save("fifo", "title", "source")

    def test_concurrent_writers_produce_one_complete_snapshot(self):
        barrier = threading.Barrier(3)
        errors = []
        def writer(title, source):
            try:
                barrier.wait()
                self.store.save("one", title, source)
            except Exception as error:  # pragma: no cover - assertion reports it
                errors.append(error)
        threads = [threading.Thread(target=writer, args=("A", "SOURCE-A")),
                   threading.Thread(target=writer, args=("B", "SOURCE-B"))]
        for thread in threads:
            thread.start()
        barrier.wait()
        for thread in threads:
            thread.join()
        self.assertEqual(errors, [])
        source = (self.root / "one/source.txt").read_text()
        html = (self.root / "one/rendition.html").read_text()
        self.assertIn(source, {"SOURCE-A", "SOURCE-B"})
        self.assertIn(source, html)
        self.assertEqual(set(p.name for p in (self.root / "one").iterdir()),
                         {"source.txt", "rendition.html", "publication.json"})

    def test_supported_reader_blocks_during_swap_and_gets_complete_new_snapshot(self):
        self.store.save("one", "old", "OLD")
        in_swap = threading.Event()
        release_swap = threading.Event()

        def pause(stage):
            if stage == "swap":
                in_swap.set()
                self.assertTrue(release_swap.wait(5))

        writer_errors = []
        reader_errors = []
        result = []
        writer = threading.Thread(
            target=lambda: self._capture(
                writer_errors,
                lambda: self.module.DraftStore(self.root, failpoint=pause).save(
                    "one", "new", "NEW")),
        )
        writer.start()
        self.assertTrue(in_swap.wait(5))
        reader = threading.Thread(
            target=lambda: self._capture(
                reader_errors, lambda: result.append(self.store.read_snapshot("one"))),
        )
        reader.start()
        reader.join(0.2)
        self.assertTrue(reader.is_alive(), "reader did not wait for the exclusive swap lock")
        release_swap.set()
        writer.join(5)
        reader.join(5)
        self.assertEqual(writer_errors, [])
        self.assertEqual(reader_errors, [])
        self.assertEqual(result[0]["source.txt"], b"NEW")
        self.assertIn(b"NEW", result[0]["rendition.html"])
        self.assertEqual(json.loads(result[0]["publication.json"]),
                         {"draft_id": "one", "schema_version": 1, "space_id": None})
        self.assertEqual(self.store.read_rendition("one"), result[0]["rendition.html"])

    def test_snapshot_metadata_is_bound_to_the_requested_directory_id(self):
        self.store.save("one", "Private", "SECRET ONE")
        self.store.save("two", "Public", "PUBLIC TWO")
        original_two = self.root / "two-original"
        (self.root / "two").rename(original_two)
        (self.root / "one").rename(self.root / "two")

        with self.assertRaisesRegex(self.module.DraftError, "^unsafe_draft_state$"):
            self.store.read_snapshot("two")
        with self.assertRaisesRegex(self.module.DraftError, "^unsafe_draft_state$"):
            self.store.read_rendition("two")

        (self.root / "two").rename(self.root / "one")
        original_two.rename(self.root / "two")
        self.assertEqual(self.store.read_snapshot("two")["source.txt"], b"PUBLIC TWO")

    def test_supported_reader_waits_through_failed_swap_and_gets_complete_old_snapshot(self):
        self.store.save("one", "old", "OLD")
        in_swap = threading.Event()
        release_swap = threading.Event()

        def fail_after_pause(stage):
            if stage == "swap":
                in_swap.set()
                self.assertTrue(release_swap.wait(5))
                raise OSError("injected swap failure")

        writer_errors = []
        reader_errors = []
        result = []
        writer = threading.Thread(
            target=lambda: self._capture(
                writer_errors,
                lambda: self.module.DraftStore(self.root, failpoint=fail_after_pause).save(
                    "one", "new", "NEW")),
        )
        writer.start()
        self.assertTrue(in_swap.wait(5))
        reader = threading.Thread(
            target=lambda: self._capture(
                reader_errors, lambda: result.append(self.store.read_snapshot("one"))),
        )
        reader.start()
        reader.join(0.2)
        self.assertTrue(reader.is_alive())
        release_swap.set()
        writer.join(5)
        reader.join(5)
        self.assertEqual(len(writer_errors), 1)
        self.assertIsInstance(writer_errors[0], self.module.DraftError)
        self.assertEqual(reader_errors, [])
        self.assertEqual(result[0]["source.txt"], b"OLD")
        self.assertIn(b"OLD", result[0]["rendition.html"])
        self.assertNotIn(b"NEW", result[0]["rendition.html"])

    def test_reads_never_create_storage_and_reject_an_unsafe_stable_lock(self):
        with self.assertRaises(self.module.DraftError):
            self.store.read_snapshot("one")
        self.assertFalse(self.root.exists())

        self.store.save("one", "title", "source")
        lock = self.root / ".lock-one"
        lock.unlink()
        outside = Path(self.temporary.name) / "outside-lock"
        outside.write_text("sentinel")
        lock.symlink_to(outside)
        with self.assertRaises(self.module.DraftError):
            self.store.read_snapshot("one")
        self.assertEqual(outside.read_text(), "sentinel")

    def test_read_and_save_reject_permissive_root_draft_and_lock_modes(self):
        self.store.save("one", "old", "OLD")
        cases = ((self.root, 0o777), (self.root / "one", 0o777),
                 (self.root / ".lock-one", 0o666))
        for path, mode in cases:
            with self.subTest(path=path.name):
                original = stat.S_IMODE(path.stat().st_mode)
                path.chmod(mode)
                with self.assertRaises(self.module.DraftError):
                    self.store.read_snapshot("one")
                with self.assertRaises(self.module.DraftError):
                    self.store.save("one", "new", "NEW")
                path.chmod(original)
                self.assertEqual((self.root / "one/source.txt").read_text(), "OLD")

    def test_root_replacement_before_commit_refuses_and_never_touches_either_tree(self):
        self.store.save("one", "old", "OLD")
        moved = Path(self.temporary.name) / "moved-root"
        attacker = Path(self.temporary.name) / "attacker-root"
        attacker.mkdir(mode=0o700)
        sentinel = attacker / "sentinel"
        sentinel.write_text("SAFE")

        def replace_root(stage):
            if stage == "replace":
                self.root.rename(moved)
                attacker.rename(self.root)

        failing = self.module.DraftStore(self.root, failpoint=replace_root)
        with self.assertRaisesRegex(self.module.DraftError, "unsafe_storage"):
            failing.save("one", "new", "NEW")
        self.assertEqual((self.root / "sentinel").read_text(), "SAFE")
        self.assertEqual((moved / "one/source.txt").read_text(), "OLD")
        self.assertNotIn("NEW", (moved / "one/rendition.html").read_text())
        # Cleanup must not mutate the detached tree after identity loss.
        self.assertTrue(any(p.name.startswith(".stage-") for p in moved.iterdir()))

    def test_root_replacement_immediately_after_namespace_commit_fails_closed(self):
        for update in (False, True):
            with self.subTest(update=update), tempfile.TemporaryDirectory() as directory:
                base = Path(directory)
                root = base / "drafts"
                store = self.module.DraftStore(root)
                if update:
                    store.save("one", "old", "OLD")
                attacker = base / "attacker"
                attacker.mkdir(mode=0o700)
                sentinel = attacker / "sentinel"
                sentinel.write_text("SAFE")
                detached = base / "detached"

                if update:
                    real_exchange = self.module._rename_exchange

                    def hostile_exchange(parent_fd, left, right):
                        real_exchange(parent_fd, left, right)
                        os.rename(root, detached)
                        os.rename(attacker, root)

                    patcher = mock.patch.object(
                        self.module, "_rename_exchange", side_effect=hostile_exchange)
                else:
                    real_rename = os.rename

                    def hostile_rename(source, destination, *args, **kwargs):
                        real_rename(source, destination, *args, **kwargs)
                        real_rename(root, detached)
                        real_rename(attacker, root)

                    patcher = mock.patch.object(self.module.os, "rename", side_effect=hostile_rename)

                with patcher, self.assertRaisesRegex(
                        self.module.DraftError, "^save_commit_uncertain$"):
                    store.save("one", "new", "NEW")
                self.assertEqual((root / "sentinel").read_text(), "SAFE")
                self.assertEqual((detached / "one/source.txt").read_text(), "NEW")
                if update:
                    self.assertTrue(any(
                        path.name.startswith(".stage-") for path in detached.iterdir()))

    @staticmethod
    def _capture(errors, operation):
        try:
            operation()
        except Exception as error:  # pragma: no cover - assertions report it
            errors.append(error)


if __name__ == "__main__":
    unittest.main()
