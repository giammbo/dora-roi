# Security policy

## Reporting a vulnerability

Use GitHub's private vulnerability reporting: **Security → Report a vulnerability** on
[this repository](https://github.com/giammbo/dora-roi/security/advisories/new). It opens
a private thread that only the maintainers can see.

Please do not open a public issue for anything in the list below. For everything else, a
normal issue is fine and faster.

Expect a first reply within a week. There is no bounty programme — this is a project run
in the open by one maintainer, and the honest answer is that a fix depends on how much
time the report needs.

## What counts, for this project specifically

`dora-roi` reads Terraform state, cloud APIs and Kubernetes for a living. That is an
unusually sensitive diet, and it gives the project three security properties that are
part of its contract rather than nice-to-haves. Anything that breaks one of them is a
vulnerability here, even if it would be a bug elsewhere.

**1. It is read-only, structurally.** Every AWS call goes through a guard that refuses
anything which is not a `List*`, `Describe*`, `Get*` or `Search*` *before it reaches the
network*. There is no `terraform apply`, no `kubectl apply`, no write to state, no
acquired lock. A path that reaches a mutating API — however it gets there, and even if
nobody would call it by accident — is a vulnerability.

**2. It does not let your secrets escape.** Terraform state routinely carries plaintext
credentials as resource attributes: database passwords, access keys, private keys. The
tool copies remote state to a scratch directory, parses it, and deletes the copy. If you
find a way to get any of that into `inventory.json`, `roi_prefill.json`, the gap report,
the terminal output, an exception message, a log line, or a GLEIF query — that is a
vulnerability, and a serious one.

**3. Input is hostile until parsed.** A state file, a `sources.yaml`, a mapping or an
overlay may come from somewhere you do not control. Code execution, a path traversal
that writes outside the output directory, a zip that escapes its extraction root, or a
YAML load that constructs arbitrary objects: all vulnerabilities.

Also in scope: a crafted `--sources` entry that makes the tool read a bucket or assume a
role the user did not ask for, and anything that sends data to a host other than
`api.gleif.org` and the endpoints of the cloud accounts you pointed it at.

## What does not count

- **The tool does not make you DORA-compliant.** That is stated everywhere on purpose,
  and it is not a vulnerability.
- **`INFERRED` values are wrong sometimes.** That is what the label means. A bad
  *hypothesis* is a bug report or a mapping fix. A hypothesis reported as `FILLED` is a
  correctness bug worth reporting publicly — provenance discipline is a ground rule, not
  a security boundary.
- **Shadow IT is invisible.** The tool sees infrastructure-as-code and cloud APIs. A
  vendor it cannot discover is a declared limitation, not a flaw.
- Findings in the vendored EBA files under `tests/fixtures/eba/`. They are published
  documents, reproduced with their provenance; report those to the EBA.

## Supported versions

Pre-1.0. Fixes land on `main` and go out in the next release; there are no backports.
Run the latest tag.

## Handling reports

A confirmed vulnerability gets a GitHub Security Advisory, a fix on `main` with a
regression test, and a release. Reporters are credited by name unless they would rather
not be.
