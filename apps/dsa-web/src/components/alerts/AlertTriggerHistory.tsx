import type React from 'react';
import { Activity } from 'lucide-react';
import { Badge, Card, EmptyState, Loading } from '../common';
import type { AlertTriggerItem } from '../../types/alerts';
import { formatDateTime } from '../../utils/format';
import { getMarketPhaseSummaryLabel } from '../../utils/marketPhase';

const statusLabel: Record<string, string> = {
  triggered: '已觸發',
  skipped: '已跳過',
  degraded: '降級',
  failed: '失敗',
};

function statusVariant(status: string): 'success' | 'warning' | 'danger' | 'default' {
  if (status === 'triggered') return 'success';
  if (status === 'skipped' || status === 'degraded') return 'warning';
  if (status === 'failed') return 'danger';
  return 'default';
}

function formatNullable(value?: string | number | null): string {
  if (value === null || value === undefined || value === '') return '--';
  return String(value);
}

function renderPhaseQuality(trigger: AlertTriggerItem): React.ReactNode {
  const phase = getMarketPhaseSummaryLabel(trigger.marketPhaseSummary, 'zh');
  const quality = trigger.analysisContextPackOverview?.dataQuality?.level;
  const limitations = trigger.analysisContextPackOverview?.dataQuality?.limitations?.slice(0, 2) ?? [];
  if (!phase && !quality && limitations.length === 0) {
    return <span className="text-xs text-muted-text">--</span>;
  }
  return (
    <div className="space-y-1">
      {phase ? <Badge variant="default">{phase.replace('市場階段: ', '').replace('市場階段：', '')}</Badge> : null}
      {quality ? <div className="text-xs text-secondary-text">質量：{quality}</div> : null}
      {limitations.length ? (
        <div className="max-w-[180px] text-xs text-muted-text">{limitations.join('；')}</div>
      ) : null}
    </div>
  );
}

interface AlertTriggerHistoryProps {
  triggers: AlertTriggerItem[];
  isLoading?: boolean;
}

export const AlertTriggerHistory: React.FC<AlertTriggerHistoryProps> = ({ triggers, isLoading = false }) => {
  return (
    <Card title="觸發歷史" subtitle="評估記錄" variant="bordered" padding="md">
      {isLoading ? <Loading label="正在加載觸發歷史" /> : null}
      {!isLoading && triggers.length === 0 ? (
        <EmptyState
          icon={<Activity className="h-6 w-6" />}
          title="暫無觸發歷史"
          description="後臺評估會記錄 triggered、skipped、degraded 和 failed 狀態；正常未觸發不會寫入歷史。"
        />
      ) : null}
      {!isLoading && triggers.length > 0 ? (
        <div className="overflow-x-auto">
          <table className="w-full min-w-[860px] text-left text-sm">
            <thead className="border-b border-border/60 text-xs uppercase text-muted-text">
              <tr>
                <th className="px-3 py-2 font-medium">狀態</th>
                <th className="px-3 py-2 font-medium">階段 / 質量</th>
                <th className="px-3 py-2 font-medium">目標</th>
                <th className="px-3 py-2 font-medium">觀察值</th>
                <th className="px-3 py-2 font-medium">閾值</th>
                <th className="px-3 py-2 font-medium">數據源</th>
                <th className="px-3 py-2 font-medium">數據時間</th>
                <th className="px-3 py-2 font-medium">原因</th>
              </tr>
            </thead>
            <tbody className="divide-y divide-border/40">
              {triggers.map((trigger) => (
                <tr key={trigger.id} className="align-top">
                  <td className="px-3 py-3">
                    <Badge variant={statusVariant(trigger.status)}>
                      {statusLabel[trigger.status] ?? trigger.status}
                    </Badge>
                  </td>
                  <td className="px-3 py-3">{renderPhaseQuality(trigger)}</td>
                  <td className="px-3 py-3 font-mono text-secondary-text">{trigger.target}</td>
                  <td className="px-3 py-3 text-secondary-text">{formatNullable(trigger.observedValue)}</td>
                  <td className="px-3 py-3 text-secondary-text">{formatNullable(trigger.threshold)}</td>
                  <td className="px-3 py-3 text-secondary-text">{formatNullable(trigger.dataSource)}</td>
                  <td className="px-3 py-3 text-xs text-secondary-text">
                    {formatDateTime(trigger.dataTimestamp ?? trigger.triggeredAt)}
                  </td>
                  <td className="px-3 py-3 text-secondary-text">
                    {trigger.reason || trigger.diagnostics || '--'}
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      ) : null}
    </Card>
  );
};
