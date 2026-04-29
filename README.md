# C/C++ Static Analyzer

A Python-based static analysis tool for C/C++ codebases. Uses **ctags** and **cscope** to extract functions, global variables, structs, macros, and call relationships, stores everything in a SQLite database, and exposes the data through an **MCP-compatible HTTP server**.

## Features

- **Full C++ qualified names** — preserves `Namespace::Class::method` form throughout
- **Typedef-struct linking** — both `struct Node` and its `typedef` alias `NodeT` are stored and searchable
- **Anonymous struct support** — `typedef struct { ... } Point` correctly identified even when ctags emits no internal `s` tag
- **Caller analysis** — uses cscope to find which functions call a given function
- **Source code extraction** — accurate brace-matching parser handles strings, char literals, line/block comments, Allman-style braces, multi-line macros
- **MCP HTTP server** — standard JSON-RPC 2.0 over SSE transport; connects directly to Claude and other MCP clients
- **Python 3.8+ compatible** — no dependency on the `mcp` SDK (requires Python ≥ 3.10)

## Architecture

```
static_analysis.py       # ctags + cscope orchestration and parsing
database.py              # SQLite persistence layer
cpp_analyzer_server.py   # HTTP MCP server (SSE transport, JSON-RPC 2.0)
requirements.txt
```

## Requirements

| Tool    | Version tested | Purpose                    |
|---------|----------------|----------------------------|
| Python  | 3.8+           |                            |
| ctags   | Exuberant 5.9  | `/usr/bin/ctags`           |
| cscope  | 15.9           | `/usr/bin/cscope`          |

Install Python dependencies:

```bash
pip3 install -r requirements.txt
```

`requirements.txt` contents:
```
starlette>=0.27.0
uvicorn>=0.24.0
anyio>=3.6.0
```

## Quick Start

### 1. Analyse a repository

```bash
python3 -c "
from static_analysis import CppAnalyzer
from database import Database

repo = '/path/to/your/cpp/repo'
result = CppAnalyzer(repo).analyze()
db = Database(repo)
db.store_analysis(result)
print(db.stats())
"
```

The database is created as `{repo_name}_analysis.db` in the current working directory.

### 2. Start the MCP server

```bash
# Serve an existing database
python3 cpp_analyzer_server.py --db ./myrepo_analysis.db

# Analyse first, then serve (combined)
python3 cpp_analyzer_server.py --db ./myrepo_analysis.db --repo /path/to/your/cpp/repo

# Custom host and port
python3 cpp_analyzer_server.py --db ./myrepo_analysis.db --host 0.0.0.0 --port 9000
```

Server output:
```
INFO     Starting cpp-analyzer MCP server on 127.0.0.1:8080
INFO     SSE endpoint     : http://127.0.0.1:8080/sse
INFO     Messages endpoint: http://127.0.0.1:8080/messages?sessionId=<id>
```

### 3. Connect Claude Code to the server

Add the server to your Claude Code MCP configuration:

```json
{
  "mcpServers": {
    "cpp-analyzer": {
      "url": "http://127.0.0.1:8080/sse"
    }
  }
}
```

Or run with the `--mcp-server` flag:

```bash
claude --mcp-server "cpp-analyzer=http://127.0.0.1:8080/sse"
```

## MCP Tools

Once the server is running, the following tools are available to any MCP-compatible client:

### `get_function_code`

Returns the full source code of a function.

```
Input:  function_name — short name ("getValue") or qualified ("Widget::getValue")
Output: qualified name, file:line, signature, source code
```

Example output:
```
Function : UI::Widget::getValue
File     : /path/to/widget.cpp:10
Signature: () const

int Widget::getValue() const {
    return value_;
}
```

### `get_global_variable`

Returns the declaration of a global variable.

```
Input:  variable_name
Output: name, inferred type, file:line, declaration line
```

### `get_struct_definition`

Returns the full struct or class definition. Accepts both the struct tag name and any typedef alias.

```
Input:  struct_name — e.g. "Node" or "NodeT" (typedef alias)
Output: struct name, typedef alias (if any), file:line, source code
```

Example — `typedef struct { float r; float g; float b; } Color;`:
```
Struct : __anon1 (typedef: Color)
File   : /path/to/types.h:12

typedef struct {
    float r;
    float g;
    float b;
} Color;
```

### `get_function_callers`

Returns all functions that call the given function (via cscope cross-reference).

```
Input:  function_name
Output: list of caller_name + file:line
```

### `analyze_repository`

Triggers a full analysis of a C/C++ repository and stores results in the database. Runs in a background thread so the server stays responsive.

```
Input:  repo_path — absolute path to the repository root
Output: counts of functions, variables, structs, macros, caller relations
```

### `search_function`

Searches for functions by partial name (case-sensitive substring match).

```
Input:  pattern — e.g. "Widget" matches "UI::Widget::getValue", "UI::Widget::setValue", …
Output: list of qualified_name + file:line
```

### `get_database_stats`

Returns row counts for all database tables and the database file path.

## Module Reference

### `static_analysis.py`

```python
from static_analysis import CppAnalyzer

analyzer = CppAnalyzer("/path/to/repo")

# Run individual steps
analyzer.run_ctags()              # writes .cpp_analysis/tags
analyzer.run_cscope()             # writes .cpp_analysis/cscope.out

parsed = analyzer.parse_tags()    # dict with functions/global_variables/structs/macros
callers = analyzer.query_callers("MyClass::myMethod")

# Or run everything at once
result = analyzer.analyze()
# result keys: functions, global_variables, structs, macros, callers
```

**Analysis output fields:**

| Category          | Key fields                                                        |
|-------------------|-------------------------------------------------------------------|
| `functions`       | `name`, `qualified_name`, `file_path`, `line_number`, `signature`, `source_code`, `is_definition` |
| `global_variables`| `name`, `file_path`, `line_number`, `source_line`                |
| `structs`         | `name`, `typedef_name`, `file_path`, `line_number`, `source_code`|
| `macros`          | `name`, `value`, `file_path`, `line_number`                      |
| `callers`         | dict: `qualified_name → [{file, caller_name, line, text}]`       |

Work files are stored in `{repo_path}/.cpp_analysis/` and reused across runs.

### `database.py`

```python
from database import Database

# Create or open by repo path (names DB automatically)
db = Database("/path/to/repo")       # → ./myrepo_analysis.db

# Open an existing DB by direct path
db = Database.open("./myrepo_analysis.db")

# Write
db.store_analysis(result)            # idempotent; clears and re-inserts

# Read
db.get_function("getValue")          # short or qualified name
db.get_function("UI::Widget::getValue")
db.get_variable("g_count")
db.get_struct("Point")               # by struct name or typedef alias
db.get_struct("NodeT")
db.get_callers("printWidget")
db.search_function("Widget")         # substring search
db.stats()                           # row counts per table

db.close()

# Context manager
with Database.open("./myrepo_analysis.db") as db:
    ...
```

**Database schema:**

```sql
functions       (id, name, qualified_name, file_path, line_number, signature, source_code)
global_variables(id, name, type_info, file_path, line_number, source_line)
structs         (id, name, typedef_name, file_path, line_number, source_code)
macros          (id, name, value, file_path, line_number)
callers         (id, callee_name, caller_name, caller_file, caller_line)
```

All name columns are indexed. Queries match both short names and fully-qualified names automatically.

### `cpp_analyzer_server.py`

```
usage: cpp_analyzer_server.py [-h] --db DB [--repo REPO]
                               [--host HOST] [--port PORT]
                               [--log-level {DEBUG,INFO,WARNING,ERROR}]

arguments:
  --db        Path to SQLite database file (required)
  --repo      Run full analysis on this repo before starting the server
  --host      Bind host (default: 127.0.0.1)
  --port      HTTP port (default: 8080)
  --log-level Logging verbosity (default: INFO)
```

**MCP Transport protocol:**

The server implements the MCP HTTP/SSE transport specification (JSON-RPC 2.0):

1. Client connects to `GET /sse` — receives a `text/event-stream` response
2. Server sends an `endpoint` event: `data: /messages?sessionId=<uuid>`
3. Client POSTs JSON-RPC messages to `/messages?sessionId=<uuid>`
4. Server sends responses back via the SSE stream as `message` events
5. Server sends `: keepalive` comments every 25 seconds to maintain the connection

## How It Works

### ctags analysis

```bash
ctags --extra=+q --fields=+n+S+a+i --c++-kinds=+p --sort=yes -R \
      -f .cpp_analysis/tags /path/to/repo
```

- `--extra=+q` — emit fully-qualified names as additional tag entries (e.g. `UI::Widget::getValue` alongside `getValue`)
- `--fields=+n+S+a+i` — include line numbers, signatures, access modifiers, inheritance
- `--c++-kinds=+p` — include function prototypes and declarations in addition to definitions

The parser then:
1. Drops unqualified entries when a qualified version exists at the same file+line
2. Prefers function definitions (`f`) over prototypes (`p`) when both exist
3. Links `typedef` entries to their struct targets via the `typeref:struct:Name` field
4. Creates synthetic struct entries for anonymous structs (which Exuberant Ctags 5.9 does not tag separately)

### cscope analysis

```bash
# Step 1: generate file list (cscope -R misses .cpp/.hpp by default)
find /path/to/repo -name "*.c" -o -name "*.cpp" ... > .cpp_analysis/cscope.files

# Step 2: build cross-reference database
cscope -b -q -i cscope.files -f cscope.out

# Step 3: query callers (search type 3)
cscope -d -f cscope.out -L3<function_name>
```

Cscope indexes only unqualified names, so `UI::Widget::getValue` is queried as `getValue`.

### Source extraction

The brace-matching extractor:
1. Starts at the line number reported by ctags
2. Scans forward (up to 50 lines) for the first `{` not inside a string, char literal, or comment
3. Tracks brace depth using a state machine with states: `normal`, `string`, `char`, `line_comment`, `block_comment`
4. Returns all source lines from the function/struct start to the matching `}`
5. Falls back to the single declaration line if no brace is found (forward declarations, `extern` declarations)
6. For macros: collects continuation lines ending with `\`

## File Structure

```
/path/to/repo/
└── .cpp_analysis/           # Work directory (auto-created)
    ├── tags                 # ctags output
    ├── cscope.files         # Source file list for cscope
    ├── cscope.out           # cscope cross-reference database
    ├── cscope.out.in        # cscope inverted index
    └── cscope.out.po        # cscope position index

./
└── {repo_name}_analysis.db  # SQLite database (in current working directory)
```

## Example Session

```python
from static_analysis import CppAnalyzer
from database import Database

# Analyse the Linux kernel (for example)
result = CppAnalyzer("/usr/src/linux").analyze()

# Store results
db = Database("/usr/src/linux")
db.store_analysis(result)

# Query
func = db.get_function("schedule")
print(func["source_code"])

callers = db.get_callers("schedule")
for c in callers:
    print(f"  called by {c['caller_name']} in {c['caller_file']}:{c['caller_line']}")

struct = db.get_struct("task_struct")
print(struct["source_code"][:500])
```

## Limitations

- **Exuberant Ctags 5.9** has limited C++ template support; template specialisations may not be fully qualified
- **cscope** does not parse C++ namespaces — caller queries use unqualified names, so `MyNS::foo` and `OtherNS::foo` both match callers of `foo`
- Very large repositories (millions of lines) may take several minutes to analyse; use `analyze_repository` via the MCP tool which runs in a background thread
- Header-only function definitions (fully inlined in `.hpp`) are captured by ctags but cscope may not record their call sites accurately

## License

Apache License 2.0 — see [LICENSE](LICENSE).
