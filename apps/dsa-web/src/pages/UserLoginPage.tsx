import React, { useState } from 'react';
import { motion, useMotionValue, useTransform, useSpring } from "motion/react";
import { Lock, Loader2, Cpu, TrendingUp, Network, Mail, ArrowRight, UserPlus } from "lucide-react";
import { Button, Input, ParticleBackground } from '../components/common';
import { UiLanguageToggle } from '../components/i18n/UiLanguageToggle';
import { userApi } from '../api/user';
import { getParsedApiError } from '../api/error';
import { SettingsAlert } from '../components/settings';

const UserLoginPage: React.FC = () => {
  const [email, setEmail] = useState('');
  const [password, setPassword] = useState('');
  const [isSubmitting, setIsSubmitting] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [mode, setMode] = useState<'login' | 'register'>('login');

  const mouseX = useMotionValue(0);
  const mouseY = useMotionValue(0);
  const smoothX = useSpring(mouseX, { damping: 30, stiffness: 200 });
  const smoothY = useSpring(mouseY, { damping: 30, stiffness: 200 });

  React.useEffect(() => {
    const handleMouseMove = (e: MouseEvent) => {
      mouseX.set(e.clientX / window.innerWidth - 0.5);
      mouseY.set(e.clientY / window.innerHeight - 0.5);
    };
    window.addEventListener("mousemove", handleMouseMove);
    return () => window.removeEventListener("mousemove", handleMouseMove);
  }, [mouseX, mouseY]);

  const handleSubmit = async (e: React.FormEvent) => {
    e.preventDefault();
    setError(null);
    setIsSubmitting(true);
    try {
      if (mode === 'register') {
        await userApi.register(email, password, password);
      }
      await userApi.login(email, password);
      window.location.href = '/';
    } catch (err) {
      const parsed = getParsedApiError(err);
      setError(parsed.message || (mode === 'login' ? '登入失敗' : '註冊失敗'));
    } finally {
      setIsSubmitting(false);
    }
  };

  return (
    <div className="relative flex min-h-screen flex-col justify-center overflow-hidden bg-[var(--login-bg-main)] py-12 font-sans selection:bg-[var(--login-accent-soft)] sm:px-6 lg:px-8 [perspective:1500px]">
      <ParticleBackground />
      <div className="absolute right-4 top-4 z-30">
        <UiLanguageToggle />
      </div>
      <div className="absolute inset-0 z-0 bg-[linear-gradient(to_right,var(--login-grid-line)_1px,transparent_1px),linear-gradient(to_bottom,var(--login-grid-line)_1px,transparent_1px)] bg-[size:24px_24px] [mask-image:var(--login-grid-mask)]" />
      
      <motion.div style={{ x: useTransform(smoothX, [-0.5, 0.5], [-50, 50]), y: useTransform(smoothY, [-0.5, 0.5], [-50, 50]) }}
        className="absolute left-[20%] top-[20%] -z-10 h-[300px] w-[300px] -translate-x-1/2 -translate-y-1/2 rounded-full bg-[var(--login-accent-glow)] blur-[100px]" />
      <motion.div style={{ x: useTransform(smoothX, [-0.5, 0.5], [60, -60]), y: useTransform(smoothY, [-0.5, 0.5], [60, -60]) }}
        className="absolute right-[20%] bottom-[10%] -z-10 h-[400px] w-[400px] translate-x-1/2 translate-y-1/2 rounded-full bg-emerald-600/10 blur-[120px]" />

      <div className="sm:mx-auto sm:w-full sm:max-w-md relative z-10">
        <motion.div initial={{ opacity: 0, y: -20 }} animate={{ opacity: 1, y: 0 }} transition={{ duration: 0.5 }} className="flex flex-col items-center justify-center mb-10 relative">
          <motion.div style={{ x: useTransform(smoothX, [-0.5, 0.5], [-8, 8]), y: useTransform(smoothY, [-0.5, 0.5], [-8, 8]), rotate: useTransform(smoothX, [-0.5, 0.5], [-0.5, 0.5]) }}
            className="pointer-events-none absolute -top-[20vh] -z-10 opacity-80">
            <div className="relative flex h-[120vh] w-[120vh] items-center justify-center rounded-full border border-[var(--login-accent-soft)] bg-gradient-to-br from-[var(--login-accent-soft)] to-[hsl(214_100%_20%_/_0.18)] shadow-[inset_0_0_200px_var(--login-accent-glow)] blur-[4px]">
              <Cpu className="h-[70vh] w-[70vh] text-[hsl(200_80%_22%_/_0.4)] brightness-50" />
              <TrendingUp className="absolute h-[25vh] w-[25vh] translate-x-[15vh] translate-y-[15vh] text-emerald-900/30 brightness-50" />
            </div>
          </motion.div>

          <div className="mt-8 flex flex-col items-center">
            <h2 className="text-4xl font-extrabold tracking-tighter sm:text-6xl">
              <span className="bg-gradient-to-r from-[var(--login-brand-start)] to-[var(--login-brand-end)] bg-clip-text text-transparent drop-shadow-[0_0_20px_var(--login-accent-glow)]">StockGPT</span>
            </h2>
            <h3 className="mt-1 text-xl font-bold uppercase tracking-[0.5em] text-[var(--login-text-muted)]">AI 投資分析平臺</h3>
          </div>

          <motion.div initial={{ opacity: 0 }} animate={{ opacity: 1 }} transition={{ delay: 0.3 }}
            className="mt-6 flex items-center gap-2 rounded-full border border-[var(--login-accent-border)] bg-[var(--login-accent-soft)] px-3 py-1 text-[10px] font-medium text-[var(--login-accent-text)] backdrop-blur-sm">
            <Network className="h-3 w-3" />
            <span>{mode === 'login' ? '用戶登入' : '註冊帳號'}</span>
          </motion.div>
        </motion.div>

        <motion.div initial={{ opacity: 0, scale: 0.95 }} animate={{ opacity: 1, scale: 1 }} transition={{ duration: 0.5, delay: 0.1 }}
          className="relative group z-20 pointer-events-auto">
          <div className="pointer-events-none absolute -inset-0.5 rounded-3xl bg-gradient-to-b from-[var(--login-accent-glow)] to-[hsl(214_100%_56%_/_0.18)] opacity-50 blur-sm transition duration-1000 group-hover:opacity-100 group-hover:duration-200" />
          <div className="pointer-events-auto relative flex flex-col overflow-hidden rounded-3xl border border-[var(--login-border-card)] bg-[var(--login-bg-card)]/80 p-8 shadow-2xl backdrop-blur-xl">
            <div className="absolute -right-20 -top-20 h-40 w-40 rounded-full bg-[var(--login-accent-soft)] blur-[50px]" />
            <div className="absolute -bottom-20 -left-20 h-40 w-40 rounded-full bg-blue-600/10 blur-[50px]" />

            <div className="mb-8">
              <h1 className="flex items-center gap-2 text-2xl font-bold tracking-tight text-[var(--login-text-primary)]">
                {mode === 'register' ? (
                  <><UserPlus className="h-6 w-6 text-emerald-400" /><span>註冊帳號</span></>
                ) : (
                  <><Lock className="h-5 w-5 text-[var(--login-accent-text)]" /><span>用戶登入</span></>
                )}
              </h1>
              <p className="mt-2 text-sm text-[var(--login-text-secondary)]">
                {mode === 'register' ? '建立您的 StockGPT 帳號，開始 AI 量化分析' : '使用 Email 和密碼登入您的帳號'}
              </p>
            </div>

            <form onSubmit={handleSubmit} className="space-y-6">
              <div className="space-y-4">
                <Input id="email" type="email" appearance="login"
                  label="Email" placeholder="your@email.com" value={email}
                  onChange={(e) => setEmail(e.target.value)} disabled={isSubmitting} autoFocus autoComplete="email" />
                <Input id="password" type="password" appearance="login" allowTogglePassword iconType="password"
                  label="密碼" placeholder={mode === 'register' ? '至少 6 個字元' : '輸入密碼'} value={password}
                  onChange={(e) => setPassword(e.target.value)} disabled={isSubmitting}
                  autoComplete={mode === 'register' ? 'new-password' : 'current-password'} />
              </div>

              {error && (
                <motion.div initial={{ opacity: 0, height: 0 }} animate={{ opacity: 1, height: 'auto' }} className="overflow-hidden">
                  <SettingsAlert title="驗證失敗" message={error} variant="error"
                    className="!border-[var(--login-error-border)] !bg-[var(--login-error-bg)] !text-[var(--login-error-text)]" />
                </motion.div>
              )}

              <Button type="submit" variant="primary" size="lg"
                className="group/btn relative h-12 w-full overflow-hidden rounded-xl border-0 bg-gradient-to-r from-[var(--login-brand-button-start)] to-[var(--login-brand-button-end)] font-medium text-[var(--login-button-text)] shadow-lg shadow-[0_18px_36px_hsl(214_100%_8%_/_0.24)] hover:from-[var(--login-brand-button-start-hover)] hover:to-[var(--login-brand-button-end-hover)]"
                disabled={isSubmitting}>
                <div className="relative z-10 flex items-center justify-center gap-2">
                  {isSubmitting ? (
                    <><Loader2 className="h-4 w-4 animate-spin" /><span>處理中...</span></>
                  ) : mode === 'register' ? (
                    <><ArrowRight className="h-4 w-4" /><span>註冊並登入</span></>
                  ) : (
                    <><ArrowRight className="h-4 w-4" /><span>登入</span></>
                  )}
                </div>
                <div className="absolute inset-0 z-0 bg-gradient-to-r from-transparent via-white/10 to-transparent -translate-x-full group-hover:animate-[shimmer_1.5s_infinite] pointer-events-none" />
              </Button>

              {/* Google Login */}
              <div className="relative my-2">
                <div className="absolute inset-0 flex items-center"><div className="w-full border-t border-[var(--login-border-card)]" /></div>
                <div className="relative flex justify-center text-xs"><span className="bg-[var(--login-bg-card)] px-3 text-[var(--login-text-muted)]">或</span></div>
              </div>
              <button
                type="button"
                onClick={() => window.location.href = '/api/v1/user/google-login'}
                className="flex w-full items-center justify-center gap-2 rounded-xl border border-[var(--login-border-card)] bg-white px-4 py-2.5 text-sm font-medium text-gray-700 transition-colors hover:bg-gray-50"
              >
                <svg className="h-5 w-5" viewBox="0 0 24 24"><path fill="#4285F4" d="M22.56 12.25c0-.78-.07-1.53-.2-2.25H12v4.26h5.92a5.06 5.06 0 0 1-2.2 3.32v2.77h3.57c2.08-1.92 3.28-4.74 3.28-8.1z"/><path fill="#34A853" d="M12 23c2.97 0 5.46-.98 7.28-2.66l-3.57-2.77c-.98.66-2.23 1.06-3.71 1.06-2.86 0-5.29-1.93-6.16-4.53H2.18v2.84C3.99 20.53 7.7 23 12 23z"/><path fill="#FBBC05" d="M5.84 14.09c-.22-.66-.35-1.36-.35-2.09s.13-1.43.35-2.09V7.07H2.18C1.43 8.55 1 10.22 1 12s.43 3.45 1.18 4.93l2.85-2.22.81-.62z"/><path fill="#EA4335" d="M12 5.38c1.62 0 3.06.56 4.21 1.64l3.15-3.15C17.45 2.09 14.97 1 12 1 7.7 1 3.99 3.47 2.18 7.07l3.66 2.84c.87-2.6 3.3-4.53 6.16-4.53z"/></svg>
                使用 Google 帳號{mode === 'register' ? '註冊' : '登入'}
              </button>

              <p className="text-center text-sm text-[var(--login-text-secondary)]">
                {mode === 'login' ? (
                  <>還沒有帳號？{' '}<button type="button" onClick={() => { setMode('register'); setError(null); }} className="font-medium text-[var(--login-accent-text)] hover:underline">註冊</button></>
                ) : (
                  <>已有帳號？{' '}<button type="button" onClick={() => { setMode('login'); setError(null); }} className="font-medium text-[var(--login-accent-text)] hover:underline">登入</button></>
                )}
              </p>
            </form>
          </div>
        </motion.div>

        <motion.p initial={{ opacity: 0 }} animate={{ opacity: 1 }} transition={{ delay: 0.6 }}
          className="mt-8 text-center font-mono text-xs uppercase tracking-wider text-[var(--login-text-muted)]">
          Secure Connection · StockGPT
        </motion.p>
      </div>

      <style dangerouslySetInnerHTML={{ __html: `@keyframes shimmer { 100% { transform: translateX(100%); } }` }} />
    </div>
  );
};

export default UserLoginPage;
