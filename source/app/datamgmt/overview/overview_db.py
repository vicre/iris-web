#  IRIS Source Code
#  DFIR-IRIS Team
#  contact@dfir-iris.org
#
#  This program is free software; you can redistribute it and/or
#  modify it under the terms of the GNU Lesser General Public
#  License as published by the Free Software Foundation; either
#  version 3 of the License, or (at your option) any later version.
#
#  This program is distributed in the hope that it will be useful,
#  but WITHOUT ANY WARRANTY; without even the implied warranty of
#  MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the GNU
#  Lesser General Public License for more details.
#
#  You should have received a copy of the GNU Lesser General Public License
#  along with this program; if not, write to the Free Software Foundation,
#  Inc., 51 Franklin Street, Fifth Floor, Boston, MA  02110-1301, USA.
"""Overview queries: bounded summaries for the UI, batched legacy details."""
import datetime

from sqlalchemy import func
from sqlalchemy.orm import joinedload, load_only, selectinload

from app import db
from app.models import Cases, CaseTasks
from app.models.authorization import UserCaseEffectiveAccess, CaseAccessLevel, User
from app.models.cases import CaseProtagonist
from app.schema.marshables import CaseDetailsSchema, CaseProtagonistSchema


SUMMARY_FIELDS = (
    'case_id', 'name', 'soc_id', 'open_date', 'status_name',
    'client.customer_id', 'client.customer_name', 'owner.id', 'owner.user_name',
    'classification.id', 'classification.name', 'state.state_name',
    'review_status.status_name', 'severity.severity_name', 'tags',
)


def accessible_cases(user_id, show_full):
    # A subquery avoids transferring every accessible ID into Python and back.
    allowed = db.session.query(UserCaseEffectiveAccess.case_id).filter(
        UserCaseEffectiveAccess.user_id == user_id,
        UserCaseEffectiveAccess.access_level != CaseAccessLevel.deny_all.value,
    )
    # Defender incidents can be unassigned; ownership must not determine visibility.
    query = Cases.query.filter(Cases.case_id.in_(allowed)).outerjoin(Cases.owner).join(Cases.client)
    return query if show_full else query.filter(Cases.close_date.is_(None))


def related_options():
    return [joinedload(getattr(Cases, name)) for name in (
        'owner', 'client', 'classification', 'state', 'review_status', 'severity',
    )] + [selectinload(Cases.tags)]


def tasks_for_cases(case_ids):
    if not case_ids:
        return {}
    rows = db.session.query(
        CaseTasks.task_case_id,
        func.count().filter(CaseTasks.task_status_id.in_([1, 2, 3])),
        func.count().filter(CaseTasks.task_status_id == 4),
    ).filter(CaseTasks.task_case_id.in_(case_ids)).group_by(CaseTasks.task_case_id).all()
    return {row[0]: {'open_tasks': row[1], 'closed_tasks': row[2]} for row in rows}


def serialize_cases(cases, schema):
    tasks = tasks_for_cases([case.case_id for case in cases])
    today = datetime.date.today()
    rows = schema.dump(cases, many=True)
    for case, row in zip(cases, rows):
        row['case_open_since_days'] = (today - case.open_date).days if case.open_date else None
        row['tasks_status'] = tasks.get(case.case_id)
    return rows


class BatchedCaseDetailsSchema(CaseDetailsSchema):
    def get_protagonists(self, obj):
        return self.context['protagonists'].get(obj.case_id, [])


def get_overview_db(user_id, show_full):
    """Preserve the legacy /overview/filter payload, without N+1 queries."""
    query = accessible_cases(user_id, show_full)
    cases = query.options(*related_options(), joinedload(Cases.user), joinedload(Cases.reviewer),
                          selectinload(Cases.alerts), selectinload(Cases.note_directories)).all()
    protagonists = db.session.query(
        CaseProtagonist.case_id, CaseProtagonist.role, CaseProtagonist.name,
        CaseProtagonist.contact, User.name.label('user_name'), User.user.label('user_login'),
    ).outerjoin(CaseProtagonist.user).filter(
        CaseProtagonist.case_id.in_(query.with_entities(Cases.case_id))
    ).all()
    mapping = {}
    schema = CaseProtagonistSchema()
    for row in protagonists:
        mapping.setdefault(row.case_id, []).append(schema.dump(row))
    return serialize_cases(cases, BatchedCaseDetailsSchema(context={'protagonists': mapping}))


def get_overview_page(user_id, args):
    return _summary_result(user_id, args, paginate=True)


def get_overview_export(user_id, args):
    # One case SELECT fixes export membership even while new incidents arrive.
    return _summary_result(user_id, args, paginate=False)


def _summary_result(user_id, args, paginate):
    from app.datamgmt.overview.overview_table import filter_and_page
    query = accessible_cases(user_id, args.get('show_closed') == 'true')
    query, total, filtered, draw = filter_and_page(query, args, paginate=paginate)
    cases = query.options(
        load_only(Cases.case_id, Cases.name, Cases.soc_id, Cases.open_date, Cases.status_id),
        *related_options(),
    ).all()
    return {'draw': draw, 'recordsTotal': total, 'recordsFiltered': filtered,
            'data': serialize_cases(cases, CaseDetailsSchema(only=SUMMARY_FIELDS))}
