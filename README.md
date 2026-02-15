# Migration from SQLite to pgembed

To migrate your database from SQLite to pgembed, follow these steps:

1. Run the migration script: `uv run migrate_opencode.py`
2. Start the database: `uv run start_db.py`
3. In another shell, run the psql command.

## To fix invalid JSON

Found unescaped \u0000 (null bytes, the literal string \u0000) and '\' which should be
escaped as '\\'

```
uv run check_json.py
uv run check_json.py --fix
```
