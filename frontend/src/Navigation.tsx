import { useEffect, useState } from 'react';
import { ClipboardList, FolderOpen, House, LogOut, Wrench } from 'lucide-react';

const ITEMS = [
  { id: 'home', label: '首页', Icon: House },
  { id: 'files', label: '文件管理', Icon: FolderOpen },
  { id: 'tasks', label: '任务管理', Icon: ClipboardList },
  { id: 'tools', label: '扩展管理', Icon: Wrench },
] as const;

export type Section = (typeof ITEMS)[number]['id'];

function currentSection(): Section {
  return ITEMS.find(item => '#' + item.id === window.location.hash)?.id ?? 'home';
}

export function useSection() {
  const [section, setSection] = useState(currentSection);
  useEffect(() => {
    const onHash = () => setSection(currentSection());
    window.addEventListener('hashchange', onHash);
    return () => window.removeEventListener('hashchange', onHash);
  }, []);

  function navigate(target: Section) {
    window.location.hash = target;
    setSection(target);
  }

  return { section, navigate, label: ITEMS.find(item => item.id === section)!.label };
}

export function Navigation({ section, onLogout }: { section: Section; onLogout: () => void }) {
  return (
    <nav className="navigation" aria-label="工作区导航">
      <div className="navigation-pages">
        {ITEMS.map(({ id, label, Icon }) => (
          <a key={id} className="nav-icon" href={'#' + id} aria-label={label}
            aria-current={section === id ? 'page' : undefined}>
            <Icon size={21} strokeWidth={1.8} aria-hidden="true" />
            <span className="nav-tooltip" aria-hidden="true">{label}</span>
          </a>
        ))}
      </div>
      <button className="nav-icon nav-sign-out" type="button" aria-label="退出登录" onClick={onLogout}>
        <LogOut size={20} strokeWidth={1.8} aria-hidden="true" />
        <span className="nav-tooltip" aria-hidden="true">退出登录</span>
      </button>
    </nav>
  );
}
