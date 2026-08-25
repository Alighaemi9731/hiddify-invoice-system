import { fmtNum } from "../../format";

/**
 * How a plan is named, everywhere: an OPTIONAL name, then its quota and duration.
 *
 * The name is a prefix, never a replacement — a customer choosing between «طلایی» and «نقره‌ای»
 * must still see what each one actually buys. A plan with no name (`title === ""`, the default)
 * renders exactly the string it always did.
 *
 * This is the word-for-word twin of `plan_label` in `backend/app/bot/storefront/keyboards.py`
 * (which appends the price, rendered separately here), so a reseller reads the same plan the same
 * way in the bot and in the portal; change one and change the other.
 */
export const planLabel = (plan: { title?: string | null; gb: number; days: number }) => {
  const name = (plan.title || "").trim();
  const head = name ? `🏅 ${name} · ` : "";
  return `${head}${fmtNum(plan.gb)} گیگابایت · ${fmtNum(plan.days)} روزه`;
};
