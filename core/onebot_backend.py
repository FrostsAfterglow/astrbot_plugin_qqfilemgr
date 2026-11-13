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


class OneBotFileBackend(FileBackend):
    def __init__(self, context, platform: str = "aiocqhttp"):
        self.context = context
        self.platform = platform
        self._adapter = None

    # ---------- 适配器与原始 action 调用 ----------
    async def _get_adapter(self):
        if self._adapter is None:
            try:
                self._adapter = self.context.get_platform_adapter(self.platform)
            except Exception:
                self._adapter = None
        return self._adapter

    async def _call(self, action: str, **params):
        adapter = await self._get_adapter()
        if adapter is None:
            raise RuntimeError(
                "无法获取 OneBot 适配器，请确认已接入支持群文件系统的 QQ 适配器"
                "（如 gocqhttp / Lagrange），且平台名为 " + self.platform
            )
        # 兼容不同 AstrBot 版本暴露的原始 action 调用方式
        for meth in ("call_action", "_call_action"):
            fn = getattr(adapter, meth, None)
            if callable(fn):
                return await fn(action, **params)
        if callable(adapter):
            return await adapter(action, **params)
        raise RuntimeError("适配器不支持原始 action 调用（call_action / _call_action）。")

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
