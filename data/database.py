"""SQLite 持久化：永久文件清单 + 群文件索引。

- ``perm_files``：被「永久保存」到本地的文件记录（即使群文件失效也不丢）。
- ``file_index``：最近一次扫描的群文件快照（含是否失效），便于增量与审计。
"""

import os
import sqlite3
import time


class FileDB:
    def __init__(self, db_path: str):
        self.db_path = db_path
        parent = os.path.dirname(db_path)
        if parent:
            os.makedirs(parent, exist_ok=True)
        self._conn = sqlite3.connect(db_path, check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        self._init_schema()

    def _init_schema(self):
        c = self._conn
        c.executescript(
            """
            CREATE TABLE IF NOT EXISTS perm_files (
                id          INTEGER PRIMARY KEY AUTOINCREMENT,
                group_id    TEXT NOT NULL,
                file_id     TEXT NOT NULL,
                name        TEXT NOT NULL,
                category    TEXT NOT NULL DEFAULT '其他',
                size        INTEGER NOT NULL DEFAULT 0,
                sha256      TEXT NOT NULL DEFAULT '',
                stored_path TEXT NOT NULL,
                created_at  INTEGER NOT NULL,
                source_dead INTEGER NOT NULL DEFAULT 0
            );
            CREATE TABLE IF NOT EXISTS file_index (
                group_id    TEXT NOT NULL,
                file_id     TEXT NOT NULL,
                name        TEXT NOT NULL,
                category    TEXT NOT NULL DEFAULT '其他',
                size        INTEGER NOT NULL DEFAULT 0,
                folder_id   TEXT NOT NULL DEFAULT '',
                dead        INTEGER NOT NULL DEFAULT 0,
                last_checked INTEGER NOT NULL,
                PRIMARY KEY (group_id, file_id)
            );
            CREATE INDEX IF NOT EXISTS idx_perm_group ON perm_files(group_id);
            """
        )
        self._conn.commit()

    def close(self):
        try:
            self._conn.close()
        except Exception:
            pass

    # ---------- 永久文件清单 ----------
    def add_perm_file(
        self,
        group_id: str,
        file_id: str,
        name: str,
        category: str,
        size: int,
        sha256: str,
        stored_path: str,
        source_dead: bool = False,
    ) -> int:
        cur = self._conn.execute(
            """
            INSERT INTO perm_files
                (group_id, file_id, name, category, size, sha256, stored_path, created_at, source_dead)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                group_id,
                file_id,
                name,
                category,
                size,
                sha256,
                stored_path,
                int(time.time()),
                1 if source_dead else 0,
            ),
        )
        self._conn.commit()
        return cur.lastrowid

    def list_perm_files(self, group_id: str = ""):
        if group_id:
            rows = self._conn.execute(
                "SELECT * FROM perm_files WHERE group_id = ? ORDER BY created_at DESC",
                (group_id,),
            ).fetchall()
        else:
            rows = self._conn.execute(
                "SELECT * FROM perm_files ORDER BY created_at DESC"
            ).fetchall()
        return [dict(r) for r in rows]

    def get_perm_by_file(self, group_id: str, file_id: str):
        row = self._conn.execute(
            "SELECT * FROM perm_files WHERE group_id = ? AND file_id = ?",
            (group_id, file_id),
        ).fetchone()
        return dict(row) if row else None

    def perm_count(self, group_id: str = "") -> int:
        if group_id:
            return self._conn.execute(
                "SELECT COUNT(*) FROM perm_files WHERE group_id = ?", (group_id,)
            ).fetchone()[0]
        return self._conn.execute("SELECT COUNT(*) FROM perm_files").fetchone()[0]

    def perm_total_size(self, group_id: str = "") -> int:
        if group_id:
            r = self._conn.execute(
                "SELECT COALESCE(SUM(size),0) FROM perm_files WHERE group_id = ?", (group_id,)
            ).fetchone()
        else:
            r = self._conn.execute("SELECT COALESCE(SUM(size),0) FROM perm_files").fetchone()
        return r[0] if r else 0

    # ---------- 群文件索引 ----------
    def upsert_file_index(self, group_id: str, node) -> None:
        self._conn.execute(
            """
            INSERT INTO file_index
                (group_id, file_id, name, category, size, folder_id, dead, last_checked)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(group_id, file_id) DO UPDATE SET
                name=excluded.name, category=excluded.category, size=excluded.size,
                folder_id=excluded.folder_id, dead=excluded.dead, last_checked=excluded.last_checked
            """,
            (
                group_id,
                node.id,
                node.name,
                node.category,
                node.size,
                node.parent_id,
                1 if node.dead else 0,
                int(time.time()),
            ),
        )
        self._conn.commit()

    def index_dead_files(self, group_id: str = "") -> list:
        if group_id:
            rows = self._conn.execute(
                "SELECT * FROM file_index WHERE group_id = ? AND dead = 1", (group_id,)
            ).fetchall()
        else:
            rows = self._conn.execute(
                "SELECT * FROM file_index WHERE dead = 1"
            ).fetchall()
        return [dict(r) for r in rows]
