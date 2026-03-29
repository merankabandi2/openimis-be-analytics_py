"""
OpenSearch query service for the analytics module.
Translates the QueryBuilder JSON config into OpenSearch DSL queries,
keeping the same input/output contract as the Django ORM QueryBuilderService.
"""
import logging
from typing import Dict, List, Any

logger = logging.getLogger(__name__)

# Maps FE entity types to OpenSearch index names
ENTITY_INDEX_MAP = {
    'individual': 'individual',
    'group': 'group_individual',
    'beneficiary': 'beneficiary',
    'payment': 'payroll',
    'grievance': 'ticket',
}

# Maps OpenSearch field types to the Django-style types the FE expects
OS_TYPE_MAP = {
    'keyword': 'CharField',
    'text': 'CharField',
    'date': 'DateField',
    'integer': 'IntegerField',
    'long': 'IntegerField',
    'float': 'FloatField',
    'double': 'FloatField',
    'boolean': 'BooleanField',
    'object': 'JSONField',
}

AGGREGATABLE_TYPES = {'keyword', 'integer', 'long', 'float', 'double', 'date', 'boolean'}


def get_opensearch_client():
    """Get the OpenSearch client from the django-opensearch-dsl connection pool."""
    from opensearchpy import connections
    return connections.get_connection('default')


def _to_os_field(field_name):
    """Convert FE field name (json_ext__sexe) to OpenSearch path (json_ext.sexe)."""
    return field_name.replace('__', '.')


def _to_fe_field(field_path):
    """Convert OpenSearch path (json_ext.sexe) to FE field name (json_ext__sexe)."""
    return field_path.replace('.', '__')


class OpenSearchQueryService:
    """Translates analytics query configs into OpenSearch DSL and executes them."""

    @classmethod
    def get_entity_fields(cls, entity_type: str) -> List[Dict]:
        """Read index mapping to discover available fields (including json_ext sub-fields)."""
        index = ENTITY_INDEX_MAP.get(entity_type)
        if not index:
            return []

        try:
            client = get_opensearch_client()
            mapping = client.indices.get_mapping(index=index)
            # Handle alias or direct index name
            index_data = mapping.get(index) or next(iter(mapping.values()), {})
            properties = index_data.get('mappings', {}).get('properties', {})
            return cls._parse_properties(properties, prefix='')
        except Exception as e:
            logger.error("Failed to read OpenSearch mapping for %s: %s", index, e)
            return []

    @classmethod
    def _parse_properties(cls, properties: dict, prefix: str) -> List[Dict]:
        """Recursively parse OpenSearch mapping properties into field descriptors."""
        fields = []
        for name, info in properties.items():
            full_name = f"{prefix}__{name}" if prefix else name
            field_type = info.get('type')

            if field_type:
                fields.append({
                    'name': full_name,
                    'type': OS_TYPE_MAP.get(field_type, 'CharField'),
                    'label': full_name.replace('__', ' › ').replace('_', ' ').title(),
                    'filterable': True,
                    'aggregatable': field_type in AGGREGATABLE_TYPES,
                })
            # Recurse into object/nested fields (e.g. json_ext, group, individual)
            if 'properties' in info:
                fields.extend(cls._parse_properties(info['properties'], prefix=full_name))

        return fields

    @classmethod
    def execute_query(cls, entity_type: str, query_config: Dict) -> List[Dict]:
        """Execute an analytics query against OpenSearch."""
        index = ENTITY_INDEX_MAP.get(entity_type)
        if not index:
            raise ValueError(f"Unknown entity type: {entity_type}")

        client = get_opensearch_client()
        body = cls.build_query(query_config)

        try:
            response = client.search(index=index, body=body)
        except Exception as e:
            logger.error("OpenSearch query failed on %s: %s", index, e)
            raise

        group_by = query_config.get('group_by', [])
        if group_by:
            return cls._parse_aggregation_response(response, query_config)
        return cls._parse_hits_response(response, query_config)

    # ── Query building ────────────────────────────────────────────

    @classmethod
    def build_query(cls, query_config: Dict) -> dict:
        """Build the OpenSearch request body from a query config."""
        filters = query_config.get('filters', {})
        group_by = query_config.get('group_by', [])
        aggregations = query_config.get('aggregations', {})
        selected_fields = query_config.get('fields', [])
        order_by = query_config.get('order_by', [])
        limit = query_config.get('limit', 1000)

        body: Dict[str, Any] = {
            'query': cls._build_bool_query(filters),
        }

        if group_by:
            body['size'] = 0
            body['aggs'] = cls._build_aggregations(group_by, aggregations)
        else:
            body['size'] = min(limit, 10000)
            if selected_fields:
                body['_source'] = [_to_os_field(f) for f in selected_fields]
            if order_by:
                body['sort'] = cls._build_sort(order_by)

        return body

    @classmethod
    def _build_bool_query(cls, filters: dict) -> dict:
        """Translate filter config to OpenSearch bool query."""
        if not filters:
            return {'match_all': {}}

        must = []
        must_not = []

        for field, condition in filters.items():
            os_field = _to_os_field(field)

            if not isinstance(condition, dict):
                must.append({'term': {os_field: condition}})
                continue

            operator = condition.get('operator', 'exact')
            value = condition.get('value')

            if operator == 'exact':
                must.append({'term': {os_field: value}})
            elif operator == 'ne':
                must_not.append({'term': {os_field: value}})
            elif operator == 'contains':
                must.append({'wildcard': {os_field: f'*{value}*'}})
            elif operator == 'startswith':
                must.append({'prefix': {os_field: value}})
            elif operator == 'endswith':
                must.append({'wildcard': {os_field: f'*{value}'}})
            elif operator in ('gt', 'gte', 'lt', 'lte'):
                must.append({'range': {os_field: {operator: value}}})
            elif operator == 'in':
                must.append({'terms': {os_field: value if isinstance(value, list) else [value]}})
            elif operator == 'not_in':
                must_not.append({'terms': {os_field: value if isinstance(value, list) else [value]}})
            elif operator == 'range':
                if isinstance(value, (list, tuple)) and len(value) == 2:
                    must.append({'range': {os_field: {'gte': value[0], 'lte': value[1]}}})
            elif operator == 'isnull':
                if value:
                    must_not.append({'exists': {'field': os_field}})
                else:
                    must.append({'exists': {'field': os_field}})
            elif operator == 'is_not_null':
                must.append({'exists': {'field': os_field}})
            else:
                must.append({'term': {os_field: value}})

        return {'bool': {'must': must, 'must_not': must_not}}

    @classmethod
    def _build_sort(cls, order_by: list) -> list:
        """Translate order_by list to OpenSearch sort clause."""
        sort = []
        for field in order_by:
            if field.startswith('-'):
                sort.append({_to_os_field(field[1:]): {'order': 'desc'}})
            else:
                sort.append({_to_os_field(field): {'order': 'asc'}})
        return sort

    @classmethod
    def _build_aggregations(cls, group_by: list, aggregations: dict) -> dict:
        """Build nested terms aggregations for group_by + metric sub-aggregations."""
        agg_funcs = {
            'count': 'value_count',
            'sum': 'sum',
            'avg': 'avg',
            'min': 'min',
            'max': 'max',
        }

        # Build metric sub-aggregations
        sub_aggs = {}
        for agg_name, agg_config in aggregations.items():
            func = agg_funcs.get(agg_config.get('function', 'count'), 'value_count')
            field = _to_os_field(agg_config.get('field', 'id'))
            if func == 'value_count':
                sub_aggs[agg_name] = {'value_count': {'field': field}}
            else:
                sub_aggs[agg_name] = {func: {'field': field}}

        if len(group_by) == 1:
            os_field = _to_os_field(group_by[0])
            return {
                'group': {
                    'terms': {'field': os_field, 'size': 10000},
                    'aggs': sub_aggs,
                }
            }

        # Multi-field: use composite aggregation
        sources = []
        for field in group_by:
            os_field = _to_os_field(field)
            sources.append({field: {'terms': {'field': os_field}}})

        return {
            'group': {
                'composite': {'sources': sources, 'size': 10000},
                'aggs': sub_aggs,
            }
        }

    # ── Response parsing ──────────────────────────────────────────

    @classmethod
    def _parse_hits_response(cls, response: dict, query_config: dict) -> List[Dict]:
        """Parse non-aggregated search hits into list of dicts."""
        results = []
        for hit in response.get('hits', {}).get('hits', []):
            source = hit.get('_source', {})
            results.append(cls._flatten_source(source))
        return results

    @classmethod
    def _flatten_source(cls, source: dict, prefix: str = '') -> dict:
        """Flatten nested dicts using __ separator to match FE field names."""
        flat = {}
        for key, value in source.items():
            full_key = f"{prefix}__{key}" if prefix else key
            if isinstance(value, dict):
                flat.update(cls._flatten_source(value, full_key))
            else:
                flat[full_key] = value
        return flat

    @classmethod
    def _parse_aggregation_response(cls, response: dict, query_config: dict) -> List[Dict]:
        """Parse aggregation buckets into list of dicts matching the ORM output shape."""
        group_by = query_config.get('group_by', [])
        aggregations = query_config.get('aggregations', {})
        agg_data = response.get('aggregations', {}).get('group', {})
        buckets = agg_data.get('buckets', [])

        results = []
        for bucket in buckets:
            row = {}
            # Extract group_by values
            if len(group_by) == 1:
                row[group_by[0]] = bucket.get('key')
            else:
                # Composite aggregation: key is a dict
                key = bucket.get('key', {})
                for field in group_by:
                    row[field] = key.get(field)

            # Extract metric values
            for agg_name in aggregations:
                agg_result = bucket.get(agg_name, {})
                row[agg_name] = agg_result.get('value', 0)

            row['doc_count'] = bucket.get('doc_count', 0)
            results.append(row)

        return results
