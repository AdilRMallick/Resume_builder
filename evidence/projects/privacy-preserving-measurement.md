# Privacy-preserving federated measurement

Source: public repository README at
https://github.com/AdilRMallick/Privacy-Preserving-Measurement.

## Differentially private federated learning

- Implemented differentially private federated averaging on MNIST with 100 simulated clients and 10 sampled clients per training round.
- Kept raw client data decentralized while aggregating clipped model updates with Gaussian noise.
- Added a Rényi differential privacy accountant and privacy-utility sweeps across noise settings.

## Documented experiment

- The repository's example 50-round run with noise multiplier 1.1 reports sample final accuracy of 0.9102 and epsilon of 4.871 at delta `1e-5`.
- The documented experiment is a repository demonstration of the privacy-utility tradeoff, not a production result.
