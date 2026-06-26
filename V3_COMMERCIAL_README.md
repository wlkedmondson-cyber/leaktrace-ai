# LeakTrace V3 Commercial Pack

Added in this zip:

- First-run owner setup at `/setup`
- Secure login/logout
- Company accounts
- Role-based team users
- Plan limits:
  - Starter: 3 users / 25 completed AI investigations per month
  - Pro: 6 users / 100 completed AI investigations per month
  - Business: unlimited users / unlimited fair-use investigations
- Purchased investigation credit packs:
  - 10 credits / $25
  - 25 credits / $50
  - 50 credits / $90
- Billable event model:
  - Draft saves do not use a credit
  - Photo uploads do not use a credit
  - PDF downloads do not use a credit
  - Only “Generate Final AI Investigation” uses 1 credit
- Investigation version tracking
- Company dashboard with usage counters
- Billing/credits page
- Team management page
- CRM estimate add-on:
  - Repair estimate
  - New roof estimate
  - Printable owner-review estimate
  - ZIP-based pricing factor placeholder

Important:
- This zip intentionally excludes `.env`, `.git`, `venv`, `leaktrace.db`, generated reports, and uploaded photos.
- The credit purchase route is currently manual-mode and adds credits immediately. Stripe Checkout can be connected to the same `credit_purchases` table next.
- ZIP material pricing currently uses local pricing factors. A supplier/material API can replace that function later without changing the estimate workflow.
