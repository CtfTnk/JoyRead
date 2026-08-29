# Change Reports

One document per significant change, written so someone who was not involved can
understand what happened and why without reading the diff.

Reports are point-in-time records. They are **not** maintained as the code
evolves — for current behaviour, read `docs/technical/`. A report explains the
reasoning, the measurements, and the alternatives that were rejected, which the
code and its comments cannot carry on their own.

Naming: `YYYY-MM-DD-short-topic.md`.

Write one with the `change-report` skill (`skills/change-report/SKILL.md`).

| Report | Topic |
| --- | --- |
| [2026-08-10](2026-08-10-launch-ownership-and-pdf-session.md) | macOS launch determinism, window ownership, PDF session rewrite |
| [2026-08-12](2026-08-12-bulk-archive-conversion.md) | Bulk conversion for solid archives, and the cache-publication bugs it exposed |
| [2026-08-13](2026-08-13-p2-bulk-conversion-validation-zh.md) | P2 固实 7z 批量转换的中文实施说明与独立核验 |
| [2026-08-18](2026-08-18-production-logging-runtime.md) | Asynchronous structured logging, operation correlation, privacy, and lifecycle failure reporting |
| [2026-08-19](2026-08-19-logging-refurbishment-review.md) | Independent SE-tester review of the logging refurbishment: verified claims, 9 defects, and measured throughput |
| [2026-08-29](2026-08-29-packaged-startup-and-windows-msi.md) | Packaged startup import boundaries, explicit bundle trimming, and reproducible Windows MSI |
