import { useCallback, useEffect, useRef, useState } from 'react';
import { type Config, type Conversation, type InputRequest, type Job, type LocalFile, type Message,
  type Room, type Snapshot, accountUrl, request, roomPath, sendText, uploadFile } from './api';

const EMPTY_ROOM: Room = { messages: [], jobs: [], input_requests: [] };

interface MessageDelta {
  conversation_id: string;
  message_id: number;
  revision: number;
  delta: string;
}

function mergeRows<T extends { id: string | number; revision?: number }>(rows: T[], updates: T[]): T[] {
  const merged = new Map(rows.map(row => [row.id, row]));
  for (const row of updates) {
    const previous = merged.get(row.id);
    if (!previous || (previous.revision ?? 0) <= (row.revision ?? 0)) {
      merged.set(row.id, row);
    }
  }
  return [...merged.values()];
}

function readSeen(key: string): Record<string, string> {
  try {
    return JSON.parse(localStorage.getItem(key) || '{}');
  } catch {
    return {};
  }
}

export function errorText(error: unknown): string {
  return error instanceof Error ? error.message : '请求失败，请稍后重试。';
}

export function useWorkspace(accountId: string) {
  const seenKey = 'research-seen-jobs:' + accountId;
  const conversationKey = 'research-active-conversation:' + accountId;
  const [config, setConfig] = useState<Config>();
  const [conversations, setConversations] = useState<Conversation[]>([]);
  const [files, setFiles] = useState<LocalFile[]>([]);
  const [jobs, setJobs] = useState<Job[]>([]);
  const [activeId, setActiveId] = useState<string | null>(null);
  const activeRef = useRef<string | null>(null);
  const [rooms, setRooms] = useState<Record<string, Room>>({});
  const roomsRef = useRef<Record<string, Room>>({});
  const [seen, setSeen] = useState(() => readSeen(seenKey));
  const [notice, setNotice] = useState('');
  const [toast, setToast] = useState<{ conversationId: string; text: string }>();
  const listRequest = useRef(0);
  const initialized = useRef(false);
  const creating = useRef<Promise<string> | null>(null);
  const sendAttempts = useRef(new Map<string, { text: string; id: string }>());

  const mutateRoom = useCallback((id: string, update: (room: Room) => Room) => {
    roomsRef.current = { ...roomsRef.current, [id]: update(roomsRef.current[id] ?? EMPTY_ROOM) };
    setRooms(roomsRef.current);
  }, []);

  const markSeen = useCallback((id: string, timestamp?: string | null) => {
    if (!timestamp) return;
    setSeen(previous => {
      if ((previous[id] ?? '') >= timestamp) return previous;
      const next = { ...previous, [id]: timestamp };
      localStorage.setItem(seenKey, JSON.stringify(next));
      return next;
    });
  }, [seenKey]);

  const loadRoom = useCallback(async (id: string) => {
    const snapshot = await request<Snapshot>(roomPath(id));
    mutateRoom(id, room => ({
      messages: mergeRows(room.messages, snapshot.messages).sort((a, b) => a.id - b.id),
      jobs: mergeRows(room.jobs, snapshot.jobs).sort((a, b) => b.created_at.localeCompare(a.created_at)),
      input_requests: mergeRows(room.input_requests, snapshot.input_requests ?? []),
    }));
    setJobs(previous => mergeRows(previous, snapshot.jobs));
    setFiles(previous => mergeRows(previous, snapshot.files));
    if (activeRef.current === id) {
      const finished = snapshot.jobs.reduce<string | null>(
        (latest, job) => job.finished_at && (!latest || job.finished_at > latest) ? job.finished_at : latest,
        null,
      );
      markSeen(id, finished);
    }
  }, [markSeen, mutateRoom]);

  const select = useCallback((id: string) => {
    activeRef.current = id;
    localStorage.setItem(conversationKey, id);
    setActiveId(id);
    setNotice('');
    setToast(previous => previous?.conversationId === id ? undefined : previous);
    void loadRoom(id).catch(error => {
      if (activeRef.current === id) setNotice(errorText(error));
    });
  }, [conversationKey, loadRoom]);

  const refreshList = useCallback(async () => {
    const token = ++listRequest.current;
    const result = await request<Conversation[]>('/api/conversations');
    if (token !== listRequest.current) return;
    setConversations(result);
    if (!initialized.current) {
      initialized.current = true;
      const remembered = localStorage.getItem(conversationKey);
      const target = result.find(item => item.id === remembered) ?? result[0];
      if (target && !activeRef.current) select(target.id);
    }
    const current = result.find(item => item.id === activeRef.current);
    if (current) markSeen(current.id, current.last_finished_at);
  }, [conversationKey, markSeen, select]);

  const recordMessage = useCallback((message: Message) => {
    mutateRoom(message.conversation_id, room => ({
      ...room,
      messages: mergeRows(room.messages, [message]).sort((a, b) => a.id - b.id),
    }));
  }, [mutateRoom]);

  const recordJob = useCallback((job: Job) => {
    setJobs(previous => mergeRows(previous, [job]));
    mutateRoom(job.conversation_id, room => ({
      ...room,
      jobs: mergeRows(room.jobs, [job]).sort((a, b) => b.created_at.localeCompare(a.created_at)),
    }));
  }, [mutateRoom]);

  const recordInputRequest = useCallback((input: InputRequest) => {
    mutateRoom(input.conversation_id, room => ({
      ...room, input_requests: mergeRows(room.input_requests, [input]),
    }));
  }, [mutateRoom]);

  useEffect(() => {
    let alive = true;
    const source = new EventSource(accountUrl('/api/events'));
    const recovering = new Set<string>();

    const report = (error: unknown) => { if (alive) setNotice(errorText(error)); };
    const getConfig = () => request<Config>('/api/config').then(data => { if (alive) setConfig(data); });
    const refresh = () => { void refreshList().catch(report); };
    const refreshResources = () => {
      void request<LocalFile[]>('/api/files').then(rows => {
        if (alive) setFiles(previous => mergeRows(previous, rows));
      }).catch(report);
      void request<Job[]>('/api/jobs').then(rows => {
        if (alive) setJobs(previous => mergeRows(previous, rows));
      }).catch(report);
    };
    const listen = <T,>(name: string, callback: (value: T) => void) => {
      source.addEventListener(name, (event: MessageEvent<string>) => {
        if (!alive) return;
        try {
          callback(JSON.parse(event.data) as T);
        } catch {
          report(new Error('收到无法解析的本机事件，刷新后可恢复会话。'));
        }
      });
    };

    listen('ready', () => {
      setNotice('');
      void getConfig().catch(report);
      refresh();
      refreshResources();
      if (activeRef.current) void loadRoom(activeRef.current).catch(report);
    });
    source.onerror = () => {
      if (!alive) return;
      void getConfig().catch(report);
    };
    for (const name of ['message.started', 'message.updated', 'message.done', 'tool.started', 'tool.finished']) {
      listen<Message>(name, message => {
        recordMessage(message);
        if (message.kind === 'user' && message.revision === 0) refresh();
      });
    }

    listen<MessageDelta>('message.delta', event => {
      const id = event.conversation_id;
      const row = roomsRef.current[id]?.messages.find(message => message.id === event.message_id);
      if (row && row.revision >= event.revision) return;
      if (row && row.revision + 1 === event.revision) {
        recordMessage({ ...row, content: row.content + event.delta, revision: event.revision });
      } else if (!recovering.has(id)) {
        recovering.add(id);
        void loadRoom(id).catch(report).finally(() => recovering.delete(id));
      }
    });

    listen<Job>('job.updated', job => {
      const previous = roomsRef.current[job.conversation_id]?.jobs.find(row => row.id === job.id);
      recordJob(job);
      if (previous?.state === job.state) return;
      refresh();
      if (!job.finished_at) return;
      if (activeRef.current === job.conversation_id) {
        markSeen(job.conversation_id, job.finished_at);
      } else {
        const result = job.state === 'succeeded' ? '分析已完成' : '分析失败';
        setToast({ conversationId: job.conversation_id, text: job.filename + ' ' + result });
      }
    });

    listen<{ file: LocalFile }>('file.added', event => {
      setFiles(previous => mergeRows(previous, [event.file]));
    });
    listen<InputRequest>('input.updated', recordInputRequest);

    void getConfig().catch(report);
    refresh();
    refreshResources();
    return () => {
      alive = false;
      source.close();
    };
  }, [loadRoom, markSeen, recordInputRequest, recordJob, recordMessage, refreshList]);

  async function create(): Promise<string> {
    if (creating.current) return creating.current;
    creating.current = request<Conversation>('/api/conversations', { method: 'POST' }).then(row => {
      setConversations(previous => [row, ...previous.filter(item => item.id !== row.id)]);
      select(row.id);
      void refreshList().catch(error => setNotice(errorText(error)));
      return row.id;
    });
    try {
      return await creating.current;
    } finally {
      creating.current = null;
    }
  }

  async function send(text: string): Promise<void> {
    const id = activeRef.current ?? await create();
    const prior = sendAttempts.current.get(id);
    const attempt = prior?.text === text ? prior : { id: crypto.randomUUID(), text };
    sendAttempts.current.set(id, attempt);
    const message = await sendText(id, text, attempt.id);
    sendAttempts.current.delete(id);
    recordMessage(message);
    void refreshList().catch(error => setNotice(errorText(error)));
  }

  async function upload(file: File, conversationId?: string): Promise<LocalFile> {
    const result = await uploadFile(file, conversationId);
    setFiles(previous => mergeRows(previous, [result]));
    return result;
  }

  async function retrySummary(job: Job): Promise<void> {
    const updated = await request<Job>(roomPath(job.conversation_id) + '/jobs/' + job.id + '/retry-summary', {
      method: 'POST',
    });
    recordJob(updated);
  }

  async function retryJob(job: Job): Promise<Job> {
    const updated = await request<Job>('/api/jobs/' + job.id + '/retry', { method: 'POST' });
    recordJob(updated);
    return updated;
  }

  async function resolveInput(id: string, choice: { file_id: string } | { repair_token: string }): Promise<void> {
    const result = await request<{ input_request: InputRequest; job: Job; file: LocalFile | null }>(
      '/api/input-requests/' + id + '/resolve', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify(choice),
      },
    );
    recordInputRequest(result.input_request);
    recordJob(result.job);
    const file = result.file;
    if (file) setFiles(previous => mergeRows(previous, [file]));
  }

  return {
    config, conversations, files, jobs, activeId, room: activeId ? rooms[activeId] ?? EMPTY_ROOM : EMPTY_ROOM,
    seen, notice, setNotice, toast, setToast, select, create, send, upload, retrySummary, retryJob, resolveInput,
  };
}
