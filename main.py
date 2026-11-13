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
CMD_DIAG = "群文件诊断"


@register(
    "astrbot_plugin_qqfilemgr",
    "FrostsAfterglow",
    "QQ群聊文件管理：自动归类 / 失效清理 / 永久保存",
    "1.0.3",
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

        # 后端（auto 模式保存 OneBot 实例，便于绑定恢复时可逆切换）
        self._onebot: OneBotFileBackend | None = None
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
        self._degraded = False
        self._start_auto_maintenance()

        self.logger.info("QQ群聊文件管理插件已加载（后端：%s）", self.config.get("BACKEND", "auto"))

    # ---------- 后端构建 ----------
    def _build_backend(self) -> FileBackend:
        mode = str(self.config.get("BACKEND", "auto") or "auto").lower()
        ob_platform = str(self.config.get("ONEBOT_PLATFORM", "aiocqhttp") or "aiocqhttp")
        if mode == "local":
            return LocalBackend(self.local_base)
        ob = OneBotFileBackend(self.context, platform=ob_platform)
        self._onebot = ob
        return ob

    # ---------- 指令前准备：绑定 event.bot（可逆） ----------
    async def _prepare_backend(self, event):
        """每次指令前尝试绑定当前消息的 event.bot 到 OneBot 后端。

        - onebot 模式：绑定失败则保留错误，由容错包装提示。
        - auto 模式：绑定成功切回真实群文件；绑定失败降级到 local 本地镜像。
        返回：是否「本次刚发生降级」（用于只提示一次）。
        """
        mode = str(self.config.get("BACKEND", "auto") or "auto").lower()
        if mode == "local" or self._onebot is None:
            return False

        ok = await self._onebot.ensure_ready(event)
        just_degraded = False
        if ok:
            if self._degraded or not isinstance(self.backend, OneBotFileBackend):
                self.backend = self._onebot
                self.manager.backend = self._onebot
            self._degraded = False
        elif mode == "auto":
            if not self._degraded:
                just_degraded = True
            self.backend = LocalBackend(self.local_base)
            self.manager.backend = self.backend
            self._degraded = True
        # onebot 强制模式：ok=False 时保持 OneBot，让 RuntimeError 正常抛出
        return just_degraded

    # ---------- 指令容错包装 ----------
    async def _safe_yield(self, event, gen):
        """包装 handler 生成器：绑定 event.bot + 捕获后端错误，转为友好提示而非崩溃。"""
        try:
            degraded = await self._prepare_backend(event)
            if degraded:
                yield event.plain_result(
                    "ℹ️ 未检测到在线的 QQ 适配器，已自动切换为【本地镜像模式】。\n"
                    "当前指令在本地演示目录上执行；接入 aiocqhttp/NapCat 后会自动切回真实群文件。\n"
                    "（如需强制走真实群文件，把 BACKEND 设为 onebot；发『群文件诊断』查看详情）"
                )
            async for r in gen:
                yield r
        except RuntimeError as e:
            msg = str(e)
            yield event.plain_result("❌ 群文件操作失败：" + msg)

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

    # ---------- 指令：诊断 ----------
    @filter.command(CMD_DIAG, "诊断 QQ 群文件后端")
    async def cmd_diag(self, event: AstrMessageEvent):
        try:
            await self._prepare_backend(event)
        except Exception:
            pass
        backend_mode = str(self.config.get("BACKEND", "auto") or "auto")
        degraded = getattr(self, "_degraded", False)
        lines = [f"🔧 群文件后端诊断（BACKEND={backend_mode}）"]
        lines.append("当前模式: " + ("本地镜像(降级)" if degraded else
                                    ("本地镜像" if isinstance(self.backend, LocalBackend) else "OneBot")))
        if self._onebot is not None:
            # 诊断始终基于 OneBot 实例探测（即使当前处于降级/本地模式）
            try:
                await self._onebot.ensure_ready(event)
                info = await self._onebot.probe(self._group_id_of(event))
            except Exception as e:  # noqa
                info = {"error": str(e)}
            lines.append(f"bot 已绑定: {info.get('bound')}  来源: {info.get('source')}")
            lines.append(f"协议端: {info.get('app_name') or '(未探测)'}"
                         + ("（LLOneBot 参数）" if info.get("is_llbot") else ""))
            lines.append(f"context 接口: get_platform={info.get('has_get_platform')} "
                         f"get_platform_adapter={info.get('has_get_platform_adapter')}")
            probe = info.get("probe", {}) or {}
            if "version" in probe:
                v = probe["version"]
                lines.append("get_version_info: " + str(v)[:120])
            if "version_error" in probe:
                lines.append("版本探测失败: " + str(probe["version_error"])[:100])
            if "root_files" in probe:
                lines.append(f"根目录: {probe['root_files']} 个文件 / "
                             f"{probe.get('root_folders', 0)} 个文件夹")
                if probe.get("sample"):
                    lines.append("示例: " + ", ".join(str(x) for x in probe["sample"]))
            if "list_error" in probe:
                lines.append("列目录失败: " + str(probe["list_error"])[:100])
            if "error" in probe:
                lines.append("探测: " + str(probe["error"]))
            if info.get("bound") and "root_files" in probe:
                lines.append("✅ 后端可用，归类/清理/永久保存可直接操作真实群文件。")
            else:
                lines.append("如未绑定：请确认已接入 aiocqhttp 适配器并在线；")
                lines.append("或把 BACKEND 设为 local 使用离线镜像。")
        else:
            lines.append("当前为本地镜像后端，所有指令在本地演示目录运行。")
        yield event.plain_result("\n".join(lines))

    @staticmethod
    def _group_id_of(event) -> str:
        gid = event.get_group_id()
        return str(gid) if gid else "0"
