# openIMIS Backend Analytics Module

This module provides self-service analytics capabilities for openIMIS, allowing users to query, filter, aggregate, and export data from various entities.

## Features

- **Visual Query Builder**: Intuitive interface for building complex queries without SQL knowledge
- **Real-time Filtering**: Dynamic filtering across multiple dimensions
- **Data Aggregation**: Support for SUM, COUNT, AVG, MIN, MAX operations
- **Interactive Dashboards**: Pre-built and custom dashboards
- **Export Options**: Excel and CSV export
- **Role-based Access**: Granular permissions for data access

## Supported Entities

- Individuals
- Groups
- Beneficiaries
- Group beneficiaries
- Payments
- Grievances (Tickets)

## Configuration

The module supports the following configuration options:

- `analytics_max_export_rows`: Maximum rows for export (default: 100,000); a larger export is refused
- `analytics_max_query_rows`: Maximum rows returned on screen (default: 10,000)
- `analytics_cache_ttl`: Cache time-to-live in seconds (default: 300)

## Permissions

- `200001` - View analytics dashboards (widget data included)
- `200002` - Run custom queries
- `200003` - Export data
- `200004` - Save the layout of one's own dashboards
- `200005` - Make a saved query public
- `200006` - Save a query
- `200007` - Edit or delete one's own saved queries

Query results are scoped to the user: soft-deleted rows are excluded, location
row security applies to individuals, groups, beneficiaries and payments, and
grievance tickets require the grievance read right and follow its category and
flag access rules.

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