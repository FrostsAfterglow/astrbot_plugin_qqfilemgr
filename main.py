"""QQ群聊文件管理插件主类（AstrBot v4 Star 插件）。

功能：
  1) 自动位移文件归类（按扩展名移动到分类目录）
  2) 清理已失效文件（无法下载的文件，已保全的可安全删除）
  3) 将文件转为永久保存（下载到本地存档 + 可选回传群「永久保存」目录）

后端：BACKEND 配置决定走 OneBot 群文件系统（真实 QQ）还是本地镜像（离线/测试）。
"""

import asyncio
import os
from pathlib import Path

from astrbot.api import logger, AstrBotConfig
from astrbot.api.event import AstrMessageEvent, filter
from astrbot.api.star import Context, Star, register

from .core.classifier import Classifier
from .core.file_backend import FileBackend
from .core.local_backend import LocalBackend
from .core.onebot_backend import OneBotFileBackend
from .core.file_manager import FileManager
from .data.database import FileDB
from .handlers.file_handlers import FileHandlers

# 指令定义
CMD_HELP = "群文件帮助"
CMD_LIST = "群文件列表"
CMD_CLASSIFY = "文件归类"
CMD_CLEAN = "清理失效文件"
CMD_PERM = "永久保存"
CMD_PERM_LIST = "永久文件列表"
CMD_CONFIG = "文件管理配置"


@register(
    "astrbot_plugin_qqfilemgr",
    "FrostsAfterglow",
    "QQ群聊文件管理：自动归类 / 失效清理 / 永久保存",
    "1.0.0",
    "https://github.com/FrostsAfterglow/astrbot_plugin_qqfilemgr",
)
class QQFileManagerPlugin(Star):
    def __init__(self, context: Context, config: AstrBotConfig | None = None):
        super().__init__(context)
        self.context = context
        self.config: AstrBotConfig = config if config is not None else AstrBotConfig({})

        # 数据目录（持久化与本地镜像都放在插件 data/ 下）
        self.data_dir = os.path.join(os.path.dirname(os.path.abspath(__file__)), "data")
        os.makedirs(self.data_dir, exist_ok=True)
        self.perm_dir = os.path.join(self.data_dir, "permanent")
        self.local_base = os.path.join(self.data_dir, "group_files")

        # 分类器（读取 config/categories.json）
        cat_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "config", "categories.json")
        self.classifier = Classifier(cat_path)

        # 数据库
        self.db = FileDB(os.path.join(self.data_dir, "filemgr.db"))

        # 后端
        self.backend: FileBackend = self._build_backend()

        # 管理器
        self.manager = FileManager(
            backend=self.backend,
            db=self.db,
            classifier=self.classifier,
            config=dict(self.config),
            perm_dir=self.perm_dir,
        )

        # 处理器
        self.file_handlers = FileHandlers(
            context=self.context,
            config=dict(self.config),
            manager=self.manager,
            classifier=self.classifier,
        )

        # 已见过的群（供自动维护使用）
        self._seen_groups = set()
        self._bg_task = None
        self._start_auto_maintenance()

        self.logger.info("QQ群聊文件管理插件已加载（后端：%s）", self.config.get("BACKEND", "auto"))

    # ---------- 后端构建 ----------
    def _build_backend(self) -> FileBackend:
        mode = str(self.config.get("BACKEND", "auto") or "auto").lower()
        ob_platform = str(self.config.get("ONEBOT_PLATFORM", "aiocqhttp") or "aiocqhttp")
        if mode == "local":
            return LocalBackend(self.local_base)
        if mode == "onebot":
            return OneBotFileBackend(self.context, platform=ob_platform)
        # auto：优先 OneBot；若需离线测试可改为 local
        return OneBotFileBackend(self.context, platform=ob_platform)

    # ---------- 指令容错包装 ----------
    async def _safe_yield(self, event, gen):
        """包装 handler 生成器：捕获后端 RuntimeError，转为友好提示而非崩溃。"""
        try:
            async for r in gen:
                yield r
        except RuntimeError as e:
            msg = str(e)
            if "适配器" in msg or "BACKEND" in msg or "local" in msg:
                yield event.plain_result(
                    "⚠️ 无法连接 QQ 群文件后端。\n"
                    "若你尚未接入支持群文件系统的 QQ 适配器（gocqhttp / Lagrange），\n"
                    "请在插件配置中将 BACKEND 设为 local 进行离线测试；\n"
                    "若已接入，请确认适配器在线（默认尝试 aiocqhttp / onebot / qq 等平台名），\n"
                    "或在配置 ONEBOT_PLATFORM 中指定你的平台名。\n"
                    "（后端详情：" + msg + "）"
                )
            else:
                yield event.plain_result("❌ 操作失败：" + msg)

    # ---------- 后台定时维护 ----------
    def _start_auto_maintenance(self):
        enabled = bool(self.config.get("ENABLE_AUTO_CLASSIFY")) or bool(
            self.config.get("ENABLE_AUTO_CLEAN")
        )
        if not enabled:
            return
        interval = max(60, int(self.config.get("AUTO_MAINTENANCE_INTERVAL", 360) or 360))
        self._bg_task = asyncio.ensure_future(self._auto_loop(interval))

    async def _auto_loop(self, interval: int):
        try:
            while True:
                await asyncio.sleep(interval * 60)
                wl = [str(x) for x in self.config.get("GROUP_WHITELIST", []) or []]
                groups = wl or list(self._seen_groups)
                if groups:
                    summary = await self.manager.auto_maintenance(groups)
                    self.logger.info("自动维护完成：%s", summary)
        except asyncio.CancelledError:
            pass
        except Exception as e:  # 防止后台任务意外退出
            self.logger.error("自动维护任务异常：%s", e)

    # ---------- 消息监听：记录群号 ----------
    @filter.event_message_type(filter.EventMessageType.ALL)
    async def _on_message(self, event: AstrMessageEvent):
        gid = event.get_group_id()
        if gid:
            self._seen_groups.add(str(gid))
        return

    # ---------- 指令：帮助 ----------
    @filter.command(CMD_HELP, "QQ群文件管理帮助")
    async def cmd_help(self, event: AstrMessageEvent):
        async for r in self._safe_yield(event, self.file_handlers.handle_help(event)):
            yield r

    # ---------- 指令：列表 ----------
    @filter.command(CMD_LIST, "查看群文件列表")
    async def cmd_list(self, event: AstrMessageEvent, folder: str = ""):
        async for r in self._safe_yield(event, self.file_handlers.handle_list(event, folder)):
            yield r

    # ---------- 指令：归类 ----------
    @filter.command(CMD_CLASSIFY, "自动位移文件归类")
    async def cmd_classify(self, event: AstrMessageEvent):
        async for r in self._safe_yield(event, self.file_handlers.handle_classify(event)):
            yield r

    # ---------- 指令：清理失效文件 ----------
    @filter.command(CMD_CLEAN, "清理已失效文件")
    async def cmd_clean(self, event: AstrMessageEvent):
        async for r in self._safe_yield(event, self.file_handlers.handle_clean(event)):
            yield r

    # ---------- 指令：永久保存 ----------
    @filter.command(CMD_PERM, "将群文件转为永久保存")
    async def cmd_perm(self, event: AstrMessageEvent):
        async for r in self._safe_yield(event, self.file_handlers.handle_perm(event)):
            yield r

    # ---------- 指令：永久文件列表 ----------
    @filter.command(CMD_PERM_LIST, "查看永久保存清单")
    async def cmd_perm_list(self, event: AstrMessageEvent):
        async for r in self._safe_yield(event, self.file_handlers.handle_perm_list(event)):
            yield r

    # ---------- 指令：配置 ----------
    @filter.command(CMD_CONFIG, "查看文件管理配置")
    async def cmd_config(self, event: AstrMessageEvent):
        async for r in self._safe_yield(event, self.file_handlers.handle_config(event)):
            yield r
