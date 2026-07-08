import type React from 'react';
import { Fragment, useCallback, useEffect, useMemo, useRef, useState } from 'react';
import {
  Activity,
  Bookmark,
  Building2,
  CheckCircle2,
  ChevronDown,
  CircleAlert,
  Clock3,
  Droplet,
  Factory,
  Flame,
  Gem,
  Landmark,
  Pickaxe,
  Plane,
  Play,
  PlusCircle,
  RefreshCw,
  Search,
  Shield,
  SlidersHorizontal,
  Stethoscope,
  Trees,
  Utensils,
  Wrench,
} from 'lucide-react';
import { useNavigate } from 'react-router-dom';
import {
  alphasiftApi,
  type AlphaSiftCandidate,
  type AlphaSiftHotspotDetail,
  type AlphaSiftHotspot,
  type AlphaSiftHotspotsResponse,
  type AlphaSiftScreenResponse,
  type AlphaSiftScreenTaskStatus,
  type AlphaSiftStrategy,
} from '../api/alphasift';
import { formatParsedApiError, getParsedApiError, toApiErrorMessage, type ParsedApiError } from '../api/error';
import { AppPage, Button, InlineAlert } from '../components/common';

const MARKETS = [{ id: 'cn', label: 'A 股' }];
const SCREEN_TASK_STORAGE_KEY = 'dsa.alphasift.activeScreenTask.v1';
const SCREEN_TASK_POLL_INTERVAL_MS = 2000;

type PersistedScreenTask = {
  taskId: string;
  market: string;
  strategy: string;
  maxResults: number;
};

const readPersistedScreenTask = (): PersistedScreenTask | null => {
  if (typeof window === 'undefined') {
    return null;
  }
  try {
    const raw = window.sessionStorage.getItem(SCREEN_TASK_STORAGE_KEY);
    if (!raw) {
      return null;
    }
    const parsed = JSON.parse(raw) as Partial<PersistedScreenTask>;
    if (typeof parsed.taskId !== 'string' || !parsed.taskId.trim()) {
      return null;
    }
    const restoredMaxResults = Number(parsed.maxResults);
    return {
      taskId: parsed.taskId,
      market: typeof parsed.market === 'string' && parsed.market.trim() ? parsed.market : 'cn',
      strategy: typeof parsed.strategy === 'string' && parsed.strategy.trim() ? parsed.strategy : 'dual_low',
      maxResults: Number.isFinite(restoredMaxResults) ? Math.min(100, Math.max(1, restoredMaxResults)) : 3,
    };
  } catch {
    return null;
  }
};

const persistScreenTask = (task: PersistedScreenTask) => {
  try {
    window.sessionStorage.setItem(SCREEN_TASK_STORAGE_KEY, JSON.stringify(task));
  } catch {
    // Session storage is best-effort; polling still works while the page stays mounted.
  }
};

const clearPersistedScreenTask = () => {
  try {
    window.sessionStorage.removeItem(SCREEN_TASK_STORAGE_KEY);
  } catch {
    // Ignore storage cleanup failures.
  }
};

const isUnrecoverableScreenTaskError = (error: ParsedApiError) =>
  error.title === '選股任務不可恢復';

const formatRecoverableScreenTaskPollingError = (error: ParsedApiError) => {
  if (error.category === 'upstream_timeout') {
    return '選股任務仍在後臺運行，狀態輪詢暫時超時，將自動重試。';
  }
  if (error.category === 'upstream_network' || error.category === 'local_connection_failed') {
    return '選股任務仍在後臺運行，暫時無法連接本地服務獲取狀態，將自動重試。';
  }
  return formatParsedApiError(error) || '暫時無法獲取選股任務狀態，稍後將自動重試。';
};

const formatScore = (score: AlphaSiftCandidate['score']) => {
  if (score == null || Number.isNaN(Number(score))) {
    return '-';
  }
  return Number(score).toFixed(2);
};

const formatNumber = (value: unknown, digits = 2) => {
  if (value == null || value === '' || Number.isNaN(Number(value))) {
    return '-';
  }
  return Number(value).toFixed(digits);
};

const formatAmount = (value: unknown) => {
  if (value == null || value === '' || Number.isNaN(Number(value))) {
    return '-';
  }
  const amount = Number(value);
  if (Math.abs(amount) >= 100_000_000) {
    return `${(amount / 100_000_000).toFixed(2)} 億`;
  }
  if (Math.abs(amount) >= 10_000) {
    return `${(amount / 10_000).toFixed(2)} 萬`;
  }
  return amount.toFixed(2);
};

const formatPercent = (value: unknown) => {
  if (value == null || value === '' || Number.isNaN(Number(value))) {
    return '-';
  }
  return `${(Number(value) * 100).toFixed(0)}%`;
};

const getCandidateReason = (item: AlphaSiftCandidate) => {
  if (item.reason) {
    return item.reason;
  }
  const summaries = item.postAnalysisSummaries || {};
  const summary = Object.values(summaries).find((value) => typeof value === 'string' && value.trim());
  if (typeof summary === 'string') {
    return summary;
  }
  return 'AlphaSift 返回候選，但沒有給出文字摘要。請查看下方因子、風險和原始字段。';
};

const getSignal = (item: AlphaSiftCandidate) => {
  const rawSignal = item.raw.action ?? item.raw.signal ?? item.raw.recommendation;
  return typeof rawSignal === 'string' && rawSignal.trim() ? rawSignal : '觀察';
};

const getFactorEntries = (item: AlphaSiftCandidate) =>
  Object.entries(item.factorScores || {})
    .filter(([, value]) => typeof value === 'number')
    .sort((a, b) => Number(b[1]) - Number(a[1]))
    .slice(0, 6);

const toMessageList = (values: string[] | undefined) =>
  Array.isArray(values) ? values.map((value) => String(value).trim()).filter(Boolean) : [];

const KNOWN_SNAPSHOT_SOURCES = new Set(['tushare', 'efinance', 'akshare_em', 'em_datacenter', 'baostock']);
const MAX_MESSAGE_DETAIL_LENGTH = 96;

const truncateMessageDetail = (value: string, maxLength = MAX_MESSAGE_DETAIL_LENGTH) => {
  const text = value.replace(/\s+/g, ' ').trim();
  if (text.length <= maxLength) {
    return text;
  }
  return `${text.slice(0, maxLength - 1)}…`;
};

const summarizeAlphaSiftDiagnostic = (detail: string) => {
  if (/trade_cal returned no open trading days/i.test(detail)) {
    return '交易日曆暫無可用開市日';
  }
  if (/too many requests|rate limit|http\s*429/i.test(detail)) {
    return '請求過於頻繁';
  }
  if (/403 forbidden|forbidden|access denied/i.test(detail)) {
    return '訪問被拒絕';
  }
  if (/timeout|timed out/i.test(detail)) {
    return '請求超時';
  }
  if (/RemoteDisconnected|Connection aborted|ProtocolError|ConnectionPool|Max retries exceeded|ProxyError|NameResolutionError/i.test(detail)) {
    return '網絡連接中斷';
  }
  if (/missing .*api key|GEMINI_API_KEY|GOOGLE_API_KEY|gemini_api_key/i.test(detail)) {
    return '缺少可用 LLM API Key';
  }
  if (/returned no data|empty/i.test(detail)) {
    return '未返回可用數據';
  }

  const withoutUrl = detail
    .replace(/https?:\/\/\S+/gi, 'URL')
    .replace(/\bwith url:\s*\S+/gi, 'with url: URL')
    .replace(/\burl:\s*\S+/gi, 'url: URL');
  return truncateMessageDetail(withoutUrl);
};

const parseSourceDiagnostic = (value: string) => {
  const match = value.match(/^([a-zA-Z0-9_-]+)\s*[:：]\s*(.+)$/);
  if (!match) {
    return null;
  }
  return {
    source: match[1],
    detail: match[2],
  };
};

const normalizeScreenMessageKey = (value: string) => {
  const formatted = formatScreenMessage(value);
  return formatted ? formatted.trim().toLowerCase() : value.trim().toLowerCase();
};

const formatScreenMessage = (value: string) => {
  if (/^DSA provider context applied \d+ of \d+ candidates/i.test(value)) {
    return '';
  }
  if (/^LLM ranking failed/i.test(value)) {
    return `LLM 重排失敗：${summarizeAlphaSiftDiagnostic(value)}，已回退到本地因子評分。`;
  }

  const snapshotFallback = value.match(/^Snapshot source fallback:\s*(.+)$/i);
  if (snapshotFallback) {
    const parsed = parseSourceDiagnostic(snapshotFallback[1]);
    if (parsed) {
      return `數據源降級：${parsed.source}（${summarizeAlphaSiftDiagnostic(parsed.detail)}）`;
    }
    return `數據源降級：${summarizeAlphaSiftDiagnostic(snapshotFallback[1])}`;
  }

  const parsed = parseSourceDiagnostic(value);
  if (parsed && KNOWN_SNAPSHOT_SOURCES.has(parsed.source.toLowerCase())) {
    return `數據源降級：${parsed.source}（${summarizeAlphaSiftDiagnostic(parsed.detail)}）`;
  }
  return truncateMessageDetail(value);
};

const getScreenMessages = (meta: AlphaSiftScreenResponse | null) => {
  if (!meta) {
    return [];
  }
  const messages: string[] = [];
  const seen = new Set<string>();
  [...toMessageList(meta.warnings), ...toMessageList(meta.sourceErrors), ...toMessageList(meta.llmParseErrors)].forEach(
    (value) => {
      const key = normalizeScreenMessageKey(value);
      if (seen.has(key)) {
        return;
      }
      const message = formatScreenMessage(value);
      if (!message) {
        return;
      }
      seen.add(key);
      messages.push(message);
    },
  );
  return messages;
};

const isRunningScreenTask = (status: string | undefined | null) => status === 'pending' || status === 'processing';

const formatScreenTaskFailure = (value: string | null | undefined) => {
  const text = String(value || '').trim();
  if (!text) {
    return '選股任務失敗，請稍後重試。';
  }
  return `選股任務失敗：${summarizeAlphaSiftDiagnostic(text)}`;
};

const ALPHASIFT_HOTSPOT_NO_CACHE_HINT = 'No cached AlphaSift hotspot snapshot. Click refresh to fetch live hotspots.';
const ALPHASIFT_HOTSPOT_UNAVAILABLE_CODE = 'eastmoney_hotspot_unavailable';

const formatHotspotEmptyMessage = (result: AlphaSiftHotspotsResponse) => {
  const message = String(result.message || '').trim();
  const sourceErrors = result.sourceErrors || [];
  if (message && sourceErrors.includes(ALPHASIFT_HOTSPOT_UNAVAILABLE_CODE)) {
    return message;
  }
  if (message === ALPHASIFT_HOTSPOT_NO_CACHE_HINT) {
    return '暫無緩存熱點題材，展開後可點擊刷新拉取實時數據。';
  }
  const sourceError = sourceErrors[0];
  if (sourceError) {
    return `熱點題材暫未返回數據：${summarizeAlphaSiftDiagnostic(sourceError)}`;
  }
  return '熱點題材暫未返回數據';
};

const ScreenAlertMessage: React.FC<{ messages: string[] }> = ({ messages }) => {
  if (messages.length <= 1) {
    return <span>{messages[0]}</span>;
  }
  return (
    <ul className="list-disc space-y-1 pl-4">
      {messages.map((message) => (
        <li key={message}>{message}</li>
      ))}
    </ul>
  );
};

const hasLlmInsight = (item: AlphaSiftCandidate) =>
  Boolean(
    item.llmThesis ||
      item.llmSector ||
      item.llmTheme ||
      item.llmConfidence != null ||
      item.llmWatchItems?.length ||
      item.llmCatalysts?.length,
  );

const getRouteTimeLabel = (item: AlphaSiftHotspotDetail['route'][number]) => {
  const rawTime = item.publishedAt || item.date || item.time || '';
  if (!rawTime) {
    return item.source || '待確認';
  }
  if (/^\d{4}-\d{2}-\d{2}$/.test(rawTime)) {
    return rawTime;
  }
  const parsed = new Date(rawTime);
  if (!Number.isNaN(parsed.getTime())) {
    return parsed.toLocaleString('zh-CN', {
      month: '2-digit',
      day: '2-digit',
      hour: '2-digit',
      minute: '2-digit',
      hour12: false,
    });
  }
  return rawTime;
};

const getHotspotRouteItems = (detail: AlphaSiftHotspotDetail) => {
  const route = detail.route || [];
  if (route.length > 0) {
    return route;
  }
  return detail.timeline || [];
};

const formatHotspotMetric = (value: unknown, digits = 1) => {
  const formatted = formatNumber(value, digits);
  return formatted === '-' ? '觀察中' : formatted;
};

const getHotspotLeadersText = (item: AlphaSiftHotspot) => {
  const leaders = (item.leaders || []).map((value) => String(value).trim()).filter(Boolean);
  if (leaders.length > 0) {
    return leaders.slice(0, 2).join('、');
  }
  return '觀察中';
};

const getHotspotSampleText = (item: AlphaSiftHotspot) => {
  if (item.sampleStockCount == null || Number.isNaN(Number(item.sampleStockCount))) {
    return '活躍股觀察中';
  }
  return `覆蓋 ${item.sampleStockCount} 股`;
};

const formatStockChangeText = (value: unknown) => {
  const formatted = formatNumber(value);
  return formatted === '-' ? '行情待取' : `${formatted}%`;
};

const formatHotspotUpdatedAt = (value: string | null) => {
  if (!value) {
    return '待刷新';
  }
  const parsed = new Date(value);
  if (Number.isNaN(parsed.getTime())) {
    return value;
  }
  return parsed.toLocaleString('zh-CN', {
    year: 'numeric',
    month: '2-digit',
    day: '2-digit',
    hour: '2-digit',
    minute: '2-digit',
    second: '2-digit',
    hour12: false,
  });
};

const getHotspotStrength = (item: AlphaSiftHotspot, index: number) => {
  const heat = Number(item.heatScore ?? 0);
  const changePct = Number(item.changePct ?? 0);
  if (index === 0 || heat >= 90 || changePct >= 8) {
    return { label: '強勢領先', className: 'bg-red-500/10 text-red-500' };
  }
  if (heat >= 80 || changePct >= 5) {
    return { label: '強勢', className: 'bg-blue-500/10 text-blue-500' };
  }
  return { label: '較強', className: 'bg-cyan/10 text-cyan' };
};

const HOTSPOT_ICON_RULES: Array<{
  pattern: RegExp;
  icon: React.ComponentType<{ className?: string }>;
  className: string;
}> = [
  { pattern: /金|銀|銅|鋁|鉛|鋅|鉬|鈷|鎳|貴金屬|礦|有色/, icon: Pickaxe, className: 'bg-orange-500/10 text-orange-500' },
  { pattern: /黃金|珠寶/, icon: Gem, className: 'bg-amber-500/10 text-amber-500' },
  { pattern: /油|氣|能源|煤/, icon: Droplet, className: 'bg-yellow-700/10 text-yellow-700' },
  { pattern: /金融|券商|銀行|保險|資本/, icon: Landmark, className: 'bg-orange-500/10 text-orange-500' },
  { pattern: /航空|機場|航天|運輸/, icon: Plane, className: 'bg-blue-500/10 text-blue-500' },
  { pattern: /林業|農業|種植/, icon: Trees, className: 'bg-emerald-500/10 text-emerald-500' },
  { pattern: /醫療|診斷|衛生|醫藥/, icon: Stethoscope, className: 'bg-teal-500/10 text-teal-500' },
  { pattern: /食品|餐飲|酒/, icon: Utensils, className: 'bg-violet-500/10 text-violet-500' },
  { pattern: /工業|製造|修理|機械|設備/, icon: Wrench, className: 'bg-blue-500/10 text-blue-500' },
  { pattern: /租賃|地產|建築/, icon: Building2, className: 'bg-emerald-500/10 text-emerald-500' },
  { pattern: /電|芯片|算力|AI|機器人/, icon: Factory, className: 'bg-indigo-500/10 text-indigo-500' },
  { pattern: /保險|安全/, icon: Shield, className: 'bg-blue-500/10 text-blue-500' },
];

const getHotspotIcon = (topic: string) => {
  const match = HOTSPOT_ICON_RULES.find((rule) => rule.pattern.test(topic));
  return match || { icon: Activity, className: 'bg-cyan/10 text-cyan' };
};

const MiniSparkline: React.FC<{ score?: number | null; selected?: boolean }> = ({ score, selected }) => {
  const normalizedScore = Number.isFinite(Number(score)) ? Math.max(0, Math.min(100, Number(score))) : 65;
  const lift = Math.max(0, Math.min(16, normalizedScore / 7));
  const path = `M2 35 C12 ${32 - lift / 4}, 16 ${34 - lift / 2}, 24 ${28 - lift / 3} S38 ${29 - lift}, 46 ${23 - lift / 2} S62 ${24 - lift}, 72 ${16 - lift / 3} S86 ${15 - lift}, 94 ${7}`;
  return (
    <svg className="h-8 w-20" viewBox="0 0 96 40" aria-hidden="true">
      <path d={`${path} L94 40 L2 40 Z`} fill={selected ? 'rgba(249,115,22,0.14)' : 'rgba(59,130,246,0.12)'} />
      <path d={path} fill="none" stroke={selected ? '#f97316' : '#3b82f6'} strokeLinecap="round" strokeWidth="2" />
    </svg>
  );
};

const StockScreeningPage: React.FC = () => {
  const navigate = useNavigate();
  const [restoredTask] = useState<PersistedScreenTask | null>(() => readPersistedScreenTask());
  const [enabled, setEnabled] = useState(false);
  const [available, setAvailable] = useState(false);
  const [market, setMarket] = useState(restoredTask?.market || 'cn');
  const [strategy, setStrategy] = useState(restoredTask?.strategy || 'dual_low');
  const [strategies, setStrategies] = useState<AlphaSiftStrategy[]>([]);
  const [maxResults, setMaxResults] = useState(restoredTask?.maxResults || 3);
  const [candidates, setCandidates] = useState<AlphaSiftCandidate[]>([]);
  const [hotspots, setHotspots] = useState<AlphaSiftHotspot[]>([]);
  const [hotspotsUpdatedAt, setHotspotsUpdatedAt] = useState<string | null>(null);
  const [hotspotsExpanded, setHotspotsExpanded] = useState(false);
  const [selectedHotspotTopic, setSelectedHotspotTopic] = useState<string | null>(null);
  const selectedHotspotTopicRef = useRef<string | null>(null);
  const hotspotDetailRequestIdRef = useRef(0);
  const hotspotDetailsByTopicRef = useRef<Record<string, AlphaSiftHotspotDetail>>({});
  const [hotspotDetail, setHotspotDetail] = useState<AlphaSiftHotspotDetail | null>(null);
  const [loadingHotspotDetail, setLoadingHotspotDetail] = useState(false);
  const [hotspotDetailError, setHotspotDetailError] = useState('');
  const [loadingHotspots, setLoadingHotspots] = useState(false);
  const [hotspotError, setHotspotError] = useState('');
  const [screenMeta, setScreenMeta] = useState<AlphaSiftScreenResponse | null>(null);
  const [expandedCode, setExpandedCode] = useState<string | null>(null);
  const [loading, setLoading] = useState(Boolean(restoredTask?.taskId));
  const [enabling, setEnabling] = useState(false);
  const [loadingStrategies, setLoadingStrategies] = useState(false);
  const [error, setError] = useState('');
  const [strategyLoadError, setStrategyLoadError] = useState('');
  const [activeTaskId, setActiveTaskId] = useState<string | null>(restoredTask?.taskId ?? null);
  const [taskProgress, setTaskProgress] = useState(restoredTask?.taskId ? 10 : 0);
  const [taskMessage, setTaskMessage] = useState(restoredTask?.taskId ? '正在恢復選股任務狀態...' : '');

  const selectedStrategy = useMemo(() => strategies.find((item) => item.id === strategy), [strategies, strategy]);
  const selectedStrategyTitle = selectedStrategy?.name || selectedStrategy?.title || '自定義策略';
  const selectedStrategyTag = selectedStrategy?.category || selectedStrategy?.tag || selectedStrategy?.tags?.[0] || '自定義';
  const displayedStrategy = selectedStrategy ? selectedStrategyTitle : `自定義策略 (${strategy})`;
  const screenMessages = useMemo(() => getScreenMessages(screenMeta), [screenMeta]);
  const llmDegraded = screenMeta?.llmRanked === false;
  const alertMessages = llmDegraded
    ? screenMessages.length > 0
      ? screenMessages
      : ['LLM 重排未完成或未返回判斷，當前候選來自 AlphaSift 本地因子評分。']
    : screenMessages;
  const isScreeningEnabled = enabled && available;
  const statusText = isScreeningEnabled ? '選股已開啟' : '選股未開啟';

  const applyScreenResult = useCallback((result: AlphaSiftScreenResponse) => {
    const nextCandidates = result.candidates || [];
    setScreenMeta(result);
    setCandidates(nextCandidates);
    setExpandedCode(nextCandidates[0]?.code ?? null);
  }, []);

  const clearScreeningResults = () => {
    setCandidates([]);
    setScreenMeta(null);
    setExpandedCode(null);
  };

  const loadHotspotDetail = useCallback(async (topic: string, options: { refresh?: boolean } = {}) => {
    if (!topic) {
      return;
    }
    const cachedDetail = !options.refresh ? hotspotDetailsByTopicRef.current[topic] : null;
    if (cachedDetail) {
      setHotspotDetail(cachedDetail);
      setHotspotDetailError('');
      setLoadingHotspotDetail(false);
      return;
    }
    const requestId = hotspotDetailRequestIdRef.current + 1;
    hotspotDetailRequestIdRef.current = requestId;
    const isCurrentRequest = () => hotspotDetailRequestIdRef.current === requestId;
    const canApplyRequest = () => isCurrentRequest() && selectedHotspotTopicRef.current === topic;
    setLoadingHotspotDetail(true);
    setHotspotDetail((currentDetail) => (currentDetail?.topic === topic ? currentDetail : null));
    setHotspotDetailError('');
    try {
      const detail = await alphasiftApi.getHotspotDetail({ topic, provider: 'akshare', refresh: options.refresh ?? false });
      if (!canApplyRequest()) {
        return;
      }
      hotspotDetailsByTopicRef.current = {
        ...hotspotDetailsByTopicRef.current,
        [topic]: detail,
      };
      setHotspotDetail(detail);
    } catch (err) {
      if (!canApplyRequest()) {
        return;
      }
      setHotspotDetail(null);
      setHotspotDetailError(toApiErrorMessage(err, '熱點題材詳情加載失敗，請稍後重試。'));
    } finally {
      if (isCurrentRequest()) {
        setLoadingHotspotDetail(false);
      }
    }
  }, []);

  const loadStrategies = useCallback(async () => {
    setLoadingStrategies(true);
    try {
      setStrategyLoadError('');
      const result = await alphasiftApi.getStrategies();
      const loadedStrategies = result.strategies || [];
      setStrategies(loadedStrategies);
      if (loadedStrategies.length > 0) {
        setStrategy((currentStrategy) =>
          loadedStrategies.some((item) => item.id === currentStrategy) ? currentStrategy : loadedStrategies[0].id,
        );
      }
    } catch (err) {
      setStrategies([]);
      setStrategyLoadError(err instanceof Error ? err.message : 'AlphaSift 策略列表加載失敗');
    } finally {
      setLoadingStrategies(false);
    }
  }, []);

  const loadHotspots = useCallback(async (refresh = false) => {
    setLoadingHotspots(true);
    setHotspotError('');
    try {
      const result = await alphasiftApi.getHotspots({ provider: 'akshare', top: 12, refresh });
      const nextHotspots = result.hotspots || [];
      const nextDetails = result.details || {};
      hotspotDetailsByTopicRef.current = {
        ...hotspotDetailsByTopicRef.current,
        ...nextDetails,
      };
      const currentTopic = selectedHotspotTopicRef.current;
      const retainedTopic = Boolean(currentTopic && nextHotspots.some((item) => item.topic === currentTopic));
      const nextTopic = retainedTopic ? currentTopic : null;
      setHotspots(nextHotspots);
      setHotspotsUpdatedAt(result.cachedAt || (nextHotspots.length > 0 ? new Date().toISOString() : null));
      setSelectedHotspotTopic(nextTopic);
      selectedHotspotTopicRef.current = nextTopic;
      if (nextTopic && nextDetails[nextTopic]) {
        setHotspotDetail(nextDetails[nextTopic]);
        setLoadingHotspotDetail(false);
      } else if (retainedTopic && refresh && nextTopic) {
        void loadHotspotDetail(nextTopic, { refresh: true });
      } else if (!retainedTopic) {
        setHotspotDetail(null);
      }
      setHotspotDetailError('');
      if (nextHotspots.length === 0) {
        setHotspotError(formatHotspotEmptyMessage(result));
      }
    } catch (err) {
      setHotspotError(toApiErrorMessage(err, '熱點題材加載失敗，請稍後重試。'));
    } finally {
      setLoadingHotspots(false);
    }
  }, [loadHotspotDetail]);

  const handleHotspotSelect = useCallback((topic: string) => {
    selectedHotspotTopicRef.current = topic;
    setSelectedHotspotTopic(topic);
    const cachedDetail = hotspotDetailsByTopicRef.current[topic];
    if (cachedDetail) {
      setHotspotDetail(cachedDetail);
      setHotspotDetailError('');
      setLoadingHotspotDetail(false);
    } else {
      setHotspotDetail((currentDetail) => (currentDetail?.topic === topic ? currentDetail : null));
    }
  }, []);

  const toggleHotspotsExpanded = useCallback(() => {
    setHotspotsExpanded((expanded) => {
      const nextExpanded = !expanded;
      if (!nextExpanded) {
        selectedHotspotTopicRef.current = null;
        setSelectedHotspotTopic(null);
        setHotspotDetail(null);
        setHotspotDetailError('');
      }
      return nextExpanded;
    });
  }, []);

  const handleAnalyzeHotspotStock = useCallback((stock: AlphaSiftHotspotDetail['stocks'][number]) => {
    const stockCode = String(stock.code || '').trim();
    if (!stockCode) {
      return;
    }
    const stockName = String(stock.name || stockCode).trim();
    navigate('/', {
      state: {
        stockCode,
        stockName,
        autoAnalyze: true,
        selectionSource: 'alphasift_hotspot',
      },
    });
  }, [navigate]);

  useEffect(() => {
    selectedHotspotTopicRef.current = selectedHotspotTopic;
  }, [selectedHotspotTopic]);

  useEffect(() => {
    if (!selectedHotspotTopic) {
      return;
    }
    void loadHotspotDetail(selectedHotspotTopic);
  }, [loadHotspotDetail, selectedHotspotTopic]);

  useEffect(() => {
    let active = true;
    alphasiftApi
      .getStatus()
      .then((status) => {
        if (!active) {
          return;
        }
        setEnabled(status.enabled);
        setAvailable(status.available);
        if (status.enabled && status.available) {
          void loadStrategies();
          void loadHotspots(false);
        }
      })
      .catch(() => {
        if (active) {
          setEnabled(false);
          setAvailable(false);
        }
      });
    return () => {
      active = false;
    };
  }, [loadHotspots, loadStrategies]);

  useEffect(() => {
    if (!activeTaskId) {
      return undefined;
    }

    const pollingTaskId = activeTaskId;
    let active = true;
    let timer: ReturnType<typeof window.setTimeout> | undefined;

    function finishTask() {
      clearPersistedScreenTask();
      setActiveTaskId(null);
      setLoading(false);
    }

    function applyTaskStatus(task: AlphaSiftScreenTaskStatus) {
      const nextProgress = Number(task.progress ?? 0);
      setTaskProgress(Number.isFinite(nextProgress) ? nextProgress : 0);
      setTaskMessage(task.message || '');

      if (task.status === 'completed') {
        if (task.result) {
          applyScreenResult(task.result);
          setError('');
        } else {
          setError('選股任務已完成，但服務端未返回候選結果。');
          setCandidates([]);
          setScreenMeta(null);
        }
        finishTask();
        return;
      }

      if (task.status === 'failed') {
        setCandidates([]);
        setScreenMeta(null);
        setExpandedCode(null);
        setError(formatScreenTaskFailure(task.error || task.message));
        finishTask();
        return;
      }

      if (isRunningScreenTask(task.status)) {
        setLoading(true);
        timer = window.setTimeout(pollTask, SCREEN_TASK_POLL_INTERVAL_MS);
        return;
      }

      setError(`選股任務返回未知狀態：${task.status || 'unknown'}`);
      finishTask();
    }

    async function pollTask() {
      try {
        const task = await alphasiftApi.getScreenTask(pollingTaskId);
        if (!active) {
          return;
        }
        applyTaskStatus(task);
      } catch (err) {
        if (!active) {
          return;
        }
        const parsedError = getParsedApiError(err);
        if (isUnrecoverableScreenTaskError(parsedError)) {
          setError(formatParsedApiError(parsedError) || '選股任務不可恢復，請重新提交。');
          setCandidates([]);
          setScreenMeta(null);
          finishTask();
          return;
        }
        setError(formatRecoverableScreenTaskPollingError(parsedError));
        setLoading(true);
        timer = window.setTimeout(pollTask, SCREEN_TASK_POLL_INTERVAL_MS);
      }
    }

    void pollTask();

    return () => {
      active = false;
      if (timer) {
        window.clearTimeout(timer);
      }
    };
  }, [activeTaskId, applyScreenResult]);

  const handleEnable = async () => {
    setEnabling(true);
    setError('');
    try {
      await alphasiftApi.enable();
      setEnabled(true);
      setAvailable(true);
      await loadStrategies();
    } catch (err) {
      try {
        const status = await alphasiftApi.getStatus();
        setEnabled(status.enabled);
        setAvailable(status.available);
      } catch {
        setEnabled(false);
        setAvailable(false);
      }
      setError(err instanceof Error ? err.message : '開啟 AlphaSift 失敗');
    } finally {
      setEnabling(false);
    }
  };

  const handleStrategyChange = (nextStrategy: string) => {
    if (nextStrategy !== strategy) {
      clearScreeningResults();
    }
    setStrategy(nextStrategy);
  };

  const handleMarketChange = (nextMarket: string) => {
    if (nextMarket !== market) {
      clearScreeningResults();
    }
    setMarket(nextMarket);
  };

  const handleMaxResultsChange = (nextMaxResults: number) => {
    if (nextMaxResults !== maxResults) {
      clearScreeningResults();
    }
    setMaxResults(nextMaxResults);
  };

  const handleSubmit = async () => {
    setLoading(true);
    setError('');
    setScreenMeta(null);
    setTaskProgress(0);
    setTaskMessage('正在提交選股任務...');
    try {
      const task = await alphasiftApi.startScreen({ market, strategy, maxResults });
      persistScreenTask({
        taskId: task.taskId,
        market,
        strategy,
        maxResults,
      });
      setActiveTaskId(task.taskId);
      setTaskProgress(0);
      setTaskMessage(task.message || 'AlphaSift 選股任務已提交');
    } catch (err) {
      setCandidates([]);
      setLoading(false);
      setError(toApiErrorMessage(err, '選股任務提交失敗，請稍後重試。'));
    }
  };

  return (
    <AppPage className="max-w-6xl space-y-6 pb-12 pt-6">
      <div className="flex flex-col gap-4 sm:flex-row sm:items-start sm:justify-between">
        <div className="flex items-center gap-3">
          <span className="grid h-7 w-7 place-items-center rounded-full border-2 border-cyan text-cyan shadow-[0_0_24px_hsl(var(--primary)/0.18)]">
            <PlusCircle className="h-4 w-4" />
          </span>
          <div>
            <h1 className="text-2xl font-bold tracking-normal text-foreground">AlphaSift 選股</h1>
            <p className="mt-1 text-sm text-secondary-text">開啟後通過內置 AlphaSift 適配層生成候選股票，並補充 DSA 數據與新聞</p>
          </div>
        </div>

        <div className="inline-flex w-fit items-center gap-2 rounded-2xl border border-border/70 bg-card/80 px-4 py-2 text-sm shadow-soft-card">
          <span className={`h-2.5 w-2.5 rounded-full ${isScreeningEnabled ? 'bg-success' : 'bg-warning'}`} />
          <span className="font-medium text-secondary-text">{statusText}</span>
        </div>
      </div>

      {!enabled ? (
        <InlineAlert
          variant="info"
          title="AlphaSift 未開啟"
          message="點擊後寫入 ALPHASIFT_ENABLED=true；AlphaSift 已隨後端依賴安裝，若適配層缺失請先更新依賴或重建後端。"
          action={
            <Button size="sm" isLoading={enabling} loadingText="開啟中..." onClick={() => void handleEnable()}>
              開啟 AlphaSift
            </Button>
          }
        />
      ) : null}

      {enabled && !available ? (
        <InlineAlert
          variant="warning"
          title="AlphaSift 適配層不可用"
          message="適配層當前不可用，請先確認後端已安裝依賴並重啟服務，必要時執行 pip install -r requirements.txt 或使用設置頁/服務端 /install 接口進行修復安裝。"
        />
      ) : null}

      <InlineAlert
        variant="warning"
        title="實驗功能與風險提示"
        message="AlphaSift 選股仍處於實驗性質，結果僅用於研究和輔助判斷，不構成投資建議；市場有風險，交易決策和損益由使用者自行承擔。"
      />

      {loading ? (
        <InlineAlert
          variant="info"
          title="選股任務運行中"
          message={`${taskMessage || '正在執行 AlphaSift 選股'}。任務 ID：${activeTaskId ? activeTaskId.slice(0, 12) : '-'}`}
        />
      ) : null}

      {error ? <InlineAlert variant="danger" title="調用失敗" message={error} /> : null}

      <section className="rounded-2xl border border-border/80 bg-card/95 p-4 shadow-soft-card">
        <div className="mb-4 flex flex-col gap-3 lg:flex-row lg:items-start lg:justify-between">
          <div className="flex items-start gap-3">
            <span className="grid h-10 w-10 shrink-0 place-items-center rounded-full bg-orange-500/10 text-orange-500 shadow-[0_10px_30px_rgba(249,115,22,0.16)]">
              <Flame className="h-5 w-5" />
            </span>
            <div>
              <h2 className="text-lg font-bold tracking-normal text-foreground">熱點題材</h2>
              <p className="mt-1 text-xs leading-5 text-secondary-text">
                來自 AlphaSift 最新 hotspot 能力；capital_heat、balanced_alpha 等策略會把 theme_heat 納入評分。
              </p>
            </div>
          </div>
          <div className="flex flex-col items-start gap-2 lg:items-end">
            <div className="flex flex-wrap items-center gap-2">
              <Button
                size="sm"
                variant="secondary"
                disabled={!isScreeningEnabled}
                onClick={toggleHotspotsExpanded}
              >
                <Bookmark className="h-4 w-4" />
                {hotspotsExpanded ? '收起熱點題材' : `展開熱點題材${hotspots.length ? `（${hotspots.length}）` : ''}`}
                <ChevronDown className={`h-4 w-4 transition-transform ${hotspotsExpanded ? 'rotate-180' : ''}`} />
              </Button>
              {hotspotsExpanded ? (
              <Button
                size="sm"
                variant="secondary"
                isLoading={loadingHotspots}
                loadingText="刷新中..."
                disabled={!isScreeningEnabled || loadingHotspots}
                onClick={() => void loadHotspots(true)}
              >
                <RefreshCw className="h-4 w-4" />
                刷新熱點題材
              </Button>
              ) : null}
            </div>
            <p className="text-xs text-secondary-text">更新時間：{formatHotspotUpdatedAt(hotspotsUpdatedAt)}</p>
          </div>
        </div>

        {hotspotError ? (
          <p className="mb-3 rounded-xl border border-warning/30 bg-warning/10 px-3 py-2 text-xs text-warning">
            {hotspotError}
          </p>
        ) : null}

        {!hotspotsExpanded ? (
          <div className="flex flex-col gap-2 rounded-xl border border-border/70 bg-surface/70 px-4 py-3 text-sm text-secondary-text sm:flex-row sm:items-center sm:justify-between">
            <span>
              {hotspots.length > 0
                ? `已緩存 ${hotspots.length} 個熱點題材，展開後可查看熱度、階段和發酵路線。`
                : '熱點題材默認摺疊；展開後可讀取緩存，點擊刷新才拉取實時數據。'}
            </span>
            <span className="text-xs">實時詳情會在選擇具體題材後加載</span>
          </div>
        ) : hotspots.length === 0 ? (
          <div className="rounded-xl border border-dashed border-border bg-surface/70 px-4 py-6 text-sm text-secondary-text">
            點擊刷新後會拉取熱點概念/行業排行、熱度分、生命週期階段和活躍龍頭。
          </div>
        ) : (
          <div className="grid gap-3 md:grid-cols-2 xl:grid-cols-3 2xl:grid-cols-4">
            {hotspots.map((item, index) => {
              const selected = selectedHotspotTopic === item.topic;
              const strength = getHotspotStrength(item, index);
              const iconMeta = getHotspotIcon(item.name || item.topic);
              const Icon = iconMeta.icon;
              return (
              <button
                key={`${item.topic}-${item.rank ?? ''}`}
                className={`group relative min-h-[116px] overflow-hidden rounded-xl border px-3 py-3 text-left transition-all ${
                  selected
                    ? 'border-orange-400 bg-gradient-to-br from-orange-500/10 via-card to-card shadow-[0_0_0_1px_rgba(249,115,22,0.16),0_18px_44px_rgba(249,115,22,0.14)]'
                    : 'border-border/80 bg-card hover:-translate-y-0.5 hover:border-orange-300/70 hover:shadow-soft-card'
                }`}
                type="button"
                onClick={() => handleHotspotSelect(item.topic)}
              >
                <div className="flex items-start justify-between gap-3">
                  <div className="flex min-w-0 items-start gap-3">
                    <span
                      className={`grid h-6 w-6 shrink-0 place-items-center rounded-full text-xs font-bold ${
                        index < 3 ? 'bg-orange-500 text-white shadow-[0_8px_24px_rgba(249,115,22,0.24)]' : 'bg-surface text-secondary-text'
                      }`}
                    >
                      {index + 1}
                    </span>
                    <span className={`grid h-9 w-9 shrink-0 place-items-center rounded-full ${iconMeta.className}`}>
                      <Icon className="h-5 w-5" />
                    </span>
                    <div className="min-w-0">
                      <p className="truncate text-sm font-bold text-foreground">{item.name || item.topic}</p>
                      <span className={`mt-1 inline-flex rounded-md px-1.5 py-0.5 text-[11px] font-semibold ${strength.className}`}>
                        {strength.label}
                      </span>
                    </div>
                  </div>
                  <span className="shrink-0 text-2xl font-black leading-none text-orange-500">
                    {formatNumber(item.heatScore, 0)}
                  </span>
                </div>
                <div className="mt-4 grid max-w-[72%] gap-1 text-[11px] text-secondary-text">
                  <span>漲跌幅 <strong className="font-semibold text-foreground">{formatHotspotMetric(item.changePct)}%</strong></span>
                  <span>趨勢 <strong className="font-semibold text-foreground">{formatHotspotMetric(item.trendScore)}</strong> · 持續 <strong className="font-semibold text-foreground">{formatHotspotMetric(item.persistenceScore)}</strong></span>
                  <span>{getHotspotSampleText(item)} · 龍頭 {getHotspotLeadersText(item)}</span>
                </div>
                <div className="absolute bottom-3 right-3 opacity-95 transition-transform group-hover:scale-105">
                  <MiniSparkline score={item.heatScore} selected={selected} />
                </div>
              </button>
              );
            })}
          </div>
        )}

        {hotspotsExpanded && selectedHotspotTopic ? (
          <div className="mt-4 rounded-xl border border-border/80 bg-surface/80 p-4">
            <div className="mb-3 flex flex-col gap-2 sm:flex-row sm:items-center sm:justify-between">
              <div>
                <h3 className="text-sm font-semibold text-foreground">
                  {hotspotDetail?.name || selectedHotspotTopic}
                </h3>
                <p className="mt-1 text-xs leading-5 text-secondary-text">
                  {loadingHotspotDetail ? '正在讀取發酵路線與概念股...' : hotspotDetail?.summary || '點擊題材查看發酵路線與概念股。'}
                </p>
                {hotspotDetail?.canonicalTopic && hotspotDetail.canonicalTopic !== selectedHotspotTopic ? (
                  <p className="mt-1 text-[11px] text-secondary-text">標準題材：{hotspotDetail.canonicalTopic}</p>
                ) : null}
              </div>
              <div className="flex flex-wrap items-center gap-2">
                {hotspotDetail?.qualityStatus ? (
                  <span className="w-fit rounded-full bg-warning/10 px-3 py-1 text-xs font-semibold text-warning">
                    質量 {hotspotDetail.qualityStatus}
                  </span>
                ) : null}
                {hotspotDetail?.fallbackUsed || hotspotDetail?.stale ? (
                  <span className="w-fit rounded-full bg-warning/10 px-3 py-1 text-xs font-semibold text-warning">
                    {hotspotDetail.staleAgeHours != null ? `緩存回退 ${formatNumber(hotspotDetail.staleAgeHours, 1)}h` : '緩存回退'}
                  </span>
                ) : null}
                {hotspotDetail?.stockCount != null ? (
                  <span className="w-fit rounded-full bg-orange-500/10 px-3 py-1 text-xs font-semibold text-orange-500">
                    概念股 {hotspotDetail.stockCount}
                  </span>
                ) : null}
              </div>
            </div>

            {hotspotDetailError ? (
              <p className="mb-3 rounded-xl border border-warning/30 bg-warning/10 px-3 py-2 text-xs text-warning">
                {hotspotDetailError}
              </p>
            ) : null}

            {hotspotDetail && ((hotspotDetail.missingFields || []).length > 0 || (hotspotDetail.sourceErrors || []).length > 0) ? (
              <details className="mb-3 rounded-xl border border-warning/30 bg-warning/10 px-3 py-2 text-xs text-warning">
                <summary className="cursor-pointer font-semibold">詳情數據已降級，展開查看原因</summary>
                <div className="mt-2 space-y-1 leading-5">
                  {(hotspotDetail.missingFields || []).length > 0 ? (
                    <p>缺失字段：{(hotspotDetail.missingFields || []).join('、')}</p>
                  ) : null}
                  {(hotspotDetail.sourceErrors || []).slice(0, 4).map((message, index) => (
                    <p key={`${message}-${index}`}>{message}</p>
                  ))}
                </div>
              </details>
            ) : null}

            {hotspotDetail ? (
              <div className="grid gap-4 lg:grid-cols-[1fr_1.3fr]">
                <div>
                  <p className="mb-3 flex items-center gap-1.5 text-xs font-semibold text-secondary-text">
                    <Clock3 className="h-3.5 w-3.5 text-orange-500" />
                    發酵時間線
                  </p>
                  <div className="relative space-y-0 pl-4 before:absolute before:bottom-3 before:left-[5px] before:top-2 before:w-px before:bg-border">
                    {getHotspotRouteItems(hotspotDetail).map((item, index) => (
                      <div key={`${item.title}-${index}`} className="relative pb-4 last:pb-0">
                        <span className="absolute -left-4 top-1 h-2.5 w-2.5 rounded-full border border-orange-400 bg-card" />
                        <div className="rounded-lg border border-border/70 bg-card/80 p-3">
                          <p className="text-[11px] font-semibold text-orange-500">{getRouteTimeLabel(item)}</p>
                          <p className="mt-1 text-xs font-semibold text-foreground">{item.title}</p>
                          <p className="mt-1 text-xs leading-5 text-secondary-text">{item.description}</p>
                          {item.source ? <p className="mt-2 text-[11px] text-secondary-text">來源 {item.source}</p> : null}
                        </div>
                      </div>
                    ))}
                  </div>
                </div>
                <div>
                  <p className="mb-2 text-xs font-semibold text-secondary-text">概念股</p>
                  <div className="grid gap-2 sm:grid-cols-2">
                    {(hotspotDetail.stocks || []).slice(0, 10).map((stock) => (
                      <div key={`${stock.code || stock.name}`} className="rounded-lg border border-border/70 bg-card/80 p-3">
                        <div className="flex items-start justify-between gap-2">
                          <div className="min-w-0">
                            <p className="truncate text-xs font-semibold text-foreground">{stock.name || stock.code || '-'}</p>
                            <p className="mt-1 text-[11px] text-secondary-text">{stock.code || '-'}</p>
                          </div>
                          <div className="flex shrink-0 items-center gap-1">
                            <span className="rounded-full bg-cyan/10 px-2 py-1 text-[11px] font-semibold text-cyan">
                              {stock.role || '概念股'}
                            </span>
                            {stock.code ? (
                              <button
                                type="button"
                                aria-label={`分析 ${stock.name || stock.code}`}
                                className="inline-flex h-7 items-center gap-1 rounded-full border border-cyan/30 bg-cyan/10 px-2 text-[11px] font-semibold text-cyan transition-colors hover:border-cyan hover:bg-cyan/15 hover:text-foreground"
                                onClick={() => handleAnalyzeHotspotStock(stock)}
                              >
                                <Play className="h-3 w-3" />
                                分析
                              </button>
                            ) : null}
                          </div>
                        </div>
                        <p className="mt-2 text-[11px] text-secondary-text">
                          漲跌幅 {formatStockChangeText(stock.changePct)} · 熱度 {formatNumber(stock.hotStockScore, 0)}
                        </p>
                        {stock.source || stock.sourceConfidence != null || stock.fallbackUsed ? (
                          <p className="mt-1 text-[11px] text-secondary-text">
                            來源 {stock.source || '-'}
                            {stock.sourceConfidence != null ? ` · 置信 ${formatPercent(stock.sourceConfidence)}` : ''}
                            {stock.fallbackUsed ? ' · 回退' : ''}
                          </p>
                        ) : null}
                      </div>
                    ))}
                  </div>
                </div>
              </div>
            ) : null}
          </div>
        ) : null}
      </section>

      <section className="rounded-2xl border border-cyan/35 bg-card/95 p-4 shadow-soft-card">
        <div className="mb-4 flex items-center justify-between gap-3">
          <div>
            <h2 className="text-sm font-semibold text-foreground">選擇策略</h2>
            <p className="mt-1 text-xs text-secondary-text">策略來自 AlphaSift；DSA 會對候選補充行情、基本面和新聞上下文。</p>
          </div>
          <span className="rounded-full border border-cyan/30 bg-cyan/10 px-3 py-1 text-xs font-semibold text-cyan">
            {selectedStrategyTag}
          </span>
        </div>

        <div className="grid gap-2 md:grid-cols-2 xl:grid-cols-4">
          {loadingStrategies ? (
            <div className="rounded-xl border border-dashed border-border bg-surface/70 p-4 text-sm text-secondary-text">
              正在讀取可用策略...
            </div>
          ) : strategies.length === 0 ? (
            <div className="rounded-xl border border-dashed border-border bg-surface/70 p-4 text-sm text-secondary-text">
              {strategyLoadError || 'AlphaSift 策略列表暫未載入，可在下方手動輸入策略參數。'}
            </div>
          ) : (
            strategies.map((item) => {
              const selected = item.id === strategy;
              return (
                <button
                  key={item.id}
                  className={`min-h-28 rounded-xl border p-4 text-left transition-all ${
                    selected
                      ? 'border-cyan bg-cyan/10 shadow-[0_0_0_1px_hsl(var(--primary)/0.15),0_16px_36px_hsl(var(--primary)/0.12)]'
                      : 'border-border/80 bg-surface/70 hover:border-cyan/45 hover:bg-hover/70'
                  }`}
                  type="button"
                  disabled={loading}
                  onClick={() => handleStrategyChange(item.id)}
                >
                  <span className="text-base font-semibold text-foreground">{item.name || item.title || item.id}</span>
                  <span className="mt-2 block text-sm leading-6 text-secondary-text">{item.description || item.id}</span>
                  <span className="mt-3 inline-flex text-xs font-semibold text-cyan">
                    {item.category || item.tag || item.tags?.[0] || item.id}
                  </span>
                </button>
              );
            })
          )}
        </div>
      </section>

      <section className="rounded-2xl border border-border bg-card/95 p-4 shadow-soft-card">
        <div className="mb-4 flex items-center gap-2 text-sm font-semibold text-foreground">
          <SlidersHorizontal className="h-4 w-4 text-cyan" />
          參數設置
        </div>

        <div className="grid gap-4 lg:grid-cols-[1fr_1.2fr_180px_auto] lg:items-end">
          <label className="space-y-2 text-xs font-medium text-secondary-text">
            市場
            <select
              className="h-11 w-full rounded-xl border border-border bg-surface px-3 text-sm text-foreground outline-none transition-colors focus:border-cyan"
              value={market}
              disabled={loading}
              onChange={(event) => handleMarketChange(event.target.value)}
            >
              {MARKETS.map((item) => (
                <option key={item.id} value={item.id}>
                  {item.label}
                </option>
              ))}
            </select>
          </label>

          <label className="space-y-2 text-xs font-medium text-secondary-text">
            策略參數
            <input
              className="h-11 w-full rounded-xl border border-border bg-surface px-3 text-sm text-foreground outline-none transition-colors focus:border-cyan"
              value={strategy}
              disabled={loading}
              onChange={(event) => handleStrategyChange(event.target.value)}
            />
          </label>

          <label className="space-y-2 text-xs font-medium text-secondary-text">
            返回數量
            <input
              className="h-11 w-full rounded-xl border border-border bg-surface px-3 text-sm text-foreground outline-none transition-colors focus:border-cyan"
              type="number"
              min={1}
              max={100}
              value={maxResults}
              disabled={loading}
              onChange={(event) => handleMaxResultsChange(Number(event.target.value))}
            />
          </label>

          <Button
            className="h-11 min-w-40"
            isLoading={loading}
            loadingText="篩選中..."
            disabled={!isScreeningEnabled || loading}
            onClick={() => void handleSubmit()}
          >
            <Play className="h-4 w-4" />
            運行選股
          </Button>
        </div>
      </section>

      <section className="rounded-2xl border border-border bg-card/95 p-4 shadow-soft-card">
        <div className="flex flex-col gap-4 sm:flex-row sm:items-center sm:justify-between">
          <div className="flex items-center gap-3">
            <span
              className={`grid h-7 w-7 place-items-center rounded-full ${
                candidates.length > 0 ? 'text-success' : isScreeningEnabled ? 'text-cyan' : 'text-warning'
              }`}
            >
              {candidates.length > 0 ? <CheckCircle2 className="h-5 w-5" /> : <CircleAlert className="h-5 w-5" />}
            </span>
            <div>
              <h2 className="text-sm font-semibold text-foreground">
                {loading ? '選股運行中' : candidates.length > 0 ? '選股完成' : isScreeningEnabled ? '等待運行' : '等待開啟'}
              </h2>
              <p className="mt-1 text-xs text-secondary-text">
                {loading
                  ? `${taskMessage || '正在執行 AlphaSift 選股'} · ${taskProgress}%`
                  : `當前策略：${displayedStrategy} · ${MARKETS.find((item) => item.id === market)?.label}`}
              </p>
            </div>
          </div>
          <div className="grid gap-1 text-xs text-secondary-text sm:text-right">
            <span>任務：{activeTaskId ? activeTaskId.slice(0, 12) : '-'}</span>
            <span>Run ID：{screenMeta?.runId || '-'}</span>
            <span>
              快照 {screenMeta?.snapshotCount ?? '-'} · 過濾後 {screenMeta?.afterFilterCount ?? '-'} · 候選 {screenMeta?.candidateCount ?? candidates.length}
            </span>
            <span>
              LLM：{screenMeta?.llmRanked ? '已重排' : screenMeta ? '未重排' : '-'}
              {screenMeta?.llmCoverage != null ? ` · 覆蓋 ${formatPercent(screenMeta.llmCoverage)}` : ''}
            </span>
            <span>
              DSA增強：{screenMeta?.dsaEnrichment?.enrichedCount ?? '-'} / {screenMeta?.dsaEnrichment?.requestedCount ?? '-'}
            </span>
          </div>
        </div>
      </section>

      {screenMeta && alertMessages.length > 0 ? (
        <InlineAlert
          variant={llmDegraded ? 'warning' : 'info'}
          title={llmDegraded ? 'LLM 已降級' : 'AlphaSift 提示'}
          message={<ScreenAlertMessage messages={alertMessages} />}
        />
      ) : null}

      <section className="rounded-2xl border border-border bg-card/95 p-4 shadow-soft-card">
        <div className="mb-5 flex flex-col gap-2 sm:flex-row sm:items-end sm:justify-between">
          <div>
            <h2 className="text-base font-semibold text-foreground">選股結果</h2>
            <p className="mt-2 max-w-3xl text-sm leading-6 text-secondary-text">
              AlphaSift 返回候選後，DSA 會對前幾名補充行情、基本面、新聞和輔助摘要。
            </p>
          </div>
          <div className="flex items-center gap-2 rounded-full border border-border bg-surface px-3 py-2 text-xs text-secondary-text">
            <Search className="h-4 w-4 text-cyan" />
            {candidates.length} 條候選
          </div>
        </div>

        {candidates.length === 0 ? (
          <div className="rounded-xl border border-dashed border-border bg-surface/70 px-5 py-10 text-center">
            <p className="text-sm font-medium text-foreground">暫無結果</p>
            <p className="mt-2 text-sm text-secondary-text">開啟 AlphaSift 後點擊“運行選股”生成候選列表。</p>
          </div>
        ) : (
          <div className="overflow-hidden rounded-xl border border-border">
            <table className="w-full min-w-[860px] border-collapse text-sm">
              <thead className="bg-surface text-left text-xs text-secondary-text">
                <tr>
                  <th className="w-14 px-4 py-3 font-semibold">#</th>
                  <th className="px-4 py-3 font-semibold">代碼</th>
                  <th className="px-4 py-3 font-semibold">名稱</th>
                  <th className="px-4 py-3 font-semibold">行業</th>
                  <th className="px-4 py-3 font-semibold">價格</th>
                  <th className="px-4 py-3 font-semibold">漲跌幅</th>
                  <th className="px-4 py-3 font-semibold">評分</th>
                  <th className="px-4 py-3 font-semibold">LLM</th>
                  <th className="px-4 py-3 font-semibold">風險</th>
                  <th className="px-4 py-3 font-semibold">詳情</th>
                </tr>
              </thead>
              <tbody>
                {candidates.map((item) => {
                  const expanded = expandedCode === item.code;
                  const factors = getFactorEntries(item);
                  const llmInsightAvailable = hasLlmInsight(item);
                  const llmFallbackText =
                    llmDegraded && !llmInsightAvailable
                      ? '本次 LLM 重排失敗或未返回判斷，當前展示的是本地因子評分結果。'
                      : '暫無 LLM 判斷';
                  const dsaWarnings = item.dsaContext?.warnings || [];
                  const dsaNews = item.dsaNews || [];
                  return (
                    <Fragment key={`${item.rank}-${item.code}`}>
                      <tr className="border-t border-border align-top transition-colors hover:bg-hover/50">
                        <td className="px-4 py-3 text-secondary-text">{item.rank}</td>
                        <td className="px-4 py-3 font-mono font-semibold text-foreground">{item.code}</td>
                        <td className="px-4 py-3 font-semibold text-foreground">{item.name || '-'}</td>
                        <td className="px-4 py-3 text-secondary-text">{item.industry || '-'}</td>
                        <td className="px-4 py-3 text-secondary-text">{formatNumber(item.price)}</td>
                        <td className="px-4 py-3 text-secondary-text">{formatNumber(item.changePct)}%</td>
                        <td className="px-4 py-3 font-bold text-cyan">{formatScore(item.score)}</td>
                        <td className="px-4 py-3 text-secondary-text">{llmDegraded ? '未重排' : formatScore(item.llmScore)}</td>
                        <td className="px-4 py-3">
                          <span className="rounded-lg bg-success/10 px-2.5 py-1 text-xs font-semibold text-success">
                            {item.riskLevel || 'unknown'}
                          </span>
                        </td>
                        <td className="px-4 py-3">
                          <button
                            className="text-sm font-semibold text-cyan transition-colors hover:text-foreground"
                            type="button"
                            onClick={() => setExpandedCode(expanded ? null : item.code)}
                          >
                            {expanded ? '收起' : '展開查看'}
                          </button>
                        </td>
                      </tr>
                      {expanded ? (
                        <tr className="border-t border-border bg-surface/45">
                          <td colSpan={10} className="px-4 py-4">
                            <div className="grid gap-4 lg:grid-cols-[1.1fr_1fr]">
                              <div className="space-y-3">
                                <div>
                                  <p className="text-xs font-semibold text-secondary-text">摘要</p>
                                  <p className="mt-1 text-sm leading-6 text-foreground">{getCandidateReason(item)}</p>
                                </div>
                                <div>
                                  <p className="text-xs font-semibold text-secondary-text">操作信號</p>
                                  <p className="mt-1 text-sm text-foreground">{getSignal(item)}</p>
                                </div>
                                {item.dsaAnalysisSummary ? (
                                  <div>
                                    <p className="text-xs font-semibold text-secondary-text">DSA 增強摘要</p>
                                    <p className="mt-1 text-sm leading-6 text-foreground">{item.dsaAnalysisSummary}</p>
                                  </div>
                                ) : null}
                                <div>
                                  <p className="text-xs font-semibold text-secondary-text">LLM 判斷</p>
                                  <p className="mt-1 text-sm leading-6 text-foreground">
                                    {item.llmThesis || llmFallbackText}
                                  </p>
                                  {llmInsightAvailable ? (
                                    <p className="mt-1 text-xs text-secondary-text">
                                      板塊 {item.llmSector || '-'} · 主題 {item.llmTheme || '-'} · 置信度 {formatPercent(item.llmConfidence)}
                                    </p>
                                  ) : (
                                    <p className="mt-1 text-xs text-secondary-text">LLM 元數據未返回</p>
                                  )}
                                </div>
                                <div>
                                  <p className="text-xs font-semibold text-secondary-text">風險標籤</p>
                                  <p className="mt-1 text-sm text-foreground">
                                    {[...(item.riskFlags || []), ...(item.llmRisks || [])].length
                                      ? [...(item.riskFlags || []), ...(item.llmRisks || [])].join('，')
                                      : '無'}
                                  </p>
                                </div>
                              </div>
                              <div className="space-y-3">
                                <div>
                                  <p className="text-xs font-semibold text-secondary-text">主要因子</p>
                                  <div className="mt-2 grid grid-cols-2 gap-2">
                                    {factors.length > 0 ? (
                                      factors.map(([key, value]) => (
                                        <div key={key} className="rounded-lg border border-border bg-card px-3 py-2">
                                          <span className="block text-xs text-secondary-text">{key}</span>
                                          <span className="text-sm font-semibold text-foreground">{formatNumber(value)}</span>
                                        </div>
                                      ))
                                    ) : (
                                      <span className="text-sm text-secondary-text">無因子明細</span>
                                    )}
                                  </div>
                                </div>
                                <div>
                                  <p className="text-xs font-semibold text-secondary-text">成交額</p>
                                  <p className="mt-1 text-sm text-foreground">{formatAmount(item.amount)}</p>
                                </div>
                                <div>
                                  <p className="text-xs font-semibold text-secondary-text">LLM 關注項</p>
                                  <p className="mt-1 text-sm text-foreground">
                                    {item.llmWatchItems?.length ? item.llmWatchItems.join('，') : llmDegraded ? '未返回（LLM 已降級）' : '無'}
                                  </p>
                                </div>
                                <div>
                                  <p className="text-xs font-semibold text-secondary-text">催化因素</p>
                                  <p className="mt-1 text-sm text-foreground">
                                    {item.llmCatalysts?.length ? item.llmCatalysts.join('，') : llmDegraded ? '未返回（LLM 已降級）' : '無'}
                                  </p>
                                </div>
                                <div>
                                  <p className="text-xs font-semibold text-secondary-text">DSA 新聞</p>
                                  {dsaNews.length > 0 ? (
                                    <ul className="mt-1 space-y-1 text-sm text-foreground">
                                      {dsaNews.slice(0, 3).map((newsItem, newsIndex) => (
                                        <li key={`${item.code}-dsa-news-${newsIndex}`}>
                                          {newsItem.title || newsItem.snippet || '-'}
                                        </li>
                                      ))}
                                    </ul>
                                  ) : (
                                    <p className="mt-1 text-sm text-secondary-text">無</p>
                                  )}
                                </div>
                                {dsaWarnings.length > 0 ? (
                                  <div>
                                    <p className="text-xs font-semibold text-secondary-text">DSA 增強提示</p>
                                    <p className="mt-1 text-sm text-secondary-text">{dsaWarnings.join('，')}</p>
                                  </div>
                                ) : null}
                              </div>
                            </div>
                          </td>
                        </tr>
                      ) : null}
                    </Fragment>
                  );
                })}
              </tbody>
            </table>
          </div>
        )}
      </section>
    </AppPage>
  );
};

export default StockScreeningPage;
