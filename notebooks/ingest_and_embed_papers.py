# Databricks notebook source
# /// script
# [tool.databricks.environment]
# environment_version = "5"
# ///
# DBTITLE 1,Ingest Papers & Compute Embeddings
# MAGIC %md
# MAGIC # Ingest Papers from OpenAlex → Lakebase → Vector Embeddings
# MAGIC
# MAGIC This notebook implements the **Spark data pipeline** for the AI Research & Learning Copilot:
# MAGIC
# MAGIC 1. **Fetch** papers from the OpenAlex API for configured research topics
# MAGIC 2. **Upsert** papers, authors, and relationships into Lakebase Postgres
# MAGIC 3. **Chunk** abstracts into overlapping segments for embedding
# MAGIC 4. **Compute** vector embeddings using `sentence-transformers/all-MiniLM-L6-v2`
# MAGIC 5. **Store** embeddings in Lakebase pgvector for semantic retrieval

# COMMAND ----------

# DBTITLE 1,Install dependencies
# MAGIC %pip uninstall -y psycopg2 psycopg2-binary
# MAGIC %pip install -q 'databricks-sdk>=0.30.0' 'psycopg2-binary>=2.9.9' sentence-transformers requests numpy sqlalchemy

# COMMAND ----------

# DBTITLE 1,Restart Python after pip install
dbutils.library.restartPython()

# COMMAND ----------

# DBTITLE 1,Adjust Python path for project imports
import sys, os

# Add the project root to import lakebase.py and openalex_client.py
project_root = os.path.dirname(os.getcwd())
sys.path.insert(0, project_root)
print(f"Project root: {project_root}")

# COMMAND ----------

# DBTITLE 1,Configuration
# MAGIC %md
# MAGIC ## Configuration & Parameters

# COMMAND ----------

# DBTITLE 1,Set pipeline parameters
dbutils.widgets.text("embedding_model", "sentence-transformers/all-MiniLM-L6-v2", "Embedding model")
dbutils.widgets.text("chunk_size", "800", "Chunk size (chars)")
dbutils.widgets.text("chunk_overlap", "200", "Chunk overlap (chars)")
dbutils.widgets.text("papers_per_topic", "25", "Papers to fetch per topic")
dbutils.widgets.text("search_topics", 
    "transformer attention mechanism,retrieval augmented generation,large language models,graph neural networks",
    "Comma-separated research topics")

# Read widget values
EMBEDDING_MODEL = dbutils.widgets.get("embedding_model")
CHUNK_SIZE = int(dbutils.widgets.get("chunk_size"))
CHUNK_OVERLAP = int(dbutils.widgets.get("chunk_overlap"))
PAPERS_PER_TOPIC = int(dbutils.widgets.get("papers_per_topic"))
SEARCH_TOPICS = [t.strip() for t in dbutils.widgets.get("search_topics").split(",") if t.strip()]

print(f"Model:          {EMBEDDING_MODEL}")
print(f"Chunk size:     {CHUNK_SIZE} chars (overlap {CHUNK_OVERLAP})")
print(f"Per topic:      {PAPERS_PER_TOPIC} papers")
print(f"Topics ({len(SEARCH_TOPICS)}):  {SEARCH_TOPICS}")

# COMMAND ----------

# DBTITLE 1,Lakebase Connection & Schema
# MAGIC %md
# MAGIC ## Test Lakebase Connection & Initialise Schema

# COMMAND ----------

# DBTITLE 1,Test Lakebase connection
from lakebase import get_connection, run_query
import traceback

print("Testing Lakebase connection...\n")
try:
    with get_connection() as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT COUNT(*) AS cnt FROM information_schema.tables")
            count = cur.fetchone()["cnt"]
            print(f"✅ Connection successful — {count} tables in information_schema")
    print("✅ psycopg2 with Lakebase working correctly!")
except Exception as e:
    print(f"❌ Connection failed: {e}")
    traceback.print_exc()

# COMMAND ----------

# DBTITLE 1,Initialise schema if needed
from lakebase import init_schema, run_query

# Check if core tables exist
result = run_query("""
    SELECT COUNT(*) AS table_count
    FROM information_schema.tables
    WHERE table_schema = 'public'
      AND table_name IN ('papers', 'authors', 'embeddings', 'users',
                         'learning_goals', 'collections', 'collection_papers',
                         'paper_authors', 'reading_progress', 'notes')
""")
count = result[0]["table_count"]

if count < 10:
    init_schema()
    print(f"✅ Missing tables created.")
else:
    print(f"✅ All 10 tables already exist.")

# COMMAND ----------

# DBTITLE 1,Fetch Papers from OpenAlex
# MAGIC %md
# MAGIC ## Fetch Papers from OpenAlex API for Configured Topics

# COMMAND ----------

# DBTITLE 1,Fetch papers for configured topics
import pandas as pd
from openalex_client import OpenAlexClient

client = OpenAlexClient()

all_papers = []
seen_ids = set()

for topic in SEARCH_TOPICS:
    print(f"\n🔍 Fetching papers for: '{topic}'")
    try:
        papers = client.search_papers_for_goal(topic, limit=PAPERS_PER_TOPIC)
        new_count = 0
        for p in papers:
            if p["paper_id"] not in seen_ids:
                seen_ids.add(p["paper_id"])
                all_papers.append(p)
                new_count += 1
        print(f"   ✅ {len(papers)} found, {new_count} new (de-duped)")
    except Exception as e:
        print(f"   ❌ Error: {e}")

print(f"\n📚 Total unique papers collected: {len(all_papers)}")

# COMMAND ----------

# DBTITLE 1,Upsert papers, authors, and relationships into Lakebase
import json
from lakebase import get_connection

paper_count = 0
author_count = 0
link_count = 0

with get_connection() as conn:
    with conn.cursor() as cur:
        for paper in all_papers:
            # --- Upsert paper ---
            # On conflict with `paper_id`: Overwrite `title`, `abstract`, `cited_by_count`, `pdf_url`, `concepts` only with new values
            cur.execute("""
                INSERT INTO papers (paper_id, title, abstract, publication_date,
                                   doi, cited_by_count, source_name, pdf_url,
                                   openalex_url, concepts)
                VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
                ON CONFLICT (paper_id) DO UPDATE
                    SET title          = EXCLUDED.title,
                        abstract       = EXCLUDED.abstract,
                        cited_by_count = EXCLUDED.cited_by_count,
                        pdf_url        = EXCLUDED.pdf_url,
                        concepts       = EXCLUDED.concepts
                RETURNING (xmax = 0) AS inserted
            """, (
                paper["paper_id"],
                paper["title"],
                paper["abstract"],
                paper["publication_date"],
                paper["doi"],
                paper["cited_by_count"],
                paper["source_name"],
                paper["pdf_url"],
                paper["openalex_url"],
                json.dumps(paper["concepts"]),  # json.dumps serializes the list of topic dicts into JSON string
            ))
            row = cur.fetchone()    # example output: {'inserted': True}
            if row and row["inserted"]:  # row['inserted'] = true -> newly inserted; false -> updated
                paper_count += 1

            # --- Upsert authors + links ---
            # On conflict with `author_id`: Overwrite `display_name`, `institution` only with new values
            for auth in paper["authorships"]:
                if not auth["author_id"]:
                    continue
                cur.execute("""
                    INSERT INTO authors (author_id, display_name, institution,
                                        orcid, openalex_url)
                    VALUES (%s, %s, %s, %s, %s)
                    ON CONFLICT (author_id) DO UPDATE
                        SET display_name = EXCLUDED.display_name,
                            institution  = EXCLUDED.institution
                    RETURNING (xmax = 0) AS inserted
                """, (
                    auth["author_id"],
                    auth["display_name"],
                    auth["institution"],
                    auth["orcid"],
                    auth["openalex_url"],
                ))
                arow = cur.fetchone()
                if arow and arow["inserted"]:
                    author_count += 1

                cur.execute("""
                    INSERT INTO paper_authors (paper_id, author_id, position)
                    VALUES (%s, %s, %s)
                    ON CONFLICT DO NOTHING
                """, (paper["paper_id"], auth["author_id"], auth["position"]))
                link_count += cur.rowcount

        conn.commit()  # Wraps all INSERT statements above into a single transaction; If any INSERT fails, all INSERTS are rolled back as if nothing happened.

print(f"✅ Upserted into Lakebase:")
print(f"   Papers:       {paper_count} new")
print(f"   Authors:      {author_count} new")
print(f"   Paper-Author Mapping:  {link_count} new links")

# COMMAND ----------

# DBTITLE 1,Seed sample learning goals and notes
from lakebase import get_connection, run_query

# Get the demo user and a couple of paper_ids for FK references
demo_user = run_query("SELECT user_id FROM users WHERE email = 'demo@example.com'")
user_id = demo_user[0]["user_id"]

sample_papers = run_query("SELECT paper_id, title FROM papers LIMIT 2")
paper_ids = [r["paper_id"] for r in sample_papers]

print(f"Demo user_id: {user_id}")
print(f"Sample paper_ids: {paper_ids}\n")

# ── Sample learning goals ────────────────────────────────────────
sample_goals = [
    {
        "title": "Understand transformer attention mechanisms",
        "description": (
            "Study the self-attention and multi-head attention mechanisms "
            "introduced in the Transformer architecture. Understand how "
            "queries, keys, and values are computed, how scaled dot-product "
            "attention works, and why positional encodings are needed. "
            "Compare with earlier sequence-to-sequence models using RNNs."
        ),
    },
    {
        "title": "Build a RAG pipeline for academic papers",
        "description": (
            "Learn how to build a Retrieval-Augmented Generation pipeline "
            "that retrieves relevant paper chunks from a vector database "
            "and feeds them as context to a large language model. Cover "
            "chunking strategies, embedding models, similarity search with "
            "pgvector, and prompt construction for grounded answers."
        ),
    },
]

# ── Sample notes ─────────────────────────────────────────────────
sample_notes = [
    {
        "paper_id": paper_ids[0],
        "content": (
            "Key takeaway: the Transformer replaces recurrence entirely with "
            "self-attention, allowing full parallelisation during training. "
            "Multi-head attention lets the model attend to information from "
            "different representation subspaces at different positions. "
            "The positional encoding uses sine and cosine functions of "
            "different frequencies so the model can learn relative positions."
        ),
    },
    {
        "paper_id": paper_ids[1],
        "content": (
            "This paper presents an interesting approach to combining visual "
            "and textual representations. The cross-attention mechanism is "
            "particularly effective for fusing multi-modal features. Need to "
            "revisit the ablation study in Table 3 — the results suggest "
            "that pre-training on larger corpora matters more than model size."
        ),
    },
]

# ── Insert with ON CONFLICT DO NOTHING (safe to re-run) ─────────
goal_count = 0
note_count = 0

with get_connection() as conn:
    with conn.cursor() as cur:
        for g in sample_goals:
            cur.execute("""
                INSERT INTO learning_goals (user_id, title, description)
                SELECT %s, %s, %s
                WHERE NOT EXISTS (
                    SELECT 1 FROM learning_goals
                    WHERE user_id = %s AND title = %s
                )
            """, (user_id, g["title"], g["description"], user_id, g["title"]))
            goal_count += cur.rowcount

        for n in sample_notes:
            cur.execute("""
                INSERT INTO notes (user_id, paper_id, content)
                SELECT %s, %s, %s
                WHERE NOT EXISTS (
                    SELECT 1 FROM notes
                    WHERE user_id = %s AND paper_id = %s AND content = %s
                )
            """, (user_id, n["paper_id"], n["content"], user_id, n["paper_id"], n["content"]))
            note_count += cur.rowcount

        conn.commit()

print(f"✅ Seeded sample data:")
print(f"   Learning goals: {goal_count} inserted")
print(f"   Notes:          {note_count} inserted")

# COMMAND ----------

# DBTITLE 1,Compute Embeddings
# MAGIC %md
# MAGIC ## Chunk Abstracts & Compute Vector Embeddings

# COMMAND ----------

# DBTITLE 1,Chunk abstracts, notes, and goals for embedding
from collections import Counter
from lakebase import run_query


def chunk_text(text, chunk_size, chunk_overlap):
    """Split text into overlapping chunks using a sliding window."""
    result = []
    start = 0
    idx = 0
    while start < len(text):
        end = start + chunk_size
        chunk = text[start:end]
        if chunk.strip():
            result.append((idx, chunk.strip()))
            idx += 1
        start += chunk_size - chunk_overlap
    return result


# ── 1. Fetch text sources from Lakebase ──────────────────────────
papers_with_abstract = run_query("""
    SELECT paper_id, title, abstract
    FROM papers
    WHERE abstract IS NOT NULL AND TRIM(abstract) != ''
""")

notes = run_query("""
    SELECT note_id, content, paper_id
    FROM notes
    WHERE content IS NOT NULL AND TRIM(content) != ''
""")

goals = run_query("""
    SELECT goal_id, title, description
    FROM learning_goals
    WHERE title IS NOT NULL AND TRIM(title) != ''
""")

print(f"Sources: {len(papers_with_abstract)} abstracts, {len(notes)} notes, {len(goals)} goals")

# ── 2. Chunk all text sources ────────────────────────────────────
chunks = []  # list of (source_type, source_id, chunk_index, chunk_text)

# Abstracts: prepend title for richer context
for row in papers_with_abstract:
    text = f"{row['title']}\n\n{row['abstract']}"
    for idx, chunk in chunk_text(text, CHUNK_SIZE, CHUNK_OVERLAP):
        chunks.append(("abstract", row["paper_id"], idx, chunk))

# Notes: embed student notes tied to papers
for row in notes:
    for idx, chunk in chunk_text(row["content"], CHUNK_SIZE, CHUNK_OVERLAP):
        chunks.append(("note", str(row["note_id"]), idx, chunk))

# Goals: combine title + description for learning goals
for row in goals:
    desc = row["description"] or ""
    text = f"{row['title']}\n\n{desc}" if desc else row["title"]
    for idx, chunk in chunk_text(text, CHUNK_SIZE, CHUNK_OVERLAP):
        chunks.append(("goal", str(row["goal_id"]), idx, chunk))

# ── 3. Summary ───────────────────────────────────────────────────
type_counts = Counter(c[0] for c in chunks)
print(f"\nTotal chunks created: {len(chunks)}")
for src_type, count in sorted(type_counts.items()):
    print(f"  {src_type:12s} {count:>5d} chunks")

# COMMAND ----------

# DBTITLE 1,Compute embeddings with sentence-transformers
import os
import hashlib
from sentence_transformers import SentenceTransformer

os.environ["HF_HOME"] = "/tmp/.cache/huggingface"
os.environ["TRANSFORMERS_CACHE"] = "/tmp/.cache/huggingface"

print(f"Loading model: {EMBEDDING_MODEL}")
model = SentenceTransformer(EMBEDDING_MODEL, cache_folder="/tmp/.cache/huggingface")
print(f"✅ Model loaded (dim={model.get_sentence_embedding_dimension()})")

# Extract just the chunk texts for batch encoding
chunk_texts = [c[3] for c in chunks]

print(f"\nEncoding {len(chunk_texts)} chunks...")
embedding_vectors = model.encode(chunk_texts, show_progress_bar=True, batch_size=64)
print(f"✅ Computed {len(embedding_vectors)} embeddings (shape: {embedding_vectors.shape})")

# Build rows for Lakebase upsert
embedding_rows = []
for i, (source_type, source_id, chunk_index, chunk_text) in enumerate(chunks):
    # Deterministic ID based on source + chunk index
    row_id = hashlib.sha256(f"{source_type}:{source_id}:{chunk_index}".encode()).hexdigest()[:32]
    vec = embedding_vectors[i].tolist()
    embedding_rows.append(
        (
            row_id, 
            source_type, 
            source_id, 
            chunk_index, 
            chunk_text,
            vec, 
            EMBEDDING_MODEL,
        )
    )

print(f"\nPrepared {len(embedding_rows)} embedding rows for upsert")

# COMMAND ----------

# DBTITLE 1,Upsert Embeddings into Lakebase
# MAGIC %md
# MAGIC ## Upsert Chunk Embeddings into Lakebase pgvector

# COMMAND ----------

# DBTITLE 1,Upsert embeddings into Lakebase
from lakebase import get_connection

inserted = 0
updated = 0

with get_connection() as conn:
    with conn.cursor() as cur:
        for embedding_id, src_type, src_id, chunk_idx, chunk_txt, vec, model_name in embedding_rows:
            embedding_str = "[" + ",".join(str(float(x)) for x in vec) + "]"
            cur.execute("""
                INSERT INTO embeddings (id, source_type, source_id, chunk_index,
                                       chunk_text, embedding, model_name)
                VALUES (%s, %s, %s, %s, %s, %s::vector, %s)
                ON CONFLICT (id) DO UPDATE
                    SET chunk_text = EXCLUDED.chunk_text,
                        embedding  = EXCLUDED.embedding,
                        model_name = EXCLUDED.model_name
                RETURNING (xmax = 0) AS inserted
            """, (embedding_id, src_type, src_id, chunk_idx, chunk_txt, embedding_str, model_name))
            result = cur.fetchone()
            if result and result["inserted"]:
                inserted += 1
            if result and not result["inserted"]:
                updated += 1
        conn.commit()

print(f"✅ Embeddings upserted into Lakebase:")
print(f"   Inserted: {inserted}")
print(f"   Updated:  {updated}")
print(f"   Total:    {inserted + updated}")

# COMMAND ----------

# DBTITLE 1,Verification & Testing
# MAGIC %md
# MAGIC ## Testing
# MAGIC
# MAGIC * Test semantic similarity search across source types (abstracts, notes, learning goals)
# MAGIC * Validate row counts across all Lakebase tables.

# COMMAND ----------

# DBTITLE 1,Semantic search: find papers matching a learning goal
from lakebase import run_query
import pandas as pd

# Find the most relevant papers for each learning goal via cosine similarity
results = run_query("""
    WITH

    abstract_embedding AS (
        SELECT
          p.title AS paper_title,
          e.chunk_text AS abstract_chunk,
          e.embedding AS abstract_embedding
        FROM embeddings AS e
        JOIN papers AS p
        ON e.source_id = p.paper_id
        WHERE e.source_type = 'abstract'
    ),

    goal_embedding AS (
        SELECT
          chunk_text AS goal,
          embedding AS goal_embedding
        FROM embeddings
        WHERE source_type = 'goal'
    ),

    Final AS (
        SELECT
            g.goal,
            a.paper_title,
            a.abstract_chunk,
            1 - (g.goal_embedding <=> a.abstract_embedding) AS similarity
        FROM goal_embedding AS g
        CROSS JOIN abstract_embedding AS a
        ORDER BY g.goal_embedding <=> a.abstract_embedding
    )

    SELECT
        goal,
        paper_title,
        MAX(similarity) AS similarity
    FROM Final
    GROUP BY 1, 2
    ORDER BY similarity DESC
    LIMIT 5
""")

df = pd.DataFrame(results)
display(df)

# COMMAND ----------

# DBTITLE 1,Verify pipeline results
from lakebase import run_query

print("\n" + "="*60)
print("PIPELINE SUMMARY")
print("="*60)

tables = [
    ("papers", "SELECT COUNT(*) AS cnt FROM papers"),
    ("authors", "SELECT COUNT(*) AS cnt FROM authors"),
    ("paper_authors", "SELECT COUNT(*) AS cnt FROM paper_authors"),
    ("embeddings", "SELECT COUNT(*) AS cnt FROM embeddings"),
    ("users", "SELECT COUNT(*) AS cnt FROM users"),
    ("learning_goals", "SELECT COUNT(*) AS cnt FROM learning_goals"),
    ("notes", "SELECT COUNT(*) AS cnt FROM notes"),
]

for name, sql in tables:
    result = run_query(sql)
    print(f"  {name:20s} {result[0]['cnt']:>6d} rows")

print("\nEmbeddings by source_type:")
rows = run_query("SELECT source_type, COUNT(*) AS cnt FROM embeddings GROUP BY source_type ORDER BY cnt DESC")
for row in rows:
    print(f"  {row['source_type']:20s} {row['cnt']:>6d}")

print("\n✅ Pipeline complete!")