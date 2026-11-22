export type JobState = 'queued' | 'running' | 'succeeded' | 'failed';
export type Analysis = 'seq_stats' | 'find_orf';

export interface Conversation {
  id: string;
  title: string;
  created_at: string;
  updated_at: string;
  last_finished_at?: string | null;
  active_jobs?: number;
}

export interface Call {
  id: string;
  function: { name: string; arguments: string };
}

export interface Message {
  id: number;
  conversation_id: string;
  turn_id: number | null;
  kind: 'user' | 'assistant' | 'invocation' | 'tool' | 'summary';
  status: 'queued' | 'running' | 'done' | 'interrupted' | 'error';
  content: string;
  revision: number;
  tool_calls: Call[];
  tool_call_id: string | null;
  invocation_id: number | null;
  job_id: string | null;
  source: 'model' | 'system' | null;
  created_at: string;
}

export interface Job {
  id: string;
  conversation_id: string;
  filename: string;
  analysis_type: Analysis;
  state: JobState;
  summary_state: 'pending' | 'running' | 'done' | 'error';
  started_at: string | null;
  finished_at: string | null;
  exit_code: number | null;
  runner_error: string | null;
  created_at: string;
  revision: number;
}

export interface LocalFile {
  id: string;
  display_name: string;
  source: 'sample' | 'upload';
  size_bytes: number;
}

export interface Config {
  model: string;
  key_configured: boolean;
  samples: string[];
  upload_limit: number;
}

export interface Room {
  messages: Message[];
  jobs: Job[];
  files: LocalFile[];
}

export interface Snapshot extends Room {
  conversation: Conversation;
}

export function roomPath(id: string): string {
  return '/api/conversations/' + id;
}

export async function request<T>(url: string, init?: RequestInit): Promise<T> {
  const response = await fetch(url, { cache: 'no-store', ...init });
  const data = await response.json().catch(() => undefined);
  if (!response.ok) {
    throw new Error(typeof data?.detail === 'string' ? data.detail : '请求失败，请检查本机后端。');
  }
  return data as T;
}

export function sendText(id: string, text: string, clientId: string): Promise<Message> {
  return request(roomPath(id) + '/messages', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ text, client_message_id: clientId }),
  });
}

export function uploadFile(id: string, file: File): Promise<LocalFile> {
  return request(roomPath(id) + '/files?filename=' + encodeURIComponent(file.name), {
    method: 'POST',
    headers: { 'Content-Type': 'application/octet-stream' },
    body: file,
  });
}
