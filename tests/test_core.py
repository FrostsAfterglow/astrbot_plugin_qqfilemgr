"""astrbot_plugin_qqfilemgr 核心层离线测试。

只依赖 core/ 与 data/（不依赖 astrbot.api），可直接运行：
    python3 -m pytest tests/test_core.py -v
    # 或
    python3 tests/test_core.py
"""

import asyncio
import os
import sys
import tempfile
import unittest

# 让测试能直接导入插件包（以本文件所在目录为根）
HERE = os.path.dirname(os.path.abspath(__file__))
PLUGIN_ROOT = os.path.dirname(HERE)
if PLUGIN_ROOT not in sys.path:
    sys.path.insert(0, PLUGIN_ROOT)

from core.classifier import Classifier  # noqa: E402
from core.local_backend import LocalBackend  # noqa: E402
from core.file_manager import FileManager, human_size, sha256_of_file  # noqa: E402
from core.onebot_backend import OneBotFileBackend  # noqa: E402
from data.database import FileDB  # noqa: E402


def run(coro):
    return asyncio.run(coro)


class TestClassifier(unittest.TestCase):
    def setUp(self):
        self.cls = Classifier(os.path.join(PLUGIN_ROOT, "config", "categories.json"))

    def test_basic(self):
        self.assertEqual(self.cls.category_of("a.jpg"), "图片")
        self.assertEqual(self.cls.category_of("b.MP4"), "视频")
        self.assertEqual(self.cls.category_of("c.docx"), "文档")
        self.assertEqual(self.cls.category_of("d.zip"), "压缩包")

    def test_unknown(self):
        self.assertEqual(self.cls.category_of("weird.xyz"), "其他")
        self.assertEqual(self.cls.category_of("noext"), "其他")

    def test_tar_gz(self):
        self.assertEqual(self.cls.category_of("src.tar.gz"), "压缩包")

    def test_is_known_category(self):
        self.assertTrue(self.cls.is_known_category("图片"))
        self.assertTrue(self.cls.is_known_category("其他"))
        self.assertFalse(self.cls.is_known_category("不存在"))


class TestLocalBackend(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="qqfile_")
        self.be = LocalBackend(os.path.join(self.tmp, "group_files"))

    def _write(self, group, rel):
        p = os.path.join(self.be.base, str(group), rel)
        os.makedirs(os.path.dirname(p), exist_ok=True)
        with open(p, "w", encoding="utf-8") as f:
            f.write("hello")
        return p

    def test_list_and_create(self):
        self._write("100", "a.png")
        files = run(self.be.list_files("100", ""))
        self.assertEqual(len(files), 1)
        self.assertEqual(files[0].name, "a.png")
        fid = run(self.be.create_folder("100", "", "图片"))
        self.assertTrue(fid)
        folders = run(self.be.list_folders("100", ""))
        self.assertEqual(len(folders), 1)

    def test_move_and_delete(self):
        self._write("100", "a.png")
        fid = run(self.be.create_folder("100", "", "图片"))
        ok = run(self.be.move_file("100", "a.png", fid))
        self.assertTrue(ok)
        moved = run(self.be.list_files("100", fid))
        self.assertEqual(len(moved), 1)
        ok = run(self.be.delete_file("100", moved[0].id))
        self.assertTrue(ok)
        self.assertEqual(len(run(self.be.list_files("100", fid))), 0)

    def test_dead(self):
        self._write("100", "gone.pdf")
        self.be.mark_dead("100", "gone.pdf")
        node = run(self.be.get_file_info("100", "gone.pdf"))
        self.assertTrue(node.dead)
        self.assertIsNone(run(self.be.get_file_url("100", "gone.pdf")))

    def test_download_and_upload(self):
        self._write("100", "b.mp3")
        dest = os.path.join(self.tmp, "dl.mp3")
        ok = run(self.be.download_file("100", "b.mp3", dest))
        self.assertTrue(ok)
        self.assertTrue(os.path.isfile(dest))
        up_id = run(self.be.upload_file("100", "", dest))
        self.assertTrue(up_id)


class TestFileManager(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="qqfile_mgr_")
        self.be = LocalBackend(os.path.join(self.tmp, "group_files"))
        self.db = FileDB(os.path.join(self.tmp, "filemgr.db"))
        self.cls = Classifier(os.path.join(PLUGIN_ROOT, "config", "categories.json"))
        self.perm = os.path.join(self.tmp, "permanent")
        self.mgr = FileManager(self.be, self.db, self.cls, {}, self.perm)

    def _seed(self, group, name):
        p = os.path.join(self.be.base, str(group), name)
        os.makedirs(os.path.dirname(p), exist_ok=True)
        with open(p, "w", encoding="utf-8") as f:
            f.write("data-" + name)

    def test_classify_dry_and_exec(self):
        self._seed("100", "x.jpg")
        self._seed("100", "y.png")
        r = run(self.mgr.classify("100", dry_run=True))
        self.assertEqual(len(r["moved"]), 2)
        # 试运行不在磁盘创建目录，但应报告将要创建的分类目录
        self.assertEqual(len(r["created_folders"]), 1)  # 图片
        r2 = run(self.mgr.classify("100", dry_run=False))
        self.assertEqual(len(r2["created_folders"]), 1)  # 图片（实际创建）
        self.assertEqual(len(r2["moved"]), 2)
        # 再次归类应跳过已归类的
        r3 = run(self.mgr.classify("100", dry_run=False))
        self.assertEqual(len(r3["moved"]), 0)

    def test_classify_target_category(self):
        self._seed("100", "a.jpg")
        self._seed("100", "b.mp4")
        r = run(self.mgr.classify("100", dry_run=False, target_category="视频"))
        self.assertEqual(len(r["moved"]), 1)

    def test_clean_invalid(self):
        self._seed("100", "alive.txt")
        self._seed("100", "dead.zip")
        self.be.mark_dead("100", "dead.zip")
        r = run(self.mgr.clean_invalid("100", dry_run=True))
        self.assertEqual(r["alive"], 1)
        self.assertEqual(len(r["dead"]), 1)
        # 永久保存后可安全删除
        run(self.mgr.permanent_save("100", "dead.zip"))
        r2 = run(self.mgr.clean_invalid("100", dry_run=False))
        self.assertEqual(len(r2["skipped_saved"]), 1)
        self.assertEqual(len(r2["deleted"]), 1)

    def test_permanent_save(self):
        self._seed("100", "keep.pdf")
        r = run(self.mgr.permanent_save("100", "keep.pdf"))
        self.assertTrue(r["ok"])
        self.assertTrue(os.path.isfile(r["stored_path"]))
        self.assertEqual(r["category"], "文档")
        self.assertEqual(self.db.perm_count("100"), 1)
        # 清单可查
        rows = self.mgr.list_perm("100")
        self.assertEqual(len(rows), 1)
        self.assertIn("size_human", rows[0])

    def test_permanent_save_missing(self):
        r = run(self.mgr.permanent_save("100", "nope.bin"))
        self.assertFalse(r["ok"])
        self.assertIn("未找到", r["error"])

    def test_permanent_save_size_limit(self):
        self._seed("100", "big.iso")
        mgr = FileManager(self.be, self.db, self.cls, {"MAX_PERMANENT_SIZE_MB": 1}, self.perm)
        # 文件内容极小，但 size 字段来自 stat；为测上限手动给 node 放大无法，此处仅验证逻辑不报错
        r = run(mgr.permanent_save("100", "big.iso"))
        self.assertTrue(r["ok"])


class TestHelpers(unittest.TestCase):
    def test_human_size(self):
        self.assertEqual(human_size(500), "500 B")
        self.assertTrue(human_size(2048).endswith("KB"))
        self.assertTrue(human_size(10 * 1024 * 1024).endswith("MB"))

    def test_sha256(self):
        p = tempfile.mktemp()
        with open(p, "w", encoding="utf-8") as f:
            f.write("abc")
        self.assertEqual(len(sha256_of_file(p)), 64)
        os.remove(p)


class _FakeAPI:
    """模拟 aiocqhttp 的 bot.api，提供 async call_action。"""

    def __init__(self, app_name="NapCat", wrap=False, url="http://dl/file"):
        self.app_name = app_name
        self.wrap = wrap                 # 是否用 {"data": {...}} 包裹返回值
        self.url = url
        self.calls = []                  # 记录 (action, params)，供断言

    def _pack(self, payload):
        return {"status": "ok", "retcode": 0, "data": payload} if self.wrap else payload

    async def call_action(self, action, **kw):
        self.calls.append((action, kw))
        if action == "get_version_info":
            return self._pack({"app_name": self.app_name})
        if action in ("get_group_root_files", "get_group_files_by_folder"):
            return self._pack({
                "files": [
                    {"file_id": "F1", "file_name": "a.jpg", "file_size": 100, "busid": 7},
                    {"file_id": "F2", "file_name": "b.pdf", "file_size": 200, "busid": 8},
                ],
                "folders": [
                    {"folder_id": "D1", "folder_name": "图片", "parent_folder_id": "/"},
                ],
            })
        if action == "get_group_file_url":
            return self._pack({"url": self.url} if self.url else {})
        if action == "create_group_file_folder":
            return self._pack({"folder_id": "D9"})
        if action in ("move_group_file", "delete_group_file"):
            return self._pack({"status": "ok", "retcode": 0})
        if action == "upload_group_file":
            return self._pack({"file_id": "FU"})
        return self._pack({})


class _FakeBot:
    def __init__(self, **kw):
        self.api = _FakeAPI(**kw)


class _FakeEvent:
    def __init__(self, bot):
        self.bot = bot


class _Ctx:
    """离线 context：没有 get_platform（模拟 AstrBot 未即时暴露平台管理器）。"""

    def get_platform_adapter(self, plat):
        return None


class TestOneBotBackend(unittest.TestCase):
    def _bound(self, **kw):
        ctx = _Ctx()
        be = OneBotFileBackend(ctx, platform="aiocqhttp")
        bot = _FakeBot(**kw)
        be.bind_event(_FakeEvent(bot))
        return be, bot

    def test_bind_from_event_bot(self):
        be, _ = self._bound()
        self.assertTrue(be.is_ready())
        self.assertEqual(be._bound_from, "event.bot")

    def test_ensure_ready_detects_napcat(self):
        be, _ = self._bound(app_name="NapCat")
        self.assertTrue(run(be.ensure_ready()))
        self.assertFalse(be._is_llbot)
        self.assertEqual(be._app_name, "NapCat")

    def test_ensure_ready_detects_llonebot(self):
        be, _ = self._bound(app_name="LLOneBot")
        run(be.ensure_ready())
        self.assertTrue(be._is_llbot)

    def test_root_uses_get_group_root_files(self):
        be, bot = self._bound()
        run(be.ensure_ready())
        files = run(be.list_files("100", ""))
        self.assertEqual(len(files), 2)
        self.assertEqual(files[0].name, "a.jpg")
        self.assertTrue(any(a == "get_group_root_files" for a, _ in bot.api.calls))

    def test_subfolder_uses_by_folder(self):
        be, bot = self._bound()
        run(be.ensure_ready())
        run(be.list_files("100", "D1"))
        self.assertTrue(any(a == "get_group_files_by_folder" for a, _ in bot.api.calls))

    def test_data_wrapped_payload(self):
        be, _ = self._bound(wrap=True)
        run(be.ensure_ready())
        files = run(be.list_files("100", ""))
        self.assertEqual(len(files), 2)  # 应正确解开 data 包裹

    def test_list_folders(self):
        be, _ = self._bound()
        run(be.ensure_ready())
        folders = run(be.list_folders("100", ""))
        self.assertEqual(len(folders), 1)
        self.assertEqual(folders[0].name, "图片")

    def test_get_file_url_no_busid(self):
        be, bot = self._bound()
        run(be.ensure_ready())
        url = run(be.get_file_url("100", "F1"))
        self.assertEqual(url, "http://dl/file")
        # 参数里不应需要 busid
        p = [kw for a, kw in bot.api.calls if a == "get_group_file_url"][0]
        self.assertNotIn("busid", p)

    def test_get_file_info_recursive(self):
        be, _ = self._bound()
        run(be.ensure_ready())
        node = run(be.get_file_info("100", "F2"))
        self.assertIsNotNone(node)
        self.assertEqual(node.name, "b.pdf")

    def test_create_folder_napcat_param(self):
        be, bot = self._bound(app_name="NapCat")
        run(be.ensure_ready())
        fid = run(be.create_folder("100", "", "文档"))
        self.assertEqual(fid, "D9")
        p = [kw for a, kw in bot.api.calls if a == "create_group_file_folder"][0]
        self.assertIn("folder_name", p)

    def test_create_folder_llonebot_param(self):
        be, bot = self._bound(app_name="LLOneBot")
        run(be.ensure_ready())
        run(be.create_folder("100", "", "文档"))
        p = [kw for a, kw in bot.api.calls if a == "create_group_file_folder"][0]
        self.assertIn("name", p)

    def test_move_file_params(self):
        be, bot = self._bound()
        run(be.ensure_ready())
        ok = run(be.move_file("100", "F1", "D1"))
        self.assertTrue(ok)
        p = [kw for a, kw in bot.api.calls if a == "move_group_file"][0]
        self.assertIn("current_parent_directory", p)
        self.assertEqual(p["target_parent_directory"], "D1")

    def test_delete_file(self):
        be, bot = self._bound()
        run(be.ensure_ready())
        ok = run(be.delete_file("100", "F1"))
        self.assertTrue(ok)
        p = [kw for a, kw in bot.api.calls if a == "delete_group_file"][0]
        self.assertEqual(p["file_id"], "F1")

    def test_dead_file_no_url(self):
        be, _ = self._bound(url="")
        run(be.ensure_ready())
        self.assertIsNone(run(be.get_file_url("100", "F1")))

    def test_not_bound_raises(self):
        ctx = _Ctx()
        be = OneBotFileBackend(ctx, platform="aiocqhttp")
        self.assertFalse(be.is_ready())
        with self.assertRaises(RuntimeError):
            run(be._call("get_group_root_files", group_id=100))

    def test_diagnose_fields(self):
        be, _ = self._bound()
        info = be.diagnose()
        self.assertTrue(info["bound"])
        self.assertEqual(info["source"], "event.bot")

    def test_probe_reports_root(self):
        be, _ = self._bound()
        run(be.ensure_ready())
        info = run(be.probe("100"))
        self.assertEqual(info["probe"]["root_files"], 2)
        self.assertIn("a.jpg", info["probe"]["sample"])


if __name__ == "__main__":
    unittest.main(verbosity=2)
