"""文件分类工具：根据扩展名把文件归入「图片 / 视频 / 音频 / 文档 / 压缩包 / 其他」类别。"""

import json
import os
from pathlib import Path
from typing import Dict, List, Optional

# 类别展示名顺序（用于稳定输出）
CATEGORY_ORDER = ["图片", "视频", "音频", "文档", "压缩包", "其他"]

DEFAULT_CATEGORY = "其他"

# 内置兜底分类表（当 config/categories.json 缺失时使用）
_FALLBACK: Dict[str, List[str]] = {
    "图片": ["jpg", "jpeg", "png", "gif", "webp", "bmp", "svg", "heic", "tiff"],
    "视频": ["mp4", "mov", "avi", "mkv", "flv", "wmv", "m4v", "rmvb", "3gp"],
    "音频": ["mp3", "wav", "flac", "m4a", "aac", "ogg", "wma"],
    "文档": ["pdf", "doc", "docx", "xls", "xlsx", "ppt", "pptx", "txt", "md", "csv", "epub", "rtf"],
    "压缩包": ["zip", "rar", "7z", "tar", "gz", "bz2", "xz", "zst", "tar.gz", "tar.bz2", "tar.xz", "tar.zst"],
}


class Classifier:
    """扩展名 -> 类别 映射器。

    优先读取插件目录下的 ``config/categories.json``；缺失或解析失败时回退内置表。
    """

    def __init__(self, config_path: Optional[str] = None):
        self._map: Dict[str, List[str]] = {}
        self._ext_to_cat: Dict[str, str] = {}
        self._load(config_path)

    def _load(self, config_path: Optional[str]):
        data = None
        if config_path and os.path.isfile(config_path):
            try:
                with open(config_path, "r", encoding="utf-8") as f:
                    data = json.load(f)
            except (json.JSONDecodeError, OSError):
                data = None
        if not isinstance(data, dict) or not data:
            data = _FALLBACK

        self._map = {k: [e.lower() for e in v] for k, v in data.items()}
        for cat, exts in self._map.items():
            for ext in exts:
                self._ext_to_cat[ext] = cat

    def category_of(self, name: str) -> str:
        """根据文件名返回类别名。无扩展名或未知扩展名归为「其他」。"""
        ext = self._safe_ext(name)
        return self._ext_to_cat.get(ext, DEFAULT_CATEGORY)

    @staticmethod
    def _safe_ext(name: str) -> str:
        base = os.path.basename(name)
        # 处理形如 "a.tar.gz" 的情况：优先取最后一个点后的扩展名，
        # 但若倒数第二扩展是常见归档容器（tar/gz/bz2/xz），则组合判断。
        parts = base.lower().split(".")
        if len(parts) <= 1:
            return ""
        last = parts[-1]
        if last in ("gz", "bz2", "xz", "zst") and len(parts) >= 3 and parts[-2] in ("tar",):
            return ".".join(parts[-2:])
        return last

    def categories(self) -> List[str]:
        """返回所有已知类别（含「其他」兜底）。"""
        cats = list(self._map.keys())
        if DEFAULT_CATEGORY not in cats:
            cats.append(DEFAULT_CATEGORY)
        return cats

    def list_by_category(self, names: List[str]) -> Dict[str, List[str]]:
        """把一组文件名按类别分组。"""
        result: Dict[str, List[str]] = {c: [] for c in CATEGORY_ORDER}
        for n in names:
            result.setdefault(self.category_of(n), []).append(n)
        return result

    def is_known_category(self, category: str) -> bool:
        return category in self._map or category == DEFAULT_CATEGORY


def category_of(name: str, config_path: Optional[str] = None) -> str:
    """便捷函数：直接得到文件名对应的类别。"""
    return Classifier(config_path).category_of(name)


def find_plugin_root() -> Path:
    """定位插件根目录（用于查找 config/categories.json）。"""
    return Path(__file__).resolve().parent.parent
