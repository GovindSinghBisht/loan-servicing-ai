import { PRODUCT_LABEL, date, humanize, rupees, SYSTEM_LABEL } from "@/lib/format";
import type { Account, Card, Loan, Ticket } from "@/lib/types";

export default function AccountPanel({ account, refreshing }: { account: Account | null; refreshing: boolean }) {
  if (!account) return null;
  return (
    <aside className="h-full overflow-y-auto px-4 py-5 sm:px-5">
      <div className="mb-4 flex items-baseline justify-between">
        <h2 className="text-sm font-semibold">Your accounts</h2>
        <span className="text-xs text-muted">{refreshing ? "Updating…" : `As of ${date(account.business_date)}`}</span>
      </div>

      {account.unavailable_systems.length > 0 && (
        <p className="mb-3 rounded-lg bg-warn-soft px-3 py-2 text-xs text-warn">
          Some information is temporarily unavailable:{" "}
          {account.unavailable_systems.map((s) => SYSTEM_LABEL[s] ?? s).join(", ")}.
        </p>
      )}

      <div className="space-y-3">
        {account.loans.map((l) => (
          <LoanCard key={l.loan_id} loan={l} />
        ))}
        {account.cards.map((c) => (
          <CardTile key={c.card_id} card={c} />
        ))}
        {account.loans.length === 0 && account.cards.length === 0 && (
          <p className="text-sm text-muted">No active loans or cards.</p>
        )}
      </div>

      <h3 className="mb-2 mt-6 text-sm font-semibold">Open requests</h3>
      {account.open_service_requests.length === 0 ? (
        <p className="text-sm text-muted">Nothing open.</p>
      ) : (
        <ul className="space-y-2">
          {account.open_service_requests.map((t) => (
            <TicketRow key={t.ticket_id} ticket={t} />
          ))}
        </ul>
      )}
    </aside>
  );
}

function LoanCard({ loan }: { loan: Loan }) {
  const overdue = loan.overdue_amount > 0;
  return (
    <div className="rounded-xl border border-line bg-surface p-4">
      <div className="flex items-start justify-between gap-2">
        <div>
          <p className="text-sm font-medium">{PRODUCT_LABEL[loan.product] ?? humanize(loan.product)}</p>
          <p className="text-xs text-muted">
            {loan.loan_id} · {loan.rate_type === "FLOATING" ? "Floating" : "Fixed"} {loan.current_rate_pct}%
          </p>
        </div>
        {overdue ? <Badge tone="bad">Overdue</Badge> : <Badge tone="ok">On track</Badge>}
      </div>
      <dl className="mt-3 grid grid-cols-2 gap-x-3 gap-y-2 text-sm">
        <Stat label="EMI" value={rupees(loan.current_emi)} />
        <Stat label="Next due" value={date(loan.next_due_date)} />
        <Stat label="Outstanding" value={rupees(loan.principal_outstanding)} />
        <Stat label="EMIs paid / left" value={`${loan.emis_paid} / ${loan.emis_remaining}`} />
      </dl>
      {overdue && (
        <p className="mt-3 rounded-lg bg-bad-soft px-3 py-2 text-xs text-bad">
          {rupees(loan.overdue_amount)} overdue{loan.unpaid_charges > 0 ? ` + ${rupees(loan.unpaid_charges)} charges` : ""}
        </p>
      )}
    </div>
  );
}

function CardTile({ card }: { card: Card }) {
  return (
    <div className="rounded-xl border border-line bg-surface p-4">
      <div className="flex items-start justify-between gap-2">
        <div>
          <p className="text-sm font-medium">{card.product} credit card</p>
          <p className="text-xs text-muted">{card.card_number_masked}</p>
        </div>
        <Badge tone={card.status === "ACTIVE" ? "ok" : "bad"}>{humanize(card.status)}</Badge>
      </div>
      <dl className="mt-3 grid grid-cols-2 gap-x-3 gap-y-2 text-sm">
        <Stat label="Outstanding" value={rupees(card.outstanding)} />
        <Stat label="Available" value={rupees(card.available_limit)} />
        <Stat label="Autopay" value={card.autopay.enabled ? humanize(card.autopay.mode ?? "on") : "Off"} />
        <Stat label="Limit" value={rupees(card.credit_limit)} />
      </dl>
    </div>
  );
}

function TicketRow({ ticket }: { ticket: Ticket }) {
  return (
    <li className="rounded-xl border border-line bg-surface p-3">
      <div className="flex items-start justify-between gap-2">
        <p className="text-sm font-medium">{humanize(ticket.category)}</p>
        <Badge tone={ticket.sla_breached ? "bad" : "neutral"}>{ticket.sla_breached ? "Overdue" : humanize(ticket.status)}</Badge>
      </div>
      <p className="mt-0.5 text-xs text-muted">
        {ticket.ticket_id} · opened {date(ticket.created_at)} · due {date(ticket.sla_due_on)}
      </p>
    </li>
  );
}

function Stat({ label, value }: { label: string; value: string }) {
  return (
    <div>
      <dt className="text-xs text-muted">{label}</dt>
      <dd className="font-medium tabular-nums">{value}</dd>
    </div>
  );
}

function Badge({ tone, children }: { tone: "ok" | "bad" | "neutral"; children: React.ReactNode }) {
  const cls = { ok: "bg-ok-soft text-ok", bad: "bg-bad-soft text-bad", neutral: "bg-surface-2 text-muted" }[tone];
  return <span className={`shrink-0 rounded-full px-2 py-0.5 text-[11px] font-medium ${cls}`}>{children}</span>;
}
