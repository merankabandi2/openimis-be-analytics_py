"""Grievance ticket visibility for analytics, decided by the grievance module.

Rows come from GrievanceAccessControl.filter_ticket_queryset, the filter the
module's `tickets` query applies. Whether a field is visible on a ticket comes
from TicketGQLType._should_restrict_field, the rule the module's GraphQL
resolvers apply to each ticket field. Both exist with the same signature in the
upstream module and in the openIMIS-WB fork, whose rules differ; analytics
follows whichever is installed.

On top of the module's rule, fields listed in the grievance configuration key
`grievance_anonymized_fields` are hidden from every user except superusers.
"""
from types import SimpleNamespace

from django.core.exceptions import FieldDoesNotExist, PermissionDenied
from django.db.models import Q


def _ticket_model():
    from grievance_social_protection.models import Ticket
    return Ticket


def canonical_fields(names):
    """Map Ticket field names or attnames (reporter_type_id, attending_staff_id)
    to model field names; `reporter` stands for its two columns."""
    ticket = _ticket_model()
    canonical = set()
    for name in names:
        if name == 'reporter':
            canonical.update({'reporter_type', 'reporter_id'})
            continue
        try:
            canonical.add(ticket._meta.get_field(name).name)
        except FieldDoesNotExist:
            canonical.add(name)
    return canonical


def ticket_field_names():
    """Model names of the Ticket columns analytics can reference."""
    return {
        field.name for field in _ticket_model()._meta.get_fields()
        if getattr(field, 'concrete', False) and not field.many_to_many
    }


def _module_restricts(user, category, flags, field):
    from grievance_social_protection.gql_queries import TicketGQLType
    rule = getattr(TicketGQLType, '_should_restrict_field', None)
    if rule is None:
        raise PermissionDenied("The installed grievance module exposes no field visibility rule")
    root = SimpleNamespace(category=category, flags=flags)
    info = SimpleNamespace(context=SimpleNamespace(user=user))
    return bool(rule(field, root, info))


def anonymised_fields(category):
    """Fields `grievance_anonymized_fields` lists for a ticket of `category`:
    the entries under the default key, under the category and under each of
    its parent categories. A value that is not a mapping lists nothing."""
    from grievance_social_protection.apps import TicketConfig, DEFAULT_STRING, CATEGORY_SEPARATOR
    configured = getattr(TicketConfig, 'grievance_anonymized_fields', None)
    if not isinstance(configured, dict):
        return set()
    names = set()
    for key, value in configured.items():
        applies = key == DEFAULT_STRING or (
            category and (category == key or category.startswith(f"{key}{CATEGORY_SEPARATOR}"))
        )
        if not applies:
            continue
        if isinstance(value, str):
            names.add(value)
        elif isinstance(value, (list, tuple, set)):
            names.update(str(item) for item in value)
    return canonical_fields(names)


def visible_fields(user, category, flags, fields):
    """The subset of `fields` (model names) visible to `user` on a ticket with
    this stored category and flags."""
    hidden = set() if user.is_superuser else anonymised_fields(category)
    return {
        field for field in fields
        if field not in hidden and not _module_restricts(user, category, flags, field)
    }


def configuration():
    """The grievance module settings the visibility rules read: every key of
    its DEFAULT_CFG and the category and flag rules derived from them, as
    loaded on TicketConfig."""
    from grievance_social_protection import apps as grievance_apps
    keys = set(getattr(grievance_apps, 'DEFAULT_CFG', {})) | {'processed_categories', 'processed_flags'}
    return {key: getattr(grievance_apps.TicketConfig, key, None) for key in sorted(keys)}


def check_read_right(user):
    from grievance_social_protection.apps import TicketConfig
    if not user or not getattr(user, 'id', None) or not user.has_perms(TicketConfig.gql_query_tickets_perms):
        raise PermissionDenied("Reading grievance tickets requires the grievance read right")


def readable_tickets(queryset, user):
    """Tickets of `queryset` the grievance module lists for `user`."""
    from grievance_social_protection.access_control import GrievanceAccessControl
    check_read_right(user)
    return GrievanceAccessControl.filter_ticket_queryset(queryset, user)


def _stored_value_q(field, value):
    return Q(**{f'{field}__isnull': True}) if value is None else Q(**{field: value})


def scope(queryset, user, referenced):
    """Tickets of `queryset` the user may read with every `referenced` field
    visible on them.

    Returns (queryset, withheld). `withheld` is None when no readable ticket is
    left out, else (readable, parts): `readable` is the queryset of tickets the
    user can read, and each part pairs a Q selecting left-out tickets with the
    referenced fields visible on them.

    Tickets are classified by their stored (category, flags) pair; the result
    keeps only the pairs found visible, so a pair stored after the
    classification is left out.
    """
    readable = readable_tickets(queryset, user)
    referenced = canonical_fields(referenced)
    if not referenced:
        return readable, None
    kept, parts = [], []
    pairs = readable.order_by().values_list('category', 'flags').distinct()
    for category, flags in pairs:
        selector = _stored_value_q('category', category) & _stored_value_q('flags', flags)
        visible = visible_fields(user, category, flags, referenced)
        if visible == referenced:
            kept.append(selector)
        else:
            parts.append((selector, visible))
    condition = Q(pk__in=[])
    for selector in kept:
        condition |= selector
    return readable.filter(condition), ((readable, parts) if parts else None)


def selectable_fields(user):
    """Ticket fields visible to `user` on a ticket without flags of at least
    one category the user can read."""
    from grievance_social_protection.access_control import GrievanceAccessControl
    fields = ticket_field_names()
    if user.is_superuser:
        return fields
    selectable = set()
    for category in GrievanceAccessControl.get_accessible_categories(user):
        selectable |= visible_fields(user, category, None, fields - selectable)
    return selectable
