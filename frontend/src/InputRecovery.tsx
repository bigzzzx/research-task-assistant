import { Fragment, useEffect, useId, useRef, useState } from 'react';
import { type InputDiagnostics, type InputIssue, type InputRequest, type LocalFile, type SourceExcerpt, request } from './api';
import { ANALYSIS } from './display';
import { errorText } from './useWorkspace';

function Excerpt({ issue, excerpt }: { issue: InputIssue; excerpt: SourceExcerpt }) {
  const characters = Array.from(excerpt.text);
  const start = excerpt.start_column;
  const end = start + characters.length - 1;
  const ranges = issue.code === 'INVALID_BASE' ? (issue.columns ?? [])
    .filter(([first, last]) => first <= end && last >= start)
    .map(([first, last]) => [Math.max(first, start), Math.min(last, end)]) : [];
  const text = (first: number, last: number) => characters.slice(first - start, last - start + 1).join('');

  return (
    <pre><code>
      {start > 1 && '…'}
      {issue.code === 'MISSING_HEADER' && <mark className="missing-title-marker">{'缺少 >'}</mark>}
      {ranges.map(([first, last], index) => (
        <Fragment key={first}>
          {text((ranges[index - 1]?.[1] ?? start - 1) + 1, first - 1)}
          <mark>{text(first, last)}</mark>
        </Fragment>
      ))}
      {text((ranges.at(-1)?.[1] ?? start - 1) + 1, end)}
      {excerpt.has_more && '…'}
    </code></pre>
  );
}

function Issues({ diagnostics }: { diagnostics: InputDiagnostics }) {
  return (
    <>
      <ul className="input-issues" aria-label="原文件诊断">
        {diagnostics.issues.map((issue, index) => {
          const columns = issue.columns?.map(([first, last]) => first === last ? String(first) : first + '–' + last);
          return (
            <li key={index}>
              {issue.line && <strong>
                {'第 ' + issue.line + ' 行'}
                {!!columns?.length && ' · 第 ' + columns.join('、') + ' 列'}
              </strong>}
              <span>{issue.message}</span>
              {issue.excerpts.map(excerpt =>
                <Excerpt key={excerpt.start_column} issue={issue} excerpt={excerpt} />)}
              {issue.columns_truncated && <small>此行展示前 {columns?.length} 处非法字符区间。</small>}
            </li>
          );
        })}
      </ul>
      {diagnostics.truncated && <p>其余诊断可在错误日志中查看。</p>}
    </>
  );
}

export function InputRecovery({ input, files, onUpload, onResolve }: {
  input: InputRequest;
  files: LocalFile[];
  onUpload: (file: File, conversationId: string) => Promise<LocalFile>;
  onResolve: (id: string, choice: { file_id: string } | { repair_token: string }) => Promise<void>;
}) {
  const selectId = useId();
  const filePicker = useRef<HTMLInputElement>(null);
  const [fileId, setFileId] = useState('');
  const [diagnostics, setDiagnostics] = useState<InputDiagnostics>();
  const [diagnosticsError, setDiagnosticsError] = useState('');
  const [showPreview, setShowPreview] = useState(false);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState('');
  const choices = files.filter(file => file.id !== input.original_file_id);
  const preview = diagnostics?.repair;
  const hasIssues = Boolean(diagnostics?.issues.length);

  useEffect(() => {
    if (!input.failed_job_id) return;
    const controller = new AbortController();
    void request<InputDiagnostics>('/api/input-requests/' + input.id + '/diagnostics', { signal: controller.signal })
      .then(setDiagnostics)
      .catch(failure => {
        if (!controller.signal.aborted) setDiagnosticsError(errorText(failure));
      });
    return () => controller.abort();
  }, [input.id, input.failed_job_id]);

  async function run(action: () => Promise<void>) {
    setBusy(true);
    setError('');
    try {
      await action();
    } catch (failure) {
      setError(errorText(failure));
    } finally {
      setBusy(false);
    }
  }

  if (input.resolved_job_id) return (
    <div className="input-resolved">
      <p>已补充 <strong>{input.filename}</strong>，继续分析任务
        <code title={input.resolved_job_id}>{input.resolved_job_id.slice(0, 8)}</code>
      </p>
      {hasIssues && diagnostics && <details>
        <summary>查看原文件诊断</summary>
        <Issues diagnostics={diagnostics} />
      </details>}
    </div>
  );

  return (
    <section className="input-recovery" aria-label={'补充文件 ' + input.filename}>
      <div className="input-recovery-heading">
        <strong>待补充文件</strong><code>{input.filename}</code>
      </div>
      {hasIssues && diagnostics && <Issues diagnostics={diagnostics} />}
      {diagnosticsError && <p className="tone-error" role="alert">{diagnosticsError}</p>}
      <p>{input.failed_job_id ? '上传或选择修正版后继续' : '选择或上传文件后继续'}{ANALYSIS[input.analysis_type]}。</p>
      <div className="input-recovery-actions">
        <label htmlFor={selectId}>选择文件</label>
        <select id={selectId} value={fileId} disabled={busy} onChange={event => setFileId(event.target.value)}>
          <option value="">请选择 FASTA</option>
          {choices.map(file => <option key={file.id} value={file.id}>{file.display_name}</option>)}
        </select>
        <input type="file" accept=".fasta,.fa,.fna" ref={filePicker} hidden onChange={event => {
          const file = event.target.files?.[0];
          event.target.value = '';
          if (file) void run(async () => setFileId((await onUpload(file, input.conversation_id)).id));
        }} />
        <button type="button" className="secondary-button" disabled={busy}
          onClick={() => filePicker.current?.click()}>{input.failed_job_id ? '上传修正版' : '上传文件'}</button>
        <button type="button" className="primary-button" disabled={busy || !fileId}
          onClick={() => void run(() => onResolve(input.id, { file_id: fileId }))}>确认继续</button>
      </div>
      {preview && !showPreview &&
        <button type="button" className="secondary-button" disabled={busy}
          onClick={() => setShowPreview(true)}>预览格式修复</button>}
      {preview && showPreview && <div className="input-repair-preview">
        <p>第 {preview.line} 行的标题改动。确认后保存为新文件。</p>
        <div><span>原标题</span><code>{preview.before}</code></div>
        <div><span>修复后</span><code>{preview.after}</code></div>
        <button type="button" className="primary-button" disabled={busy}
          onClick={() => void run(() => onResolve(input.id, { repair_token: preview.token }))}>
          确认生成副本并继续
        </button>
      </div>}
      {error && <p className="tone-error" role="alert">{error}</p>}
    </section>
  );
}
