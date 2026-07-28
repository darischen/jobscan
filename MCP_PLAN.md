# Jobscan MCP Server Implementation Plan

## Overview
Convert jobscan into an MCP (Model Context Protocol) server that exposes scanning, querying, and registry management as tools and resources. Allows Claude and any AI agent to interact with jobscan programmatically.

## Architecture

### Server Type
- **Framework**: Python MCP SDK (`mcp` package)
- **Runtime**: Runs as separate process, starts fresh per Claude session
- **Lifecycle**: Spin up on demand, runs until Claude session ends
- **Access**: Any AI/agent that can connect to MCP protocol

### Core Components

1. **MCP Server Process** (`jobscan/mcp_server.py`)
   - Wraps existing jobscan Python code
   - Exposes tools and resources via MCP protocol
   - Uses asyncio for async operations (matches jobscan's runner.py)
   - Handles database connections, file I/O

2. **Tool Layer** (Claude-callable operations)
   - `scan_company` — run a single company scan
   - `scan_registry` — run batch scan across multiple companies
   - `query_jobs` — search the jobs database
   - `list_companies` — show registry
   - `add_company` — add/update registry entry
   - `get_scan_results` — fetch latest scan output
   - `get_stats` — scan statistics, error summary

3. **Resource Layer** (read-only data streams)
   - `jobs://open/{company?}` — open jobs, optionally filtered
   - `jobs://recent/{limit}` — recently added jobs
   - `scans://history` — past scan runs with metadata
   - `registry://companies` — current registry
   - `results://latest` — most recent scan folder and CSV

4. **Database Interface**
   - Reuse existing `Store` class from `jobscan/store.py`
   - Direct SQLite access via existing connection
   - Queries return structured data (dicts/lists, not raw SQL)

## Self-Hosting Requirements

### Installation
```
pip install mcp
# mcp package provides: @server.tool, @server.resource decorators
# and SSE transport for stdio-based communication
```

### File Structure
```
jobscan/
├── __init__.py
├── cli.py (existing)
├── runner.py (existing)
├── store.py (existing)
├── core.py (existing)
├── adapters/ (existing)
├── dates.py (existing)
├── mcp_server.py (NEW)
└── mcp_config.json (NEW, optional—just for local testing)
```

### Configuration

**Option A: Direct invocation (recommended)**
```bash
python -m jobscan.mcp_server
```
Starts MCP server on stdio transport (what Claude Code uses).

**Option B: Claude Code integration**
Add to `.claude/mcp.json` or settings:
```json
{
  "mcpServers": {
    "jobscan": {
      "command": "python",
      "args": ["-m", "jobscan.mcp_server"],
      "cwd": "/path/to/jobscan"
    }
  }
}
```
Claude automatically starts/stops this MCP server per session.

### Runtime Dependencies
- Python 3.10+
- `mcp` package (lightweight, no heavy dependencies)
- Existing jobscan deps (httpx, etc.) already available
- SQLite (built-in)

### Data Access
- **Database path**: `data/jobs.db` (passed to Store, relative to CWD)
- **Registry path**: `registry/companies.csv` (passed to registry loader)
- **Results path**: `results/` (read for latest scan output)
- All paths relative to project root when running MCP server

## Tool Definitions

### `scan_company(company: str, bucket?: str, no_store?: bool) -> dict`
- **Input**: company name (e.g., "Nvidia"), optional bucket filter, optional no-store flag
- **Output**: `{success: bool, rows: int, new: int, closed: int, errors: int, result_file: str, message: str}`
- **Behavior**: Runs `runner.scan()` for tier A, streams results, stores in DB (unless no_store=true)
- **Async**: Yes (wraps async runner)

### `scan_registry(limit?: int, tier_a_only?: bool) -> dict`
- **Input**: optional row limit, filter to tier A only
- **Output**: `{companies_scanned: int, total_rows: int, total_new: int, errors: int, run_id: str, result_file: str}`
- **Behavior**: Batch scan multiple companies, aggregates stats
- **Async**: Yes

### `query_jobs(company?: str, buckets?: list[str], since?: int, limit?: int) -> list[dict]`
- **Input**: optional company filter, optional bucket list, optional "since N days", optional result limit
- **Output**: List of job dicts with all fields (company, title, location, link, posted, bucket, etc.)
- **Behavior**: Calls `Store.open_jobs()`, returns structured data
- **Sync**: Yes (database query)

### `list_companies() -> list[dict]`
- **Input**: None
- **Output**: All rows from registry.csv as dicts with tier info
- **Behavior**: Parse CSV, add tier A/B column
- **Sync**: Yes

### `add_company(company: str, ticker?: str, ats: str, token?: str, ...) -> dict`
- **Input**: All registry columns
- **Output**: `{success: bool, message: str, line: str}` (the line to paste)
- **Behavior**: Validates ATS type, formats CSV line, returns it (doesn't write—user pastes)
- **Sync**: Yes

### `get_scan_results(run_id?: str) -> dict`
- **Input**: optional run_id (defaults to latest)
- **Output**: `{csv: str, metadata: dict, timestamp: str}`
- **Behavior**: Reads latest results folder, returns CSV content and metadata JSON
- **Sync**: Yes

### `get_stats() -> dict`
- **Input**: None
- **Output**: `{total_jobs: int, total_runs: int, last_run: str, by_company: dict, by_bucket: dict, errors_recent: list}`
- **Behavior**: Query stats from DB and runs table
- **Sync**: Yes

## Resource Definitions

### `jobs://open/{company?}`
- **Path**: `jobs://open` (all) or `jobs://open/Nvidia` (filtered)
- **Mime**: `text/csv`
- **Content**: CSV with columns: company, ticker, title, location, link, posted, bucket
- **Async**: No (DB read)

### `jobs://recent/{limit}`
- **Path**: `jobs://recent/50` (default 50)
- **Mime**: `text/csv`
- **Content**: Last N jobs added, sorted by first_seen DESC

### `scans://history`
- **Path**: `scans://history`
- **Mime**: `application/json`
- **Content**: Array of past scans with metadata (date, companies, rows, errors)

### `registry://companies`
- **Path**: `registry://companies`
- **Mime**: `text/csv`
- **Content**: Current registry with tier classification added

### `results://latest`
- **Path**: `results://latest`
- **Mime**: `application/json`
- **Content**: `{timestamp: str, folder: str, csv_file: str, metadata: {...}}`

## Implementation Details

### Error Handling
- Tool errors return `{success: false, error: "message"}` dict
- Resource errors return text/plain with error message
- Database connection errors retry once before failing
- Scan timeouts after 5 minutes, returns partial results

### Async Strategy
- `scan_company`, `scan_registry`: wrap `runner.scan()` with asyncio.run()
- Other tools: sync, blocking (DB queries are fast)
- MCP server maintains event loop, doesn't block waiting

### State Management
- **Stateless**: Each MCP call is independent
- **Shared state**: SQLite database (concurrent reads safe)
- **Per-run state**: Results written to timestamped folders
- No in-memory caching across calls

### Security Considerations
- Input validation on company names (CSV injection risk)
- Sanitize paths in `get_scan_results()` to prevent directory traversal
- Database queries use parameterized queries (via Store class)
- No authentication needed (self-hosted, trusted environment)

## Integration with Claude Code

### Session Flow
1. Claude Code starts jobscan MCP server (`python -m jobscan.mcp_server`)
2. Server listens on stdio, connects to Claude via MCP protocol
3. Claude can call tools and read resources
4. User ends session → server process exits
5. Next session → fresh server instance

### Example Claude Interaction
```
User: "Scan Nvidia's explicit_early roles and show me the results"

Claude:
  1. Calls tool: scan_company("Nvidia", "explicit_early")
  2. Waits for scan to complete
  3. Calls resource: jobs://open/Nvidia
  4. Renders CSV table to user
```

## Testing

### Pre-Implementation Tests
1. **Unit**: Mock runner/store, verify tool logic
2. **Integration**: Run MCP server, test with mcp-cli tool
3. **End-to-end**: Have Claude Code connect and run full workflow

### Manual Testing
```bash
# Start server
python -m jobscan.mcp_server

# In another terminal, use mcp-cli (install via npm)
mcp-cli call jobscan list_companies
mcp-cli resource jobscan jobs://open/Nvidia
```

## Deployment & Scaling

### For Single User (Recommended)
- Run on local machine, Claude Code connects via stdio
- No network exposure, no authentication needed
- Each session gets fresh server process

### For Multiple Users (Future)
- Could wrap MCP server in HTTP proxy (mcp-server-http)
- Host on cloud machine
- Adds authentication layer
- Out of scope for initial implementation

## Dependencies to Add

```python
# pyproject.toml or requirements.txt additions
mcp>=0.1.0  # Python MCP SDK
```

No other dependencies needed (uses existing jobscan packages).

## Portability to Cloud (Future-Proof Design)

Design the MCP server now to be easily portable to cloud hosting later, without code changes.

### Configuration via Environment Variables
Define paths as env vars, not hardcoded. This enables both local and cloud deployment:

```python
# In mcp_server.py
import os

DATA_DIR = os.getenv("DATA_DIR", "./data")
REGISTRY_PATH = os.getenv("REGISTRY_PATH", "./registry/companies.csv")
RESULTS_DIR = os.getenv("RESULTS_DIR", "./results")

db_path = os.path.join(DATA_DIR, "jobs.db")
store = Store(db_path)
```

**Local usage (Claude Code):**
```bash
python -m jobscan.mcp_server
# Uses ./data/jobs.db, ./registry/companies.csv (defaults)
```

**Cloud usage (later):**
```bash
export DATA_DIR=/app/data
export REGISTRY_PATH=/app/registry/companies.csv
export RESULTS_DIR=/app/results
python -m jobscan.mcp_server
```

### Docker Support (Optional but Recommended)

Add `Dockerfile` to project root:

```dockerfile
FROM python:3.11-slim
WORKDIR /app
COPY . .
RUN pip install -e . && pip install mcp
ENV DATA_DIR=/data
ENV REGISTRY_PATH=/app/registry/companies.csv
ENV RESULTS_DIR=/results
VOLUME ["/data", "/results"]
CMD ["python", "-m", "jobscan.mcp_server"]
```

**Benefits:**
- Identical behavior local → cloud (no "works on my machine" issues)
- Push to DigitalOcean, AWS, or any cloud with one command
- Takes 30 mins to add now, saves hours during cloud migration

### Cloud Deployment Path (Deferred)

**Phase 1 (Now):** Claude Code local, MCP server via stdio
- No Docker needed yet
- Config via env vars (but use defaults)

**Phase 2 (Later):** Add Web UI access via cloud
1. Build Docker image: `docker build -t jobscan-mcp .`
2. Push to cloud registry or self-host on DigitalOcean
3. Expose via HTTP proxy (mcp-server-http wrapper)
4. Zero code changes to mcp_server.py

### Files to Create Now

```
jobscan/
├── Dockerfile (NEW — for future cloud deployment)
├── .dockerignore (NEW — exclude data/results from image)
└── mcp_server.py (NEW — uses env vars)
```

### Implementation Checklist

- [ ] Use `os.getenv()` for all path configuration
- [ ] Document all env vars in server startup message
- [ ] Add `.dockerignore` to exclude `data/` and `results/`
- [ ] Create basic `Dockerfile` (boilerplate, doesn't need tweaking)
- [ ] Store Dockerfile in root for easy cloud deployment later

This adds ~10 minutes to initial implementation and eliminates cloud migration headaches later.

## Future Extensions

1. **Long-running scans**: Implement progress streaming via MCP notifications
2. **Webhooks**: Alert Claude when scans complete without polling
3. **Caching**: Cache open_jobs results, invalidate on scan completion
4. **Multi-tenant**: Run separate MCP servers per user
5. **Reverse proxy**: Expose via HTTP for non-local access

## Timeline Estimate

- **Skeleton**: 1-2 hours (basic server structure, 2-3 tools)
- **Full tools**: 2-3 hours (all 7 tools + error handling)
- **Resources**: 1-2 hours (CSV/JSON formatting)
- **Testing**: 1-2 hours (unit + integration)
- **Total**: 5-9 hours for a complete, tested implementation

## Next Steps

1. Review this plan, adjust scope if needed
2. Create `jobscan/mcp_server.py` skeleton
3. Implement tools one by one
4. Add resources
5. Test with Claude Code
