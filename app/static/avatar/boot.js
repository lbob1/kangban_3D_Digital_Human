/**
 * 3D 数字人启动器：把渲染引擎包成 window.KangbanAvatar，
 * 供现有通话页的普通 <script> 调用（无需把整页改成 ES Module）。
 */

import { KangbanAvatar } from './engine.js';

const pending = {
  state: 'idle',
  stream: null,
  actions: [],
  hostId: 'avatarHomeSlot',
};

let avatar = null;

function setHomeStatus(text) {
  const el = document.getElementById('avatarHomeStatus');
  if (el) el.textContent = text;
}

async function boot() {
  const host = document.getElementById('avatarHomeSlot');
  if (!host) return;

  try {
    avatar = new KangbanAvatar({
      host,
      onStatus(status) {
        if (status.phase === 'ready') {
          document.body.classList.add('avatar3d-ready');
          setHomeStatus(`3D 数字人已就绪${status.avatar ? '（' + status.avatar + '）' : ''}`);
        } else if (status.phase === 'error') {
          setHomeStatus('数字人加载失败：' + (status.message || '未知错误'));
        } else {
          setHomeStatus(status.message || '数字人加载中…');
        }
      },
    });
    await avatar.init();

    if (pending.hostId && pending.hostId !== 'avatarHomeSlot') {
      const target = document.getElementById(pending.hostId);
      if (target) avatar.moveTo(target);
    }
    avatar.setState(pending.state);
    if (pending.stream) avatar.attachStream(pending.stream);
    for (const [actionId, param] of pending.actions.splice(0)) {
      avatar.playAction(actionId, param);
    }
    document.body.classList.add('avatar3d-ready');
  } catch (error) {
    console.error('[avatar] 初始化失败', error);
    setHomeStatus('3D 数字人不可用：' + (error?.message || error));
  }
}

window.KangbanAvatar = {
  get ready() {
    return Boolean(avatar);
  },
  /** idle / calling / in_call / off */
  setState(state) {
    pending.state = state || 'idle';
    avatar?.setState(pending.state);
  },
  /** 远端 WebRTC MediaStream，用于 AnalyserNode 口型驱动 */
  attachStream(stream) {
    pending.stream = stream || null;
    if (pending.stream) avatar?.attachStream(pending.stream);
  },
  detachStream() {
    pending.stream = null;
    avatar?.detachStream();
  },
  /** 信令 kind=action 触发的动作播放 */
  playAction(actionId, param = null) {
    if (!actionId) return;
    if (avatar) avatar.playAction(actionId, param);
    else pending.actions.push([actionId, param]);
  },
  /** 把画布移动到指定容器（首页卡片 / 通话浮层） */
  mount(hostId) {
    pending.hostId = hostId;
    if (!avatar) return;
    const target = document.getElementById(hostId);
    if (target) avatar.moveTo(target);
  },
  /** 运行时切换形象；注册表来自 /avatars.json */
  async setAvatar(id) {
    if (!avatar) return { ok: false, error: '数字人尚未初始化' };
    return avatar.setAvatar(id);
  },
  /** 当前形象 id */
  getAvatar() {
    return avatar?.avatarId || null;
  },
  /** 可切换的形象列表 */
  listAvatars() {
    return avatar?.listAvatars() || [];
  },
  /** 贴图/几何计数，用于切换前后对比 */
  getMemoryStats() {
    return avatar?.getMemoryStats() || null;
  },
  /** 调试用：控制台可执行 __kbAvatar.playAction('wave') */
  debug() {
    return avatar;
  },
};

boot();
