"""Root entrypoint for the PostgreSQL-only workstation runtime."""

import datetime
import importlib.util
import json
import logging
from pathlib import Path

from flask import jsonify, request


PACKAGED_APP = (
    Path(__file__).resolve().parents[2]
    / "app"
    / "Koha-AI-Workstation"
    / "server"
    / "remote_app.py"
)
if not PACKAGED_APP.is_file():
    raise RuntimeError(f"PostgreSQL workstation runtime not found: {PACKAGED_APP}")

spec = importlib.util.spec_from_file_location(
    "koha_editor_packaged_remote_app", PACKAGED_APP
)
if spec is None or spec.loader is None:
    raise RuntimeError(f"Unable to load PostgreSQL workstation runtime: {PACKAGED_APP}")
runtime = importlib.util.module_from_spec(spec)
spec.loader.exec_module(runtime)
app = runtime.app


@app.route("/api/queue/poll", methods=["GET"])
def queue_poll():
    """Claim one PostgreSQL task for a distributed worker."""
    task = runtime.qpg.claim_next_task()
    if not task:
        return jsonify({"task_id": None})
    images = json.loads(task["images"]) if task.get("images") else []
    return jsonify(
        {
            "task_id": task["task_id"],
            "images": [f"http://{request.host}/images/{image}" for image in images],
            "barcode": task.get("barcode"),
            "item_count": task.get("item_count"),
            "task_config": task.get("task_config"),
        }
    )


@app.route("/api/queue/result", methods=["POST"])
def queue_result():
    """Persist a distributed worker result in PostgreSQL."""
    data = request.get_json(silent=True) or {}
    task_id = data.get("task_id")
    if not task_id:
        return jsonify({"error": "Missing task_id"}), 400
    try:
        runtime.qpg.update_task_status(
            task_id,
            "completed",
            result_data=json.dumps(data.get("result")),
            completed_at=datetime.datetime.now(),
        )
        return jsonify({"success": True})
    except Exception as exc:
        logging.error("Error saving queue result: %s", exc)
        return jsonify({"error": str(exc)}), 500
