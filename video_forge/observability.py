"""Pipeline observability — pipeline.log.json per run + optional Mermaid Gantt.

Each `Pipeline` instance accumulates stages. `stage(name)` is a context
manager that records start/end timestamps, captures errors, and lets
the caller attach metadata (cache_hit, retry_count, fallback_chain_walked,
output_size_bytes, sub_agent_id, etc).
"""

from __future__ import annotations

import json
import time
import traceback
from contextlib import contextmanager
from dataclasses import dataclass, field, asdict
from pathlib import Path
from typing import Any


@dataclass
class StageRecord:
    stage_id: str
    start_ts: float
    end_ts: float | None = None
    duration_s: float | None = None
    cache_hit: bool = False
    sub_agent_id: str | None = None
    retry_count: int = 0
    output_size_bytes: int | None = None
    fallback_chain_walked: list[dict] = field(default_factory=list)
    error: str | None = None
    extra: dict = field(default_factory=dict)


class Pipeline:
    def __init__(self, project_dir: Path, *, run_id: str | None = None):
        self.project_dir = Path(project_dir)
        self.run_id = run_id or time.strftime("%Y%m%dT%H%M%S")
        self.start_ts = time.time()
        self.stages: list[StageRecord] = []
        self.log_path = self.project_dir / "edit" / "pipeline.log.json"
        self.log_path.parent.mkdir(parents=True, exist_ok=True)

    @contextmanager
    def stage(self, name: str, *, sub_agent_id: str | None = None):
        rec = StageRecord(stage_id=name, start_ts=time.time(), sub_agent_id=sub_agent_id)
        self.stages.append(rec)
        try:
            yield rec
        except Exception as e:
            rec.error = f"{type(e).__name__}: {e}"
            rec.extra["traceback"] = traceback.format_exc(limit=5)
            self.flush()
            raise
        finally:
            rec.end_ts = time.time()
            rec.duration_s = round(rec.end_ts - rec.start_ts, 3)
            self.flush()

    def flush(self) -> None:
        payload = {
            "run_id": self.run_id,
            "project_dir": str(self.project_dir),
            "start_ts": self.start_ts,
            "stages": [asdict(s) for s in self.stages],
        }
        self.log_path.write_text(json.dumps(payload, indent=2), encoding="utf-8")

    def render_gantt(self) -> str:
        """Emit a Mermaid Gantt block summarising stage timings."""
        lines = ["```mermaid", "gantt", "  dateFormat  X", "  axisFormat  %S s"]
        title = f"  title Pipeline run {self.run_id}"
        lines.insert(2, title)
        for s in self.stages:
            start_off = int(s.start_ts - self.start_ts)
            dur = int((s.end_ts or s.start_ts) - s.start_ts) or 1
            status = "crit," if s.error else ""
            lines.append(f"  {s.stage_id} : {status}{start_off}, {dur}s")
        lines.append("```")
        return "\n".join(lines)
