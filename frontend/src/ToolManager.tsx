import { type ChangeEvent, type FormEvent, useCallback, useEffect, useId, useRef, useState } from 'react';
import { ChevronRight, Cpu, Plus, Server, Terminal, X } from 'lucide-react';
import { type McpSettings, type McpTransport, type Skill, type SkillDetail, type ToolDefinition, accountUrl, request } from './api';
import { errorText } from './useWorkspace';

type Panel = 'servers' | 'tools' | 'skills';
type Save = (path: string, payload?: object) => Promise<boolean>;

const PANELS: Record<Panel, string> = { servers: 'MCP 服务', tools: '工具列表', skills: 'Skills' };
const TRANSPORTS: Record<McpTransport, string> = {
  'streamable-http': 'Streamable HTTP',
  sse: 'SSE（旧版）',
  stdio: 'stdio（本机命令）',
};
const EMPTY_SKILL = '---\nname: \ndescription: \n---\n\n## 执行步骤\n\n## 输出\n';
const EXAMPLE_SKILL = `---
name: research-result-report
description: 当用户想把背景分析、统计结果或已完成的科研任务整理成报告、实验记录或结果汇总时使用。
---

## 执行步骤
1. 确认要整理的任务，按需查询状态并读取日志。
2. 依据实际结果说明计算口径，标明失败原因或日志截断。

## 输出
按任务信息、计算口径、分析结果、日志证据四节输出，并引用任务 ID。
`;

function AddMcpForm({ settings, busy, save, close }: {
  settings: McpSettings; busy: boolean; save: Save; close: () => void;
}) {
  const trusted = useId();
  const [customName, setCustomName] = useState<string>();
  const [address, setAddress] = useState('');
  const [profile, setProfile] = useState('');
  const [transport, setTransport] = useState<McpTransport>('streamable-http');
  const local = transport === 'stdio';
  const endpoint = local ? profile : address.trim();
  const configError = local ? settings.stdio_config_error : null;
  const defaultName = local ? profile : '';
  const enteredName = customName ?? defaultName;
  const name = enteredName.trim() || defaultName;
  const canAdd = !busy && !configError && Boolean(name && endpoint);

  async function add(event: FormEvent) {
    event.preventDefault();
    if (canAdd && await save('/api/mcp/servers', { name, transport, endpoint })) close();
  }

  return (
    <section className="catalog-card">
      <div className="catalog-title"><h2>连接新的 MCP 服务</h2></div>
      <form className="extension-form" onSubmit={add}>
        <div className="mcp-fields">
          <label>服务名称
            <input value={enteredName} onChange={event => setCustomName(event.target.value)} maxLength={80}
              placeholder={local ? '默认使用本机服务名' : '请输入服务名称'} required={!local} disabled={busy} />
          </label>
          <label>连接方式
            <select value={transport} onChange={event => setTransport(event.target.value as McpTransport)} disabled={busy}>
              {(Object.keys(TRANSPORTS) as McpTransport[]).map(type =>
                <option key={type} value={type}>{TRANSPORTS[type]}</option>)}
            </select>
          </label>
        </div>
        {local ? <>
          <label>本机服务
            <select value={profile} onChange={event => setProfile(event.target.value)}
              required disabled={busy || !!configError || settings.stdio_servers.length === 0}>
              <option value="">{settings.stdio_servers.length ? '选择已登记的服务' : '尚无已登记的本机服务'}</option>
              {settings.stdio_servers.map(server => <option key={server} value={server}>{server}</option>)}
            </select>
          </label>
          {configError
            ? <small className="tone-error" role="alert">{configError}</small>
            : settings.stdio_servers.length === 0 &&
              <small className="muted">在 <code>.env</code> 的 <code>MCP_STDIO_SERVERS</code> 中登记本机命令后，重新打开添加表单。</small>}
        </> : <>
          <label>服务地址
            <input type="url" value={address} onChange={event => setAddress(event.target.value)}
              maxLength={512} placeholder={transport === 'sse' ? 'https://example.com/sse' : 'https://example.com/mcp'}
              list={trusted} required disabled={busy} />
          </label>
          <datalist id={trusted}>
            {settings.trusted_endpoints.map(url => <option key={url} value={url} />)}
          </datalist>
        </>}
        <div className="extension-row">
          <button className="primary-button" disabled={!canAdd}>连接并添加</button>
          <button type="button" className="secondary-button" disabled={busy} onClick={close}>取消</button>
        </div>
      </form>
    </section>
  );
}

function EnableSwitch({ checked, disabled, label, text, onChange }: {
  checked: boolean; disabled: boolean; label: string; text?: string; onChange: (enabled: boolean) => void;
}) {
  return <label className="setting-switch">
    <input type="checkbox" role="switch" aria-label={label} checked={checked} disabled={disabled}
      onChange={event => onChange(event.target.checked)} />
    <span>{text ?? (checked ? '已启用' : '已停用')}</span>
  </label>;
}

function Servers({ settings, busy, save, onOpenAdd }: {
  settings: McpSettings; busy: boolean; save: Save; onOpenAdd: () => void;
}) {
  const [adding, setAdding] = useState(false);

  function openAdd() {
    setAdding(true);
    onOpenAdd();
  }

  return (
    <div className="extension-stack">
      <div className="mcp-list-heading">
        <h2>已接入的 MCP</h2>
        {!adding && <button type="button" className="primary-button" disabled={busy} onClick={openAdd}>
          <Plus size={16} aria-hidden="true" /> 添加 MCP
        </button>}
      </div>
      {adding && <AddMcpForm settings={settings} busy={busy} save={save} close={() => setAdding(false)} />}
      <section className="catalog-card">
        <div className="extension-row">
          <div className="catalog-title"><Terminal size={18} aria-hidden="true" /><h2>{settings.research.name}</h2></div>
          <span className="muted">内置 · 自动接入 · stdio · {settings.research.tool_count} 个工具</span>
        </div>
      </section>
      {settings.servers.map(server => (
        <article className="catalog-card" key={server.id}>
          <div className="extension-row">
            <div className="catalog-title"><h2>{server.name}</h2></div>
            <span className="muted">{TRANSPORTS[server.transport]} · {server.tool_count} 个工具{!server.available && ' · 连接配置不可用'}</span>
            <EnableSwitch checked={server.enabled} disabled={busy} label={'启用服务 ' + server.name}
              onChange={enabled => void save('/api/mcp/servers/' + server.id + '/enabled', { enabled })} />
            <button type="button" className="secondary-button" disabled={busy || !server.available}
              onClick={() => void save('/api/mcp/servers/' + server.id + '/refresh')}>刷新工具</button>
          </div>
          <p><code>{server.endpoint}</code></p>
        </article>
      ))}
    </div>
  );
}

function ToolDetails({ tool, close }: { tool: ToolDefinition; close: () => void }) {
  const dialog = useRef<HTMLDialogElement>(null);
  const title = useId();
  useEffect(() => {
    const element = dialog.current;
    element?.showModal();
    return () => element?.close();
  }, []);

  return (
    <dialog ref={dialog} className="task-detail tool-dialog" aria-labelledby={title} onClose={close}>
      <div className="detail-content">
        <header className="detail-header">
          <h2 id={title}><code>{tool.display_name}</code></h2>
          <button type="button" className="detail-close" aria-label="关闭工具详情" onClick={() => dialog.current?.close()}>
            <X size={18} aria-hidden="true" />
          </button>
        </header>
        <p className="tool-origin">{tool.server_name} · {TRANSPORTS[tool.transport]}</p>
        {tool.description && <p className="tool-full-description">{tool.description}</p>}
        <h3>输入参数</h3>
        <pre>{JSON.stringify(tool.parameters, null, 2)}</pre>
      </div>
    </dialog>
  );
}

function Tools({ tools, busy, onToggle }: {
  tools: ToolDefinition[]; busy: boolean; onToggle: (name: string, enabled: boolean) => void;
}) {
  const [selected, setSelected] = useState<string | null>(null);
  const detail = tools.find(tool => tool.name === selected);
  const groups = new Map<string | null, ToolDefinition[]>();
  for (const tool of tools) {
    const group = groups.get(tool.server_id);
    if (group) group.push(tool);
    else groups.set(tool.server_id, [tool]);
  }
  if (!tools.length) return <p className="muted">添加 MCP 服务后，这里会显示发现的工具。</p>;
  return (
    <div className="tool-groups">
      {Array.from(groups, ([serverId, members]) => {
        const server = members[0];
        return <section className="tool-group" key={serverId ?? 'research'} aria-label={server.server_name}>
          <header className="tool-group-heading">
            <div className="tool-group-name">
              {serverId === null ? <Cpu size={18} aria-hidden="true" /> : <Server size={18} aria-hidden="true" />}
              <h2>{server.server_name}</h2>
              <span className="tool-transport">{server.transport === 'stdio' ? 'stdio' : TRANSPORTS[server.transport]}</span>
            </div>
            <span className="tool-count">{members.length} 个工具</span>
          </header>
          {!server.server_enabled && <p className="tool-service-paused">服务已停用，以下设置将在服务启用后生效。</p>}
          <div className="tool-catalog">
            {members.map(tool => <article className="tool-card" key={tool.name}>
              <div className="tool-card-heading">
                <span className="tool-icon"><Terminal size={16} aria-hidden="true" /></span>
                <h3 title={tool.display_name}><code>{tool.display_name}</code></h3>
              </div>
              <p className="tool-preview">{tool.description}</p>
              <div className="tool-card-footer">
                <EnableSwitch checked={tool.enabled} disabled={busy || (!tool.available && !tool.enabled)}
                  label={'启用工具 ' + tool.display_name + ' · ' + tool.server_name}
                  text={!tool.available ? '不可用' : !tool.server_enabled && tool.enabled ? '待生效' : undefined}
                  onChange={enabled => onToggle(tool.name, enabled)} />
                <button type="button" className="tool-details-link" aria-label={'查看工具详情 ' + tool.display_name}
                  onClick={() => setSelected(tool.name)}>详情<ChevronRight size={14} aria-hidden="true" /></button>
              </div>
            </article>)}
          </div>
        </section>;
      })}
      {detail && <ToolDetails tool={detail} close={() => setSelected(null)} />}
    </div>
  );
}

function fileBase64(file: File): Promise<string> {
  return new Promise((resolve, reject) => {
    const reader = new FileReader();
    reader.onerror = () => reject(new Error('无法读取技能附件。'));
    reader.onload = () => resolve(String(reader.result).split(',', 2)[1] ?? '');
    reader.readAsDataURL(file);
  });
}

function Skills({ skills, busy, save }: { skills: Skill[]; busy: boolean; save: Save }) {
  const editorId = useId();
  const folderPicker = useRef<HTMLInputElement>(null);
  const [selected, setSelected] = useState<SkillDetail>();
  const [editing, setEditing] = useState(false);
  const [document, setDocument] = useState(EMPTY_SKILL);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState('');
  const blocked = busy || loading;

  function create() {
    setSelected(undefined);
    setDocument(EMPTY_SKILL);
    setError('');
    setEditing(true);
  }

  async function edit(id: string) {
    setError('');
    setLoading(true);
    try {
      const detail = await request<SkillDetail>('/api/skills/' + id);
      setSelected(detail);
      setDocument(detail.document);
      setEditing(true);
    } catch (failure) {
      setError(errorText(failure));
    } finally {
      setLoading(false);
    }
  }

  async function submit(event: FormEvent) {
    event.preventDefault();
    const existing = skills.find(skill => skill.id === selected?.id);
    if (await save('/api/skills' + (selected ? '/' + selected.id : ''), {
      document, enabled: existing?.enabled ?? true,
    })) setEditing(false);
  }

  async function importFolder(event: ChangeEvent<HTMLInputElement>) {
    const files = Array.from(event.target.files ?? []);
    event.target.value = '';
    if (!files.length) return;
    setError('');
    setLoading(true);
    try {
      const [folder] = files[0].webkitRelativePath.split('/');
      const file = files.find(entry => entry.webkitRelativePath === folder + '/SKILL.md');
      if (!file) throw new Error('请选择根目录下包含 SKILL.md 的技能文件夹。');
      const resources = files.filter(entry => entry !== file);
      if (files.length > 64 || file.size > 12 * 1024 ||
          resources.reduce((size, entry) => size + entry.size, 0) > 2 * 1024 * 1024) {
        throw new Error('单个技能目录最多 64 个文件，SKILL.md 最大 12 KiB，附件总共最大 2 MiB。');
      }
      const attachments = await Promise.all(resources.map(async entry =>
        [entry.webkitRelativePath.slice(folder.length + 1), await fileBase64(entry)] as const,
      ));
      if (await save('/api/skill-bundles', {
        folder, document: new TextDecoder('utf-8', { fatal: true }).decode(await file.arrayBuffer()),
        files: Object.fromEntries(attachments), enabled: false,
      })) setEditing(false);
    } catch (failure) {
      setError(errorText(failure));
    } finally {
      setLoading(false);
    }
  }

  return (
    <div className="extension-stack">
      <div className="mcp-list-heading">
        <h2>已添加的技能</h2>
        <div className="extension-row">
          <input type="file" ref={element => {
            folderPicker.current = element;
            element?.setAttribute('webkitdirectory', '');
          }} hidden onChange={event => void importFolder(event)} />
          <button type="button" className="secondary-button" disabled={blocked}
            onClick={() => folderPicker.current?.click()}>导入目录</button>
          <button type="button" className="primary-button" disabled={blocked} onClick={create}>
            <Plus size={16} aria-hidden="true" /> 新建 Skill
          </button>
        </div>
      </div>
      {error && <p className="tone-error" role="alert">{error}</p>}
      {editing && <section className="catalog-card skill-editor">
        <div className="mcp-list-heading">
          <h2>{selected ? '编辑 Skill' : '新建 Skill'}</h2>
          {!selected && <button type="button" className="secondary-button" disabled={blocked}
            onClick={() => setDocument(EXAMPLE_SKILL)}>填入报告示例</button>}
        </div>
        <form className="extension-form" onSubmit={submit}>
          <label htmlFor={editorId}>SKILL.md</label>
          <p id={editorId + '-hint'}>在 YAML 中填写 <code>name</code> 和 <code>description</code>，在 Markdown 正文中写执行步骤与输出要求。</p>
          <textarea id={editorId} aria-describedby={editorId + '-hint'} rows={13} required maxLength={12288}
            spellCheck={false} value={document} onChange={event => setDocument(event.target.value)} />
          {selected && selected.files.length > 0 && <details>
            <summary>目录附件（{selected.files.length}）</summary>
            <ul>{selected.files.map(file => <li key={file}><code>{file}</code></li>)}</ul>
          </details>}
          <div className="extension-row">
            <button className="primary-button" disabled={blocked}>保存 Skill</button>
            <button type="button" className="secondary-button" disabled={blocked} onClick={() => setEditing(false)}>取消</button>
          </div>
        </form>
      </section>}
      {skills.length === 0 && !editing && <div className="management-empty">
        <p>添加技能后，助手会按任务需要加载。导入的技能可在检查内容后启用。</p>
      </div>}
      {skills.map(skill => (
        <article className="catalog-card skill-card" key={skill.id}>
          <div>
            <div className="catalog-title"><h2><code>{skill.name}</code></h2></div>
            <p>{skill.description}</p>
            <p className="skill-command">手动使用 <code>/skill {skill.name} 你的请求</code></p>
          </div>
          <div className="extension-row">
            <EnableSwitch checked={skill.enabled} disabled={blocked} label={'启用技能 ' + skill.name}
              onChange={enabled => void save('/api/skills/' + skill.id + '/enabled', { enabled })} />
            <button className="secondary-button" disabled={blocked} onClick={() => void edit(skill.id)}>编辑</button>
            <a className="secondary-button skill-download" href={accountUrl('/api/skills/' + skill.id + '/bundle')}>下载目录</a>
          </div>
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
    const servers = await request<McpSettings>('/api/mcp/servers', { signal });
    const [catalog, templates] = await Promise.all([
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

  async function refreshForAdd() {
    setBusy(true);
    setError('');
    setNotice('');
    try {
      await refresh();
    } catch (failure) {
      setError(errorText(failure));
    } finally {
      setBusy(false);
    }
  }

  async function save(path: string, payload?: object, applyChange?: () => void) {
    setBusy(true);
    setError('');
    setNotice('');
    try {
      const result = await request<{ tool_count?: number }>(path, {
        method: 'POST',
        headers: payload ? { 'Content-Type': 'application/json' } : undefined,
        body: payload ? JSON.stringify(payload) : undefined,
      });
      if (path === '/api/mcp/servers') {
        setNotice(`已添加服务，发现 ${result.tool_count} 个工具。可前往「工具列表」启用。`);
      } else if (path.endsWith('/refresh')) {
        setNotice(`已刷新工具列表，共 ${result.tool_count} 个工具。`);
      } else {
        setNotice('设置已更新。');
      }
      if (applyChange) applyChange();
      else await refresh().catch(failure => setError(errorText(failure)));
      return true;
    } catch (failure) {
      setError(errorText(failure));
      return false;
    } finally {
      setBusy(false);
    }
  }

  function toggleTool(name: string, enabled: boolean) {
    void save('/api/mcp/tools/' + encodeURIComponent(name) + '/enabled', { enabled }, () => {
      setTools(current => current.map(tool => tool.name === name ? { ...tool, enabled } : tool));
    });
  }

  return (
    <div className="management-content tool-management">
      <header className="management-heading"><h1>扩展管理</h1></header>
      <nav className="extension-tabs" aria-label="扩展管理分类">
        {(Object.keys(PANELS) as Panel[]).map(item => (
          <button type="button" key={item} aria-pressed={panel === item}
            onClick={() => setPanel(item)}>{PANELS[item]}</button>
        ))}
      </nav>
      {error && <p className="tone-error" role="alert">{error}</p>}
      {(notice || busy) && <p className={panel === 'tools' ? 'sr-only' : 'management-feedback muted'} role="status">
        {busy ? '正在处理…' : notice}
      </p>}
      {!settings && !error && <p className="muted">正在读取工具配置…</p>}
      {panel === 'servers' && settings && <Servers settings={settings} busy={busy} save={save} onOpenAdd={() => void refreshForAdd()} />}
      {panel === 'tools' && settings && <Tools tools={tools} busy={busy} onToggle={toggleTool} />}
      {panel === 'skills' && <Skills skills={skills} busy={busy} save={save} />}
    </div>
  );
}
