export type Loan = {
  loan_id: string;
  product: "HOME_LOAN" | "LAP" | "PERSONAL_LOAN" | string;
  status: string;
  rate_type: "FIXED" | "FLOATING";
  current_rate_pct: number;
  current_emi: number | null;
  next_due_date: string | null;
  principal_outstanding: number;
  emis_paid: number;
  emis_remaining: number;
  overdue_amount: number;
  unpaid_charges: number;
  reset_preference: "EMI" | "TENURE" | null;
  sanctioned_amount: number;
  disbursal_date: string;
};

export type Card = {
  card_id: string;
  product: string;
  card_number_masked: string;
  status: string;
  credit_limit: number;
  outstanding: number;
  available_limit: number;
  autopay: { enabled: boolean; mode: string | null };
};

export type Ticket = {
  ticket_id: string;
  category: string;
  status: string;
  created_at: string;
  sla_due_on: string;
  sla_breached: boolean;
  related_entity: { type: string; id: string };
};

export type Account = {
  business_date: string;
  customer: { customer_id: string; first_name: string; last_name: string } | null;
  loans: Loan[];
  closed_loans: string[];
  cards: Card[];
  open_service_requests: Ticket[];
  unavailable_systems: string[];
};

export type DemoCustomer = { phone: string; name: string; story: string };

export type Activity = {
  name: string;
  label: string;
  status: "running" | "ok" | "error";
  error?: string | null;
  system?: string | null;
};

export type ProposedAction = {
  action_id: string;
  summary: string;
  details: Record<string, unknown>;
  state: "pending" | "confirmed" | "cancelled";
};

export type TurnMetrics = {
  first_text_ms: number | null;
  total_ms: number;
  llm_ms: number;
  tools_ms: number;
  steps: number;
};

export type BackendStat = { calls: number; ms: number; retries: number; failed: number };

export type ChatMessage = {
  id: string;
  role: "user" | "assistant";
  text: string;
  activities: Activity[];
  sources: string[];
  action?: ProposedAction;
  metrics?: TurnMetrics;
  backend?: Record<string, BackendStat>;
  streaming: boolean;
  error?: string;
};
