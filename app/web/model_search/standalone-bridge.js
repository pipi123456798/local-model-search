/* 独立版桥接：把桌面宿主协议（window.wx）映射到本地服务端点。
   由 index.html 在 app.js 之前加载；嵌入 Bambu Studio 时不加载本文件。
   令牌来自启动器打开的地址 hash（#token=…），并缓存到 sessionStorage。 */
(() => {
  'use strict';

  const TOKEN_PATTERN = /(?:^|[#&])token=([0-9a-f]{64})/;
  const SETTINGS_HINT = '已打开用户数据目录：可修改 config.json 后重启；sessions 中包含服务日志。';

  function sessionToken() {
    const match = (location.hash || '').match(TOKEN_PATTERN);
    if (match) {
      try { sessionStorage.setItem('local-search-token', match[1]); } catch { /* 忽略存储失败 */ }
      return match[1];
    }
    try { return sessionStorage.getItem('local-search-token') || ''; } catch { return ''; }
  }

  function reply(payload) {
    if (typeof window.localSearchHost === 'function') window.localSearchHost(payload);
  }

  function detectTheme() {
    try { return matchMedia('(prefers-color-scheme: dark)').matches ? 'dark' : 'light'; } catch { return 'light'; }
  }

  function offline(message) {
    const box = document.getElementById('connection');
    if (!box) return;
    box.textContent = message;
    box.hidden = false;
    box.classList.add('error');
  }

  async function endpoint(path, body) {
    const response = await fetch(path, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json', 'X-Search-Token': sessionToken() },
      body: JSON.stringify(body),
    });
    const data = await response.json().catch(() => ({}));
    if (!response.ok) throw new Error(data.error || '本地服务返回错误');
    return data;
  }

  async function pick(kind) {
    try {
      const data = await endpoint('/api/standalone/pick', { kind });
      if (data.cancelled) return;
      if (kind === 'directory') reply({ command: 'directory', grant: data.grant, path: data.path });
      else reply({ command: 'file', grant: data.grant, name: data.name });
    } catch (error) {
      reply({ error: error.message });
    }
  }

  document.addEventListener('DOMContentLoaded', () => {
    const button = document.getElementById('import-legacy');
    if (!button) return;
    button.addEventListener('click', async () => {
      if (!confirm('将旧版本 data 文件夹中的检索库复制到当前用户目录。原模型不变；已有新库时不会覆盖。请先退出旧版程序，再选择旧 data 文件夹。')) return;
      button.disabled = true;
      try {
        const result = await endpoint('/api/standalone/import-legacy', {});
        if (!result.cancelled) location.reload();
      } catch (error) {
        reply({ error: error.message });
      } finally {
        button.disabled = false;
      }
    });
  });

  window.wx = {
    postMessage(message) {
      let data;
      try { data = JSON.parse(message); } catch { return; }
      switch (data.command) {
        case 'local_search_ready': {
          const token = sessionToken();
          if (token) reply({ token, native: false, theme: detectTheme() });
          else {
            reply({ error: '未检测到本地会话令牌，请重新打开本地模型检索应用。' });
            offline('未检测到本地会话令牌 —— 请重新打开本地模型检索应用。');
          }
          break;
        }
        case 'local_search_choose_directory': pick('directory'); break;
        case 'local_search_choose_file': pick('file'); break;
        case 'local_search_settings':
          endpoint('/api/standalone/data-folder', {})
            .then(() => reply({ error: SETTINGS_HINT }))
            .catch(error => reply({ error: error.message }));
          break;
        case 'local_search_reveal':
          endpoint('/api/standalone/reveal', { ticket: data.ticket })
            .catch(error => reply({ error: error.message }));
          break;
        case 'local_search_edit':
          endpoint('/api/standalone/edit', { ticket: data.ticket, program: data.program || '' })
            .then(result => reply({ command: 'edit', mode: result.mode, name: result.name, program: result.program }))
            .catch(error => reply({ error: error.message }));
          break;
        default: break;
      }
    },
  };
})();
