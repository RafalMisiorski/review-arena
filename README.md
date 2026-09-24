# review-arena

This repository hosts a **pre-registered benchmark of AI pull-request reviewers**. `src/requests/` is a verbatim copy
of [psf/requests](https://github.com/psf/requests) at commit `4c800e9aea2059660b8306b0fc8f9e9a4232cb3e` (Apache-2.0; see LICENSE and NOTICE). Pull
requests on this repository are benchmark items: some contain exactly one real single-line defect, some contain none.
The ground truth was sealed (`key.sealed`, sha-256 over salt + key) and committed to `main` **before** the first pull
request was opened; the key is revealed only after every reviewer under test has reviewed. Protocol, results and the
scoring code are published from the pre-registration in the operator's measurement ledger.

Please do not merge these pull requests; they exist to be reviewed, not shipped.
