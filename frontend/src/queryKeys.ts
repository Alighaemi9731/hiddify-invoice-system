// Query keys that a money-changing mutation must invalidate so every view depending on
// invoice/payment state refetches. Shared by Invoices.tsx and Payments.tsx so the two can't
// drift (marking an invoice paid on the Invoices page must also refresh Payments/Dashboard/Debts).
export const MONEY_KEYS = ["payments", "invoices", "dashboard", "debts"];
