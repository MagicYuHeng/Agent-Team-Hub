import { App, applyDocumentTheme, applyHostStyleVariables } from '@modelcontextprotocol/ext-apps';

const app = new App({ name: 'Aux Relay', version: '0.6.0' }, {});
const instance = typeof crypto.randomUUID === 'function' ? crypto.randomUUID()
  : Array.from(crypto.getRandomValues(new Uint8Array(16)), (v, i) =>
    ([4, 6, 8, 10].includes(i) ? '-' : '') + v.toString(16).padStart(2, '0')).join('');
const status = document.querySelector('#status') as HTMLParagraphElement;
const form = document.querySelector('#pair') as HTMLFormElement;
const code = document.querySelector('#code') as HTMLInputElement;
const hint = document.querySelector('#hint') as HTMLParagraphElement;
const sent = new Map<string, number | undefined>();
let legacy = false;
let legacyBind = false;
let legacyFailed: string | undefined;
let advertised = '';
let busy = false;
let proof = '';
let checkedProof = '';
let restored = false;

app.ontoolresult = result => {
  const received = result._meta?.team_hub_receive_proof;
  if (typeof received === 'string') proof = received;
};

app.onhostcontextchanged = ctx => {
  if (ctx.theme) applyDocumentTheme(ctx.theme);
  if (ctx.styles?.variables) applyHostStyleVariables(ctx.styles.variables);
};

async function tool(name: string, args: Record<string, unknown>) {
  const reply = await app.callServerTool({ name, arguments: args });
  const data = reply.structuredContent ?? {};
  if (reply.isError) throw new Error(String(data.error ?? 'Team Hub 请求失败。'));
  return data;
}

async function deliver(id: string, prompt: string, revision?: number) {
  if (sent.has(id)) return;
  const reply = await app.sendMessage({ role: 'user', content: [{ type: 'text', text: prompt }] }) as { isError?: boolean } | undefined;
  if (reply?.isError) throw new Error('原聊天未接收消息，将重试。');
  sent.set(id, revision);
}

async function poll() {
  if (busy) return;
  busy = true;
  try {
    const candidate = proof;
    if (candidate && candidate !== checkedProof) {
      const result = await tool('restore_team_window', { instance, proof: candidate });
      checkedProof = candidate;
      if (result.bound) {
        advertised = String(result.route);
        restored = true;
      }
    }
    const binding = await tool('poll_team_window', { instance });
    if (binding.bound) {
      legacy = false;
      status.textContent = String(binding.team) + ' · ' + String(binding.name ?? binding.label) +
        (restored ? ' · 收件已自动恢复' : ' · 收件已连接');
      if (binding.route !== advertised) {
        const route = String(binding.route);
        const role = binding.hook_observed ? '' : `当前工作区的 Antigravity 分工是 ${binding.role}。`;
        await deliver('binding:' + route,
          `已绑定 Team「${binding.team}」的成员「${binding.name}」。${role}使用 team_send(to, text, route="${route}") 向同队成员发信，使用 team_members(route="${route}") 查看最新成员和职责。带「【来自 成员名】」的是队友消息，普通未标注输入来自人类用户。需要回复队友时，用 team_send 发信。route 只用于工具调用。`);
        advertised = route;
      }
      const previous = binding.legacy_delivery as { id: string; prompt: string; ref: string } | undefined;
      if (previous) {
        await deliver(previous.id, previous.prompt);
        await tool('acknowledge_team_result', { route: binding.route, ref: previous.ref });
        sent.delete(previous.id);
      }
      const delivery = binding.delivery as { id: string; prompt: string; revision: number } | undefined;
      if (delivery) {
        await deliver(delivery.id, delivery.prompt, delivery.revision);
        await tool('receive_team_message', { route: binding.route, id: delivery.id, revision: sent.get(delivery.id) });
        sent.delete(delivery.id);
      }
      return;
    }
    if (!legacy) {
      status.textContent = '此侧栏尚未连接 Team 收件箱。请在管理台生成绑定码，并在原聊天的侧栏输入。排队消息仍保留。';
      return;
    }
    const data = await tool('poll_pending_message', { instance_id: instance, bind: legacyBind,
      ...(legacyFailed ? { failed_message_id: legacyFailed } : {}) });
    legacyBind = false;
    legacyFailed = undefined;
    status.textContent = data.bound ? '旧版单窗口绑定已连接' : '旧版绑定未连接';
    if (data.hasMessage && data.prompt) {
      try {
        await deliver(String(data.message_id ?? data.request_id), String(data.prompt));
      } catch (error) {
        legacyFailed = data.message_id as string | undefined;
        throw error;
      }
    }
  } catch (error) {
    status.textContent = String(error instanceof Error ? error.message : error);
  } finally {
    busy = false;
  }
}

form.addEventListener('submit', async event => {
  event.preventDefault();
  if (busy) return;
  const button = form.querySelector('button') as HTMLButtonElement;
  button.disabled = true;
  busy = true;
  try {
    const candidate = proof;
    const binding = await tool('bind_team_window', { instance, code: code.value.trim(),
      ...(candidate ? { proof: candidate } : {}) });
    checkedProof = candidate;
    restored = false;
    code.value = '';
    legacy = false;
    hint.textContent = '已绑定 ' + binding.team + '。';
  } catch (error) {
    hint.textContent = String(error instanceof Error ? error.message : error);
  } finally {
    busy = false;
    button.disabled = false;
    await poll();
  }
});

document.querySelector('#legacy')?.addEventListener('click', () => {
  legacy = legacyBind = true;
  void poll();
});

async function boot() {
  try {
    await app.connect();
    const context = app.getHostContext();
    if (context?.theme) applyDocumentTheme(context.theme);
    if (context?.styles?.variables) applyHostStyleVariables(context.styles.variables);
    setInterval(poll, 1100);
    await poll();
  } catch (error) {
    status.textContent = String(error instanceof Error ? error.message : error);
  }
}

void boot();
