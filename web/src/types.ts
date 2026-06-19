export interface RunSummary {
  run_id: string;
  store_id: string;
  jpin: string;
  receipt_date: string;
  clearance_date: string;
  product_title: string;
  category: string;
  is_rte: boolean;
  status: string;
  current_rung: string;
  list_price: number;
  current_price: number;
  q0: number;
  units_sold: number;
  awaiting_approval: boolean;
  shadow_mode: boolean;
  summary: string;
  updated_at: string | null;
}

export interface DecisionRow {
  rung: string;
  price: number;
  units_sold: number;
  run_rate: number;
  ratio: number;
  residual: number;
  decision: string;
  approval: string;
  reason: string;
  ts: string;
}

export interface RunEvent {
  kind: string;
  message: string;
  ts: string;
}

export interface PriceChange {
  rung: string;
  from_price: number;
  to_price: number;
  confirmed: boolean;
  ts: string;
}

export interface Offer {
  rung: string;
  headline: string;
  price: number;
  channel: string;
  ts: string;
}

export interface LiveState {
  current_rung: string;
  current_price: number;
  q0: number;
  units_sold: number;
  run_rate: number;
  projected_clearance: number;
  residual: number;
  ratio: number;
  status: string;
  awaiting_approval: boolean;
  pending_rung: string | null;
  pending_price: number | null;
  last_reason: string;
}

export interface RunDetail extends RunSummary {
  events: RunEvent[];
  decisions: DecisionRow[];
  price_changes: PriceChange[];
  offers: Offer[];
  live?: LiveState | null;
}
