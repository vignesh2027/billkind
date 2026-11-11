# Billkind design

Billkind lets anyone, anywhere, post a real bill they cannot pay (electricity, water, school fees, a hospital invoice, rent, a phone bill) and lets sponsors clear it with a swipe. The money goes to the verified biller, never to the person. The sponsor sees exactly what their money did: the biller's paid stamp, the receipt and a thank-you, live.

Inspired by a viral joke about an app where you upload your bills and bored billionaires swipe right to pay them. Billkind is the safe, real version of that joke.

## 1. The problem

Bills are where hardship becomes concrete, and the gap is usually small.

| Fact | Source |
|---|---|
| 37% of US adults would not cover a 400 USD emergency expense with cash or its equivalent (63% would). | Federal Reserve, Economic Well-Being of U.S. Households in 2025, May 2026. https://www.federalreserve.gov/newsevents/pressreleases/other20260513a.htm |
| One in six US households is behind on its utility bills; utility debt is projected to reach about 25 billion USD by the end of 2026; utilities disconnected electric service about 13.5 million times in 2024. | NEADA, 9 June 2026. https://neada.org/?p=10510 |
| 34 million US households (27%) had difficulty paying energy bills or kept the home at an unsafe temperature in 2020. | US EIA, RECS 2020. https://www.eia.gov/todayinenergy/detail.php?id=51979 |
| US residents owe at least 220 billion USD in medical debt. | KFF, Feb 2024. https://www.kff.org/health-costs/issue-brief/the-burden-of-medical-debt-in-the-united-states/ |
| Nearly half of US renter households were cost burdened in 2024. | Harvard JCHS, State of the Nation's Housing 2026, via Harvard GSD. https://www.gsd.harvard.edu/2026/06/state-of-the-nations-housing-2026-finds-affordability-challenges/ |
| In India, families spend on average 25,002 INR a year per student in non-government schools against 2,863 INR in government schools; only 1.2% of students rely mainly on government scholarships. | MoSPI NSS 80th round, Aug 2025 |
| In low and lower-middle income countries households cover 39% of the cost of education, against 16% in high income countries; 8% of families borrow to send children to school. | UNESCO GEM Report, Dec 2021. https://world-education-blog.org/2021/12/14/8-of-families-in-the-poorest-countries-have-to-borrow-money-to-send-their-children-to-school/ |

Crowdfunding is the current answer, and it fails the people who need it most:

| Fact | Source |
|---|---|
| Of 437,596 US medical crowdfunding campaigns (2016 to 2020), only 12% met their goal and 16% received no donations at all; campaigns from poorer, high medical-debt areas raised less. | Kenworthy and Igra, American Journal of Public Health, 2022. https://pmc.ncbi.nlm.nih.gov/articles/PMC8887155 |
| 67.7% of donors say trust is essential before giving, but only 18.3% report high trust in charities. Only 31% rank giving directly to an individual among the most impactful ways to help. | BBB Wise Giving Alliance, Donor Trust Report 2026 |

The pattern: a campaign needs a compelling story, a large network and the giver's trust that the money will be used as described. A 60 USD power bill has none of these, so it is never posted, and the power is cut.

## 2. Who it is for

- **Posters**: a parent with a school-fee invoice, a pensioner with a power bill, a patient with a clinic balance. Anywhere in the world, any currency. They need help with one specific bill, quickly, without public exposure.
- **Sponsors**: individuals with spare money and little time, from a few dollars to a very large budget. They want certainty that the money did the thing it was given for, and they want it to feel good.
- **Billers**: schools, clinics, utilities, landlords. They want the account credited correctly and nothing else to do.

## 3. The experience

1. **Post a bill.** Photo or PDF. The AI reads it, fills in biller, amount, currency, due date and account reference, checks it for fraud, masks personal data, and helps the poster write a short, dignified note with their consent.
2. **Swipe.** Sponsors see a deck of verified bill cards from around the world: category, city and country, amount in the bill's currency and in theirs, days to the due date, the AI-written note, and a plain verification badge. Swipe right (or press the right arrow, or the Pay button) to pay all or part. Swipe left to pass.
3. **Pay.** PayPal checkout in the bill's settlement currency. Several sponsors can pool partial payments into one bill.
4. **Biller paid.** When a bill is fully funded, Billkind sends one PayPal payout to the biller's verified account with the account reference in the payout note. When PayPal confirms the payout, the card gets a PAID stamp.
5. **Gratitude, live.** The sponsor's dashboard updates in real time: payout confirmed, receipt issued, and the poster's thank-you arriving.
6. **The world wall.** A public live page: bills being cleared around the world, a counter of bills cleared, totals per currency and per country. Every number comes from the database of real sandbox transactions.

Big-sponsor mode: "clear every school-fee bill under 50 USD in Nairobi" becomes one PayPal checkout for the total, then per-bill payouts in one batch and a receipt per bill.

Giving agent: "up to 100 USD a month for children's school fees and medical bills, never more than 40 per bill". The AI turns this into a structured policy the sponsor confirms, then pays matching bills on its own with the sponsor's vaulted PayPal, explaining every decision. Caps are enforced in code.

## 4. Safety model: how fraud is stopped

The single most important rule: **money only ever moves to a verified biller's PayPal account, never to the poster.** The worst a fraudster can do is get a real biller's real account credited, which is the outcome the sponsor wanted anyway. This removes the main reason to lie.

Layers, in order:

| Layer | What it does | Where |
|---|---|---|
| Verified biller registry | A bill is payable only when its biller matches a biller whose PayPal account was verified by an admin. Unknown billers go to the review queue with an invite step. | `app/core/billers.py` |
| Document checks | Image EXIF software tags and PDF producer/modification metadata from editing tools; OCR text cross-checked against the AI extraction (amount and due date must agree); arithmetic of line items against the total. | `app/ai/fraud.py` |
| Sanity rules | Amount range per category and country, due date not in the past and not more than 120 days out, currency consistent with the biller. | `app/ai/fraud.py` |
| Duplicates | Perceptual image hash, normalized text hash, and same biller plus account reference plus amount within 60 days. | `app/ai/fraud.py` |
| Velocity | Open bills per poster, bills per 30 days, total open amount per poster, posts per device fingerprint. | `app/ai/fraud.py` |
| Risk score | 0 to 100 with every contributing reason in plain words. Low risk lists automatically, medium goes to human review, hard failures (duplicate of a paid bill, blocked biller) are rejected with the reason shown. | `app/ai/fraud.py` |
| Privacy | Account numbers are masked to the last 4 digits; names, phone numbers, emails and street addresses are removed from anything public. The original document is visible only to admins. | `app/ai/redact.py` |
| Payment controls | Server-side amount checks on every order, overfunding refunds, refunds on withdrawal or payout failure, webhook signature verification, idempotent event handling. | `app/core/funding.py`, `app/paypal/webhooks.py` |
| Agent caps | Monthly budget, per-bill cap, categories, countries, maximum risk score, enforced by code after the model proposes. The model cannot exceed them. | `app/ai/agent.py` |

AI is used where it helps a human decide, never as the only gate on money: the deterministic rules and the biller registry decide whether money can move.

## 5. The four pillars

### AI

| Feature | Model | Fallback without AI |
|---|---|---|
| Bill intake from photo | `qwen/qwen3.8-27b` vision on Groq returns structured JSON | Tesseract OCR plus regex parser |
| Bill intake from PDF | `pypdf` text, then `openai/gpt-oss-120b` structured extraction | Regex parser |
| Fraud signals and plain reasons | Rules plus the model's consistency review | Rules only |
| Privacy redaction | Regex masking, then a model pass for names and addresses | Regex only |
| Dignified story | `openai/gpt-oss-120b` from the poster's own words, poster approves | Template |
| Sponsor matching | Preference scoring in code, model writes the "why this bill" line | Score only |
| Giving agent | Model ranks eligible bills and explains; code enforces caps | Rule-based picker |
| Support assistant | Model answers only from tool results (the user's own records, policies, PayPal order lookups through the PayPal Agent Toolkit); every number in an answer is checked against the tool output before it is shown | Keyword intents with templated answers |

### Automation

- Payment state machine driven by PayPal webhooks, with polling as a fallback.
- Automatic payout when a bill is fully funded.
- Receipts and thank-you requests on payout success.
- Due-date reminders and urgency boosts in the deck.
- Recurring monthly sponsorship of a household's bill through PayPal Vault.
- Pooled partial funding, overfunding refunds, refunds on withdrawal, expiry and payout failure.
- Admin review queue for risky bills, unknown billers, failed payouts and escalated support tickets.

### Support

An assistant for posters and sponsors: "where is my bill", "why was it held", "how do refunds work", "did the school get my money". It sees only the signed-in user's records and the policy pages. If it cannot answer from those, it says so and offers a human ticket, which lands in the admin queue.

### PayPal

| API | Use | Code |
|---|---|---|
| OAuth 2.0 client credentials | Access tokens, cached until expiry | `app/paypal/client.py` |
| Orders v2: create, get, capture | Every sponsor payment, single or bulk | `app/paypal/client.py` |
| JS SDK Buttons | Buyer approval in the browser | `app/web/templates/pay.html` |
| Payments v2: refund capture | Withdrawn, expired or overfunded bills, failed payouts | `app/paypal/client.py` |
| Payouts v1: create batch, get batch | Paying the biller; bulk mode sends one batch with one item per bill | `app/paypal/client.py` |
| Vault v3: setup tokens, payment tokens, charge with vault_id | Giving agent and recurring sponsorship without the sponsor present | `app/paypal/client.py` |
| Webhooks: self-verification (CRC32, cert, SHA256withRSA) with verify-webhook-signature API fallback | Trusting event notifications | `app/paypal/webhooks.py` |
| PayPal Agent Toolkit (Python, official) | The support assistant's order and transaction lookups go through PayPal's own agent tools | `app/paypal/toolkit.py` |
| APIMatic-generated PayPal Server SDK | Orders, Payments and Vault calls | `app/paypal/client.py` |

Multi-currency: each bill keeps its own currency for display. When that currency is supported by PayPal for cross-border payments (USD, EUR, GBP, CAD, AUD, JPY, MXN, PHP, SGD and others), the order and the payout use it directly. When it is not (for example INR or KES), the bill settles in USD at an indicative rate that is shown to everyone, and the biller receives USD in its PayPal account. This is stated on every card.

PayPal Agent Toolkit and MCP: PayPal publishes its agent tools as a TypeScript and Python toolkit and as a remote MCP server. The Python toolkit's core needs only `requests` and `pydantic`, so it is installed without its optional LangChain and CrewAI extras and called directly; no Node toolchain is used. The support assistant reads live order status through the toolkit's `get_order_details` tool, limited to the user's own orders. The Python toolkit (1.11) has no tool for charging a vaulted payment method or for refunds, so the giving agent pays through Billkind's own executor, which is also where the hard caps live. The remote MCP server exposes the same tool set and needs a live access token, so it adds nothing over the toolkit here.

APIMatic: PayPal's official Python Server SDK is generated by APIMatic. Billkind uses it for every Orders, Payments and Vault call, with a plain REST path kept for comparison; contract tests run both. Payouts and webhook verification are not in that SDK and use REST.

## 6. Payment state machines

Bill:

```
SUBMITTED   intake and checks running
REVIEW      held for a human (risk, unknown biller)
REJECTED    terminal, reason shown to the poster
LISTED      visible in the deck
FUNDING     at least one capture, not yet fully funded
FUNDED      captures cover the amount; payout requested
PAYOUT_SENT payout batch accepted by PayPal
PAID        payout item succeeded: the biller has the money (terminal)
PAYOUT_FAILED payout item failed, unclaimed, returned or blocked; admin decides retry or refund
WITHDRAWN   poster withdrew; captures refunded (terminal)
EXPIRED     due date passed while unfunded; captures refunded (terminal)
```

Contribution (one sponsor payment): `CREATED`, `CAPTURED`, `REFUND_PENDING`, `REFUNDED`, `PARTIALLY_REFUNDED`, `DENIED`, `CANCELLED`.

Transitions are a table in `app/core/states.py`; anything not in the table raises. Every transition writes an event row, which also feeds the live wall.

Overfunding: before an order is created, the server reserves the remaining amount for 15 minutes so two sponsors cannot both pay the last 30 USD. If a race still overfunds (two captures settle together), the excess is refunded automatically from the last capture.

## 6b. Live by design

- **Any bill, anywhere**: bills keep their own currency; sponsors see their own currency alongside; settlement follows PayPal's currency support with a stated USD fallback.
- **Live wall**: server-sent events (no framework) push every public event: listed, funded, fully funded, payout sent, paid, thank-you. The map, counters and per-currency totals come from the database.
- **Big-sponsor mode**: "clear every school-fee bill under 50 USD" becomes one PayPal order with one item per bill, then one payout batch with one item per biller and one receipt per bill.
- **Instant gratitude**: the sponsor's payment sheet walks through captured, funded, payout sent, PAID and thank-you as each event arrives; the poster's thank-you appears on the sponsor's dashboard live.

## 7. Giving agent with hard caps

1. The sponsor writes rules in plain words.
2. The model turns them into a policy: monthly budget, per-bill cap, categories, countries, maximum risk, minimum days to due date. The sponsor sees and confirms the structured version.
3. The sponsor vaults their PayPal once (Vault v3).
4. On each run, code selects candidates that already satisfy the policy. The model ranks them and proposes amounts with a one-line reason each.
5. Code clamps every proposal: per-bill cap, remaining monthly budget, remaining bill amount. A proposal that breaks a rule is not paid, and the log says which rule stopped it.
6. Payments use Orders v2 with the vaulted PayPal (no buyer present), and flow through the same funding and payout path as a manual swipe.

## 8. Path to production (not claimed for the hackathon)

Everything in this repository runs in the PayPal sandbox. Going live would need at least:

- PayPal live app approval, and enabling Payouts and Vault on the live business account, which PayPal reviews separately.
- KYC: identity checks for posters above small amounts, and business verification for billers (registration documents, and a PayPal business account in the biller's legal name). The current admin verification is a manual stand-in.
- Regulatory review: collecting sponsor funds and paying them out to third parties can be money transmission in many jurisdictions. A production design would use a PayPal marketplace or platform model (PayPal Commerce Platform) where funds move to the biller as payee directly, or partner with a licensed fiscal sponsor or charity. Tax treatment of gifts and receipts differs by country.
- Sanctions and restricted-country screening on posters, billers and sponsors.
- Data protection review (GDPR, India DPDP Act) for stored bill images, with retention limits.

## 9. Architecture

- Python 3.12, FastAPI, Jinja2 server-rendered pages, SQLite. Inline JS only for the swipe gesture, server-sent events and the PayPal JS SDK.
- `app/paypal/fake.py` is a local fake of every PayPal endpoint Billkind uses, with the documented request and response shapes, signed webhooks and a buyer-approval page. It lets the app and its tests run without credentials. `PAYPAL_MODE=sandbox` switches to the real sandbox.
- Background loop for reminders, recurring charges, agent runs, payout polling and reservation expiry.
- Hosted on an Oracle Cloud Always Free VM: one always-on Docker container behind Caddy (HTTPS), SQLite on a host volume. The Modal app (`modal_app.py`, SQLite on a Modal Volume) is kept as an alternative host.
