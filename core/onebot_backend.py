"""OneBot v11 群文件系统后端（基于 event.bot.api.call_action）。

关键：AstrBot 插件里调用原始 OneBot action 的正确姿势不是
``context.get_platform_adapter(字符串)``，而是：
  - 每条消息事件 ``event.bot`` 即 aiocqhttp 客户端，具备 ``.api.call_action``；
  - 或 ``context.get_platform(filter.PlatformAdapterType.AIOCQHTTP).get_client()``。
本后端同时支持两者：指令触发时用 event 绑定，后台任务用 context 绑定。

协议端兼容要点（2026 实测 QQNT/NapCat 系）：
  - ``get_group_file_url`` / ``delete_group_file`` 常**必需** ``busid``，缺它报
    ``retcode=1400 / Expected required property``；busid 来自列目录返回的文件条目。
  - ``move_group_file`` 参数方言有三套：NapCat(``current_parent_directory``/
    ``target_parent_directory``)、部分 NapCat 文档(``target_dir``)、
    Lagrange/LLOneBot(``parent_directory``/``target_directory``)。
因此对文件级 action 统一做「带 busid + 多方言」自动重试，首个成功即采用。

参考实现：https://github.com/Foolllll-J/astrbot_plugin_GroupFS
"""

import os
import time
from typing import Dict, List, Optional

from .file_backend import FileBackend, FileNode


def _normalize(result) -> dict:
    """兼容 OneBot 返回值被 {'data': {...}} 包裹的情况（部分协议端如此）。

    明确失败的响应（status=failed）保持原样，避免解包后丢失失败标志。
    """
    if not isinstance(result, dict):
        return {}
    if result.get("status") == "failed":
        return result
    if isinstance(result.get("data"), dict):
        return result.get("data", {})
    return result


class OneBotFileBackend(FileBackend):
    def __init__(self, context, platform: str = "aiocqhttp"):
        self.context = context
        self.platform = platform
        self._bot = None                 # aiocqhttp 客户端，具备 .api.call_action
        self._bound_from = ""            # 绑定来源，供诊断
        self._last_error = ""           # 最近一次绑定错误，供诊断
        self._is_llbot = False           # LLOneBot / SnowLuma 参数略有差异
        self._app_name = ""
        self._detected = False
        self.last_move_error = ""       # 最近一次移动失败的真实原因，供回执/诊断
        self.last_url_error = ""        # 最近一次「取下载链接」失败的真实原因
        self.last_download_error = ""   # 最近一次下载失败的真实原因
        self.last_delete_error = ""     # 最近一次删除失败的真实原因
        self._busid_cache: Dict[str, int] = {}   # file_id -> busid（列目录时填充）
        self.bind_from_context()

    # ---------- 绑定 OneBot 客户端 ----------
    @staticmethod
    def _valid(client) -> bool:
        """参考插件 _is_supported_bot_client：需具备 .api.call_action。"""
        return bool(
            client
            and hasattr(client, "api")
            and hasattr(client.api, "call_action")
        )

    def bind_event(self, event) -> bool:
        """从消息事件绑定（最可靠：能进消息说明 bot 一定在线）。"""
        cand = getattr(event, "bot", None)
        if self._valid(cand):
            if cand is not self._bot:
                self._bot = cand
                self._detected = False
            self._bound_from = "event.bot"
            return True
        return False

    def bind_from_context(self) -> bool:
        """后台任务（无 event）时用 context 的 platform manager 绑定。"""
        if self._valid(self._bot):
            return True
        try:
            from astrbot.api.event import filter as _af
            ptype = getattr(getattr(_af, "PlatformAdapterType", None), "AIOCQHTTP", None)
            getter = getattr(self.context, "get_platform", None)
            if callable(getter) and ptype is not None:
                plat = getter(ptype)
                client = plat.get_client() if hasattr(plat, "get_client") else None
                if self._valid(client):
                    self._bot = client
                    self._detected = False
                    self._bound_from = "context.get_platform(AIOCQHTTP).get_client()"
                    return True
        except Exception as e:  # 离线/版本差异时静默，交由 event 绑定
            self._last_error = "get_platform: " + str(e)
        return False

    async def ensure_ready(self, event=None) -> bool:
        """指令执行前调用：绑定 bot 并探测协议端类型。"""
        if event is not None:
            self.bind_event(event)
        self.bind_from_context()
        if not self._valid(self._bot):
            return False
        if not self._detected:
            try:
                info = _normalize(await self._bot.api.call_action("get_version_info"))
                self._app_name = info.get("app_name", "") if isinstance(info, dict) else ""
                self._is_llbot = self._app_name in ("LLOneBot", "SnowLuma")
            except Exception:
                self._is_llbot = False
                self._app_name = ""
            self._detected = True
        return True

    def is_ready(self) -> bool:
        return self._valid(self._bot)

    def diagnose(self) -> dict:
        """诊断：输出真实绑定状态与可用接口，便于定位。"""
        return {
            "bound": self._valid(self._bot),
            "source": self._bound_from or "(未绑定)",
            "app_name": self._app_name,
            "is_llbot": self._is_llbot,
            "platform": self.platform,
            "last_error": self._last_error,
            "last_move_error": self.last_move_error,
            "last_url_error": self.last_url_error,
            "last_download_error": self.last_download_error,
            "busid_known": len(self._busid_cache),
            "has_get_platform": callable(getattr(self.context, "get_platform", None)),
            "has_get_platform_adapter": callable(
                getattr(self.context, "get_platform_adapter", None)
            ),
        }

    async def probe(self, group_id: str) -> dict:
        """真实探测：版本信息 + 列根目录 + 实测一次「取下载链接」。"""
        out = dict(self.diagnose())
        out["probe"] = {}
        if not self._valid(self._bot):
            out["probe"]["error"] = "尚未绑定 bot，请在群内触发一次指令"
            return out
        try:
            ver = _normalize(await self._bot.api.call_action("get_version_info"))
            out["probe"]["version"] = ver
        except Exception as e:
            out["probe"]["version_error"] = str(e)
        try:
            data = await self._list_dir(str(group_id), "")
            files = data.get("files", []) or []
            out["probe"]["root_files"] = len(files)
            out["probe"]["root_folders"] = len(data.get("folders", []) or [])
            out["probe"]["sample"] = [
                f.get("file_name") for f in files[:3] if isinstance(f, dict)
            ]
            # 实测「取下载链接」——归类兜底/永久保存能否工作的关键
            if files and isinstance(files[0], dict):
                f0 = files[0]
                fid = str(f0.get("file_id") or "")
                self._remember([f0])
                out["probe"]["sample_busid"] = f0.get("busid")
                try:
                    url = await self.get_file_url(str(group_id), fid)
                    out["probe"]["file_url_ok"] = bool(url)
                    out["probe"]["file_url_error"] = self.last_url_error
                except Exception as e:  # noqa
                    out["probe"]["file_url_error"] = str(e)
        except Exception as e:
            out["probe"]["list_error"] = str(e)
        return out

    # ---------- 原始 action 调用 ----------
    async def _call(self, action: str, **params) -> dict:
        self.bind_from_context()
        if not self._valid(self._bot):
            raise RuntimeError(
                "尚未绑定 OneBot 客户端（event.bot / context.get_platform）。"
                "请在群聊中触发一次指令，或确认已接入 aiocqhttp 适配器且在线。"
            )
        try:
            result = await self._bot.api.call_action(action, **params)
        except Exception as e:
            raise RuntimeError(action + " 调用失败: " + str(e))
        return _normalize(result)

    async def _call_variants(self, action: str, base: dict, variants: List[dict]):
        """按序尝试多套参数方言，返回 (成功?, 数据, 错误列表)。

        每个变体：抛异常 -> 记为失败继续；返回体判定为失败 -> 记为失败继续；
        否则视为成功并立即返回。用于兼容 busid / 目录参数名等协议端差异。
        """
        errors: List[str] = []
        for extra in variants:
            try:
                data = await self._call(action, **{**base, **extra})
            except RuntimeError as e:
                errors.append(str(e))
                continue
            if not self._action_ok(data):
                errors.append(action + " 响应判定失败: " + str(data)[:160])
                continue
            return True, data, errors
        return False, {}, errors

    # ---------- busid 缓存 ----------
    def _remember(self, files: list):
        """从列目录返回的文件条目里缓存 file_id -> busid。"""
        for f in files or []:
            if not isinstance(f, dict):
                continue
            fid = str(f.get("file_id") or "")
            if not fid:
                continue
            try:
                self._busid_cache[fid] = int(f.get("busid") or 0)
            except (TypeError, ValueError):
                self._busid_cache[fid] = 0

    def _busid_of(self, file_id: str) -> int:
        return int(self._busid_cache.get(str(file_id), 0) or 0)

    # ---------- 读取 ----------
    async def _list_dir(self, group_id: str, folder_id: str) -> dict:
        """根目录用 get_group_root_files，子目录用 get_group_files_by_folder。"""
        gid = int(group_id)
        if not folder_id or folder_id == "/":
            args = {"group_id": gid}
            if not self._is_llbot:
                args["file_count"] = 2000
            data = await self._call("get_group_root_files", **args)
        else:
            args = {"group_id": gid, "folder_id": folder_id}
            if not self._is_llbot:
                args["file_count"] = 2000
            data = await self._call("get_group_files_by_folder", **args)
        self._remember(data.get("files", []) or [])
        return data

    async def list_files(self, group_id: str, folder_id: str = "") -> List[FileNode]:
        data = await self._list_dir(group_id, folder_id)
        return [
            self._to_node(f, folder_id)
            for f in (data.get("files", []) or [])
            if isinstance(f, dict)
        ]

    async def list_folders(self, group_id: str, folder_id: str = "") -> List[FileNode]:
        data = await self._list_dir(group_id, folder_id)
        return [
            self._to_folder(f)
            for f in (data.get("folders", []) or [])
            if isinstance(f, dict)
        ]

    @staticmethod
    def _to_node(f: dict, parent_id: str) -> FileNode:
        dead_time = f.get("dead_time", 0) or 0
        dead = bool(dead_time) and dead_time < int(time.time())
        try:
            busid = int(f.get("busid") or 0)
        except (TypeError, ValueError):
            busid = 0
        return FileNode(
            id=f.get("file_id", ""),
            name=f.get("file_name", ""),
            is_folder=False,
            size=f.get("file_size", 0) or 0,
            parent_id=parent_id,
            busid=busid,
            dead=dead,
        )

    @staticmethod
    def _to_folder(f: dict) -> FileNode:
        return FileNode(
            id=f.get("folder_id", ""),
            name=f.get("folder_name", ""),
            is_folder=True,
            size=0,
            parent_id=f.get("parent_folder_id", ""),
        )

    async def get_file_url(self, group_id: str, file_id: str) -> Optional[str]:
        """取下载链接。NapCat/QQNT 系必需 busid，故「带 busid」优先、再退回不带。"""
        self.last_url_error = ""
        base = {"group_id": int(group_id), "file_id": file_id}
        busid = self._busid_of(file_id)
        variants: List[dict] = []
        if busid:
            variants.append({"busid": busid})
        variants.append({})  # LLOneBot / 旧协议端不需要 busid
        errors: List[str] = []
        for extra in variants:
            try:
                data = await self._call("get_group_file_url", **{**base, **extra})
            except RuntimeError as e:
                errors.append(str(e))
                continue
            url = (data or {}).get("url")
            if url:
                return url
            errors.append("get_group_file_url 响应无 url: " + str(data)[:160])
        self.last_url_error = " | ".join(dict.fromkeys(errors))[:500]
        return None

    async def get_file_info(self, group_id: str, file_id: str) -> Optional[FileNode]:
        """递归查找单个文件节点（OneBot 无单文件查询接口）。"""
        visited = set()

        async def walk(fid: str, depth: int = 0):
            if depth > 8 or fid in visited:
                return None
            visited.add(fid)
            try:
                data = await self._list_dir(group_id, fid)
            except RuntimeError:
                return None
            for f in data.get("files", []) or []:
                if isinstance(f, dict) and f.get("file_id") == file_id:
                    return self._to_node(f, fid)
            for fo in data.get("folders", []) or []:
                if isinstance(fo, dict) and fo.get("folder_id"):
                    hit = await walk(fo["folder_id"], depth + 1)
                    if hit:
                        return hit
            return None

        return await walk("")

    # ---------- 写入 ----------
    async def create_folder(self, group_id: str, parent_id: str, name: str) -> Optional[str]:
        # NapCat/OB11 用 folder_name；LLOneBot 用 name
        key = "name" if self._is_llbot else "folder_name"
        try:
            data = await self._call(
                "create_group_file_folder", group_id=int(group_id), **{key: name}
            )
        except RuntimeError:
            return None
        fid = (data or {}).get("folder_id")
        if fid:
            return fid
        # 部分协议端不返回 folder_id，回查目录
        try:
            folders = await self.list_folders(group_id, "")
            for fo in folders:
                if fo.name == name:
                    return fo.id
        except RuntimeError:
            pass
        return None

    @staticmethod
    def _action_ok(data) -> bool:
        """判定 action 响应是否真成功，防止「协议端返回失败但没抛异常」被误判为成功。"""
        if not isinstance(data, dict) or not data:
            return True  # 部分协议端成功时无返回体
        if data.get("status") == "failed" or data.get("ok") is False:
            return False
        rc = data.get("retcode")
        if rc is not None and str(rc) not in ("0", ""):
            return False
        return True

    async def move_file(self, group_id: str, file_id: str, new_folder_id: str) -> bool:
        """移动文件：覆盖三套目录参数方言 + busid 变体，首个成功即返回。"""
        self.last_move_error = ""
        tid = new_folder_id or "/"
        napcat = {"current_parent_directory": "/", "target_parent_directory": tid}
        target_dir = {"target_dir": tid}
        llonebot = {"parent_directory": "/", "target_directory": tid}
        styles = [llonebot, target_dir, napcat] if self._is_llbot else [napcat, target_dir, llonebot]
        busid = self._busid_of(file_id)
        variants: List[dict] = []
        for st in styles:
            variants.append(dict(st))
            if busid:
                variants.append({**st, "busid": busid})
        ok, _, errors = await self._call_variants(
            "move_group_file", {"group_id": int(group_id), "file_id": file_id}, variants
        )
        if ok:
            return True
        self.last_move_error = " | ".join(dict.fromkeys(errors))[:600] or "未知原因"
        return False

    async def delete_file(self, group_id: str, file_id: str) -> bool:
        self.last_delete_error = ""
        busid = self._busid_of(file_id)
        variants: List[dict] = []
        if busid:
            variants.append({"busid": busid})
        variants.append({})
        ok, _, errors = await self._call_variants(
            "delete_group_file", {"group_id": int(group_id), "file_id": file_id}, variants
        )
        if ok:
            return True
        self.last_delete_error = " | ".join(dict.fromkeys(errors))[:400] or "未知原因"
        return False

    async def download_file(self, group_id: str, file_id: str, dest_path: str) -> bool:
        self.last_download_error = ""
        url = await self.get_file_url(group_id, file_id)
        if not url:
            self.last_download_error = "取下载链接失败：" + (self.last_url_error or "无 url")
            return False
        try:
            import aiohttp

            os.makedirs(os.path.dirname(dest_path), exist_ok=True)
            headers = {"User-Agent": "Mozilla/5.0 (astrbot-qqfilemgr)"}
            timeout = aiohttp.ClientTimeout(total=90)
            async with aiohttp.ClientSession(timeout=timeout) as session:
                async with session.get(
                    url, headers=headers, allow_redirects=True
                ) as resp:
                    if resp.status != 200:
                        self.last_download_error = f"HTTP {resp.status} @ 下载链接"
                        return False
                    with open(dest_path, "wb") as fp:
                        fp.write(await resp.read())
            return True
        except Exception as e:
            self.last_download_error = "下载异常: " + str(e)[:200]
            return False

    async def upload_file(self, group_id: str, folder_id: str, local_path: str) -> Optional[str]:
        if not os.path.isfile(local_path):
            return None
        common = {
            "group_id": int(group_id),
            "file": f"file://{os.path.abspath(local_path)}",
            "name": os.path.basename(local_path),
        }
        if self._is_llbot:
            params = {**common, "folder_id": folder_id or "/"}
        else:
            params = {
                **common,
                "folder": folder_id or "/",
                "folder_id": folder_id or "/",
                "timeout": 300,
            }
        try:
            data = await self._call("upload_group_file", **params)
        except RuntimeError as e:
            self.last_move_error = "upload_group_file 调用失败: " + str(e)
            return None
        if not self._action_ok(data):
            self.last_move_error = "upload_group_file 响应判定失败: " + str(data)[:200]
            return None
        return (data or {}).get("file_id") or "uploaded"

    async def relocate_by_reupload(
        self, group_id: str, node: FileNode, target_folder_id: str
    ) -> bool:
        """协议端不支持移动时的兜底：下载 → 上传到目标目录 → 删除原文件。

        参考 astrbot_plugin_GroupFS 的同步链路。只有上传确认成功后才删除
        源文件，任何一步失败都保留原文件，避免数据丢失。
        """
        import shutil
        import tempfile

        # 兜底入口显式补一次 busid（防止该节点未经列目录、缓存缺失）
        if node.busid:
            self._busid_cache[str(node.id)] = int(node.busid or 0)
        tmpdir = tempfile.mkdtemp(prefix="qqfile_relocate_")
        tmp = os.path.join(tmpdir, node.name or ("file_" + str(node.id)))
        try:
            if not await self.download_file(group_id, node.id, tmp):
                self.last_move_error = (
                    "兜底失败：无法下载源文件（"
                    + (self.last_download_error or "未知")
                    + "）；上次移动错误："
                    + (self.last_move_error or "无")
                )
                return False
            if not await self.upload_file(group_id, target_folder_id, tmp):
                self.last_move_error = (
                    "兜底失败：上传到目标文件夹未成功，源文件已保留；"
                    + (self.last_move_error or "")
                )
                return False
            if not await self.delete_file(group_id, node.id):
                # 上传已成功，删除源失败只造成重复，不丢数据
                self.last_move_error = (
                    "兜底部分成功：已上传到目标文件夹，但删除原文件失败"
                    "（可能出现重复文件）；"
                    + (getattr(self, "last_delete_error", "") or "")
                )
                return True
            self.last_move_error = ""
            return True
        finally:
            shutil.rmtree(tmpdir, ignore_errors=True)
