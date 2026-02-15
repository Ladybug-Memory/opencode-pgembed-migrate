import os
import pgembed
import sqlalchemy as sa
from fastembed import TextEmbedding
import argparse

# Path to pgdata directory
pgdata_path = "/tmp/t1/pgdata"
database_name = "opencode"

# Embedding model
try:
    model = TextEmbedding()
except Exception as e:
    print(f"Failed to load embedding model: {e}")
    exit(1)

DIM = 384


def search_similar_sessions(query_text, limit=5, min_similarity=None):
    """Search for similar sessions by title using cosine similarity."""
    query_vector = [float(x) for x in next(model.embed([query_text]))]
    query_str = "[" + ",".join(str(v) for v in query_vector) + "]"

    with pgembed.get_server(pgdata_path) as pg:
        uri = pg.get_uri(database_name)
        engine = sa.create_engine(uri)
        with engine.connect() as conn:
            sql = """
                SELECT id, title,
                       1 - (title_vector <=> :query_vec) AS similarity
                FROM session
                WHERE title IS NOT NULL
            """

            if min_similarity:
                sql += f" AND (title_vector <=> :query_vec) < {1 - min_similarity}"

            sql += f"""
                ORDER BY title_vector <=> :query_vec
                LIMIT {limit}
            """

            result = conn.execute(sa.text(sql), {"query_vec": query_str})

            print(f"\nTop {limit} similar sessions for: '{query_text}'")
            print("=" * 80)
            for row in result:
                print(f"ID: {row.id}")
                print(f"Title: {row.title}")
                print(f"Similarity: {row.similarity:.4f}")
                print("-" * 80)


def search_messages_by_model(query_text, model_id, limit=5):
    """Search messages filtered by modelID."""
    query_vector = [float(x) for x in next(model.embed([query_text]))]
    query_str = "[" + ",".join(str(v) for v in query_vector) + "]"

    with pgembed.get_server(pgdata_path) as pg:
        uri = pg.get_uri(database_name)
        engine = sa.create_engine(uri)
        with engine.connect() as conn:
            sql = """
                SELECT id,
                       data->>'modelID' as model_id,
                       data->'summary'->>'title' as title,
                       1 - (data_vector <=> :query_vec) AS similarity
                FROM message
                WHERE data_vector IS NOT NULL
                  AND data->>'modelID' = :model_id
                ORDER BY data_vector <=> :query_vec
                LIMIT :limit
            """

            result = conn.execute(
                sa.text(sql),
                {"query_vec": query_str, "model_id": model_id, "limit": limit},
            )

            print(
                f"\nTop {limit} messages matching '{query_text}' with modelID='{model_id}':"
            )
            print("=" * 80)
            for row in result:
                print(f"ID: {row.id}")
                print(f"Title: {row.title}")
                print(f"Model: {row.model_id}")
                print(f"Similarity: {row.similarity:.4f}")
                print("-" * 80)


def search_messages_by_agent(query_text, agent, limit=5):
    """Search messages filtered by agent type."""
    query_vector = [float(x) for x in next(model.embed([query_text]))]
    query_str = "[" + ",".join(str(v) for v in query_vector) + "]"

    with pgembed.get_server(pgdata_path) as pg:
        uri = pg.get_uri(database_name)
        engine = sa.create_engine(uri)
        with engine.connect() as conn:
            sql = """
                SELECT id,
                       data->>'agent' as agent,
                       data->>'modelID' as model_id,
                       data->'summary'->>'title' as title,
                       1 - (data_vector <=> :query_vec) AS similarity
                FROM message
                WHERE data_vector IS NOT NULL
                  AND data->>'agent' = :agent
                ORDER BY data_vector <=> :query_vec
                LIMIT :limit
            """

            result = conn.execute(
                sa.text(sql), {"query_vec": query_str, "agent": agent, "limit": limit}
            )

            print(
                f"\nTop {limit} messages matching '{query_text}' with agent='{agent}':"
            )
            print("=" * 80)
            for row in result:
                print(f"ID: {row.id}")
                print(f"Title: {row.title}")
                print(f"Agent: {row.agent}, Model: {row.model_id}")
                print(f"Similarity: {row.similarity:.4f}")
                print("-" * 80)


def search_with_date_range(query_text, start_time=None, end_time=None, limit=5):
    """Search messages with time range filter."""
    query_vector = [float(x) for x in next(model.embed([query_text]))]
    query_str = "[" + ",".join(str(v) for v in query_vector) + "]"

    with pgembed.get_server(pgdata_path) as pg:
        uri = pg.get_uri(database_name)
        engine = sa.create_engine(uri)
        with engine.connect() as conn:
            sql = """
                SELECT id,
                       data->>'modelID' as model_id,
                       data->'summary'->>'title' as title,
                       to_timestamp((data->'time'->>'created')::bigint / 1000) as created_at,
                       1 - (data_vector <=> :query_vec) AS similarity
                FROM message
                WHERE data_vector IS NOT NULL
            """

            if start_time:
                sql += f" AND to_timestamp((data->'time'->>'created')::bigint / 1000) >= '{start_time}'"
            if end_time:
                sql += f" AND to_timestamp((data->'time'->>'created')::bigint / 1000) <= '{end_time}'"

            sql += f"""
                ORDER BY data_vector <=> :query_vec
                LIMIT {limit}
            """

            result = conn.execute(sa.text(sql), {"query_vec": query_str})

            time_range = (
                f"from {start_time} to {end_time}"
                if (start_time or end_time)
                else "all time"
            )
            print(f"\nTop {limit} messages matching '{query_text}' ({time_range}):")
            print("=" * 80)
            for row in result:
                print(f"ID: {row.id}")
                print(f"Title: {row.title}")
                print(f"Model: {row.model_id}, Created: {row.created_at}")
                print(f"Similarity: {row.similarity:.4f}")
                print("-" * 80)


def main():
    parser = argparse.ArgumentParser(
        description="Query pgvector HNSW index with filters"
    )
    parser.add_argument("query", help="Search query text")
    parser.add_argument(
        "--limit", type=int, default=5, help="Number of results (default: 5)"
    )
    parser.add_argument("--model", help="Filter by modelID")
    parser.add_argument(
        "--agent", help="Filter by agent (build, explore, general, etc)"
    )
    parser.add_argument(
        "--sessions", action="store_true", help="Search sessions instead of messages"
    )
    parser.add_argument(
        "--min-similarity", type=float, help="Minimum similarity threshold (0-1)"
    )
    parser.add_argument("--start-time", help="Start time (YYYY-MM-DD HH:MM:SS)")
    parser.add_argument("--end-time", help="End time (YYYY-MM-DD HH:MM:SS)")

    args = parser.parse_args()

    if args.sessions:
        search_similar_sessions(args.query, args.limit, args.min_similarity)
    elif args.model:
        search_messages_by_model(args.query, args.model, args.limit)
    elif args.agent:
        search_messages_by_agent(args.query, args.agent, args.limit)
    elif args.start_time or args.end_time:
        search_with_date_range(args.query, args.start_time, args.end_time, args.limit)
    else:
        # Default: search all messages
        search_messages_by_agent(args.query, "", args.limit)


if __name__ == "__main__":
    main()
