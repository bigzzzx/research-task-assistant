import { useState } from 'react';
import type { FormEvent } from 'react';
import { type Account, request } from './api';
import { errorText } from './useWorkspace';

export function AccountScreen({ onSignIn }: { onSignIn: (account: Account) => void }) {
  const [registering, setRegistering] = useState(false);
  const [username, setUsername] = useState('');
  const [password, setPassword] = useState('');
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState('');

  async function submit(event: FormEvent) {
    event.preventDefault();
    if (busy) return;
    setBusy(true);
    setError('');
    try {
      const account = await request<Account>('/api/auth/' + (registering ? 'register' : 'login'), {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ username, password }),
      });
      onSignIn(account);
    } catch (failure) {
      setError(errorText(failure));
    } finally {
      setBusy(false);
    }
  }

  function toggle() {
    setRegistering(previous => !previous);
    setError('');
    setPassword('');
  }

  return (
    <main className="account-page">
      <form className="account-card" onSubmit={submit}>
        <div className="account-brand">
          <div className="brand-mark" aria-hidden="true"><i /><i /><i /></div>
          <span>科研计算助手</span>
        </div>
        <h1>{registering ? '创建账号' : '登录工作区'}</h1>
        <p>登录后可访问自己的对话、FASTA 文件和任务。</p>
        <label htmlFor="account-name">用户名</label>
        <input id="account-name" autoComplete="username" value={username}
          onChange={event => setUsername(event.target.value)} required minLength={3} maxLength={32} />
        <label htmlFor="account-password">密码</label>
        <input id="account-password" type="password" value={password}
          autoComplete={registering ? 'new-password' : 'current-password'}
          aria-describedby={registering ? 'password-hint' : undefined}
          onChange={event => setPassword(event.target.value)} minLength={registering ? 15 : 1}
          maxLength={128} required />
        {registering && <p className="account-hint" id="password-hint">至少 15 个字符，可以使用一段容易记住的短语。</p>}
        {error && <p className="account-error" role="alert">{error}</p>}
        <button className="primary-button" type="submit" disabled={busy}>
          {busy ? '正在处理' : registering ? '注册并登录' : '登录'}
        </button>
        <button className="account-switch" type="button" disabled={busy} onClick={toggle}>
          {registering ? '已有账号，返回登录' : '创建账号'}
        </button>
      </form>
    </main>
  );
}
