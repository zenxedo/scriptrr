import asyncio
import html
import json
import os
import signal
import subprocess
import shutil
import time
import sqlite3
import shlex
import threading
from datetime import datetime
from json import JSONDecodeError
from pathlib import Path
from urllib.parse import parse_qs, quote

from fastapi import FastAPI, HTTPException, Request, UploadFile, File, Form
from fastapi.staticfiles import StaticFiles
from fastapi.responses import HTMLResponse, JSONResponse, PlainTextResponse, RedirectResponse, Response, StreamingResponse
from jinja2 import Environment, select_autoescape, FileSystemLoader

try:
    from apscheduler.schedulers.background import BackgroundScheduler
    from apscheduler.triggers.cron import CronTrigger
except ImportError:
    BackgroundScheduler = None
    CronTrigger = None


app = FastAPI(title="Scriptrr v3")
_running_processes = {}
scheduler = BackgroundScheduler() if BackgroundScheduler else None

BASE_DIR = Path(__file__).resolve().parent
SCRIPT_DIR = BASE_DIR / "scripts"
LOG_DIR = BASE_DIR / "logs"
METADATA_FILE = BASE_DIR / "metadata.json"
SCHEDULE_METADATA_FILE = BASE_DIR / "schedule_metadata.json"
TEMPLATE_FILE = BASE_DIR / "dashboard.html"
DB_FILE = BASE_DIR / "scriptrr.db"
STATIC_DIR = BASE_DIR / "static"

STATIC_DIR.mkdir(exist_ok=True)
app.mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="static")


SCRIPT_DIR.mkdir(exist_ok=True)
LOG_DIR.mkdir(exist_ok=True)

jinja = Environment(
    loader=FileSystemLoader(BASE_DIR),
    autoescape=select_autoescape(["html", "xml"])
)

def render_dashboard(combined: list[dict], partial_only: bool = False, non_script_files: list[dict] | None = None, internal_logs: list[dict] | None = None) -> str:
    if partial_only:
        # Load just the standalone rows template block directly for HTMX
        return jinja.get_template("partials/script_rows.html").render(combined=combined)
    
    # Load the main dashboard template shell
    return jinja.get_template("dashboard.html").render(combined=combined, non_script_files=non_script_files or [], internal_logs=internal_logs or [])


def load_json(path: Path) -> dict:
    if not path.exists():
        return {}
    try:
        with path.open() as f:
            return json.load(f)
    except JSONDecodeError:
        return {}


def save_json(path: Path, data: dict) -> None:
    with path.open("w") as f:
        json.dump(data, f, indent=2)


def load_metadata() -> dict:
    return load_json(METADATA_FILE)


def load_schedule_metadata() -> dict:
    return load_json(SCHEDULE_METADATA_FILE)


def get_relative_time(timestamp: float) -> str:
    diff = time.time() - float(timestamp)
    future = diff < 0
    diff = abs(diff)

    if diff < 60:
        value, unit = int(diff), "seconds"
    elif diff < 3600:
        value, unit = int(diff // 60), "minutes"
    elif diff < 86400:
        value, unit = int(diff // 3600), "hours"
    else:
        value, unit = int(diff // 86400), "days"

    return f"in {value} {unit}" if future else f"{value} {unit} ago"


def safe_script_name(script: str) -> str:
    if (not isinstance(script, str) or script != os.path.basename(script)
            or "\\" in script or not script.endswith((".py", ".sh"))):
        raise HTTPException(status_code=400, detail="Invalid script name")
    return script


async def form_value(request: Request, key: str, default: str = "") -> str:
    body = await request.body()
    if not body:
        return default
    parsed = parse_qs(body.decode("utf-8"), keep_blank_values=True)
    return parsed.get(key, [default])[0]






def init_db():
    with sqlite3.connect(DB_FILE) as conn:
        conn.execute("""
            CREATE TABLE IF NOT EXISTS script_status (
                script_name TEXT PRIMARY KEY,
                last_run REAL,
                last_status TEXT,
                pid INTEGER,
                exit_code INTEGER
            )
        """)
        conn.execute("""
            CREATE TABLE IF NOT EXISTS script_metadata (
                script_name TEXT PRIMARY KEY,
                description TEXT DEFAULT '',
                tags TEXT DEFAULT '',
                default_args TEXT DEFAULT '',
                schedule TEXT DEFAULT ''
            )
        """)
        metadata_columns = {row[1] for row in conn.execute("PRAGMA table_info(script_metadata)")}
        if "starred" not in metadata_columns:
            conn.execute("ALTER TABLE script_metadata ADD COLUMN starred INTEGER DEFAULT 0")


@app.on_event("startup")
async def startup_event():
    init_db()
    with sqlite3.connect(DB_FILE) as conn:
        conn.execute("UPDATE script_status SET last_status = 'stopped', pid = NULL WHERE last_status = 'running'")
    if scheduler:
        scheduler.start()
        refresh_scheduler_jobs()


@app.on_event("shutdown")
async def shutdown_event():
    if scheduler and scheduler.running:
        scheduler.shutdown(wait=False)



def update_script_status(script_name: str, status_data: dict) -> None:
    with sqlite3.connect(DB_FILE) as conn:
        conn.execute("""
            INSERT INTO script_status (script_name, last_run, last_status, pid, exit_code)
            VALUES (?, ?, ?, ?, ?)
            ON CONFLICT(script_name) DO UPDATE SET
                last_run=excluded.last_run,
                last_status=excluded.last_status,
                pid=excluded.pid,
                exit_code=excluded.exit_code
        """, (script_name, status_data.get('last_run'), status_data.get('last_status'), 
              status_data.get('pid'), status_data.get('exit_code')))

def get_all_script_metadata() -> dict:
    with sqlite3.connect(DB_FILE) as conn:
        conn.row_factory = sqlite3.Row
        rows = conn.execute("SELECT * FROM script_metadata").fetchall()
        return {row["script_name"]: dict(row) for row in rows}

def get_script_metadata(script_name: str) -> dict:
    with sqlite3.connect(DB_FILE) as conn:
        conn.row_factory = sqlite3.Row
        row = conn.execute("SELECT * FROM script_metadata WHERE script_name = ?", (script_name,)).fetchone()
        return dict(row) if row else {}

def set_script_metadata_field(script_name: str, field: str, value: str):
    allowed_fields = {"description", "tags", "default_args", "schedule", "starred"}
    if field not in allowed_fields:
        return
    with sqlite3.connect(DB_FILE) as conn:
        conn.execute(f"""
            INSERT INTO script_metadata (script_name, {field})
            VALUES (?, ?)
            ON CONFLICT(script_name) DO UPDATE SET {field}=excluded.{field}
        """, (script_name, value))

def get_script_status(script_name: str) -> dict:
    with sqlite3.connect(DB_FILE) as conn:
        conn.row_factory = sqlite3.Row
        row = conn.execute("SELECT * FROM script_status WHERE script_name = ?", (script_name,)).fetchone()
        if row:
            return dict(row)
        return {}


def get_all_script_statuses() -> dict:
    with sqlite3.connect(DB_FILE) as conn:
        conn.row_factory = sqlite3.Row
        rows = conn.execute("SELECT * FROM script_status").fetchall()
        return {row["script_name"]: dict(row) for row in rows}


def launch_script(script: str, args: str = ""):
    """Start a script and attach the same status/log watcher used by the UI."""
    script = safe_script_name(script)
    script_path = SCRIPT_DIR / script
    if not script_path.is_file():
        raise HTTPException(status_code=404, detail="Script not found")

    existing = _running_processes.get(script)
    if existing is not None:
        if existing.poll() is None:
            raise HTTPException(status_code=409, detail="Script is already running")
        _running_processes.pop(script, None)

    log_path = LOG_DIR / f"{script}.log"
    if log_path.exists():
        try:
            archive_dir = LOG_DIR / "archive"
            archive_dir.mkdir(exist_ok=True)
            shutil.move(str(log_path), str(archive_dir / f"{script}_{int(time.time())}.log"))
        except OSError:
            pass

    logfile = log_path.open("w")
    cmd = ["python3", "-u", str(script_path)] if script.endswith(".py") else ["bash", str(script_path)]
    if args:
        try:
            cmd.extend(shlex.split(args))
        except ValueError as exc:
            logfile.close()
            raise HTTPException(status_code=400, detail=f"Invalid arguments: {exc}")

    kwargs = {"start_new_session": True} if os.name == "posix" else {}
    try:
        proc = subprocess.Popen(
            cmd,
            stdout=logfile,
            stderr=subprocess.STDOUT,
            text=True,
            bufsize=1,
            **kwargs,
        )
    except OSError:
        logfile.close()
        raise

    _running_processes[script] = proc
    update_script_status(script, {
        "last_status": "running",
        "pid": proc.pid,
        "last_run": time.time(),
        "exit_code": None,
    })

    def watcher_task():
        proc.wait()
        logfile.close()
        current_status = get_script_status(script)
        if current_status.get("last_status") == "stopped":
            _running_processes.pop(script, None)
            return
        update_script_status(script, {
            "last_status": "success" if proc.returncode == 0 else "fail",
            "pid": None,
            "last_run": time.time(),
            "exit_code": proc.returncode,
        })
        _running_processes.pop(script, None)

    threading.Thread(target=watcher_task, daemon=True, name=f"scriptrr-{script}").start()
    return proc


def run_scheduled_script(script: str) -> None:
    """APScheduler callback; skips overlapping executions and records failures."""
    try:
        metadata = get_script_metadata(script)
        launch_script(script, metadata.get("default_args", "").strip())
    except HTTPException as exc:
        if exc.status_code != 409:
            print(f"Scheduled run failed for {script}: {exc.detail}")
    except Exception as exc:
        print(f"Scheduled run failed for {script}: {exc}")


def refresh_scheduler_jobs() -> None:
    """Rebuild scheduled jobs from the database after startup or schedule edits."""
    if not scheduler or not CronTrigger or not scheduler.running:
        return

    scheduler.remove_all_jobs()
    for script, metadata in get_all_script_metadata().items():
        schedule = (metadata.get("schedule") or "").strip()
        if not schedule or not (SCRIPT_DIR / script).is_file():
            continue
        try:
            trigger = CronTrigger.from_crontab(schedule)
            scheduler.add_job(
                run_scheduled_script,
                trigger=trigger,
                args=[script],
                id=f"script:{script}",
                replace_existing=True,
                max_instances=1,
                coalesce=True,
                misfire_grace_time=300,
            )
        except Exception as exc:
            print(f"Failed to schedule {script} with schedule '{schedule}': {exc}")


def resolve_status(entry: dict) -> str:
    last_status = entry.get("last_status")
    if last_status == "stopped":
        return "stopped"
    if last_status == "running" and entry.get("exit_code") is None:
        return "running"
    if last_status in ("success", "pass"):
        return "pass"
    if last_status == "fail":
        return "fail"
    return "unknown"


def status_badge(status: str) -> str:
    if status == "running":
        return '<span class="inline-flex items-center gap-1 px-1.5 py-0.5 rounded-full text-[11px] font-medium bg-blue-500/10 text-blue-400 border border-blue-500/20"><span class="h-1.5 w-1.5 rounded-full bg-blue-400 animate-pulse"></span> RUNNING</span>'
    if status == "stopped":
        return '<span class="inline-flex items-center gap-1 px-1.5 py-0.5 rounded-full text-[11px] font-medium bg-amber-500/10 text-amber-400 border border-amber-500/20"><span class="h-1.5 w-1.5 rounded-full bg-amber-400"></span> STOPPED</span>'
    if status in ("success", "pass"):
        return '<span class="inline-flex items-center gap-1 px-1.5 py-0.5 rounded-full text-[11px] font-medium bg-emerald-500/10 text-emerald-400 border border-emerald-500/20"><span class="h-1.5 w-1.5 rounded-full bg-emerald-400"></span> PASS</span>'
    if status == "fail":
        return '<span class="inline-flex items-center gap-1 px-1.5 py-0.5 rounded-full text-[11px] font-medium bg-rose-500/10 text-rose-400 border border-rose-500/20"><span class="h-1.5 w-1.5 rounded-full bg-rose-400"></span> FAIL</span>'
    return '<span class="inline-flex items-center gap-1 px-1.5 py-0.5 rounded-full text-[11px] font-medium bg-gray-500/10 text-gray-400 border border-gray-500/20"><span class="h-1.5 w-1.5 rounded-full bg-gray-400"></span> IDLE</span>'


def next_run_info(schedule: str) -> tuple[str, float]:
    if not schedule:
        return "", float('inf')
    if CronTrigger is None:
        return "Schedule support unavailable", float('inf')
    try:
        trigger = CronTrigger.from_crontab(schedule)
        now = datetime.now(trigger.timezone) if getattr(trigger, "timezone", None) else datetime.now()
        next_fire = trigger.get_next_fire_time(None, now)
        if next_fire:
            ts = next_fire.astimezone().timestamp()
            return get_relative_time(ts), ts
        return "", float('inf')
    except Exception:
        return "Invalid schedule", float('inf')


def get_combined_data(search_query: str = "", sort_by: str = "name", order: str = "asc") -> list[dict]:
    scripts = sorted([p.name for p in SCRIPT_DIR.iterdir() if p.is_file() and p.suffix in (".py", ".sh")])
    statuses = get_all_script_statuses()
    metadata_all = get_all_script_metadata()

    if search_query:
        query = search_query.strip().casefold()
        scripts = [
            script for script in scripts
            if query in " ".join((
                script,
                str(metadata_all.get(script, {}).get("description", "")),
                str(metadata_all.get(script, {}).get("tags", "")),
            )).casefold()
        ]

    combined = []
    for script in scripts:
        entry = statuses.get(script, {})
        last_run = entry.get("last_run")
        
        meta = metadata_all.get(script, {})
        schedule = meta.get("schedule", "")
        tags_raw = meta.get("tags", "")
        tags = [t.strip() for t in tags_raw.split(",")] if tags_raw else []
        script_path = SCRIPT_DIR / script
        script_content = script_path.read_text(encoding="utf-8", errors="replace") if script_path.exists() else ""

        next_run_str, next_run_ts = next_run_info(schedule)

        combined.append({
            "name": script,
            "url": quote(script, safe=""),
            "description": meta.get("description", ""),
            "tags": tags,
            "content": script_content,
            "type": "python" if script.endswith(".py") else "bash",
            "last_run": get_relative_time(last_run) if last_run else "Never",
            "last_run_ts": last_run or 0,
            "status": resolve_status(entry),
            "next_run": next_run_str,
            "next_run_ts": next_run_ts,
            "schedule_raw": schedule, # ADD THIS LINE
            "default_args": meta.get("default_args", ""),
            "starred": bool(meta.get("starred", 0)),
        })

    reverse = order == "desc"
    if sort_by == "last_run":
        combined.sort(key=lambda item: item["last_run_ts"], reverse=reverse)
    elif sort_by == "next_run":
        combined.sort(key=lambda item: item["next_run_ts"], reverse=reverse)
    elif sort_by == "status":
        status_order = {"running": 0, "fail": 1, "stopped": 2, "pass": 3, "unknown": 4}
        combined.sort(key=lambda item: status_order.get(item["status"], 99), reverse=reverse)
    else:
        combined.sort(key=lambda item: item["name"].lower(), reverse=reverse)
        
    return combined





@app.get("/", include_in_schema=False)
async def root():
    return RedirectResponse("/v2/dashboard")


@app.get("/v2/dashboard", response_class=HTMLResponse)
async def dashboard():
    # Collect non-script files in /scripts (exclude .py/.sh)
    non_script_files = []
    try:
        for p in sorted(SCRIPT_DIR.iterdir(), key=lambda x: x.name):
            if p.is_file() and p.suffix not in (".py", ".sh"):
                non_script_files.append({
                    "name": p.name,
                    "url": quote(p.name, safe=""),
                    "size": p.stat().st_size,
                    "mtime": p.stat().st_mtime,
                })
    except Exception:
        non_script_files = []

    # Collect recent internal logs from /logs (take .log and .txt files)
    internal_logs = []
    try:
        logs = [p for p in LOG_DIR.iterdir() if p.is_file() and p.suffix in (".log", ".txt")]
        logs.sort(key=lambda x: x.stat().st_mtime, reverse=True)
        for p in logs[:10]:
            internal_logs.append({
                "name": p.name,
                "url": quote(p.name, safe=""),
                "size": p.stat().st_size,
                "mtime": p.stat().st_mtime,
            })
    except Exception:
        internal_logs = []

    return HTMLResponse(render_dashboard(get_combined_data(), non_script_files=non_script_files, internal_logs=internal_logs))


@app.get("/v2/api/scripts", response_class=HTMLResponse)
async def api_get_rows(sort: str = "name", order: str = "asc"):
    return HTMLResponse(render_dashboard(get_combined_data(sort_by=sort, order=order), partial_only=True))


@app.get("/v2/api/statuses")
async def api_statuses():
    statuses = get_all_script_statuses()
    response_data = {}
    for script_path in SCRIPT_DIR.iterdir():
        if script_path.is_file() and script_path.suffix in (".py", ".sh"):
            response_data[script_path.name] = status_badge(resolve_status(statuses.get(script_path.name, {})))
    return JSONResponse(response_data)

@app.post("/v2/api/script/create", response_class=HTMLResponse)
async def create_script(
    filename: str = Form(...),
    description: str = Form(""),
    content: str = Form(...)
):
    filename = safe_script_name(filename)
    script_path = SCRIPT_DIR / filename
    script_path.write_text(content.replace('\r\n', '\n'), encoding="utf-8")
    
    if description:
        set_script_metadata_field(filename, "description", description)
        
    return HTMLResponse(render_dashboard(get_combined_data(), partial_only=True))


@app.post("/v2/api/script/upload", response_class=HTMLResponse)
async def upload_script(file: UploadFile = File(...)):
    if not file.filename:
        raise HTTPException(status_code=400, detail="Uploaded file has no filename")
    filename = safe_script_name(file.filename)
    script_path = SCRIPT_DIR / filename
    content = await file.read()
    script_path.write_bytes(content)
    
    return HTMLResponse(render_dashboard(get_combined_data(), partial_only=True))


@app.post("/v2/api/scripts/search", response_class=HTMLResponse)
async def api_search_rows(request: Request):
    query = (await form_value(request, "search")).strip()
    return HTMLResponse(render_dashboard(get_combined_data(search_query=query), partial_only=True))

@app.post("/v2/api/script/{script}/save-schedule", response_class=HTMLResponse)
async def save_schedule(script: str, request: Request):
    script = safe_script_name(script)
    new_schedule = (await form_value(request, "schedule")).strip()

    if new_schedule:
        if not CronTrigger:
            raise HTTPException(status_code=503, detail="Scheduling support is unavailable")
        try:
            CronTrigger.from_crontab(new_schedule)
        except Exception as exc:
            raise HTTPException(status_code=400, detail=f"Invalid cron schedule: {exc}")

    set_script_metadata_field(script, "schedule", new_schedule)
    refresh_scheduler_jobs()
    
    # Render the entire table partial so the "Next Run" column updates immediately
    return HTMLResponse(render_dashboard(get_combined_data(), partial_only=True))


@app.post("/v2/api/run/{script}")
async def run_script_v2(script: str, request: Request):
    script = safe_script_name(script)
    args = (await form_value(request, "args")).strip()
    if not args:
        args = get_script_metadata(script).get("default_args", "").strip()
    launch_script(script, args)
    return Response(status_code=204)


@app.post("/v2/api/log/{script}/clear")
async def clear_log(script: str):
    script = safe_script_name(script)
    log_path = LOG_DIR / f"{script}.log"
    if log_path.exists():
        log_path.unlink()
    return JSONResponse({"status": "cleared"})


@app.post("/v2/api/stop/{script}")
async def stop_script(script: str):
    script = safe_script_name(script)
    entry = get_script_status(script)
    pid = entry.get("pid")
    
    proc = _running_processes.get(script)
    
    # Primary method: Kill via direct memory reference
    if proc:
        try:
            if os.name == "posix" and hasattr(os, "killpg"):
                os.killpg(os.getpgid(proc.pid), signal.SIGTERM)
            else:
                proc.terminate()
            try:
                proc.wait(timeout=1.0)
            except subprocess.TimeoutExpired:
                if os.name == "posix" and hasattr(os, "killpg"):
                    os.killpg(os.getpgid(proc.pid), signal.SIGKILL)
                else:
                    proc.kill()
        except Exception as e:
            print(f"Error stopping script from memory: {e}")
        finally:
            _running_processes.pop(script, None)
            
    # Fallback method: OS signals if process isn't in memory
    elif pid:
        try:
            import signal
            if hasattr(os, "killpg") and hasattr(os, "getpgid"):
                pgid = os.getpgid(pid)
                os.killpg(pgid, signal.SIGTERM)
                for _ in range(5):
                    await asyncio.sleep(0.2)
                    try:
                        os.kill(pid, 0)
                    except OSError:
                        break
                else:
                    os.killpg(pgid, signal.SIGKILL)
            else:
                os.kill(pid, signal.SIGTERM)
        except (ProcessLookupError, OSError):
            pass
        except Exception as e:
            print(f"Error stopping script via OS signal: {e}")

    # Immediately lock in the stopped state
    entry.update({"last_status": "stopped", "pid": None, "last_run": time.time()})
    update_script_status(script, entry)
    return JSONResponse({"success": True})


@app.post("/v2/api/script/{script}/delete", response_class=HTMLResponse)
async def delete_script(script: str):
    script = safe_script_name(script)
    script_path = SCRIPT_DIR / script

    entry = get_script_status(script)
    if script in _running_processes or entry.get("last_status") == "running":
        await stop_script(script)
    
    if script_path.exists():
        script_path.unlink()
        
    with sqlite3.connect(DB_FILE) as conn:
        conn.execute("DELETE FROM script_status WHERE script_name = ?", (script,))
        conn.execute("DELETE FROM script_metadata WHERE script_name = ?", (script,))
    refresh_scheduler_jobs()
        
    log_path = LOG_DIR / f"{script}.log"
    if log_path.exists():
        log_path.unlink()

    # Return the same valid row-only partial used by search and other refreshes.
    return HTMLResponse(render_dashboard(get_combined_data(), partial_only=True))


@app.get("/v2/stream/{script}")
async def stream_script_status_v2(script: str):
    script = safe_script_name(script)

    async def generate():
        last_status = None
        while True:
            current_status = resolve_status(get_script_status(script))
            if current_status != last_status:
                yield f"event: status_update\ndata: {status_badge(current_status)}\n\n"
                last_status = current_status
            await asyncio.sleep(1)

    return StreamingResponse(generate(), media_type="text/event-stream")


@app.get("/v2/log-stream/{script}")
async def stream_script_log(script: str, fresh: str = "0"):
    script = safe_script_name(script)
    script_path = SCRIPT_DIR / script
    if not script_path.exists():
        raise HTTPException(status_code=404, detail="Script not found")

    log_path = LOG_DIR / f"{script}.log"

    async def generate():
        if fresh != "1" and log_path.exists():
            try:
                lines = await asyncio.to_thread(log_path.read_text, errors="replace")
                for line in lines.splitlines()[-1000:]:
                    yield f"data: {line}\n\n"
            except Exception as exc:
                yield f"data: Error reading past logs: {exc}\n\n"

        while not log_path.exists():
            yield "data: Waiting for log file...\n\n"
            await asyncio.sleep(1)

        try:
            with log_path.open("r", errors="replace") as log_file:
                log_file.seek(0, os.SEEK_END)
                while True:
                    line = await asyncio.to_thread(log_file.readline)
                    if line:
                        yield f"data: {line.rstrip()}\n\n"
                    else:
                        log_file.seek(0, os.SEEK_CUR)
                        await asyncio.sleep(0.2)
        except Exception as exc:
            yield f"data: Error streaming logs: {exc}\n\n"

    return StreamingResponse(generate(), media_type="text/event-stream")


@app.post("/v2/api/script/{script}/save-desc", response_class=HTMLResponse)
async def inline_save_description(script: str, request: Request, index: str = "1"):
    script = safe_script_name(script)
    new_desc = (await form_value(request, "description")).strip()
    
    set_script_metadata_field(script, "description", new_desc)

    escaped_script = html.escape(quote(script, safe=""), quote=True)
    escaped_index = html.escape(index, quote=True)
    escaped_desc = html.escape(new_desc or "No description")
    return HTMLResponse(f"""
    <div class="flex items-center justify-between group/desc cursor-pointer hover:bg-[#2b2b33]/40 p-1 rounded transition" hx-get="/v2/api/script/{escaped_script}/edit-form?index={escaped_index}" hx-target="#desc-container-{escaped_index}">
      <p class="truncate text-gray-300 pr-4">{escaped_desc}</p>
    </div>
    """)

@app.post("/v2/api/script/{script}/save-args", response_class=HTMLResponse)
async def save_arguments(script: str, request: Request):
    script = safe_script_name(script)
    new_args = (await form_value(request, "args")).strip()
    
    set_script_metadata_field(script, "default_args", new_args)
    
    return HTMLResponse(f'<script>alert("Saved default arguments for {html.escape(script)} successfully!")</script>')

@app.post("/v2/api/script/{script}/toggle-star", response_class=PlainTextResponse)
async def toggle_star(script: str):
    script = safe_script_name(script)
    current = bool(get_script_metadata(script).get("starred", 0))
    set_script_metadata_field(script, "starred", "0" if current else "1")
    return PlainTextResponse("starred" if not current else "unstarred")

@app.get("/v2/api/script/{script}/edit-form", response_class=HTMLResponse)
async def inline_edit_form(script: str, index: str = "1"):
    script = safe_script_name(script)
    current_desc = get_script_metadata(script).get("description", "")
    escaped_script = html.escape(quote(script, safe=""), quote=True)
    escaped_index = html.escape(index, quote=True)
    escaped_desc = html.escape(current_desc, quote=True)
    return HTMLResponse(f"""
    <form hx-post="/v2/api/script/{escaped_script}/save-desc?index={escaped_index}" hx-target="#desc-container-{escaped_index}" class="w-full flex gap-1">
        <input type="text" name="description" value="{escaped_desc}" autofocus class="w-full bg-[#121214] border border-blue-600 rounded px-2 py-0.5 text-sm text-gray-200 focus:outline-none" onblur="this.form.requestSubmit()" />
    </form>
    """)


@app.post("/v2/api/script/{script}/save", response_class=HTMLResponse)
async def save_script(script: str, request: Request):
    """Saves edits to a script: name (rename), description, tags, and content.
    Expects form fields: name, description, tags (comma-separated), content.
    Returns the updated table partial so the UI refreshes.
    """
    original = safe_script_name(script)
    original_metadata = get_script_metadata(original)
    original_status = get_script_status(original)
    new_name = (await form_value(request, "name")).strip() or original
    new_description = (await form_value(request, "description")).strip()
    new_tags = (await form_value(request, "tags")).strip()
    new_content = (await form_value(request, "content"))

    # Validate new name; if invalid, keep original
    try:
        new_name = safe_script_name(new_name)
    except HTTPException:
        new_name = original

    orig_path = SCRIPT_DIR / original
    new_path = SCRIPT_DIR / new_name

    # If renaming, ensure target doesn't exist and move file + metadata
    if original != new_name:
        if original in _running_processes or original_status.get("last_status") == "running":
            raise HTTPException(status_code=409, detail="Stop the script before renaming it")
        if new_path.exists():
            raise HTTPException(status_code=400, detail="Target filename already exists")
        if orig_path.exists():
            orig_path.rename(new_path)
        old_log_path = LOG_DIR / f"{original}.log"
        new_log_path = LOG_DIR / f"{new_name}.log"
        if old_log_path.exists() and not new_log_path.exists():
            old_log_path.rename(new_log_path)

        # Move metadata/status rows to the new name without losing persisted settings.
        with sqlite3.connect(DB_FILE) as conn:
            conn.execute("DELETE FROM script_metadata WHERE script_name = ?", (original,))
            conn.execute("DELETE FROM script_status WHERE script_name = ?", (original,))
        for field in ("default_args", "schedule", "starred"):
            if field in original_metadata:
                set_script_metadata_field(new_name, field, original_metadata[field])
        if original_status:
            update_script_status(new_name, original_status)

    # Write content if provided
    if new_content is not None:
        try:
            new_path.write_text(new_content.replace('\r\n', '\n'), encoding="utf-8")
        except Exception:
            # If write fails and original exists, try writing original back
            if orig_path.exists():
                orig_path.write_text(new_content.replace('\r\n', '\n'), encoding="utf-8")

    # Persist metadata fields
    set_script_metadata_field(new_name, "description", new_description)
    set_script_metadata_field(new_name, "tags", new_tags)
    refresh_scheduler_jobs()

    # Return refreshed partial
    return HTMLResponse(render_dashboard(get_combined_data(), partial_only=True))


@app.get("/view_log/{log_file}")
async def view_log(log_file: str):
    if log_file != os.path.basename(log_file) or not log_file.endswith((".log", ".txt")):
        raise HTTPException(status_code=404, detail="Log not found")
    log_path = LOG_DIR / log_file
    if not log_path.is_file():
        raise HTTPException(status_code=404, detail="Log not found")
    return PlainTextResponse(log_path.read_text(errors="replace"))


@app.get("/api/statuses/all")
async def get_all_statuses():
    return JSONResponse(get_all_script_statuses())


@app.get("/v2/view/script-file/{filename}")
async def view_script_file(filename: str):
    # Only allow basename and disallow script extensions
    if filename != os.path.basename(filename) or filename.endswith((".py", ".sh")):
        raise HTTPException(status_code=404, detail="File not found")
    path = SCRIPT_DIR / filename
    if not path.is_file():
        raise HTTPException(status_code=404, detail="File not found")
    # Return as plain text (read with replacement for any decoding errors)
    return PlainTextResponse(path.read_text(errors="replace"))
