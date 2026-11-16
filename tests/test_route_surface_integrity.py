"""Route-surface integrity checks.

These are deliberately small structural guards. A duplicate method/path is dangerous in FastAPI
because the first registered handler wins, so a later weaker-looking handler can sit unnoticed until a
future refactor changes registration order. The guest-invite audit found one; this locks the whole app
against the same class of regression.
"""
from collections import defaultdict


def test_no_duplicate_method_path_routes():
    from mongoose.core_app import app

    seen = defaultdict(list)
    for route in app.routes:
        path = getattr(route, "path", None)
        methods = getattr(route, "methods", None) or set()
        if not path:
            continue
        for method in methods:
            if method in {"HEAD", "OPTIONS"}:
                continue
            seen[(method, path)].append(getattr(route, "name", repr(route)))

    duplicates = {k: v for k, v in seen.items() if len(v) > 1}
    assert duplicates == {}
