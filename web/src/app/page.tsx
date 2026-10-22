"use client";

import { useCallback, useState } from "react";
import AccountPanel from "@/components/AccountPanel";
import Chat from "@/components/Chat";
import SignIn from "@/components/SignIn";
import { getAccount, logout } from "@/lib/api";
import type { Account } from "@/lib/types";

export default function Home() {
  const [sessionId, setSessionId] = useState<string | null>(null);
  const [account, setAccount] = useState<Account | null>(null);
  const [refreshing, setRefreshing] = useState(false);
  const [showDetails, setShowDetails] = useState(false);
  const [mobileTab, setMobileTab] = useState<"chat" | "accounts">("chat");
  const [notice, setNotice] = useState<string | null>(null);

  const refresh = useCallback(async () => {
    if (!sessionId) return;
    setRefreshing(true);
    try {
      setAccount(await getAccount(sessionId));
    } catch {
      // Keep showing the last snapshot; the chat reports outages itself.
    } finally {
      setRefreshing(false);
    }
  }, [sessionId]);

  function signOut(message: string | null = null) {
    if (sessionId) logout(sessionId);
    setSessionId(null);
    setAccount(null);
    setNotice(message);
  }

  if (!sessionId) {
    return (
      <>
        {notice && <p className="mx-auto mt-6 max-w-md rounded-lg bg-warn-soft px-3 py-2 text-center text-sm text-warn">{notice}</p>}
        <SignIn
          onSignedIn={(s) => {
            setSessionId(s.sessionId);
            setAccount(s.account);
            setNotice(null);
          }}
        />
      </>
    );
  }

  const customer = account?.customer;
  return (
    <div className="flex h-full flex-col">
      <header className="flex items-center justify-between gap-3 border-b border-line bg-surface px-4 py-3 sm:px-6">
        <div className="flex items-center gap-3">
          <div className="h-8 w-8 rounded-lg bg-accent text-white grid place-items-center text-sm font-semibold">LS</div>
          <div className="leading-tight">
            <p className="whitespace-nowrap text-sm font-semibold">Loan Servicing</p>
            <p className="text-xs text-muted">{customer ? `${customer.first_name} ${customer.last_name}` : ""}</p>
          </div>
        </div>
        <div className="flex items-center gap-3">
          <label className="flex cursor-pointer items-center gap-2 whitespace-nowrap text-xs text-muted">
            <input
              type="checkbox"
              checked={showDetails}
              onChange={(e) => setShowDetails(e.target.checked)}
              className="accent-[var(--accent)]"
            />
            <span className="hidden sm:inline">Latency details</span>
            <span className="sm:hidden">Details</span>
          </label>
          <button onClick={() => signOut()} className="whitespace-nowrap rounded-lg border border-line px-3 py-1.5 text-xs hover:border-accent">
            Sign out
          </button>
        </div>
      </header>

      <nav className="flex border-b border-line bg-surface text-sm lg:hidden">
        {(["chat", "accounts"] as const).map((t) => (
          <button
            key={t}
            onClick={() => setMobileTab(t)}
            className={`flex-1 py-2 ${mobileTab === t ? "border-b-2 border-accent font-medium text-accent" : "text-muted"}`}
          >
            {t === "chat" ? "Assistant" : "Accounts"}
          </button>
        ))}
      </nav>

      <div className="flex min-h-0 flex-1">
        <div className={`min-w-0 flex-1 ${mobileTab === "chat" ? "block" : "hidden"} lg:block`}>
          <Chat
            sessionId={sessionId}
            customerId={customer?.customer_id}
            firstName={customer?.first_name}
            showDetails={showDetails}
            onTurnComplete={refresh}
            onSessionExpired={() => signOut("Your session expired. Please sign in again.")}
          />
        </div>
        <div
          className={`w-full border-l border-line bg-background lg:block lg:w-[360px] ${mobileTab === "accounts" ? "block" : "hidden"}`}
        >
          <AccountPanel account={account} refreshing={refreshing} />
        </div>
      </div>
    </div>
  );
}
