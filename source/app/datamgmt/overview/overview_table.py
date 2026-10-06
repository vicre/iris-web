#  IRIS Source Code — LGPL-3.0
"""DataTables query translation. Client input never selects SQL identifiers."""
import datetime
import json
import math

from sqlalchemy import String, Float, Date, and_, or_, cast, case, func, literal
from sqlalchemy.dialects.postgresql import aggregate_order_by

from app import db
from app.models import Cases, CaseTasks, Tags, CaseStatus, CaseClassification, Client
from app.models.authorization import User
from app.models.cases import CaseTags, CaseState
from app.models.models import ReviewStatus
from app.models.alerts import Severity


COLUMN_KEYS = ('status_name', 'case_id', 'severity', 'name', 'client', 'classification',
               'state', 'tags', 'case_open_since_days', 'open_date', 'tasks_status', 'owner')
NUMERIC_KEYS = {'case_id', 'case_open_since_days', 'tasks_status'}
MAX_PAGE_SIZE = 250


def integer(args, key, default, minimum, maximum):
    try:
        value = int(args.get(key, default))
    except (ValueError, TypeError):
        raise ValueError('Invalid ' + key)
    if not minimum <= value <= maximum:
        raise ValueError('Invalid ' + key)
    return value


def contains(column, value):
    value = str(value)
    if len(value) > 256:
        raise ValueError('Search values must be at most 256 characters')
    escaped = value.replace('\\', '\\\\').replace('%', '\\%').replace('_', '\\_')
    return cast(column, String).ilike('%' + escaped + '%', escape='\\')


def builder_filter(node, columns, depth=0, budget=None):
    if budget is None:
        budget = [0]
    budget[0] += 1
    if budget[0] > 50 or depth > 4 or not isinstance(node, dict):
        raise ValueError('Invalid or overly complex advanced filter')
    if 'criteria' in node:
        children = node['criteria']
        logic = node.get('logic', 'AND')
        if not isinstance(children, list) or logic not in ('AND', 'OR'):
            raise ValueError('Invalid advanced filter group')
        predicates = [builder_filter(child, columns, depth + 1, budget) for child in children]
        predicates = [predicate for predicate in predicates if predicate is not None]
        return (and_(*predicates) if logic == 'AND' else or_(*predicates)) if predicates else None
    key = node.get('origData', node.get('dataOrig'))
    if key is None:
        return None  # SearchBuilder sends unfinished rules while the analyst edits.
    if not isinstance(key, str) or key not in columns:
        raise ValueError('Unknown advanced filter column')
    column = columns[key]
    condition = node.get('condition')
    if condition is None:
        return None
    if not isinstance(condition, str):
        raise ValueError('Invalid advanced filter condition')
    values = node.get('value', [])
    if not isinstance(values, list) or len(values) > 2:
        raise ValueError('Invalid advanced filter values')
    empty = column.is_(None) if key in NUMERIC_KEYS or key == 'open_date' else or_(column.is_(None), column == '')
    if condition in ('null', '!null'):
        return empty if condition == 'null' else ~empty
    if not values or any(value == '' for value in values):
        return None
    if not all(isinstance(v, (str, int, float)) and len(str(v)) <= 256 for v in values):
        raise ValueError('Missing or invalid advanced filter value')
    if key in NUMERIC_KEYS:
        column = func.coalesce(column, 0)
        try:
            values = [float(v) for v in values]
            if not all(math.isfinite(v) for v in values):
                raise ValueError()
        except (ValueError, TypeError):
            raise ValueError('Invalid numeric filter')
    elif key == 'open_date':
        try:
            values = [datetime.date.fromisoformat(v) for v in values]
        except (ValueError, TypeError):
            raise ValueError('Use YYYY-MM-DD for date filters')
    else:
        values = [str(v).lower() for v in values]
        column = func.lower(func.coalesce(cast(column, String), ''))
    value = values[0]
    if condition in ('between', '!between'):
        if len(values) != 2:
            raise ValueError('Between requires two values')
        predicate = column.between(min(values), max(values))
        return predicate if condition == 'between' else ~predicate
    comparisons = {'=': lambda: column == value, '!=': lambda: column != value,
                   '<': lambda: column < value, '<=': lambda: column <= value,
                   '>': lambda: column > value, '>=': lambda: column >= value}
    if condition in comparisons:
        return comparisons[condition]()
    if key not in NUMERIC_KEYS and key != 'open_date':
        if condition in ('contains', '!contains'):
            predicate = contains(column, value)
        elif condition in ('starts', '!starts', 'ends', '!ends'):
            predicate = column.startswith(value, autoescape=True) if 'starts' in condition else column.endswith(value, autoescape=True)
        else:
            raise ValueError('Unsupported advanced filter condition')
        return ~predicate if condition.startswith('!') else predicate
    raise ValueError('Unsupported advanced filter condition')


def filter_and_page(query, args, paginate=True):
    draw = integer(args, 'draw', 0, 0, 2147483647)
    start = integer(args, 'start', 0, 0, 2147483647)
    length = integer(args, 'length', 25, 1, MAX_PAGE_SIZE)
    if args.get('search[regex]') == 'true' or any(
        args.get('columns[%d][search][regex]' % i) == 'true' for i in range(len(COLUMN_KEYS))
    ):
        raise ValueError('Regular expression searches are not supported')
    total = query.with_entities(func.count(Cases.case_id)).scalar()
    task_counts = db.session.query(
        CaseTasks.task_case_id.label('case_id'),
        func.count().filter(CaseTasks.task_status_id == 4).label('closed'),
        func.count().filter(CaseTasks.task_status_id.in_([1, 2, 3, 4])).label('total'),
    ).group_by(CaseTasks.task_case_id).subquery()
    tag_names = db.session.query(
        CaseTags.case_id,
        func.string_agg(Tags.tag_title, aggregate_order_by(literal(', '), Tags.tag_title)).label('names'),
    ).join(Tags, Tags.id == CaseTags.tag_id).group_by(CaseTags.case_id).subquery()
    query = query.outerjoin(Cases.classification).outerjoin(Cases.severity).outerjoin(Cases.state).outerjoin(
        Cases.review_status).outerjoin(task_counts, task_counts.c.case_id == Cases.case_id).outerjoin(
        tag_names, tag_names.c.case_id == Cases.case_id)
    review = func.coalesce(ReviewStatus.status_name, 'Not reviewed')
    state_label = case(
        (CaseState.state_name == 'Closed', func.concat('Closed - ', review)),
        else_=func.concat(CaseState.state_name, case(
            (review != 'Not reviewed', func.concat(' - ', review)), else_='')),
    )
    columns = dict(zip(COLUMN_KEYS, (
        case({s.value: s.name for s in CaseStatus}, value=Cases.status_id),
        Cases.case_id, Severity.severity_name, Cases.name, Client.name, CaseClassification.name,
        state_label,
        tag_names.c.names, literal(datetime.date.today(), type_=Date) - Cases.open_date,
        Cases.open_date, cast(task_counts.c.closed, Float) / func.nullif(task_counts.c.total, 0), User.name,
    )))
    search = args.get('search[value]', '')
    if len(search) > 256:
        raise ValueError('Search values must be at most 256 characters')
    for word in search.split():
        query = query.filter(or_(*(contains(c, word) for c in list(columns.values()) + [Cases.soc_id])))
    for index, key in enumerate(COLUMN_KEYS):
        value = args.get('columns[%d][search][value]' % index, '')
        if value:
            query = query.filter(contains(columns[key], value))
    raw_builder = args.get('builder')
    if raw_builder:
        if len(raw_builder) > 16384:
            raise ValueError('Advanced filter is too large')
        try:
            node = json.loads(raw_builder)
        except (ValueError, TypeError):
            raise ValueError('Invalid advanced filter JSON')
        predicate = builder_filter(node, columns)
        if predicate is not None:
            query = query.filter(predicate)
    filtered = query.with_entities(func.count(Cases.case_id)).scalar()
    ordering = []
    for n in range(len(COLUMN_KEYS)):
        if 'order[%d][column]' % n not in args:
            break
        index = integer(args, 'order[%d][column]' % n, 1, 0, len(COLUMN_KEYS)-1)
        direction = args.get('order[%d][dir]' % n, 'desc')
        if direction not in ('asc', 'desc'):
            raise ValueError('Invalid sort direction')
        column = columns[COLUMN_KEYS[index]]
        ordering.append((column.asc() if direction == 'asc' else column.desc()).nullslast())
    # Tie-breaker makes paging deterministic for repeated values.
    query = query.order_by(*ordering, Cases.case_id.desc())
    if paginate:
        query = query.offset(start).limit(length)
    return query, total, filtered, draw
