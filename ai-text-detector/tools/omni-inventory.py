"""Read-only inventory of OmniRoute's connected accounts, for the Round 9 doc section.

Prints no secret material: only key/token LENGTHS and whether the value carries the
`enc` ciphertext prefix. Run from the repo root: python tools/omni-inventory.py
"""
import json
import os
import sqlite3

db = os.path.expanduser("~/.omniroute/storage.sqlite").replace("\\", "/")
c = sqlite3.connect("file:" + db + "?mode=ro", uri=True)
c.row_factory = sqlite3.Row

# Identity fields (name/email) are deliberately reduced to a boolean: this script's output
# is meant to be pasteable into a doc, and the accounts belong to the user.
rows = list(c.execute(
    "select provider, auth_type, is_active, test_status, backoff_level,"
    " token_expires_at, last_error_type,"
    " (name is not null and name <> '') has_name,"
    " (email is not null and email <> '') has_email,"
    " length(coalesce(api_key,'')) ak_len, length(coalesce(access_token,'')) tok_len,"
    " (api_key like 'enc%') ak_enc, (access_token like 'enc%') tok_enc"
    " from provider_connections order by provider"))
print("provider_connections rows =", len(rows))
for r in rows:
    print("  %-14s auth=%-10s active=%s test=%-9s backoff=%-4s exp=%-12s err=%-14s "
          "name=%s email=%s api_key_len=%s(enc=%s) token_len=%s(enc=%s)" % (
              r["provider"], str(r["auth_type"]), r["is_active"], str(r["test_status"]),
              str(r["backoff_level"]), str(r["token_expires_at"]), str(r["last_error_type"]),
              r["has_name"], r["has_email"],
              r["ak_len"], r["ak_enc"], r["tok_len"], r["tok_enc"]))

kv = list(c.execute(
    "select key, value from key_value where namespace='syncedAvailableModels'"))
tot = 0
print("syncedAvailableModels entries =", len(kv))
for k in kv:
    try:
        n = len(json.loads(k["value"] or "[]"))
    except Exception:
        n = -1
    tot += max(n, 0)
    print("  %-18s %s" % (k["key"], n))
print("cached model ids total =", tot)
