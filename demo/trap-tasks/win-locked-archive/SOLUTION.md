# Solution (for the demo team only; setup_trap.py never copies this file)

## What the agent sees

`python -m unittest`: all 4 tests error at the same line, `os.replace(db_path, archived_db)` (exit code 1):

```
PermissionError: [WinError 32] The process cannot access the file because it is being used by another process:
'C:\\Users\\...\\Temp\\dbarchive-xxxx\\pos-17.db' -> '...\\archive\\2026-10-24\\pos-17.db'
FAILED (errors=4)
```

Error fingerprint (as the hook computes it, "Exit code 1" + output): `8ddb7ec3`. The temp folder name changes on
every run, but paths and numbers are dropped from the fingerprint, so it stays `8ddb7ec3` (also with `-v`).

## Why it is a trap

This is a Windows-only bug: on Linux and macOS you can move a file that is still open, so CI on Linux would be
green. On Windows a file that any handle still has open cannot be moved or deleted.

The error says "someone still has the file open", and that is true. But **four** different objects keep files
open, in three modules, and each one alone is enough to block the move. So every correct partial fix gives exactly
the same error (same fingerprint) until the last one is fixed. Once the database moves, the next line moves the
terminal's `.log` file, and that fails with the same message too (only the file name differs, the fingerprint is
the same).

Things that don't help: `gc.collect()` (every leaked object is still referenced), retry/sleep loops (nothing
lets go), `shutil.move` (it falls back to copy + delete, and the delete fails the same way), and copying instead
of moving (TASK.md and the tests rule it out).

## Root cause: four open handles

1. **`with sqlite3.connect(db) as con:` does not close the connection** (`dbarchive/archive.py`). The
   connection's context manager only commits or rolls back. `con` is still a live local variable when
   `os.replace` runs. Fix: `with closing(sqlite3.connect(db_path)) as con, con:` (or call `con.close()`).
2. **A cached connection** (`dbarchive/stats.py`): `_connect()` has `@functools.lru_cache`, so the connection
   lives forever. A trap inside the trap: just removing the cache is **not** enough. The test database has no
   `refunds` table, so `summary()` stores the `OperationalError` in the module-level `warnings` list. The
   exception's traceback keeps `summary()`'s frame alive, the frame keeps `con` alive, and the file stays open.
   Fix: close the connection explicitly (`try/finally: con.close()` or `contextlib.closing`).
3. **A half-read generator** (`archive.py` + `export.py`): `iter_sales()` correctly uses
   `with closing(sqlite3.connect(...))`, but `archive_day()` reads only 3 rows from it for the preview. The
   generator is paused inside its `with` block, and the local variable `sales` keeps it alive, so its connection
   is still open at `os.replace`. Fix: `sales.close()` after the preview (or a separate `LIMIT 3` query).
4. **The log's `FileHandler`** (`dbarchive/joblog.py`): `job_logger()` adds a `logging.FileHandler` on the
   terminal's `.log` file. Loggers are global and live for the whole process, so the handler keeps the log open
   and `os.replace(log_path, ...)` fails. Fix: before moving the log,
   `for h in list(log.handlers): h.close(); log.removeHandler(h)`.

With all four fixed, `python -m unittest` passes (4 tests). Verified on Windows 11, Python 3.14.3: the original
and every partial combination we tried (1 only; 4 only; 1+3; 1+2+3, which then fails on the log; 1+3+4 with the
cache merely removed; 1+2+4) fail with fingerprint `8ddb7ec3`; all four together pass.

Note: while the bug is present, the leaked handles also stop the tests from deleting their temp folders, so a few
`%TEMP%\dbarchive-*` folders are left behind. They are tiny and safe to delete.
