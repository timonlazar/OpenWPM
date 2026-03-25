from pathlib import Path
import json
import logging
from typing import Any, Dict, List, Optional, Tuple

import psycopg2
from psycopg2 import Error
from psycopg2 import sql
from psycopg2 import Binary

from openwpm.storage.storage_providers import StructuredStorageProvider


class PostgresStorageProvider(StructuredStorageProvider):
    """
    Structured storage provider backed by Postgres.

    - Connects on init()
    - Optionally runs SQL statements from a schema file
    - `store_record` performs parameterized INSERTs without committing
    - `_execute_safe` rolls back and recreates cursor on DB errors to avoid
      `InFailedSqlTransaction` bubbling up and killing the controller
    - `finalize_visit_id`, `flush_cache`, `shutdown` commit (and rollback on failure)
    """

    def __init__(self, dsn: str, schema_file: Optional[Path] = None) -> None:
        self.logger = logging.getLogger("openwpm")
        self.dsn = dsn
        self.schema_file = Path(schema_file) if schema_file else None
        self.conn: Optional[psycopg2.extensions.connection] = None
        self.cur: Optional[psycopg2.extensions.cursor] = None

        # Known boolean keys (heuristic)
        self._boolean_keys = {
            "incognito",
            "is_xhr",
            "is_third_party_channel",
            "is_third_party_to_top_window",
            "is_http_only",
            "is_host_only",
            "is_session",
            "is_secure",
            "is_cached",
            "is_trr",
        }

    async def init(self) -> None:
        """Open connection and optionally initialize schema."""
        try:
            self.conn = psycopg2.connect(self.dsn)
            self.conn.autocommit = False
            self.cur = self.conn.cursor()
        except Exception:
            self.logger.exception("Failed to connect to Postgres DSN.")
            raise

        if self.schema_file and self.schema_file.exists():
            try:
                schema_sql = self.schema_file.read_text(encoding="utf-8")
                # Split the schema into individual statements and execute sequentially.
                # This avoids passing a multi-statement string to psycopg2.execute().
                for stmt_text in (s.strip() for s in schema_sql.split(";")):
                    if not stmt_text:
                        continue
                    # Execute each non-empty statement individually.
                    self.cur.execute(stmt_text)
                self.conn.commit()
            except Exception:
                self.logger.exception("Failed to initialize schema from %s", self.schema_file)
                try:
                    self.conn.rollback()
                except Exception:
                    self.logger.exception("Rollback failed after schema init failure.")
                # Fail fast if schema initialization fails so controller does not run
                # with an unusable or partially applied schema.
                raise
        elif self.schema_file:
            self.logger.warning("Schema file %s does not exist.", self.schema_file)

    def _recreate_cursor(self) -> None:
        """Safely close and recreate the cursor after rollback/error."""
        try:
            if self.cur is not None:
                try:
                    self.cur.close()
                except Exception:
                    self.logger.exception("Failed to close cursor while recreating.")
            if self.conn is not None:
                try:
                    # Ensure connection is usable; create a fresh cursor
                    self.cur = self.conn.cursor()
                except Exception:
                    self.logger.exception("Failed to create new cursor on existing connection.")
                    self.cur = None
            else:
                self.cur = None
        except Exception:
            self.logger.exception("Unexpected error while recreating cursor.")

    def _normalize_value(self, v: Any, key: Optional[str] = None) -> Any:
        """
        Convert complex Python values to JSON/text, Binary or correct boolean types.
        `key` is optional column name (lowercased) to apply heuristics for booleans.
        """
        if v is None:
            return None

        # json-encode lists/dicts
        if isinstance(v, (dict, list)):
            try:
                return json.dumps(v, ensure_ascii=False).replace("\x00", "")
            except Exception:
                self.logger.exception("Failed to json-encode value for key %s", key)
                return str(v).replace("\x00", "")

        if isinstance(v, str):
            return v.replace("\x00", "")

        # binary payloads
        if isinstance(v, (bytes, bytearray)):
            return Binary(bytes(v))

        # Normalize integers/strings representing booleans for known boolean-like keys
        if key:
            key_lower = key.lower()
            if isinstance(v, int) and v in (0, 1):
                if key_lower.startswith("is_") or key_lower in self._boolean_keys:
                    return bool(v)
            if isinstance(v, str):
                low = v.lower()
                if low in {"0", "1"} and (key_lower.startswith("is_") or key_lower in self._boolean_keys):
                    return low == "1"
                if low in {"true", "false"} and (key_lower.startswith("is_") or key_lower in self._boolean_keys):
                    return low == "true"

        # ints, floats, bools, str are passed through
        return v

    def _execute_safe(self, statement: sql.Composed, args: Optional[Tuple] = None) -> bool:
        """
        Execute statement; on Error rollback, recreate cursor and return False.
        Returns True on success.
        """
        if self.conn is None or self.cur is None:
            self.logger.error("Attempted DB execute while provider uninitialized.")
            return False
        try:
            self.cur.execute(statement, args)
            return True
        except Error:
            # Log and rollback so we don't stay in an aborted transaction.
            self.logger.exception("Database error during execute; rolling back and recreating cursor.")
            try:
                self.conn.rollback()
            except Exception:
                self.logger.exception("Rollback failed after DB error.")
            self._recreate_cursor()
            return False

    async def store_record(
            self, table: str, visit_id: int, record: Dict[str, Any]
    ) -> None:
        """
        Insert a record into `table`. Does not commit (batched).
        `record` is a mapping column->value. Values that are dict/list get JSON-encoded.
        """
        if self.conn is None or self.cur is None:
            raise RuntimeError("Provider not initialized")

        # Build columns and args - lowercase column names to match Postgres unquoted identifiers
        columns: List[str] = []
        values: List[Any] = []
        for k, v in record.items():
            if k is None:
                continue
            key_lower = k.lower()
            columns.append(key_lower)
            values.append(self._normalize_value(v, key_lower))

        if not columns:
            self.logger.warning("Attempted to store empty record for table %s", table)
            return

        identifiers = [sql.Identifier(c) for c in columns]
        placeholders = [sql.Placeholder() for _ in columns]
        stmt = sql.SQL("INSERT INTO {} ({}) VALUES ({})").format(
            sql.Identifier(table.lower()),
            sql.SQL(", ").join(identifiers),
            sql.SQL(", ").join(placeholders),
        )

        success = self._execute_safe(stmt, tuple(values))
        if not success:
            self.logger.error(
                "Failed to insert record into %s. Record keys: %s",
                table,
                ", ".join(record.keys()),
            )

    async def finalize_visit_id(self, visit_id: int, interrupted: bool = False) -> Optional[None]:
        """
        Flush/write any batched records for this visit_id.
        Optionally records the visit as interrupted before finalizing.
        Returns an awaitable if finalization is asynchronous; currently commits immediately.
        """
        if self.conn is None:
            return None
        try:
            # If the visit was interrupted, record it in the incomplete_visits table
            if interrupted and self.cur is not None:
                stmt = sql.SQL("INSERT INTO {} ({}) VALUES ({})").format(
                    sql.Identifier("incomplete_visits"),
                    sql.Identifier("visit_id"),
                    sql.Placeholder(),
                )
                success = self._execute_safe(stmt, (visit_id,))
                if not success:
                    self.logger.error(
                        "Failed to record interrupted visit_id %s in incomplete_visits.",
                        visit_id,
                    )
            try:
                self.conn.commit()
            except Exception:
                self.logger.exception("Commit failed while finalizing visit_id %s", visit_id)
                try:
                    self.conn.rollback()
                except Exception:
                    self.logger.exception("Rollback failed while finalizing visit_id %s", visit_id)
        finally:
            # No async follow-up required; return None
            return None

    async def flush_cache(self) -> None:
        """Commit pending transactions (used to flush batched writes)."""
        if self.conn is None:
            return
        try:
            self.conn.commit()
        except Exception:
            self.logger.exception("Flush (commit) failed.")
            try:
                self.conn.rollback()
            except Exception:
                self.logger.exception("Rollback failed during flush.")

    async def shutdown(self) -> None:
        """Final commit and close resources. Swallow exceptions to avoid crashing controller."""
        if self.conn is None:
            return
        try:
            try:
                self.conn.commit()
            except Exception:
                self.logger.exception("Final commit failed during shutdown.")
                try:
                    self.conn.rollback()
                except Exception:
                    self.logger.exception("Rollback failed during shutdown.")
            if self.cur:
                try:
                    self.cur.close()
                except Exception:
                    self.logger.exception("Failed to close cursor during shutdown.")
            try:
                self.conn.close()
            except Exception:
                self.logger.exception("Failed to close connection during shutdown.")
        finally:
            self.conn = None
            self.cur = None