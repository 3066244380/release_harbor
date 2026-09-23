import argparse
import json
import os
import queue
import re
import shlex
import shutil
import subprocess
import sys
import tempfile
import threading
import time
import traceback
import uuid
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path, PurePosixPath
from tkinter import BOTH, DISABLED, END, LEFT, NORMAL, RIGHT, W, Button, Entry, Frame, Label, Listbox, Scrollbar, StringVar, Tk, Text, messagebox, ttk


APP_DIR = Path(__file__).resolve().parent
LOCAL_CONFIG = APP_DIR / "config.local.json"
EXAMPLE_CONFIG = APP_DIR / "config.example.json"
SECRET_CONFIG = APP_DIR / "config.secret.json"
LOG_DIR = APP_DIR / "logs"
MAX_REMOTE_LOG_OUTPUT = 200 * 1024
DEFAULT_BACKUP_KEEP = 5
REMOTE_COMMAND_TIMEOUTS = {
    "stop": 120,
    "start": 60,
    "status": 30,
}


class ReleaseError(Exception):
    pass


@dataclass
class ConfigFile:
    path: Path
    data: dict
    using_example: bool


class Logger:
    def __init__(self, ui_queue=None):
        LOG_DIR.mkdir(parents=True, exist_ok=True)
        stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        self.path = LOG_DIR / f"release_{stamp}.log"
        self.ui_queue = ui_queue

    def write(self, message):
        line = f"[{datetime.now().strftime('%H:%M:%S')}] {message}"
        with self.path.open("a", encoding="utf-8") as log_file:
            log_file.write(line + "\n")
        if self.ui_queue:
            self.ui_queue.put(("log", line))
        else:
            print(line)


def read_json_file(path):
    with path.open("r", encoding="utf-8-sig") as file:
        return json.load(file)


def merge_secret_config(data):
    if not SECRET_CONFIG.exists():
        return
    secret_data = read_json_file(SECRET_CONFIG)
    global_deploy_secret = secret_data.get("deploy")
    if isinstance(global_deploy_secret, dict):
        data.setdefault("deploy", {}).update(global_deploy_secret)
        for project in data.get("projects", []):
            project.setdefault("deploy", {}).update(global_deploy_secret)
    secret_projects = secret_data.get("projects")
    if isinstance(secret_projects, list):
        projects_by_name = {project.get("name"): project for project in data.get("projects", [])}
        for secret_project in secret_projects:
            target = projects_by_name.get(secret_project.get("name"))
            secret_deploy = secret_project.get("deploy")
            if target is not None and isinstance(secret_deploy, dict):
                target.setdefault("deploy", {}).update(secret_deploy)


def load_config():
    config_path = LOCAL_CONFIG if LOCAL_CONFIG.exists() else EXAMPLE_CONFIG
    if not config_path.exists():
        raise ReleaseError("找不到 config.local.json 或 config.example.json")
    data = read_json_file(config_path)
    merge_secret_config(data)
    return ConfigFile(config_path, data, config_path == EXAMPLE_CONFIG)


def resolve_path(base_file, raw_path):
    path = Path(str(raw_path)).expanduser()
    if path.is_absolute():
        return path.resolve()
    return (base_file.parent / path).resolve()


def command_path(value):
    if value:
        path = Path(str(value)).expanduser()
        return str(path) if path.exists() else str(value)
    return None


def default_ssh_path():
    return shutil.which("ssh") or r"C:\Windows\System32\OpenSSH\ssh.exe"


def default_scp_path():
    return shutil.which("scp") or r"C:\Windows\System32\OpenSSH\scp.exe"


def default_plink_path():
    return shutil.which("plink") or r"D:\tools\putty\plink.exe"


def default_pscp_path():
    return shutil.which("pscp") or r"D:\tools\putty\pscp.exe"


def merge_environment_config(base, override):
    merged = {}
    if isinstance(base, dict):
        merged.update(base)
    if isinstance(override, dict):
        merged.update(override)
    return merged


def get_project_env_config(project, env_name=None):
    env = env_name or project.get("default_environment")
    env_configs = project.get("environment_configs") or {}
    if not env or not isinstance(env_configs, dict):
        return {}
    env_config = env_configs.get(env) or {}
    return env_config if isinstance(env_config, dict) else {}


def get_project_deploy(project, data, env_name=None):
    base = merge_environment_config(data.get("deploy"), project.get("deploy"))
    env_deploy = get_project_env_config(project, env_name).get("deploy")
    return merge_environment_config(base, env_deploy)


def get_project_service(project, env_name=None, replica=None):
    service = project.get("service") or {}
    env_service = get_project_env_config(project, env_name).get("service")
    merged = merge_environment_config(service, env_service)
    replica_service = replica.get("service") if isinstance(replica, dict) else None
    return merge_environment_config(merged, replica_service)


def get_project_log(project, env_name=None, replica=None):
    log = project.get("log") or {}
    env_log = get_project_env_config(project, env_name).get("log")
    merged = merge_environment_config(log, env_log)
    replica_log = replica.get("log") if isinstance(replica, dict) else None
    return merge_environment_config(merged, replica_log)


def get_project_replicas(project, env_name=None):
    replicas = get_project_env_config(project, env_name).get("replicas") or []
    return replicas if isinstance(replicas, list) else []


def selected_replica_names(replica_names):
    if replica_names is None:
        return None
    return [str(name).strip() for name in replica_names if str(name).strip()]


def get_deploy_targets(project, data, env_name=None, replica_names=None, require_selection=False):
    base = get_project_deploy(project, data, env_name)
    replicas = get_project_replicas(project, env_name)
    names = selected_replica_names(replica_names)
    if not replicas:
        return [{"name": "", "deploy": base, "replica": None}]
    if require_selection and not names:
        raise ReleaseError("请选择至少一个副本")

    targets = []
    wanted = set(names) if names is not None else None
    seen = set()
    for replica in replicas:
        if not isinstance(replica, dict):
            continue
        name = str(replica.get("name") or "").strip()
        if not name:
            continue
        if wanted is not None and name not in wanted:
            continue
        seen.add(name)
        deploy = merge_environment_config(base, replica.get("deploy") or {})
        targets.append({"name": name, "deploy": deploy, "replica": replica})

    if wanted is not None:
        missing = sorted(wanted - seen)
        if missing:
            raise ReleaseError(f"找不到副本: {', '.join(missing)}")
    if require_selection and not targets:
        raise ReleaseError("请选择至少一个副本")
    return targets


def deploy_auth_type(deploy):
    return str(deploy.get("auth_type", "key")).strip().lower()


def get_deploy_password(deploy):
    return deploy.get("password") or os.getenv(deploy.get("password_env", "DEPLOY_PASSWORD"))


def backup_keep_count(deploy):
    if "backup_keep" not in deploy:
        return DEFAULT_BACKUP_KEEP
    value = deploy.get("backup_keep")
    if value in (None, ""):
        return 0
    try:
        keep = int(value)
    except (TypeError, ValueError):
        return DEFAULT_BACKUP_KEEP
    return max(keep, 0)


def validate_backup_keep(deploy, deploy_prefix, errors):
    if "backup_keep" not in deploy or deploy.get("backup_keep") in (None, ""):
        return
    try:
        keep = int(deploy.get("backup_keep"))
    except (TypeError, ValueError):
        errors.append(f"{deploy_prefix}.backup_keep 必须是大于等于 0 的整数")
        return
    if keep < 0:
        errors.append(f"{deploy_prefix}.backup_keep 必须是大于等于 0 的整数")


def validate_non_negative_int(value, field_name, errors):
    if value in (None, ""):
        return
    try:
        if int(value) < 0:
            errors.append(f"{field_name} 不能小于 0")
    except (TypeError, ValueError):
        errors.append(f"{field_name} 必须是数字")


JOB_STEPS = {
    "build": [
        {"key": "validate", "title": "校验配置"},
        {"key": "env", "title": "切换环境"},
        {"key": "build", "title": "打包"},
        {"key": "done", "title": "完成"},
    ],
    "deploy": [
        {"key": "validate", "title": "校验配置"},
        {"key": "backup", "title": "备份旧包"},
        {"key": "upload", "title": "上传新包"},
        {"key": "done", "title": "完成"},
    ],
    "upload": [
        {"key": "validate", "title": "校验配置"},
        {"key": "env", "title": "切换环境"},
        {"key": "build", "title": "打包"},
        {"key": "backup", "title": "备份旧包"},
        {"key": "upload", "title": "上传新包"},
        {"key": "done", "title": "完成"},
    ],
    "start": [
        {"key": "validate", "title": "校验配置"},
        {"key": "stop", "title": "优雅停机"},
        {"key": "start", "title": "启动服务"},
        {"key": "wait", "title": "等待启动"},
        {"key": "status", "title": "状态检查"},
        {"key": "health", "title": "健康检查"},
        {"key": "done", "title": "完成"},
    ],
    "full": [
        {"key": "validate", "title": "校验配置"},
        {"key": "env", "title": "切换环境"},
        {"key": "build", "title": "打包"},
        {"key": "backup", "title": "备份旧包"},
        {"key": "upload", "title": "上传新包"},
        {"key": "stop", "title": "优雅停机"},
        {"key": "start", "title": "启动服务"},
        {"key": "wait", "title": "等待启动"},
        {"key": "status", "title": "状态检查"},
        {"key": "health", "title": "健康检查"},
        {"key": "done", "title": "完成"},
    ],
}


def get_job_steps(mode):
    return JOB_STEPS.get(mode, JOB_STEPS["upload"])


def set_stage(logger, key, title=None):
    if hasattr(logger, "set_stage"):
        logger.set_stage(key, title)


def effective_env_name(project, env_name=None):
    if env_name:
        return env_name
    environments = project.get("environments") or []
    return project.get("default_environment") or (environments[0] if environments else None)


def env_config_prefix(prefix, project, env_name, key):
    env_config = get_project_env_config(project, env_name)
    if env_name and isinstance(env_config.get(key), dict):
        return f"{prefix}.environment_configs.{env_name}.{key}"
    return f"{prefix}.{key}"


def deploy_target_prefix(prefix, active_env, target, fallback_prefix):
    if target.get("name"):
        return f"{prefix}.environment_configs.{active_env}.replicas[{target['name']}].deploy"
    return fallback_prefix


def service_target_prefix(prefix, active_env, target, fallback_prefix):
    if target.get("name"):
        return f"{prefix}.environment_configs.{active_env}.replicas[{target['name']}].service"
    return fallback_prefix


def validate_deploy_block(config_file, deploy, deploy_prefix, errors):
    for key in ["host", "user", "remote_dir"]:
        if not deploy.get(key):
            errors.append(f"{deploy_prefix}.{key} 不能为空")
    auth_type = deploy_auth_type(deploy)
    if auth_type == "password":
        if not get_deploy_password(deploy):
            errors.append(f"{deploy_prefix}.password/password_env 不能为空")
        plink_path = command_path(deploy.get("plink_path")) or default_plink_path()
        pscp_path = command_path(deploy.get("pscp_path")) or default_pscp_path()
        if not Path(plink_path).exists():
            errors.append(f"{deploy_prefix}.plink_path 不存在: {plink_path}")
        if not Path(pscp_path).exists():
            errors.append(f"{deploy_prefix}.pscp_path 不存在: {pscp_path}")
    elif auth_type == "key":
        if not deploy.get("private_key"):
            errors.append(f"{deploy_prefix}.private_key 不能为空")
        else:
            key_path = resolve_path(config_file.path, deploy["private_key"])
            if not key_path.exists():
                errors.append(f"{deploy_prefix}.private_key 不存在: {key_path}")
        ssh_path = command_path(deploy.get("ssh_path")) or default_ssh_path()
        scp_path = command_path(deploy.get("scp_path")) or default_scp_path()
        if not Path(ssh_path).exists():
            errors.append(f"{deploy_prefix}.ssh_path 不存在: {ssh_path}")
        if not Path(scp_path).exists():
            errors.append(f"{deploy_prefix}.scp_path 不存在: {scp_path}")
    else:
        errors.append(f"{deploy_prefix}.auth_type 只支持 password 或 key")
    validate_backup_keep(deploy, deploy_prefix, errors)


def validate_config(config_file, mode="upload", project_name=None, env_name=None, replica_names=None, require_replica_selection=False):
    data = config_file.data
    errors = []
    if not isinstance(data.get("projects"), list) or not data["projects"]:
        errors.append("projects 至少需要配置一个项目")

    for index, project in enumerate(data.get("projects", []), start=1):
        if project_name and project.get("name") != project_name:
            continue
        prefix = f"projects[{index}]"
        active_env = effective_env_name(project, env_name)
        if not project.get("name"):
            errors.append(f"{prefix}.name 不能为空")
        if not project.get("path"):
            errors.append(f"{prefix}.path 不能为空")
            continue
        project_path = resolve_path(config_file.path, project["path"])
        if not project_path.exists():
            errors.append(f"{prefix}.path 不存在: {project_path}")
        if not project.get("build_command"):
            errors.append(f"{prefix}.build_command 不能为空")
        if not project.get("artifact"):
            errors.append(f"{prefix}.artifact 不能为空")
        for replacement in project.get("environment_replacements", []):
            target = project_path / replacement.get("file", "")
            if not replacement.get("file"):
                errors.append(f"{prefix}.environment_replacements.file 不能为空")
            elif not target.exists():
                errors.append(f"{prefix}.environment_replacements.file 不存在: {target}")
            if not replacement.get("regex"):
                errors.append(f"{prefix}.environment_replacements.regex 不能为空")
            if "replacement" not in replacement:
                errors.append(f"{prefix}.environment_replacements.replacement 不能为空")

        if mode in ("deploy", "upload", "start", "full"):
            try:
                deploy_targets = get_deploy_targets(project, data, active_env, replica_names, require_replica_selection)
            except ReleaseError as exc:
                errors.append(str(exc))
                deploy_targets = []
            if not deploy_targets:
                continue
            deploy = deploy_targets[0]["deploy"]
            deploy_prefix = deploy_target_prefix(prefix, active_env, deploy_targets[0], env_config_prefix(prefix, project, active_env, "deploy"))
            for key in ["host", "user", "remote_dir"]:
                if not deploy.get(key):
                    errors.append(f"{deploy_prefix}.{key} 不能为空")
            auth_type = deploy_auth_type(deploy)
            if auth_type == "password":
                if not get_deploy_password(deploy):
                    errors.append(f"{deploy_prefix}.password/password_env 不能为空")
                plink_path = command_path(deploy.get("plink_path")) or default_plink_path()
                pscp_path = command_path(deploy.get("pscp_path")) or default_pscp_path()
                if not Path(plink_path).exists():
                    errors.append(f"{deploy_prefix}.plink_path 不存在: {plink_path}")
                if not Path(pscp_path).exists():
                    errors.append(f"{deploy_prefix}.pscp_path 不存在: {pscp_path}")
            elif auth_type == "key":
                if not deploy.get("private_key"):
                    errors.append(f"{deploy_prefix}.private_key 不能为空")
                else:
                    key_path = resolve_path(config_file.path, deploy["private_key"])
                    if not key_path.exists():
                        errors.append(f"{deploy_prefix}.private_key 不存在: {key_path}")
                ssh_path = command_path(deploy.get("ssh_path")) or default_ssh_path()
                scp_path = command_path(deploy.get("scp_path")) or default_scp_path()
                if not Path(ssh_path).exists():
                    errors.append(f"{deploy_prefix}.ssh_path 不存在: {ssh_path}")
                if not Path(scp_path).exists():
                    errors.append(f"{deploy_prefix}.scp_path 不存在: {scp_path}")
            else:
                errors.append(f"{deploy_prefix}.auth_type 只支持 password 或 key")
            validate_backup_keep(deploy, deploy_prefix, errors)

            for target in deploy_targets[1:]:
                target_prefix = deploy_target_prefix(prefix, active_env, target, env_config_prefix(prefix, project, active_env, "deploy"))
                validate_deploy_block(config_file, target["deploy"], target_prefix, errors)

        if mode in ("start", "full"):
            for target in deploy_targets:
                service = get_project_service(project, active_env, target.get("replica"))
                service_prefix = service_target_prefix(prefix, active_env, target, env_config_prefix(prefix, project, active_env, "service"))
                if not service.get("stop_command"):
                    errors.append(f"{service_prefix}.stop_command 不能为空")
                if not service.get("start_command"):
                    errors.append(f"{service_prefix}.start_command 不能为空")
                wait_seconds = service.get("startup_wait_seconds", 3)
                try:
                    if int(wait_seconds) < 0:
                        errors.append(f"{service_prefix}.startup_wait_seconds 不能小于 0")
                except (TypeError, ValueError):
                    errors.append(f"{service_prefix}.startup_wait_seconds 必须是数字")
                if service.get("health_check_url"):
                    url = str(service.get("health_check_url") or "").strip()
                    if not (url.startswith("http://") or url.startswith("https://")):
                        errors.append(f"{service_prefix}.health_check_url 只支持 http:// 或 https://")
                    validate_non_negative_int(service.get("health_check_retries", 5), f"{service_prefix}.health_check_retries", errors)
                    validate_non_negative_int(service.get("health_check_interval_seconds", 3), f"{service_prefix}.health_check_interval_seconds", errors)
                    validate_non_negative_int(service.get("health_check_timeout_seconds", 5), f"{service_prefix}.health_check_timeout_seconds", errors)
    return errors


def apply_environment(project_path, replacements, env_name, logger):
    backups = []
    try:
        for replacement in replacements:
            target = project_path / replacement["file"]
            encoding = replacement.get("encoding", "utf-8")
            original = target.read_text(encoding=encoding)
            backup_path = Path(tempfile.gettempdir()) / f"release_harbor_{uuid.uuid4().hex}.bak"
            backup_path.write_text(original, encoding="utf-8")
            backups.append((target, backup_path, encoding))
            repl = replacement["replacement"].replace("{env}", env_name)
            updated, count = re.subn(replacement["regex"], repl, original)
            if count == 0:
                raise ReleaseError(f"环境替换未匹配到内容: {target}")
            target.write_text(updated, encoding=encoding)
            logger.write(f"已临时切换环境配置: {target} -> {env_name}")
        return backups
    except Exception:
        restore_backups(backups, logger)
        raise


def restore_backups(backups, logger):
    for target, backup_path, encoding in backups:
        if backup_path.exists():
            target.write_text(backup_path.read_text(encoding="utf-8"), encoding=encoding)
            backup_path.unlink(missing_ok=True)
            logger.write(f"已恢复环境配置: {target}")


def format_command(command):
    masked = []
    hide_next = False
    for item in command:
        text = str(item)
        if hide_next:
            masked.append("******")
            hide_next = False
            continue
        masked.append(text)
        if text in ("-pw", "--password"):
            hide_next = True
    return " ".join(masked)


def logger_cancel_requested(logger):
    checker = getattr(logger, "is_cancel_requested", None)
    return bool(checker and checker())


def terminate_process_tree(process):
    if process.poll() is not None:
        return
    if os.name == "nt":
        subprocess.run(["taskkill", "/PID", str(process.pid), "/T", "/F"], capture_output=True, text=True)
    else:
        process.kill()


def run_process(command, logger, cwd=None, encoding="gbk", timeout=None):
    if logger_cancel_requested(logger):
        raise ReleaseError("任务已取消")
    logger.write(f"执行命令: {format_command(command)}")
    process = subprocess.Popen(
        [str(item) for item in command],
        cwd=str(cwd) if cwd else None,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        encoding=encoding,
        errors="replace",
    )
    set_process = getattr(logger, "set_process", None)
    clear_process = getattr(logger, "clear_process", None)
    if set_process:
        set_process(process)
    output_queue = queue.Queue()

    def read_output():
        try:
            for line in process.stdout:
                output_queue.put(line)
        finally:
            output_queue.put(None)

    reader = threading.Thread(target=read_output, daemon=True)
    reader.start()
    start_time = time.monotonic()
    reader_done = False
    timed_out = False
    code = None
    try:
        while True:
            try:
                item = output_queue.get(timeout=0.1)
                if item is None:
                    reader_done = True
                else:
                    logger.write(item.rstrip())
            except queue.Empty:
                pass

            code = process.poll()
            if code is not None and reader_done:
                break

            if timeout is not None and time.monotonic() - start_time >= timeout:
                timed_out = True
                logger.write(f"命令执行超时（{timeout}s），正在终止进程")
                terminate_process_tree(process)
                try:
                    code = process.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    code = process.poll()
                while True:
                    try:
                        item = output_queue.get_nowait()
                    except queue.Empty:
                        break
                    if item is not None:
                        logger.write(item.rstrip())
                break
    finally:
        if clear_process:
            clear_process(process)
    if timed_out:
        raise ReleaseError(f"命令执行超时（{timeout}s）")
    if logger_cancel_requested(logger):
        raise ReleaseError("任务已取消")
    if code != 0:
        raise ReleaseError(f"命令执行失败，退出码: {code}")


def run_process_capture(command, encoding="utf-8", timeout=30, max_output_chars=MAX_REMOTE_LOG_OUTPUT):
    try:
        completed = subprocess.run(
            [str(item) for item in command],
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            encoding=encoding,
            errors="replace",
            timeout=timeout,
        )
    except subprocess.TimeoutExpired as exc:
        output = exc.stdout or ""
        if isinstance(output, bytes):
            output = output.decode(encoding, errors="replace")
        if len(output) > max_output_chars:
            output = output[:max_output_chars]
        detail = f"\n{output.rstrip()}" if output else ""
        raise ReleaseError(f"命令执行超时（{timeout}s）{detail}") from exc
    output = completed.stdout or ""
    truncated = len(output) > max_output_chars
    if truncated:
        output = output[:max_output_chars] + "\n...（输出超过 200KB，已截断）"
    return {"exit_code": completed.returncode, "output": output, "truncated": truncated}


def run_build(project_path, command, logger):
    set_stage(logger, "build", "打包")
    logger.write(f"开始打包: {' '.join(command)}")
    run_process(command, logger, cwd=project_path, encoding="gbk")


def artifact_path(project_path, artifact):
    path = Path(artifact)
    if path.is_absolute():
        return path
    return project_path / path


def file_size_text(path):
    size = path.stat().st_size
    for unit in ["B", "KB", "MB", "GB"]:
        if size < 1024 or unit == "GB":
            return f"{size:.2f} {unit}" if unit != "B" else f"{size} B"
        size /= 1024
    return f"{size} B"


def sh_quote(value):
    return "'" + str(value).replace("'", "'\"'\"'") + "'"


def remote_join(remote_dir, filename):
    return remote_dir.rstrip("/") + "/" + filename.lstrip("/")


def remote_deploy_context(project, data, env_name=None, target_config=None):
    deploy = target_config["deploy"] if target_config else get_project_deploy(project, data, env_name)
    remote_dir = str(deploy.get("remote_dir", "")).rstrip("/")
    remote_filename = deploy.get("remote_filename") or Path(project.get("artifact", "")).name
    remote_path = remote_join(remote_dir, remote_filename) if remote_dir else remote_filename
    return deploy, remote_dir, remote_filename, remote_path


def run_remote_capture(config_file, deploy, command, timeout=30, check_exit=True):
    validate_log_deploy(config_file, deploy)
    host = deploy["host"]
    user = deploy["user"]
    port = str(deploy.get("port", 22))
    target = f"{user}@{host}"
    remote_command = "sh -lc " + sh_quote(command)
    if deploy_auth_type(deploy) == "password":
        password = get_deploy_password(deploy)
        plink_path = command_path(deploy.get("plink_path")) or default_plink_path()
        result = run_process_capture([plink_path, "-batch", "-ssh", "-P", port, "-pw", password, target, remote_command], encoding="utf-8", timeout=timeout)
    else:
        private_key = resolve_path(config_file.path, deploy["private_key"])
        ssh_path = command_path(deploy.get("ssh_path")) or default_ssh_path()
        result = run_process_capture([ssh_path, "-n", "-T", "-i", str(private_key), "-p", port, "-o", "BatchMode=yes", "-o", "StrictHostKeyChecking=accept-new", target, remote_command], encoding="utf-8", timeout=timeout)
    if check_exit and result["exit_code"] != 0:
        detail = f": {result['output'].strip()}" if result.get("output") else ""
        raise ReleaseError(f"远程命令执行失败，退出码: {result['exit_code']}{detail}")
    return target, result


def parse_remote_backup_line(line):
    parts = line.rstrip("\n").split("\t", 4)
    if len(parts) != 5:
        return None
    mtime_epoch, name, path, size, mtime_text = parts
    try:
        size_value = int(size)
    except ValueError:
        size_value = 0
    try:
        timestamp_value = float(mtime_epoch)
    except ValueError:
        timestamp_value = 0
    return {
        "name": name,
        "path": path,
        "size": size_value,
        "mtime": mtime_text.split(".")[0],
        "mtime_epoch": timestamp_value,
    }


def list_remote_backups(config_file, project, env_name=None, target_config=None):
    deploy, remote_dir, remote_filename, remote_path = remote_deploy_context(project, config_file.data, env_name, target_config)
    if not remote_dir:
        raise ReleaseError("服务器目录不能为空")
    backup_pattern = f"{remote_filename}.bak_*"
    printf_format = "%T@\\t%f\\t%p\\t%s\\t%TY-%Tm-%Td %TH:%TM:%TS\\n"
    command = (
        f"if [ ! -d {sh_quote(remote_dir)} ]; then echo __NO_REMOTE_DIR__; exit 0; fi; "
        f"find {sh_quote(remote_dir)} -maxdepth 1 -type f -name {sh_quote(backup_pattern)} "
        f"-printf {sh_quote(printf_format)} | sort -rn"
    )
    target, result = run_remote_capture(config_file, deploy, command, timeout=30)
    output = result.get("output") or ""
    if "__NO_REMOTE_DIR__" in output:
        return {
            "target": f"{target}:{remote_path}",
            "remote_dir": remote_dir,
            "remote_filename": remote_filename,
            "remote_path": remote_path,
            "backup_keep": backup_keep_count(deploy),
            "backups": [],
        }
    backups = []
    for line in output.splitlines():
        item = parse_remote_backup_line(line)
        if item:
            backups.append(item)
    return {
        "target": f"{target}:{remote_path}",
        "remote_dir": remote_dir,
        "remote_filename": remote_filename,
        "remote_path": remote_path,
        "backup_keep": backup_keep_count(deploy),
        "backups": backups,
    }


def validated_backup_name(remote_dir, remote_filename, backup_value):
    text = str(backup_value or "").strip()
    if not text:
        raise ReleaseError("备份文件不能为空")
    name = PurePosixPath(text).name
    if not name or "/" in name or name in (".", ".."):
        raise ReleaseError("备份文件名非法")
    expected_path = remote_join(remote_dir, name)
    if text.startswith("/") and text.rstrip("/") != expected_path:
        raise ReleaseError("备份文件不属于当前发布目录")
    if not name.startswith(f"{remote_filename}.bak_"):
        raise ReleaseError("只能回滚当前包对应的备份文件")
    return name


def rollback_remote_backup(config_file, project, backup_value, env_name=None, target_config=None, restart=False, logger=None):
    deploy, remote_dir, remote_filename, remote_path = remote_deploy_context(project, config_file.data, env_name, target_config)
    if not remote_dir:
        raise ReleaseError("服务器目录不能为空")
    backup_name = validated_backup_name(remote_dir, remote_filename, backup_value)
    backup_path = remote_join(remote_dir, backup_name)
    current_backup = f"{remote_path}.bak_rollback_{datetime.now().strftime('%Y%m%d%H%M%S')}"
    command = (
        f"if [ ! -f {sh_quote(backup_path)} ]; then echo backup-not-found; exit 2; fi; "
        f"if [ -f {sh_quote(remote_path)} ]; then "
        f"cp -p {sh_quote(remote_path)} {sh_quote(current_backup)} && echo __BACKUP_CURRENT__:{current_backup}; "
        f"else echo no-current-artifact; fi; "
        f"cp -p {sh_quote(backup_path)} {sh_quote(remote_path)} && echo __ROLLBACK__:{backup_path}->{remote_path}"
    )
    target, result = run_remote_capture(config_file, deploy, command, timeout=60)
    response = {
        "target": f"{target}:{remote_path}",
        "backup": backup_path,
        "current_backup": current_backup,
        "output": result.get("output") or "",
        "restarted": False,
    }
    if restart:
        restart_logger = logger or Logger()
        control_service(config_file, project, restart_logger, env_name=env_name, target_config=target_config)
        response["restarted"] = True
        response["restart_log_file"] = str(restart_logger.path)
    return response


def preflight_check(key, title, status, message, details=None):
    return {
        "key": key,
        "title": title,
        "status": status,
        "message": message,
        "details": details or "",
    }


def preflight_target_status(checks):
    statuses = [item.get("status") for item in checks]
    if "fail" in statuses:
        return "fail"
    if "warn" in statuses:
        return "warn"
    return "ok"


def parse_preflight_remote_output(output, remote_dir, remote_path, local_artifact_size=None):
    checks = []
    raw = output or ""
    dir_state = ""
    write_state = ""
    disk_parts = None
    artifact_parts = None
    for line in raw.splitlines():
        dir_match = re.search(r"__RH_DIR__:(exists|missing)", line)
        if dir_match:
            dir_state = dir_match.group(1)
        write_match = re.search(
            r"__RH_WRITE__:(dir_writable|parent_writable|dir_not_writable|parent_not_writable)",
            line,
        )
        if write_match:
            write_state = write_match.group(1)
        disk_match = re.search(r"__RH_DISK__:(\d+):(\d+%)", line)
        if disk_match:
            disk_parts = [disk_match.group(1), disk_match.group(2)]
        artifact_match = re.search(r"__RH_ARTIFACT__:(exists|missing)(?::(\d+):(.*))?", line)
        if artifact_match:
            artifact_parts = [artifact_match.group(1)]
            if artifact_match.group(1) == "exists":
                artifact_parts.extend([artifact_match.group(2) or "0", artifact_match.group(3) or ""])

    if dir_state == "exists":
        checks.append(preflight_check("remote_dir", "远程目录", "ok", f"目录存在: {remote_dir}"))
    elif dir_state == "missing":
        checks.append(preflight_check("remote_dir", "远程目录", "warn", f"目录不存在，发布时会尝试创建: {remote_dir}"))
    else:
        checks.append(preflight_check("remote_dir", "远程目录", "fail", f"无法确认目录状态: {remote_dir}", raw))

    if write_state == "dir_writable":
        checks.append(preflight_check("writable", "写入权限", "ok", "目标目录可写"))
    elif write_state == "parent_writable":
        checks.append(preflight_check("writable", "写入权限", "warn", "目标目录不存在，但父目录可写"))
    elif write_state == "dir_not_writable":
        checks.append(preflight_check("writable", "写入权限", "fail", "目标目录不可写"))
    elif write_state == "parent_not_writable":
        checks.append(preflight_check("writable", "写入权限", "fail", "目标目录不存在，且父目录不可写或不存在"))
    else:
        checks.append(preflight_check("writable", "写入权限", "fail", "无法确认写入权限", raw))

    if disk_parts and len(disk_parts) >= 2:
        available_kb = int(disk_parts[0]) if disk_parts[0].isdigit() else 0
        used_percent = disk_parts[1]
        available_bytes = available_kb * 1024
        message = f"可用空间 {file_size_text_from_bytes(available_bytes)}，已用 {used_percent}"
        status = "ok"
        if local_artifact_size and available_bytes and available_bytes < local_artifact_size:
            status = "fail"
            message = f"可用空间不足，{message}，本地包 {file_size_text_from_bytes(local_artifact_size)}"
        checks.append(preflight_check("disk", "磁盘空间", status, message))
    else:
        checks.append(preflight_check("disk", "磁盘空间", "warn", "无法读取磁盘空间"))

    if artifact_parts and artifact_parts[0] == "exists":
        size = int(artifact_parts[1]) if len(artifact_parts) > 1 and artifact_parts[1].isdigit() else 0
        mtime = artifact_parts[2].split(".")[0] if len(artifact_parts) > 2 else ""
        status = "warn" if size <= 0 else "ok"
        message = "当前包存在但大小为 0 B" if size <= 0 else f"当前包存在: {file_size_text_from_bytes(size)}"
        checks.append(preflight_check("remote_artifact", "远程当前包", status, message, mtime))
    elif artifact_parts and artifact_parts[0] == "missing":
        checks.append(preflight_check("remote_artifact", "远程当前包", "warn", f"当前包不存在: {remote_path}"))
    else:
        checks.append(preflight_check("remote_artifact", "远程当前包", "warn", "无法确认当前包状态"))
    return checks


def file_size_text_from_bytes(size):
    value = float(size or 0)
    for unit in ["B", "KB", "MB", "GB"]:
        if value < 1024 or unit == "GB":
            return f"{value:.2f} {unit}" if unit != "B" else f"{int(value)} B"
        value /= 1024
    return f"{int(value)} B"


def build_preflight_remote_command(remote_dir, remote_path):
    return (
        f"REMOTE_DIR={sh_quote(remote_dir)}; "
        f"REMOTE_PATH={sh_quote(remote_path)}; "
        f"if [ -d \"$REMOTE_DIR\" ]; then echo __RH_DIR__:exists; else echo __RH_DIR__:missing; fi; "
        f"if [ -d \"$REMOTE_DIR\" ]; then "
        f"if [ -w \"$REMOTE_DIR\" ]; then echo __RH_WRITE__:dir_writable; else echo __RH_WRITE__:dir_not_writable; fi; "
        f"else PARENT_DIR=$(dirname \"$REMOTE_DIR\"); "
        f"if [ -d \"$PARENT_DIR\" ] && [ -w \"$PARENT_DIR\" ]; then echo __RH_WRITE__:parent_writable; else echo __RH_WRITE__:parent_not_writable; fi; "
        f"fi; "
        f"DISK_PATH=\"$REMOTE_DIR\"; [ -d \"$DISK_PATH\" ] || DISK_PATH=$(dirname \"$REMOTE_DIR\"); "
        f"df -Pk \"$DISK_PATH\" 2>/dev/null | awk 'NR==2 {{print \"__RH_DISK__:\" $4 \":\" $5}}'; "
        f"if [ -f \"$REMOTE_PATH\" ]; then "
        f"stat -c '__RH_ARTIFACT__:exists:%s:%y' \"$REMOTE_PATH\"; "
        f"else echo __RH_ARTIFACT__:missing; fi"
    )


def run_status_preflight(config_file, project, env_name, target_config):
    replica = target_config.get("replica") if target_config else None
    service = get_project_service(project, env_name, replica)
    status_command = service.get("status_command")
    if not status_command:
        return preflight_check("service_status", "服务状态", "warn", "未配置状态检查命令")
    deploy = target_config["deploy"] if target_config else get_project_deploy(project, config_file.data, env_name)
    context = service_command_context(project, config_file.data, env_name, deploy, service)
    rendered = render_service_command(status_command, context)
    remote_body = remote_service_body(rendered, context)
    _, result = run_remote_capture(
        config_file,
        deploy,
        remote_body,
        timeout=REMOTE_COMMAND_TIMEOUTS.get("status", 30),
        check_exit=False,
    )
    output = (result.get("output") or "").strip()
    if result.get("exit_code") == 0:
        return preflight_check("service_status", "服务状态", "ok", "状态检查命令执行成功", output)
    return preflight_check("service_status", "服务状态", "warn", f"状态检查命令退出码: {result.get('exit_code')}", output)


def local_artifact_size(config_file, project):
    project_path = resolve_path(config_file.path, project.get("path", ""))
    artifact = artifact_path(project_path, project.get("artifact", ""))
    return artifact.stat().st_size if artifact.exists() else None


def run_preflight_checks(config_file, project, env_name=None, replica_names=None):
    active_env = effective_env_name(project, env_name)
    errors = validate_config(
        config_file,
        mode="full",
        project_name=project.get("name"),
        env_name=active_env,
        replica_names=replica_names,
        require_replica_selection=False,
    )
    targets = []
    summary = {"ok": 0, "warn": 0, "fail": 0}
    if errors:
        checks = [preflight_check("config", "配置校验", "fail", error) for error in errors]
        summary["fail"] = len(checks)
        return {
            "project_name": project.get("name", ""),
            "env_name": active_env,
            "status": "fail",
            "summary": summary,
            "targets": [{"name": "", "target": "-", "status": "fail", "checks": checks}],
        }

    deploy_targets = get_deploy_targets(project, config_file.data, active_env, replica_names, require_selection=False)
    artifact_size = local_artifact_size(config_file, project)
    for target_config in deploy_targets:
        deploy, remote_dir, remote_filename, remote_path = remote_deploy_context(project, config_file.data, active_env, target_config)
        target_label = f"{deploy.get('user', '-')}@{deploy.get('host', '-')}:{remote_path}"
        checks = []
        try:
            command = build_preflight_remote_command(remote_dir, remote_path)
            _, result = run_remote_capture(config_file, deploy, command, timeout=30)
            checks.append(preflight_check("connection", "SSH 连接", "ok", "连接成功"))
            checks.extend(parse_preflight_remote_output(result.get("output") or "", remote_dir, remote_path, artifact_size))
            checks.append(run_status_preflight(config_file, project, active_env, target_config))
        except Exception as exc:
            checks.append(preflight_check("connection", "SSH 连接", "fail", str(exc)))

        status = preflight_target_status(checks)
        summary[status] += 1
        targets.append({
            "name": target_config.get("name") or "",
            "target": target_label,
            "remote_dir": remote_dir,
            "remote_filename": remote_filename,
            "status": status,
            "checks": checks,
        })

    status = "fail" if summary["fail"] else "warn" if summary["warn"] else "ok"
    return {
        "project_name": project.get("name", ""),
        "env_name": active_env,
        "status": status,
        "summary": summary,
        "targets": targets,
    }


def build_remote_backup_cleanup(remote_dir, remote_filename, keep):
    if keep <= 0:
        return "echo backup-cleanup-disabled"
    backup_pattern = f"{remote_filename}.bak_*"
    find_backups = f"find {sh_quote(remote_dir)} -maxdepth 1 -type f -name {sh_quote(backup_pattern)}"
    return (
        f"backup_keep={keep}; "
        f"backup_count=$({find_backups} | wc -l); "
        f"echo backup-retention:${{backup_keep}} current:${{backup_count}}; "
        f"if [ \"$backup_count\" -gt \"$backup_keep\" ]; then "
        f"{find_backups} | sort -r | tail -n +$((backup_keep + 1)) | "
        f"while IFS= read -r old_backup; do "
        f"rm -f \"$old_backup\" && echo __CLEAN_BACKUP__:$old_backup; "
        f"done; "
        f"fi"
    )


def build_remote_prepare(remote_dir, remote_path, remote_filename, backup_keep):
    timestamp = datetime.now().strftime("%Y%m%d%H%M%S")
    backup_path = f"{remote_path}.bak_{timestamp}"
    cleanup_command = build_remote_backup_cleanup(remote_dir, remote_filename, backup_keep)
    return (
        f"mkdir -p {sh_quote(remote_dir)} && "
        f"if [ -f {sh_quote(remote_path)} ]; then "
        f"mv {sh_quote(remote_path)} {sh_quote(backup_path)} && echo __BACKUP__:{backup_path}; "
        f"else echo no-existing-artifact; fi; "
        f"{cleanup_command}"
    )


def deploy_with_key(config_file, deploy, artifact, remote_dir, remote_path, remote_filename, target, logger):
    port = str(deploy.get("port", 22))
    private_key = resolve_path(config_file.path, deploy["private_key"])
    ssh_path = command_path(deploy.get("ssh_path")) or default_ssh_path()
    scp_path = command_path(deploy.get("scp_path")) or default_scp_path()
    logger.write(f"认证方式: SSH 私钥")
    logger.write(f"使用私钥: {private_key}")
    remote_prepare = build_remote_prepare(remote_dir, remote_path, remote_filename, backup_keep_count(deploy))
    remote_command = "sh -lc " + sh_quote(remote_prepare)
    ssh_command = [ssh_path, "-n", "-T", "-i", str(private_key), "-p", port, "-o", "BatchMode=yes", "-o", "StrictHostKeyChecking=accept-new", target, remote_command]
    set_stage(logger, "backup", "备份旧包")
    run_process(ssh_command, logger, encoding="utf-8")
    remote_spec = f"{target}:{remote_path}"
    scp_command = [scp_path, "-i", str(private_key), "-P", port, "-o", "BatchMode=yes", "-o", "StrictHostKeyChecking=accept-new", str(artifact), remote_spec]
    set_stage(logger, "upload", "上传新包")
    run_process(scp_command, logger, encoding="utf-8")


def deploy_with_password(deploy, artifact, remote_dir, remote_path, remote_filename, target, logger):
    port = str(deploy.get("port", 22))
    password = get_deploy_password(deploy)
    plink_path = command_path(deploy.get("plink_path")) or default_plink_path()
    pscp_path = command_path(deploy.get("pscp_path")) or default_pscp_path()
    logger.write("认证方式: 账号密码（plink/pscp）")
    remote_prepare = build_remote_prepare(remote_dir, remote_path, remote_filename, backup_keep_count(deploy))
    remote_command = "sh -lc " + sh_quote(remote_prepare)
    plink_command = [plink_path, "-batch", "-ssh", "-P", port, "-pw", password, target, remote_command]
    set_stage(logger, "backup", "备份旧包")
    run_process(plink_command, logger, encoding="utf-8")
    remote_spec = f"{target}:{remote_path}"
    pscp_command = [pscp_path, "-batch", "-P", port, "-pw", password, str(artifact), remote_spec]
    set_stage(logger, "upload", "上传新包")
    run_process(pscp_command, logger, encoding="utf-8")


def process_pattern(filename):
    text = str(filename)
    if not text:
        return text
    return f"[{text[0]}]{text[1:]}"


def infer_package_type(project, deploy=None):
    deploy = deploy or project.get("deploy") or {}
    for candidate in (project.get("artifact"), deploy.get("remote_filename")):
        suffix = Path(str(candidate or "")).suffix.lower()
        if suffix == ".war":
            return "war"
        if suffix == ".jar":
            return "jar"
    return "jar"


def war_context_name(filename):
    name = Path(str(filename or "")).name
    if name.lower().endswith(".war"):
        return name[:-4]
    return Path(name).stem or "app"


def infer_tomcat_home(remote_dir):
    text = str(remote_dir or "").rstrip("/")
    if text.lower().endswith("/webapps"):
        return text[:-8] or "/"
    return text


def service_command_context(project, data, env_name=None, deploy_override=None, service_override=None):
    deploy = deploy_override or get_project_deploy(project, data, env_name)
    service = service_override if isinstance(service_override, dict) else get_project_service(project, env_name)
    remote_dir = str(deploy.get("remote_dir", "")).rstrip("/")
    remote_filename = deploy.get("remote_filename") or Path(project.get("artifact", "")).name
    remote_path = remote_join(remote_dir, remote_filename) if remote_dir else remote_filename
    package_type = infer_package_type(project, deploy)
    context = {
        "package_type": package_type,
        "remote_dir": remote_dir,
        "remote_filename": remote_filename,
        "remote_process_pattern": process_pattern(remote_filename),
        "remote_path": remote_path,
        "tomcat_home": infer_tomcat_home(remote_dir),
        "war_context": war_context_name(remote_filename),
        "env": env_name or project.get("default_environment", ""),
    }
    default_work_dir = "{tomcat_home}" if package_type == "war" else "{remote_dir}"
    context["service_work_dir"] = render_service_command(service.get("work_dir") or default_work_dir, context)
    context["service_pid_file"] = render_service_command(service.get("pid_file") or "{service_work_dir}/app.pid", context)
    context["service_process_pattern"] = render_service_command(service.get("process_pattern") or "{remote_process_pattern}", context)
    return context


def log_command_context(project, data, env_name=None, deploy_override=None, replica=None):
    context = service_command_context(project, data, env_name, deploy_override)
    log_config = get_project_log(project, env_name, replica)
    work_dir = render_service_command(log_config.get("work_dir") or "{remote_dir}", context)
    context["work_dir"] = work_dir
    return context


def render_service_command(command, context):
    rendered = command
    for key, value in context.items():
        rendered = rendered.replace("{" + key + "}", str(value))
    return rendered


def remote_service_body(command, context):
    work_dir = str(context.get("service_work_dir", "") or "").rstrip("/")
    if not work_dir:
        return command
    return f"cd {sh_quote(work_dir)} && {command}"


def split_log_pipeline(command):
    segments = []
    current = []
    quote = ""
    escaped = False
    text = str(command)
    if "`" in text:
        raise ReleaseError("日志查看命令不允许使用反引号")
    if "$(" in text:
        raise ReleaseError("日志查看命令不允许使用 $()")
    index = 0
    while index < len(text):
        char = text[index]
        if escaped:
            current.append(char)
            escaped = False
            index += 1
            continue
        if char == "\\":
            current.append(char)
            escaped = True
            index += 1
            continue
        if quote:
            current.append(char)
            if char == quote:
                quote = ""
            index += 1
            continue
        if char in ("\n", "\r", ";", "&", "<", ">", "(", ")"):
            raise ReleaseError(f"日志查看命令不允许使用控制符: {char}")
        if char in ("'", '"'):
            current.append(char)
            quote = char
            index += 1
            continue
        if char == "|":
            if index + 1 < len(text) and text[index + 1] == "|":
                raise ReleaseError("日志查看命令不允许使用 ||")
            segment = "".join(current).strip()
            if not segment:
                raise ReleaseError("管道前缺少命令")
            segments.append(segment)
            current = []
            index += 1
            continue
        current.append(char)
        index += 1
    if quote:
        raise ReleaseError("日志查看命令引号未闭合")
    segment = "".join(current).strip()
    if segment:
        segments.append(segment)
    if not segments:
        raise ReleaseError("日志查看命令不能为空")
    return segments


def shell_tokens(segment):
    try:
        tokens = shlex.split(segment, posix=True)
    except ValueError as exc:
        raise ReleaseError(f"日志查看命令解析失败: {exc}") from exc
    if not tokens:
        raise ReleaseError("管道中包含空命令")
    return tokens


def ensure_current_dir_arg(value):
    text = str(value or "")
    if not text or text == "-":
        raise ReleaseError("文件名不能为空")
    normalized = text.replace("\\", "/")
    if normalized in (".", "./"):
        return
    while normalized.startswith("./"):
        normalized = normalized[2:]
    parts = [part for part in normalized.split("/") if part]
    if normalized.startswith("/") or normalized.startswith("~") or ".." in parts or "/" in normalized:
        raise ReleaseError(f"文件参数只能使用当前目录内文件: {value}")


def option_has_short_flag(token, flags):
    if not token.startswith("-") or token.startswith("--") or token == "-":
        return False
    return any(char in flags for char in token[1:])


def validate_ls_tokens(tokens):
    for token in tokens[1:]:
        if token == "--recursive" or option_has_short_flag(token, {"R"}):
            raise ReleaseError("日志查看不允许使用 ls -R")
        if token.startswith("-"):
            continue
        ensure_current_dir_arg(token)


def validate_tail_tokens(tokens):
    has_file = False
    skip_next = False
    for token in tokens[1:]:
        if skip_next:
            skip_next = False
            continue
        if token in ("-f", "-F", "--follow") or token.startswith("--follow=") or option_has_short_flag(token, {"f", "F"}):
            raise ReleaseError("日志查看不允许使用 tail -f")
        if token in ("-n", "--lines", "-c", "--bytes"):
            skip_next = True
            continue
        if token.startswith("--lines=") or token.startswith("--bytes=") or re.fullmatch(r"-\d+", token):
            continue
        if token.startswith("-"):
            continue
        ensure_current_dir_arg(token)
        has_file = True
    if not has_file:
        raise ReleaseError("tail 必须指定当前目录内日志文件")


def validate_grep_tokens(tokens, has_stdin):
    pattern_seen = False
    file_args = []
    skip_next = False
    for token in tokens[1:]:
        if skip_next:
            skip_next = False
            continue
        if token in ("-r", "-R", "--recursive", "--dereference-recursive") or option_has_short_flag(token, {"r", "R"}):
            raise ReleaseError("日志查看不允许使用 grep 递归搜索")
        if token in ("-f", "--file") or token.startswith("--file=") or option_has_short_flag(token, {"f"}):
            raise ReleaseError("日志查看不允许使用 grep -f")
        if token in ("-e", "--regexp"):
            pattern_seen = True
            skip_next = True
            continue
        if token in ("-A", "-B", "-C", "--after-context", "--before-context", "--context"):
            skip_next = True
            continue
        if token.startswith("--regexp=") or token.startswith("--after-context=") or token.startswith("--before-context=") or token.startswith("--context="):
            pattern_seen = True
            continue
        if token.startswith("-"):
            continue
        if not pattern_seen:
            pattern_seen = True
            continue
        file_args.append(token)
    if not pattern_seen:
        raise ReleaseError("grep 必须指定搜索关键字")
    if not has_stdin and not file_args:
        raise ReleaseError("grep 直接执行时必须指定当前目录内日志文件")
    for token in file_args:
        ensure_current_dir_arg(token)


def validate_remote_log_command(command):
    segments = split_log_pipeline(command)
    allowed = {"tail", "grep", "ls"}
    for index, segment in enumerate(segments):
        tokens = shell_tokens(segment)
        command_name = tokens[0]
        if "/" in command_name or command_name not in allowed:
            raise ReleaseError(f"日志查看只允许 tail、grep、ls: {command_name}")
        if command_name == "ls":
            validate_ls_tokens(tokens)
        elif command_name == "tail":
            validate_tail_tokens(tokens)
        elif command_name == "grep":
            validate_grep_tokens(tokens, has_stdin=index > 0)
    return segments


def validate_log_deploy(config_file, deploy):
    errors = []
    if not deploy.get("host"):
        errors.append("服务器地址不能为空")
    if not deploy.get("user"):
        errors.append("服务器用户不能为空")
    auth_type = deploy_auth_type(deploy)
    if auth_type == "password":
        if not get_deploy_password(deploy):
            errors.append("password/password_env 不能为空")
    elif auth_type == "key":
        if not deploy.get("private_key"):
            errors.append("私钥路径不能为空")
        else:
            key_path = resolve_path(config_file.path, deploy["private_key"])
            if not key_path.exists():
                errors.append(f"私钥路径不存在: {key_path}")
    else:
        errors.append("auth_type 只支持 password 或 key")
    if errors:
        raise ReleaseError("; ".join(errors))


def run_remote_command(config_file, project, command, logger, stage_key=None, stage_title=None, env_name=None, deploy_override=None, timeout=None, service_override=None):
    if not command:
        return
    if stage_key:
        set_stage(logger, stage_key, stage_title)
    deploy = deploy_override or get_project_deploy(project, config_file.data, env_name)
    context = service_command_context(project, config_file.data, env_name, deploy, service_override)
    command = render_service_command(command, context)
    command = remote_service_body(command, context)
    host = deploy["host"]
    user = deploy["user"]
    port = str(deploy.get("port", 22))
    target = f"{user}@{host}"
    remote_command = "sh -lc " + sh_quote(command)
    timeout = REMOTE_COMMAND_TIMEOUTS.get(stage_key) if timeout is None else timeout
    logger.write(f"远程执行: {target}")
    if deploy_auth_type(deploy) == "password":
        password = get_deploy_password(deploy)
        plink_path = command_path(deploy.get("plink_path")) or default_plink_path()
        run_process([plink_path, "-batch", "-ssh", "-P", port, "-pw", password, target, remote_command], logger, encoding="utf-8", timeout=timeout)
    else:
        private_key = resolve_path(config_file.path, deploy["private_key"])
        ssh_path = command_path(deploy.get("ssh_path")) or default_ssh_path()
        run_process([ssh_path, "-n", "-T", "-i", str(private_key), "-p", port, "-o", "BatchMode=yes", "-o", "StrictHostKeyChecking=accept-new", target, remote_command], logger, encoding="utf-8", timeout=timeout)


def build_health_check_command(url, retries, interval_seconds, timeout_seconds):
    return (
        f"URL={sh_quote(url)}; "
        f"TRIES={int(retries)}; "
        f"INTERVAL={int(interval_seconds)}; "
        f"TIMEOUT={int(timeout_seconds)}; "
        f"i=1; "
        f"while [ \"$i\" -le \"$TRIES\" ]; do "
        f"echo health-check-attempt:$i/$TRIES; "
        f"if command -v curl >/dev/null 2>&1; then "
        f"CODE=$(curl -k -L -s -o /dev/null -w '%{{http_code}}' --max-time \"$TIMEOUT\" \"$URL\" || true); "
        f"elif command -v wget >/dev/null 2>&1; then "
        f"CODE=$(wget --no-check-certificate -q -O /dev/null --timeout=\"$TIMEOUT\" --server-response \"$URL\" 2>&1 | awk '/HTTP\\// {{code=$2}} END {{print code+0}}' || true); "
        f"else echo no-curl-or-wget; exit 127; fi; "
        f"echo health-status:$CODE; "
        f"case \"$CODE\" in 2*|3*) exit 0;; esac; "
        f"if [ \"$i\" -lt \"$TRIES\" ]; then sleep \"$INTERVAL\"; fi; "
        f"i=$((i + 1)); "
        f"done; "
        f"exit 1"
    )


def run_health_check(config_file, project, logger, env_name=None, target_config=None, service_override=None):
    service = service_override or get_project_service(project, env_name, target_config.get("replica") if target_config else None)
    url = str(service.get("health_check_url") or "").strip()
    set_stage(logger, "health", "健康检查")
    if not url:
        logger.write("未配置健康检查 URL，跳过")
        return
    deploy = target_config["deploy"] if target_config else get_project_deploy(project, config_file.data, env_name)
    context = service_command_context(project, config_file.data, env_name, deploy, service)
    url = render_service_command(url, context)
    retries = max(1, int(service.get("health_check_retries", 5) or 5))
    interval_seconds = max(0, int(service.get("health_check_interval_seconds", 3) or 0))
    timeout_seconds = max(1, int(service.get("health_check_timeout_seconds", 5) or 5))
    logger.write(f"健康检查 URL: {url}")
    logger.write(f"健康检查策略: {retries} 次，每次超时 {timeout_seconds}s，间隔 {interval_seconds}s")
    command = build_health_check_command(url, retries, interval_seconds, timeout_seconds)
    run_remote_command(
        config_file,
        project,
        command,
        logger,
        "health",
        "健康检查",
        env_name,
        deploy,
        timeout=retries * (timeout_seconds + interval_seconds) + 15,
        service_override=service,
    )


def run_remote_log_command(config_file, project, command, env_name=None, target_config=None, work_dir_override=None):
    command = str(command or "").strip()
    if not command:
        raise ReleaseError("日志查看命令不能为空")
    replica = target_config.get("replica") if target_config else None
    deploy = target_config["deploy"] if target_config else get_project_deploy(project, config_file.data, env_name)
    validate_log_deploy(config_file, deploy)
    context = log_command_context(project, config_file.data, env_name, deploy, replica)
    if work_dir_override is not None:
        context["work_dir"] = render_service_command(str(work_dir_override), context)
    rendered_command = render_service_command(command, context).strip()
    validate_remote_log_command(rendered_command)
    work_dir = context.get("work_dir") or ""
    if not work_dir:
        raise ReleaseError("服务器当前目录不能为空")
    remote_body = f"cd {sh_quote(work_dir)} && {rendered_command}" if work_dir else rendered_command
    host = deploy["host"]
    user = deploy["user"]
    port = str(deploy.get("port", 22))
    target = f"{user}@{host}"
    remote_command = "sh -lc " + sh_quote(remote_body)
    if deploy_auth_type(deploy) == "password":
        password = get_deploy_password(deploy)
        plink_path = command_path(deploy.get("plink_path")) or default_plink_path()
        result = run_process_capture([plink_path, "-batch", "-ssh", "-P", port, "-pw", password, target, remote_command], encoding="utf-8")
    else:
        private_key = resolve_path(config_file.path, deploy["private_key"])
        ssh_path = command_path(deploy.get("ssh_path")) or default_ssh_path()
        result = run_process_capture([ssh_path, "-n", "-T", "-i", str(private_key), "-p", port, "-o", "BatchMode=yes", "-o", "StrictHostKeyChecking=accept-new", target, remote_command], encoding="utf-8")
    return {
        "target": target,
        "work_dir": work_dir,
        "command": rendered_command,
        "exit_code": result["exit_code"],
        "output": result["output"],
        "truncated": result.get("truncated", False),
    }


def control_service(config_file, project, logger, env_name=None, target_config=None):
    replica = target_config.get("replica") if target_config else None
    service = get_project_service(project, env_name, replica)
    deploy = target_config["deploy"] if target_config else None
    stop_command = service.get("stop_command")
    start_command = service.get("start_command")
    status_command = service.get("status_command")
    wait_seconds = int(service.get("startup_wait_seconds", 3) or 0)

    logger.write("开始服务控制")
    run_remote_command(config_file, project, stop_command, logger, "stop", "优雅停机", env_name, deploy, service_override=service)
    run_remote_command(config_file, project, start_command, logger, "start", "启动服务", env_name, deploy, service_override=service)
    set_stage(logger, "wait", "等待启动")
    logger.write(f"等待服务启动: {wait_seconds}s")
    if wait_seconds > 0:
        time.sleep(wait_seconds)
    if status_command:
        run_remote_command(config_file, project, status_command, logger, "status", "状态检查", env_name, deploy, service_override=service)
    else:
        set_stage(logger, "status", "状态检查")
        logger.write("未配置状态检查命令，跳过")
    run_health_check(config_file, project, logger, env_name, target_config, service)


def deploy_artifact(config_file, project, artifact, logger, env_name=None, target_config=None):
    deploy = target_config["deploy"] if target_config else get_project_deploy(project, config_file.data, env_name)
    host = deploy["host"]
    user = deploy["user"]
    remote_dir = deploy["remote_dir"].rstrip("/")
    remote_filename = deploy.get("remote_filename") or artifact.name
    remote_path = remote_join(remote_dir, remote_filename)
    target = f"{user}@{host}"

    if target_config and target_config.get("name"):
        logger.write(f"目标副本: {target_config['name']}")
    logger.write(f"目标服务器: {target}:{remote_path}")
    if deploy_auth_type(deploy) == "password":
        deploy_with_password(deploy, artifact, remote_dir, remote_path, remote_filename, target, logger)
    else:
        deploy_with_key(config_file, deploy, artifact, remote_dir, remote_path, remote_filename, target, logger)
    logger.write("上传完成")


def execute_release(config_file, project_name, env_name, logger, mode="upload", replica_names=None):
    if mode not in JOB_STEPS:
        raise ReleaseError(f"不支持的执行模式: {mode}")
    data = config_file.data
    projects = {item["name"]: item for item in data["projects"]}
    if project_name not in projects:
        raise ReleaseError(f"找不到项目: {project_name}")
    project = projects[project_name]
    project_path = resolve_path(config_file.path, project["path"])
    build_command = project["build_command"]
    artifact = artifact_path(project_path, project["artifact"])
    backups = []
    start_time = time.time()
    try:
        set_stage(logger, "validate", "校验配置")
        errors = validate_config(
            config_file,
            mode=mode,
            project_name=project_name,
            env_name=env_name,
            replica_names=replica_names,
            require_replica_selection=mode in ("deploy", "upload", "start", "full"),
        )
        if errors:
            raise ReleaseError("; ".join(errors))
        deploy_targets = get_deploy_targets(
            project,
            data,
            env_name,
            replica_names,
            require_selection=mode in ("deploy", "upload", "start", "full"),
        )
        if mode in ("build", "upload", "full"):
            set_stage(logger, "env", "切换环境")
            backups = apply_environment(project_path, project.get("environment_replacements", []), env_name, logger)
            run_build(project_path, build_command, logger)
            if not artifact.exists():
                raise ReleaseError(f"找不到构建产物: {artifact}")
            logger.write(f"构建产物: {artifact} ({file_size_text(artifact)})")
        if mode in ("deploy", "upload", "full"):
            if not artifact.exists():
                raise ReleaseError(f"找不到构建产物: {artifact}")
            if mode == "deploy":
                logger.write(f"使用已有构建产物: {artifact} ({file_size_text(artifact)})")
            for target_config in deploy_targets:
                deploy_artifact(config_file, project, artifact, logger, env_name, target_config)
        if mode in ("start", "full"):
            for target_config in deploy_targets:
                if target_config.get("name"):
                    logger.write(f"开始控制副本: {target_config['name']}")
                control_service(config_file, project, logger, env_name, target_config)
        set_stage(logger, "done", "完成")
        elapsed = time.time() - start_time
        logger.write(f"执行完成，用时 {elapsed:.1f}s")
    except Exception as exc:
        logger.write(f"执行失败: {exc}")
        logger.write(traceback.format_exc())
        raise
    finally:
        restore_backups(backups, logger)


class ReleaseSenderApp:
    def __init__(self, root):
        self.root = root
        self.root.title("自动打包并上传服务器")
        self.root.geometry("920x680")
        self.ui_queue = queue.Queue()
        self.config_file = load_config()
        self.logger = None
        self.project_var = StringVar()
        self.env_var = StringVar()
        self.target_var = StringVar(value="-")
        self.key_var = StringVar(value="-")
        self.status_var = StringVar(value="就绪")
        self.build_ui()
        self.populate()
        self.root.after(200, self.flush_queue)

    def build_ui(self):
        top = Frame(self.root, padx=14, pady=12)
        top.pack(fill="x")

        Label(top, text="项目").grid(row=0, column=0, sticky=W, padx=(0, 8), pady=4)
        self.project_combo = ttk.Combobox(top, textvariable=self.project_var, state="readonly", width=28)
        self.project_combo.grid(row=0, column=1, sticky=W, pady=4)
        self.project_combo.bind("<<ComboboxSelected>>", lambda _event: self.on_project_change())

        Label(top, text="环境").grid(row=0, column=2, sticky=W, padx=(24, 8), pady=4)
        self.env_combo = ttk.Combobox(top, textvariable=self.env_var, state="readonly", width=16)
        self.env_combo.grid(row=0, column=3, sticky=W, pady=4)

        self.run_button = Button(top, text="开始打包并上传", command=self.start_release)
        self.run_button.grid(row=0, column=4, sticky=W, padx=(24, 0), pady=4)

        target_frame = Frame(self.root, padx=14)
        target_frame.pack(fill="x")
        Label(target_frame, text="目标服务器").grid(row=0, column=0, sticky=W, padx=(0, 8), pady=4)
        Label(target_frame, textvariable=self.target_var, anchor=W).grid(row=0, column=1, sticky=W, pady=4)
        Label(target_frame, text="私钥").grid(row=1, column=0, sticky=W, padx=(0, 8), pady=4)
        Label(target_frame, textvariable=self.key_var, anchor=W).grid(row=1, column=1, sticky=W, pady=4)

        bottom = Frame(self.root, padx=14, pady=8)
        bottom.pack(fill=BOTH, expand=True)
        Label(bottom, textvariable=self.status_var).pack(anchor=W)
        self.log_text = Text(bottom, height=30, wrap="word")
        self.log_text.pack(fill=BOTH, expand=True, pady=(6, 0))

    def populate(self):
        errors = validate_config(self.config_file)
        if self.config_file.using_example:
            self.log_direct("当前使用 config.example.json，建议复制为 config.local.json 后填写真实服务器配置。")
        if errors:
            self.log_direct("配置存在问题：")
            for error in errors:
                self.log_direct(f"- {error}")
        projects = self.config_file.data.get("projects", [])
        self.project_combo["values"] = [project["name"] for project in projects]
        if projects:
            self.project_var.set(projects[0]["name"])
            self.on_project_change()

    def log_direct(self, message):
        self.log_text.insert(END, message + "\n")
        self.log_text.see(END)

    def current_project(self):
        for project in self.config_file.data.get("projects", []):
            if project["name"] == self.project_var.get():
                return project
        raise ReleaseError("请选择项目")

    def on_project_change(self):
        project = self.current_project()
        environments = project.get("environments", ["pro"])
        self.env_combo["values"] = environments
        self.env_var.set(project.get("default_environment") or environments[0])
        deploy = get_project_deploy(project, self.config_file.data)
        remote_filename = deploy.get("remote_filename") or project.get("artifact", "").split("/")[-1]
        remote_dir = deploy.get("remote_dir", "-")
        target = f"{deploy.get('user', '-') }@{deploy.get('host', '-') }:{remote_join(remote_dir, remote_filename) if remote_dir != '-' else '-'}"
        self.target_var.set(target)
        self.key_var.set(str(resolve_path(self.config_file.path, deploy.get("private_key", ""))) if deploy.get("private_key") else "-")

    def start_release(self):
        try:
            errors = validate_config(self.config_file)
            if errors:
                raise ReleaseError("\n".join(errors))
            project_name = self.project_var.get()
            env_name = self.env_var.get()
            self.set_running(True)
            self.logger = Logger(self.ui_queue)
            self.logger.write(f"日志文件: {self.logger.path}")
            thread = threading.Thread(
                target=self.release_worker,
                args=(project_name, env_name),
                daemon=True,
            )
            thread.start()
        except Exception as exc:
            messagebox.showerror("配置错误", str(exc))

    def release_worker(self, project_name, env_name):
        try:
            execute_release(self.config_file, project_name, env_name, self.logger)
            self.ui_queue.put(("status", "完成"))
            self.ui_queue.put(("done", None))
        except Exception as exc:
            self.ui_queue.put(("status", f"失败: {exc}"))
            self.ui_queue.put(("failed", str(exc)))

    def set_running(self, running):
        self.run_button.config(state=DISABLED if running else NORMAL)
        self.status_var.set("执行中..." if running else "就绪")

    def flush_queue(self):
        try:
            while True:
                event, payload = self.ui_queue.get_nowait()
                if event == "log":
                    self.log_direct(payload)
                elif event == "status":
                    self.status_var.set(payload)
                elif event == "done":
                    self.set_running(False)
                    messagebox.showinfo("完成", "打包并上传完成")
                elif event == "failed":
                    self.set_running(False)
                    messagebox.showerror("失败", payload)
        except queue.Empty:
            pass
        self.root.after(200, self.flush_queue)


def main():
    parser = argparse.ArgumentParser(description="自动打包并上传服务器")
    parser.add_argument("--check-config", action="store_true", help="只校验配置，不启动界面")
    args = parser.parse_args()
    if args.check_config:
        config_file = load_config()
        errors = validate_config(config_file)
        if errors:
            for error in errors:
                print(error)
            return 1
        print(f"配置校验通过: {config_file.path}")
        return 0
    root = Tk()
    ReleaseSenderApp(root)
    root.mainloop()
    return 0


if __name__ == "__main__":
    sys.exit(main())
