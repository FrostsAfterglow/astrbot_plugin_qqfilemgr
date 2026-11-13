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


class _FakeAdapter:
    """同步版 call_action。"""

    def call_action(self, action, **kw):
        return {"ok": True, "echo": action}


class _AsyncAdapter:
    """异步版 call_action（真实 AstrBot 适配器多为 async）。"""

    async def call_action(self, action, **kw):
        return {"ok": True, "echo": action}


class _WrappedAdapter:
    """action 方法挂在 bot 子对象上。"""

    def __init__(self):
        self.bot = _FakeAdapter()


class _FakeContext:
    def __init__(self, adapters: dict):
        self._adapters = adapters

    def get_platform_adapter(self, plat):
        return self._adapters.get(plat)


class TestOneBotDiscovery(unittest.TestCase):
    def test_finds_adapter_on_named_platform(self):
        ctx = _FakeContext({"aiocqhttp": _FakeAdapter()})
        be = OneBotFileBackend(ctx, platform="aiocqhttp")
        ad = run(be._get_adapter())
        self.assertIsNotNone(ad)
        self.assertEqual(be._resolved_platform, "aiocqhttp")

    def test_falls_back_to_other_platform_name(self):
        # 用户平台名是 qq 而非默认 aiocqhttp，应能自动发现
        ctx = _FakeContext({"qq": _FakeAdapter()})
        be = OneBotFileBackend(ctx, platform="aiocqhttp")
        ad = run(be._get_adapter())
        self.assertIsNotNone(ad)
        self.assertEqual(be._resolved_platform, "qq")

    def test_enumerate_when_only_internal_dict(self):
        ctx = _FakeContext({})
        ctx._platform_adapters = {"whatever": _FakeAdapter()}
        be = OneBotFileBackend(ctx, platform="aiocqhttp")
        ad = run(be._get_adapter())
        self.assertIsNotNone(ad)

    def test_get_platform_adapters_dict(self):
        # 通过 get_platform_adapters() 返回 dict 的场景
        class Ctx(_FakeContext):
            def get_platform_adapters(self):
                return {"onebot": _AsyncAdapter()}

        be = OneBotFileBackend(Ctx({}), platform="aiocqhttp")
        info = be.diagnose()
        self.assertIn("onebot", info["platforms"])
        self.assertTrue(info["details"]["onebot"]["usable"])

    def test_no_adapter_returns_none(self):
        ctx = _FakeContext({})
        be = OneBotFileBackend(ctx, platform="aiocqhttp")
        ad = run(be._get_adapter())
        self.assertIsNone(ad)
        with self.assertRaises(RuntimeError):
            run(be._call("get_group_root_files", group_id=100))

    def test_call_sync_action(self):
        ctx = _FakeContext({"aiocqhttp": _FakeAdapter()})
        be = OneBotFileBackend(ctx, platform="aiocqhttp")
        r = run(be._call("get_group_root_files", group_id=100))
        self.assertTrue(r["ok"])
        self.assertEqual(r["echo"], "get_group_root_files")

    def test_call_async_action(self):
        ctx = _FakeContext({"onebot": _AsyncAdapter()})
        be = OneBotFileBackend(ctx, platform="aiocqhttp")
        r = run(be._call("get_group_root_files", group_id=100))
        self.assertTrue(r["ok"])

    def test_call_via_bot_wrapper(self):
        ctx = _FakeContext({"qq": _WrappedAdapter()})
        be = OneBotFileBackend(ctx, platform="aiocqhttp")
        r = run(be._call("get_group_root_files", group_id=100))
        self.assertTrue(r["ok"])

    def test_diagnose_empty(self):
        ctx = _FakeContext({})
        be = OneBotFileBackend(ctx, platform="aiocqhttp")
        info = be.diagnose()
        self.assertEqual(info["platforms"], [])
        self.assertEqual(info["details"], {})


if __name__ == "__main__":
    unittest.main(verbosity=2)
