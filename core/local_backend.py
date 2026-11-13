"""本地文件系统镜像后端。

把「群文件系统」映射到一个本地目录树：
    <base>/<group_id>/...    （根目录即群文件根）
节点 id = 相对根目录的路径；文件夹 = 子目录；文件 = 文件。

该后端让插件在**不连接 QQ** 的情况下也能完整演示三大功能，且天然可测。
真实联网场景下应使用 OneBotFileBackend。
"""

import json
import shutil
from pathlib import Path
from typing import Dict, List, Optional, Set

from .file_backend import FileBackend, FileNode


class LocalBackend(FileBackend):
    def __init__(self, base_dir: str):
        self.base = Path(base_dir)
        self.base.mkdir(parents=True, exist_ok=True)
        # dead 标记：(group_id, file_id) -> True，持久化到各群目录下的 .dead.json
        self._dead: Dict[str, Set[str]] = {}
        self._load_dead()

    # ---------- 持久化 dead 标记 ----------
    def _dead_file(self, group_id: str) -> Path:
        return self._group_dir(group_id) / ".dead.json"

    def _load_dead(self):
        for p in self.base.glob("*"):
            if not p.is_dir():
                continue
            df = p / ".dead.json"
            if df.is_file():
                try:
                    self._dead[p.name] = set(json.loads(df.read_text(encoding="utf-8")))
                except (json.JSONDecodeError, OSError):
                    self._dead[p.name] = set()

    def _save_dead(self, group_id: str):
        df = self._dead_file(group_id)
        df.write_text(
            json.dumps(sorted(self._dead.get(group_id, set())), ensure_ascii=False),
            encoding="utf-8",
        )

    # ---------- 路径工具 ----------
    def _group_dir(self, group_id: str) -> Path:
        d = self.base / str(group_id)
        d.mkdir(parents=True, exist_ok=True)
        return d

    def _node_from_path(self, group_id: str, path: Path) -> FileNode:
        gd = self._group_dir(group_id)
        rel = path.relative_to(gd).as_posix()
        is_folder = path.is_dir()
        size = 0 if is_folder else path.stat().st_size
        parent = path.parent
        parent_id = "" if parent == gd else parent.relative_to(gd).as_posix()
        return FileNode(
            id=rel,
            name=path.name,
            is_folder=is_folder,
            size=size,
            parent_id=parent_id,
            dead=self._is_dead(group_id, rel),
        )

    def _is_dead(self, group_id: str, file_id: str) -> bool:
        return file_id in self._dead.get(group_id, set())

    def mark_dead(self, group_id: str, file_id: str):
        """（测试/模拟用）标记某文件为失效。"""
        self._dead.setdefault(group_id, set()).add(file_id)
        self._save_dead(group_id)

    # ---------- 读取 ----------
    async def list_files(self, group_id: str, folder_id: str = "") -> List[FileNode]:
        gd = self._group_dir(group_id)
        root = gd / folder_id if folder_id else gd
        if not root.is_dir():
            return []
        out = []
        for p in sorted(root.iterdir()):
            if p.name.startswith("."):
                continue
            if p.is_file():
                out.append(self._node_from_path(group_id, p))
        return out

    async def list_folders(self, group_id: str, folder_id: str = "") -> List[FileNode]:
        gd = self._group_dir(group_id)
        root = gd / folder_id if folder_id else gd
        if not root.is_dir():
            return []
        out = []
        for p in sorted(root.iterdir()):
            if p.name.startswith("."):
                continue
            if p.is_dir():
                out.append(self._node_from_path(group_id, p))
        return out

    async def get_file_url(self, group_id: str, file_id: str) -> Optional[str]:
        if self._is_dead(group_id, file_id):
            return None
        gd = self._group_dir(group_id)
        target = gd / file_id
        if not target.is_file():
            return None
        return f"file://{target.resolve().as_posix()}"

    async def get_file_info(self, group_id: str, file_id: str) -> Optional[FileNode]:
        gd = self._group_dir(group_id)
        target = gd / file_id
        if not target.exists():
            return None
        return self._node_from_path(group_id, target)

    # ---------- 写入 ----------
    async def create_folder(self, group_id: str, parent_id: str, name: str) -> Optional[str]:
        gd = self._group_dir(group_id)
        parent = gd / parent_id if parent_id else gd
        new_dir = parent / name
        if new_dir.exists():
            return new_dir.relative_to(gd).as_posix()
        try:
            new_dir.mkdir(parents=True, exist_ok=True)
        except OSError:
            return None
        return new_dir.relative_to(gd).as_posix()

    async def move_file(self, group_id: str, file_id: str, new_folder_id: str) -> bool:
        gd = self._group_dir(group_id)
        src = gd / file_id
        if not src.is_file():
            return False
        dest_dir = gd / new_folder_id if new_folder_id else gd
        dest_dir.mkdir(parents=True, exist_ok=True)
        try:
            shutil.move(str(src), str(dest_dir / src.name))
        except OSError:
            return False
        # 同步 dead 标记到新 id
        new_id = (dest_dir / src.name).relative_to(gd).as_posix()
        dead_set = self._dead.get(group_id, set())
        if file_id in dead_set:
            dead_set.discard(file_id)
            dead_set.add(new_id)
            self._save_dead(group_id)
        return True

    async def delete_file(self, group_id: str, file_id: str) -> bool:
        gd = self._group_dir(group_id)
        target = gd / file_id
        if not target.is_file():
            return False
        try:
            target.unlink()
        except OSError:
            return False
        self._dead.get(group_id, set()).discard(file_id)
        self._save_dead(group_id)
        return True

    async def download_file(self, group_id: str, file_id: str, dest_path: str) -> bool:
        gd = self._group_dir(group_id)
        src = gd / file_id
        if not src.is_file():
            return False
        try:
            Path(dest_path).parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(str(src), dest_path)
        except OSError:
            return False
        return True

    async def upload_file(self, group_id: str, folder_id: str, local_path: str) -> Optional[str]:
        gd = self._group_dir(group_id)
        dest_dir = gd / folder_id if folder_id else gd
        dest_dir.mkdir(parents=True, exist_ok=True)
        src = Path(local_path)
        if not src.is_file():
            return None
        try:
            shutil.copyfile(str(src), str(dest_dir / src.name))
        except OSError:
            return None
        return (dest_dir / src.name).relative_to(gd).as_posix()
