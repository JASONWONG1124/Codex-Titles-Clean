"""Behavioral tests for title quality boundaries and reversible title changes.

These tests use an in-memory RPC double and temporary storage. They never read
or rename real Codex tasks.
"""
import copy
import contextlib
import io
import json
import sys
import tempfile
import threading
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
import title_manager
from title_manager import Manager, Store, validate_title


THREAD_ID = "11111111-2222-4333-8444-555555555555"
ORIGINAL = "讨论对话窗口标题整理功能"
INITIAL = "Codex丨讨论丨对话标题自动整理"
DEVELOPMENT = "Codex丨开发丨对话标题自动整理"
OTHER = "课程丨教研丨观点表达练习"


class FakeRpc:
    def __init__(self, title=ORIGINAL):
        self.thread = {"id": THREAD_ID, "name": title, "parentThreadId": None}
        self.calls = []
        self.fail_read = False
        self.fail_set = False
        self.fail_after_set = False
        self.ignore_set = False
        self.before_read = None

    def call(self, method, params):
        self.calls.append((method, copy.deepcopy(params)))
        if params.get("threadId") != THREAD_ID:
            raise AssertionError("Unexpected target task")
        if method == "thread/read":
            if self.before_read:
                self.before_read(self)
            if self.fail_read:
                raise RuntimeError("Read denied")
            return {"thread": copy.deepcopy(self.thread)}
        if method == "thread/name/set":
            if self.fail_set:
                raise RuntimeError("Write denied")
            if not self.ignore_set:
                self.thread["name"] = params["name"]
            if self.fail_after_set:
                raise TimeoutError("Response lost after remote write")
            return {}
        raise AssertionError(f"Unexpected RPC: {method}")

    @property
    def writes(self):
        return [params for method, params in self.calls if method == "thread/name/set"]


class BatchRpc:
    def __init__(self, titles, denied=()):
        self.threads = {
            tid: {"id": tid, "name": title, "parentThreadId": None}
            for tid, title in titles.items()
        }
        self.denied = set(denied)
        self.writes = []

    def call(self, method, params):
        tid = params["threadId"]
        if method == "thread/read":
            return {"thread": copy.deepcopy(self.threads[tid])}
        if method == "thread/name/set":
            self.writes.append(copy.deepcopy(params))
            if tid in self.denied:
                raise RuntimeError("Write denied for this task")
            self.threads[tid]["name"] = params["name"]
            return {}
        raise AssertionError(f"Unexpected RPC: {method}")


class UntitledMigrationTests(unittest.TestCase):
    def test_explicit_migration_names_untitled_and_restores_display(self):
        with tempfile.TemporaryDirectory() as root:
            rpc = FakeRpc(None)
            rpc.thread['preview'] = '讨论人的生活目的\n后续内容'
            store = Store(root)
            manager = Manager(store, rpc)
            result = manager.check(THREAD_ID, INITIAL, '整理历史', 'initial', '首次迁移',
                                   expected_title=None, allow_untitled=True)
            self.assertEqual(result['status'], 'updated')
            self.assertIsNone(store.load(THREAD_ID)['original_title'])
            restored = manager.restore(THREAD_ID)
            self.assertEqual(restored['restore_mode'], 'display_fallback')
            self.assertEqual(rpc.thread['name'], '讨论人的生活目的')

    def test_expected_null_rejects_a_newly_assigned_name(self):
        with tempfile.TemporaryDirectory() as root:
            rpc = FakeRpc('用户刚改的标题')
            result = Manager(Store(root), rpc).check(THREAD_ID, INITIAL, '整理历史', 'major', '首次迁移',
                                                    expected_title=None, allow_untitled=True)
            self.assertEqual(result['status'], 'conflict')
            self.assertEqual(rpc.writes, [])


class TitleValidationTests(unittest.TestCase):
    def test_categories_are_open_two_han_characters(self):
        for category in ("教研", "翻译", "复盘", "评审", "策划"):
            title = f"课程丨{category}丨观点表达练习"
            with self.subTest(category=category):
                self.assertEqual(validate_title(title), title)

    def test_rejects_wrong_separators_or_missing_segments(self):
        for title in (
            "PPT|创作|用户感受", "PPT｜创作｜用户感受", "PPT丨创作",
            "PPT丨创作丨用户丨感受", "丨创作丨用户感受", "PPT丨丨用户感受",
            "PPT丨创作丨", " PPT丨创作丨用户感受", "PPT丨 创作丨用户感受",
        ):
            with self.subTest(title=repr(title)), self.assertRaises(ValueError):
                validate_title(title)

    def test_rejects_control_characters_and_embedded_pipes(self):
        for char in ("\n", "\r", "\t", "\x00", "\x1b", "\u200b", "|", "｜"):
            with self.subTest(char=repr(char)), self.assertRaises(ValueError):
                validate_title(f"PPT丨创作丨用户{char}感受")

    def test_category_must_be_exactly_two_han_characters(self):
        for category in ("AI", "创", "创作类", "创1", "创😀"):
            with self.subTest(category=category), self.assertRaises(ValueError):
                validate_title(f"PPT丨{category}丨用户感受")

    def test_subject_and_summary_have_independent_limits(self):
        validate_title("12345678丨创作丨主题")
        validate_title("A丨创作丨" + "x" * 24)
        for title in ("123456789丨创作丨主题", "A丨创作丨" + "x" * 25):
            with self.subTest(title=title), self.assertRaises(ValueError):
                validate_title(title)

    def test_display_width_prevents_long_han_titles(self):
        # A = 1 column; two Han separators + category = 8; summary = 41.
        validate_title("A丨创作丨" + "题" * 20 + "x")
        with self.assertRaises(ValueError):
            validate_title("A丨创作丨" + "题" * 21)

    def test_non_text_is_rejected(self):
        for title in (None, 12, [INITIAL]):
            with self.subTest(title=title), self.assertRaises(ValueError):
                validate_title(title)


class StoreTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.store = Store(Path(self.tmp.name))

    def test_unicode_state_survives_a_new_store_instance(self):
        self.store.save(THREAD_ID, {"original_title": ORIGINAL, "history": [{"new_title": INITIAL}]})
        state = Store(Path(self.tmp.name)).load(THREAD_ID)
        self.assertEqual(state["original_title"], ORIGINAL)
        self.assertEqual(state["history"], [{"new_title": INITIAL}])

    def test_missing_task_has_no_state(self):
        self.assertEqual(self.store.load(THREAD_ID), {})

    def test_task_id_cannot_escape_storage_directory(self):
        for thread_id in ("../outside", "/tmp/title", "a/b", "", "x" * 129):
            with self.subTest(thread_id=thread_id), self.assertRaises(ValueError):
                self.store.save(thread_id, {"original_title": ORIGINAL})
        self.assertEqual(list(Path(self.tmp.name).iterdir()), [])

    def test_lock_serializes_access_and_releases_after_exception(self):
        attempting = threading.Event()
        entered = threading.Event()
        errors = []

        def worker():
            try:
                attempting.set()
                with Store(Path(self.tmp.name)).locked(THREAD_ID):
                    entered.set()
            except Exception as exc:
                errors.append(exc)

        thread = threading.Thread(target=worker, daemon=True)
        with self.assertRaisesRegex(RuntimeError, "deliberate"):
            with self.store.locked(THREAD_ID):
                thread.start()
                self.assertTrue(attempting.wait(2))
                self.assertFalse(entered.wait(0.1), "Concurrent access bypassed the lock")
                raise RuntimeError("deliberate")
        thread.join(2)
        self.assertFalse(thread.is_alive(), "Lock was not released after an exception")
        self.assertTrue(entered.is_set())
        self.assertEqual(errors, [])


class ManagerTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.store = Store(Path(self.tmp.name))
        self.rpc = FakeRpc()
        self.manager = Manager(self.store, self.rpc)

    def check(self, title=INITIAL, change="initial", check_id=None):
        return self.manager.check(
            THREAD_ID, title, "实现并验证对话标题自动整理", change,
            "主要目标从讨论可行性转为开发和验证", check_id=check_id,
        )

    def pending(self, nonce="current-turn", checked=False):
        state = self.store.load(THREAD_ID)
        state["pending"] = {"id": nonce, "checked": checked}
        self.store.save(THREAD_ID, state)

    def assert_no_applied_history(self):
        history = self.store.load(THREAD_ID).get("history", [])
        self.assertFalse(any(record.get("status") == "applied" for record in history))

    def test_initial_and_major_change_update_but_followup_keeps_title(self):
        self.assertEqual(self.check()["status"], "updated")
        self.assertEqual(self.rpc.thread["name"], INITIAL)
        self.assertEqual(self.check(OTHER, "keep")["status"], "kept")
        self.assertEqual(self.rpc.thread["name"], INITIAL)
        self.assertEqual(self.check(DEVELOPMENT, "major")["status"], "updated")
        self.assertEqual(self.rpc.thread["name"], DEVELOPMENT)
        self.assertEqual(len(self.rpc.writes), 2)

    def test_initial_does_not_reword_an_already_managed_title(self):
        self.check()
        self.assertEqual(self.check(OTHER, "initial")["status"], "kept")
        self.assertEqual(self.rpc.thread["name"], INITIAL)
        self.assertEqual(len(self.rpc.writes), 1)

    def test_identical_candidate_does_not_make_redundant_write(self):
        self.check()
        self.assertEqual(self.check(INITIAL, "major")["status"], "kept")
        self.assertEqual(len(self.rpc.writes), 1)

    def test_manual_rename_is_detected_after_restart_and_preserved(self):
        self.check()
        self.rpc.thread["name"] = "用户指定的标题"
        self.manager = Manager(Store(Path(self.tmp.name)), self.rpc)
        self.assertEqual(self.check(DEVELOPMENT, "major")["status"], "locked")
        self.assertEqual(self.check(OTHER, "major")["status"], "locked")
        self.assertEqual(self.rpc.thread["name"], "用户指定的标题")
        self.assertEqual(len(self.rpc.writes), 1)

    def test_restore_uses_first_original_and_locks_automatic_changes(self):
        self.check()
        self.check(DEVELOPMENT, "major")
        self.manager = Manager(Store(Path(self.tmp.name)), self.rpc)
        result = self.manager.restore(THREAD_ID)
        self.assertEqual(result["status"], "restored")
        self.assertEqual(self.rpc.thread["name"], ORIGINAL)
        self.assertEqual(self.check(OTHER, "major")["status"], "locked")
        self.assertEqual(self.rpc.thread["name"], ORIGINAL)

    def test_unlock_adopts_current_manual_name_then_allows_major_change(self):
        self.check()
        self.rpc.thread["name"] = "用户指定的标题"
        self.assertEqual(self.check(DEVELOPMENT, "major")["status"], "locked")
        result = self.manager.unlock(THREAD_ID)
        self.assertEqual(result["title"], "用户指定的标题")
        self.assertEqual(self.check(DEVELOPMENT, "major")["status"], "updated")
        self.assertEqual(self.rpc.thread["name"], DEVELOPMENT)

    def test_old_turn_cannot_overwrite_title_or_complete_new_turn(self):
        self.check()
        self.pending()
        result = self.check(DEVELOPMENT, "major", check_id="old-turn")
        self.assertEqual(result["status"], "stale")
        self.assertEqual(self.rpc.thread["name"], INITIAL)
        self.assertFalse(self.store.load(THREAD_ID)["pending"]["checked"])
        self.assertEqual(self.check(DEVELOPMENT, "major", "current-turn")["status"], "updated")
        self.assertTrue(self.store.load(THREAD_ID)["pending"]["checked"])

    def test_unknown_or_already_consumed_check_cannot_write(self):
        self.assertEqual(self.check(check_id="unknown-turn")["status"], "stale")
        self.assertEqual(self.rpc.writes, [])
        self.pending()
        self.assertEqual(self.check(check_id="current-turn")["status"], "updated")
        self.assertEqual(self.check(OTHER, "major", "current-turn")["status"], "kept")
        self.assertEqual(len(self.rpc.writes), 1)

    def test_read_failure_has_no_side_effects(self):
        self.rpc.fail_read = True
        with self.assertRaises(RuntimeError):
            self.check()
        self.assertEqual(self.rpc.writes, [])
        self.assert_no_applied_history()

    def test_write_rejection_is_not_recorded_as_success_and_can_retry(self):
        self.rpc.fail_set = True
        with self.assertRaises(RuntimeError):
            self.check()
        self.assertEqual(self.rpc.thread["name"], ORIGINAL)
        self.assert_no_applied_history()
        self.rpc.fail_set = False
        self.assertEqual(self.check()["status"], "updated")
        self.manager.restore(THREAD_ID)
        self.assertEqual(self.rpc.thread["name"], ORIGINAL)

    def test_silent_remote_noop_does_not_produce_success_receipt(self):
        self.pending()
        self.rpc.ignore_set = True
        with self.assertRaises(RuntimeError):
            self.check(check_id="current-turn")
        self.assert_no_applied_history()
        self.assertFalse(self.store.load(THREAD_ID)["pending"]["checked"])
        self.assertEqual(self.rpc.thread["name"], ORIGINAL)

    def test_lost_write_response_recovers_after_restart_without_duplicate_write(self):
        self.rpc.fail_after_set = True
        with self.assertRaises(TimeoutError):
            self.check()
        self.assertEqual(self.rpc.thread["name"], INITIAL)
        self.assert_no_applied_history()
        self.rpc.fail_after_set = False
        self.manager = Manager(Store(Path(self.tmp.name)), self.rpc)
        self.assertEqual(self.check()["status"], "kept")
        self.assertEqual(len(self.rpc.writes), 1)
        self.manager.restore(THREAD_ID)
        self.assertEqual(self.rpc.thread["name"], ORIGINAL)

    def test_conflict_during_prewrite_read_does_not_overwrite_manual_name(self):
        reads = 0

        def rename_on_second_read(rpc):
            nonlocal reads
            reads += 1
            if reads == 2:
                rpc.thread["name"] = "用户刚刚改名"

        self.rpc.before_read = rename_on_second_read
        with self.assertRaises(RuntimeError):
            self.check()
        self.assertEqual(self.rpc.thread["name"], "用户刚刚改名")
        self.assertEqual(self.rpc.writes, [])
        self.assert_no_applied_history()

    def test_empty_default_title_waits_without_creating_unrestorable_change(self):
        self.rpc.thread["name"] = None
        self.assertEqual(self.check()["status"], "skipped")
        self.assertEqual(self.rpc.writes, [])
        self.assert_no_applied_history()

    def test_subagent_task_is_skipped(self):
        self.rpc.thread["parentThreadId"] = "66666666-7777-4888-8999-aaaaaaaaaaaa"
        self.assertEqual(self.check()["status"], "skipped")
        self.assertEqual(self.rpc.writes, [])

    def test_custom_source_is_not_confused_with_subagent_source(self):
        self.rpc.thread["source"] = {"custom": "desktop-integration"}
        self.assertEqual(self.check()["status"], "updated")
        self.rpc.thread["source"] = {"subAgent": "review"}
        self.assertEqual(self.check(DEVELOPMENT, "major")["status"], "skipped")
        self.assertEqual(len(self.rpc.writes), 1)

    def test_batch_manual_edit_between_preview_read_and_check_is_preserved(self):
        reads = 0

        def edit_after_initial_plan_check(rpc):
            nonlocal reads
            reads += 1
            if reads == 2:
                rpc.thread["name"] = "用户刚刚指定的标题"

        self.rpc.before_read = edit_after_initial_plan_check

        @contextlib.contextmanager
        def fake_server():
            yield self.rpc

        plan = Path(self.tmp.name) / "batch-plan.json"
        plan.write_text(json.dumps([{
            "thread_id": THREAD_ID, "expected_title": ORIGINAL,
            "title": INITIAL, "focus": "整理对话标题", "reason": "初次整理",
        }]))
        argv = ["title_manager.py", "batch", "--plan", str(plan), "--apply"]
        output = io.StringIO()
        with patch.object(title_manager, "Store", return_value=self.store), \
                patch.object(title_manager, "AppServer", fake_server), \
                patch.object(sys, "argv", argv), contextlib.redirect_stdout(output):
            title_manager.main()
        result = json.loads(output.getvalue())
        self.assertEqual(result["results"][0]["status"], "conflict")
        self.assertEqual(result["report"]["status"], "written")
        self.assertEqual(self.rpc.thread["name"], "用户刚刚指定的标题")
        self.assertEqual(self.rpc.writes, [])

    def test_app_server_startup_failure_prevents_stop_retry(self):
        from title_hook import handle

        self.pending(nonce="current-turn")

        @contextlib.contextmanager
        def failed_server():
            raise RuntimeError("initialize failed")
            yield  # pragma: no cover -- establishes the context-manager interface

        argv = [
            "title_manager.py", "--thread-id", THREAD_ID, "check",
            "--check-id", "current-turn", "--title", INITIAL,
            "--focus", "整理对话标题", "--change", "initial", "--reason", "初次整理",
        ]
        with patch.object(title_manager, "Store", return_value=self.store), \
                patch.object(title_manager, "AppServer", failed_server), \
                patch.object(sys, "argv", argv), \
                self.assertRaisesRegex(RuntimeError, "initialize failed"):
            title_manager.main()
        state = self.store.load(THREAD_ID)
        self.assertTrue(state["pending"]["checked"])
        self.assertEqual(state["pending"]["result"], "error")
        self.assertEqual(handle({
            "hook_event_name": "Stop", "session_id": THREAD_ID,
            "turn_id": "current-turn", "stop_hook_active": False,
        }, self.store), {})
        self.assertEqual(self.rpc.writes, [])

    def run_batch(self, entries, rpc):
        @contextlib.contextmanager
        def fake_server():
            yield rpc

        plan = Path(self.tmp.name) / "batch-plan.json"
        plan.write_text(json.dumps(entries))
        argv = ["title_manager.py", "batch", "--plan", str(plan), "--apply"]
        output = io.StringIO()
        with patch.object(title_manager, "Store", return_value=self.store), \
                patch.object(title_manager, "AppServer", fake_server), \
                patch.object(sys, "argv", argv), contextlib.redirect_stdout(output):
            title_manager.main()
        payload = json.loads(output.getvalue())
        self.assertTrue(Path(payload["report"]["path"]).is_file())
        return payload["results"]

    def batch_entry(self, tid, original):
        return {
            "thread_id": tid, "expected_title": original,
            "title": INITIAL, "focus": "整理对话标题", "reason": "初次整理",
        }

    def test_batch_validates_all_decisions_before_any_write(self):
        second_id = "66666666-7777-4888-8999-aaaaaaaaaaaa"
        for field, length in (("focus", 301), ("reason", 501)):
            with self.subTest(field=field):
                rpc = BatchRpc({THREAD_ID: ORIGINAL, second_id: "第二个原标题"})
                entries = [self.batch_entry(THREAD_ID, ORIGINAL), self.batch_entry(second_id, "第二个原标题")]
                entries[1][field] = "x" * length
                with self.assertRaises(ValueError):
                    self.run_batch(entries, rpc)
                self.assertEqual(rpc.writes, [])
                self.assertEqual(rpc.threads[THREAD_ID]["name"], ORIGINAL)

    def test_batch_returns_success_and_failure_and_continues_after_error(self):
        second_id = "66666666-7777-4888-8999-aaaaaaaaaaaa"
        third_id = "bbbbbbbb-cccc-4ddd-8eee-ffffffffffff"
        titles = {THREAD_ID: ORIGINAL, second_id: "第二个原标题", third_id: "第三个原标题"}
        rpc = BatchRpc(titles, denied=[second_id])
        result = self.run_batch([self.batch_entry(tid, title) for tid, title in titles.items()], rpc)
        self.assertEqual([entry["thread_id"] for entry in result], list(titles))
        self.assertEqual([entry["status"] for entry in result], ["updated", "error", "updated"])
        self.assertIn("Write denied", result[1]["message"])
        self.assertEqual(rpc.threads[THREAD_ID]["name"], INITIAL)
        self.assertEqual(rpc.threads[second_id]["name"], "第二个原标题")
        self.assertEqual(rpc.threads[third_id]["name"], INITIAL)

    def test_new_prompt_bypasses_slow_manager_lock_and_supersedes_old_write(self):
        from title_hook import handle

        def prompt(turn):
            return {
                "hook_event_name": "UserPromptSubmit", "session_id": THREAD_ID,
                "turn_id": turn, "prompt": "正常用户问题",
            }

        handle(prompt("old-turn"), self.store)
        inside_rpc = threading.Event()
        release_rpc = threading.Event()
        hook_finished = threading.Event()
        results, errors = {}, []
        reads = 0

        def slow_first_read(rpc):
            nonlocal reads
            reads += 1
            if reads == 1:
                inside_rpc.set()
                if not release_rpc.wait(3):
                    raise RuntimeError("Test did not release blocked RPC")

        self.rpc.before_read = slow_first_read

        def run_old_check():
            try:
                results["check"] = self.check(check_id="old-turn")
            except Exception as exc:
                errors.append(exc)

        def run_new_hook():
            try:
                results["hook"] = handle(prompt("new-turn"), Store(Path(self.tmp.name)))
            except Exception as exc:
                errors.append(exc)
            finally:
                hook_finished.set()

        manager_thread = threading.Thread(target=run_old_check, daemon=True)
        hook_thread = threading.Thread(target=run_new_hook, daemon=True)
        manager_thread.start()
        try:
            self.assertTrue(inside_rpc.wait(2), "Manager did not enter its locked RPC")
            hook_thread.start()
            self.assertTrue(hook_finished.wait(1), "New input was blocked by the Manager lock")
            self.assertEqual(self.store.load(THREAD_ID)["pending"]["id"], "new-turn")
        finally:
            release_rpc.set()
            manager_thread.join(2)
            if hook_thread.ident is not None:
                hook_thread.join(2)
        self.assertEqual(errors, [])
        self.assertFalse(manager_thread.is_alive())
        self.assertEqual(results["check"]["status"], "stale")
        self.assertEqual(self.rpc.writes, [])
        self.assertEqual(self.rpc.thread["name"], ORIGINAL)
        self.assertFalse(self.store.load(THREAD_ID)["pending"]["checked"])
        self.assertIn("--check-id new-turn", results["hook"]["hookSpecificOutput"]["additionalContext"])

    def test_old_keep_receipt_does_not_complete_new_prompt(self):
        from title_hook import handle

        def prompt(turn):
            return {
                "hook_event_name": "UserPromptSubmit", "session_id": THREAD_ID,
                "turn_id": turn, "prompt": "正常用户问题",
            }

        handle(prompt("old-turn"), self.store)
        self.rpc.before_read = lambda rpc: handle(prompt("new-turn"), self.store)
        self.assertEqual(self.check(change="keep", check_id="old-turn")["status"], "kept")
        pending = self.store.load(THREAD_ID)["pending"]
        self.assertEqual(pending["id"], "new-turn")
        self.assertFalse(pending["checked"])
        self.assertEqual(self.rpc.writes, [])


if __name__ == "__main__":
    unittest.main()
