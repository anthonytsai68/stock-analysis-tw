import apiClient from './index';

export type UserInfo = {
  id: number;
  email: string;
  plan: string;
  stocksLimit: number;
  markets: string;
  active: boolean;
  createdAt: string;
  lastLogin: string | null;
  notes: string | null;
};

export type PlanInfo = {
  name: string;
  max_stocks: number;
  markets: string[];
  price_ntd: number;
};

export type AdminUsersResponse = {
  users: UserInfo[];
  plans: Record<string, PlanInfo>;
};

export const adminApi = {
  async getUsers(): Promise<AdminUsersResponse> {
    const { data } = await apiClient.get<AdminUsersResponse>('/api/v1/admin/users');
    return data;
  },

  async getUser(userId: number): Promise<UserInfo> {
    const { data } = await apiClient.get<UserInfo>(`/api/v1/admin/users/${userId}`);
    return data;
  },

  async activateUser(userId: number): Promise<void> {
    await apiClient.post(`/api/v1/admin/users/${userId}/activate`);
  },

  async deactivateUser(userId: number): Promise<void> {
    await apiClient.post(`/api/v1/admin/users/${userId}/deactivate`);
  },

  async updatePlan(userId: number, plan: string, notes?: string): Promise<void> {
    await apiClient.post(`/api/v1/admin/users/${userId}/plan`, { plan, notes });
  },
};
