import type { DecisionAction } from '../types/analysis';

export type DecisionActionTone = 'success' | 'warning' | 'danger' | 'default';
export type DecisionActionLabelMap = Record<DecisionAction, string>;
export type DecisionActionLabelTextKey =
  | 'history.actionBuy'
  | 'history.actionAdd'
  | 'history.actionHold'
  | 'history.actionReduce'
  | 'history.actionSell'
  | 'history.actionWatch'
  | 'history.actionAvoid'
  | 'history.actionAlert';
export type DecisionActionLabelTranslator = (key: DecisionActionLabelTextKey) => string;

export const DEFAULT_DECISION_ACTION_LABELS: DecisionActionLabelMap = {
  buy: '買入',
  add: '加倉',
  hold: '持有',
  reduce: '減倉',
  sell: '賣出',
  watch: '觀望',
  avoid: '迴避',
  alert: '預警',
};

const resolveActionLabels = (labels?: Partial<DecisionActionLabelMap>): DecisionActionLabelMap => ({
  ...DEFAULT_DECISION_ACTION_LABELS,
  ...labels,
});

export const buildDecisionActionLabelMap = (
  t: DecisionActionLabelTranslator,
): DecisionActionLabelMap => ({
  buy: t('history.actionBuy'),
  add: t('history.actionAdd'),
  hold: t('history.actionHold'),
  reduce: t('history.actionReduce'),
  sell: t('history.actionSell'),
  watch: t('history.actionWatch'),
  avoid: t('history.actionAvoid'),
  alert: t('history.actionAlert'),
});

const toneForAction = (action: DecisionAction): DecisionActionTone => {
  if (action === 'buy' || action === 'add' || action === 'hold') return 'success';
  if (action === 'sell' || action === 'reduce') return 'danger';
  return 'warning';
};

const includesAny = (value: string, phrases: readonly string[]): boolean =>
  phrases.some((phrase) => value.includes(phrase));

const normalizeEnglishAdvice = (value: string): string =>
  value.toLowerCase().replace(/[_-]/g, ' ');

const maskEnglishFinancialCompounds = (value: string): string =>
  value
    .replace(/(^|[^a-z0-9_])buy\s*back(?=$|[^a-z0-9_])/g, '$1financialcompound')
    .replace(/(^|[^a-z0-9_])sell\s*off(?=$|[^a-z0-9_])/g, '$1financialcompound');

const matchesEnglishTerm = (value: string, terms: readonly string[]): boolean =>
  terms.some((term) => new RegExp(`(^|[^a-z0-9_])${term}(?=$|[^a-z0-9_])`).test(value));

const matchesEnglishNegatedAction = (value: string, terms: readonly string[]): boolean => {
  const negationPrefix = String.raw`(?:not\s+(?:a\s+|an\s+|to\s+)?|no\s+(?:need\s+to\s+)?|need\s+not\s+|cannot\s+|can't\s+|cant\s+|do\s+not\s+|don't\s+|dont\s+)`;
  return terms.some((term) =>
    new RegExp(`(^|[^a-z0-9_])${negationPrefix}${term}(?=$|[^a-z0-9_])`).test(value),
  );
};

const hasEnglishAvoidedHoldAction = (value: string): boolean => {
  const terms = String.raw`(?:adding|accumulating|selling|reducing|trimming)`;
  return new RegExp(`(^|[^a-z0-9_])avoid\\s+${terms}(?=$|[^a-z0-9_])`).test(value);
};

const hasEnglishDeferredAction = (value: string): boolean => {
  const terms = String.raw`(?:buy|add|accumulate|sell|reduce|trim)`;
  return (
    new RegExp(`(^|[^a-z0-9_])wait(?:ing)?\\s+to\\s+${terms}(?=$|[^a-z0-9_])`).test(value) ||
    new RegExp(`(^|[^a-z0-9_])waiting\\s+(?:for|until)\\b.*?${terms}(?=$|[^a-z0-9_])`).test(value)
  );
};

export const getLegacyDecisionActionLabel = (
  advice?: string | null,
  labels?: Partial<DecisionActionLabelMap>,
): string | null => {
  const action = getLegacyDecisionAction(advice);
  if (!action) return null;
  return resolveActionLabels(labels)[action];
};

export const getLegacyDecisionAction = (advice?: string | null): DecisionAction | null => {
  const normalized = advice?.trim();
  if (!normalized) return null;
  const lower = maskEnglishFinancialCompounds(normalizeEnglishAdvice(normalized));

  if (hasEnglishDeferredAction(lower)) {
    return null;
  }

  if (
    includesAny(normalized, [
      '暫不買入',
      '不要買入',
      '不宜買入',
      '先不買入',
      '無需買入',
      '無須買入',
      '不建議建倉',
      '暫不建倉',
      '不要建倉',
      '不宜建倉',
      '先不建倉',
      '無需建倉',
      '無須建倉',
      '不建議佈局',
      '暫不佈局',
      '不要佈局',
      '不宜佈局',
      '先不佈局',
      '無需佈局',
      '無須佈局',
    ]) ||
    matchesEnglishNegatedAction(lower, ['buy'])
  ) {
    return 'avoid';
  }
  if (
    includesAny(normalized, [
      '不建議加倉',
      '無需加倉',
      '無須加倉',
      '不要加倉',
      '不宜加倉',
      '暫不加倉',
      '不建議增持',
      '無需增持',
      '無須增持',
      '不要增持',
      '不宜增持',
      '暫不增持',
      '不建議賣出',
      '無需賣出',
      '無須賣出',
      '不要賣出',
      '不宜賣出',
      '暫不賣出',
      '不建議減倉',
      '無需減倉',
      '無須減倉',
      '不要減倉',
      '不宜減倉',
      '暫不減倉',
      '不建議清倉',
      '無需清倉',
      '無須清倉',
      '不要清倉',
      '不宜清倉',
      '暫不清倉',
    ]) ||
    hasEnglishAvoidedHoldAction(lower) ||
    matchesEnglishNegatedAction(lower, ['add', 'accumulate', 'sell', 'reduce', 'trim'])
  ) {
    return 'hold';
  }
  const guardMatches = new Set<DecisionAction>();
  if (
    normalized.includes('不建議買入') ||
    normalized.includes('避免買入') ||
    normalized.includes('迴避') ||
    normalized.includes('規避') ||
    matchesEnglishTerm(lower, ['avoid'])
  ) {
    guardMatches.add('avoid');
  }
  if (
    normalized.includes('風險預警') ||
    normalized.includes('觸發告警') ||
    normalized.includes('警惕') ||
    lower.includes('risk alert') ||
    matchesEnglishTerm(lower, ['alert'])
  ) {
    guardMatches.add('alert');
  }
  if (guardMatches.size === 1) {
    return Array.from(guardMatches)[0];
  }
  if (guardMatches.size > 1) {
    return null;
  }

  const matches = new Set<DecisionAction>();
  if (normalized.includes('加倉') || normalized.includes('增持') || matchesEnglishTerm(lower, ['add', 'accumulate'])) {
    matches.add('add');
  }
  if (normalized.includes('減倉') || matchesEnglishTerm(lower, ['reduce', 'trim'])) {
    matches.add('reduce');
  }
  if (normalized.includes('強烈賣出') || normalized.includes('賣出') || normalized.includes('清倉') || matchesEnglishTerm(lower, ['sell'])) {
    matches.add('sell');
  }
  if (normalized.includes('持有') || normalized.includes('洗盤觀察') || matchesEnglishTerm(lower, ['hold'])) {
    matches.add('hold');
  }
  if (normalized.includes('觀望') || normalized.includes('等待') || matchesEnglishTerm(lower, ['watch', 'wait'])) {
    matches.add('watch');
  }
  if (normalized.includes('強烈買入') || normalized.includes('買入') || normalized.includes('佈局') || normalized.includes('建倉') || matchesEnglishTerm(lower, ['buy'])) {
    matches.add('buy');
  }

  if (matches.size === 1) {
    return Array.from(matches)[0];
  }
  return null;
};

export const getDecisionActionLabel = (
  action?: DecisionAction | null,
  actionLabel?: string | null,
  legacyAdvice?: string | null,
  emptyLabel: string | null = '建議',
  labels?: Partial<DecisionActionLabelMap>,
): string | null => {
  const actionLabels = resolveActionLabels(labels);
  if (action) return actionLabels[action];
  const explicitLabel = actionLabel?.trim();
  if (explicitLabel) return explicitLabel;
  return getLegacyDecisionActionLabel(legacyAdvice, actionLabels) || emptyLabel;
};

export const getDecisionActionTone = (
  action?: DecisionAction | null,
  actionLabel?: string | null,
  legacyAdvice?: string | null,
): DecisionActionTone => {
  if (action) return toneForAction(action);

  const label = actionLabel?.trim() || '';
  if (label) {
    const lowerLabel = normalizeEnglishAdvice(label);
    if (label.includes('買') || label.includes('加倉') || label.includes('持有')) return 'success';
    if (label.includes('賣') || label.includes('減倉') || label.includes('清倉')) return 'danger';
    if (label.includes('觀望') || label.includes('等待') || label.includes('迴避') || label.includes('預警')) {
      return 'warning';
    }
    if (matchesEnglishTerm(lowerLabel, ['buy', 'add', 'hold'])) return 'success';
    if (matchesEnglishTerm(lowerLabel, ['sell', 'reduce', 'trim'])) return 'danger';
    if (matchesEnglishTerm(lowerLabel, ['watch', 'wait', 'avoid', 'alert'])) return 'warning';
    return 'default';
  }

  const legacyAction = getLegacyDecisionAction(legacyAdvice);
  if (legacyAction) return toneForAction(legacyAction);

  return 'default';
};
