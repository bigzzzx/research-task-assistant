import { useCallback, useEffect, useLayoutEffect, useMemo, useRef, useState } from 'react';
import type { ChangeEvent, FormEvent, KeyboardEvent as ReactKeyboardEvent } from 'react';
import { X } from 'lucide-react';
import Markdown from 'react-markdown';
import remarkGfm from 'remark-gfm';
import { AccountScreen } from './AccountScreen';
import { accountUrl, bindAccount, request, RequestError, SESSION_CHANGED,
  type Account, type Analysis, type Job, type LocalFile, type Message } from './api';
import { ANALYSIS, Status, showSize } from './display';
import { FileManager, TaskManager, ToolManager } from './Management';
import { Navigation, useSection } from './Navigation';
import { errorText, useWorkspace } from './useWorkspace';

const EXAMPLES = [
  { title: '统计 GC 含量', file: 'sample1.fasta', prompt: '帮我统计一下 sample1 里序列的 GC 含量。' },
  { title: '查找最长 ORF', file: 'sample2.fasta', prompt: '找出 sample2 中每条序列的最长 ORF。' },
  { title: '诊断格式错误', file: 'sample3.fasta', prompt: '对 sample3 做一次序列统计。' },
];

function showTime(value: string): string {
  const date = new Date(value);
  const today = new Date();
  return date.toDateString() === today.toDateString()
    ? date.toLocaleTimeString('zh-CN', { hour: '2-digit', minute: '2-digit' })
    : date.toLocaleDateString('zh-CN', { month: 'numeric', day: 'numeric' });
}

function pretty(raw: string): string {
  try {
    return JSON.stringify(JSON.parse(raw), undefined, 2);
  } catch {
    return raw;
  }
}

type StatsRow = { id: string; length_nt: number; gc_pct: number | null };
type OrfRow = { id: string; orf: { start: number; end: number; length_nt: number } | null };
type ToolOutput = {
  ok: boolean;
  stdout?: string;
  stderr?: string;
  job_id?: string;
  state?: Job['state'];
  analysis_type?: Analysis;
  stdout_truncated?: boolean;
  stderr_truncated?: boolean;
};

function asOutput(message?: Message): ToolOutput | undefined {
  if (!message) return undefined;
  try {
    return JSON.parse(message.content) as ToolOutput;
  } catch {
    return undefined;
  }
}

function ResultTable({ output }: { output: ToolOutput }) {
  const rows: (StatsRow | OrfRow)[] = [];
  for (const line of output.stdout?.split('\n') ?? []) {
    try {
      const row = JSON.parse(line);
      if (typeof row?.id === 'string' && ('gc_pct' in row || 'orf' in row)) rows.push(row);
    } catch {
      // A truncated preview can contain a partial record.
    }
  }
  if (!rows.length) return null;
  const stats = output.analysis_type === 'seq_stats';
  return (
    <div className="result-wrap">
      <table className="results">
        <thead><tr><th>序列 ID</th><th>{stats ? '长度（nt）' : '起止位置'}</th><th>{stats ? 'GC 含量' : '长度（nt）'}</th></tr></thead>
        <tbody>{rows.map((row, index) => (
          <tr key={index}>
            <td>{row.id}</td>
            {'gc_pct' in row ? <>
              <td>{row.length_nt}</td>
              <td>{row.gc_pct === null ? '无法计算' : row.gc_pct.toFixed(2) + '%'}</td>
            </> : <>
              <td>{row.orf ? row.orf.start + '–' + row.orf.end : '无完整 ORF'}</td>
              <td>{row.orf?.length_nt ?? '—'}</td>
            </>}
          </tr>
        ))}</tbody>
      </table>
    </div>
  );
}

function LogResult({ output }: { output: ToolOutput }) {
  const { stdout, stderr, ...fields } = output;
  return (
    <>
      <ResultTable output={output} />
      {(output.stdout_truncated || output.stderr_truncated) && (
        <p className="preview-note">预览已截断。可在任务记录下载完整日志。</p>
      )}
      <pre>{JSON.stringify(fields, undefined, 2)}</pre>
      <h4>标准输出</h4>
      <pre>{stdout || '空'}</pre>
      <h4>错误输出</h4>
      <pre>{stderr || '空'}</pre>
    </>
  );
}

function ToolCards({ invocation, observations }: { invocation: Message; observations: Map<string, Message> }) {
  return (
    <div className="tool-stack">
      {invocation.tool_calls.map(call => {
        const observed = observations.get(invocation.id + '/' + call.id);
        const output = asOutput(observed);
        const isLog = call.function.name === 'read_log' && output?.ok;
        return (
          <details className="tool" key={call.id}>
            <summary>
              <span className="tool-glyph" aria-hidden="true">⌘</span>
              <code>{call.function.name}</code>
              {invocation.source === 'system' && <span className="tool-source">系统读取</span>}
              <span className={'tool-status ' + (output?.ok === false ? 'tone-error' : '')}>
                {observed ? output?.ok === false ? '参数或调用有误' : '已返回' :
                  invocation.status === 'interrupted' ? '已中断' : '等待返回'}
              </span>
            </summary>
            <div className="tool-body">
              <h4>调用参数</h4>
              <pre>{pretty(call.function.arguments)}</pre>
              {observed && <>
                <h4>返回结果</h4>
                {isLog ? <LogResult output={output} /> : <pre>{pretty(observed.content)}</pre>}
              </>}
            </div>
          </details>
        );
      })}
    </div>
  );
}

function MessageView({ message, jobs, observations }: {
  message: Message; jobs: Job[]; observations: Map<string, Message>;
}) {
  if (message.kind === 'tool') return null;
  if (message.kind === 'invocation') return <ToolCards invocation={message} observations={observations} />;
  const user = message.kind === 'user';
  const job = jobs.find(item => item.id === message.job_id);
  if (!user && message.status === 'running' && !message.content) return null;
  return (
    <article className={user ? 'message user-message' : 'message assistant-message'} data-kind={message.kind}>
      {!user && <span className={'avatar ' + (job?.state === 'failed' ? 'avatar-failure' : job ? 'avatar-result' : '')} aria-hidden="true">
        {job?.state === 'failed' ? '!' : job ? '✓' : '研'}
      </span>}
      <div className="message-inner">
        <div className="message-meta">
          <span>{user ? '你' : job ? ANALYSIS[job.analysis_type] + ' · ' + job.filename : '科研计算助手'}</span>
          {job && <code title={job.id}>{job.id.slice(0, 8)}</code>}
          <time dateTime={message.created_at}>{showTime(message.created_at)}</time>
          {message.status === 'interrupted' && <span className="tone-error">已中断</span>}
          {message.status === 'running' && !user && <span className="typing" aria-label="正在输出"><i /><i /><i /></span>}
        </div>
        {user ? (
          <div className="user-body">{message.content}</div>
        ) : (
          <div className={'markdown ' + (message.status === 'error' ? 'tone-error' : '')}>
            <Markdown skipHtml remarkPlugins={[remarkGfm]} components={{ a: ({ children }) => <span>{children}</span> }}>
              {message.content}
            </Markdown>
          </div>
        )}
      </div>
    </article>
  );
}

function JobCards({ jobs, onRetry }: { jobs: Job[]; onRetry: (job: Job) => void }) {
  if (!jobs.length) return <p className="muted rail-empty">提交分析后，可在这里查看状态和日志。</p>;
  return (
    <div className="job-list">
      {jobs.map(job => (
        <article className="job-card" key={job.id}>
          <div className="job-title"><strong title={job.filename}>{job.filename}</strong><Status state={job.state} /></div>
          <div className="job-meta"><span>{ANALYSIS[job.analysis_type]}</span><code title={job.id}>{job.id.slice(0, 8)}</code></div>
          {job.finished_at && <div className="job-meta">
            <span>{job.summary_state === 'running' || job.summary_state === 'pending' ? '等待解释' : showTime(job.finished_at)}</span>
            {job.exit_code !== null && <span>退出码 {job.exit_code}</span>}
          </div>}
          {job.runner_error && <div className="job-reason">{job.runner_error}</div>}
          {job.finished_at && <div className="job-actions">
            {(!job.runner_error || job.exit_code !== null) && <>
              <a href={accountUrl('/api/jobs/' + job.id + '/logs/stdout')}>输出日志</a>
              <a href={accountUrl('/api/jobs/' + job.id + '/logs/stderr')}>错误日志</a>
            </>}
            {job.summary_state === 'error' && <button onClick={() => onRetry(job)}>重新解释</button>}
          </div>}
        </article>
      ))}
    </div>
  );
}

export function App() {
  const [account, setAccount] = useState<Account | null>();
  const [authError, setAuthError] = useState('');
  const checkId = useRef(0);

  const applyAccount = useCallback((user: Account | null) => {
    checkId.current++;
    bindAccount(user ?? undefined);
    setAccount(user);
    setAuthError('');
  }, []);

  const checkSession = useCallback(async () => {
    const check = ++checkId.current;
    try {
      const user = await request<Account>('/api/auth/me');
      if (check === checkId.current) applyAccount(user);
    } catch (error) {
      if (check !== checkId.current) return;
      if (error instanceof RequestError && error.status === 401) applyAccount(null);
      else setAuthError('无法连接本机服务，请确认后端已启动。');
    }
  }, [applyAccount]);

  useEffect(() => {
    void checkSession();
    const check = () => { void checkSession(); };
    window.addEventListener(SESSION_CHANGED, check);
    window.addEventListener('focus', check);
    return () => {
      checkId.current++;
      window.removeEventListener(SESSION_CHANGED, check);
      window.removeEventListener('focus', check);
    };
  }, [checkSession]);

  async function signOut() {
    await request('/api/auth/logout', { method: 'POST' });
    applyAccount(null);
  }

  if (account === undefined) {
    return <main className="account-page"><div className="account-card">
      <p role="status">{authError || '正在连接本机工作区…'}</p>
      {authError && <button className="secondary-button" onClick={() => void checkSession()}>重试</button>}
    </div></main>;
  }
  if (!account) return <AccountScreen onSignIn={applyAccount} />;
  return <Workspace key={account.id} account={account} onLogout={signOut} />;
}

function Workspace({ account, onLogout }: { account: Account; onLogout: () => Promise<void> }) {
  const workspace = useWorkspace(account.id);
  const { section, navigate, label } = useSection();
  const { activeId, room, config } = workspace;
  const [drafts, setDrafts] = useState<Record<string, string>>({});
  const [creating, setCreating] = useState(false);
  const [posting, setPosting] = useState(false);
  const [uploading, setUploading] = useState(false);
  const [mobilePanel, setMobilePanel] = useState<'history' | 'jobs' | null>(null);
  const composer = useRef<HTMLTextAreaElement>(null);
  const form = useRef<HTMLFormElement>(null);
  const fileInput = useRef<HTMLInputElement>(null);
  const scroll = useRef<HTMLDivElement>(null);
  const followEnd = useRef(true);
  const current = workspace.conversations.find(row => row.id === activeId);
  const uploaded = workspace.files.filter(file => file.source === 'upload');
  const history = room.messages;
  const draftKey = activeId || '';
  const draft = drafts[draftKey] ?? '';
  const pending = history.filter(message => message.kind === 'user' && (message.status === 'queued' || message.status === 'running')).length;
  const observations = useMemo(() => new Map(
    history.filter(message => message.kind === 'tool').map(message => [message.invocation_id + '/' + message.tool_call_id, message]),
  ), [history]);

  useEffect(() => {
    followEnd.current = true;
  }, [activeId]);

  useEffect(() => {
    setMobilePanel(null);
  }, [section]);

  useEffect(() => {
    if (!mobilePanel) return;
    const closeOnEscape = (event: KeyboardEvent) => {
      if (event.key === 'Escape') setMobilePanel(null);
    };
    window.addEventListener('keydown', closeOnEscape);
    return () => window.removeEventListener('keydown', closeOnEscape);
  }, [mobilePanel]);

  useLayoutEffect(() => {
    const target = scroll.current;
    if (target && followEnd.current) target.scrollTop = target.scrollHeight;
  }, [activeId, history, section]);

  function fill(text: string) {
    setDrafts(previous => ({ ...previous, [draftKey]: text }));
    setMobilePanel(null);
    navigate('home');
    requestAnimationFrame(() => composer.current?.focus());
  }

  async function create() {
    setCreating(true);
    workspace.setNotice('');
    try {
      await workspace.create();
      navigate('home');
      setMobilePanel(null);
      requestAnimationFrame(() => composer.current?.focus());
    } catch (error) {
      workspace.setNotice(errorText(error));
    } finally {
      setCreating(false);
    }
  }

  function choose(id: string) {
    followEnd.current = true;
    navigate('home');
    setMobilePanel(null);
    workspace.select(id);
  }

  function composerKey(event: ReactKeyboardEvent<HTMLTextAreaElement>) {
    if (event.key === 'Enter' && !event.shiftKey && !event.nativeEvent.isComposing) {
      event.preventDefault();
      form.current?.requestSubmit();
    }
  }

  async function submit(event: FormEvent) {
    event.preventDefault();
    const text = draft.trim();
    if (!text || posting || !config?.key_configured) return;
    setPosting(true);
    workspace.setNotice('');
    followEnd.current = true;
    try {
      await workspace.send(text);
      setDrafts(previous => previous[draftKey]?.trim() === text ? { ...previous, [draftKey]: '' } : previous);
    } catch (error) {
      workspace.setNotice(errorText(error));
    } finally {
      setPosting(false);
      composer.current?.focus();
    }
  }

  async function upload(event: ChangeEvent<HTMLInputElement>) {
    const file = event.target.files?.[0];
    event.target.value = '';
    if (!file) return;
    setUploading(true);
    workspace.setNotice('');
    try {
      if (config && file.size > config.upload_limit) throw new Error('文件超过 5 MiB，请使用较小的 FASTA。');
      const conversationId = activeId ?? await workspace.create();
      const saved = await workspace.upload(file, conversationId);
      setDrafts(previous => ({
        ...previous,
        [conversationId]: previous[conversationId] ||
          '帮我统计 ' + saved.display_name + ' 中每条序列的长度和 GC 含量。',
      }));
      composer.current?.focus();
    } catch (error) {
      workspace.setNotice(errorText(error));
    } finally {
      setUploading(false);
    }
  }

  function suggestFile(file: LocalFile) {
    fill('帮我统计 ' + file.display_name + ' 中每条序列的长度和 GC 含量。');
  }

  function retry(job: Job) {
    workspace.setNotice('');
    void workspace.retrySummary(job).catch(error => workspace.setNotice(errorText(error)));
  }

  return (
    <div className="workspace">
      <Navigation section={section} onLogout={() => void onLogout().catch(error => workspace.setNotice(errorText(error)))} />
      {mobilePanel && <div className="panel-overlay" aria-hidden="true" onClick={() => setMobilePanel(null)} />}
      <aside className={'sidebar ' + (mobilePanel === 'history' ? 'panel-visible' : '')} aria-label="历史会话">
        {mobilePanel === 'history' && <button type="button" className="mobile-drawer-close"
          aria-label="关闭会话侧栏" onClick={() => setMobilePanel(null)}><X size={18} aria-hidden="true" /></button>}
        <div className="brand">
          <div className="brand-mark" aria-hidden="true"><i /><i /><i /></div>
          <div>科研计算助手<span>本机工作区</span></div>
        </div>
        <button className="new-chat" disabled={creating} onClick={create}>
          <span aria-hidden="true">＋</span> 新建会话
        </button>
        <div className="sidebar-list">
          <h2>历史会话</h2>
          {workspace.conversations.length === 0 && <p className="empty-history">还没有会话</p>}
          {workspace.conversations.map(item => {
            const selected = activeId === item.id;
            const unread = !selected && !!item.last_finished_at && (workspace.seen[item.id] ?? '') < item.last_finished_at;
            return (
              <button key={item.id} className={'session ' + (selected ? 'selected' : '')} aria-current={selected || undefined}
                onClick={() => choose(item.id)} title={item.title}>
                <span className="session-name">{item.title}</span>
                {unread ? <span className="session-dot" title="有任务完成" aria-label="有任务完成" /> :
                  !!item.active_jobs && <span className="session-count" title="正在运行的任务">{item.active_jobs}</span>}
              </button>
            );
          })}
        </div>
        <div className="sidebar-footer" title={account.username}>
          <span className="account-initial" aria-hidden="true">{Array.from(account.username)[0].toUpperCase()}</span>
          <span className="current-account">{account.username}</span>
        </div>
      </aside>

      <div className="main-area">
        <header className="topbar">
          <div className="topbar-room">
            <button className="mobile-button" onClick={() => setMobilePanel('history')}>会话</button>
            {section === 'home' && <div className="room-title" title={current?.title}>{current?.title || '新会话'}</div>}
          </div>
          <div className="topbar-status">
            <span className={'connection ' + workspace.connection}><i />
              {workspace.connection === 'online' ? '本机已连接' : workspace.connection === 'retrying' ? '正在重连' : '连接中'}
            </span>
            <span className="model-label" title={config?.model}>
              {config?.model === 'deepseek-flash' || !config?.model ? 'DeepSeek Flash' : config.model}
            </span>
            {section === 'home' && <button className="mobile-button" onClick={() => setMobilePanel('jobs')}>任务</button>}
          </div>
        </header>

        {section === 'home' ? <div className="body-grid">
          <main className="chat" aria-label="聊天">
            {config && !config.key_configured && (
              <div className="config-banner">
                <strong>DeepSeek 尚未配置</strong>
                <span>在 <code>.env</code> 填入 <code>DEEPSEEK_API_KEY</code> 后重启后端，即可发送消息。</span>
              </div>
            )}
            <div className="chat-scroll" ref={scroll} onScroll={() => {
              const target = scroll.current;
              if (target) followEnd.current = target.scrollHeight - target.scrollTop - target.clientHeight < 96;
            }}>
              <div className="chat-column">
                {history.length === 0 ? (
                  <section className="welcome">
                    <h1>开始一次序列分析</h1>
                    <p>输入文件名和你想做的分析，或上传本机 FASTA 文件。</p>
                    <div className="examples">
                      {EXAMPLES.map(example => (
                        <button className="example" key={example.file} onClick={() => fill(example.prompt)}>
                          <strong>{example.title}</strong><code>{example.file}</code>
                        </button>
                      ))}
                    </div>
                  </section>
                ) : (
                  <div className="message-stack" aria-label="会话记录">
                    {history.map(message => (
                      <MessageView key={message.id} message={message} jobs={room.jobs} observations={observations} />
                    ))}
                    {pending > 0 && <div className="pending-line" role="status">
                      <span className="typing" aria-hidden="true"><i /><i /><i /></span>
                      {pending === 1 ? '助手正在处理消息' : pending + ' 条消息待处理'}
                    </div>}
                  </div>
                )}
              </div>
            </div>

            <div className="composer-area">
              <form className="composer" onSubmit={submit} ref={form}>
                <label className="sr-only" htmlFor="chat-input">消息</label>
                <textarea id="chat-input" ref={composer} rows={2} value={draft} maxLength={4000}
                  onChange={event => {
                    const text = event.target.value;
                    setDrafts(previous => ({ ...previous, [draftKey]: text }));
                  }} onKeyDown={composerKey}
                  placeholder="例如，统计 sample1 里序列的 GC 含量…"
                />
                <div className="composer-controls">
                  <input type="file" accept=".fasta,.fa,.fna" ref={fileInput} onChange={upload} hidden />
                  <button type="button" className="attach" disabled={uploading} onClick={() => fileInput.current?.click()}>
                    <span aria-hidden="true">＋</span> {uploading ? '正在上传' : '上传 FASTA'}
                  </button>
                  <button className="send" type="submit" disabled={posting || !draft.trim() || !config?.key_configured}>
                    {posting ? '正在发送' : '发送'}
                  </button>
                </div>
              </form>
              {workspace.notice ? <p className="composer-note tone-error" role="alert">{workspace.notice}</p> :
                <p className="composer-note">Enter 发送 <span>·</span> Shift + Enter 换行</p>}
            </div>
          </main>

          <aside className={'rail ' + (mobilePanel === 'jobs' ? 'panel-visible' : '')} aria-label="附件和任务">
            {mobilePanel === 'jobs' && <button type="button" className="mobile-drawer-close"
              aria-label="关闭任务侧栏" onClick={() => setMobilePanel(null)}><X size={18} aria-hidden="true" /></button>}
            <section className="rail-section">
              <h2>样例文件</h2>
              <div className="file-list">
                {EXAMPLES.map(example => (
                  <button className="file" key={example.file} onClick={() => fill(example.prompt)}>
                    <span className="file-mark" aria-hidden="true" />
                    <span><code>{example.file}</code><small>{example.title}</small></span>
                  </button>
                ))}
              </div>
            </section>
            {!!uploaded.length && <section className="rail-section uploads">
              <h2>我的文件</h2>
              <div className="file-list">
                {uploaded.map(file => (
                  <button className="file" key={file.id} onClick={() => suggestFile(file)}>
                    <span className="file-mark" aria-hidden="true" />
                    <span><code>{file.display_name}</code><small>{showSize(file.size_bytes)}</small></span>
                  </button>
                ))}
              </div>
            </section>}
            <section className="rail-section tasks" aria-label="任务记录">
              <div className="rail-heading"><h2>任务记录</h2>{!!room.jobs.length && <span>{room.jobs.length}</span>}</div>
              <JobCards jobs={room.jobs} onRetry={retry} />
            </section>
          </aside>
        </div> : <main className="management" aria-label={label}>
          {workspace.notice && <p className="management-notice tone-error" role="alert">{workspace.notice}</p>}
          {section === 'files' && <FileManager files={workspace.files} onUpload={workspace.upload} onAnalyze={suggestFile} />}
          {section === 'tasks' && <TaskManager jobs={workspace.jobs} onConversation={choose} />}
          {section === 'tools' && <ToolManager />}
        </main>}
      </div>
      {workspace.toast && <div className="toast" role="status">
        <span>{workspace.toast.text}</span>
        <button onClick={() => choose(workspace.toast!.conversationId)}>查看会话</button>
        <button className="toast-dismiss" aria-label="关闭提示" onClick={() => workspace.setToast(undefined)}>×</button>
      </div>}
    </div>
  );
}
