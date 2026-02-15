import argparse
import datetime
import os
import pgembed
import sqlalchemy as sa
from sqlalchemy_utils import database_exists, create_database
from sqlalchemy.dialects.postgresql import JSONB


def migrate_sqlite_to_pgembed(sqlite_path, pgdata_path):
    # Connect to SQLite
    sqlite_engine = sa.create_engine(f"sqlite:///{sqlite_path}")

    # Reflect SQLite schema
    metadata = sa.MetaData()
    metadata.reflect(sqlite_engine)

    # Remove all constraints and make all columns nullable to allow NULLs in migrated data
    for table in metadata.tables.values():
        table.constraints = set()
        for col in table.columns:
            col.nullable = True
            col.primary_key = False
            if isinstance(col.type, sa.Integer):
                col.type = sa.BigInteger()
            if col.name == "id":
                col.autoincrement = False

    # Start pgembed server
    with pgembed.get_server(pgdata_path) as pg:
        database_name = "opencode"
        uri = pg.get_uri(database_name)

        if not database_exists(uri):
            create_database(uri)

        pg_engine = sa.create_engine(uri, isolation_level="AUTOCOMMIT")

        # Drop and recreate schema to ensure clean state with updated types
        with pg_engine.connect() as conn:
            conn.execute(sa.text("DROP SCHEMA public CASCADE; CREATE SCHEMA public;"))

        # Identify partitioned tables
        partitioned_tables = ["session", "message"]

        # Create non-partitioned tables first
        regular_metadata = sa.MetaData()
        for table_name, table in metadata.tables.items():
            if table_name not in partitioned_tables:
                table.tometadata(regular_metadata)

        regular_metadata.create_all(pg_engine)

        # Create partitioned tables manually with proper structure
        with pg_engine.connect() as conn:
            for table_name in partitioned_tables:
                if table_name in metadata.tables:
                    table = metadata.tables[table_name]
                    columns_sql = []
                    for col in table.columns:
                        col_type = col.type
                        if isinstance(col_type, sa.Integer):
                            col_type = sa.BigInteger()
                        if col.name == "time_created":
                            col_type = sa.TIMESTAMP()
                        elif col.name in [
                            "time_updated",
                            "time_compacting",
                            "time_archived",
                        ]:
                            col_type = sa.TIMESTAMP()
                        columns_sql.append(f'"{col.name}" {col_type}')

                    create_sql = f"""CREATE TABLE {table_name} (
                        {', '.join(columns_sql)}
                    ) PARTITION BY RANGE (time_created)"""
                    conn.execute(sa.text(create_sql))

                    # Create default partition
                    conn.execute(sa.text(f"""CREATE TABLE {table_name}_default
                        PARTITION OF {table_name} DEFAULT"""))

        # Copy data for each table
        with pg_engine.connect() as conn:
            for table_name, table in metadata.tables.items():
                if table_name.startswith("__drizzle"):
                    print(f"Skipping table: {table_name}")
                    continue
                print(f"Migrating table: {table_name}")
                select_stmt = sa.select(table)
                with sqlite_engine.connect() as sqlite_conn:
                    data = sqlite_conn.execute(select_stmt).fetchall()
                if data:
                    insert_stmt = table.insert()
                    insert_data = []
                    for row in data:
                        row_dict = row._asdict()
                        for col in table.columns:
                            if col.autoincrement and row_dict.get(col.name) is None:
                                row_dict.pop(col.name, None)
                        if "id" in row_dict and row_dict["id"] is None:
                            row_dict.pop("id")
                        # Convert bigint timestamps to datetime for partitioned tables
                        if table_name in partitioned_tables:
                            for ts_col in [
                                "time_created",
                                "time_updated",
                                "time_compacting",
                                "time_archived",
                            ]:
                                if ts_col in row_dict and row_dict[ts_col] is not None:
                                    row_dict[ts_col] = datetime.datetime.fromtimestamp(
                                        row_dict[ts_col] / 1000.0
                                    )
                        for col_name, value in list(row_dict.items()):
                            # Clean null bytes from string values
                            if isinstance(value, str):
                                row_dict[col_name] = value.replace("\x00", "").replace(
                                    "\\u0000", ""
                                )
                            if value is None and not table.columns[col_name].nullable:
                                if isinstance(table.columns[col_name].type, sa.String):
                                    row_dict[col_name] = ""
                                elif isinstance(
                                    table.columns[col_name].type, sa.Integer
                                ):
                                    row_dict[col_name] = 0
                                # add more if needed
                        insert_data.append(row_dict)
                    # Ensure all dicts have the same keys
                    all_keys = set()
                    for d in insert_data:
                        all_keys.update(d.keys())
                    for d in insert_data:
                        for k in all_keys:
                            if k not in d:
                                d[k] = None
                    conn.execute(insert_stmt, insert_data)
                    print(f"  Migrated {len(data)} rows")
                else:
                    print("  No data to migrate")
        print("Migration completed successfully.")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(
        description="Migrate SQLite OpenCode DB to pgembed PostgreSQL."
    )
    parser.add_argument(
        "--dbpath",
        default="pgdata",
        help="Path to pgembed data directory (default: pgdata)",
    )

    args = parser.parse_args()

    sqlite_path = os.path.expanduser("~/.local/share/opencode/opencode.db")

    if not os.path.exists(sqlite_path):
        print(f"SQLite database not found at {sqlite_path}")
        exit(1)

    migrate_sqlite_to_pgembed(sqlite_path, args.dbpath)
