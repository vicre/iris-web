"""Real PostgreSQL regression tests; only run against a disposable *_test DB.

See docs/overview-performance.md for the container command. No app startup,
migrations, modules, or external integrations run in this test process.
"""
import datetime
import json
import os
import sys
import types
import unittest

if not os.environ.get('POSTGRES_DB', '').endswith('_test'):
    raise RuntimeError('These fixture tests require a disposable POSTGRES_DB ending in _test')
sys.modules['app.views'] = types.ModuleType('app.views')

from app import app, db, lm
from app.models import Cases
from app.models.authorization import User
from app.datamgmt.overview import overview_db
from app.blueprints.overview.overview_routes import overview_blueprint
from sqlalchemy import event, text
from flask import g

app.register_blueprint(overview_blueprint)
app.config['TESTING'] = True
lm.user_loader(lambda uid: db.session.get(User, int(uid)))


class OverviewTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        with app.app_context():
            # This database contains only the schema until these fixtures load.
            assert db.session.query(Cases).count() == 0, 'Use a fresh test database'
            for statement in [
                '''INSERT INTO "user" (id, "user", name, active) VALUES
                   (1, 'analyst', 'Analyst', true), (2, 'other', 'Other', true)''',
                "INSERT INTO client (client_id,name) VALUES (1,'Customer A')",
                "INSERT INTO case_state (state_id,state_name) VALUES (1,'Open'), (2,'Closed')",
                "INSERT INTO task_status (id,status_name) VALUES (1,'Open'), (4,'Done')",
                '''INSERT INTO cases (case_id,name,soc_id,client_id,owner_id,user_id,open_date,state_id,description)
                   SELECT n, 'Defender incident ' || n, 'XDR-' || n, 1, 1, 1, CURRENT_DATE, 1,
                          repeat('Large summary ', 1000) FROM generate_series(1,65) n''',
                "UPDATE cases SET close_date=CURRENT_DATE,state_id=2 WHERE case_id=61",
                "UPDATE cases SET name='Literal 100%_match' WHERE case_id=60",
                "UPDATE cases SET owner_id=NULL WHERE case_id IN (59,61,62,63,65)",
                '''INSERT INTO user_case_effective_access (user_id,case_id,access_level)
                   SELECT 1,n,2 FROM generate_series(1,61) n''',
                "INSERT INTO user_case_effective_access (user_id,case_id,access_level) VALUES (1,62,1),(2,65,4)",
                "INSERT INTO tags (id,tag_title) VALUES (1,'SRC_XDR'),(2,'phishing')",
                "INSERT INTO case_tags (case_id,tag_id) VALUES (1,1),(1,2),(2,1)",
                "INSERT INTO case_tasks (task_case_id,task_status_id) VALUES (1,1),(1,4)",
                "INSERT INTO note_directory (case_id,name) SELECT n,'Notes' FROM generate_series(1,65) n",
            ]:
                db.session.execute(text(statement))
            db.session.commit()

    def setUp(self):
        self.ctx = app.app_context()
        self.ctx.push()
        self.queries = []
        self.listener = lambda conn, cursor, statement, *args: self.queries.append(statement)
        event.listen(db.engine, 'before_cursor_execute', self.listener)

    def tearDown(self):
        event.remove(db.engine, 'before_cursor_execute', self.listener)
        db.session.remove()
        self.ctx.pop()

    def page(self, user=1, **args):
        self.assertTrue(hasattr(overview_db, 'get_overview_page'), 'Overview needs a bounded page query')
        return overview_db.get_overview_page(user, args)

    def test_page_is_bounded_and_does_not_leak_inaccessible_counts(self):
        result = self.page(length='25', draw='7')
        self.assertEqual((result['recordsTotal'], result['recordsFiltered'], result['draw']), (60,60,7))
        self.assertEqual([r['case_id'] for r in result['data']], list(range(60,35,-1)))
        self.assertEqual(self.page(start='25',length='25')['data'][0]['case_id'],35)
        self.assertEqual(self.page(user=2)['recordsTotal'],1)
        self.assertEqual(self.page(user=999)['recordsTotal'],0)
        self.assertEqual(self.page(show_closed='true')['recordsTotal'],61)

    def test_summaries_are_small_and_relationship_queries_are_batched(self):
        result = self.page(length='60')
        self.assertLessEqual(len(self.queries),12)
        self.assertLess(len(json.dumps(result)),100000)
        first = next(r for r in result['data'] if r['case_id']==1)
        self.assertEqual(first['tasks_status'],{'open_tasks':1,'closed_tasks':1})
        self.assertEqual({t['tag_title'] for t in first['tags']},{'SRC_XDR','phishing'})
        self.assertNotIn('description',first)
        self.assertNotIn('note_directories',first)
        self.assertNotIn('protagonists',first)

    def test_search_covers_all_pages_and_treats_sql_wildcards_literally(self):
        self.assertEqual(self.page(**{'search[value]':'XDR-1'})['recordsFiltered'],11)
        self.assertEqual(self.page(**{'search[value]':'100%_'})['recordsFiltered'],1)
        self.assertEqual(self.page(**{'search[value]':'XDR-65'})['recordsFiltered'],0)
        self.assertEqual(self.page(**{'columns[7][search][value]':'phishing'})['recordsFiltered'],1)
        result=self.page(**{'order[0][column]':'1','order[0][dir]':'asc'})
        self.assertEqual(result['data'][0]['case_id'],1)

    def test_unassigned_cases_are_searchable_without_widening_access(self):
        result = self.page(**{'search[value]': 'XDR-59'})
        self.assertEqual(result['recordsFiltered'], 1)
        self.assertEqual([row['case_id'] for row in result['data']], [59])
        self.assertIsNone(result['data'][0]['owner'])
        self.assertEqual(self.page(**{'search[value]': 'XDR-61'})['recordsFiltered'], 0)
        closed = self.page(show_closed='true', **{'search[value]': 'XDR-61'})
        self.assertEqual([row['case_id'] for row in closed['data']], [61])
        for identifier in (62, 63, 65):
            with self.subTest(inaccessible_case=identifier):
                self.assertEqual(self.page(**{'search[value]': f'XDR-{identifier}'})['recordsFiltered'], 0)
        export = overview_db.get_overview_export(1, {'search[value]': 'XDR-59'})
        self.assertEqual([row['case_id'] for row in export['data']], [59])
        self.assertIn(59, [row['case_id'] for row in overview_db.get_overview_db(1, False)])

    def test_advanced_filters_and_no_duplicate_rows_for_multiple_tags(self):
        rule={'logic':'AND','criteria':[
            {'origData':'tags','condition':'contains','value':['SRC_XDR']},
            {'origData':'case_id','condition':'=','value':['1']}]}
        result=self.page(builder=json.dumps(rule))
        self.assertEqual(result['recordsFiltered'],1)
        self.assertEqual(result['data'][0]['case_id'],1)
        rule['logic']='OR'
        rule['criteria'][1]['value']=['3']
        self.assertEqual(self.page(builder=json.dumps(rule))['recordsFiltered'],3)

    def test_request_limits_and_invalid_filters(self):
        for args in ({'length':'-1'},{'start':'-1'},{'length':'100000'},
                     {'order[0][column]':'99'},{'draw':'<script>'},
                     {'builder':'broken'}, {'search[regex]':'true'},
                     {'builder':json.dumps({'criteria':[{'origData':'password','condition':'=','value':['x']}]})}):
            with self.subTest(args=args), self.assertRaises(ValueError):
                self.page(**args)

    def test_incomplete_advanced_rules_do_not_erase_results_or_widen_or_group(self):
        rule = {'logic': 'OR', 'criteria': [
            {'origData': 'case_id', 'condition': '=', 'value': ['1']},
            {'type': '', 'value': []},
            {'origData': 'tags', 'value': []},
            {'logic': 'AND', 'criteria': []},
        ]}
        self.assertEqual(self.page(builder=json.dumps(rule))['recordsFiltered'], 1)
        self.assertEqual(self.page(builder=json.dumps({'criteria': [{'value': []}]}))['recordsFiltered'], 60)

    def test_negative_text_filters_include_cases_with_no_value(self):
        rule = {'criteria': [{'origData': 'tags', 'condition': '!contains', 'value': ['SRC_XDR']}]}
        self.assertEqual(self.page(builder=json.dumps(rule))['recordsFiltered'], 58)
        rule['criteria'][0] = {'origData': 'classification', 'condition': '!=', 'value': ['phishing']}
        self.assertEqual(self.page(builder=json.dumps(rule))['recordsFiltered'], 60)

    def test_closed_review_search_matches_the_displayed_state(self):
        result = self.page(show_closed='true', **{'search[value]': 'Not reviewed'})
        self.assertEqual([row['case_id'] for row in result['data']], [61])

    def test_task_numeric_filters_treat_missing_progress_as_zero(self):
        rule = {'criteria': [{'origData': 'tasks_status', 'condition': '<', 'value': ['1']}]}
        self.assertEqual(self.page(builder=json.dumps(rule))['recordsFiltered'], 60)
        rule['criteria'][0] = {'origData': 'tasks_status', 'condition': 'null', 'value': []}
        self.assertEqual(self.page(builder=json.dumps(rule))['recordsFiltered'], 59)

    def test_export_is_not_truncated_to_the_displayed_page(self):
        self.assertTrue(hasattr(overview_db, 'get_overview_export'))
        result = overview_db.get_overview_export(1, {'length': '25', 'start': '25'})
        self.assertEqual(len(result['data']), 60)
        self.assertEqual(len({row['case_id'] for row in result['data']}), 60)
        result = overview_db.get_overview_export(2, {})
        self.assertEqual([row['case_id'] for row in result['data']], [65])

    def test_legacy_endpoint_keeps_details_without_per_case_queries(self):
        result=overview_db.get_overview_db(1,False)
        self.assertEqual(len(result),60)
        self.assertIn('description',result[0])
        self.assertIn('protagonists',result[0])
        self.assertLessEqual(len(self.queries),16)

    def test_route_requires_authentication_and_returns_datatables_contract(self):
        client=app.test_client()
        self.assertEqual(client.get('/overview/page').status_code,401)
        self.assertEqual(client.get('/overview/export').status_code,401)
        with client.session_transaction() as session:
            session['_user_id']='1'
            session['_fresh']=True
            session['permissions']=1
        g.pop('_login_user', None)  # New request identity in this test's outer app context.
        response=client.get('/overview/page?length=10&draw=3')
        self.assertEqual(response.status_code,200)
        self.assertEqual(len(response.json['data']),10)
        self.assertEqual(response.json['draw'],3)
        self.assertEqual(client.get('/overview/page?length=-1').status_code,400)
        self.assertEqual(len(client.get('/overview/export?length=10').json['data']),60)


if __name__ == '__main__':
    unittest.main(verbosity=2)
