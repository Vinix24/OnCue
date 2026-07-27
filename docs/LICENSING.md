# License decision

Copyright (C) 2026 Vincent van Deth / VNX Digital.

This document records the recommended license choice for OnCue and
why. The final choice rests with the owner (Vincent van Deth / VNX
Digital). This document describes the recommendation, not an irreversible fact.

## Recommendation

AGPL-3.0 as the open-source license, combined with dual-licensing.

The full license text is in [`../LICENSE`](../LICENSE). The commercial
exception is in [`../LICENSE-COMMERCIAL.md`](../LICENSE-COMMERCIAL.md). The
Contributor License Agreement that makes dual-licensing possible is in
[`../CLA.md`](../CLA.md).

## Why AGPL-3.0

The code is open. The moat is not in the code. The moat is in the curated
card DATA, the hosted compliance and license service, and the
implementation expertise. Those three stay separate and private.

AGPL-3.0 fits that strategy exactly. It keeps the code open and enforces that
anyone who offers a modified version as a network service also publishes their
changes. That is section 13 of the AGPL, the "remote network interaction" clause.
A regular GPL has that gap: someone runs a modified fork as SaaS without
ever releasing the source code. AGPL closes that gap.

Concretely, that protects against the scenario where a competitor takes my code,
builds a hosted version around it, locks it down, and sells it as closed SaaS.
Under AGPL, that competitor must publish their changes. The card DATA and the
service remain my edge, because they fall outside the license.

## What dual-licensing means here

The default license is AGPL-3.0. Anyone may use, modify, and run the code under
those terms.

Some customers cannot accept AGPL. Think of parties with a policy against copyleft
in derivative works, or who want to embed the software in a closed product, or who
want to offer it as proprietary SaaS without publishing their changes. For those
parties, I sell a commercial exception: the same code, but without the copyleft
obligation.

That is only possible if I have the right to also release the contributions of
others under that commercial license. That is what the CLA is for. Every
contributor grants me a broad license to their contribution, including the right
to distribute it under AGPL and under a commercial license. Without that CLA, a
single external contribution would block the entire dual-license model, because
then I could not commercially relicense their code.

The CLA follows the structure of the Apache Individual and Corporate CLA. The
contributor keeps their own copyright. They grant me a license, not an assignment.
That is enough to make dual-licensing possible and is less onerous than a full
copyright assignment.

## Why not BSL or FSL

BSL (Business Source License) and FSL (Functional Source License) are
source-available, not open source. The code is visible and you may use it,
but with a commercial restriction: you may not offer a competing service with it.
After a term (usually four years for BSL, two years for FSL), the
code reverts to a true open-source license.

The difference with my choice:

- BSL and FSL are not on the OSI list. I may not call it open source. For
  the positioning "open source, measurable results", that is a problem.
- BSL and FSL deter contributors and early adopters. The license reads as
  "free until I decide you have to pay". That clashes with a community-first
  launch.
- The protection that BSL and FSL offer, the anti-competitor clause, I already get
  with AGPL plus the separated moat. AGPL lets a competitor use the code,
  but forces them to publish their changes, and they do not get the card DATA and
  the hosted service anyway. That is enough.

The price of AGPL over BSL/FSL: AGPL is truly open, so a competitor may run the
code as long as they respect the copyleft. BSL/FSL explicitly prohibit that until
the conversion date. I accept that price because my edge is not the code.
Whoever takes only the open code without the DATA and the service has an empty
shell.

In short: BSL/FSL buy temporary code exclusivity that I do not need, at the
cost of the open-source label that I do need.

## Open sub-decision: only versus or-later

The recommendation leans toward `AGPL-3.0-only` rather than `AGPL-3.0-or-later`.

With dual-licensing, you want to keep control over which license version applies.
`or-later` lets downstream users unilaterally choose a future AGPL version, and
that version could interpret the copyleft arrangement I rely on slightly
differently. `only` keeps that choice with me.

The owner has confirmed `-only`. `pyproject.toml` and the PyPI classifiers were
aligned to `AGPL-3.0-only` in the same change. The license text in `LICENSE`
is identical for both; the difference lies only in how you apply the license (the
"or any later version" wording in metadata and file headers).

## Summary of the choice

| Component | Choice |
|---|---|
| Open-source license | AGPL-3.0 |
| Version variant (recommended) | AGPL-3.0-only |
| Commercial track | Dual-license via commercial exception |
| Contributions | Apache-style CLA (individual + entity), license grant, no assignment |
| Rejected alternative | BSL / FSL (source-available, not open source) |
| Moat | Card DATA + hosted service + implementation, outside the license |
