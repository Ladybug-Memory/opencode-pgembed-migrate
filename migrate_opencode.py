import argparse
import os
import pgembed
import sqlalchemy as sa
from sqlalchemy_utils import database_exists, create_database
from sqlalchemy.dialects.postgresql import JSONB

def migrate_sqlite_to_pgembed(sqlite_path, pgdata_path):
    # Connect to SQLite
    sqlite_engine = sa.create_engine(f'sqlite:///{sqlite_path}')
    
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
            if col.name == 'id':
                col.autoincrement = False
    
    # Start pgembed server
    with pgembed.get_server(pgdata_path) as pg:
        database_name = 'opencode'
        uri = pg.get_uri(database_name)
        
        if not database_exists(uri):
            create_database(uri)
        
        pg_engine = sa.create_engine(uri, isolation_level='AUTOCOMMIT')
        
        # Drop and recreate schema to ensure clean state with updated types
        with pg_engine.connect() as conn:
            conn.execute(sa.text("DROP SCHEMA public CASCADE; CREATE SCHEMA public;"))
        
        # Create tables in PostgreSQL
        metadata.create_all(pg_engine)
        
        # Ensure all columns are nullable
        with pg_engine.connect() as conn:
            for table_name, table in metadata.tables.items():
                if table_name.startswith('__drizzle'):
                    continue
                # Drop primary key if exists
                conn.execute(sa.text(f"ALTER TABLE {table_name} DROP CONSTRAINT IF EXISTS {table_name}_pkey CASCADE"))
                for col in table.columns:
                    conn.execute(sa.text(f"ALTER TABLE {table_name} ALTER COLUMN \"{col.name}\" DROP NOT NULL"))
        
        # Partition session table by date if exists
        if 'session' in metadata.tables:
            session_table = metadata.tables['session']
            if 'created_at' in session_table.columns:
                with pg_engine.connect() as conn:
                    conn.execute(sa.text("ALTER TABLE session DETACH PARTITION session_default;"))  # in case
                    conn.execute(sa.text("DROP TABLE IF EXISTS session_default;"))
                    conn.execute(sa.text("ALTER TABLE session PARTITION BY RANGE (created_at);"))
                    conn.execute(sa.text("CREATE TABLE session_default PARTITION OF session DEFAULT;"))
        
        # Copy data for each table
        with pg_engine.connect() as conn:
            for table_name, table in metadata.tables.items():
                if table_name.startswith('__drizzle'):
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
                        if 'id' in row_dict and row_dict['id'] is None:
                            row_dict.pop('id')
                        for col_name, value in list(row_dict.items()):
                            if value is None and not table.columns[col_name].nullable:
                                if isinstance(table.columns[col_name].type, sa.String):
                                    row_dict[col_name] = ''
                                elif isinstance(table.columns[col_name].type, sa.Integer):
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

if __name__ == '__main__':
    parser = argparse.ArgumentParser(description='Migrate SQLite OpenCode DB to pgembed PostgreSQL.')
    parser.add_argument('--dbpath', default='pgdata', help='Path to pgembed data directory (default: pgdata)')
    
    args = parser.parse_args()
    
    sqlite_path = os.path.expanduser('~/.local/share/opencode/opencode.db')
    
    if not os.path.exists(sqlite_path):
        print(f"SQLite database not found at {sqlite_path}")
        exit(1)
    
    migrate_sqlite_to_pgembed(sqlite_path, args.dbpath)