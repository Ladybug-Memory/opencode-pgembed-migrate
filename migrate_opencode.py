import argparse
import datetime
import os
import pgembed
import sqlalchemy as sa
from sqlalchemy_utils import database_exists, create_database
from sqlalchemy.dialects.postgresql import JSONB

# Import duckdb for archiving partitions
import duckdb


def migrate_partitions_to_archive(
    pg_engine,
    partitioned_tables,
    pgdata_path,
    age_days=90,
    archive_dir="duckdb_archive",
    archive_format="vortex",
):
    """Migrate partitions older than age_days to archive files via DuckDB postgres scanner."""
    import pathlib

    archive_format = archive_format.lower()
    supported_formats = {"vortex", "lance", "parquet"}
    if archive_format not in supported_formats:
        raise ValueError(
            f"Unsupported archive format '{archive_format}'. Expected one of: {sorted(supported_formats)}"
        )

    # Store archive output inside pgdata dir
    archive_dir = os.path.join(pgdata_path, archive_dir)

    # Ensure archive directory exists
    pathlib.Path(archive_dir).mkdir(parents=True, exist_ok=True)

    cutoff_date = datetime.datetime.now() - datetime.timedelta(days=age_days)

    # pgembed uses Unix socket and 'postgres' user (as shown in start_db.py line 22)
    pg_db = "opencode"
    pg_user = "postgres"
    socket_dir = os.path.abspath(pgdata_path)

    with pg_engine.connect() as conn:
        for table_name in partitioned_tables:
            print(f"Checking partitions for {table_name}...")

            # Get all partitions for this table
            result = conn.execute(sa.text(f"""
                SELECT inhrelid::regclass::text as partition_name,
                       pg_get_expr(relpartbound, inhrelid) as bounds
                FROM pg_inherits
                JOIN pg_class ON pg_class.oid = inhrelid
                WHERE inhparent = '{table_name}'::regclass
            """))
            partitions = result.fetchall()

            for partition_name, bounds in partitions:
                # Skip default partition
                if "DEFAULT" in (bounds or "").upper():
                    continue

                # Parse partition bounds to find date range
                import re

                date_match = re.search(r"FROM \('(\d{4}-\d{2}-\d{2})", bounds or "")
                if date_match:
                    partition_start = datetime.datetime.strptime(
                        date_match.group(1), "%Y-%m-%d"
                    )

                    # If partition is older than cutoff, migrate to archive file format
                    if partition_start < cutoff_date:
                        archive_file = os.path.abspath(
                            f"{archive_dir}/{partition_name}.{archive_format}"
                        )
                        print(
                            f"  Migrating {partition_name} to {archive_file} (format={archive_format})"
                        )

                        # Use in-memory duckdb session to export partition directly to archive file
                        ddb = duckdb.connect()

                        # Install and load postgres scanner
                        ddb.execute("INSTALL postgres;")
                        ddb.execute("LOAD postgres;")
                        if archive_format in {"vortex", "lance"}:
                            ddb.execute(f"INSTALL {archive_format};")
                            ddb.execute(f"LOAD {archive_format};")

                        # Attach PostgreSQL using Unix socket and write partition to archive file
                        ddb.execute(f"""
                            ATTACH 'dbname={pg_db} user={pg_user} host={socket_dir}' AS pg (TYPE postgres);
                            COPY (
                                SELECT * FROM pg.{partition_name}
                            ) TO '{archive_file}' (FORMAT {archive_format})
                        """)

                        ddb.close()

                        # Detach and drop original partition after successful migration
                        conn.execute(sa.text(f"""
                            ALTER TABLE {table_name} DETACH PARTITION {partition_name}
                        """))
                        conn.execute(sa.text(f"DROP TABLE {partition_name}"))

                        print(f"    Migrated and removed {partition_name}")

        # Run checkpoint on PostgreSQL after all migrations are done
        conn.execute(sa.text("CHECKPOINT;"))


def migrate_sqlite_to_pgembed(
    sqlite_path, pgdata_path, duckdb_age_days=None, archive_format="vortex"
):
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

                    # Get date range from SQLite data
                    with sqlite_engine.connect() as sqlite_conn:
                        min_max_result = sqlite_conn.execute(
                            sa.text(
                                f"SELECT MIN(time_created), MAX(time_created) FROM {table_name}"
                            )
                        ).fetchone()
                        min_ts, max_ts = min_max_result

                    if min_ts is not None and max_ts is not None:
                        # Convert from milliseconds to datetime
                        min_date = datetime.datetime.fromtimestamp(min_ts / 1000.0)
                        max_date = datetime.datetime.fromtimestamp(max_ts / 1000.0)

                        # Create daily partitions
                        current = min_date.replace(
                            hour=0, minute=0, second=0, microsecond=0
                        )
                        end_date = max_date.replace(
                            hour=0, minute=0, second=0, microsecond=0
                        )

                        while current <= end_date:
                            next_day = current + datetime.timedelta(days=1)

                            partition_name = (
                                f"{table_name}_{current.strftime('%Y_%m_%d')}"
                            )
                            start_str = current.strftime("%Y-%m-%d")
                            end_str = next_day.strftime("%Y-%m-%d")

                            conn.execute(sa.text(f"""
                                CREATE TABLE {partition_name}
                                PARTITION OF {table_name}
                                FOR VALUES FROM ('{start_str}') TO ('{end_str}')
                            """))
                            print(
                                f"  Created partition: {partition_name} ({start_str} to {end_str})"
                            )

                            current = next_day

                    # Create default partition for any data outside the range
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

        # Optionally migrate older partitions to archive files
        if duckdb_age_days:
            print(
                f"\nMigrating partitions older than {duckdb_age_days} days to {archive_format}..."
            )
            migrate_partitions_to_archive(
                pg_engine,
                partitioned_tables,
                pgdata_path,
                duckdb_age_days,
                archive_format=archive_format,
            )


if __name__ == "__main__":
    parser = argparse.ArgumentParser(
        description="Migrate SQLite OpenCode DB to pgembed PostgreSQL."
    )
    parser.add_argument(
        "--dbpath",
        default="pgdata",
        help="Path to pgembed data directory (default: pgdata)",
    )
    parser.add_argument(
        "--duckdb-age-days",
        type=int,
        default=1,
        help="Migrate partitions older than N days to archive files",
    )
    format_group = parser.add_mutually_exclusive_group()
    format_group.add_argument(
        "--archive-format",
        choices=["vortex", "lance", "parquet"],
        help="Archive file format (default: parquet)",
    )
    format_group.add_argument(
        "--lance",
        action="store_true",
        help="Shortcut for --archive-format lance",
    )
    format_group.add_argument(
        "--vortex",
        action="store_true",
        help="Shortcut for --archive-format vortex",
    )

    args = parser.parse_args()

    sqlite_path = os.path.expanduser("~/.local/share/opencode/opencode.db")

    if not os.path.exists(sqlite_path):
        print(f"SQLite database not found at {sqlite_path}")
        exit(1)

    archive_format = (
        "lance"
        if args.lance
        else ("vortex" if args.vortex else (args.archive_format or "parquet"))
    )
    migrate_sqlite_to_pgembed(
        sqlite_path, args.dbpath, args.duckdb_age_days, archive_format=archive_format
    )
