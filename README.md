# dora-roi

**Prefill the DORA Register of Information from your infrastructure-as-code, and get a
straight answer about what is still missing.**

[![CI](https://github.com/giammbo/dora-roi/actions/workflows/ci.yml/badge.svg)](https://github.com/giammbo/dora-roi/actions/workflows/ci.yml)
[![License](https://img.shields.io/badge/license-Apache--2.0-blue.svg)](LICENSE)
[![Python](https://img.shields.io/badge/python-3.11%20%7C%203.12-blue.svg)](pyproject.toml)

Every EU financial entity in DORA scope has to maintain and file a Register of
Information: 15 relational templates covering entities, contracts, providers, supply
chains, functions and assessments, under Commission Implementing Regulation (EU)
2024/2956.

It goes badly. In the ESAs' 2024 dry run **6.5% of submitted registers passed all
data-quality checks**, and **86% of the data errors were missing mandatory
information** — dominated by provider identifiers and invalid or missing LEIs. A
Deloitte survey found the register is the single hardest DORA task for **46%** of
entities.

The organisations that filed in hours had a queryable asset inventory. The ones that
struggled for weeks had spreadsheets. If you are cloud-native you already have the
inventory — it is called Terraform state — and nothing reads it for this purpose.

`dora-roi` does.

## Quickstart

No account, no key, no network. Two ways in; pick one.

**Docker** — how the project itself is built and verified:

```bash
git clone https://github.com/giammbo/dora-roi && cd dora-roi
terraform state pull > /tmp/infra/terraform.tfstate     # your state, read-only
DORA_WORKDIR=/tmp/infra docker compose run --rm dora-roi scan -s /work/terraform.tfstate -o /out
```

**Or install the CLI** (not on PyPI yet — install from the repository):

```bash
uv tool install git+https://github.com/giammbo/dora-roi
dora-roi scan -s terraform.tfstate -o out/
```

More than one stack? `-s` takes a file, a **directory** (searched recursively for
`.tfstate` files) or a glob, and repeats:

```bash
dora-roi scan -s infra/ -o out/                    # every stack under infra/
dora-roi scan -s prod.tfstate -s staging.tfstate -o out/
```

**State in S3, across accounts?** That is the normal case, and exporting five
backends by hand before you can start is the friction this removes. List them
once, with the credentials each one needs:

```yaml
# sources.yaml
states:
  - uri: s3://acme-tfstate-prod/          # every stack under the prefix
    profile: prod
  - uri: s3://acme-tfstate-payments/network/terraform.tfstate
    role_arn: arn:aws:iam::222222222222:role/dora-roi-readonly
  - ./local/sandbox/                       # local paths work here too
```

```bash
dora-roi scan --sources sources.yaml -o out/
```

Credentials are **per source**, not global: five buckets in five accounts need
five sets, and a single profile would quietly read the ones it could and skip
the rest — a register that looks complete and is not. Objects are copied to a
scratch directory, read, and never written back; the report names the `s3://`
URI it came from, not the temporary path it landed in.

A provider found in several states is one provider with the weight of all of them,
and the report names every file it read — never the directory it found them in.

Look before you leap:

```bash
dora-roi providers -s terraform.tfstate
```

```
3 provider(s) discovered
┏━━━━━━━━━━━━┳━━━━━━━━━━━━┳━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━┳━━━━━━━━━━┳━━━━━━━━━━━┳━━━━━━━━━━━━━━━━━━━━━━━┓
┃ Provider   ┃ Namespace  ┃ Suggested vendor              ┃ Services ┃ Resources ┃ Regions               ┃
┡━━━━━━━━━━━━╇━━━━━━━━━━━━╇━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━╇━━━━━━━━━━╇━━━━━━━━━━━╇━━━━━━━━━━━━━━━━━━━━━━━┩
│ aws        │ hashicorp  │ Amazon Web Services EMEA SARL │ S17, S18 │         5 │ eu-south-1, eu-west-1 │
│ datadog    │ DataDog    │ Datadog, Inc.                 │ S19, S06 │         2 │ —                     │
│ cloudflare │ cloudflare │ Cloudflare, Inc.              │ S11, S04 │         1 │ —                     │
└────────────┴────────────┴───────────────────────────────┴──────────┴───────────┴───────────────────────┘
```

Add `--gleif` to resolve LEIs against the free GLEIF API (throttled to one request per
second, no key needed). It is **off by default**: a tool you might put in CI should not
call out on first run without being asked.

## What you get

`scan` writes four files:

| File | What it is |
| --- | --- |
| `inventory.json` | Raw discovery: providers, resource types, regions, and which state files were read. |
| `roi_prefill.json` | The register itself, keyed by **official field codes** (`"0010"`, `"0020"`, …), each value paired with its provenance. |
| `gap-report.md` | For humans. Blocking gaps first. |
| `gap-report.json` | For CI. `dora-roi check --fail-on blocking` is the gate. |
| `methodology.md` | For an auditor. Date, sources, **what was deliberately not read**, method, and how much of the register is asserted versus guessed. |

The gap report is the point. A half-filled register is not useful on its own; knowing
precisely which half is missing, and which part of that half stops a filing, is:

```
**0 filled · 33 inferred · 137 missing** across 170 fields in 15 templates.
**68 of the missing ones block a filing.**
```

## Every value says where it came from

No value in the output is anonymous. This is the discipline the whole tool is built
around, because a register you cannot audit is a register you cannot defend.

| Status | Meaning | Where it comes from |
| --- | --- | --- |
| `FILLED` | An authoritative source said so. | GLEIF exact match, your manual overlay, a billing API. |
| `INFERRED` | A hypothesis. **Review before filing.** | The provider mapping, discovery from state, defaults. |
| `MISSING` | Nothing could produce a value. | The gap report tells you whether it blocks a filing. |

Anything derived from the provider mapping or from reading your state is `INFERRED`,
never `FILLED`. A fuzzy GLEIF match stays `INFERRED` and records what was searched
against what was found. There is no path by which a guess acquires the same badge as a
confirmed fact.

## What it does **not** do

Read this part before the rest.

- **It does not make you compliant, and it never will claim to.** It produces
  hypotheses and a gap analysis. That is the whole product.
- **It does not file anything.** No submission portal, no regulator integration.
- **The blind spot does not shrink — only how much of the visible estate this tool can
  read does.** Four channels now look for vendors with no Terraform footprint at all: AWS
  Marketplace billing, federated identity providers, cross-account trust relationships,
  and EventBridge partner event sources — the last three only inside accounts a
  `--sources` file names to sweep. A vendor that touches none of those four — paid on a
  personal or company card, no billing line, no IAM trust, no event integration — is
  exactly as invisible as it always was, and now sits next to vendors the tool *can* see,
  which makes the gap easier to miss, not smaller. Every command prints exactly what was
  scanned, and the methodology note states, per channel and per account, whether it was
  read, read and found nothing, or refused — an empty result and a refusal are opposite
  claims and this tool never lets them look the same.
- **It does not classify your services for you.** The S01–S19 code a provider gets is
  your regulatory responsibility. The packaged mapping suggests; you decide.
- **It does not know your contracts.** Reference numbers, dates, notice periods,
  governing law, criticality assessments, exit plans — no scanner can derive these.
  They arrive via the manual overlay (phase 2) and are the reason the gap report exists.
- **It is not legal advice.**
- **It is read-only, and structurally so.** Every AWS call goes through a guard that
  refuses anything which is not a `List*`, `Describe*`, `Get*` or `Search*` **before it
  reaches the network** — writing `put_object` fails at the call, not in review. Reading
  state from S3 uses `GetObject` and `ListObjectsV2` and nothing else, and never touches
  the DynamoDB lock table, so a scan cannot block a `terraform apply` running beside it.
  In the shipped container your state directory is mounted read-only as well.

  There are tests for all of it, including one that snapshots every object in a bucket,
  runs a full fetch, and asserts the bucket is byte-identical afterwards.

  Two narrow, declared exceptions: fetching state from S3 with a `role_arn`, and sweeping
  an account for vendor-discovery evidence with one, both call `sts:AssumeRole` to get
  there. Neither goes through the guard above, and neither is a read. Both are defensible
  on the same reasoning: assuming a role changes nothing in the target account, and every
  call made *through* the session it returns still goes through the guard exactly like
  every other credential this tool uses.

## Your DNS zone is a confession

A `required_providers` block lists the APIs your Terraform talks to. Your DNS
records list who you actually depend on, and the two are not the same estate.

An MX record names who reads your mail. A CNAME names who serves a subdomain. An
SPF `include:` names everyone allowed to send as you. `dora-roi` reads all of
them out of the state it already has:

```
aws_route53_record  MX    → aspmx.l.google.com          → Google Workspace
aws_route53_record  TXT   → include:spf06.hubspotemail  → HubSpot
aws_route53_record  CNAME → dkim2.mcsv.net              → Mailchimp
aws_route53_record  CNAME → ext.teamtailor.com          → Teamtailor
```

That example is real. On the estate it came from, Terraform declared exactly one
provider — `hashicorp/aws` — and the DNS records in the same state file named
eight more, most of them handling personal data. A register built from the
provider list alone would have been short by eight counterparties, and looked
complete.

Matching is on the registrable domain and never a substring: `evil-hubspot.net`
is not HubSpot. The table is
[`vendor_domains.yaml`](src/dora_roi/data/vendor_domains.yaml) — **adding a
domain to it is the most useful thing you can contribute.**

## The provider mapping

The packaged table maps 26 common Terraform providers to a vendor, an HQ country and
suggested S-codes. It is wrong for somebody in two structural ways, and says so at the
top of the file:

- **The contracting entity.** Defaults name who an EU customer usually contracts with,
  because DORA is EU law — AWS EMEA SARL in Luxembourg, not Amazon Web Services, Inc.
  If you contract with the US parent or through a reseller, the default is wrong for you.
- **The service codes.** A provider is listed with the codes its typical use attracts.
  One archival bucket is not a full AWS estate.

Override with your own YAML. The merge is per key, so this keeps the vendor, the
services and the notes from the defaults:

```yaml
# vendors-map.yaml
aws:
  hq_country: US        # we contract with the US parent
snowflake:
  services: [S09]       # we only use it for archival storage
acme:                   # a provider the packaged table has never heard of
  vendor: Acme Cloud SpA
  hq_country: IT
  services: [S19]
```

```bash
dora-roi scan -s terraform.tfstate -m vendors-map.yaml -o out/
```

**Adding a provider to the packaged mapping is the single most useful contribution you
can make.** There is an issue template for exactly that.

## What dora-roi is allowed to do to your AWS account

Nothing, beyond one declared exception. Grant exactly this and no more — every
action but `sts:AssumeRole` is a read, and the code refuses to issue anything
else before the call leaves the process:

```json
{
  "Version": "2012-10-17",
  "Statement": [{
    "Effect": "Allow",
    "Action": [
      "ce:GetCostAndUsage",
      "events:ListEventSources",
      "iam:GetOpenIDConnectProvider",
      "iam:GetSAMLProvider",
      "iam:ListOpenIDConnectProviders",
      "iam:ListRoles",
      "iam:ListSAMLProviders",
      "organizations:DescribeOrganization",
      "organizations:ListAccountsForParent",
      "organizations:ListOrganizationalUnitsForParent",
      "organizations:ListRoots",
      "s3:GetObject",
      "s3:ListBucket",
      "sts:AssumeRole",
      "sts:GetCallerIdentity"
    ],
    "Resource": "*"
  }]
}
```

Scope the two `s3:` actions to your state buckets and nothing wider. Drop them entirely if you never point `--sources` at S3.

`sts:AssumeRole` is needed only for the `assume_role_name` multi-account sweep
shortcut and the S3 `role_arn` option, both described above — list every account
with its own `profile` instead of relying on the shortcut and you can drop it
from the policy too.

This list shrank as well as grew. Seven actions the previous policy granted are
gone because no line of code ever called them: `ce:GetTags`, `tag:GetResources`,
`tag:GetTagKeys`, `resource-explorer-2:Search`, `resource-explorer-2:ListViews`,
`iam:ListUsers`, and `organizations:ListAccounts` (a different, broader
Organizations call than the one the account walker actually makes,
`ListAccountsForParent`, from the root down). `sts:GetCallerIdentity`,
`iam:ListSAMLProviders`, `iam:GetSAMLProvider`, `iam:ListOpenIDConnectProviders`,
`iam:GetOpenIDConnectProvider` and `events:ListEventSources` are new: the first
labels which account a Cost Explorer figure was billed to, the rest back the
identity-provider, trust-relationship and EventBridge-partner channels described
under "What it does **not** do" above. A test
(`tests/test_iam_policy.py`) parses this JSON block and asserts its action set
equals `dora_roi.collectors.aws.REQUIRED_IAM_ACTIONS` exactly, so the two
cannot drift apart again the way they already had.

The guarantee is not a promise in a document. Every AWS call but the declared
`sts:AssumeRole` exception goes through a wrapper that rejects any operation
which is not a `List*`, `Describe*`, `Get*` or `Search*`, and there is a test
asserting that `create_account` raises rather than executes.

## More sources, and the things only you know

Terraform state is the starting point, not the whole picture.

```bash
dora-roi scan -s terraform.tfstate --aws --k8s-context prod -o out/
```

`--aws` reads Organizations for the group perimeter and Cost Explorer for the real
annual spend per provider — twelve trailing whole months, with the payer accounts it
covers recorded alongside the number. `--k8s` reads a cluster for image registries,
`ExternalName` services and ingress hosts. Both are read-only, both degrade to a
warning if unreachable, and neither invents a vendor it cannot identify.

None of them can tell you your contract reference, your notice period, your governing
law, or whether an exit plan exists. That is what the overlay is for:

```bash
dora-roi overlay init -o out/          # a commented vendors.yaml, seeded from the scan
dora-roi scan -s terraform.tfstate --overlay vendors.yaml -o out/
```

Everything you uncomment there is recorded as `FILLED, source=overlay` and beats every
machine guess. Everything you leave commented stays a gap the report keeps showing you,
which is more useful than a guess repeated back at you.

## Filing

```bash
dora-roi export -o out/ --check          # what would come back, writing nothing
dora-roi export -o out/                  # the xBRL-CSV package, if it passes
```

`export` writes an official report package — `<LEI>.CON_<CC>_DORA010100_DORA_<date>_<ts>.zip`
— and **pre-flight runs first**. Blocking findings stop it: a package that is well-formed
and wrong is worse than no package, because it looks filed. `--force` exists and says so
in the output.

Three things the exporter gets right only because it was built against the EBA's own
sample rather than against a reading of the spec: headers carry a `c` prefix (`c0010`),
closed-list values are emitted as QNames (`eba_TA:S17`, never "Cloud services: IaaS"), and
the archive wraps everything in one directory named like the zip.

```bash
dora-roi check -o out/ --fail-on blocking
```

`check` asks a harsher question than the gap report: if you filed this today, what would
come back? Each check encodes a failure mode from the 2025 first filing — missing and
invalid LEIs, contracts aggregated instead of one record per arrangement, sub-outsourcing
left empty for a hyperscaler, reference dates that disagree across templates.

Two severities. **BLOCKING** means a filing built from this register is expected to be
refused — including, deliberately, any `ARR-…` reference this tool generated and nobody
replaced. **WARNING** means it would be accepted and still be wrong; an unreviewed
`INFERRED` value is the clearest case, because nothing rejects it and nobody but you
knows it was a guess.

`--fail-on blocking` makes it a CI gate.

## Roadmap

**Phase 1 — done.** Terraform/OpenTofu state → provider discovery → mapping → optional
GLEIF LEI resolution → register prefill → gap report over all 15 templates.

**Phase 2 — done.** AWS Organizations and Cost Explorer, Kubernetes discovery, the
`vendors.yaml` overlay, row models for all 15 templates, pre-flight validation, and the
xBRL-CSV exporter — built against the official EBA sample package and annotated
template, both vendored in `tests/fixtures/eba/`, with a golden test that compares every
table header against the sample on each run.

**Phase 3.** A NIS2 supplier register from the same inventory, continuous mode
(`dora-roi scan` in CI, diffed against the previous run to catch a "major change"),
GCP and Azure collectors, remote-state fetchers.

## Development

Everything runs in the project's image — never against a host virtualenv. See
[CONTRIBUTING.md](CONTRIBUTING.md).

```bash
docker compose run --rm dev                 # tests
docker compose run --rm dev ruff check .    # lint
docker compose run --rm dev mypy src        # types
```

## A note on the domain data

The field codes, names, counts and closed lists in this repository **are reconciled
against the official EBA files** — the annotated table layout, the sample xBRL-CSV
package, the list of possible values and the validation rules, all vendored in
`tests/fixtures/eba/` with their provenance. The reconciliation is not a one-off: the
test suite re-derives it from those files on every run, from two independent sources, and
asserts the two agree with each other.

It found real drift, which is the point. Five templates had the wrong field count, one
had a field whose *meaning* was wrong, and one — B_99.01 — turned out to be an entirely
different template from what the drafting notes described. Every correction is recorded
field by field, beside the count it changed, in `tests/test_templates.py`.

What is still **not** verified: the mandatory flags. The annotated layout does not carry
them, so `dora-roi` reports two things separately — its own inherited flags, and the
columns an *active* EBA validation rule requires to be non-null, at the EBA's own
`warning` severity.

## License

Apache-2.0. See [LICENSE](LICENSE).
