import apiClient from '../api/index';

export type UserProfile = {
  email: string;
  plan: string;
  stocksLimit: number;
  markets: string;
  active: boolean;
  createdAt: string;
  lastLogin: string | null;
};

export type UserStatusResponse = {
  loggedIn: boolean;
  user: {
    email: string;
    plan: string;
    stocksLimit: number;
  } | null;
};

export const userApi = {
  async register(email: string, password: string, passwordConfirm: string): Promise<void> {
    await apiClient.post('/api/v1/user/register', { email, password, passwordConfirm });
  },

  async login(email: string, password: string): Promise<void> {
    await apiClient.post('/api/v1/user/login', { email, password });
  },

  async getProfile(): Promise<UserProfile> {
    const { data } = await apiClient.get<UserProfile>('/api/v1/user/profile');
    return data;
  },

  async getStatus(): Promise<UserStatusResponse> {
    const { data } = await apiClient.get<UserStatusResponse>('/api/v1/user/status');
    return data;
  },

  async logout(): Promise<void> {
    await apiClient.post('/api/v1/user/logout');
  },
};
