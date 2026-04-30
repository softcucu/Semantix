"""
C/C++ static analysis using Exuberant Ctags 5.9 and cscope 15.9.

Extracts functions (with full C++ qualified names), global variables,
structs (with typedef aliases), macros, and caller relationships.
"""

import logging
import os
import re
import subprocess
from pathlib import Path
from typing import Optional

logger = logging.getLogger(__name__)

CTAGS_BIN = "/usr/bin/ctags"
CSCOPE_BIN = "/usr/bin/cscope"
SOURCE_EXTENSIONS = {".c", ".cpp", ".cc", ".cxx", ".h", ".hpp", ".hxx", ".hh"}


# ---------------------------------------------------------------------------
# File reading helpers
# ---------------------------------------------------------------------------

def _read_file_lines(file_path: str) -> list:
    for enc in ("utf-8", "latin-1"):
        try:
            with open(file_path, "r", encoding=enc) as fh:
                return fh.readlines()
        except (UnicodeDecodeError, OSError):
            continue
    return []


def _decode(data: bytes) -> str:
    for enc in ("utf-8", "latin-1"):
        try:
            return data.decode(enc)
        except UnicodeDecodeError:
            continue
    return data.decode("ascii", errors="replace")


# ---------------------------------------------------------------------------
# Tag line parser
# ---------------------------------------------------------------------------

def _parse_tag_line(line: str, repo_path: Path) -> Optional[dict]:
    """Parse a single ctags tag line into a structured dict."""
    parts = line.split("\t")
    if len(parts) < 4:
        return None

    tagname = parts[0]
    filepath_raw = parts[1]
    address_raw = parts[2]   # e.g. `/^int foo($/;"` or `18;"`
    kind = parts[3].strip()

    if not kind or not tagname:
        return None

    # Resolve file path
    if os.path.isabs(filepath_raw):
        file_path = filepath_raw
    else:
        file_path = str(repo_path / filepath_raw)

    # Parse extended fields (parts[4] onwards)
    fields: dict = {}
    for field in parts[4:]:
        field = field.strip()
        if ":" in field:
            k, _, v = field.partition(":")
            fields[k] = v
        elif field == "file":
            fields["file"] = ""  # file-scoped (static linkage)

    # Extract line number
    line_number = 0
    if "line" in fields:
        try:
            line_number = int(fields["line"])
        except ValueError:
            pass

    # If no line: field, try to parse numeric address (macro case)
    if line_number == 0:
        addr = address_raw
        if addr.endswith(';"'):
            addr = addr[:-2]
        if addr.isdigit():
            line_number = int(addr)

    # Extract signature from dedicated field
    signature = fields.get("signature", "")

    # If not present, try to infer from pattern
    if not signature:
        addr = address_raw
        if addr.endswith(';"'):
            addr = addr[:-2]
        if addr.startswith("/^") and addr.endswith("$/"):
            text = addr[2:-2]
            m = re.search(r"(\([^)]*\))", text)
            if m:
                signature = m.group(1)

    is_qualified = "::" in tagname
    short_name = tagname.split("::")[-1] if is_qualified else tagname

    return {
        "name": short_name,
        "qualified_name": tagname,
        "file_path": file_path,
        "line_number": line_number,
        "kind": kind,
        "signature": signature,
        "is_qualified": is_qualified,
        "is_scoped": "file" in fields,
        "class_": fields.get("class"),
        "struct_": fields.get("struct"),
        "namespace": fields.get("namespace"),
        "typeref": fields.get("typeref"),
        "access": fields.get("access"),
        "raw_address": address_raw,
    }


# ---------------------------------------------------------------------------
# Deduplication: prefer qualified names and definitions over prototypes
# ---------------------------------------------------------------------------

def _deduplicate_tags(all_tags: list) -> list:
    """
    With --extra=+q, ctags emits up to 4 entries per C++ symbol:
    qualified+unqualified × definition(f)+prototype(p).
    Keep only qualified entries when available; prefer 'f' over 'p'.
    """
    # Build set of (short_name, file_path, line_number) that have a qualified version
    qualified_keys: set = set()
    for tag in all_tags:
        if tag["is_qualified"]:
            qualified_keys.add((tag["name"], tag["file_path"], tag["line_number"]))

    filtered = []
    for tag in all_tags:
        if not tag["is_qualified"]:
            key = (tag["name"], tag["file_path"], tag["line_number"])
            if key in qualified_keys:
                continue  # drop: qualified version exists
        filtered.append(tag)

    # Among same qualified_name: prefer 'f' (definition) over 'p' (prototype)
    by_qname: dict = {}
    for tag in filtered:
        by_qname.setdefault(tag["qualified_name"], []).append(tag)

    result = []
    for qname, tags in by_qname.items():
        defs = [t for t in tags if t["kind"] == "f"]
        if defs:
            result.extend(defs)
        else:
            result.extend(tags)
    return result


# ---------------------------------------------------------------------------
# Typedef-struct linking
# ---------------------------------------------------------------------------

_ANON_RE = re.compile(r"^__anon\w+$")


def _find_typedef_keyword_start(file_path: str, typedef_line: int, keyword: str) -> int:
    """
    Scan backwards from typedef_line to find 'typedef <keyword>' (struct or enum).
    Returns the 1-based line number, or typedef_line if not found.
    Used for anonymous types that Exuberant Ctags 5.9 does not emit a separate tag for.
    """
    lines = _read_file_lines(file_path)
    start_idx = min(typedef_line - 1, len(lines) - 1)
    pattern = re.compile(rf"\btypedef\s+{keyword}\b")
    for i in range(start_idx, max(start_idx - 100, -1), -1):
        if pattern.search(lines[i]):
            return i + 1  # convert to 1-based
    return typedef_line


def _link_typedef_structs(structs_raw: list, typedef_entries: list) -> list:
    """
    Match typedef entries (kind='t') to their struct targets via the
    `typeref:struct:Name` field.  Annotates struct dicts with `typedef_name`.

    Handles anonymous structs (Exuberant Ctags 5.9 emits no 's' tag for them):
    creates synthetic struct entries derived from the typedef tag itself.

    Returns structs, filtering out un-aliased anonymous structs.
    """
    # Build lookup by struct name (both short and qualified)
    struct_lookup: dict = {}
    for s in structs_raw:
        struct_lookup[s["name"]] = s
        if s["qualified_name"] != s["name"]:
            struct_lookup[s["qualified_name"]] = s

    synthetic: list = []

    for typedef in typedef_entries:
        typeref = typedef.get("typeref") or ""
        if not typeref.startswith("struct:"):
            continue
        struct_ref = typeref[len("struct:"):]
        target = struct_lookup.get(struct_ref) or struct_lookup.get(struct_ref.split("::")[-1])

        if target is None:
            # Anonymous struct: Exuberant Ctags 5.9 emits no 's' tag.
            # Create a synthetic entry; the actual struct name is the anon internal name.
            start_line = _find_typedef_keyword_start(typedef["file_path"], typedef["line_number"], "struct")
            synth = {
                "name": struct_ref,            # e.g. "__anon1"
                "qualified_name": struct_ref,
                "file_path": typedef["file_path"],
                "line_number": start_line,
                "kind": "s",
                "signature": "",
                "is_qualified": False,
                "typedef_name": typedef["name"],
            }
            synthetic.append(synth)
            struct_lookup[struct_ref] = synth
            continue

        existing = target.get("typedef_name")
        if existing:
            target["typedef_name"] = f"{existing}, {typedef['name']}"
        else:
            target["typedef_name"] = typedef["name"]

    all_structs = structs_raw + synthetic

    # Filter: keep anonymous structs only if they have a typedef alias
    result = []
    for s in all_structs:
        if _ANON_RE.match(s["name"]):
            if s.get("typedef_name"):
                result.append(s)
        else:
            result.append(s)
    return result


# ---------------------------------------------------------------------------
# Typedef-enum linking  (mirrors _link_typedef_structs for kind='g')
# ---------------------------------------------------------------------------

def _link_typedef_enums(enums_raw: list, typedef_entries: list) -> list:
    """
    Match typedef entries (kind='t', typeref starts with 'enum:') to their
    enum targets.  Annotates enum dicts with `typedef_name`.

    Handles anonymous enums (Exuberant Ctags 5.9 emits no 'g' tag for them):
    creates synthetic enum entries derived from the typedef tag.

    Returns enums, filtering out un-aliased anonymous ones.
    """
    enum_lookup: dict = {}
    for e in enums_raw:
        enum_lookup[e["name"]] = e
        if e["qualified_name"] != e["name"]:
            enum_lookup[e["qualified_name"]] = e

    synthetic: list = []

    for typedef in typedef_entries:
        typeref = typedef.get("typeref") or ""
        if not typeref.startswith("enum:"):
            continue
        enum_ref = typeref[len("enum:"):]
        target = enum_lookup.get(enum_ref) or enum_lookup.get(enum_ref.split("::")[-1])

        if target is None:
            # Anonymous enum — synthesise an entry
            start_line = _find_typedef_keyword_start(
                typedef["file_path"], typedef["line_number"], "enum"
            )
            synth = {
                "name": enum_ref,
                "qualified_name": enum_ref,
                "file_path": typedef["file_path"],
                "line_number": start_line,
                "kind": "g",
                "signature": "",
                "is_qualified": False,
                "typedef_name": typedef["name"],
            }
            synthetic.append(synth)
            enum_lookup[enum_ref] = synth
            continue

        existing = target.get("typedef_name")
        if existing:
            target["typedef_name"] = f"{existing}, {typedef['name']}"
        else:
            target["typedef_name"] = typedef["name"]

    all_enums = enums_raw + synthetic

    # Filter out un-aliased anonymous enums
    return [
        e for e in all_enums
        if not _ANON_RE.match(e["name"]) or e.get("typedef_name")
    ]


# ---------------------------------------------------------------------------
# Source block extraction
# ---------------------------------------------------------------------------

def _find_open_brace(lines: list, start_idx: int, max_scan: int = 50) -> int:
    """
    Return the index of the line containing the first unquoted '{' at or after
    start_idx, within max_scan lines.  Returns -1 if not found.
    """
    for i in range(start_idx, min(start_idx + max_scan, len(lines))):
        line = lines[i]
        in_str = False
        in_char = False
        j = 0
        while j < len(line):
            ch = line[j]
            if in_str:
                if ch == "\\" and j + 1 < len(line):
                    j += 2
                    continue
                if ch == '"':
                    in_str = False
            elif in_char:
                if ch == "\\" and j + 1 < len(line):
                    j += 2
                    continue
                if ch == "'":
                    in_char = False
            else:
                if ch == "/" and j + 1 < len(line):
                    if line[j + 1] == "/":
                        break  # rest of line is comment
                    if line[j + 1] == "*":
                        # Skip block comment (may span multiple lines — handle in caller)
                        break
                elif ch == '"':
                    in_str = True
                elif ch == "'":
                    in_char = True
                elif ch == "{":
                    return i
            j += 1
    return -1


def _extract_macro_info(lines: list, ln: int) -> tuple:
    """
    Returns (source_code, value) for the macro at line ln (0-based).

    source_code : complete #define text as written, single or multi-line.
    value       : pure expansion — no '#define NAME(params)' prefix.
                  For multi-line macros the continuation backslashes are
                  stripped and lines are joined with newline.
    """
    # Collect all continuation lines
    raw_lines: list = []
    i = ln
    while i < len(lines):
        raw = lines[i].rstrip("\n")
        raw_lines.append(raw)
        if not raw.rstrip().endswith("\\"):
            break
        i += 1

    source_code = "\n".join(raw_lines)

    # Strip '#define name(params)' prefix from the first line
    # Handles: object macros, function-like (incl. variadic '...'), empty macros
    first = raw_lines[0] if raw_lines else ""
    m = re.match(r"#\s*define\s+\w+(?:\([^)]*\))?\s*(.*)", first)
    if m:
        first_val = m.group(1).rstrip("\\").strip()
        if len(raw_lines) == 1:
            value = first_val
        else:
            # Multi-line: join continuation lines, strip trailing backslashes
            parts = [first_val] + [l.rstrip("\\").strip() for l in raw_lines[1:]]
            value = "\n".join(parts)
    else:
        value = source_code  # fallback for unusual macro forms

    return source_code, value


def extract_source_block(file_path: str, start_line: int) -> str:
    """
    Read from start_line (1-based) and return the complete source block
    (function body, struct body, etc.) using brace-matching.

    Falls back to single-line return for declarations without a body.
    """
    lines = _read_file_lines(file_path)
    if not lines or start_line < 1 or start_line > len(lines):
        return ""

    idx = start_line - 1  # 0-based

    open_brace_line = _find_open_brace(lines, idx)
    if open_brace_line < 0:
        # No opening brace found — declaration or macro
        raw = lines[idx].rstrip("\n")
        if raw.rstrip().endswith("\\"):
            return _extract_macro_lines(lines, idx)
        return raw

    # Brace-matching state machine
    STATE_NORMAL = 0
    STATE_STRING = 1
    STATE_CHAR = 2
    STATE_LINE_COMMENT = 3
    STATE_BLOCK_COMMENT = 4

    state = STATE_NORMAL
    depth = 0
    result_lines = []

    for i in range(idx, len(lines)):
        line = lines[i]
        if i >= open_brace_line:
            result_lines.append(line.rstrip("\n"))

        j = 0
        while j < len(line):
            ch = line[j]

            if state == STATE_NORMAL:
                if ch == "/" and j + 1 < len(line):
                    if line[j + 1] == "/":
                        state = STATE_LINE_COMMENT
                        break
                    if line[j + 1] == "*":
                        state = STATE_BLOCK_COMMENT
                        j += 2
                        continue
                elif ch == '"':
                    state = STATE_STRING
                elif ch == "'":
                    state = STATE_CHAR
                elif ch == "{" and i >= open_brace_line:
                    depth += 1
                elif ch == "}" and i >= open_brace_line:
                    depth -= 1
                    if depth == 0:
                        return "\n".join(result_lines)

            elif state == STATE_STRING:
                if ch == "\\" and j + 1 < len(line):
                    j += 2
                    continue
                if ch == '"':
                    state = STATE_NORMAL

            elif state == STATE_CHAR:
                if ch == "\\" and j + 1 < len(line):
                    j += 2
                    continue
                if ch == "'":
                    state = STATE_NORMAL

            elif state == STATE_BLOCK_COMMENT:
                if ch == "*" and j + 1 < len(line) and line[j + 1] == "/":
                    state = STATE_NORMAL
                    j += 2
                    continue

            j += 1

        # Line comment resets at end of line
        if state == STATE_LINE_COMMENT:
            state = STATE_NORMAL

    # Fell off end of file (malformed source)
    return "\n".join(result_lines)


# ---------------------------------------------------------------------------
# Main analyser class
# ---------------------------------------------------------------------------

class CppAnalyzer:
    WORK_DIR_NAME = ".cpp_analysis"

    def __init__(self, repo_path: str) -> None:
        self.repo_path = Path(repo_path).resolve()
        if not self.repo_path.is_dir():
            raise ValueError(f"repo_path is not a directory: {repo_path}")
        self.work_dir = self.repo_path / self.WORK_DIR_NAME
        self.work_dir.mkdir(exist_ok=True)
        self._tags_path = self.work_dir / "tags"
        self._cscope_files_path = self.work_dir / "cscope.files"
        self._cscope_out_path = self.work_dir / "cscope.out"

    # ------------------------------------------------------------------
    # ctags
    # ------------------------------------------------------------------

    def run_ctags(self) -> None:
        if not os.path.exists(CTAGS_BIN):
            raise RuntimeError(f"ctags not found at {CTAGS_BIN}")
        cmd = [
            CTAGS_BIN,
            "--extra=+q",        # emit fully-qualified names (Exuberant Ctags singular form)
            "--fields=+n+S+a+i", # line numbers, signatures, access, inheritance
            "--c++-kinds=+p",    # include prototypes/declarations
            "--sort=yes",
            "-R",
            "-f", str(self._tags_path),
            str(self.repo_path),
        ]
        logger.info("Running ctags: %s", " ".join(cmd))
        result = subprocess.run(cmd, capture_output=True, text=True)
        if result.returncode != 0:
            raise RuntimeError(f"ctags failed (rc={result.returncode}): {result.stderr}")
        logger.info("ctags complete: %s", self._tags_path)

    # ------------------------------------------------------------------
    # Tag file parsing
    # ------------------------------------------------------------------

    def parse_tags(self) -> dict:
        if not self._tags_path.exists():
            raise RuntimeError("tags file not found; run run_ctags() first")

        raw_tags = []
        try:
            content = _decode(self._tags_path.read_bytes())
        except OSError as e:
            raise RuntimeError(f"Cannot read tags file: {e}") from e

        for line in content.splitlines():
            if line.startswith("!_TAG_") or not line.strip():
                continue
            tag = _parse_tag_line(line, self.repo_path)
            if tag:
                raw_tags.append(tag)

        deduped = _deduplicate_tags(raw_tags)

        functions = []
        global_vars = []
        structs_raw = []
        enums_raw = []
        macros = []
        typedef_entries = []

        for tag in deduped:
            kind = tag["kind"]
            if kind in ("f", "p"):
                tag["is_definition"] = kind == "f"
                functions.append(tag)
            elif kind == "v":
                # Only top-level (not class/struct/enum members)
                if not tag.get("class_") and not tag.get("struct_"):
                    global_vars.append(tag)
            elif kind == "s":
                structs_raw.append(tag)
            elif kind == "g":
                enums_raw.append(tag)
            elif kind == "d":
                macros.append(tag)
            elif kind == "t":
                typedef_entries.append(tag)
            # 'c' (class), 'n' (namespace), 'm' (member), 'e' (enumerator value) — ignored

        structs = _link_typedef_structs(structs_raw, typedef_entries)
        enums = _link_typedef_enums(enums_raw, typedef_entries)

        logger.info(
            "Parsed tags: %d functions, %d variables, %d structs, %d enums, %d macros",
            len(functions), len(global_vars), len(structs), len(enums), len(macros),
        )
        return {
            "functions": functions,
            "global_variables": global_vars,
            "structs": structs,
            "enums": enums,
            "macros": macros,
        }

    # ------------------------------------------------------------------
    # cscope
    # ------------------------------------------------------------------

    def run_cscope(self) -> None:
        if not os.path.exists(CSCOPE_BIN):
            raise RuntimeError(f"cscope not found at {CSCOPE_BIN}")

        # Gather all source files explicitly (cscope -R ignores .cpp/.hpp)
        source_files = []
        for ext in SOURCE_EXTENSIONS:
            for p in self.repo_path.rglob(f"*{ext}"):
                # Skip the work dir itself
                if self.work_dir in p.parents or p.parent == self.work_dir:
                    continue
                source_files.append(str(p))

        if not source_files:
            logger.warning("No source files found in %s", self.repo_path)
            return

        self._cscope_files_path.write_text(
            "\n".join(source_files) + "\n", encoding="utf-8"
        )
        logger.info("cscope file list: %d files", len(source_files))

        cmd = [
            CSCOPE_BIN,
            "-b",              # build only (no interactive UI)
            "-q",              # build inverted index for fast lookup
            "-i", str(self._cscope_files_path),
            "-f", str(self._cscope_out_path),
        ]
        logger.info("Running cscope: %s", " ".join(cmd))
        result = subprocess.run(
            cmd, capture_output=True, cwd=str(self.work_dir)
        )
        if result.returncode != 0:
            raise RuntimeError(
                f"cscope failed (rc={result.returncode}): {_decode(result.stderr)}"
            )
        logger.info("cscope complete: %s", self._cscope_out_path)

    def query_callers(self, func_name: str) -> list:
        """
        Return list of {file, caller_name, line, text} for functions that call func_name.
        Uses cscope search type 3 (functions calling this function).
        Cscope indexes unqualified names, so we strip C++ qualifiers.
        """
        if not self._cscope_out_path.exists():
            return []

        # cscope does not understand C++ namespaces — use the unqualified name
        short_name = func_name.split("::")[-1]

        cmd = [
            CSCOPE_BIN,
            "-d",                            # don't rebuild database
            "-f", str(self._cscope_out_path),
            f"-L3{short_name}",              # search type 3: callers
        ]
        result = subprocess.run(
            cmd, capture_output=True, cwd=str(self.work_dir)
        )
        output = _decode(result.stdout)

        callers = []
        for line in output.splitlines():
            line = line.strip()
            if not line:
                continue
            parts = line.split(None, 3)
            if len(parts) >= 3:
                try:
                    lineno = int(parts[2])
                except ValueError:
                    lineno = 0
                callers.append({
                    "file": parts[0],
                    "caller_name": parts[1],
                    "line": lineno,
                    "text": parts[3].strip() if len(parts) > 3 else "",
                })
        return callers

    # ------------------------------------------------------------------
    # Full analysis pipeline
    # ------------------------------------------------------------------

    def analyze(self) -> dict:
        """
        Run the full analysis pipeline:
        1. ctags  → parse tags
        2. Extract source blocks for functions and structs
        3. cscope → query callers for each function definition
        Returns {functions, global_variables, structs, macros, callers}.
        """
        self.run_ctags()
        parsed = self.parse_tags()

        # Enrich functions with source code
        for func in parsed["functions"]:
            if func.get("is_definition", False):
                func["source_code"] = extract_source_block(
                    func["file_path"], func["line_number"]
                )
            else:
                lines = _read_file_lines(func["file_path"])
                ln = func["line_number"] - 1
                func["source_code"] = lines[ln].rstrip("\n") if 0 <= ln < len(lines) else ""

        # Enrich structs with source code
        for struct in parsed["structs"]:
            struct["source_code"] = extract_source_block(
                struct["file_path"], struct["line_number"]
            )

        # Enrich enums with source code
        for enum in parsed["enums"]:
            enum["source_code"] = extract_source_block(
                enum["file_path"], enum["line_number"]
            )

        # Enrich global variables with source line
        for var in parsed["global_variables"]:
            lines = _read_file_lines(var["file_path"])
            ln = var["line_number"] - 1
            var["source_line"] = lines[ln].rstrip("\n") if 0 <= ln < len(lines) else ""

        # Enrich macros with source_code (full text) and value (expansion only)
        for macro in parsed["macros"]:
            file_lines = _read_file_lines(macro["file_path"])
            ln = macro["line_number"] - 1
            if 0 <= ln < len(file_lines):
                macro["source_code"], macro["value"] = _extract_macro_info(file_lines, ln)
            else:
                macro["source_code"] = macro["value"] = ""

        # Build cscope database and query callers
        self.run_cscope()
        callers: dict = {}
        for func in parsed["functions"]:
            if func.get("is_definition", False):
                func_key = func["qualified_name"]
                caller_list = self.query_callers(func_key)
                if caller_list:
                    callers[func_key] = caller_list

        logger.info("Analysis complete. Callers found for %d functions.", len(callers))
        return {
            "functions": parsed["functions"],
            "global_variables": parsed["global_variables"],
            "structs": parsed["structs"],
            "enums": parsed["enums"],
            "macros": parsed["macros"],
            "callers": callers,
        }
