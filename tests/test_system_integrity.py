"""系统性回归：文件事务、真实并发、取消收尾及安全重下（离线）。"""

import asyncio
import os
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
from support import prepare_offline_f2
prepare_offline_f2()
from douyin_tool import config, core, storage, web_app as web
from f2.apps.douyin import dl, handler

__test__ = False


def wait_finished(tid):
    deadline = time.monotonic() + 5
    while time.monotonic() < deadline:
        task = web._get(tid)
        if task["status"] not in ("pending", "running"):
            return task
        time.sleep(0.02)
    raise AssertionError("任务没有及时结束")


class PersistenceTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.history = self.root / "history.json"

    def test_parallel_history_updates_keep_every_record(self):
        def write(i):
            config.mark_downloaded(self.root, "one", {str(i): self.root / f"{i}.mp4"}, self.history)
        with ThreadPoolExecutor(max_workers=12) as pool:
            list(pool.map(write, range(120)))
        self.assertEqual(len(config.load_history(self.history)["one"]), 120)

    def test_parallel_config_updates_keep_every_field(self):
        with patch.object(config, "get_config_path", return_value=self.root / "config.yaml"):
            with ThreadPoolExecutor(max_workers=8) as pool:
                list(pool.map(lambda i: config.update_config(**{f"field_{i}": i}), range(60)))
            cfg = config.load_config()
        self.assertTrue(all(cfg[f"field_{i}"] == i for i in range(60)))

    def test_cross_process_updates_keep_every_record(self):
        code = (
            "import sys; from pathlib import Path; from douyin_tool import config; "
            "root=Path(sys.argv[1]); prefix=sys.argv[2]; "
            "[config.mark_downloaded(root,'one',{prefix+str(i):root/(prefix+str(i)+'.mp4')},"
            "root/'history.json') for i in range(25)]"
        )
        children = [subprocess.Popen([sys.executable, "-X", "utf8", "-c", code,
                                     str(self.root), f"p{i}_"], cwd=ROOT,
                                    stdout=subprocess.PIPE, stderr=subprocess.PIPE) for i in range(4)]
        try:
            for child in children:
                _, err = child.communicate(timeout=30)
                self.assertEqual(child.returncode, 0, err.decode("utf-8", errors="replace"))
        finally:
            for child in children:
                if child.poll() is None:
                    child.kill()
                child.wait()
        self.assertEqual(len(config.load_history(self.history)["one"]), 100)

    def test_failed_replace_keeps_old_file_and_removes_temp(self):
        self.history.write_text("old", encoding="utf-8")
        with patch.object(storage.os, "replace", side_effect=PermissionError):
            with self.assertRaises(storage.PersistenceError):
                storage.atomic_write(self.history, "new")
        self.assertEqual(self.history.read_text(), "old")
        self.assertEqual(list(self.root.glob("*.tmp")), [])

    def test_failed_config_save_is_not_reported_as_success(self):
        with patch.object(config, "update_config", side_effect=storage.PersistenceError("test")):
            response = web.app.test_client().post("/api/config", json={"download_dir": "Download"})
        self.assertEqual(response.status_code, 500)
        self.assertFalse(response.get_json()["ok"])


class TaskTests(unittest.TestCase):
    def setUp(self):
        with web._TASKS_LOCK:
            web._TASKS.clear()

    def test_cancel_running_transfer_runs_cleanup(self):
        started, cleaned = threading.Event(), threading.Event()

        async def transfer(cb):
            started.set()
            try:
                await asyncio.sleep(30)
            finally:
                cleaned.set()
        tid = web._new_task("one", "test")
        web._run_in_thread(tid, transfer)
        self.assertTrue(started.wait(2))
        response = web.app.test_client().post(f"/api/cancel/{tid}")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(wait_finished(tid)["status"], "cancelled")
        self.assertTrue(cleaned.is_set())

    def test_cancel_pending_task_does_not_start_download(self):
        called = threading.Event()
        async def transfer(cb):
            called.set()
            return {}
        web._DOWNLOAD_SLOT.acquire()
        try:
            tid = web._new_task("one", "test")
            web._run_in_thread(tid, transfer)
            web.app.test_client().post(f"/api/cancel/{tid}")
            self.assertEqual(wait_finished(tid)["status"], "cancelled")
            self.assertFalse(called.is_set())
        finally:
            web._DOWNLOAD_SLOT.release()

    def test_failure_releases_download_slot(self):
        async def broken(cb):
            raise core.DouyinError("test failure")
        tid = web._new_task("one", "test")
        web._run_in_thread(tid, broken)
        self.assertEqual(wait_finished(tid)["status"], "failed")
        self.assertTrue(web._DOWNLOAD_SLOT.acquire(timeout=2))
        web._DOWNLOAD_SLOT.release()

    def test_downloads_are_serialized(self):
        active = peak = 0
        async def transfer(cb):
            nonlocal active, peak
            active += 1
            peak = max(peak, active)
            await asyncio.sleep(0.05)
            active -= 1
            return {}
        ids = [web._new_task("one", "test") for _ in range(4)]
        for tid in ids:
            web._run_in_thread(tid, transfer)
        for tid in ids:
            self.assertEqual(wait_finished(tid)["status"], "done")
        self.assertEqual(peak, 1)

    def test_queue_has_limit(self):
        for _ in range(web.MAX_ACTIVE_TASKS):
            web._new_task("one", "test")
        with self.assertRaises(web.TaskQueueFull):
            web._new_task("one", "overflow")

    def test_finished_task_retention_has_limit(self):
        for _ in range(web.MAX_FINISHED_TASKS + 10):
            tid = web._new_task("one", "test")
            web._update(tid, status="done")
        self.assertLessEqual(len(web._TASKS), web.MAX_FINISHED_TASKS + 1)

    def test_cancel_missing_or_finished_does_not_mutate_task(self):
        client = web.app.test_client()
        self.assertEqual(client.post("/api/cancel/missing").status_code, 404)
        tid = web._new_task("one", "test")
        web._update(tid, status="done", message="完成")
        self.assertEqual(client.post(f"/api/cancel/{tid}").status_code, 409)
        self.assertEqual(web._get(tid)["message"], "完成")

    def test_all_failed_batch_is_failed(self):
        async def transfer(cb):
            return {"failed": [{"error": "test"}], "download_count": 0, "skipped": []}
        tid = web._new_task("post", "test")
        web._run_in_thread(tid, transfer)
        self.assertEqual(wait_finished(tid)["status"], "failed")


class ForcedDownloadTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.user_dir = self.root / "author"
        self.user_dir.mkdir()
        self.old = self.user_dir / "target_video.mp4"
        self.old.write_bytes(b"old-content")
        self.mode = "success"
        self.started = threading.Event()
        self.closed = False
        owner = self

        class Downloader:
            def __init__(self, kwargs):
                self.download_tasks = []
            async def handler_download(self, kwargs, data, folder):
                self.folder = folder
            async def execute_tasks(self):
                owner.started.set()
                if owner.mode == "failure":
                    raise core.DouyinError("transfer failed")
                if owner.mode == "empty":
                    return
                if owner.mode == "slow":
                    await asyncio.sleep(30)
                if not (self.folder / "target_video.mp4").exists():
                    (self.folder / "target_video.mp4").write_bytes(b"new-content")
            async def close(self):
                owner.closed = True
        self.addCleanup(patch.stopall)
        patch.object(dl, "DouyinDownloader", Downloader).start()
        patch.object(core, "expected_media_paths", side_effect=lambda folder, *a: [folder / "target_video.mp4"]).start()

    def save(self, force=True, stop=None):
        return asyncio.run(core._save_video({"_force": force, "_should_stop": stop}, {}, self.root,
                                            nickname="author", desc=""))

    def assert_clean(self):
        self.assertTrue(self.closed)
        self.assertEqual(list(self.user_dir.glob(".redownload-*")), [])

    def test_force_replaces_old_content(self):
        self.assertEqual(Path(self.save()), self.old)
        self.assertEqual(self.old.read_bytes(), b"new-content")
        self.assert_clean()

    def test_regular_download_reuses_existing_file(self):
        self.save(force=False)
        self.assertEqual(self.old.read_bytes(), b"old-content")

    def test_failed_force_keeps_old_content(self):
        self.mode = "failure"
        with self.assertRaises(core.DouyinError):
            self.save()
        self.assertEqual(self.old.read_bytes(), b"old-content")
        self.assert_clean()

    def test_empty_force_is_not_false_success(self):
        self.mode = "empty"
        self.assertIsNone(self.save())
        self.assertEqual(self.old.read_bytes(), b"old-content")
        self.assert_clean()

    def test_cancel_force_keeps_old_content_and_closes_downloader(self):
        self.mode = "slow"
        with self.assertRaises(core.DownloadCancelled):
            self.save(stop=self.started.is_set)
        self.assertEqual(self.old.read_bytes(), b"old-content")
        self.assert_clean()

    def test_single_empty_download_raises(self):
        fake_handler = unittest.mock.Mock()
        fake_handler.downloader = unittest.mock.Mock(download_tasks=[], close=unittest.mock.AsyncMock())
        fake_handler.fetch_one_video = unittest.mock.AsyncMock(return_value=unittest.mock.Mock(
            _to_dict=lambda: {"aweme_id": "123456789", "nickname": "author", "desc": "test"}))
        with patch.object(handler, "DouyinHandler", return_value=fake_handler), \
             patch.object(core, "_save_video", new=unittest.mock.AsyncMock(return_value=None)):
            with self.assertRaises(core.DouyinError):
                asyncio.run(core.download_one("123456789", {"download_dir": str(self.root)}, skip_downloaded=False))
        fake_handler.downloader.close.assert_awaited_once()


class LoginTests(unittest.TestCase):
    def setUp(self):
        self.original = web._login_get()
        web._login_set(status="idle")
        self.addCleanup(lambda: web._login_set(**self.original))
        self.addCleanup(web._LOGIN_CANCEL.clear)

    def wait_login(self):
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline:
            state = web._login_get()
            if state["status"] not in web._LOGIN_BUSY:
                return state
            time.sleep(0.02)
        self.fail("登录线程没有结束")

    def test_concurrent_login_starts_only_one_browser(self):
        release = threading.Event()
        def fetch(**kwargs):
            release.wait(3)
            return "sessionid=test", "test"
        with patch.object(web.ck, "detect_browser", return_value=("chrome", "fake")), \
             patch.object(web.bl, "fetch_cookie", side_effect=fetch) as browser, \
             patch.object(config, "update_config"):
            try:
                with ThreadPoolExecutor(max_workers=8) as pool:
                    statuses = list(pool.map(lambda _: web.app.test_client().post(
                        "/api/login/start", json={"interactive": True}).status_code, range(8)))
                self.assertEqual(statuses.count(202), 1)
                self.assertEqual(statuses.count(409), 7)
            finally:
                release.set()
                self.wait_login()
            self.assertEqual(browser.call_count, 1)

    def test_cancelled_login_does_not_save_late_cookie(self):
        release = threading.Event()
        def fetch(**kwargs):
            release.wait(3)
            return "sessionid=test", "test"
        with patch.object(web.ck, "detect_browser", return_value=("chrome", "fake")), \
             patch.object(web.bl, "fetch_cookie", side_effect=fetch), \
             patch.object(config, "update_config") as save:
            try:
                web.app.test_client().post("/api/login/start", json={"interactive": True})
                web.app.test_client().post("/api/login/cancel")
            finally:
                release.set()
                state = self.wait_login()
            self.assertEqual(state["status"], "cancelled")
            save.assert_not_called()

    def test_login_save_failure_is_reported(self):
        with patch.object(web.ck, "detect_browser", return_value=("chrome", "fake")), \
             patch.object(web.bl, "fetch_cookie", return_value=("sessionid=test", "test")), \
             patch.object(config, "update_config", side_effect=storage.PersistenceError("test")):
            web.app.test_client().post("/api/login/start", json={"interactive": True})
            state = self.wait_login()
        self.assertEqual(state["status"], "failed")
        self.assertFalse(state["logged_in"])


class ApiValidationTests(unittest.TestCase):
    def test_application_factory_registers_routes(self):
        application = web.create_app()
        self.assertIsNot(application, web.app)
        self.assertIn("/api/alive", {rule.rule for rule in application.url_map.iter_rules()})

    def test_user_id_prefix_does_not_bypass_domain_check(self):
        for raw in ("MS4wLjAB https://127.0.0.1/", "MS4wLjAB https://evil.example/",
                    "MS4wLjABinvalid https://evil.example/"):
            self.assertIsNotNone(web._check_url(raw))
        self.assertIsNone(web._check_url("MS4wLjABabcdefghijklmnop123456"))

    def test_invalid_inputs_rejected_before_task_creation(self):
        client = web.app.test_client()
        bad = [{"mode": "unknown"}, {"force": "false"}, {"max_counts": -1},
               {"max_counts": 1.5}, {"date_start": "2026-02-30"},
               {"date_start": "2026-10-06", "date_end": "2026-01-01"}, {"date_end": []}]
        for data in bad:
            with self.subTest(data=data):
                response = client.post("/api/download", json={"url": "123456789", **data})
                self.assertEqual(response.status_code, 400)

    def test_non_object_json_and_malformed_json_rejected(self):
        client = web.app.test_client()
        for body in ('[]', '"text"', 'null', '{'):
            self.assertEqual(client.post("/api/config", data=body,
                                        content_type="application/json").status_code, 400)

    def test_unsafe_templates_rejected(self):
        client = web.app.test_client()
        for naming in ('../{desc}', '{unknown}', '{nickname.__class__}', '{desc', '{desc:1000}'):
            self.assertEqual(client.post("/api/config", json={"naming": naming}).status_code, 400)

    def test_external_origin_and_rebound_host_rejected(self):
        client = web.app.test_client()
        self.assertEqual(client.post("/api/reset-history", headers={"Origin": "https://evil.example"}).status_code, 403)
        self.assertEqual(client.get("/api/config", headers={"Host": "evil.example"}).status_code, 403)

    def test_cookie_value_not_returned(self):
        secret = "sessionid=private_value_" * 5
        with patch.object(config, "load_config", return_value={"cookie": secret}):
            response = web.app.test_client().get("/api/config")
        self.assertNotIn("private_value", response.get_data(as_text=True))


if __name__ == "__main__":
    unittest.main(verbosity=2)
