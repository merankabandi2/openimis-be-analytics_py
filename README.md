# openIMIS Backend Analytics Module

This module provides self-service analytics capabilities for openIMIS, allowing users to query, filter, aggregate, and export data from various entities.

## Features

- **Visual Query Builder**: Intuitive interface for building complex queries without SQL knowledge
- **Real-time Filtering**: Dynamic filtering across multiple dimensions
- **Data Aggregation**: Support for SUM, COUNT, AVG, MIN, MAX operations
- **Interactive Dashboards**: Pre-built and custom dashboards
- **Export Options**: Excel, CSV, and PDF export capabilities
- **Role-based Access**: Granular permissions for data access

## Supported Entities

- Individuals
- Groups
- Beneficiaries
- Payments
- Grievances (Tickets)

## Configuration

The module supports the following configuration options:

- `analytics_max_export_rows`: Maximum rows for export (default: 100,000)
- `analytics_cache_ttl`: Cache time-to-live in seconds (default: 300)
- `analytics_enable_sql_queries`: Allow raw SQL queries (default: False)

## Permissions

- `200001` - View analytics dashboards
- `200002` - Create custom queries
- `200003` - Export data
- `200004` - Save custom dashboards
- `200005` - Share dashboards with others

## GraphQL Queries

### analyticsQuery
Execute a custom analytics query with filters and aggregations

### analyticsDashboards
List available dashboards

### analyticsExport
Export query results in various formats

## Installation

```bash
pip install openimis-be-analytics
```

## Usage

The module automatically registers its GraphQL schema and provides REST endpoints for data export.