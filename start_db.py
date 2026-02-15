import argparse
import pgembed

def main():
    parser = argparse.ArgumentParser(description='Start pgembed PostgreSQL database and print URL.')
    parser.add_argument('--dbpath', default='pgdata', help='Path to pgembed data directory (default: pgdata)')
    parser.add_argument('--database', default='opencode', help='Database name (default: opencode)')
    
    args = parser.parse_args()
    
    with pgembed.get_server(args.dbpath) as pg:
        uri = pg.get_uri(args.database)
        print(f"psql -h {args.dbpath} -U postgres -d {args.database}")
        print("Database is running. Press Ctrl+C to stop.")
        try:
            input()
        except KeyboardInterrupt:
            pass

if __name__ == '__main__':
    main()