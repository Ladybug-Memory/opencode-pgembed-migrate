import os
import pgembed
import sqlalchemy as sa
from fastembed import TextEmbedding
from tqdm import tqdm
import traceback

# Path to pgdata directory
pgdata_path = "/tmp/t1/pgdata"
database_name = "opencode"

# Embedding model - using default BAAI/bge-small-en-v1.5
try:
    model = TextEmbedding()
except Exception as e:
    print(f"Failed to load embedding model: {e}")
    exit(1)

DIM = 384  # bge-small-en-v1.5 dimensions - not matryoshka, truncating would hurt recall


def main():
    with pgembed.get_server(pgdata_path) as pg:
        uri = pg.get_uri(database_name)
        engine = sa.create_engine(uri)
        with engine.connect() as conn:
            try:
                # Ensure vector extension
                with conn.begin():
                    conn.execute(sa.text("CREATE EXTENSION IF NOT EXISTS vector"))

                # Note: message.data is already converted to jsonb by check_json.py

                # Add vector columns if not exist
                with conn.begin():
                    # Check if session table exists
                    result = conn.execute(
                        sa.text(
                            "SELECT EXISTS (SELECT 1 FROM information_schema.tables WHERE table_name = 'session')"
                        )
                    )
                    row = result.fetchone()
                    if row and row[0]:
                        conn.execute(sa.text(f"""
                            ALTER TABLE session ADD COLUMN IF NOT EXISTS title_vector VECTOR({DIM})
                        """))
                    # Check if message table exists
                    result = conn.execute(
                        sa.text(
                            "SELECT EXISTS (SELECT 1 FROM information_schema.tables WHERE table_name = 'message')"
                        )
                    )
                    row = result.fetchone()
                    if row and row[0]:
                        conn.execute(sa.text(f"""
                            ALTER TABLE message ADD COLUMN IF NOT EXISTS data_vector VECTOR({DIM})
                        """))

                # Populate vectors for session
                with conn.begin():
                    result = conn.execute(
                        sa.text(
                            "SELECT EXISTS (SELECT 1 FROM information_schema.tables WHERE table_name = 'session')"
                        )
                    )
                    row = result.fetchone()
                    if row and row[0]:
                        # Get count for progress bar
                        count_result = conn.execute(
                            sa.text(
                                "SELECT COUNT(*) FROM session WHERE title IS NOT NULL AND title_vector IS NULL"
                            )
                        )
                        total = count_result.fetchone()[0]
                        result = conn.execute(
                            sa.text(
                                "SELECT id, title FROM session WHERE title IS NOT NULL AND title_vector IS NULL"
                            )
                        )
                        for row in tqdm(result, total=total, desc="Indexing sessions"):
                            embed_gen = model.embed([row.title])
                            vector = [float(x) for x in next(embed_gen)]
                            conn.execute(
                                sa.text(
                                    "UPDATE session SET title_vector = :vector WHERE id = :id"
                                ),
                                {"vector": vector, "id": row.id},
                            )

                # Populate vectors for message (using summary.title only)
                with conn.begin():
                    result = conn.execute(
                        sa.text(
                            "SELECT EXISTS (SELECT 1 FROM information_schema.tables WHERE table_name = 'message')"
                        )
                    )
                    row = result.fetchone()
                    if row and row[0]:
                        # Get count for progress bar - only rows with summary.title
                        count_result = conn.execute(sa.text("""
                            SELECT COUNT(*) FROM message
                            WHERE data->'summary'->>'title' IS NOT NULL
                            AND data_vector IS NULL
                        """))
                        total = count_result.fetchone()[0]
                        result = conn.execute(sa.text("""
                            SELECT id, data->'summary'->>'title' as title
                            FROM message
                            WHERE data->'summary'->>'title' IS NOT NULL
                            AND data_vector IS NULL
                        """))
                        for row in tqdm(
                            result, total=total, desc="Indexing message summaries"
                        ):
                            title = row.title
                            if title:
                                embed_gen = model.embed([title])
                                vector = [float(x) for x in next(embed_gen)]
                                conn.execute(
                                    sa.text(
                                        "UPDATE message SET data_vector = :vector WHERE id = :id"
                                    ),
                                    {"vector": vector, "id": row.id},
                                )

                # Create HNSW indexes
                with conn.begin():
                    result = conn.execute(
                        sa.text(
                            "SELECT EXISTS (SELECT 1 FROM information_schema.tables WHERE table_name = 'session')"
                        )
                    )
                    row = result.fetchone()
                    if row and row[0]:
                        conn.execute(
                            sa.text(
                                "CREATE INDEX IF NOT EXISTS idx_session_title_vector ON session USING hnsw (title_vector vector_cosine_ops)"
                            )
                        )
                    result = conn.execute(
                        sa.text(
                            "SELECT EXISTS (SELECT 1 FROM information_schema.tables WHERE table_name = 'message')"
                        )
                    )
                    row = result.fetchone()
                    if row and row[0]:
                        conn.execute(
                            sa.text(
                                "CREATE INDEX IF NOT EXISTS idx_message_data_vector ON message USING hnsw (data_vector vector_cosine_ops)"
                            )
                        )

                print("Indexing completed.")
            except Exception as e:
                print(f"Error during indexing: {e}")
                traceback.print_exc()
                # Don't re-raise to allow pgembed context to exit cleanly


if __name__ == "__main__":
    try:
        main()
    except Exception as e:
        print(f"Fatal error: {e}")
        traceback.print_exc()
        exit(1)
