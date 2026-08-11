# ETL and machine-learning pipeline

Source: public repository README at
https://github.com/AdilRMallick/etl-ml-pipeline.

## Pipeline design

- Built a production-shaped Python workflow that ingests data, validates records, transforms features, trains a model, and emits reports.
- Accepted JSON files, JSONL files, and directories; skipped and logged malformed lines instead of aborting an entire batch.
- Split schema validation results into clean and rejected records with explicit rejection reasons.

## Reproducible machine learning

- Applied standard scaling and one-hot encoding while returning fitted transformation parameters for reproducibility.
- Used a Keras model when TensorFlow is available and a dependency-free baseline otherwise.
- Included pytest coverage, Docker and Docker Compose execution, and GitHub Actions checks on Python 3.10 and 3.11 with an end-to-end smoke test and container build.

## Bundled sample output

- The repository's documented sample contains 12 records, of which 10 pass validation and 2 are rejected, for a sample pass rate of 0.8333.
- The repository documents 0.95 training accuracy for its bundled example; this is a sample output rather than a production benchmark.
