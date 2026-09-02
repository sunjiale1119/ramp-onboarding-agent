"""MySQLSaver —— 自研的 LangGraph checkpointer。

## 为什么要自己写

LangGraph 官方只提供 MemorySaver / SqliteSaver / PostgresSaver，**没有 MySQL**。
而这个项目的业务数据全在 MySQL 里（知识库、三层记忆、trace、升级卡片）。
用 SqliteSaver 起步是可以的，但代价是**两个库**：会话文本在 MySQL，
暂停中的图在 SQLite——运维时要备份两处，出问题时要对两处的时间线。

## checkpoint 到底存什么

不是"聊天记录"。是**图跑到一半时的完整状态**，包括：

    checkpoint    各通道的值（RampState 里那些字段）+ 版本号
    metadata      这一步是谁写的、第几步、来源
    writes        节点产出但还没合并进主状态的挂起写入

`interrupt()` 之所以能跨天恢复，靠的就是这三样。用户点确认可能是
五分钟后也可能是第二天，中间进程重启过——状态得能从库里捞回来。

## 三张表的设计

    checkpoints          (thread_id, checkpoint_ns, checkpoint_id) 主键
    checkpoint_writes    加上 (task_id, idx)，存挂起写入
    checkpoint_blobs     大通道值单独存，避免主表行过大

`checkpoint_ns` 不能省：**子图有自己的命名空间**。父图和三个域子图
各自写各自的 checkpoint，少了这个字段它们会互相覆盖。

## 序列化

用 LangGraph 自带的 `JsonPlusSerializer`，它返回 `(type, bytes)`——
类型标签必须一起存，否则反序列化时不知道是 msgpack 还是 json。
存成 LONGBLOB 而不是 TEXT：msgpack 是二进制，塞进 utf8mb4 列会坏掉。
"""

from __future__ import annotations

import threading
from contextlib import contextmanager
from typing import Any, Iterator, Sequence

import pymysql
from langgraph.checkpoint.base import (
    BaseCheckpointSaver,
    ChannelVersions,
    Checkpoint,
    CheckpointMetadata,
    CheckpointTuple,
)
from langgraph.checkpoint.serde.jsonplus import JsonPlusSerializer

from . import config

DDL = [
    """
    CREATE TABLE IF NOT EXISTS checkpoints (
        thread_id      VARCHAR(191) NOT NULL,
        checkpoint_ns  VARCHAR(191) NOT NULL DEFAULT '',
        checkpoint_id  VARCHAR(191) NOT NULL,
        parent_id      VARCHAR(191) NULL,
        type           VARCHAR(64)  NULL,
        checkpoint     LONGBLOB     NOT NULL,
        metadata       LONGBLOB     NOT NULL,
        created_at     TIMESTAMP    NOT NULL DEFAULT CURRENT_TIMESTAMP,
        PRIMARY KEY (thread_id, checkpoint_ns, checkpoint_id),
        KEY idx_thread_created (thread_id, checkpoint_ns, checkpoint_id DESC)
    ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4
    """,
    """
    CREATE TABLE IF NOT EXISTS checkpoint_writes (
        thread_id      VARCHAR(191) NOT NULL,
        checkpoint_ns  VARCHAR(191) NOT NULL DEFAULT '',
        checkpoint_id  VARCHAR(191) NOT NULL,
        task_id        VARCHAR(191) NOT NULL,
        idx            INT          NOT NULL,
        channel        VARCHAR(191) NOT NULL,
        type           VARCHAR(64)  NULL,
        value          LONGBLOB     NULL,
        task_path      VARCHAR(255) NOT NULL DEFAULT '',
        PRIMARY KEY (thread_id, checkpoint_ns, checkpoint_id, task_id, idx)
    ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4
    """,
]


class MySQLSaver(BaseCheckpointSaver):
    """把 LangGraph 的 checkpoint 存进 MySQL。

    线程安全靠一把锁 + 每次操作新开连接——**不是为了性能，是为了正确**。
    pymysql 的连接不是线程安全的，而 LangGraph 会在多个任务里并发调用
    checkpointer。省掉这把锁的代价是偶发的 `Packet sequence number wrong`，
    那种错误极难复现也极难定位。
    """

    def __init__(self, *, autocreate: bool = True) -> None:
        super().__init__(serde=JsonPlusSerializer())
        self._lock = threading.Lock()
        if autocreate:
            self.setup()

    # ---------------------------------------------------------------- 连接
    def _connect(self) -> pymysql.connections.Connection:
        return pymysql.connect(
            host=config.MYSQL_HOST,
            port=config.MYSQL_PORT,
            user=config.MYSQL_USER,
            password=config.MYSQL_PASSWORD,
            database=config.MYSQL_DB,
            charset="utf8mb4",
            autocommit=True,
        )

    @contextmanager
    def _cursor(self) -> Iterator[pymysql.cursors.Cursor]:
        with self._lock:
            conn = self._connect()
            try:
                with conn.cursor() as cur:
                    yield cur
            finally:
                conn.close()

    def setup(self) -> None:
        with self._cursor() as cur:
            for stmt in DDL:
                cur.execute(stmt)

    # ---------------------------------------------------------------- 写
    def put(
        self,
        config_: dict[str, Any],
        checkpoint: Checkpoint,
        metadata: CheckpointMetadata,
        new_versions: ChannelVersions,
    ) -> dict[str, Any]:
        cfg = config_["configurable"]
        thread_id = str(cfg["thread_id"])
        ns = str(cfg.get("checkpoint_ns", "") or "")
        cid = str(checkpoint["id"])
        parent = cfg.get("checkpoint_id")

        ctype, cbytes = self.serde.dumps_typed(checkpoint)
        _, mbytes = self.serde.dumps_typed(dict(metadata))

        with self._cursor() as cur:
            # REPLACE 而不是 INSERT：同一个 checkpoint_id 可能被重写
            # （例如 interrupt 恢复后同一步再落一次盘）。
            cur.execute(
                "REPLACE INTO checkpoints "
                "(thread_id, checkpoint_ns, checkpoint_id, parent_id, type, checkpoint, metadata) "
                "VALUES (%s,%s,%s,%s,%s,%s,%s)",
                (thread_id, ns, cid, parent, ctype, cbytes, mbytes),
            )
        return {"configurable": {"thread_id": thread_id, "checkpoint_ns": ns,
                                 "checkpoint_id": cid}}

    def put_writes(
        self,
        config_: dict[str, Any],
        writes: Sequence[tuple[str, Any]],
        task_id: str,
        task_path: str = "",
    ) -> None:
        """存挂起写入。

        **这是 interrupt() 能恢复的关键**：节点产出了值但图停住了，
        这些值还没并进主状态。不存下来，resume 时就丢了。
        """
        cfg = config_["configurable"]
        rows = []
        for idx, (channel, value) in enumerate(writes):
            vtype, vbytes = self.serde.dumps_typed(value)
            rows.append((
                str(cfg["thread_id"]), str(cfg.get("checkpoint_ns", "") or ""),
                str(cfg["checkpoint_id"]), task_id, idx, channel, vtype, vbytes, task_path,
            ))
        if not rows:
            return
        with self._cursor() as cur:
            cur.executemany(
                "REPLACE INTO checkpoint_writes "
                "(thread_id, checkpoint_ns, checkpoint_id, task_id, idx, channel, type, value, task_path) "
                "VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s)",
                rows,
            )

    # ---------------------------------------------------------------- 读
    def _load_writes(self, cur, thread_id: str, ns: str, cid: str) -> list[tuple]:
        cur.execute(
            "SELECT task_id, channel, type, value FROM checkpoint_writes "
            "WHERE thread_id=%s AND checkpoint_ns=%s AND checkpoint_id=%s "
            "ORDER BY task_id, idx",
            (thread_id, ns, cid),
        )
        return [(t, ch, self.serde.loads_typed((ty, v))) for t, ch, ty, v in cur.fetchall()]

    def _row_to_tuple(self, cur, row: tuple) -> CheckpointTuple:
        thread_id, ns, cid, parent, ctype, cbytes, mbytes = row
        return CheckpointTuple(
            config={"configurable": {"thread_id": thread_id, "checkpoint_ns": ns,
                                     "checkpoint_id": cid}},
            checkpoint=self.serde.loads_typed((ctype, cbytes)),
            metadata=self.serde.loads_typed(("msgpack", mbytes)),
            parent_config=(
                {"configurable": {"thread_id": thread_id, "checkpoint_ns": ns,
                                  "checkpoint_id": parent}} if parent else None
            ),
            pending_writes=self._load_writes(cur, thread_id, ns, cid),
        )

    def get_tuple(self, config_: dict[str, Any]) -> CheckpointTuple | None:
        cfg = config_["configurable"]
        thread_id = str(cfg["thread_id"])
        ns = str(cfg.get("checkpoint_ns", "") or "")
        cid = cfg.get("checkpoint_id")

        cols = ("thread_id, checkpoint_ns, checkpoint_id, parent_id, type, checkpoint, metadata")
        with self._cursor() as cur:
            if cid:
                cur.execute(
                    f"SELECT {cols} FROM checkpoints "
                    "WHERE thread_id=%s AND checkpoint_ns=%s AND checkpoint_id=%s",
                    (thread_id, ns, str(cid)),
                )
            else:
                # 没指定就取最新的一个。checkpoint_id 是单调递增的 UUID v6 风格，
                # 按它倒序等价于按时间倒序——比按 created_at 排更可靠，
                # 因为同一秒内可能落多个盘。
                cur.execute(
                    f"SELECT {cols} FROM checkpoints "
                    "WHERE thread_id=%s AND checkpoint_ns=%s "
                    "ORDER BY checkpoint_id DESC LIMIT 1",
                    (thread_id, ns),
                )
            row = cur.fetchone()
            return self._row_to_tuple(cur, row) if row else None

    def list(
        self,
        config_: dict[str, Any] | None,
        *,
        filter: dict[str, Any] | None = None,  # noqa: A002
        before: dict[str, Any] | None = None,
        limit: int | None = None,
    ) -> Iterator[CheckpointTuple]:
        where, params = [], []
        if config_ and config_.get("configurable", {}).get("thread_id"):
            where.append("thread_id=%s")
            params.append(str(config_["configurable"]["thread_id"]))
            where.append("checkpoint_ns=%s")
            params.append(str(config_["configurable"].get("checkpoint_ns", "") or ""))
        if before and before.get("configurable", {}).get("checkpoint_id"):
            where.append("checkpoint_id < %s")
            params.append(str(before["configurable"]["checkpoint_id"]))

        sql = ("SELECT thread_id, checkpoint_ns, checkpoint_id, parent_id, type, checkpoint, metadata "
               "FROM checkpoints")
        if where:
            sql += " WHERE " + " AND ".join(where)
        sql += " ORDER BY checkpoint_id DESC"
        if limit:
            sql += f" LIMIT {int(limit)}"

        with self._cursor() as cur:
            cur.execute(sql, params)
            rows = cur.fetchall()
            for row in rows:
                t = self._row_to_tuple(cur, row)
                # filter 是对 metadata 的等值匹配。放在 Python 侧做而不是
                # SQL 侧——metadata 是序列化过的 blob，SQL 查不动。
                if filter and not all(t.metadata.get(k) == v for k, v in filter.items()):
                    continue
                yield t

    # ---------------------------------------------------------------- 运维
    def delete_thread(self, thread_id: str) -> int:
        """删掉一个会话的全部 checkpoint。

        产品上对应"用户删除这段对话"——记忆可删，暂停中的任务也得可删，
        否则删了会话却留着一个半截的待确认工单，是更糟的状态。
        """
        with self._cursor() as cur:
            cur.execute("DELETE FROM checkpoint_writes WHERE thread_id=%s", (thread_id,))
            cur.execute("DELETE FROM checkpoints WHERE thread_id=%s", (thread_id,))
            return cur.rowcount

    def stats(self) -> dict[str, Any]:
        """运维统计。

        命名空间**按前缀聚合**，不逐条列。每次子图调用都会生成一个
        `domain_hr:<uuid>` 形式的新命名空间——60 道题就能产生 55 个，
        原样打印出来是一屏乱码，没人看得下去。
        """
        with self._cursor() as cur:
            cur.execute("SELECT COUNT(*), COUNT(DISTINCT thread_id) FROM checkpoints")
            n, threads = cur.fetchone()
            cur.execute("SELECT COUNT(*) FROM checkpoint_writes")
            (writes,) = cur.fetchone()
            cur.execute("SELECT checkpoint_ns, COUNT(*) FROM checkpoints GROUP BY checkpoint_ns")
            agg: dict[str, int] = {}
            for ns, c in cur.fetchall():
                key = (ns or "").split(":")[0] or "(父图)"
                agg[key] = agg.get(key, 0) + c
        return {
            "checkpoints": n,
            "threads": threads,
            "pending_writes": writes,
            "by_namespace": dict(sorted(agg.items(), key=lambda x: -x[1])),
        }

    def prune(self, *, keep_per_thread: int = 3, older_than_days: int | None = None) -> dict[str, int]:
        """清理历史 checkpoint。

        **这张表会无限增长**：60 道评测题就写了 480 个 checkpoint、
        6839 条挂起写入。跑几轮回归就是几万行。

        产品上真正需要的只有两种 checkpoint：
          · 每个会话最新的那个（用来恢复 interrupt）
          · 用户可能回溯的最近几个

        中间那些跑完就没用了。默认每个 thread 保留最近 3 个。
        """
        deleted_cp = deleted_w = 0
        with self._cursor() as cur:
            cur.execute("SELECT DISTINCT thread_id, checkpoint_ns FROM checkpoints")
            pairs = cur.fetchall()

            for thread_id, ns in pairs:
                cur.execute(
                    "SELECT checkpoint_id FROM checkpoints "
                    "WHERE thread_id=%s AND checkpoint_ns=%s "
                    "ORDER BY checkpoint_id DESC LIMIT %s OFFSET %s",
                    (thread_id, ns, 10_000_000, keep_per_thread),
                )
                stale = [r[0] for r in cur.fetchall()]
                if not stale:
                    continue
                marks = ",".join(["%s"] * len(stale))
                cur.execute(
                    f"DELETE FROM checkpoint_writes WHERE thread_id=%s AND checkpoint_ns=%s "
                    f"AND checkpoint_id IN ({marks})",
                    (thread_id, ns, *stale),
                )
                deleted_w += cur.rowcount
                cur.execute(
                    f"DELETE FROM checkpoints WHERE thread_id=%s AND checkpoint_ns=%s "
                    f"AND checkpoint_id IN ({marks})",
                    (thread_id, ns, *stale),
                )
                deleted_cp += cur.rowcount

            if older_than_days:
                cur.execute(
                    "DELETE FROM checkpoints WHERE created_at < NOW() - INTERVAL %s DAY",
                    (older_than_days,),
                )
                deleted_cp += cur.rowcount

        return {"checkpoints_deleted": deleted_cp, "writes_deleted": deleted_w}


__all__ = ["MySQLSaver"]
