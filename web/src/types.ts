export interface Store {
  store_id: string;
  name: string;
  org_id: string;
  facility_id: string;
  city: string;
}

export interface Candidate {
  jpin: string;
  product_title: string;
  category: string;
  is_rte: boolean;
  shelf_life_days: number;
  list_price: number;
  mrp: number;
}

export interface InventoryItem {
  jpin: string;
  product_title: string;
  inventory_at_t0: number | null;
  received_today: number | null;
  sold_today: number | null;
  t0_ms: number;
}

export interface InventorySnapshot {
  store: Store | null;
  facility_id: string | null;
  source: "live" | "partial" | "error" | "loading";
  t0_ms: number;
  loading: boolean;
  items: InventoryItem[];
}

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
  price_seq: number;
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
  list_price: number;
  floor_price: number;
  q0: number;
  q0_source: string;
  units_sold: number;
  recent_rate: number;
  projected_clearance: number;
  residual: number;
  ratio: number;
  clears: boolean;
  floored: boolean;
  clearance_mode: string;
  reorder_action: string;
  standing_rule_pct: number;
  low_confidence: boolean;
  status: string;
  awaiting_approval: boolean;
  pending_rung: string | null;
  pending_price: number | null;
  last_reason: string;
}

export interface OfferOutcome {
  run_id: string;
  jpin: string;
  product_title: string;
  phase: string;
  discount_pct: number;
  rate_before: number;
  rate_after: number;
  lift_pct: number;
  units_sold_after: number;
  units_left: number;
  revenue_recovered: number;
  waste_avoided_units: number;
  waste_avoided_value: number;
  headline: string;
  ts_ist: string;
}

export interface RunDetail extends RunSummary {
  events: RunEvent[];
  decisions: DecisionRow[];
  price_changes: PriceChange[];
  offers: Offer[];
  live?: LiveState | null;
}
