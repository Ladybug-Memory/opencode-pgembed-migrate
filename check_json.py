import argparse
import json
import pgembed
import psycopg2
from psycopg2.extras import RealDictCursor
from pprint import pprint


def find_problematic_json(db_path, database, table, fix=False):
    with pgembed.get_server(db_path) as pg:
        conn = psycopg2.connect(pg.get_uri(database))
        conn.autocommit = True

        with conn.cursor(cursor_factory=RealDictCursor) as cur:
            # Find rows with null bytes by checking each row in Python
            cur.execute(f"SELECT * FROM {table}")
            all_rows = cur.fetchall()

            # Check for both actual null bytes AND literal string "\u0000"
            null_byte_rows = []
            literal_null_rows = []
            for row in all_rows:
                data = row.get("data", "")
                if data:
                    # Check for actual null byte character
                    if "\x00" in data:
                        null_byte_rows.append(row)
                    # Check for literal string "\u0000" in the data
                    elif "\\u0000" in data:
                        literal_null_rows.append(row)

            if null_byte_rows:
                print(
                    f"\nFound {len(null_byte_rows)} rows with actual null byte characters:\n"
                )
                for row in null_byte_rows:
                    print("=" * 80)
                    print(f"Row ID: {row.get('id')}")
                    print(f"Data preview: {str(row.get('data'))[:200]}...")
                    print("=" * 80)
                    print()

                if fix:
                    # Fix each row individually by removing actual null bytes
                    cleaned_count = 0
                    for row in null_byte_rows:
                        cleaned_data = row["data"].replace("\x00", "")
                        cur.execute(
                            f"""
                            UPDATE {table} 
                            SET data = %s
                            WHERE id = %s
                        """,
                            (cleaned_data, row["id"]),
                        )
                        cleaned_count += cur.rowcount
                    print(f"Cleaned {cleaned_count} rows with actual null bytes.")
                else:
                    print("Run with --fix to remove actual null bytes.")
            else:
                print("No rows with actual null byte characters found.")

            if literal_null_rows:
                print(
                    f"\nFound {len(literal_null_rows)} rows with literal '\\u0000' strings:\n"
                )
                for row in literal_null_rows:
                    print("=" * 80)
                    print(f"Row ID: {row.get('id')}")
                    print(f"Data preview: {str(row.get('data'))[:200]}...")
                    print("=" * 80)
                    print()

                if fix:
                    # Don't automatically fix literal "\u0000" strings as they may be legitimate
                    # (e.g., in error messages about null bytes)
                    print(
                        "Skipping literal '\\u0000' strings - these may be legitimate content."
                    )
                    print(
                        "If you need to remove them, handle manually after verifying."
                    )
                else:
                    print(
                        "Note: These are literal strings '\\u0000', not actual null bytes."
                    )
                    print("They may be legitimate content (e.g., error messages).")
            else:
                print("No rows with literal '\\u0000' strings found.")

            # Also check for unparseable JSON
            cur.execute(f"SELECT * FROM {table}")
            rows = cur.fetchall()

            unparseable_rows = []
            for row in rows:
                data = row.get("data")
                if data is None:
                    continue

                try:
                    json.loads(data)
                except json.JSONDecodeError as e:
                    unparseable_rows.append({"row": dict(row), "error": str(e)})

            if unparseable_rows:
                print(f"\nFound {len(unparseable_rows)} rows with unparseable JSON:\n")
                for item in unparseable_rows:
                    print("=" * 80)
                    print(f"Row ID: {item['row'].get('id')}")
                    print(f"Parse error: {item['error']}")
                    print("=" * 80)
                    print()

                if fix:
                    print("Attempting to fix unparseable JSON rows...")
                    fixed_count = 0
                    for item in unparseable_rows:
                        row = item["row"]
                        data = row.get("data", "")

                        # Try to fix common issues
                        cleaned = data

                        # Remove null bytes and other control characters except \n, \t, \r
                        cleaned = "".join(
                            char
                            for char in cleaned
                            if ord(char) >= 32 or char in "\n\t\r"
                        )

                        # Fix unescaped backslashes
                        # Strategy: Find backslashes that are NOT followed by valid JSON escape chars
                        # Valid JSON escape sequences: \", \\, \/, \b, \f, \n, \r, \t, \uXXXX
                        import re

                        # Replace backslashes that are NOT followed by valid escape characters
                        # This regex matches a backslash followed by anything that's NOT a valid escape char
                        # We need to escape these backslashes
                        def fix_backslash(match):
                            # If backslash is followed by a valid escape char, keep it
                            # Otherwise, double it
                            return match.group(0)

                        # More direct approach: scan character by character
                        result = []
                        i = 0
                        while i < len(cleaned):
                            if cleaned[i] == "\\":
                                # Check if this is a valid escape sequence
                                if i + 1 < len(cleaned):
                                    next_char = cleaned[i + 1]
                                    if next_char in '"\\/bfnrt':
                                        # Valid escape sequence, keep as-is
                                        result.append(cleaned[i : i + 2])
                                        i += 2
                                    elif next_char == "u" and i + 5 < len(cleaned):
                                        # Could be \uXXXX, check if next 4 are hex
                                        hex_part = cleaned[i + 2 : i + 6]
                                        try:
                                            int(hex_part, 16)
                                            # Valid \uXXXX sequence
                                            result.append(cleaned[i : i + 6])
                                            i += 6
                                        except ValueError:
                                            # Invalid hex, escape the backslash
                                            result.append("\\\\")
                                            i += 1
                                    else:
                                        # Invalid escape sequence, double the backslash
                                        result.append("\\\\")
                                        i += 1
                                else:
                                    # Backslash at end of string, escape it
                                    result.append("\\\\")
                                    i += 1
                            else:
                                result.append(cleaned[i])
                                i += 1

                        cleaned = "".join(result)

                        # Apply specific fixes for known issues
                        # Fix: "must be escaped to \\\\" should be "must be escaped to \\\\u0000"
                        cleaned = cleaned.replace(
                            'must be escaped to \\\\\\\\"',
                            'must be escaped to \\\\\\\\u0000\\"',
                        )

                        try:
                            json.loads(cleaned)
                            cur.execute(
                                f"""
                                UPDATE {table} 
                                SET data = %s
                                WHERE id = %s
                            """,
                                (cleaned, row["id"]),
                            )
                            fixed_count += 1
                            print(f"Fixed row {row['id']}")
                        except json.JSONDecodeError as e:
                            print(
                                f"Could not fix row {row['id']} - still unparseable after cleaning: {e}"
                            )
                    print(
                        f"\nFixed {fixed_count} of {len(unparseable_rows)} unparseable rows."
                    )
            else:
                print("\nAll JSON data is valid! Ready to migrate to JSONB.")

        conn.close()


def main():
    parser = argparse.ArgumentParser(
        description="Check for problematic JSON (null bytes, unparseable) before migrating to JSONB."
    )
    parser.add_argument(
        "--dbpath",
        default="pgdata",
        help="Path to pgembed data directory (default: pgdata)",
    )
    parser.add_argument(
        "--database", default="opencode", help="Database name (default: opencode)"
    )
    parser.add_argument(
        "--table", default="message", help="Table name to check (default: message)"
    )
    parser.add_argument(
        "--fix", action="store_true", help="Automatically fix null byte issues"
    )

    args = parser.parse_args()

    find_problematic_json(args.dbpath, args.database, args.table, fix=args.fix)


if __name__ == "__main__":
    main()
