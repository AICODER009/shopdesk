"""Storage-limitation job (GDPR art. 5(1)(e)). Run daily, e.g. from cron or a k8s CronJob.

- conversations idle longer than CONVERSATION_TTL_DAYS are deleted outright;
- active conversations keep only their latest checkpoint (LangGraph otherwise keeps every
  intermediate snapshot, i.e. many copies of the same personal data);
- delivered / dead dead-letter rows past the TTL are deleted.
"""


import time
from datetime import UTC, datetime, timedelta

import aiosqlite
import structlog
from langgraph.checkpoint.postgres.aio import AsyncPostgresSaver
from langgraph.checkpoint.sqlite.aio import AsyncSqliteSaver
from psycopg import AsyncConnection
from psycopg.rows import DictRow, dict_row
from psycopg_pool import AsyncConnectionPool

from shopdesk.config import get_settings
from shopdesk.observability import configure_logging
from shopdesk.runtime import run
from shopdesk.storage_sqlite import SqliteDB, sqlite_path

log = structlog.get_logger()

IDLE_THREADS = """
    SELECT thread_id FROM checkpoints
    WHERE checkpoint_ns = ''
    GROUP BY thread_id
    HAVING max((checkpoint ->> 'ts')::timestamptz) < now() - make_interval(days => %s)
"""
ACTIVE_THREADS = """
    SELECT thread_id FROM checkpoints
    WHERE checkpoint_ns = ''
    GROUP BY thread_id
    HAVING count(*) > 1
"""


async def purge(
    pool: AsyncConnectionPool[AsyncConnection[DictRow]], ttl_days: int, batch: int = 200
) -> dict[str, int]:
    saver = AsyncPostgresSaver(pool)
    async with pool.connection() as conn:
        idle = [r["thread_id"] for r in await (await conn.execute(IDLE_THREADS, (ttl_days,)))
                .fetchall()]
        active = [r["thread_id"] for r in await (await conn.execute(ACTIVE_THREADS)).fetchall()]
        cur = await conn.execute(
            "DELETE FROM handoff_dead_letters WHERE status IN ('delivered', 'dead') "
            "AND created_at < now() - make_interval(days => %s)",
            (ttl_days,),
        )
        letters = cur.rowcount

    for i in range(0, len(idle), batch):
        await saver.aprune(idle[i : i + batch], strategy="delete")
    idle_set = set(idle)
    trim = [t for t in active if t not in idle_set]
    for i in range(0, len(trim), batch):
        await saver.aprune(trim[i : i + batch], strategy="keep_latest")
    return {"deleted_threads": len(idle), "trimmed_threads": len(trim),
            "deleted_dead_letters": letters}


async def purge_sqlite(saver: AsyncSqliteSaver, db: SqliteDB, ttl_days: int) -> dict[str, int]:
    # SQLite checkpoints are serialised blobs, so idle threads are found through the saver
    # rather than with SQL. Fine at single-server volumes.
    latest: dict[str, datetime] = {}
    counts: dict[str, int] = {}
    async for item in saver.alist(None):
        conf = item.config["configurable"]
        if conf.get("checkpoint_ns", ""):
            continue
        thread = conf["thread_id"]
        ts = datetime.fromisoformat(item.checkpoint["ts"])
        latest[thread] = max(ts, latest.get(thread, ts))
        counts[thread] = counts.get(thread, 0) + 1

    cutoff = datetime.now(UTC) - timedelta(days=ttl_days)
    idle = [t for t, ts in latest.items() if ts < cutoff]
    trim = [t for t, n in counts.items() if n > 1 and t not in set(idle)]
    for thread in idle:
        await saver.adelete_thread(thread)
    # AsyncSqliteSaver doesn't implement aprune; keep-latest is two deletes on its tables
    # (checkpoint ids are time-ordered, so MAX is the newest).
    newest = ("(SELECT MAX(checkpoint_id) FROM checkpoints"
              " WHERE thread_id = ? AND checkpoint_ns = '')")
    async with saver.lock:
        for thread in trim:
            for table in ("writes", "checkpoints"):
                await saver.conn.execute(
                    f"DELETE FROM {table} WHERE thread_id = ? AND checkpoint_ns = ''"  # noqa: S608
                    f" AND checkpoint_id < {newest}",
                    (thread, thread),
                )
        await saver.conn.commit()

    async with db.tx() as conn:
        cur = await conn.execute(
            "DELETE FROM handoff_dead_letters WHERE status IN ('delivered', 'dead')"
            " AND created_at < ?",
            (time.time() - ttl_days * 86400,),
        )
        letters = cur.rowcount
    return {"deleted_threads": len(idle), "trimmed_threads": len(trim),
            "deleted_dead_letters": letters}


async def _main() -> None:
    settings = get_settings()
    configure_logging(settings)
    if settings.database_url is None:
        raise SystemExit("DATABASE_URL is required")
    url = settings.database_url.get_secret_value()
    if url.startswith("sqlite"):
        db = await SqliteDB.open(sqlite_path(url))
        async with aiosqlite.connect(sqlite_path(url)) as conn:
            stats = await purge_sqlite(AsyncSqliteSaver(conn), db, settings.conversation_ttl_days)
        await db.close()
        log.info("retention.done", **stats)
        return
    async with AsyncConnectionPool(
        settings.database_url.get_secret_value(),
        connection_class=AsyncConnection[DictRow],
        kwargs={"autocommit": True, "prepare_threshold": 0, "row_factory": dict_row},
    ) as pool:
        stats = await purge(pool, settings.conversation_ttl_days)
    log.info("retention.done", **stats)


if __name__ == "__main__":
    run(_main())
