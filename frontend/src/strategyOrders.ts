export interface Fill {
  date: string;
  execution_phase: string;
  price: number;
  shares: number;
}

interface Attempt {
  date: string;
  reason: string;
}

export interface Order {
  date: string;
  secid: string;
  name: string;
  strategy: string;
  status: string;
  reason: string;
  quality: string;
  provenance: string;
  eligible_for_validation: boolean;
  buy: Fill | null;
  sell: Fill | null;
  net_return: number | null;
  net_pnl: number | null;
  total_cost: number | null;
  cost?: number | null;
  target_date: string | null;
  trigger_date: string | null;
  invalidation_price: number | null;
  invalidation_basis: string | null;
  exit_rule_version: string;
  entry_attempts: Attempt[];
  exit_attempts: Attempt[];
  problem_date?: string;
  path_gaps?: string[];
  confirmation?: Attempt & { trigger: number; open?: number; close?: number };
  signal?: {
    reason: string;
    trigger_price: number | null;
    indicator_asof: string;
    indicator_basis: string;
    sector?: string;
    sector_asof?: string;
  };
}

export const policies = [
  { id: "O", name: "开盘直买" },
  { id: "R", name: "日线确认后买入" },
] as const;

export type PairedOrders = Partial<Record<(typeof policies)[number]["id"], Order>>;

export interface Candidate {
  date: string;
  secid: string;
  symbol: string;
  name: string;
  mode: string;
  origin: string;
  mode_basis: string;
  trigger: number | null;
  risk: number | null;
  generated_at: string | null;
  entry_session: string | null;
  target_date: string | null;
  signal_bar?: {
    close: number;
    source: string;
    observed_at?: string;
    volume_ratio_20?: number;
  };
  history?: { start: string; end: string; count: number; price_sources?: string[] };
  macd?: { state: string; dif: number; dea: number; histogram: number };
  orders: PairedOrders;
  structure?: PairedOrders;
}

export type ReviewOrders = Record<string, Order[]>;

export type OrderRow = {
  id: string;
  date: string;
  secid: string;
  name: string;
  mode: string;
} & (
  | { dataset: "original"; raw: Order }
  | { dataset: "abc"; raw: Candidate }
);

export const reviewModes: Record<string, string> = {
  baseline: "原四信号对照",
  setups: "趋势突破 + 止跌确认",
  setups_sector: "相对强弱排序",
  setups_sector_exit: "结构与时间退出",
};

export const statuses: Record<string, string> = {
  closed: "已完成",
  pending_entry: "待入场",
  pending_exit: "待退出",
  entry_expired: "入场过期",
  entry_invalidated: "入场失效",
  confirmation_not_met: "未获确认",
  confirmation_lost: "确认后取消",
  unavailable: "数据不可用",
};

const labels: Record<string, string> = {
  ...statuses,
  baseline_core: "原四信号",
  trend_breakout: "趋势突破",
  reversal_confirmed: "止跌确认",
  open: "开盘",
  close: "收盘",
  time_exit_close: "到期收盘退出",
  structure_next_open: "结构失效后次日开盘退出",
  spot: "未复权快照",
  kline: "历史日线",
  mixed: "混合价格来源",
  qfq_or_mixed: "前复权或混合价格来源",
  spot_daily_simulation: "未复权日线模拟",
  exploratory_history: "探索性历史数据",
  no_completed_trade: "未完成买卖",
  point_in_time_reconstruction: "按信号日重建",
  reconstructed_from_current_database: "由当前数据库重建",
  dated_eod_archive: "当日收盘归档",
  dated_eod_record: "当日收盘记录",
  current_snapshot_membership: "当前行业归属快照",
  confirmed_daily: "日线已确认",
  provisional_intraday_or_unverified: "盘中暂定或尚未核验",
};

export function label(value: string | null | undefined): string {
  return value ? labels[value] || value : "—";
}

export function statusTone(status: string) {
  if (status === "closed") return "success";
  if (status === "pending_entry" || status === "pending_exit") return "info";
  if (status === "unavailable") return "warning";
  return "default";
}

export function orderMode(row: OrderRow): string {
  return row.dataset === "abc"
    ? `模式 ${row.mode}`
    : reviewModes[row.mode] || row.mode;
}

export function normalizeReview(data: ReviewOrders): OrderRow[] {
  if (!data || Array.isArray(data) || typeof data !== "object") {
    throw new Error("规则比较逐笔数据格式异常");
  }
  return Object.entries(data).flatMap(([mode, rows]) =>
    rows.map((raw, index) => ({
      id: `${mode}-${index}`,
      dataset: "original",
      date: raw.date,
      secid: raw.secid,
      name: raw.name,
      mode,
      raw,
    })),
  );
}

export function normalizeCandidates(data: Candidate[]): OrderRow[] {
  if (!Array.isArray(data)) throw new Error("A/B/C 逐笔数据格式异常");
  return data.map((raw, index) => ({
    id: `abc-${index}`,
    dataset: "abc",
    date: raw.date,
    secid: raw.secid,
    name: raw.name,
    mode: raw.mode,
    raw,
  }));
}
