import { fmtNum } from "../../format";

/**
 * How a plan is named, everywhere. Plans have no title (owner decision, 2026-08-18: the field was
 * portal-only, invisible in the bot, and left every bot-made plan permanently unnamed) — a plan IS
 * its quota and duration. This is the word-for-word twin of `plan_label` in
 * `backend/app/bot/storefront/keyboards.py`, so a reseller reads the same plan the same way in the
 * bot and in the portal; change one and change the other.
 */
export const planLabel = (plan: { gb: number; days: number }) =>
  `${fmtNum(plan.gb)} گیگابایت · ${fmtNum(plan.days)} روزه`;
