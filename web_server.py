import argparse
import copy
import base64
import hashlib
import hmac
import json
import mimetypes
import os
import subprocess
import sys
import threading
import time
import traceback
import webbrowser
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen
from dataclasses import dataclass, field
from datetime import datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, unquote, urlparse

import release_harbor
from release_harbor import ConfigFile, EXAMPLE_CONFIG, LOCAL_CONFIG, LOG_DIR, ReleaseError

APP_DIR = Path(__file__).resolve().parent
WEB_DIR = APP_DIR / "web"
DATA_DIR = APP_DIR / "data"
HISTORY_FILE = DATA_DIR / "release_history.json"
HISTORY_LIMIT = 500
HOST = "0.0.0.0"
PORT = 8765

jobs = {}
jobs_lock = threading.Lock()
history_lock = threading.Lock()
current_job_id = None


@dataclass
class Job:
    id: str
    project_name: str
    env_name: str
    mode: str
    steps: list
    replica_names: list | None = None
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
    started_at: str = ""
    completed_at: str = ""
    duration_seconds: float = 0
    started_timestamp: float = field(default=0, repr=False, compare=False)
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
            "replica_names": self.replica_names,
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
            "started_at": self.started_at,
            "completed_at": self.completed_at,
            "duration_seconds": self.duration_seconds,
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


def mode_title(mode):
    return {
        "build": "只打包",
        "deploy": "上传现有包",
        "upload": "打包上传",
        "start": "启动服务",
        "full": "上传并启动",
    }.get(mode, mode)


def load_history_records():
    if not HISTORY_FILE.exists():
        return []
    try:
        data = json.loads(HISTORY_FILE.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return []
    return data if isinstance(data, list) else []


def save_history_records(records):
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    HISTORY_FILE.write_text(json.dumps(records[:HISTORY_LIMIT], ensure_ascii=False, indent=2), encoding="utf-8")


def job_history_record(job):
    return {
        "id": job.id,
        "project_name": job.project_name,
        "env_name": job.env_name,
        "mode": job.mode,
        "mode_title": mode_title(job.mode),
        "replica_names": job.replica_names or [],
        "status": job.status,
        "active_title": job.active_title,
        "error": job.error,
        "log_file": job.log_file,
        "created_at": job.created_at,
        "started_at": job.started_at,
        "completed_at": job.completed_at,
        "duration_seconds": round(float(job.duration_seconds or 0), 1),
    }


def append_history_record(record):
    with history_lock:
        records = load_history_records()
        records = [item for item in records if item.get("id") != record.get("id")]
        records.insert(0, record)
        save_history_records(records)


def list_history_records(query):
    with history_lock:
        records = load_history_records()
    project_name = (query.get("project_name") or [""])[0].strip()
    env_name = (query.get("env_name") or [""])[0].strip()
    status = (query.get("status") or [""])[0].strip()
    limit_text = (query.get("limit") or ["100"])[0]
    try:
        limit = max(1, min(int(limit_text), HISTORY_LIMIT))
    except ValueError:
        limit = 100
    if project_name:
        records = [item for item in records if item.get("project_name") == project_name]
    if env_name:
        records = [item for item in records if item.get("env_name") == env_name]
    if status:
        records = [item for item in records if item.get("status") == status]
    return records[:limit]


def read_history_log(record_id):
    if not record_id:
        raise ReleaseError("记录 ID 不能为空")
    with history_lock:
        records = load_history_records()
    record = next((item for item in records if item.get("id") == record_id), None)
    if not record:
        raise ReleaseError("发布记录不存在")
    log_file = str(record.get("log_file") or "").strip()
    if not log_file:
        return {"record": record, "content": ""}
    path = Path(log_file).resolve()
    log_root = LOG_DIR.resolve()
    if path != log_root and log_root not in path.parents:
        raise ReleaseError("只能读取发布日志目录内的文件")
    if not path.exists():
        return {"record": record, "content": "", "missing": True}
    return {"record": record, "content": path.read_text(encoding="utf-8", errors="replace")}


def notification_config(data):
    config = data.get("notification") if isinstance(data, dict) else None
    return config if isinstance(config, dict) else {}


def notification_enabled_for_status(config, status):
    if not config.get("enabled"):
        return False
    if status == "success":
        return bool(config.get("notify_on_success"))
    if status == "failed":
        return bool(config.get("notify_on_failure"))
    if status == "cancelled":
        return bool(config.get("notify_on_cancelled"))
    return False


def feishu_sign(secret, timestamp):
    string_to_sign = f"{timestamp}\n{secret}"
    digest = hmac.new(string_to_sign.encode("utf-8"), digestmod=hashlib.sha256).digest()
    return base64.b64encode(digest).decode("utf-8")


def post_json(url, payload, timeout=10):
    body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    request = Request(url, data=body, headers={"Content-Type": "application/json; charset=utf-8"}, method="POST")
    try:
        with urlopen(request, timeout=timeout) as response:
            text = response.read().decode("utf-8", errors="replace")
    except HTTPError as exc:
        detail = exc.read().decode("utf-8", errors="replace")
        raise ReleaseError(f"通知发送失败，HTTP {exc.code}: {detail}") from exc
    except URLError as exc:
        raise ReleaseError(f"通知发送失败: {exc}") from exc
    try:
        return json.loads(text) if text else {}
    except json.JSONDecodeError:
        return {"raw": text}


def send_feishu_notification(config, text):
    webhook_url = str(config.get("webhook_url") or "").strip()
    if not webhook_url:
        raise ReleaseError("飞书 Webhook 地址不能为空")
    payload = {
        "msg_type": "text",
        "content": {"text": text},
    }
    secret = str(config.get("secret") or "").strip()
    if secret:
        timestamp = str(int(time.time()))
        payload["timestamp"] = timestamp
        payload["sign"] = feishu_sign(secret, timestamp)
    result = post_json(webhook_url, payload)
    code = result.get("StatusCode", result.get("code", 0))
    if code not in (0, "0", None):
        message = result.get("StatusMessage") or result.get("msg") or result.get("message") or result
        raise ReleaseError(f"飞书通知返回失败: {message}")
    return result


def build_notification_text(record, title="Release Harbor 发布通知"):
    status_text = {
        "success": "成功",
        "failed": "失败",
        "cancelled": "已停止",
    }.get(record.get("status"), record.get("status") or "-")
    replicas = record.get("replica_names") or []
    lines = [
        title,
        f"状态: {status_text}",
        f"项目: {record.get('project_name') or '-'}",
        f"环境: {record.get('env_name') or '-'}",
        f"动作: {record.get('mode_title') or record.get('mode') or '-'}",
        f"副本: {', '.join(replicas) if replicas else '默认目标'}",
        f"耗时: {record.get('duration_seconds') or 0}s",
        f"完成时间: {record.get('completed_at') or '-'}",
        f"日志: {record.get('log_file') or '-'}",
    ]
    if record.get("error"):
        lines.append(f"错误: {record.get('error')}")
    return "\n".join(lines)


def send_notification(config, text):
    provider = str(config.get("provider") or "feishu").strip()
    if provider != "feishu":
        raise ReleaseError(f"暂不支持的通知渠道: {provider}")
    return send_feishu_notification(config, text)


def notify_job_finished(config_file, record, logger=None):
    config = notification_config(config_file.data)
    if not notification_enabled_for_status(config, record.get("status")):
        return
    try:
        send_notification(config, build_notification_text(record))
        if logger:
            logger.write("飞书通知已发送")
    except Exception as exc:
        if logger:
            logger.write(f"飞书通知发送失败: {exc}")


def send_test_notification(payload):
    data = payload_to_config(payload) if "config" in payload or "projects" in payload else load_public_config()[1]
    config = notification_config(data)
    if not config.get("webhook_url"):
        raise ReleaseError("请先填写飞书 Webhook 地址")
    text = "\n".join([
        "Release Harbor 测试通知",
        "状态: 测试",
        "说明: 如果你能看到这条消息，说明飞书机器人配置可用。",
        f"发送时间: {datetime.now().isoformat(timespec='seconds')}",
    ])
    result = send_notification(config, text)
    return {"message": "测试通知已发送", "result": result}


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
        env_configs = project.get("environment_configs")
        if isinstance(env_configs, dict):
            for env_config in env_configs.values():
                if isinstance(env_config, dict) and isinstance(env_config.get("deploy"), dict):
                    yield env_config["deploy"]
                if isinstance(env_config, dict) and isinstance(env_config.get("replicas"), list):
                    for replica in env_config["replicas"]:
                        if isinstance(replica, dict) and isinstance(replica.get("deploy"), dict):
                            yield replica["deploy"]


def payload_to_config(payload):
    data = payload.get("config") if isinstance(payload, dict) and "config" in payload else payload
    if not isinstance(data, dict):
        raise ReleaseError("配置必须是 JSON 对象")
    if "projects" not in data and isinstance(data.get("state"), dict):
        data = data["state"]
    if "projects" not in data:
        raise ReleaseError("配置缺少 projects")
    clean = {"projects": data.get("projects")}
    if isinstance(data.get("notification"), dict):
        clean["notification"] = data.get("notification")
    return sanitize_config(clean)


def merged_config_for_validation(data):
    merged = copy.deepcopy(data)
    release_harbor.merge_secret_config(merged)
    return ConfigFile(LOCAL_CONFIG, merged, False)


def parse_replica_names(payload):
    names = payload.get("replica_names") if isinstance(payload, dict) else None
    if names is None:
        return None
    if not isinstance(names, list):
        raise ReleaseError("replica_names 必须是数组")
    return [str(name).strip() for name in names if str(name).strip()]


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


def run_log_command(payload):
    project_name = str(payload.get("project_name") or "").strip()
    env_name = str(payload.get("env_name") or "").strip()
    replica_name = str(payload.get("replica_name") or "").strip()
    command = str(payload.get("command") or "").strip()
    work_dir = payload.get("work_dir")
    if not project_name:
        raise ReleaseError("project_name 不能为空")
    if not env_name:
        raise ReleaseError("env_name 不能为空")
    if not command:
        raise ReleaseError("日志查看命令不能为空")

    data = payload_to_config(payload) if "config" in payload or "projects" in payload else load_public_config()[1]
    config_file = merged_config_for_validation(data)
    projects = {item.get("name"): item for item in config_file.data.get("projects", []) or []}
    project = projects.get(project_name)
    if not project:
        raise ReleaseError(f"找不到项目: {project_name}")

    replicas = release_harbor.get_project_replicas(project, env_name)
    if replicas and not replica_name:
        raise ReleaseError("请选择副本")
    target_configs = release_harbor.get_deploy_targets(
        project,
        config_file.data,
        env_name,
        [replica_name] if replica_name else None,
        require_selection=bool(replicas),
    )
    target_config = target_configs[0] if target_configs else None
    return release_harbor.run_remote_log_command(
        config_file,
        project,
        command,
        env_name=env_name,
        target_config=target_config,
        work_dir_override=work_dir,
    )


def resolve_single_deploy_target(payload):
    project_name = str(payload.get("project_name") or "").strip()
    env_name = str(payload.get("env_name") or "").strip()
    replica_name = str(payload.get("replica_name") or "").strip()
    if not project_name:
        raise ReleaseError("project_name 不能为空")
    if not env_name:
        raise ReleaseError("env_name 不能为空")

    data = payload_to_config(payload) if "config" in payload or "projects" in payload else load_public_config()[1]
    config_file = merged_config_for_validation(data)
    projects = {item.get("name"): item for item in config_file.data.get("projects", []) or []}
    project = projects.get(project_name)
    if not project:
        raise ReleaseError(f"找不到项目: {project_name}")

    replicas = release_harbor.get_project_replicas(project, env_name)
    if replicas and not replica_name:
        raise ReleaseError("请选择副本")
    target_configs = release_harbor.get_deploy_targets(
        project,
        config_file.data,
        env_name,
        [replica_name] if replica_name else None,
        require_selection=bool(replicas),
    )
    target_config = target_configs[0] if target_configs else None
    return config_file, project, env_name, target_config


def list_backups(payload):
    config_file, project, env_name, target_config = resolve_single_deploy_target(payload)
    return release_harbor.list_remote_backups(config_file, project, env_name=env_name, target_config=target_config)


def rollback_backup(payload):
    backup_path = str(payload.get("backup_path") or payload.get("backup_name") or "").strip()
    restart = bool(payload.get("restart"))
    if not backup_path:
        raise ReleaseError("backup_path 不能为空")
    config_file, project, env_name, target_config = resolve_single_deploy_target(payload)
    logger = release_harbor.Logger() if restart else None
    return release_harbor.rollback_remote_backup(
        config_file,
        project,
        backup_path,
        env_name=env_name,
        target_config=target_config,
        restart=restart,
        logger=logger,
    )


def run_preflight(payload):
    project_name = str(payload.get("project_name") or "").strip()
    env_name = str(payload.get("env_name") or "").strip()
    replica_names = parse_replica_names(payload)
    if not project_name:
        raise ReleaseError("project_name 不能为空")
    if not env_name:
        raise ReleaseError("env_name 不能为空")

    data = payload_to_config(payload) if "config" in payload or "projects" in payload else load_public_config()[1]
    config_file = merged_config_for_validation(data)
    projects = {item.get("name"): item for item in config_file.data.get("projects", []) or []}
    project = projects.get(project_name)
    if not project:
        raise ReleaseError(f"找不到项目: {project_name}")
    return release_harbor.run_preflight_checks(config_file, project, env_name=env_name, replica_names=replica_names)


def server_url(host, port):
    return f"http://{host}:{port}/"

def server_display_url(host, port):
    if host == "0.0.0.0":
        return f"http://127.0.0.1:{port}/"
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
    replica_names = parse_replica_names(payload)
    if mode not in release_harbor.JOB_STEPS:
        raise ReleaseError(f"不支持的执行模式: {mode}")
    if not project_name:
        raise ReleaseError("project_name 不能为空")
    if not env_name:
        raise ReleaseError("env_name 不能为空")

    config_file = release_harbor.load_config()
    errors = release_harbor.validate_config(
        config_file,
        mode=mode,
        project_name=project_name,
        env_name=env_name,
        replica_names=replica_names,
        require_replica_selection=mode in ("deploy", "upload", "start", "full"),
    )
    if errors:
        raise ReleaseError("; ".join(errors))

    with jobs_lock:
        if current_job_id and jobs.get(current_job_id) and jobs[current_job_id].status == "running":
            raise ReleaseError("已有任务正在执行，请等待完成")
        job_id = datetime.now().strftime("%Y%m%d%H%M%S%f")
        job = Job(job_id, project_name, env_name, mode, release_harbor.get_job_steps(mode), replica_names=replica_names)
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
        job.started_at = datetime.now().isoformat(timespec="seconds")
        job.started_timestamp = time.time()
        job.set_stage("validate", "校验配置")
    logger = JobLogger(job)
    config_file = None
    try:
        logger.write(f"日志文件: {logger.path}")
        config_file = release_harbor.load_config()
        release_harbor.execute_release(config_file, job.project_name, job.env_name, logger, mode=job.mode, replica_names=job.replica_names)
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
        history_record = None
        with jobs_lock:
            job.current_process = None
            job.current_process_id = None
            job.completed_at = datetime.now().isoformat(timespec="seconds")
            if job.started_timestamp:
                job.duration_seconds = time.time() - job.started_timestamp
            if current_job_id == job_id:
                current_job_id = None
            history_record = job_history_record(job)
        if history_record:
            append_history_record(history_record)
            if config_file:
                notify_job_finished(config_file, history_record, logger)


class ReleaseWebHandler(BaseHTTPRequestHandler):
    server_version = "ReleaseSenderHTTP/1.0"

    def do_GET(self):
        parsed = urlparse(self.path)
        path = parsed.path
        query = parse_qs(parsed.query)
        try:
            if path == "/api/config":
                source, data = load_public_config()
                send_json(self, {"ok": True, "source": str(source), "config": data})
                return
            if path == "/api/history":
                records = list_history_records(query)
                send_json(self, {"ok": True, "records": records})
                return
            if path == "/api/history/log":
                result = read_history_log((query.get("id") or [""])[0])
                send_json(self, {"ok": True, "result": result})
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
                project_name = str(payload.get("project_name") or "").strip() or None
                env_name = str(payload.get("env_name") or "").strip() or None
                replica_names = parse_replica_names(payload)
                data = payload_to_config(payload) if "config" in payload or "projects" in payload else load_public_config()[1]
                config_file = merged_config_for_validation(data)
                errors = release_harbor.validate_config(config_file, mode=mode, project_name=project_name, env_name=env_name, replica_names=replica_names)
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
            if path == "/api/logs/command":
                result = run_log_command(payload)
                send_json(self, {"ok": True, "result": result})
                return
            if path == "/api/backups/list":
                result = list_backups(payload)
                send_json(self, {"ok": True, "result": result})
                return
            if path == "/api/backups/rollback":
                result = rollback_backup(payload)
                send_json(self, {"ok": True, "result": result})
                return
            if path == "/api/preflight":
                result = run_preflight(payload)
                send_json(self, {"ok": True, "result": result})
                return
            if path == "/api/notifications/test":
                result = send_test_notification(payload)
                send_json(self, {"ok": True, "result": result})
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
    parser.add_argument("--host", default=HOST, help="监听地址，默认 0.0.0.0（所有接口）")
    parser.add_argument("--port", type=int, default=PORT, help="监听端口，默认 8765")
    parser.add_argument("--open-browser", action="store_true", help="启动后自动打开浏览器")
    args = parser.parse_args()
    url = server_url(args.host, args.port)
    display = server_display_url(args.host, args.port)
    if args.open_browser and existing_server_is_alive(args.host, args.port):
        print(f"Release Harbor 发布港已在运行: {display}")
        webbrowser.open(display)
        return 0
    try:
        server = ThreadingHTTPServer((args.host, args.port), ReleaseWebHandler)
    except OSError:
        if existing_server_is_alive(args.host, args.port):
            print(f"Release Harbor 发布港已在运行: {display}")
            if args.open_browser:
                webbrowser.open(display)
            return 0
        raise
    print(f"Release Harbor 发布港已启动: {display}")
    if args.open_browser:
        open_browser_later(display)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\n正在停止服务...")
    finally:
        server.server_close()
    return 0



if __name__ == "__main__":
    sys.exit(main())

