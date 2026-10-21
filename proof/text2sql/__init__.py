"""Text-to-SQL over the plant schema: route to a few tables, generate, run read-only.

    index.py        offline: introspect + enrich + embed every table
    router.py       online: hybrid retrieval of top-k tables + FK join bridges
    guard.py        static check, EXPLAIN allow-list, read-only execution
    pipeline.py     prompt on the routed DDL only, generate, repair, answer
"""
