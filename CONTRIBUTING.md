# Contributing to dora-roi

By taking part you agree to the [code of conduct](CODE_OF_CONDUCT.md). If you think you
have found a security issue, read [SECURITY.md](SECURITY.md) first — some of them should
not be filed in public.

Thanks for looking. The most valuable contribution to this project is small and
specific: **a provider added to the mapping table**. More on that below.

## Ground rules

Three of them, and they are not negotiable, because the project is useless the moment
it stops being true about its own output.

1. **Provenance discipline.** Every generated value carries a status. `FILLED` requires
   an authoritative source — a GLEIF exact match, the user's overlay, a billing API.
   Anything derived from the provider mapping or from reading state is `INFERRED`,
   never `FILLED`. If a change would let a guess be reported as a fact, it will not be
   merged.
2. **Read-only.** Collectors never mutate anything. AWS calls are `List*`, `Describe*`
   and `Get*` only. No `terraform apply`, no `kubectl apply`, no writes to state.
3. **No compliance claims.** Neither the code, nor the output, nor the docs may state
   or imply that using this tool makes anyone DORA-compliant. It produces hypotheses
   and gap analyses.

Two more that matter almost as much:

4. **S01–S19 is the user's call.** The mapping ships suggestions, clearly labelled and
   always overridable. Never hard-code a classification as authoritative.
5. **Declare the perimeter.** Reports say what was scanned. Never imply completeness —
   shadow IT is invisible to this tool and pretending otherwise is the most damaging
   thing it could do.

## Everything runs in Docker

Not against a host virtualenv. The image pins the lowest supported Python, so a check
that passes here passes in CI, and the reverse.

```bash
docker compose build dev                      # after a uv.lock change
docker compose run --rm dev                   # tests
docker compose run --rm dev ruff check .      # lint
docker compose run --rm dev ruff format .     # format
docker compose run --rm dev mypy src          # types — blocking in CI
docker compose run --rm dev pytest --cov=dora_roi --cov-report=term-missing  # coverage, floor 93%
docker compose run --rm dev dora-roi scan -s tests/fixtures/sample.tfstate -o /app/out
```

To reproduce the other leg of the CI matrix:

```bash
PYTHON_VERSION=3.12 docker compose build dev && docker compose run --rm dev
```

## Adding a provider to the mapping — start here

`src/dora_roi/data/provider_mapping.yaml` maps a Terraform provider to a vendor, an HQ
country and suggested service codes. Every entry someone adds makes the tool useful for
the next person with that provider in their state. This is the growth loop; PRs here get
reviewed fastest.

```yaml
your_provider_name:          # exactly as it appears in `provider["registry/ns/NAME"]`
  vendor: Legal Name of the Entity
  hq_country: IE             # ISO 3166-1 alpha-2
  services: [S19, S06]       # from the S01-S19 closed list
  notes: >-
    Anything a reader needs to not be misled. Which entity an EU customer
    actually contracts with, whether self-hosting changes the codes, who the
    ultimate parent is.
```

What a good entry does:

- **Names the entity an EU customer contracts with**, not the best-known brand.
  `Google Cloud EMEA Limited` (IE), not "Google". DORA is EU law and the counterparty is
  what the register asks for.
- **Says in `notes` when the default is likely wrong for the reader.** Self-hosted vs
  managed changes the service code. A US-signed contract changes the entity. An
  acquisition changes the ultimate parent. Write it down.
- **Picks conservative service codes.** Two accurate codes beat five aspirational ones.
- **Does not invent an LEI.** The mapping carries no identification codes; GLEIF
  resolves those at scan time, and a wrong LEI is the single most common reason a real
  filing is rejected.

Add a test only if the entry exercises something new. The suite already checks that
every entry is complete and that its codes are in the closed list.

## Adding a vendor domain — also start here

`src/dora_roi/data/vendor_domains.yaml` maps a hostname suffix to a provider key.
It is how a DNS record becomes a discovered vendor, and it is thin by nature:
nobody can enumerate the SaaS the world uses. One line adds a vendor.

```yaml
mcsv.net: mailchimp     # the key must exist in provider_mapping.yaml
```

Two rules. The domain must be one the **vendor** controls, not one a customer
picks — `mcsv.net` is Mailchimp's, `acme.mailchimp.com` would be a customer's.
And the provider key must have an entry in `provider_mapping.yaml`, or the vendor
resolves to a name the register cannot use; there is a test asserting exactly
that.

## Changing the code

One change per pull request, and the tests come first: a pull request that adds
behaviour without a test that fails before it is not ready, whatever the behaviour.

Definition of done, all seven:

1. `docker compose run --rm dev ruff check .` clean
2. `docker compose run --rm dev` green, with new tests
3. `docker compose run --rm dev mypy src` clean — CI blocks on this
4. coverage at or above 93% — CI blocks on this too
5. the smoke run works, in the container
6. docs updated if behaviour changed
7. the ground rules above respected

Conventions: Python ≥ 3.11, `StrEnum` for closed lists, pydantic v2 with aliases set to
official field codes, typer + rich, one exception type per module raised with an
actionable message and chained with `raise … from e`. Conventional commits (`feat:`,
`fix:`, `docs:`, `test:`, `ci:`, `build:`).

Tests never touch the network. GLEIF is mocked with `httpx.MockTransport`, AWS with
`moto`.

## About the domain data

Field codes, counts and closed lists were first reconstructed from the ITS and secondary
sources, then reconciled against the official EBA files vendored in `tests/fixtures/eba/`.
The reconciliation runs in the test suite on every commit, from two independent official
sources, and asserts they agree with each other. It found real drift — see the note at the
end of the README.

One thing is still **not** verified that way: the mandatory flags. The annotated layout
does not carry them, so they remain inherited from the reconstruction and reported
separately from the EBA's own validation rules.

**If you find a discrepancy with the official EBA files, that is a valuable bug report.**
Cite the EBA document and the page or cell. Where the two disagree, the EBA files win.

## Reporting a bug

Include the command you ran and what happened. If it involves a state file, a **redacted**
excerpt of the resource that broke it is worth more than a description — but read it
first: a Terraform state can contain secrets in resource attributes. Never attach a real
state file.
