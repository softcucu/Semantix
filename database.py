"""
SQLite persistence layer for C/C++ static analysis results.

The database file is named {repo_basename}_analysis.db and placed in the
current working directory.
"""

import logging
import re
import sqlite3
from pathlib import Path
from typing import Optional

logger = logging.getLogger(__name__)

_SCHEMA = """
CREATE TABLE IF NOT EXISTS functions (
    id             INTEGER PRIMARY KEY AUTOINCREMENT,
    name           TEXT    NOT NULL,
    qualified_name TEXT    NOT NULL,
    file_path      TEXT    NOT NULL,
    line_number    INTEGER NOT NULL,
    signature      TEXT,
    source_code    TEXT
);
CREATE INDEX IF NOT EXISTS idx_functions_name
    ON functions(name);
CREATE INDEX IF NOT EXISTS idx_functions_qname
    ON functions(qualified_name);

CREATE TABLE IF NOT EXISTS global_variables (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    name        TEXT    NOT NULL,
    type_info   TEXT,
    file_path   TEXT    NOT NULL,
    line_number INTEGER NOT NULL,
    source_line TEXT
);
CREATE INDEX IF NOT EXISTS idx_variables_name
    ON global_variables(name);

CREATE TABLE IF NOT EXISTS structs (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    name         TEXT    NOT NULL,
    typedef_name TEXT,
    file_path    TEXT    NOT NULL,
    line_number  INTEGER NOT NULL,
    source_code  TEXT
);
CREATE INDEX IF NOT EXISTS idx_structs_name
    ON structs(name);
CREATE INDEX IF NOT EXISTS idx_structs_typedef
    ON structs(typedef_name);

CREATE TABLE IF NOT EXISTS macros (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    name        TEXT    NOT NULL,
    value       TEXT,
    file_path   TEXT    NOT NULL,
    line_number INTEGER NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_macros_name
    ON macros(name);

CREATE TABLE IF NOT EXISTS callers (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    callee_name TEXT    NOT NULL,
    caller_name TEXT    NOT NULL,
    caller_file TEXT    NOT NULL,
    caller_line INTEGER NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_callers_callee
    ON callers(callee_name);
CREATE INDEX IF NOT EXISTS idx_callers_caller
    ON callers(caller_name);
"""


def _extract_type_info(source_line: str, var_name: str) -> str:
    """
    Heuristically extract the type portion from a variable declaration line.
    E.g. "static int g_count = 0;" with name "g_count" → "static int"
    """
    # Strip storage class keywords and the variable name + everything after
    # Pattern: everything before the last occurrence of var_name
    idx = source_line.rfind(var_name)
    if idx > 0:
        type_part = source_line[:idx].strip().rstrip("*").strip()
        # Remove trailing pointer/ref symbols that belong to the type
        type_part = re.sub(r"[\s*&]+$", "", type_part)
        return type_part or source_line.strip()
    return source_line.strip()


class Database:
    def __init__(self, repo_path: str) -> None:
        repo_name = Path(repo_path).name
        self.db_path = Path.cwd() / f"{repo_name}_analysis.db"
        logger.info("Opening database: %s", self.db_path)
        self._conn = sqlite3.connect(str(self.db_path))
        self._conn.row_factory = sqlite3.Row
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._create_schema()

    @classmethod
    def open(cls, db_path: str) -> "Database":
        """Open an existing database by direct path (bypasses repo-name derivation)."""
        obj = object.__new__(cls)
        obj.db_path = Path(db_path)
        logger.info("Opening database: %s", obj.db_path)
        obj._conn = sqlite3.connect(str(obj.db_path))
        obj._conn.row_factory = sqlite3.Row
        obj._conn.execute("PRAGMA journal_mode=WAL")
        obj._create_schema()
        return obj

    def _create_schema(self) -> None:
        with self._conn:
            for statement in _SCHEMA.strip().split(";"):
                stmt = statement.strip()
                if stmt:
                    self._conn.execute(stmt)

    # ------------------------------------------------------------------
    # Write
    # ------------------------------------------------------------------

    def store_analysis(self, analysis_result: dict) -> None:
        """Clear previous data and store fresh analysis results (idempotent)."""
        with self._conn:
            for table in ("functions", "global_variables", "structs", "macros", "callers"):
                self._conn.execute(f"DELETE FROM {table}")

            for f in analysis_result.get("functions", []):
                self._conn.execute(
                    "INSERT INTO functions"
                    " (name, qualified_name, file_path, line_number, signature, source_code)"
                    " VALUES (?,?,?,?,?,?)",
                    (
                        f["name"],
                        f["qualified_name"],
                        f["file_path"],
                        f["line_number"],
                        f.get("signature", ""),
                        f.get("source_code", ""),
                    ),
                )

            for v in analysis_result.get("global_variables", []):
                source_line = v.get("source_line", "")
                type_info = _extract_type_info(source_line, v["name"])
                self._conn.execute(
                    "INSERT INTO global_variables"
                    " (name, type_info, file_path, line_number, source_line)"
                    " VALUES (?,?,?,?,?)",
                    (v["name"], type_info, v["file_path"], v["line_number"], source_line),
                )

            for s in analysis_result.get("structs", []):
                self._conn.execute(
                    "INSERT INTO structs"
                    " (name, typedef_name, file_path, line_number, source_code)"
                    " VALUES (?,?,?,?,?)",
                    (
                        s["name"],
                        s.get("typedef_name"),
                        s["file_path"],
                        s["line_number"],
                        s.get("source_code", ""),
                    ),
                )

            for m in analysis_result.get("macros", []):
                self._conn.execute(
                    "INSERT INTO macros (name, value, file_path, line_number)"
                    " VALUES (?,?,?,?)",
                    (m["name"], m.get("value", ""), m["file_path"], m["line_number"]),
                )

            for func_key, caller_list in analysis_result.get("callers", {}).items():
                for c in caller_list:
                    self._conn.execute(
                        "INSERT INTO callers"
                        " (callee_name, caller_name, caller_file, caller_line)"
                        " VALUES (?,?,?,?)",
                        (func_key, c["caller_name"], c["file"], c["line"]),
                    )

        logger.info(
            "Stored: %d functions, %d variables, %d structs, %d macros",
            len(analysis_result.get("functions", [])),
            len(analysis_result.get("global_variables", [])),
            len(analysis_result.get("structs", [])),
            len(analysis_result.get("macros", [])),
        )

    # ------------------------------------------------------------------
    # Read — all queries support both short and qualified names
    # ------------------------------------------------------------------

    def get_function(self, name: str) -> Optional[dict]:
        """
        Look up a function by short name or qualified name.
        Prefers exact qualified match, then exact short-name match, then suffix match.
        """
        cur = self._conn.execute(
            """
            SELECT * FROM functions
            WHERE qualified_name = ?
               OR name = ?
               OR qualified_name LIKE ?
            ORDER BY
              CASE
                WHEN qualified_name = ? THEN 0
                WHEN name = ?           THEN 1
                ELSE 2
              END
            LIMIT 1
            """,
            (name, name, f"%::{name}", name, name),
        )
        row = cur.fetchone()
        return dict(row) if row else None

    def get_variable(self, name: str) -> Optional[dict]:
        cur = self._conn.execute(
            "SELECT * FROM global_variables WHERE name = ? OR name LIKE ? LIMIT 1",
            (name, f"%::{name}"),
        )
        row = cur.fetchone()
        return dict(row) if row else None

    def get_struct(self, name: str) -> Optional[dict]:
        """Accepts both the struct tag name and the typedef alias."""
        cur = self._conn.execute(
            """
            SELECT * FROM structs
            WHERE name = ?
               OR typedef_name = ?
               OR name LIKE ?
               OR typedef_name LIKE ?
            LIMIT 1
            """,
            (name, name, f"%::{name}", f"%{name}%"),
        )
        row = cur.fetchone()
        return dict(row) if row else None

    def get_callers(self, func_name: str) -> list:
        """Return all callers of func_name (matched by qualified or suffix)."""
        cur = self._conn.execute(
            """
            SELECT * FROM callers
            WHERE callee_name = ?
               OR callee_name LIKE ?
            ORDER BY caller_file, caller_line
            """,
            (func_name, f"%::{func_name}"),
        )
        return [dict(row) for row in cur.fetchall()]

    def get_macro(self, name: str) -> Optional[dict]:
        cur = self._conn.execute(
            "SELECT * FROM macros WHERE name = ? LIMIT 1",
            (name,),
        )
        row = cur.fetchone()
        return dict(row) if row else None

    def search_function(self, pattern: str) -> list:
        """LIKE search across name and qualified_name. Auto-wraps plain text with %."""
        if "%" not in pattern and "_" not in pattern:
            sql_pattern = f"%{pattern}%"
        else:
            sql_pattern = pattern
        cur = self._conn.execute(
            """
            SELECT * FROM functions
            WHERE name LIKE ? OR qualified_name LIKE ?
            ORDER BY qualified_name
            """,
            (sql_pattern, sql_pattern),
        )
        return [dict(row) for row in cur.fetchall()]

    def stats(self) -> dict:
        """Return row counts for all tables."""
        result = {}
        for table in ("functions", "global_variables", "structs", "macros", "callers"):
            cur = self._conn.execute(f"SELECT COUNT(*) FROM {table}")
            result[table] = cur.fetchone()[0]
        return result

    def close(self) -> None:
        self._conn.close()

    def __enter__(self):
        return self

    def __exit__(self, *_):
        self.close()
