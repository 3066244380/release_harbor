import argparse
import json
import os
import queue
import re
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
from pathlib import Path
from tkinter import BOTH, DISABLED, END, LEFT, NORMAL, RIGHT, W, Button, Entry, Frame, Label, Listbox, Scrollbar, StringVar, Tk, Text, messagebox, ttk


APP_DIR = Path(__file__).resolve().parent
LOCAL_CONFIG = APP_DIR / "config.local.json"
EXAMPLE_CONFIG = APP_DIR / "config.example.json"
SECRET_CONFIG = APP_DIR / "config.secret.json"
LOG_DIR = APP_DIR / "logs"


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


def get_project_deploy(project, data):
    deploy = project.get("deploy") or data.get("deploy") or {}
    return deploy if isinstance(deploy, dict) else {}


def deploy_auth_type(deploy):
    return str(deploy.get("auth_type", "key")).strip().lower()


def get_deploy_password(deploy):
    return deploy.get("password") or os.getenv(deploy.get("password_env", "DEPLOY_PASSWORD"))


JOB_STEPS = {
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
        {"key": "done", "title": "完成"},
    ],
}


def get_job_steps(mode):
    return JOB_STEPS.get(mode, JOB_STEPS["upload"])


def set_stage(logger, key, title=None):
    if hasattr(logger, "set_stage"):
        logger.set_stage(key, title)


def validate_config(config_file, mode="upload"):
    data = config_file.data
    errors = []
    if not isinstance(data.get("projects"), list) or not data["projects"]:
        errors.append("projects 至少需要配置一个项目")

    for index, project in enumerate(data.get("projects", []), start=1):
        prefix = f"projects[{index}]"
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

        deploy = get_project_deploy(project, data)
        deploy_prefix = f"{prefix}.deploy"
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

        if mode in ("start", "full"):
            service = project.get("service") or {}
            service_prefix = f"{prefix}.service"
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


def run_process(command, logger, cwd=None, encoding="gbk"):
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
    try:
        for line in process.stdout:
            logger.write(line.rstrip())
        code = process.wait()
    finally:
        if clear_process:
            clear_process(process)
    if logger_cancel_requested(logger):
        raise ReleaseError("任务已取消")
    if code != 0:
        raise ReleaseError(f"命令执行失败，退出码: {code}")


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


def build_remote_prepare(remote_dir, remote_path):
    timestamp = datetime.now().strftime("%Y%m%d%H%M%S")
    backup_path = f"{remote_path}.bak_{timestamp}"
    return (
        f"mkdir -p {sh_quote(remote_dir)} && "
        f"if [ -f {sh_quote(remote_path)} ]; then "
        f"mv {sh_quote(remote_path)} {sh_quote(backup_path)} && echo __BACKUP__:{backup_path}; "
        f"else echo no-existing-artifact; fi"
    )


def deploy_with_key(config_file, deploy, artifact, remote_dir, remote_path, target, logger):
    port = str(deploy.get("port", 22))
    private_key = resolve_path(config_file.path, deploy["private_key"])
    ssh_path = command_path(deploy.get("ssh_path")) or default_ssh_path()
    scp_path = command_path(deploy.get("scp_path")) or default_scp_path()
    logger.write(f"认证方式: SSH 私钥")
    logger.write(f"使用私钥: {private_key}")
    remote_prepare = build_remote_prepare(remote_dir, remote_path)
    remote_command = "sh -lc " + sh_quote(remote_prepare)
    ssh_command = [ssh_path, "-T", "-i", str(private_key), "-p", port, "-o", "BatchMode=yes", "-o", "StrictHostKeyChecking=accept-new", target, remote_command]
    set_stage(logger, "backup", "备份旧包")
    run_process(ssh_command, logger, encoding="utf-8")
    remote_spec = f"{target}:{remote_path}"
    scp_command = [scp_path, "-i", str(private_key), "-P", port, "-o", "BatchMode=yes", "-o", "StrictHostKeyChecking=accept-new", str(artifact), remote_spec]
    set_stage(logger, "upload", "上传新包")
    run_process(scp_command, logger, encoding="utf-8")


def deploy_with_password(deploy, artifact, remote_dir, remote_path, target, logger):
    port = str(deploy.get("port", 22))
    password = get_deploy_password(deploy)
    plink_path = command_path(deploy.get("plink_path")) or default_plink_path()
    pscp_path = command_path(deploy.get("pscp_path")) or default_pscp_path()
    logger.write("认证方式: 账号密码（plink/pscp）")
    remote_prepare = build_remote_prepare(remote_dir, remote_path)
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


def infer_package_type(project):
    for candidate in (project.get("artifact"), (project.get("deploy") or {}).get("remote_filename")):
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


def service_command_context(project, data, env_name=None):
    deploy = get_project_deploy(project, data)
    remote_dir = str(deploy.get("remote_dir", "")).rstrip("/")
    remote_filename = deploy.get("remote_filename") or Path(project.get("artifact", "")).name
    remote_path = remote_join(remote_dir, remote_filename) if remote_dir else remote_filename
    package_type = infer_package_type(project)
    return {
        "package_type": package_type,
        "remote_dir": remote_dir,
        "remote_filename": remote_filename,
        "remote_process_pattern": process_pattern(remote_filename),
        "remote_path": remote_path,
        "tomcat_home": infer_tomcat_home(remote_dir),
        "war_context": war_context_name(remote_filename),
        "env": env_name or project.get("default_environment", ""),
    }


def render_service_command(command, context):
    rendered = command
    for key, value in context.items():
        rendered = rendered.replace("{" + key + "}", str(value))
    return rendered


def run_remote_command(config_file, project, command, logger, stage_key=None, stage_title=None, env_name=None):
    if not command:
        return
    if stage_key:
        set_stage(logger, stage_key, stage_title)
    deploy = get_project_deploy(project, config_file.data)
    command = render_service_command(command, service_command_context(project, config_file.data, env_name))
    host = deploy["host"]
    user = deploy["user"]
    port = str(deploy.get("port", 22))
    target = f"{user}@{host}"
    remote_command = "sh -lc " + sh_quote(command)
    logger.write(f"远程执行: {target}")
    if deploy_auth_type(deploy) == "password":
        password = get_deploy_password(deploy)
        plink_path = command_path(deploy.get("plink_path")) or default_plink_path()
        run_process([plink_path, "-batch", "-ssh", "-P", port, "-pw", password, target, remote_command], logger, encoding="utf-8")
    else:
        private_key = resolve_path(config_file.path, deploy["private_key"])
        ssh_path = command_path(deploy.get("ssh_path")) or default_ssh_path()
        run_process([ssh_path, "-T", "-i", str(private_key), "-p", port, "-o", "BatchMode=yes", "-o", "StrictHostKeyChecking=accept-new", target, remote_command], logger, encoding="utf-8")


def control_service(config_file, project, logger, env_name=None):
    service = project.get("service") or {}
    stop_command = service.get("stop_command")
    start_command = service.get("start_command")
    status_command = service.get("status_command")
    wait_seconds = int(service.get("startup_wait_seconds", 3) or 0)

    logger.write("开始服务控制")
    run_remote_command(config_file, project, stop_command, logger, "stop", "优雅停机")
    run_remote_command(config_file, project, start_command, logger, "start", "启动服务")
    set_stage(logger, "wait", "等待启动")
    logger.write(f"等待服务启动: {wait_seconds}s")
    if wait_seconds > 0:
        time.sleep(wait_seconds)
    if status_command:
        run_remote_command(config_file, project, status_command, logger, "status", "状态检查")
    else:
        set_stage(logger, "status", "状态检查")
        logger.write("未配置状态检查命令，跳过")


def deploy_artifact(config_file, project, artifact, logger):
    deploy = get_project_deploy(project, config_file.data)
    host = deploy["host"]
    user = deploy["user"]
    remote_dir = deploy["remote_dir"].rstrip("/")
    remote_filename = deploy.get("remote_filename") or artifact.name
    remote_path = remote_join(remote_dir, remote_filename)
    target = f"{user}@{host}"

    logger.write(f"目标服务器: {target}:{remote_path}")
    if deploy_auth_type(deploy) == "password":
        deploy_with_password(deploy, artifact, remote_dir, remote_path, target, logger)
    else:
        deploy_with_key(config_file, deploy, artifact, remote_dir, remote_path, target, logger)
    logger.write("上传完成")


def execute_release(config_file, project_name, env_name, logger, mode="upload"):
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
        errors = validate_config(config_file, mode=mode)
        if errors:
            raise ReleaseError("; ".join(errors))
        if mode in ("upload", "full"):
            set_stage(logger, "env", "切换环境")
            backups = apply_environment(project_path, project.get("environment_replacements", []), env_name, logger)
            run_build(project_path, build_command, logger)
            if not artifact.exists():
                raise ReleaseError(f"找不到构建产物: {artifact}")
            logger.write(f"构建产物: {artifact} ({file_size_text(artifact)})")
            deploy_artifact(config_file, project, artifact, logger)
        if mode in ("start", "full"):
            control_service(config_file, project, logger, env_name)
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
