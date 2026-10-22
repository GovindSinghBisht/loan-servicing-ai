/** Starter questions per demo customer, chosen to show the main customer journeys. */
const BY_CUSTOMER: Record<string, string[]> = {
  CUST100001: [
    "Why did my EMI increase this month?",
    "If I prepay ₹50,000 today, how much would I save?",
    "Show payments that failed in the last six months",
    "Can you waive my May bounce charge?",
  ],
  CUST100002: [
    "Am I eligible for foreclosure without a penalty?",
    "What happens if I prepay ₹50,000?",
    "What do I owe right now?",
  ],
  CUST100003: [
    "Rates went up but my EMI didn't change. Why?",
    "My October EMI was debited twice",
    "What's the status of my complaints?",
  ],
  CUST100004: [
    "What's my EMI?",
    "What are my options on my personal loan?",
    "How much to close my personal loan today?",
  ],
};

const DEFAULT = ["What's my next EMI?", "Did any payment fail recently?", "What are my options on my loan?"];

export function suggestionsFor(customerId: string | undefined): string[] {
  return (customerId && BY_CUSTOMER[customerId]) || DEFAULT;
}
