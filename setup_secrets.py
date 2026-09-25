"""
One-time setup script: creates the Databricks secret scope and stores the
Lakebase URL and (optionally) the OpenAlex API key.

Run this from a notebook or locally with the Databricks CLI configured.
Never commit secret values to version control.

Usage:
    python setup_secrets.py
"""

from databricks.sdk import WorkspaceClient
from databricks.sdk.service import workspace
import getpass

w = WorkspaceClient()

SCOPE = "research_copilot"

# --- Create scope if it doesn't exist ----------------------------------------
if not any(scope.name == SCOPE for scope in w.secrets.list_scopes()):
    w.secrets.create_scope(scope=SCOPE)
    print(f"Scope `{SCOPE}` successfully created")
else:
    print(f"Scope `{SCOPE}` already exists")

# --- Store Lakebase connection URL -------------------------------------------
w.secrets.put_secret(
    scope=SCOPE,
    key="lakebase-url",
    string_value=getpass.getpass("Paste your Lakebase URL: "),
)
print("Secret `lakebase-url` stored.")

# --- Store OpenAlex API key --------------------------------------------------
openalex_api_key = getpass.getpass(
    "Paste your OpenAlex API key (or press Enter to skip): "
).strip()
if openalex_api_key:
    w.secrets.put_secret(
        scope=SCOPE,
        key="openalex-api-key",
        string_value=openalex_api_key,
    )
    print("Secret `openalex-api-key` stored.")
else:
    print("Skipped OpenAlex API key.")

# --- (Optional) Store email for OpenAlex polite pool -------------------------
openalex_email = input(
    "Enter your email for OpenAlex polite pool (or press Enter to skip): "
).strip()
if openalex_email:
    w.secrets.put_secret(
        scope=SCOPE,
        key="openalex-email",
        string_value=openalex_email,
    )
    print("Secret `openalex-email` stored.")
else:
    print("Skipped OpenAlex email (anonymous access will be used).")

# --- Grant read access to all workspace users --------------------------------
w.secrets.put_acl(
    scope=SCOPE,
    principal="users",
    permission=workspace.AclPermission.READ,
)
print(f"Read ACL granted on scope `{SCOPE}` for all users.")
