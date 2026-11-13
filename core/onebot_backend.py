"""OneBot v11 群文件系统后端（最佳努力 / 防御式）。

调用 gocqhttp / Lagrange 等支持群文件系统的适配器提供的原始 action：
    get_group_root_files / get_group_files_by_folder
    get_group_file_url / upload_group_file
    create_group_file_folder / delete_group_file / move_group_file

AstrBot 的 Star 上下文通过 ``context.get_platform_adapter(platform)`` 取得适配器，
不同版本暴露原始 action 调用的方法名不同，这里做多重兼容尝试并全程防御，
任何失败都会抛出清晰的 RuntimeError（由上层捕获并提示用户检查适配器）。
"""

import time
from typing import List, Optional

from .file_backend import FileBackend, FileNode

# 常见 OneBot / QQ 适配器平台名，按顺序尝试（首个可取到的即绑定）。
# AstrBot 不同版本/不同适配器注册的平台名不同，这里尽量兼容。
PLATFORM_CANDIDATES = ["aiocqhttp", "onebot", "qq", "qq_official", "qqbot"]


class OneBotFileBackend(FileBackend):
    # 调用原始 action 时尝试的方法名（不同 AstrBot 版本/适配器命名不同）
    ACTION_METHODS = ("call_action", "_call_action", "send_action", "send")
    # 适配器上可能包裹 bot/client 的属性，action 方法挂在它们上面
    WRAPPER_ATTRS = ("bot", "client", "http", "api")

    def __init__(self, context, platform: str = "aiocqhttp"):
        self.context = context
        self.platform = platform
        self._adapter = None
        self._resolved_platform = ""
        self._tried_platforms: List[str] = []

    # ---------- 适配器与原始 action 调用 ----------
    def _get_one(self, plat: str):
        """通过 context.get_platform_adapter(plat) 取单个适配器（兼容方法缺失）。"""
        fn = getattr(self.context, "get_platform_adapter", None)
        if not callable(fn):
            return None
        try:
            return fn(plat)
        except Exception:
            return None

    def _discover_all(self) -> dict:
        """尽可能全面地枚举当前已注册的所有平台适配器。

        兼容多种 AstrBot 版本暴露适配器注册表的方式：
          - context.get_platform_adapters()  (返回 dict 或 list)
          - context.get_all_adapters() / get_adapters()
          - context.platform_adapters / _platform_adapters (内部 dict)
        返回 {平台名: 适配器对象}。
        """
        found: dict = {}
        # 1) 多取值函数
        for name in ("get_platform_adapters", "get_all_adapters", "get_adapters"):
            fn = getattr(self.context, name, None)
            if not callable(fn):
                continue
            try:
                res = fn()
            except Exception:
                continue
            if isinstance(res, dict):
                found.update(res)
            elif isinstance(res, (list, tuple)):
                for a in res:
                    nm = getattr(a, "platform", None) or getattr(a, "platform_name", None)
                    if nm:
                        found[str(nm)] = a
        # 2) 内部属性（dict）
        for attr in ("platform_adapters", "_platform_adapters", "adapters"):
            reg = getattr(self.context, attr, None)
            if isinstance(reg, dict):
                found.update(reg)
        # 3) 单平台取（配置的 + 常见候选），补全
        for plat in [self.platform] + [p for p in PLATFORM_CANDIDATES if p != self.platform]:
            if plat in found:
                continue
            ad = self._get_one(plat)
            if ad is not None:
                found[plat] = ad
        return found

    @staticmethod
    def _has_action(adapter) -> bool:
        """对象（或它的 bot/client 包装）是否具备某种 action 调用方法。"""
        if adapter is None:
            return False
        for meth in OneBotFileBackend.ACTION_METHODS:
            if callable(getattr(adapter, meth, None)):
                return True
        for attr in OneBotFileBackend.WRAPPER_ATTRS:
            sub = getattr(adapter, attr, None)
            if sub is not None and any(
                callable(getattr(sub, m, None)) for m in OneBotFileBackend.ACTION_METHODS
            ):
                return True
        return False

    def _invoke(self, adapter, action: str, **params):
        """对某个适配器尝试多种调用姿势，成功返回结果。"""
        if adapter is None:
            raise RuntimeError("adapter is None")
        # 直接方法
        for meth in self.ACTION_METHODS:
            fn = getattr(adapter, meth, None)
            if callable(fn):
                return fn(action, **params)
        # 经 bot / client 包装
        for attr in self.WRAPPER_ATTRS:
            sub = getattr(adapter, attr, None)
            if sub is None:
                continue
            for meth in self.ACTION_METHODS:
                fn = getattr(sub, meth, None)
                if callable(fn):
                    return fn(action, **params)
        if callable(adapter):
            return adapter(action, **params)
        raise RuntimeError("适配器无可用 action 调用方法")

    def diagnose(self) -> dict:
        """侦察：列出实际可用的平台名，以及每个适配器暴露的 action 方法。"""
        found = self._discover_all()
        details = {}
        for plat, adapter in found.items():
            methods = [m for m in self.ACTION_METHODS if callable(getattr(adapter, m, None))]
            sub_methods: dict = {}
            for attr in self.WRAPPER_ATTRS:
                sub = getattr(adapter, attr, None)
                if sub is not None:
                    sm = [m for m in self.ACTION_METHODS if callable(getattr(sub, m, None))]
                    if sm:
                        sub_methods[attr] = sm
            details[str(plat)] = {
                "type": type(adapter).__name__,
                "methods": methods,
                "wrapper_methods": sub_methods,
                "usable": bool(methods or sub_methods),
            }
        return {"platforms": list(found.keys()), "details": details}

    async def _get_adapter(self):
        if self._adapter is not None:
            return self._adapter
        found = self._discover_all()
        for plat, adapter in found.items():
            if self._has_action(adapter):
                self._adapter = adapter
                self._resolved_platform = plat
                return self._adapter
        return None

    async def _call(self, action: str, **params):
        import inspect

        found = self._discover_all()
        for plat, adapter in found.items():
            try:
                result = self._invoke(adapter, action, **params)
                if inspect.isawaitable(result):
                    result = await result
                return result
            except Exception:  # 该适配器调不动，换下一个
                continue
        # 仍失败：给出诊断信息，便于定位
        diag = self.diagnose()
        plat_line = ", ".join(diag["platforms"] or ["(无)"])
        raise RuntimeError(
            "无法获取/调用 QQ 群文件适配器。已注册平台：" + plat_line +
            "。请确认：1) 已接入支持群文件系统的 QQ 适配器（gocqhttp / Lagrange 等）且在线；"
            "2) 若仅离线测试，可在插件配置中将 BACKEND 设为 local；"
            "3) 若平台名特殊，可在配置 ONEBOT_PLATFORM 中指定。（细节：" + str(diag["details"]) + "）"
        )

    # ---------- 读取 ----------
    async def list_files(self, group_id: str, folder_id: str = "") -> List[FileNode]:
        data = await self._call(
            "get_group_files_by_folder", group_id=int(group_id), folder_id=folder_id
        )
        files = (data or {}).get("files", [])
        return [self._to_node(f, folder_id) for f in files if isinstance(f, dict)]

    async def list_folders(self, group_id: str, folder_id: str = "") -> List[FileNode]:
        data = await self._call(
            "get_group_files_by_folder", group_id=int(group_id), folder_id=folder_id
        )
        folders = (data or {}).get("folders", [])
        return [self._to_folder(f) for f in folders if isinstance(f, dict)]

    @staticmethod
    def _to_node(f: dict, parent_id: str) -> FileNode:
        # 失效判定：dead_time 存在且早于当前时间（且不为 0 表示永久）
        dead_time = f.get("dead_time", 0) or 0
        dead = bool(dead_time) and dead_time < int(time.time())
        return FileNode(
            id=f.get("file_id", ""),
            name=f.get("file_name", ""),
            is_folder=False,
            size=f.get("file_size", 0) or 0,
            parent_id=parent_id,
            busid=f.get("busid", 0) or 0,
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
        # 需要先拿到 busid：尝试从文件信息补充；OneBot 的 get_group_file_url 需要 busid
        info = await self.get_file_info(group_id, file_id)
        if info is None:
            return None
        if info.dead:
            return None
        try:
            data = await self._call(
                "get_group_file_url",
                group_id=int(group_id),
                file_id=file_id,
                busid=info.busid,
            )
        except RuntimeError:
            return None
        url = (data or {}).get("url", "")
        return url or None

    async def get_file_info(self, group_id: str, file_id: str) -> Optional[FileNode]:
        # OneBot 没有单文件查询，需遍历根 + 已知文件夹。这里采用轻量策略：
        # 仅从根目录列表里找；深层文件交给 manager 的递归扫描补齐。
        try:
            data = await self._call(
                "get_group_root_files", group_id=int(group_id)
            )
        except RuntimeError:
            return None
        for f in (data or {}).get("files", []):
            if f.get("file_id") == file_id:
                return self._to_node(f, "")
        return None

    # ---------- 写入 ----------
    async def create_folder(self, group_id: str, parent_id: str, name: str) -> Optional[str]:
        data = await self._call(
            "create_group_file_folder",
            group_id=int(group_id),
            folder_id=parent_id,
            folder_name=name,
        )
        return (data or {}).get("folder_id") or None

    async def move_file(self, group_id: str, file_id: str, new_folder_id: str) -> bool:
        # 需要先知道文件当前所在 folder_id；这里接受调用方传入时通过 file 的 parent 处理。
        # OneBot move 需要 folder_id（源）与 target_folder_id。为简化，尝试用 "" 根作为源。
        try:
            await self._call(
                "move_group_file",
                group_id=int(group_id),
                folder_id="",
                file_id=file_id,
                target_folder_id=new_folder_id,
            )
            return True
        except RuntimeError:
            return False

    async def delete_file(self, group_id: str, file_id: str) -> bool:
        try:
            await self._call(
                "delete_group_file",
                group_id=int(group_id),
                folder_id="",
                file_id=file_id,
            )
            return True
        except RuntimeError:
            return False

    async def download_file(self, group_id: str, file_id: str, dest_path: str) -> bool:
        import os

        url = await self.get_file_url(group_id, file_id)
        if not url:
            return False
        try:
            import aiohttp

            os.makedirs(os.path.dirname(dest_path), exist_ok=True)
            async with aiohttp.ClientSession() as session:
                async with session.get(url) as resp:
                    if resp.status != 200:
                        return False
                    with open(dest_path, "wb") as fp:
                        fp.write(await resp.read())
            return True
        except Exception:
            return False

    async def upload_file(self, group_id: str, folder_id: str, local_path: str) -> Optional[str]:
        import os

        if not os.path.isfile(local_path):
            return None
        try:
            data = await self._call(
                "upload_group_file",
                group_id=int(group_id),
                file=os.path.abspath(local_path),
                name=os.path.basename(local_path),
                folder=folder_id,
            )
            return (data or {}).get("file_id") or None
        except RuntimeError:
            return None
