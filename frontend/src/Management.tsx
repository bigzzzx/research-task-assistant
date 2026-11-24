import { useEffect, useRef, useState } from 'react';
import { ClipboardList, FileText, Plus, Terminal, X } from 'lucide-react';
import { accountUrl, type FailureCategory, type Job, type JobLog, type JobState, type LocalFile, request, type ToolDefinition } from './api';
import { ANALYSIS, STATES, Status, showSize } from './display';
import { errorText } from './useWorkspace';

const timestamp = new Intl.DateTimeFormat('zh-CN', {
  year: 'numeric', month: '2-digit', day: '2-digit',
  hour: '2-digit', minute: '2-digit', second: '2-digit', hour12: false,
});

function date(value: string | null) {
  return value ? <time dateTime={value}>{timestamp.format(new Date(value))}</time> : '—';
}

function duration(seconds: number | null) {
  if (seconds === null) return '—';
  if (seconds < 60) return seconds.toFixed(1) + ' 秒';
  return Math.floor(seconds / 60) + ' 分 ' + (Math.floor(seconds) % 60) + ' 秒';
}

function sinceStatus(job: Job, clock: number) {
  return Math.max(0, clock - Date.parse(job.status_at)) / 1000;
}

function runDuration(job: Job, clock: number) {
  if (job.duration_seconds === null) return '—';
  return duration(job.duration_seconds + (job.state === 'running' ? sinceStatus(job, clock) : 0));
}

function FileList({ files, onAnalyze }: { files: LocalFile[]; onAnalyze: (file: LocalFile) => void }) {
  return (
    <ul className="managed-files">
      {files.map(file => (
        <li className="managed-file" key={file.id}>
          <span className="file-mark" aria-hidden="true" />
          <div className="managed-file-label">
            <code title={file.display_name}>{file.display_name}</code>
            <span>{showSize(file.size_bytes)}{file.created_at && <> · {date(file.created_at)}</>}</span>
          </div>
          <div className="row-actions">
            <a className="text-button" href={accountUrl('/api/files/' + file.id + '/download')}>下载</a>
            <button className="secondary-button" type="button" onClick={() => onAnalyze(file)}>用于分析</button>
          </div>
        </li>
      ))}
    </ul>
  );
}

export function FileManager({ files, onUpload, onAnalyze }: {
  files: LocalFile[];
  onUpload: (file: File) => Promise<LocalFile>;
  onAnalyze: (file: LocalFile) => void;
}) {
  const picker = useRef<HTMLInputElement>(null);
  const [busy, setBusy] = useState(false);
  const [feedback, setFeedback] = useState('');
  const [error, setError] = useState(false);
  const uploads = files.filter(file => file.source === 'upload').reverse();
  const samples = files.filter(file => file.source === 'sample');

  async function add(file: File) {
    setBusy(true);
    setFeedback('');
    setError(false);
    try {
      const result = await onUpload(file);
      setFeedback('已上传 ' + result.display_name);
    } catch (failure) {
      setError(true);
      setFeedback(errorText(failure));
    } finally {
      setBusy(false);
    }
  }

  return (
    <div className="management-content">
      <header className="management-heading">
        <div><h1>文件管理</h1><p>上传的 FASTA 可在你自己的任一会话中使用。</p></div>
        <input aria-label="选择 FASTA 文件" type="file" accept=".fa,.fasta,.fna" ref={picker} hidden
          onChange={event => {
            const file = event.target.files?.[0];
            event.target.value = '';
            if (file) void add(file);
          }} />
        <button className="primary-button" type="button" disabled={busy} onClick={() => picker.current?.click()}>
          <Plus size={17} aria-hidden="true" /> {busy ? '正在上传' : '上传 FASTA'}
        </button>
      </header>
      {feedback && <p className={'management-feedback ' + (error ? 'tone-error' : '')} role={error ? 'alert' : 'status'}>
        {feedback}
      </p>}
      <section className="management-section">
        <h2>我的文件</h2>
        {uploads.length ? <FileList files={uploads} onAnalyze={onAnalyze} /> :
          <div className="management-empty"><FileText aria-hidden="true" /><p>还没有上传文件</p></div>}
      </section>
      <section className="management-section">
        <h2>内置样例</h2>
        <FileList files={samples} onAnalyze={onAnalyze} />
      </section>
    </div>
  );
}

type Stream = 'stdout' | 'stderr';

function LogPanel({ job, stream }: { job: Job; stream: Stream }) {
  const [positions, setPositions] = useState([0]);
  const [log, setLog] = useState<JobLog>();
  const [error, setError] = useState('');
  const [busy, setBusy] = useState(true);
  const [refresh, setRefresh] = useState(0);
  const offset = positions[positions.length - 1];
  const path = '/api/jobs/' + job.id + '/logs/' + stream;
  const terminal = job.state === 'succeeded' || job.state === 'failed';

  useEffect(() => {
    const controller = new AbortController();
    setBusy(true);
    setError('');
    void request<JobLog>(path + '/content?offset=' + offset, { signal: controller.signal }).then(result => {
      if (!controller.signal.aborted) setLog(result);
    }).catch(failure => {
      if (!controller.signal.aborted) setError(errorText(failure));
    }).finally(() => {
      if (!controller.signal.aborted) setBusy(false);
    });
    return () => controller.abort();
  }, [offset, path, refresh, job.state]);

  return (
    <section className="log-panel" aria-label={stream === 'stdout' ? '标准输出日志' : '错误日志详情'}>
      <div className="log-actions">
        <span>{positions.length > 1 || log?.has_more ? '第 ' + positions.length + ' 段' : '原始日志'}</span>
        <button className="secondary-button" onClick={() => setRefresh(value => value + 1)} disabled={busy}>重新读取</button>
        {terminal && log?.available &&
          <a className="secondary-button" href={accountUrl(path)}>下载完整日志</a>}
      </div>
      {error ? <p className="tone-error" role="alert">{error}</p> :
        <pre tabIndex={0}>{busy ? '正在读取日志…' : log?.text || (log?.available ? '此路日志为空' : '任务暂未生成此路日志')}</pre>}
      {(positions.length > 1 || log?.has_more) && <div className="log-paging">
        <button className="secondary-button" disabled={busy || positions.length < 2}
          onClick={() => setPositions(previous => previous.slice(0, -1))}>上一段</button>
        <button className="secondary-button" disabled={busy || !log?.has_more || log.next_offset <= offset}
          onClick={() => { if (log) setPositions(previous => [...previous, log.next_offset]); }}>下一段</button>
      </div>}
    </section>
  );
}

const FAILURE_LABELS: Record<FailureCategory, string> = {
  input: '输入文件问题',
  configuration: '运行配置问题',
  temporary_resource: '临时资源故障',
  resource_limit: '资源不足',
  timeout: '执行超时',
  interrupted: '执行中断',
  unknown: '原因待确认',
};
const FAILURE_STAGES = { input: '读取输入', preparation: '准备任务', launch: '启动进程', execution: '执行脚本' };

function TaskDetail({ job, clock, attempts, onSelect, onRetry, onClose, onConversation }: {
  job: Job; clock: number; onClose: () => void; onConversation: (id: string) => void;
  attempts: Job[]; onSelect: (id: string) => void; onRetry: (job: Job) => Promise<Job>;
}) {
  const dialog = useRef<HTMLDialogElement>(null);
  const [stream, setStream] = useState<Stream>(job.state === 'failed' ? 'stderr' : 'stdout');
  const [retrying, setRetrying] = useState(false);
  const [error, setError] = useState('');
  useEffect(() => {
    const element = dialog.current;
    element?.showModal();
    return () => element?.close();
  }, []);

  async function retry() {
    setRetrying(true);
    setError('');
    try {
      onSelect((await onRetry(job)).id);
    } catch (failure) {
      setError(errorText(failure));
    } finally {
      setRetrying(false);
    }
  }

  return (
    <dialog className="task-detail" aria-labelledby="task-detail-title" ref={dialog}
      onCancel={event => { event.preventDefault(); onClose(); }}>
      <div className="detail-content">
        <header className="detail-header">
          <div><h2 id="task-detail-title">任务详情</h2><Status state={job.state} /></div>
          <button className="detail-close" aria-label="关闭任务详情" type="button" onClick={onClose}>
            <X size={21} aria-hidden="true" />
          </button>
        </header>
        <dl className="task-data">
          <div className="task-id-detail"><dt>任务 ID</dt><dd><code>{job.id}</code></dd></div>
          <div><dt>任务类型</dt><dd>{ANALYSIS[job.analysis_type]}</dd></div>
          <div><dt>文件名称</dt><dd><code>{job.filename}</code></dd></div>
          <div><dt>提交时间</dt><dd>{date(job.created_at)}</dd></div>
          <div><dt>执行时长</dt><dd>{runDuration(job, clock)}</dd></div>
          <div><dt>排队等待</dt><dd>
            {duration(job.queue_wait_seconds + (job.state === 'queued' ? sinceStatus(job, clock) : 0))}
          </dd></div>
          <div><dt>开始时间</dt><dd>{date(job.started_at)}</dd></div>
          <div><dt>完成时间</dt><dd>{date(job.finished_at)}</dd></div>
          <div><dt>退出码</dt><dd>{job.exit_code ?? '—'}</dd></div>
          {job.state === 'queued' && job.available_at &&
            <div><dt>计划重试时间</dt><dd>{date(job.available_at)}</dd></div>}
          {job.state === 'failed' && <>
            <div><dt>失败原因</dt><dd>{FAILURE_LABELS[job.failure_category ?? 'unknown']}</dd></div>
            {job.failure_stage && <div><dt>失败阶段</dt><dd>{FAILURE_STAGES[job.failure_stage]}</dd></div>}
            {job.failure_code && <div><dt>错误码</dt><dd><code>{job.failure_code}</code></dd></div>}
          </>}
        </dl>
        {job.runner_error && <p className="job-reason">{job.runner_error}</p>}
        {attempts.length > 1 && <section className="task-attempts" aria-label="尝试记录">
          <h3>尝试记录</h3>
          {attempts.map(attempt => (
            <button type="button" className="secondary-button" key={attempt.id}
              aria-pressed={attempt.id === job.id} onClick={() => onSelect(attempt.id)}>
              第 {attempt.attempt_number} 次
              {attempt.retry_kind === 'automatic' && ' · 自动重试'}
              {attempt.retry_kind === 'manual' && ' · 手动重跑'}
              <code title={attempt.id}>{attempt.id.slice(0, 8)}</code>
              <Status state={attempt.state} />
            </button>
          ))}
        </section>}
        <div className="log-tabs" aria-label="日志类型">
          <button type="button" aria-pressed={stream === 'stdout'} onClick={() => setStream('stdout')}>标准输出</button>
          <button type="button" aria-pressed={stream === 'stderr'} onClick={() => setStream('stderr')}>错误日志</button>
        </div>
        <LogPanel key={stream} job={job} stream={stream} />
        {error && <p className="tone-error" role="alert">{error}</p>}
        <footer className="detail-footer">
          {job.state === 'failed' && !job.next_attempt_id && (job.input_request_id
            ? <button className="primary-button" onClick={() => onConversation(job.conversation_id)}>补充文件并继续</button>
            : <button className="primary-button" disabled={retrying} onClick={() => void retry()}>
              {retrying ? '正在提交' : '重新运行'}
            </button>)}
          <button className="secondary-button" onClick={() => onConversation(job.conversation_id)}>打开原会话</button>
        </footer>
      </div>
    </dialog>
  );
}

const FILTERS: Array<{ state: 'all' | JobState; label: string }> = [
  { state: 'all', label: '全部' },
  { state: 'queued', label: STATES.queued },
  { state: 'running', label: STATES.running },
  { state: 'succeeded', label: STATES.succeeded },
  { state: 'failed', label: STATES.failed },
];

export function TaskManager({ jobs, onConversation, onRetry }: {
  jobs: Job[]; onConversation: (id: string) => void; onRetry: (job: Job) => Promise<Job>;
}) {
  const [filter, setFilter] = useState<'all' | JobState>('all');
  const [search, setSearch] = useState('');
  const [selected, setSelected] = useState<string>();
  const [clock, setClock] = useState(Date.now);
  const active = jobs.some(job => job.state === 'running' || job.state === 'queued');
  useEffect(() => {
    if (!active) return;
    const tick = () => setClock(Date.now());
    tick();
    const timer = window.setInterval(tick, 1000);
    return () => window.clearInterval(timer);
  }, [active]);

  const query = search.trim().toLowerCase();
  const results = jobs.filter(job =>
    (filter === 'all' || job.state === filter) &&
    (job.id.includes(query) || job.filename.toLowerCase().includes(query)),
  ).sort((a, b) => b.created_at.localeCompare(a.created_at));
  const detail = jobs.find(job => job.id === selected);
  const attempts = detail ? jobs.filter(job =>
    (job.root_job_id ?? job.id) === (detail.root_job_id ?? detail.id)).sort((a, b) => a.attempt_number - b.attempt_number) : [];

  return (
    <div className="management-content">
      <header className="management-heading"><div>
        <h1>任务管理</h1><p>查看当前账号跨会话的任务、执行时间和原始日志。</p>
      </div></header>
      <div className="task-controls">
        <div className="filters" aria-label="按状态筛选">
          {FILTERS.map(item => (
            <button key={item.state} type="button" aria-pressed={item.state === filter}
              onClick={() => setFilter(item.state)}>{item.label}</button>
          ))}
        </div>
        <input aria-label="搜索任务" placeholder="搜索文件名或任务 ID" value={search}
          onChange={event => setSearch(event.target.value)} />
      </div>
      {results.length ? (
        <div className="task-table-scroll">
          <table className="task-table">
            <thead><tr>
              <th scope="col">任务 ID</th><th scope="col">任务类型</th><th scope="col">文件名称</th>
              <th scope="col">执行状态</th><th scope="col">提交时间</th><th scope="col">执行时长</th><th scope="col">操作</th>
            </tr></thead>
            <tbody>{results.map(job => (
              <tr key={job.id}>
                <td className="task-id-cell"><code>{job.id}</code></td>
                <td>{ANALYSIS[job.analysis_type]}</td>
                <td className="task-file-cell" title={job.filename}>{job.filename}</td>
                <td><Status state={job.state} /></td>
                <td className="task-time-cell">{date(job.created_at)}</td>
                <td>{runDuration(job, clock)}</td>
                <td><button className="text-button" onClick={() => setSelected(job.id)}>查看日志</button></td>
              </tr>
            ))}</tbody>
          </table>
        </div>
      ) : (
        <div className="management-empty">
          <ClipboardList aria-hidden="true" /><p>{jobs.length ? '没有符合条件的任务' : '还没有提交任务'}</p>
        </div>
      )}
      {detail && <TaskDetail key={detail.id} job={detail} clock={clock} onClose={() => setSelected(undefined)}
        attempts={attempts} onSelect={setSelected} onRetry={onRetry} onConversation={onConversation} />}
    </div>
  );
}

export function ToolManager() {
  const [tools, setTools] = useState<ToolDefinition[]>();
  const [error, setError] = useState('');
  useEffect(() => {
    const controller = new AbortController();
    void request<ToolDefinition[]>('/api/tools', { signal: controller.signal }).then(result => {
      if (!controller.signal.aborted) setTools(result);
    }).catch(failure => {
      if (!controller.signal.aborted) setError(errorText(failure));
    });
    return () => controller.abort();
  }, []);

  return (
    <div className="management-content">
      <header className="management-heading">
        <div><h1>工具管理</h1><p>查看助手可调用的内置工具、参数和用途。</p></div>
      </header>
      {error && <p className="tone-error" role="alert">{error}</p>}
      {!error && !tools && <p className="muted" role="status">正在读取工具目录…</p>}
      <div className="tool-catalog">
        {tools?.map(tool => (
          <article className="catalog-card" key={tool.name}>
            <div className="catalog-title"><Terminal size={18} aria-hidden="true" /><h2><code>{tool.name}</code></h2></div>
            <p>{tool.description}</p>
            <dl>{Object.entries(tool.parameters.properties).map(([name, parameter]) => (
              <div key={name}>
                <dt><code>{name}</code> <small>{tool.parameters.required.includes(name) ? '必填' : '可选'}</small></dt>
                <dd>{parameter.description}
                  {parameter.enum && <div className="catalog-options">{parameter.enum.map(option => (
                    <code key={option}>{option}</code>
                  ))}</div>}
                </dd>
              </div>
            ))}</dl>
          </article>
        ))}
      </div>
    </div>
  );
}
