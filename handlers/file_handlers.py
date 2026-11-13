"""指令处理器：群文件帮助 / 列表 / 归类 / 失效清理 / 永久保存 / 永久列表 / 配置。

这些方法是普通 async 生成器（yield event.plain_result(...)），由 main.py 的
Star 指令方法包装调用，保持与项目既有写法一致。
"""

import re

from astrbot.api.event import AstrMessageEvent


class FileHandlers:
    def __init__(self, context, config: dict, manager, classifier):
        self.context = context
        self.config = config or {}
        self.manager = manager
        self.classifier = classifier

    # ---------- 权限/范围辅助 ----------
    def _sender_id(self, event: AstrMessageEvent) -> str:
        return str(event.get_sender_id())

    def _group_id(self, event: AstrMessageEvent):
        gid = event.get_group_id()
        return str(gid) if gid else ""

    def _authorized(self, event: AstrMessageEvent) -> bool:
        admins = [str(x) for x in self.config.get("ADMINS", []) or []]
        if not admins:
            return True
        return self._sender_id(event) in admins

    def _group_allowed(self, event: AstrMessageEvent) -> bool:
        wl = [str(x) for x in self.config.get("GROUP_WHITELIST", []) or []]
        if not wl:
            return True
        return self._group_id(event) in wl

    @staticmethod
    def _rest_text(event: AstrMessageEvent, cmd: str) -> str:
        """取指令关键字之后的全部文本（filter.command 普通参数只收首词）。"""
        raw = event.get_message_str()
        m = re.search(re.escape(cmd) + r"\s*(.*)", raw)
        return m.group(1).strip() if m else ""

    @staticmethod
    def _has_force(text: str) -> bool:
        return bool(re.search(r"(--force|-f|强制)", text))

    @staticmethod
    def _strip_force(text: str) -> str:
        return re.sub(r"(--force|-f|强制)", "", text).strip()

    # ---------- 帮助 ----------
    async def handle_help(self, event: AstrMessageEvent):
        text = (
            "📂 QQ群聊文件管理 v1.0.0 指令\n"
            "━━━━━━━━━━━━━━━━\n"
            "  群文件列表 [文件夹]\n"
            "    └─ 查看群文件系统的文件/文件夹\n"
            "  文件归类 [类别] [--force]\n"
            "    └─ 把根目录文件按类型移动到「图片/视频/音频/文档/压缩包/其他」目录\n"
            "    └─ 默认试运行(只报告)；加 --force 才真正移动\n"
            "    └─ 可选类别只处理该类别，如：文件归类 图片 --force\n"
            "  清理失效文件 [--force]\n"
            "    └─ 扫描并清理无法下载的失效文件（已永久保存的可安全删除）\n"
            "    └─ 默认试运行；加 --force 才真正删除\n"
            "  永久保存 <文件名或ID>\n"
            "    └─ 把群文件下载到本地永久存档（即使群文件失效也不丢）\n"
            "    └─ 可配置回传到群「永久保存」目录做双保险\n"
            "  永久文件列表\n"
            "    └─ 查看本地已永久保存的文件清单\n"
            "  文件管理配置\n"
            "    └─ 查看当前插件配置\n"
            "━━━━━━━━━━━━━━━━\n"
            "安全提示：归类/清理默认试运行，确认无误后加 --force 执行。"
        )
        yield event.plain_result(text)

    # ---------- 列表 ----------
    async def handle_list(self, event: AstrMessageEvent, folder: str = ""):
        gid = self._group_id(event)
        if not gid:
            yield event.plain_result("该指令仅在群聊中可用。")
            return
        if not self._group_allowed(event):
            yield event.plain_result("当前群不在管理白名单内。")
            return
        data = await self.manager.list_files(gid, folder)
        files = data["files"]
        folders = data["folders"]
        if not files and not folders:
            yield event.plain_result("该目录下暂无文件或文件夹。")
            return
        lines = [f"📂 群 {gid} 文件列表："]
        for f in folders:
            lines.append(f"  📁 {f.name}/")
        for f in files:
            tag = "💀失效" if f.dead else "✅"
            lines.append(f"  {tag} {f.name}（{f.size}字节, {f.category}）")
        yield event.plain_result("\n".join(lines))

    # ---------- 归类 ----------
    async def handle_classify(self, event: AstrMessageEvent, args: str = ""):
        gid = self._group_id(event)
        if not gid:
            yield event.plain_result("该指令仅在群聊中可用。")
            return
        if not self._authorized(event):
            yield event.plain_result("您没有执行该指令的权限。")
            return
        if not self._group_allowed(event):
            yield event.plain_result("当前群不在管理白名单内。")
            return
        force = self._has_force(args)
        rest = self._strip_force(args)
        target_cat = rest if rest else None
        dry_run = bool(self.config.get("DRY_RUN_DEFAULT", True)) and not force
        r = await self.manager.classify(gid, dry_run=dry_run, target_category=target_cat)
        mode = "【试运行】" if dry_run else "【已执行】"
        lines = [f"🗂️ 文件归类 {mode}（群 {gid}）"]
        if r["created_folders"]:
            lines.append("新建分类目录: " + ", ".join(r["created_folders"]))
        if r["moved"]:
            lines.append(f"将移动 {len(r['moved'])} 个文件：")
            for m in r["moved"]:
                lines.append(f"  → {m['name']}  ⇒  {m['category']}")
        else:
            lines.append("没有需要归类的文件（根目录已整洁）。")
        if r["skipped"]:
            lines.append(f"跳过(已归类) {len(r['skipped'])} 个")
        if r["errors"]:
            lines.append("⚠️ 错误: " + "; ".join(e["reason"] for e in r["errors"]))
        lines.append("（加 --force 可真正执行移动）" if dry_run else "归类完成。")
        yield event.plain_result("\n".join(lines))

    # ---------- 清理失效文件 ----------
    async def handle_clean(self, event: AstrMessageEvent, args: str = ""):
        gid = self._group_id(event)
        if not gid:
            yield event.plain_result("该指令仅在群聊中可用。")
            return
        if not self._authorized(event):
            yield event.plain_result("您没有执行该指令的权限。")
            return
        if not self._group_allowed(event):
            yield event.plain_result("当前群不在管理白名单内。")
            return
        force = self._has_force(args)
        dry_run = bool(self.config.get("DRY_RUN_DEFAULT", True)) and not force
        r = await self.manager.clean_invalid(gid, dry_run=dry_run)
        mode = "【试运行】" if dry_run else "【已执行】"
        lines = [f"🧹 清理失效文件 {mode}（群 {gid}）"]
        lines.append(f"有效文件: {r['alive']} 个")
        if r["dead"]:
            lines.append(f"失效待处理 {len(r['dead'])} 个：")
            for d in r["dead"]:
                lines.append(f"  💀 {d['name']}（{d['size']}字节）")
        if r["skipped_saved"]:
            lines.append(f"已永久保存、可安全删除 {len(r['skipped_saved'])} 个")
        if r["deleted"] and not dry_run:
            lines.append(f"已删除 {len(r['deleted'])} 个失效文件")
        if r["errors"]:
            lines.append("⚠️ 错误: " + "; ".join(e["reason"] for e in r["errors"]))
        lines.append("（加 --force 才会真正删除）" if dry_run else "清理完成。")
        yield event.plain_result("\n".join(lines))

    # ---------- 永久保存 ----------
    async def handle_perm(self, event: AstrMessageEvent, name: str = ""):
        gid = self._group_id(event)
        if not gid:
            yield event.plain_result("该指令仅在群聊中可用。")
            return
        if not self._authorized(event):
            yield event.plain_result("您没有执行该指令的权限。")
            return
        if not self._group_allowed(event):
            yield event.plain_result("当前群不在管理白名单内。")
            return
        target = name or self._rest_text(event, "永久保存")
        if not target:
            yield event.plain_result("用法：永久保存 <文件名或文件ID>")
            return
        enable_upload = bool(self.config.get("ENABLE_PERMANENT_UPLOAD", False))
        r = await self.manager.permanent_save(gid, target, enable_upload=enable_upload)
        if not r.get("ok"):
            yield event.plain_result(f"❌ 永久保存失败：{r.get('error', '未知错误')}")
            return
        lines = [
            f"💾 已永久保存：{r['name']}",
            f"类别: {r['category']}  大小: {r['size_human']}",
            f"SHA256: {r['sha256'][:16]}…",
            f"本地路径: {r['stored_path']}",
        ]
        if r.get("source_dead"):
            lines.append("⚠️ 源文件已失效，本次存档为抢救性副本。")
        if enable_upload:
            lines.append("已回传群「永久保存」目录 ✅" if r.get("uploaded") else "⚠️ 回传群目录失败")
        yield event.plain_result("\n".join(lines))

    # ---------- 永久文件列表 ----------
    async def handle_perm_list(self, event: AstrMessageEvent):
        gid = self._group_id(event)
        rows = self.manager.list_perm(gid) if gid else self.manager.list_perm()
        if not rows:
            yield event.plain_result("暂无永久保存的文件。")
            return
        lines = [f"💾 永久保存清单（共 {len(rows)} 个）："]
        for i, r in enumerate(rows, 1):
            lines.append(
                f"{i}. {r['name']}｜{r['category']}｜{r['size_human']}"
                + ("｜源已失效" if r.get("source_dead") else "")
            )
        yield event.plain_result("\n".join(lines))

    # ---------- 配置 ----------
    async def handle_config(self, event: AstrMessageEvent):
        if not self._authorized(event):
            yield event.plain_result("您没有查看配置的权限。")
            return
        lines = ["⚙️ 文件管理当前配置："]
        for k in (
            "BACKEND",
            "ENABLE_AUTO_CLASSIFY",
            "ENABLE_AUTO_CLEAN",
            "AUTO_MAINTENANCE_INTERVAL",
            "DRY_RUN_DEFAULT",
            "ENABLE_PERMANENT_UPLOAD",
            "PERMANENT_UPLOAD_FOLDER",
            "MAX_PERMANENT_SIZE_MB",
            "GROUP_WHITELIST",
            "ADMINS",
        ):
            lines.append(f"  {k}: {self.config.get(k)}")
        yield event.plain_result("\n".join(lines))
