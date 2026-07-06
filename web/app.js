const STORAGE_KEY = 'release_harbor_html_fallback_v1';
    const API_BASE = location.protocol === 'file:' ? null : window.location.origin;
    const defaultProject = {
      name: 'visa',
      description: '签证业务后台 API 服务',
      path: 'E:\\work\\workSpace\\visa',
      build_command: ['D:\\tools\\apache-maven-3.8.3-bin\\apache-maven-3.8.3\\bin\\mvn.cmd', 'clean', 'package', '-DskipTests'],
      package_type: 'jar',
      artifact: 'target/visaV3.jar',
      environments: ['dev', 'test', 'pro'],
      default_environment: 'pro',
      environment_replacements: [{ file: 'src\\main\\resources\\application.yml', regex: '(?m)^(\\s*active:\\s*).*$', replacement: '\\1{env}' }],
      deploy: { auth_type: 'key', host: '192.168.88.50', port: 22, user: 'root', private_key: 'C:\\Users\\Admin\\.ssh\\id_ed25519', remote_dir: '/www/nvisa', remote_filename: 'visaV3.jar', ssh_path: 'C:\\Windows\\System32\\OpenSSH\\ssh.exe', scp_path: 'C:\\Windows\\System32\\OpenSSH\\scp.exe' },
      service: { stop_command: "PID_FILE=\"{remote_dir}/app.pid\"\nif [ -f \"$PID_FILE\" ]; then\n  PID=$(cat \"$PID_FILE\" 2>/dev/null || true)\n  if [ -n \"$PID\" ] && ps -p \"$PID\" >/dev/null 2>&1; then\n    kill -15 \"$PID\" 2>/dev/null || true\n    i=0\n    while [ $i -lt 60 ]; do\n      if ! ps -p \"$PID\" >/dev/null 2>&1; then\n        break\n      fi\n      sleep 1\n      i=$((i + 1))\n    done\n    kill -9 \"$PID\" 2>/dev/null || true\n  fi\n  rm -f \"$PID_FILE\"\nfi", start_command: "nohup java -jar {remote_path} --spring.profiles.active={env} > {remote_dir}/app.log 2>&1 & echo $! > {remote_dir}/app.pid", status_command: "PID_FILE=\"{remote_dir}/app.pid\"; if [ -f \"$PID_FILE\" ]; then PID=$(cat \"$PID_FILE\" 2>/dev/null || true); ps -p \"$PID\" -o pid,etime,cmd 2>/dev/null || true; else echo no-pid-file; fi", startup_wait_seconds: 3 }
    };
    const defaultState = { selectedIndex: 0, projects: [structuredClone(defaultProject)] };
    let state = structuredClone(defaultState);
    let pollTimer = null;
    let lastLogCount = 0;
    let activeJobId = null;

    const projectList = document.getElementById('projectList');
    const logOutput = document.getElementById('logOutput');
    const projectBadge = document.getElementById('projectBadge');
    const progressBar = document.getElementById('progressBar');
    const progressTitle = document.getElementById('progressTitle');
    const progressText = document.getElementById('progressText');
    const stepList = document.getElementById('stepList');
    const actionButtons = ['validateBtn', 'saveBtn', 'uploadBtn', 'startBtn', 'fullBtn', 'deleteBtn', 'addProjectBtn'].map(id => document.getElementById(id));

    async function api(path, options = {}) {
      if (!API_BASE) throw new Error('当前是 file:// 打开，只能使用本地缓存；请启动 web_server.py 后访问 http://127.0.0.1:8765');
      const response = await fetch(API_BASE + path, {
        ...options,
        headers: { 'Content-Type': 'application/json', ...(options.headers || {}) }
      });
      const payload = await response.json().catch(() => ({}));
      if (!response.ok || payload.ok === false) throw new Error(payload.error || `HTTP ${response.status}`);
      return payload;
    }

    async function loadStateFromBackend() {
      if (!API_BASE) {
        state = loadFallbackState();
        return;
      }
      try {
        const payload = await api('/api/config');
        const projects = (payload.config?.projects || []).map(project => normalizeProject(project));
        state = projects.length ? { selectedIndex: 0, projects } : structuredClone(defaultState);
        log(`已读取配置: ${payload.source}`);
      } catch (error) {
        state = loadFallbackState();
        log(`读取后端配置失败，已回退到浏览器缓存: ${error.message}`);
      }
    }

    function loadFallbackState() {
      try {
        const raw = localStorage.getItem(STORAGE_KEY);
        const parsed = raw ? JSON.parse(raw) : structuredClone(defaultState);
        parsed.projects = (parsed.projects || []).map(project => normalizeProject(project));
        return parsed.projects.length ? parsed : structuredClone(defaultState);
      } catch {
        return structuredClone(defaultState);
      }
    }

    function normalizeProject(project) {
      const next = { ...structuredClone(defaultProject), ...project };
      next.deploy = { ...structuredClone(defaultProject.deploy), ...(project.deploy || {}) };
      next.service = { ...structuredClone(defaultProject.service), ...(project.service || {}) };
      next.environment_replacements = project.environment_replacements?.length ? project.environment_replacements : structuredClone(defaultProject.environment_replacements);
      next.environments = next.environments?.length ? next.environments : structuredClone(defaultProject.environments);
      next.package_type = normalizePackageType(project.package_type, next);
      return next;
    }

    function normalizePackageType(type, project) {
      const text = String(type || '').trim().toLowerCase();
      if (text === 'jar' || text === 'war') return text;
      return inferPackageType(project);
    }

    function inferPackageType(project) {
      const candidates = [project?.artifact, project?.deploy?.remote_filename];
      for (const item of candidates) {
        const text = String(item || '').trim().toLowerCase();
        if (text.endsWith('.war')) return 'war';
        if (text.endsWith('.jar')) return 'jar';
      }
      return 'jar';
    }

    function fileNameFromPath(path) {
      return String(path || '').split(/[\\/]/).pop() || '';
    }

    function deriveWarContext(project) {
      const filename = fileNameFromPath(project?.deploy?.remote_filename || project?.artifact || '');
      return filename.toLowerCase().endsWith('.war') ? filename.slice(0, -4) : (filename.split('.')[0] || 'app');
    }

    function deriveTomcatHome(remoteDir) {
      const text = String(remoteDir || '').replace(/\/+$/, '');
      return text.toLowerCase().endsWith('/webapps') ? (text.slice(0, -8) || '/') : text;
    }

    function buildServiceTemplate(project) {
      const packageType = normalizePackageType(project.package_type, project);
      if (packageType === 'war') {
        return {
          packageType,
          title: `Tomcat war 模板：${deriveTomcatHome(project.deploy?.remote_dir) || '未设置 Tomcat 目录'} / ${deriveWarContext(project)}`,
          stop_command: '{tomcat_home}/bin/shutdown.sh || true\nsleep 5\nPATTERN="[o]rg.apache.catalina.startup.Bootstrap.*{tomcat_home}"\nPID=$(pgrep -f "$PATTERN" || true)\nif [ -n "$PID" ]; then\n  kill -15 $PID 2>/dev/null || true\n  sleep 10\n  PID=$(pgrep -f "$PATTERN" || true)\n  if [ -n "$PID" ]; then\n    kill -9 $PID 2>/dev/null || true\n  fi\nfi',
          start_command: 'rm -rf {remote_dir}/{war_context}\n{tomcat_home}/bin/startup.sh',
          status_command: 'pgrep -af "[o]rg.apache.catalina.startup.Bootstrap.*{tomcat_home}" || true',
          startup_wait_seconds: 3
        };
      }
      return {
        packageType,
        title: 'jar 模板：PID 文件优雅停机 + java -jar 启动',
        stop_command: 'PID_FILE="{remote_dir}/app.pid"\nif [ -f "$PID_FILE" ]; then\n  PID=$(cat "$PID_FILE" 2>/dev/null || true)\n  if [ -n "$PID" ] && ps -p "$PID" >/dev/null 2>&1; then\n    kill -15 "$PID" 2>/dev/null || true\n    i=0\n    while [ $i -lt 60 ]; do\n      if ! ps -p "$PID" >/dev/null 2>&1; then\n        break\n      fi\n      sleep 1\n      i=$((i + 1))\n    done\n    kill -9 "$PID" 2>/dev/null || true\n  fi\n  rm -f "$PID_FILE"\nfi',
        start_command: 'nohup java -jar {remote_path} --spring.profiles.active={env} > {remote_dir}/app.log 2>&1 & echo $! > {remote_dir}/app.pid',
        status_command: 'PID_FILE="{remote_dir}/app.pid"; if [ -f "$PID_FILE" ]; then PID=$(cat "$PID_FILE" 2>/dev/null || true); ps -p "$PID" -o pid,etime,cmd 2>/dev/null || true; else echo no-pid-file; fi',
        startup_wait_seconds: 3
      };
    }

    function currentProject() {
      state.selectedIndex = Math.min(Math.max(state.selectedIndex, 0), state.projects.length - 1);
      return state.projects[state.selectedIndex];
    }

    async function saveState(options = {}) {
      syncFormToState();
      const config = { projects: state.projects };
      if (!API_BASE) {
        localStorage.setItem(STORAGE_KEY, JSON.stringify(state, null, 2));
        if (!options.silent) log('配置已保存到浏览器 localStorage');
        return true;
      }
      try {
        const payload = await api('/api/config', { method: 'POST', body: JSON.stringify({ config }) });
        if (!options.silent) log(`配置已保存: ${payload.path}`);
        setBadge('已保存', 'ok');
        return true;
      } catch (error) {
        log(`保存失败: ${error.message}`);
        setBadge('保存失败', 'warn');
        return false;
      }
    }

    function render() {
      renderProjects();
      renderForm();
      renderProgress([]);
    }

    function renderProjects() {
      projectList.innerHTML = '';
      state.projects.forEach((project, index) => {
        const item = document.createElement('button');
        item.className = 'project-item' + (index === state.selectedIndex ? ' active' : '');
        item.innerHTML = `<div class="project-name">${escapeHtml(project.name || '未命名项目')}</div><div class="project-desc">${escapeHtml(project.description || '未填写项目说明')}</div><div class="project-path">${escapeHtml(project.path || '-')}</div>`;
        item.addEventListener('click', () => { syncFormToState(); state.selectedIndex = index; render(); });
        projectList.appendChild(item);
      });
    }

    function renderForm() {
      const project = currentProject();
      document.getElementById('pageTitle').textContent = project.name || '项目配置';
      document.getElementById('projectSubtitle').textContent = project.description || '未填写项目说明';
      document.querySelectorAll('[data-path]').forEach(input => input.value = project[input.dataset.path] ?? '');
      document.querySelectorAll('[data-json]').forEach(input => input.value = JSON.stringify(project[input.dataset.json] ?? [], null, 2));
      document.querySelectorAll('[data-list]').forEach(input => input.value = (project[input.dataset.list] ?? []).join(','));
      document.querySelectorAll('[data-deploy]').forEach(input => input.value = project.deploy?.[input.dataset.deploy] ?? '');
      document.querySelectorAll('[data-service]').forEach(input => input.value = project.service?.[input.dataset.service] ?? '');
      const replacement = project.environment_replacements?.[0] ?? {};
      document.querySelectorAll('[data-replacement]').forEach(input => input.value = replacement[input.dataset.replacement] ?? '');
      document.getElementById('deployMode').textContent = project.deploy?.auth_type === 'password' ? '账号密码' : 'SSH Key';
      renderTemplateHint(project);
      setBadge('未校验', '');
    }

    function syncFormToState() {
      const project = currentProject();
      document.querySelectorAll('[data-path]').forEach(input => project[input.dataset.path] = input.value.trim());
      document.querySelectorAll('[data-list]').forEach(input => project[input.dataset.list] = input.value.split(',').map(item => item.trim()).filter(Boolean));
      document.querySelectorAll('[data-json]').forEach(input => {
        try { const value = JSON.parse(input.value || '[]'); project[input.dataset.json] = Array.isArray(value) ? value : []; }
        catch { project[input.dataset.json] = []; }
      });
      project.deploy = project.deploy || { auth_type: 'key' };
      document.querySelectorAll('[data-deploy]').forEach(input => {
        const key = input.dataset.deploy;
        project.deploy[key] = key === 'port' ? Number(input.value || 22) : input.value.trim();
      });
      project.deploy.auth_type = project.deploy.auth_type || 'key';
      project.service = project.service || {};
      document.querySelectorAll('[data-service]').forEach(input => {
        const key = input.dataset.service;
        project.service[key] = key === 'startup_wait_seconds' ? Number(input.value || 3) : input.value.trim();
      });
      const replacement = project.environment_replacements?.[0] || {};
      document.querySelectorAll('[data-replacement]').forEach(input => replacement[input.dataset.replacement] = input.value);
      project.environment_replacements = [replacement];
      project.package_type = normalizePackageType(project.package_type, project);
    }

    function renderTemplateHint(project) {
      const template = buildServiceTemplate(project);
      document.getElementById('serviceTemplateBadge').textContent = template.packageType === 'war' ? 'Tomcat war 推荐模板' : 'jar 推荐模板';
      document.getElementById('serviceTemplateHint').textContent = template.title;
    }

    function applyServiceTemplate() {
      syncFormToState();
      const project = currentProject();
      const template = buildServiceTemplate(project);
      project.package_type = template.packageType;
      project.service = project.service || {};
      project.service.stop_command = template.stop_command;
      project.service.start_command = template.start_command;
      project.service.status_command = template.status_command;
      project.service.startup_wait_seconds = template.startup_wait_seconds;
      renderForm();
      log(`已应用 ${template.packageType} 推荐命令`);
    }

    async function browseLocalPath(kind, targetId, title) {
      if (!API_BASE) {
        log('当前是 file:// 打开，路径选择需要启动 web_server.py 后访问 http://127.0.0.1:8765');
        return;
      }
      try {
        const target = document.getElementById(targetId);
        const payload = await api('/api/select-path', {
          method: 'POST',
          body: JSON.stringify({ kind, title, initial_dir: target.value || undefined })
        });
        if (payload.cancelled) {
          log('已取消路径选择');
          return;
        }
        target.value = payload.path;
        syncFormToState();
        log(`已选择路径: ${payload.path}`);
      } catch (error) {
        log(`选择路径失败: ${error.message}`);
      }
    }

    async function checkConfig(mode = 'full') {
      syncFormToState();
      const config = { projects: state.projects };
      if (!API_BASE) return validateCurrentProjectFallback(mode);
      try {
        const payload = await api('/api/check', { method: 'POST', body: JSON.stringify({ mode, config }) });
        if (payload.errors?.length) {
          setBadge('校验失败', 'warn');
          payload.errors.forEach(error => log(`校验失败: ${error}`));
          return false;
        }
        setBadge('校验通过', 'ok');
        log(`校验通过: ${currentProject().name}`);
        return true;
      } catch (error) {
        setBadge('校验失败', 'warn');
        log(`校验失败: ${error.message}`);
        return false;
      }
    }

    function validateCurrentProjectFallback(mode = 'upload') {
      syncFormToState();
      const project = currentProject();
      const errors = [];
      if (!project.name) errors.push('项目名不能为空');
      if (!project.path) errors.push('本地项目路径不能为空');
      if (!project.build_command?.length) errors.push('打包命令不能为空或 JSON 格式不正确');
      if (!project.artifact) errors.push('构建产物不能为空');
      if (!project.default_environment) errors.push('默认环境不能为空');
      if (!project.deploy?.host) errors.push('服务器地址不能为空');
      if (!project.deploy?.user) errors.push('服务器用户不能为空');
      if (project.deploy?.auth_type !== 'password' && !project.deploy?.private_key) errors.push('私钥路径不能为空');
      if (!project.deploy?.remote_dir) errors.push('服务器目录不能为空');
      if (!project.deploy?.remote_filename) errors.push('远程文件名不能为空');
      if (mode !== 'upload') {
        if (!project.service?.stop_command) errors.push('优雅停机命令不能为空');
        if (!project.service?.start_command) errors.push('启动命令不能为空');
      }
      if (errors.length) {
        setBadge('校验失败', 'warn');
        errors.forEach(error => log(`校验失败: ${error}`));
        return false;
      }
      setBadge('校验通过', 'ok');
      log(`校验通过: ${project.name}`);
      return true;
    }

    function fallbackSteps(mode) {
      const uploadSteps = [
        { key: 'validate', title: '校验配置' },
        { key: 'env', title: '切换环境' },
        { key: 'build', title: '打包' },
        { key: 'backup', title: '备份旧包' },
        { key: 'upload', title: '上传新包' },
        { key: 'done', title: '完成' }
      ];
      const startSteps = [
        { key: 'validate', title: '校验配置' },
        { key: 'stop', title: '优雅停机' },
        { key: 'start', title: '启动服务' },
        { key: 'wait', title: '等待启动' },
        { key: 'status', title: '状态检查' },
        { key: 'done', title: '完成' }
      ];
      if (mode === 'upload') return uploadSteps;
      if (mode === 'start') return startSteps;
      return [...uploadSteps.slice(0, -1), ...startSteps.slice(1)];
    }

    function renderProgress(steps, activeIndex = -1, status = 'idle') {
      stepList.innerHTML = '';
      steps.forEach((step, index) => {
        const node = document.createElement('span');
        node.className = 'step' + (index < activeIndex || status === 'success' ? ' done' : index === activeIndex ? ' active' : '');
        node.textContent = step.title;
        stepList.appendChild(node);
      });
      const percent = steps.length && activeIndex >= 0 ? Math.round(((activeIndex + 1) / steps.length) * 100) : 0;
      progressBar.style.width = `${status === 'success' ? 100 : percent}%`;
      progressText.textContent = `${status === 'success' ? 100 : percent}%`;
      progressTitle.textContent = activeIndex >= 0 && steps[activeIndex] ? steps[activeIndex].title : '等待操作';
    }

    async function runFlow(mode) {
      if (pollTimer) clearInterval(pollTimer);
      if (!await checkConfig(mode)) return;
      if (!await saveState({ silent: true })) return;
      if (!API_BASE) {
        log('??? file:// ?????????? web_server.py ??? http://127.0.0.1:8765');
        return;
      }
      const project = currentProject();
      setRunning(true);
      lastLogCount = 0;
      activeJobId = null;
      renderProgress(fallbackSteps(mode), 0, 'running');
      try {
        const payload = await api('/api/jobs', {
          method: 'POST',
          body: JSON.stringify({ project_name: project.name, env_name: project.default_environment, mode })
        });
        activeJobId = payload.job.id;
        renderJob(payload.job);
        pollTimer = setInterval(() => pollJob(activeJobId), 1000);
      } catch (error) {
        setRunning(false);
        setBadge('执行失败', 'warn');
        log(`执行失败: ${error.message}`);
      }
    }

    async function pollJob(jobId) {
      try {
        const payload = await api(`/api/jobs/${jobId}`);
        renderJob(payload.job);
        if (['success', 'failed'].includes(payload.job.status)) {
          clearInterval(pollTimer);
          pollTimer = null;
          setRunning(false);
          setBadge(payload.job.status === 'success' ? '执行完成' : '执行失败', payload.job.status === 'success' ? 'ok' : 'warn');
        }
      } catch (error) {
        clearInterval(pollTimer);
        pollTimer = null;
        setRunning(false);
        log(`读取任务状态失败: ${error.message}`);
      }
    }

    function renderJob(job) {
      renderProgress(job.steps || [], job.active_index ?? 0, job.status);
      (job.logs || []).slice(lastLogCount).forEach(line => appendLogLine(line));
      lastLogCount = (job.logs || []).length;
      if (job.error) log(`任务错误: ${job.error}`);
    }

    function addProject() {
      syncFormToState();
      const project = structuredClone(defaultProject);
      project.name = `new-project-${state.projects.length + 1}`;
      project.description = '';
      project.path = '';
      project.package_type = inferPackageType(project);
      state.projects.push(project);
      state.selectedIndex = state.projects.length - 1;
      render();
      log(`已新增项目: ${project.name}`);
    }

    function deleteProject() {
      const project = currentProject();
      if (!confirm(`删除项目 ${project.name || '未命名项目'}？`)) return;
      state.projects.splice(state.selectedIndex, 1);
      if (!state.projects.length) state.projects.push(structuredClone(defaultProject));
      state.selectedIndex = Math.max(0, state.selectedIndex - 1);
      render();
      log('项目已删除，点击保存配置后写入本地文件');
    }

    function copyConfig() {
      syncFormToState();
      const payload = JSON.stringify({ projects: state.projects }, null, 2);
      navigator.clipboard?.writeText(payload).then(() => log('配置 JSON 已复制到剪贴板')).catch(() => { log('当前浏览器不允许直接复制，请手动选择日志中的 JSON'); log(payload); });
    }

    function setRunning(running) {
      actionButtons.forEach(button => { if (button) button.disabled = running; });
    }

    function setBadge(text, type) {
      projectBadge.textContent = text;
      projectBadge.className = 'badge' + (type ? ` ${type}` : '');
    }

    function log(message) {
      appendLogLine(`[${new Date().toLocaleTimeString('zh-CN', { hour12: false })}] ${message}`);
    }

    function appendLogLine(line) {
      logOutput.textContent += line + '\n';
      logOutput.scrollTop = logOutput.scrollHeight;
    }

    function escapeHtml(value) {
      return String(value).replace(/[&<>'"]/g, char => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', "'": '&#39;', '"': '&quot;' }[char]));
    }

    document.getElementById('addProjectBtn').addEventListener('click', addProject);
    document.getElementById('deleteBtn').addEventListener('click', deleteProject);
    document.getElementById('saveBtn').addEventListener('click', () => saveState());
    document.getElementById('validateBtn').addEventListener('click', () => checkConfig('full'));
    document.getElementById('uploadBtn').addEventListener('click', () => runFlow('upload'));
    document.getElementById('startBtn').addEventListener('click', () => runFlow('start'));
    document.getElementById('fullBtn').addEventListener('click', () => runFlow('full'));
    document.getElementById('applyTemplateBtn').addEventListener('click', applyServiceTemplate);
    document.getElementById('clearLogBtn').addEventListener('click', () => logOutput.textContent = '');
    document.getElementById('copyConfigBtn').addEventListener('click', copyConfig);
    document.getElementById('browseProjectPath').addEventListener('click', () => browseLocalPath('directory', 'path', '选择本地项目目录'));
    document.getElementById('browsePrivateKey').addEventListener('click', () => browseLocalPath('file', 'privateKey', '选择 SSH 私钥文件'));
    document.querySelectorAll('input, textarea, select').forEach(input => input.addEventListener('change', () => {
      syncFormToState();
      renderTemplateHint(currentProject());
    }));

    loadStateFromBackend().then(() => {
      render();
      log(API_BASE ? 'Web 后端已连接' : 'HTML 本地缓存模式已加载');
    });
