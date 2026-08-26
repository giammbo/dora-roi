# Changelog

Notable changes to `dora-roi`. The format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and the project follows
[semantic versioning](https://semver.org/spec/v2.0.0.html) — with the usual pre-1.0
caveat that the minor version is where breaking changes live until 1.0.

## [Unreleased]

### 0.1.0 — first release

Everything below is new, so this is a feature list rather than a diff.

#### Commands

- `providers` — what the state files say you run, in one table.
- `scan` — the whole pipeline: discovery, mapping, optional LEI enrichment, optional
  manual overlay, then `inventory.json`, `roi_prefill.json`, `gap-report.md`,
  `gap-report.json` and `methodology.md`.
- `check` — read a prefill back and answer the harsher question: if you filed this
  today, what would come back? Exits non-zero on findings with `--fail-on`.
- `export` — an xBRL-CSV report package, built against the official EBA sample package
  and annotated template.
- `overlay init` — a `vendors.yaml` skeleton, pre-filled with the providers a scan found
  and the fields only a human can supply.

#### Discovery

- Terraform / OpenTofu state, format version 4: managed resources only, regions never
  guessed. One file, a directory, a glob, or several at once.
- Remote state over S3 with per-source profile, assume-role and workspace, declared in a
  `sources.yaml`. Merging two workspaces into one register is refused rather than done
  quietly.
- DNS records — MX, CNAME, SPF `include:`, alias targets — matched on the registrable
  domain against a packaged vendor table. This is where most vendors actually live: on
  the estate this was field-tested against, nine of the ten found appeared in no
  `required_providers` block at all.
- AWS Organizations for the group structure, and Cost Explorer for annual expense over
  twelve whole months, scoped to the payer account the call was actually made from.
- Four channels for vendors that appear in no Terraform code at all, in the accounts an
  `aws:` block in `sources.yaml` names — a file that needs no `states:` key:
  - **AWS Marketplace billing**: the seller of record on the Marketplace charges. The
    only channel that produces a `FILLED` legal name and expense, because it is the only
    one that reads a legal entity AWS itself invoices. Two seller names that reduce to
    one vendor key are refused rather than resolved by picking one.
  - **Federated identity providers**: SAML metadata and OIDC issuer hostnames.
  - **Cross-account trust policies**: which outside AWS accounts hold standing
    `sts:AssumeRole` access. An account no table can name is declared as an unknown
    rather than given an invented vendor name, and a role whose principal is `*` — no
    account named at all — is reported as its own kind of finding rather than as
    nothing.
  - **EventBridge partner event sources**: SaaS integrations wired into an event bus,
    swept only in regions an AWS resource in the perimeter already named.
- Per account and per channel, the methodology note distinguishes read, read-and-found-
  nothing, refused, never attempted, and unconfirmed — the states that used to arrive as
  the same empty list.
- Kubernetes: image registries, `ExternalName` services, ingress hosts.

#### The register

- Row models, field catalog and closed lists for all fifteen templates, keyed by the
  official field codes.
- Every value carries a provenance — `FILLED`, `INFERRED` or `MISSING` — with its source
  and, where it matters, a note saying what the inference assumed. A provenance is never
  stronger than its weakest input.
- Gap report over all fifteen templates, blocking gaps first, in markdown and JSON.
- A methodology note written for whoever audits the register: what was read, **what was
  deliberately not read**, how vendors were identified, and what the register asserts on
  what basis.
- Pre-flight checks encoding the failure modes observed in the 2025 first filing.
- GLEIF enrichment where enabled. A match counts as confirmed only when the legal name
  GLEIF returned matches the one that was searched — its name filter is a search, not an
  equality test.

#### Domain data

Field codes, names, counts and closed lists are reconciled against the official EBA
files vendored in `tests/fixtures/eba/`, from two independent sources, on every run of
the test suite. That reconciliation found real drift: five templates had the wrong field
count, one had a field whose meaning was wrong, and B_99.01 turned out to be an entirely
different template from what the drafting notes described.

Exported packages are validated locally against the official EBA taxonomy with Arelle.
Nothing is uploaded anywhere.

#### Not in this release

No filing, no submission portal, no compliance claim, and no assessment generation —
criticality, substitutability and exit plans are judgements, and the tool only gives
them somewhere to live.
