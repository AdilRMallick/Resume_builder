# Geospatial API platform

Source: public repository README at
https://github.com/AdilRMallick/geo-api-platform.

## End-to-end data path

- Built a full-stack geospatial system with a Python ingestion pipeline, an Express REST API, MongoDB GeoJSON data and `2dsphere` indexes, and a React dashboard.
- Normalized longitude and latitude fields or existing GeoJSON input, validated coordinate ranges, and reported rejected records during ingestion.
- Implemented search, filtering, pagination, geospatial-near queries, statistics, and CRUD operations.

## API and user interface quality

- Added structured JSON logging, centralized error handling, and input validation to the Node.js API.
- Built the React interface from reusable components and debounced search input to avoid unnecessary requests.
- Packaged the system with Docker Compose and routed frontend API traffic through an Nginx proxy.
