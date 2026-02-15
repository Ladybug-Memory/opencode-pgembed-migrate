# Migration from SQLite to pgembed

To migrate your database from SQLite to pgembed, follow these steps:

## 1. Migrate SQLite to PostgreSQL

```bash
uv run migrate_opencode.py
```

This will migrate your SQLite database from `~/.local/share/opencode/opencode.db` to the `pgdata` directory.

## 2. Fix Invalid JSON

Found unescaped `\u0000` (null bytes, the literal string `\u0000`) and `\` which should be escaped as `\\`

```bash
uv run check_json.py
uv run check_json.py --fix
```

The `--fix` flag will also convert the `message.data` column to JSONB.

## 3. Build Vector Index

Create pgvector HNSW indexes on session titles and message summaries:

```bash
uv run index_pgdata.py
```

This will:
- Load the `BAAI/bge-small-en-v1.5` embedding model
- Generate 384-dimensional vectors for session titles
- Generate vectors for message `summary.title` fields
- Create HNSW indexes for fast similarity search

**Note:** This step can take several minutes depending on your data size. Progress bars will show indexing status.

## 4. Query the Database

### Start the database server

```bash
uv run start_db.py
```

In another shell, you can run psql commands.

### Sample similarity search queries

```bash
# Search similar sessions
uv run python query_pgvector.py "database migration" --sessions

# Search messages by model
uv run python query_pgvector.py "debug error" --model minimax-m2.1-free

# Search by agent type
uv run python query_pgvector.py "fix bug" --agent build

# Search with date range
uv run python query_pgvector.py "vcpkg setup" --start-time "2025-01-01 00:00:00"

# With similarity threshold
uv run python query_pgvector.py "graph creation" --min-similarity 0.8
```

### Query options

- `--sessions`: Search session titles instead of messages
- `--model <modelID>`: Filter by model (e.g., `minimax-m2.1-free`)
- `--agent <agent>`: Filter by agent type (`build`, `explore`, `general`, `plan`)
- `--start-time/--end-time`: Filter by date range
- `--min-similarity <0-1>`: Minimum similarity threshold
- `--limit <n>`: Number of results (default: 5)