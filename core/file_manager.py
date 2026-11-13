"""文件管理核心：归类 / 失效清理 / 永久保存 三大功能编排。

所有功能只依赖 ``FileBackend`` 抽象与 ``FileDB`` 持久层，
不关心底层是 QQ 群文件系统还是本地镜像。
"""

import asyncio
import hashlib
import os
from typing import Dict, List, Optional

from .classifier import Classifier
from .file_backend import FileNode


def human_size(num: int) -> str:
    """把字节数换算成人类可读字符串。"""
    if num < 1024:
        return f"{num} B"
    for unit in ("KB", "MB", "GB", "TB"):
        num /= 1024.0
        if num < 1024:
            return f"{num:.1f} {unit}"
    return f"{num:.1f} PB"


def sha256_of_file(path: str) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(65536), b""):
            h.update(chunk)
    return h.hexdigest()


class FileManager:
    def __init__(
        self,
        backend,
        db,
        classifier: Classifier,
        config: dict,
        perm_dir: str,
    ):
        self.backend = backend
        self.db = db
        self.classifier = classifier
        self.config = config or {}
        self.perm_dir = perm_dir

    # ---------- 递归扫描 ----------
    async def scan(self, group_id: str, folder_id: str = "", _depth: int = 0) -> List[FileNode]:
        """递归列出群文件系统中所有文件（不包含文件夹）。"""
        out: List[FileNode] = []
        files = await self.backend.list_files(group_id, folder_id)
        folders = await self.backend.list_folders(group_id, folder_id)
        for f in files:
            f.category = self.classifier.category_of(f.name)
            out.append(f)
            # 更新索引快照
            self.db.upsert_file_index(group_id, f)
        for fo in folders:
            out.extend(await self.scan(group_id, fo.id, _depth + 1))
        return out

    # ---------- 功能一：自动位移文件归类 ----------
    async def classify(
        self, group_id: str, dry_run: bool = True, target_category: Optional[str] = None
    ) -> Dict:
        """把群文件根目录下的文件按扩展名移动到对应分类文件夹。

        - 仅在**根目录**层面归类，避免对已归类文件反复移动。
        - 分类文件夹不存在则创建（根目录下的「图片/视频/...」）。
        - ``target_category`` 非空时只处理该类别。
        - ``dry_run=True`` 仅报告，不落盘。
        """
        report = {
            "group_id": group_id,
            "dry_run": dry_run,
            "created_folders": [],
            "moved": [],
            "skipped": [],
            "errors": [],
            "fallback_used": 0,
            "error_detail": "",
            "warnings": [],
        }
        root_files = await self.backend.list_files(group_id, "")
        root_folders = {f.name: f for f in await self.backend.list_folders(group_id, "")}

        for node in root_files:
            if node.is_folder:
                continue
            cat = self.classifier.category_of(node.name)
            if target_category and cat != target_category:
                continue
            # 已在该分类目录下则跳过
            if node.parent_id == cat or node.name.startswith(cat + "/"):
                report["skipped"].append({"name": node.name, "reason": "已归类"})
                continue

            folder_node = root_folders.get(cat)
            if folder_node is None:
                if dry_run:
                    # 试运行仅报告将创建的分类目录，不在磁盘上真实创建
                    if cat not in report["created_folders"]:
                        report["created_folders"].append(cat)
                    report["moved"].append(
                        {"name": node.name, "category": cat, "action": "将移动(试运行)"}
                    )
                    continue
                new_id = await self.backend.create_folder(group_id, "", cat)
                if not new_id:
                    report["errors"].append({"name": node.name, "reason": "创建分类目录失败"})
                    continue
                folder_node = type(node)(id=new_id, name=cat, is_folder=True)
                root_folders[cat] = folder_node
                report["created_folders"].append(cat)

            if dry_run:
                report["moved"].append(
                    {"name": node.name, "category": cat, "action": "将移动(试运行)"}
                )
                continue

            ok = await self.backend.move_file(group_id, node.id, folder_node.id)
            used_fallback = False
            if (
                not ok
                and self.config.get("ENABLE_MOVE_FALLBACK", True)
                and hasattr(self.backend, "relocate_by_reupload")
            ):
                # 协议端不支持 move_group_file 时，走「下载→上传→删除」兜底
                ok = await self.backend.relocate_by_reupload(group_id, node, folder_node.id)
                used_fallback = bool(ok)
            if ok:
                report["moved"].append({"name": node.name, "category": cat})
                if used_fallback:
                    report["fallback_used"] += 1
                    # 兜底里「上传成功但删除源失败」会留警告，需透传给用户
                    warn = str(getattr(self.backend, "last_move_error", "") or "")
                    if warn and warn not in report["warnings"]:
                        report["warnings"].append(warn)
            else:
                report["errors"].append({"name": node.name, "reason": "移动失败"})
                if not report["error_detail"]:
                    report["error_detail"] = str(
                        getattr(self.backend, "last_move_error", "") or "协议端未返回具体原因"
                    )

        return report

    # ---------- 功能二：清理已失效文件 ----------
    async def clean_invalid(self, group_id: str, dry_run: bool = True) -> Dict:
        """扫描全部文件，识别并清理已失效（无法获取下载地址）的文件。

        安全策略：
        - 只有真正失效（get_file_url 返回 None / 标记 dead）才会处理；
        - 若该文件已有本地永久副本，则判定为「已保全」，可直接删除并提示；
        - ``dry_run=True`` 仅报告，不删除。
        """
        report = {
            "group_id": group_id,
            "dry_run": dry_run,
            "dead": [],
            "deleted": [],
            "skipped_saved": [],   # 已永久保存、可安全删除
            "alive": 0,
            "errors": [],
        }
        all_files = await self.scan(group_id)
        for node in all_files:
            is_dead = node.dead
            if not is_dead:
                url = await self.backend.get_file_url(group_id, node.id)
                is_dead = url is None
            # 同步 dead 到索引
            node.dead = is_dead
            self.db.upsert_file_index(group_id, node)

            if not is_dead:
                report["alive"] += 1
                continue

            perm = self.db.get_perm_by_file(group_id, node.id)
            entry = {"name": node.name, "size": node.size}
            if perm:
                entry["saved_path"] = perm["stored_path"]
                report["skipped_saved"].append(entry)
            else:
                report["dead"].append(entry)

            if dry_run:
                continue

            ok = await self.backend.delete_file(group_id, node.id)
            if ok:
                if perm:
                    report["deleted"].append(entry)
                # 若没有永久副本但确实失效，也删除（调用方应在命令层二次确认）
                if not perm:
                    report["deleted"].append(entry)
            else:
                report["errors"].append({"name": node.name, "reason": "删除失败"})

        return report

    # ---------- 功能三：将文件转为永久保存 ----------
    async def permanent_save(
        self, group_id: str, target: str, enable_upload: bool = False
    ) -> Dict:
        """把群文件下载到本地永久存档目录；可选回传到群文件系统「永久保存」目录。

        ``target`` 可为 file_id 或文件名（自动在根/递归查找）。
        返回含 stored_path / sha256 / size / category 的报告；失败含 error。
        """
        report = {"group_id": group_id, "target": target, "ok": False}

        node = await self._resolve_file(group_id, target)
        if node is None:
            report["error"] = f"未找到文件：{target}"
            return report

        max_mb = int(self.config.get("MAX_PERMANENT_SIZE_MB", 0) or 0)
        if max_mb > 0 and node.size > max_mb * 1024 * 1024:
            report["error"] = (
                f"文件过大（{human_size(node.size)} > 上限 {max_mb}MB），已跳过"
            )
            return report

        # 下载到本地（暂存名，计算 sha256 后重命名为 sha8__原名 防覆盖）
        cat = self.classifier.category_of(node.name)
        dest_dir = os.path.join(self.perm_dir, str(group_id), cat)
        os.makedirs(dest_dir, exist_ok=True)
        local_name = node.name
        dest_path = os.path.join(dest_dir, f"tmp__{local_name}")

        downloaded = await self.backend.download_file(group_id, node.id, dest_path)
        if not downloaded or not os.path.isfile(dest_path):
            # 清理半截文件
            if os.path.exists(dest_path):
                os.remove(dest_path)
            report["error"] = "下载失败（文件可能已失效或后端不可用）"
            return report

        digest = sha256_of_file(dest_path)
        final_path = os.path.join(dest_dir, f"{digest[:8]}__{local_name}")
        if os.path.exists(final_path):
            os.remove(dest_path)
        else:
            os.rename(dest_path, final_path)

        source_dead = node.dead or (await self.backend.get_file_url(group_id, node.id) is None)
        self.db.add_perm_file(
            group_id=group_id,
            file_id=node.id,
            name=node.name,
            category=cat,
            size=node.size,
            sha256=digest,
            stored_path=final_path,
            source_dead=bool(source_dead),
        )
        report.update(
            {
                "ok": True,
                "name": node.name,
                "category": cat,
                "size": node.size,
                "size_human": human_size(node.size),
                "sha256": digest,
                "stored_path": final_path,
                "source_dead": bool(source_dead),
                "uploaded": False,
            }
        )

        # 可选：回传到群文件系统「永久保存」目录
        if enable_upload:
            folder_name = self.config.get("PERMANENT_UPLOAD_FOLDER", "永久保存")
            folder = await self.backend.find_folder_by_name(group_id, folder_name)
            fid = folder.id if folder else await self.backend.create_folder(group_id, "", folder_name)
            if fid:
                up = await self.backend.upload_file(group_id, fid, final_path)
                report["uploaded"] = bool(up)
            else:
                report["uploaded"] = False

        return report

    async def _resolve_file(self, group_id: str, target: str) -> Optional[FileNode]:
        # 先按 file_id 直接取
        info = await self.backend.get_file_info(group_id, target)
        if info and not info.is_folder:
            return info
        # 否则按名称在根目录/递归查找
        all_files = await self.scan(group_id)
        for n in all_files:
            if n.name == target:
                return n
        return None

    # ---------- 列表 ----------
    async def list_files(self, group_id: str, folder_id: str = "") -> Dict:
        files = await self.backend.list_files(group_id, folder_id)
        folders = await self.backend.list_folders(group_id, folder_id)
        for f in files:
            f.category = self.classifier.category_of(f.name)
        return {"files": files, "folders": folders}

    def list_perm(self, group_id: str = "") -> List[Dict]:
        rows = self.db.list_perm_files(group_id)
        for r in rows:
            r["size_human"] = human_size(r.get("size", 0))
        return rows

    # ---------- 后台定时任务入口 ----------
    async def auto_maintenance(self, group_ids: List[str]) -> Dict:
        """一次定时维护：对给定群执行归类 + 清理（dry_run 受配置保护）。"""
        summary = {"groups": {}, "errors": []}
        dry = bool(self.config.get("DRY_RUN_DEFAULT", True))
        for gid in group_ids:
            try:
                c = await self.classify(gid, dry_run=dry)
                cl = await self.clean_invalid(gid, dry_run=dry)
                summary["groups"][gid] = {
                    "classified": len(c["moved"]),
                    "created_folders": c["created_folders"],
                    "dead": len(cl["dead"]),
                    "deleted": len(cl["deleted"]),
                    "dry_run": dry,
                }
            except Exception as e:  # 单个群出错不影响其他群
                summary["errors"].append({"group_id": gid, "error": str(e)})
        return summary
