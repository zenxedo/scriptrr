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
import uuid
from collections import deque
from datetime import date, datetime
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


VERSION = "0.2.1"
MCP_PROTOCOL_VERSIONS = ("2025-03-26", "2024-11-05")

app = FastAPI(title="Scriptrr v0.2")
_running_processes = {}
scheduler = BackgroundScheduler() if BackgroundScheduler else None

BASE_DIR = Path(__file__).resolve().parent
SCRIPT_DIR = BASE_DIR / "scripts"
LOG_DIR = BASE_DIR / "logs"
DB_FILE = BASE_DIR / "scriptrr.db"
STATIC_DIR = BASE_DIR / "static"

SAFETY_TIERS = ("green", "cautious", "helper")
METADATA_FIELDS = (
    "description", "tags", "default_args", "schedule", "starred",
    "safety", "expires_at", "retired", "retired_reason",
)

# Snapshot of the schedule-relevant metadata the running APScheduler reflects.
# Compared against the DB on a timer so external `sqlite3` edits reconcile.
_active_schedule_map: dict[str, tuple] = {}
_schedule_lock = threading.Lock()
_drift_thread_started = False

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
                exit_code INTEGER,
                last_duration REAL,
                last_trigger TEXT DEFAULT ''
            )
        """)
        conn.execute("""
            CREATE TABLE IF NOT EXISTS script_metadata (
                script_name TEXT PRIMARY KEY,
                description TEXT DEFAULT '',
                tags TEXT DEFAULT '',
                default_args TEXT DEFAULT '',
                schedule TEXT DEFAULT '',
                starred INTEGER DEFAULT 0,
                safety TEXT DEFAULT 'green',
                expires_at TEXT DEFAULT '',
                retired INTEGER DEFAULT 0,
                retired_reason TEXT DEFAULT ''
            )
        """)

        # In-place, data-preserving migrations for existing v0.1 databases.
        metadata_migrations = {
            "starred": "INTEGER DEFAULT 0",
            "safety": "TEXT DEFAULT 'green'",
            "expires_at": "TEXT DEFAULT ''",
            "retired": "INTEGER DEFAULT 0",
            "retired_reason": "TEXT DEFAULT ''",
        }
        metadata_columns = {row[1] for row in conn.execute("PRAGMA table_info(script_metadata)")}
        for column, ddl in metadata_migrations.items():
            if column not in metadata_columns:
                conn.execute(f"ALTER TABLE script_metadata ADD COLUMN {column} {ddl}")

        status_migrations = {
            "last_duration": "REAL",
            "last_trigger": "TEXT DEFAULT ''",
        }
        status_columns = {row[1] for row in conn.execute("PRAGMA table_info(script_status)")}
        for column, ddl in status_migrations.items():
            if column not in status_columns:
                conn.execute(f"ALTER TABLE script_status ADD COLUMN {column} {ddl}")

        # Normalise legacy NULLs left behind by ALTER TABLE defaults.
        conn.execute("UPDATE script_metadata SET safety = 'green' WHERE safety IS NULL OR safety = ''")
        conn.execute("UPDATE script_metadata SET retired = 0 WHERE retired IS NULL")
        conn.execute("UPDATE script_metadata SET expires_at = '' WHERE expires_at IS NULL")
        conn.execute("UPDATE script_metadata SET retired_reason = '' WHERE retired_reason IS NULL")
        conn.execute("UPDATE script_status SET last_trigger = '' WHERE last_trigger IS NULL")


def sync_scripts_on_disk() -> None:
    """Ensure every runnable file in SCRIPT_DIR has a metadata row."""
    if not SCRIPT_DIR.is_dir():
        return
    names = [p.name for p in SCRIPT_DIR.iterdir() if p.is_file() and p.suffix in (".py", ".sh")]
    if not names:
        return
    with sqlite3.connect(DB_FILE) as conn:
        conn.executemany(
            "INSERT OR IGNORE INTO script_metadata (script_name) VALUES (?)",
            [(name,) for name in names],
        )


def _schedule_snapshot() -> dict[str, tuple]:
    with sqlite3.connect(DB_FILE) as conn:
        rows = conn.execute(
            "SELECT script_name, schedule, expires_at, retired FROM script_metadata"
        ).fetchall()
    return {
        row[0]: ((row[1] or "").strip(), row[2] or "", int(row[3] or 0))
        for row in rows
    }


def expire_stale_nudges() -> list[str]:
    """Retire scheduled nudges whose expires_at date has passed."""
    today = date.today().isoformat()
    retired_now = []
    with sqlite3.connect(DB_FILE) as conn:
        rows = conn.execute(
            "SELECT script_name, schedule, expires_at, retired FROM script_metadata"
        ).fetchall()
        for script_name, schedule, expires_at, retired in rows:
            expires_at = (expires_at or "").strip()
            if not expires_at or not (schedule or "").strip() or int(retired or 0):
                continue
            if today > expires_at[:10]:
                conn.execute(
                    "UPDATE script_metadata SET schedule = '', retired = 1, retired_reason = ? "
                    "WHERE script_name = ?",
                    (f"expired {expires_at[:10]}", script_name),
                )
                retired_now.append(script_name)
    return retired_now


def check_schedule_drift() -> bool:
    """Reconcile APScheduler with the DB, including edits made outside the app."""
    sync_scripts_on_disk()
    expired = expire_stale_nudges()
    snapshot = _schedule_snapshot()
    with _schedule_lock:
        changed = snapshot != _active_schedule_map
    if changed or expired:
        refresh_scheduler_jobs()
        return True
    return False


def _drift_watchdog(stop_event: threading.Event) -> None:
    while not stop_event.wait(15):
        try:
            check_schedule_drift()
        except Exception as exc:  # pragma: no cover - defensive
            print(f"Schedule drift check failed: {exc}")


@app.on_event("startup")
async def startup_event():
    init_db()
    sync_scripts_on_disk()
    with sqlite3.connect(DB_FILE) as conn:
        conn.execute("UPDATE script_status SET last_status = 'stopped', pid = NULL WHERE last_status = 'running'")
    if scheduler:
        scheduler.start()
        refresh_scheduler_jobs()
    global _drift_thread_started
    if not _drift_thread_started:
        _drift_thread_started = True
        stop_event = threading.Event()
        threading.Thread(
            target=_drift_watchdog, args=(stop_event,), daemon=True, name="scriptrr-drift"
        ).start()


@app.on_event("shutdown")
async def shutdown_event():
    if scheduler and scheduler.running:
        scheduler.shutdown(wait=False)



STATUS_FIELDS = ("last_run", "last_status", "pid", "exit_code", "last_duration", "last_trigger")


def update_script_status(script_name: str, status_data: dict) -> None:
    """Upsert the provided status fields, preserving the ones not supplied."""
    current = get_script_status(script_name)
    merged = {field: current.get(field) for field in STATUS_FIELDS}
    merged.update({key: value for key, value in status_data.items() if key in STATUS_FIELDS})
    with sqlite3.connect(DB_FILE) as conn:
        conn.execute("""
            INSERT INTO script_status (script_name, last_run, last_status, pid, exit_code, last_duration, last_trigger)
            VALUES (?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(script_name) DO UPDATE SET
                last_run=excluded.last_run,
                last_status=excluded.last_status,
                pid=excluded.pid,
                exit_code=excluded.exit_code,
                last_duration=excluded.last_duration,
                last_trigger=excluded.last_trigger
        """, (
            script_name,
            merged["last_run"],
            merged["last_status"],
            merged["pid"],
            merged["exit_code"],
            merged["last_duration"],
            merged["last_trigger"],
        ))

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

def set_script_metadata_field(script_name: str, field: str, value):
    allowed_fields = set(METADATA_FIELDS)
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


def retire_script(script: str, reason: str) -> dict:
    """Close out a standing intention: clear cron, mark retired, record why."""
    set_script_metadata_field(script, "schedule", "")
    set_script_metadata_field(script, "retired", 1)
    set_script_metadata_field(script, "retired_reason", reason or "retired")
    refresh_scheduler_jobs()
    return get_script_metadata(script)


def read_log_tail(script: str, lines: int = 100) -> str:
    log_path = LOG_DIR / f"{script}.log"
    if not log_path.is_file():
        return ""
    try:
        with log_path.open("r", errors="replace") as fh:
            return "".join(deque(fh, maxlen=max(1, int(lines))))
    except OSError:
        return ""


def _has_pep723_header(script_path: Path) -> bool:
    try:
        with script_path.open("rb") as fh:
            return b"# /// script" in fh.read(4096)
    except OSError:
        return False


def launch_script(script: str, args: str = "", trigger: str = "manual"):
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
    if script.endswith(".py"):
        if _has_pep723_header(script_path) and shutil.which("uv"):
            cmd = ["uv", "run", "--script", str(script_path)]
        else:
            cmd = ["python3", "-u", str(script_path)]
    else:
        cmd = ["bash", str(script_path)]
    if args:
        try:
            cmd.extend(shlex.split(args))
        except ValueError as exc:
            logfile.close()
            raise HTTPException(status_code=400, detail=f"Invalid arguments: {exc}")

    child_env = os.environ.copy()
    child_env.update({
        "SCRIPTRR_API": os.environ.get("SCRIPTRR_API", "http://127.0.0.1:8000"),
        "SCRIPTRR_SCRIPT": script,
        "SCRIPTRR_TRIGGER": trigger,
    })

    start_time = time.time()
    kwargs = {"start_new_session": True} if os.name == "posix" else {}
    try:
        proc = subprocess.Popen(
            cmd,
            stdout=logfile,
            stderr=subprocess.STDOUT,
            text=True,
            bufsize=1,
            env=child_env,
            **kwargs,
        )
    except OSError:
        logfile.close()
        raise

    _running_processes[script] = proc
    update_script_status(script, {
        "last_status": "running",
        "pid": proc.pid,
        "last_run": start_time,
        "exit_code": None,
        "last_duration": None,
        "last_trigger": trigger,
    })

    def watcher_task():
        proc.wait()
        logfile.close()
        duration = round(time.time() - start_time, 3)
        current_status = get_script_status(script)
        if current_status.get("last_status") == "stopped":
            _running_processes.pop(script, None)
            return
        returncode = proc.returncode
        final_status = "success" if returncode in (0, 42) else "fail"
        if returncode == 42:
            # Commit the retirement metadata before flipping status, so a
            # caller polling for "not running" also sees the retired fields.
            metadata = get_script_metadata(script)
            if not int(metadata.get("retired") or 0):
                set_script_metadata_field(script, "schedule", "")
                set_script_metadata_field(script, "retired", 1)
                set_script_metadata_field(script, "retired_reason", "self-retired (exit 42)")
                refresh_scheduler_jobs()
        update_script_status(script, {
            "last_status": final_status,
            "pid": None,
            "last_run": start_time,
            "exit_code": returncode,
            "last_duration": duration,
            "last_trigger": trigger,
        })
        _running_processes.pop(script, None)

    threading.Thread(target=watcher_task, daemon=True, name=f"scriptrr-{script}").start()
    return proc


def run_scheduled_script(script: str) -> None:
    """APScheduler callback; skips overlapping executions and records failures."""
    try:
        # Catch an expiry that landed between watchdog ticks (e.g. midnight).
        expire_stale_nudges()
        metadata = get_script_metadata(script)
        if int(metadata.get("retired") or 0):
            return
        launch_script(script, metadata.get("default_args", "").strip(), trigger="schedule")
    except HTTPException as exc:
        if exc.status_code != 409:
            print(f"Scheduled run failed for {script}: {exc.detail}")
    except Exception as exc:
        print(f"Scheduled run failed for {script}: {exc}")


def refresh_scheduler_jobs() -> None:
    """Rebuild scheduled jobs from the database after startup or schedule edits."""
    global _active_schedule_map
    if not scheduler or not CronTrigger or not scheduler.running:
        return

    scheduler.remove_all_jobs()
    snapshot: dict[str, tuple] = {}
    for script, metadata in get_all_script_metadata().items():
        schedule = (metadata.get("schedule") or "").strip()
        expires_at = (metadata.get("expires_at") or "").strip()
        retired = int(metadata.get("retired") or 0)
        snapshot[script] = (schedule, expires_at, retired)
        if not schedule or retired or not (SCRIPT_DIR / script).is_file():
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
    with _schedule_lock:
        _active_schedule_map = snapshot


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
    sync_scripts_on_disk()
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

        retired = bool(meta.get("retired") or 0)
        next_run_str, next_run_ts = ("Not scheduled", float("inf")) if retired else next_run_info(schedule)

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
            "schedule_raw": schedule,
            "default_args": meta.get("default_args", ""),
            "starred": bool(meta.get("starred", 0)),
            "safety": (meta.get("safety") or "green").strip() or "green",
            "expires_at": (meta.get("expires_at") or "").strip(),
            "retired": retired,
            "retired_reason": meta.get("retired_reason") or "",
            "last_duration": entry.get("last_duration"),
            "last_trigger": entry.get("last_trigger") or "",
            "exit_code": entry.get("exit_code"),
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





# ─────────────────────────────────────────────────────────────────────────────
# Agent-native surface: REST JSON API (/api/v1) + built-in MCP server (/mcp)
# ─────────────────────────────────────────────────────────────────────────────

def _normalise_status(entry: dict) -> str:
    resolved = resolve_status(entry)
    return "idle" if resolved == "unknown" else resolved


def script_object(name: str) -> dict:
    """Canonical JSON representation of one script (metadata + latest run)."""
    metadata = get_script_metadata(name)
    entry = get_script_status(name)
    schedule = (metadata.get("schedule") or "").strip()
    tags_raw = metadata.get("tags") or ""
    tags = [t.strip() for t in tags_raw.split(",") if t.strip()]
    last_run = entry.get("last_run")
    retired = bool(metadata.get("retired") or 0)
    next_run_str, next_run_ts = ("Not scheduled", float("inf")) if retired else next_run_info(schedule)
    return {
        "name": name,
        "type": "python" if name.endswith(".py") else "bash",
        "description": metadata.get("description") or "",
        "tags": tags,
        "safety": (metadata.get("safety") or "green").strip() or "green",
        "schedule": schedule,
        "default_args": metadata.get("default_args") or "",
        "starred": bool(metadata.get("starred") or 0),
        "retired": retired,
        "retired_reason": metadata.get("retired_reason") or "",
        "expires_at": (metadata.get("expires_at") or "").strip(),
        "status": _normalise_status(entry),
        "last_run_ts": last_run or None,
        "last_run_relative": get_relative_time(last_run) if last_run else "Never",
        "last_duration": entry.get("last_duration"),
        "last_trigger": entry.get("last_trigger") or "",
        "exit_code": entry.get("exit_code"),
        "next_run_ts": next_run_ts if next_run_ts != float("inf") else None,
        "next_run_relative": next_run_str or "Not scheduled",
    }


def list_script_objects(safety: str = "", tag: str = "", status: str = "",
                        scheduled: bool | None = None, include_retired: bool = False,
                        q: str = "") -> list[dict]:
    sync_scripts_on_disk()
    names = sorted(p.name for p in SCRIPT_DIR.iterdir() if p.is_file() and p.suffix in (".py", ".sh"))
    safety = (safety or "").strip().lower()
    tag = (tag or "").strip().lower()
    status = (status or "").strip().lower()
    q = (q or "").strip().casefold()
    results = []
    for name in names:
        obj = script_object(name)
        if not include_retired and obj["retired"]:
            continue
        if safety and obj["safety"] != safety:
            continue
        if tag and tag not in [t.lower() for t in obj["tags"]]:
            continue
        if status and obj["status"] != status:
            continue
        if scheduled is not None and bool(obj["schedule"]) != scheduled:
            continue
        if q and q not in " ".join([name, obj["description"], " ".join(obj["tags"])]).casefold():
            continue
        results.append(obj)
    return results


def _require_api_token(request: Request) -> None:
    """Optional Bearer auth; open by default for trusted-LAN use."""
    expected = os.environ.get("SCRIPTRR_API_TOKEN", "").strip()
    if not expected:
        return
    supplied = request.headers.get("authorization", "")
    token = supplied[7:] if supplied.lower().startswith("bearer ") else request.query_params.get("token", "")
    if token != expected:
        raise HTTPException(status_code=401, detail="Invalid or missing API token")


async def _json_body(request: Request) -> dict:
    raw = await request.body()
    if not raw:
        return {}
    try:
        payload = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise HTTPException(status_code=400, detail=f"Invalid JSON body: {exc}")
    if not isinstance(payload, dict):
        raise HTTPException(status_code=400, detail="JSON body must be an object")
    return payload


def _coerce_tags(value) -> str:
    if value is None:
        return ""
    if isinstance(value, (list, tuple)):
        return ", ".join(str(v).strip() for v in value if str(v).strip())
    return ", ".join(part.strip() for part in str(value).split(",") if part.strip())


def _validate_schedule(value: str) -> str:
    value = (value or "").strip()
    if not value:
        return ""
    if not CronTrigger:
        raise HTTPException(status_code=503, detail="Scheduling support is unavailable")
    try:
        CronTrigger.from_crontab(value)
    except Exception as exc:
        raise HTTPException(status_code=400, detail=f"Invalid cron schedule: {exc}")
    return value


def _validate_expires_at(value: str) -> str:
    value = (value or "").strip()
    if not value:
        return ""
    try:
        datetime.strptime(value[:10], "%Y-%m-%d")
    except ValueError:
        raise HTTPException(status_code=400, detail="expires_at must be YYYY-MM-DD")
    return value[:10]


def _validate_safety(value, default: str = "green") -> str:
    value = (str(value).strip().lower() if value is not None else default)
    if value not in SAFETY_TIERS:
        raise HTTPException(status_code=400, detail=f"safety must be one of {', '.join(SAFETY_TIERS)}")
    return value


def apply_script_updates(script: str, payload: dict, create: bool = False) -> dict:
    """Write content and/or metadata for a script, then sync the scheduler."""
    script = safe_script_name(script)
    script_path = SCRIPT_DIR / script
    content = payload.get("content")
    if create and content is None and not script_path.exists():
        raise HTTPException(status_code=400, detail="content is required to create a new script")
    if content is not None:
        if not content.endswith("\n"):
            content += "\n"
        script_path.write_text(content.replace("\r\n", "\n"), encoding="utf-8", newline="\n")
        try:
            script_path.chmod(0o755)
        except OSError:
            pass

    if payload.get("description") is not None:
        set_script_metadata_field(script, "description", str(payload["description"]).strip())
    if payload.get("tags") is not None:
        set_script_metadata_field(script, "tags", _coerce_tags(payload["tags"]))
    if payload.get("default_args") is not None:
        set_script_metadata_field(script, "default_args", str(payload["default_args"]).strip())
    if payload.get("safety") is not None:
        set_script_metadata_field(script, "safety", _validate_safety(payload["safety"]))
    if payload.get("schedule") is not None:
        set_script_metadata_field(script, "schedule", _validate_schedule(str(payload["schedule"])))
    if payload.get("expires_at") is not None:
        set_script_metadata_field(script, "expires_at", _validate_expires_at(str(payload["expires_at"])))
    if payload.get("retired") is not None:
        set_script_metadata_field(script, "retired", 1 if _as_bool(payload["retired"]) else 0)
    if payload.get("retired_reason") is not None:
        set_script_metadata_field(script, "retired_reason", str(payload["retired_reason"]))
    if payload.get("starred") is not None:
        set_script_metadata_field(script, "starred", 1 if _as_bool(payload["starred"]) else 0)

    # Re-arming a retired nudge (setting a cron without explicitly asking to
    # stay retired) clears the retirement state — same rule as the UI route.
    if str(payload.get("schedule") or "").strip() and not _as_bool(payload.get("retired")):
        metadata = get_script_metadata(script)
        if int(metadata.get("retired") or 0):
            set_script_metadata_field(script, "retired", 0)
            set_script_metadata_field(script, "retired_reason", "")

    if not script_path.exists():
        raise HTTPException(status_code=404, detail="Script not found")
    refresh_scheduler_jobs()
    return script_object(script)


CAUTIOUS_BLOCK_MESSAGE = (
    "Script is marked 'cautious' (mutates data). Pass confirm_cautious=true after "
    "operator approval, or run with --dry-run."
)


def _as_bool(value) -> bool:
    """Lenient boolean coercion: only explicit truthy values count.

    Guards against JSON clients sending "false"/"0"/"no" as strings, which
    ``bool("false")`` would otherwise treat as True.
    """
    if isinstance(value, bool):
        return value
    if value is None:
        return False
    if isinstance(value, (int, float)):
        return value != 0
    return str(value).strip().lower() in ("true", "1", "yes", "on")


def _args_request_dry_run(args: str) -> bool:
    """True only when args contain an actual dry-run flag (not --dry-run=false)."""
    try:
        tokens = shlex.split(args or "")
    except ValueError:
        tokens = (args or "").split()
    for token in tokens:
        if token == "--dry-run":
            return True
        if token.startswith("--dry-run="):
            value = token.split("=", 1)[1].strip().lower()
            if value not in ("false", "0", "no", "off", ""):
                return True
    return False


def cautious_blocked(metadata: dict, args: str, confirm_cautious: bool) -> bool:
    if (metadata.get("safety") or "green") != "cautious":
        return False
    if _as_bool(confirm_cautious):
        return False
    return not _args_request_dry_run(args)


def effective_args(metadata: dict, requested) -> str:
    """Explicit args win; None *or* blank falls back to the saved default_args."""
    if requested is None or str(requested).strip() == "":
        return str(metadata.get("default_args", "")).strip()
    return str(requested).strip()


async def execute_run(script: str, payload: dict) -> dict:
    """Launch a script and optionally wait for it, returning a JSON-friendly result."""
    script = safe_script_name(script)
    if not (SCRIPT_DIR / script).is_file():
        raise HTTPException(status_code=404, detail="Script not found")
    metadata = get_script_metadata(script)
    args = effective_args(metadata, payload.get("args"))
    wait = _as_bool(payload.get("wait", True))
    try:
        timeout = max(1, int(payload.get("timeout") or 60))
    except (TypeError, ValueError):
        raise HTTPException(status_code=400, detail="timeout must be an integer number of seconds")
    trigger = str(payload.get("trigger") or "agent").strip().lower()
    if trigger not in ("agent", "manual", "schedule"):
        trigger = "agent"

    proc = launch_script(script, args, trigger=trigger)
    result = {"script": script, "pid": proc.pid, "args": args, "trigger": trigger}

    if not wait:
        result.update({"status": "running", "message": "Started in background"})
        return result

    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline and proc.poll() is None:
        await asyncio.sleep(0.2)
    if proc.poll() is None:
        result.update({"status": "running", "pid": proc.pid, "message": "Still running in background"})
        return result

    for _ in range(40):
        entry = get_script_status(script)
        if entry.get("last_status") != "running":
            break
        await asyncio.sleep(0.1)
    entry = get_script_status(script)
    result.update({
        "status": _normalise_status(entry),
        "exit_code": entry.get("exit_code"),
        "duration": entry.get("last_duration"),
        "log_tail": read_log_tail(script, 60),
    })
    return result


@app.get("/api/v1/health")
async def api_v1_health():
    return JSONResponse({"status": "ok", "name": "scriptrr", "version": VERSION})


@app.get("/api/v1/scripts")
async def api_v1_list_scripts(
    request: Request,
    safety: str = "",
    tag: str = "",
    status: str = "",
    scheduled: bool | None = None,
    include_retired: bool = False,
    q: str = "",
):
    _require_api_token(request)
    check_schedule_drift()
    return JSONResponse(list_script_objects(
        safety=safety, tag=tag, status=status, scheduled=scheduled,
        include_retired=include_retired, q=q,
    ))


@app.get("/api/v1/scripts/{script}")
async def api_v1_get_script(
    script: str,
    request: Request,
    include_content: bool = False,
    log_lines: int = 100,
):
    _require_api_token(request)
    script = safe_script_name(script)
    if not (SCRIPT_DIR / script).is_file():
        raise HTTPException(status_code=404, detail="Script not found")
    obj = script_object(script)
    if include_content:
        obj["content"] = (SCRIPT_DIR / script).read_text(encoding="utf-8", errors="replace")
    obj["log_tail"] = read_log_tail(script, max(1, min(log_lines, 5000)))
    return JSONResponse(obj)


@app.post("/api/v1/scripts", status_code=201)
async def api_v1_create_script(request: Request):
    _require_api_token(request)
    payload = await _json_body(request)
    name = str(payload.get("name") or payload.get("script") or "").strip()
    if not name:
        raise HTTPException(status_code=400, detail="name is required")
    created = not (SCRIPT_DIR / name).exists()
    obj = apply_script_updates(name, payload, create=True)
    return JSONResponse(obj, status_code=201 if created else 200)


@app.put("/api/v1/scripts/{script}")
async def api_v1_put_script(script: str, request: Request):
    _require_api_token(request)
    payload = await _json_body(request)
    return JSONResponse(apply_script_updates(script, payload, create=True))


@app.patch("/api/v1/scripts/{script}")
async def api_v1_patch_script(script: str, request: Request):
    _require_api_token(request)
    payload = await _json_body(request)
    return JSONResponse(apply_script_updates(script, payload))


@app.post("/api/v1/scripts/{script}/run")
async def api_v1_run_script(script: str, request: Request):
    _require_api_token(request)
    script = safe_script_name(script)
    payload = await _json_body(request)
    metadata = get_script_metadata(script)
    if not (SCRIPT_DIR / script).is_file():
        raise HTTPException(status_code=404, detail="Script not found")
    args = payload.get("args")
    run_args = effective_args(metadata, args)
    if cautious_blocked(metadata, run_args, payload.get("confirm_cautious", False)):
        return JSONResponse(
            status_code=409,
            content={"error": "cautious_script", "message": CAUTIOUS_BLOCK_MESSAGE},
        )
    return JSONResponse(await execute_run(script, payload))


@app.post("/api/v1/scripts/{script}/stop")
async def api_v1_stop_script(script: str, request: Request):
    _require_api_token(request)
    script = safe_script_name(script)
    if not (SCRIPT_DIR / script).is_file():
        raise HTTPException(status_code=404, detail="Script not found")
    await stop_script(script)
    return JSONResponse(script_object(script))


@app.post("/api/v1/scripts/{script}/retire")
async def api_v1_retire_script(script: str, request: Request):
    _require_api_token(request)
    script = safe_script_name(script)
    payload = await _json_body(request)
    if not (SCRIPT_DIR / script).is_file():
        raise HTTPException(status_code=404, detail="Script not found")
    reason = str(payload.get("reason") or "retired").strip() or "retired"
    retire_script(script, reason)
    return JSONResponse(script_object(script))


@app.get("/api/v1/scripts/{script}/log")
async def api_v1_script_log(script: str, request: Request, tail: int = 200):
    _require_api_token(request)
    script = safe_script_name(script)
    if not (SCRIPT_DIR / script).is_file():
        raise HTTPException(status_code=404, detail="Script not found")
    return JSONResponse({"script": script, "log": read_log_tail(script, max(1, min(tail, 5000)))})


# ── MCP server (Streamable HTTP, JSON-RPC 2.0) ──────────────────────────────

MCP_TOOLS = [
    {
        "name": "scriptrr_list_scripts",
        "description": (
            "List scripts in Scriptrr with their purpose, safety tier (green = run freely, "
            "cautious = ask human first, helper = utility), schedule, last run "
            "status/duration/trigger, and expiry. Use this to answer 'what should we run now' "
            "or check standing jobs."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "safety": {"type": "string", "enum": list(SAFETY_TIERS)},
                "tag": {"type": "string"},
                "scheduled_only": {"type": "boolean"},
                "include_retired": {"type": "boolean", "default": False},
                "search": {"type": "string"},
            },
        },
    },
    {
        "name": "scriptrr_get_script",
        "description": "Read a script's metadata, latest run log tail, and optionally its source code.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "script": {"type": "string"},
                "include_content": {"type": "boolean", "default": False},
                "log_lines": {"type": "integer", "default": 100},
            },
            "required": ["script"],
        },
    },
    {
        "name": "scriptrr_run_script",
        "description": (
            "Run a script through Scriptrr so its execution, duration, exit code, and output "
            "log are captured on the dashboard (trigger='agent'). Enforces safety tiers: "
            "cautious scripts require confirm_cautious=true (only after explicit human "
            "approval) unless --dry-run is passed in args."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "script": {"type": "string"},
                "args": {"type": "string"},
                "wait": {"type": "boolean", "default": True},
                "timeout": {"type": "integer", "default": 60},
                "confirm_cautious": {"type": "boolean", "default": False},
            },
            "required": ["script"],
        },
    },
    {
        "name": "scriptrr_upsert_script",
        "description": (
            "Create a new script or update an existing script's metadata (description, tags, "
            "safety, default_args, schedule, expires_at) and/or source content, immediately "
            "syncing the live cron scheduler."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "script": {"type": "string"},
                "description": {"type": "string"},
                "tags": {"type": "string"},
                "safety": {"type": "string", "enum": list(SAFETY_TIERS)},
                "default_args": {"type": "string"},
                "schedule": {"type": "string"},
                "expires_at": {"type": "string"},
                "content": {"type": "string"},
                "retired": {"type": "boolean"},
            },
            "required": ["script"],
        },
    },
    {
        "name": "scriptrr_retire_script",
        "description": (
            "Retire a standing-intention / nudge script (retired=true), clearing its cron "
            "schedule and recording reason."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "script": {"type": "string"},
                "reason": {"type": "string"},
            },
            "required": ["script", "reason"],
        },
    },
]


def _mcp_content(payload, is_error: bool = False) -> dict:
    return {
        "content": [{"type": "text", "text": json.dumps(payload, indent=2, default=str)}],
        "isError": is_error,
    }


def _mcp_error(request_id, code: int, message: str) -> dict:
    return {"jsonrpc": "2.0", "id": request_id, "error": {"code": code, "message": message}}


def _mcp_require(arguments: dict, key: str) -> str:
    value = arguments.get(key)
    if value is None or str(value).strip() == "":
        raise ValueError(f"'{key}' is required")
    return str(value)


async def _mcp_call_tool(name: str, arguments: dict) -> dict:
    arguments = arguments or {}
    if name == "scriptrr_list_scripts":
        objects = list_script_objects(
            safety=arguments.get("safety", ""),
            tag=arguments.get("tag", ""),
            scheduled=True if _as_bool(arguments.get("scheduled_only")) else None,
            include_retired=_as_bool(arguments.get("include_retired", False)),
            q=arguments.get("search", ""),
        )
        return _mcp_content({"count": len(objects), "scripts": objects})

    if name == "scriptrr_get_script":
        script = safe_script_name(_mcp_require(arguments, "script"))
        if not (SCRIPT_DIR / script).is_file():
            return _mcp_content({"error": "script_not_found", "script": script}, is_error=True)
        obj = script_object(script)
        if arguments.get("include_content"):
            obj["content"] = (SCRIPT_DIR / script).read_text(encoding="utf-8", errors="replace")
        obj["log_tail"] = read_log_tail(script, int(arguments.get("log_lines") or 100))
        return _mcp_content(obj)

    if name == "scriptrr_run_script":
        script = safe_script_name(_mcp_require(arguments, "script"))
        if not (SCRIPT_DIR / script).is_file():
            return _mcp_content({"error": "script_not_found", "script": script}, is_error=True)
        metadata = get_script_metadata(script)
        args = arguments.get("args")
        run_args = effective_args(metadata, args)
        if cautious_blocked(metadata, run_args, arguments.get("confirm_cautious", False)):
            return _mcp_content(
                {"error": "cautious_script", "script": script, "message": CAUTIOUS_BLOCK_MESSAGE},
                is_error=True,
            )
        try:
            result = await execute_run(script, {
                "args": args,
                "wait": arguments.get("wait", True),
                "timeout": arguments.get("timeout", 60),
                "confirm_cautious": arguments.get("confirm_cautious", False),
                "trigger": "agent",
            })
        except HTTPException as exc:
            return _mcp_content({"error": "run_failed", "message": str(exc.detail)}, is_error=True)
        return _mcp_content(result)

    if name == "scriptrr_upsert_script":
        script = _mcp_require(arguments, "script")
        try:
            obj = apply_script_updates(script, arguments, create=True)
        except HTTPException as exc:
            return _mcp_content({"error": "upsert_failed", "message": str(exc.detail)}, is_error=True)
        return _mcp_content(obj)

    if name == "scriptrr_retire_script":
        script = safe_script_name(_mcp_require(arguments, "script"))
        reason = _mcp_require(arguments, "reason")
        if not (SCRIPT_DIR / script).is_file():
            return _mcp_content({"error": "script_not_found", "script": script}, is_error=True)
        retire_script(script, reason)
        return _mcp_content(script_object(script))

    return _mcp_content({"error": "unknown_tool", "tool": name}, is_error=True)


@app.post("/mcp")
async def mcp_endpoint(request: Request):
    _require_api_token(request)
    raw = await request.body()
    if not raw:
        return JSONResponse(_mcp_error(None, -32600, "Empty request body"), status_code=400)
    try:
        message = json.loads(raw)
    except json.JSONDecodeError as exc:
        return JSONResponse(_mcp_error(None, -32700, f"Parse error: {exc}"), status_code=400)

    if isinstance(message, list):
        if not message:
            return JSONResponse(_mcp_error(None, -32600, "Empty batch"), status_code=400)
        responses = []
        for item in message:
            response = await _mcp_handle_message(item)
            if response is not None:
                responses.append(response)
        if not responses:
            return Response(status_code=202)
        return JSONResponse(responses)

    response = await _mcp_handle_message(message)
    if response is None:
        return Response(status_code=202)
    return JSONResponse(response)


async def _mcp_handle_message(message: dict):
    if not isinstance(message, dict) or message.get("jsonrpc") != "2.0":
        return _mcp_error(message.get("id") if isinstance(message, dict) else None, -32600, "Invalid Request")

    method = message.get("method", "")
    request_id = message.get("id")
    params = message.get("params") or {}
    if not isinstance(params, dict):
        return _mcp_error(request_id, -32602, "params must be an object")
    is_notification = "id" not in message

    if method == "initialize":
        requested = params.get("protocolVersion", MCP_PROTOCOL_VERSIONS[0])
        # Fall back to the newest version we support when the client asks for
        # something we don't know.
        protocol = requested if requested in MCP_PROTOCOL_VERSIONS else MCP_PROTOCOL_VERSIONS[0]
        return {
            "jsonrpc": "2.0",
            "id": request_id,
            "result": {
                "protocolVersion": protocol,
                "capabilities": {"tools": {"listChanged": False}},
                "serverInfo": {"name": "scriptrr", "version": VERSION},
                "instructions": (
                    "Scriptrr runs and remembers plain .py/.sh scripts on disk. "
                    "Check 'safety' before running: green = run freely, cautious = ask the "
                    "human first, helper = utility not meant for standalone runs."
                ),
            },
        }

    if method in ("notifications/initialized", "notifications/cancelled"):
        return None

    if method == "ping":
        return {"jsonrpc": "2.0", "id": request_id, "result": {}}

    if method == "tools/list":
        return {"jsonrpc": "2.0", "id": request_id, "result": {"tools": MCP_TOOLS}}

    if method == "tools/call":
        tool_name = params.get("name", "")
        arguments = params.get("arguments") or {}
        known = {tool["name"] for tool in MCP_TOOLS}
        if tool_name not in known:
            return _mcp_error(request_id, -32602, f"Unknown tool: {tool_name}")
        try:
            result = await _mcp_call_tool(tool_name, arguments)
        except ValueError as exc:
            return _mcp_error(request_id, -32602, str(exc))
        except HTTPException as exc:
            return _mcp_error(request_id, -32602, str(exc.detail))
        except Exception as exc:  # pragma: no cover - defensive
            return _mcp_error(request_id, -32603, f"Tool execution failed: {exc}")
        return {"jsonrpc": "2.0", "id": request_id, "result": result}

    if is_notification:
        return None
    return _mcp_error(request_id, -32601, f"Method not found: {method}")


@app.get("/mcp")
async def mcp_get():
    # Streamable HTTP allows a server to refuse the optional SSE stream.
    return Response(status_code=405, headers={"Allow": "POST"})


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
    safety: str = Form("green"),
    content: str = Form(...)
):
    filename = safe_script_name(filename)
    script_path = SCRIPT_DIR / filename
    script_path.write_text(content.replace('\r\n', '\n'), encoding="utf-8")
    
    if description:
        set_script_metadata_field(filename, "description", description)
    try:
        set_script_metadata_field(filename, "safety", _validate_safety(safety))
    except HTTPException:
        set_script_metadata_field(filename, "safety", "green")
        
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
    expires_at = (await form_value(request, "expires_at")).strip()

    if new_schedule:
        if not CronTrigger:
            raise HTTPException(status_code=503, detail="Scheduling support is unavailable")
        try:
            CronTrigger.from_crontab(new_schedule)
        except Exception as exc:
            raise HTTPException(status_code=400, detail=f"Invalid cron schedule: {exc}")

    if expires_at:
        expires_at = _validate_expires_at(expires_at)

    set_script_metadata_field(script, "schedule", new_schedule)
    set_script_metadata_field(script, "expires_at", expires_at)
    if new_schedule and get_script_metadata(script).get("retired"):
        # Re-arming a retired nudge clears the retirement state.
        set_script_metadata_field(script, "retired", 0)
        set_script_metadata_field(script, "retired_reason", "")
    refresh_scheduler_jobs()
    
    # Render the entire table partial so the "Next Run" column updates immediately
    return HTMLResponse(render_dashboard(get_combined_data(), partial_only=True))


@app.post("/v2/api/script/{script}/retire", response_class=HTMLResponse)
async def ui_retire_script(script: str, request: Request):
    script = safe_script_name(script)
    reason = (await form_value(request, "reason", "retired via flight deck")).strip() or "retired via flight deck"
    retire_script(script, reason)
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
    was_running = bool(proc and proc.poll() is None) or entry.get("last_status") == "running"

    # Lock in "stopped" *before* signalling, so the watcher_task that wakes on
    # process exit sees it and does not overwrite it with "fail" (SIGTERM).
    if was_running:
        entry.update({"last_status": "stopped", "pid": None, "last_run": time.time()})
        update_script_status(script, entry)

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
    elif pid and was_running:
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

    return JSONResponse({"success": True, "stopped": was_running})


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
    new_safety = (await form_value(request, "safety")).strip()
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
        for field in ("default_args", "schedule", "starred", "safety", "expires_at", "retired", "retired_reason"):
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
    if new_safety:
        set_script_metadata_field(new_name, "safety", _validate_safety(new_safety))
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
