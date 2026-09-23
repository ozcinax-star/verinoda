"""graphify - extract · build · cluster · analyze · report."""


def __getattr__(name):
    # Lazy imports so `graphify install` works before heavy deps are in place.
    _map = {
        "extract": ("verinoda.project_index.extract", "extract"),
        "collect_files": ("verinoda.project_index.extract", "collect_files"),
        "build_from_json": ("verinoda.project_index.build", "build_from_json"),
        "cluster": ("verinoda.project_index.cluster", "cluster"),
        "score_all": ("verinoda.project_index.cluster", "score_all"),
        "cohesion_score": ("verinoda.project_index.cluster", "cohesion_score"),
        "god_nodes": ("verinoda.project_index.analyze", "god_nodes"),
        "surprising_connections": ("verinoda.project_index.analyze", "surprising_connections"),
        "suggest_questions": ("verinoda.project_index.analyze", "suggest_questions"),
        "generate": ("verinoda.project_index.report", "generate"),
        "to_json": ("verinoda.project_index.export", "to_json"),
        "to_html": ("verinoda.project_index.export", "to_html"),
        "to_svg": ("verinoda.project_index.export", "to_svg"),
        "to_canvas": ("verinoda.project_index.export", "to_canvas"),
        "to_wiki": ("verinoda.project_index.wiki", "to_wiki"),
        "reflect": ("verinoda.project_index.reflect", "reflect"),
        "save_query_result": ("verinoda.project_index.ingest", "save_query_result"),
    }
    if name in _map:
        import importlib
        mod_name, attr = _map[name]
        mod = importlib.import_module(mod_name)
        return getattr(mod, attr)
    raise AttributeError(f"module 'graphify' has no attribute {name!r}")
