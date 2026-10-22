const inr = new Intl.NumberFormat("en-IN", { style: "currency", currency: "INR", maximumFractionDigits: 0 });
const inrPaise = new Intl.NumberFormat("en-IN", { style: "currency", currency: "INR", maximumFractionDigits: 2 });

export function rupees(value: number | null | undefined, paise = false): string {
  if (value === null || value === undefined) return "—";
  return (paise ? inrPaise : inr).format(value);
}

export function date(iso: string | null | undefined): string {
  if (!iso) return "—";
  const d = new Date(`${iso.slice(0, 10)}T00:00:00`);
  return d.toLocaleDateString("en-IN", { day: "numeric", month: "short", year: "numeric" });
}

export function seconds(ms: number | null | undefined): string {
  if (ms === null || ms === undefined) return "—";
  return ms < 1000 ? `${Math.round(ms)} ms` : `${(ms / 1000).toFixed(1)} s`;
}

export const PRODUCT_LABEL: Record<string, string> = {
  HOME_LOAN: "Home loan",
  LAP: "Loan against property",
  PERSONAL_LOAN: "Personal loan",
};

export function humanize(code: string): string {
  const s = code.replace(/_/g, " ").toLowerCase();
  return s.charAt(0).toUpperCase() + s.slice(1);
}

export const SYSTEM_LABEL: Record<string, string> = {
  crm: "Customer profile",
  loans: "Loan system",
  payments: "Payments",
  policy: "Policy",
  service_ops: "Service requests",
  cards: "Cards",
};

/** Turn an engine source reference (e.g. "policy:LP-PRE-001 v1") into a short readable label. */
export function sourceLabel(ref: string): string {
  const cut = ref.indexOf(":");
  const system = cut < 0 ? ref : ref.slice(0, cut);
  const rest = cut < 0 ? "" : ref.slice(cut + 1);
  if (system === "policy") return `Policy ${rest.replace(" v", " · v")}`;
  if (system === "loans") {
    const m = rest.match(/^([A-Z0-9]+)(?:\/(schedule)#(\d+)|\/(rate-history)|\/charges\/(.+))?$/);
    if (m?.[2]) return `${m[1]} · instalment #${m[3]}`;
    if (m?.[4]) return `${m[1]} · rate history`;
    if (m?.[5]) return `${m[1]} · charge ${m[5]}`;
    return `Loan ${rest}`;
  }
  if (system === "payments") return `Payment ${rest}`;
  if (system === "service_ops") return `Request ${rest}`;
  if (system === "cards") return `Card ${rest}`;
  if (system === "crm") return "Customer profile";
  return ref;
}
