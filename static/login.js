'use strict';
const qr = document.getElementById('qr');
const statusBox = document.getElementById('status');
const placeholder = document.getElementById('placeholder');
const restart = document.getElementById('restart');
const cancel = document.getElementById('cancel');
const token = document.querySelector('meta[name="login-token"]').content;
let version = -1, hasQr = false, pending = false;
qr.addEventListener('load', () => { if (hasQr) { qr.hidden = false; placeholder.hidden = true; } });
qr.addEventListener('error', () => { statusBox.textContent = '二维码图片暂时不可用，请查看官方登录窗口。'; });
async function action(path) {
  if (pending) return;
  pending = true;
  restart.disabled = cancel.disabled = true;
  try {
    const response = await fetch(path, {method:'POST', headers:{'X-Login-Token':token}});
    if (!response.ok) throw new Error(response.status === 409 ? '登录仍在进行，请完成或取消当前窗口。' : '操作失败，请刷新登录页面后重试。');
    statusBox.textContent = path === '/cancel' ? '正在取消登录…' : '正在重新打开官方登录…';
  } catch (error) { statusBox.textContent = error.message; }
  finally { pending = false; restart.disabled = cancel.disabled = false; }
}
restart.addEventListener('click', () => action('/start'));
cancel.addEventListener('click', () => action('/cancel'));
async function poll() {
  try {
    const response = await fetch('/status', {cache:'no-store'});
    if (!response.ok) throw new Error('无法读取登录状态，请刷新页面。');
    const state = await response.json();
    const terminal = ['success','error','failed','failure','cancelled'].includes(state.status);
    hasQr = state.has_qr && !terminal;
    statusBox.className = state.status;
    statusBox.setAttribute('role', state.status === 'error' ? 'alert' : 'status');
    if (!pending) statusBox.textContent = state.message || '正在初始化登录，请稍候';
    restart.hidden = state.active;
    cancel.hidden = !state.active;
    placeholder.querySelector('.spinner').hidden = terminal || !state.active;
    document.getElementById('placeholder-text').textContent = state.status === 'success' ? '✓ 登录凭证已保存' : terminal ? '本次登录已结束' : state.status === 'verification_required' ? '请在抖音官方窗口完成身份验证' : '请切换到新打开的抖音官方窗口';
    if (hasQr && state.version !== version) { version = state.version; qr.src = `/qr.png?v=${encodeURIComponent(version)}`; }
    if (!hasQr) { qr.hidden = true; qr.removeAttribute('src'); placeholder.hidden = false; }
  } catch (error) { statusBox.textContent = `${error.message} 请确认登录程序仍在运行。`; }
  setTimeout(poll, 1500);
}
poll();
