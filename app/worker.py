"""One process per job: GPU model memory is reclaimed when this process exits."""
from __future__ import annotations

import contextlib
import json
import math
import os
from pathlib import Path
import sys
import traceback


def main():
    if len(sys.argv) != 2:
        raise SystemExit("Usage: python -m app.worker request.json")
    request = json.loads(Path(sys.argv[1]).read_text(encoding="utf-8"))
    data_dir = Path(request["data_dir"]).resolve()
    os.environ["HF_HOME"] = str(data_dir / "models")
    os.environ["HF_HUB_CACHE"] = str(data_dir / "models" / "hub")
    os.environ["TOKENIZERS_PARALLELISM"] = "false"
    os.environ["HF_HUB_DISABLE_TELEMETRY"] = "1"
    if request["kind"] not in {"download", "download_model", "model_download"}:
        os.environ["HF_HUB_OFFLINE"] = "1"
        os.environ["TRANSFORMERS_OFFLINE"] = "1"
    output_channel = sys.stdout

    def progress(value, message):
        number = float(value)
        if math.isfinite(number):
            output_channel.write(json.dumps({"progress": max(0, min(1, number)), "message": str(message)}, ensure_ascii=False) + "\n")
            output_channel.flush()

    try:
        # Third-party libraries may print informational logs; reserve stdout for
        # machine-readable progress and send their output to the job log.
        with contextlib.redirect_stdout(sys.stderr):
            if request["kind"] == "download_video":
                from .acquire import run_download
                result = run_download(request, progress)
            else:
                from .pipeline import run_job
                result = run_job(request["kind"], request.get("project"), request.get("options", {}), data_dir, progress)
        output = Path(request["output_path"])
        output.parent.mkdir(parents=True, exist_ok=True)
        temp = output.with_suffix(output.suffix + ".tmp")
        temp.write_text(json.dumps(result, ensure_ascii=False, allow_nan=False), encoding="utf-8")
        temp.replace(output)
        progress(1.0, "任务完成")
    except Exception as exc:
        # No prompt, subtitle text, or API credentials are printed on failure.
        print(f"{type(exc).__name__}: {exc}", file=sys.stderr, flush=True)
        raise SystemExit(1)


if __name__ == "__main__":
    main()
