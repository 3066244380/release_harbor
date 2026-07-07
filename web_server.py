import argparse
import copy
import json
import mimetypes
import os
import subprocess
import sys
import threading
import traceback
import webbrowser
from urllib.error import URLError
from urllib.request import urlopen
from dataclasses import dataclass, field
from datetime import datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import unquote, urlparse

import release_harbor
from release_harbor import ConfigFile, EXAMPLE_CONFIG, LOCAL_CONFIG, LOG_DIR, ReleaseError

APP_DIR = Path(__file__).resolve().parent
WEB_DIR = APP_DIR / "web"
HOST = "127.0.0.1"
PORT = 8765

jobs = {}
jobs_lock = threading.Lock()
current_job_id = None


@dataclass
class Job:
    id: str
    project_name: str
    env_name: str
    mode: str
    steps: list
    status: str = "pending"
    active_key: str = "validate"
    active_title: str = "校验配置"
    active_index: int = 0
    percent: int = 0
    logs: list = field(default_factory=list)
    error: str = ""
    log_file: str = ""
    cancel_requested: bool = False
    current_process: object | None = field(default=None, repr=False, compare=False)
    current_process_id: int | None = None
    created_at: str = field(default_factory=lambda: datetime.now().isoformat(timespec="seconds"))
    updated_at: str = field(default_factory=lambda: datetime.now().isoformat(timespec="seconds"))

    def set_stage(self, key, title=None):
        self.active_key = key
        self.active_title = title or next((step["title"] for step in self.steps if step["key"] == key), key)
        self.active_index = next((index for index, step in enumerate(self.steps) if step["key"] == key), self.active_index)
        self.percent = round(((self.active_index + 1) / len(self.steps)) * 100) if self.steps else 0
        self.updated_at = datetime.now().isoformat(timespec="seconds")

    def append_log(self, line):
        self.logs.append(line)
        self.updated_at = datetime.now().isoformat(timespec="seconds")

    def to_dict(self):
        return {
            "id": self.id,
            "project_name": self.project_name,
            "env_name": self.env_name,
            "mode": self.mode,
            "status": self.status,
            "active_key": self.active_key,
            "active_title": self.active_title,
            "active_index": self.active_index,
            "percent": self.percent,
            "steps": self.steps,
            "logs": self.logs,
            "error": self.error,
            "log_file": self.log_file,
            "cancel_requested": self.cancel_requested,
            "current_process_id": self.current_process_id,
            "created_at": self.created_at,
            "updated_at": self.updated_at,
        }


class JobLogger:
    def __init__(self, job):
        self.job = job
        LOG_DIR.mkdir(parents=True, exist_ok=True)
        stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        self.path = LOG_DIR / f"release_{stamp}.log"
        self.job.log_file = str(self.path)

    def set_stage(self, key, title=None):
        with jobs_lock:
            self.job.set_stage(key, title)

    def set_process(self, process):
        with jobs_lock:
            self.job.current_process = process
            self.job.current_process_id = process.pid
            self.job.updated_at = datetime.now().isoformat(timespec="seconds")

    def clear_process(self, process):
        with jobs_lock:
            if self.job.current_process is process:
                self.job.current_process = None
                self.job.current_process_id = None
                self.job.updated_at = datetime.now().isoformat(timespec="seconds")

    def is_cancel_requested(self):
        with jobs_lock:
            return self.job.cancel_requested

    def write(self, message):
        line = f"[{datetime.now().strftime('%H:%M:%S')}] {message}"
        with self.path.open("a", encoding="utf-8") as log_file:
            log_file.write(line + "\n")
        with jobs_lock:
            self.job.append_log(line)


def terminate_process(process):
    if process.poll() is not None:
        return
    if os.name == "nt":
        subprocess.run(["taskkill", "/PID", str(process.pid), "/T", "/F"], capture_output=True, text=True)
    else:
        process.terminate()


def cancel_job(job_id):
    with jobs_lock:
        job = jobs.get(job_id)
        if not job:
            raise ReleaseError("任务不存在")
        if job.status != "running":
            return job
        job.cancel_requested = True
        job.updated_at = datetime.now().isoformat(timespec="seconds")
        process = job.current_process
        job.append_log(f"[{datetime.now().strftime('%H:%M:%S')}] 收到停止请求")
    if process:
        terminate_process(process)
    return job


def read_request_json(handler):
    length = int(handler.headers.get("Content-Length", "0") or 0)
    if length <= 0:
        return {}
    raw = handler.rfile.read(length).decode("utf-8-sig")
    return json.loads(raw) if raw.strip() else {}


def send_json(handler, payload, status=200):
    body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    handler.send_response(status)
    handler.send_header("Content-Type", "application/json; charset=utf-8")
    handler.send_header("Content-Length", str(len(body)))
    handler.end_headers()
    handler.wfile.write(body)


def send_error_json(handler, message, status=400, extra=None):
    payload = {"ok": False, "error": message}
    if extra:
        payload.update(extra)
    send_json(handler, payload, status=status)


def config_source_path():
    return LOCAL_CONFIG if LOCAL_CONFIG.exists() else EXAMPLE_CONFIG


def load_public_config():
    path = config_source_path()
    if not path.exists():
        raise ReleaseError("找不到 config.local.json 或 config.example.json")
    data = release_harbor.read_json_file(path)
    return path, sanitize_config(data)


def sanitize_config(data):
    clean = copy.deepcopy(data)
    for deploy in iter_deploy_blocks(clean):
        deploy.pop("password", None)
    return clean


def iter_deploy_blocks(data):
    deploy = data.get("deploy")
    if isinstance(deploy, dict):
        yield deploy
    for project in data.get("projects", []) or []:
        deploy = project.get("deploy")
        if isinstance(deploy, dict):
            yield deploy


def payload_to_config(payload):
    data = payload.get("config") if isinstance(payload, dict) and "config" in payload else payload
    if not isinstance(data, dict):
        raise ReleaseError("配置必须是 JSON 对象")
    if "projects" not in data and isinstance(data.get("state"), dict):
        data = data["state"]
    if "projects" not in data:
        raise ReleaseError("配置缺少 projects")
    return sanitize_config({"projects": data.get("projects")})


def merged_config_for_validation(data):
    merged = copy.deepcopy(data)
    release_harbor.merge_secret_config(merged)
    return ConfigFile(LOCAL_CONFIG, merged, False)


def save_config(payload):
    data = payload_to_config(payload)
    LOCAL_CONFIG.write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return data


def select_local_path(payload):
    kind = str(payload.get("kind") or "").strip()
    title = str(payload.get("title") or "选择路径")
    initial_dir = str(payload.get("initial_dir") or APP_DIR)
    if kind not in ("directory", "file"):
        raise ReleaseError("kind 只支持 directory 或 file")
    try:
        from tkinter import Tk, filedialog
    except Exception as exc:
        raise ReleaseError(f"无法打开系统选择框: {exc}") from exc

    root = Tk()
    root.withdraw()
    root.attributes("-topmost", True)
    try:
        if kind == "directory":
            selected = filedialog.askdirectory(title=title, initialdir=initial_dir)
        else:
            selected = filedialog.askopenfilename(title=title, initialdir=initial_dir)
    finally:
        root.destroy()
    return selected


def server_url(host, port):
    return f"http://{host}:{port}/"


def existing_server_is_alive(host, port):
    try:
        with urlopen(server_url(host, port) + "api/config", timeout=1) as response:
            return response.status == 200
    except (OSError, URLError):
        return False


def open_browser_later(url, delay=0.5):
    timer = threading.Timer(delay, lambda: webbrowser.open(url))
    timer.daemon = True
    timer.start()


def start_job(payload):
    global current_job_id
    project_name = str(payload.get("project_name") or "").strip()
    env_name = str(payload.get("env_name") or "").strip()
    mode = str(payload.get("mode") or "upload").strip()
    if mode not in release_harbor.JOB_STEPS:
        raise ReleaseError(f"不支持的执行模式: {mode}")
    if not project_name:
        raise ReleaseError("project_name 不能为空")
    if not env_name:
        raise ReleaseError("env_name 不能为空")

    config_file = release_harbor.load_config()
    errors = release_harbor.validate_config(config_file, mode=mode)
    if errors:
        raise ReleaseError("; ".join(errors))

    with jobs_lock:
        if current_job_id and jobs.get(current_job_id) and jobs[current_job_id].status == "running":
            raise ReleaseError("已有任务正在执行，请等待完成")
        job_id = datetime.now().strftime("%Y%m%d%H%M%S%f")
        job = Job(job_id, project_name, env_name, mode, release_harbor.get_job_steps(mode))
        jobs[job_id] = job
        current_job_id = job_id

    thread = threading.Thread(target=run_job, args=(job_id,), daemon=True)
    thread.start()
    return job


def run_job(job_id):
    global current_job_id
    with jobs_lock:
        job = jobs[job_id]
        job.status = "running"
        job.set_stage("validate", "校验配置")
    logger = JobLogger(job)
    try:
        logger.write(f"日志文件: {logger.path}")
        config_file = release_harbor.load_config()
        release_harbor.execute_release(config_file, job.project_name, job.env_name, logger, mode=job.mode)
        with jobs_lock:
            job.status = "success"
            job.set_stage("done", "完成")
    except Exception as exc:
        logger.write(traceback.format_exc())
        with jobs_lock:
            if job.cancel_requested:
                job.status = "cancelled"
                job.error = "任务已取消"
            else:
                job.status = "failed"
                job.error = str(exc)
    finally:
        with jobs_lock:
            job.current_process = None
            job.current_process_id = None
            if current_job_id == job_id:
                current_job_id = None


class ReleaseWebHandler(BaseHTTPRequestHandler):
    server_version = "ReleaseSenderHTTP/1.0"

    def do_GET(self):
        parsed = urlparse(self.path)
        path = parsed.path
        try:
            if path == "/api/config":
                source, data = load_public_config()
                send_json(self, {"ok": True, "source": str(source), "config": data})
                return
            if path.startswith("/api/jobs/"):
                job_id = path.rsplit("/", 1)[-1]
                with jobs_lock:
                    job = jobs.get(job_id)
                    payload = job.to_dict() if job else None
                if not payload:
                    send_error_json(self, "任务不存在", status=404)
                    return
                send_json(self, {"ok": True, "job": payload})
                return
            self.serve_static(path)
        except Exception as exc:
            send_error_json(self, str(exc), status=500)

    def do_POST(self):
        parsed = urlparse(self.path)
        path = parsed.path
        try:
            payload = read_request_json(self)
            if path == "/api/config":
                data = save_config(payload)
                send_json(self, {"ok": True, "config": data, "path": str(LOCAL_CONFIG)})
                return
            if path == "/api/check":
                mode = str(payload.get("mode") or "upload")
                data = payload_to_config(payload) if "config" in payload or "projects" in payload else load_public_config()[1]
                config_file = merged_config_for_validation(data)
                errors = release_harbor.validate_config(config_file, mode=mode)
                send_json(self, {"ok": not bool(errors), "errors": errors})
                return
            if path == "/api/jobs":
                job = start_job(payload)
                send_json(self, {"ok": True, "job": job.to_dict()}, status=202)
                return
            if path.startswith("/api/jobs/") and path.endswith("/cancel"):
                job_id = path.split("/")[-2]
                job = cancel_job(job_id)
                send_json(self, {"ok": True, "job": job.to_dict()})
                return
            if path == "/api/select-path":
                selected = select_local_path(payload)
                send_json(self, {"ok": True, "path": selected, "cancelled": not bool(selected)})
                return
            send_error_json(self, "接口不存在", status=404)
        except ReleaseError as exc:
            send_error_json(self, str(exc), status=400)
        except Exception as exc:
            send_error_json(self, str(exc), status=500)

    def serve_static(self, path):
        if path in ("", "/"):
            path = "/index.html"
        relative = unquote(path).lstrip("/").replace("/", "\\")
        target = (WEB_DIR / relative).resolve()
        web_root = WEB_DIR.resolve()
        if target != web_root and web_root not in target.parents:
            send_error_json(self, "非法路径", status=403)
            return
        if not target.exists() or not target.is_file():
            send_error_json(self, "文件不存在", status=404)
            return
        content_type = mimetypes.guess_type(str(target))[0] or "application/octet-stream"
        body = target.read_bytes()
        if target.suffix.lower() in (".html", ".css", ".js", ".json", ".txt"):
            content_type = content_type.split(";")[0] + "; charset=utf-8"
        self.send_response(200)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, format, *args):
        timestamp = datetime.now().strftime("%H:%M:%S")
        print(f"[{timestamp}] {self.address_string()} {format % args}")


def main():
    parser = argparse.ArgumentParser(description="Release Harbor 发布港本地 Web 服务")
    parser.add_argument("--host", default=HOST, help="监听地址，默认 127.0.0.1")
    parser.add_argument("--port", type=int, default=PORT, help="监听端口，默认 8765")
    parser.add_argument("--open-browser", action="store_true", help="启动后自动打开浏览器")
    args = parser.parse_args()
    url = server_url(args.host, args.port)
    if args.open_browser and existing_server_is_alive(args.host, args.port):
        print(f"Release Harbor 发布港已在运行: {url}")
        webbrowser.open(url)
        return 0
    try:
        server = ThreadingHTTPServer((args.host, args.port), ReleaseWebHandler)
    except OSError:
        if existing_server_is_alive(args.host, args.port):
            print(f"Release Harbor 发布港已在运行: {url}")
            if args.open_browser:
                webbrowser.open(url)
            return 0
        raise
    print(f"Release Harbor 发布港已启动: {url}")
    if args.open_browser:
        open_browser_later(url)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\n正在停止服务...")
    finally:
        server.server_close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
