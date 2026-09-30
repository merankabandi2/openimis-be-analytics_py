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

- `analytics_max_export_rows`: Maximum rows for export (default: 20,000); a larger export is refused.
  The export file is built in memory within the GraphQL request.
- `analytics_max_query_rows`: Maximum rows returned on screen (default: 10,000)
- `analytics_cache_ttl`: Cache time-to-live in seconds (default: 300)
- `analytics_query_timeout`: Seconds each database statement of a query, widget or
  export may run on PostgreSQL (default: 30). A statement running longer is cancelled
  and the caller gets "The query ran longer than N seconds and was stopped; narrow the
  filters". 0 keeps the connection's own `statement_timeout`.
- `analytics_field_allowlist`: per entity type, the columns and relation paths a
  non-superuser may use in `fields`, `filters`, `group_by`, `order_by` and aggregations,
  and the fields the query builder offers them. Default: `DEFAULT_FIELD_ALLOWLIST` in
  `analytics/apps.py`, which leaves out `json_ext` on every entity, `first_name`,
  `last_name` and `dob` on individuals, `code` on groups, `photo`, `receipt` and `code`
  on payments, and `title`, `description` and `reporter_id` on grievances. An entity
  given in the module configuration replaces its default list, the others keep
  theirs; a value that is not a list allows nothing on that entity. Only the entity's
  own columns and the relation paths already accepted by the module can be added.
  Superusers are not restricted. A refused field raises
  "Field 'X' is not allowed in <clause> for entity '<entity>'", and a query without
  `fields` returns only the allowed columns.

Filters `contains`, `startswith` and `endswith` are refused on JSON fields (`json_ext`):
they cast every document of the table to text and no index serves them. `isnull`,
`is_not_null` and the other operators remain accepted there.

Export files are written to `MEDIA_ROOT/analytics_exports/` and served only by the download endpoint, which checks the export right. `MEDIA_ROOT` must be on persistent storage and must not be served directly by a web server.

## Permissions

- `803001` - View analytics dashboards (widget data included)
- `803002` - Run custom queries
- `803003` - Export data
- `803004` - Save the layout of one's own dashboards
- `803005` - Make a saved query public
- `803006` - Save a query
- `803007` - Edit or delete one's own saved queries

Query results are scoped to the user: soft-deleted rows are excluded, location
row security applies to individuals, groups, beneficiaries and payments, and
grievance tickets require the grievance read right and follow its category and
flag access rules.

## GraphQL Queries

### executeAnalyticsQuery
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