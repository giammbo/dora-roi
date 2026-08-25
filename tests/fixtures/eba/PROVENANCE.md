# Official EBA reference files

Vendored so the exporter and its golden test can be built against the official
files rather than against this repository's reconstructed tables — golden rule 6.
Downloaded 2026-08-23 from the EBA page *Preparations for reporting of DORA
registers of information*.

| File | Source | sha256 of the archive it came from |
|---|---|---|
| `sample_dora_package.zip` | [sample_documents.zip](https://www.eba.europa.eu/sites/default/files/2024-12/f4519b45-d6c2-4e7d-a8d4-4bee91a9c530/sample_documents.zip) → `instances xBRL-CSV.zip` → `DUMMYLEI123456789012.CON_FR_DORA010100_DORA_2024-12-31_20241213174803429.zip` | `2183cdd27898d6ed95de2962b0ae4c54b4037ccb9ff668b00cf9db15d0d8ecac` |
| `annotated_dora_4.0.xlsx` | [annotated_templates.zip](https://www.eba.europa.eu/sites/default/files/2024-12/7ae0363a-ad3d-42d9-a192-34711416c039/annotated_templates.zip) → `20241217 Annotated Table Layout  DORADORA 4.0.xlsx` | `3b973e7e9dd06f9d855b9afe77a930b3881a3a84e64292c90f0ef8d397a5e57d` |
| `possible_values.xlsx` | [List of possible values…](https://www.eba.europa.eu/sites/default/files/2025-03/213f539f-0742-44a8-8657-a5c4ecb0a202/List%20of%20possible%20values%20for%20all%20data%20fields%20with%20drop%20downs%20%28updated%203%20March%202025%29%20.xlsx) (3 March 2025) | `a0fed557cc6837f909227088338408581cd413cce046566dcc7b4253ba009c9f` |
| `validation_rules.xlsx` | [EBA Validation Rules](https://www.eba.europa.eu/sites/default/files/2025-03/de521052-1069-4e43-a08b-43aaeb938a35/EBA%20Validation%20Rules%202025-03-20%20deactivation.xlsx) (20 March 2025) | `ac82e6e9d3903a3519dcda9370108db478b5a33968d5b1dca0e46bff01246343` |

The sample's own readme warns that its **data** is random and does not obey the
validation rules. Only its **structure** is authoritative, and structure is all
the golden test asserts on.

## Taxonomy, for validation

`test_xbrl_validation.py` runs [Arelle](https://arelle.org), the reference XBRL
processor, against an exported package. It needs the official taxonomy, which is
18 MB and therefore **not vendored** — without it those two tests skip, and a plain
`docker compose run --rm dev` stays fast and offline.

```bash
curl -sSL -o tests/fixtures/eba/taxo_package_4.0.zip \
  "https://www.eba.europa.eu/sites/default/files/2025-03/729fe4f5-bbcc-495d-b520-8ad5cbeeead0/taxo_package_4.0_errata5.zip"
```

sha256 `2cf8a0fe6aadee36a5bf07403d7bd63a43cab96f98fad8251c31938e8242e2fb`.

Worth the download when you touch the exporter: run for the first time, it found
four bugs the rest of the suite could not see.

## Not vendored

`sample_documents.zip` and `annotated_templates.zip` (4.8 MB of COREP, MICA and IF
this project never reads) and the taxonomy above. Re-fetch from the URLs here.
