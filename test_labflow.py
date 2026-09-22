"""Run: python -m unittest test_labflow.py -v. All writes use temporary databases."""
import sqlite3
import json
import csv
import io
import manage_users
import tempfile
import unittest
from concurrent.futures import ThreadPoolExecutor
from datetime import date, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from unittest.mock import patch

import database as db
from purchase_parser import parse_purchase_text
from streamlit.testing.v1 import AppTest

EXAMPLES = [
    ('CST 4970S Phospho-Akt (Ser473) Antibody 100ul 报价1680元',
     ('CST', '4970S', 'Phospho-Akt (Ser473) Antibody', '100 μL', 1680, 1)),
    ('碧云天 P0013B RIPA裂解液 100ml 价格98元 2瓶',
     ('碧云天', 'P0013B', 'RIPA裂解液', '100 ml', 98, 2)),
    ('MedChemExpress HY-10162 Olaparib 10mg ￥850',
     ('MedChemExpress', 'HY-10162', 'Olaparib', '10 mg', 850, 1)),
]


class LabFlowTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.original = db.DB_PATH
        db.DB_PATH = Path(self.temp.name) / 'test.db'
        db.init_db()
        owner_key = db.generate_access_key()
        # Fixture bootstrapping writes only to this temporary test database.
        with db.connection(write=True) as conn:
            conn.execute('UPDATE users SET access_key_hash=? WHERE id=1', (db._hash_key(owner_key),))
        self.owner = db.authenticate(owner_key)
        keys = db.provision_missing_keys(self.owner)
        self.keys = {key['id']: key['access_key'] for key in keys}
        self.keys[1] = owner_key
        self.a = db.authenticate(self.keys[4])
        self.b = db.authenticate(self.keys[3])
        self.manager = db.authenticate(self.keys[2])

    def tearDown(self):
        db.DB_PATH = self.original
        self.temp.cleanup()

    def order(self, principal=None, name='测试订单', **kwargs):
        kwargs.setdefault('order_type', db.ORDER_TYPES[0])
        return db.save_order(principal or self.a, name, **kwargs)

    def app(self, user_id=None):
        app = AppTest.from_file(str(Path(__file__).with_name('app.py'))).run()
        if user_id:
            app.text_input(key='login_access_key').input(self.keys[user_id])
            app.button[0].click().run()
        self.assertFalse(app.exception)
        return app

    def test_key_login_gate_and_hashes(self):
        self.assertIsNone(db.authenticate(''))
        self.assertIsNone(db.authenticate('LF-invalid'))
        self.assertEqual(len(db.list_users_for_cli(self.owner)), 12)
        self.assertEqual(db.provision_missing_keys(self.owner), [])
        with db.connection() as conn:
            hashes = [row[0] for row in conn.execute('SELECT access_key_hash FROM users')]
            self.assertTrue(all(len(value) == 64 for value in hashes))
            self.assertFalse(any(key in db.DB_PATH.read_bytes().decode(errors='ignore') for key in self.keys.values()))
        app = self.app()
        self.assertFalse(app.radio)
        self.assertFalse(app.metric)
        self.assertEqual(len(app.text_input), 1)
        app.button[0].click().run()
        self.assertTrue(app.error)
        self.assertFalse(app.radio)

    def test_query_layer_authorization(self):
        a_id = self.order(self.a, 'A订单')
        self.order(self.b, 'B订单')
        self.assertEqual([row['item_name'] for row in db.get_orders(self.a)], ['A订单'])
        self.assertEqual([row['item_name'] for row in db.get_orders(self.b)], ['B订单'])
        self.assertEqual(db.get_orders(self.b, applicant_user_id=4), [])
        self.assertEqual(db.get_orders(self.b, applicant_name='采购人4'), [])
        for call in [lambda: db.get_all_orders(self.b), lambda: db.get_order_overview(self.b),
                     lambda: db.list_applicants(self.b), lambda: db.get_orders(None),
                     lambda: db.get_orders(db.Principal(1, 'wrong'))]:
            with self.assertRaises(PermissionError):
                call()
        self.assertEqual(len(db.get_all_orders(self.manager)), 2)
        self.assertFalse(db.get_orders(self.a)[0]['received'])

    def test_receipt_identity_and_no_repeat(self):
        order_id = self.order(self.a)
        self.assertTrue(db.confirm_received(self.manager, order_id))
        order = db.get_orders(self.a)[0]
        self.assertEqual(order['received_by_user_id'], 2)
        self.assertEqual(order['received_by_name'], '代院长')
        self.assertIsNotNone(datetime.fromisoformat(order['received_at']).tzinfo)
        self.assertFalse(db.confirm_received(self.a, order_id))
        self.assertEqual(db.get_orders(self.a)[0]['received_at'], order['received_at'])
        self.assertFalse(db.confirm_received(self.b, order_id))
        with db.connection() as conn:
            self.assertEqual(conn.execute("SELECT count(*) FROM audit_events WHERE event_type='order_received'").fetchone()[0], 1)

    def test_concurrent_receipt_first_wins(self):
        order_id = self.order()
        with ThreadPoolExecutor(max_workers=2) as pool:
            results = list(pool.map(lambda _: db.confirm_received(self.manager, order_id), range(2)))
        self.assertEqual(sorted(results), [False, True])

    def test_own_receipt_and_rename_trace(self):
        order_id = self.order(self.a)
        db.update_user(self.owner, 4, name='新姓名')
        db.init_db()
        self.assertTrue(db.confirm_received(self.a, order_id))
        row = db.get_orders(self.a)[0]
        self.assertEqual(row['applicant'], '采购人4')
        self.assertEqual(row['applicant_user_id'], 4)
        self.assertEqual(row['received_by_name'], '新姓名')
        db.update_user(self.owner, 4, name='再次改名')
        self.assertEqual(db.get_orders(self.a)[0]['received_by_name'], '新姓名')
        self.assertEqual(len(db.list_users_for_cli(self.owner)), 12)

    def test_revocation_and_role_refresh(self):
        self.order()
        db.update_user(self.owner, 2, role='purchaser')
        with self.assertRaises(PermissionError):
            db.get_all_orders(self.manager)
        new = db.rotate_access_key(self.owner, 4)
        self.assertIsNone(db.authenticate(self.keys[4]))
        self.assertIsNotNone(db.authenticate(new['access_key']))
        with self.assertRaises(PermissionError):
            db.get_orders(self.a)
        db.update_user(self.owner, 3, active=False)
        self.assertIsNone(db.authenticate(self.keys[3]))
        with self.assertRaises(PermissionError):
            db.get_orders(self.b)

    def test_submission_validation_and_identity(self):
        with self.assertRaises(ValueError):
            db.save_order(self.a, '试剂')
        for change in [dict(item_name='  '), dict(quantity=0), dict(unit_price=float('nan'))]:
            args = dict(item_name='试剂', order_type=db.ORDER_TYPES[0])
            args.update(change)
            with self.assertRaises(ValueError):
                db.save_order(self.a, **args)
        self.order(self.a, catalog_no='A1234', unit_price=9.8, quantity=2, supplier='供应商')
        row = db.get_orders(self.a)[0]
        self.assertEqual(row['applicant_user_id'], 4)
        self.assertEqual(row['applicant'], '采购人4')
        self.assertEqual(row['catalog_no'], row['catalog_number'])
        self.assertEqual(row['status'], 'Submitted')

    def test_statistics_and_filters(self):
        self.order(self.a, unit_price=98, quantity=2)
        self.order(self.b, unit_price=850, quantity=3, order_type=db.ORDER_TYPES[1])
        self.order(self.a, unit_price=None)
        self.order(self.a, unit_price=0.1, quantity=3)
        rows, totals, missing = db.get_order_overview(self.manager)
        self.assertEqual(totals[db.ORDER_TYPES[0]], Decimal('196.30'))
        self.assertEqual(totals[db.ORDER_TYPES[1]], Decimal('2550.00'))
        self.assertEqual(totals['全部'], Decimal('2746.30'))
        self.assertEqual(missing, 1)
        _, totals, _ = db.get_order_overview(self.manager, applicant_user_id=4)
        self.assertEqual(totals['全部'], Decimal('196.30'))
        _, totals, _ = db.get_order_overview(self.manager, order_type=db.ORDER_TYPES[1])
        self.assertEqual(totals['全部'], Decimal('2550.00'))
        db.confirm_received(self.manager, rows[0]['id'])
        _, totals, _ = db.get_order_overview(self.manager, received=True)
        self.assertEqual(totals['全部'], Decimal('0.30'))

    def test_inclusive_beijing_dates(self):
        for stamp in ['2026-09-20T15:59:59+00:00', '2026-09-20T16:00:00+00:00',
                      '2026-09-21T15:59:59+00:00', '2026-09-21T16:00:00+00:00']:
            with patch.object(db, 'utc_now', return_value=stamp):
                self.order(name=stamp, unit_price=10)
        rows, totals, _ = db.get_order_overview(self.manager, start_date=date(2026, 9, 21), end_date=date(2026, 9, 21))
        self.assertEqual([row['item_name'] for row in rows], ['2026-09-21T15:59:59+00:00', '2026-09-20T16:00:00+00:00'])
        self.assertEqual(totals['全部'], Decimal('20.00'))
        with self.assertRaises(ValueError):
            db.get_orders(self.manager, start_date=date(2026, 9, 22), end_date=date(2026, 9, 21))

    def test_legacy_migration_preserves_every_original_field(self):
        db.DB_PATH = Path(self.temp.name) / 'legacy.db'
        with db.connection(write=True) as conn:
            conn.execute('''CREATE TABLE orders(id INTEGER PRIMARY KEY,requester TEXT,item_name TEXT,
                catalog_number TEXT,quantity INTEGER,project TEXT,notes TEXT,created_at TEXT,status TEXT)''')
            conn.execute("INSERT INTO orders VALUES(71,'猛男zzp','旧订单','OLD-123',2,'旧项目','原备注','2026-01-01T00:00:00+00:00','Submitted')")
            conn.execute("INSERT INTO orders VALUES(72,'历史姓名','旧订单2','OLD-124',1,'','',NULL,'Submitted')")
            old = [tuple(row) for row in conn.execute('SELECT * FROM orders ORDER BY id')]
        db.init_db()
        db.init_db()
        with db.connection() as conn:
            after = [tuple(row) for row in conn.execute('SELECT id,requester,item_name,catalog_number,quantity,project,notes,created_at,status FROM orders ORDER BY id')]
            self.assertEqual(after, old)
            row = conn.execute('SELECT applicant,applicant_user_id,order_type,received FROM orders WHERE id=71').fetchone()
            self.assertEqual(tuple(row), ('猛男zzp', 1, '未分类', 0))
            self.assertIsNone(conn.execute('SELECT applicant_user_id FROM orders WHERE id=72').fetchone()[0])

    def test_legacy_without_created_at(self):
        db.DB_PATH = Path(self.temp.name) / 'no-time.db'
        with db.connection(write=True) as conn:
            conn.execute('''CREATE TABLE orders(id INTEGER PRIMARY KEY,requester TEXT,item_name TEXT,
                quantity INTEGER,project TEXT,notes TEXT,status TEXT)''')
            conn.execute("INSERT INTO orders VALUES(1,'代院长','历史物品',1,'','','Submitted')")
        db.init_db()
        with db.connection() as conn:
            self.assertIsNone(conn.execute('SELECT created_at FROM orders').fetchone()[0])

    def test_parser(self):
        for text, expected in EXAMPLES:
            parsed = parse_purchase_text(text)
            self.assertEqual(tuple(parsed[k] for k in ('brand', 'catalog_no', 'item_name', 'specification', 'unit_price', 'quantity')), expected)
        for price in ['¥1680', '￥1680', '1680元', '价格1680', '报价1680', '1,680.00元']:
            self.assertEqual(parse_purchase_text(price)['unit_price'], 1680)

    def test_ui_login_submission_logout(self):
        app = self.app(4)
        self.assertEqual(app.session_state.current_user_id, 4)
        self.assertEqual(app.session_state.current_user_role, 'purchaser')
        self.assertEqual(app.radio[0].options, ['🛒 提交采购', '📋 订单记录', '📦 待收货'])
        for text, expected in EXAMPLES:
            app.text_area(key='purchase_text').input(text)
            next(b for b in app.button if b.label == '识别并填充').click().run()
            self.assertEqual(app.text_input(key='item_name').value, expected[2])
            self.assertEqual(app.number_input(key='unit_price').value, expected[4])
            self.assertEqual(db.get_orders(self.a), [])
        app.selectbox(key='order_type').select(db.ORDER_TYPES[1])
        app.text_input(key='item_name').input('人工核对后的试剂')
        next(b for b in app.button if b.label == '提交申请').click().run()
        self.assertFalse(app.exception)
        self.assertTrue(app.success)
        row = db.get_orders(self.a)[0]
        self.assertRegex(row['order_number'], r'^LF-\d{8}-\d{5,}$')
        app.radio[0].set_value('📋 订单记录').run()
        self.assertEqual(app.dataframe[0].value.iloc[0]['产品'], '人工核对后的试剂')
        self.assertEqual(len(app.dataframe[0].value.columns), 15)
        next(b for b in app.button if b.label == '退出登录').click().run()
        self.assertFalse(app.radio)
        app.text_input(key='login_access_key').input(self.keys[3])
        app.button[0].click().run()
        app.radio[0].set_value('📋 订单记录').run()
        self.assertTrue(any(info.value == '暂无订单' for info in app.info))
        app.radio[0].set_value('📦 待收货').run()
        self.assertFalse(any('单价' in c.value or '总价' in c.value for c in app.caption))
        next(b for b in app.button if b.label == '确认到货').click().run()
        self.assertEqual(db.get_orders(self.a)[0]['received_by_user_id'], 3)
        self.assertFalse(any(b.label == '确认到货' for b in app.button))
        self.assertFalse(app.exception)

    def test_manager_ui_filters_statistics_receipt(self):
        self.order(self.a, unit_price=20, quantity=2)
        self.order(self.b, unit_price=30, order_type=db.ORDER_TYPES[1])
        app = self.app(2)
        app.radio[0].set_value('📋 订单记录').run()
        self.assertEqual(app.selectbox(key='records_applicant').value, 0)
        self.assertEqual(len(app.dataframe[0].value), 2)
        app.radio[0].set_value('💰 账目').run()
        self.assertEqual(app.metric[2].value, '¥70.00')
        app.selectbox(key='accounts_applicant').set_value(4).run()
        self.assertEqual(app.metric[2].value, '¥40.00')
        app.selectbox(key='accounts_applicant').set_value(0).run()
        app.selectbox(key='accounts_type').select(db.ORDER_TYPES[1]).run()
        self.assertEqual(app.metric[2].value, '¥30.00')
        self.assertEqual(len(app.get('download_button')), 2)
        app.radio[0].set_value('📦 待收货').run()
        next(b for b in app.button if b.label == '确认到货').click().run()
        self.assertEqual(db.get_orders(self.b)[0]['received_by_user_id'], 2)
        self.assertFalse(app.exception)

    def test_three_roles_and_owner_management(self):
        users = db.list_users_for_cli(self.owner)
        self.assertEqual([(u['role'], u['can_view_all'], u['can_manage_users']) for u in users[:3]],
                         [('owner', 1, 1), ('manager', 1, 0), ('purchaser', 0, 0)])
        for principal in (None, self.manager, self.a):
            for operation in (
                lambda: db.list_users_for_cli(principal),
                lambda: db.create_user(principal, '不应创建'),
                lambda: db.update_user(principal, 3, role='manager'),
                lambda: db.update_user(principal, 3, active=0),
                lambda: db.rotate_access_key(principal, 3),
                lambda: db.provision_missing_keys(principal),
            ):
                with self.assertRaises(PermissionError):
                    operation()
        new = db.create_user(self.owner, '新增成员')
        member = db.authenticate(new['access_key'])
        order_id = self.order(member)
        db.confirm_received(member, order_id)
        db.update_user(self.owner, new['id'], role='manager')
        db.rotate_access_key(self.owner, new['id'])
        db.update_user(self.owner, new['id'], active=0)
        db.init_db()
        with db.connection() as conn:
            row = conn.execute('SELECT * FROM users WHERE id=?', (new['id'],)).fetchone()
            self.assertEqual((row['role'], row['active']), ('manager', 0))
            order = conn.execute('SELECT * FROM orders WHERE id=?', (order_id,)).fetchone()
            self.assertEqual(order['received_by_user_id'], new['id'])
            self.assertEqual(order['received_by_name'], '新增成员')
            events = conn.execute("SELECT * FROM audit_events WHERE target_id=? AND event_type IN ('user_role_changed','access_key_rotated','user_created','user_deactivated')", (new['id'],)).fetchall()
            self.assertEqual(len(events), 4)
            self.assertTrue(all(e['actor_user_id'] == 1 and e['actor_name'] == '猛男zzp' for e in events))
        with self.assertRaises(sqlite3.IntegrityError):
            with db.connection(write=True) as conn:
                conn.execute('DELETE FROM users WHERE id=?', (new['id'],))
        with self.assertRaises(sqlite3.IntegrityError):
            with db.connection(write=True) as conn:
                conn.execute("UPDATE audit_events SET actor_name='改写' WHERE id=1")
        for change in (dict(active=0), dict(role='manager')):
            with self.assertRaises(ValueError):
                db.update_user(self.owner, 1, **change)
        with self.assertRaises(ValueError):
            db.update_user(self.owner, 2, role='owner')
        with self.assertRaises(ValueError):
            db.update_user(self.owner, 2, can_manage_users=1)

    def test_cli_requires_owner_and_has_no_delete(self):
        for user_id in (2, 3):
            with patch('sys.argv', ['manage_users.py', 'deactivate-user', '--user-id', '4']), patch('getpass.getpass', return_value=self.keys[user_id]):
                with self.assertRaises(PermissionError):
                    manage_users.main()
        with patch('sys.argv', ['manage_users.py', 'change-role', '--user-id', '4', '--role', 'manager']), patch('getpass.getpass', return_value=self.keys[1]):
            manage_users.main()
        self.assertEqual(db.get_current_user(self.a)['role'], 'manager')
        for user_id in (1, 2, 3):
            app = self.app(user_id)
            labels = [button.label for button in app.button]
            self.assertFalse(any(label in ('新增用户', '修改权限', '用户管理') for label in labels))
            self.assertEqual(app.session_state.can_manage_users, user_id == 1)

    def test_account_viewers_read_only(self):
        order_id = self.order(self.a, unit_price=25)
        for user_id in (11, 12):
            viewer = db.authenticate(self.keys[user_id])
            self.assertEqual(db.get_current_user(viewer)['role'], 'account_viewer')
            self.assertEqual(len(db.get_all_orders(viewer)), 1)
            for action in (lambda: self.order(viewer), lambda: db.confirm_received(viewer, order_id),
                           lambda: db.cancel_order(viewer, order_id, '不允许'),
                           lambda: db.get_pending_orders(viewer),
                           lambda: db.create_user(viewer, '不允许')):
                with self.assertRaises(PermissionError):
                    action()
            app = self.app(user_id)
            self.assertEqual(app.radio[0].options, ['📋 订单记录', '💰 账目'])
            self.assertEqual(len(app.dataframe[0].value), 1)
            self.assertFalse(any('取消' in b.label or '确认到货' == b.label for b in app.button))
            app.radio[0].set_value('💰 账目').run()
            self.assertEqual(app.metric[2].value, '¥25.00')
            self.assertEqual(len(app.get('download_button')), 2)
            self.assertFalse(app.exception)

    def test_pending_projection_and_cross_user_receipt(self):
        order_id = self.order(self.a, unit_price=123.45, supplier='供应商隐私', notes='内部备注')
        rows = db.get_pending_orders(self.b)
        self.assertEqual(set(rows[0]), {'id','order_number','applicant','item_name','brand','catalog_no','specification','quantity','created_at'})
        self.assertEqual(db.get_orders(self.b), [])
        self.assertTrue(db.confirm_received(self.b, order_id))
        self.assertEqual(db.get_orders(self.a)[0]['received_by_user_id'], 3)
        self.assertEqual(db.get_pending_orders(self.b), [])
        self.assertFalse(db.confirm_received(self.manager, order_id))

    def test_cancel_rules_and_effective_totals(self):
        public = self.order(self.a, unit_price=98, quantity=2)
        personal = self.order(self.b, unit_price=850, order_type=db.ORDER_TYPES[1])
        missing = self.order(self.a, unit_price=None)
        with self.assertRaises(ValueError):
            db.cancel_order(self.a, public, '  ')
        with self.assertRaises(PermissionError):
            db.cancel_order(self.b, public, '不是我的')
        self.assertTrue(db.cancel_order(self.a, public, '重复提交'))
        self.assertFalse(db.cancel_order(self.a, public, '不能改原因'))
        self.assertFalse(db.confirm_received(self.b, public))
        self.assertNotIn(public, [o['id'] for o in db.get_pending_orders(self.a)])
        self.assertTrue(db.confirm_received(self.a, personal))
        with self.assertRaises(PermissionError):
            db.cancel_order(self.b, personal, '已收到')
        _, totals, missing_count = db.get_order_overview(self.manager)
        self.assertEqual(totals[db.ORDER_TYPES[0]], Decimal('0.00'))
        self.assertEqual(totals[db.ORDER_TYPES[1]], Decimal('850.00'))
        self.assertEqual(totals['全部'], Decimal('850.00'))
        self.assertEqual(missing_count, 1)
        self.assertTrue(db.cancel_order(self.manager, personal, '负责人取消'))
        self.assertTrue(db.cancel_order(self.owner, missing, '无效需求'))
        rows, totals, _ = db.get_order_overview(self.manager)
        self.assertEqual(len(rows), 3)
        self.assertEqual(totals['全部'], Decimal('0.00'))
        row = next(o for o in rows if o['id']==public)
        self.assertEqual(row['cancel_reason'], '重复提交')
        self.assertEqual(row['cancelled_by_user_id'], 4)
        self.assertEqual(db.status_label(row), '已取消')

    def test_number_immutability_and_concurrent_creation(self):
        with ThreadPoolExecutor(max_workers=3) as pool:
            ids = list(pool.map(lambda n: self.order(name=f'并发订单{n}'), range(6)))
        self.assertEqual(len(set(ids)), 6)
        rows = db.get_orders(self.a)
        numbers = {row['id']:row['order_number'] for row in rows}
        self.assertEqual(len(set(numbers.values())), 6)
        db.cancel_order(self.a, ids[0], '取消不改变编号')
        db.init_db()
        self.assertEqual({r['id']:r['order_number'] for r in db.get_orders(self.a)}, numbers)
        for sql in ("UPDATE orders SET quantity=999 WHERE id=?", "UPDATE orders SET order_number='FAKE' WHERE id=?", 'DELETE FROM orders WHERE id=?'):
            with self.assertRaises(sqlite3.IntegrityError):
                with db.connection(write=True) as conn:
                    conn.execute(sql, (ids[0],))

    def test_csv_filters_beijing_and_audit(self):
        viewer = db.authenticate(self.keys[11])
        with patch.object(db, 'utc_now', return_value='2026-09-21T02:03:00+00:00'):
            selected = self.order(self.a, name='=危险公式', unit_price=12.5, quantity=2)
            self.order(self.b, name='其他人', unit_price=50, order_type=db.ORDER_TYPES[1])
        with patch.object(db, 'utc_now', return_value='2026-09-22T02:03:00+00:00'):
            self.order(self.a, name='其他日期', unit_price=100)
        data = db.export_orders_csv(viewer, applicant_user_id=4, order_type=db.ORDER_TYPES[0],
                                    start_date=date(2026,9,21), end_date=date(2026,9,21))
        rows = list(csv.DictReader(io.StringIO(data.decode('utf-8-sig'))))
        self.assertEqual(len(rows), 1)
        self.assertEqual(tuple(rows[0]), db.CSV_COLUMNS)
        self.assertEqual(rows[0]['item_name'], "'=危险公式")
        self.assertEqual(rows[0]['created_at'], '2026-09-21 10:03')
        self.assertEqual(rows[0]['order_total'], '25.00')
        self.assertEqual(len(list(csv.DictReader(io.StringIO(db.export_orders_csv(viewer, all_accounts=True).decode('utf-8-sig'))))), 3)
        with self.assertRaises(PermissionError):
            db.export_orders_csv(self.a, all_accounts=True)
        db.cancel_order(self.a, selected, '错误采购')
        cancelled = list(csv.DictReader(io.StringIO(db.export_orders_csv(viewer, status='已取消').decode('utf-8-sig'))))
        self.assertEqual(cancelled[0]['cancel_reason'], '错误采购')
        self.assertEqual(cancelled[0]['status'], '已取消')
        with db.connection() as conn:
            events = conn.execute("SELECT * FROM audit_events WHERE event_type='orders_exported'").fetchall()
            self.assertEqual(len(events), 3)
            self.assertTrue(all(e['actor_user_id']==11 for e in events))
        db.update_user(self.owner, 11, active=0)
        with self.assertRaises(PermissionError):
            db.export_orders_csv(viewer)

    def test_cancel_form_requires_reason(self):
        order_id = self.order(self.a)
        app = self.app(4)
        app.radio[0].set_value('📋 订单记录').run()
        next(b for b in app.button if b.label=='确认取消订单').click().run()
        self.assertTrue(app.error)
        app.text_area(key='cancel_reason_input').input('录入错误')
        next(b for b in app.button if b.label=='确认取消订单').click().run()
        self.assertFalse(app.exception)
        self.assertEqual(app.dataframe[0].value.iloc[0]['状态'], '已取消')
        self.assertEqual(db.get_orders(self.a)[0]['cancel_reason'], '录入错误')


    def test_download_is_lazy_and_rechecks_permissions(self):
        import streamlit as st
        self.order(self.a, unit_price=10)
        downloads = []
        original_download = st.download_button
        def capture(label, data, **kwargs):
            downloads.append(data)
            return original_download(label, data, **kwargs)
        app = self.app(11)
        with patch('streamlit.download_button', side_effect=capture):
            app.radio[0].set_value('💰 账目').run()
        self.assertFalse(app.exception)
        self.assertEqual(len(downloads), 2)
        with db.connection() as conn:
            self.assertEqual(conn.execute("SELECT count(*) FROM audit_events WHERE event_type='orders_exported'").fetchone()[0], 0)
        self.assertIn(b'order_number', downloads[0]())
        db.update_user(self.owner, 11, role='purchaser')
        with self.assertRaises(PermissionError):
            downloads[1]()


if __name__ == '__main__':
    unittest.main()
