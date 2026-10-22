"use client";

import { useEffect, useState } from "react";
import { getDemoCustomers, login } from "@/lib/api";
import type { Account, DemoCustomer } from "@/lib/types";

type Props = {
  onSignedIn: (session: { sessionId: string; account: Account }) => void;
};

export default function SignIn({ onSignedIn }: Props) {
  const [phone, setPhone] = useState("");
  const [otp, setOtp] = useState("");
  const [step, setStep] = useState<"phone" | "otp">("phone");
  const [demo, setDemo] = useState<DemoCustomer[]>([]);
  const [otpHint, setOtpHint] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    getDemoCustomers()
      .then((r) => {
        setDemo(r.customers);
        setOtpHint(r.otp_hint);
      })
      .catch((e) => setError(e.message));
  }, []);

  async function verify(p = phone, code = otp) {
    setBusy(true);
    setError(null);
    try {
      const r = await login(p, code);
      onSignedIn({ sessionId: r.session_id, account: r.account });
    } catch (e) {
      setError((e as Error).message);
      setBusy(false);
    }
  }

  function pickDemo(c: DemoCustomer) {
    setPhone(c.phone);
    setStep("otp");
    if (otpHint) {
      setOtp(otpHint);
      verify(c.phone, otpHint);
    }
  }

  return (
    <main className="min-h-full flex items-center justify-center px-4 py-10">
      <div className="w-full max-w-md">
        <div className="mb-8 flex items-center gap-3">
          <div className="h-10 w-10 rounded-xl bg-accent text-white grid place-items-center font-semibold">LS</div>
          <div>
            <h1 className="text-lg font-semibold leading-tight">Loan Servicing</h1>
            <p className="text-sm text-muted">Your loans, payments and requests in one conversation</p>
          </div>
        </div>

        <div className="rounded-2xl border border-line bg-surface p-6 shadow-sm">
          <h2 className="text-base font-semibold">Sign in</h2>
          <p className="mt-1 text-sm text-muted">Use the mobile number registered with your loan.</p>

          <form
            className="mt-5 space-y-4"
            onSubmit={(e) => {
              e.preventDefault();
              if (step === "phone") setStep("otp");
              else verify();
            }}
          >
            <label className="block">
              <span className="text-sm font-medium">Mobile number</span>
              <input
                className="mt-1.5 w-full rounded-lg border border-line bg-surface px-3 py-2.5 text-base outline-none focus:border-accent focus:ring-2 focus:ring-accent-soft"
                inputMode="tel"
                autoComplete="tel"
                placeholder="+91 98000 00001"
                value={phone}
                onChange={(e) => {
                  setPhone(e.target.value);
                  setStep("phone");
                }}
                required
              />
            </label>

            {step === "otp" && (
              <label className="block">
                <span className="text-sm font-medium">One-time password</span>
                <input
                  className="mt-1.5 w-full rounded-lg border border-line bg-surface px-3 py-2.5 text-base tracking-[0.3em] outline-none focus:border-accent focus:ring-2 focus:ring-accent-soft"
                  inputMode="numeric"
                  autoComplete="one-time-code"
                  maxLength={6}
                  value={otp}
                  onChange={(e) => setOtp(e.target.value.replace(/\D/g, ""))}
                  autoFocus
                  required
                />
                {otpHint && <span className="mt-1.5 block text-xs text-muted">Demo OTP: {otpHint}</span>}
              </label>
            )}

            {error && <p className="rounded-lg bg-bad-soft px-3 py-2 text-sm text-bad">{error}</p>}

            <button
              type="submit"
              disabled={busy}
              className="w-full rounded-lg bg-accent px-4 py-2.5 text-sm font-medium text-white hover:bg-accent-strong disabled:opacity-60"
            >
              {busy ? "Signing in…" : step === "phone" ? "Send OTP" : "Verify and continue"}
            </button>
          </form>
        </div>

        {demo.length > 0 && (
          <div className="mt-6">
            <p className="mb-2 text-xs font-medium uppercase tracking-wide text-muted">Demo customers</p>
            <div className="grid gap-2 sm:grid-cols-2">
              {demo.map((c) => (
                <button
                  key={c.phone}
                  onClick={() => pickDemo(c)}
                  disabled={busy}
                  className="rounded-xl border border-line bg-surface p-3 text-left hover:border-accent disabled:opacity-60"
                >
                  <span className="block text-sm font-medium">{c.name}</span>
                  <span className="mt-0.5 block text-xs text-muted">{c.story}</span>
                </button>
              ))}
            </div>
          </div>
        )}
      </div>
    </main>
  );
}
