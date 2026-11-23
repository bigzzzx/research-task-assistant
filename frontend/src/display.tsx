import type { Analysis, JobState } from './api';

export const ANALYSIS: Record<Analysis, string> = { seq_stats: '序列统计', find_orf: '最长 ORF' };
export const STATES: Record<JobState, string> = { queued: '排队中', running: '运行中', succeeded: '成功', failed: '失败' };

export function Status({ state }: { state: JobState }) {
  return <span className={'state-pill ' + state}><i />{STATES[state]}</span>;
}

export function showSize(bytes: number): string {
  if (bytes < 1024) return bytes + ' B';
  if (bytes < 1024 ** 2) return (bytes / 1024).toFixed(1) + ' KiB';
  return (bytes / 1024 ** 2).toFixed(2) + ' MiB';
}
