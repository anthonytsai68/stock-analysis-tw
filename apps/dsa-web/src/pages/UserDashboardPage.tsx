import React, { useEffect, useState } from 'react';
import { useNavigate } from 'react-router-dom';
import { BarChart3, Crown, Zap, Star, LogOut, AlertCircle } from 'lucide-react';
import { userApi, type UserProfile } from '../api/user';
import { getParsedApiError } from '../api/error';

const PLAN_NAMES: Record<string, string> = {
  free: 'Free',
  pro: 'Pro',
  business: 'Business',
};

const PLAN_ICONS: Record<string, React.ComponentType<{ className?: string }>> = {
  free: Star,
  pro: Zap,
  business: Crown,
};

const PLAN_COLORS: Record<string, string> = {
  free: 'text-slate-500',
  pro: 'text-blue-500',
  business: 'text-amber-500',
};

export const UserDashboardPage: React.FC = () => {
  const navigate = useNavigate();
  const [profile, setProfile] = useState<UserProfile | null>(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState('');

  useEffect(() => {
    userApi.getProfile()
      .then(setProfile)
      .catch((err) => {
        const parsed = getParsedApiError(err);
        if (parsed.status === 401) {
          navigate('/user/login', { replace: true });
        } else {
          setError(parsed.message || '載入失敗');
        }
      })
      .finally(() => setLoading(false));
  }, [navigate]);

  const handleLogout = async () => {
    try {
      await userApi.logout();
      navigate('/user/login', { replace: true });
    } catch { /* ignore */ }
  };

  if (loading) {
    return (
      <div className="flex min-h-screen items-center justify-center bg-base">
        <div className="h-8 w-8 animate-spin rounded-full border-4 border-primary border-t-transparent" />
      </div>
    );
  }

  if (error || !profile) {
    return (
      <div className="flex min-h-screen items-center justify-center bg-base px-4">
        <div className="text-center">
          <AlertCircle className="mx-auto mb-3 h-10 w-10 text-red-500" />
          <p className="text-secondary-text">{error || '無法載入'}</p>
          <button className="btn-primary mt-4" onClick={() => navigate('/user/login')}>
            重新登入
          </button>
        </div>
      </div>
    );
  }

  const PlanIcon = PLAN_ICONS[profile.plan] || Star;

  return (
    <div className="flex min-h-screen flex-col bg-base">
      {/* Header */}
      <header className="flex items-center justify-between border-b border-border px-6 py-3">
        <div className="flex items-center gap-2">
          <BarChart3 className="h-5 w-5 text-primary" />
          <span className="font-semibold text-foreground">StockGPT</span>
        </div>
        <button
          onClick={() => void handleLogout()}
          className="flex items-center gap-1 rounded-lg px-3 py-1.5 text-sm text-secondary-text hover:bg-muted hover:text-foreground"
        >
          <LogOut className="h-4 w-4" />
          登出
        </button>
      </header>

      {/* Main */}
      <main className="mx-auto w-full max-w-lg flex-1 px-4 py-12">
        <div className="mb-8 text-center">
          <h1 className="text-2xl font-bold text-foreground">{profile.email}</h1>
          <div className="mt-3 inline-flex items-center gap-2 rounded-full bg-muted px-4 py-1.5">
            <PlanIcon className={`h-5 w-5 ${PLAN_COLORS[profile.plan] || ''}`} />
            <span className="font-semibold">{PLAN_NAMES[profile.plan] || profile.plan}</span>
          </div>
        </div>

        {/* Plan card */}
        <div className="rounded-xl border border-border bg-card p-6">
          <h2 className="mb-4 text-lg font-semibold text-foreground">方案詳情</h2>
          <div className="space-y-3 text-sm">
            <div className="flex justify-between">
              <span className="text-secondary-text">自選上限</span>
              <span className="font-medium text-foreground">{profile.stocksLimit === 0 ? '∞ 無限' : `${profile.stocksLimit} 檔`}</span>
            </div>
            <div className="flex justify-between">
              <span className="text-secondary-text">可用市場</span>
              <span className="font-medium text-foreground">
                {profile.markets.split(',').map((m: string) => {
                  const names: Record<string, string> = { tw: '台股', us: '美股', hk: '港股', cn: 'A股', jp: '日股', kr: '韓股', crypto: '加密' };
                  return names[m] || m;
                }).join('、')}
              </span>
            </div>
            <div className="flex justify-between">
              <span className="text-secondary-text">帳號狀態</span>
              <span className={`font-medium ${profile.active ? 'text-green-600' : 'text-red-500'}`}>
                {profile.active ? '已開通' : '待開通'}
              </span>
            </div>
            <div className="flex justify-between">
              <span className="text-secondary-text">註冊時間</span>
              <span className="font-medium text-foreground">{profile.createdAt ? new Date(profile.createdAt).toLocaleDateString('zh-TW') : '-'}</span>
            </div>
          </div>
        </div>

        {/* Upgrade CTA for free users */}
        {profile.plan === 'free' && (
          <div className="mt-6 rounded-xl border border-amber-200 bg-amber-50 p-5 text-center dark:border-amber-800 dark:bg-amber-900/20">
            <Zap className="mx-auto mb-2 h-6 w-6 text-amber-500" />
            <p className="font-semibold text-foreground">升級解鎖更多功能</p>
            <p className="mt-1 text-sm text-secondary-text">Pro 30 檔台美股 · Business 全市場無限</p>
            <button
              className="btn-primary mt-4"
              onClick={() => alert('請聯絡管理員升級方案')}
            >
              聯絡我們
            </button>
          </div>
        )}
      </main>
    </div>
  );
};

export default UserDashboardPage;
