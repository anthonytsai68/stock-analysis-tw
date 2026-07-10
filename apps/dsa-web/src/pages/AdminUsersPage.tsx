import React, { useCallback, useEffect, useState } from 'react';
import { Shield, Users, Check, X, Zap, Crown, Star } from 'lucide-react';
import { adminApi, type UserInfo, type PlanInfo } from '../api/admin';
import { getParsedApiError, type ParsedApiError } from '../api/error';
import { ApiErrorAlert } from '../components/common';

const PLAN_ICONS: Record<string, React.ComponentType<{ className?: string }>> = {
  free: Star,
  pro: Zap,
  business: Crown,
};

const PLAN_COLORS: Record<string, string> = {
  free: 'text-slate-400',
  pro: 'text-blue-500',
  business: 'text-amber-500',
};

const PLAN_BG: Record<string, string> = {
  free: 'bg-slate-100 dark:bg-slate-800',
  pro: 'bg-blue-100 dark:bg-blue-900/30',
  business: 'bg-amber-100 dark:bg-amber-900/30',
};

const PLAN_BADGE: Record<string, string> = {
  free: 'bg-slate-200 text-slate-700 dark:bg-slate-700 dark:text-slate-300',
  pro: 'bg-blue-200 text-blue-700 dark:bg-blue-800 dark:text-blue-300',
  business: 'bg-amber-200 text-amber-700 dark:bg-amber-800 dark:text-amber-300',
};

export const AdminUsersPage: React.FC = () => {
  const [users, setUsers] = useState<UserInfo[]>([]);
  const [plans, setPlans] = useState<Record<string, PlanInfo>>({});
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<ParsedApiError | null>(null);
  const [actionMsg, setActionMsg] = useState<string | null>(null);

  const fetchUsers = useCallback(async () => {
    setLoading(true);
    setError(null);
    try {
      const result = await adminApi.getUsers();
      setUsers(result.users);
      setPlans(result.plans);
    } catch (err) {
      setError(getParsedApiError(err));
    } finally {
      setLoading(false);
    }
  }, []);

  useEffect(() => {
    void fetchUsers();
  }, [fetchUsers]);

  const handleActivate = async (userId: number) => {
    try {
      await adminApi.activateUser(userId);
      setActionMsg('已開通');
      setTimeout(() => setActionMsg(null), 2000);
      void fetchUsers();
    } catch (err) {
      setActionMsg('操作失敗');
      setTimeout(() => setActionMsg(null), 2000);
    }
  };

  const handleDeactivate = async (userId: number) => {
    try {
      await adminApi.deactivateUser(userId);
      setActionMsg('已停用');
      setTimeout(() => setActionMsg(null), 2000);
      void fetchUsers();
    } catch (err) {
      setActionMsg('操作失敗');
      setTimeout(() => setActionMsg(null), 2000);
    }
  };

  const handlePlanChange = async (userId: number, plan: string) => {
    try {
      await adminApi.updatePlan(userId, plan);
      setActionMsg(`方案已更新為 ${plan}`);
      setTimeout(() => setActionMsg(null), 2000);
      void fetchUsers();
    } catch (err) {
      setActionMsg('方案更新失敗');
      setTimeout(() => setActionMsg(null), 2000);
    }
  };

  const planNames: Record<string, string> = {
    free: 'Free',
    pro: 'Pro (30檔)',
    business: 'Business (無限)',
  };

  if (loading) {
    return (
      <div className="flex min-h-[400px] items-center justify-center">
        <div className="h-8 w-8 animate-spin rounded-full border-4 border-primary border-t-transparent" />
      </div>
    );
  }

  return (
    <div className="mx-auto max-w-6xl px-4 py-8">
      <div className="mb-8 flex items-center gap-3">
        <Shield className="h-7 w-7 text-primary" />
        <div>
          <h1 className="text-2xl font-bold text-foreground">用戶管理</h1>
          <p className="text-sm text-secondary-text">管理訂閱方案與開通狀態</p>
        </div>
      </div>

      {error && (
        <div className="mb-6">
          <ApiErrorAlert error={error} />
          <button type="button" className="btn-secondary mt-3" onClick={() => void fetchUsers()}>
            重試
          </button>
        </div>
      )}

      {actionMsg && (
        <div className="mb-4 rounded-lg bg-green-100 px-4 py-2 text-sm text-green-700 dark:bg-green-900/30 dark:text-green-400">
          {actionMsg}
        </div>
      )}

      {users.length === 0 && !error ? (
        <div className="flex min-h-[200px] flex-col items-center justify-center gap-3 text-secondary-text">
          <Users className="h-12 w-12" />
          <p>尚無註冊用戶</p>
        </div>
      ) : (
        <div className="overflow-hidden rounded-xl border border-border bg-card">
          <table className="w-full text-left text-sm">
            <thead className="border-b border-border bg-muted">
              <tr>
                <th className="px-4 py-3 font-medium">Email</th>
                <th className="px-4 py-3 font-medium">方案</th>
                <th className="px-4 py-3 font-medium">額度</th>
                <th className="px-4 py-3 font-medium">狀態</th>
                <th className="px-4 py-3 font-medium">註冊時間</th>
                <th className="px-4 py-3 font-medium">最後登入</th>
                <th className="px-4 py-3 font-medium">操作</th>
              </tr>
            </thead>
            <tbody>
              {users.map((user) => {
                const PlanIcon = PLAN_ICONS[user.plan] || Star;
                return (
                  <tr key={user.id} className="border-b border-border last:border-0 hover:bg-muted/50">
                    <td className="px-4 py-3 font-medium">{user.email}</td>
                    <td className="px-4 py-3">
                      <span className={`inline-flex items-center gap-1.5 rounded-full px-2.5 py-0.5 text-xs font-medium ${PLAN_BADGE[user.plan] || PLAN_BADGE.free}`}>
                        <PlanIcon className="h-3 w-3" />
                        {planNames[user.plan] || user.plan}
                      </span>
                    </td>
                    <td className="px-4 py-3 text-secondary-text">
                      {user.stocksLimit === 0 ? '∞' : `${user.stocksLimit} 檔`}
                      <span className="ml-1 text-xs opacity-60">({user.markets})</span>
                    </td>
                    <td className="px-4 py-3">
                      {user.active ? (
                        <span className="inline-flex items-center gap-1 text-green-600 dark:text-green-400">
                          <Check className="h-3.5 w-3.5" /> 已開通
                        </span>
                      ) : (
                        <span className="inline-flex items-center gap-1 text-red-500">
                          <X className="h-3.5 w-3.5" /> 停用
                        </span>
                      )}
                    </td>
                    <td className="px-4 py-3 text-secondary-text text-xs">
                      {user.createdAt ? new Date(user.createdAt).toLocaleDateString('zh-TW') : '-'}
                    </td>
                    <td className="px-4 py-3 text-secondary-text text-xs">
                      {user.lastLogin ? new Date(user.lastLogin).toLocaleDateString('zh-TW') : '從未登入'}
                    </td>
                    <td className="px-2 py-3">
                      <div className="flex items-center gap-1">
                        <select
                          value={user.plan}
                          onChange={(e) => void handlePlanChange(user.id, e.target.value)}
                          className="rounded border border-border bg-input px-2 py-1 text-xs focus:outline-none focus:ring-1 focus:ring-primary"
                        >
                          <option value="free">Free</option>
                          <option value="pro">Pro</option>
                          <option value="business">Business</option>
                        </select>
                        {user.active ? (
                          <button
                            type="button"
                            onClick={() => void handleDeactivate(user.id)}
                            className="rounded px-2 py-1 text-xs text-red-600 hover:bg-red-50 dark:hover:bg-red-900/20"
                          >
                            停用
                          </button>
                        ) : (
                          <button
                            type="button"
                            onClick={() => void handleActivate(user.id)}
                            className="rounded px-2 py-1 text-xs text-green-600 hover:bg-green-50 dark:hover:bg-green-900/20"
                          >
                            開通
                          </button>
                        )}
                      </div>
                    </td>
                  </tr>
                );
              })}
            </tbody>
          </table>
        </div>
      )}

      {/* Plan summary */}
      <div className="mt-8">
        <h2 className="mb-4 text-lg font-semibold text-foreground">方案總覽</h2>
        <div className="grid grid-cols-1 gap-4 sm:grid-cols-3">
          {Object.entries(plans).map(([key, plan]) => {
            const PlanIcon = PLAN_ICONS[key] || Star;
            return (
              <div key={key} className={`rounded-xl border border-border p-5 ${PLAN_BG[key] || ''}`}>
                <div className="mb-2 flex items-center gap-2">
                  <PlanIcon className={`h-5 w-5 ${PLAN_COLORS[key] || ''}`} />
                  <span className="font-semibold">{plan.name}</span>
                </div>
                <p className="text-2xl font-bold">NT${plan.price_ntd}<span className="text-sm font-normal text-secondary-text">/月</span></p>
                <ul className="mt-3 space-y-1 text-sm text-secondary-text">
                  <li>{plan.max_stocks === 0 ? '📍 無限檔' : `📍 最多 ${plan.max_stocks} 檔`}</li>
                  <li>🌍 {plan.markets.map(m => ({tw:'台股',us:'美股',hk:'港股',cn:'A股',jp:'日股',kr:'韓股',crypto:'加密'}[m] || m)).join(', ')}</li>
                </ul>
              </div>
            );
          })}
        </div>
      </div>
    </div>
  );
};

export default AdminUsersPage;
