# AI Research & Learning Copilot
### A Databricks App Powered by Lakebase, OpenAlex & MCP

An AI-powered platform for discovering academic papers, building personalized
study plans, and tracking research progress. Users create learning objectives,
discover relevant papers via semantic search and the OpenAlex API, save them
into collections, and ask an AI agent to summarize, compare, or recommend
papers.

## About This Project

This application is a submission for the [DataExpert.io](https://www.dataexpert.io)
program [The Rise of the AI Data Engineer](https://learn.dataexpert.io/program/the-one-week-beginners-databricks-boot-camp-7129).

All data is stored in **Databricks Lakebase Postgres** with **pgvector** for
semantic retrieval.

## Architecture

The system has two runtime paths — the **Flask web app** for interactive
use and the **MCP server** for Agent Bricks — both sharing a single backend.
A separate **Spark notebook** handles offline data ingestion.

### Request Flow

```
                        ┌─────────────────┐
                        │  User (Browser)  │
                        └────────┬────────┘
                                 │
                                 ▼
                      ┌─────────────────────┐
                      │  app.py (Flask App)  │
                      │  Databricks App      │
                      └──────┬──────┬───────┘
                             │      │
              ┌──────────────┘      └──────────────┐
              │  UI routes                         │  /api/agent/chat
              │  (goals, search,                   │
              │   collections,                     │
              │   paper detail)                    │
              │                                    ▼
              │                       ┌────────────────────────┐
              │                       │  research_tools.py     │
              │                       │  ┌──────────────────┐  │
              │                       │  │    dispatch()     │  │
              │                       │  │  LLM classifies   │  │
              │                       │  │  user intent →    │  │
              │                       │  │  picks tool →     │  │
              │                       │  │  extracts params  │  │
              │                       │  └────────┬─────────┘  │
              │                       │           │            │
              │                       │           ▼            │
              │                       │  ┌──────────────────┐  │
              │                       │  │  8 Tool Funcs    │  │
              │                       │  │  search_papers   │  │
              │                       │  │  summarize       │  │
              │                       │  │  compare         │  │
              │                       │  │  study_plan      │  │
              │                       │  │  recommend       │  │
              │                       │  │  add_to_coll.    │  │
              │                       │  │  update_progress │  │
              │                       │  │  general_rag     │  │
              │                       │  └──────────────────┘  │
              │                       └───┬──────┬────────┬────┘
              │                           │      │        │
              ▼                           ▼      │        ▼
     ┌──────────────┐          ┌────────────┐    │   ┌──────────────────┐
     │  lakebase.py │          │ lakebase.py│    │   │openalex_client.py│
     └──────┬───────┘          └─────┬──────┘    │   └────────┬─────────┘
            │                        │           │            │
            ▼                        ▼           ▼            ▼
   ┌─────────────────┐    ┌─────────────────┐  ┌───────────────────────┐
   │    Lakebase     │    │    Lakebase     │  │    External APIs      │
   │    Postgres     │    │    Postgres     │  │  • OpenAlex API       │
   │  (CRUD queries) │    │  (pgvector)     │  │  • Databricks LLM     │
   └─────────────────┘    └─────────────────┘  │   (Llama 3.3 70B)    │
                                               └───────────────────────┘
```

### How the Agent Chat Works

1. User types a message on the `/agent` page
2. `app.py` forwards it to `research_tools.dispatch()`
3. `dispatch()` calls the **LLM** with a routing prompt to classify intent
   (e.g. "find papers on transformers" → `search_papers`)
4. The chosen **tool function** executes — querying Lakebase via `lakebase.py`,
   fetching from OpenAlex via `openalex_client.py`, or calling the LLM for
   summarization/comparison
5. The tool returns `{tool, answer, citations}` → rendered in `agent.html`
   with a tool badge and source links

### Agent Bricks MCP Server

The `mcp_server/` folder is deployed as a standalone Databricks App that
exposes 7 research tools over the Model Context Protocol (MCP). It is designed
to work with a **Databricks Agent Bricks** agent, which acts as the
orchestration layer that interprets user intent, decides which tools to call,
and synthesizes natural-language responses from the results. The diagram
below shows the full request→response chain:

```
               ┌─────────────────┐
               │       User      │
               └────────┬────────┘
                   ▲    │
          Response │    │ Prompt
                   │    ▼
┌──────────────────┴────────────────────────────┐
│         Databricks Agent Bricks Agent         │
│  • Interprets user intent                     │
│  • Decides which MCP tool(s) to call          |
|  • Synthesizes answer                         │
└───────────────────────┬───────────────────────┘
                   ▲    │
         {status,  │    │ MCP tool call (HTTP)
          message, │    │
          data}    │    ▼
┌──────────────────┴──────────────────────────────┐
│               Research MCP Server               |
|        (research_mcp_server.py, FAST MCP)       |
│  7 @mcp.tool wrappers:                          │
│  • search_papers                                │
│  • summarize_papers                             │
│  • compare_papers                               │
│  • generate_study_plan                          │
│  • add_to_collection                            │
│  • update_reading_progress                      │
│  • recommend_next_paper                         │
└───────────────────────┬─────────────────────────┘
                   ▲    │
            Result │    │ Function call
                   │    ▼
┌──────────────────┴───────────────────────────────┐
│             Broker (research_broker.py)          |
|  Self-contained backend:                         |
│  • Lakebase connection + DB helpers              │
│  • pgvector cosine-similarity search             │
│  • sentence-transformers encoding                │
│  • LLM calls (summarize, compare, recommend)     │
│  • OpenAlex search + abstract reconstruction     │
└──────┬───────────────────┬──────────────────┬────┘
       │                   │                  │
       ▼                   ▼                  ▼
┌───────────────┐  ┌───────────────┐  ┌───────────────┐
│    Lakebase   │  │  Databricks   │  │   OpenAlex    │
│    Postgres   │  │     LLM       │  │     API       │
│    (CRUD,     │  │(Llama 3.3 70B)│  │   (papers,    │
│   pgvector)   │  │               │  │   authors)    │
└───────────────┘  └───────────────┘  └───────────────┘
```

**How it works (step by step):**

1. **User** sends a natural-language prompt to the **Agent Bricks Agent** (Databricks-hosted LLM)
2. **Agent Bricks Agent** interprets intent and
   decides which MCP tool(s) to call
3. **Agent** sends MCP tool call(s) over HTTP to the research MCP server **`research_mcp_server.py`**
   — the list of research tools
4. The selected tools then call the corresponding backend functions in
   **`research_broker.py`**
5. **`research_broker.py`** hits the external services as needed:
   - **Lakebase Postgres** — for CRUD queries and pgvector similarity search
   - **OpenAlex API** — for discovering new papers and fetch data (via `openalex_search()`)
   - **Databricks LLM** (Llama 3.3 70B) — for summarization, comparison,
     study plans, recommendations (via `call_llm()`)
6. Results bubble back up: `research_broker.py` →
   `research_mcp_server.py` (formatted as `{status, message, data}`) →
   **Agent Bricks Agent** (synthesizes response in natural language) → **User**

### Offline Data Pipeline

```
┌─────────────────────────────────────────────────────────────┐
│  notebooks/ingest_and_embed_papers  (Spark notebook)        │
│                                                             │
│  OpenAlex API ──fetch──▶ papers, authors, paper_authors     │
│       │                        │                            │
│       │                        ▼                            │
│       │               Upsert to Lakebase Postgres           │
│       │                        │                            │
│       │                        ▼                            │
│       │               Chunk abstracts (sliding window)      │
│       │                        │                            │
│       │                        ▼                            │
│       │               Encode with sentence-transformers     │
│       │               (all-MiniLM-L6-v2, 384-dim)           │
│       │                        │                            │
│       │                        ▼                            │
│       │               Upsert embeddings to pgvector         │
│       │                        │                            │
│       ▼                        ▼                            │
│   OpenAlex API          Lakebase Postgres                   │
└─────────────────────────────────────────────────────────────┘
```

Run this notebook **before first use** to populate the database and build the
vector index that powers semantic search. Re-run it periodically to discover
new papers matching users' **learning goals**, update citation counts, and
refresh embeddings — all other operations (searching, adding to collections,
tracking progress) are handled by the Flask app and Agent Bricks agent at
runtime.

### Module Dependency Chain

```
Flask App (root):
lakebase.py  ◀── openalex_client.py  ◀── research_tools.py  ◀── app.py
     │                                         │
     ▼                                         ▼
Lakebase Postgres                   Databricks LLM + OpenAlex API

MCP Server (mcp_server/, self-contained):
research_broker.py  ◀── research_mcp_server.py
   │
   ▼
Lakebase Postgres + OpenAlex API + Databricks LLM
```

## Tech Stack

- **Flask** — Web framework and REST API
- **Databricks Lakebase Postgres** — Managed Postgres with pgvector
- **OpenAlex API** — Academic paper metadata, abstracts, authors, citations
- **sentence-transformers/all-MiniLM-L6-v2** — Embedding model (384-dim)
- **pgvector** — HNSW-indexed vector similarity search
- **Databricks Foundation Models** — LLM for RAG summaries (Meta Llama 3.3 70B)
- **FastMCP** — MCP server for Agent Bricks integration
- **Databricks Apps** — Hosting platform

## Features

- **Learning Goals** — Create and track research learning objectives
- **Paper Discovery** — Semantic search over indexed papers + live OpenAlex search
- **Collections** — Organize papers into themed collections
- **Reading Progress** — Track what you've read, what you're reading, and what's next
- **Notes** — Take notes on individual papers
- **AI Agent Chat** — LLM-routed tool dispatch with 8 capabilities:
  search, summarize, compare, study plan, recommend, add to collection,
  update progress, and general RAG (with citations)
- **MCP Agent Tools** — The same 7 core tools exposed via FastMCP for
  Agent Bricks integration

## Project Structure

```
AI-Research-and-Learning-Copilot/
├── mcp_server/                       # Standalone MCP server (Databricks App)
│   ├── app.yaml                      #   Deployment config (entry: research_mcp_server.py)
│   ├── requirements.txt              #   MCP server dependencies
│   ├── research_broker.py            #   Self-contained backend (DB, vector, LLM, OpenAlex)
│   └── research_mcp_server.py        #   7 @mcp.tool wrappers over broker.py
├── notebooks/
│   └── ingest_and_embed_papers      # Spark data pipeline
├── templates/                        # 8 Jinja HTML templates
│   ├── base.html                     #   Base layout + CSS design system
│   ├── index.html                    #   Dashboard (stats, goals, collections)
│   ├── goals.html                    #   Learning goals management
│   ├── search.html                   #   Paper search (semantic + OpenAlex)
│   ├── collections.html              #   Collections list
│   ├── collection.html               #   Collection detail
│   ├── paper.html                    #   Paper detail (notes, progress)
│   └── agent.html                    #   AI agent chat interface
├── app.py                            # Flask app — 15+ routes, agent chat
├── app.yaml                          # Flask app deployment config
├── requirements.txt                  # Flask app dependencies
├── research_tools.py                 # Shared backend — 8 tools + dispatcher
├── lakebase.py                       # Lakebase Postgres connection helper
├── openalex_client.py                # OpenAlex API client
├── setup_secrets.py                  # Secret scope + key provisioning
├── setup_database.sql                # 10-table schema with pgvector
├── AGENT_SYSTEM_PROMPT.md            # Agent Bricks system prompt
└── README.md                         # This file
```

## Database Schema

10 tables in Lakebase Postgres:

| Table | Purpose |
| --- | --- |
| `users` | User accounts (seed demo user) |
| `learning_goals` | Research learning objectives |
| `papers` | Paper metadata from OpenAlex |
| `authors` | Author information |
| `paper_authors` | Many-to-many paper ↔ author |
| `collections` | User paper collections |
| `collection_papers` | Many-to-many collection ↔ paper |
| `reading_progress` | Per-user reading status tracking |
| `notes` | User notes on papers |
| `embeddings` | pgvector embeddings (384-dim, HNSW index) |

## Prerequisites

1. **Databricks Lakebase Postgres** instance with pgvector extension
2. **Databricks workspace** with Apps and Foundation Model access
3. **Python 3.10+**

## Setup Instructions

### Step 1: Configure Secrets

```bash
python setup_secrets.py
```

This creates the `research_copilot` secret scope and stores:
- `lakebase-url` — Lakebase Postgres connection URL
- `openalex-api-key` — OpenAlex API key (optional but recommended)
- `openalex-email` — Email for OpenAlex polite pool (optional)

### Step 2: Create Database Schema

Run `setup_database.sql` in the Lakebase SQL Editor to create all 10 tables,
the pgvector extension, HNSW index, and seed demo user.

### Step 3: Run the Data Pipeline

Open and run `notebooks/ingest_and_embed_papers` to:
- Fetch papers from OpenAlex for configured research topics
- Upsert papers, authors, and paper-author relationships to Lakebase
- Chunk abstracts with a sliding window
- Compute 384-dim embeddings with sentence-transformers
- Upsert embeddings into pgvector

### Step 4: Deploy the Flask App

```bash
databricks apps create research-copilot --source-code-path ./
databricks apps deploy research-copilot
databricks apps start research-copilot
```

### Step 5: Deploy the MCP Server (optional, for Agent Bricks)

The `mcp_server/` folder is a self-contained Databricks App. Deploy it
separately:

```bash
databricks apps create research-copilot-mcp --source-code-path ./mcp_server
databricks apps deploy research-copilot-mcp
databricks apps start research-copilot-mcp
```

Then register the MCP server URL in your Agent Bricks agent configuration
using `AGENT_SYSTEM_PROMPT.md` as the system prompt.

## Agent Capabilities

Both the Flask agent chat and the MCP server expose the same tools:

| Tool | Type | Description |
| --- | --- | --- |
| `search_papers` | Read | Find papers by semantic search or OpenAlex |
| `summarize_papers` | Read | LLM summary of papers with citations |
| `compare_papers` | Read | Side-by-side comparison of two papers |
| `generate_study_plan` | Read | Sequenced reading plan from foundational to advanced |
| `add_to_collection` | Write | Add a paper to a user's collection |
| `update_reading_progress` | Write | Mark a paper as reading/completed |
| `recommend_next_paper` | Read | Suggest next paper based on reading history |
| `general_rag` | Read | Answer any question via RAG over the paper database |

The Flask agent chat uses `research_tools.dispatch()` — an LLM-based router
that classifies user intent, picks the right tool, extracts parameters, and
returns a unified `{tool, answer, citations}` response. The MCP server wraps
the same functions as `@mcp.tool` endpoints with the `{status, message, data}`
contract for Agent Bricks.

## Environment Variables

Configured in `app.yaml`:

| Variable | Default | Purpose |
| --- | --- | --- |
| `RESEARCH_SECRET_SCOPE` | `research_copilot` | Databricks secret scope name |
| `LAKEBASE_SECRET_URL` | `lakebase-url` | Secret key for the Lakebase connection URL |
| `OPENALEX_API_KEY_SECRET` | `openalex-api-key` | Secret key for the OpenAlex API key |
| `OPENALEX_EMAIL_SECRET` | `openalex-email` | Secret key for the OpenAlex polite-pool email |
| `LLM_MODEL` | `databricks-meta-llama-3-3-70b-instruct` | Serving endpoint for LLM calls |
| `EMBEDDING_MODEL_NAME` | `sentence-transformers/all-MiniLM-L6-v2` | Embedding model for vector search |
