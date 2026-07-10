import React, { useState } from 'react';
import { useNavigate } from 'react-router-dom';
import { BarChart3, Mail, Lock, ArrowRight, AlertCircle } from 'lucide-react';
import { userApi } from '../api/user';
import { getParsedApiError } from '../api/error';

export const UserRegisterPage: React.FC = () => {
  const navigate = useNavigate();
  const [email, setEmail] = useState('');
  const [password, setPassword] = useState('');
  const [passwordConfirm, setPasswordConfirm] = useState('');
  const [error, setError] = useState('');
  const [loading, setLoading] = useState(false);
  const [success, setSuccess] = useState(false);

  const handleSubmit = async (e: React.FormEvent) => {
    e.preventDefault();
    setError('');

    if (password !== passwordConfirm) {
      setError('兩次輸入的密碼不一致');
      return;
    }
    if (password.length < 6) {
      setError('密碼至少需要 6 個字元');
      return;
    }

    setLoading(true);
    try {
      await userApi.register(email, password, passwordConfirm);
      setSuccess(true);
      // Auto login after registration
      setTimeout(async () => {
        try {
          await userApi.login(email, password);
          navigate('/user/dashboard', { replace: true });
        } catch {
          setError('註冊成功但自動登入失敗，請手動登入');
        }
      }, 1000);
    } catch (err) {
      const parsed = getParsedApiError(err);
      setError(parsed.message || '註冊失敗，請稍後再試');
    } finally {
      setLoading(false);
    }
  };

  if (success) {
    return (
      <div className="flex min-h-screen items-center justify-center bg-base px-4">
        <div className="w-full max-w-sm text-center">
          <div className="mx-auto mb-4 flex h-16 w-16 items-center justify-center rounded-full bg-green-100 dark:bg-green-900/30">
            <BarChart3 className="h-8 w-8 text-green-600" />
          </div>
          <h1 className="mb-2 text-xl font-bold text-foreground">註冊成功！</h1>
          <p className="text-secondary-text">正在為您自動登入...</p>
        </div>
      </div>
    );
  }

  return (
    <div className="flex min-h-screen items-center justify-center bg-base px-4">
      <div className="w-full max-w-sm">
        <div className="mb-8 text-center">
          <div className="mx-auto mb-4 flex h-14 w-14 items-center justify-center rounded-2xl bg-primary-gradient shadow-lg">
            <BarChart3 className="h-7 w-7 text-white" />
          </div>
          <h1 className="text-xl font-bold text-foreground">加入 StockGPT</h1>
          <p className="mt-1 text-sm text-secondary-text">AI 驅動的量化投資分析平台</p>
        </div>

        <form onSubmit={(e) => void handleSubmit(e)} className="space-y-4">
          {error && (
            <div className="flex items-center gap-2 rounded-lg bg-red-50 px-4 py-3 text-sm text-red-600 dark:bg-red-900/20 dark:text-red-400">
              <AlertCircle className="h-4 w-4 shrink-0" />
              {error}
            </div>
          )}

          <div>
            <label className="mb-1.5 block text-sm font-medium text-foreground">Email</label>
            <div className="relative">
              <Mail className="absolute left-3 top-1/2 h-4 w-4 -translate-y-1/2 text-secondary-text" />
              <input
                type="email"
                value={email}
                onChange={(e) => setEmail(e.target.value)}
                required
                className="w-full rounded-lg border border-border bg-input py-2.5 pl-10 pr-3 text-sm text-foreground placeholder:text-secondary-text/60 focus:outline-none focus:ring-2 focus:ring-primary"
                placeholder="your@email.com"
              />
            </div>
          </div>

          <div>
            <label className="mb-1.5 block text-sm font-medium text-foreground">密碼</label>
            <div className="relative">
              <Lock className="absolute left-3 top-1/2 h-4 w-4 -translate-y-1/2 text-secondary-text" />
              <input
                type="password"
                value={password}
                onChange={(e) => setPassword(e.target.value)}
                required
                minLength={6}
                className="w-full rounded-lg border border-border bg-input py-2.5 pl-10 pr-3 text-sm text-foreground focus:outline-none focus:ring-2 focus:ring-primary"
                placeholder="至少 6 個字元"
              />
            </div>
          </div>

          <div>
            <label className="mb-1.5 block text-sm font-medium text-foreground">確認密碼</label>
            <div className="relative">
              <Lock className="absolute left-3 top-1/2 h-4 w-4 -translate-y-1/2 text-secondary-text" />
              <input
                type="password"
                value={passwordConfirm}
                onChange={(e) => setPasswordConfirm(e.target.value)}
                required
                minLength={6}
                className="w-full rounded-lg border border-border bg-input py-2.5 pl-10 pr-3 text-sm text-foreground focus:outline-none focus:ring-2 focus:ring-primary"
                placeholder="再次輸入密碼"
              />
            </div>
          </div>

          <button
            type="submit"
            disabled={loading}
            className="flex w-full items-center justify-center gap-2 rounded-lg bg-primary py-2.5 text-sm font-medium text-primary-foreground hover:bg-primary/90 disabled:opacity-50"
          >
            {loading ? '註冊中...' : '註冊免費帳號'}
            {!loading && <ArrowRight className="h-4 w-4" />}
          </button>
        </form>

        <p className="mt-4 text-center text-sm text-secondary-text">
          已有帳號？{' '}
          <a href="/user/login" className="font-medium text-primary hover:underline">
            登入
          </a>
        </p>

        {/* Plans summary */}
        <div className="mt-8 space-y-2 rounded-xl border border-border bg-card p-4">
          <h3 className="text-sm font-semibold text-foreground">方案比較</h3>
          <div className="grid grid-cols-3 gap-2 text-xs">
            <div className="rounded-lg bg-slate-100 p-2 text-center dark:bg-slate-800">
              <p className="font-semibold">Free</p>
              <p className="text-secondary-text">5 檔台股</p>
              <p className="font-bold text-foreground">免費</p>
            </div>
            <div className="rounded-lg bg-blue-100 p-2 text-center dark:bg-blue-900/30">
              <p className="font-semibold text-blue-700 dark:text-blue-400">Pro</p>
              <p className="text-secondary-text">30 檔台美</p>
              <p className="font-bold text-foreground">NT$99/月</p>
            </div>
            <div className="rounded-lg bg-amber-100 p-2 text-center dark:bg-amber-900/30">
              <p className="font-semibold text-amber-700 dark:text-amber-400">Business</p>
              <p className="text-secondary-text">全市場無限</p>
              <p className="font-bold text-foreground">NT$299/月</p>
            </div>
          </div>
        </div>
      </div>
    </div>
  );
};

export default UserRegisterPage;
