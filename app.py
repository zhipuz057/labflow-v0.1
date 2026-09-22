"""Run with: streamlit run app.py"""

import logging
import sqlite3
from datetime import datetime, timedelta
from functools import partial

import streamlit as st

from database import (
    ORDER_TYPES, LOCAL_TZ, authenticate, get_current_user, list_applicants,
    get_orders, get_order_overview, init_db, save_order, confirm_received, order_total,
    get_pending_orders, cancel_order, export_orders_csv, format_time, status_label,
    ACCOUNT_ROLES, OPERATING_ROLES,
)
from purchase_parser import parse_purchase_text

FIELD_DEFAULTS = dict(item_name="", brand="", catalog_no="", specification="",
                      unit_price=None, quantity=1, supplier="", project="", notes="", order_type=None)

st.set_page_config(page_title="LabFlow · 实验室订购记录", page_icon="🧪", layout="centered")
st.markdown("""
    <style>
    .block-container {max-width: 1200px; padding-top: 2rem; padding-bottom: 3rem;}
    @media (max-width: 640px) {
        .block-container {padding-left: 1rem; padding-right: 1rem;}
    }
    </style>
""", unsafe_allow_html=True)
st.title("LabFlow")
st.caption("实验室订购记录 · V0.1")

try:
    init_db()
except (sqlite3.Error, ValueError):
    logging.exception("Database initialization failed")
    st.error("无法打开订购记录，请检查应用目录是否可写，然后重试。")
    st.stop()

def login():
    try:
        principal = authenticate(st.session_state.get("login_access_key", ""))
    except sqlite3.Error:
        st.session_state.login_error = "登录暂时不可用，请稍后重试。"
        return
    st.session_state.login_access_key = ""
    if principal is None:
        st.session_state.login_error = "访问钥匙无效或用户已停用。"
    else:
        st.session_state.clear()
        st.session_state["_principal"] = principal


def logout():
    st.session_state.clear()


principal = st.session_state.get("_principal")
if principal is not None:
    try:
        current_user = get_current_user(principal)
    except PermissionError:
        logout()
        st.session_state.login_error = "登录已失效，请重新输入访问钥匙。"
        st.rerun()
    except sqlite3.Error:
        st.error("无法读取用户信息，请稍后重试。")
        st.stop()
else:
    with st.form("key_login"):
        st.text_input("请输入 LabFlow 访问钥匙", type="password", key="login_access_key")
        st.form_submit_button("进入 LabFlow", on_click=login, type="primary")
    if st.session_state.get("login_error"):
        st.error(st.session_state.login_error)
    st.stop()

# Display state is refreshed from the database; it is never the authorization source.
st.session_state.update(current_user_id=current_user["id"],
                        current_user_name=current_user["name"],
                        current_user_role=current_user["role"],
                        can_view_all=bool(current_user["can_view_all"]),
                        can_manage_users=bool(current_user["can_manage_users"]))
st.caption(f"当前用户：{current_user['name']}")
st.button("退出登录", on_click=logout)
pages = (["📋 订单记录", "💰 账目"] if current_user['role'] == 'account_viewer'
         else ["🛒 提交采购", "📋 订单记录", "📦 待收货"] +
         (["💰 账目"] if current_user['role'] in ACCOUNT_ROLES else []))
page = st.radio("页面", pages, horizontal=True, label_visibility="collapsed")


def read_filters(prefix, *, accounting=False):
    result = {}
    dates = st.columns(2)
    today = datetime.now(LOCAL_TZ).date()
    start = dates[0].date_input("开始日期", value=today - timedelta(days=29), key=f"{prefix}_start")
    end = dates[1].date_input("结束日期", value=today, key=f"{prefix}_end")
    # Records default to all dates; accounting defaults to the recent 30 days.
    unlimited = st.checkbox("不限日期", value=not accounting, key=f"{prefix}_all_dates")
    if not unlimited:
        if start > end:
            st.error("开始日期不能晚于结束日期。")
            st.stop()
        result.update(start_date=start, end_date=end)
    if current_user['can_view_all']:
        applicants = list_applicants(principal)
        labels = ['全部申请人'] + [f"{u['name']}（ID {u['id']}）" if u['id'] else f"{u['name']}（历史）" for u in applicants]
        selected = st.selectbox("申请人", range(len(labels)), format_func=lambda index: labels[index], key=f"{prefix}_applicant")
        if selected:
            user = applicants[selected - 1]
            result['applicant_user_id' if user['id'] else 'applicant_name'] = user['id'] or user['name']
    kind = st.selectbox("订单类型", ["全部类型", *ORDER_TYPES, "未分类"], key=f"{prefix}_type")
    if kind != '全部类型':
        result['order_type'] = kind
    if not accounting:
        status = st.selectbox("状态", ["全部状态", "待到货", "已到货", "已取消"], key=f"{prefix}_status")
        if status != '全部状态':
            result['status'] = status
    return result


def show_orders(orders):
    if not orders:
        st.info("暂无订单")
        return
    rows = []
    for order in orders:
        total = order_total(order)
        rows.append({
            '订单号': order['order_number'], '申请人': order['applicant'],
            '产品': order['item_name'], '品牌': order['brand'], '货号': order['catalog_no'],
            '规格': order['specification'], '供应商': order['supplier'], '单价': order['unit_price'],
            '数量': order['quantity'], '总价': float(total) if total is not None else None,
            '类型': order['order_type'], '状态': status_label(order),
            '下单时间': format_time(order['created_at']), '收货人': order['received_by_name'],
            '收货时间': format_time(order['received_at']),
        })
    st.dataframe(rows, hide_index=True, width="stretch")


def receive(order_id):
    try:
        changed = confirm_received(st.session_state.get('_principal'), order_id)
        st.session_state.flash = '已确认到货。' if changed else '订单已被收货或取消，本次未作更改。'
    except (PermissionError, ValueError) as error:
        st.session_state.action_error = str(error)
    except sqlite3.Error:
        st.session_state.action_error = '确认失败，请稍后重试。'


if 'flash' in st.session_state:
    st.success(st.session_state.pop('flash'))
if 'action_error' in st.session_state:
    st.error(st.session_state.pop('action_error'))

if page == "🛒 提交采购":
    st.subheader("提交采购")
    st.caption("带 * 的项目为必填项。提交前请核对，提交后不可编辑。")
    if "success_message" in st.session_state:
        st.success(st.session_state.pop("success_message"))

    if st.session_state.pop("reset_order_form", False):
        st.session_state.update(FIELD_DEFAULTS)
    for field, default in FIELD_DEFAULTS.items():
        st.session_state.setdefault(field, default)

    st.subheader("⚡ 快速录入")
    st.caption("直接粘贴供应商发来的产品信息或报价，系统会自动识别并填入下方表单。")
    pasted = st.text_area("采购信息", key="purchase_text",
                          placeholder="例如：CST 4970S Phospho-Akt (Ser473) Antibody 100 μL，报价1680元")
    if st.button("识别并填充"):
        parsed = parse_purchase_text(pasted)
        meaningful = sum(bool(parsed[field]) for field in
                         ("item_name", "brand", "catalog_no", "specification", "supplier"))
        meaningful += parsed["unit_price"] is not None
        # Set widget state before constructing the form; parsing never writes an order.
        if meaningful:
            st.session_state.update(parsed)
        if meaningful >= 2:
            st.success("已自动识别采购信息，请检查后提交。")
        else:
            st.warning("未能完整识别，请手动填写或调整粘贴内容。")

    with st.form("order_form"):
        st.text(f"申请人：{current_user['name']}")
        order_type = st.selectbox("订单类型 *", ORDER_TYPES, index=None,
                                  placeholder="请选择订单类型", key="order_type")
        item_name = st.text_input("物品名称 *", key="item_name")
        brand = st.text_input("品牌", key="brand")
        catalog_number = st.text_input("货号", key="catalog_no")
        specification = st.text_input("规格", placeholder="例如：500 mL / 瓶", key="specification")
        unit_price = st.number_input("单价（元）", min_value=0.0, value=None,
                                     step=0.01, format="%.2f", key="unit_price")
        quantity = st.number_input("数量", min_value=1, step=1, key="quantity")
        supplier = st.text_input("供应商", key="supplier")
        project = st.text_input("项目 / 用途", key="project")
        notes = st.text_area("备注", key="notes")
        submitted = st.form_submit_button("提交申请", type="primary",
                                          width="stretch")

    if submitted:
        if not item_name.strip() or order_type not in ORDER_TYPES:
            st.error("请填写物品名称并选择订单类型。")
        else:
            try:
                order_id = save_order(principal, item_name, brand, catalog_number,
                                      specification, quantity, project, notes,
                                      unit_price=unit_price, supplier=supplier, order_type=order_type)
            except (ValueError, PermissionError) as error:
                st.error(str(error))
            except sqlite3.Error:
                logging.exception("Order save failed")
                st.error("保存失败，内容已保留。请稍后重试。")
            else:
                st.session_state.reset_order_form = True
                st.session_state.success_message = (
                    "申请已成功提交，请在订单记录查看永久订单号。"
                )
                st.rerun()

else:
    st.subheader(page)
    try:
        if page == '📦 待收货':
            orders = get_pending_orders(principal)
            st.button('刷新记录')
            if not orders:
                st.info('暂无待收货订单')
            for order in orders:
                with st.container(border=True):
                    info, details, action = st.columns([3, 2, 1.5])
                    with info:
                        st.text(order['order_number'])
                        st.text(order['item_name'])
                        st.caption(f"申请人：{order['applicant']}")
                        st.caption(f"下单时间：{format_time(order['created_at'])}")
                    with details:
                        st.text(f"数量：{order['quantity']}")
                        st.caption(f"品牌：{order['brand'] or '—'}")
                        st.caption(f"货号：{order['catalog_no'] or '—'}")
                        st.caption(f"规格：{order['specification'] or '—'}")
                        if current_user['role'] in ('owner', 'manager'):
                            st.caption(f"供应商：{order['supplier'] or '—'}")
                            st.caption('单价：未填写' if order['unit_price'] is None else f"单价：¥{order['unit_price']:,.2f}")
                            total = order_total(order)
                            st.caption('总价：未计入' if total is None else f"总价：¥{total:,.2f}")
                            st.caption(order['order_type'])
                    with action:
                        st.button('确认到货', key=f"receive_{order['id']}", on_click=receive, args=(order['id'],))
        elif page == '📋 订单记录':
            filters = read_filters('records')
            orders = get_orders(principal, **filters)
            st.button('刷新记录')
            show_orders(orders)
            if current_user['role'] in OPERATING_ROLES:
                cancellable = [order for order in orders if not order['cancelled'] and
                               (current_user['role'] in ('owner', 'manager') or not order['received'])]
                if cancellable:
                    with st.expander('取消订单'):
                        with st.form('cancel_form'):
                            names = {order['id']: f"{order['order_number']} · {order['item_name']}" for order in cancellable}
                            cancel_id = st.selectbox('要取消的订单', list(names), format_func=lambda value: names[value])
                            reason = st.text_area('取消原因 *', key='cancel_reason_input')
                            if st.form_submit_button('确认取消订单'):
                                changed = cancel_order(principal, cancel_id, reason)
                                st.session_state.flash = '订单已取消，原记录永久保留。' if changed else '订单已取消，无需重复操作。'
                                st.rerun()
            cancelled_rows = [order for order in orders if order['cancelled']]
            if cancelled_rows:
                with st.expander('取消记录'):
                    for order in cancelled_rows:
                        st.text(f"{order['order_number']} · {order['cancelled_by_name']} · {format_time(order['cancelled_at'])}")
                        st.text(order['cancel_reason'])
        elif page == '💰 账目':
            filters = read_filters('accounts', accounting=True)
            orders, totals, missing_prices = get_order_overview(principal, **filters)
            for col, kind in zip(st.columns(3), (*ORDER_TYPES, '全部')):
                col.metric('全部有效采购总额' if kind == '全部' else kind + '总额', f"¥{totals[kind]:,.2f}")
            if missing_prices:
                st.caption(f'{missing_prices} 条有效订单缺少单价，未计入金额。')
            st.caption('已取消订单不计金额；导出保留取消记录，可按状态核对。')
            st.download_button('导出当前结果 CSV',
                data=partial(export_orders_csv, principal, **filters),
                file_name='labflow-filtered.csv', mime='text/csv', on_click='ignore')
            st.download_button('导出全部账目 CSV',
                data=partial(export_orders_csv, principal, all_accounts=True),
                file_name='labflow-all.csv', mime='text/csv', on_click='ignore')
    except (ValueError, PermissionError) as error:
        st.error(str(error))
    except sqlite3.Error:
        logging.exception('Order operation failed')
        st.error('操作失败，请稍后刷新重试。')
