# Social media post content

Ready-to-use posts announcing **Actualize** — the GitHub Copilot skill that reports the
*actual* cost of Azure resources (what you really pay after Enterprise/MCA discounts,
reservations, and savings plans) next to the *retail* price, with the percent difference
per line item.

Character counts are approximate. Swap `<REPO_URL>` for the repository link and adjust
hashtags/handles to fit each platform.

---

## LinkedIn (long form)

> **What are you *actually* paying for Azure — not the list price?**
>
> Retail price is easy to find. What you *really* pay after Enterprise/MCA discounts,
> reservations, and savings plans is much harder to see. That gap is exactly what
> **Actualize** surfaces.
>
> Actualize is a GitHub Copilot skill. You ask in plain language — "What did we actually
> pay for *Web-Prod* in June vs list price?" — and it:
>
> ✅ Pulls retail and actual from the **same** Azure Cost Management line item, so the
>    comparison is apples-to-apples — nothing is guessed.
> ✅ Reports the effective discount % per resource, meter, service, or subscription.
> ✅ Enriches missing retail prices from the public Azure Retail Prices API — and refuses
>    to guess when a meter maps to multiple SKUs, leaving those `UNKNOWN`.
> ✅ Turns "what do we save if we delete these 3 VMs?" into a real run-rate delta.
> ✅ Converts a proposed list-price cut into the **actual** saving after each resource's
>    real discount.
>
> Under the hood it's a deterministic, dependency-free Python calculator (stdlib only) —
> so every figure traces back to data you pasted, and you can run it standalone too.
>
> No agents to install on your tenant: it emits a copy-paste Azure Cloud Shell command,
> you run it, paste the result back, and get the answer.
>
> Open source (MIT). Take a look 👇
> <REPO_URL>
>
> #Azure #FinOps #CloudCost #GitHubCopilot #CostOptimization #OpenSource

---

## Bluesky (≤ 300 chars)

> Azure list price ≠ what you actually pay. Actualize is a GitHub Copilot skill that shows
> retail vs actual cost — after EA/MCA discounts, reservations & savings plans — with the
> % diff per line item. Never guesses; deterministic & open source.
> <REPO_URL>

---

## Mastodon (≤ 500 chars)

> Ever wonder what you *actually* pay for Azure after discounts, reservations and savings
> plans — not the list price?
>
> **Actualize** is a GitHub Copilot skill that puts retail next to actual cost from the
> same Cost Management line item, with the % difference per resource. It enriches missing
> retail prices safely and never guesses — anything unknown stays `UNKNOWN`.
>
> Deterministic Python core, no third-party deps, MIT licensed.
>
> <REPO_URL>
>
> #Azure #FinOps #OpenSource

---

## Threads (≤ 500 chars)

> Azure's list price isn't what you actually pay.
>
> Actualize is a GitHub Copilot skill that shows retail vs actual cost — after your
> EA/MCA discounts, reservations & savings plans — with the % difference per line item.
>
> Ask in plain language, get an apples-to-apples answer. It never guesses: unknowns stay
> unknown. Open source + deterministic.
>
> <REPO_URL>

---

## X / short micro-post (≤ 280 chars)

> Azure list price ≠ what you actually pay. Actualize (a GitHub Copilot skill) shows retail
> vs actual cost after EA/MCA discounts, reservations & savings plans — % diff per line
> item, never guessed. Open source. <REPO_URL>

---

## Suggested hashtags

`#Azure` `#FinOps` `#CloudCost` `#CostOptimization` `#GitHubCopilot` `#OpenSource`
