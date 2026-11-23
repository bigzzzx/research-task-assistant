export type JobState = 'queued' | 'running' | 'succeeded' | 'failed';
export type Analysis = 'seq_stats' | 'find_orf';

export interface Account {
  id: string;
  username: string;
}

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
  created_at: string | null;
}

export interface ToolDefinition {
  name: string;
  description: string;
  parameters: { properties: Record<string, { type: string; description: string; enum?: string[] }>; required: string[] };
}

export interface JobLog {
  text: string;
  next_offset: number;
  has_more: boolean;
  available: boolean;
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
}

export interface Snapshot extends Room {
  conversation: Conversation;
  files: LocalFile[];
}

export function roomPath(id: string): string {
  return '/api/conversations/' + id;
}

export class RequestError extends Error {
  constructor(readonly status: number, message: string) {
    super(message);
  }
}

export const SESSION_CHANGED = 'research-session-changed';
let expectedAccount: string | undefined;

export function bindAccount(account?: Account) {
  expectedAccount = account?.id;
}

export function accountUrl(path: string): string {
  if (!expectedAccount) return path;
  return path + (path.includes('?') ? '&' : '?') + 'account=' + encodeURIComponent(expectedAccount);
}

export async function request<T>(url: string, init?: RequestInit): Promise<T> {
  const headers = new Headers(init?.headers);
  const account = url.startsWith('/api/auth/') ? undefined : expectedAccount;
  if (account) headers.set('X-Workspace-Account', account);
  const response = await fetch(url, { cache: 'no-store', ...init, headers });
  const data = await response.json().catch(() => undefined);
  if (!response.ok) {
    if (response.status === 401 && account && account === expectedAccount) {
      window.dispatchEvent(new Event(SESSION_CHANGED));
    }
    throw new RequestError(response.status, typeof data?.detail === 'string' ? data.detail : '请求失败，请检查本机后端。');
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

export function uploadFile(file: File, conversationId?: string): Promise<LocalFile> {
  const folder = conversationId ? roomPath(conversationId) + '/files' : '/api/files';
  return request(folder + '?filename=' + encodeURIComponent(file.name), {
    method: 'POST',
    headers: { 'Content-Type': 'application/octet-stream' },
    body: file,
  });
}
