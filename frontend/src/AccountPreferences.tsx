import { useEffect, useRef, useState } from 'react';
import type { FormEvent } from 'react';
import { X } from 'lucide-react';
import { request } from './api';
import { errorText } from './useWorkspace';

interface Preferences {
  result_order: 'file' | 'sequence_id' | null;
  gc_decimal_places: number | null;
}

export function AccountPreferences({ onClose }: { onClose: () => void }) {
  const dialog = useRef<HTMLDialogElement>(null);
  const [draft, setDraft] = useState<Preferences>();
  const [error, setError] = useState('');
  const [saving, setSaving] = useState(false);

  useEffect(() => {
    const element = dialog.current;
    const controller = new AbortController();
    element?.showModal();
    void request<Preferences>('/api/preferences', { signal: controller.signal }).then(preferences => {
      if (!controller.signal.aborted) setDraft(preferences);
    }).catch(failure => {
      if (!controller.signal.aborted) setError(errorText(failure));
    });
    return () => {
      controller.abort();
      element?.close();
    };
  }, []);

  async function save(event: FormEvent) {
    event.preventDefault();
    if (!draft || saving) return;
    setSaving(true);
    setError('');
    try {
      await request<Preferences>('/api/preferences', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify(draft),
      });
      onClose();
    } catch (failure) {
      setError(errorText(failure));
      setSaving(false);
    }
  }

  return (
    <dialog className="task-detail preferences-dialog" aria-labelledby="preferences-title" ref={dialog}
      onCancel={event => { event.preventDefault(); if (!saving) onClose(); }}>
      <div className="detail-content">
        <header className="detail-header">
          <h2 id="preferences-title">分析偏好</h2>
          <button className="detail-close" type="button" aria-label="关闭分析偏好" disabled={saving} onClick={onClose}>
            <X size={20} aria-hidden="true" />
          </button>
        </header>
        <p className="preferences-hint">保存在当前账号，适用于各会话。你在对话中明确指定的格式优先。</p>
        {error && <p className="tone-error" role="alert">{error}</p>}
        {!draft && !error && <p role="status">正在读取偏好…</p>}
        {draft && <form className="preferences-form" onSubmit={save}>
          <label htmlFor="preferred-order">结果展示顺序</label>
          <select id="preferred-order" value={draft.result_order ?? ''} disabled={saving}
            onChange={event => setDraft({ ...draft, result_order: (event.target.value || null) as Preferences['result_order'] })}>
            <option value="">使用默认规则</option>
            <option value="file">按 FASTA 原始顺序</option>
            <option value="sequence_id">按序列 ID 升序</option>
          </select>
          <label htmlFor="preferred-gc-decimals">GC 百分比小数位数</label>
          <select id="preferred-gc-decimals" value={draft.gc_decimal_places ?? ''} disabled={saving}
            onChange={event => setDraft({ ...draft, gc_decimal_places: event.target.value === '' ? null : Number(event.target.value) })}>
            <option value="">使用默认规则</option>
            <option value="0">0 位</option>
            <option value="1">1 位</option>
            <option value="2">2 位</option>
          </select>
          <button className="primary-button" type="submit" disabled={saving}>{saving ? '正在保存' : '保存偏好'}</button>
        </form>}
      </div>
    </dialog>
  );
}
