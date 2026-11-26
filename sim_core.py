"""SQLite-backed simulation core for contiguous memory reclamation.

This module is intentionally independent of Flask so the important state
transition and rollback rules can be tested with the standard library.
"""

from __future__ import annotations

import json
import sqlite3
from contextlib import contextmanager
from datetime import datetime, timezone
from typing import Any

SLOT_COUNT = 16
PAGE_SIZE = 4096
TARGET_SIZE = 4
DISK_PAGE_COUNT = 32
OLD_VM = "old-vm"
NEW_VM = "new-vm"


class SimError(Exception):
    """Base class for expected simulation/request errors."""

    status_code = 400


class ValidationError(SimError):
    status_code = 400


class NotFoundError(SimError):
    status_code = 404


class ConflictError(SimError):
    status_code = 409


class BatchRolledBack(ConflictError):
    """Raised after a simulated writeback failure and SQLite rollback."""

    status_code = 409

    def __init__(self, message: str, batch: dict[str, Any]):
        super().__init__(message)
        self.batch = batch


class WritebackSimulated(Exception):
    def __init__(self, ordinal: int, page_id: int, slot_no: int):
        self.ordinal = ordinal
        self.page_id = page_id
        self.slot_no = slot_no
        super().__init__(
            f"simulated writeback failure: ordinal={ordinal}, "
            f"page={page_id}, slot={slot_no}"
        )


def now_iso() -> str:
    return datetime.now(timezone.utc).astimezone().isoformat(timespec="seconds")


def connect(db_path: str) -> sqlite3.Connection:
    conn = sqlite3.connect(db_path, timeout=15)
    conn.row_factory = sqlite3.Row
    conn.isolation_level = None  # explicit BEGIN/COMMIT/ROLLBACK
    conn.execute("PRAGMA foreign_keys=ON")
    try:
        conn.execute("PRAGMA journal_mode=WAL")
    except sqlite3.DatabaseError:
        # Some in-memory or unusual filesystems do not support WAL.
        pass
    return conn


@contextmanager
def immediate_transaction(conn: sqlite3.Connection):
    """Serialize state-changing guest operations with batch execution."""
    conn.execute("BEGIN IMMEDIATE")
    try:
        yield
        conn.commit()
    except Exception:
        conn.rollback()
        raise


def init_db(conn: sqlite3.Connection) -> None:
    conn.executescript(
        """
        CREATE TABLE IF NOT EXISTS slots (
            slot_no INTEGER PRIMARY KEY CHECK (slot_no BETWEEN 0 AND 15),
            owner TEXT,
            role TEXT,
            page_id INTEGER,
            dirty INTEGER NOT NULL DEFAULT 0 CHECK (dirty IN (0, 1)),
            content TEXT,
            updated_at TEXT
        );

        CREATE TABLE IF NOT EXISTS disk_pages (
            page_id INTEGER PRIMARY KEY CHECK (page_id BETWEEN 0 AND 65535),
            content TEXT NOT NULL,
            write_count INTEGER NOT NULL DEFAULT 0,
            updated_at TEXT
        );

        CREATE TABLE IF NOT EXISTS batches (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            target_start INTEGER NOT NULL,
            target_slots TEXT NOT NULL,
            move_quota INTEGER NOT NULL,
            writeback_quota INTEGER NOT NULL,
            fail_on_writeback_index INTEGER NOT NULL DEFAULT 0,
            status TEXT NOT NULL,
            preliminary_plan_json TEXT,
            plan_json TEXT,
            result_json TEXT,
            error TEXT,
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL
        );

        CREATE TABLE IF NOT EXISTS events (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            ts TEXT NOT NULL,
            kind TEXT NOT NULL,
            message TEXT NOT NULL,
            detail_json TEXT
        );

        CREATE TABLE IF NOT EXISTS kv (
            key TEXT PRIMARY KEY,
            value TEXT
        );

        CREATE UNIQUE INDEX IF NOT EXISTS idx_one_old_slot_per_page
            ON slots(page_id)
            WHERE owner = 'old-vm' AND page_id IS NOT NULL;
        """
    )
    count = conn.execute("SELECT COUNT(*) AS c FROM slots").fetchone()["c"]
    if count == 0:
        seed_sample(conn)


def reset_sample(conn: sqlite3.Connection) -> None:
    conn.executescript(
        """
        DROP TABLE IF EXISTS slots;
        DROP TABLE IF EXISTS disk_pages;
        DROP TABLE IF EXISTS batches;
        DROP TABLE IF EXISTS events;
        DROP TABLE IF EXISTS kv;
        """
    )
    init_db(conn)


def add_event(
    conn: sqlite3.Connection,
    kind: str,
    message: str,
    detail: dict[str, Any] | None = None,
) -> None:
    conn.execute(
        """
        INSERT INTO events(ts, kind, message, detail_json)
        VALUES (?, ?, ?, ?)
        """,
        (now_iso(), kind, message, json.dumps(detail or {}, ensure_ascii=False)),
    )


def get_kv(conn: sqlite3.Connection, key: str, default: str | None = None) -> str | None:
    row = conn.execute("SELECT value FROM kv WHERE key = ?", (key,)).fetchone()
    return row["value"] if row else default


def set_kv(conn: sqlite3.Connection, key: str, value: str) -> None:
    conn.execute(
        """
        INSERT INTO kv(key, value) VALUES (?, ?)
        ON CONFLICT(key) DO UPDATE SET value = excluded.value
        """,
        (key, value),
    )


def delete_kv(conn: sqlite3.Connection, key: str) -> None:
    conn.execute("DELETE FROM kv WHERE key = ?", (key,))


def seed_sample(conn: sqlite3.Connection) -> None:
    ts = now_iso()

    for slot_no in range(SLOT_COUNT):
        conn.execute(
            """
            INSERT INTO slots(slot_no, owner, role, page_id, dirty, content, updated_at)
            VALUES (?, NULL, 'free', NULL, 0, NULL, ?)
            """,
            (slot_no, ts),
        )

    for page_id in range(DISK_PAGE_COUNT):
        conn.execute(
            """
            INSERT INTO disk_pages(page_id, content, write_count, updated_at)
            VALUES (?, ?, 0, ?)
            """,
            (
                page_id,
                f"DISK-P{page_id:02d}: stable backing-page content; uncached pages can be faulted.",
                ts,
            ),
        )

    # Required sample for target slots 0..3:
    # slot 0: dirty page 0
    # slot 1: clean page 1
    # slot 2: dirty page 2
    # slot 3: free
    dirty0 = "LIVE-P00: dirty write in slot 0, not yet on backing disk."
    dirty2 = "LIVE-P02: dirty write in slot 2, not yet on backing disk."
    clean1 = conn.execute(
        "SELECT content FROM disk_pages WHERE page_id = 1"
    ).fetchone()["content"]

    mappings: list[tuple[int, int, int, str]] = [
        (0, 0, 1, dirty0),
        (1, 1, 0, clean1),
        (2, 2, 1, dirty2),
    ]

    # Slots 4..14 cache clean disk pages 3..13, leaving slot 15 as the only
    # free slot outside the default target area 0..3.
    for slot_no in range(4, 15):
        page_id = slot_no - 1
        content = conn.execute(
            "SELECT content FROM disk_pages WHERE page_id = ?", (page_id,)
        ).fetchone()["content"]
        mappings.append((slot_no, page_id, 0, content))

    for slot_no, page_id, dirty, content in mappings:
        conn.execute(
            """
            UPDATE slots
               SET owner = ?, role = 'cache', page_id = ?, dirty = ?,
                   content = ?, updated_at = ?
             WHERE slot_no = ?
            """,
            (OLD_VM, page_id, dirty, content, ts, slot_no),
        )

    set_kv(conn, "sample", "default-16-slot-layout")
    add_event(
        conn,
        "reset",
        "已载入样例：0/2 槽为脏页，1 槽为干净页，3 槽空；区外仅 15 槽空；盘上有未缓存页。",
        {
            "target0": [
                "slot0 dirty P0",
                "slot1 clean P1",
                "slot2 dirty P2",
                "slot3 free",
            ],
            "outside_free_slots": [15],
            "uncached_disk_pages": [14, 15, 16, 17],
        },
    )
    conn.commit()


def as_int(value: Any, name: str, minimum: int = 0, maximum: int = 65535) -> int:
    if isinstance(value, bool):
        # JSON true is not a valid numeric quota/page number here.
        raise ValidationError(f"{name} 必须是整数")
    try:
        ivalue = int(value)
    except (TypeError, ValueError) as exc:
        raise ValidationError(f"{name} 必须是整数") from exc
    if ivalue < minimum or ivalue > maximum:
        raise ValidationError(f"{name} 必须在 {minimum}..{maximum} 之间")
    return ivalue


def validate_target_start(target_start: Any) -> int:
    start = as_int(target_start, "首槽编号", minimum=0, maximum=SLOT_COUNT - TARGET_SIZE)
    if start % TARGET_SIZE != 0:
        raise ValidationError(
            f"首槽编号必须是 {TARGET_SIZE} 的倍数（可选 0、4、8、12）"
        )
    return start


def target_slot_numbers(target_start: int) -> list[int]:
    return list(range(target_start, target_start + TARGET_SIZE))


def external_free_slots(
    conn: sqlite3.Connection, target_start: int | None
) -> list[sqlite3.Row]:
    if target_start is None:
        rows = conn.execute(
            """
            SELECT * FROM slots
             WHERE owner IS NULL AND page_id IS NULL
             ORDER BY slot_no
            """
        ).fetchall()
        return list(rows)

    targets = target_slot_numbers(target_start)
    placeholders = ",".join("?" for _ in targets)
    rows = conn.execute(
        f"""
        SELECT * FROM slots
         WHERE owner IS NULL AND page_id IS NULL
           AND slot_no NOT IN ({placeholders})
         ORDER BY slot_no
        """,
        targets,
    ).fetchall()
    return list(rows)


def compute_plan(
    conn: sqlite3.Connection,
    target_start: Any,
    move_quota: Any,
    writeback_quota: Any,
) -> dict[str, Any]:
    start = validate_target_start(target_start)
    move_quota = as_int(move_quota, "搬页限额", 0, SLOT_COUNT)
    writeback_quota = as_int(writeback_quota, "写回限额", 0, SLOT_COUNT)
    targets = target_slot_numbers(start)

    placeholders = ",".join("?" for _ in targets)
    target_rows = conn.execute(
        f"SELECT * FROM slots WHERE slot_no IN ({placeholders}) ORDER BY slot_no",
        targets,
    ).fetchall()

    if any(row["owner"] == NEW_VM for row in target_rows):
        raise ConflictError("目标槽中已有分配给新虚拟机的大页，不能再次整理")

    actions: list[dict[str, Any]] = []
    dirty_rows: list[sqlite3.Row] = []
    already_free = 0
    clean_count = 0

    for row in target_rows:
        if row["owner"] is None and row["page_id"] is None:
            already_free += 1
            actions.append(
                {
                    "type": "already_free",
                    "slot_no": row["slot_no"],
                    "label": f"槽 {row['slot_no']} 已空",
                }
            )
        elif row["owner"] == OLD_VM and row["page_id"] is not None:
            page_id = row["page_id"]
            if row["dirty"]:
                dirty_rows.append(row)
                actions.append(
                    {
                        "type": "pending_dirty",
                        "slot_no": row["slot_no"],
                        "page_id": page_id,
                        "dirty": True,
                        "label": f"槽 {row['slot_no']} 的脏页 P{page_id:02d} 待决定",
                    }
                )
            else:
                clean_count += 1
                actions.append(
                    {
                        "type": "release_clean",
                        "slot_no": row["slot_no"],
                        "page_id": page_id,
                        "label": f"直接释放槽 {row['slot_no']} 的干净页 P{page_id:02d}",
                    }
                )
        else:
            raise ConflictError(
                f"槽 {row['slot_no']} 状态不受支持：owner={row['owner']!r}"
            )

    outside = external_free_slots(conn, start)
    outside_numbers = [row["slot_no"] for row in outside]

    move_count = min(move_quota, len(outside_numbers), len(dirty_rows))
    writeback_count = min(
        writeback_quota, max(0, len(dirty_rows) - move_count)
    )

    final_actions: list[dict[str, Any]] = []
    # Preserve non-dirty/free entries first for a readable plan.
    for action in actions:
        if action["type"] != "pending_dirty":
            final_actions.append(action)

    for index, row in enumerate(dirty_rows):
        page_id = row["page_id"]
        slot_no = row["slot_no"]
        if index < move_count:
            to_slot = outside_numbers[index]
            final_actions.append(
                {
                    "type": "move_dirty",
                    "slot_no": slot_no,
                    "from_slot": slot_no,
                    "to_slot": to_slot,
                    "page_id": page_id,
                    "label": (
                        f"搬移脏页 P{page_id:02d}：槽 {slot_no} → 区外空槽 {to_slot}"
                    ),
                }
            )
        elif index < move_count + writeback_count:
            wb_ordinal = index - move_count + 1
            final_actions.append(
                {
                    "type": "writeback_dirty",
                    "slot_no": slot_no,
                    "page_id": page_id,
                    "writeback_ordinal": wb_ordinal,
                    "label": (
                        f"第 {wb_ordinal} 次写回：槽 {slot_no} 的 P{page_id:02d}"
                        " → 后备盘，然后释放"
                    ),
                }
            )
        else:
            final_actions.append(
                {
                    "type": "deferred_dirty",
                    "slot_no": slot_no,
                    "page_id": page_id,
                    "label": f"限额不足，槽 {slot_no} 的脏页 P{page_id:02d} 暂留",
                }
            )

    final_actions.sort(key=lambda item: item["slot_no"])
    free_after = already_free + clean_count + move_count + writeback_count
    return {
        "target_start": start,
        "target_slots": targets,
        "move_quota": move_quota,
        "writeback_quota": writeback_quota,
        "outside_free_slots": outside_numbers,
        "already_free": already_free,
        "clean_releases": clean_count,
        "moves": move_count,
        "writebacks": writeback_count,
        "deferred": len(dirty_rows) - move_count - writeback_count,
        "free_after_batch": free_after,
        "target_will_be_full": free_after == TARGET_SIZE,
        "actions": final_actions,
        "computed_at": now_iso(),
    }


def batch_to_dict(row: sqlite3.Row | None) -> dict[str, Any] | None:
    if row is None:
        return None
    data = dict(row)
    for key in ("target_slots", "preliminary_plan_json", "plan_json", "result_json"):
        raw = data.get(key)
        if key == "target_slots":
            data[key] = json.loads(raw) if raw else []
        elif raw:
            short_key = key.removesuffix("_json")
            data[short_key] = json.loads(raw)
    return data


def get_batch(conn: sqlite3.Connection, batch_id: int) -> dict[str, Any] | None:
    row = conn.execute("SELECT * FROM batches WHERE id = ?", (batch_id,)).fetchone()
    return batch_to_dict(row)


def get_current_batch_id(conn: sqlite3.Connection) -> int | None:
    raw = get_kv(conn, "current_batch_id")
    if not raw:
        return None
    try:
        return int(raw)
    except ValueError:
        return None


def get_current_batch(conn: sqlite3.Connection) -> dict[str, Any] | None:
    batch_id = get_current_batch_id(conn)
    if batch_id is None:
        row = conn.execute("SELECT * FROM batches ORDER BY id DESC LIMIT 1").fetchone()
        return batch_to_dict(row)
    batch = get_batch(conn, batch_id)
    if batch is None:
        row = conn.execute("SELECT * FROM batches ORDER BY id DESC LIMIT 1").fetchone()
        return batch_to_dict(row)
    return batch


def create_batch(
    conn: sqlite3.Connection,
    target_start: Any,
    move_quota: Any,
    writeback_quota: Any,
    fail_on_writeback_index: Any = 0,
    run_now: bool = False,
) -> dict[str, Any]:
    start = validate_target_start(target_start)
    move_quota = as_int(move_quota, "搬页限额", 0, SLOT_COUNT)
    writeback_quota = as_int(writeback_quota, "写回限额", 0, SLOT_COUNT)
    fail_index = as_int(fail_on_writeback_index, "写回故障序号", 0, 99)

    # Validate target state before inserting anything.
    plan = compute_plan(conn, start, move_quota, writeback_quota)
    ts = now_iso()

    current_id = get_current_batch_id(conn)
    if current_id is not None:
        conn.execute(
            """
            UPDATE batches SET status = 'cancelled', updated_at = ?
             WHERE id = ? AND status IN ('paused', 'rolled_back')
            """,
            (ts, current_id),
        )

    cur = conn.execute(
        """
        INSERT INTO batches(
            target_start, target_slots, move_quota, writeback_quota,
            fail_on_writeback_index, status, preliminary_plan_json,
            plan_json, result_json, error, created_at, updated_at
        ) VALUES (?, ?, ?, ?, ?, 'paused', ?, NULL, NULL, NULL, ?, ?)
        """,
        (
            start,
            json.dumps(target_slot_numbers(start)),
            move_quota,
            writeback_quota,
            fail_index,
            json.dumps(plan, ensure_ascii=False),
            ts,
            ts,
        ),
    )
    batch_id = cur.lastrowid
    set_kv(conn, "current_batch_id", str(batch_id))
    set_kv(conn, "active_target_start", str(start))
    add_event(
        conn,
        "batch_created",
        f"已选定目标槽 {start}..{start + 3}，搬页限额 {move_quota}，"
        f"写回限额 {writeback_quota}；整理暂停，原虚拟机可继续读写。",
        {"batch_id": batch_id, "plan": plan},
    )

    if run_now or plan["already_free"] == TARGET_SIZE:
        return execute_batch(conn, batch_id)
    return get_batch(conn, batch_id)  # type: ignore[return-value]


def update_fault(
    conn: sqlite3.Connection, batch_id: int, fail_on_writeback_index: Any
) -> dict[str, Any]:
    fail_index = as_int(fail_on_writeback_index, "写回故障序号", 0, 99)
    batch = get_batch(conn, batch_id)
    if batch is None:
        raise NotFoundError(f"找不到批次 {batch_id}")
    if batch["status"] not in ("paused", "rolled_back"):
        raise ConflictError("只有暂停或已回滚、待继续的批次可以修改故障注入")
    conn.execute(
        "UPDATE batches SET fail_on_writeback_index = ?, updated_at = ? WHERE id = ?",
        (fail_index, now_iso(), batch_id),
    )
    add_event(
        conn,
        "fault_config",
        (
            f"批次 {batch_id} 设置第 {fail_index} 次写回模拟失败"
            if fail_index
            else f"批次 {batch_id} 已取消写回故障"
        ),
        {"batch_id": batch_id, "fail_on_writeback_index": fail_index},
    )
    return get_batch(conn, batch_id)  # type: ignore[return-value]


def execute_batch(
    conn: sqlite3.Connection,
    batch_id: int,
    clear_fault: bool = False,
) -> dict[str, Any]:
    batch = get_batch(conn, batch_id)
    if batch is None:
        raise NotFoundError(f"找不到批次 {batch_id}")
    if batch["status"] not in ("paused", "rolled_back"):
        raise ConflictError(f"批次 {batch_id} 当前状态为 {batch['status']}，不能继续执行")

    if clear_fault:
        batch["fail_on_writeback_index"] = 0
        conn.execute(
            "UPDATE batches SET fail_on_writeback_index = 0, updated_at = ? WHERE id = ?",
            (now_iso(), batch_id),
        )

    start = batch["target_start"]
    move_quota = batch["move_quota"]
    writeback_quota = batch["writeback_quota"]
    fail_index = int(batch.get("fail_on_writeback_index") or 0)
    targets = batch["target_slots"]

    conn.execute("BEGIN IMMEDIATE")
    executed: list[dict[str, Any]] = []
    actual_moves = 0
    actual_writebacks = 0
    actual_clean = 0
    wb_ordinal = 0

    try:
        # Re-plan at resume time. Guest writes/faults during the pause are
        # already committed and therefore included here.
        plan = compute_plan(conn, start, move_quota, writeback_quota)

        # 1. Clean pages can be reclaimed directly.
        for action in plan["actions"]:
            if action["type"] != "release_clean":
                continue
            slot_no = action["slot_no"]
            page_id = action["page_id"]
            ts = now_iso()
            cur = conn.execute(
                """
                UPDATE slots
                   SET owner = NULL, role = 'free', page_id = NULL,
                       dirty = 0, content = NULL, updated_at = ?
                 WHERE slot_no = ? AND owner = ? AND page_id = ? AND dirty = 0
                """,
                (ts, slot_no, OLD_VM, page_id),
            )
            if cur.rowcount != 1:
                raise ConflictError(f"干净页 P{page_id:02d} 在执行前发生变化")
            actual_clean += 1
            executed.append({**action, "executed_at": ts})

        # 2. Move dirty pages to currently free slots outside the target.
        for action in plan["actions"]:
            if action["type"] != "move_dirty":
                continue
            src_no = action["from_slot"]
            dest_no = action["to_slot"]
            page_id = action["page_id"]
            src = conn.execute("SELECT * FROM slots WHERE slot_no = ?", (src_no,)).fetchone()
            dest = conn.execute("SELECT * FROM slots WHERE slot_no = ?", (dest_no,)).fetchone()
            if src is None or src["owner"] != OLD_VM or src["page_id"] != page_id or not src["dirty"]:
                raise ConflictError(f"源槽 {src_no} 的脏页状态已变化")
            if dest is None or dest["owner"] is not None or dest["page_id"] is not None:
                raise ConflictError(f"区外目标空槽 {dest_no} 已被占用，已按事务回滚")

            ts = now_iso()
            content = src["content"]
            # Clear the source first; the partial unique index permits only
            # one old-VM slot per disk page. The transaction still restores
            # both slots if a later writeback fails.
            conn.execute(
                """
                UPDATE slots
                   SET owner = NULL, role = 'free', page_id = NULL,
                       dirty = 0, content = NULL, updated_at = ?
                 WHERE slot_no = ?
                """,
                (ts, src_no),
            )
            conn.execute(
                """
                UPDATE slots
                   SET owner = ?, role = 'cache', page_id = ?, dirty = 1,
                       content = ?, updated_at = ?
                 WHERE slot_no = ? AND owner IS NULL AND page_id IS NULL
                """,
                (OLD_VM, page_id, content, ts, dest_no),
            )
            actual_moves += 1
            executed.append({**action, "content_digest": (src["content"] or "")[:80]})

        # 3. Write back remaining dirty pages using their latest content.
        for action in plan["actions"]:
            if action["type"] != "writeback_dirty":
                continue
            slot_no = action["slot_no"]
            page_id = action["page_id"]
            wb_ordinal += 1
            if fail_index and wb_ordinal == fail_index:
                raise WritebackSimulated(wb_ordinal, page_id, slot_no)

            src = conn.execute("SELECT * FROM slots WHERE slot_no = ?", (slot_no,)).fetchone()
            if src is None or src["owner"] != OLD_VM or src["page_id"] != page_id or not src["dirty"]:
                raise ConflictError(f"待写回槽 {slot_no} 的脏页状态已变化")

            ts = now_iso()
            conn.execute(
                """
                INSERT INTO disk_pages(page_id, content, write_count, updated_at)
                VALUES (?, ?, 1, ?)
                ON CONFLICT(page_id) DO UPDATE SET
                    content = excluded.content,
                    write_count = disk_pages.write_count + 1,
                    updated_at = excluded.updated_at
                """,
                (page_id, src["content"], ts),
            )
            conn.execute(
                """
                UPDATE slots
                   SET owner = NULL, role = 'free', page_id = NULL,
                       dirty = 0, content = NULL, updated_at = ?
                 WHERE slot_no = ?
                """,
                (ts, slot_no),
            )
            actual_writebacks += 1
            executed.append({**action, "writeback_ordinal_actual": wb_ordinal})

        target_rows = conn.execute(
            f"SELECT * FROM slots WHERE slot_no IN ({','.join('?' for _ in targets)})",
            targets,
        ).fetchall()
        all_free = all(row["owner"] is None and row["page_id"] is None for row in target_rows)

        allocated = False
        allocation: dict[str, Any] | None = None
        if all_free:
            ts = now_iso()
            cur = conn.execute(
                f"""
                UPDATE slots
                   SET owner = ?, role = 'hugepage', page_id = NULL,
                       dirty = 0, content = NULL, updated_at = ?
                 WHERE slot_no IN ({','.join('?' for _ in targets)})
                   AND owner IS NULL AND page_id IS NULL
                """,
                (NEW_VM, ts, *targets),
            )
            if cur.rowcount != TARGET_SIZE:
                raise ConflictError("目标槽并非全部空闲，停止分配新虚拟机大页")
            allocated = True
            allocation = {
                "target_start": start,
                "target_slots": targets,
                "allocated_at": ts,
                "batch_id": batch_id,
                "owner": NEW_VM,
            }
            set_kv(conn, "last_allocation", json.dumps(allocation, ensure_ascii=False))
            delete_kv(conn, "active_target_start")

        result = {
            "executed_at": now_iso(),
            "actual_clean_releases": actual_clean,
            "actual_moves": actual_moves,
            "actual_writebacks": actual_writebacks,
            "target_slots": targets,
            "target_all_free_before_allocation": all_free,
            "allocated_to_new_vm": allocated,
            "allocation": allocation,
            "executed_actions": executed,
        }
        status = "allocated" if allocated else "applied"
        conn.execute(
            """
            UPDATE batches
               SET status = ?, plan_json = ?, result_json = ?,
                   error = NULL, updated_at = ?
             WHERE id = ?
            """,
            (status, json.dumps(plan, ensure_ascii=False),
             json.dumps(result, ensure_ascii=False), now_iso(), batch_id),
        )
        if allocated:
            delete_kv(conn, "current_batch_id")
        add_event(
            conn,
            "batch_applied",
            (
                f"批次 {batch_id} 完成：搬页 {actual_moves}，写回 {actual_writebacks}，"
                f"干净释放 {actual_clean}。"
                + ("四槽已齐，立即分配给新虚拟机。" if allocated else "目标尚未全部腾出。")
            ),
            result,
        )
        if allocated and allocation:
            add_event(
                conn,
                "new_vm_allocated",
                f"新虚拟机获得连续四槽 {start}..{start + 3}；原VM缓存映射已清除。",
                allocation,
            )

        conn.commit()
        return get_batch(conn, batch_id)  # type: ignore[return-value]

    except WritebackSimulated as exc:
        conn.rollback()
        message = (
            f"第 {exc.ordinal} 次写回（P{exc.page_id:02d}，槽 {exc.slot_no}）"
            "模拟失败；本批整理已全部撤销。暂停期间已提交的虚拟机改写保留。"
        )
        ts = now_iso()
        rollback_result = {
            "rolled_back": True,
            "allocated_to_new_vm": False,
            # After ROLLBACK these are the durable counts shown to the operator.
            "actual_moves": 0,
            "actual_writebacks": 0,
            "actual_clean_releases": 0,
            "durable_actual_moves": 0,
            "durable_actual_writebacks": 0,
            "durable_actual_clean_releases": 0,
            "attempted_before_failure": {
                "moves": actual_moves,
                "writebacks_before_failed_ordinal": actual_writebacks,
                "clean_releases": actual_clean,
                "actions": executed,
            },
            "failed_writeback": {
                "ordinal": exc.ordinal,
                "page_id": exc.page_id,
                "slot_no": exc.slot_no,
            },
            "message": message,
        }
        conn.execute(
            """
            UPDATE batches
               SET status = 'rolled_back', error = ?, result_json = ?, updated_at = ?
             WHERE id = ?
            """,
            (message, json.dumps(rollback_result, ensure_ascii=False), ts, batch_id),
        )
        add_event(
            conn,
            "batch_rollback",
            message,
            {
                "batch_id": batch_id,
                "ordinal": exc.ordinal,
                "page_id": exc.page_id,
                "slot_no": exc.slot_no,
                "durable_counts": {
                    "moves": 0,
                    "writebacks": 0,
                    "clean_releases": 0,
                },
                "attempted_before_failure": rollback_result["attempted_before_failure"],
            },
        )
        latest = get_batch(conn, batch_id)
        raise BatchRolledBack(message, latest) from exc
    except Exception:
        conn.rollback()
        raise


def cancel_target(conn: sqlite3.Connection) -> None:
    batch = get_current_batch(conn)
    ts = now_iso()
    if batch and batch["status"] in ("paused", "rolled_back"):
        conn.execute(
            "UPDATE batches SET status='cancelled', updated_at=? WHERE id=?",
            (ts, batch["id"]),
        )
    delete_kv(conn, "active_target_start")
    delete_kv(conn, "current_batch_id")
    add_event(conn, "target_cancelled", "已取消当前整理目标；空闲槽可重新用于缺页缓存。")


def get_active_target(conn: sqlite3.Connection) -> int | None:
    raw = get_kv(conn, "active_target_start")
    if raw in (None, ""):
        return None
    try:
        start = int(raw)
        return validate_target_start(start)
    except (TypeError, ValueError, ValidationError):
        return None


def get_disk_page(conn: sqlite3.Connection, page_id: int) -> sqlite3.Row | None:
    return conn.execute("SELECT * FROM disk_pages WHERE page_id = ?", (page_id,)).fetchone()


def guest_read(conn: sqlite3.Connection, page_id: Any) -> dict[str, Any]:
    pid = as_int(page_id, "磁盘页号", 0, DISK_PAGE_COUNT - 1)

    with immediate_transaction(conn):
        cached = conn.execute(
            "SELECT * FROM slots WHERE owner = ? AND page_id = ?", (OLD_VM, pid)
        ).fetchone()

        if cached is not None:
            result = {
                "mode": "hit",
                "page_id": pid,
                "slot_no": cached["slot_no"],
                "dirty": bool(cached["dirty"]),
                "content": cached["content"],
                "message": f"命中当前缓存：槽 {cached['slot_no']}，读取最新内容。",
            }
            add_event(conn, "guest_read_hit", result["message"], result)
            return result

        target = get_active_target(conn)
        outside = external_free_slots(conn, target)
        disk = get_disk_page(conn, pid)
        disk_content = disk["content"] if disk is not None else f"DISK-P{pid:02d}: zero page."

        if outside:
            slot = outside[0]
            ts = now_iso()
            cur = conn.execute(
                """
                UPDATE slots
                   SET owner = ?, role = 'cache', page_id = ?, dirty = 0,
                       content = ?, updated_at = ?
                 WHERE slot_no = ? AND owner IS NULL AND page_id IS NULL
                """,
                (OLD_VM, pid, disk_content, ts, slot["slot_no"]),
            )
            if cur.rowcount != 1:
                raise ConflictError("选中的区外空槽状态已变化，请重试读取")
            result = {
                "mode": "miss_recached",
                "page_id": pid,
                "slot_no": slot["slot_no"],
                "protected_target_start": target,
                "content": disk_content,
                "message": (
                    f"缺页：使用区外空槽 {slot['slot_no']} 重新缓存 P{pid:02d}"
                    + (f"（目标区 {target}..{target + 3} 不可占用）。" if target is not None else "。")
                ),
            }
            add_event(conn, "guest_read_miss", result["message"], result)
            return result

        result = {
            "mode": "direct_disk",
            "page_id": pid,
            "slot_no": None,
            "content": disk_content,
            "message": "缺页且区外无空槽：直读后备盘，不建立缓存。",
        }
        add_event(conn, "guest_read_direct", result["message"], result)
        return result


def guest_write(
    conn: sqlite3.Connection, page_id: Any, content: Any, marker: bool = False
) -> dict[str, Any]:
    pid = as_int(page_id, "磁盘页号", 0, DISK_PAGE_COUNT - 1)
    if marker:
        content = f"PAUSE/UPDATE P{pid:02d} @ {now_iso()} #{datetime.now().microsecond}"
    if not isinstance(content, str):
        raise ValidationError("写入内容必须是文本")
    content = content.strip() if marker else content
    if not content:
        raise ValidationError("写入内容不能为空")
    if len(content.encode("utf-8")) > PAGE_SIZE:
        raise ValidationError(f"页内容不能超过 {PAGE_SIZE} 字节")

    with immediate_transaction(conn):
        cached = conn.execute(
            "SELECT * FROM slots WHERE owner = ? AND page_id = ?", (OLD_VM, pid)
        ).fetchone()
        ts = now_iso()

        if cached is not None:
            cur = conn.execute(
                """
                UPDATE slots
                   SET dirty = 1, content = ?, updated_at = ?
                 WHERE slot_no = ? AND owner = ? AND page_id = ?
                """,
                (content, ts, cached["slot_no"], OLD_VM, pid),
            )
            if cur.rowcount != 1:
                raise ConflictError("缓存槽归属在写入前发生变化，请重试")
            result = {
                "mode": "hit_dirty",
                "page_id": pid,
                "slot_no": cached["slot_no"],
                "dirty": True,
                "content": content,
                "message": f"命中缓存槽 {cached['slot_no']}：已改写并标记为脏，整理继续时使用此最新内容。",
            }
            add_event(conn, "guest_write_hit", result["message"], {"page_id": pid, "slot_no": cached["slot_no"]})
            return result

        target = get_active_target(conn)
        outside = external_free_slots(conn, target)
        if outside:
            slot = outside[0]
            cur = conn.execute(
                """
                UPDATE slots
                   SET owner = ?, role = 'cache', page_id = ?, dirty = 1,
                       content = ?, updated_at = ?
                 WHERE slot_no = ? AND owner IS NULL AND page_id IS NULL
                """,
                (OLD_VM, pid, content, ts, slot["slot_no"]),
            )
            if cur.rowcount != 1:
                raise ConflictError("选中的区外空槽状态已变化，请重试写入")
            result = {
                "mode": "miss_recached_dirty",
                "page_id": pid,
                "slot_no": slot["slot_no"],
                "dirty": True,
                "content": content,
                "message": f"写缺页：已在区外空槽 {slot['slot_no']} 建脏缓存。",
            }
            add_event(conn, "guest_write_miss", result["message"], {"page_id": pid, "slot_no": slot["slot_no"]})
            return result

        conn.execute(
            """
            INSERT INTO disk_pages(page_id, content, write_count, updated_at)
            VALUES (?, ?, 1, ?)
            ON CONFLICT(page_id) DO UPDATE SET
                content = excluded.content,
                write_count = disk_pages.write_count + 1,
                updated_at = excluded.updated_at
            """,
            (pid, content, ts),
        )
        result = {
            "mode": "direct_disk_write",
            "page_id": pid,
            "slot_no": None,
            "dirty": False,
            "content": content,
            "message": "缺页且区外无空槽：已直写后备盘。",
        }
        add_event(conn, "guest_write_direct", result["message"], {"page_id": pid})
        return result


def get_state(conn: sqlite3.Connection) -> dict[str, Any]:
    slots = [dict(row) for row in conn.execute("SELECT * FROM slots ORDER BY slot_no")]
    disk = [dict(row) for row in conn.execute("SELECT * FROM disk_pages ORDER BY page_id")]
    cached_slot_by_page = {
        row["page_id"]: row["slot_no"]
        for row in conn.execute(
            "SELECT page_id, slot_no FROM slots WHERE owner = ? AND page_id IS NOT NULL",
            (OLD_VM,),
        )
    }
    for page in disk:
        page["cached_slot"] = cached_slot_by_page.get(page["page_id"])

    events = [
        dict(row)
        for row in conn.execute("SELECT * FROM events ORDER BY id DESC LIMIT 30")
    ]
    for event in events:
        raw = event.pop("detail_json", None)
        try:
            event["detail"] = json.loads(raw) if raw else {}
        except json.JSONDecodeError:
            event["detail"] = {}

    batch = get_current_batch(conn)
    live_plan = None
    if batch and batch["status"] in ("paused", "rolled_back"):
        try:
            live_plan = compute_plan(
                conn,
                batch["target_start"],
                batch["move_quota"],
                batch["writeback_quota"],
            )
        except SimError as exc:
            live_plan = {"error": str(exc)}

    allocation_raw = get_kv(conn, "last_allocation")
    allocation = json.loads(allocation_raw) if allocation_raw else None
    target = get_active_target(conn)

    free_count = sum(1 for slot in slots if slot["owner"] is None and slot["page_id"] is None)
    old_count = sum(1 for slot in slots if slot["owner"] == OLD_VM)
    new_count = sum(1 for slot in slots if slot["owner"] == NEW_VM)
    dirty_count = sum(1 for slot in slots if slot["owner"] == OLD_VM and slot["dirty"])

    return {
        "slots": slots,
        "disk_pages": disk,
        "events": events,
        "current_batch": batch,
        "live_plan": live_plan,
        "active_target_start": target,
        "allocation": allocation,
        "choices": {"target_starts": [0, 4, 8, 12], "disk_pages": list(range(DISK_PAGE_COUNT))},
        "stats": {
            "free_slots": free_count,
            "old_vm_slots": old_count,
            "new_vm_slots": new_count,
            "dirty_slots": dirty_count,
            "outside_free_slots": len(external_free_slots(conn, target)),
        },
    }
