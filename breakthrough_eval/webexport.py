"""Export run artifacts as a static-site data bundle (docs/data.js).

The visualisation site (``docs/``) is a dependency-free static page meant for
GitHub Pages. To work both from ``file://`` and Pages without CORS issues, the
data is shipped as a JS assignment (``window.BE_DATA = {...}``) rather than a
fetched JSON file.

Bundle shape::

    {
      "generated_at": iso8601,
      "tasks":       {task_id: TaskSpec-dump},
      "leaderboard": [LeaderboardRow-dump (incl. points)],
      "results":     [{"prover": ProverRunResult-dump, "eval": EvalResult-dump}]
    }

Everything is derived from ``tasks/`` + ``results/`` on disk — rerun
``python -m breakthrough_eval export-web`` after a run to refresh the site.
"""

from __future__ import annotations

import json
from dataclasses import asdict
from datetime import datetime, timezone
from pathlib import Path

from .leaderboard import Leaderboard
from .storage import ResultStore
from .taskspec import load_all_tasks


def build_site_data(results_dir: str | Path, tasks_dir: str | Path) -> dict:
    store = ResultStore(results_dir)
    provers = store.load_prover_all()
    evals = {e.job_id: e for e in store.load_eval_all()}
    tasks = load_all_tasks(tasks_dir)

    max_levels = {tid: t.max_hint_level for tid, t in tasks.items()}
    board = Leaderboard.build(
        provers,
        list(evals.values()),
        max_hint_level=max(max_levels.values()) if max_levels else None,
    )

    results = [
        {
            "prover": pr.model_dump(mode="json"),
            "eval": evals[pr.job_id].model_dump(mode="json") if pr.job_id in evals else None,
        }
        for pr in sorted(provers, key=lambda p: (p.task_id, p.model, p.hint_level, p.trial))
    ]
    return {
        "generated_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "tasks": {tid: t.model_dump(mode="json") for tid, t in sorted(tasks.items())},
        "leaderboard": [asdict(row) for row in board.rows],
        "results": results,
    }


def write_data_js(data: dict, out_path: str | Path) -> Path:
    out = Path(out_path)
    out.parent.mkdir(parents=True, exist_ok=True)
    payload = json.dumps(data, ensure_ascii=False, indent=1)
    # Keep the blob safe to inline inside a <script> tag.
    payload = payload.replace("</", "<\\/")
    out.write_text(f"window.BE_DATA = {payload};\n", encoding="utf-8")
    return out
