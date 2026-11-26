from __future__ import annotations

import os
from pathlib import Path
from typing import Any

from flask import (
    Flask,
    flash,
    g,
    jsonify,
    redirect,
    render_template,
    request,
    url_for,
)

import sim_core as sim

BASE_DIR = Path(__file__).resolve().parent
DEFAULT_DB = BASE_DIR / "memory_drill.db"

app = Flask(__name__)
app.config["SECRET_KEY"] = os.environ.get("SECRET_KEY", "memory-drill-dev-key")
app.config["DB_PATH"] = os.environ.get("DB_PATH", str(DEFAULT_DB))


def get_db():
    if "db" not in g:
        db_path = app.config["DB_PATH"]
        parent = Path(db_path).parent
        if str(parent) not in ("", "."):
            parent.mkdir(parents=True, exist_ok=True)
        g.db = sim.connect(db_path)
        sim.init_db(g.db)
    return g.db


@app.teardown_appcontext
def close_db(exc: BaseException | None) -> None:
    db = g.pop("db", None)
    if db is not None:
        db.close()


@app.get("/healthz")
def healthz():
    return {"ok": True}


@app.get("/")
def index():
    db = get_db()
    state = sim.get_state(db)
    return render_template("index.html", state=state)


def request_data() -> dict[str, Any]:
    if request.is_json:
        return request.get_json(silent=True) or {}
    return request.form.to_dict(flat=True)


def fail_index_from_data(data: dict[str, Any]) -> int:
    if "fail_on_writeback_index" in data:
        value = data.get("fail_on_writeback_index")
        if value in (None, "", False):
            return 0
        return sim.as_int(value, "写回故障序号", 0, 99)
    enabled = data.get("fail_enabled")
    if enabled in (True, "on", "1", "true", "yes", "y"):
        return sim.as_int(data.get("fail_index", 1), "第几次写回失败", 1, 99)
    return 0


# ---------------------------------------------------------------------------
# HTML form routes
# ---------------------------------------------------------------------------


@app.post("/ui/reset")
def ui_reset():
    db = get_db()
    sim.reset_sample(db)
    flash("已重置为初始样例。", "ok")
    return redirect(url_for("index") + "#topology")


@app.post("/ui/batches")
def ui_create_batch():
    db = get_db()
    data = request.form.to_dict(flat=True)
    try:
        batch = sim.create_batch(
            db,
            data.get("target_start"),
            data.get("move_quota", 0),
            data.get("writeback_quota", 0),
            fail_index_from_data(data),
            run_now=data.get("action") == "run",
        )
        if batch["status"] == "allocated":
            flash("目标四槽已全部腾出，并立即分配给新虚拟机。", "ok")
        elif data.get("action") == "run":
            flash("本批已执行，但四槽尚未腾齐；可按最新状态选择下一批。", "warn")
        else:
            flash("本批方案已选定，整理处于暂停状态；此时可让原虚拟机读写或注入故障。", "info")
    except sim.BatchRolledBack as exc:
        flash(str(exc), "error")
    except sim.SimError as exc:
        flash(str(exc), "error")
    return redirect(url_for("index") + "#compact")


@app.post("/ui/batch/resume")
def ui_resume_batch():
    db = get_db()
    batch_id = request.form.get("batch_id", type=int) or sim.get_current_batch_id(db)
    if batch_id is None:
        flash("当前没有可继续的批次。", "error")
        return redirect(url_for("index") + "#compact")
    try:
        batch = sim.execute_batch(
            db, batch_id, clear_fault=request.form.get("clear_fault") == "1"
        )
        if batch["status"] == "allocated":
            flash("继续执行成功：四槽腾齐，已立即分配给新虚拟机。", "ok")
        else:
            result = batch["result"]
            flash(
                f"本批完成：搬页 {result['actual_moves']}，"
                f"写回 {result['actual_writebacks']}，"
                f"干净释放 {result['actual_clean_releases']}；尚未腾齐四槽。",
                "warn",
            )
    except sim.BatchRolledBack as exc:
        flash(str(exc), "error")
    except sim.SimError as exc:
        flash(str(exc), "error")
    return redirect(url_for("index") + "#compact")


@app.post("/ui/batch/fault")
def ui_update_fault():
    db = get_db()
    batch_id = request.form.get("batch_id", type=int) or sim.get_current_batch_id(db)
    if batch_id is None:
        flash("请先创建批次，再设置写回故障。", "error")
        return redirect(url_for("index") + "#compact")
    try:
        sim.update_fault(db, batch_id, fail_index_from_data(request.form.to_dict(flat=True)))
        flash("故障注入设置已更新。", "info")
    except sim.SimError as exc:
        flash(str(exc), "error")
    return redirect(url_for("index") + "#compact")


@app.post("/ui/cancel")
def ui_cancel():
    sim.cancel_target(get_db())
    flash("已取消当前整理目标。", "info")
    return redirect(url_for("index") + "#compact")


@app.post("/ui/guest")
def ui_guest():
    db = get_db()
    form = request.form
    page_id = form.get("page_id")
    try:
        if form.get("op") == "read":
            result = sim.guest_read(db, page_id)
        else:
            result = sim.guest_write(
                db,
                page_id,
                form.get("content", ""),
                marker=form.get("marker") == "1",
            )
        flash(result["message"], "ok")
    except sim.SimError as exc:
        flash(str(exc), "error")
    return redirect(url_for("index") + "#vm")


# ---------------------------------------------------------------------------
# JSON API: convenient for scripts and automated checks
# ---------------------------------------------------------------------------


@app.get("/api/state")
def api_state():
    return jsonify(sim.get_state(get_db()))


@app.post("/api/reset")
def api_reset():
    db = get_db()
    sim.reset_sample(db)
    return jsonify(sim.get_state(db))


@app.post("/api/batches")
def api_create_batch():
    db = get_db()
    data = request_data()
    batch = sim.create_batch(
        db,
        data.get("target_start"),
        data.get("move_quota", 0),
        data.get("writeback_quota", 0),
        fail_index_from_data(data),
        run_now=bool(data.get("run_now", False)),
    )
    return jsonify(batch_to_response(db, batch)), 201


@app.post("/api/batches/<int:batch_id>/fault")
def api_fault(batch_id: int):
    db = get_db()
    data = request_data()
    batch = sim.update_fault(
        db, batch_id, data.get("fail_on_writeback_index", data.get("fail_index", 0))
    )
    return jsonify(batch)


@app.post("/api/batches/<int:batch_id>/resume")
def api_resume(batch_id: int):
    db = get_db()
    data = request_data()
    try:
        batch = sim.execute_batch(
            db, batch_id, clear_fault=bool(data.get("clear_fault", False))
        )
        return jsonify(batch_to_response(db, batch))
    except sim.BatchRolledBack as exc:
        response = jsonify(
            {
                "error": str(exc),
                "status": "rolled_back",
                "batch": exc.batch,
                "state": sim.get_state(db),
            }
        )
        response.status_code = 409
        return response


@app.post("/api/cancel")
def api_cancel():
    db = get_db()
    sim.cancel_target(db)
    return jsonify(sim.get_state(db))


@app.post("/api/guest/read")
def api_guest_read():
    data = request_data()
    result = sim.guest_read(get_db(), data.get("page_id"))
    return jsonify(result)


@app.post("/api/guest/write")
def api_guest_write():
    db = get_db()
    data = request_data()
    result = sim.guest_write(
        db,
        data.get("page_id"),
        data.get("content", ""),
        marker=bool(data.get("marker", False)),
    )
    return jsonify(result)


def batch_to_response(db, batch):
    state = sim.get_state(db)
    return {"batch": batch, "state": state}


# With DB_PATH set (as in the container), initialize before Gunicorn forks.
# Request-scoped initialization also handles `flask run` and test clients.
if os.environ.get("DB_PATH"):
    with app.app_context():
        get_db()


@app.errorhandler(sim.SimError)
def handle_sim_error(exc: sim.SimError):
    if request.path.startswith("/api/") or request.is_json:
        return jsonify({"error": str(exc)}), getattr(exc, "status_code", 400)
    flash(str(exc), "error")
    return redirect(url_for("index") + "#compact")


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", "8000")), debug=False)
