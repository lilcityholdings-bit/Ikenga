# Company operating rules (apply to every session)

The owner is building a tech company made of several digital products and
plans to sell the company, or individual products, one day. Every piece of
work should keep each product easy to hand over to a buyer.

## Guide the owner
- Say so when a request would make a product harder to transfer or sell:
  personal accounts, mixed repos, secrets in code, unlicensed content, or
  work that isn't documented.
- Suggest the organized option first, and keep advice practical and short.

## Structure
- One product per GitHub repo, owned by the company GitHub organization
  (`lilcityholdings-bit`). Don't put a new product inside another
  product's repo.
- Every repo has a README.md (what it is, how to run it, how to deploy it)
  and a line in `docs/ASSETS.md` for each external account it depends on.
- Each product gets its own Railway project, named after the product.
- Accounts (YouTube, Railway, Stripe, domains, APIs) go under a company
  email, never a personal one, so they can be transferred.

## Code and data
- Secrets stay in environment variables and never go into git. Keep a
  `.env.example` listing the names.
- Record third-party licenses and content sources (for example, CC-BY
  attribution) so a buyer's due diligence can check them.
- Only use content the company owns or is licensed to use. Never automate
  anything that risks copyright strikes or account bans.
- Keep tests passing and commit with clear messages. The git history is
  part of what a buyer inspects.
