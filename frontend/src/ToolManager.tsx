import { type FormEvent, useCallback, useEffect, useId, useState } from 'react';
import { Terminal } from 'lucide-react';
import { type McpSettings, type Skill, type ToolDefinition, request } from './api';
import { errorText } from './useWorkspace';

type SkillDraft = Omit<Skill, 'id'>;
type Panel = 'servers' | 'tools' | 'skills';
type Save = (path: string, payload?: object) => Promise<boolean>;

const PANELS: Record<Panel, string> = { servers: 'MCP 服务', tools: '工具列表', skills: 'Skills' };
const EMPTY_SKILL: SkillDraft = { name: '', trigger: '', instructions: '', enabled: true };
const EXAMPLE_SKILL: SkillDraft = {
  name: '科研结果报告',
  trigger: '生成科研结果报告',
  instructions: '按任务信息、计算口径、结果和日志证据四部分呈现。引用任务 ID，并说明日志中的错误或截断情况。',
  enabled: true,
};

function Servers({ settings, busy, save }: { settings: McpSettings; busy: boolean; save: Save }) {
  const [name, setName] = useState('');
  const [endpoint, setEndpoint] = useState('');
  const selected = settings.endpoints.includes(endpoint) ? endpoint : settings.endpoints[0];

  async function add(event: FormEvent) {
    event.preventDefault();
    if (await save('/api/mcp/servers', { name, endpoint: selected })) setName('');
  }

  return (
    <div className="extension-stack">
      <section className="catalog-card">
        <div className="extension-row">
          <div className="catalog-title"><Terminal size={18} aria-hidden="true" /><h2>本机科研任务服务</h2></div>
          <span className="muted">stdio · {settings.research_added ? '已添加' : '待添加'}</span>
          <button type="button" className={settings.research_added ? 'secondary-button' : 'primary-button'}
            disabled={busy} onClick={() => void save('/api/mcp/research')}>
            {settings.research_added ? '重新发现本机工具' : '添加本机 MCP'}
          </button>
        </div>
        <details className="extension-details">
          <summary>查看启动命令</summary>
          <pre>{settings.research_command.map(part => part.includes(' ') ? JSON.stringify(part) : part).join(' ')}</pre>
        </details>
      </section>
      <section className="catalog-card">
        <div className="catalog-title"><h2>添加外部 Streamable HTTP 服务</h2></div>
        {selected ? <form className="extension-form" onSubmit={add}>
          <label>服务名称
            <input value={name} onChange={event => setName(event.target.value)} maxLength={80} required />
          </label>
          <label>服务地址
            <select value={selected} onChange={event => setEndpoint(event.target.value)}>
              {settings.endpoints.map(url => <option key={url} value={url}>{url}</option>)}
            </select>
          </label>
          <div className="extension-actions">
            <button className="primary-button" disabled={busy || !name.trim()}>连接并发现工具</button>
          </div>
        </form> : <p>在 <code>.env</code> 的 <code>MCP_HTTP_ENDPOINTS</code> 中登记可信地址并重启后端。</p>}
      </section>
      {settings.servers.map(server => (
        <article className="catalog-card" key={server.id}>
          <div className="extension-row">
            <div className="catalog-title"><h2>{server.name}</h2></div>
            <span className="muted">{server.tool_count} 个工具 · {server.available ? (server.enabled ? '已启用' : '已停用') : '地址未获授权'}</span>
            <label className="extension-switch">
              <input type="checkbox" checked={server.enabled} disabled={busy}
                aria-label={'启用服务 ' + server.name}
                onChange={event => void save('/api/mcp/servers/' + server.id + '/enabled', { enabled: event.target.checked })} />
              启用
            </label>
            <button type="button" className="secondary-button" disabled={busy || !server.available}
              onClick={() => void save('/api/mcp/servers/' + server.id + '/refresh')}>刷新工具</button>
          </div>
          <p><code>{server.endpoint}</code></p>
        </article>
      ))}
    </div>
  );
}

function Tools({ tools, busy, save }: { tools: ToolDefinition[]; busy: boolean; save: Save }) {
  if (!tools.length) return <p className="muted">添加 MCP 服务后，这里会显示发现的工具。</p>;
  return (
    <div className="tool-catalog">
      {tools.map(tool => (
        <article className="catalog-card" key={tool.name}>
          <div className="catalog-title"><Terminal size={18} aria-hidden="true" /><h2><code>{tool.display_name}</code></h2></div>
          <p>{tool.server_name} · {tool.transport}</p>
          <p>{tool.description}</p>
          {tool.required ? <p><strong>必需工具</strong></p> : (
            <div className="extension-row">
              <label className="extension-switch">
                <input type="checkbox" checked={tool.enabled} disabled={busy || !tool.available}
                  aria-label={'启用工具 ' + tool.display_name + ' · ' + tool.server_name}
                  onChange={event => void save('/api/mcp/tools/' + tool.name + '/enabled', { enabled: event.target.checked })} />
                启用
              </label>
              {!tool.available && <small className="muted">工具不可用</small>}
              {tool.available && !tool.server_enabled && <small className="muted">所属服务已停用</small>}
            </div>
          )}
          <details className="extension-details">
            <summary>查看参数</summary>
            <pre>{JSON.stringify(tool.parameters, null, 2)}</pre>
          </details>
        </article>
      ))}
    </div>
  );
}

function Skills({ skills, busy, save }: { skills: Skill[]; busy: boolean; save: Save }) {
  const instructionsLabel = useId();
  const [selected, setSelected] = useState<string>();
  const [draft, setDraft] = useState<SkillDraft>(EMPTY_SKILL);

  function edit(skill?: Skill) {
    setSelected(skill?.id);
    setDraft(skill ? {
      name: skill.name, trigger: skill.trigger, instructions: skill.instructions, enabled: skill.enabled,
    } : EMPTY_SKILL);
  }

  async function submit(event: FormEvent) {
    event.preventDefault();
    if (await save('/api/skills' + (selected ? '/' + selected : ''), draft)) edit();
  }

  return (
    <div className="extension-stack">
      <section className="catalog-card">
        <div className="extension-row">
          <div className="catalog-title"><h2>{selected ? '编辑 Skill' : '添加文字型 Skill'}</h2></div>
          {selected ? <button className="secondary-button" onClick={() => edit()}>新建 Skill</button>
            : <button className="secondary-button" onClick={() => setDraft(EXAMPLE_SKILL)}>填入报告示例</button>}
        </div>
        <form className="extension-form" onSubmit={submit}>
          <label>名称<input required maxLength={80} value={draft.name}
            onChange={event => setDraft({ ...draft, name: event.target.value })} /></label>
          <label>触发短语<input required minLength={2} maxLength={120} value={draft.trigger}
            onChange={event => setDraft({ ...draft, trigger: event.target.value })} /></label>
          <label><span id={instructionsLabel}>说明</span>
            <textarea aria-labelledby={instructionsLabel} required rows={4} maxLength={1600} value={draft.instructions}
            onChange={event => setDraft({ ...draft, instructions: event.target.value })} /></label>
          <div className="extension-row">
            <label className="extension-switch"><input type="checkbox" checked={draft.enabled}
              onChange={event => setDraft({ ...draft, enabled: event.target.checked })} />启用</label>
            <button className="primary-button" disabled={busy}>保存 Skill</button>
          </div>
        </form>
      </section>
      {skills.map(skill => (
        <article className="catalog-card extension-row" key={skill.id}>
          <div className="catalog-title"><h2>{skill.name}</h2></div>
          <code>{skill.trigger}</code><span className="muted">{skill.enabled ? '已启用' : '已停用'}</span>
          <button className="secondary-button" disabled={busy} onClick={() => edit(skill)}>编辑</button>
        </article>
      ))}
    </div>
  );
}

export function ToolManager() {
  const [panel, setPanel] = useState<Panel>('servers');
  const [settings, setSettings] = useState<McpSettings>();
  const [tools, setTools] = useState<ToolDefinition[]>([]);
  const [skills, setSkills] = useState<Skill[]>([]);
  const [error, setError] = useState('');
  const [notice, setNotice] = useState('');
  const [busy, setBusy] = useState(false);

  const refresh = useCallback(async (signal?: AbortSignal) => {
    const [servers, catalog, templates] = await Promise.all([
      request<McpSettings>('/api/mcp/servers', { signal }),
      request<ToolDefinition[]>('/api/tools', { signal }),
      request<Skill[]>('/api/skills', { signal }),
    ]);
    if (signal?.aborted) return;
    setSettings(servers);
    setTools(catalog);
    setSkills(templates);
  }, []);

  useEffect(() => {
    const controller = new AbortController();
    void refresh(controller.signal).catch(failure => {
      if (!controller.signal.aborted) setError(errorText(failure));
    });
    return () => controller.abort();
  }, [refresh]);

  async function save(path: string, payload?: object) {
    setBusy(true);
    setError('');
    setNotice('');
    try {
      await request(path, {
        method: 'POST',
        headers: payload ? { 'Content-Type': 'application/json' } : undefined,
        body: payload ? JSON.stringify(payload) : undefined,
      });
      await refresh();
      setNotice(path === '/api/mcp/research' ? '已发现并启用三个科研工具。' : '设置已更新。');
      return true;
    } catch (failure) {
      setError(errorText(failure));
      return false;
    } finally {
      setBusy(false);
    }
  }

  return (
    <div className="management-content">
      <header className="management-heading"><h1>工具管理</h1></header>
      <nav className="extension-tabs" aria-label="工具管理分类">
        {(Object.keys(PANELS) as Panel[]).map(item => (
          <button type="button" key={item} className="secondary-button" aria-pressed={panel === item}
            onClick={() => setPanel(item)}>{PANELS[item]}</button>
        ))}
      </nav>
      {error && <p className="tone-error" role="alert">{error}</p>}
      {(notice || busy) && <p className="muted" role="status">{busy ? '正在处理…' : notice}</p>}
      {!settings && !error && <p className="muted">正在读取工具配置…</p>}
      {panel === 'servers' && settings && <Servers settings={settings} busy={busy} save={save} />}
      {panel === 'tools' && <Tools tools={tools} busy={busy} save={save} />}
      {panel === 'skills' && <Skills skills={skills} busy={busy} save={save} />}
    </div>
  );
}
