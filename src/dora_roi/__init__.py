"""dora-roi: prefill the DORA Register of Information from infrastructure-as-code.

The package is deliberately layered:

* ``collectors``  discover what you actually run (tfstate, AWS, Kubernetes).
* ``enrichment``  turn discoveries into third-party provider facts (mapping, GLEIF).
* ``overlay``     let a human assert the things no scanner can know (contracts, LEIs).
* ``models``      the Register of Information itself, with per-field provenance.
* ``report``      what is still missing, ranked by whether it blocks a filing.
* ``export``      the regulator-facing artefacts (xBRL-CSV) and their pre-flight checks.
"""

__version__ = "0.1.0"

__all__ = ["__version__"]
