# EV Grid Flexibility Platform

Sources: résumé and public repository README at
https://github.com/AdilRMallick/ev-grid-platform.

## Product and architecture

- Built a full-stack platform prototype connecting residential EV drivers, utilities, and OEM telematics providers for managed charging, demand response, and V1G/V2G grid events.
- Used React 18 and TypeScript for driver and utility interfaces, Spring Boot 3 with Java 17 for the core API, Kotlin for OEM adapters, and PostgreSQL with Flyway for persistence.
- Modeled driver and vehicle enrollment, charging sessions, grid-event dispatch, participation, energy shifted, and data quality.

## OEM integration

- Implemented a Kotlin provider simulator for Tesla, Ford, and SmartCar-style APIs with divergent JSON payloads, authentication behavior, error responses, and runtime fault injection.
- Used a pluggable OEM adapter interface to normalize vehicle status, charging sessions, and enrollment handoffs across providers.

## Database performance

- Reduced report latency from 2.1 seconds to 95 milliseconds at 100,000 rows with an indexed PostgreSQL `GROUP BY` query.
- Benchmarked the reporting query against a real PostgreSQL Testcontainers instance rather than an in-memory database.

## Security, quality, and deployment

- Added JWT bearer authentication, role-based authorization, request validation, centralized error handling, automated tests, and OpenAPI documentation.
- Used JUnit 5, Mockito, MockMvc, Testcontainers, Vitest, and React Testing Library for backend and frontend tests.
- Designed deployment around Docker Compose locally and AWS ECS Fargate, RDS, S3, CloudWatch, and IAM for production infrastructure.
