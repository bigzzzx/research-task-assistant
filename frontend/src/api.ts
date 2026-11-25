export type JobState = 'queued' | 'running' | 'succeeded' | 'failed';
export type BuiltInAnalysis = 'seq_stats' | 'find_orf';
export type Analysis = BuiltInAnalysis | 'custom_python';
export type FailureCategory = 'input' | 'configuration' | 'temporary_resource' | 'resource_limit'
  | 'timeout' | 'interrupted' | 'unknown';

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
  kind: 'user' | 'assistant' | 'invocation' | 'tool' | 'summary' | 'input_reminder';
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
  task_type: string | null;
  script_id: string | null;
  script_name: string | null;
  script_sha256: string | null;
  state: JobState;
  summary_state: 'pending' | 'running' | 'done' | 'error';
  started_at: string | null;
  finished_at: string | null;
  exit_code: number | null;
  runner_error: string | null;
  created_at: string;
  status_at: string;
  duration_seconds: number | null;
  queue_wait_seconds: number;
  failure_category: FailureCategory | null;
  failure_code: string | null;
  failure_stage: 'input' | 'preparation' | 'launch' | 'execution' | null;
  input_request_id: string | null;
  retry_of_job_id: string | null;
  root_job_id: string | null;
  next_attempt_id: string | null;
  attempt_number: number;
  retry_kind: 'manual' | 'automatic' | null;
  available_at: string | null;
  revision: number;
}

export interface AnalysisScript {
  id: string;
  display_name: string;
  task_type: string;
  sha256: string;
  size_bytes: number;
  created_at: string;
}

export interface InputRequest {
  id: string;
  conversation_id: string;
  origin_message_id: number;
  filename: string;
  analysis_type: Analysis;
  task_type: string | null;
  failed_job_id: string | null;
  original_file_id: string | null;
  failure_code: string | null;
  resolved_job_id: string | null;
  reminder_message_id: number | null;
  created_at: string;
  revision: number;
}

export interface RepairPreview {
  token: string;
  line: number;
  before: string;
  after: string;
}

export interface SourceExcerpt {
  start_column: number;
  text: string;
  has_more: boolean;
}

export interface InputIssue {
  code: string;
  message: string;
  line?: number;
  columns?: [number, number][];
  columns_truncated?: boolean;
  excerpts: SourceExcerpt[];
}

export interface InputDiagnostics {
  issues: InputIssue[];
  truncated: boolean;
  repair: RepairPreview | null;
}

export interface LocalFile {
  id: string;
  display_name: string;
  source: 'sample' | 'upload';
  size_bytes: number;
  created_at: string | null;
}

export type McpTransport = 'streamable-http' | 'sse' | 'stdio';

export interface ToolDefinition {
  name: string;
  display_name: string;
  description: string;
  parameters: Record<string, unknown>;
  server_id: string | null;
  server_name: string;
  transport: McpTransport;
  enabled: boolean;
  available: boolean;
  server_enabled: boolean;
}

export interface McpServer {
  id: string;
  name: string;
  endpoint: string;
  transport: McpTransport;
  tool_count: number;
  enabled: boolean;
  available: boolean;
  synced_at: string;
}

export interface McpSettings {
  research: { name: string; tool_count: number };
  stdio_servers: string[];
  stdio_config_error: string | null;
  trusted_endpoints: string[];
  servers: McpServer[];
}

export interface Skill {
  id: string;
  name: string;
  description: string;
  enabled: boolean;
}

export interface SkillDetail extends Skill {
  document: string;
  files: string[];
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
  input_requests: InputRequest[];
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

export function sendText(id: string, text: string, clientId: string): Promise<Message & { input_reminder?: Message }> {
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
