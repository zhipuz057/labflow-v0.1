"""SQLite persistence for LabFlow. No sample records are inserted."""

import sqlite3
import csv
import io
import math
import base64
import hashlib
import hmac
import json
import secrets
from contextlib import contextmanager
from dataclasses import dataclass, field
from decimal import Decimal, ROUND_HALF_UP
from contextlib import closing
from datetime import datetime, time, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

DB_PATH = Path(__file__).resolve().with_name("labflow.db")
LOCAL_TZ = ZoneInfo("Asia/Shanghai")
ROLE_PERMISSIONS = {'owner': (True, True), 'manager': (True, False), 'purchaser': (False, False), 'account_viewer': (True, False)}
ACCOUNT_ROLES = ('owner', 'manager', 'account_viewer')
OPERATING_ROLES = ('owner', 'manager', 'purchaser')
ORDER_TYPES = ("公共实验试剂", "个人订购试剂")
INITIAL_USERS = ("猛男zzp", "代院长", "采购人3", "采购人4", "采购人5",
                 "采购人6", "采购人7", "采购人8", "采购人9", "采购人10")


@contextmanager
def connection(write=False):
    """SQLite adapter boundary. Domain callers never open SQL connections."""
    with closing(sqlite3.connect(DB_PATH, timeout=10)) as conn:
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA foreign_keys = ON")
        with conn:
            if write:
                conn.execute("BEGIN IMMEDIATE")
            yield conn


def utc_now():
    return datetime.now(timezone.utc).isoformat(timespec="microseconds")


def init_db():
    with connection(write=True) as conn:
        _migrate(conn)


def _migrate(connection):
    connection.execute("""
        CREATE TABLE IF NOT EXISTS orders (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            requester TEXT NOT NULL CHECK(length(trim(requester)) > 0),
            item_name TEXT NOT NULL CHECK(length(trim(item_name)) > 0),
            brand TEXT NOT NULL DEFAULT '',
            catalog_number TEXT NOT NULL DEFAULT '',
            specification TEXT NOT NULL DEFAULT '',
            quantity INTEGER NOT NULL CHECK(quantity > 0),
            project TEXT NOT NULL DEFAULT '',
            notes TEXT NOT NULL DEFAULT '',
            created_at TEXT NOT NULL,
            status TEXT NOT NULL DEFAULT 'Submitted'
        )
    """)
    connection.execute("""
        CREATE TABLE IF NOT EXISTS users (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            name TEXT NOT NULL UNIQUE,
            access_key_hash TEXT UNIQUE,
            role TEXT NOT NULL DEFAULT 'purchaser',
            can_view_all INTEGER NOT NULL DEFAULT 0,
            active INTEGER NOT NULL DEFAULT 1,
            created_at TEXT NOT NULL
        )
    """)
    connection.execute("""
        CREATE TABLE IF NOT EXISTS schema_migrations (
            version TEXT PRIMARY KEY, applied_at TEXT NOT NULL
        )
    """)
    connection.execute("""
        CREATE TABLE IF NOT EXISTS audit_events (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            event_type TEXT NOT NULL,
            actor_user_id INTEGER REFERENCES users(id),
            actor_name TEXT NOT NULL,
            target_id INTEGER,
            details TEXT NOT NULL,
            created_at TEXT NOT NULL
        )
    """)
    columns = {row[1] for row in connection.execute("PRAGMA table_info(orders)")}
    additions = {
        "brand": "TEXT NOT NULL DEFAULT ''",
        "catalog_number": "TEXT NOT NULL DEFAULT ''",
        "catalog_no": "TEXT NOT NULL DEFAULT ''",
        "specification": "TEXT NOT NULL DEFAULT ''",
        "unit_price": "REAL",
        "supplier": "TEXT NOT NULL DEFAULT ''",
        "applicant": "TEXT NOT NULL DEFAULT ''",
        "applicant_user_id": "INTEGER REFERENCES users(id)",
        "order_type": "TEXT NOT NULL DEFAULT '未分类'",
        "received": "INTEGER NOT NULL DEFAULT 0",
        "received_by_user_id": "INTEGER REFERENCES users(id)",
        "received_by_name": "TEXT",
        "received_at": "TEXT",
        "created_at": "TEXT",
    }
    for name, definition in additions.items():
        if name not in columns:
            connection.execute(f"ALTER TABLE orders ADD COLUMN {name} {definition}")
    connection.execute("UPDATE orders SET catalog_no = catalog_number WHERE catalog_no = '' AND catalog_number != ''")
    connection.execute("UPDATE orders SET catalog_number = catalog_no WHERE catalog_number = '' AND catalog_no != ''")
    connection.execute("""
        CREATE INDEX IF NOT EXISTS orders_requester_created
        ON orders(requester, created_at DESC, id DESC)
    """)


    if not connection.execute("SELECT 1 FROM schema_migrations WHERE version = 'identity_v1'").fetchone():
        for name in INITIAL_USERS:
            connection.execute("INSERT OR IGNORE INTO users(name, created_at) VALUES (?, ?)", (name, utc_now()))
        connection.execute("UPDATE orders SET applicant = requester WHERE applicant = ''")
        connection.execute("""
            UPDATE orders SET applicant_user_id = (
                SELECT id FROM users WHERE users.name = orders.applicant
            ) WHERE applicant_user_id IS NULL
        """)
        connection.execute("INSERT INTO schema_migrations VALUES ('identity_v1', ?)", (utc_now(),))
    connection.execute("CREATE INDEX IF NOT EXISTS orders_user_created ON orders(applicant_user_id, created_at DESC)")

    user_columns = {row[1] for row in connection.execute('PRAGMA table_info(users)')}
    if 'can_manage_users' not in user_columns:
        connection.execute('ALTER TABLE users ADD COLUMN can_manage_users INTEGER NOT NULL DEFAULT 0')
    if not connection.execute("SELECT 1 FROM schema_migrations WHERE version = 'three_roles_v1'").fetchone():
        owner = connection.execute("SELECT id FROM users WHERE name = ?", ('猛男zzp',)).fetchone()
        manager = connection.execute("SELECT id FROM users WHERE name = ?", ('代院长',)).fetchone()
        if not owner or not manager:
            raise ValueError('三级权限迁移无法匹配指定负责人姓名，请核对用户记录。')
        for row in connection.execute('SELECT * FROM users').fetchall():
            role = 'owner' if row['id'] == owner['id'] else 'manager' if row['id'] == manager['id'] else 'purchaser'
            view, manage = ROLE_PERMISSIONS[role]
            changes = dict(role=role, can_view_all=int(view), can_manage_users=int(manage))
            if role == 'owner':
                changes['active'] = 1
            connection.execute('UPDATE users SET role=?, can_view_all=?, can_manage_users=?, active=? WHERE id=?',
                               (role, view, manage, changes.get('active', row['active']), row['id']))
            _audit(connection, 'user_role_changed', None, row['id'],
                   {'source': 'explicit_owner_assignment', 'role': role})
        connection.execute("INSERT INTO schema_migrations VALUES ('three_roles_v1', ?)", (utc_now(),))
    connection.execute("CREATE UNIQUE INDEX IF NOT EXISTS users_single_owner ON users(role) WHERE role='owner'")
    connection.execute("""CREATE TRIGGER IF NOT EXISTS users_no_physical_delete
        BEFORE DELETE ON users BEGIN SELECT RAISE(ABORT, 'Users must be deactivated, never deleted'); END""")
    for operation in ('UPDATE', 'DELETE'):
        connection.execute(f"""CREATE TRIGGER IF NOT EXISTS audit_no_{operation.lower()}
            BEFORE {operation} ON audit_events BEGIN SELECT RAISE(ABORT, 'Audit history is append-only'); END""")

    # V0.1 is additive: old values and all historical audit rows stay untouched.
    order_columns = {row[1] for row in connection.execute('PRAGMA table_info(orders)')}
    for name, definition in {
        'order_number': 'TEXT',
        'cancelled': 'INTEGER NOT NULL DEFAULT 0',
        'cancelled_by_user_id': 'INTEGER REFERENCES users(id)',
        'cancelled_by_name': 'TEXT',
        'cancelled_at': 'TEXT',
        'cancel_reason': "TEXT NOT NULL DEFAULT ''",
    }.items():
        if name not in order_columns:
            connection.execute(f'ALTER TABLE orders ADD COLUMN {name} {definition}')
    if not connection.execute("SELECT 1 FROM schema_migrations WHERE version='v01_orders'").fetchone():
        for row in connection.execute('SELECT id,created_at FROM orders WHERE order_number IS NULL').fetchall():
            connection.execute('UPDATE orders SET order_number=? WHERE id=?',
                               (_order_number(row['id'], row['created_at']), row['id']))
        for name in ('老师1', '老师2'):
            existing = connection.execute('SELECT id,role FROM users WHERE name=?', (name,)).fetchone()
            if existing and existing['role'] != 'account_viewer':
                raise ValueError('老师占位姓名已被其他角色使用，请先更名后迁移。')
            if not existing:
                cursor = connection.execute("""INSERT INTO users
                    (name,role,can_view_all,can_manage_users,active,created_at)
                    VALUES (?, 'account_viewer', 1, 0, 1, ?)""", (name, utc_now()))
                _audit(connection, 'user_created', None, cursor.lastrowid,
                       {'name': name, 'role': 'account_viewer', 'source': 'v01_migration'})
        connection.execute("INSERT INTO schema_migrations VALUES ('v01_orders', ?)", (utc_now(),))
    connection.execute('CREATE UNIQUE INDEX IF NOT EXISTS orders_number_unique ON orders(order_number)')
    connection.execute("""CREATE TRIGGER IF NOT EXISTS orders_no_delete BEFORE DELETE ON orders
        BEGIN SELECT RAISE(ABORT, 'Orders must be cancelled, never deleted'); END""")
    immutable = ('order_number', 'applicant', 'applicant_user_id', 'requester', 'item_name',
                 'brand', 'catalog_no', 'catalog_number', 'specification', 'unit_price',
                 'quantity', 'order_type', 'supplier', 'created_at', 'project', 'notes')
    changed = ' OR '.join(f'NEW.{column} IS NOT OLD.{column}' for column in immutable)
    connection.execute(f"""CREATE TRIGGER IF NOT EXISTS orders_no_edit BEFORE UPDATE ON orders
        WHEN OLD.order_number IS NOT NULL AND ({changed})
        BEGIN SELECT RAISE(ABORT, 'Submitted orders cannot be edited'); END""")
    for flag, fields in [('received', ('received', 'received_by_user_id', 'received_by_name', 'received_at')),
                         ('cancelled', ('cancelled', 'cancelled_by_user_id', 'cancelled_by_name', 'cancelled_at', 'cancel_reason'))]:
        changed = ' OR '.join(f'NEW.{column} IS NOT OLD.{column}' for column in fields)
        connection.execute(f"""CREATE TRIGGER IF NOT EXISTS orders_{flag}_immutable BEFORE UPDATE ON orders
            WHEN OLD.{flag}=1 AND ({changed})
            BEGIN SELECT RAISE(ABORT, 'Order event cannot be overwritten'); END""")


def _order_number(order_id, created_at):
    try:
        stamp = datetime.fromisoformat(created_at.replace('Z', '+00:00'))
        if stamp.tzinfo is None:
            stamp = stamp.replace(tzinfo=timezone.utc)
        day = stamp.astimezone(LOCAL_TZ).strftime('%Y%m%d')
    except (AttributeError, TypeError, ValueError):
        # No invented order timestamp: only the identifier uses the migration date.
        day = datetime.now(LOCAL_TZ).strftime('%Y%m%d')
    return f'LF-{day}-{order_id:05d}'


def format_time(value):
    if not value:
        return ''
    try:
        moment = datetime.fromisoformat(value.replace('Z', '+00:00'))
        if moment.tzinfo is None:
            moment = moment.replace(tzinfo=timezone.utc)
        return moment.astimezone(LOCAL_TZ).strftime('%Y-%m-%d %H:%M')
    except (ValueError, TypeError):
        return str(value)


def status_label(order):
    return '已取消' if order['cancelled'] else '已到货' if order['received'] else '待到货'


@dataclass(frozen=True)
class Principal:
    user_id: int
    credential_hash: str = field(repr=False)


def _hash_key(key):
    return hashlib.sha256(key.strip().upper().encode('utf-8')).hexdigest()


def generate_access_key():
    # 128 random bits; SHA-256 is appropriate for high-entropy machine-generated keys.
    raw = base64.b32encode(secrets.token_bytes(16)).decode().rstrip('=')
    return 'LF-' + '-'.join(raw[i:i + 4] for i in range(0, len(raw), 4))


def authenticate(access_key):
    if not access_key or not access_key.strip():
        return None
    digest = _hash_key(access_key)
    with connection() as conn:
        user = conn.execute('SELECT id FROM users WHERE access_key_hash = ? AND active = 1', (digest,)).fetchone()
        return Principal(user['id'], digest) if user else None


def _require_user(conn, principal):
    if not isinstance(principal, Principal):
        raise PermissionError('请使用有效访问钥匙登录。')
    user = conn.execute('SELECT * FROM users WHERE id = ? AND active = 1', (principal.user_id,)).fetchone()
    if not user or not user['access_key_hash'] or not hmac.compare_digest(user['access_key_hash'], principal.credential_hash):
        raise PermissionError('登录已失效，请重新输入访问钥匙。')
    return dict(user)


def get_current_user(principal):
    with connection() as conn:
        user = _require_user(conn, principal)
        user.pop('access_key_hash')
        return user


def _audit(conn, event, user, target_id, details):
    conn.execute('''INSERT INTO audit_events
        (event_type, actor_user_id, actor_name, target_id, details, created_at)
        VALUES (?, ?, ?, ?, ?, ?)''',
        (event, user['id'] if user else None, user['name'] if user else 'local_operator',
         target_id, json.dumps(details, ensure_ascii=False), utc_now()))


def _require_owner(conn, principal):
    user = _require_user(conn, principal)
    if user['role'] != 'owner' or not user['can_manage_users']:
        raise PermissionError('仅 owner 可以执行用户管理。')
    return user


def require_owner(principal):
    with connection() as conn:
        user = _require_owner(conn, principal)
        user.pop('access_key_hash')
        return user


def provision_missing_keys(principal):
    credentials = []
    with connection(write=True) as conn:
        actor = _require_owner(conn, principal)
        for user in conn.execute('SELECT id, name FROM users WHERE access_key_hash IS NULL ORDER BY id').fetchall():
            key = generate_access_key()
            conn.execute('UPDATE users SET access_key_hash = ? WHERE id = ?', (_hash_key(key), user['id']))
            _audit(conn, 'access_key_rotated', actor, user['id'], {})
            credentials.append(dict(id=user['id'], name=user['name'], access_key=key))
    return credentials


def rotate_access_key(principal, user_id):
    with connection(write=True) as conn:
        actor = _require_owner(conn, principal)
        user = conn.execute('SELECT id, name FROM users WHERE id = ?', (user_id,)).fetchone()
        if not user:
            raise ValueError('用户不存在。')
        key = generate_access_key()
        conn.execute('UPDATE users SET access_key_hash = ? WHERE id = ?', (_hash_key(key), user_id))
        _audit(conn, 'access_key_rotated', actor, user_id, {})
        return dict(id=user_id, name=user['name'], access_key=key)


def create_user(principal, name, role='purchaser'):
    with connection(write=True) as conn:
        actor = _require_owner(conn, principal)
        if not name.strip() or role not in ('purchaser', 'manager', 'account_viewer'):
            raise ValueError('请填写姓名；新增用户只能为 purchaser、manager 或 account_viewer。')
        key = generate_access_key()
        view, manage = ROLE_PERMISSIONS[role]
        cursor = conn.execute("""INSERT INTO users
            (name, access_key_hash, role, can_view_all, can_manage_users, active, created_at)
            VALUES (?, ?, ?, ?, ?, 1, ?)""",
            (name.strip(), _hash_key(key), role, view, manage, utc_now()))
        _audit(conn, 'user_created', actor, cursor.lastrowid, {'name': name.strip(), 'role': role})
        return dict(id=cursor.lastrowid, name=name.strip(), access_key=key)


def update_user(principal, user_id, *, name=None, role=None, can_view_all=None,
                can_manage_users=None, active=None):
    with connection(write=True) as conn:
        actor = _require_owner(conn, principal)
        old = conn.execute('SELECT * FROM users WHERE id = ?', (user_id,)).fetchone()
        if not old:
            raise ValueError('用户不存在。')
        next_role = role if role is not None else old['role']
        if next_role not in ROLE_PERMISSIONS:
            raise ValueError('角色必须为 owner、manager、purchaser 或 account_viewer。')
        if (old['role'] == 'owner' and (next_role != 'owner' or active == 0)) or (old['role'] != 'owner' and next_role == 'owner'):
            raise ValueError('必须保留唯一且启用的 owner；不能停用、降级 owner 或创建第二个 owner。')
        view, manage = ROLE_PERMISSIONS[next_role]
        if ((can_view_all is not None and can_view_all != view) or
                (can_manage_users is not None and can_manage_users != manage)):
            raise ValueError('权限必须符合角色规则，请通过修改 role 调整权限。')
        changes = dict(role=next_role, can_view_all=int(view), can_manage_users=int(manage))
        if name is not None:
            if not name.strip():
                raise ValueError('姓名不能为空。')
            changes['name'] = name.strip()
        if active is not None:
            if active not in (0, 1, False, True):
                raise ValueError('启用状态必须为 0 或 1。')
            changes['active'] = int(active)
        for key, value in changes.items():
            conn.execute(f'UPDATE users SET {key} = ? WHERE id = ?', (value, user_id))
        if next_role != old['role'] or (name is not None and name.strip() != old['name']):
            _audit(conn, 'user_role_changed', actor, user_id,
                   {'role': next_role, 'name': changes.get('name', old['name'])})
        if active is not None and int(active) != old['active']:
            _audit(conn, 'user_activated' if active else 'user_deactivated', actor, user_id, {})


def list_users_for_cli(principal):
    with connection() as conn:
        _require_owner(conn, principal)
        return [dict(row) for row in conn.execute("""SELECT id, name, role, can_view_all,
            can_manage_users, active, (access_key_hash IS NOT NULL) AS has_key FROM users ORDER BY id""")]


def list_applicants(principal):
    with connection() as conn:
        user = _require_user(conn, principal)
        if not user['can_view_all']:
            raise PermissionError('无权查看全部申请人。')
        users = [dict(row) for row in conn.execute('SELECT id, name FROM users ORDER BY id')]
        # Unmatched historical names remain visible to managers without guessing ownership.
        users += [dict(id=None, name=row[0]) for row in conn.execute(
            'SELECT DISTINCT applicant FROM orders WHERE applicant_user_id IS NULL ORDER BY applicant')]
        return users


def save_order(principal, item_name, brand='', catalog_number='', specification='',
               quantity=1, project='', notes='', *, catalog_no=None, unit_price=None,
               supplier='', order_type=None):
    if not item_name.strip():
        raise ValueError('请填写物品名称。')
    if order_type not in ORDER_TYPES:
        raise ValueError('请选择订单类型。')
    if isinstance(quantity, bool) or not isinstance(quantity, int) or quantity < 1:
        raise ValueError('数量必须为正整数。')
    if unit_price is not None and (not math.isfinite(unit_price) or unit_price < 0):
        raise ValueError('单价必须为非负金额。')
    catalog = (catalog_number if catalog_no is None else catalog_no).strip()
    with connection(write=True) as conn:
        user = _require_user(conn, principal)
        _require_operator(user)
        created_at = utc_now()
        cursor = conn.execute('''INSERT INTO orders
            (requester, applicant, applicant_user_id, item_name, brand, catalog_number,
             catalog_no, specification, quantity, project, notes, unit_price, supplier,
             order_type, status, created_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'Submitted', ?)''',
            (user['name'], user['name'], user['id'], item_name.strip(), brand.strip(), catalog,
             catalog, specification.strip(), quantity, project.strip(), notes.strip(),
             unit_price, supplier.strip(), order_type, created_at))
        number = _order_number(cursor.lastrowid, created_at)
        conn.execute('UPDATE orders SET order_number=? WHERE id=?', (number, cursor.lastrowid))
        _audit(conn, 'order_created', user, cursor.lastrowid, {'order_type': order_type})
        return cursor.lastrowid


def _filters(user, applicant_user_id=None, applicant_name=None, order_type=None,
             received=None, start_date=None, end_date=None, status=None, cancelled=None):
    clauses, params = [], []
    if not user['can_view_all']:
        # Always enforce ownership, even if the caller supplies another user's filter.
        clauses.append('applicant_user_id = ?')
        params.append(user['id'])
    if applicant_user_id is not None:
        clauses.append('applicant_user_id = ?')
        params.append(applicant_user_id)
    if applicant_name is not None:
        clauses.append('applicant = ?')
        params.append(applicant_name)
    if order_type is not None:
        clauses.append('order_type = ?')
        params.append(order_type)
    if received is not None:
        clauses.append('received = ?')
        params.append(int(received))
    if cancelled is not None:
        clauses.append('cancelled = ?')
        params.append(int(cancelled))
    status_clauses = {'待到货': 'cancelled=0 AND received=0', '已到货': 'cancelled=0 AND received=1', '已取消': 'cancelled=1'}
    if status is not None:
        if status not in status_clauses:
            raise ValueError('无效订单状态。')
        clauses.append('(' + status_clauses[status] + ')')
    if start_date and end_date and start_date > end_date:
        raise ValueError('开始日期不能晚于结束日期。')
    for day, operator in ((start_date, '>='), (end_date, '<')):
        if day:
            boundary = datetime.combine(day, time.min, LOCAL_TZ)
            if operator == '<':
                boundary += timedelta(days=1)
            clauses.append(f'julianday(created_at) {operator} julianday(?)')
            params.append(boundary.astimezone(timezone.utc).isoformat())
    return (' WHERE ' + ' AND '.join(clauses) if clauses else ''), params


def get_orders(principal, **filters):
    with connection() as conn:
        user = _require_user(conn, principal)
        where, params = _filters(user, **filters)
        return [dict(row) for row in conn.execute(
            'SELECT * FROM orders' + where + ' ORDER BY julianday(created_at) DESC, id DESC', params)]


def get_all_orders(principal, **filters):
    with connection() as conn:
        user = _require_user(conn, principal)
        if user['role'] not in ACCOUNT_ROLES:
            raise PermissionError('无权查看全部订单。')
        where, params = _filters(user, **filters)
        return [dict(row) for row in conn.execute(
            'SELECT * FROM orders' + where + ' ORDER BY julianday(created_at) DESC, id DESC', params)]


def order_total(order):
    if order['unit_price'] is None:
        return None
    value = Decimal(str(order['unit_price'])) * Decimal(str(order['quantity']))
    return value.quantize(Decimal('0.01'), rounding=ROUND_HALF_UP) if value.is_finite() else None


def get_order_overview(principal, **filters):
    """One authorized snapshot supplies both the table and its matching totals."""
    orders = get_all_orders(principal, **filters)
    totals = {kind: Decimal('0.00') for kind in (*ORDER_TYPES, '全部')}
    missing_prices = 0
    for order in orders:
        if order['cancelled']:
            continue
        amount = order_total(order)
        if amount is None:
            missing_prices += 1
            continue
        totals['全部'] += amount
        if order['order_type'] in ORDER_TYPES:
            totals[order['order_type']] += amount
    return orders, totals, missing_prices


def _require_operator(user):
    if user['role'] not in OPERATING_ROLES:
        raise PermissionError('该账号只可查看和导出，不能操作订单。')


def get_pending_orders(principal):
    with connection() as conn:
        user = _require_user(conn, principal)
        _require_operator(user)
        # Purchaser projection never selects price, supplier, notes or full accounts.
        fields = ('id, order_number, applicant, item_name, brand, catalog_no, specification, quantity, created_at'
                  if user['role'] == 'purchaser' else '*')
        return [dict(row) for row in conn.execute('SELECT ' + fields +
            ' FROM orders WHERE received=0 AND cancelled=0 ORDER BY julianday(created_at) DESC,id DESC')]


def confirm_received(principal, order_id):
    with connection(write=True) as conn:
        user = _require_user(conn, principal)
        _require_operator(user)
        now = utc_now()
        cursor = conn.execute("""UPDATE orders SET received=1, received_by_user_id=?,
            received_by_name=?, received_at=? WHERE id=? AND received=0 AND cancelled=0""",
            (user['id'], user['name'], now, order_id))
        if cursor.rowcount:
            _audit(conn, 'order_received', user, order_id, {})
        return bool(cursor.rowcount)


def cancel_order(principal, order_id, reason):
    if not reason or not reason.strip():
        raise ValueError('请填写取消原因。')
    with connection(write=True) as conn:
        user = _require_user(conn, principal)
        _require_operator(user)
        order = conn.execute('SELECT * FROM orders WHERE id=?', (order_id,)).fetchone()
        if not order:
            raise PermissionError('无权操作该订单。')
        if user['role'] == 'purchaser' and (order['applicant_user_id'] != user['id'] or order['received']):
            raise PermissionError('只能取消自己尚未到货的订单。')
        cursor = conn.execute("""UPDATE orders SET cancelled=1, cancelled_by_user_id=?,
            cancelled_by_name=?, cancelled_at=?, cancel_reason=? WHERE id=? AND cancelled=0""",
            (user['id'], user['name'], utc_now(), reason.strip(), order_id))
        if cursor.rowcount:
            _audit(conn, 'order_cancelled', user, order_id, {'reason': reason.strip()})
        return bool(cursor.rowcount)


CSV_COLUMNS = ('order_number', 'applicant', 'item_name', 'brand', 'catalog_no',
               'specification', 'supplier', 'unit_price', 'quantity', 'order_total',
               'order_type', 'status', 'created_at', 'received_by_name', 'received_at', 'cancel_reason')


def _csv_cell(value):
    if value is None:
        return ''
    if isinstance(value, str) and (value.startswith(('\t', '\r', '\n')) or value.lstrip().startswith(('=', '+', '-', '@'))):
        return "'" + value  # Prevent formula execution when opened in spreadsheet programs.
    return value


def export_orders_csv(principal, *, all_accounts=False, **filters):
    # Export is generated on demand, with current permissions and one consistent snapshot.
    with connection(write=True) as conn:
        user = _require_user(conn, principal)
        if user['role'] not in ACCOUNT_ROLES:
            raise PermissionError('无权导出全局账目。')
        applied = {} if all_accounts else filters
        where, params = _filters(user, **applied)
        orders = [dict(row) for row in conn.execute(
            'SELECT * FROM orders' + where + ' ORDER BY julianday(created_at) DESC,id DESC', params)]
        output = io.StringIO(newline='')
        writer = csv.DictWriter(output, fieldnames=CSV_COLUMNS)
        writer.writeheader()
        for order in orders:
            row = {key: order.get(key) for key in CSV_COLUMNS}
            row.update(order_total=order_total(order), status=status_label(order),
                       created_at=format_time(order['created_at']), received_at=format_time(order['received_at']))
            writer.writerow({key: _csv_cell(value) for key, value in row.items()})
        payload = output.getvalue().encode('utf-8-sig')
        _audit(conn, 'orders_exported', user, None, {
            'scope': 'all' if all_accounts else 'filtered', 'row_count': len(orders),
            'filters': {key: value.isoformat() if hasattr(value, 'isoformat') else value for key, value in applied.items()},
        })
        return payload
