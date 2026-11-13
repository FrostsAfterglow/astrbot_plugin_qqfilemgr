"""文件后端抽象层。

定义统一的群文件系统视图 ``FileNode`` 与操作接口 ``FileBackend``。
- ``LocalFileBackend``：基于本地目录树实现（可离线运行、可测）。
- ``OneBotFileBackend``：调用 OneBot v11 群文件 API（需要 gocqhttp/Lagrange 等支持群文件系统的适配器）。

管理器（file_manager）只依赖本层抽象，不关心底层是 QQ 群文件系统还是本地镜像。
"""

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import List, Optional


@dataclass
class FileNode:
    """群文件系统中的一个节点（文件或文件夹）。"""

    id: str
    name: str
    is_folder: bool = False
    size: int = 0
    parent_id: str = ""
    busid: int = 0              # OneBot 群文件 busid（获取下载地址需要）
    # 业务派生字段（由 manager 填充 / 读取）
    category: str = ""          # 分类（图片/视频/...），仅文件有效
    dead: bool = False          # 是否已失效（无法获取下载地址）
    url: str = ""               # 下载地址（文件有效时）

    @property
    def ext(self) -> str:
        if self.is_folder:
            return ""
        name = self.name.lower()
        if "." not in name:
            return ""
        return name.rsplit(".", 1)[-1]

    def as_dict(self) -> dict:
        return {
            "id": self.id,
            "name": self.name,
            "is_folder": self.is_folder,
            "size": self.size,
            "parent_id": self.parent_id,
            "category": self.category,
            "dead": self.dead,
            "url": self.url,
            "busid": self.busid,
        }


class FileBackend(ABC):
    """群文件系统操作接口。"""

    # ---------- 读取 ----------

    @abstractmethod
    async def list_files(self, group_id: str, folder_id: str = "") -> List[FileNode]:
        """列出指定文件夹下的文件（不含子文件夹）。"""

    @abstractmethod
    async def list_folders(self, group_id: str, folder_id: str = "") -> List[FileNode]:
        """列出指定文件夹下的子文件夹。"""

    @abstractmethod
    async def get_file_url(self, group_id: str, file_id: str) -> Optional[str]:
        """获取文件下载地址；失效/不可用时返回 None。"""

    @abstractmethod
    async def get_file_info(self, group_id: str, file_id: str) -> Optional[FileNode]:
        """获取单个文件节点信息。"""

    # ---------- 写入 ----------

    @abstractmethod
    async def create_folder(self, group_id: str, parent_id: str, name: str) -> Optional[str]:
        """在指定父目录下创建文件夹，返回新文件夹 id；失败返回 None。"""

    @abstractmethod
    async def move_file(self, group_id: str, file_id: str, new_folder_id: str) -> bool:
        """将文件移动到目标文件夹。"""

    @abstractmethod
    async def delete_file(self, group_id: str, file_id: str) -> bool:
        """删除文件。"""

    @abstractmethod
    async def download_file(self, group_id: str, file_id: str, dest_path: str) -> bool:
        """下载文件到本地 dest_path。"""

    @abstractmethod
    async def upload_file(self, group_id: str, folder_id: str, local_path: str) -> Optional[str]:
        """上传本地文件到指定文件夹，返回新文件 id；失败返回 None。"""

    # ---------- 工具 ----------

    async def find_folder_by_name(
        self, group_id: str, name: str, parent_id: str = ""
    ) -> Optional[FileNode]:
        """在指定父目录下查找同名文件夹（不存在返回 None）。"""
        folders = await self.list_folders(group_id, parent_id)
        for f in folders:
            if f.name == name:
                return f
        return None

    async def find_file_by_name(
        self, group_id: str, name: str, folder_id: str = ""
    ) -> Optional[FileNode]:
        files = await self.list_files(group_id, folder_id)
        for f in files:
            if f.name == name:
                return f
        return None
