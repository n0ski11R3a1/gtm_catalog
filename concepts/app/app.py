# GTM Distribution ERP - Phase 1
# Flask + SQLite, single-file app. Deploy: PythonAnywhere (Flask web app) - see README.
import os, re, sqlite3, json, uuid, datetime, io, zipfile, math, shutil, tempfile
from xml.sax.saxutils import escape as xml_escape
from urllib.parse import urlencode as _urlencode, quote as _urlquote
from functools import wraps
from flask import (Flask, request, session, redirect, url_for, g, jsonify,
                   render_template, flash, abort, send_from_directory, Response)
from werkzeug.security import generate_password_hash, check_password_hash

BASE = os.path.dirname(os.path.abspath(__file__))
DB_PATH = os.environ.get('GTM_DB', os.path.join(BASE, 'data', 'gtm.db'))
IMG_DIR = os.path.join(BASE, 'static', 'product-images')

app = Flask(__name__)
app.secret_key = os.environ.get('GTM_SECRET', 'gtm-dev-secret-change-me')
# Field staff stay signed in for 30 days (Ko, 2026-10-14): the default
# browser-session cookie logged reps out whenever the app was closed,
# which also breaks offline work. Deactivating an account still cuts
# access immediately because current_user() checks active=1 per request.
app.permanent_session_lifetime = datetime.timedelta(days=30)
app.config['MAX_CONTENT_LENGTH'] = 6 * 1024 * 1024
# v62 (performance): static files (CSS, logo, icons) change only with a
# release, and every release bumps the service-worker cache name, which
# drops the old cached copies — so browsers may keep static files for a
# long time and repeat taps skip re-downloading them. (The service
# worker script itself and /api/products are routes, not static files,
# and are NOT long-cached.)
app.config['SEND_FILE_MAX_AGE_DEFAULT'] = 30 * 24 * 3600

ROLES = {'owner': 'Owner (Ko)', 'warehouse': 'Warehouse', 'sales': 'Sales', 'finance': 'Account/Finance', 'delivery': 'Delivery'}
# v47: payment method groups (Ko, 2026-10-18/19) — exactly three broad groups;
# the specific channel (KBZ Pay, Wave, MMQR...) lives in the free-text note.
PAY_METHODS = [('cash', 'Cash'), ('digital', 'Digital Pay'), ('bank', 'Banking')]
# v60: Ks note values of the denomination counter (helpers near fmt_money).
DENOMS = (10000, 5000, 1000, 500, 200, 100, 50)
PAY_METHOD_KEYS = tuple(k for k, _v in PAY_METHODS)
FINANCE_ROLES = ('owner', 'finance')   # can see Actual Base + margins + verify
# Thet Mon Zin (Warehouse) is a trusted insider, so she may SEE Actual prices in the
# settlement workflow (Ko, 2026-10-14). Flip to False when the warehouse holder changes.
WAREHOUSE_SEES_ACTUAL = True

def show_actual(user):
    # v75: Actual visibility is a permission now. Owner/Finance hold
    # actual.view; warehouse holds only actual.view.settlement, and that
    # key is already flag-gated inside user_perms(). Every other site
    # (products, costing card, order flags, profit, at-risk, ask, money)
    # is gated by actual.view alone, so warehouse never sees Actual there.
    if not user:
        return False
    return has_perm(user, 'actual.view') or has_perm(user, 'actual.view.settlement')
# ---------------- v75: permissions catalogue, data roles, departments ----------------
# Ko, 2026-10-25: roles are permission bundles stored in DATA (roles +
# role_permissions), not string checks in code. Enforcement reads the DATA
# role (users.role_id); users.role stays only as a synced label/classifier
# (report channels, edit-cap side, sales/delivery enumerations) and for
# fallback on rows not yet mapped. Departments are LABELS ONLY: no
# permission logic ever reads users.department_id.
# Secrecy: keys/labels marked secret=True name the second cost layer.
# They are rendered ONLY on the Owner-only /roles pages, never on any
# other template, never in a 403 page, and never in a non-Owner API error.
# PERMISSIONS: key -> (group, my_label, en_label, zh_label, secret)
PERMISSIONS = {
 'auth.any': ('admin', 'ဝင်ရောက် (လက်မှတ်ထိုးပြီး)', 'Signed-in access', '已登录访问', False),
 'dashboard.office': ('dashboard', 'ရုံး ပင်မ စာမျက်နှာ', 'Office dashboard', '办公室主页', False),
 'dashboard.sales_own': ('dashboard', 'အရောင်း ကိုယ်ပိုင် ပင်မ', 'Own sales dashboard', '个人销售主页', False),
 'dashboard.delivery_own': ('dashboard', 'ပို့ဆောင် ကိုယ်ပိုင် ပင်မ', 'Own delivery dashboard', '个人配送主页', False),
 'dashboard.pills.finance': ('dashboard', 'ငွေ/ဈေး သတိပေးချက် ပြရန်', 'Finance warning pills', '财务提醒', False),
 'dashboard.pills.gps': ('dashboard', 'နေရာ စစ်ရန် သတိပေးချက် ပြရန်', 'GPS-check warning pill', '位置核查提醒', False),
 'dashboard.position': ('dashboard', 'ငွေ အခြေအနေ (position) ကြည့်', 'Money position', '资金状况', False),
 'dashboard.backup_due': ('dashboard', 'Backup လိုအပ် သတိပေးချက်', 'Backup-due reminder', '备份提醒', False),
 'outlets.view': ('reports', 'ဆိုင်များ ကြည့်', 'View outlets', '查看店铺', False),
 'outlets.manage': ('reports', 'ဆိုင်များ ပြင်/ပေါင်း', 'Manage outlets', '管理店铺', False),
 'profit.view': ('reports', 'လချုပ် အမြတ် ကြည့်', 'View profit', '查看利润', False),
 'leader_prices.view': ('products', 'Leader ဈေးနှုန်း စာရင်း ကြည့်', 'View leader prices', '查看组长价格表', False),
 'leader_prices.toggle': ('admin', 'Leader ဈေးနှုန်း ဖွင့်/ပိတ်', 'Toggle leader prices', '开关组长价格表', False),
 'settings.office': ('admin', 'ရုံး တည်နေရာ သတ်မှတ်', 'Set office location', '设置办公室位置', False),
 'settings.gps_threshold': ('admin', 'GPS ကွာခြား သတ်မှတ်', 'Set GPS threshold', '设置 GPS 阈值', False),
 'settings.business': ('admin', 'လုပ်ငန်း အချက်အလက် ပြင်', 'Edit business profile', '编辑企业资料', False),
 'expenses.view': ('money', 'အသုံးစရိတ် ကြည့်', 'View expenses', '查看开支', False),
 'expenses.manage': ('money', 'အသုံးစရိတ် မှတ်/ပြင်/ဖျက်', 'Manage expenses', '管理开支', False),
 'at_risk.view': ('reports', 'အန္တရာယ် စာရင်း ကြည့်', 'View at-risk list', '查看风险清单', False),
 'at_risk.mark': ('reports', 'အန္တရာယ် မှတ်သား', 'Mark at-risk handled', '标记风险已处理', False),
 'visits.view': ('reports', 'ဆိုင်လည် မှတ်တမ်း ကြည့်', 'View visits', '查看走访记录', False),
 'sales_report.view': ('reports', 'Sales စစ်ဆေး ကြည့်', 'View sales report', '查看销售报告', False),
 'users.manage': ('admin', 'အသုံးပြုသူများ စီမံ', 'Manage users', '管理用户', False),
 'users.leader_toggle': ('admin', 'Leader ခန့်/ဖြုတ်', 'Toggle team leader', '任免组长', False),
 'roles.manage': ('admin', 'အခန်း (Roles) စီမံ', 'Manage roles', '管理角色', False),
 'departments.manage': ('admin', 'ဌာနများ စီမံ', 'Manage departments', '管理部门', False),
 'areas.manage': ('admin', 'ရပ်ကွက် စီမံ', 'Manage areas', '管理区域', False),
 'products.view': ('products', 'ပစ္စည်း စာရင်း ကြည့်', 'View products', '查看商品', False),
 'products.create': ('products', 'ပစ္စည်း အသစ် ဆောက်', 'Create products', '新建商品', False),
 'products.edit': ('products', 'ပစ္စည်း ပြင် (မိမိ အပိုင်း)', 'Edit products (own part)', '编辑商品(负责部分)', False),
 'products.data_edit': ('products', 'ပစ္စည်း အချက်အလက် ပြင်', 'Edit product data', '编辑商品资料', False),
 'prices.edit': ('products', 'ဈေးနှုန်း ပြင်', 'Edit prices', '编辑价格', False),
 'actual.edit': ('products', 'အရင်းအမှန် ပြင်', 'Edit true cost', '编辑真实成本', True),
 'prices.view.base': ('products', 'Base ဈေး ကြည့်', 'View Base price', '查看 Base 价格', False),
 'actual.view': ('products', 'အရင်းအမှန် ကြည့်', 'View true cost', '查看真实成本', True),
 'actual.view.settlement': ('buy', 'ရှင်းရန် ထဲမှာ အရင်းအမှန် ကြည့်', 'View true cost in settlements', '在结算中查看真实成本', True),
 'base.view.leader': ('products', 'Leader အတွက် Base ဈေး ကြည့်', 'Leader Base view', '组长 Base 视图', False),
 'products.sellable_toggle': ('products', 'ရောင်းနိုင်/မရောင်း ဖွင့်/ပိတ်', 'Toggle sellable', '开关可售', False),
 'suppliers.toggle': ('products', 'Supplier ဖွင့်/ပိတ်', 'Toggle supplier', '开关供应商', False),
 'stock.adjust': ('stock', 'Stock ပြင်', 'Adjust stock', '调整库存', False),
 'products.pack_link': ('products', 'Pack ချိတ်', 'Link packs', '关联包装', False),
 'products.pack_unlink': ('products', 'Pack ဖြုတ်', 'Unlink packs', '解除包装关联', False),
 'purchases.view': ('buy', 'အဝယ် မှတ်တမ်း ကြည့်', 'View purchases', '查看采购', False),
 'purchases.create_direct': ('buy', 'အဝယ် တိုက်ရိုက် သွင်း', 'Enter direct purchase', '直接录入采购', False),
 'purchases.extra_edit': ('buy', 'အဝယ် အပို ကုန်ကျစရိတ် ပြင်', 'Edit purchase extra cost', '编辑采购附加费用', False),
 'purchases.fx_edit': ('buy', 'အဝယ် ငွေလဲ မှတ်တမ်း ပြင်', 'Edit purchase currency note', '编辑采购币种备注', False),
 'purchases.check': ('buy', 'အဝယ် စစ်ပြီး မှတ်', 'Check purchases', '核对采购', False),
 'purchases.cancel': ('buy', 'အဝယ် ပယ်ဖျက်', 'Cancel purchases', '取消采购', False),
 'orders.view_list': ('orders', 'ဘောက်ချာ စာရင်း ကြည့်', 'View order list', '查看发票清单', False),
 'orders.create': ('orders', 'ဘောက်ချာ အသစ် ဆောက်', 'Create orders', '新建发票', False),
 'orders.create.credit_override': ('orders', 'ကြွေးကျော် ဘောက်ချာ ဆောက် (Owner)', 'Credit override', '超信用开单', False),
 'catalog.api_products': ('catalog', 'Catalog ဈေးများ (API) ရယူ', 'Catalog prices via API', '通过 API 获取目录价格', False),
 'catalog.api_outlets': ('catalog', 'ဆိုင် စာရင်း (API) ရယူ', 'Outlet list via API', '通过 API 获取店铺清单', False),
 'sales.api_my_orders': ('sales_pwa', 'ကိုယ်ပိုင် ဘောက်ချာ (API)', 'Own orders via API', '通过 API 获取个人发票', False),
 'sales.api_order_edits': ('sales_pwa', 'Offline ပြင်ချက် တင် (API)', 'Offline edits via API', '通过 API 提交离线修改', False),
 'sales.api_visit_probe': ('sales_pwa', 'ဆိုင် အနား ရောက် မှတ်တမ်း (API)', 'Visit probe via API', '通过 API 记录到店', False),
 'orders.view_detail': ('orders', 'ဘောက်ချာ အသေးစိတ် ကြည့်', 'View order detail', '查看发票详情', False),
 'orders.attach_outlet': ('orders', 'ဘောက်ချာကို ဆိုင် တွဲ', 'Attach outlet to order', '为发票关联店铺', False),
 'orders.status.deliver': ('orders', 'ပို့ပြီး မှတ်', 'Mark delivered', '标记已送达', False),
 'orders.status.cancel': ('orders', 'ဘောက်ချာ ပယ်ဖျက်', 'Cancel orders', '取消发票', False),
 'orders.status.paid_toggle': ('orders', 'ငွေရပြီး မှတ်', 'Mark paid', '标记已收款', False),
 'orders.edit': ('orders', 'ဘောက်ချာ ပြင်', 'Edit orders', '编辑发票', False),
 'payments.record': ('money', 'ငွေ ရရှိ မှတ်', 'Record payments', '记录收款', False),
 'payments.delete': ('money', 'ငွေ မှတ်တမ်း ဖျက်', 'Delete payments', '删除收款记录', False),
 'invoice.print': ('orders', 'Invoice ထုတ်', 'Print invoices', '打印发票', False),
 'delivery.view_board': ('orders', 'ပို့ဆောင် စာမျက်နှာ ကြည့်', 'View delivery board', '查看配送看板', False),
 'delivery.assign': ('orders', 'ပို့သူ တာဝန် ပေး', 'Assign deliverer', '指派配送员', False),
 'delivery.deliver': ('orders', 'ပို့ဆောင် (ပို့သူ/ရုံး)', 'Deliver orders', '配送', False),
 'delivery.offline': ('orders', 'Offline ပို့ပြီး မှတ်', 'Offline delivery', '离线配送', False),
 'gps.check_mark': ('orders', 'နေရာ စစ်ပြီး မှတ်', 'Mark GPS checked', '标记位置已核查', False),
 'gps_check.view': ('reports', 'နေရာ စစ်ရန် စာရင်း ကြည့်', 'View GPS-check list', '查看位置核查清单', False),
 'handover.view': ('money', 'ငွေ လွှဲအပ် ကြည့်', 'View handovers', '查看交款', False),
 'handover.verify': ('money', 'ငွေ လွှဲအပ် အတည်ပြု', 'Verify handovers', '核对交款', False),
 'returns.view': ('orders', 'ပြန်အပ် စာရင်း ကြည့်', 'View returns', '查看退货', False),
 'returns.record': ('orders', 'ပြန်အပ် မှတ်', 'Record returns', '记录退货', False),
 'returns.check': ('orders', 'ပြန်အပ် စစ်ပြီး မှတ်', 'Check returns', '核对退货', False),
 'returns.exchange_once': ('orders', 'ကုန် လဲ (တစ်ကြိမ်) မှတ်', 'Record goods exchange', '记录换货', False),
 'export.stock_xlsx': ('stock', 'Stock+ဈေး Excel ထုတ်', 'Export stock sheet', '导出库存表', False),
 'stock.view': ('stock', 'Stock မှတ်တမ်း ကြည့်', 'View stock ledger', '查看库存账', False),
 'catalog.view': ('catalog', 'Catalog ကြည့်', 'View catalog', '查看目录', False),
 'buy_group.set': ('buy', 'အဝယ် အုပ်စု ရွေး', 'Set buy group tab', '设置采购分组', False),
 'buy_list.view': ('buy', 'ဝယ်ရန် စာရင်း ကြည့်', 'View buy list', '查看采购清单', False),
 'buy_list.why': ('buy', 'ဝယ်ရန် အကြောင်း ကြည့်', 'View buy-list reasons', '查看采购原因', False),
 'requests.view': ('buy', 'အမှာ စာရင်း ကြည့်', 'View order requests', '查看订购单', False),
 'requests.create': ('buy', 'အမှာ အသစ် ဆောက်', 'Create order requests', '新建订购单', False),
 'requests.update': ('buy', 'အမှာ ပြင်', 'Update order requests', '编辑订购单', False),
 'requests.add_line': ('buy', 'အမှာ လိုင်း ထည့်', 'Add request lines', '添加订购行', False),
 'requests.remove_line': ('buy', 'အမှာ လိုင်း ဖြုတ်', 'Remove request lines', '删除订购行', False),
 'requests.cancel': ('buy', 'အမှာ ပယ်ဖျက်', 'Cancel order requests', '取消订购单', False),
 'requests.close': ('buy', 'အမှာ ပိတ်', 'Close order requests', '关闭订购单', False),
 'requests.receive': ('buy', 'ကုန်ရောက် သွင်း', 'Receive order requests', '收货录入', False),
 'requests.merge_choice': ('buy', 'အမှာ ပေါင်း ရွေး', 'Merge request choice', '选择合并订购单', False),
 'requests.from_buylist': ('buy', 'ဝယ်ရန် စာရင်းက အမှာ လုပ်', 'Requests from buy list', '从采购清单建订购单', False),
 'notifications.view': ('admin', 'အကြောင်းကြားစာ ကြည့်', 'View notifications', '查看通知', False),
 'audit.view': ('admin', 'မှတ်တမ်း စစ် ကြည့်', 'View audit log', '查看审计记录', False),
 'audit.handled': ('admin', 'မှတ်တမ်း ကိုင်တွယ်ပြီး မှတ်', 'Mark audit handled', '标记审计已处理', False),
 'settlements.view': ('buy', 'ရှင်းရန် ကြည့်', 'View settlements', '查看结算', False),
 'settlements.create': ('buy', 'ရှင်းရန် အသစ် ဆောက်', 'Create settlements', '新建结算', False),
 'settlements.edit_draft': ('buy', 'ရှင်းရန် မူကြမ်း ပြင်', 'Edit settlement drafts', '编辑结算草稿', False),
 'settlements.send': ('buy', 'ရှင်းရန် ပို့', 'Send settlements', '发送结算', False),
 'settlements.confirm': ('buy', 'ရှင်းရန် အတည်ပြု', 'Confirm settlements', '确认结算', False),
 'settlements.pay': ('buy', 'ရှင်းရန် ငွေ ပေး', 'Pay settlements', '支付结算', False),
 'money.view': ('money', 'ငွေ စာရင်း ကြည့်', 'View money book', '查看资金账', False),
 'money.manage': ('money', 'ငွေ စာရင်း မှတ်/ဖျက်', 'Manage money book', '管理资金账', False),
 'money.opening_set': ('money', 'ငွေ အဖွင့် လက်ကျန် သတ်', 'Set money opening', '设置资金期初', False),
 'cashbox.view': ('money', 'Cashbox ကြည့်', 'View cashbox', '查看现金箱', False),
 'cashbox.manage': ('money', 'Cashbox မှတ်/ရေတွက်', 'Manage cashbox', '管理现金箱', False),
 'cashbox.verify': ('money', 'Cashbox အတည်ပြု', 'Verify cashbox close', '核对现金箱结账', False),
 'stock_count.view': ('stock', 'Stock စစ်ဆေး ကြည့်', 'View stock counts', '查看盘点', False),
 'stock_count.create': ('stock', 'Stock စစ်ဆေး အသစ်', 'Create stock counts', '新建盘点', False),
 'stock_count.save': ('stock', 'Stock စစ်ဆေး သိမ်း', 'Save stock counts', '保存盘点', False),
 'stock_count.approve': ('stock', 'Stock စစ်ဆေး အတည်ပြု', 'Approve stock counts', '批准盘点', False),
 'stock_count.delete': ('stock', 'Stock စစ်ဆေး ဖျက်', 'Delete stock counts', '删除盘点', False),
 'expiry.view': ('stock', 'သက်တမ်း နီးနေတာ ကြည့်', 'View expiry list', '查看临期清单', False),
 'tax.view': ('money', 'အခွန် အစီရင်ခံ ကြည့်', 'View tax report', '查看税务报告', False),
 'tax.rate_set': ('money', 'အခွန် နှုန်း သတ်', 'Set tax rate', '设置税率', False),
 'backups.view': ('admin', 'Backup များ ကြည့်', 'View backups', '查看备份', False),
 'backups.make': ('admin', 'Backup ဆွဲ', 'Make backups', '创建备份', False),
 'backups.download': ('admin', 'Backup ဒေါင်း', 'Download backups', '下载备份', False),
 # v85: Restore (Owner only — the owner bundle is all keys except the five
 # excluded, so the owner seed picks this up on boot; the finance and
 # warehouse bundles are explicit lists and do NOT include it).
 'backups.restore': ('admin', 'Restore လုပ်', 'Restore from backup', '从备份恢复', False),
 'ask.view': ('reports', 'Ai မေးကြည့်', 'Ask AI', '问 AI', False),
 'ask.prices_manage': ('reports', 'Ai ဈေး မှတ်တမ်း စီမံ', 'Manage ask prices', '管理 AI 价格记录', False),
 # v79: Daily snapshot file (builder + writer land separately; this key only gates it).
 'snapshot.daily': ('reports', 'နေ့စဉ် ဖိုင် Download', 'Daily snapshot file', '每日快照文件', False),
 # v91: Product Data Analysis (Ko, 2026-10-29) — Owner + Finance only, because
 # the profit view reveals cost.
 'analysis.view': ('reports', 'အရောင်း ခွဲခြမ်းစိတ်ဖြာ ကြည့်', 'View sales analysis', '查看销售分析', False),
}
PERM_GROUP_LABELS = {
 'dashboard': ('ပင်မ', 'Dashboard', '主页'),
 'products': ('ပစ္စည်း & ဈေး', 'Products & prices', '商品与价格'),
 'stock': ('Stock', 'Stock', '库存'),
 'buy': ('အဝယ် & ရှင်းရန်', 'Buying & settlements', '采购与结算'),
 'orders': ('ဘောက်ချာ & ပို့ဆောင်', 'Orders & delivery', '发票与配送'),
 'money': ('ငွေ & Cashbox', 'Money & cashbox', '资金与现金箱'),
 'reports': ('အစီရင်ခံ & စစ်ဆေး', 'Reports & checks', '报告与核查'),
 'catalog': ('Catalog', 'Catalog', '目录'),
 'sales_pwa': ('အရောင်း ဖုန်း (API)', 'Sales phone APIs', '销售手机接口'),
 'admin': ('စီမံ', 'Admin', '管理'),
}
PERM_GROUP_ORDER = ['dashboard', 'products', 'stock', 'buy', 'orders', 'money', 'reports', 'catalog', 'sales_pwa', 'admin']
# v75 bundles — EXACT preservation of audit §8. Owner is an explicit list
# (all keys except the five it never had), NOT a wildcard, so Owner still
# 403s on the sales-only PWA APIs and still lacks the sales/delivery
# dashboards. Seeded into role_permissions at startup; afterwards the DB
# rows are the truth and this tuple is only the seed.
_OWNER_EXCLUDED = {'dashboard.sales_own', 'dashboard.delivery_own', 'sales.api_my_orders', 'sales.api_order_edits', 'sales.api_visit_probe'}
ROLE_BUNDLES = {
 'owner': tuple(k for k in PERMISSIONS if k not in _OWNER_EXCLUDED),
 'finance': ('auth.any', 'dashboard.office', 'dashboard.pills.finance', 'dashboard.pills.gps', 'dashboard.position', 'outlets.view', 'outlets.manage', 'profit.view', 'expenses.view', 'expenses.manage', 'at_risk.view', 'at_risk.mark', 'visits.view', 'sales_report.view', 'products.view', 'products.edit', 'prices.edit', 'actual.edit', 'prices.view.base', 'actual.view', 'actual.view.settlement', 'purchases.view', 'purchases.extra_edit', 'purchases.fx_edit', 'purchases.check', 'orders.view_list', 'catalog.api_products', 'catalog.api_outlets', 'orders.view_detail', 'orders.attach_outlet', 'orders.status.paid_toggle', 'payments.record', 'payments.delete', 'invoice.print', 'delivery.view_board', 'gps.check_mark', 'gps_check.view', 'handover.view', 'handover.verify', 'returns.view', 'returns.check', 'export.stock_xlsx', 'stock.view', 'catalog.view', 'buy_group.set', 'buy_list.view', 'buy_list.why', 'requests.view', 'notifications.view', 'settlements.view', 'money.view', 'money.manage', 'cashbox.view', 'cashbox.manage', 'cashbox.verify', 'stock_count.view', 'expiry.view', 'tax.view', 'ask.view', 'ask.prices_manage', 'snapshot.daily', 'analysis.view'),
 'warehouse': ('auth.any', 'dashboard.office', 'dashboard.pills.gps', 'products.view', 'products.create', 'products.edit', 'products.data_edit', 'prices.view.base', 'actual.view.settlement', 'products.sellable_toggle', 'suppliers.toggle', 'stock.adjust', 'products.pack_link', 'purchases.view', 'purchases.create_direct', 'purchases.extra_edit', 'purchases.fx_edit', 'purchases.cancel', 'orders.view_list', 'orders.create', 'catalog.api_products', 'catalog.api_outlets', 'orders.view_detail', 'orders.attach_outlet', 'orders.status.deliver', 'orders.status.cancel', 'orders.edit', 'invoice.print', 'delivery.view_board', 'delivery.assign', 'delivery.deliver', 'gps.check_mark', 'gps_check.view', 'returns.view', 'returns.record', 'returns.exchange_once', 'export.stock_xlsx', 'stock.view', 'catalog.view', 'buy_group.set', 'buy_list.view', 'buy_list.why', 'requests.view', 'requests.create', 'requests.update', 'requests.add_line', 'requests.remove_line', 'requests.cancel', 'requests.close', 'requests.receive', 'requests.merge_choice', 'requests.from_buylist', 'notifications.view', 'settlements.view', 'settlements.create', 'settlements.edit_draft', 'settlements.send', 'settlements.confirm', 'settlements.pay', 'stock_count.view', 'stock_count.create', 'stock_count.save', 'stock_count.delete', 'expiry.view', 'snapshot.daily'),
 'sales': ('auth.any', 'dashboard.sales_own', 'orders.view_list', 'orders.create', 'catalog.api_products', 'catalog.api_outlets', 'sales.api_my_orders', 'sales.api_order_edits', 'sales.api_visit_probe', 'orders.view_detail', 'orders.attach_outlet', 'orders.edit', 'catalog.view'),
 'delivery': ('auth.any', 'dashboard.delivery_own', 'orders.view_list', 'catalog.api_outlets', 'orders.view_detail', 'delivery.deliver', 'delivery.offline'),
}
SYSTEM_ROLES = ('owner', 'finance', 'warehouse', 'sales', 'delivery')
SEED_DEPARTMENTS = ('Admin', 'Accounts', 'Logistics', 'Sales', 'Delivery')

def user_role_name(u):
    """Data role name for a user row (roles table), else legacy users.role."""
    if not u:
        return ''
    try:
        rid = u['role_id']
    except Exception:
        rid = None
    if rid:
        try:
            r = db().execute("SELECT name FROM roles WHERE id=?", (rid,)).fetchone()
            if r: return r['name']
        except Exception:
            pass
    try:
        return u['role'] or ''
    except Exception:
        return ''

def user_perms(u):
    """Permission-key set for a user, read from the DATA role."""
    if not u:
        return set()
    try:
        uid = u['id']
        memo = g.get('_perms_memo')
    except Exception:
        uid = None; memo = None
    if memo and memo[0] == uid:
        return set(memo[1])
    perms = set()
    try:
        rid = u['role_id']
    except Exception:
        rid = None
    if rid:
        try:
            perms = {r[0] for r in db().execute("SELECT perm_key FROM role_permissions WHERE role_id=?", (rid,)).fetchall()}
        except Exception:
            perms = set()
    if not perms:
        # Fallback only for rows not yet mapped to a data role (first
        # startup before ensure_perm_data runs, or a legacy row). The
        # mapping in init_db removes this path for every known role.
        try:
            perms = set(ROLE_BUNDLES.get(u['role'], ()))
        except Exception:
            perms = set()
    # Leader add-on is a FLAG, not a role: sales + is_leader widens the
    # order view and grants the Base-only leader page (still switch-gated
    # at the route). It never grants edit/print/deliver/mark-paid.
    try:
        if is_leader(u) and user_role_name(u) == 'sales':
            perms.add('leader_prices.view'); perms.add('base.view.leader')
    except Exception:
        pass
    # Warehouse settlement Actual is conditional on the flag: if the flag
    # is ever flipped off, the settlement per-line key stops granting.
    if user_role_name(u) == 'warehouse' and not WAREHOUSE_SEES_ACTUAL:
        perms.discard('actual.view.settlement')
    if uid is not None:
        try: g._perms_memo = (uid, perms)
        except Exception: pass
    return set(perms)

def has_perm(u, key):
    return key in user_perms(u)

def can(key):
    """Current-user permission check for routes (g.user set by decorator)."""
    u = None
    try: u = g.user
    except Exception: u = None
    if u is None:
        try: u = current_user()
        except Exception: u = None
    return has_perm(u, key)

def require_perm(*keys):
    """Route gate: any authenticated user holding ANY of the keys (OR,
    matching the old multi-role decorator semantics). No keys = any
    authenticated user. Wrong permission -> generic 403 (never names the
    missing key, per the secrecy rule)."""
    def deco(fn):
        @wraps(fn)
        def wrap(*a, **kw):
            u = current_user()
            if not u:
                return redirect(url_for('login', next=request.path))
            if keys and not any(has_perm(u, k) for k in keys):
                abort(403)
            g.user = u
            return fn(*a, **kw)
        return wrap
    return deco

def ensure_perm_data(con):
    """Guarded v75 migration (safe on existing DBs, no data wiped):
    roles + role_permissions + departments tables; users.role_id /
    department_id columns; system-role seeds (only when empty / missing
    bundle rows); department seeds (only when empty); map every user
    whose role_id is NULL from their legacy users.role string."""
    con.execute("CREATE TABLE IF NOT EXISTS roles(id INTEGER PRIMARY KEY, name TEXT UNIQUE NOT NULL, is_system INTEGER DEFAULT 0)")
    con.execute("CREATE TABLE IF NOT EXISTS role_permissions(role_id INTEGER NOT NULL REFERENCES roles(id), perm_key TEXT NOT NULL, PRIMARY KEY(role_id, perm_key))")
    con.execute("CREATE TABLE IF NOT EXISTS departments(id INTEGER PRIMARY KEY, name TEXT UNIQUE NOT NULL)")
    ucols = [r[1] for r in con.execute("PRAGMA table_info(users)")]
    if 'role_id' not in ucols:
        con.execute("ALTER TABLE users ADD COLUMN role_id INTEGER")
    if 'department_id' not in ucols:
        con.execute("ALTER TABLE users ADD COLUMN department_id INTEGER")
    for rn in SYSTEM_ROLES:
        con.execute("INSERT OR IGNORE INTO roles(name, is_system) VALUES(?,1)", (rn,))
    for rn, bundle in ROLE_BUNDLES.items():
        rid = con.execute("SELECT id FROM roles WHERE name=?", (rn,)).fetchone()[0]
        for k in bundle:
            con.execute("INSERT OR IGNORE INTO role_permissions(role_id, perm_key) VALUES(?,?)", (rid, k))
    for dn in SEED_DEPARTMENTS:
        con.execute("INSERT OR IGNORE INTO departments(name) VALUES(?)", (dn,))
    con.execute("UPDATE users SET role_id=(SELECT id FROM roles WHERE roles.name=users.role) WHERE role_id IS NULL AND users.role IN (SELECT name FROM roles)")
    con.commit()



# ---------------- i18n (v43, Ko 2026-10-18) ----------------
# Full Myanmar/English/Chinese UI (zh added v56). TRANS is keyed by the (plain-language) Burmese
# string, which doubles as the 'my' text; an entry may also override 'my'
# (for keys that are English words in the original UI). User-entered data and
# printed documents are NEVER translated.
# ==== TRANS BEGIN ====
TRANS = {
 "ရောင်း နှင့် ဆိုင်": {
  "en": "Sell & Shops",
  "zh": "销售与店铺"
 },
 "ငွေ": {
  "en": "Money",
  "zh": "钱"
 },
 "စစ်ဆေး": {
  "en": "Checks",
  "zh": "检查"
 },
 "စီမံ": {
  "en": "Manage",
  "zh": "管理"
 },
 "ပို့ဆောင်": {
  "en": "Delivery",
  "zh": "送货"
 },
 " (Order Request တစ်ခု ဒါမှ မဟုတ် OR မပါတဲ့ PUR တစ်ခု) — batch တွေ ပေါင်း မရှင်းဘူး။ ကျသင့် = စစ်ပြီး PUR (": {
  "en": " (one Order Request, or one PUR with no OR) — batches are never merged. Payable = checked PURs (worked out at ",
  "zh": " (一张 Order Request,或一张没有 OR 的 PUR) — 批次从不合并结算。应付 = 已核对的 PUR (按 "
 },
 " (ရက် မခွဲ) — ": {
  "en": " (any day) — ",
  "zh": " (不限日期) — "
 },
 " + ပြင်ဆင် {adj}": {
  "en": " + adjustment {adj}",
  "zh": " + 调整 {adj}"
 },
 " = order မပါပေမယ့် Catalog ဖွင့်ချိန် ဆိုင် အနား (30 m အတွင်း) မှာ တွေ့လို့ မှတ်ထားတာ။ တစ်ရက် + ဆိုင် တစ်ဆိုင် + rep တစ်ယောက် = မှတ်တမ်း တစ်ခု တည်း။": {
  "en": " = no order, but the Catalog was opened near the shop (within 30 m), so it was noted. One day + one shop + one rep = a single record.",
  "zh": " = 没有订单,但打开 Catalog 时在店铺附近(30 m 以内),所以记了下来。一天 + 一家店 + 一名业务员 = 只记一条。"
 },
 " = အရင်းအောက်တော့ မဟုတ်ပေမယ့် အမြတ် လျော့သွားပြီ။": {
  "en": " = not below cost, but the profit has shrunk.",
  "zh": " = 还没到成本以下,但利润已经变少了。"
 },
 " = အသားတင် ရောင်းဈေးက လက်ရှိ အရင်းအောက် ရောက်နေတယ် (ဒီနေ့ ဝယ်ဈေးနဲ့ဆို ရှုံးမယ်) ·": {
  "en": " = the net selling price is now below the current cost (you'd lose at today's buy price) ·",
  "zh": " = 净售价现在已经低于当前成本(按今天的买价算会亏) ·"
 },
 " · [Offline ဖုန်းတန်းစီမှ]": {
  "en": " · [from the offline phone queue]",
  "zh": " · [来自离线手机队列]"
 },
 " · {rej} ခု မဝင်လိုက် ⚠️ (အောက်မှာ အကြောင်းပြ ထားတယ်)": {
  "en": " · {rej} did not go through ⚠️ (reasons below)",
  "zh": " · {rej} 条没有通过 ⚠️ (原因在下面)"
 },
 " · ငွေရပြီး": {
  "en": " · paid",
  "zh": " · 已付款"
 },
 " · ဈေးအချိုး {n}": {
  "en": " · price ratio {n}",
  "zh": " · 价格比例 {n}"
 },
 " · ပါဝင်မှု {n}": {
  "en": " · pack size {n}",
  "zh": " · 每包数量 {n}"
 },
 " · ပြင်ခွင့် သုံးပြီး {n}/2": {
  "en": " · edits used {n}/2",
  "zh": " · 已用修改次数 {n}/2"
 },
 " · 🖨 ပုံနှိပ်ပြီး": {
  "en": " · 🖨 printed",
  "zh": " · 🖨 已打印"
 },
 " ကိုပဲ ထည့်ပါ (မှာတာထက် နည်းလည်း ရတယ် — ကျန်တာက အမှာမှာ ဆက် ကျန်နေမယ်)။ သွင်းလိုက်ရင် PUR တစ်ခု ဖြစ်သွားပြီး stock ဝင်မယ်၊ Finance စစ်ရန် ကျန်မယ်။": {
  "en": " only (less than ordered is fine — the rest stays open on the request). Receiving creates a PUR, stock comes in, and it waits for Finance to check.",
  "zh": " 就行(比订的数量少也可以 — 剩下的继续留在订购单上)。录入后会生成一张 PUR,库存进来,等 Finance 核对。"
 },
 " နဲ့ ": {
  "en": " and ",
  "zh": " 和 "
 },
 " နဲ့ auto တွက်တယ် (Actual မရှိတဲ့ ပစ္စည်း ဆို မှတ်လို့ မရဘူး — Finance/Owner အရင် ဖြည့်ရမယ်)။": {
  "en": " (a product with no Actual cannot be recorded — Finance/Owner must fill it first).",
  "zh": " 自动计算(没有 Actual 价格的商品不能记录 — 要先由 Finance/Owner 填)。"
 },
 " နဲ့ တွက်) − Supplier ပြန်ပို့ + ပြင်ဆင် ပမာဏ။": {
  "en": ") − supplier returns + adjustment.",
  "zh": " 计算) − 供应商退货 + 调整金额。"
 },
 " ပဲ — stock မဝင်သေးဘူး။ ကုန် တကယ် ရောက်မှ အမှာပေါ်ကနေ ကုန်ရောက် သွင်း (PUR) လုပ်ရမယ်။": {
  "en": " placed with a supplier — no stock has come in yet. Only when the goods actually arrive do you receive them (PUR) from the request.",
  "zh": " — 库存还没有进来。只有货真正到了,才能从订购单收货(PUR)。"
 },
 " ပြင်လို့ရတယ် (၀ ထည့်ရင် အဲဒီ line ဖြုတ်မယ်)။ ပြင်တိုင်း မှတ်တမ်း ကျန်ပြီး Warehouse/Finance ဆီ အသိပေးမယ်။ PV နံပါတ် မပြောင်းဘူး။": {
  "en": " (enter 0 to remove that line). Every edit is recorded and Warehouse/Finance are notified. The PV number does not change.",
  "zh": " 可以修改(填 0 就删掉那一行)。每次修改都有记录,并会通知 Warehouse/Finance。PV 编号不变。"
 },
 " ပြင်လို့ရတယ် (၀ ထည့်ရင် အဲဒီ line ဖြုတ်မယ်)။ ပြင်တိုင်း မှတ်တမ်း ကျန်ပြီး Warehouse/Finance ဆီ အသိပေးမယ်။ ဘောက်ချာ နံပါတ် မပြောင်းဘူး။": {
  "en": " (enter 0 to remove that line). Every edit is recorded and Warehouse/Finance are notified. The voucher number does not change.",
  "zh": " 可以修改(填 0 就删掉那一行)。每次修改都有记录,并会通知 Warehouse/Finance。凭证编号不变。"
 },
 " ပြထားတယ် — Supplier တစ်ခု ချင်းစီ အတွက် အမှာ (Order Request) သီးခြား တင်ရမယ်။": {
  "en": " — raise a separate order request (Order Request) for each supplier.",
  "zh": " 显示 — 每个 Supplier 要单独建一张订购单(Order Request)。"
 },
 " ဘောက်ချာတွေကို ဘောက်ချာ ရက် ရှိတဲ့ လနဲ့ တွက်တယ်။ အရင်း ကို နှစ်မျိုး ပြထားတယ် — Actual ဈေး နဲ့ Base ဈေး (ရောင်းတဲ့ အချိန်က မှတ်ထားတဲ့ ဈေး ရှိရင် အဲ့ဒါ၊ မရှိသေးတဲ့ အရင် ဘောက်ချာတွေဆို လက်ရှိ ဈေး နဲ့ တွက်တယ်)။ Net အမြတ် = အရောင်း − အရင်း − အဝယ် အပို ကုန်ကျ − အသုံးစရိတ်။": {
  "en": " invoices are counted in the month of the invoice date. Cost is shown two ways — Actual and Base (the price recorded at sale time when there is one; older invoices without one use the current price). Net profit = Sales − Cost − Extra buying cost − Expenses.",
  "zh": " 发票按发票日期所在的月份计算。成本有两种显示 — Actual 价格和 Base 价格(有销售时记录的价格就用那个;没有的旧发票用当前价格算)。净利润 = 销售额 − 成本 − 采购额外费用 − 开支。"
 },
 "\"{name}\" ဆိုတဲ့ အရေး က အခြား ဆိုင် မှာ မှတ်ပြီး သား မို့ အခြား အရေး အဖြစ် မထည့်လိုက်ဘူး": {
  "en": "The spelling \"{name}\" is already recorded for another shop, so it was not kept as another spelling",
  "zh": "\"{name}\"这个写法已经登记在另一家店了,所以没有保留为其他写法"
 },
 "(Owner / Finance သာ နှိပ်နိုင်တယ်)": {
  "en": "(only Owner / Finance can press this)",
  "zh": "(只有 Owner / Finance 能按)"
 },
 "(Supplier ပိတ်ထားသည်)": {
  "en": "(supplier closed)",
  "zh": "(供应商已关闭)"
 },
 "(stock ပြန် နှုတ်ပြီးပြီ)": {
  "en": "(stock already taken back)",
  "zh": "(库存已扣回)"
 },
 "({n} ကြောင်း)": {
  "en": "({n} entries)",
  "zh": "({n} 条)"
 },
 "({n} ခု တင်ရန် ကျန်တယ်)": {
  "en": "({n} waiting to send)",
  "zh": "({n} 张待发送)"
 },
 "({n} စောင်)": {
  "en": "({n})",
  "zh": "({n} 张)"
 },
 "({n} မျိုး)": {
  "en": "({n} types)",
  "zh": "({n} 种)"
 },
 "(စုစုပေါင်း)": {
  "en": "(total)",
  "zh": "(总计)"
 },
 "(ဒီ စာမျက်နှာမှာ ဒီနေ့ ဘောက်ချာတွေပဲ ပြထားတယ်)": {
  "en": "(this page shows only today's invoices)",
  "zh": "(本页只显示今天的发票)"
 },
 "(ပစ္စည်း အတန်း ချင်းမှာ ထည့်)": {
  "en": "(enter it per product line)",
  "zh": "(按商品逐行填)"
 },
 "(ပစ္စည်း အတန်း ချင်းမှာ ထည့်ရန်)": {
  "en": "(enter it per product line)",
  "zh": "(按商品逐行填)"
 },
 "(ပြင်လို့ မရတော့)": {
  "en": "(cannot edit anymore)",
  "zh": "(不能再修改)"
 },
 "(ဘဏ် လက်ကျန် မဟုတ် — ဒီလ ရဲ့ ငွေ ရွေ့လျားမှု ပဲ)": {
  "en": "(not a bank balance — just this month's cash movement)",
  "zh": "(不是银行余额 — 只是本月的资金流动)"
 },
 "(ဘောက်ချာ {c} စောင်, ပယ်ဖျက် မပါ)": {
  "en": "({c} invoices, cancelled not counted)",
  "zh": "({c} 张发票,不含已取消)"
 },
 "(ဘောက်ချာ နံပါတ် မပါ)": {
  "en": "(no invoice number)",
  "zh": "(无发票号)"
 },
 "(မဖြစ်မနေ)": {
  "en": "(required)",
  "zh": "(必填)"
 },
 "(မသိ)": {
  "en": "(unknown)",
  "zh": "(未知)"
 },
 "(မှတ်ချိန် Actual: {amt})": {
  "en": "(Actual when marked: {amt})",
  "zh": "(标记时的 Actual:{amt})"
 },
 "(မှတ်တမ်း {n} ခု ပျမ်းမျှ)": {
  "en": "(average of {n} records)",
  "zh": "({n} 条记录的平均)"
 },
 "(ရှိရင်)": {
  "en": "(if any)",
  "zh": "(如有)"
 },
 "(လျှော့တာ/ပိုတာရှိရင်)": {
  "en": "(if reduced/added)",
  "zh": "(如有增减)"
 },
 "+ Order အသစ်": {
  "en": "+ New order",
  "my": "+ ဘောက်ချာ အသစ်",
  "zh": "+ 新订单"
 },
 "+ ကြို အမှာ အသစ်": {
  "en": "+ New pre-order",
  "zh": "+ 新预先订购"
 },
 "+ ထည့်မယ်": {
  "en": "+ Add",
  "zh": "+ 添加"
 },
 "+ နောက် ၁ မျိုး": {
  "en": "+ Another item",
  "zh": "+ 再加一种"
 },
 "+ ပစ္စည်း ထပ်ထည့်": {
  "en": "+ Add another product",
  "zh": "+ 再加商品"
 },
 "+ ပစ္စည်း အသစ်": {
  "en": "+ New product",
  "zh": "+ 新商品"
 },
 "+ ပြန်ပို့ မှတ် (stock နုတ်မယ်)": {
  "en": "+ Record return (deduct stock)",
  "zh": "+ 记录退货(扣库存)"
 },
 "+ ဘောက်ချာ အသစ်": {
  "en": "+ New invoice",
  "zh": "+ 新发票"
 },
 "+ အမှာ အသစ်": {
  "en": "+ New request",
  "zh": "+ 新订购单"
 },
 "+ အဝယ် အသစ် သွင်းမယ်": {
  "en": "+ Record a new purchase",
  "zh": "+ 录入新采购"
 },
 "= အဲဒီ ဆိုင်မှာ order တင်ခဲ့တယ်။ 🔵 ": {
  "en": "= an order was placed at that shop. 🔵 ",
  "zh": "= 在这家店下过订单。 🔵 "
 },
 "Account အသစ် ဆောက်မယ်": {
  "en": "Create new account",
  "zh": "新建 Account 账户"
 },
 "Actual Base (အရင်းအမှန် ★)": {
  "en": "Actual Base (true cost ★)",
  "zh": "Actual Base (真实成本 ★)"
 },
 "Actual စုစုပေါင်း": {
  "en": "Actual total",
  "zh": "Actual 总计"
 },
 "Actual ဈေး": {
  "en": "Actual price",
  "zh": "Actual 价格"
 },
 "Actual ဈေး မဖြည့်ရသေးတဲ့ ပစ္စည်း ရှိတယ်: {names} — Finance/Owner ကို အကြောင်းကြားပါ": {
  "en": "Some products have no Actual price yet: {names} — please tell Finance/Owner",
  "zh": "有些商品还没填 Actual 价格:{names} — 请通知 Finance/Owner"
 },
 "Actual ဈေး ★": {
  "en": "Actual price ★",
  "zh": "Actual 价格 ★"
 },
 "Actual ဈေးနဲ့ တွက် အသေးစိတ် (ရှင်းရန် အခြေခံ)": {
  "en": "Worked out at Actual prices (settlement basis)",
  "zh": "按 Actual 价格计算的明细(结算依据)"
 },
 "Actual တွက် စုစုပေါင်း {gross} − ပြန်ပို့ {rv}": {
  "en": "Actual total {gross} − returns {rv}",
  "zh": "Actual 总计 {gross} − 退货 {rv}"
 },
 "B2C (မှတ်ရန်သီးသန့်)": {
  "en": "B2C (reference only)",
  "zh": "B2C (仅供参考)"
 },
 "Backup မ ဆွဲ ရ သေး တာ ၁၅ ရက် ကျော် နေ ပြီ": {
  "en": "A backup copy has not been downloaded for over 15 days",
  "zh": "备份副本已经超过 15 天没有下载了"
 },
 "Backup မိတ္တူ ကို ကိုယ့် ဖုန်း/ကွန်ပျူတာ ထဲ download ဆွဲ မထား တာ ကြာပြီ (သို့) မဆွဲ ရသေးဘူး။ မိတ္တူ တွေ က server ပေါ်မှာ ပဲ ရှိတာ မို့ ၁၅ ရက် ၁ ကြိမ် လောက် download ဆွဲ ထား ပါ။": {
  "en": "It has been a while since a backup copy was downloaded to your own phone/computer (or never). The copies live only on the server, so download one about every 15 days.",
  "zh": "备份副本已经很久没有下载到自己的手机/电脑了(或者从来没下载过)。副本只在服务器上,所以请大约每 15 天下载一次。"
 },
 "Backup အကြောင်း": {
  "en": "About the backup",
  "zh": "关于备份"
 },
 "Banking": {
  "en": "Banking",
  "my": "Banking (ဘဏ်)",
  "zh": "银行转账 (Banking)"
 },
 "Base Price (အရင်း — လုပ်ငန်းသုံး)": {
  "en": "Base Price (working cost)",
  "zh": "Base Price (成本 — 工作用)"
 },
 "Cash": {
  "en": "Cash",
  "my": "ငွေသား (Cash)",
  "zh": "现金 (Cash)"
 },
 "Category": {
  "en": "Category",
  "my": "အမျိုးအစား",
  "zh": "类别"
 },
 "Category နဲ့ ပမာဏ (0 ထက် ကြီး) လိုပါတယ်": {
  "en": "Category and an amount (more than 0) are required",
  "my": "အမျိုးအစား နဲ့ ပမာဏ (0 ထက် ကြီး) လိုပါတယ်",
  "zh": "需要类别和金额(大于 0)"
 },
 "Category အကုန်": {
  "en": "All categories",
  "my": "အမျိုးအစား အကုန်",
  "zh": "全部类别"
 },
 "Credit (ငွေကျန်)": {
  "en": "Credit (balance)",
  "my": "ကြွေး (ငွေကျန်)",
  "zh": "欠款 (Credit)"
 },
 "Credit ကျန်ငွေ (လက်ရှိ, ဘောက်ချာ {c})": {
  "en": "Credit outstanding (now, {c} invoices)",
  "my": "ကြွေး ကျန်ငွေ (လက်ရှိ, ဘောက်ချာ {c})",
  "zh": "未结欠款(当前,{c} 张发票)"
 },
 "Delivery ကောက် ထားတဲ့ ငွေ {x} Ks အတည် ပြုရန် ကျန် နေ တယ် — ထပ် မမှတ် ပါနဲ့၊ အရင် Handover မှာ အတည် ပြုပါ": {
  "en": "The delivery person already collected {x} Ks — it is still waiting to be checked. Do not record it again; verify it at Handover first",
  "zh": "送货员已经收了 {x} Ks — 还在等核对。不要再记一次;先在交接时核对"
 },
 "Demand": {
  "en": "Demand",
  "my": "Demand",
  "zh": "需求"
 },
 "Demand Driven": {
  "en": "Demand Driven",
  "my": "Demand Driven",
  "zh": "需求驱动"
 },
 "Digital Pay": {
  "en": "Digital Pay",
  "my": "Digital Pay (ဖုန်းငွေ)",
  "zh": "电子支付 (Digital Pay)"
 },
 "Direct Driven": {
  "en": "Direct Driven",
  "my": "Direct Driven",
  "zh": "直接采购"
 },
 "Download ဆွဲမယ်": {
  "en": "Download",
  "zh": "下载"
 },
 "Effective": {
  "en": "Effective",
  "my": "သေချာ",
  "zh": "确定到访"
 },
 "Finance အတွက် —": {
  "en": "For Finance —",
  "zh": "给 Finance —"
 },
 "From": {
  "en": "From",
  "my": "မှ",
  "zh": "从"
 },
 "GPS မရဘူး — Location ဖွင့်ပြီး ထပ်စမ်းပါ": {
  "en": "Can't get GPS — turn Location on and try again",
  "zh": "拿不到 GPS — 打开定位后再试"
 },
 "GPS မရဘူး — ထပ်စမ်းပါ": {
  "en": "No GPS reading — please try again",
  "zh": "拿不到 GPS — 请再试一次"
 },
 "Leader ကြည့်ခွင့်": {
  "en": "Leader price access",
  "zh": "Leader 查看权限"
 },
 "Leader ခန့်ရန်": {
  "en": "Make Leader",
  "zh": "设为 Leader"
 },
 "Leader ဈေးနှုန်း ကြည့်ခွင့် ပြောင်းပြီးပြီ": {
  "en": "Leader price access changed",
  "zh": "Leader 价格查看权限已更改"
 },
 "Line ဖြုတ်ပြီးပြီ": {
  "en": "Line removed",
  "zh": "已删除该行"
 },
 "Location History": {
  "en": "Location History",
  "my": "ရောက် မှတ်တမ်း",
  "zh": "到访记录"
 },
 "Login": {
  "en": "Login",
  "my": "ဝင်မယ်",
  "zh": "登录"
 },
 "Logout": {
  "en": "Logout",
  "my": "ထွက်မယ်",
  "zh": "退出"
 },
 "Margin (Actual အခြေခံ ★) —": {
  "en": "Margin (on Actual ★) —",
  "zh": "利润率 (按 Actual ★) —"
 },
 "Margin (Base အခြေခံ) —": {
  "en": "Margin (on Base) —",
  "zh": "利润率 (按 Base) —"
 },
 "Net အမြတ် (Base)": {
  "en": "Net profit (Base)",
  "zh": "净利润 (Base)"
 },
 "Net အမြတ် (Margin)": {
  "en": "Net profit (Margin)",
  "zh": "净利润 (利润率)"
 },
 "Offline က တင်လာတာ — stock ထက် ပိုနေတယ်": {
  "en": "Came in from offline — quantity is more than the stock",
  "zh": "从离线提交来的 — 数量比库存多"
 },
 "Offline ပြင် (ပြောင်းလဲမှု မရှိ)": {
  "en": "Offline edit (no change)",
  "zh": "离线修改(没有变化)"
 },
 "Order": {
  "en": "Order",
  "my": "ဘောက်ချာ",
  "zh": "订单"
 },
 "Order (ရှိရင်)": {
  "en": "Order (if any)",
  "my": "ဘောက်ချာ (ရှိရင်)",
  "zh": "订单(如有)"
 },
 "Order ကို ပယ်ဖျက်မလား?": {
  "en": "Cancel this order?",
  "my": "ဘောက်ချာ ကို ပယ်ဖျက်မလား?",
  "zh": "要取消这个订单吗?"
 },
 "Order တင်ပြီးပြီ": {
  "en": "Order submitted",
  "my": "ဘောက်ချာ တင်ပြီးပြီ",
  "zh": "订单已提交"
 },
 "Order တင်မယ်": {
  "en": "Submit order",
  "my": "ဘောက်ချာ တင်မယ်",
  "zh": "提交订单"
 },
 "Order တင်ရင် ရွေးလို့ရတဲ့ ရပ်ကွက် စာရင်းပါ။ မှားတာတွေ ဒီကနေ ဖျက်လို့ရတယ် — ဖျက်လည်း အရင် Order တွေက မှတ်တမ်း မပျက်ဘူး။": {
  "en": "These are the areas you can pick when entering an order. Mistakes can be deleted here — deleting does not change the area recorded on old orders.",
  "zh": "这是下单时可以选择的街区列表。错的可以在这里删除 — 删除不会改变旧订单上记录的街区。"
 },
 "Order မရွေးရင် ဆိုင် နာမည် ရိုက်": {
  "en": "If no order picked, type the shop name",
  "zh": "如果没选订单,就输入店铺名字"
 },
 "Order အသစ်": {
  "en": "New order",
  "my": "ဘောက်ချာ အသစ်",
  "zh": "新订单"
 },
 "Orders": {
  "en": "Orders",
  "my": "ဘောက်ချာများ",
  "zh": "订单列表"
 },
 "Outlet": {
  "en": "Outlet",
  "my": "ဆိုင်",
  "zh": "店铺"
 },
 "Outlet (ဆိုင်နာမည်) *": {
  "en": "Outlet (shop name) *",
  "zh": "店铺 (店名) *"
 },
 "Outlet နဲ့ ပစ္စည်း အနည်းဆုံး ၁ မျိုး လိုပါတယ်": {
  "en": "An outlet and at least one product are required",
  "my": "ဆိုင် နဲ့ ပစ္စည်း အနည်းဆုံး ၁ မျိုး လိုပါတယ်",
  "zh": "需要店铺和至少一种商品"
 },
 "Owner (Ko) account ဆောက်ပါ။ ဒီ account က အကုန် လုပ်လို့ရတယ်။": {
  "en": "Create the Owner (Ko) account. This account can do everything.",
  "zh": "创建 Owner (Ko) 账户。这个账户可以做一切操作。"
 },
 "Owner / Finance သာ မြင်ရတယ်။ ကျန်ငွေ (Credit) အများဆုံး ဆိုင် အရင် စီထားတယ်။ ဆိုင် နာမည် နှိပ်ရင် အသေးစိတ် (ဘောက်ချာများ · Credit · ရောက် မှတ်တမ်း) ကြည့်လို့ရတယ်။": {
  "en": "Only Owner / Finance can see this. Shops are sorted by the most credit outstanding first. Tap a shop name for details (invoices · credit · visit history).",
  "my": "Owner / Finance သာ မြင်ရတယ်။ ကျန်ငွေ (ကြွေး) အများဆုံး ဆိုင် အရင် စီထားတယ်။ ဆိုင် နာမည် နှိပ်ရင် အသေးစိတ် (ဘောက်ချာများ · ကြွေး · ရောက် မှတ်တမ်း) ကြည့်လို့ရတယ်။",
  "zh": "只有 Owner / Finance 能看。按未结欠款从多到少排列店铺。点店铺名字可以看明细(发票 · 欠款 · 到访记录)。"
 },
 "Owner / Warehouse အကောင့်များမှ ထည့်သော ဘောက်ချာများ — Rep များ၏ အရောင်း မဟုတ်ပါ": {
  "en": "Invoices entered from Owner / Warehouse accounts — not rep sales",
  "zh": "从 Owner / Warehouse 账户录入的发票 — 不是业务员的销售"
 },
 "Owner account ဆောက်ပြီးပြီ — login ဝင်ပါ": {
  "en": "Owner account created — please log in",
  "zh": "Owner 账户已创建 — 请登录"
 },
 "Owner account ဆောက်မယ်": {
  "en": "Create owner account",
  "zh": "创建 Owner 账户"
 },
 "Owner ကို ဆက်သွယ်ပါ": {
  "en": "contact the Owner",
  "zh": "请联系 Owner"
 },
 "Password": {
  "en": "Password",
  "my": "စကားဝှက်",
  "zh": "密码"
 },
 "Password (အနည်းဆုံး ၆ လုံး)": {
  "en": "Password (at least 6 characters)",
  "zh": "密码 (至少 6 位)"
 },
 "Password ပြောင်းရန်": {
  "en": "Change password",
  "zh": "修改密码"
 },
 "Password အနည်းဆုံး ၆ လုံး ရှိရမယ်": {
  "en": "Password must be at least 6 characters",
  "zh": "密码至少要有 6 位"
 },
 "Password အသစ် ပြောင်းပြီးပြီ": {
  "en": "New password set",
  "zh": "新密码已设置"
 },
 "Password အသစ် ပြောင်းမှာ သေချာလား?": {
  "en": "Change to a new password — are you sure?",
  "zh": "确定要改成新密码吗?"
 },
 "Print ထုတ်ပြီးသား ဘောက်ချာ — အရောင်း ပြင်ခွင့် မရှိတော့ပါ (Warehouse ကို ဆက်သွယ်ပါ)": {
  "en": "This invoice is already printed — sales editing is closed (contact Warehouse)",
  "my": "ပုံနှိပ် ထုတ်ပြီးသား ဘောက်ချာ — အရောင်း ပြင်ခွင့် မရှိတော့ပါ (Warehouse ကို ဆက်သွယ်ပါ)",
  "zh": "这张发票已经打印过了 — 业务员不能再改(请联系 Warehouse)"
 },
 "Product name လိုပါတယ်": {
  "en": "Product name is required",
  "zh": "需要商品名称"
 },
 "Rep": {
  "en": "Rep",
  "my": "ရောင်းသူ",
  "zh": "业务员"
 },
 "Rep အလိုက် စွမ်းဆောင်ချက်": {
  "en": "Performance by rep",
  "zh": "按业务员的业绩"
 },
 "Rep အားလုံး": {
  "en": "All reps",
  "zh": "全部业务员"
 },
 "Returns / Damage (ပြန်အပ် / ပျက်စီး)": {
  "en": "Returns / Damage",
  "my": "ပြန်အပ် / ပျက်စီး",
  "zh": "退货 / 损坏 (Returns / Damage)"
 },
 "Returns စစ်ရန် ကျန်": {
  "en": "Returns to check",
  "my": "ပြန်အပ် စစ်ရန် ကျန်",
  "zh": "待核对的退货"
 },
 "Risk လျာထား": {
  "en": "Risk allowance",
  "zh": "Risk 预留"
 },
 "SKU ထွက်အား": {
  "en": "SKU movement",
  "zh": "SKU 出货量"
 },
 "SKU ပမာဏ နဲ့ ဘောက်ချာ အရေအတွက်မှာ ပယ်ဖျက် ဘောက်ချာများ မပါဝင်ပါ · ပမာဏ = ပစ္စည်းအလိုက် လျှော့ဈေး နှုတ်ပြီး အမှန် တန်ဖိုး": {
  "en": "Cancelled invoices are not counted in SKU quantities or invoice counts · Amount = the true value after per-line discounts",
  "zh": "已取消的发票不计入 SKU 数量和发票张数 · 金额 = 每行折扣后的真实金额"
 },
 "Sales စစ်ဆေး": {
  "en": "Sales check",
  "my": "အရောင်း စစ်ဆေး",
  "zh": "销售检查"
 },
 "Sales ပမာဏ": {
  "en": "Sales amount",
  "my": "အရောင်း ပမာဏ",
  "zh": "销售金额"
 },
 "Stock ကို {sku} အတွက် ပြင်ထားတာပါ": {
  "en": "Stock adjusted for {sku}",
  "my": "လက်ကျန် ကို {sku} အတွက် ပြင်ထားတာပါ",
  "zh": "已为 {sku} 调整库存"
 },
 "Stock စုစုပေါင်း": {
  "en": "Total stock",
  "zh": "库存总计"
 },
 "Stock ညှိ/ရေတွက်": {
  "en": "Stock adjust / count",
  "zh": "库存调整 / 盘点"
 },
 "Stock တန်ဖိုး (အရင်း နဲ့)": {
  "en": "Stock value (at cost)",
  "zh": "库存价值 (按成本)"
 },
 "Stock တန်ဖိုး ကို အရင်း အမှန် နဲ့ တွက်ထားတယ်။ ရရန်/ပေးရန် တွေ က စာရင်း ပေါ်က အရာ — ငွေ အဖြစ် မရ သေးဘူး။": {
  "en": "Stock value is counted at true cost. Amounts owed in and out are book figures — not cash in hand yet.",
  "zh": "库存价值按真实成本计算。应收/应付都是账面数字 — 还不是到手的现金。"
 },
 "Stock နည်းနေတာ ≤၅": {
  "en": "Low stock ≤5",
  "my": "လက်ကျန် နည်းနေတာ ≤၅",
  "zh": "库存偏低 ≤5"
 },
 "Stock ပြင်": {
  "en": "Adjust stock",
  "my": "လက်ကျန် ပြင်",
  "zh": "调整库存"
 },
 "Stock ပြင်ပြီးပြီ": {
  "en": "Stock adjusted",
  "my": "လက်ကျန် ပြင်ပြီးပြီ",
  "zh": "库存已调整"
 },
 "Stock ပြင်မှာ သေချာလား?": {
  "en": "Adjust stock — are you sure?",
  "my": "လက်ကျန် ပြင်မှာ သေချာလား?",
  "zh": "确定要调整库存吗?"
 },
 "Stock မှတ်တမ်း": {
  "en": "Stock ledger",
  "my": "လက်ကျန် မှတ်တမ်း",
  "zh": "库存记录"
 },
 "Stock ရှိတာ": {
  "en": "In stock",
  "my": "လက်ကျန် ရှိတာ",
  "zh": "有库存"
 },
 "Supplier က အတည်ပြုပြီးပြီလား?": {
  "en": "Has the supplier confirmed?",
  "zh": "供应商确认了吗?"
 },
 "Supplier ဆီ ပို့ပြီး 📤": {
  "en": "Sent to supplier 📤",
  "zh": "已发给供应商 📤"
 },
 "Supplier ဆီ ပြန်ပို့များ": {
  "en": "Returns to supplier",
  "zh": "退给供应商的货"
 },
 "Supplier ဆီ မှာမယ့် ပစ္စည်းတွေ — ဒါက အစီအစဉ် ပဲ၊ stock မဝင်သေးဘူး။ ဈေး နောက်မှ ကုန်ရောက် သွင်းတဲ့ အခါ ထည့်ရမယ်။": {
  "en": "Products to order from a supplier — this is just a plan, no stock comes in yet. Prices are entered later, when the goods arrive.",
  "zh": "要向 Supplier 订的商品 — 这只是计划,库存还没进来。价格等货到时再填。"
 },
 "Supplier တွေ ကို ပေးရန်": {
  "en": "Still owed to suppliers",
  "zh": "还欠供应商的"
 },
 "Supplier နာမည် ထည့်ပါ": {
  "en": "Enter the supplier name",
  "zh": "请输入供应商名字"
 },
 "Supplier ဘောက်ချာ နံပါတ် (ရှိမှ ထည့်)": {
  "en": "Supplier invoice number (only if there is one)",
  "zh": "供应商发票号(有才填)"
 },
 "Supplier ရှင်း": {
  "en": "Supplier paid",
  "zh": "供应商已付款"
 },
 "Supplier ရှင်းရန်": {
  "en": "Settle with suppliers",
  "zh": "供应商结算"
 },
 "Supplier အကုန်": {
  "en": "All suppliers",
  "zh": "全部供应商"
 },
 "Supplier အတည်ပြု": {
  "en": "Supplier confirmed",
  "zh": "供应商已确认"
 },
 "Supplier အတည်ပြု ✅": {
  "en": "Supplier confirmed ✅",
  "zh": "供应商已确认 ✅"
 },
 "Supplier အတည်ပြုပြီး အဖြစ် မှတ်ပြီ": {
  "en": "Marked as confirmed by the supplier",
  "zh": "已标记为供应商确认"
 },
 "Supplier အရောင်း အခြေအနေ": {
  "en": "Supplier sales status",
  "zh": "供应商销售状态"
 },
 "Team": {
  "en": "Team",
  "my": "အဖွဲ့",
  "zh": "团队"
 },
 "Team ({name}) စုစုပေါင်း": {
  "en": "Team ({name}) total",
  "my": "အဖွဲ့ ({name}) စုစုပေါင်း",
  "zh": "团队 ({name}) 总计"
 },
 "Team စုစုပေါင်း အရောင်း (ဘောက်ချာ {c} စောင်)": {
  "en": "Team total sales ({c} invoices)",
  "my": "အဖွဲ့ စုစုပေါင်း အရောင်း (ဘောက်ချာ {c} စောင်)",
  "zh": "团队总销售额 ({c} 张发票)"
 },
 "Team တပ်": {
  "en": "Team label",
  "my": "အဖွဲ့ တပ်",
  "zh": "团队标签"
 },
 "Team တပ် ပြောင်းပြီးပြီ (အတိတ် ဘောက်ချာတွေ မပြောင်း)": {
  "en": "Team label changed (past invoices keep their old label)",
  "my": "အဖွဲ့ တပ် ပြောင်းပြီးပြီ (အတိတ် ဘောက်ချာတွေ မပြောင်း)",
  "zh": "团队标签已更改 (旧发票保持原来的标签)"
 },
 "Team ဒီလ အရောင်း (ဘောက်ချာ {c} စောင်)": {
  "en": "Team sales this month ({c} invoices)",
  "my": "အဖွဲ့ ဒီလ အရောင်း (ဘောက်ချာ {c} စောင်)",
  "zh": "团队本月销售额 ({c} 张发票)"
 },
 "Team နှိုင်းယှဉ်": {
  "en": "Team comparison",
  "my": "အဖွဲ့ နှိုင်းယှဉ်",
  "zh": "团队对比"
 },
 "To": {
  "en": "To",
  "my": "အထိ",
  "zh": "到"
 },
 "Username": {
  "en": "Username",
  "my": "အသုံးပြုသူနာမည်",
  "zh": "用户名"
 },
 "Username / password မှားနေတယ်": {
  "en": "Wrong username or password",
  "zh": "用户名或密码不对"
 },
 "Username ပြင်ပြီးပြီ": {
  "en": "Username updated",
  "zh": "用户名已更新"
 },
 "Username ရှိပြီးသား ဖြစ်နေတယ်": {
  "en": "That username is already taken",
  "zh": "这个用户名已经被用了"
 },
 "Username, name နဲ့ password (အနည်းဆုံး ၆ လုံး) လိုပါတယ်": {
  "en": "Username, name and a password (at least 6 characters) are required",
  "zh": "需要用户名、名字和密码 (至少 6 位)"
 },
 "Warehouse ကို ဆက်သွယ်ပါ": {
  "en": "contact Warehouse",
  "zh": "请联系 Warehouse"
 },
 "Warehouse ပြင်နိုင်တာ: နာမည် / Category / Unit / ပါဝင်မှု / Supplier / ပုံ ပဲ။": {
  "en": "Warehouse can edit: Name / Category / Unit / Pack / Supplier / Image.",
  "my": "Warehouse ပြင်နိုင်တာ: နာမည် / အမျိုးအစား / Unit / ပါဝင်မှု / Supplier / ပုံ ပဲ။",
  "zh": "Warehouse 能改的:名字 / 类别 / 单位 / 每包数量 / Supplier / 图片。"
 },
 "batch တစ်ခုချင်း": {
  "en": "one batch at a time",
  "zh": "一次一个批次"
 },
 "box မဖွင့် ရသေးတဲ့ ရက် (auto ငွေ ရှိ)": {
  "en": "Days with cash movement but no box day",
  "zh": "有现金流水但未开箱的日期"
 },
 "stock မလောက်": {
  "en": "not enough stock",
  "zh": "库存不够"
 },
 "stock လုံးဝ မရှိဘူး — ပို့လို့ မရဘူး": {
  "en": "No stock at all — cannot deliver",
  "zh": "完全没有库存 — 无法配送"
 },
 "{a} ကို {b} ထဲ ပေါင်းလိုက်ပြီ": {
  "en": "{a} was merged into {b}",
  "zh": "{a} 已合并到 {b}"
 },
 "{code} က ကုန်ရောက် သွင်းတာ": {
  "en": "Goods received from {code}",
  "zh": "从 {code} 收到的货"
 },
 "{code} ထဲ ပေါင်းထည့်ပြီးပြီ": {
  "en": "Added into {code}",
  "zh": "已合并到 {code}"
 },
 "{code} ပြန်ပို့ မှတ်ပြီ — stock နုတ်ပြီး အကြွေး လျော့မယ်": {
  "en": "{code} return recorded — stock deducted, and the amount owed will go down",
  "zh": "{code} 退货已记录 — 库存已扣,欠款会减少"
 },
 "{code} ရှင်းရန် စပြင်ပြီ": {
  "en": "{code} settlement started",
  "zh": "{code} 结算已开始准备"
 },
 "{code} သွင်းပြီးပြီ — Finance စစ်ရန် ကျန်နေတယ်": {
  "en": "{code} recorded — waiting for Finance to check",
  "zh": "{code} 已录入 — 等 Finance 核对"
 },
 "{code} အမှာ တင်ပြီးပြီ": {
  "en": "{code} order request created",
  "zh": "{code} 订购单已创建"
 },
 "{code} အမှာ တင်ပြီးပြီ — အောက် စလစ် စာရင်း ထဲမှာ ရှိတယ် (ကုန် ရောက်မှ stock ဝင်မယ်)": {
  "en": "{code} order request created — find it in the slip list below (stock is added only when the goods arrive)",
  "zh": "{code} 订购单已创建 — 在下面的单据列表里(货到时才加库存)"
 },
 "{code} — ရောင်း တဲ့ နေရာ နဲ့ ပို့ တဲ့ နေရာ ~{dist} ခြား နေ တယ် — စစ် ကြည့် ပါ": {
  "en": "{code} — the sold place and the delivered place are ~{dist} apart — please check",
  "zh": "{code} — 销售地点和送货地点相距 ~{dist} — 请核对"
 },
 "{c} ကုန်မရ — ပယ်": {
  "en": "{c} not available — cancelled",
  "zh": "{c} 件没货 — 已取消"
 },
 "{day} မှာ ကောက်တဲ့ ဘောက်ချာ မရှိသေးဘူး (သို့) လိုအပ်ချက် အကုန် လုံလောက်နေပြီ။": {
  "en": "No invoices collected on {day} yet, or everything needed is already covered.",
  "zh": "{day} 还没有收到的发票,或者需要的都已经备齐了。"
 },
 "{day} မှာ ကောက်တဲ့ ဘောက်ချာတွေ အတွက် — ": {
  "en": "For invoices collected on {day} — ",
  "zh": "为 {day} 收到的发票 — "
 },
 "{f} မှ {t} အထိ · တင်သမျှ = အဲ့ဒီ ကာလမှာ ထည့်လိုက်တဲ့ ဘောက်ချာ အကုန် (ပယ်ဖျက် အပါ) · ကိန်းဂဏန်း ပုံစံ: ပမာဏ (ဘောက်ချာ စောင်)": {
  "en": "{f} to {t} · Entered = all invoices put in during this period (incl. cancelled) · numbers shown as: amount (invoice count)",
  "zh": "{f} 到 {t} · 已录入 = 这段时间录入的全部发票 (含已取消) · 数字格式:金额 (发票张数)"
 },
 "{g} — အဝင် {i} Ks · အထွက် {o} Ks": {
  "en": "{g} — in {i} Ks · out {o} Ks",
  "zh": "{g} — 进 {i} Ks · 出 {o} Ks"
 },
 "{name} အရောင်း ပိတ်လိုက်ပြီ — ပစ္စည်း {cnt} မျိုး Catalog ကနေ ပျောက်သွားမယ် (ပစ္စည်းချင်း setting မပြောင်းပါ)": {
  "en": "{name} sales closed — its {cnt} products will disappear from the Catalog (each product’s own setting is not changed)",
  "zh": "{name} 的销售已关闭 — 它的 {cnt} 种商品会从 Catalog 消失 (每种商品自己的设置不变)"
 },
 "{name} အရောင်း ပြန်ဖွင့်လိုက်ပြီ — ပစ္စည်း {cnt} မျိုး မူလ အတိုင်း ပြန်ပေါ်မယ်": {
  "en": "{name} sales reopened — its {cnt} products will show again as before",
  "zh": "{name} 的销售已重开 — 它的 {cnt} 种商品会按原来显示"
 },
 "{nm} ({role}) account ဆောက်ပြီးပြီ": {
  "en": "{nm} ({role}) account created",
  "zh": "{nm} ({role}) 账户已创建"
 },
 "{nm} ထည့်ပြီးပြီ": {
  "en": "{nm} added",
  "zh": "{nm} 已添加"
 },
 "{n} စောင်": {
  "en": "{n} invoices",
  "zh": "{n} 张"
 },
 "{n} မျိုး · {t} Ks": {
  "en": "{n} items · {t} Ks",
  "zh": "{n} 种 · {t} Ks"
 },
 "{n} ရက် ကျန်": {
  "en": "{n} days left",
  "zh": "剩 {n} 天"
 },
 "{shop} — {d} ကတည်းက ကျန် {v} Ks ({code})": {
  "en": "{shop} — {v} Ks owing since {d} ({code})",
  "zh": "{shop} — 从 {d} 起欠 {v} Ks ({code})"
 },
 "{shop} — နောက်ဆုံး ဝယ်တာ {d} ({n} ရက် ကြာပြီ)": {
  "en": "{shop} — last bought on {d} ({n} days ago)",
  "zh": "{shop} — 上次购买 {d} (已经 {n} 天了)"
 },
 "{sku} {name}": {
  "en": "{sku} {name}",
  "my": "{sku} {name}",
  "zh": "{sku} {name}"
 },
 "{sku} {name} — မှာ ထား {q} ခု, stock မရှိတော့ဘူး": {
  "en": "{sku} {name} — {q} ordered, stock finished",
  "zh": "{sku} {name} — 订了 {q} 件,库存没了"
 },
 "{sku} {name} — ရောင်းချိန် အရင်း {a} Ks, ခု အရင်း {b} Ks ({code})": {
  "en": "{sku} {name} — cost at sale {a} Ks, cost now {b} Ks ({code})",
  "zh": "{sku} {name} — 销售时成本 {a} Ks,现在成本 {b} Ks ({code})"
 },
 "{sku} ဆောက်ပြီးပြီ": {
  "en": "{sku} created",
  "zh": "{sku} 已创建"
 },
 "{sku} လျှော့ဈေး: {a} → {b}": {
  "en": "{sku} discount: {a} → {b}",
  "zh": "{sku} 折扣:{a} → {b}"
 },
 "{sku}: Wholesale ({amt}) အောက် ရောင်းထားတယ်": {
  "en": "{sku}: sold below Wholesale ({amt})",
  "zh": "{sku}:卖得低于 Wholesale ({amt})"
 },
 "{sku}: {q} → ဖြုတ်": {
  "en": "{sku}: {q} → removed",
  "zh": "{sku}:{q} → 已删除"
 },
 "{sku}: ဖြုတ်": {
  "en": "{sku}: removed",
  "zh": "{sku}:已删除"
 },
 "{sku}: အရင်းအမှန် ({amt}) အောက် ရောင်းထားတယ်": {
  "en": "{sku}: sold below true cost ({amt})",
  "zh": "{sku}:卖得低于真实成本 ({amt})"
 },
 "{sku}: အသစ်ထည့် × {q}": {
  "en": "{sku}: added × {q}",
  "zh": "{sku}:新增 × {q}"
 },
 "{src} — ဝယ်ဖူး တဲ့ အနှစ် ဆုံး ဈေး {p} Ks": {
  "en": "{src} — lowest price ever bought: {p} Ks",
  "zh": "{src} — 买过的最低价 {p} Ks"
 },
 "{supplier} အတွက် ပွင့်နေတဲ့ အမှာ ရှိတယ်": {
  "en": "There is an open order for {supplier}",
  "zh": "{supplier} 已有未完成的订单"
 },
 "{tier} ဈေး {dir} · {when}": {
  "en": "{tier} price {dir} · {when}",
  "zh": "{tier} 价格 {dir} · {when}"
 },
 "{who} ပြင်ခွင့် {cap} ကြိမ် ကုန်ပြီ — {contact}": {
  "en": "{who} edit limit of {cap} is used up — {contact}",
  "zh": "{who} 的修改次数 {cap} 次已用完 — {contact}"
 },
 "{y} နှစ်": {
  "en": "Year {y}",
  "zh": "{y} 年"
 },
 "· Owner {n} ကြိမ်": {
  "en": "· Owner {n}",
  "zh": " · Owner {n} 次"
 },
 "· အမှာ (Order Request) က သွင်းတာ": {
  "en": "· Received from an order request (Order Request)",
  "my": "· အမှာ က သွင်းတာ",
  "zh": " · 从订购单 (Order Request) 收货的"
 },
 "· အဲ့ဒီထဲက ပစ္စည်း {n} မျိုး က stock/မှာထားပြီး နဲ့ မလုံလောက်သေးဘူး": {
  "en": "· {n} of their items are still not covered by stock / already-ordered",
  "zh": " · 其中 {n} 种商品的库存/已订购还不够"
 },
 "ကာလ အကျဉ်း (Rep အလိုက်)": {
  "en": "Period summary (by rep)",
  "zh": "期间汇总 (按业务员)"
 },
 "ကိုက်တယ် ✅": {
  "en": "Matches ✅",
  "zh": "对得上 ✅"
 },
 "ကိုင်တွယ်နည်း: ဘောက်ချာကို ဖွင့် → ဈေးသစ် ပြင် / Line Discount ပေး (ညနေ မ print ခင် ပြင်တာ အကောင်းဆုံး)။": {
  "en": "What to do: open the invoice → set a new price / give a line discount (best done before the evening print).",
  "zh": "处理方法:打开发票 → 改新价格 / 给行折扣 (最好在晚上打印之前改)。"
 },
 "ကိုင်တွယ်ပြီး မှတ်မယ်": {
  "en": "Mark handled",
  "zh": "标记为已处理"
 },
 "ကိုင်တွယ်ပြီး မှတ်လိုက်ပြီ ✓": {
  "en": "Marked as handled ✓",
  "zh": "已标记为处理 ✓"
 },
 "ကိုင်တွယ်ပြီး — {name}": {
  "en": "Handled — {name}",
  "zh": "已处理 — {name}"
 },
 "ကိုင်တွယ်ရန် ကျန် သတိပေးချက်များ": {
  "en": "Alerts still to handle",
  "zh": "还没处理的提醒"
 },
 "ကိုယ့် ပို့ မှတ်တမ်း": {
  "en": "My delivery record",
  "zh": "我的送货记录"
 },
 "ကိုယ့် ရောင်းဈေး: လက်လီ {r} Ks · လက်ကား {w} Ks": {
  "en": "Our selling prices: retail {r} Ks · wholesale {w} Ks",
  "zh": "我们的售价:零售 {r} Ks · 批发 {w} Ks"
 },
 "ကိုယ့် လက်လီ က ဈေးကွက် ထက် {v} Ks နည်း (ယှဉ်နိုင်) နေတယ်": {
  "en": "Our retail is {v} Ks below the market (can compete)",
  "zh": "我们的零售价比市场低 {v} Ks (有竞争力)"
 },
 "ကိုယ့် လက်လီ က ဈေးကွက် ထက် {v} Ks များ နေတယ်": {
  "en": "Our retail is {v} Ks above the market",
  "zh": "我们的零售价比市场高 {v} Ks"
 },
 "ကိုယ် ကိုယ်တိုင် သိ ထား တဲ့၊ သေချာ စစ် ပြီး သား ဈေးကွက် ဈေး ပဲ ထည့်ပါ — \"Ai မေးကြည့်\" က ဒီ ဈေး တွေ နဲ့ ပဲ ယှဉ် ဖြေ မယ်။ ဘယ် သူ ဆီ က၊ ဘယ် နေ့ က ဈေး လဲ ဆို တာ မှတ်ချက် မှာ ရေးထား ပါ။": {
  "en": "Only enter market prices you know yourself and have checked — \"Ai Ask\" compares only with these prices. Note whose price it is and from which day.",
  "zh": "只填你自己知道、已经核实过的市场价格 — “Ai 问问”只用这些价格来对比回答。请在备注里写清是谁的价格、哪一天的。"
 },
 "ကိုယ်တိုင် သွား ဝယ် တဲ့ အဝယ်": {
  "en": "you go and buy it yourself",
  "zh": "亲自去买"
 },
 "ကုန် ဖိုး": {
  "en": "Goods value",
  "zh": "货物价值"
 },
 "ကုန် လဲ ပေး (ဆိုင် ကို အမျိုး တူ ပြန် ပေး)": {
  "en": "Give a goods exchange (same goods back to the shop)",
  "zh": "给换货 (同样的货还给店铺)"
 },
 "ကုန် လဲ ပြီး သား": {
  "en": "Already exchanged",
  "zh": "已经换过货了"
 },
 "ကုန် လဲ ပြီး သား ဆိုင်များ (ဆိုင် ၁ ဆိုင် = ၁ ကြိမ် ပဲ)": {
  "en": "Shops that already had an exchange (1 shop = 1 time only)",
  "zh": "已经换过货的店铺 (一家店 = 只能 1 次)"
 },
 "ကုန် လဲ ဖို့ ဆိုင် နာမည် လိုပါတယ် — Order ရွေးပါ (သို့) ဆိုင် နာမည် ရိုက်ပါ": {
  "en": "An exchange needs the shop name — pick the order, or type the shop name",
  "zh": "换货需要店铺名字 — 请选择订单,或输入店名"
 },
 "ကုန် လဲ — ဆိုင် ကို အသစ် ပြန် ပေး": {
  "en": "Goods exchange — fresh goods given back to the shop",
  "zh": "换货 — 新的货给回店铺"
 },
 "ကုန် သွား ပြီ — {n} ရက် ကျော်": {
  "en": "Expired — {n} days over",
  "zh": "已过期 — 超过 {n} 天"
 },
 "ကုန် အရင်း: {v} Ks": {
  "en": "Cost of goods: {v} Ks",
  "zh": "货物成本:{v} Ks"
 },
 "ကုန်မရ": {
  "en": "Goods not available",
  "zh": "没货了"
 },
 "ကုန်ရောက် သွင်း": {
  "en": "Receive goods",
  "zh": "收货"
 },
 "ကုန်ရောက် သွင်းရန် ကျန်တဲ့ အမှာ မရှိသေးဘူး": {
  "en": "No request is waiting for goods to arrive",
  "zh": "目前没有待收货的订购单"
 },
 "ကောက်ခဲ့တဲ့ နေ့ ကျော်သွားပြီ — Sales ပြင်ခွင့် ပိတ်ပြီ (Warehouse ကို ဆက်သွယ်ပါ)": {
  "en": "The collection day has passed — sales editing is closed (contact Warehouse)",
  "zh": "收款日已经过了 — 业务员不能再改 (请联系 Warehouse)"
 },
 "ကောက်ငွေ က ရှင်းရမယ့် ငွေထက် များနေတယ်": {
  "en": "The collected money is more than the amount to settle",
  "zh": "收到的钱比要结的金额多"
 },
 "ကောက်ပြီး ငွေ": {
  "en": "Money collected",
  "zh": "已收到的钱"
 },
 "ကောက်ရတဲ့ ငွေ (Ks)": {
  "en": "Money collected (Ks)",
  "zh": "收到的钱 (Ks)"
 },
 "ကောက်ရမယ့် ငွေ": {
  "en": "Money to collect",
  "zh": "要收的钱"
 },
 "ကော်ဖီဆိုင်": {
  "en": "Coffee shop",
  "zh": "咖啡店"
 },
 "ကျန်": {
  "en": "Left",
  "zh": "剩余"
 },
 "ကျန် ရက်": {
  "en": "Days left",
  "zh": "剩余天数"
 },
 "ကျန် အရွက်": {
  "en": "Loose leaves",
  "zh": "散张"
 },
 "ကျန်ငွေ (Credit)": {
  "en": "Balance (Credit)",
  "my": "ကျန်ငွေ (ကြွေး)",
  "zh": "余额 (欠款)"
 },
 "ကျန်ငွေ ထက် များနေတယ် (ကျန်: {bal} Ks)": {
  "en": "More than the balance due (balance: {bal} Ks)",
  "zh": "超过了应付余额 (余额:{bal} Ks)"
 },
 "ကျန်ငွေ ရှိတဲ့ ဆိုင် မရှိဘူး": {
  "en": "No shop has a balance owing",
  "zh": "没有店铺有欠款"
 },
 "ကျန်တာ ပိတ်ပြီး": {
  "en": "remainder closed",
  "zh": "余数已关闭"
 },
 "ကျန်တာ ပိတ်မယ်": {
  "en": "Close the rest",
  "zh": "关闭剩余的"
 },
 "ကျန်တာ မစောင့်တော့ရင်": {
  "en": "If you won't wait for the rest",
  "zh": "如果不等剩下的了"
 },
 "ကျန်တာ မရောက်တော့ — ပိတ်မယ်": {
  "en": "The rest isn't coming — close it",
  "zh": "余下的不会到了 — 关闭"
 },
 "ကျန်တာ ရှိသေးတယ် — {n} ခု · ထပ်စောင့်မလား / မရောက်တော့ ပိတ်မလား": {
  "en": "Still outstanding — {n} left · keep waiting, or close it as not coming?",
  "zh": "还有未到货 — 剩 {n} 件 · 继续等,还是按不再到货关闭?"
 },
 "ကျန်တဲ့ {n} ခု မရောက်တော့ဘူးလို့ မှတ်ပြီး ပိတ်မလား?": {
  "en": "Mark the remaining {n} as not coming and close this?",
  "zh": "将剩余的 {n} 件标记为不再到货并关闭吗?"
 },
 "ကျန်တဲ့ အရေအတွက်": {
  "en": "Qty left",
  "zh": "剩余数量"
 },
 "ကျသင့် ပမာဏ": {
  "en": "Payable amount",
  "zh": "应付金额"
 },
 "ကျသင့် ပမာဏ {amt} Ks အဖြစ် အတည်ပြုပြီး Supplier ဆီ ပို့ပြီး အဖြစ် မှတ်ပြီ": {
  "en": "Payable amount confirmed as {amt} Ks and marked as sent to the supplier",
  "zh": "应付金额已确认为 {amt} Ks,并标记为已发给 Supplier"
 },
 "ကျသင့် ပမာဏကို အတည်ပြုပြီး Supplier ဆီ ပို့ပြီး အဖြစ် မှတ်မလား? ပို့ပြီးရင် ပြန် ပြင်လို့ မရတော့ဘူး": {
  "en": "Confirm the payable amount and mark as sent to the supplier? After sending it cannot be edited.",
  "zh": "确认应付金额并标记为已发给供应商吗?发了以后就不能再改了"
 },
 "ကျသွား": {
  "en": "went down",
  "zh": "下降了"
 },
 "ကျော်မယ်": {
  "en": "Skip",
  "zh": "跳过"
 },
 "ကြည့်": {
  "en": "View",
  "zh": "看"
 },
 "ကြည့်မယ်": {
  "en": "View",
  "zh": "查看"
 },
 "ကြို · Direct": {
  "en": "Pre-order · Direct",
  "zh": "预先订购 · Direct"
 },
 "ကြို အမှာ (Direct)": {
  "en": "Pre-orders (Direct)",
  "zh": "预先订购 (Direct)"
 },
 "ကြို အမှာ (ခရီး အတွက်)": {
  "en": "Pre-order (for a trip)",
  "zh": "预先订购 (出行用)"
 },
 "ကြို အမှာ စာရင်း (Direct)": {
  "en": "Pre-order list (Direct)",
  "zh": "预先订购列表 (Direct)"
 },
 "ကြို အမှာ — Direct Driven (ခရီး)": {
  "en": "Pre-order — Direct Driven (trip)",
  "zh": "预先订购 — 直接采购 (出行)"
 },
 "ကွာ": {
  "en": "Difference",
  "zh": "差额"
 },
 "ကွာ (အရွက်)": {
  "en": "Difference (leaves)",
  "zh": "差异（张）"
 },
 "ကွာ — {v} Ks": {
  "en": "Different by {v} Ks",
  "zh": "差 — {v} Ks"
 },
 "ကွာ:": {
  "en": "Diff:",
  "zh": "差额:"
 },
 "ကွာခြားချက်": {
  "en": "Difference",
  "zh": "差额"
 },
 "ကွာခြားချက် ရှိ ရင် အကြောင်း ရေး မှ ပိတ် လို့ ရမယ်": {
  "en": "A reason is required when there is a difference",
  "zh": "有差额时必须注明原因"
 },
 "ကွာခြားချက် အကြောင်း (ကွာ ရင် ရေး ရ မယ်)": {
  "en": "Difference reason (required when different)",
  "zh": "差额原因（有差额时必填）"
 },
 "ကွာဟချက်": {
  "en": "Difference",
  "zh": "差额"
 },
 "ခဏ စောင့်ပါ…": {
  "en": "Please wait…",
  "zh": "请稍等…"
 },
 "ခိုင် — ၂ မျိုး တိုက်": {
  "en": "Strong — two checks agree",
  "zh": "可靠 — 两个检查一致"
 },
 "ခု တင်မယ့် ပစ္စည်းတွေ": {
  "en": "Items you are about to order",
  "zh": "这次要订的商品"
 },
 "ခု တင်မယ့် ပစ္စည်းတွေကို ရှိပြီးသား အမှာထဲ ပေါင်းထည့်လို့လည်း ရတယ်, အသစ် သီးခြား ဖွင့်လို့လည်း ရတယ် — ရွေးပါ": {
  "en": "You can add these items into the existing order, or open a separate new one — please choose",
  "zh": "可以把这些商品合并到已有的订单里,也可以单独新建 — 请选择"
 },
 "ခု ယူ မယ်": {
  "en": "Take one now",
  "zh": "现在拿一个"
 },
 "ခု လက်ရှိ အပြည့် ဘယ်နှစ် ထုပ် ရှိလဲ (ရေတွက်ပြီး ထည့်ပါ)": {
  "en": "How many full packs are there now (count them, then enter)",
  "zh": "现在一共有多少整包(数完再填)"
 },
 "ခု လောလောဆယ် အရင်း တက်ပြီး ထိနေတဲ့ ဘောက်ချာ မရှိဘူး": {
  "en": "Right now no invoice is hit by a risen cost",
  "zh": "目前没有发票被涨了的成本影响"
 },
 "ခု အမြတ် အတိုင်း ထိန်း မယ် ဆို စဉ်းစား လို့ ရတဲ့ ဈေး: {v} Ks က စပြီး (ဈေး ပြင် တာ Ko ကိုယ် တိုင် ပဲ)": {
  "en": "To keep the same profit, a price to consider starts from {v} Ks (changing prices is always up to Ko himself)",
  "zh": "想保持现在的利润,可以考虑的价格:从 {v} Ks 起 (改价格永远是 Ko 自己决定)"
 },
 "ခု အရင်း အမှန်: {v} Ks": {
  "en": "True cost now: {v} Ks",
  "zh": "现在的真实成本:{v} Ks"
 },
 "ချိတ်ထားတဲ့ ပစ္စည်းတွေ ပေါင်း တွက်ထားတယ်": {
  "en": "Linked items are counted together as one pool",
  "zh": "关联商品合并计算(同一个库存)"
 },
 "ချိတ်ပြီးပြီ — stock တစ်စု တည်း ဖြစ်သွားပြီ": {
  "en": "Linked — the stock is now kept as one",
  "zh": "已关联 — 库存合并为一处管理"
 },
 "ချိတ်ဖြုတ် — {sku} ကနေ ခွဲထုတ်": {
  "en": "Unlinked — split from {sku}",
  "zh": "已取消关联 — 从 {sku} 拆出"
 },
 "ချိတ်ဖြုတ် — အပြည့် {n} ထုပ် ခွဲထုတ်": {
  "en": "Unlinked — took out {n} full packs",
  "zh": "已取消关联 — 拆出 {n} 整包"
 },
 "ချိတ်ဖြုတ်ပြီးပြီ": {
  "en": "Unlinked",
  "zh": "已取消关联"
 },
 "ချိတ်ဖြုတ်မယ်": {
  "en": "Unlink",
  "zh": "取消关联"
 },
 "ချိတ်ဖြုတ်မှာ သေချာလား?": {
  "en": "Unlink for sure?",
  "zh": "确定要取消关联吗?"
 },
 "ချိတ်မယ့် ပစ္စည်း နာမည်": {
  "en": "Name of the product being linked",
  "zh": "要关联的商品名字"
 },
 "ချိတ်မယ်": {
  "en": "Link",
  "zh": "关联"
 },
 "ဆက်စပ်တဲ့ (နာမည်တူ) ပစ္စည်းကိုသာ ချိတ်လို့ ရတယ်": {
  "en": "Only related (same-name) products can be linked",
  "zh": "只能关联同名产品"
 },
 "ဆက်စပ်တဲ့ (နာမည်တူ) ပစ္စည်း မရှိဘူး — ချိတ်လို့ မရဘူး": {
  "en": "No related (same-name) product — cannot link",
  "zh": "没有同名相关产品，无法关联"
 },
 "ချိတ်မယ့် ပစ္စည်း": {
  "en": "Product to link",
  "zh": "要关联的产品"
 },
 "ဘယ်ဟာက အကြီးလဲ?": {
  "en": "Which one is the big pack?",
  "zh": "哪一个是大包装？"
 },
 "အကြီး ဘယ်ဟာလဲ ရွေးပါ": {
  "en": "Choose which one is the big pack",
  "zh": "请选择哪一个是大包装"
 },
 "ချိတ်ရန် အကြံပြု": {
  "en": "Suggested pack links",
  "zh": "建议的包装关联"
 },
 "ခွဲခြား မှတ်ချက်": {
  "en": "Distinguishing note",
  "zh": "区分备注"
 },
 "ခွဲခြား မှတ်ချက်: နာမည် တူ ဆိုင် နှစ်ဆိုင် ရှိရင် ခွဲခြား သိရအောင် ရေးပါ (ဥပမာ — ဈေး နား)": {
  "en": "Distinguishing note: if two shops share a name, write something that tells them apart (e.g. near the market)",
  "zh": "区分备注:如果有两家店同名,写一句能把它们区分开的话(例如 — 市场旁)"
 },
 "ဂိုဒေါင် မှာ လက်တွေ့ ရေတွက် ပြီး စာရင်း နဲ့ တိုက် စစ် တာ။ ပစ္စည်း မိသားစု (ထုပ် နဲ့ အလိပ်) ကို အ သေး ဆုံး အရေအတွက် နဲ့ ၁ ကြိမ် ပဲ ရေး ရမယ်။ Owner အတည်ပြု မှ စာရင်း ညှိ မယ်။": {
  "en": "Count the goods in the warehouse by hand and compare with the book. A product family (packs and pieces) is counted once, in the smallest unit. Only when the Owner confirms does the book get adjusted.",
  "zh": "在仓库实际数货,再和账面对照。商品家族(整包和散件)只按最小单位数一次。只有 Owner 确认后才会调整账面。"
 },
 "ငွေ": {
  "en": "Money",
  "zh": "钱"
 },
 "ငွေ {amt} Ks မှတ်ပြီးပြီ": {
  "en": "{amt} Ks recorded",
  "zh": "{amt} Ks 已记录"
 },
 "ငွေ ပမာဏ ထည့်ပါ": {
  "en": "Enter the payment amount",
  "zh": "请输入付款金额"
 },
 "ငွေ ပေးတဲ့ နည်း": {
  "en": "How the money was paid",
  "zh": "付款方式"
 },
 "ငွေ မရသေးတဲ့ Order": {
  "en": "Unpaid orders",
  "my": "ငွေ မရသေးတဲ့ ဘောက်ချာ",
  "zh": "未付款的订单"
 },
 "ငွေ မရသေးတဲ့ မိမိ ဘောက်ချာ": {
  "en": "My unpaid invoices",
  "zh": "我未付款的发票"
 },
 "ငွေ မှတ်": {
  "en": "Record payment",
  "zh": "记录付款"
 },
 "ငွေ မှတ်တမ်း ဖျက်ပြီးပြီ": {
  "en": "Payment record deleted",
  "zh": "付款记录已删除"
 },
 "ငွေ မှတ်တမ်းကို အောက်က \"ငွေ ရရှိ မှတ်တမ်း\" မှာ ပြင်/ဖျက် ပါ": {
  "en": "Edit or delete the payment in the \"Payments received\" record below",
  "zh": "在下面的“收款记录”里改/删这笔付款"
 },
 "ငွေ ရပြီး": {
  "en": "Money received",
  "zh": "钱已收到"
 },
 "ငွေ ရပြီး မှတ်ဖို့ အရင် ပစ္စည်း ပို့ပြီး ဖြစ်ရပါမယ်": {
  "en": "To mark as paid, the goods must be delivered first",
  "zh": "要标记为已付款,货必须先送到"
 },
 "ငွေ ရရှိ မှတ်တမ်း မရှိသေးပါ": {
  "en": "No payments recorded yet",
  "zh": "还没有收款记录"
 },
 "ငွေ ရေတွက် စာရင်း ကွာ နေ တယ် — {grp} {amt} Ks": {
  "en": "The counted money and the book do not match — {grp} {amt} Ks",
  "zh": "数的钱和账面对不上 — {grp} {amt} Ks"
 },
 "ငွေ လွှဲအပ် အတည်ပြုပြီးပြီ": {
  "en": "Handed-over money confirmed",
  "zh": "交接的钱已确认"
 },
 "ငွေ အထွက် — Supplier ရှင်းပြီး (Settlement {c} ခု)": {
  "en": "Money out — supplier settlements ({c} settlements)",
  "my": "ငွေ အထွက် — Supplier ရှင်းပြီး ({c} ခု)",
  "zh": "钱支出 — 供应商已结算 ({c} 笔结算)"
 },
 "ငွေ အထွက် — အသုံးစရိတ်": {
  "en": "Money out — expenses",
  "zh": "钱支出 — 开支"
 },
 "ငွေ အဝင် — ": {
  "en": "Money in — ",
  "zh": "钱收入 — "
 },
 "ငွေ အဝင် — ဘောက်ချာ ငွေ ရရှိ (Payments {c} ကြောင်း)": {
  "en": "Money in — invoice payments ({c} payments)",
  "my": "ငွေ အဝင် — ဘောက်ချာ ငွေ ရရှိ ({c} ကြောင်း)",
  "zh": "钱收入 — 发票收款 ({c} 笔)"
 },
 "ငွေကောက်ခံပြီးသား ဘောက်ချာ — ပြင်လို့ မရတော့ပါ (Owner ကို ဆက်သွယ်ပါ)": {
  "en": "This invoice is already paid — it cannot be edited (contact the Owner)",
  "zh": "这张发票已经收过钱了 — 不能再改 (请联系 Owner)"
 },
 "ငွေကြေး မှတ်ချက်": {
  "en": "Currency note",
  "zh": "外币备注"
 },
 "ငွေကြေး မှတ်ချက် (ရှိရင်)": {
  "en": "Currency note (if any)",
  "zh": "外币备注 (如有)"
 },
 "ငွေကြေး မှတ်ချက် သိမ်းပြီးပြီ": {
  "en": "Currency note saved",
  "zh": "外币备注已保存"
 },
 "ငွေကြေး ရွေး ထား ရင် လဲ နှုန်း ထည့် ပါ": {
  "en": "You picked a currency — enter the exchange rate too",
  "zh": "选了币种,请填汇率"
 },
 "ငွေကြေး အမျိုးအစား": {
  "en": "Currency",
  "zh": "币种"
 },
 "ငွေပေးချေပြီးသား": {
  "en": "Already paid",
  "zh": "已经付过了"
 },
 "ငွေရှင်းပြီး အဖြစ် မှတ်ပြီ 💰": {
  "en": "Marked as paid 💰",
  "zh": "已标记为付清 💰"
 },
 "ငွေရှင်းပြီး 💰": {
  "en": "Paid 💰",
  "zh": "已付清 💰"
 },
 "ငွေသား": {
  "en": "Cash",
  "zh": "现金"
 },
 "ငွေသား / ဘဏ်လွှဲ / KPay...": {
  "en": "Cash / bank transfer / KPay...",
  "zh": "现金 / 银行转账 / KPay..."
 },
 "ငွေသား ပိတ် ကို Cashbox မှာ လုပ် ပါ": {
  "en": "Do the cash close in the Cashbox",
  "zh": "请在现金箱中做现金结账"
 },
 "ငွေသား သေတ္တာ": {
  "en": "Cashbox",
  "zh": "现金箱"
 },
 "ငွေသား အမျိုးအစား": {
  "en": "Cash notes",
  "zh": "现金面额"
 },
 "ငွေသား အမျိုးအစား အလိုက် (ဖြည့်ချင်မှ ဖြည့်)": {
  "en": "Cash by note value (optional)",
  "zh": "现金面额明细(可选填)"
 },
 "စစ် တဲ့ သူ": {
  "en": "Checked by",
  "zh": "核对人"
 },
 "စစ် တဲ့ အချိန်": {
  "en": "Checked at",
  "zh": "核对时间"
 },
 "စစ်ဆေး အတည်ပြုရန်": {
  "en": "Check and confirm",
  "zh": "核对并确认"
 },
 "စစ်ပြီး": {
  "en": "Checked",
  "zh": "已核对"
 },
 "စစ်ပြီး PUR စုစုပေါင်း": {
  "en": "Checked PUR total",
  "zh": "已核对 PUR 总计"
 },
 "စစ်ပြီး စုစုပေါင်း (ပြေစာ ဈေး)": {
  "en": "Checked total (invoice price)",
  "zh": "已核对总计 (票面价格)"
 },
 "စစ်ပြီး သား အဝယ် — အပို ကုန်ကျ ကို Owner ပဲ ပြင် နိုင်တယ်": {
  "en": "This purchase is already checked — only the Owner can change its extra cost",
  "zh": "这张采购已经核对过了 — 额外费用只有 Owner 能改"
 },
 "စစ်ပြီး အတည်ပြုပြီးပြီ": {
  "en": "Checked and confirmed",
  "zh": "已核对并确认"
 },
 "စစ်ပြီး အတည်ပြုမယ်": {
  "en": "Mark checked & confirm",
  "zh": "标记核对并确认"
 },
 "စစ်ပြီး ✅": {
  "en": "Checked ✅",
  "zh": "已核对 ✅"
 },
 "စစ်ပြီး ✅ (တွက်ထဲ ပါ)": {
  "en": "Checked ✅ (counted)",
  "zh": "已核对 ✅ (计入)"
 },
 "စစ်ပြီးရင် နှိပ်ပါ — အရင်း ထပ်ပြောင်းမှ ပြန်ပေါ်မယ်": {
  "en": "Tap once you have checked it — it only comes back when the cost changes again",
  "zh": "看完就点一下 — 只有成本再变时才会重新出现"
 },
 "စစ်ရန် ကျန်": {
  "en": "Still to check",
  "zh": "还没核对"
 },
 "စစ်ရန် ကျန် ငွေ": {
  "en": "Money waiting to be checked",
  "zh": "待核对的钱"
 },
 "စစ်ရန် ကျန် အသိပေးချက်များ": {
  "en": "Alerts still to check",
  "zh": "还没看的提醒"
 },
 "စစ်ရန် ကျန် — Delivery ကောက် ထား": {
  "en": "Waiting to be checked — collected by Delivery",
  "zh": "待核对 — 送货员已收"
 },
 "စစ်ရန်ကျန်": {
  "en": "Waiting for check",
  "zh": "等核对"
 },
 "စစ်ရန်ကျန် PUR ကျန်နေသေးတယ် — အရင် စစ် ပြီးမှ ပို့ပါ": {
  "en": "A purchase is still waiting for check — check it first, then send",
  "zh": "还有 PUR 没核对完 — 先核对再发送"
 },
 "စစ်ရန်ကျန် အဝယ်တွေပဲ ပြထားတယ်။": {
  "en": "Showing only purchases still to check.",
  "zh": "只显示还没核对的采购。"
 },
 "စစ်ရန်ကျန် အဝယ်ပဲ ပယ်ဖျက်လို့ ရတယ်": {
  "en": "Only a purchase still waiting for check can be cancelled",
  "zh": "只有还没核对的采购能取消"
 },
 "စစ်ရန်ကျန် ⏳": {
  "en": "Waiting for check ⏳",
  "zh": "等核对 ⏳"
 },
 "စစ်ရန်ကျန် ⏳ (တွက်ထဲ မပါ)": {
  "en": "To check ⏳ (not counted)",
  "zh": "待核对 ⏳ (不计入)"
 },
 "စစ်သူ": {
  "en": "Checked by",
  "zh": "核对人"
 },
 "စတွေ့": {
  "en": "First seen",
  "zh": "第一次出现"
 },
 "စလစ် အားလုံး (ပိတ်ပြီးပါ) ကြည့်": {
  "en": "View all slips (incl. closed)",
  "zh": "查看全部单据 (含已关闭)"
 },
 "စသူ": {
  "en": "Started by",
  "zh": "发起人"
 },
 "စာရင်း ဖိုင် ရဲ့ လုံခြုံ မိတ္တူ တွေ။ နေ့တိုင်း Owner/Finance တစ်ယောက် ယောက် ပထမဆုံး ဝင် တဲ့ အချိန် အလိုအလျောက် ၁ စောင် ယူ တယ်၊ နောက်ဆုံး ၁၅ စောင် ပဲ ထား တယ်။": {
  "en": "Safety copies of the records file. One is taken automatically each day when the Owner/Finance first signs in; only the newest 15 are kept.",
  "zh": "账目文件的安全副本。每天 Owner/Finance 第一个登录时自动备一份,只保留最新的 15 份。"
 },
 "စာရင်း အရ": {
  "en": "Per the book",
  "zh": "按账面"
 },
 "စာရင်း အရွက်": {
  "en": "Book leaves",
  "zh": "账面张数"
 },
 "စားဖွယ်စုံဆိုင်": {
  "en": "Assorted food shop",
  "zh": "食品杂货店"
 },
 "စားသောက်ဆိုင်": {
  "en": "Restaurant",
  "zh": "餐馆"
 },
 "စီမံ": {
  "en": "Admin",
  "zh": "管理"
 },
 "စုစုပေါင်း": {
  "en": "Total",
  "zh": "总计"
 },
 "စုစုပေါင်း အခြေအနေ": {
  "en": "Total position",
  "zh": "总状况"
 },
 "စုစုပေါင်း အရောင်း (ဘောက်ချာ {c} စောင်)": {
  "en": "Total sales ({c} invoices)",
  "zh": "总销售额 ({c} 张发票)"
 },
 "ဆိုင်": {
  "en": "Shop",
  "zh": "店铺"
 },
 "ဆိုင် (ကုန် လဲ ရင်)": {
  "en": "Shop (for an exchange)",
  "zh": "店铺 (换货时)"
 },
 "ဆိုင် ကွဲ အဖြစ် မှတ်ထားပြီ": {
  "en": "Marked as different shops",
  "zh": "已标记为不同的店"
 },
 "ဆိုင် ကွဲ — မှန် တယ်": {
  "en": "Different shops — correct",
  "zh": "是不同的店 — 没错"
 },
 "ဆိုင် တွဲပြီးပြီ": {
  "en": "Shop attached",
  "zh": "店铺已关联"
 },
 "ဆိုင် နာမည် ကို အခြား အရေး/ဘာသာ နဲ့ ရေးတာ ရှိရင် ဒီမှာ ထည့်ထားပါ — အဲ့ဒီ အရေး နဲ့ ရှာရင်လည်း ဒီ ဆိုင် ကိုပဲ တွေ့မယ်": {
  "en": "If this shop's name is also written another way or in another language, add it here — searching by that spelling will find this shop too",
  "zh": "如果这家店名还有其他写法或语言版本,加在这里 — 用那种写法搜索也能找到这家店"
 },
 "ဆိုင် နာမည် တူတာ နှစ်ဆိုင် ရှိတယ် — ဘယ် ဆိုင် လဲ မသဲကွဲဘူး။ Order ကို ရွေးပြီး ပြန် လုပ်ပါ": {
  "en": "Two shops share this name — it is not clear which one. Pick the order and try again",
  "zh": "有两家店同名 — 不清楚是哪一家。请选择订单后重试"
 },
 "ဆိုင် နာမည် တူတာ နှစ်ဆိုင် ရှိတယ် — ဘယ် ဆိုင် လဲ ရွေးပါ": {
  "en": "Two shops share this name — pick which shop it is",
  "zh": "有两家店同名 — 请选择是哪一家"
 },
 "ဆိုင် နာမည် နဲ့ တူနေတယ် — ထည့်ဖို့ မလိုဘူး": {
  "en": "That is the shop's own name — no need to add it",
  "zh": "这就是店名本身 — 不需要添加"
 },
 "ဆိုင် နာမည် ရှာရန်": {
  "en": "Search shop name",
  "zh": "搜索店名"
 },
 "ဆိုင် နဲ့ ပစ္စည်း လိုပါတယ်": {
  "en": "Shop and items are needed",
  "zh": "需要店铺和商品"
 },
 "ဆိုင် နဲ့ ဝယ်ရန်": {
  "en": "Shops and buying",
  "zh": "店铺与采购"
 },
 "ဆိုင် ပြောင်း — နေရာ အမှတ် ပြန် စ": {
  "en": "Shop moved — reset the place pin",
  "zh": "店铺搬了 — 重新记位置点"
 },
 "ဆိုင် ပြောင်းသွားလို့ နေရာ အမှတ် အသစ် ပြန် မှတ်မလား? အမှတ် ဟောင်း ပျောက်သွားပြီး နောက် order ရောက်မှ အမှတ် အသစ် ပြန် မှတ်မယ်။": {
  "en": "The shop has moved — clear the old place pin and learn a new one? The old pin will be removed, and the next order from the shop will set the new pin.",
  "zh": "店铺搬走了,要清掉旧位置点重新记吗?旧位置点会删掉,这家店下一张订单来了会记新的位置点。"
 },
 "ဆိုင် မရှိသေးဘူး — order တင်ရင် အလိုလို ပေါ်လာမယ်။": {
  "en": "No shops yet — they appear automatically when you enter orders.",
  "zh": "还没有店铺 — 录入订单时会自动出现。"
 },
 "ဆိုင် မသဲကွဲ": {
  "en": "Shop unclear",
  "zh": "店铺不明确"
 },
 "ဆိုင် မသဲကွဲ — {code}: {name}": {
  "en": "Shop unclear — {code}: {name}",
  "zh": "店铺不明确 — {code}: {name}"
 },
 "ဆိုင် ရွေး တွဲ": {
  "en": "Pick & attach the shop",
  "zh": "选择并关联店铺"
 },
 "ဆိုင် သစ် 🆕": {
  "en": "New shops 🆕",
  "zh": "新店铺 🆕"
 },
 "ဆိုင် အမျိုးအစား": {
  "en": "Shop type",
  "zh": "店铺类型"
 },
 "ဆိုင် အမျိုးအစား (သိရင်)": {
  "en": "Shop type (if you know it)",
  "zh": "店铺类型(如果知道)"
 },
 "ဆိုင် အမျိုးအစား အလိုက်": {
  "en": "By shop type",
  "zh": "按店铺类型"
 },
 "ဆိုင် အရေအတွက်": {
  "en": "No. of shops",
  "zh": "店铺数量"
 },
 "ဆိုင် အသစ် ပဲ": {
  "en": "It's a new shop",
  "zh": "是新店"
 },
 "ဆိုင် အသေးစိတ်": {
  "en": "Shop details",
  "zh": "店铺明细"
 },
 "ဆိုင်တွေ က ပေးရန် ကျန်: {v} Ks (ဘောက်ချာ {c} စောင်)": {
  "en": "Shops still owe: {v} Ks ({c} invoices)",
  "zh": "店铺还欠:{v} Ks ({c} 张发票)"
 },
 "ဆိုင်တွေ က ရရန်": {
  "en": "Shops still owe",
  "zh": "店铺还欠的"
 },
 "ဆိုင်ပိတ်": {
  "en": "Shop closed",
  "zh": "店铺关门"
 },
 "ဆိုင်များ": {
  "en": "Shops",
  "zh": "店铺"
 },
 "ဆိုင်များ စာရင်း": {
  "en": "Shop list",
  "zh": "店铺列表"
 },
 "ဈေး": {
  "en": "Price",
  "zh": "价格"
 },
 "ဈေး (Ks)": {
  "en": "Price (Ks)",
  "zh": "价格 (Ks)"
 },
 "ဈေး ပြန် စစ် ရန် ကျန် ပစ္စည်း {n} မျိုး ရှိတယ်": {
  "en": "{n} products are waiting for a price review",
  "zh": "有 {n} 种商品等着复查价格"
 },
 "ဈေး ပြန် စစ် ရန် — အရင်း ပြောင်း သွား တဲ့ ပစ္စည်း တွေ": {
  "en": "Price review — products whose cost has changed",
  "zh": "价格复查 — 成本变了的商品"
 },
 "ဈေး ပြောင်းလဲမှု": {
  "en": "Price changes",
  "zh": "价格变化"
 },
 "ဈေး ဘယ် လောက် ထား ရင် ကောင်း မလဲ": {
  "en": "What price would be good?",
  "zh": "价格定多少好呢"
 },
 "ဈေး ရက်": {
  "en": "Price date",
  "zh": "价格日期"
 },
 "ဈေး အကြံ": {
  "en": "Price ideas",
  "zh": "价格建议"
 },
 "ဈေး အမျိုးအစား": {
  "en": "Price kind",
  "zh": "价格类型"
 },
 "ဈေး အဟောင်း — သတိ နဲ့ သုံး (ရက် {n} ကြာပြီ)": {
  "en": "Old price — use with care ({n} days old)",
  "zh": "旧价格 — 小心使用 (已经 {n} 天了)"
 },
 "ဈေးကွက် ဈေး ထည့်ရန်": {
  "en": "Enter market prices",
  "zh": "录入市场价格"
 },
 "ဈေးကွက် နဲ့ ယှဉ် ရင်": {
  "en": "Compared with the market",
  "zh": "和市场比"
 },
 "ဈေးကွက် နဲ့ ယှဉ် ရင် ဘယ်လို လဲ": {
  "en": "How does it compare with the market?",
  "zh": "和市场比怎么样?"
 },
 "ဈေးကွက် ရောင်းဈေး": {
  "en": "Market selling price",
  "zh": "市场售价"
 },
 "ဈေးကွက် ရောင်းဈေး: {p} Ks ({src} · {d})": {
  "en": "Market selling price: {p} Ks ({src} · {d})",
  "zh": "市场售价:{p} Ks ({src} · {d})"
 },
 "ဈေးကွက် လိုက် — {p} Ks (အမြတ် {m}%)": {
  "en": "Match the market — {p} Ks (profit {m}%)",
  "zh": "跟市场一样 — {p} Ks (利润 {m}%)"
 },
 "ဈေးနှုန်း စာရင်း (Leader)": {
  "en": "Price list (Leader)",
  "zh": "价格表 (Leader)"
 },
 "ဈေးနှုန်းတွေ သိမ်းမယ်": {
  "en": "Save prices",
  "zh": "保存价格"
 },
 "ဈေးနှုန်းများ (ကြည့်ရန်သာ — ပြင်ခွင့် Owner/Finance):": {
  "en": "Prices (view only — Owner/Finance can edit):",
  "zh": "价格 (只能看 — Owner/Finance 能改):"
 },
 "ဈေးနှုန်းများ သာ": {
  "en": "prices only",
  "zh": "只有价格"
 },
 "ဈေးပြင်": {
  "en": "Edit prices",
  "zh": "改价格"
 },
 "ဈေးအချိုး အခြေခံ": {
  "en": "From the price ratio",
  "zh": "按价格比例"
 },
 "ဈေးအမျိုးအစား": {
  "en": "Price type",
  "zh": "价格类型"
 },
 "ညှိ စစ်": {
  "en": "Balancing check",
  "zh": "平衡检查"
 },
 "တကယ် ပို့တဲ့ အရေအတွက်": {
  "en": "How many were really delivered",
  "zh": "实际送了多少"
 },
 "တကယ် ရောက်တဲ့ အရေအတွက်": {
  "en": "the quantity that actually arrived",
  "zh": "实际到的数量"
 },
 "တက်သွား": {
  "en": "went up",
  "zh": "上升了"
 },
 "တခြား": {
  "en": "Other",
  "zh": "其他"
 },
 "တင်ပြီးပြီ ✅": {
  "en": "Sent ✅",
  "zh": "已发送 ✅"
 },
 "တင်သမျှ": {
  "en": "Entered",
  "zh": "已录入"
 },
 "တင်သူ": {
  "en": "Raised by",
  "zh": "提交人"
 },
 "တစ်ချို့ မရသေးဘူး — နောက်မှ ပြန်စမ်းပါ": {
  "en": "Some did not go through — try again later",
  "zh": "有些还没通过 — 等会儿再试"
 },
 "တစ်ချို့ ရောက်ပြီး 🚚": {
  "en": "Partly arrived 🚚",
  "zh": "到了一部分 🚚"
 },
 "တစ်စိတ်တစ်ပိုင်း ရပြီး (Credit)": {
  "en": "Partly received (Credit)",
  "my": "တစ်စိတ်တစ်ပိုင်း ရပြီး (ကြွေး)",
  "zh": "收到一部分 (欠款)"
 },
 "တစ်ပိုင်း": {
  "en": "Partial",
  "zh": "一部分"
 },
 "တစ်ပိုင်း (ကျန် {amt})": {
  "en": "Partial ({amt} left)",
  "zh": "一部分 (还剩 {amt})"
 },
 "တစ်ပိုင်း ပို့ပြီး": {
  "en": "Partly delivered",
  "zh": "送了一部分"
 },
 "တစ်ပိုင်း ရပြီး": {
  "en": "Partially received",
  "zh": "收到一部分"
 },
 "တစ်လုံး တည်း — ဝင် စာရင်း တစ်ခု, ထွက် စာရင်း တစ်ခု။ အဖွင့် = အရင် နေ့ အပိတ် (ရေတွက် ပြီး သား) ကနေ အလို ဆက်; လ အကူး မှာ ဘာမှ ပြန် စ စရာ မရှိ။": {
  "en": "One box — one receipt list, one issue list. Opening carries from the previous counted closing; month changes reset nothing.",
  "zh": "一个箱子，一本收入账，一本支出账。期初由上一日实点结余自动带入；跨月不重置。"
 },
 "တန်": {
  "en": "note",
  "zh": "面额"
 },
 "တန်ဖိုး": {
  "en": "Value",
  "zh": "价值"
 },
 "တန်ဖိုး အလိုက် ရွက်ရေ": {
  "en": "Note counts by denomination",
  "zh": "按面额统计纸币张数"
 },
 "တာဝန် ပေးပြီးပြီ": {
  "en": "Job given",
  "zh": "已分配任务"
 },
 "တာဝန် ပေးမယ်": {
  "en": "Give the job",
  "zh": "分配任务"
 },
 "တာဝန် ရသူ": {
  "en": "Given to",
  "zh": "负责人"
 },
 "တောင့်ခံ နိုင် မှု": {
  "en": "How much it can withstand",
  "zh": "能撑多久"
 },
 "တွက်ဈေး": {
  "en": "Costing price",
  "zh": "核算价"
 },
 "တွက်ဈေး (Actual)": {
  "en": "Priced at (Actual)",
  "zh": "核算价 (Actual)"
 },
 "တွက်ဈေး (ဈေး ဆုံးဖြတ် ဖို့)": {
  "en": "Costing view (for price decisions)",
  "zh": "核算价 (用来定价格)"
 },
 "တွက်ဈေး (မထည့်ရင် Actual)": {
  "en": "Price (blank = Actual)",
  "zh": "价格 (不填 = Actual)"
 },
 "တွက်ဈေး (၁ ခု)": {
  "en": "Costing price (1 pc)",
  "zh": "核算价 (1 个)"
 },
 "ထည့်ထား တဲ့ ဈေးများ": {
  "en": "Prices entered",
  "zh": "已填的价格"
 },
 "ထည့်ပြီးပြီ": {
  "en": "Added",
  "zh": "已添加"
 },
 "ထည့်မယ်": {
  "en": "Add",
  "zh": "添加"
 },
 "ထည့်သူ": {
  "en": "Entered by",
  "zh": "录入人"
 },
 "ထပ် နေ နိုင် တဲ့ ဆိုင်": {
  "en": "Shops that may be duplicates",
  "zh": "可能重复的店铺"
 },
 "ထိခိုက်တဲ့ ဘောက်ချာ": {
  "en": "Affected invoices",
  "zh": "受影响的发票"
 },
 "ထိန်း — ခု လက်လီ {p} Ks (အမြတ် {m}%)": {
  "en": "Keep — current retail {p} Ks (profit {m}%)",
  "zh": "保持 — 当前零售 {p} Ks (利润 {m}%)"
 },
 "ထွက် အရွက်": {
  "en": "Issue leaves",
  "zh": "支出张数"
 },
 "ဒါ တွေ က အကြံ ပြု သက်သက် — ဈေး ပြင် တာ Ko ကိုယ် တိုင် ပဲ လုပ် ရမယ်": {
  "en": "These are only ideas — changing prices is always done by Ko himself",
  "zh": "这些只是建议 — 改价格永远是 Ko 自己做"
 },
 "ဒီ browser မှာ GPS မရဘူး": {
  "en": "This browser has no GPS",
  "zh": "这个浏览器没有 GPS"
 },
 "ဒီ line ကို ဖြုတ်မလား?": {
  "en": "Remove this line?",
  "zh": "要删掉这一行吗?"
 },
 "ဒီ tab ပိတ်ရန်": {
  "en": "Close this tab",
  "zh": "关闭这个标签页"
 },
 "ဒီ ကာလ အတွင်း Sales": {
  "en": "Sales in this period",
  "my": "ဒီ ကာလ အတွင်း အရောင်း",
  "zh": "这段时间的销售"
 },
 "ဒီ ကာလ အတွင်း မှတ်တမ်း မရှိဘူး။": {
  "en": "No records in this period.",
  "zh": "这段时间没有记录。"
 },
 "ဒီ ခလုတ်က ဒီ အဝယ်ကို စစ်ပြီး လို့ မှတ်ရုံပဲ — ဈေးတွေ ဘာမှ မပြောင်းဘူး (ဈေး ပြောင်းတာက Products → ဈေးပြင် form ကနေ သီးသန့် လုပ်ရတယ်)။": {
  "en": "This button only marks the purchase as checked — it changes no prices (price changes are done separately in Products → the price form).",
  "zh": "这个按钮只是把这张采购标记为已核对 — 不改任何价格 (改价格要在 Products → 改价格表单里单独做)。"
 },
 "ဒီ ငွေ ကို မှတ်ပြီးသား — ထပ်မမှတ်ဘူး": {
  "en": "This payment was already recorded — not recording it again",
  "zh": "这笔钱已经记过了 — 不会重复记"
 },
 "ဒီ ငွေ ကို မှတ်ပြီးသား — ထပ်မမှတ်ဘူး ({code})": {
  "en": "This payment was already recorded — not recording it again ({code})",
  "zh": "这笔钱已经记过了 — 不会重复记({code})"
 },
 "ဒီ ငွေ မှတ်တမ်း ဖျက်မလား?": {
  "en": "Delete this payment record?",
  "zh": "要删掉这笔付款记录吗?"
 },
 "ဒီ စရိတ်ကို ဖျက်မှာ သေချာလား?": {
  "en": "Delete this expense — are you sure?",
  "zh": "确定要删掉这笔开支吗?"
 },
 "ဒီ စာမျက်နှာ က အစီရင်ခံ အတွက် ပဲ — ဘောက်ချာ တွေ မှာ အခွန် ထပ် မတင် ဘူး၊ ဈေး လည်း မပြောင်း ဘူး။ ပို့ပြီး ဘောက်ချာ တွေ ရဲ့ ရောင်းရငွေ ကို ခု အခွန် နှုန်း နဲ့ တွက် ပြ ထား တာ။": {
  "en": "This page is for reporting only — no tax is added to invoices and no price changes. It works out the tax on delivered sales at the current rate.",
  "zh": "本页只是用来报告的 — 发票上不加税,价格也不变。这里是按当前税率算已送达销售额的税。"
 },
 "ဒီ စာရင်း စ သုံးချိန် က လက်ထဲ ရှိ နေ တဲ့ ငွေ ကို ထည့်ပါ — လက်ကျန် တွေ အဲ့ ကနေ စ ရေး မယ်။": {
  "en": "Enter the money that was in hand when this book started — balances count on from there.",
  "zh": "填入开始用这本账时手头有的钱 — 余额从这里接着算。"
 },
 "ဒီ ဆိုင် က ကုန် လဲ ခွင့် ၁ ကြိမ် သုံး ပြီးပြီ — {info}။ နောက်ထပ် လဲ ပေးဖို့ Owner ကို အရင် မေးပါ။": {
  "en": "This shop already used its one exchange — {info}. Ask the Owner first before exchanging again.",
  "zh": "这家店的一次换货机会已经用过了 — {info}。再换之前先问 Owner。"
 },
 "ဒီ ဆိုင် ကို ရွေးတဲ့ ဆိုင် ထဲ ပေါင်းမှာ သေချာလား? ဘောက်ချာ/မှတ်တမ်း အကုန် ရွှေ့မယ်": {
  "en": "Merge this shop into the selected shop — are you sure? All invoices/records will move.",
  "zh": "确定要把这家店合并到选的那家店吗?所有发票/记录都会移过去"
 },
 "ဒီ ဆိုင် နဲ့ တွဲမယ်": {
  "en": "Attach this shop",
  "zh": "关联这家店"
 },
 "ဒီ ဆိုင် ပဲ": {
  "en": "Yes, this shop",
  "zh": "就是这家"
 },
 "ဒီ ဆိုင်ကို ပေါင်းရန်": {
  "en": "Merge this shop into",
  "zh": "把这家店合并到"
 },
 "ဒီ ဆိုင်တွေ က ကုန် လဲ ခွင့် ၁ ကြိမ် သုံးပြီးပြီ — နောက်ထပ် တောင်းလာရင် ဒီ မှတ်တမ်း ကို ပြပြီး ပြောလို့ ရတယ်။": {
  "en": "These shops have already used their one exchange — if they ask again, show this record and say so.",
  "zh": "这些店的一次换货机会已经用过了 — 如果他们再要求,把这个记录给他们看就行。"
 },
 "ဒီ ဆိုင်မှာ ငွေ မရသေးတဲ့ ဘောက်ချာ {n} စောင် ရှိနေပြီ — နောက်ထပ် ဘောက်ချာ အသစ် ဖွင့်လို့ မရတော့ဘူး။ ကျန်ငွေ အရင် ရှင်းပါ": {
  "en": "This shop already has {n} unpaid invoices — a new invoice cannot be opened. Collect the balance first",
  "zh": "这家店已经有 {n} 张未付款的发票了 — 不能再开新发票。先把欠款收了"
 },
 "ဒီ တွက်ဈေး က ဈေး ဆုံးဖြတ် တဲ့ အခါ ကြည့် ဖို့ ပဲ — Supplier ကို ရှင်း ရ မယ့် ငွေ မဟုတ် ဘူး။ အပို ကုန်ကျ ကို ကုန် ဖိုး အချိုး နဲ့ ခွဲ ထည့် ပြီး, Risk % ကို ပစ္စည်း တစ်မျိုး ချင်း သတ်မှတ် ထား တဲ့ အတိုင်း ထည့် တွက် ထား တယ်။": {
  "en": "This costing view is only for price decisions — it is not the amount to pay the supplier. The extra cost is shared out in proportion to each line's goods value, and each product's own Risk % is added.",
  "zh": "这个核算价只是用来定价格看的 — 不是要付给 Supplier 的金额。额外费用按每行货物价值的比例分摊,每个商品自己的 Risk % 也加上了。"
 },
 "ဒီ ပစ္စည်း ကို လိုတဲ့ မပို့ရသေး ဘောက်ချာ မရှိဘူး": {
  "en": "No undelivered invoice needs this item",
  "zh": "没有未送达的发票需要这个商品"
 },
 "ဒီ ပစ္စည်း အတွက် ဈေးကွက် ဈေး မထည့်ရသေးဘူး — အရင် ထည့်ပြီးမှ ဖြေ လို့ ရမယ်": {
  "en": "No market price has been entered for this product yet — enter one first, then I can answer",
  "zh": "这个商品还没填市场价格 — 先填一个,才能回答"
 },
 "ဒီ ပစ္စည်းက {sku} — {name} ရဲ့ အထုပ် ကြီး ပါ — ၁ ထုပ် = {f} ခု။ stock အကုန် အခြေခံ ပစ္စည်းနဲ့ တစ်စု တည်း ထားတယ်။": {
  "en": "This product is the big pack of {sku} — {name} — 1 pack = {f}. All of its stock is kept together with the base product.",
  "zh": "这个商品是 {sku} — {name} 的大包装 — 1 包 = {f} 个。它的库存全部和基础商品放在一起。"
 },
 "ဒီ ပစ္စည်းက ချိတ်ထားပြီးသား — အရင် ချိတ်ဖြုတ်ပါ": {
  "en": "This product is already linked — unlink it first",
  "zh": "这个商品已经关联了 — 先取消关联"
 },
 "ဒီ ပစ္စည်းက အခြေခံ (အသေး) — အထုပ် ကြီး ပစ္စည်း ရွေးပါ": {
  "en": "This product is the base (small) — choose its big pack product",
  "zh": "这个商品是基础 (小) — 请选择它的大包装商品"
 },
 "ဒီ ပစ္စည်းက အထုပ် ကြီး — အခြေခံ (အသေး) ပစ္စည်း ရွေးပါ": {
  "en": "This product is the big pack — choose its base (small) product",
  "zh": "这个商品是大包装 — 请选择它的基础 (小) 商品"
 },
 "ဒီ ပစ္စည်းကို နောက် ပစ္စည်း တစ်ခုရဲ့ အထုပ် အဖြစ် ချိတ်လို့ ရတယ်။ ချိတ်ပြီးရင် stock က တစ်စု တည်း ဖြစ်သွားပြီး အထုပ် နဲ့ ဝင်/ထွက် တိုင်း အခြေခံ ပစ္စည်းပါ အလိုအလျောက် ပြောင်းမယ်။": {
  "en": "This product can be linked as the pack of another product. Once linked, the stock is kept as one lot — every pack in or out also changes the base product on its own.",
  "zh": "这个商品可以关联为另一个商品的包装。关联后库存合并为一处 — 每次包装出入,基础商品都会自动跟着变。"
 },
 "ဒီ ပစ္စည်းမှာ Actual ဈေး မဖြည့်ရသေးဘူး — Finance/Owner ဖြည့်ပြီးမှ ပြန်ပို့ မှတ်လို့ရမယ်": {
  "en": "This product has no Actual price yet — Finance/Owner must fill it in before a return can be recorded",
  "zh": "这个商品还没填 Actual 价格 — Finance/Owner 填了以后才能记退货"
 },
 "ဒီ ပစ္စည်းမှာ အထုပ် ချိတ်ထားပြီးသား ရှိတယ်": {
  "en": "This product already has a pack linked",
  "zh": "这个商品已经有关联的包装了"
 },
 "ဒီ ပစ္စည်းရဲ့ နာမည်": {
  "en": "This product's name",
  "zh": "这个商品的名字"
 },
 "ဒီ ပစ္စည်းရဲ့ အထုပ် ကြီး: {sku} — {name} (၁ ထုပ် = {f} ခု)": {
  "en": "Big pack of this product: {sku} — {name} (1 pack = {f})",
  "zh": "这个商品的大包装:{sku} — {name} (1 包 = {f} 个)"
 },
 "ဒီ ဘောက်ချာ က ပို့လို့ မရတော့ဘူး": {
  "en": "This invoice can no longer be delivered",
  "zh": "这张发票不能再送了"
 },
 "ဒီ ဘောက်ချာ ဖွင့်ချိန်တုန်းက ဆိုင်မှာ ငွေ မရသေးတဲ့ ဘောက်ချာ ရှိနေခဲ့တယ် — ဒါက နောက် တစ်စောင် ထပ်တိုးတဲ့ အကြွေး ဘောက်ချာ": {
  "en": "When this invoice was opened, the shop already had an unpaid invoice — this one added another credit invoice",
  "zh": "开这张发票时,这家店已经有一张未付款的发票了 — 这张是又加的一张欠款发票"
 },
 "ဒီ ဘောက်ချာ ဘယ် ဆိုင် အတွက် လဲ မသဲကွဲသေးဘူး — ဆိုင် ရွေး တွဲပါ": {
  "en": "It is not clear which shop this invoice is for yet — pick the shop and attach it",
  "zh": "还不清楚这张发票是哪家店的 — 请选择店铺并关联"
 },
 "ဒီ မပို့ရသေး ဘောက်ချာကို ဖျက်မှာလား?": {
  "en": "Delete this unsent invoice?",
  "zh": "要删掉这张还没发送的发票吗?"
 },
 "ဒီ မိတ္တူ တွေ က ဒီ server ပေါ် မှာပဲ ရှိတယ် — ဖိုင် ပျက်/မှား ရင် ကာကွယ် ပေမယ့်၊ အကောင့် တစ်ခုလုံး ပျောက် သွား ရင် မပါ ဘူး။ ဒါ့ကြောင့် ၁၅ ရက် ၁ ကြိမ် လောက် download ဆွဲ ပြီး ကိုယ့် ဖုန်း/ကွန်ပျူတာ ထဲ သိမ်း ထား ပါ။": {
  "en": "These copies live on this same server — they protect against file damage or mistakes, but not if the whole account is lost. So download one about every 15 days and keep it on your own phone/computer.",
  "zh": "这些副本只在这台服务器上 — 能防文件损坏或出错,但如果整个账户丢了就没了。所以请大约每 15 天下载一份,存在自己的手机/电脑里。"
 },
 "ဒီ မိသားစု ထဲ ပါဝင်၊ တွဲ ပြီး ရေး ရမယ်": {
  "en": "Part of this family — count together",
  "zh": "属于这个家族 — 一起数"
 },
 "ဒီ မေးခွန်း မပါ သေးဘူး — အောက်က ခလုတ်တွေ ထဲ က ရွေးပါ": {
  "en": "That question is not included yet — pick from the buttons below",
  "zh": "这个问题还没包括 — 请从下面的按钮里选"
 },
 "ဒီ ရက်ပိုင်းမှာ အရောင်း မရှိသေးပါ": {
  "en": "No sales in this period yet",
  "zh": "这段时间还没有销售"
 },
 "ဒီ ရေတွက် ကို ဖျက်မယ်": {
  "en": "Delete this count",
  "zh": "删掉这次盘点"
 },
 "ဒီ အမှာ က ကုန် သွင်းလို့ မရတော့ဘူး": {
  "en": "Goods can't be received on this order request anymore",
  "zh": "这张订购单不能再收货了"
 },
 "ဒီ အမှာ ကို ပေါင်းပြီးသား — ထပ်မပေါင်းဘူး": {
  "en": "This was already added in — not adding it again",
  "zh": "已经合并过了 — 不会重复合并"
 },
 "ဒီ အမှာ ကို ပေါင်းပြီးသား — ထပ်မပေါင်းဘူး ({code})": {
  "en": "This was already added in — not adding it again ({code})",
  "zh": "已经合并过了 — 不会重复合并({code})"
 },
 "ဒီ အမှာ ကို ဖွင့်ပြီးသား — ထပ်မဖွင့်ဘူး": {
  "en": "This order was already created — not creating it again",
  "zh": "这个订单已经创建过了 — 不会重复创建"
 },
 "ဒီ အမှာ ကို ဖွင့်ပြီးသား — ထပ်မဖွင့်ဘူး ({code})": {
  "en": "This order was already created — not creating it again ({code})",
  "zh": "这个订单已经创建过了 — 不会重复创建({code})"
 },
 "ဒီ အမှာထဲ ပေါင်းထည့်": {
  "en": "Add into this order",
  "zh": "合并到这个订单"
 },
 "ဒီ အဝယ်ကို ပယ်ဖျက်ပြီး stock ပြန် နှုတ်မလား?": {
  "en": "Cancel this purchase and take the stock back?",
  "zh": "取消这张采购并把库存扣回吗?"
 },
 "ဒီ ဧရိယာ တစ်ခုလုံး ပေးမယ်": {
  "en": "Give this whole area",
  "zh": "把这整个区域都给他"
 },
 "ဒီနေ့": {
  "en": "Today",
  "zh": "今天"
 },
 "ဒီနေ့ Cashbox ပိတ်": {
  "en": "Today's cashbox close",
  "zh": "今日现金箱结账"
 },
 "ဒီနေ့ Order": {
  "en": "Today's orders",
  "my": "ဒီနေ့ ဘောက်ချာ",
  "zh": "今天的订单"
 },
 "ဒီနေ့ ငွေ အဝင်/အထွက်": {
  "en": "Today's money in/out",
  "zh": "今天的钱进/出"
 },
 "ဒီနေ့ ပို့ဆောင် အခြေအနေ": {
  "en": "Today's delivery state",
  "zh": "今天的送货状态"
 },
 "ဒီနေ့ ပို့ပြီး": {
  "en": "Delivered today",
  "zh": "今天已送达"
 },
 "ဒီနေ့ ဘယ် လောက် ရောင်း ရ လဲ": {
  "en": "How much did we sell today?",
  "zh": "今天卖了多少?"
 },
 "ဒီနေ့ မှတ်တမ်း မရှိသေးဘူး။": {
  "en": "No entries for this day yet.",
  "zh": "这一天还没有记录。"
 },
 "ဒီနေ့ ရေတွက် ငွေ မမှတ် ရသေးဘူး။": {
  "en": "Today's counted cash is not recorded yet.",
  "zh": "今天数的现金还没记录。"
 },
 "ဒီနေ့ ရောင်းရငွေ: {v} Ks (ဘောက်ချာ {c} စောင်)": {
  "en": "Sales today: {v} Ks ({c} invoices)",
  "zh": "今天的销售额:{v} Ks ({c} 张发票)"
 },
 "ဒီနေ့ လိုအပ်": {
  "en": "Today's need",
  "zh": "今天需要的"
 },
 "ဒီနေ့ လွှဲအပ် စစ်ရန် မရှိဘူး": {
  "en": "Nothing to check today",
  "zh": "今天没有要核对交接的"
 },
 "ဒီနေ့ အထွက်": {
  "en": "Today's out",
  "zh": "今天的支出"
 },
 "ဒီနေ့ အရောင်း": {
  "en": "Today's sales",
  "zh": "今天的销售"
 },
 "ဒီနေ့ အရောင်း · ဘောက်ချာ {c} စောင်": {
  "en": "Today's sales · {c} invoices",
  "zh": "今天的销售 · {c} 张发票"
 },
 "ဒီနေ့ အဝင်": {
  "en": "Today's in",
  "zh": "今天的收入"
 },
 "ဒီနေ့/မနေ့က မိမိ ဘောက်ချာ မရှိဘူး (လိုင်း ရမှ နောက်ဆုံး အခြေအနေ ဆွဲယူမယ်)": {
  "en": "No invoices of yours today/yesterday (the latest state loads when you are online)",
  "zh": "今天/昨天没有你的发票 (联网后会拉最新状态)"
 },
 "ဒီလ": {
  "en": "This month",
  "zh": "本月"
 },
 "ဒီလ SKU အခြေအနေ": {
  "en": "SKUs this month",
  "zh": "本月 SKU 情况"
 },
 "ဒီလ ငွေ ဝင်ငွေ (Payments)": {
  "en": "Cash in this month (Payments)",
  "my": "ဒီလ ငွေ ဝင်ငွေ",
  "zh": "本月的钱收入 (Payments)"
 },
 "ဒီလ ငွေ အဝင် − အထွက်": {
  "en": "This month's in − out",
  "zh": "本月 收入 − 支出"
 },
 "ဒီလ စုစုပေါင်း ({n} ကြောင်း)": {
  "en": "This month total ({n} entries)",
  "zh": "本月总计 ({n} 条)"
 },
 "ဒီလ ပို့ပြီး ဘောက်ချာ မရှိသေးဘူး။": {
  "en": "No delivered invoices this month yet.",
  "zh": "本月还没有已送达的发票。"
 },
 "ဒီလ မိမိ ရောင်းခဲ့ပြီးသော SKU အမျိုးအစားပေါင်း": {
  "en": "Different SKUs you sold this month",
  "zh": "本月你卖过的不同 SKU 种类"
 },
 "ဒီလ မှာ Cashbox နေ့ မရှိ သေးဘူး။": {
  "en": "No cashbox day in this month yet.",
  "zh": "本月还没有现金箱记录。"
 },
 "ဒီလ မှာ ပိတ်/ရေတွက် ထား တဲ့ နေ့ မရှိ သေးဘူး။": {
  "en": "No closed or counted day in this month yet.",
  "zh": "本月还没有结账或点钞的日期。"
 },
 "ဒီလ ရောင်းရငွေ (ပို့ပြီး)": {
  "en": "This month's sales (delivered)",
  "zh": "本月销售额 (已送达)"
 },
 "ဒီလ ရောင်းရငွေ (ပို့ပြီး): {v} Ks": {
  "en": "This month's sales (delivered): {v} Ks",
  "zh": "本月销售额 (已送达):{v} Ks"
 },
 "ဒီလ အမြတ်": {
  "en": "This month's profit",
  "zh": "本月利润"
 },
 "ဒီလ အရောင်း ({m}) · ဘောက်ချာ {c} စောင်": {
  "en": "Sales this month ({m}) · {c} invoices",
  "zh": "本月销售 ({m}) · {c} 张发票"
 },
 "ဒီလ အသုံးစရိတ်": {
  "en": "This month's expenses",
  "zh": "本月开支"
 },
 "ဒီလ အသုံးစရိတ် စုစုပေါင်း: {v} Ks": {
  "en": "This month's expenses total: {v} Ks",
  "zh": "本月开支总计:{v} Ks"
 },
 "ဒီလ အသုံးစရိတ် မမှတ်ရသေးဘူး။": {
  "en": "No expenses recorded this month yet.",
  "zh": "本月还没有记录开支。"
 },
 "ဒီလမှာ အသုံးစရိတ် မမှတ်ရသေးဘူး။": {
  "en": "No expenses recorded this month yet.",
  "zh": "本月还没有记录开支。"
 },
 "နည်းလမ်း": {
  "en": "Method",
  "zh": "方式"
 },
 "နယ်မြေလက်ကား": {
  "en": "Regional wholesale",
  "zh": "区域批发"
 },
 "နာမည်": {
  "en": "Name",
  "zh": "名字"
 },
 "နာမည် *": {
  "en": "Name *",
  "zh": "名字 *"
 },
 "နာမည် / SKU ရှာရန်": {
  "en": "Search name / SKU",
  "zh": "搜索名字 / SKU"
 },
 "နာမည် ထည့်ပါ": {
  "en": "Please enter a name",
  "zh": "请输入名字"
 },
 "နာမည် နဲ့ ပါဝင်မှု အချက်အလက်ကို ကြည့်ပြီး တွဲနိုင်တဲ့ ပစ္စည်းတွေ အကြံပြုထားတာပါ — သေချာ စစ်ပြီးမှ ချိတ်ပါ။ အရွယ်အစား ပဲ မတူတဲ့ ပစ္စည်း (ဥပမာ (B) နဲ့ (S)) တွေလည်း ပါနိုင်တယ် — အဲ့ဒါမျိုးဆို ကျော်လိုက်ပါ။": {
  "en": "Suggested from the names and pack details — please check carefully before linking. Products that only differ in size (for example (B) and (S)) may appear too — skip those.",
  "zh": "这是根据名字和每包数量建议可以关联的商品 — 关联前请仔细核对。只是大小不同的商品 (例如 (B) 和 (S)) 也可能出现 — 那种就跳过。"
 },
 "နာမည် ပြင်ပြီးပြီ": {
  "en": "Name updated",
  "zh": "名字已更新"
 },
 "နာမည် ပြင်ရင် ဒီ ဆိုင်ရဲ့ ဘောက်ချာ အားလုံးပါ လိုက် ပြောင်းတယ်။": {
  "en": "Renaming changes it on all of this shop's invoices too.",
  "zh": "改名会连这家店的所有发票一起改。"
 },
 "နိုင်ငံခြား ငွေ ပမာဏ": {
  "en": "Foreign amount",
  "zh": "外币金额"
 },
 "နေရာ": {
  "en": "Area",
  "zh": "地区"
 },
 "နေရာ / ရပ်ကွက်": {
  "en": "Area / ward",
  "zh": "地区 / 街区"
 },
 "နေရာ pin ချင်း ၄၀ m အတွင်း ကပ်နေတဲ့ ဆိုင်တွေ — ဆိုင် တစ် ဆိုင် တည်း ဖြစ်နိုင်တယ်။ ပေါင်းသင့်ရင် ပေါင်းပါ၊ ဆိုင် ကွဲ ဆိုရင် မှတ်ထားပါ။": {
  "en": "Shops whose place pins sit within 40 m of each other — they may be one and the same shop. Merge them if they are one shop; mark them if they are truly different.",
  "zh": "位置标记相距40米以内的店铺 — 可能是同一家店。如果是同一家请合并;确实是不同的店请标记。"
 },
 "နေရာ ကြည့်ရန်": {
  "en": "See on map",
  "zh": "在地图上看"
 },
 "နေရာ စစ် ရန် ကျန် {n} ခု ရှိတယ်": {
  "en": "{n} place checks are waiting",
  "zh": "有 {n} 个地点等着核对"
 },
 "နေရာ အမှတ် ပြန် စပြီးပြီ — နောက် order ရောက်ရင် အမှတ် အသစ် မှတ်မယ်": {
  "en": "Place pin cleared — the next order from this shop will set a new pin",
  "zh": "位置点已清除 — 这家店下一张订单会记新的位置点"
 },
 "နေရာ အမှတ်ကို ပို့ပြီး မှတ်တဲ့ အချိန် တစ်ခါပဲ မှတ်တယ် — နောက်က လိုက် ကြည့်တာ မဟုတ်ပါ": {
  "en": "Your place is noted once, at the moment you tap delivered — nobody follows you live",
  "zh": "位置只在你点送达的那一刻记一次 — 不是在后面跟踪你"
 },
 "နောက် ဆုံး ၅၀ ခု ပဲ ပြ ထား တယ်": {
  "en": "Only the latest 50 are shown",
  "zh": "只显示最新的 50 条"
 },
 "နောက်ဆုံး Orders": {
  "en": "Latest orders",
  "my": "နောက်ဆုံး ဘောက်ချာများ",
  "zh": "最新订单"
 },
 "နောက်ဆုံး download ဆွဲ ချိန်": {
  "en": "Last downloaded",
  "zh": "上次下载时间"
 },
 "နောက်ဆုံး မိမိ Orders": {
  "en": "My latest orders",
  "my": "နောက်ဆုံး မိမိ ဘောက်ချာများ",
  "zh": "我的最新订单"
 },
 "နောက်ဆုံး ဝယ်ရက်": {
  "en": "Last bought",
  "zh": "上次购买日期"
 },
 "နောက်ပြန်": {
  "en": "Back",
  "zh": "返回"
 },
 "နောက်မှ လာပို့ရန်": {
  "en": "Asked to come later",
  "zh": "让改天再来送"
 },
 "နေ့ အလိုက် မှတ်တမ်း": {
  "en": "The day's entries",
  "zh": "当天的记录"
 },
 "နေ့ကျော်သွားပြီ": {
  "en": "The day has passed",
  "zh": "日期已经过了"
 },
 "နှစ် အလိုက်": {
  "en": "By year",
  "zh": "按年"
 },
 "နှုတ်ပေးရမယ့် တန်ဖိုး (Ks)": {
  "en": "Value to deduct (Ks)",
  "zh": "要扣的价值 (Ks)"
 },
 "နှုန်း မတွေ့ — ဖြည့်ရန်": {
  "en": "No rate found — fill one in",
  "zh": "没找到比率 — 请填一个"
 },
 "နှုန်း — ၁ ထုပ် = အသေး ဘယ်နှစ် ခု": {
  "en": "Rate — how many small units in 1 pack",
  "zh": "比率 — 1 包 = 多少个小的"
 },
 "နှုန်းက ၁ ထက် ကြီးရမယ်": {
  "en": "The rate must be more than 1",
  "zh": "比率必须大于 1"
 },
 "နှုန်းကို ချိတ်ထားတုန်း ပြင်လို့ မရဘူး — stock မှတ်တမ်းတွေ ရှိနေလို့ပါ။ နှုန်း ပြောင်းချင်ရင် အရင် ချိတ်ဖြုတ်ပြီး အသစ် ပြန် ချိတ်ပါ။": {
  "en": "The rate cannot be changed while linked — stock records already use it. To change the rate, unlink first and then link again.",
  "zh": "关联期间不能改比率 — 已经有库存记录在用了。要改比率,先取消关联再重新关联。"
 },
 "ပင်မ": {
  "en": "Home",
  "zh": "首页"
 },
 "ပင်မ · Dashboard": {
  "en": "Home · Dashboard",
  "zh": "首页 · Dashboard"
 },
 "ပစ္စည်း": {
  "en": "Product",
  "zh": "商品"
 },
 "ပစ္စည်း & Stock": {
  "en": "Products & Stock",
  "my": "ပစ္စည်း & လက်ကျန်",
  "zh": "商品 & 库存"
 },
 "ပစ္စည်း {n} မျိုး": {
  "en": "{n} items",
  "zh": "{n} 种商品"
 },
 "ပစ္စည်း စာမျက်နှာ": {
  "en": "Product page",
  "zh": "商品页"
 },
 "ပစ္စည်း ထပ်ထည့်": {
  "en": "Add a product",
  "zh": "添加商品"
 },
 "ပစ္စည်း ထပ်ထည့်ပြီးပြီ": {
  "en": "Product added",
  "zh": "商品已添加"
 },
 "ပစ္စည်း နဲ့ ဈေး လိုပါတယ်": {
  "en": "A product and a price are needed",
  "zh": "需要商品和价格"
 },
 "ပစ္စည်း ပို့ပြီးမှ ငွေ မှတ်လို့ရပါမယ်": {
  "en": "Payments can only be recorded after delivery",
  "zh": "货送到后才能记录收款"
 },
 "ပစ္စည်း ပြင် — {sku}": {
  "en": "Edit product — {sku}",
  "zh": "编辑商品 — {sku}"
 },
 "ပစ္စည်း ပြောင်းမယ်": {
  "en": "Change product",
  "zh": "换商品"
 },
 "ပစ္စည်း မကျန်တော့ပါ — အနည်းဆုံး ၁ မျိုး ကျန်ရမယ်": {
  "en": "No products would remain — at least 1 must stay",
  "zh": "商品不能全删 — 至少要留 1 种"
 },
 "ပစ္စည်း ရွေးရန်": {
  "en": "Pick products",
  "zh": "选择商品"
 },
 "ပစ္စည်း ရှာရန် (နာမည် / SKU)": {
  "en": "Search a product (name / SKU)",
  "zh": "搜索商品 (名字 / SKU)"
 },
 "ပစ္စည်း အကုန်": {
  "en": "All products",
  "zh": "全部商品"
 },
 "ပစ္စည်း အနည်းဆုံး ၁ မျိုး ကျန်ရမယ် — တစ်ခုလုံး ဖျက်ချင်ရင် ပယ်ဖျက် သုံးပါ": {
  "en": "At least one product must remain — to remove everything, cancel the invoice instead",
  "zh": "至少要留 1 种商品 — 想全删就取消整张发票"
 },
 "ပစ္စည်း အနည်းဆုံး ၁ မျိုး ထည့်ပါ": {
  "en": "Add at least one product",
  "zh": "至少添加 1 种商品"
 },
 "ပစ္စည်း အမျိုးအစား": {
  "en": "Product types",
  "my": "ပစ္စည်း မျိုး",
  "zh": "商品种类"
 },
 "ပစ္စည်း အသစ်": {
  "en": "New product",
  "zh": "新商品"
 },
 "ပစ္စည်း ၁ မျိုး အကြောင်း (အရင် ရွေးပါ)": {
  "en": "About one product (pick it first)",
  "zh": "关于一种商品 (先选它)"
 },
 "ပစ္စည်းကို စာရင်းထဲက ရွေးပါ": {
  "en": "Please pick the product from the list",
  "zh": "请从列表里选择商品"
 },
 "ပစ္စည်းနဲ့ အရေအတွက် မှန်အောင် ထည့်ပါ": {
  "en": "Enter the product and quantity correctly",
  "zh": "请正确填写商品和数量"
 },
 "ပစ္စည်းများ": {
  "en": "Products",
  "zh": "商品"
 },
 "ပစ္စည်းများ ({n} ခု)": {
  "en": "Products ({n})",
  "zh": "商品 ({n} 种)"
 },
 "ပစ္စည်းအလိုက် လျှော့ဈေး": {
  "en": "per-line discount",
  "zh": "每行折扣"
 },
 "ပထမ နေ့ — လက် နဲ့ ရေတွက် ထည့်ပါ": {
  "en": "First day — enter a counted opening",
  "zh": "首日，请输入实点期初"
 },
 "ပထမဆုံး Setup": {
  "en": "First-time setup",
  "zh": "第一次设置"
 },
 "ပမာဏ": {
  "en": "Amount",
  "zh": "金额"
 },
 "ပမာဏ (0 ထက် ကြီး) လိုပါတယ်": {
  "en": "An amount (more than 0) is needed",
  "zh": "需要金额 (大于 0)"
 },
 "ပမာဏ (Ks)": {
  "en": "Amount (Ks)",
  "zh": "金额 (Ks)"
 },
 "ပမာဏ (Ks) *": {
  "en": "Amount (Ks) *",
  "zh": "金额 (Ks) *"
 },
 "ပမာဏမှာ ကျန်ငွေ အပြည့် ထည့်ထားပြီး — တစ်ပိုင်း ပဲ ရရင် ရတဲ့ ပမာဏ ပြင်ထည့်ပါ။ အပြည့် ရတာနဲ့ \"ရပြီး\" အလိုအလျောက် ဖြစ်သွားမယ်။": {
  "en": "The amount is filled with the full balance — if you received only part, change it to the amount received. When the full amount is received it becomes \"received\" automatically.",
  "zh": "金额已填成全部余额 — 如果只收到一部分,改成实际收到的金额。全额收到后会自动变成“已收到”。"
 },
 "ပယ်ဖျက်": {
  "en": "Cancelled",
  "zh": "已取消"
 },
 "ပယ်ဖျက် ❌": {
  "en": "Cancelled ❌",
  "zh": "已取消 ❌"
 },
 "ပယ်ဖျက်/ပိတ် လုပ်ရင် ရောက်ပြီးသား အပိုင်းက PUR အဖြစ် ဆက် ကျန်နေမယ် — မရောက်သေးတဲ့ အပိုင်းကိုပဲ သက်ရောက်တယ်။": {
  "en": "Cancelling or closing only affects the part that has not arrived — the part already received stays as a PUR.",
  "zh": "取消/关闭只影响还没到的部分 — 已经收到的部分继续留作 PUR。"
 },
 "ပယ်ဖျက်မယ်": {
  "en": "Cancel it",
  "zh": "取消它"
 },
 "ပယ်ဖျက်မယ် ဆိုရင်": {
  "en": "To cancel",
  "zh": "要取消的话"
 },
 "ပယ်ဖျက်ရတဲ့ အကြောင်းပြချက် ရေးပါ": {
  "en": "Please write the reason for cancelling",
  "zh": "请写取消的原因"
 },
 "ပယ်ဖျက်သူ": {
  "en": "Cancelled by",
  "zh": "取消人"
 },
 "ပါဝင်မှု": {
  "en": "Pack",
  "zh": "每包数量"
 },
 "ပါဝင်မှု (Pack) — ၁ ဖာ/အိတ်/ပုံး မှာ ပါတဲ့ အရေအတွက်": {
  "en": "Pack — pieces in 1 case/bag/box",
  "zh": "每包数量 (Pack) — 1 箱/袋/盒里有多少个"
 },
 "ပါဝင်မှု အခြေခံ": {
  "en": "From pack details",
  "zh": "按包装明细"
 },
 "ပိတ် ပြီး နောက် ပြောင်း သွား တယ် — ပြင်ဆင် မှတ်တမ်း ကို ဖွင့် ထား တဲ့ နေ့ မှာ ထည့်ပါ": {
  "en": "Changed after close — enter the correction on the open day",
  "zh": "结账后有变动，请在未结账日期中录入更正"
 },
 "ပိတ်ထား": {
  "en": "Disabled",
  "zh": "已停用"
 },
 "ပိတ်ထားတယ်": {
  "en": "Off",
  "zh": "关"
 },
 "ပိတ်ထားရင် Leader က ဒီ စာမျက်နှာ ကို ဝင်လို့ မရဘူး — သူ့ ပင်မ စာမျက်နှာမှာလည်း ဝင်ပေါက် မပြဘူး။": {
  "en": "When off, the Leader cannot open this page — and the entry link is hidden on his home page too.",
  "zh": "关了以后,Leader 打不开这个页面 — 他的首页上也不会显示入口。"
 },
 "ပိတ်ပြီးပြီ ✅": {
  "en": "Closed ✅",
  "zh": "已结账 ✅"
 },
 "ပိတ်ပြီးသား နေ့ ကို ပြင် လို့ မရဘူး": {
  "en": "A closed day cannot be changed",
  "zh": "已结账的日期不能修改"
 },
 "ပိတ်မယ်": {
  "en": "Close",
  "zh": "关闭"
 },
 "ပိတ်ရန်": {
  "en": "Disable",
  "zh": "停用"
 },
 "ပိတ်ရန် အတည်ပြု": {
  "en": "Confirm close",
  "zh": "确认结账"
 },
 "ပိုင်ရှင် ထုတ် ငွေ": {
  "en": "Owner draw",
  "zh": "业主提款"
 },
 "ပို့ တဲ့ နေရာ": {
  "en": "Where it was delivered",
  "zh": "送到的地点"
 },
 "ပို့ ဘောက်ချာ မရှိသေးဘူး": {
  "en": "No invoices to deliver yet",
  "zh": "还没有要送的发票"
 },
 "ပို့ပြီး": {
  "en": "Delivered",
  "zh": "已送达"
 },
 "ပို့ပြီး ဘောက်ချာ ကို ပယ်ဖျက်လို့ မရတော့ဘူး — ကုန် ပြန်လာရင် Returns မှာ မှတ်ပါ": {
  "en": "A delivered invoice cannot be cancelled — if goods come back, record them under Returns",
  "zh": "已送达的发票不能取消 — 货退回来就在 Returns 里记录"
 },
 "ပို့ပြီး ဘောက်ချာ: {c} စောင်": {
  "en": "Delivered invoices: {c}",
  "zh": "已送达发票:{c} 张"
 },
 "ပို့ပြီး မှတ်ပြီးပြီ": {
  "en": "Marked as delivered",
  "zh": "已标记为送达"
 },
 "ပို့ပြီး မှတ်ပြီးပြီ (တစ်ပိုင်း — stock ရှိသလောက်ပဲ ပို့ပြီး ကျန်တာ ပယ်ပါတယ်)": {
  "en": "Marked delivered (partial — sent what was in stock, the rest is cancelled)",
  "zh": "已标记配送(部分 — 按现有库存配送,其余取消)"
 },
 "ပို့ပြီး မှတ်မယ်": {
  "en": "Mark as delivered",
  "zh": "标记为送达"
 },
 "ပို့ပြီးပြီလို့ မှတ်ပြီး stock နှုတ်မလား?": {
  "en": "Mark as delivered and deduct stock?",
  "zh": "标记为送达并扣库存吗?"
 },
 "ပို့ပြီးသား ရှင်းရန်ကို ပြင်လို့ မရဘူး": {
  "en": "A settlement that is already sent can't be changed",
  "zh": "已经发送的结算不能改"
 },
 "ပို့ပြီးသား ရှင်းရန်မှာ ပြန်ပို့ ဖျက်လို့ မရဘူး": {
  "en": "Returns can't be deleted from a settlement that is already sent",
  "zh": "已经发送的结算里的退货不能删"
 },
 "ပို့ရန်": {
  "en": "To deliver",
  "zh": "待送"
 },
 "ပို့ရန် ကျန်": {
  "en": "Still to deliver",
  "zh": "还没送的"
 },
 "ပို့ရန် ဘောက်ချာများ": {
  "en": "Invoices to deliver",
  "zh": "要送的发票"
 },
 "ပို့သူ": {
  "en": "Delivery person",
  "zh": "送货员"
 },
 "ပို့သူ က ပို့ပြီး မှတ်တယ်": {
  "en": "Delivery marked as delivered",
  "zh": "送货员标记为已送达"
 },
 "ပို့သူ က ပို့ပြီး မှတ်တယ် (တစ်ပိုင်း — ကုန်မရ ပယ် ပါတယ်)": {
  "en": "Delivery marked as delivered (partly — some goods were not available and were cancelled)",
  "zh": "送货员标记为已送达 (一部分 — 有些货没有,已取消)"
 },
 "ပို့သူ က မပို့ဖြစ် မှတ်တယ် — ဘောက်ချာ ပယ်ဖျက်ပြီး": {
  "en": "Delivery could not be made — invoice cancelled",
  "zh": "送货员记录没送成 — 发票已取消"
 },
 "ပို့သူ ကောက်ငွေ စစ်ရန်: {v} Ks": {
  "en": "Delivery collections to check: {v} Ks",
  "zh": "送货员收的钱待核对:{v} Ks"
 },
 "ပို့သူ မှတ်ထားတာ — ရုံး က စစ်ပြီး အတည်ပြုမှ \"ရပြီး\" ဖြစ်မယ်": {
  "en": "Recorded by the delivery person — it only counts as received after the office checks and confirms it",
  "zh": "送货员记录的 — 办公室核对确认后才算“已收到”"
 },
 "ပို့သူ ရွေးပါ": {
  "en": "Choose the delivery person",
  "zh": "请选择送货员"
 },
 "ပုံ": {
  "en": "Image",
  "zh": "图片"
 },
 "ပုံ (WebP/JPG, max 5MB)": {
  "en": "Image (WebP/JPG, max 5MB)",
  "zh": "图片 (WebP/JPG,最大 5MB)"
 },
 "ပုံနှိပ်ပြီးသား": {
  "en": "Already printed",
  "zh": "已经打印过了"
 },
 "ပုံမှန် မှတ်ရုံ": {
  "en": "Record only, as usual",
  "zh": "照常只记录"
 },
 "ပေါက်ဈေး က Base ကို ကျော် သွား ပြီ — ကာကွယ် မကျန် တော့ဘူး": {
  "en": "The market price has passed Base — no buffer left",
  "zh": "市场价已经超过 Base 了 — 没有缓冲了"
 },
 "ပေါက်ဈေး က ခု အရင်း ထက် များ နေတယ် — ပြန် ဝယ် ရင် အရင်း တက် မယ်": {
  "en": "The market price is above our current cost — buying again will raise the cost",
  "zh": "市场价比我们现在的成本高 — 再买成本会涨"
 },
 "ပေါက်ဈေး ဝယ် {p} Ks ({src})": {
  "en": "Market buy {p} Ks ({src})",
  "zh": "市场买价 {p} Ks ({src})"
 },
 "ပေါက်ဈေး ဝယ်: {p} Ks ({src} · {d})": {
  "en": "Market buy: {p} Ks ({src} · {d})",
  "zh": "市场买价:{p} Ks ({src} · {d})"
 },
 "ပေါင်းမယ် (Merge)": {
  "en": "Merge",
  "zh": "合并 (Merge)"
 },
 "ပေးပြီး": {
  "en": "Paid",
  "zh": "已付款"
 },
 "ပေးရန် ဘောက်ချာ မရှိဘူး": {
  "en": "No invoices to give out",
  "zh": "没有要分配的发票"
 },
 "ပျက်စီး": {
  "en": "Damaged",
  "zh": "损坏"
 },
 "ပျက်စီး/ပျောက် အတွက် လျာထား % — တွက်ဈေး ထဲမှာ ပဲ သုံးတယ်, ဈေး ကို အလိုအလျောက် မပြောင်း ဘူး": {
  "en": "Allowance % for damage/loss — used only in the costing view; it never changes a price by itself",
  "zh": "损坏/丢失预留 % — 只用在核算价里,不会自动改价格"
 },
 "ပျက်စီး/သက်တမ်းကုန် (ဖျက်သိမ်း)": {
  "en": "Damaged/expired (write off)",
  "zh": "损坏/过期 (核销)"
 },
 "ပျမ်းမျှ": {
  "en": "Average",
  "zh": "平均"
 },
 "ပျမ်းမျှ = ပယ်ဖျက် မပါတဲ့ ဘောက်ချာ တစ်စောင်ချင်း ပျမ်းမျှ တန်ဖိုး · ငွေ ရပြီး = လက်ခံ ရရှိပြီး ငွေ စုစုပေါင်း (ကွင်းထဲက ကိန်းက အပြည့် ရပြီး ဘောက်ချာ စောင်) · Credit = ပို့ပြီး ငွေကျန် ရှိနေတဲ့ ပမာဏ (လိုက် ကောက်ရမယ့် ကိန်း) · မရသေး (မပို့ရသေး) = မပို့ရသေးတဲ့ ဘောက်ချာများ၏ တန်ဖိုး": {
  "en": "Average = average value per invoice, excluding cancelled · Money received = total money taken in (the number in brackets is fully-paid invoices) · Credit = delivered amount still outstanding (what to collect) · Not yet (undelivered) = value of invoices not yet delivered",
  "zh": "平均 = 不含已取消的每张发票平均价值 · 已收钱 = 一共收到的钱 (括号里的数字是全额收齐的发票张数) · 欠款 = 已送达还没收的金额 (要去收的数) · 还没收到 (没送) = 还没送达发票的价值"
 },
 "ပြင်": {
  "en": "Edit",
  "zh": "编辑"
 },
 "ပြင် #{n}": {
  "en": "edit #{n}",
  "zh": "修改 #{n}"
 },
 "ပြင်ချက် {ok} ခု တင်ပြီးပြီ ✓": {
  "en": "Edits sent: {ok} applied ✓",
  "zh": "修改已发送:{ok} 条已生效 ✓"
 },
 "ပြင်ချက် မဝင်လိုက်ဘူး": {
  "en": "Edit didn't go through",
  "zh": "修改没通过"
 },
 "ပြင်ချက် သိမ်းထားပြီ — လိုင်း ရရင် တင်မယ် (စည်းမျဉ်း စစ်ပြီးမှ အတည်ပြုမယ်)": {
  "en": "Edit saved — it will be sent when you have a connection (the rules are checked before it is applied)",
  "zh": "修改已保存 — 有网络时会发送 (生效前会先检查规则)"
 },
 "ပြင်ခွင့် ရှိတယ်။ Unit / ပါဝင်မှု / နာမည် တွေက Warehouse နဲ့ Owner ရဲ့ အပိုင်း။": {
  "en": "can be edited. Unit / Pack / Name belong to Warehouse and Owner.",
  "zh": "可以修改。单位 / 每包数量 / 名字是 Warehouse 和 Owner 的部分。"
 },
 "ပြင်ခွင့် ၂ ကြိမ် ကုန်ပြီ": {
  "en": "Both edits (2) are used up",
  "zh": "2 次修改都用完了"
 },
 "ပြင်ဆင် ပမာဏ": {
  "en": "Adjustment",
  "zh": "调整金额"
 },
 "ပြင်ဆင် ပမာဏ (+/− Ks)": {
  "en": "Adjustment amount (+/− Ks)",
  "zh": "调整金额 (+/− Ks)"
 },
 "ပြင်ဆင် ပမာဏ သိမ်းပြီ": {
  "en": "Adjustment saved",
  "zh": "调整已保存"
 },
 "ပြင်ဆင် မှတ်တမ်း": {
  "en": "Adjustment entry",
  "zh": "调整记录"
 },
 "ပြင်ဆင် မှတ်တမ်း ကို ဖွင့် ထား တဲ့ နေ့ မှာ ထည့်ပါ": {
  "en": "Enter the correction on the open day instead",
  "zh": "请在未结账的日期中录入更正"
 },
 "ပြင်ဆင်ခွင့် သုံးပြီး: Sales {a}/{cap} ကြိမ် · Warehouse {b}/{cap} ကြိမ်": {
  "en": "Edits used: Sales {a}/{cap} · Warehouse {b}/{cap}",
  "zh": "已用修改次数:Sales {a}/{cap} 次 · Warehouse {b}/{cap} 次"
 },
 "ပြင်ဆင်နေ 📝": {
  "en": "Draft 📝",
  "zh": "准备中 📝"
 },
 "ပြင်ဆင်မှု မှတ်တမ်း": {
  "en": "Edit history",
  "zh": "修改记录"
 },
 "ပြင်ဆင်မှု မှတ်တမ်း မရှိသေးဘူး": {
  "en": "No edit records yet",
  "zh": "还没有修改记录"
 },
 "ပြင်တာတွေ သိမ်းမယ်": {
  "en": "Save changes",
  "zh": "保存修改"
 },
 "ပြင်ပြီ ✓": {
  "en": "Updated ✓",
  "zh": "已更新 ✓"
 },
 "ပြင်ပြီး/ညှိပြီးရင် နှိပ်ပါ — ဒီ အရင်းအဆင့်နဲ့ဆို စာရင်းက ပျောက်မယ်၊ အရင်း ထပ်တက်မှ ပြန်ပေါ်မယ်": {
  "en": "Press after you have fixed/adjusted it — at this cost level it leaves the list, and comes back only if the cost rises again",
  "zh": "改好/调好后按一下 — 在这个成本水平它会离开列表,只有成本再涨才会回来"
 },
 "ပြင်ပြီးပြီ": {
  "en": "Updated",
  "zh": "已更新"
 },
 "ပြင်ပြီးပြီ — ဒီ ဘောက်ချာ တစ်စောင် အနေနဲ့ ပဲ တင်မယ်": {
  "en": "Edited — it will be sent as this one invoice",
  "zh": "已修改 — 会按这一张发票发送"
 },
 "ပြင်မယ်": {
  "en": "Update",
  "zh": "更新"
 },
 "ပြင်ရန်": {
  "en": "Edit",
  "zh": "编辑"
 },
 "ပြင်သူ": {
  "en": "Edited by",
  "zh": "修改人"
 },
 "ပြန် ရှင်းပုံ": {
  "en": "How it is settled",
  "zh": "结算方式"
 },
 "ပြန်ပို့": {
  "en": "Returns",
  "zh": "退货"
 },
 "ပြန်ပို့ စုစုပေါင်း": {
  "en": "Returns total",
  "zh": "退货总计"
 },
 "ပြန်ပို့ မရှိဘူး": {
  "en": "No returns",
  "zh": "没有退货"
 },
 "ပြန်ပို့ မှတ်တမ်း ဖျက်ပြီ — stock ပြန် ပေါင်းပြီ": {
  "en": "Return record deleted — stock added back",
  "zh": "退货记录已删除 — 库存已加回"
 },
 "ပြန်ပို့ မှတ်တမ်း ဖျက်ပြီး stock ပြန် ပေါင်းမလား?": {
  "en": "Delete this return record and add the stock back?",
  "zh": "删掉这条退货记录并把库存加回吗?"
 },
 "ပြန်ပို့ မှတ်တာနဲ့ stock က တန်း နုတ်ပြီး ကျသင့် ပမာဏ လျော့သွားမယ်။ တန်ဖိုး ကို ": {
  "en": "Recording a return deducts stock right away and lowers the payable amount. The value is worked out automatically at ",
  "zh": "一记录退货,库存马上扣掉,应付金额也会降低。价值按 "
 },
 "ပြန်ပို့ အရေအတွက်": {
  "en": "Return qty",
  "zh": "退货数量"
 },
 "ပြန်ဖွင့်မယ်": {
  "en": "Reopen",
  "zh": "重新打开"
 },
 "ပြန်အပ်": {
  "en": "Return",
  "zh": "退货"
 },
 "ပြန်အပ် / ပျက်စီး": {
  "en": "Returns / Damage",
  "zh": "退货 / 损坏"
 },
 "ပြန်အပ် စစ်ရန်: {c} ခု": {
  "en": "Returns to check: {c}",
  "zh": "待核对退货:{c} 条"
 },
 "ပြီးဆုံး ✅": {
  "en": "Done ✅",
  "zh": "完成 ✅"
 },
 "ပြေစာ ဈေး စုစုပေါင်း {bill} နဲ့ Actual တွက် {gross} ကွာခြား: {diff} Ks": {
  "en": "Difference between invoice-price total {bill} and Actual total {gross}: {diff} Ks",
  "zh": "票面价格总计 {bill} 和 Actual 计算 {gross} 的差额:{diff} Ks"
 },
 "ပြောင်း": {
  "en": "Change",
  "zh": "更改"
 },
 "ပြောင်းလဲမှု": {
  "en": "Changes",
  "zh": "变化"
 },
 "ဖိုင်": {
  "en": "File",
  "zh": "文件"
 },
 "ဖျက်": {
  "en": "Delete",
  "zh": "删除"
 },
 "ဖျက်မယ်": {
  "en": "Delete it",
  "zh": "删掉"
 },
 "ဖျက်မှာ သေချာလား?": {
  "en": "Are you sure you want to delete this?",
  "zh": "确定要删除吗?"
 },
 "ဖျက်လိုက်ပြီ": {
  "en": "Deleted",
  "zh": "已删除"
 },
 "ဖျော်ဖြေရေးဆိုင်": {
  "en": "Entertainment venue",
  "zh": "娱乐场所"
 },
 "ဖြစ်နိုင်ခြေ": {
  "en": "Probable",
  "zh": "可能到访"
 },
 "ဖြည့် (Digital/Banking ထဲ)": {
  "en": "Top up (into Digital/Banking)",
  "zh": "补款（转入 Digital/Banking）"
 },
 "ဖြုတ်": {
  "en": "Remove",
  "zh": "移除"
 },
 "ဖြုတ်ပြီးပြီ": {
  "en": "Removed",
  "zh": "已移除"
 },
 "ဖြုတ်ရန်": {
  "en": "Remove",
  "zh": "移除"
 },
 "ဖြုတ်လိုက်ရင် အပြည့် အရေအတွက်ကို အထုပ် ကြီး ဆီ ခွဲပေးပြီး ကျန်တဲ့ အဖြည်း ကို အခြေခံ ပစ္စည်းမှာ ထားမယ်။ ဖြုတ်တာကို Owner ပဲ လုပ်လို့ရတယ်။": {
  "en": "Unlinking gives the full packs to the big pack product and keeps the loose remainder on the base product. Only the Owner can unlink.",
  "zh": "取消关联后,整包会归到大包装商品,零散的余数留在基础商品上。只有 Owner 能取消关联。"
 },
 "ဖွင့် ထား တယ်": {
  "en": "Open",
  "zh": "未结账"
 },
 "ဖွင့်ထား": {
  "en": "Active",
  "zh": "启用中"
 },
 "ဖွင့်ထားတယ်": {
  "en": "On",
  "zh": "开"
 },
 "ဖွင့်ထားတဲ့ စလစ် မရှိဘူး — အပေါ်က ဝယ်ရန် စာရင်းကနေ စလစ် ထုတ်လိုက်ရင် ဒီမှာ ပေါ်လာမယ်။": {
  "en": "No open slips — raise a slip from the buy list above and it will show here.",
  "zh": "没有打开的单据 — 从上面的采购清单建一张单据,就会出现在这里。"
 },
 "ဖွင့်ထားတဲ့ အမှာတွေပဲ ပြထားတယ်": {
  "en": "Only open requests are shown",
  "zh": "只显示未完成的订购单"
 },
 "ဖွင့်နေတယ်…": {
  "en": "Creating…",
  "zh": "创建中…"
 },
 "ဖွင့်မယ်": {
  "en": "Open",
  "zh": "打开"
 },
 "ဖွင့်ရန်": {
  "en": "Enable",
  "zh": "启用"
 },
 "ဘဏ်": {
  "en": "Bank",
  "zh": "银行"
 },
 "ဘဏ် အပ် ငွေ": {
  "en": "Bank deposit",
  "zh": "存入银行"
 },
 "ဘယ် ဆိုင် လဲ?": {
  "en": "Which shop?",
  "zh": "是哪一家店?"
 },
 "ဘယ် သူ ဆီ က": {
  "en": "From whom",
  "zh": "从谁那里"
 },
 "ဘယ် သူ ဆီ က ဈေး လဲ": {
  "en": "Whose price is it?",
  "zh": "这是谁的价格?"
 },
 "ဘယ် သူ ဆီ က အ သက်သာ ဆုံး လဲ": {
  "en": "Who is cheapest to buy from?",
  "zh": "从谁那里买最便宜?"
 },
 "ဘယ် သူ အ သက်သာ ဆုံး လဲ": {
  "en": "Who is cheapest?",
  "zh": "谁最便宜?"
 },
 "ဘယ် အုပ်စု ထဲ ဖြည့် မလဲ": {
  "en": "Top up into which group?",
  "zh": "补入哪个账户？"
 },
 "ဘယ်သူ့ဆီက ဝယ်မလဲ": {
  "en": "Who will you buy from?",
  "zh": "向谁买?"
 },
 "ဘာလို့ လို နေ တာ လဲ": {
  "en": "Why is this needed?",
  "zh": "为什么需要这个?"
 },
 "ဘောက်ချာ": {
  "en": "Invoices",
  "zh": "发票"
 },
 "ဘောက်ချာ ကိစ္စ": {
  "en": "Invoice matters",
  "zh": "发票事项"
 },
 "ဘောက်ချာ ငွေ": {
  "en": "Invoice payment",
  "zh": "发票付款"
 },
 "ဘောက်ချာ စာရင်း": {
  "en": "Invoice list",
  "zh": "发票列表"
 },
 "ဘောက်ချာ တင်ပြီးပြီ: {code}": {
  "en": "Invoice submitted: {code}",
  "zh": "发票已提交:{code}"
 },
 "ဘောက်ချာ တစ်ခု ပြင်တိုင်း ဒီမှာ မှတ်တမ်း ပေါ်တယ် — ဘယ်သူ၊ ဘယ်အချိန်၊ ဘာတွေ ပြောင်းခဲ့လဲ။ လိုအပ်ရင် ပြင်တဲ့ သူကို ပြန် မေးလို့ရအောင်။": {
  "en": "Every time an invoice is edited, a record shows here — who, when and what changed — so you can ask the person who edited it if needed.",
  "zh": "每次修改发票,这里都会有一条记录 — 谁、什么时候、改了什么。需要时可以去问修改的人。"
 },
 "ဘောက်ချာ ပြင်ပြီးပြီ — မှတ်တမ်း တင်ထားတယ်": {
  "en": "Invoice updated — the change is recorded",
  "zh": "发票已更新 — 修改已记录"
 },
 "ဘောက်ချာ လိုအပ်": {
  "en": "Invoice need",
  "zh": "需要发票"
 },
 "ဘောက်ချာ လိုအပ် ချက် က စ တဲ့ အဝယ်": {
  "en": "starts from invoice demand",
  "zh": "从订单需求开始"
 },
 "ဘောက်ချာ အသေးစိတ်": {
  "en": "Invoice details",
  "zh": "发票明细"
 },
 "ဘောက်ချာနံပါတ်": {
  "en": "Invoice no.",
  "zh": "发票编号"
 },
 "မင်္ဂလာပါ": {
  "en": "Hello",
  "zh": "你好"
 },
 "မဆွဲ ရသေးဘူး": {
  "en": "Not yet",
  "zh": "还没有"
 },
 "မတွေ့ဘူး — နာမည် / SKU ပြန် စစ်ပါ": {
  "en": "Not found — check the name / SKU again",
  "zh": "没找到 — 再检查名字 / SKU"
 },
 "မတွေ့ဘူး။": {
  "en": "Not found.",
  "zh": "没找到。"
 },
 "မထားရင် ဗလာ": {
  "en": "Leave blank for none",
  "zh": "没有就留空"
 },
 "မပို့နိုင်တဲ့ အကြောင်း": {
  "en": "Why it could not be delivered",
  "zh": "送不了的原因"
 },
 "မပို့ဖြစ် မှတ်တမ်း": {
  "en": "Could-not-deliver records",
  "zh": "送不了的记录"
 },
 "မပို့ဖြစ် မှတ်တမ်း မရှိဘူး": {
  "en": "No could-not-deliver records",
  "zh": "没有送不了的记录"
 },
 "မပို့ဖြစ် — ဘောက်ချာ ပယ်ဖျက်": {
  "en": "Could not deliver — cancel the invoice",
  "zh": "没送成 — 取消发票"
 },
 "မပို့ဖြစ် — ဘောက်ချာ ပယ်ဖျက်ပြီးပြီ": {
  "en": "Not delivered — invoice cancelled",
  "zh": "没送成 — 发票已取消"
 },
 "မပို့ရသေး တာ မရှိဘူး": {
  "en": "Nothing waiting to send",
  "zh": "没有等着发送的"
 },
 "မပို့ရသေး ဘောက်ချာ": {
  "en": "Undelivered invoices",
  "zh": "未送达发票"
 },
 "မပို့ရသေး အကုန်": {
  "en": "All undelivered",
  "zh": "全部未送达"
 },
 "မပို့ရသေးတဲ့ ဘောက်ချာ မရှိသေးဘူး — ဘောက်ချာ ဝင်မှ ဒီ စာရင်း ပေါ်မယ်။": {
  "en": "No undelivered invoices yet — this list appears once invoices come in.",
  "zh": "还没有未送达的发票 — 有发票进来后这个列表才会出现。"
 },
 "မပို့ရသေးတဲ့ ဘောက်ချာတွေထဲက ပစ္စည်းရဲ့ လက်ရှိ Actual Base က ရောင်းချိန် မှတ်ထားတဲ့ အရင်းထက် တက်သွားတဲ့ လိုင်းတွေကို စုပြထားတာပါ။": {
  "en": "Among undelivered invoices, these are the lines where the product's current Actual Base has risen above the cost recorded at sale time.",
  "zh": "在未送达的发票里,列出商品当前 Actual Base 已经涨过销售时记录成本的行。"
 },
 "မပို့ရသေးတဲ့ ဘောက်ချာပဲ ပြင်လို့ ရတယ်": {
  "en": "Only an invoice that is not delivered yet can be edited",
  "zh": "只有还没送达的发票能修改"
 },
 "မပို့ရသေးတဲ့ ဘောက်ချာမို့ ပြင်လို့ရတာ — ဈေး မပြောင်းဘူး၊": {
  "en": "This invoice hasn't been delivered yet, so it can be edited — the price doesn't change; you can edit the",
  "zh": "这张发票还没送达,所以能改 — 价格不变;你可以改"
 },
 "မပေးရသေး": {
  "en": "Not given yet",
  "zh": "还没给"
 },
 "မပြင်တော့": {
  "en": "Don't edit",
  "zh": "不改了"
 },
 "မပြင်တော့ဘူး": {
  "en": "Don't edit",
  "zh": "不改了"
 },
 "မမှတ်ရသေး — order နဲ့ အတူ GPS ဝင်လာရင် အလိုလို မှတ်မယ်": {
  "en": "Not recorded yet — it is saved automatically when GPS comes in with an order",
  "zh": "还没记录 — 订单带 GPS 一起来时会自动记录"
 },
 "မမှတ်ရသေးဘူး — ရုံးမှာ ထိုင်ရင်း အောက်က ခလုတ်ကို နှိပ်ပါ။": {
  "en": "Not saved yet — while sitting in the office, press the button below.",
  "zh": "还没保存 — 坐在办公室时,按下面的按钮。"
 },
 "မရ တဲ့ အပိုင်းကို ၀ / ရတဲ့ အရေအတွက် ပြင်ထည့်ပါ — ကျန်တာက ပယ် ဖြစ်သွားပြီး ထပ် မပို့တော့ဘူး။": {
  "en": "For goods that did not arrive, enter 0 or the amount that really went out — the rest is cancelled and will not be delivered later.",
  "zh": "没到的部分填 0 或实际送出的数量 — 剩下的会取消,不会再送。"
 },
 "မရသေး": {
  "en": "Not yet",
  "zh": "还没"
 },
 "မရသေး (မပို့ရသေး)": {
  "en": "Not yet (undelivered)",
  "zh": "还没 (未送达)"
 },
 "မရွေး": {
  "en": "No choice",
  "zh": "不选"
 },
 "မရွေးတော့": {
  "en": "Not picking",
  "zh": "不选了"
 },
 "မရှိ": {
  "en": "None",
  "zh": "没有"
 },
 "မရှိရင် အလွတ်ထားပါ — မထည့်လည်း ရတယ်": {
  "en": "If there is none, leave it blank — it is fine without one",
  "zh": "没有就留空 — 不填也可以"
 },
 "မဝယ် တာ ကြာ ပြီ ဆိုင်": {
  "en": "Shops not buying for a while",
  "zh": "很久没买的店铺"
 },
 "မသတ်မှတ်ရသေး": {
  "en": "Not set yet",
  "zh": "尚未设置"
 },
 "မသိ": {
  "en": "Don't know",
  "zh": "不知道"
 },
 "မိတ္တူ မရှိသေးဘူး — \"ခု ယူ မယ်\" ကို နှိပ်ပါ။": {
  "en": "No copies yet — press \"Take one now\".",
  "zh": "还没有副本 — 按“现在拿一个”。"
 },
 "မိတ္တူ ယူပြီးပြီ": {
  "en": "Copy taken",
  "zh": "副本已备好"
 },
 "မိမိ ဘောက်ချာများ": {
  "en": "My invoices",
  "zh": "我的发票"
 },
 "မိမိ အရောင်း": {
  "en": "My sales",
  "zh": "我的销售"
 },
 "မျှော်လင့် ငွေ (ပို့ပြီး ဘောက်ချာ)": {
  "en": "Expected money (from delivered invoices)",
  "zh": "预计的钱 (已送达发票)"
 },
 "မြစ်ကြီးနား": {
  "en": "Myitkyina",
  "zh": "密支那"
 },
 "မြို့": {
  "en": "Town",
  "zh": "城镇"
 },
 "မှ": {
  "en": "From",
  "zh": "从"
 },
 "မှတ်ချက်": {
  "en": "Note",
  "zh": "备注"
 },
 "မှတ်ချက် ပဲ — စာရင်း ထဲ မှာ Ks နဲ့ ပဲ တွက် ထား တယ်": {
  "en": "Note only — the books are kept in Ks",
  "zh": "仅作备注 — 账目只用 Ks 计算"
 },
 "မှတ်ချက်: 🔵 မှတ်တမ်းက Catalog ဖွင့်ထားချိန် ပဲ ဝင်တယ် (နောက်ကွယ် လိုက်ကြည့်နေတာ မဟုတ်)။ လိုင်း မရှိဘဲ မှတ်ထားတဲ့ မှတ်တမ်းဆို \"မှတ်ချိန်\" က လိုင်း ပြန် ရတဲ့ အချိန် ဖြစ်နေနိုင်တယ် — ရက် ကတော့ ဖုန်းက ရက် အမှန် ပဲ။ ဆိုင် နေရာ အမှတ် မရှိသေးတဲ့ ဆိုင် (တစ်ခါမှ order မဝင်ဖူးသေး) ကို 🔵 နဲ့ မဖမ်းနိုင်ဘူး။": {
  "en": "Note: 🔵 records are only made while the Catalog is open (nobody is being watched in the background). For a record saved without a connection, the \"recorded time\" may be the time the connection came back — the date is always the phone's real date. A shop with no saved place mark yet (never ordered from) cannot be caught with 🔵.",
  "zh": "备注:🔵 记录只在 Catalog 打开时才会产生 (不是在后台跟踪)。没有网络时保存的记录,“记录时间”可能是网络恢复的时间 — 日期永远是手机的真实日期。还没有位置点的店铺 (从来没下过单) 用 🔵 抓不到。"
 },
 "မှတ်ချိန်": {
  "en": "Recorded at",
  "zh": "记录时间"
 },
 "မှတ်ခွင့်ရှိသူ: Owner + Finance ပဲ။ Category က ကိုယ်တိုင် ရိုက်ထည့်ရတာ — အရင် သုံးဖူးတဲ့ category တွေ အကြံပြု ပြပေးမယ်၊ အသစ်လည်း ရိုက်ထည့်လို့ရတယ်။": {
  "en": "Only Owner + Finance can record. Categories are typed freely — ones you used before are suggested, and you can type new ones.",
  "my": "မှတ်ခွင့်ရှိသူ: Owner + Finance ပဲ။ အမျိုးအစား က ကိုယ်တိုင် ရိုက်ထည့်ရတာ — အရင် သုံးဖူးတဲ့ အမျိုးအစား တွေ အကြံပြု ပြပေးမယ်၊ အသစ်လည်း ရိုက်ထည့်လို့ရတယ်။",
  "zh": "只有 Owner + Finance 能记录。类别是自己输入的 — 以前用过的类别会建议给你,也可以输入新的。"
 },
 "မှတ်တမ်း စစ်": {
  "en": "Audit trail",
  "zh": "记录审查"
 },
 "မှတ်တမ်း မရှိသေးဘူး": {
  "en": "No records yet",
  "zh": "还没有记录"
 },
 "မှတ်တာ ပြန်ဖျက်လိုက်ပြီ": {
  "en": "Mark removed",
  "zh": "标记已移除"
 },
 "မှတ်ထားတဲ့ ကောက်ငွေ": {
  "en": "Recorded collections",
  "zh": "记录的收款"
 },
 "မှတ်ထားတဲ့ အမျိုးအစား": {
  "en": "Recorded notes",
  "zh": "已记录的面额"
 },
 "မှတ်ထားပြီး:": {
  "en": "Saved:",
  "zh": "已保存:"
 },
 "မှတ်ပြီးပြီ": {
  "en": "Recorded",
  "zh": "已记录"
 },
 "မှတ်ပြီးပြီ — Finance စစ်ရန် ကျန်တယ်": {
  "en": "Recorded — waiting for Finance to check",
  "zh": "已记录 — 等 Finance 核对"
 },
 "မှတ်မယ်": {
  "en": "Record",
  "zh": "记录"
 },
 "မှတ်သူ": {
  "en": "Recorded by",
  "zh": "记录人"
 },
 "မှာ ထား ပြီး stock ကုန် နေ တဲ့ ပစ္စည်း မရှိဘူး": {
  "en": "Nothing ordered is out of stock",
  "zh": "没有订了却断货的商品"
 },
 "မှာတဲ့": {
  "en": "Ordered",
  "zh": "订购的"
 },
 "မှာတဲ့ အရေအတွက်": {
  "en": "Ordered qty",
  "zh": "订购数量"
 },
 "မှာထား 📝": {
  "en": "Ordered 📝",
  "zh": "已订购 📝"
 },
 "မှာထားပြီး (ရောက်ရန်)": {
  "en": "Ordered (to arrive)",
  "zh": "已订购 (待到)"
 },
 "မှာမယ့် အရေအတွက်": {
  "en": "Qty to order",
  "zh": "要订的数量"
 },
 "မှား မှတ်မိလို့ ပြန် ဖျက်": {
  "en": "Recorded by mistake — removed",
  "zh": "记错了 — 已删除"
 },
 "ယူနစ်": {
  "en": "Unit",
  "zh": "单位"
 },
 "ရက်": {
  "en": "Date",
  "zh": "日期"
 },
 "ရက် {n} ကြာ ဈေး": {
  "en": "{n}-day-old price",
  "zh": "{n} 天前的价格"
 },
 "ရက် ကျော် (box မဖွင့်)": {
  "en": "Skipped days (box not opened)",
  "zh": "未开箱的日子"
 },
 "ရက် ရွေး": {
  "en": "Pick a day",
  "zh": "选一天"
 },
 "ရက် ရွေးရန်": {
  "en": "Pick a day",
  "zh": "选一天"
 },
 "ရက် အစ": {
  "en": "From date",
  "zh": "开始日期"
 },
 "ရက် အဆုံး": {
  "en": "To date",
  "zh": "结束日期"
 },
 "ရက် ၃၀ ကျော် မဝယ် တဲ့ ဆိုင် မရှိဘူး": {
  "en": "No shop has gone 30+ days without buying",
  "zh": "没有店铺超过 30 天没买了"
 },
 "ရပ်ကွက်": {
  "en": "Areas",
  "zh": "街区"
 },
 "ရပ်ကွက် / ဧရိယာ ({n} ခု)": {
  "en": "Areas ({n})",
  "zh": "街区 / 地区 ({n} 个)"
 },
 "ရပ်ကွက် ဖျက်ပြီးပြီ": {
  "en": "Area deleted",
  "zh": "街区已删除"
 },
 "ရပ်ကွက် အသစ် ထည့်ရန်": {
  "en": "Add a new area",
  "zh": "添加新街区"
 },
 "ရပြီး": {
  "en": "Paid",
  "zh": "已付款"
 },
 "ရုံး တည်နေရာ ဖျက်ပြီးပြီ": {
  "en": "Office location cleared",
  "zh": "办公室位置已清除"
 },
 "ရုံး တည်နေရာ ဖျက်မှာလား?": {
  "en": "Delete the office location?",
  "zh": "要删除办公室位置吗?"
 },
 "ရုံး တည်နေရာ မှတ်ပြီးပြီ 🏢": {
  "en": "Office location saved 🏢",
  "zh": "办公室位置已保存 🏢"
 },
 "ရုံးက ပို့ပြီး မှတ်တယ် (တစ်ပိုင်း — ကုန်မရ ပယ် ပါတယ်)": {
  "en": "Office marked delivered (partial — the rest was cancelled, no stock)",
  "zh": "办公室标记已配送(部分 — 其余因无货取消)"
 },
 "ရုံးချိန် (မနက် ၈:၃၀ – ညနေ ၅:၃၀) ကျော်သွားပြီ": {
  "en": "Office hours (8:30 AM – 5:30 PM) are over",
  "zh": "办公时间 (早上 8:30 – 下午 5:30) 已经过了"
 },
 "ရုံးချိန် (မနက် ၈:၃၀ – ညနေ ၅:၃၀) မဟုတ်တော့ပါ — Sales ပြင်ခွင့် ပိတ်ပြီ (Warehouse ကို ဆက်သွယ်ပါ)": {
  "en": "Outside office hours (8:30 AM – 5:30 PM) — sales editing is closed (contact Warehouse)",
  "zh": "不在办公时间 (早上 8:30 – 下午 5:30) — 业务员不能修改了 (请联系 Warehouse)"
 },
 "ရေတွက် ငွေ (ဒီနေ့)": {
  "en": "Counted cash (today)",
  "zh": "数的现金 (今天)"
 },
 "ရေတွက် ပြီးပြီ — အတည်ပြု စောင့် နေ တယ်": {
  "en": "Counted — awaiting verification",
  "zh": "已点钞，等待复核"
 },
 "ရေတွက် ပြီးမှ အတည်ပြု လို့ ရမယ်": {
  "en": "Verify only after counting",
  "zh": "请先点钞再复核"
 },
 "ရေတွက် မှတ်မယ်": {
  "en": "Record count",
  "zh": "记录点钞"
 },
 "ရေတွက် သူ": {
  "en": "Counter",
  "zh": "点钞人"
 },
 "ရေတွက် သူ နဲ့ အတည်ပြု သူ မတူ ရမယ်": {
  "en": "Counter and verifier must be different people",
  "zh": "点钞人与复核人不能是同一人"
 },
 "ရေတွက် အပိတ်": {
  "en": "Counted closing",
  "zh": "实点结余"
 },
 "ရေတွက် အရွက်": {
  "en": "Counted leaves",
  "zh": "实点张数"
 },
 "ရေတွက် အသစ် စမယ်": {
  "en": "Start a new count",
  "zh": "开始一次新盘点"
 },
 "ရေတွက်ငွေ မှတ်ပြီးပြီ": {
  "en": "Counted cash recorded",
  "zh": "数的现金已记录"
 },
 "ရေပြီး / စုစုပေါင်း": {
  "en": "Counted / Total",
  "zh": "已数 / 总计"
 },
 "ရောက်": {
  "en": "Visits",
  "zh": "到访"
 },
 "ရောက် စုစုပေါင်း": {
  "en": "Total visits",
  "zh": "到访总计"
 },
 "ရောက် မှတ်တမ်း": {
  "en": "Visit history",
  "zh": "到访记录"
 },
 "ရောက် အကြိမ် စုစုပေါင်း": {
  "en": "Total visits",
  "zh": "到访总次数"
 },
 "ရောက်တဲ့ အရေအတွက်": {
  "en": "Qty arrived",
  "zh": "到的数量"
 },
 "ရောက်တဲ့ အရေအတွက် ထည့်ပါ — ဘာမှ မရောက်ရင် အမှာ ကို ပိတ်ပါ": {
  "en": "Enter the quantity that actually arrived — if nothing arrived, close the request instead",
  "zh": "请输入实际到货数量 — 如果没有货到,请改为关闭订单"
 },
 "ရောက်ပြီး": {
  "en": "Arrived",
  "zh": "已到"
 },
 "ရောက်ပြီးသား line ကို ဖြုတ်လို့ မရဘူး": {
  "en": "A line that already arrived can't be removed",
  "zh": "已经到的行不能删除"
 },
 "ရောက်ရန် ကျန် {q}": {
  "en": "Still to arrive {q}",
  "zh": "待到货 {q}"
 },
 "ရောင်း တဲ့ နေရာ": {
  "en": "Where it was sold",
  "zh": "销售的地点"
 },
 "ရောင်း တဲ့ နေရာ နဲ့ ပို့ တဲ့ နေရာ ~{dist} ခြား နေ တယ် — စစ် ကြည့် ပါ": {
  "en": "The sold place and the delivered place are ~{dist} apart — please check",
  "zh": "销售地点和送货地点相距 ~{dist} — 请核对"
 },
 "ရောင်း တဲ့ နေရာ နဲ့ ပို့ တဲ့ နေရာ ဘယ် လောက် ခြား ရင် သတိ ပေး မလဲ (m)": {
  "en": "How far apart (in metres) before a place warning is raised?",
  "zh": "销售地点和送货地点相差多少米才提醒?(m)"
 },
 "ရောင်း တဲ့ နေရာ နဲ့ ပို့ တဲ့ နေရာ သတ် မှတ် ထား တာ ထက် ခြား သွား တဲ့ ပို့ ချက် တွေ ပါ — ကြည့် ပြီး ရင် ✓ စစ် ပြီး နှိပ် ပါ။": {
  "en": "These are the deliveries where the sold place and the delivered place were farther apart than the set distance — after you have looked, tap ✓ Checked.",
  "zh": "这些是销售地点和送货地点相距超过设定距离的送货 — 看完后点 ✓ 已核对。"
 },
 "ရောင်းချိန် အရင်း": {
  "en": "Cost at sale",
  "zh": "销售时成本"
 },
 "ရောင်းဈေး": {
  "en": "Selling",
  "zh": "售价"
 },
 "ရောင်းဈေး နောက်ဆုံး သတ်မှတ်ပြီးနောက် အရင်း (Actual) ပြောင်းသွားတဲ့ ပစ္စည်း တွေ ပါ။ ရောင်းဈေး ပြင် / မပြင် ဆုံးဖြတ် ဖို့ ကြည့်ရန် သက်သက် ပါ — ဈေး ကို အလိုအလျောက် မပြောင်း ပါ ဘူး။": {
  "en": "These products had a cost (Actual) change after their selling prices were last set. This list is only for deciding whether to change a selling price — it never changes a price by itself.",
  "zh": "这些商品是售价最后设定之后成本 (Actual) 变了的。这个列表只是用来决定要不要改售价 — 不会自动改价格。"
 },
 "ရောင်းဈေး သတ်မှတ်ချိန် အရင်း": {
  "en": "Cost when the selling price was set",
  "zh": "设定售价时的成本"
 },
 "ရောင်းပိတ် 🔴": {
  "en": "Sales closed 🔴",
  "zh": "销售关闭 🔴"
 },
 "ရောင်းဖွင့် 🟢": {
  "en": "Sales open 🟢",
  "zh": "销售开启 🟢"
 },
 "ရောင်းဖွင့်/ပိတ် အကုန်": {
  "en": "Sell on/off — all",
  "zh": "销售开关 — 全部"
 },
 "ရောင်းရငွေ": {
  "en": "Sales",
  "zh": "销售额"
 },
 "ရောင်းရန် ဖွင့်ထားသော SKU စုစုပေါင်း": {
  "en": "Total SKUs open for sale",
  "zh": "开启销售的 SKU 总数"
 },
 "ရောင်းဝယ်ရေး": {
  "en": "Trading",
  "zh": "贸易"
 },
 "ရောင်းသူ": {
  "en": "Seller",
  "zh": "卖家"
 },
 "ရောင်းသူ အလိုက် (ဒီ လ, ပို့ပြီး)": {
  "en": "By seller (this month, delivered)",
  "zh": "按卖家 (本月,已送达)"
 },
 "ရေးနေဆဲ": {
  "en": "Still writing",
  "zh": "还在写"
 },
 "ရွက်ရေ": {
  "en": "Note counts",
  "zh": "纸币张数"
 },
 "ရွက်ရေ ပြင်": {
  "en": "Correct note breakdown",
  "zh": "更正纸币明细"
 },
 "ရွက်ရေ ဖြည့်": {
  "en": "Fill in note breakdown",
  "zh": "补填纸币明细"
 },
 "ရွက်ရေ မပြည့် စုံ": {
  "en": "Note counts incomplete",
  "zh": "纸币明细不完整"
 },
 "ရွက်ရေ မမှတ်": {
  "en": "No note breakdown recorded",
  "zh": "未记录纸币明细"
 },
 "ရွက်ရေ မမှတ် ထား တဲ့ အထွက်": {
  "en": "Issues without a note breakdown",
  "zh": "无纸币明细的支出"
 },
 "ရွက်ရေ မမှတ် ထား တဲ့ အဝင်": {
  "en": "Receipts without a note breakdown",
  "zh": "无纸币明细的收入"
 },
 "ရွက်ရေ မှ တန်ဖိုး": {
  "en": "Value from note counts",
  "zh": "按张数计算的金额"
 },
 "ရွေးထားတဲ့ ပစ္စည်း နဲ့ ချိတ်လို့ မရဘူး": {
  "en": "Cannot link with the chosen product",
  "zh": "不能和所选商品关联"
 },
 "ရှင်းတာက ": {
  "en": "Settling is done ",
  "zh": "结算是 "
 },
 "ရှင်းတဲ့ ရက်": {
  "en": "Payment date",
  "zh": "付款日期"
 },
 "ရှင်းပြီး": {
  "en": "Settled",
  "zh": "已结算"
 },
 "ရှင်းရန်": {
  "en": "Settlement",
  "zh": "结算"
 },
 "ရှင်းရန် ကိစ္စ": {
  "en": "Settlement matters",
  "zh": "结算事项"
 },
 "ရှင်းရန် ကျန်တဲ့ batch မရှိဘူး": {
  "en": "No batches left to settle",
  "zh": "没有批次要结算了"
 },
 "ရှင်းရန် ကျန်တဲ့ batch များ": {
  "en": "Batches still to settle",
  "zh": "还没结算的批次"
 },
 "ရှင်းရန် စ ပြင်": {
  "en": "Start settling",
  "zh": "开始结算"
 },
 "ရှင်းရန် စာရင်း": {
  "en": "Settlement list",
  "zh": "结算列表"
 },
 "ရှင်းရန် ဆက်ပြင်": {
  "en": "Continue settling",
  "zh": "继续结算"
 },
 "ရှင်းရန် ပို့ပြီးရင် ပြန်ပို့ ထပ်ထည့်လို့ မရဘူး": {
  "en": "After the settlement is sent, returns can't be added",
  "zh": "结算发出去以后就不能再加退货了"
 },
 "ရှင်းရန် မှတ်တမ်းများ": {
  "en": "Settlement records",
  "zh": "结算记录"
 },
 "ရှင်းရန် အသစ်": {
  "en": "New settlement",
  "zh": "新结算"
 },
 "ရှင်းရန် အသေးစိတ်": {
  "en": "Settlement details",
  "zh": "结算明细"
 },
 "ရှာ": {
  "en": "Search",
  "zh": "搜索"
 },
 "ရှာရန် (နာမည်/SKU)": {
  "en": "Search (name/SKU)",
  "zh": "搜索 (名字/SKU)"
 },
 "လ": {
  "en": "Month",
  "zh": "月"
 },
 "လ ချုပ်": {
  "en": "Month summary",
  "zh": "月结"
 },
 "လ ရွေးရန်": {
  "en": "Pick a month",
  "zh": "选月份"
 },
 "လကုန် လက်ကျန်": {
  "en": "Month-end balance",
  "zh": "月末余额"
 },
 "လက် နဲ့ မှတ်တမ်း": {
  "en": "Manual entry",
  "zh": "手工记录"
 },
 "လက်ကား": {
  "en": "Wholesale",
  "zh": "批发"
 },
 "လက်ကား (Wholesale)": {
  "en": "Wholesale",
  "zh": "批发 (Wholesale)"
 },
 "လက်ကား ဆိုင်": {
  "en": "Wholesale shop",
  "zh": "批发店"
 },
 "လက်ကျန်": {
  "en": "On hand",
  "zh": "手头有"
 },
 "လက်ကျန် ငွေ": {
  "en": "Balance",
  "zh": "余额"
 },
 "လက်တွေ့ ရေတွက်": {
  "en": "Counted by hand",
  "zh": "实际数的"
 },
 "လက်တွေ့ ရေတွက် စစ်ဆေး": {
  "en": "Physical stock count",
  "zh": "实地盘点"
 },
 "လက်တွေ့ ရေတွက် ရတဲ့ ငွေသား (Ks)": {
  "en": "Cash actually counted (Ks)",
  "zh": "实际数的现金 (Ks)"
 },
 "လက်ဖက်ရည်ဆိုင်": {
  "en": "Tea shop",
  "zh": "茶馆"
 },
 "လက်ရေးထည့် မှတ်တမ်း": {
  "en": "Hand-written entry",
  "zh": "手写记录"
 },
 "လက်ရေးထည့် မှတ်တမ်း (ဥပမာ — ရင်းနှီးငွေ ထည့်, Owner ထုတ်, အုပ်စု အချင်းချင်း လွှဲ)": {
  "en": "Hand-written entry (e.g. capital put in, Owner takes out, move between groups)",
  "zh": "手写记录 (例如:投入资金、Owner 取出、组之间调动)"
 },
 "လက်ရှိ Actual": {
  "en": "Current Actual",
  "zh": "当前 Actual"
 },
 "လက်ရှိ stock": {
  "en": "Current stock",
  "my": "လက်ကျန်",
  "zh": "当前库存"
 },
 "လက်ရှိ ရွက်ရေ": {
  "en": "Current note breakdown",
  "zh": "当前纸币明细"
 },
 "လက်ရှိ လက်ကား": {
  "en": "Current Wholesale",
  "zh": "当前批发价"
 },
 "လက်ရှိ လက်လီ": {
  "en": "Current Retail",
  "zh": "当前零售价"
 },
 "လက်လီ": {
  "en": "Retail",
  "zh": "零售"
 },
 "လက်လီ (Retail)": {
  "en": "Retail",
  "zh": "零售 (Retail)"
 },
 "လက်လီ ဆိုင်": {
  "en": "Retail shop",
  "zh": "零售店"
 },
 "လက်လီ ဈေး မပြောင်း ပဲ အရင်း {v} Ks ထိ တက် ခံ နိုင် တယ် (အဲ့ အထက် ဆို အမြတ် ကုန်)": {
  "en": "Without changing the retail price, the cost can rise by up to {v} Ks (above that, the profit is gone)",
  "zh": "不改零售价的话,成本最多能涨 {v} Ks (超过就没利润了)"
 },
 "လချုပ် အမြတ်": {
  "en": "Monthly profit",
  "zh": "月度利润"
 },
 "လို": {
  "en": "need",
  "zh": "需要"
 },
 "လိုင်း မရှိလို့ ဖုန်းထဲ သိမ်းထားတယ် — လိုင်း ရရင် ပြန် တင်ပါ": {
  "en": "No connection — saved on this phone; it will be sent when you are back online",
  "zh": "没有网络 — 存在这台手机里;有网络后会发送"
 },
 "လိုင်း မရှိသေးဘူး": {
  "en": "No connection yet",
  "zh": "还没有网络"
 },
 "လုပ်ငန်း ဝင်ငွေ မဟုတ်ဘူး — အိတ် ချင်း ရွှေ့ တာ ပဲ": {
  "en": "Not business income — just a move between pockets",
  "zh": "不是营业收入，只是内部调拨"
 },
 "လုပ်ငန်း အကြောင်း": {
  "en": "About the business",
  "zh": "关于生意"
 },
 "လုပ်ငန်း အခြေအနေ (ခု လက်ရှိ)": {
  "en": "Business position (right now)",
  "zh": "生意状况 (当前)"
 },
 "လုပ်ဆောင်ချက် မှတ်တမ်း — ဘယ်သူ, ဘယ်အချိန်, ဘာ လုပ်သွားလဲ ဆိုတာ တစ်နေရာ တည်း မှာ ကြည့် လို့ ရ တယ် (Owner သာ)": {
  "en": "Activity record — who did what, and when, all in one place (Owner only)",
  "zh": "操作记录 — 谁在什么时候做了什么,都集中在一处看 (只有 Owner)"
 },
 "လုပ်သူ": {
  "en": "Done by",
  "zh": "操作人"
 },
 "လုံလောက် ✓": {
  "en": "Enough ✓",
  "zh": "够了 ✓"
 },
 "လူ": {
  "en": "Person",
  "my": "လူ",
  "zh": "人"
 },
 "လူမရှိ": {
  "en": "Nobody there",
  "zh": "没人"
 },
 "လဲ နှုန်း (Ks)": {
  "en": "Rate (Ks)",
  "zh": "汇率 (Ks)"
 },
 "လျှော့": {
  "en": "Less",
  "zh": "更低"
 },
 "လျှော့ဈေး": {
  "en": "Discount",
  "zh": "折扣"
 },
 "လျှော့ဈေး (Ks)": {
  "en": "Discount (Ks)",
  "zh": "折扣 (Ks)"
 },
 "လျှော့ဈေး စုစုပေါင်း": {
  "en": "Total discount",
  "zh": "折扣总计"
 },
 "လျှော့ဈေး စုစုပေါင်း: {amt} Ks (ပစ္စည်း အတန်း ချင်းက ပေါင်းတာ)": {
  "en": "Total discount: {amt} Ks (added up from the product lines)",
  "zh": "折扣总计:{amt} Ks (从商品行加起来的)"
 },
 "လျှော့ဈေး စုစုပေါင်း: {a} → {b}": {
  "en": "Total discount: {a} → {b}",
  "zh": "折扣总计:{a} → {b}"
 },
 "လွန်ခဲ့တဲ့ {d} ရက်": {
  "en": "{d} days ago",
  "zh": "{d} 天前"
 },
 "လွှဲပြောင်း ဝင်": {
  "en": "Transfer in",
  "zh": "转入"
 },
 "လွှဲပြောင်း ဝင် (Cashbox ဖြည့်)": {
  "en": "Transfer in (cashbox top-up)",
  "zh": "转入（现金箱补款）"
 },
 "လွှဲအပ်ငွေ ထည့်ပါ": {
  "en": "Enter the handed-over amount",
  "zh": "请输入交接的金额"
 },
 "လွှဲအပ်တဲ့ ငွေ (Ks)": {
  "en": "Money handed over (Ks)",
  "zh": "交接的钱 (Ks)"
 },
 "ဝင် အရွက်": {
  "en": "Receipt leaves",
  "zh": "收入张数"
 },
 "ဝယ် မှတ်တမ်း မရှိသေးဘူး": {
  "en": "No purchase record yet",
  "zh": "还没有采购记录"
 },
 "ဝယ် ရမယ့် ပစ္စည်း": {
  "en": "Things to buy",
  "zh": "要买的东西"
 },
 "ဝယ် လို့ ရတဲ့ ဈေး (ပေါက်ဈေး)": {
  "en": "Price it can be bought at (market)",
  "zh": "能买到的价格 (市场价)"
 },
 "ဝယ်ဈေး": {
  "en": "Buy price",
  "zh": "买价"
 },
 "ဝယ်ဈေး (၁ ခု)": {
  "en": "Buy price (each)",
  "zh": "买价 (每个)"
 },
 "ဝယ်ဖူး စုစုပေါင်း": {
  "en": "Total bought",
  "zh": "一共买过"
 },
 "ဝယ်ရန် စာရင်း": {
  "en": "Buy list",
  "zh": "采购清单"
 },
 "ဝယ်ရန် စာရင်း (ဝယ်ရမယ့် စာရင်း အမှန်)": {
  "en": "Buy list (what truly needs buying)",
  "zh": "采购清单 (真正要买的清单)"
 },
 "ဝယ်ရန် စာရင်း ကနေ လုပ်ရန်": {
  "en": "Make from buy list",
  "zh": "从采购清单制作"
 },
 "ဝယ်ရန် စာရင်းက ပေါင်းထည့် ({date})": {
  "en": "Added in from the buy list ({date})",
  "zh": "从采购清单合并加入({date})"
 },
 "ဝယ်ရန် ပစ္စည်း အနည်းဆုံး ၁ မျိုး ရွေးပါ": {
  "en": "Choose at least one product to buy",
  "zh": "至少选一种要买的商品"
 },
 "ဝယ်ရန် လို": {
  "en": "Need to buy",
  "zh": "要买"
 },
 "ဝယ်ရန် လို = ဒီနေ့ လိုအပ် − (stock + မှာထားပြီး − အရင်နေ့ လိုအပ်)": {
  "en": "Need to buy = today need − (stock + already ordered − earlier need)",
  "zh": "要买 = 今天需要 − (库存 + 已订购 − 之前需要)"
 },
 "ဝယ်ရန် လို = ဘောက်ချာ လိုအပ် − လက်ရှိ stock − မှာထားပြီး": {
  "en": "Need to buy = invoice need − current stock − already ordered",
  "zh": "要买 = 发票需要 − 当前库存 − 已订购"
 },
 "သက်တမ်း ကုန်ဖို့ ရက် {n} အတွင်း ကျန် တာ နဲ့ ကုန် သွား ပြီ သား တွေ ကို ပြ ထား တာ။ အသုတ် (batch) တွေ ကို အဝယ် မှတ်တမ်း အတိုင်း လိုက် ထား တာ — ပစ္စည်း ၁ မျိုး ရဲ့ စုစုပေါင်း stock ကို ဘေး မှာ တွဲ ပြထား တယ်။": {
  "en": "Shows goods expiring within {n} days and ones already past. Batches follow the purchase records — the total stock of each product is shown alongside.",
  "zh": "显示 {n} 天内到期和已经过期的货。批次按采购记录跟踪 — 每种商品的库存总数显示在旁边。"
 },
 "သက်တမ်း ကုန်ရက်": {
  "en": "Expiry date",
  "zh": "到期日"
 },
 "သက်တမ်း ကုန်ရက် (ရှိရင်)": {
  "en": "Expiry date (if any)",
  "zh": "到期日 (如有)"
 },
 "သက်တမ်း နီး နေ တဲ့ ပစ္စည်း မရှိဘူး ✅": {
  "en": "Nothing near expiry ✅",
  "zh": "没有快到期的商品 ✅"
 },
 "သက်တမ်း နီးနေတဲ့ ပစ္စည်း {n} မျိုး ရှိတယ်": {
  "en": "{n} product(s) are near expiry",
  "zh": "有 {n} 种商品快到期了"
 },
 "သင့် ပြင်ခွင့်: {used}/{cap} ကြိမ် သုံးပြီး — ဒီ ပြင်တာ က အကြိမ် {n} မြောက် ဖြစ်မယ်။": {
  "en": "Your edits: {used}/{cap} used — this edit will be number {n}.",
  "zh": "你的修改:已用 {used}/{cap} 次 — 这次修改是第 {n} 次。"
 },
 "သတိ — ဒီ ဆိုင်မှာ ငွေ မရသေးတဲ့ ဘောက်ချာ {n} စောင် (ကျန်ငွေ {bal} Ks) ရှိနေတယ်။ ဒီ ဘောက်ချာ က နောက် တစ်စောင် ထပ်တိုးတယ်": {
  "en": "Warning — this shop already has {n} unpaid invoice(s) (balance {bal} Ks). This invoice adds one more.",
  "zh": "注意 — 这家店已经有 {n} 张未付款的发票 (余额 {bal} Ks)。这张发票又加了一张"
 },
 "သတိ — အမျိုးအစား ပေါင်းလဒ် {total} Ks က ပမာဏ {amount} Ks နဲ့ မကိုက်ဘူး (မှတ်လို့တော့ ရတယ်)": {
  "en": "Heads-up — the notes total {total} Ks does not match the amount {amount} Ks (you can still save)",
  "zh": "提醒 — 面额合计 {total} Ks 与金额 {amount} Ks 不符(仍可保存)"
 },
 "သိချင်တာ ကို ခလုတ် နှိပ် မေး — စာရင်း ထဲ က အမှန် တွေ နဲ့ တွက် ဖြေ မယ်။ ဈေးကွက် ဈေး တွေ က ကိုယ် တိုင် ထည့်ထား တာ နဲ့ ပဲ ယှဉ် တယ်။ အဖြေ အားလုံး အကြံ ပြု သက်သက် — ဈေး ပြင် တာ Ko ကိုယ် တိုင် ပဲ။": {
  "en": "Tap a button to ask — answers are worked out from the real records. Market prices are compared only with prices you entered yourself. Every answer is advice only — changing prices is always up to Ko himself.",
  "zh": "点按钮提问 — 用真实记录计算回答。市场价格只和你自己填的比。所有答案都只是建议 — 改价格永远是 Ko 自己决定。"
 },
 "သိပြီ": {
  "en": "Got it",
  "zh": "知道了"
 },
 "သိမ်းပြီးပြီ": {
  "en": "Saved",
  "zh": "已保存"
 },
 "သိမ်းမယ်": {
  "en": "Save",
  "zh": "保存"
 },
 "လုပ်ငန်း အချက်အလက်": {
  "en": "Business profile",
  "zh": "企业资料"
 },
 "အရောင်း ခွဲခြမ်းစိတ်ဖြာ": {
  "en": "Sales analysis",
  "zh": "销售分析"
 },
 "လ ရွေး": {
  "en": "Pick a month",
  "zh": "选择月份"
 },
 "အများဆုံး": {
  "en": "Most",
  "zh": "最多"
 },
 "ပုံမှန်": {
  "en": "Regular",
  "zh": "一般"
 },
 "အနည်းဆုံး": {
  "en": "Least",
  "zh": "最少"
 },
 "ရောင်းအား": {
  "en": "Sales qty",
  "zh": "销量"
 },
 "လုံးဝမဝယ်": {
  "en": "Never bought",
  "zh": "从未购买"
 },
 "ရောင်းမထွက်": {
  "en": "Unsold",
  "zh": "未售出"
 },
 "အရောင်းသွက်": {
  "en": "Fast-selling",
  "zh": "畅销"
 },
 "အရောင်းနှေး": {
  "en": "Slow-moving",
  "zh": "滞销"
 },
 "အမျိုးအစား မသတ်": {
  "en": "Unset type",
  "zh": "未分类"
 },
 "ဝယ်ယူမှု (ကြိမ်)": {
  "en": "Purchases (times)",
  "zh": "购买次数"
 },
 "ဝင်ခဲ့မှု (ကြိမ်)": {
  "en": "Visits (times)",
  "zh": "到访次数"
 },
 "အမြတ် (Ks)": {
  "en": "Profit (Ks)",
  "zh": "利润 (Ks)"
 },
 "ဆိုင်များ — အမျိုးအစား အလိုက် ဝယ်ယူမှု": {
  "en": "Shops by type — purchases",
  "zh": "按类型统计的店铺购买"
 },
 "Outlet များ — ဝင်ခဲ့တဲ့ အကြိမ်ရေ": {
  "en": "Outlets — visit frequency",
  "zh": "门店到访次数"
 },
 "ကုန်ပစ္စည်း — ရောင်းအား (စုစုပေါင်း)": {
  "en": "Products — sales (all time)",
  "zh": "商品销量（累计）"
 },
 "ကုန်ပစ္စည်း — အချိန်ကာလ အလိုက် (ဒီလ)": {
  "en": "Products — this month",
  "zh": "商品（本月）"
 },
 "အမြတ် — ပစ္စည်း အလိုက် (ဒီလ)": {
  "en": "Profit by product (this month)",
  "zh": "按商品统计的利润（本月）"
 },
 "အသုံးစရိတ် — အမျိုးအစား အလိုက် (ဒီလ)": {
  "en": "Expenses by category (this month)",
  "zh": "按类别统计的费用（本月）"
 },
 "ဝယ်ယူမှု စုစုပေါင်း {n} ကြိမ်": {
  "en": "Total purchases {n} times",
  "zh": "购买总计 {n} 次"
 },
 "ဝင်ခဲ့မှု စုစုပေါင်း {n} ကြိမ်": {
  "en": "Total visits {n} times",
  "zh": "到访总计 {n} 次"
 },
 "ရောင်းအား စုစုပေါင်း {n}": {
  "en": "Total sales qty {n}",
  "zh": "销量总计 {n}"
 },
 "အမြတ် စုစုပေါင်း {n} Ks": {
  "en": "Total profit {n} Ks",
  "zh": "利润总计 {n} Ks"
 },
 "အသုံးစရိတ် စုစုပေါင်း {n} Ks": {
  "en": "Total expenses {n} Ks",
  "zh": "费用总计 {n} Ks"
 },
 "snapshot မရှိ လိုင်း {n} ခု — လက်ရှိ actual_base နဲ့ တွက်ထား": {
  "en": "{n} lines without a cost snapshot — computed with the current actual base",
  "zh": "{n} 行无成本快照——按当前实际成本计算"
 },
 "အဆင့် ခွဲပုံ — အစဉ်လိုက် စီ ပြီး အပေါ် ၁/၃ = အများဆုံး / အရောင်းသွက်, အလယ် ၁/၃ = ပုံမှန်, အောက် ၁/၃ = အနည်းဆုံး / အရောင်းနှေး, ၀ = လုံးဝမဝယ် / ရောင်းမထွက်။": {
  "en": "Tier rule — ranked in order: top third = most / fast-selling, middle third = regular, bottom third = least / slow-moving, 0 = never bought / unsold.",
  "zh": "分级规则——按顺序排列：前三分之一=最多/畅销，中间三分之一=一般，后三分之一=最少/滞销，0=从未购买/未售出。"
 },
 "ဝင် ခဲ့ တဲ့ ဆိုင် = ရွေး ထား တဲ့ လ မှာ visit မှတ်တမ်း ရှိ တဲ့ ဆိုင်။ ဝယ်ယူမှု = outlet နဲ့ ချိတ်ထား တဲ့ ဘောက်ချာ (ID သို့မဟုတ် နာမည် + နေရာ တူ)။ ပယ်ဖျက် ဘောက်ချာ မပါ။": {
  "en": "Visited shop = a shop with a visit record in the chosen month. Purchases = invoices linked to the outlet (by ID, or by matching name + area). Cancelled invoices excluded.",
  "zh": "到访店铺=在所选月份有到访记录的店铺。购买=关联到该门店的单据（按 ID，或按名称+地区匹配）。不含已取消的单据。"
 },
 "အော်ဒါဝင် = အော်ဒါ တင် တဲ့ ဝင် ခဲ့ မှု။ ခန့်မှန်းဝင် = Catalog ဖွင့် ပြီး ဆိုင် အနား ရောက် တဲ့ ဝင် ခဲ့ မှု။": {
  "en": "Order visit = a visit where an order was placed. Probable visit = a visit inferred from opening the Catalog near the shop.",
  "zh": "订单到访=下了订单的到访。可能到访=在店铺附近打开 Catalog 而推断的到访。"
 },
 "အော်ဒါဝင်": {
  "en": "Order visits",
  "zh": "订单到访"
 },
 "ခန့်မှန်းဝင်": {
  "en": "Probable visits",
  "zh": "可能到访"
 },
 "လ နဲ့ မဆိုင် — အစ က တည်း က စုစုပေါင်း။ ပယ်ဖျက် ဘောက်ချာ မပါ။ ရောင်းမထွက် = အရောင်း ဖွင့် ထား ပြီး တစ်ခါမှ မရောင်း ရ တဲ့ ပစ္စည်း။": {
  "en": "Not tied to a month — cumulative from the start. Cancelled invoices excluded. Unsold = an open product never sold.",
  "zh": "不限月份——从一开始累计。不含已取消的单据。未售出=开放销售但从未售出的商品。"
 },
 "ရွေး ထား တဲ့ လ အတွက်။ ပယ်ဖျက် ဘောက်ချာ မပါ။ ရောင်းမထွက် = ဒီလ မှာ မရောင်း ရ ပြီး အရောင်း ဖွင့် ထား တဲ့ ပစ္စည်း။": {
  "en": "For the chosen month. Cancelled invoices excluded. Unsold = an open product with no sales this month.",
  "zh": "针对所选月份。不含已取消的单据。未售出=本月无销售的开放商品。"
 },
 "အမြတ် = (ရောင်းဈေး × အရေအတွက် − လိုင်း discount − ဘောက်ချာ discount ခွဲဝေ) − ကုန်ကျစရိတ် × အရေအတွက်။ ကုန်ကျစရိတ် = ရောင်းချိန် Actual, မရှိ ရင် လက်ရှိ Actual။ ပယ်ဖျက် ဘောက်ချာ မပါ။": {
  "en": "Profit = (selling price × qty − line discount − allocated invoice discount) − cost × qty. Cost = Actual at sale time, or the current Actual if none was recorded. Cancelled invoices excluded.",
  "zh": "利润=（售价×数量−行折扣−分摊的单据折扣）−成本×数量。成本=销售时的实际成本，若无记录则按当前实际成本计算。不含已取消的单据。"
 },
 "ရွေး ထား တဲ့ လ ရဲ့ အသုံးစရိတ်, အမျိုးအစား အလိုက်။": {
  "en": "Expenses for the chosen month, by category.",
  "zh": "所选月份的费用，按类别统计。"
 },
 "ဈေး ပြောင်း": {
  "en": "Price change",
  "zh": "价格变动"
 },
 "Stock ညှိ": {
  "en": "Stock adjust",
  "zh": "库存调整"
 },
 "Stock ရေတွက်": {
  "en": "Stock count",
  "zh": "库存盘点"
 },
 "လုပ်ငန်း နာမည်": {
  "en": "Business name",
  "zh": "企业名称"
 },
 "လုပ်ငန်း လိပ်စာ": {
  "en": "Business address",
  "zh": "企业地址"
 },
 "ဖုန်း နံပါတ်များ": {
  "en": "Phone numbers",
  "zh": "电话号码"
 },
 "လုပ်ငန်း အချက်အလက် ပြင်ပြီးပြီ": {
  "en": "Business profile updated",
  "zh": "企业资料已更新"
 },
 "Invoice, Settlement, Order Issue စာရွက်တွေရဲ့ ခေါင်းစဉ်မှာ ပေါ်မယ့် လုပ်ငန်း အချက်အလက်ပါ။ ဒီကနေ ပြင်လိုက်ရင် စာရွက်တိုင်း အသစ် ပေါ်မယ်။": {
  "en": "This is the business info shown on Invoice, Settlement and Order Issue headers. Edit here and every document updates.",
  "zh": "这是显示在发票、结算单和订单发出单抬头上的企业信息。在此编辑后，所有单据都会更新。"
 },
 "ဖုန်း အများ ကြီး ရှိရင် ကော်မာ (,) နဲ့ ခြား ရေးပါ။": {
  "en": "If there are several phones, separate them with commas (,).",
  "zh": "如有多个电话，请用逗号（,）分隔。"
 },
 "သီးခြား အသစ် ဖွင့်ချင်ရင်": {
  "en": "Or open a separate new one",
  "zh": "或者单独新建一个"
 },
 "သူ့ Supplier အလိုက် ခွဲ": {
  "en": "their supplier",
  "zh": "按各自的 Supplier 分开"
 },
 "သေချာ": {
  "en": "Certain",
  "zh": "确定"
 },
 "သွင်းတဲ့ အရေအတွက် ထည့်ပါ": {
  "en": "Enter the received quantity",
  "zh": "请输入收到的数量"
 },
 "သွင်းမယ်": {
  "en": "Record it",
  "zh": "录入"
 },
 "သွင်းမယ် (PUR ဖွင့်မယ်)": {
  "en": "Receive (open PUR)",
  "zh": "收货 (建 PUR)"
 },
 "သွင်းလိုက်တာနဲ့ stock ထဲ ပေါင်းထည့်ပြီး \"စစ်ရန်ကျန်\" ဖြစ်မယ် — Finance/Ko က ဈေး စစ်ပြီး အတည်ပြုပေးမယ်။": {
  "en": "As soon as you record it, the stock is added and it waits as \"to check\" — Finance/Ko check the prices and confirm.",
  "zh": "一录入,库存就加上,并变成“待核对” — Finance/Ko 会核对价格并确认。"
 },
 "သွင်းသူ": {
  "en": "Recorded by",
  "zh": "录入人"
 },
 "အကုန်": {
  "en": "all",
  "zh": "全部"
 },
 "အကုန် ကြည့်မယ်": {
  "en": "View all",
  "zh": "查看全部"
 },
 "အကုန်လုံး (Rep + ရုံး)": {
  "en": "All (reps + office)",
  "zh": "全部 (业务员 + 办公室)"
 },
 "အကောင့် စတင်ချိန်: {since} မှ ယနေ့အထိ · မိမိ အရောင်း အချုပ်သာ ပြထားတယ်": {
  "en": "Account since {since} until today · showing only your own sales summary",
  "zh": "账户从 {since} 到今天 · 只显示你自己的销售汇总"
 },
 "အကောင်း": {
  "en": "Good",
  "zh": "好的"
 },
 "အကောင်း (stock ပြန်ဝင်)": {
  "en": "Good (back into stock)",
  "zh": "好的 (回到库存)"
 },
 "အကျဉ်း ရေး ပါ": {
  "en": "Write a short note",
  "zh": "请写简短备注"
 },
 "အကြောင်း": {
  "en": "Reason",
  "zh": "原因"
 },
 "အကြောင်း (ပျက်/သက်တမ်းကုန်...)": {
  "en": "Reason (damaged/expired...)",
  "zh": "原因 (损坏/过期...)"
 },
 "အကြောင်း (ဥပမာ supplier short)": {
  "en": "Reason (e.g. supplier short)",
  "zh": "原因 (例如 supplier 货不够)"
 },
 "အကြောင်း အရေအတွက်": {
  "en": "No. of entries",
  "zh": "条数"
 },
 "အကြောင်းပြချက်": {
  "en": "Reason",
  "zh": "原因"
 },
 "အကြောင်းပြချက် (မဖြစ်မနေ)": {
  "en": "Reason (required)",
  "zh": "原因 (必填)"
 },
 "အကြောင်းအရင်း": {
  "en": "Reason",
  "zh": "原因"
 },
 "အကြောင်းအရင်း *": {
  "en": "Reason *",
  "zh": "原因 *"
 },
 "အကြောင်းအရာ": {
  "en": "Details",
  "zh": "详情"
 },
 "အကြံပြု နှုန်း": {
  "en": "Suggested rate",
  "zh": "建议比率"
 },
 "အကြွေး ကန့်သတ် (Ks)": {
  "en": "Credit limit (Ks)",
  "zh": "欠款上限 (Ks)"
 },
 "အကြွေး ကန့်သတ်: ဆိုင်ရဲ့ ကျန်ငွေ ဒီ ပမာဏ ရောက်ရင် သတိ ပေးမယ်။ ဗလာ ထားရင် ကန့်သတ် မရှိဘူး။": {
  "en": "Credit limit: when the shop's balance reaches this amount, a warning is shown. Leave blank for no limit.",
  "zh": "欠款上限:店铺余额到这个数时会提醒。留空就是没有上限。"
 },
 "အကြွေး ကျန်": {
  "en": "Credit still out",
  "zh": "欠款未结"
 },
 "အကြွေး အကြာဆုံး ဆိုင်": {
  "en": "Shop owing the longest",
  "zh": "欠得最久的店铺"
 },
 "အကွာအဝေး": {
  "en": "Distance apart",
  "zh": "相距距离"
 },
 "အချက်": {
  "en": "Item",
  "zh": "项"
 },
 "အချက်အလက် မပြည့်စုံဘူး": {
  "en": "Details are incomplete",
  "zh": "资料不完整"
 },
 "အချက်အလက် မပြည့်စုံဘူး (password အနည်းဆုံး ၆ လုံး)": {
  "en": "Details are incomplete (password must be at least 6 characters)",
  "zh": "资料不完整 (密码至少 6 位)"
 },
 "အချိန်": {
  "en": "Time",
  "zh": "时间"
 },
 "အခြား အရေး": {
  "en": "Other spellings",
  "zh": "其他写法"
 },
 "အခြား အရေး ထည့်": {
  "en": "Add another spelling",
  "zh": "添加其他写法"
 },
 "အခြေခံ (အသေး)": {
  "en": "Base (small)",
  "zh": "基础 (小)"
 },
 "အခြေခံ ပစ္စည်း နာမည်": {
  "en": "Base product name",
  "zh": "基础商品名字"
 },
 "အခြေအနေ": {
  "en": "Status",
  "zh": "状态"
 },
 "အခြေအနေ ပြောင်းသွားပြီ": {
  "en": "The status has changed",
  "zh": "状态已经变了"
 },
 "အခွန်": {
  "en": "Tax",
  "zh": "税"
 },
 "အခွန် နှုန်း": {
  "en": "Tax rate",
  "zh": "税率"
 },
 "အခွန် နှုန်း % (Owner ပဲ ပြင်နိုင်)": {
  "en": "Tax rate % (Owner only)",
  "zh": "税率 % (只有 Owner 能改)"
 },
 "အခွန် ပမာဏ (ခန့်မှန်း)": {
  "en": "Tax amount (estimate)",
  "zh": "税额 (估计)"
 },
 "အခွန်မပါ စုစုပေါင်း": {
  "en": "Subtotal (no tax)",
  "zh": "小计 (不含税)"
 },
 "အစ လက်ကျန် ငွေ (Owner ပဲ ပြင်နိုင်)": {
  "en": "Starting balances (Owner only)",
  "zh": "期初余额 (只有 Owner 能改)"
 },
 "အစအဆုံး": {
  "en": "All time",
  "zh": "全部时间"
 },
 "အစီအစဉ်": {
  "en": "plan",
  "zh": "计划"
 },
 "အဆင့်": {
  "en": "Role",
  "zh": "角色"
 },
 "အဆာပြေဆိုင်": {
  "en": "Snack shop",
  "zh": "小吃店"
 },
 "အတည်ပြု + စာရင်း ညှိမယ်": {
  "en": "Confirm + adjust the book",
  "zh": "确认 + 调账"
 },
 "အတည်ပြု ပြီးရင် ပြန် ပြင်လို့ မရ တော့ဘူး။": {
  "en": "After confirming, it cannot be changed.",
  "zh": "确认以后就不能再改了。"
 },
 "အတည်ပြု ရင် ကွာ တဲ့ အရေအတွက် တွေ ကို stock စာရင်း မှာ ညှိ မယ်။ သေချာလား?": {
  "en": "Confirming will adjust the stock book by the differing amounts. Are you sure?",
  "zh": "确认后会按差额调整库存账。确定吗?"
 },
 "အတည်ပြု သူ": {
  "en": "Verifier",
  "zh": "复核人"
 },
 "အတည်ပြုပြီး": {
  "en": "Confirmed",
  "zh": "已确认"
 },
 "အတည်ပြုပြီး ငွေ": {
  "en": "Confirmed money",
  "zh": "已确认的钱"
 },
 "အတည်ပြုပြီးပြီ — ကွာတဲ့ {n} မျိုးကို စာရင်း ညှိပြီးပြီ": {
  "en": "Confirmed — {n} differing item(s) adjusted in the book",
  "zh": "已确认 — {n} 种有差异的已在账上调整"
 },
 "အတည်ပြုပြီးရင် \"စစ်ရန်ကျန်\" အဖြစ် ပြန် ပြောင်းလို့ မရတော့ပါဘူး။": {
  "en": "Once confirmed, it cannot be changed back to \"to check\".",
  "zh": "确认后不能再改回“待核对”。"
 },
 "အတည်ပြုမယ်": {
  "en": "Confirm",
  "zh": "确认"
 },
 "အတွဲ (၁၀၀ ရွက်)": {
  "en": "Bundles (100 leaves)",
  "zh": "捆（100 张）"
 },
 "အထိ": {
  "en": "To",
  "zh": "到"
 },
 "အထုပ် (အကြီး)": {
  "en": "Pack (big)",
  "zh": "包装 (大)"
 },
 "အထုပ် ချိတ်ဆက် — stock ကို {sku} ထဲ ပေါင်း": {
  "en": "Pack linked — stock moved into {sku}",
  "zh": "包装已关联 — 库存并入 {sku}"
 },
 "အထုပ် ချိတ်ဆက် — {sku} {q} × {f} ပေါင်း": {
  "en": "Pack linked — added {sku} {q} × {f}",
  "zh": "包装已关联 — 加入 {sku} {q} × {f}"
 },
 "အထုပ် ပစ္စည်း နာမည်": {
  "en": "Pack product name",
  "zh": "包装商品名字"
 },
 "အထောက်အထား": {
  "en": "Based on",
  "zh": "依据"
 },
 "အထွက်": {
  "en": "Out",
  "zh": "出"
 },
 "အနည်းဆုံး ၁ မျိုး ပို့တဲ့ အရေအတွက် ထည့်ပါ": {
  "en": "Enter a delivered amount for at least one product",
  "zh": "至少填一种商品的送达数量"
 },
 "အနီး မှာ \"{name}\" ရှိ တယ် — ဒီ ဆိုင် ပဲ လား?": {
  "en": "\"{name}\" is nearby — is it this one?",
  "zh": "\"{name}\"就在附近 — 是这家吗?"
 },
 "အနီးဆုံး သက်တမ်း ကုန်ရက်": {
  "en": "Nearest expiry date",
  "zh": "最近的到期日"
 },
 "အန္တရာယ် စာရင်း": {
  "en": "At-risk list",
  "zh": "风险清单"
 },
 "အန္တရာယ် ပစ္စည်းများ": {
  "en": "Products at risk",
  "zh": "有风险的商品"
 },
 "အပိတ် (စာရင်း)": {
  "en": "Book closing",
  "zh": "账面结余"
 },
 "အပို ကုန်ကျ (ကားခ စသည်)": {
  "en": "Extra cost (transport etc.)",
  "zh": "额外费用 (车费等)"
 },
 "အပို ကုန်ကျ က ဘာအတွက် လဲ": {
  "en": "What was the extra cost for?",
  "zh": "额外费用是做什么的?"
 },
 "အပို ကုန်ကျ ခွဲတမ်း": {
  "en": "Share of extra cost",
  "zh": "额外费用分摊"
 },
 "အပို ကုန်ကျ ထည့်/ပြင်": {
  "en": "Add / correct the extra cost",
  "zh": "添加/修改额外费用"
 },
 "အပို ကုန်ကျ မှတ်ပြီးပြီ": {
  "en": "Extra cost recorded",
  "zh": "额外费用已记录"
 },
 "အပြည့် ရပြီး": {
  "en": "Fully received",
  "zh": "已全部收到"
 },
 "အပြည့် ရရှိ": {
  "en": "Paid in full",
  "zh": "已付清"
 },
 "အပြည့် အရေအတွက်က လက်ကျန်ထက် များနေတယ်": {
  "en": "The full-pack count is more than the stock on hand",
  "zh": "整包数量比手头库存多"
 },
 "အပြည့် အရေအတွက်ကို ဂဏန်း အပြည့် ထည့်ပါ": {
  "en": "Enter the full-pack count as a whole number",
  "zh": "整包数量请填整数"
 },
 "အပြောင်းအလဲ": {
  "en": "Change",
  "zh": "变化"
 },
 "အဖွင့်": {
  "en": "Opening",
  "zh": "期初"
 },
 "အဖွင့် ငွေ (Ks)": {
  "en": "Opening cash (Ks)",
  "zh": "期初现金（缅元）"
 },
 "အဖွင့် ရွက်ရေ မသိ": {
  "en": "Opening leaves unknown",
  "zh": "期初张数未知"
 },
 "အဖွင့် အရင် ထည့်ပါ": {
  "en": "Enter the opening first",
  "zh": "请先录入期初"
 },
 "အဖွင့် အရွက်": {
  "en": "Opening leaves",
  "zh": "期初张数"
 },
 "အဖွဲ့လိုက် စုစုပေါင်း (ဘောက်ချာ {c} စောင်)": {
  "en": "Team total ({c} invoices)",
  "zh": "按团队总计 ({c} 张发票)"
 },
 "အဖွဲ့လိုက် ဒီနေ့ အရောင်း (ဘောက်ချာ {c} စောင်)": {
  "en": "Team sales today ({c} invoices)",
  "zh": "团队今天的销售 ({c} 张发票)"
 },
 "အဖွဲ့လိုက် ဒီလ အရောင်း (ဘောက်ချာ {c} စောင်)": {
  "en": "Team sales this month ({c} invoices)",
  "zh": "团队本月的销售 ({c} 张发票)"
 },
 "အဖွဲ့လိုက် နောက်ဆုံး Orders": {
  "en": "Latest team orders",
  "my": "အဖွဲ့လိုက် နောက်ဆုံး ဘောက်ချာများ",
  "zh": "团队最新订单"
 },
 "အမည် မသိသေး": {
  "en": "Name not known yet",
  "zh": "还不知道名字"
 },
 "အများဆုံး သုံးတာ: {c} — {v} Ks": {
  "en": "Biggest spending: {c} — {v} Ks",
  "zh": "花得最多:{c} — {v} Ks"
 },
 "အမျိုးအစား": {
  "en": "Category",
  "zh": "类别"
 },
 "အမျိုးအစား ခွဲ (၁၀၀ ရွက် တစ်တွဲ)": {
  "en": "Denomination split (100 leaves a bundle)",
  "zh": "面额拆分（100 张一捆）"
 },
 "အမျိုးအစား ပေါင်းလဒ်": {
  "en": "Notes total",
  "zh": "面额合计"
 },
 "အမျိုးအစား ပေါင်းလဒ် က ကောက်ငွေ နဲ့ {v} Ks ကွာ တယ်": {
  "en": "The notes total differs from the collected amount by {v} Ks",
  "zh": "面额合计与收款金额相差 {v} Ks"
 },
 "အမျိုးအစား ပေါင်းလဒ် ပမာဏ နဲ့ ကိုက်မှ သိမ်း လို့ ရမယ်": {
  "en": "The note breakdown total must match the amount exactly before it can be saved",
  "zh": "纸币明细合计必须与金额完全一致方可保存"
 },
 "အမျိုးအစား ပေါင်းလဒ် ပမာဏ နဲ့ ကိုက်မှ သိမ်း လို့ ရမယ် — မကိုက် ရင် မသိမ်း ဘူး။ ငွေ ဂဏန်း ကို ဒါ က မပြောင်း ဘူး။": {
  "en": "The note breakdown total must match the amount before it can be saved — if it does not match, nothing is saved. This does not change the money figure.",
  "zh": "纸币明细合计必须与金额一致方可保存，不一致则不保存。此操作不会改变金额。"
 },
 "အမျိုးအစား ဖြည့်ထားရင် အဲ့ဒီ ပေါင်းလဒ်ကို ရေတွက်ငွေ အဖြစ် မှတ်မယ် — အောက်က ပမာဏ အကွက်ကို မသုံးဘူး။": {
  "en": "If the notes above are filled in, their total is saved as the counted cash — the amount box below is not used.",
  "zh": "如填写了上面的面额,将以面额合计作为点算金额 — 不使用下面的金额框。"
 },
 "အမျိုးအစား ဖြည့်ထားရင် အဲ့ဒီ ပေါင်းလဒ်ကို ရေတွက်ငွေ အဖြစ် မှတ်မယ်။ ကွာခြားချက် ရှိ ရင် အကြောင်း ရေး မှ ပိတ် လို့ ရမယ် — ကွာ တာ က ပိတ် တာ ကို တား မထားဘူး။": {
  "en": "If the counter is filled, its total is the counted cash. A difference needs a reason before closing — but a difference never blocks closing.",
  "zh": "如填写了点钞器，其合计即为实点金额。有差额时须注明原因方可结账，但差额本身不阻止结账。"
 },
 "အမျိုးအစား ဖြည့်ထားရင် အဲ့ဒီ ပေါင်းလဒ်ကို အဖွင့် အဖြစ် မှတ်မယ်။": {
  "en": "If the counter is filled, its total is the opening.",
  "zh": "如填写了点钞器，其合计即为期初。"
 },
 "အမျိုးအစား မသတ်မှတ်ရသေးတဲ့ ဆိုင် နဲ့ ဆိုင် မှတ်တမ်း နဲ့ မချိတ်မိတဲ့ ဘောက်ချာတွေကို \"မသတ်မှတ်ရသေး\" ထဲ ထည့်တွက်တယ် · ပယ်ဖျက် ဘောက်ချာများ မပါဝင်ပါ": {
  "en": "Invoices for shops whose type is not set yet, and invoices not matched to a shop record, are counted under \"Not set yet\" · Cancelled invoices are not included",
  "zh": "尚未设置类型的店铺的发票,以及未匹配到店铺记录的发票,计入\"尚未设置\" · 不含已取消的发票"
 },
 "အမြတ်": {
  "en": "Profit",
  "zh": "利润"
 },
 "အမြတ် % (လက်ကား)": {
  "en": "Margin % (Wholesale)",
  "zh": "利润率 % (批发)"
 },
 "အမြတ် % (လက်လီ)": {
  "en": "Margin % (Retail)",
  "zh": "利润率 % (零售)"
 },
 "အမြတ် (Margin · Base)": {
  "en": "Profit (Margin · Base)",
  "zh": "利润 (Margin · Base)"
 },
 "အမြတ် (Margin)": {
  "en": "Profit (Margin)",
  "zh": "利润 (Margin)"
 },
 "အမြတ် ကြမ်း: {v} Ks": {
  "en": "Rough profit: {v} Ks",
  "zh": "大约利润:{v} Ks"
 },
 "အမြတ် ထိန်း (ပေါက်ဈေး အရင်း နဲ့) — {p} Ks": {
  "en": "Keep the profit (at market cost) — {p} Ks",
  "zh": "保利润 (按市场成本) — {p} Ks"
 },
 "အမှတ်/မှတ်ချက်": {
  "en": "Ref/note",
  "zh": "编号/备注"
 },
 "အမှာ": {
  "en": "Request",
  "zh": "订购"
 },
 "အမှာ (Demand)": {
  "en": "Requests (Demand)",
  "zh": "订购单 (Demand)"
 },
 "အမှာ (Order Requests)": {
  "en": "Requests (Order Requests)",
  "my": "အမှာ",
  "zh": "订购单 (Order Requests)"
 },
 "အမှာ စာရင်း": {
  "en": "Order request list",
  "zh": "订购单列表"
 },
 "အမှာ စာရင်း (Demand)": {
  "en": "Request list (Demand)",
  "zh": "订购单列表 (Demand)"
 },
 "အမှာ စာရင်း (Order Requests)": {
  "en": "Request list (Order Requests)",
  "my": "အမှာ စာရင်း",
  "zh": "订购单列表 (Order Requests)"
 },
 "အမှာ စာရင်း အကုန် ကြည့်": {
  "en": "See all requests",
  "zh": "查看全部订购单"
 },
 "အမှာ ဆိုတာ supplier ဆီ မှာထားတဲ့ ": {
  "en": "A request is just a ",
  "zh": "订购单就是向 supplier 订货的 "
 },
 "အမှာ တင်မယ်": {
  "en": "Submit request",
  "zh": "提交订购单"
 },
 "အမှာ တစ်ခုလုံး ပယ်ဖျက်မလား?": {
  "en": "Cancel the whole request?",
  "zh": "要取消整张订购单吗?"
 },
 "အမှာ ပယ်ဖျက်": {
  "en": "Cancel request",
  "zh": "取消订购单"
 },
 "အမှာ ပယ်ဖျက်ပြီးပြီ — ရောက်ပြီးသား အပိုင်း PUR အဖြစ် ကျန်နေမယ်": {
  "en": "Order request cancelled — the part that already arrived stays as a purchase (PUR)",
  "zh": "订购单已取消 — 已经到的部分留作采购 (PUR)"
 },
 "အမှာ ပိတ်ပြီးပြီ — ကျန်တဲ့ အရေအတွက် ထပ် မစောင့်တော့ဘူး": {
  "en": "Order request closed — the remaining quantity won't be waited for anymore",
  "zh": "订购单已关闭 — 剩余数量不再等了"
 },
 "အမှာ ပြင်ပြီးပြီ": {
  "en": "Order request updated",
  "zh": "订购单已更新"
 },
 "အမှာ မရှိသေးဘူး": {
  "en": "No requests yet",
  "zh": "还没有订购单"
 },
 "အမှာ ရွေးရန်": {
  "en": "Choose an order",
  "zh": "选择订单"
 },
 "အမှာ အမျိုးအစား": {
  "en": "Request kind",
  "zh": "订购单类型"
 },
 "အမှာ အသစ်": {
  "en": "New request",
  "zh": "新订购单"
 },
 "အမှာ အသစ် တင်ရန်": {
  "en": "Raise a new request",
  "zh": "建一张新订购单"
 },
 "အမှာ အသစ်က ပေါင်းထည့် ({date})": {
  "en": "Added in from a new-order form ({date})",
  "zh": "从新订单表单合并加入({date})"
 },
 "အမှာ အသေးစိတ်": {
  "en": "Order request details",
  "zh": "订购单明细"
 },
 "အမှာ — Demand Driven": {
  "en": "Request — Demand Driven",
  "zh": "订购单 — 需求驱动"
 },
 "အရင် ကျန်": {
  "en": "Left over from before",
  "zh": "之前剩下的"
 },
 "အရင် နေ့ အပိတ် ကနေ အလို ဆက်": {
  "en": "Carried from the previous closing",
  "zh": "由上一日结余自动带入"
 },
 "အရင် ပစ္စည်း ရွေးပါ": {
  "en": "Pick a product first",
  "zh": "先选商品"
 },
 "အရင် ရေတွက် မှတ်တမ်း (ဖတ် လို့ ပဲ ရ)": {
  "en": "Earlier count records (read-only)",
  "zh": "以往点钞记录（只读）"
 },
 "အရင် လုံးပေါင်း လျှော့ဈေး (Ks)": {
  "en": "Old whole-invoice discount (Ks)",
  "zh": "以前的整单折扣 (Ks)"
 },
 "အရင်နေ့ ကျန် လိုအပ်": {
  "en": "Earlier need left",
  "zh": "之前剩下的需求"
 },
 "အရင်း": {
  "en": "Cost",
  "zh": "成本"
 },
 "အရင်း (Actual)": {
  "en": "Cost (Actual)",
  "zh": "成本 (Actual)"
 },
 "အရင်း (Base)": {
  "en": "Cost (Base)",
  "zh": "成本 (Base)"
 },
 "အရင်း (Base): {v} Ks — ကြား ကာကွယ် ကျန် {b} Ks ({p}%)": {
  "en": "Cost (Base): {v} Ks — buffer left in between {b} Ks ({p}%)",
  "zh": "成本 (Base):{v} Ks — 中间还剩缓冲 {b} Ks ({p}%)"
 },
 "အရင်း တက် ရင် ဘယ် လောက် တောင့်ခံ နိုင် လဲ": {
  "en": "If the cost rises, how much can it take?",
  "zh": "成本涨了能撑多少?"
 },
 "အရင်း ပြောင်းတဲ့ ရက်": {
  "en": "Cost changed on",
  "zh": "成本变更日期"
 },
 "အရင်း အမှန် မထည့်ရသေးဘူး — ဖြေ လို့ မရဘူး": {
  "en": "The true cost is not entered yet — cannot answer",
  "zh": "真实成本还没填 — 不能回答"
 },
 "အရေအတွက်": {
  "en": "Qty",
  "zh": "数量"
 },
 "အရေအတွက် တွေ အားလုံး အ သေး ဆုံး ယူနစ် (ဥပမာ အလိပ်) နဲ့ ရေးပါ။ ထုပ်ကြီး နဲ့ အလိပ် က မိသားစု ၁ ခု ပဲ — တစ် ကြိမ် ပဲ ရေး။": {
  "en": "Write every amount in the smallest unit (e.g. pieces). A big pack and its pieces are one family — count it once.",
  "zh": "所有数量都用最小单位写 (例如:个)。大包装和它的散件是一个家族 — 只数一次。"
 },
 "အရေအတွက် ပြင် ပို့ရန်": {
  "en": "Deliver with amounts",
  "zh": "按数量送货"
 },
 "အရောင်း": {
  "en": "Sales",
  "zh": "销售"
 },
 "အရောင်း (ပို့ပြီး {c} စောင်)": {
  "en": "Sales ({c} delivered)",
  "zh": "销售 (已送达 {c} 张)"
 },
 "အရောင်း/အရင်း ကိန်းတွေက ": {
  "en": "Sales/cost figures count ",
  "zh": "销售/成本数字按 "
 },
 "အရေး ထည့်ပါ": {
  "en": "Type a spelling first",
  "zh": "请先填写写法"
 },
 "အရွက်": {
  "en": "Leaves",
  "zh": "张数"
 },
 "အရွယ်": {
  "en": "Size",
  "zh": "大小"
 },
 "အလို ဝင်": {
  "en": "Auto-fed",
  "zh": "自动记入"
 },
 "အလှကုန်": {
  "en": "Cosmetics",
  "zh": "化妆品"
 },
 "အလှကုန်လက်ကား": {
  "en": "Cosmetics wholesale",
  "zh": "化妆品批发"
 },
 "အဝင်": {
  "en": "In",
  "zh": "进"
 },
 "အဝင် / အထွက်": {
  "en": "In / Out",
  "zh": "进 / 出"
 },
 "အဝယ်": {
  "en": "Purchases",
  "zh": "采购"
 },
 "အဝယ် ကိစ္စ": {
  "en": "Purchase matters",
  "zh": "采购事项"
 },
 "အဝယ် စစ်ရန် (စစ်ရန်ကျန် {n} ခု)": {
  "en": "Purchases to check ({n} waiting)",
  "zh": "待核对的采购 (还有 {n} 张)"
 },
 "အဝယ် စစ်ရန် ကျန်": {
  "en": "Purchases to check",
  "zh": "待核对的采购"
 },
 "အဝယ် စစ်ရန်: {c} ခု": {
  "en": "Purchases to check: {c}",
  "zh": "待核对采购:{c} 张"
 },
 "အဝယ် စာရင်း": {
  "en": "Purchase list",
  "zh": "采购列表"
 },
 "အဝယ် တိုက်ရိုက် သွင်း": {
  "en": "Enter purchase directly",
  "zh": "直接录入采购"
 },
 "အဝယ် ပယ်ဖျက် (မှား သွင်းမိရင်)": {
  "en": "Cancel purchase (if recorded by mistake)",
  "zh": "取消采购 (录错了的话)"
 },
 "အဝယ် ပယ်ဖျက်ပြီးပြီ — stock ပြန် နှုတ်ပြီးပြီ": {
  "en": "Purchase cancelled — stock taken back out",
  "zh": "采购已取消 — 库存已扣回"
 },
 "အဝယ် မှတ်တမ်း": {
  "en": "Purchase records",
  "zh": "采购记录"
 },
 "အဝယ် သွင်း": {
  "en": "Enter purchase",
  "zh": "录入采购"
 },
 "အဝယ် သွင်းရန် (ဂိုထောင်)": {
  "en": "Record a purchase (warehouse)",
  "zh": "录入采购 (仓库)"
 },
 "အဝယ် အပို ကုန်ကျ": {
  "en": "Extra buying cost",
  "zh": "采购额外费用"
 },
 "အဝယ် အပို ကုန်ကျ (ဒီလ)": {
  "en": "Extra buying cost (this month)",
  "zh": "采购额外费用 (本月)"
 },
 "အဝယ် အသစ်": {
  "en": "New purchase",
  "zh": "新采购"
 },
 "အဝယ် အားလုံး ကြည့်ရန်": {
  "en": "View all purchases",
  "zh": "查看全部采购"
 },
 "အဝယ်များ (PUR)": {
  "en": "Purchases (PUR)",
  "zh": "采购 (PUR)"
 },
 "အသစ်": {
  "en": "New",
  "zh": "新的"
 },
 "အသစ် (၆ လုံး+)": {
  "en": "New (6+ characters)",
  "zh": "新的 (6 位以上)"
 },
 "အသစ် ဖွင့်": {
  "en": "Open a new one",
  "zh": "新建一个"
 },
 "အသားတင် ရောင်းဈေး (၁ ခု)": {
  "en": "Net selling price (each)",
  "zh": "净售价 (每个)"
 },
 "အသိပေးချက်များ": {
  "en": "Notifications",
  "zh": "通知"
 },
 "အသုတ် များ": {
  "en": "Batches",
  "zh": "批次"
 },
 "အသုံးစရိတ်": {
  "en": "Expenses",
  "zh": "开支"
 },
 "အသုံးစရိတ် (ဒီလ)": {
  "en": "Expenses (this month)",
  "zh": "开支 (本月)"
 },
 "အသုံးစရိတ် ကိစ္စ": {
  "en": "Expense matters",
  "zh": "开支事项"
 },
 "အသုံးစရိတ် ပြင်": {
  "en": "Edit expense",
  "zh": "编辑开支"
 },
 "အသုံးစရိတ် မှတ်ပြီ 💸": {
  "en": "Expense saved 💸",
  "zh": "开支已保存 💸"
 },
 "အသုံးစရိတ် အသစ်": {
  "en": "New expense",
  "zh": "新开支"
 },
 "အသုံးပြုသူ ပြင်": {
  "en": "Edit user",
  "zh": "编辑用户"
 },
 "အသုံးပြုသူများ": {
  "en": "Users",
  "zh": "用户"
 },
 "အသေးစိတ် / မှတ်ရန်": {
  "en": "Details / record",
  "zh": "明细 / 记录"
 },
 "အသေးစိတ် မှတ်တမ်း ({n})": {
  "en": "Detail records ({n})",
  "zh": "明细记录 ({n})"
 },
 "အသံ နီး တဲ့ ဆိုင်": {
  "en": "Shops with a similar sound",
  "zh": "发音相近的店铺"
 },
 "အအေးဆိုင်": {
  "en": "Cold drinks shop",
  "zh": "冷饮店"
 },
 "အားလုံး": {
  "en": "All",
  "my": "အားလုံး",
  "zh": "全部"
 },
 "အုပ်စု": {
  "en": "Group",
  "zh": "组别"
 },
 "အုပ်စု တစ်ခု ကနေ နောက် တစ်ခု ကို လွှဲ ရင်: ဒီ မှာ အထွက် ၁ ကြောင်း + လွှဲ သွား တဲ့ အုပ်စု မှာ အဝင် ၁ ကြောင်း — ၂ ကြောင်း မှတ်ပါ။": {
  "en": "To move money from one group to another: record one OUT line here plus one IN line in the group it went to — 2 lines.",
  "zh": "把钱从一个组转到另一个组:在这里记一行支出 + 在转到的组记一行收入 — 一共 2 行。"
 },
 "အဲဒီ Username ကို တခြားသူ သုံးနေတယ် — မပြောင်းဘူး": {
  "en": "Another user already uses that username — not changed",
  "zh": "这个用户名已经被别人用了 — 没有更改"
 },
 "အဲဒီနေ့ အဲဆိုင် အရောင်း": {
  "en": "That day, that shop — sales",
  "zh": "那天、那家店 — 销售"
 },
 "အဲ့ဒီ နာမည် နဲ့ ဆိုင် ရှိနှင့်ပြီးသား — ပေါင်းချင်ရင် Merge ကို သုံးပါ": {
  "en": "A shop with that name already exists — use Merge to combine them",
  "zh": "同名的店铺已经有了 — 想合并就用 Merge"
 },
 "အဲ့ဒီ အမှာ က ပေါင်းလို့ မရတော့ဘူး — ပြန် စမ်းပါ": {
  "en": "That order can no longer take additions — please try again",
  "zh": "那个订单已经不能再合并了 — 请重试"
 },
 "အဲ့ဒီ အရေး က အခြား ဆိုင် မှာ မှတ်ပြီး သား — ထည့်လို့ မရဘူး": {
  "en": "That spelling is already recorded for another shop — it cannot be added",
  "zh": "这个写法已经登记在另一家店了 — 不能添加"
 },
 "အဲ့ဒီ အရေး ဒီ ဆိုင် မှာ မှတ်ပြီး သား": {
  "en": "That spelling is already recorded for this shop",
  "zh": "这个写法已经登记在这家店了"
 },
 "ဥပမာ 20": {
  "en": "e.g. 20",
  "zh": "例如 20"
 },
 "ဥပမာ T-1, T-2, Online": {
  "en": "e.g. T-1, T-2, Online",
  "zh": "例如 T-1, T-2, Online"
 },
 "ဥပမာ ဆီ ဖိုး": {
  "en": "e.g. fuel",
  "zh": "例如 油费"
 },
 "ဥပမာ — Sut": {
  "en": "e.g. Sut",
  "zh": "例如 — Sut"
 },
 "ဥပမာ — ငွေသား ကနေ ဘဏ် ကို လွှဲ": {
  "en": "e.g. moved from cash to bank",
  "zh": "例如 — 从现金转到银行"
 },
 "ဥပမာ — ဆိုင် နာမည် / ဖုန်း ပို့စ်": {
  "en": "e.g. shop name / phone post",
  "zh": "例如 — 店名 / 电话帖子"
 },
 "ဥပမာ — ဈေး နား": {
  "en": "e.g. near the market",
  "zh": "例如 — 市场旁"
 },
 "ဥပမာ — ဗူးပိန်, သက်တမ်းကုန်, ဆိုင်က ပြန်အပ်": {
  "en": "e.g. dented can, expired, returned by shop",
  "zh": "例如 — 罐头压扁、过期、店铺退回"
 },
 "ဥပမာ — ဘဏ် အပ် ငွေ / ပိုင်ရှင် ထုတ် ငွေ": {
  "en": "E.g. — bank deposit / owner draw",
  "zh": "例如：存入银行 / 业主提款"
 },
 "ဥပမာ — ရန်ကုန် ကားခ": {
  "en": "e.g. — Yangon transport",
  "zh": "例如 — 仰光车费"
 },
 "ဥပမာ: ဆိုင် တစ်ဝက် ပေး": {
  "en": "e.g. shop paid half",
  "zh": "例如:店铺付了一半"
 },
 "၁ · ဝယ်ရန်": {
  "en": "1 · To buy",
  "zh": "1 · 要买"
 },
 "၁ နှစ် အတွင်း": {
  "en": "Last 1 year",
  "zh": "近 1 年"
 },
 "၂ · ရှင်းရန်": {
  "en": "2 · To settle",
  "zh": "2 · 要结算"
 },
 "၃ လ အတွင်း": {
  "en": "Last 3 months",
  "zh": "近 3 个月"
 },
 "၆ လ အတွင်း": {
  "en": "Last 6 months",
  "zh": "近 6 个月"
 },
 "၉ လ အတွင်း": {
  "en": "Last 9 months",
  "zh": "近 9 个月"
 },
 "။ ပစ္စည်းတွေကို ": {
  "en": ". Items are grouped by ",
  "zh": "。商品按 "
 },
 "— Supplier တစ်ခု ပိတ်လိုက်ရင် သူ့ ပစ္စည်း အကုန် Catalog က ပျောက်မယ်။ ပစ္စည်းချင်း ရောင်းဖွင့်/ပိတ် setting ကို မထိဘူး — ပြန် ဖွင့်ရင် မူလ အတိုင်း ပြန်ပေါ်မယ်။": {
  "en": "— Closing a supplier hides all its products from the Catalog. It does not change each product's own sell on/off setting — reopen and they come back as before.",
  "zh": " — 关闭一个 Supplier,它的商品会全部从 Catalog 消失。不会动每个商品自己的销售开关设置 — 重新开启就会恢复原样。"
 },
 "— မရှိ —": {
  "en": "— None —",
  "zh": "— 无 —"
 },
 "— အကြာဆုံး: {d} ကတည်းက ({n} ရက်)": {
  "en": "— oldest: since {d} ({n} days)",
  "zh": " — 最久的:从 {d} 起 ({n} 天)"
 },
 "— အဆင့် ၂ ဆင့် ပဲ": {
  "en": "— just 2 steps",
  "zh": " — 只有 2 步"
 },
 "↩ မှတ်တာ ပြန်ဖျက်": {
  "en": "↩ Undo the mark",
  "zh": "↩ 撤销标记"
 },
 "⏳ စစ်ရန်": {
  "en": "⏳ To check",
  "zh": "⏳ 待核对"
 },
 "⏳ မပို့ရသေး (ဖုန်းထဲ စောင့်နေတာ)": {
  "en": "⏳ Not sent yet (waiting on this phone)",
  "zh": "⏳ 还没发送 (在这台手机上等着)"
 },
 "⏳ သက်တမ်း နီးနေတာ": {
  "en": "⏳ Near expiry",
  "zh": "⏳ 快到期"
 },
 "▲ ဖွင့်ရန်": {
  "en": "▲ Open",
  "zh": "▲ 展开"
 },
 "▼ ပိတ်ရန်": {
  "en": "▼ Close",
  "zh": "▼ 收起"
 },
 "★ Actual Base (အရင်းအမှန်) ကို Owner နဲ့ Finance ပဲ မြင်ရတယ်။": {
  "en": "★ Only Owner and Finance can see Actual Base (true cost).",
  "zh": "★ Actual Base (真实成本) 只有 Owner 和 Finance 能看。"
 },
 "⚠️ Actual ဈေး မဖြည့်ရသေးတဲ့ ပစ္စည်း: {list} — Finance/Owner ဖြည့်ပြီးမှ ရှင်းရန် ပို့လို့ရမယ်": {
  "en": "⚠️ Products with no Actual price yet: {list} — Finance/Owner must fill them before this can be sent",
  "zh": "⚠️ 还没填 Actual 价格的商品:{list} — Finance/Owner 填了以后才能发送结算"
 },
 "⚠️ Actual ဈေး လို ({n} မျိုး)": {
  "en": "⚠️ Actual price needed ({n} items)",
  "zh": "⚠️ 需要 Actual 价格 ({n} 种)"
 },
 "⚠️ စစ်ရန်ကျန် PUR {n} ခု ကျန်သေးတယ် — Finance စစ် ပြီးမှ ရှင်းရန် ပို့လို့ရမယ် (အဲဒီ ပမာဏတွေ တွက်ချက်ထဲ မပါသေးဘူး)": {
  "en": "⚠️ {n} PURs are still unchecked — Finance must check them before this can be sent (their amounts are not counted yet)",
  "zh": "⚠️ 还有 {n} 张 PUR 没核对 — Finance 核对后才能发送结算 (那些金额还没算进来)"
 },
 "⚠️ စစ်ရန်ကျန် PUR ရှိသေး": {
  "en": "⚠️ A PUR is still unchecked",
  "zh": "⚠️ 还有 PUR 没核对"
 },
 "⚠️ ဒီ ပစ္စည်းတွေမှာ Supplier မှတ်ထားတာ မရှိသေးဘူး — ဒီတစ်ခေါက် ဘယ်သူ့ဆီက ဝယ်မလဲ ရိုက်ထည့်ပေးပါ (အမြဲ ဝယ်နေကျ သူ ဆိုရင် Products မှာ သူ့ Supplier ကိုလည်း ပြင် မှတ်ထားပါ)။": {
  "en": "⚠️ These products have no supplier recorded yet — type who you are buying from this time (if it is your regular supplier, also set it on the product in Products).",
  "zh": "⚠️ 这些商品还没记 Supplier — 这次向谁买请填一下 (如果是常买的那家,也在 Products 里把它的 Supplier 记上)。"
 },
 "⚠️ ဒီ ရက် မတိုင်ခင် မပို့ရသေး ဘောက်ချာ {n} စောင် ကျန်နေတယ်": {
  "en": "⚠️ {n} undelivered invoices from before this day are still open",
  "zh": "⚠️ 这天之前还有 {n} 张未送达的发票没结"
 },
 "⚠️ ဒီ လရဲ့ ပစ္စည်း အတန်း {n} တန်း (စုစုပေါင်း {total} ထဲ) က ရောင်းချိန် Base ဈေး မှတ်ထားတာ မရှိသေးလို့ လက်ရှိ Base နဲ့ တွက်ထားတယ် — Base အမြတ် ကိန်း နည်းနည်း ခန့်မှန်း ဖြစ်နိုင်တယ်။ နောင် ဘောက်ချာ အသစ်တွေက ရောင်းချိန်မှာ မှတ်ထားမှာ မို့ အတိအကျ ဖြစ်မယ်။": {
  "en": "⚠️ {n} product lines this month (of {total}) have no Base price noted at the time of sale, so the current Base is used — the Base profit figure may be a little off. New invoices will have it noted at sale time and be exact.",
  "zh": "⚠️ 本月有 {n} 行商品 (共 {total} 行) 销售时没记 Base 价格,所以用当前 Base 算 — Base 利润数字可能有点不准。以后的新发票会在销售时记下来,就会准确了。"
 },
 "⚠️ ဒီ လရဲ့ ပစ္စည်း အတန်း {n} တန်း (စုစုပေါင်း {total} ထဲ) က ရောင်းချိန် အရင်း မှတ်ထားတာ မရှိသေးလို့ လက်ရှိ Actual နဲ့ တွက်ထားတယ် — အမြတ် ကိန်း နည်းနည်း ခန့်မှန်း ဖြစ်နိုင်တယ်။ နောင် ဘောက်ချာ အသစ်တွေက ရောင်းချိန်မှာ မှတ်ထားမှာ မို့ အတိအကျ ဖြစ်မယ်။": {
  "en": "⚠️ {n} product lines this month (of {total}) have no cost noted at the time of sale, so the current Actual is used — the profit figure may be a little off. New invoices will have the cost noted at sale time and be exact.",
  "zh": "⚠️ 本月有 {n} 行商品 (共 {total} 行) 销售时没记成本,所以用当前 Actual 算 — 利润数字可能有点不准。以后的新发票会在销售时记下成本,就会准确了。"
 },
 "⚠️ ပယ်ဖျက်ရင် ဒီ အဝယ်နဲ့ ဝင်ထားတဲ့ stock အကုန် ပြန် နှုတ်မယ်။ မှတ်တမ်းကတော့ \"ပယ်ဖျက်\" အဖြစ် ဆက် ပေါ်နေမယ်။ စစ်ပြီး သွားတဲ့ အဝယ်ကို ပယ်ဖျက်လို့ မရဘူး။": {
  "en": "⚠️ Cancelling takes back all the stock that came in with this purchase. The record stays, marked \"cancelled\". A purchase that has been checked cannot be cancelled.",
  "zh": "⚠️ 取消会把这张采购进来的库存全部扣回。记录会留着,标为“已取消”。已经核对过的采购不能取消。"
 },
 "⚠️ အန္တရာယ် စာရင်း": {
  "en": "⚠️ At-risk list",
  "zh": "⚠️ 风险清单"
 },
 "⚠️ အန္တရာယ် စာရင်း (အရင်း တက်သွားတဲ့ ဘောက်ချာများ)": {
  "en": "⚠️ At-risk list (invoices whose cost went up)",
  "zh": "⚠️ 风险清单 (成本涨了的发票)"
 },
 "⚠️ အန္တရာယ် ပစ္စည်းများ": {
  "en": "⚠️ Products at risk",
  "zh": "⚠️ 有风险的商品"
 },
 "✅ Supplier အတည်ပြုပြီး": {
  "en": "✅ Supplier confirmed",
  "zh": "✅ Supplier 已确认"
 },
 "✅ စစ် ရန် ကျန် နေရာ မရှိဘူး။": {
  "en": "✅ No place checks are waiting.",
  "zh": "✅ 没有等着核对的地点了。"
 },
 "✅ ဈေး ပြန် စစ် ရန် ကျန် ပစ္စည်း မရှိဘူး။": {
  "en": "✅ No products are waiting for a price review.",
  "zh": "✅ 没有等着复查价格的商品了。"
 },
 "✅ ဒီ ရွေးထားတဲ့ အပိုင်းမှာ အရင်း တက်လို့ ထိခိုက်တဲ့ ဘောက်ချာ မရှိဘူး။": {
  "en": "✅ No invoices in the selected range are affected by a cost rise.",
  "zh": "✅ 所选范围里没有被成本上涨影响的发票。"
 },
 "✅ ပို့ပြီးသား (ဒီနေ့ + မနေ့က)": {
  "en": "✅ Sent (today + yesterday)",
  "zh": "✅ 已发送 (今天 + 昨天)"
 },
 "✅ သေချာ (order)": {
  "en": "✅ Certain (order)",
  "zh": "✅ 确定 (订单)"
 },
 "✅ သေချာ/order": {
  "en": "✅ Certain/order",
  "zh": "✅ 确定/订单"
 },
 "✏️ ပြင်ချက် {n} ခု တင်ရန် ကျန်တယ် — လိုင်း ရရင် ပြန် တင်ပါ": {
  "en": "✏️ {n} edits waiting to send — send them when you have a connection",
  "zh": "✏️ {n} 条修改等着发送 — 有网络时发出去"
 },
 "✏️ ပြင်ချက် တင်ရန် ကျန်တယ်": {
  "en": "✏️ Edit waiting to send",
  "zh": "✏️ 修改等着发送"
 },
 "✏️ ပြင်မယ်": {
  "en": "✏️ Edit",
  "zh": "✏️ 修改"
 },
 "✏️ ပြင်ရန် (အရေအတွက်)": {
  "en": "✏️ Edit (quantities)",
  "zh": "✏️ 修改 (数量)"
 },
 "✓ ကိုင်တွယ်ပြီး": {
  "en": "✓ Handled",
  "zh": "✓ 已处理"
 },
 "✓ ကိုင်တွယ်ပြီး မှတ်တမ်း": {
  "en": "✓ Handled records",
  "zh": "✓ 已处理记录"
 },
 "✓ ကိုင်တွယ်ပြီး မှတ်မယ်": {
  "en": "✓ Mark handled",
  "zh": "✓ 标记为已处理"
 },
 "✓ စစ် ပြီး": {
  "en": "✓ Reviewed",
  "zh": "✓ 已看过"
 },
 "✓ စစ် ပြီး မှတ်တမ်း": {
  "en": "✓ Reviewed record",
  "zh": "✓ 已看过记录"
 },
 "✓ စစ်ပြီး": {
  "en": "✓ Checked",
  "zh": "✓ 已核对"
 },
 "✓ ဒီ Supplier အတွက် အမှာ (Order Request) လုပ်မယ်": {
  "en": "✓ Make an order request for this supplier",
  "my": "✓ ဒီ Supplier အတွက် အမှာ လုပ်မယ်",
  "zh": "✓ 为这个 Supplier 建一张订购单 (Order Request)"
 },
 "✓ မှာ ပြီး — ရောက် ဖို့ စောင့် နေ": {
  "en": "✓ Ordered — waiting for it to arrive",
  "zh": "✓ 已订货 — 等待到货"
 },
 "✓ လက်ကျန် ရှိ": {
  "en": "✓ In stock",
  "zh": "✓ 有现货"
 },
 "✓ လက်ကျန် လုံလောက် — ဝယ် စရာ မလို": {
  "en": "✓ Stock is enough — nothing to buy",
  "zh": "✓ 库存充足 — 无需购买"
 },
 "✓ အကုန် မှာ ပြီး — ကုန် ရောက် ဖို့ စောင့် နေ": {
  "en": "✓ All ordered — waiting for the goods to arrive",
  "zh": "✓ 已全部订货 — 等待到货"
 },
 "✖ စစ်ထုတ်မှု ရှင်း": {
  "en": "✖ Clear filters",
  "zh": "✖ 清除筛选"
 },
 "✖ ရှင်း": {
  "en": "✖ Clear",
  "zh": "✖ 清除"
 },
 "❌ ပယ်ဖျက်ပြီး — ပယ်ဖျက်သူ:": {
  "en": "❌ Cancelled — cancelled by:",
  "zh": "❌ 已取消 — 取消人:"
 },
 "➕ အသစ် မှတ်မယ်": {
  "en": "➕ Add new",
  "zh": "➕ 新增记录"
 },
 "⭐ Leader ဈေးနှုန်း": {
  "en": "⭐ Leader prices",
  "zh": "⭐ Leader 价格"
 },
 "⭐ Sales Team Leader — အဖွဲ့လိုက် အခြေအနေ": {
  "en": "⭐ Sales Team Leader — whole team",
  "my": "⭐ အဖွဲ့ ခေါင်းဆောင် — အဖွဲ့လိုက် အခြေအနေ",
  "zh": "⭐ 销售团队 Leader — 全队情况"
 },
 "🏢 ရုံး": {
  "en": "🏢 Office",
  "zh": "🏢 办公室"
 },
 "🏢 ရုံး တည်နေရာ — Pin/Visit ချန်ရန် (20 m)": {
  "en": "🏢 Office location — skip Pin/Visit within (20 m)",
  "zh": "🏢 办公室位置 — (20 m) 内不记 Pin/到访"
 },
 "🏢 ရုံး အရောင်း": {
  "en": "🏢 Office sales",
  "zh": "🏢 办公室销售"
 },
 "🏪 ဆိုင်များ (Credit)": {
  "en": "🏪 Shops (Credit)",
  "my": "🏪 ဆိုင်များ",
  "zh": "🏪 店铺 (欠款)"
 },
 "🏪 ဆိုင်များ ({n})": {
  "en": "🏪 Shops ({n})",
  "zh": "🏪 店铺 ({n})"
 },
 "👁 ကြည့်မယ်": {
  "en": "👁 View",
  "zh": "👁 查看"
 },
 "👁 ပြမယ်": {
  "en": "👁 Show",
  "zh": "👁 显示"
 },
 "💰 Cash Summary (ငွေ အဝင်/အထွက်, ဒီလ)": {
  "en": "💰 Cash Summary (money in/out, this month)",
  "my": "💰 ငွေ အဝင်/အထွက် (ဒီလ)",
  "zh": "💰 现金汇总 (钱进/出,本月)"
 },
 "💰 ငွေ စာရင်း": {
  "en": "💰 Money book",
  "zh": "💰 钱账本"
 },
 "💰 ငွေ မှတ်ဖို့ အရင် 🚚 ပို့ပြီး မှတ်ရပါမယ် (ပို့ပြီးမှ ငွေ ရတဲ့ စနစ်)": {
  "en": "💰 Mark 🚚 Delivered first before recording money (payment comes after delivery)",
  "zh": "💰 记钱之前先标记 🚚 已送达 (送达后才收钱的制度)"
 },
 "💰 ငွေ ရရှိ မှတ်တမ်း": {
  "en": "💰 Payments received",
  "zh": "💰 收款记录"
 },
 "💰 ငွေကောက်ခံပြီးသား ဘောက်ချာ — ပြင်လို့ မရတော့ပါ": {
  "en": "💰 This invoice is already paid — it can no longer be edited",
  "zh": "💰 这张发票已经收过钱了 — 不能再改"
 },
 "💰 ငွေရှင်းပြီး မှတ်မယ်": {
  "en": "💰 Mark as paid",
  "zh": "💰 标记为已付款"
 },
 "💰 ဒီ batch အတွက် ရှင်းရန် မှတ်တမ်း ရှိတယ်:": {
  "en": "💰 This batch has a settlement record:",
  "zh": "💰 这个批次有结算记录:"
 },
 "💰 ဒီ အမှာ အတွက် ရှင်းရန် မှတ်တမ်း ရှိတယ်:": {
  "en": "💰 This request has a settlement record:",
  "zh": "💰 这张订购单有结算记录:"
 },
 "💵 ငွေ လွှဲအပ် စစ်ရန်": {
  "en": "💵 Check handed-over money",
  "zh": "💵 核对交接的钱"
 },
 "💸 အသုံးစရိတ်": {
  "en": "💸 Expenses",
  "zh": "💸 开支"
 },
 "💸 အသုံးစရိတ် ပြင်": {
  "en": "💸 Edit expense",
  "zh": "💸 编辑开支"
 },
 "💸 အသုံးစရိတ် အကျဉ်း (ဒီလ) — ": {
  "en": "💸 Expense summary (this month) — ",
  "zh": "💸 开支汇总 (本月) — "
 },
 "💾 Backup မိတ္တူများ": {
  "en": "💾 Backup copies",
  "zh": "💾 备份副本"
 },
 "💾 သိမ်းမယ်": {
  "en": "💾 Save",
  "zh": "💾 保存"
 },
 "📅 ကောက်ခဲ့တဲ့ နေ့ ကျော်သွားပြီ — Sales ပြင်ခွင့် ပိတ်ပြီ (Warehouse ကို ဆက်သွယ်ပါ)": {
  "en": "📅 The collection day has passed — Sales editing is closed (contact Warehouse)",
  "zh": "📅 收款日已经过了 — 业务员不能改了 (请联系 Warehouse)"
 },
 "📈 လချုပ် အမြတ်": {
  "en": "📈 Monthly profit",
  "zh": "📈 月度利润"
 },
 "📊 Sales စစ်ဆေး": {
  "en": "📊 Sales check",
  "my": "📊 အရောင်း စစ်ဆေး",
  "zh": "📊 销售检查"
 },
 "📊 Sales စစ်ဆေး (Performance)": {
  "en": "📊 Sales report (Performance)",
  "my": "📊 အရောင်း စစ်ဆေး",
  "zh": "📊 销售报告 (业绩)"
 },
 "📋 ဈေးနှုန်း စာရင်း": {
  "en": "📋 Price list",
  "zh": "📋 价格表"
 },
 "📋 ဈေးနှုန်း စာရင်း (Base + Margin) — Leader သီးသန့်": {
  "en": "📋 Price list (Base + Margin) — Leader only",
  "zh": "📋 价格表 (Base + 利润率) — 只有 Leader 能看"
 },
 "📋 မိမိ ဘောက်ချာများ (Offline ကြည့်/ပြင် ရတယ်)": {
  "en": "📋 My invoices (view/edit offline)",
  "zh": "📋 我的发票 (离线可看/可改)"
 },
 "📋 မှတ်တမ်း စစ်": {
  "en": "📋 Audit trail",
  "zh": "📋 记录审查"
 },
 "📋 မှတ်တမ်း စစ် — သတိပေးချက်များ": {
  "en": "📋 Audit trail — alerts",
  "zh": "📋 记录审查 — 提醒"
 },
 "📋 မှာပြီး စလစ်များ (အမှာ)": {
  "en": "📋 Order slips raised (requests)",
  "zh": "📋 已建的订购单 (订购)"
 },
 "📋 အမှာ": {
  "en": "📋 Order requests",
  "zh": "📋 订购单"
 },
 "📍 Map ဖွင့်ရန်": {
  "en": "📍 Open Map",
  "zh": "📍 打开地图"
 },
 "📍 နေရာ စစ် စာရင်း": {
  "en": "📍 Place-check list",
  "zh": "📍 地点核对清单"
 },
 "📍 နေရာ မှတ်တမ်း": {
  "en": "📍 Recorded places",
  "zh": "📍 记录的地点"
 },
 "📍 လက်ရှိ နေရာ ကို ရုံး အဖြစ် မှတ်မယ်": {
  "en": "📍 Save current place as the office",
  "zh": "📍 把当前位置存为办公室"
 },
 "📒 အဝယ် မှတ်တမ်း": {
  "en": "📒 Purchase records",
  "zh": "📒 采购记录"
 },
 "📤 ကျသင့် အတည်ပြု — Supplier ဆီ ပို့ပြီး": {
  "en": "📤 Confirm payable — mark sent to supplier",
  "zh": "📤 确认应付 — 标记已发给 Supplier"
 },
 "📥 Stock+ဈေး Excel": {
  "en": "📥 Stock+Price Excel",
  "my": "📥 လက်ကျန်+ဈေး Excel",
  "zh": "📥 库存+价格 Excel"
 },
 "📥 Stock+ဈေး Excel ဆွဲမယ်": {
  "en": "📥 Download Stock+Price Excel",
  "my": "📥 လက်ကျန်+ဈေး Excel ဆွဲမယ်",
  "zh": "📥 下载库存+价格 Excel"
 },
 "📥 ကုန်ရောက် သွင်း": {
  "en": "📥 Receive goods",
  "zh": "📥 收货"
 },
 "📦 ရောင်း · ပို့ · ငွေ": {
  "en": "📦 Sale · Delivery · Collection",
  "zh": "📦 销售 · 送货 · 收款"
 },
 "📱 Catalog ဖွင့်ရန်": {
  "en": "📱 Open Catalog",
  "zh": "📱 打开 Catalog"
 },
 "📴 Offline က တင်ထား": {
  "en": "📴 Submitted offline",
  "zh": "📴 离线提交的"
 },
 "🔁 ကုန် လဲ": {
  "en": "🔁 Exchange",
  "zh": "🔁 换货"
 },
 "🔄 ပြန် တင်မယ်": {
  "en": "🔄 Send now",
  "zh": "🔄 现在发送"
 },
 "🔔 အသိပေးချက်များ — ဘောက်ချာ ပြင်ဆင်မှုများ": {
  "en": "🔔 Notifications — invoice edits",
  "zh": "🔔 通知 — 发票修改"
 },
 "🔗 {sku} နဲ့ ချိတ်ထား — စုစုပေါင်း လက်ကျန် {pool} {unit}": {
  "en": "🔗 Linked with {sku} — total on hand {pool} {unit}",
  "zh": "🔗 已和 {sku} 关联 — 手头总计 {pool} {unit}"
 },
 "🔗 {sku} ရဲ့ အထုပ်": {
  "en": "🔗 Pack of {sku}",
  "zh": "🔗 {sku} 的包装"
 },
 "🔗 အထုပ် ချိတ်ဆက်မှု": {
  "en": "🔗 Pack linking",
  "zh": "🔗 包装关联"
 },
 "🔗 အထုပ်: {skus}": {
  "en": "🔗 Pack: {skus}",
  "zh": "🔗 包装:{skus}"
 },
 "🔢 Stock စစ်ဆေး": {
  "en": "🔢 Stock count",
  "zh": "🔢 库存盘点"
 },
 "🔴 Offline — သိမ်းထားတဲ့ ဈေးတွေနဲ့ အလုပ်ဖြစ်တယ်": {
  "en": "🔴 Offline — working with saved prices",
  "zh": "🔴 离线 — 用保存的价格工作"
 },
 "🔴 ရောင်းပိတ်": {
  "en": "🔴 Sell closed",
  "zh": "🔴 销售关闭"
 },
 "🔴 ရောင်းပိတ် သာ": {
  "en": "🔴 Sell-closed only",
  "zh": "🔴 只看销售关闭的"
 },
 "🔴 အရောင်း ပိတ်": {
  "en": "🔴 Sales closed",
  "zh": "🔴 销售已关闭"
 },
 "🔴 အရှုံး သေချာ": {
  "en": "🔴 Certain loss",
  "zh": "🔴 肯定亏损"
 },
 "🔴 အရှုံး သေချာ စုစုပေါင်း": {
  "en": "🔴 Total certain loss",
  "zh": "🔴 肯定亏损总计"
 },
 "🔴 အရှုံး:": {
  "en": "🔴 Loss:",
  "zh": "🔴 亏损:"
 },
 "🔵 ဖြစ်နိုင်ခြေ": {
  "en": "🔵 Probable",
  "zh": "🔵 可能到访"
 },
 "🕠 ရုံးချိန် (မနက် ၈:၃၀ – ညနေ ၅:၃၀) မဟုတ်တော့ပါ — Sales ပြင်ခွင့် ပိတ်ပြီ (Warehouse ကို ဆက်သွယ်ပါ)": {
  "en": "🕠 Outside office hours (8:30 AM – 5:30 PM) — Sales editing is closed (contact Warehouse)",
  "zh": "🕠 不在办公时间 (早上 8:30 – 下午 5:30) — 业务员不能修改了 (请联系 Warehouse)"
 },
 "🖨 Print ထုတ်ပြီး:": {
  "en": "🖨 Printed:",
  "my": "🖨 ပုံနှိပ် ထုတ်ပြီး:",
  "zh": "🖨 已打印:"
 },
 "🖨 Print ထုတ်ပြီးသား ဘောက်ချာ — အရောင်း ပြင်ခွင့် ပိတ်ပြီး (Warehouse ကို ဆက်သွယ်ပါ)": {
  "en": "🖨 This invoice has been printed — Sales editing is closed (contact Warehouse)",
  "my": "🖨 ပုံနှိပ် ထုတ်ပြီးသား ဘောက်ချာ — အရောင်း ပြင်ခွင့် ပိတ်ပြီး (Warehouse ကို ဆက်သွယ်ပါ)",
  "zh": "🖨 这张发票已经打印过了 — 业务员不能修改了 (请联系 Warehouse)"
 },
 "🖨️ Print ထုတ်ပြီး": {
  "en": "🖨️ Printed",
  "my": "🖨️ ပုံနှိပ် ထုတ်ပြီး",
  "zh": "🖨️ 已打印"
 },
 "🖨️ အမှာ စာရွက် Print (Order Issue)": {
  "en": "🖨️ Print order slip (Order Issue)",
  "my": "🖨️ အမှာ စာရွက် ပုံနှိပ်",
  "zh": "🖨️ 打印订购单 (Order Issue)"
 },
 "🗑 ဖျက်မယ်": {
  "en": "🗑 Delete",
  "zh": "🗑 删除"
 },
 "🙈 ဖျောက်မယ်": {
  "en": "🙈 Hide",
  "zh": "🙈 隐藏"
 },
 "🚚 ကုန်ရောက် သွင်း": {
  "en": "🚚 Receive goods",
  "zh": "🚚 收货"
 },
 "🚚 ကုန်ရောက် သွင်းရန်": {
  "en": "🚚 Receive goods",
  "zh": "🚚 收货"
 },
 "🚚 ကုန်ရောက် သွင်းရန် (PUR ဖွင့်မယ်)": {
  "en": "🚚 Receive goods (open PUR)",
  "zh": "🚚 收货 (建 PUR)"
 },
 "🚚 ပို့ဆောင်": {
  "en": "🚚 Delivery",
  "zh": "🚚 送货"
 },
 "🚚 ပို့ပြီး (stock နှုတ်)": {
  "en": "🚚 Delivered (deduct stock)",
  "zh": "🚚 已送达 (扣库存)"
 },
 "🚚 ပို့မယ်": {
  "en": "🚚 Deliver",
  "zh": "🚚 送货"
 },
 "🚶 Location History": {
  "en": "🚶 Location History",
  "my": "🚶 ရောက် မှတ်တမ်း",
  "zh": "🚶 到访记录"
 },
 "🚶 Location History (ရောက် မှတ်တမ်း)": {
  "en": "🚶 Location History",
  "my": "🚶 ရောက် မှတ်တမ်း",
  "zh": "🚶 到访记录"
 },
 "🛒 အဝယ်": {
  "en": "🛒 Purchases",
  "zh": "🛒 采购"
 },
 "🟡 အမြတ် ကျုံ့": {
  "en": "🟡 Margin shrunk",
  "zh": "🟡 利润变薄"
 },
 "🟡 အမြတ် ကျုံ့ စုစုပေါင်း": {
  "en": "🟡 Total shrunk margin",
  "zh": "🟡 利润变薄总计"
 },
 "🟡 အမြတ် ကျုံ့:": {
  "en": "🟡 Shrunk:",
  "zh": "🟡 变薄:"
 },
 "🟢 Online — နောက်ဆုံး ဈေးတွေ ရထားတယ်": {
  "en": "🟢 Online — latest prices loaded",
  "zh": "🟢 在线 — 已加载最新价格"
 },
 "🟢 ဖွင့်": {
  "en": "🟢 Open",
  "zh": "🟢 开启"
 },
 "🟢 ရောင်းဖွင့်": {
  "en": "🟢 Sell open",
  "zh": "🟢 销售开启"
 },
 "🟢 ရောင်းဖွင့် သာ": {
  "en": "🟢 Sell-open only",
  "zh": "🟢 只看销售开启的"
 },
 "🤖 Ai မေးကြည့်": {
  "en": "🤖 Ai Ask",
  "zh": "🤖 Ai 问问"
 },
 "🧮 ငွေသား ရေတွက် ပိတ်": {
  "en": "🧮 Count the cash and close",
  "zh": "🧮 数现金并结账"
 },
 "🧰 Cashbox": {
  "en": "🧰 Cashbox",
  "zh": "🧰 现金箱"
 },
 "🧾 Invoice (A5) ထုတ်ရန်": {
  "en": "🧾 Print Invoice (A5)",
  "zh": "🧾 打印发票 (A5)"
 },
 "🧾 ဘောက်ချာများ (နောက်ဆုံး ၁၀၀)": {
  "en": "🧾 Invoices (latest 100)",
  "zh": "🧾 发票 (最新 100 张)"
 },
 "🧾 အခွန် အစီရင်ခံ": {
  "en": "🧾 Tax report",
  "zh": "🧾 税务报告"
 },
 "Ai ဈေး မှတ်တမ်း စီမံ": {
  "en": "Manage ask prices",
  "zh": "管理 AI 价格记录"
 },
 "Ai မေးကြည့်": {
  "en": "Ask AI",
  "zh": "问 AI"
 },
 "Backup ဆွဲ": {
  "en": "Make backups",
  "zh": "创建备份"
 },
 "Backup ဒေါင်း": {
  "en": "Download backups",
  "zh": "下载备份"
 },
 "Backup များ ကြည့်": {
  "en": "View backups",
  "zh": "查看备份"
 },
 "Backup လိုအပ် သတိပေးချက်": {
  "en": "Backup-due reminder",
  "zh": "备份提醒"
 },
 "Base ဈေး ကြည့်": {
  "en": "View Base price",
  "zh": "查看 Base 价格"
 },
 "Cashbox ကြည့်": {
  "en": "View cashbox",
  "zh": "查看现金箱"
 },
 "Cashbox မှတ်/ရေတွက်": {
  "en": "Manage cashbox",
  "zh": "管理现金箱"
 },
 "Cashbox အတည်ပြု": {
  "en": "Verify cashbox close",
  "zh": "核对现金箱结账"
 },
 "Catalog": {
  "my": "Catalog",
  "en": "Catalog",
  "zh": "目录"
 },
 "Catalog ကြည့်": {
  "en": "View catalog",
  "zh": "查看目录"
 },
 "Catalog ဈေးများ (API) ရယူ": {
  "en": "Catalog prices via API",
  "zh": "通过 API 获取目录价格"
 },
 "GPS ကွာခြား သတ်မှတ်": {
  "en": "Set GPS threshold",
  "zh": "设置 GPS 阈值"
 },
 "Invoice ထုတ်": {
  "en": "Print invoices",
  "zh": "打印发票"
 },
 "Leader ခန့်/ဖြုတ်": {
  "en": "Toggle team leader",
  "zh": "任免组长"
 },
 "Leader ဈေးနှုန်း စာရင်း ကြည့်": {
  "en": "View leader prices",
  "zh": "查看组长价格表"
 },
 "Leader ဈေးနှုန်း ဖွင့်/ပိတ်": {
  "en": "Toggle leader prices",
  "zh": "开关组长价格表"
 },
 "Leader အတွက် Base ဈေး ကြည့်": {
  "en": "Leader Base view",
  "zh": "组长 Base 视图"
 },
 "Offline ပြင်ချက် တင် (API)": {
  "en": "Offline edits via API",
  "zh": "通过 API 提交离线修改"
 },
 "Owner အခန်းရဲ့ ခွင့်ပြုချက်တွေကို ဖြုတ်လို့ မရဘူး": {
  "en": "The Owner role’s permissions cannot be removed",
  "zh": "Owner 角色的权限不能移除"
 },
 "Owner အခန်းရဲ့ ခွင့်ပြုချက်တွေကို ဖြုတ်လို့ မရဘူး — ဒီ စာမျက်နှာက ကြည့်ရန်သာ ပါ။": {
  "en": "The Owner role’s permissions cannot be removed — this page is view-only.",
  "zh": "Owner 角色的权限不能移除 — 本页仅供查看。"
 },
 "Pack ချိတ်": {
  "en": "Link packs",
  "zh": "关联包装"
 },
 "Pack ဖြုတ်": {
  "en": "Unlink packs",
  "zh": "解除包装关联"
 },
 "Roles": {
  "my": "Roles",
  "en": "Roles",
  "zh": "角色"
 },
 "Sales စစ်ဆေး ကြည့်": {
  "en": "View sales report",
  "zh": "查看销售报告"
 },
 "Stock": {
  "my": "Stock",
  "en": "Stock",
  "zh": "库存"
 },
 "Stock စစ်ဆေး ကြည့်": {
  "en": "View stock counts",
  "zh": "查看盘点"
 },
 "Stock စစ်ဆေး ဖျက်": {
  "en": "Delete stock counts",
  "zh": "删除盘点"
 },
 "Stock စစ်ဆေး သိမ်း": {
  "en": "Save stock counts",
  "zh": "保存盘点"
 },
 "Stock စစ်ဆေး အတည်ပြု": {
  "en": "Approve stock counts",
  "zh": "批准盘点"
 },
 "Stock စစ်ဆေး အသစ်": {
  "en": "Create stock counts",
  "zh": "新建盘点"
 },
 "Stock မှတ်တမ်း ကြည့်": {
  "en": "View stock ledger",
  "zh": "查看库存账"
 },
 "Stock+ဈေး Excel ထုတ်": {
  "en": "Export stock sheet",
  "zh": "导出库存表"
 },
 "Supplier ဖွင့်/ပိတ်": {
  "en": "Toggle supplier",
  "zh": "开关供应商"
 },
 "System အခန်း": {
  "en": "System role",
  "zh": "系统角色"
 },
 "System အခန်း — ဖျက်လို့ မရဘူး": {
  "en": "System role — cannot be deleted",
  "zh": "系统角色 — 不能删除"
 },
 "System အခန်း — ဖျက်လို့ မရဘူး။ Owner အခန်းရဲ့ ခွင့်ပြုချက်တွေကို ဖြုတ်လို့ မရဘူး။": {
  "en": "System roles cannot be deleted. The Owner role’s permissions cannot be removed.",
  "zh": "系统角色不能删除。Owner 角色的权限不能移除。"
 },
 "System အခန်းရဲ့ နာမည်ကို ပြောင်းလို့ မရဘူး": {
  "en": "A system role’s name cannot be changed",
  "zh": "系统角色的名称不能更改"
 },
 "ကိုယ့် အခန်း ကိုယ် မပြောင်းပါနဲ့ — လော့ခ် ဖြစ်သွားနိုင်တယ်": {
  "en": "Do not change your own role — you could lock yourself out",
  "zh": "不要改自己的角色 — 可能会把自己锁在外面"
 },
 "ကိုယ်တိုင် ဆောက်ထားတဲ့ အခန်း": {
  "en": "Custom role",
  "zh": "自定义角色"
 },
 "ကိုယ်ပိုင် ဘောက်ချာ (API)": {
  "en": "Own orders via API",
  "zh": "通过 API 获取个人发票"
 },
 "ကုန် လဲ (တစ်ကြိမ်) မှတ်": {
  "en": "Record goods exchange",
  "zh": "记录换货"
 },
 "ကြွေးကျော် ဘောက်ချာ ဆောက် (Owner)": {
  "en": "Credit override",
  "zh": "超信用开单"
 },
 "ခွင့်ပြုချက် အရေအတွက်": {
  "en": "Permission count",
  "zh": "权限数"
 },
 "ငွေ & Cashbox": {
  "en": "Money & cashbox",
  "zh": "资金与现金箱"
 },
 "ငွေ စာရင်း ကြည့်": {
  "en": "View money book",
  "zh": "查看资金账"
 },
 "ငွေ စာရင်း မှတ်/ဖျက်": {
  "en": "Manage money book",
  "zh": "管理资金账"
 },
 "ငွေ မှတ်တမ်း ဖျက်": {
  "en": "Delete payments",
  "zh": "删除收款记录"
 },
 "ငွေ ရရှိ မှတ်": {
  "en": "Record payments",
  "zh": "记录收款"
 },
 "ငွေ လွှဲအပ် ကြည့်": {
  "en": "View handovers",
  "zh": "查看交款"
 },
 "ငွေ လွှဲအပ် အတည်ပြု": {
  "en": "Verify handovers",
  "zh": "核对交款"
 },
 "ငွေ အခြေအနေ (position) ကြည့်": {
  "en": "Money position",
  "zh": "资金状况"
 },
 "ငွေ အဖွင့် လက်ကျန် သတ်": {
  "en": "Set money opening",
  "zh": "设置资金期初"
 },
 "ငွေ/ဈေး သတိပေးချက် ပြရန်": {
  "en": "Finance warning pills",
  "zh": "财务提醒"
 },
 "ငွေရပြီး မှတ်": {
  "en": "Mark paid",
  "zh": "标记已收款"
 },
 "ဆိုင် စာရင်း (API) ရယူ": {
  "en": "Outlet list via API",
  "zh": "通过 API 获取店铺清单"
 },
 "ဆိုင် အနား ရောက် မှတ်တမ်း (API)": {
  "en": "Visit probe via API",
  "zh": "通过 API 记录到店"
 },
 "ဆိုင်များ ကြည့်": {
  "en": "View outlets",
  "zh": "查看店铺"
 },
 "ဆိုင်များ ပြင်/ပေါင်း": {
  "en": "Manage outlets",
  "zh": "管理店铺"
 },
 "ဆိုင်လည် မှတ်တမ်း ကြည့်": {
  "en": "View visits",
  "zh": "查看走访记录"
 },
 "ဈေးနှုန်း ပြင်": {
  "en": "Edit prices",
  "zh": "编辑价格"
 },
 "ဌာန": {
  "en": "Department",
  "zh": "部门"
 },
 "ဌာန နာမည် ပြင်ပြီးပြီ": {
  "en": "Department name updated",
  "zh": "部门名称已更新"
 },
 "ဌာန နာမည် လိုပါတယ်": {
  "en": "A department name is needed",
  "zh": "需要部门名称"
 },
 "ဌာန ပြောင်းတာက ခွင့်ပြုချက် မပြောင်းဘူး — ဌာနက တံဆိပ် သက်သက်ပါ။": {
  "en": "Changing a department does not change permissions — a department is only a label.",
  "zh": "改部门不改权限 — 部门只是一个标签。"
 },
 "ဌာန ပြောင်းပြီးပြီ — ခွင့်ပြုချက် မပြောင်းဘူး": {
  "en": "Department changed — permissions unchanged",
  "zh": "部门已更改 — 权限不变"
 },
 "ဌာန ဖျက်ပြီးပြီ — အဖွဲ့ဝင်တွေကို ဌာန မရှိအဖြစ် ပြန်ထားတယ်": {
  "en": "Department deleted — members are now unassigned",
  "zh": "部门已删除 — 成员已设为无部门"
 },
 "ဌာန မရှိ": {
  "en": "No department",
  "zh": "无部门"
 },
 "ဌာန အသစ် ဆောက်ပြီးပြီ": {
  "en": "New department created",
  "zh": "新部门已创建"
 },
 "ဌာန အသစ် ဆောက်မယ်": {
  "en": "Create new department",
  "zh": "新建部门"
 },
 "ဌာန အသစ် နာမည်": {
  "en": "New department name",
  "zh": "新部门名称"
 },
 "ဌာနက တံဆိပ် သက်သက်ပါ — ဌာန ပြောင်းတာက ခွင့်ပြုချက် လုံးဝ မပြောင်းဘူး။ ဌာန ဖျက်ရင် အဖွဲ့ဝင်တွေကို ဌာန မရှိအဖြစ် ပြန်ထားတယ်၊ ခွင့်ပြုချက်ကို မထိဘူး။": {
  "en": "A department is only a label — changing it never changes permissions. Deleting a department leaves its members unassigned and never touches permissions.",
  "zh": "部门只是一个标签 — 改部门绝不改权限。删除部门会让成员变为无部门，绝不动权限。"
 },
 "ဌာနများ": {
  "en": "Departments",
  "zh": "部门"
 },
 "ဌာနများ စီမံ": {
  "en": "Manage departments",
  "zh": "管理部门"
 },
 "ဒီ ဌာန ဖျက်မှာ သေချာလား? အဖွဲ့ဝင်တွေကို ဌာန မရှိအဖြစ် ပြန်ထားမယ်။": {
  "en": "Delete this department? Its members will become unassigned.",
  "zh": "确定删除这个部门吗？成员将变为无部门。"
 },
 "ဒီ အခန်း ဖျက်မှာ သေချာလား?": {
  "en": "Delete this role?",
  "zh": "确定删除这个角色吗？"
 },
 "ဒီ အခန်းကို အသုံးပြုသူ ရှိသေးလို့ ဖျက်လို့ မရဘူး": {
  "en": "This role still has users, so it cannot be deleted",
  "zh": "该角色还有用户，不能删除"
 },
 "နေရာ စစ်ပြီး မှတ်": {
  "en": "Mark GPS checked",
  "zh": "标记位置已核查"
 },
 "နေရာ စစ်ရန် စာရင်း ကြည့်": {
  "en": "View GPS-check list",
  "zh": "查看位置核查清单"
 },
 "နေရာ စစ်ရန် သတိပေးချက် ပြရန်": {
  "en": "GPS-check warning pill",
  "zh": "位置核查提醒"
 },
 "ပစ္စည်း & ဈေး": {
  "en": "Products & prices",
  "zh": "商品与价格"
 },
 "ပစ္စည်း စာရင်း ကြည့်": {
  "en": "View products",
  "zh": "查看商品"
 },
 "ပစ္စည်း ပြင် (မိမိ အပိုင်း)": {
  "en": "Edit products (own part)",
  "zh": "编辑商品(负责部分)"
 },
 "ပစ္စည်း အချက်အလက် ပြင်": {
  "en": "Edit product data",
  "zh": "编辑商品资料"
 },
 "ပစ္စည်း အသစ် ဆောက်": {
  "en": "Create products",
  "zh": "新建商品"
 },
 "ပို့ဆောင် (ပို့သူ/ရုံး)": {
  "en": "Deliver orders",
  "zh": "配送"
 },
 "ပို့ဆောင် ကိုယ်ပိုင် ပင်မ": {
  "en": "Own delivery dashboard",
  "zh": "个人配送主页"
 },
 "ပို့ဆောင် စာမျက်နှာ ကြည့်": {
  "en": "View delivery board",
  "zh": "查看配送看板"
 },
 "ပို့ပြီး မှတ်": {
  "en": "Mark delivered",
  "zh": "标记已送达"
 },
 "ပို့သူ တာဝန် ပေး": {
  "en": "Assign deliverer",
  "zh": "指派配送员"
 },
 "ပြန်သွားမယ်": {
  "en": "Go back",
  "zh": "返回"
 },
 "ပြန်အပ် စစ်ပြီး မှတ်": {
  "en": "Check returns",
  "zh": "核对退货"
 },
 "ပြန်အပ် စာရင်း ကြည့်": {
  "en": "View returns",
  "zh": "查看退货"
 },
 "ပြန်အပ် မှတ်": {
  "en": "Record returns",
  "zh": "记录退货"
 },
 "ဘောက်ချာ & ပို့ဆောင်": {
  "en": "Orders & delivery",
  "zh": "发票与配送"
 },
 "ဘောက်ချာ စာရင်း ကြည့်": {
  "en": "View order list",
  "zh": "查看发票清单"
 },
 "ဘောက်ချာ ပယ်ဖျက်": {
  "en": "Cancel orders",
  "zh": "取消发票"
 },
 "ဘောက်ချာ ပြင်": {
  "en": "Edit orders",
  "zh": "编辑发票"
 },
 "ဘောက်ချာ အသစ် ဆောက်": {
  "en": "Create orders",
  "zh": "新建发票"
 },
 "ဘောက်ချာ အသေးစိတ် ကြည့်": {
  "en": "View order detail",
  "zh": "查看发票详情"
 },
 "ဘောက်ချာကို ဆိုင် တွဲ": {
  "en": "Attach outlet to order",
  "zh": "为发票关联店铺"
 },
 "မှတ်တမ်း ကိုင်တွယ်ပြီး မှတ်": {
  "en": "Mark audit handled",
  "zh": "标记审计已处理"
 },
 "မှတ်တမ်း စစ် ကြည့်": {
  "en": "View audit log",
  "zh": "查看审计记录"
 },
 "ယောက် ဒီ အခန်းကို သုံးနေတယ်။": {
  "en": "people use this role.",
  "zh": "人使用这个角色。"
 },
 "ရပ်ကွက် စီမံ": {
  "en": "Manage areas",
  "zh": "管理区域"
 },
 "ရုံး တည်နေရာ သတ်မှတ်": {
  "en": "Set office location",
  "zh": "设置办公室位置"
 },
 "ရုံး ပင်မ စာမျက်နှာ": {
  "en": "Office dashboard",
  "zh": "办公室主页"
 },
 "ရောင်းနိုင်/မရောင်း ဖွင့်/ပိတ်": {
  "en": "Toggle sellable",
  "zh": "开关可售"
 },
 "ရှင်းရန် ကြည့်": {
  "en": "View settlements",
  "zh": "查看结算"
 },
 "ရှင်းရန် ငွေ ပေး": {
  "en": "Pay settlements",
  "zh": "支付结算"
 },
 "ရှင်းရန် ထဲမှာ အရင်းအမှန် ကြည့်": {
  "en": "View true cost in settlements",
  "zh": "在结算中查看真实成本"
 },
 "ရှင်းရန် ပို့": {
  "en": "Send settlements",
  "zh": "发送结算"
 },
 "ရှင်းရန် မူကြမ်း ပြင်": {
  "en": "Edit settlement drafts",
  "zh": "编辑结算草稿"
 },
 "ရှင်းရန် အတည်ပြု": {
  "en": "Confirm settlements",
  "zh": "确认结算"
 },
 "ရှင်းရန် အသစ် ဆောက်": {
  "en": "Create settlements",
  "zh": "新建结算"
 },
 "လချုပ် အမြတ် ကြည့်": {
  "en": "View profit",
  "zh": "查看利润"
 },
 "ဝင်ရောက် (လက်မှတ်ထိုးပြီး)": {
  "en": "Signed-in access",
  "zh": "已登录访问"
 },
 "ဝယ်ရန် စာရင်း ကြည့်": {
  "en": "View buy list",
  "zh": "查看采购清单"
 },
 "ဝယ်ရန် စာရင်းက အမှာ လုပ်": {
  "en": "Requests from buy list",
  "zh": "从采购清单建订购单"
 },
 "ဝယ်ရန် အကြောင်း ကြည့်": {
  "en": "View buy-list reasons",
  "zh": "查看采购原因"
 },
 "သက်တမ်း နီးနေတာ ကြည့်": {
  "en": "View expiry list",
  "zh": "查看临期清单"
 },
 "အကြောင်းကြားစာ ကြည့်": {
  "en": "View notifications",
  "zh": "查看通知"
 },
 "အခန်း": {
  "en": "Role",
  "zh": "角色"
 },
 "အခန်း (Roles) စီမံ": {
  "en": "Manage roles",
  "zh": "管理角色"
 },
 "အခန်း တစ်ခုက ခွင့်ပြုချက် အစု တစ်ခု ပါ။ အခန်း ပြောင်းမှ ခွင့်ပြုချက် ပြောင်းတယ် — ဌာန ပြောင်းတာက မပြောင်းဘူး။": {
  "en": "A role is a bundle of permissions. Permissions change only when the role changes — changing a department does not change them.",
  "zh": "角色是一组权限。只有改角色才会改权限 — 改部门不会改权限。"
 },
 "အခန်း နာမည်": {
  "en": "Role name",
  "zh": "角色名称"
 },
 "အခန်း နာမည် လိုပါတယ်": {
  "en": "A role name is needed",
  "zh": "需要角色名称"
 },
 "အခန်း ပြင်": {
  "en": "Edit role",
  "zh": "编辑角色"
 },
 "အခန်း ပြင်ပြီးပြီ": {
  "en": "Role updated",
  "zh": "角色已更新"
 },
 "အခန်း ပြောင်း": {
  "en": "Change role",
  "zh": "更改角色"
 },
 "အခန်း ပြောင်းပြီးပြီ": {
  "en": "Role changed",
  "zh": "角色已更改"
 },
 "အခန်း ဖျက်ပြီးပြီ": {
  "en": "Role deleted",
  "zh": "角色已删除"
 },
 "အခန်း မတွေ့ဘူး": {
  "en": "Role not found",
  "zh": "找不到角色"
 },
 "အခန်း အသစ် ဆောက်ပြီးပြီ": {
  "en": "New role created",
  "zh": "新角色已创建"
 },
 "အခန်း အသစ် ဆောက်မယ်": {
  "en": "Create new role",
  "zh": "新建角色"
 },
 "အခန်း အသစ် နာမည်": {
  "en": "New role name",
  "zh": "新角色名称"
 },
 "အခွန် နှုန်း သတ်": {
  "en": "Set tax rate",
  "zh": "设置税率"
 },
 "အခွန် အစီရင်ခံ ကြည့်": {
  "en": "View tax report",
  "zh": "查看税务报告"
 },
 "အစီရင်ခံ & စစ်ဆေး": {
  "en": "Reports & checks",
  "zh": "报告与核查"
 },
 "အန္တရာယ် စာရင်း ကြည့်": {
  "en": "View at-risk list",
  "zh": "查看风险清单"
 },
 "အန္တရာယ် မှတ်သား": {
  "en": "Mark at-risk handled",
  "zh": "标记风险已处理"
 },
 "အဖွဲ့ဝင် အရေအတွက်": {
  "en": "Member count",
  "zh": "成员数"
 },
 "အမှာ စာရင်း ကြည့်": {
  "en": "View order requests",
  "zh": "查看订购单"
 },
 "အမှာ ပိတ်": {
  "en": "Close order requests",
  "zh": "关闭订购单"
 },
 "အမှာ ပေါင်း ရွေး": {
  "en": "Merge request choice",
  "zh": "选择合并订购单"
 },
 "အမှာ ပြင်": {
  "en": "Update order requests",
  "zh": "编辑订购单"
 },
 "အမှာ လိုင်း ထည့်": {
  "en": "Add request lines",
  "zh": "添加订购行"
 },
 "အမှာ လိုင်း ဖြုတ်": {
  "en": "Remove request lines",
  "zh": "删除订购行"
 },
 "အမှာ အသစ် ဆောက်": {
  "en": "Create order requests",
  "zh": "新建订购单"
 },
 "အရင်းအမှန် ကြည့်": {
  "en": "View true cost",
  "zh": "查看真实成本"
 },
 "အရင်းအမှန် နဲ့ ဆိုင်တဲ့ ခွင့်ပြုချက် — Owner နဲ့ Finance ပဲ ပေးသင့်တယ်။": {
  "en": "Permission about true cost — give it only to Owner and Finance.",
  "zh": "关于真实成本的权限 — 只应给 Owner 和 Finance。"
 },
 "အရင်းအမှန် ပြင်": {
  "en": "Edit true cost",
  "zh": "编辑真实成本"
 },
 "အရောင်း ကိုယ်ပိုင် ပင်မ": {
  "en": "Own sales dashboard",
  "zh": "个人销售主页"
 },
 "အရောင်း ဖုန်း (API)": {
  "en": "Sales phone APIs",
  "zh": "销售手机接口"
 },
 "အဝယ် & ရှင်းရန်": {
  "en": "Buying & settlements",
  "zh": "采购与结算"
 },
 "အဝယ် ငွေလဲ မှတ်တမ်း ပြင်": {
  "en": "Edit purchase currency note",
  "zh": "编辑采购币种备注"
 },
 "အဝယ် စစ်ပြီး မှတ်": {
  "en": "Check purchases",
  "zh": "核对采购"
 },
 "အဝယ် ပယ်ဖျက်": {
  "en": "Cancel purchases",
  "zh": "取消采购"
 },
 "အဝယ် မှတ်တမ်း ကြည့်": {
  "en": "View purchases",
  "zh": "查看采购"
 },
 "အဝယ် အပို ကုန်ကျစရိတ် ပြင်": {
  "en": "Edit purchase extra cost",
  "zh": "编辑采购附加费用"
 },
 "အဝယ် အုပ်စု ရွေး": {
  "en": "Set buy group tab",
  "zh": "设置采购分组"
 },
 "အသုံးစရိတ် ကြည့်": {
  "en": "View expenses",
  "zh": "查看开支"
 },
 "အသုံးစရိတ် မှတ်/ပြင်/ဖျက်": {
  "en": "Manage expenses",
  "zh": "管理开支"
 },
 "အသုံးပြုသူ": {
  "en": "Users",
  "zh": "用户"
 },
 "အသုံးပြုသူ အရေအတွက်": {
  "en": "User count",
  "zh": "用户数"
 },
 "အသုံးပြုသူများ စီမံ": {
  "en": "Manage users",
  "zh": "管理用户"
 },
 "အဲဒီ ဌာန ရှိပြီးသား ဖြစ်နေတယ်": {
  "en": "That department already exists",
  "zh": "该部门已存在"
 },
 "အဲဒီ အခန်း ရှိပြီးသား ဖြစ်နေတယ်": {
  "en": "That role already exists",
  "zh": "该角色已存在"
 },
 "အခန်း အမျိုးအစား": {
  "en": "Role type",
  "zh": "角色类型"
 },
 # v78 Offline Delivery (server t() strings on delivery_offline.html).
 "ဒီ စာမျက်နှာကို လိုင်း ရှိတုန်း တစ်ခါ ဖွင့်ထားမှ Offline သုံးလို့ ရမယ် — ပို့ရန် စာရင်း ဖုန်းထဲ သိမ်းထားမှ ဖြစ်တယ်": {
  "en": "Open this page once while online before you can use it offline — the delivery list must be saved on the phone first",
  "zh": "先在有网络时打开一次本页，才能离线使用 — 送货单必须先存到手机上"
 },
 "ပစ္စည်း အရေအတွက် ပြင်ရင် ကောက်ရတဲ့ ငွေကို ကိုယ်တိုင် ပြင်ပါ — ဈေး မပါလို့ အလိုလို မတွက်ဘူး": {
  "en": "If you change a delivered quantity, adjust the money collected by hand — no prices are shown, so it is not worked out automatically",
  "zh": "如果改了送货数量，请自己改收到的钱 — 这里不显示价格，所以不会自动算"
 },
 "ပို့မယ် / အတည်ပြု": {
  "en": "Deliver / Confirm",
  "zh": "送货 / 确认"
 },
 "ပို့မယ် / အတည်ပြု နှိပ်ပြီးရင် ပြင်လို့ မရတော့ဘူး — မှားရင် ရုံးကို ပြောပါ": {
  "en": "Once you tap Deliver / Confirm you cannot edit it any more — if it is wrong, tell the office",
  "zh": "点了“送货 / 确认”就不能再改了 — 如果错了，请告诉办公室"
 },
 "မပို့ဖြစ် — ဒီ ဘောက်ချာ ပို့လို့ မရဘူး": {
  "en": "Could not deliver — this invoice cannot be delivered",
  "zh": "没送成 — 这张发票送不了"
 },
 "⏳ တင်ရန် ကျန်တဲ့ ပို့ဆောင်ချက်များ": {
  "en": "⏳ Deliveries still to send",
  "zh": "⏳ 还没发送的送货记录"
 },
 "⚠️ တင်မဝင်တဲ့ အကြောင်းပြချက်": {
  "en": "⚠️ Reasons a delivery did not send",
  "zh": "⚠️ 没发送成功的原因"
 },
 "📍 နေရာ ပြန် ယူမယ်": {
  "en": "📍 Take the location again",
  "zh": "📍 重新获取位置"
 },
 "🔄 စာရင်း ပြန် ဆွဲမယ်": {
  "en": "🔄 Refresh the list",
  "zh": "🔄 重新拉取单子"
 },
 # v78 Offline Delivery (JS tf() strings on delivery_offline.html).
 "🟢 Online": {
  "en": "🟢 Online",
  "zh": "🟢 在线"
 },
 "🔴 Offline — သိမ်းထားတဲ့ စာရင်းနဲ့ အလုပ်ဖြစ်တယ်": {
  "en": "🔴 Offline — working from the saved list",
  "zh": "🔴 离线 — 用已保存的单子工作"
 },
 "ပို့ရန် ဘောက်ချာ မရှိဘူး (လိုင်း ရမှ စာရင်း ပြန် ဆွဲပါ)": {
  "en": "No invoices to deliver (refresh the list when you have signal)",
  "zh": "没有要送的发票（有网络时重新拉取单子）"
 },
 "တင်ရန် ကျန်တာ မရှိဘူး": {
  "en": "Nothing left to send",
  "zh": "没有要发送的了"
 },
 "တင်မဝင်တာ မရှိဘူး": {
  "en": "Nothing failed to send",
  "zh": "没有发送失败的"
 },
 "ပို့ချက် ဖြည့်မယ်": {
  "en": "Fill in the delivery",
  "zh": "填写送货记录"
 },
 "အတည်ပြုပြီး — ပြင်လို့ မရတော့": {
  "en": "Confirmed — cannot edit any more",
  "zh": "已确认 — 不能再改了"
 },
 "ပို့ဆောင်ချက် တင်မဝင်ဘူး": {
  "en": "Delivery did not send",
  "zh": "送货记录没发送成功"
 },
 "📍 နေရာ ယူနေတယ်…": {
  "en": "📍 Getting the location…",
  "zh": "📍 正在获取位置…"
 },
 "📍 နေရာ ရပြီ (± {acc} m)": {
  "en": "📍 Location got (± {acc} m)",
  "zh": "📍 位置已获取（± {acc} 米）"
 },
 "ပို့မယ် / အတည်ပြု ပြီးရင် ပြင်လို့ မရတော့ဘူး — သေချာပြီလား?": {
  "en": "Once you confirm Deliver / Confirm you cannot edit it — are you sure?",
  "zh": "点了“送货 / 确认”就不能再改了 — 确定吗？"
 },
 "မပို့ဖြစ်ရင် အကြောင်း ထည့်ပါ": {
  "en": "If it could not be delivered, add the reason",
  "zh": "如果没送成，请写上原因"
 },
 "အနည်းဆုံး ၁ မျိုး ပို့တဲ့ အရေအတွက် ထည့်ပါ — ဒါမှ မဟုတ် မပို့ဖြစ် လုပ်ပါ": {
  "en": "Enter a delivered quantity for at least 1 item — or mark it as could not deliver",
  "zh": "至少填 1 种货的送货数量 — 或者标记为没送成"
 },
 # v78 Offline Delivery (server rejection reasons returned by
 # /api/delivery-attempts via tr() — EN/ZH users would otherwise see
 # Burmese reasons in the phone's rejection notices).
 "မှတ်တမ်း ကုဒ် မပါဘူး": {
  "en": "Delivery record code missing",
  "zh": "送货记录编号缺失"
 },
 "ဘောက်ချာ ထဲမှာ မပါတဲ့ ပစ္စည်း ပါဝင်နေတယ်": {
  "en": "A product not on this invoice was included",
  "zh": "包含了这张发票上没有的商品"
 },
 "အရေအတွက် မှားနေတယ်": {
  "en": "Quantity is wrong",
  "zh": "数量不对"
 },
 "မပို့ဖြစ်တဲ့ အကြောင်း ထည့်ပါ": {
  "en": "Add the reason it could not be delivered",
  "zh": "请写上没送成的原因"
 },
 "ကောက်ငွေ မှားနေတယ်": {
  "en": "Collected money is wrong",
  "zh": "收到的钱不对"
 },
 "တင်လို့ မရဘူး — နောက်မှ ပြန်စမ်းပါ": {
  "en": "Could not send — try again later",
  "zh": "发送失败 — 请稍后再试"
 },
 "ပို့မှတ်တမ်း စာရင်း မပါဘူး": {
  "en": "No delivery record list was sent",
  "zh": "没有发送送货记录单"
 },
 # v79 Daily snapshot file (daily_file.html + /daily-file route).
 "📄 နေ့စဉ် ဖိုင်": {
  "en": "📄 Daily file",
  "zh": "📄 每日文件"
 },
 "ညနေ print ထုတ်ပြီးတိုင်း download ဆွဲ ထားပါ — ဖုန်းရဲ့ Files ထဲက offline ဖွင့်လို့ ရတယ်၊ download ဆွဲတဲ့ အချိန်က ပုံရိပ် ပဲ။": {
  "en": "Download it each evening after printing — it opens offline from the phone's Files app, and it is a snapshot of the moment you download it.",
  "zh": "每天傍晚打印后下载一份 — 可以离线从手机的文件里打开，它是你下载那一刻的快照。"
 },
 "ရက် ရွေး": {
  "en": "Choose a date",
  "zh": "选择日期"
 },
 "ဖိုင် Download ဆွဲမယ်": {
  "en": "Download the file",
  "zh": "下载文件"
 },
 "ရက် ပုံစံ မှားနေတယ် — YYYY-MM-DD နဲ့ ရေးပါ": {
  "en": "Date format is wrong — write it as YYYY-MM-DD",
  "zh": "日期格式不对 — 请写成 YYYY-MM-DD"
 },
 "အနာဂတ် ရက် ကို ရွေး လို့ မရဘူး": {
  "en": "You cannot choose a future date",
  "zh": "不能选择未来的日期"
 },
}
# ==== TRANS END ====

def tr(s, lang=None, **kw):
    """Translate a UI string into lang ('my'/'en'/'zh'); falls back to the key."""
    if s is None:
        return ''
    if lang is None:
        lang = getattr(g, 'lang', 'my')
    e = TRANS.get(s)
    out = e.get(lang, s) if e else s
    if kw:
        try:
            out = out.format(**kw)
        except Exception:
            pass
    return out

LANGS = ('my', 'en', 'zh')
# v95: native labels for the language dropdown, in LANGS order. Adding a
# language later = one entry here (and in LANGS) + its TRANS translations;
# the dropdown in base.html renders from this list, so no template change
# is needed when a language is added.
LANG_LABELS = {'my': 'မြန်မာ', 'en': 'English', 'zh': '中文'}

def resolve_lang():
    """Current request language: the signed-in user's saved choice first
    (users.lang), else the pre-login session/cookie choice, else 'my'."""
    u = None
    try:
        u = current_user()
    except Exception:
        u = None
    if u is not None:
        try:
            v = u['lang']
        except Exception:
            v = None
        if v in LANGS:
            return v
        return 'my'
    v = session.get('lang') or request.cookies.get('gtm_lang')
    return v if v in LANGS else 'my'

@app.before_request
def _apply_lang():
    g.lang = resolve_lang()

# Keys whose strings are built by inline JS (catalog PWA, order form, small
# page scripts). base.html renders them as window.GTM_T in the page language.
JS_KEYS = ['လက်လီ', 'လက်ကား', 'လျှော့', 'ဒီနေ့', 'လွန်ခဲ့တဲ့ {d} ရက်', '{tier} ဈေး {dir} · {when}', 'ကျသွား', 'တက်သွား', '🟢 Online — နောက်ဆုံး ဈေးတွေ ရထားတယ်', '🔴 Offline — သိမ်းထားတဲ့ ဈေးတွေနဲ့ အလုပ်ဖြစ်တယ်', '▼ ပိတ်ရန်', '▲ ဖွင့်ရန်', '{n} မျိုး · {t} Ks', '({n} ခု တင်ရန် ကျန်တယ်)', 'ဆိုင် နဲ့ ပစ္စည်း လိုပါတယ်', 'ဘောက်ချာ တင်ပြီးပြီ: {code}', 'လိုင်း မရှိလို့ ဖုန်းထဲ သိမ်းထားတယ် — လိုင်း ရရင် ပြန် တင်ပါ', 'လိုင်း မရှိသေးဘူး', 'တစ်ချို့ မရသေးဘူး — နောက်မှ ပြန်စမ်းပါ', 'တင်ပြီးပြီ ✅', 'ပြင်ချက် {ok} ခု တင်ပြီးပြီ ✓', ' · {rej} ခု မဝင်လိုက် ⚠️ (အောက်မှာ အကြောင်းပြ ထားတယ်)', 'ပြင်ချက် မဝင်လိုက်ဘူး', 'သိပြီ', '✏️ ပြင်ချက် {n} ခု တင်ရန် ကျန်တယ် — လိုင်း ရရင် ပြန် တင်ပါ', 'မပို့ရသေး တာ မရှိဘူး', 'ပစ္စည်း {n} မျိုး', '✏️ ပြင်မယ်', '🗑 ဖျက်မယ်', 'ဒီနေ့/မနေ့က မိမိ ဘောက်ချာ မရှိဘူး (လိုင်း ရမှ နောက်ဆုံး အခြေအနေ ဆွဲယူမယ်)', 'အသစ်', 'ပို့ပြီး', 'ပယ်ဖျက်', ' · ငွေရပြီး', ' · 🖨 ပုံနှိပ်ပြီး', ' · ပြင်ခွင့် သုံးပြီး {n}/2', '✏️ ပြင်ချက် တင်ရန် ကျန်တယ်', '👁 ကြည့်မယ်', '(ပြင်လို့ မရတော့)', 'ဒီ မပို့ရသေး ဘောက်ချာကို ဖျက်မှာလား?', 'စုစုပေါင်း', 'ဖြုတ်', 'ပစ္စည်း ထပ်ထည့်', 'ဈေး အမျိုးအစား', '+ ထည့်မယ်', '💾 သိမ်းမယ်', 'မပြင်တော့', 'ပစ္စည်း မကျန်တော့ပါ — အနည်းဆုံး ၁ မျိုး ကျန်ရမယ်', 'ပြင်ပြီးပြီ — ဒီ ဘောက်ချာ တစ်စောင် အနေနဲ့ ပဲ တင်မယ်', 'ပြင်ချက် သိမ်းထားပြီ — လိုင်း ရရင် တင်မယ် (စည်းမျဉ်း စစ်ပြီးမှ အတည်ပြုမယ်)', 'ရုံး တည်နေရာ ဖျက်မှာလား?', 'ဒီ browser မှာ GPS မရဘူး', 'GPS မရဘူး — Location ဖွင့်ပြီး ထပ်စမ်းပါ', '👁 ပြမယ်', '🙈 ဖျောက်မယ်', 'ပစ္စည်းကို စာရင်းထဲက ရွေးပါ', 'ပစ္စည်း ရှာရန် (နာမည် / SKU)', 'ဘယ် ဆိုင် လဲ?', 'မရွေးတော့', 'ဆိုင် မသဲကွဲ', 'ဆိုင် ရွေး တွဲ', 'အသံ နီး တဲ့ ဆိုင်', 'အနီး မှာ "{name}" ရှိ တယ် — ဒီ ဆိုင် ပဲ လား?', 'ဒီ ဆိုင် ပဲ', 'ဆိုင် အသစ် ပဲ', '🟢 Online', '🔴 Offline — သိမ်းထားတဲ့ စာရင်းနဲ့ အလုပ်ဖြစ်တယ်', 'ပို့ရန် ဘောက်ချာ မရှိဘူး (လိုင်း ရမှ စာရင်း ပြန် ဆွဲပါ)', 'တင်ရန် ကျန်တာ မရှိဘူး', 'တင်မဝင်တာ မရှိဘူး', 'ပို့ချက် ဖြည့်မယ်', 'အတည်ပြုပြီး — ပြင်လို့ မရတော့', 'ပို့ဆောင်ချက် တင်မဝင်ဘူး', '📍 နေရာ ယူနေတယ်…', '📍 နေရာ ရပြီ (± {acc} m)', 'ပို့မယ် / အတည်ပြု ပြီးရင် ပြင်လို့ မရတော့ဘူး — သေချာပြီလား?', 'မပို့ဖြစ်ရင် အကြောင်း ထည့်ပါ', 'အနည်းဆုံး ၁ မျိုး ပို့တဲ့ အရေအတွက် ထည့်ပါ — ဒါမှ မဟုတ် မပို့ဖြစ် လုပ်ပါ', 'Cash', 'Digital Pay', 'Banking', 'ကောက်ရတဲ့ ငွေ (Ks)', 'မှာတဲ့ အရေအတွက်', 'မပို့ဖြစ် — ဘောက်ချာ ပယ်ဖျက်']


# v69 (Ko, 2026-10-24): the full outlet-type taxonomy, in his order.
# ONE list drives every surface — outlet edit, outlets filter, Catalog
# checkout choice, office order form choice, /sales-report rows. No
# surface hardcodes its own copy. Keys are stable storage values; the
# second item is the TRANS label key (legacy retail/wholesale keep
# their existing '… ဆိုင်' labels; the new types are self-contained
# shop nouns). No schema change: shop_type stays TEXT, NULL = unset.
SHOP_TYPES = [
    ('retail', 'လက်လီ ဆိုင်'),
    ('wholesale', 'လက်ကား ဆိုင်'),
    ('area_wholesale', 'နယ်မြေလက်ကား'),
    ('trading', 'ရောင်းဝယ်ရေး'),
    ('cosmetics', 'အလှကုန်'),
    ('cosmetics_wholesale', 'အလှကုန်လက်ကား'),
    ('cold_drinks', 'အအေးဆိုင်'),
    ('coffee', 'ကော်ဖီဆိုင်'),
    ('tea', 'လက်ဖက်ရည်ဆိုင်'),
    ('food_variety', 'စားဖွယ်စုံဆိုင်'),
    ('snack', 'အဆာပြေဆိုင်'),
    ('restaurant', 'စားသောက်ဆိုင်'),
    ('entertainment', 'ဖျော်ဖြေရေးဆိုင်'),
]
SHOP_TYPE_KEYS = [k for k, _ in SHOP_TYPES]

def shop_type_ctx():
    """Template context for any surface that shows shop types."""
    return {'shop_types': SHOP_TYPES,
            'st_labels': {k: tr(lbl) for k, lbl in SHOP_TYPES}}


# ---------------- DB ----------------
def db():
    if 'db' not in g:
        g.db = sqlite3.connect(DB_PATH)
        g.db.row_factory = sqlite3.Row
        g.db.execute('PRAGMA foreign_keys=ON')
    return g.db

@app.teardown_appcontext
def close_db(exc):
    d = g.pop('db', None)
    if d is not None:
        d.close()

SCHEMA = """
CREATE TABLE IF NOT EXISTS users(
  id INTEGER PRIMARY KEY, username TEXT UNIQUE NOT NULL, pw_hash TEXT NOT NULL,
  name TEXT NOT NULL, role TEXT NOT NULL, role_id INTEGER, department_id INTEGER, active INTEGER DEFAULT 1, created_at TEXT);
CREATE TABLE IF NOT EXISTS products(
  id INTEGER PRIMARY KEY, sku TEXT UNIQUE NOT NULL, category TEXT, name TEXT NOT NULL,
  unit TEXT, upc REAL, supplier TEXT, base_price REAL DEFAULT 0, actual_base REAL,
  retail REAL DEFAULT 0, wholesale REAL DEFAULT 0, b2c REAL DEFAULT 0,
  image TEXT, active INTEGER DEFAULT 1, legacy_status TEXT,
  pool_id INTEGER, pack_factor REAL DEFAULT 1);
CREATE TABLE IF NOT EXISTS pack_dismissed(
  small_id INTEGER, big_id INTEGER, dismissed_by INTEGER, created_at TEXT,
  PRIMARY KEY(small_id, big_id));
CREATE TABLE IF NOT EXISTS stock(
  product_id INTEGER PRIMARY KEY REFERENCES products(id), qty REAL DEFAULT 0);
CREATE TABLE IF NOT EXISTS stock_moves(
  id INTEGER PRIMARY KEY, product_id INTEGER NOT NULL REFERENCES products(id),
  delta REAL NOT NULL, kind TEXT NOT NULL, ref TEXT, note TEXT,
  by_user INTEGER, created_at TEXT);
CREATE TABLE IF NOT EXISTS purchases(
  id INTEGER PRIMARY KEY, code TEXT UNIQUE, supplier TEXT, invoice_no TEXT,
  pdate TEXT, status TEXT DEFAULT 'pending_check', created_by INTEGER,
  checked_by INTEGER, note TEXT, created_at TEXT);
CREATE TABLE IF NOT EXISTS purchase_items(
  id INTEGER PRIMARY KEY, purchase_id INTEGER NOT NULL REFERENCES purchases(id),
  product_id INTEGER NOT NULL REFERENCES products(id), qty REAL NOT NULL, cost REAL DEFAULT 0);
CREATE TABLE IF NOT EXISTS orders(
  id INTEGER PRIMARY KEY, code TEXT UNIQUE, client_uuid TEXT UNIQUE,
  outlet TEXT NOT NULL, area TEXT, sales_user INTEGER, odate TEXT,
  status TEXT DEFAULT 'new', paid INTEGER DEFAULT 0, discount REAL DEFAULT 0,
  note TEXT, offline INTEGER DEFAULT 0, overstock INTEGER DEFAULT 0, created_at TEXT,
  printed_at TEXT, team TEXT, outlet_id INTEGER,
  assigned_to INTEGER, assigned_at TEXT, cancel_reason TEXT, partial INTEGER DEFAULT 0);
CREATE TABLE IF NOT EXISTS order_items(
  id INTEGER PRIMARY KEY, order_id INTEGER NOT NULL REFERENCES orders(id),
  product_id INTEGER NOT NULL REFERENCES products(id), qty REAL NOT NULL,
  price REAL NOT NULL, tier TEXT, discount REAL DEFAULT 0,
  cost_act REAL, cost_base REAL, orig_qty REAL, cancelled_qty REAL DEFAULT 0);
CREATE TABLE IF NOT EXISTS returns(
  id INTEGER PRIMARY KEY, order_id INTEGER REFERENCES orders(id),
  product_id INTEGER NOT NULL REFERENCES products(id), qty REAL NOT NULL,
  kind TEXT NOT NULL, reason TEXT, amount REAL DEFAULT 0,
  status TEXT DEFAULT 'recorded', by_user INTEGER, checked_by INTEGER, created_at TEXT);
CREATE TABLE IF NOT EXISTS areas(id INTEGER PRIMARY KEY, name TEXT UNIQUE);
CREATE TABLE IF NOT EXISTS counters(name TEXT PRIMARY KEY, val INTEGER DEFAULT 0);
CREATE TABLE IF NOT EXISTS order_requests(
  id INTEGER PRIMARY KEY, code TEXT UNIQUE, supplier TEXT, status TEXT DEFAULT 'open',
  note TEXT, cancel_reason TEXT, created_by INTEGER, cancelled_by INTEGER, created_at TEXT,
  kind TEXT NOT NULL DEFAULT 'demand');
CREATE TABLE IF NOT EXISTS order_request_items(
  id INTEGER PRIMARY KEY, or_id INTEGER NOT NULL REFERENCES order_requests(id),
  product_id INTEGER NOT NULL REFERENCES products(id), qty REAL NOT NULL, received_qty REAL DEFAULT 0);
CREATE TABLE IF NOT EXISTS buy_form_tokens(
  token TEXT PRIMARY KEY, user_id INTEGER, or_id INTEGER, created_at TEXT);
CREATE TABLE IF NOT EXISTS pay_form_tokens(
  token TEXT PRIMARY KEY, user_id INTEGER, order_id INTEGER, created_at TEXT);
CREATE TABLE IF NOT EXISTS restore_form_tokens(
  token TEXT PRIMARY KEY, user_id INTEGER, created_at TEXT);
CREATE TABLE IF NOT EXISTS order_edits(
  id INTEGER PRIMARY KEY, order_id INTEGER NOT NULL REFERENCES orders(id),
  by_user INTEGER, summary TEXT, created_at TEXT);
CREATE TABLE IF NOT EXISTS settlements(
  id INTEGER PRIMARY KEY, code TEXT UNIQUE, or_id INTEGER, pu_id INTEGER,
  supplier TEXT, status TEXT DEFAULT 'draft',
  adjust REAL DEFAULT 0, adjust_note TEXT, final_amount REAL,
  prepared_by INTEGER, sent_at TEXT, confirmed_at TEXT,
  paid_at TEXT, pay_method TEXT, pay_ref TEXT, paid_by INTEGER, created_at TEXT,
  pay_denom TEXT);
CREATE TABLE IF NOT EXISTS price_history(
  id INTEGER PRIMARY KEY, product_id INTEGER NOT NULL REFERENCES products(id),
  field TEXT, old_price REAL, new_price REAL, by_user INTEGER, created_at TEXT);
CREATE TABLE IF NOT EXISTS order_payments(
  id INTEGER PRIMARY KEY, order_id INTEGER NOT NULL REFERENCES orders(id),
  amount REAL NOT NULL, rdate TEXT, note TEXT, by_user INTEGER, created_at TEXT,
  method TEXT DEFAULT 'cash', verified INTEGER DEFAULT 1);
CREATE TABLE IF NOT EXISTS delivery_attempts(
  id INTEGER PRIMARY KEY, order_id INTEGER NOT NULL REFERENCES orders(id),
  by_user INTEGER, result TEXT, reason TEXT, note TEXT,
  lat REAL, lng REAL, acc REAL, created_at TEXT, denom TEXT);
CREATE TABLE IF NOT EXISTS delivery_handovers(
  id INTEGER PRIMARY KEY, deliverer_id INTEGER NOT NULL, hdate TEXT,
  handed REAL DEFAULT 0, verified_by INTEGER, note TEXT, created_at TEXT);
CREATE TABLE IF NOT EXISTS app_settings(k TEXT PRIMARY KEY, v TEXT);
-- v42: Market Analysis module removed (Ko, 2026-10-18). The market_prices /
-- market_briefs tables are intentionally no longer created here; on existing
-- databases they remain in place, dormant and untouched, with their rows.
CREATE TABLE IF NOT EXISTS expenses(
 id INTEGER PRIMARY KEY, edate TEXT, category TEXT, detail TEXT,
 amount REAL DEFAULT 0, by_user INTEGER, created_at TEXT, denom TEXT
);
CREATE TABLE IF NOT EXISTS supplier_state(
 supplier TEXT PRIMARY KEY, closed INTEGER DEFAULT 0, updated_by INTEGER, updated_at TEXT
);
CREATE TABLE IF NOT EXISTS outlets(
 id INTEGER PRIMARY KEY, name TEXT NOT NULL, area TEXT,
 lat REAL, lng REAL, gps_n INTEGER DEFAULT 0,
 created_by INTEGER, created_at TEXT, first_seen TEXT, note TEXT,
 UNIQUE(name, area)
);
CREATE TABLE IF NOT EXISTS visits(
 id INTEGER PRIMARY KEY, outlet_id INTEGER NOT NULL, user_id INTEGER NOT NULL,
 vdate TEXT NOT NULL, kind TEXT DEFAULT 'probable', order_id INTEGER,
 lat REAL, lng REAL, acc REAL, client_uuid TEXT, created_at TEXT,
 UNIQUE(user_id, outlet_id, vdate)
);
CREATE TABLE IF NOT EXISTS risk_marks(
 order_id INTEGER NOT NULL, product_id INTEGER NOT NULL, cur_act REAL,
 marked_by INTEGER, marked_at TEXT, PRIMARY KEY(order_id, product_id)
);
CREATE TABLE IF NOT EXISTS price_review_marks(
 product_id INTEGER PRIMARY KEY, marked_act REAL,
 marked_by INTEGER, marked_at TEXT
);
CREATE TABLE IF NOT EXISTS offline_edit_log(
 client_edit_uuid TEXT PRIMARY KEY, order_id INTEGER, result TEXT, reason TEXT,
 by_user INTEGER, created_at TEXT
);
CREATE TABLE IF NOT EXISTS supplier_returns(
  id INTEGER PRIMARY KEY, code TEXT UNIQUE, or_id INTEGER, pu_id INTEGER,
  supplier TEXT, product_id INTEGER NOT NULL REFERENCES products(id),
  qty REAL NOT NULL, cost REAL DEFAULT 0, reason TEXT, by_user INTEGER, created_at TEXT);
-- v49: money book (manual lines + counted-cash close), stock counts,
-- the Ask layer's market prices, and the once-only shop exchange record.
CREATE TABLE IF NOT EXISTS money_entries(
 id INTEGER PRIMARY KEY, edate TEXT, grp TEXT, direction TEXT,
 amount REAL DEFAULT 0, note TEXT, by_user INTEGER, created_at TEXT);
CREATE TABLE IF NOT EXISTS cash_counts(
 cdate TEXT PRIMARY KEY, amount REAL DEFAULT 0, by_user INTEGER, created_at TEXT,
 denom TEXT);
-- v70: cashbox (Ko, 2026-10-25) — ONE box, ONE receipt list, ONE issue
-- list. One row per active day; the chain is Opening = previous day's
-- COUNTED closing (counted wins over book), the first day is a manual
-- counted seed, closed days are frozen snapshots, corrections land as
-- adjustment entries on the current open day. Auto-feed is computed
-- from the ledgers at read time (never stored rows); only manual
-- entries (incl. top-ups) are stored, each carrying kind/direction/
-- destination/date so future groupings are views, not a restructure.
CREATE TABLE IF NOT EXISTS cashbox_days(
 cdate TEXT PRIMARY KEY, opening REAL, status TEXT DEFAULT 'open',
 book_closing REAL, counted REAL, diff REAL, diff_reason TEXT,
 counted_denom TEXT, counted_by INTEGER, verified_by INTEGER,
 verified_at TEXT, created_at TEXT);
CREATE TABLE IF NOT EXISTS cashbox_entries(
 id INTEGER PRIMARY KEY, edate TEXT, kind TEXT, direction TEXT,
 dest TEXT, amount REAL DEFAULT 0, note TEXT, by_user INTEGER, created_at TEXT);
CREATE TABLE IF NOT EXISTS stock_counts(
 id INTEGER PRIMARY KEY, cdate TEXT, status TEXT DEFAULT 'draft',
 created_by INTEGER, approved_by INTEGER, created_at TEXT, approved_at TEXT);
CREATE TABLE IF NOT EXISTS stock_count_lines(
 id INTEGER PRIMARY KEY, count_id INTEGER NOT NULL REFERENCES stock_counts(id),
 product_id INTEGER NOT NULL, counted REAL);
CREATE TABLE IF NOT EXISTS ask_market_prices(
 id INTEGER PRIMARY KEY, product_id INTEGER NOT NULL REFERENCES products(id),
 town TEXT, kind TEXT DEFAULT 'sell', price REAL DEFAULT 0, pdate TEXT,
 source TEXT, note TEXT, by_user INTEGER, created_at TEXT);
CREATE TABLE IF NOT EXISTS outlet_exchanges(
 id INTEGER PRIMARY KEY, outlet_id INTEGER, outlet_name TEXT,
 product_id INTEGER NOT NULL REFERENCES products(id), qty REAL NOT NULL,
 edate TEXT, return_id INTEGER, by_user INTEGER, created_at TEXT);
-- v68: alias spellings for outlets (one shop written more than one way —
-- 'Sut' vs 'ဆွတ်') and the possible-duplicate dismissals. An alias text
-- belongs to ONE outlet only (alias_norm is the case-insensitive,
-- whitespace-collapsed form and is UNIQUE). Dismissals store the
-- ordered id pair of two pinned shops a person confirmed are DIFFERENT.
CREATE TABLE IF NOT EXISTS outlet_aliases(
 id INTEGER PRIMARY KEY, outlet_id INTEGER NOT NULL REFERENCES outlets(id),
 alias TEXT NOT NULL, alias_norm TEXT NOT NULL UNIQUE,
 created_by INTEGER, created_at TEXT);
CREATE TABLE IF NOT EXISTS outlet_dup_dismissals(
 id INTEGER PRIMARY KEY, outlet_a INTEGER NOT NULL, outlet_b INTEGER NOT NULL,
 by_user INTEGER, created_at TEXT, UNIQUE(outlet_a, outlet_b));
"""

def init_db():
    os.makedirs(os.path.dirname(DB_PATH), exist_ok=True)
    con = sqlite3.connect(DB_PATH)
    con.executescript(SCHEMA)
    # migrations (safe on existing databases)
    ucols = [r[1] for r in con.execute("PRAGMA table_info(users)")]
    if 'team' not in ucols:
        con.execute("ALTER TABLE users ADD COLUMN team TEXT")
    ocols = [r[1] for r in con.execute("PRAGMA table_info(orders)")]
    if 'team' not in ocols:
        con.execute("ALTER TABLE orders ADD COLUMN team TEXT")
    ecols = [r[1] for r in con.execute("PRAGMA table_info(order_edits)")]
    if 'by_role' not in ecols:
        con.execute("ALTER TABLE order_edits ADD COLUMN by_role TEXT")
        con.execute("UPDATE order_edits SET by_role=(SELECT role FROM users WHERE users.id=order_edits.by_user) WHERE by_role IS NULL")
    wcols = [r[1] for r in con.execute("PRAGMA table_info(users)")]
    if 'last_edit_seen' not in wcols:
        con.execute("ALTER TABLE users ADD COLUMN last_edit_seen INTEGER DEFAULT 0")
    lcols = [r[1] for r in con.execute("PRAGMA table_info(users)")]
    if lcols and 'lang' not in lcols:
        con.execute("ALTER TABLE users ADD COLUMN lang TEXT DEFAULT 'my'")
    prcols = [r[1] for r in con.execute("PRAGMA table_info(products)")]
    if prcols and 'sellable' not in prcols:
        con.execute("ALTER TABLE products ADD COLUMN sellable INTEGER DEFAULT 1")
        con.execute("UPDATE products SET sellable=0 WHERE legacy_status='Out Of Stock'")
    if prcols and 'pool_id' not in prcols:
        con.execute("ALTER TABLE products ADD COLUMN pool_id INTEGER")
    if prcols and 'pack_factor' not in prcols:
        con.execute("ALTER TABLE products ADD COLUMN pack_factor REAL DEFAULT 1")
    ucols = [r[1] for r in con.execute("PRAGMA table_info(users)")]
    if ucols and 'is_leader' not in ucols:
        con.execute("ALTER TABLE users ADD COLUMN is_leader INTEGER DEFAULT 0")
    ocols2 = [r[1] for r in con.execute("PRAGMA table_info(orders)")]
    if ocols2 and 'printed_at' not in ocols2:
        con.execute("ALTER TABLE orders ADD COLUMN printed_at TEXT")
    scols = [r[1] for r in con.execute("PRAGMA table_info(settlements)")]
    if scols and 'figures_json' not in scols:
        con.execute("ALTER TABLE settlements ADD COLUMN figures_json TEXT")
    pcols = [r[1] for r in con.execute("PRAGMA table_info(purchases)")]
    if 'or_id' not in pcols:
        con.execute("ALTER TABLE purchases ADD COLUMN or_id INTEGER")
    if 'cancel_reason' not in pcols:
        con.execute("ALTER TABLE purchases ADD COLUMN cancel_reason TEXT")
    if 'cancelled_by' not in pcols:
        con.execute("ALTER TABLE purchases ADD COLUMN cancelled_by INTEGER")
    oicols = [r[1] for r in con.execute("PRAGMA table_info(order_items)")]
    if oicols and 'discount' not in oicols:
        con.execute("ALTER TABLE order_items ADD COLUMN discount REAL DEFAULT 0")
    if oicols and 'cost_act' not in oicols:
        con.execute("ALTER TABLE order_items ADD COLUMN cost_act REAL")
    if oicols and 'cost_base' not in oicols:
        con.execute("ALTER TABLE order_items ADD COLUMN cost_base REAL")
    # v42: products.chain_towns / chain_note columns are kept in the schema for
    # data compatibility only — the Market module was removed, so nothing in
    # the code reads or writes them any more.
    prcols2 = [r[1] for r in con.execute("PRAGMA table_info(products)")]
    if prcols2 and 'chain_towns' not in prcols2:
        con.execute("ALTER TABLE products ADD COLUMN chain_towns TEXT DEFAULT ''")
    if prcols2 and 'chain_note' not in prcols2:
        con.execute("ALTER TABLE products ADD COLUMN chain_note TEXT DEFAULT ''")
    ocols3 = [r[1] for r in con.execute("PRAGMA table_info(orders)")]
    if ocols3 and 'outlet_id' not in ocols3:
        con.execute("ALTER TABLE orders ADD COLUMN outlet_id INTEGER")
    # v47: payment method groups + verification; delivery assignment; shortfall
    # bookkeeping. Guarded adds only — no existing balance or status changes.
    pmcols = [r[1] for r in con.execute("PRAGMA table_info(order_payments)")]
    if pmcols and 'method' not in pmcols:
        con.execute("ALTER TABLE order_payments ADD COLUMN method TEXT DEFAULT 'cash'")
    if pmcols and 'verified' not in pmcols:
        con.execute("ALTER TABLE order_payments ADD COLUMN verified INTEGER DEFAULT 1")
    ocols4 = [r[1] for r in con.execute("PRAGMA table_info(orders)")]
    for _col, _ddl in (('assigned_to', "ALTER TABLE orders ADD COLUMN assigned_to INTEGER"),
                       ('assigned_at', "ALTER TABLE orders ADD COLUMN assigned_at TEXT"),
                       ('cancel_reason', "ALTER TABLE orders ADD COLUMN cancel_reason TEXT"),
                       ('partial', "ALTER TABLE orders ADD COLUMN partial INTEGER DEFAULT 0")):
        if ocols4 and _col not in ocols4:
            con.execute(_ddl)
    oicols2 = [r[1] for r in con.execute("PRAGMA table_info(order_items)")]
    for _col, _ddl in (('orig_qty', "ALTER TABLE order_items ADD COLUMN orig_qty REAL"),
                       ('cancelled_qty', "ALTER TABLE order_items ADD COLUMN cancelled_qty REAL DEFAULT 0")):
        if oicols2 and _col not in oicols2:
            con.execute(_ddl)
    # v49: guarded adds only — expense money group, per-batch expiry dates,
    # the credit-guard flag on orders, and the optional per-shop credit limit.
    excols = [r[1] for r in con.execute("PRAGMA table_info(expenses)")]
    if excols and 'method' not in excols:
        con.execute("ALTER TABLE expenses ADD COLUMN method TEXT DEFAULT 'cash'")
    pcols2 = [r[1] for r in con.execute("PRAGMA table_info(purchase_items)")]
    if pcols2 and 'expiry_date' not in pcols2:
        con.execute("ALTER TABLE purchase_items ADD COLUMN expiry_date TEXT")
    ocols5 = [r[1] for r in con.execute("PRAGMA table_info(orders)")]
    if ocols5 and 'credit_warn' not in ocols5:
        con.execute("ALTER TABLE orders ADD COLUMN credit_warn INTEGER DEFAULT 0")
    oucols = [r[1] for r in con.execute("PRAGMA table_info(outlets)")]
    if oucols and 'credit_limit' not in oucols:
        con.execute("ALTER TABLE outlets ADD COLUMN credit_limit REAL")
    # v65: outlet shop type (Ko, 2026-10-23) — DATA for the sales
    # report's per-rep type split. NULL = not set yet; the Catalog does
    # NOT switch anything on it (that change stays held).
    if oucols and 'shop_type' not in oucols:
        con.execute("ALTER TABLE outlets ADD COLUMN shop_type TEXT")
    # v67 (Ko, 2026-10-23): outlet IDENTITY is (name, area) — two genuinely
    # different shops may share a name, and the old UNIQUE(name) forced
    # them into ONE record (invoices, credit and shop type all merged).
    # Rebuild the table without it, adding the distinguishing note
    # column; every id and every orders.outlet_id link is preserved.
    # Safety: a snapshot is taken first (the v49 backup helper; a plain
    # dated file copy as fallback), the rebuild runs in ONE transaction,
    # and the outlet + order-link counts are verified before commit —
    # any mismatch aborts the start loudly instead of half-migrating.
    _osql67 = con.execute("SELECT sql FROM sqlite_master WHERE type='table' AND name='outlets'").fetchone()
    _ocols67 = [r[1] for r in con.execute("PRAGMA table_info(outlets)")]
    if _ocols67 and _osql67 and _osql67[0] and (re.search(r'\bname\s+TEXT\s+UNIQUE', _osql67[0]) or 'note' not in _ocols67):
        _n_out67 = con.execute("SELECT COUNT(*) FROM outlets").fetchone()[0]
        _n_link67 = con.execute("SELECT COUNT(*) FROM orders WHERE outlet_id IS NOT NULL").fetchone()[0]
        con.commit()
        try:
            make_backup_copy()
        except Exception:
            try:
                shutil.copyfile(DB_PATH, DB_PATH + '.pre-v67-' + today())
            except Exception:
                pass
        _info67 = con.execute("PRAGMA table_info(outlets)").fetchall()
        _defs67 = []
        for _r67 in _info67:
            _d67 = '"%s" %s' % (_r67[1], _r67[2] or 'TEXT')
            if _r67[5]: _d67 += ' PRIMARY KEY'
            if _r67[3]: _d67 += ' NOT NULL'
            if _r67[4] is not None: _d67 += ' DEFAULT ' + _r67[4]
            _defs67.append(_d67)
        _cols67 = [r[1] for r in _info67]
        if 'note' not in _cols67:
            _defs67.append('"note" TEXT')
            _sel67 = ', '.join('"%s"' % c for c in _cols67) + ', NULL'
            _cols67 = _cols67 + ['note']
        else:
            _sel67 = ', '.join('"%s"' % c for c in _cols67)
        con.execute("BEGIN")
        try:
            con.execute("CREATE TABLE outlets_new(%s, UNIQUE(name, area))" % ', '.join(_defs67))
            con.execute("INSERT INTO outlets_new(%s) SELECT %s FROM outlets" % (', '.join('"%s"' % c for c in _cols67), _sel67))
            if con.execute("SELECT COUNT(*) FROM outlets_new").fetchone()[0] != _n_out67:
                raise RuntimeError('v67 outlet rebuild: outlet row count changed')
            con.execute("DROP TABLE outlets")
            con.execute("ALTER TABLE outlets_new RENAME TO outlets")
            if con.execute("SELECT COUNT(*) FROM orders WHERE outlet_id IS NOT NULL").fetchone()[0] != _n_link67:
                raise RuntimeError('v67 outlet rebuild: order link count changed')
            con.commit()
        except Exception:
            con.rollback(); raise
    # v50: notification domains + the GPS place-check marks on delivery
    # attempts. Guarded, additive only — no existing row changes meaning:
    # old order_edits read as domain 'orders', old attempts as unflagged.
    ecols2 = [r[1] for r in con.execute("PRAGMA table_info(order_edits)")]
    if 'domain' not in ecols2:
        con.execute("ALTER TABLE order_edits ADD COLUMN domain TEXT DEFAULT 'orders'")
    if 'attempt_id' not in ecols2:
        con.execute("ALTER TABLE order_edits ADD COLUMN attempt_id INTEGER")
    acols = [r[1] for r in con.execute("PRAGMA table_info(delivery_attempts)")]
    for _col, _ddl in (('gps_flag', "ALTER TABLE delivery_attempts ADD COLUMN gps_flag INTEGER DEFAULT 0"),
                       ('gps_dist', "ALTER TABLE delivery_attempts ADD COLUMN gps_dist REAL"),
                       ('checked_by', "ALTER TABLE delivery_attempts ADD COLUMN checked_by INTEGER"),
                       ('checked_at', "ALTER TABLE delivery_attempts ADD COLUMN checked_at TEXT")):
        if _col not in acols:
            con.execute(_ddl)
    # v51: batch costing (per-purchase extra cost + per-product Risk %) and
    # the Mini Audit table. Guarded, additive only — no existing row changes
    # meaning: old purchases read as extra_cost 0, old products as risk 0.
    pucols3 = [r[1] for r in con.execute("PRAGMA table_info(purchases)")]
    if 'extra_cost' not in pucols3:
        con.execute("ALTER TABLE purchases ADD COLUMN extra_cost REAL DEFAULT 0")
    if 'extra_note' not in pucols3:
        con.execute("ALTER TABLE purchases ADD COLUMN extra_note TEXT")
    # v56: currency memo on purchases (Ko, 2026-10-22) — the foreign amount
    # paid and that day's rate, kept as evidence only. Guarded, additive:
    # old purchases read as no memo (all three NULL). These columns are
    # NEVER read by any total, settlement, profit or costing figure.
    if 'fx_currency' not in pucols3:
        con.execute("ALTER TABLE purchases ADD COLUMN fx_currency TEXT")
    if 'fx_amount' not in pucols3:
        con.execute("ALTER TABLE purchases ADD COLUMN fx_amount REAL")
    if 'fx_rate' not in pucols3:
        con.execute("ALTER TABLE purchases ADD COLUMN fx_rate REAL")
    prcols3 = [r[1] for r in con.execute("PRAGMA table_info(products)")]
    if 'risk_pct' not in prcols3:
        con.execute("ALTER TABLE products ADD COLUMN risk_pct REAL DEFAULT 0")
    # v59: request kind (Ko, 2026-10-22) — every Order Request is one of
    # the two ways a purchase starts: 'demand' (Demand Driven, from the
    # buy list) or 'direct' (Direct Driven, a trip pre-order). Guarded,
    # additive: existing requests read as 'demand' — historically they
    # all came through the demand flow. The kind is presentation/entry
    # only: on-order math, receiving, stock and settlements never read it.
    orcols59 = [r[1] for r in con.execute("PRAGMA table_info(order_requests)")]
    if orcols59 and 'kind' not in orcols59:
        con.execute("ALTER TABLE order_requests ADD COLUMN kind TEXT NOT NULL DEFAULT 'demand'")
    # v60: denomination breakdowns (Ko, 2026-10-22) — a JSON note-count
    # record of how cash was counted at the four cash points. Guarded,
    # additive: old rows read as NULL (no record). Evidence only — no
    # total, balance, verification or report ever reads these columns.
    for _tbl60, _col60 in (('delivery_attempts', 'denom'), ('cash_counts', 'denom'),
                           ('expenses', 'denom'), ('settlements', 'pay_denom')):
        _cols60 = [r[1] for r in con.execute(f"PRAGMA table_info({_tbl60})")]
        if _cols60 and _col60 not in _cols60:
            con.execute(f"ALTER TABLE {_tbl60} ADD COLUMN {_col60} TEXT")
    # v71 (Ko, 2026-10-25 — 'ရွက်ရေ အစ က မြင် ချင်'): denomination splits
    # from the very first record. Manual order payments never stored one
    # (v60 covered delivery/expenses/settlements only), cashbox manual
    # entries had no column, and the cashbox seed counter's split was
    # computed but discarded. All three are added here, guarded and
    # additive; old rows read as NULL (no record).
    for _tbl71, _col71 in (('order_payments', 'denom'), ('cashbox_entries', 'denom'),
                           ('cashbox_days', 'opening_denom')):
        _cols71 = [r[1] for r in con.execute(f"PRAGMA table_info({_tbl71})")]
        if _cols71 and _col71 not in _cols71:
            con.execute(f"ALTER TABLE {_tbl71} ADD COLUMN {_col71} TEXT")
    # v78 (Ko, 2026-10-25 — Offline Delivery): the idempotency key on
    # delivery attempts. A re-push of the same client_attempt_uuid must
    # NOT double-apply; the UNIQUE index is the backstop against a
    # concurrent double-push (NULLs stay distinct, so every pre-v78
    # online attempt is untouched). Guarded, additive only. Note the
    # asymmetry, deliberate: a REJECTED attempt stores no row, so a
    # corrected retry with the same uuid can still apply — only an
    # APPLIED (delivered / failed-cancel) attempt seals its uuid.
    _acols78 = [r[1] for r in con.execute("PRAGMA table_info(delivery_attempts)")]
    if _acols78:
        if 'client_uuid' not in _acols78:
            con.execute("ALTER TABLE delivery_attempts ADD COLUMN client_uuid TEXT")
        con.execute("CREATE UNIQUE INDEX IF NOT EXISTS idx_attempts_client_uuid ON delivery_attempts(client_uuid)")
    con.execute("""CREATE TABLE IF NOT EXISTS audit_events(
      id INTEGER PRIMARY KEY, created_at TEXT, user_id INTEGER, kind TEXT,
      summary TEXT, ref_type TEXT, ref_id INTEGER,
      notify INTEGER DEFAULT 0, handled_by INTEGER, handled_at TEXT)""")
    # outlets master: seed from existing order outlet names (idempotent)
    for (nm,) in con.execute("SELECT DISTINCT outlet FROM orders WHERE outlet IS NOT NULL AND TRIM(outlet)!=''").fetchall():
        first = con.execute("SELECT odate, sales_user, area FROM orders WHERE outlet=? ORDER BY odate, id LIMIT 1", (nm,)).fetchone()
        con.execute("INSERT OR IGNORE INTO outlets(name, area, created_by, created_at, first_seen) VALUES(?,?,?,?,?)",
                    (nm, (first[2] or '') if first else '', first[1] if first else None, now(), first[0] if first else None))
    # v67: link legacy orders by (name, area) first — a bare name may be
    # two shops now; fall back to the name only where it is unambiguous.
    con.execute("UPDATE orders SET outlet_id=(SELECT id FROM outlets WHERE outlets.name=orders.outlet AND COALESCE(outlets.area,'')=COALESCE(orders.area,'')) WHERE outlet_id IS NULL AND outlet IS NOT NULL AND TRIM(outlet)!=''")
    con.execute("UPDATE orders SET outlet_id=(SELECT id FROM outlets WHERE outlets.name=orders.outlet) WHERE outlet_id IS NULL AND outlet IS NOT NULL AND TRIM(outlet)!='' AND (SELECT COUNT(*) FROM outlets WHERE outlets.name=orders.outlet)=1")
    # backfill (2026-10-15): orders already flagged paid get one full payment record
    for (oid0,) in con.execute("SELECT id FROM orders WHERE paid=1").fetchall():
        if not con.execute("SELECT 1 FROM order_payments WHERE order_id=?", (oid0,)).fetchone():
            tot0 = con.execute("SELECT COALESCE(SUM(qty*price),0) - COALESCE((SELECT discount FROM orders WHERE id=?),0) FROM order_items WHERE order_id=?", (oid0, oid0)).fetchone()[0]
            if tot0 and tot0 > 0:
                con.execute("INSERT INTO order_payments(order_id, amount, rdate, note, by_user, created_at) VALUES(?,?,date('now','+390 minutes'),'အရင် paid မှတ်တမ်း',NULL,datetime('now','+390 minutes'))", (oid0, tot0))
    # v62 (performance): lookup indexes for the hot list/report queries
    # profiled in checks/perf_v62.py — order lines by their order (every
    # total/received sum), orders by status/date/rep/outlet (dashboards,
    # buy list, reports), payments by order, purchase lines by purchase,
    # price history by product (price review), visits by outlet/order,
    # stock moves and delivery attempts by their owners. CREATE INDEX
    # IF NOT EXISTS: idempotent, no data changes, safe on every start.
    for _ix in (
        "CREATE INDEX IF NOT EXISTS idx_order_items_order ON order_items(order_id)",
        "CREATE INDEX IF NOT EXISTS idx_order_items_product ON order_items(product_id)",
        "CREATE INDEX IF NOT EXISTS idx_orders_status ON orders(status)",
        "CREATE INDEX IF NOT EXISTS idx_orders_odate ON orders(odate)",
        "CREATE INDEX IF NOT EXISTS idx_orders_sales ON orders(sales_user)",
        "CREATE INDEX IF NOT EXISTS idx_orders_outlet ON orders(outlet_id)",
        "CREATE INDEX IF NOT EXISTS idx_order_payments_order ON order_payments(order_id)",
        "CREATE INDEX IF NOT EXISTS idx_purchases_status ON purchases(status)",
        "CREATE INDEX IF NOT EXISTS idx_purchase_items_purchase ON purchase_items(purchase_id)",
        "CREATE INDEX IF NOT EXISTS idx_or_items_or ON order_request_items(or_id)",
        "CREATE INDEX IF NOT EXISTS idx_price_history_product ON price_history(product_id)",
        "CREATE INDEX IF NOT EXISTS idx_visits_outlet ON visits(outlet_id)",
        "CREATE INDEX IF NOT EXISTS idx_visits_order ON visits(order_id)",
        "CREATE INDEX IF NOT EXISTS idx_stock_moves_product ON stock_moves(product_id)",
        "CREATE INDEX IF NOT EXISTS idx_attempts_user ON delivery_attempts(by_user)",
        "CREATE INDEX IF NOT EXISTS idx_attempts_order ON delivery_attempts(order_id)",
    ):
        con.execute(_ix)
    ensure_perm_data(con)
    con.commit(); con.close()

MMT = datetime.timezone(datetime.timedelta(hours=6, minutes=30))

def now():
    return datetime.datetime.now(MMT).strftime('%Y-%m-%d %H:%M:%S')

def today():
    return datetime.datetime.now(MMT).date().isoformat()

# Office hours (Ko, 2026-10-14): Myanmar time 08:30-17:30 — Sales invoice
# edits are only allowed inside this window on the collection day.
OFFICE_START = 8 * 60 + 30
OFFICE_END = 17 * 60 + 30

def is_leader(u):
    try:
        return bool(u['is_leader'])
    except Exception:
        return False

def office_open(dt=None):
    dt = dt or datetime.datetime.now(MMT)
    mins = dt.hour * 60 + dt.minute
    return OFFICE_START <= mins <= OFFICE_END

def next_code(prefix, name):
    d = db()
    d.execute("INSERT OR IGNORE INTO counters(name,val) VALUES(?,0)", (name,))
    d.execute("UPDATE counters SET val=val+1 WHERE name=?", (name,))
    v = d.execute("SELECT val FROM counters WHERE name=?", (name,)).fetchone()[0]
    return f"{prefix}-{v:04d}"

# ---- v46: pack-linked stock (one pool per family, counted in base units) ----
# products.pool_id = id of the family's base (smallest) product; NULL or the
# product's own id = standalone. products.pack_factor = base units per 1 unit
# of this SKU (the base SKU itself is 1). The single balance lives on the BASE
# product's stock row; a linked big SKU's own stock row is never used again.
def pool_info(d, pid):
    """(base_product_id, factor) for a product id."""
    r = d.execute("SELECT pool_id, pack_factor FROM products WHERE id=?", (pid,)).fetchone()
    if not r:
        return (pid, 1)
    if r['pool_id'] and r['pool_id'] != pid:
        return (r['pool_id'], r['pack_factor'] or 1)
    return (pid, 1)

def raw_stock(d, pid):
    r = d.execute("SELECT qty FROM stock WHERE product_id=?", (pid,)).fetchone()
    return r[0] if r else 0

def pool_stock(d, pid):
    """Total stock of pid's family, in base units."""
    base, _ = pool_info(d, pid)
    return raw_stock(d, base)

def sku_stock(d, pid):
    """Stock shown in this SKU's own units (whole packs only for a big SKU)."""
    base, f = pool_info(d, pid)
    pool = raw_stock(d, base)
    if f == 1:
        return pool
    return int(pool / f)

def line_overstock(d, pid, qty):
    """True when selling qty of pid would exceed the family pool."""
    base, f = pool_info(d, pid)
    return qty * f > raw_stock(d, base) + 1e-9

def family_ids(d, pid):
    """All product ids in pid's family (base first)."""
    base, _ = pool_info(d, pid)
    ids = [base]
    for r in d.execute("SELECT id FROM products WHERE pool_id=? AND id!=?", (base, base)).fetchall():
        ids.append(r['id'])
    return ids

def family_has_moves(d, pid):
    ids = family_ids(d, pid)
    ph = ','.join('?' * len(ids))
    return bool(d.execute(f"SELECT 1 FROM stock_moves WHERE product_id IN ({ph}) LIMIT 1", ids).fetchone())

def move_stock(pid, delta, kind, ref='', note='', user=None):
    """The single stock gate: delta is in pid's own units; for a linked big
    SKU it is converted (x factor) and written on the BASE product's row, with
    a ledger row on the base noting the original SKU and qty."""
    d = db()
    base, f = pool_info(d, pid)
    bdelta = delta * f
    d.execute("INSERT OR IGNORE INTO stock(product_id,qty) VALUES(?,0)", (base,))
    d.execute("UPDATE stock SET qty=qty+? WHERE product_id=?", (bdelta, base))
    if base != pid:
        pr = d.execute("SELECT sku FROM products WHERE id=?", (pid,)).fetchone()
        tag = '[%s ×%s]' % (pr['sku'] if pr else pid, '%g' % delta)
        note = (note + ' ' + tag) if note else tag
    d.execute("INSERT INTO stock_moves(product_id,delta,kind,ref,note,by_user,created_at) VALUES(?,?,?,?,?,?,?)",
              (base, bdelta, kind, ref, note, user, now()))

def stock_qty(pid):
    r = db().execute("SELECT qty FROM stock WHERE product_id=?", (pid,)).fetchone()
    return r[0] if r else 0

# ---- v46: link / unlink (pairs only — one big pack per base) ----
def do_link(d, base_id, big_id, factor, user_id):
    """Link big_id as the pack of base_id. Returns (ok, error_key_or_None).
    Merges balances: pool = base.stock + big.stock x factor; the big SKU's own
    stock row is zeroed and never used again while linked."""
    try:
        factor = float(factor)
    except (TypeError, ValueError):
        return False, 'နှုန်းက ၁ ထက် ကြီးရမယ်'
    if not factor or factor <= 1:
        return False, 'နှုန်းက ၁ ထက် ကြီးရမယ်'
    base = d.execute("SELECT * FROM products WHERE id=?", (base_id,)).fetchone()
    big = d.execute("SELECT * FROM products WHERE id=?", (big_id,)).fetchone()
    if not base or not big or base_id == big_id:
        return False, 'ရွေးထားတဲ့ ပစ္စည်း နဲ့ ချိတ်လို့ မရဘူး'
    if big['pool_id'] and big['pool_id'] != big['id']:
        return False, 'ဒီ ပစ္စည်းက ချိတ်ထားပြီးသား — အရင် ချိတ်ဖြုတ်ပါ'
    if base['pool_id'] and base['pool_id'] != base['id']:
        return False, 'ရွေးထားတဲ့ ပစ္စည်း နဲ့ ချိတ်လို့ မရဘူး'
    if d.execute("SELECT 1 FROM products WHERE pool_id=? AND id!=?", (big_id, big_id)).fetchone():
        return False, 'ရွေးထားတဲ့ ပစ္စည်း နဲ့ ချိတ်လို့ မရဘူး'
    if d.execute("SELECT 1 FROM products WHERE pool_id=? AND id!=?", (base_id, base_id)).fetchone():
        return False, 'ဒီ ပစ္စည်းမှာ အထုပ် ချိတ်ထားပြီးသား ရှိတယ်'
    # v99: same-name rule, enforced server-side (the dropdown filter alone
    # is not enforcement). Same group logic as pack_related, so the edit
    # page can never offer a pair this check would refuse.
    _stand = [dict(r) for r in d.execute(
        "SELECT id, sku, name, category, unit FROM products WHERE active=1 AND (pool_id IS NULL OR pool_id=id) "
        "AND id NOT IN (SELECT pool_id FROM products WHERE pool_id IS NOT NULL)").fetchall()]
    _pool = {r['id']: r for r in _stand}
    _pool[base['id']] = dict(base)
    _pool[big['id']] = dict(big)
    _rel = pack_related(list(_pool.values()), base_id)
    if big_id not in {m['id'] for m in _rel['smalls'] + _rel['bigs']}:
        return False, 'ဆက်စပ်တဲ့ (နာမည်တူ) ပစ္စည်းကိုသာ ချိတ်လို့ ရတယ်'
    old_big = raw_stock(d, big_id)
    d.execute("UPDATE products SET pool_id=?, pack_factor=? WHERE id=?", (base_id, factor, big_id))
    if old_big:
        merged = old_big * factor
        d.execute("INSERT OR IGNORE INTO stock(product_id,qty) VALUES(?,0)", (base_id,))
        d.execute("UPDATE stock SET qty=qty+? WHERE product_id=?", (merged, base_id))
        d.execute("UPDATE stock SET qty=0 WHERE product_id=?", (big_id,))
        d.execute("INSERT INTO stock_moves(product_id,delta,kind,ref,note,by_user,created_at) VALUES(?,?,?,?,?,?,?)",
                  (base_id, merged, 'pack_link', big['sku'],
                   tr('အထုပ် ချိတ်ဆက် — {sku} {q} × {f} ပေါင်း', sku=big['sku'], q='%g' % old_big, f='%g' % factor),
                   user_id, now()))
        d.execute("INSERT INTO stock_moves(product_id,delta,kind,ref,note,by_user,created_at) VALUES(?,?,?,?,?,?,?)",
                  (big_id, -old_big, 'pack_link', base['sku'],
                   tr('အထုပ် ချိတ်ဆက် — stock ကို {sku} ထဲ ပေါင်း', sku=base['sku']), user_id, now()))
    else:
        d.execute("INSERT INTO stock_moves(product_id,delta,kind,ref,note,by_user,created_at) VALUES(?,?,?,?,?,?,?)",
                  (base_id, 0, 'pack_link', big['sku'],
                   tr('အထုပ် ချိတ်ဆက် — {sku} {q} × {f} ပေါင်း', sku=big['sku'], q='0', f='%g' % factor),
                   user_id, now()))
    return True, None

def do_unlink(d, big_id, packs_raw, user_id):
    """Split a linked big SKU back out. packs = full packs held right now;
    packs x factor move to the big SKU, the loose remainder stays on the base."""
    big = d.execute("SELECT * FROM products WHERE id=?", (big_id,)).fetchone()
    if not big or not big['pool_id'] or big['pool_id'] == big['id']:
        return False, 'ရွေးထားတဲ့ ပစ္စည်း နဲ့ ချိတ်လို့ မရဘူး'
    try:
        packs = float(packs_raw)
    except (TypeError, ValueError):
        return False, 'အပြည့် အရေအတွက်ကို ဂဏန်း အပြည့် ထည့်ပါ'
    if packs < 0 or packs != int(packs):
        return False, 'အပြည့် အရေအတွက်ကို ဂဏန်း အပြည့် ထည့်ပါ'
    packs = int(packs)
    base_id = big['pool_id']; factor = big['pack_factor'] or 1
    base = d.execute("SELECT * FROM products WHERE id=?", (base_id,)).fetchone()
    pool = raw_stock(d, base_id)
    take = packs * factor
    if take > pool + 1e-9:
        return False, 'အပြည့် အရေအတွက်က လက်ကျန်ထက် များနေတယ်'
    d.execute("UPDATE products SET pool_id=NULL, pack_factor=1 WHERE id=?", (big_id,))
    d.execute("UPDATE stock SET qty=qty-? WHERE product_id=?", (take, base_id))
    d.execute("INSERT OR IGNORE INTO stock(product_id,qty) VALUES(?,0)", (big_id,))
    d.execute("UPDATE stock SET qty=? WHERE product_id=?", (packs, big_id))
    d.execute("INSERT INTO stock_moves(product_id,delta,kind,ref,note,by_user,created_at) VALUES(?,?,?,?,?,?,?)",
              (base_id, -take, 'pack_unlink', big['sku'],
               tr('ချိတ်ဖြုတ် — အပြည့် {n} ထုပ် ခွဲထုတ်', n=packs), user_id, now()))
    d.execute("INSERT INTO stock_moves(product_id,delta,kind,ref,note,by_user,created_at) VALUES(?,?,?,?,?,?,?)",
              (big_id, packs, 'pack_unlink', base['sku'] if base else '',
               tr('ချိတ်ဖြုတ် — {sku} ကနေ ခွဲထုတ်', sku=base['sku'] if base else ''), user_id, now()))
    return True, None

# ---- v46: link suggestions (same heuristics as checks/pack_pairs_extract.py;
# NEVER auto-linked — a person confirms or skips each one) ----
import re as _re
_PACK_BIG_UNITS = {'case', 'ctn', 'carton'}
_PACK_SIZE_TAGS = ('(B)', '(S)', '(SF)')
_PACK_GENERIC = {'shampoo'}

def _pack_core(name):
    s = name or ''
    for t in _PACK_SIZE_TAGS:
        s = s.replace(t, ' ')
    s = _re.sub(r'\([^)]*\)', ' ', s)
    s = s.replace('ဖာ', ' ')
    return ' '.join(s.split()).lower()

def _pack_is_big(r):
    return (r['unit'] or '').strip().lower() in _PACK_BIG_UNITS or 'ဖာ' in (r['name'] or '')

def pack_suggestions(d):
    """Candidate (base, big) pairs among standalone, active products."""
    prods = [dict(r) for r in d.execute(
        "SELECT id, sku, name, category, unit, upc, wholesale FROM products WHERE active=1").fetchall()]
    linked_big = set()
    has_member = set()
    for r in d.execute("SELECT id, pool_id FROM products WHERE pool_id IS NOT NULL AND pool_id != id").fetchall():
        linked_big.add(r['id']); has_member.add(r['pool_id'])
    standalone = [p for p in prods if p['id'] not in linked_big and p['id'] not in has_member]
    dismissed = {(r['small_id'], r['big_id']) for r in d.execute("SELECT small_id, big_id FROM pack_dismissed").fetchall()}
    groups = {}
    for p in standalone:
        groups.setdefault(_pack_core(p['name']), []).append(p)
    cores = list(groups)
    _toks = {c: set(c.split()) for c in cores}   # v62: token sets built once, not per pair
    for i in range(len(cores)):
        for j in range(i + 1, len(cores)):
            a, b = _toks[cores[i]], _toks[cores[j]]
            if not a or not b:
                continue
            if a <= b or b <= a:
                small_set = a if len(a) <= len(b) else b
                if small_set - _PACK_GENERIC:
                    groups[cores[i]] = groups[cores[i]] + groups[cores[j]]
                    groups[cores[j]] = []
    out = []
    for core, members in groups.items():
        if len(members) != 2:
            continue
        bigs = [m for m in members if _pack_is_big(m)]
        smalls = [m for m in members if not _pack_is_big(m)]
        if len(bigs) != 1 or len(smalls) != 1:
            continue
        small, big = smalls[0], bigs[0]
        if small['category'] != big['category']:
            continue
        if (small['id'], big['id']) in dismissed:
            continue
        f_upc = small['upc'] if small['upc'] and small['upc'] > 1 else None
        f_ratio = None
        if small['wholesale'] and big['wholesale']:
            ratio = big['wholesale'] / small['wholesale']
            if abs(ratio - round(ratio)) < 0.06 and round(ratio) > 1:
                f_ratio = int(round(ratio))
        factor = f_upc or f_ratio
        if f_upc and f_ratio and int(f_upc) == f_ratio:
            grade = tr('ခိုင် — ၂ မျိုး တိုက်')
        elif f_upc:
            grade = tr('ပါဝင်မှု အခြေခံ')
        elif f_ratio:
            grade = tr('ဈေးအချိုး အခြေခံ')
        else:
            grade = tr('နှုန်း မတွေ့ — ဖြည့်ရန်')
        ev = grade
        if f_upc:
            ev += tr(' · ပါဝင်မှု {n}', n='%g' % f_upc)
        if f_ratio:
            ev += tr(' · ဈေးအချိုး {n}', n=f_ratio)
        out.append({'small': small, 'big': big, 'factor': factor, 'evidence': ev})
    out.sort(key=lambda s: s['small']['sku'])
    return out


# v99: same-name-only pack-link candidates for the edit page (Ko, 2026-10-31:
# "နာမည်တူတဲ့ အတွဲပဲ ပြပါ"). Pure helper: same name-core grouping as
# pack_suggestions (core + token-subset merging), split by _pack_is_big.
# No category requirement — Ko's rule is name-based; the dropdown is a
# manual choice, and the server check in do_link uses this same helper.
def pack_related(prods, pid):
    """Relatives of pid among prods. Returns {'smalls': [...], 'bigs': [...]}."""
    by_id = {p['id']: p for p in prods}
    if pid not in by_id:
        return {'smalls': [], 'bigs': []}
    groups = {}
    for p in prods:
        groups.setdefault(_pack_core(p['name']), []).append(p)
    cores = list(groups)
    _toks = {c: set(c.split()) for c in cores}
    for i in range(len(cores)):
        for j in range(i + 1, len(cores)):
            a, b = _toks[cores[i]], _toks[cores[j]]
            if not a or not b:
                continue
            if a <= b or b <= a:
                small_set = a if len(a) <= len(b) else b
                if small_set - _PACK_GENERIC:
                    groups[cores[i]] = groups[cores[i]] + groups[cores[j]]
                    groups[cores[j]] = []
    mine = None
    for members in groups.values():
        if any(m['id'] == pid for m in members):
            mine = members
            break
    if not mine:
        return {'smalls': [], 'bigs': []}
    others = [m for m in mine if m['id'] != pid]
    return {'smalls': [m for m in others if not _pack_is_big(m)],
            'bigs': [m for m in others if _pack_is_big(m)]}


def pack_link_direction(pid, other_id, big_side):
    """v100: derive (base_id, big_id) for the ONE link form.
    big_side 'this' = this product is the big pack; 'other' = the
    counterpart is. Anything else (missing, same product) → None."""
    if not other_id or other_id == pid:
        return None
    if big_side == 'this':
        return (other_id, pid)
    if big_side == 'other':
        return (pid, other_id)
    return None


# ---------------- Auth ----------------
def current_user():
    uid = session.get('uid')
    if not uid:
        return None
    # v62: this row is fetched several times per request (login check,
    # trail tracker, both context processors). Within one request the
    # row cannot change under us — the one exception (the notifications
    # page marking edits as seen) refreshes the memo itself.
    try:
        memo = g.get('_cu_memo')
    except RuntimeError:
        memo = None
    if memo and memo[0] == uid:
        return memo[1]
    u = db().execute("SELECT * FROM users WHERE id=? AND active=1", (uid,)).fetchone()
    try:
        g._cu_memo = (uid, u)
    except RuntimeError:
        pass
    return u

def login_required(*keys):
    # v75: arguments are PERMISSION keys now (require_perm is the same
    # gate under its clearer name). No keys = any authenticated user.
    # Kept for the any-auth routes and for in-body-gated routes.
    return require_perm(*keys)

@app.context_processor
def inject_flowcounts():
    u = current_user()
    if not u or not has_perm(u, 'buy_list.view'):
        return {}
    d = db()
    # v62: the three strip counts ride in one statement instead of three.
    fc = d.execute("SELECT "
                   "(SELECT COUNT(*) FROM order_requests WHERE status IN ('open','partial')) req, "
                   "(SELECT COUNT(*) FROM purchases WHERE status='pending_check') chk, "
                   "(SELECT COUNT(*) FROM settlements WHERE status IN ('draft','sent','confirmed')) stl").fetchone()
    return {'flowcounts': {'req': fc['req'], 'chk': fc['chk'], 'stl': fc['stl']}}

app.jinja_env.globals['t'] = lambda s, **kw: tr(s, getattr(g, 'lang', 'my'), **kw)
# v60: the denomination counter partial (_denom.html) iterates DENOMS.
# (The |denom template filter is registered next to fmt_denom below —
# that helper sits further down the file than this line.)
app.jinja_env.globals['DENOMS'] = DENOMS

@app.context_processor
def inject():
    u = current_user()
    nc = 0
    if u and has_perm(u, 'notifications.view'):
        try:
            _d = db()
            # v50: the bell counts what is still OPEN for this user, in the
            # domains their role may open. Stored 'orders' notices count
            # while unseen (GPS place notices are excluded here — they stay
            # open until a person checks them, so they count off the attempt
            # itself); derived money/backup alerts count while their
            # condition lasts and clear themselves when it resolves.
            nc = _d.execute("SELECT COUNT(*) c FROM order_edits e WHERE e.id>? AND e.attempt_id IS NULL "
                            "AND NOT (COALESCE(e.domain,'orders')='outlets' AND EXISTS (SELECT 1 FROM orders ox WHERE ox.id=e.order_id AND ox.outlet_id IS NOT NULL))",
                            (u['last_edit_seen'] or 0,)).fetchone()['c']
            nc += gps_open_count(_d)
            nc += len(open_alerts(_d, u))
            if has_perm(u, 'audit.view'):   # v51: audit notices are Owner-only
                nc += audit_open_count(_d)
        except Exception:
            nc = 0
    bh, bl, bkey = _trail_back(request.path)
    lang = getattr(g, 'lang', None) or resolve_lang()
    bg = session.get('buy_group')
    _rl = user_role_name(u) if u else ''
    return {'me': u, 'ROLES': ROLES, 'PAY_METHODS': PAY_METHODS, 'can_finance': bool(u and has_perm(u, 'actual.view')), 'can': (lambda k, _u=u: has_perm(_u, k)), 'perms': user_perms(u), 'role_label': (ROLES.get(_rl, _rl) if _rl in ROLES else _rl), 'notif_count': nc,
            'buy_group': bg if bg in ('demand', 'direct') else 'demand',
            'back_href': bh, 'back_label': (tr(bl, lang) if (bl and bkey) else bl), 'back_label_my': bl,
            'lang': lang, 'lang_list': [(c, LANG_LABELS.get(c, c)) for c in LANGS], 't': (lambda s, **kw: tr(s, lang, **kw)),
            'audit_sum': (lambda s: audit_summary_lang(s, lang)),
            'tjs': {k: tr(k, lang) for k in JS_KEYS}}

def back_target(p):
    # Contextual back button: (href, label) naming the destination, per Ko 2026-10-16.
    # v44 (Ko, 2026-10-18): back buttons ONLY on detail/edit/print sub-pages.
    # Nav-level / list pages get NO back button at all (the old fallback showed
    # a self-referential button on the very list the user was already on).
    import re as _re
    m = _re.match(r'^/orders/(\d+)/(edit|invoice)$', p)
    if m: return '/orders/' + m.group(1), 'ဘောက်ချာ အသေးစိတ်'
    m = _re.match(r'^/requests/(\d+)/(receive|print)$', p)
    if m: return '/requests/' + m.group(1), 'အမှာ အသေးစိတ်'
    m = _re.match(r'^/settlements/(\d+)/print$', p)
    if m: return '/settlements/' + m.group(1), 'ရှင်းရန် အသေးစိတ်'
    if _re.match(r'^/orders/\d+$', p): return '/orders', 'ဘောက်ချာ စာရင်း'
    if _re.match(r'^/deliver/\d+$', p): return '/', 'ပင်မ'
    if p == '/orders/new': return '/orders', 'ဘောက်ချာ စာရင်း'
    if _re.match(r'^/requests/\d+$', p): return '/requests', 'အမှာ စာရင်း'
    if p == '/buy-list/why': return '/buy-list', 'ဝယ်ရန် စာရင်း (ဝယ်ရမယ့် စာရင်း အမှန်)'
    if p == '/requests/new': return '/requests', 'အမှာ စာရင်း'
    if _re.match(r'^/purchases/\d+$', p): return '/purchases', 'အဝယ် မှတ်တမ်း'
    if p == '/purchases/new': return '/purchases', 'အဝယ် မှတ်တမ်း'
    if _re.match(r'^/settlements/\d+$', p): return '/settlements', 'ရှင်းရန် စာရင်း'
    if p == '/settlements/create': return '/settlements', 'ရှင်းရန် စာရင်း'
    if _re.match(r'^/outlets/\d+$', p): return '/outlets', 'ဆိုင်များ စာရင်း'
    if p == '/products/new' or _re.match(r'^/products/\d+/edit$', p): return '/products', 'ပစ္စည်း & Stock'
    if _re.match(r'^/users/\d+/edit$', p): return '/users', 'အသုံးပြုသူများ'
    if p == '/expenses/add' or _re.match(r'^/expenses/\d+/edit$', p): return '/expenses', 'အသုံးစရိတ်'
    if p == '/ask/prices': return '/ask', '🤖 Ai မေးကြည့်'
    m = _re.match(r'^/outlets/(\d+)/edit$', p)
    if m: return '/outlets/' + m.group(1), 'ဆိုင် အသေးစိတ်'
    if p == '/audit': return '/', 'ပင်မ'
    if p == '/gps-check': return '/', 'ပင်မ'
    # v76 (Ko, 2026-10-25): Cashbox step-by-step back — day page returns
    # to the month view; denom page's fixed fallback is the month view
    # (the trail supplies its day page in the real month → day → denom
    # flow, since the denom path itself carries no date).
    if _re.match(r'^/cashbox/\d{4}-\d{2}-\d{2}$', p): return '/cashbox', '🧰 Cashbox'
    if _re.match(r'^/cashbox/denom/[^/]+/\d+$', p): return '/cashbox', '🧰 Cashbox'
    return None, None

# ---- v49: step-by-step Back (Ko's rule, 2026-10-19/20) ----
# The back button must NAME the page it returns to, and that page is the one
# the user actually came from. A per-session trail of visited pages supplies
# it; the fixed back_target() map above remains the fallback for a cold
# arrival (typed link, fresh session). Home ('/') roots the trail but is
# never offered as a back target — the top bar already goes home, and the
# v44 rule (no button on a direct visit to a list page) still holds because
# a cold visit has no earlier trail entry.
# v54 (Ko-flagged, 2026-10-21): the trail now serves SUB-PAGES only, and
# only inside the same flow — see _trail_back(). Section/list pages get a
# fixed '← ပင်မ' and never read the trail.
_TRAIL_STATIC = [
    (r'^/$', 'ပင်မ'), (r'^/orders$', 'ဘောက်ချာ စာရင်း'), (r'^/orders/new$', 'ဘောက်ချာ အသစ်'),
    (r'^/orders/\d+/edit$', 'ဘောက်ချာ ပြင်'), (r'^/buy-list$', 'ဝယ်ရန် စာရင်း (ဝယ်ရမယ့် စာရင်း အမှန်)'),
    (r'^/buy-list/why$', 'ဘာလို့ လို နေ တာ လဲ'),
    (r'^/requests$', 'အမှာ စာရင်း'), (r'^/requests/new$', 'အမှာ အသစ်'),
    (r'^/requests/\d+/receive$', '🚚 ကုန်ရောက် သွင်းရန်'),
    (r'^/purchases$', 'အဝယ် မှတ်တမ်း'), (r'^/purchases/new$', 'အဝယ် အသစ်'),
    (r'^/settlements$', 'ရှင်းရန် စာရင်း'), (r'^/products$', 'ပစ္စည်း & Stock'),
    (r'^/products/new$', 'ပစ္စည်း အသစ်'), (r'^/products/\d+/edit$', 'ပစ္စည်း ပြင်'),
    (r'^/stock$', 'Stock မှတ်တမ်း'), (r'^/stock-count$', '🔢 Stock စစ်ဆေး'),
    (r'^/stock-count/\d+$', '🔢 Stock စစ်ဆေး'),
    (r'^/returns$', 'Returns / Damage (ပြန်အပ် / ပျက်စီး)'),
    (r'^/outlets$', 'ဆိုင်များ စာရင်း'), (r'^/users$', 'အသုံးပြုသူများ'), (r'^/areas$', 'ရပ်ကွက်'),
    (r'^/expenses$', '💸 အသုံးစရိတ်'), (r'^/expenses/\d+/edit$', 'အသုံးစရိတ် ပြင်'),
    (r'^/profit$', '📈 လချုပ် အမြတ်'), (r'^/sales-report$', '📊 Sales စစ်ဆေး'),
    (r'^/visits$', '🚶 Location History (ရောက် မှတ်တမ်း)'),
    (r'^/at-risk$', '⚠️ အန္တရာယ် စာရင်း (အရင်း တက်သွားတဲ့ ဘောက်ချာများ)'),
    (r'^/leader-prices$', '⭐ Leader ဈေးနှုန်း'),
    (r'^/notifications$', '🔔 အသိပေးချက်များ — ဘောက်ချာ ပြင်ဆင်မှုများ'),
    (r'^/delivery-board$', '🚚 ပို့ဆောင်'), (r'^/handover$', '💵 ငွေ လွှဲအပ် စစ်ရန်'),
    (r'^/money$', '💰 ငွေ စာရင်း'), (r'^/expiry$', '⏳ သက်တမ်း နီးနေတာ'),
    (r'^/tax$', '🧾 အခွန် အစီရင်ခံ'), (r'^/backups$', '💾 Backup မိတ္တူများ'),
    (r'^/ask$', '🤖 Ai မေးကြည့်'), (r'^/ask/prices$', 'ဈေးကွက် ဈေး ထည့်ရန်'),
    (r'^/users/\d+/edit$', 'အသုံးပြုသူ ပြင်'), (r'^/settlements/create$', 'ရှင်းရန် အသစ်'),
    (r'^/expenses/add$', 'အသုံးစရိတ် အသစ်'), (r'^/audit$', '📋 မှတ်တမ်း စစ်'),
    (r'^/gps-check$', '📍 နေရာ စစ် စာရင်း'),
    (r'^/cashbox$', '🧰 Cashbox'),
]
_TRAIL_DYN = [
    (r'^/orders/(\d+)$', "SELECT code FROM orders WHERE id=?"),
    (r'^/deliver/(\d+)$', "SELECT code FROM orders WHERE id=?"),
    (r'^/requests/(\d+)$', "SELECT code FROM order_requests WHERE id=?"),
    (r'^/purchases/(\d+)$', "SELECT code FROM purchases WHERE id=?"),
    (r'^/settlements/(\d+)$', "SELECT code FROM settlements WHERE id=?"),
    (r'^/outlets/(\d+)$', "SELECT name FROM outlets WHERE id=?"),
]

def _trail_label(d, p):
    """(label, is_trans_key) for a page path; detail pages use the document
    code / shop name itself (user data, shown as-is, never translated)."""
    import re as _re
    for pat, key in _TRAIL_STATIC:
        if _re.match(pat, p): return key, True
    for pat, sql in _TRAIL_DYN:
        m = _re.match(pat, p)
        if m:
            try:
                r = d.execute(sql, (int(m.group(1)),)).fetchone()
            except Exception:
                r = None
            if r and r[0]: return r[0], False
            return None, False
    # v76: Cashbox day pages label themselves with their date (user data,
    # shown as-is, like the _TRAIL_DYN code labels) so the denom page's
    # step-back can name its day page. Denom paths get no label and are
    # never tracked — the day page stays the trail's last entry.
    m = _re.match(r'^/cashbox/(\d{4}-\d{2}-\d{2})$', p)
    if m: return '🧰 Cashbox ' + m.group(1), False
    return None, False

# ---- v57: which buy group the strip expands (Ko-flagged live, 2026-10-22) ----
# v56 rendered BOTH buy groups in full on every buy page and the shared
# stops lit up in both rows — the two 'separated' paths visually glued
# back together ('ဘာလို့ ဒါတွေက လာတွဲနေတာလည်း?'). Now ONE group is
# active per page. Group pages own their group; the shared tail
# (purchases / settlements) activates the group the user is travelling
# in, remembered here per session (default Demand).
# v58: the groups present as TABS (Ko's pick) — tapping a tab also posts
# to /buy-group, which updates this same session value.
# v59 (Ko-flagged live, 2026-10-22): requests carry a KIND ('demand' /
# 'direct') and the requests section has kind views (/requests?kind=…).
# Ko tapped the Direct path, landed in the request list — and the strip
# flipped back to the Demand tab, because /requests* owned Demand no
# matter how he arrived. Now the KIND VIEW wins inside the requests
# section: session['req_kind'] remembers the view being worked in, the
# list/new pages set it from their ?kind= parameter, and the detail /
# receive views re-pin it from the request's own kind (_pin_req_kind).
# The plain list (no ?kind=) is the 'all' view: it never forces a tab —
# the session/default rule decides, exactly as v58 left it.
@app.before_request
def _buy_group_track():
    if request.method != 'GET':
        return
    p = request.path
    if p == '/buy-list' or p == '/buy-list/why':
        session['buy_group'] = 'demand'
    elif p == '/requests':
        k = (request.args.get('kind') or '').strip()
        if k in ('demand', 'direct'):
            session['req_kind'] = k
            session['buy_group'] = k
        elif k == 'all':
            session['req_kind'] = 'all'   # tab: session/default rule
        elif request.args.get('open') == '1':
            # v60 (Ko-flagged live, 2026-10-22): the receive landing
            # ALWAYS renders in Demand mode. GENERAL RULE: a page whose
            # strip stop exists in only ONE group's path renders in that
            # group — never from leftover session state. The '🚚
            # ကုန်ရောက် သွင်း' stop exists only in the Demand path, so
            # arriving here with a poisoned session (e.g. req_kind left
            # at 'direct' by a kind view) presented this Demand list
            # under the Direct tab, with its current stop flipped to the
            # Direct row's ကြို အမှာ. The list itself is unchanged (all
            # open/partial requests, kind-tagged — receiving is shared-
            # tail work, and Direct users receive from their own kind
            # view's per-row buttons). /buy-list (demand) and
            # /purchases/new (direct) already follow this same rule by
            # owning their group outright, above and below.
            session['buy_group'] = 'demand'
        else:
            session['req_kind'] = 'all'   # plain /requests IS the all view
    elif p == '/requests/new':
        k = (request.args.get('kind') or '').strip()
        if k in ('demand', 'direct'):
            session['req_kind'] = k
            session['buy_group'] = k
        elif session.get('req_kind') in ('demand', 'direct'):
            session['buy_group'] = session['req_kind']
        else:
            session['buy_group'] = 'demand'
    elif p.startswith('/requests/'):
        # detail / receive / print: the views re-pin the session from the
        # request's own kind; here only guarantee a sane default.
        if session.get('buy_group') not in ('demand', 'direct'):
            session['buy_group'] = 'demand'
    elif p == '/purchases/new':
        session['buy_group'] = 'direct'
    elif p == '/purchases' or p.startswith('/purchases/') or \
         p == '/settlements' or p.startswith('/settlements/'):
        if session.get('buy_group') not in ('demand', 'direct'):
            session['buy_group'] = 'demand'

@app.before_request
def _trail_track():
    if request.method != 'GET':
        return
    p = request.path
    if p in ('/login', '/logout', '/setup', '/catalog', '/sw.js', '/manifest.webmanifest'):
        return
    if p.startswith(('/static', '/api', '/lang', '/export')):
        return
    if p.endswith('/invoice') or p.endswith('/print'):
        return
    if '.' in p.rsplit('/', 1)[-1]:
        return
    try:
        u = current_user()
    except Exception:
        u = None
    if not u:
        return
    lbl, is_key = _trail_label(db(), p)
    if lbl is None:
        return
    trail = [e for e in (session.get('trail') or []) if isinstance(e, dict) and e.get('p')]
    paths = [e['p'] for e in trail]
    if p in paths:
        # Revisiting a page (often via the back button itself) trims the
        # trail back to it, so the next step back is the page before it.
        trail = trail[:paths.index(p) + 1]
        trail[-1] = {'p': p, 'l': lbl, 'k': 1 if is_key else 0}
    else:
        trail.append({'p': p, 'l': lbl, 'k': 1 if is_key else 0})
        trail = trail[-10:]
    session['trail'] = trail

def _is_subpage(p):
    """Detail/edit/action pages — the SUB-PAGE class of the v54 back rule
    (see _trail_back). back_target() maps them; v49 adds the count sheet."""
    if back_target(p)[0] is not None:
        return True
    import re as _re
    return bool(_re.match(r'^/stock-count/\d+$', p))

# ---- v55: FLOW GROUPS (Ko-flagged live, 2026-10-21) ----
# One workflow's pages form ONE flow group for back-button purposes. The
# v54 guard compared first path segments, which split the single buy
# process into fake flows (/buy-list vs /requests vs /purchases vs
# /settlements), so a legitimate in-process predecessor was rejected and
# the purchase-entry form fell back to '← အဝယ် မှတ်တမ်း' though Ko had
# arrived through the buy strip. To put a future page into a process,
# add its prefix HERE — this list is the only place groups are defined.
FLOW_GROUPS = [
    # buy: need list → order requests → purchases → settlements
    ('buy', ('/buy-list', '/requests', '/purchases', '/settlements')),
    # sales: invoices → deliver → delivery board → money handover
    ('sales', ('/orders', '/deliver', '/delivery-board', '/handover')),
    # cashbox (v76, Ko 2026-10-25): month view → day page → denom fill
    ('cashbox', ('/cashbox',)),
]

def _flow_group(p):
    """Flow-group name for path `p`. Pages outside the named groups form
    their own single-section group keyed by first segment (the pre-v55
    behaviour for every other section)."""
    for name, prefixes in FLOW_GROUPS:
        for pre in prefixes:
            if p == pre or p.startswith(pre + '/'):
                return name
    seg = p.strip('/').split('/')[0] if p.strip('/') else ''
    return 'sec:' + seg

def _trail_back_raw(path):
    """(href, label, label_is_trans_key) for the back button on `path`.

    v54 (Ko-flagged live, 2026-10-21): Ko landed on the buy list and its
    back button read '← ဘောက်ချာ အသစ်' — the unrelated page he happened
    to visit before it. A trail target on a section page is nonsense, so
    the v51 'trail everywhere' rule is split by page kind:
    * SECTION/LIST pages (anything that is not a detail/edit/print/action
      sub-page) — the back button is FIXED to home ('/' 'ပင်မ'), always;
      the trail is never consulted. (/audit and /gps-check sit in
      back_target() only so their parent is home — they are section
      pages and land here too.)
    * SUB-PAGES (the back_target() map + /stock-count/<id>) — the trail
      predecessor may be used ONLY when it belongs to the same flow
      group (FLOW_GROUPS above; v55 replaced the first-segment test,
      which had split the one buy process into fake flows and rejected
      legitimate in-process predecessors such as /buy-list before
      /purchases/new). The sub-page's fixed parent always qualifies, a
      predecessor of '/' renders as '← ပင်မ', and the predecessor is
      never the page itself. Any other predecessor is never offered;
      the fixed back_target() parent is used instead.
    Print views (/invoice, /print) keep their existing bar target;
    home, login, setup and the Catalog PWA stay exempt."""
    if path in ('/', '/catalog', '/login', '/setup'):
        return None, None, False
    if path.endswith('/invoice') or path.endswith('/print'):
        h, l = back_target(path)   # print views keep their existing bar target
        return h, l, True
    if path in ('/audit', '/gps-check') or not _is_subpage(path):
        return '/', 'ပင်မ', True   # section/list page: fixed home (v54)
    fb_h, fb_l = back_target(path)
    if fb_h is None:
        fb_h, fb_l = '/stock-count', '🔢 Stock စစ်ဆေး'
    trail = [e for e in (session.get('trail') or []) if isinstance(e, dict) and e.get('p')]
    idx = None
    for i in range(len(trail) - 1, -1, -1):
        if trail[i]['p'] == path:
            idx = i; break
    prev = None
    if idx is not None and idx > 0:
        prev = trail[idx - 1]
    elif idx is None and trail and trail[-1]['p'] != path:
        prev = trail[-1]
    if prev and prev['p'] != path:
        pp = prev['p']
        if pp == '/':
            return '/', prev['l'], bool(prev.get('k'))   # came from home: '← ပင်မ'
        if pp == fb_h or _flow_group(pp) == _flow_group(path):
            return pp, prev['l'], bool(prev.get('k'))
    return fb_h, fb_l, True

def _trail_back(path):
    """Back target for `path`, with the v59 kind-view rule on top: inside
    the requests section, a back target of '/requests' returns to the
    KIND VIEW the user is working in (session['req_kind'], pinned by the
    kind views and by the detail/receive views themselves) — never to
    the undifferentiated all view they did not come from."""
    h, l, k = _trail_back_raw(path)
    if h == '/requests' and path.startswith('/requests'):
        rk = session.get('req_kind')
        if rk in ('demand', 'direct'):
            h = '/requests?kind=' + rk
    # v64: same treatment for the buy list's why page — back lands on
    # the buy-list VIEW the user came from (today pick or 'all'),
    # remembered by buy_list() itself in session['buylist_day'].
    if h == '/buy-list' and path == '/buy-list/why':
        bd = session.get('buylist_day')
        if bd:
            h = '/buy-list?day=' + bd
    return h, l, k

@app.route('/setup', methods=['GET', 'POST'])
def setup():
    d = db()
    if d.execute("SELECT COUNT(*) c FROM users").fetchone()['c'] > 0:
        return redirect(url_for('login'))
    if request.method == 'POST':
        un = request.form.get('username', '').strip()
        nm = request.form.get('name', '').strip()
        pw = request.form.get('password', '')
        if not un or not nm or len(pw) < 6:
            flash(tr('Username, name နဲ့ password (အနည်းဆုံး ၆ လုံး) လိုပါတယ်'))
        else:
            _orid = d.execute("SELECT id FROM roles WHERE name='owner'").fetchone()
            d.execute("INSERT INTO users(username,pw_hash,name,role,role_id,created_at) VALUES(?,?,?,?,?,?)",
                      (un, generate_password_hash(pw), nm, 'owner', _orid[0] if _orid else None, now()))
            d.commit()
            flash(tr('Owner account ဆောက်ပြီးပြီ — login ဝင်ပါ'))
            return redirect(url_for('login'))
    return render_template('setup.html')

@app.route('/login', methods=['GET', 'POST'])
def login():
    if db().execute("SELECT COUNT(*) c FROM users").fetchone()['c'] == 0:
        return redirect(url_for('setup'))
    if request.method == 'POST':
        u = db().execute("SELECT * FROM users WHERE username=? AND active=1",
                         (request.form.get('username', '').strip(),)).fetchone()
        if u and check_password_hash(u['pw_hash'], request.form.get('password', '')):
            session.permanent = True
            session['uid'] = u['id']
            _pl = session.get('lang')
            if _pl in LANGS:
                db().execute("UPDATE users SET lang=? WHERE id=?", (_pl, u['id'])); db().commit()
            return redirect(request.args.get('next') or url_for('dashboard'))
        flash(tr('Username / password မှားနေတယ်'))
    return render_template('login.html')

@app.route('/logout')
def logout():
    session.clear()
    return redirect(url_for('login'))

@app.route('/lang/<code>')
def set_lang(code):
    if code not in LANGS:
        code = 'my'
    u = current_user()
    if u is not None:
        db().execute("UPDATE users SET lang=? WHERE id=?", (code, u['id'])); db().commit()
    session['lang'] = code
    dest = request.referrer or (url_for('dashboard') if u is not None else url_for('login'))
    resp = redirect(dest)
    resp.set_cookie('gtm_lang', code, max_age=30 * 24 * 3600)
    return resp


# ---------------- Dashboard ----------------
@app.route('/')
@login_required()
def dashboard():
    d = db()
    if has_perm(g.user, 'dashboard.delivery_own'):
        uid = g.user['id']
        _todo_rows = d.execute("SELECT * FROM orders WHERE assigned_to=? AND status='new' ORDER BY odate, id", (uid,)).fetchall()
        _done_ids = [r['order_id'] for r in d.execute("SELECT DISTINCT order_id FROM delivery_attempts WHERE by_user=? AND result='delivered' AND substr(created_at,1,10)=?", (uid, today())).fetchall()]
        _tot = order_totals_map(d, [o['id'] for o in _todo_rows] + _done_ids)
        todo = [{'o': o, 'total': _tot.get(o['id'], (0, 0))[0]} for o in _todo_rows]
        done = d.execute("SELECT COUNT(*) c FROM delivery_attempts WHERE by_user=? AND result='delivered' AND substr(created_at,1,10)=?", (uid, today())).fetchone()['c']
        expected = 0.0
        for oid in _done_ids:
            expected += _tot.get(oid, (0, 0))[0]
        collected = d.execute("SELECT COALESCE(SUM(amount),0) v FROM order_payments WHERE by_user=? AND rdate=?", (uid, today())).fetchone()['v']
        pending = d.execute("SELECT COALESCE(SUM(amount),0) v FROM order_payments WHERE by_user=? AND COALESCE(verified,1)=0", (uid,)).fetchone()['v']
        attempts = d.execute("SELECT a.*, o.code, o.outlet FROM delivery_attempts a JOIN orders o ON o.id=a.order_id WHERE a.by_user=? ORDER BY a.id DESC LIMIT 20", (uid,)).fetchall()
        return render_template('delivery_dashboard.html', todo=todo, done=done, expected=expected,
                               collected=collected, pending=pending, attempts=attempts, fmt_money=fmt_money)
    if has_perm(g.user, 'dashboard.sales_own'):
        uid = g.user['id']
        team = (g.user['team'] or '').strip() if 'team' in g.user.keys() else ''
        TOT = "(SELECT COALESCE(SUM(qty*price),0) FROM order_items WHERE order_id=o.id)-o.discount"
        def agg(where, args):
            r = d.execute("SELECT COALESCE(SUM(" + TOT + "),0) v, COUNT(*) c FROM orders o WHERE o.status!='cancelled' AND " + where, args).fetchone()
            return {'v': r['v'], 'c': r['c']}
        mine = {'life': agg("o.sales_user=?", (uid,)),
                'month': agg("o.sales_user=? AND substr(o.odate,1,7)=?", (uid, today()[:7])),
                'day': agg("o.sales_user=? AND o.odate=?", (uid, today()))}
        sku_open = d.execute("SELECT COUNT(*) c FROM products WHERE active=1 AND COALESCE(sellable,1)=1" + _sup_open() + "").fetchone()['c']
        sku_sold = d.execute("SELECT COUNT(DISTINCT oi.product_id) c FROM order_items oi JOIN orders o ON o.id=oi.order_id WHERE o.sales_user=? AND o.status!='cancelled' AND substr(o.odate,1,7)=?", (uid, today()[:7])).fetchone()['c']
        unpaid_own = d.execute("SELECT COUNT(*) c FROM orders WHERE sales_user=? AND paid=0 AND status!='cancelled'", (uid,)).fetchone()['c']
        team_stats = None
        if team:
            _tl = agg("o.team=?", (team,)); _tm = agg("o.team=? AND substr(o.odate,1,7)=?", (team, today()[:7]))
            if _tl['v'] != mine['life']['v'] or _tl['c'] != mine['life']['c']:
                team_stats = {'name': team, 'life': _tl, 'month': _tm}
        leader = None
        if is_leader(g.user):
            # v62: one grouped pass per period for ALL members (was three
            # aggregate queries per member). Same SUM expression per row,
            # so each member's figures are the agg() numbers unchanged.
            def agg_by_user(cond, args):
                m = {}
                for r in d.execute("SELECT o.sales_user uid, COALESCE(SUM(" + TOT + "),0) v, COUNT(*) c FROM orders o WHERE o.status!='cancelled' AND " + cond + " GROUP BY o.sales_user", args).fetchall():
                    m[r['uid']] = {'v': r['v'], 'c': r['c']}
                return m
            _md = agg_by_user("o.odate=?", (today(),))
            _mm = agg_by_user("substr(o.odate,1,7)=?", (today()[:7],))
            _ml = agg_by_user("1=1", ())
            _z = {'v': 0, 'c': 0}
            members = []
            for u in d.execute("SELECT id, name, team, active FROM users WHERE role='sales' ORDER BY active DESC, name").fetchall():
                members.append({'name': u['name'], 'team': u['team'] or '—', 'active': u['active'],
                                'day': _md.get(u['id'], _z),
                                'month': _mm.get(u['id'], _z),
                                'life': _ml.get(u['id'], _z)})
            SALES_IDS = "o.sales_user IN (SELECT id FROM users WHERE role='sales')"
            grand = {'day': agg(SALES_IDS + " AND o.odate=?", (today(),)),
                     'month': agg(SALES_IDS + " AND substr(o.odate,1,7)=?", (today()[:7],)),
                     'life': agg(SALES_IDS, ())}
            recent_all = [dict(r) for r in d.execute("SELECT o.*, u.name rep FROM orders o LEFT JOIN users u ON u.id=o.sales_user WHERE " + SALES_IDS + " ORDER BY o.id DESC LIMIT 10").fetchall()]
            _tot = order_totals_map(d, [r['id'] for r in recent_all])
            for r in recent_all:
                r['total'] = _tot.get(r['id'], (0, 0))[0]
            leader = {'members': members, 'grand': grand, 'recent_all': recent_all}
        recent = [dict(r) for r in d.execute("SELECT o.*, u.name rep FROM orders o LEFT JOIN users u ON u.id=o.sales_user WHERE o.sales_user=? ORDER BY o.id DESC LIMIT 8", (uid,)).fetchall()]
        _tot = order_totals_map(d, [r['id'] for r in recent])
        for r in recent:
            r['total'] = _tot.get(r['id'], (0, 0))[0]
        since = (g.user['created_at'] or '')[:10]
        return render_template('sales_dashboard.html', mine=mine, team_stats=team_stats, leader=leader,
                               sku_open=sku_open, sku_sold=sku_sold, unpaid_own=unpaid_own,
                               recent=recent, since=since, month_label=today()[:7],
                               lp_on=leader_prices_on(d))
    stats = {}
    stats['products'] = d.execute("SELECT COUNT(*) c FROM products WHERE active=1").fetchone()['c']
    # v46: counts use each SKU's own view of its family pool
    _smap = {r['product_id']: r['qty'] for r in d.execute("SELECT product_id, qty FROM stock").fetchall()}
    _views = []
    for _p in d.execute("SELECT id, pool_id, pack_factor FROM products WHERE active=1").fetchall():
        if _p['pool_id'] and _p['pool_id'] != _p['id']:
            _pool = _smap.get(_p['pool_id'], 0); _f = _p['pack_factor'] or 1
            _views.append(int(_pool / _f) if _f != 1 else _pool)
        else:
            _views.append(_smap.get(_p['id'], 0))
    stats['in_stock'] = sum(1 for v in _views if v > 0)
    stats['low_stock'] = sum(1 for v in _views if 0 < v <= 5)
    stats['orders_today'] = d.execute("SELECT COUNT(*) c FROM orders WHERE odate=?", (today(),)).fetchone()['c']
    stats['sales_today'] = d.execute(
        "SELECT COALESCE(SUM(t),0) v FROM (SELECT (SELECT COALESCE(SUM(qty*price),0) FROM order_items WHERE order_id=o.id)-o.discount t FROM orders o WHERE o.odate=? AND o.status!='cancelled')", (today(),)).fetchone()['v']
    stats['pending_purchases'] = d.execute("SELECT COUNT(*) c FROM purchases WHERE status='pending_check'").fetchone()['c']
    stats['pending_returns'] = d.execute("SELECT COUNT(*) c FROM returns WHERE status='recorded'").fetchone()['c']
    stats['unpaid'] = d.execute("SELECT COUNT(*) c FROM orders WHERE paid=0 AND status!='cancelled'").fetchone()['c']
    # v49: expiry warning count, owner position card, backup download reminder
    try:
        stats['expiry_soon'] = len({b['product_id'] for b in expiry_batches(d)})
    except Exception:
        stats['expiry_soon'] = 0
    # v52: open price-review count for the dashboard pill (Owner/Finance only —
    # the /at-risk audience; other roles never see the figure)
    try:
        stats['price_review'] = len(_price_review(d)[0]) if has_perm(g.user, 'at_risk.view') else 0
    except Exception:
        stats['price_review'] = 0
    # v53: open place-check count for the dashboard pill (the /gps-check
    # audience: owner/warehouse/finance — other roles never see the figure)
    try:
        stats['gps_open'] = gps_open_count(d) if has_perm(g.user, 'gps_check.view') else 0
    except Exception:
        stats['gps_open'] = 0
    try:
        stats['position'] = money_position(d) if has_perm(g.user, 'money.view') else None
    except Exception:
        stats['position'] = None
    stats['backup_due'] = False
    if has_perm(g.user, 'backups.view'):
        last_dl = get_setting(d, 'last_backup_download')
        due = True
        if last_dl:
            try:
                _y, _mo, _dd = map(int, last_dl[:10].split('-'))
                due = (datetime.datetime.now(MMT).date() - datetime.date(_y, _mo, _dd)).days >= 15
            except Exception:
                due = True
        stats['backup_due'] = due
    recent = [dict(r) for r in d.execute("SELECT o.*, u.name rep FROM orders o LEFT JOIN users u ON u.id=o.sales_user ORDER BY o.id DESC LIMIT 8").fetchall()]
    _tot = order_totals_map(d, [r['id'] for r in recent])
    _rec = order_received_map(d, [r['id'] for r in recent])
    for r in recent:
        r['total'] = _tot.get(r['id'], (0, 0))[0]
        r['received'] = _rec.get(r['id'], 0)
    return render_template('dashboard.html', stats=stats, recent=recent)



@app.route('/outlets')
@require_perm('outlets.view')
def outlets_list():
    d = db()
    q = (request.args.get('q') or '').strip()
    ty = (request.args.get('ty') or '').strip()
    if ty not in SHOP_TYPE_KEYS + ['unset']: ty = ''
    sql = "SELECT * FROM outlets"; args = []; conds = []
    if q: conds.append("name LIKE ?"); args.append(f'%{q}%')
    # v65: filter by shop type; 'unset' = never typed (NULL or '')
    if ty == 'unset': conds.append("(shop_type IS NULL OR shop_type='')")
    elif ty: conds.append("shop_type=?"); args.append(ty)
    if conds: sql += " WHERE " + " AND ".join(conds)
    sql += " ORDER BY name"
    outs = []
    this_month = today()[:7]
    TOT = "(SELECT COALESCE(SUM(qty*price),0) FROM order_items WHERE order_id=o.id)-o.discount"
    # v62: every outlet's figures in a few grouped passes (was 3+ queries
    # per outlet, plus two more per unpaid invoice). Same expressions,
    # grouped by outlet instead of re-run per outlet.
    agg_map = {r['oid']: r for r in d.execute(
        "SELECT o.outlet_id oid, COUNT(*) c, COALESCE(SUM(" + TOT + "),0) v, MAX(o.odate) last "
        "FROM orders o WHERE o.outlet_id IS NOT NULL AND o.status!='cancelled' GROUP BY o.outlet_id").fetchall()}
    recv_map = {r['oid']: r['v'] for r in d.execute(
        "SELECT o.outlet_id oid, COALESCE(SUM(p.amount),0) v FROM order_payments p "
        "JOIN orders o ON o.id=p.order_id WHERE o.outlet_id IS NOT NULL AND o.status!='cancelled' "
        "AND COALESCE(p.verified,1)=1 GROUP BY o.outlet_id").fetchall()}
    _unpaid = d.execute("SELECT id, outlet_id FROM orders WHERE status='delivered' AND paid=0 AND outlet_id IS NOT NULL").fetchall()
    _tot = order_totals_map(d, [co['id'] for co in _unpaid])
    _rec = order_received_map(d, [co['id'] for co in _unpaid])
    bal_map = {}
    for co in _unpaid:
        bal_map[co['outlet_id']] = bal_map.get(co['outlet_id'], 0.0) + _tot.get(co['id'], (0, 0))[0] - _rec.get(co['id'], 0)
    uname_map = {r['id']: r['name'] for r in d.execute("SELECT id, name FROM users").fetchall()}
    # v67: names shared by 2+ outlets render disambiguated in the list.
    dupnames = {r['name'] for r in d.execute("SELECT name FROM outlets GROUP BY name HAVING COUNT(*)>1").fetchall()}
    for o in d.execute(sql, args).fetchall():
        agg = agg_map.get(o['id'])
        outs.append({'o': o, 'label': (picker_label(o) if o['name'] in dupnames else o['name']),
                     'orders': (agg['c'] if agg else 0), 'total': (agg['v'] if agg else 0),
                     'recv': recv_map.get(o['id'], 0), 'balance': bal_map.get(o['id'], 0.0),
                     'last': (agg['last'] if agg else None),
                     'creator': (uname_map.get(o['created_by'], '') if o['created_by'] else ''),
                     'is_new': bool(o['first_seen']) and (o['first_seen'] or '')[:7] == this_month})
    outs.sort(key=lambda x: (-x['balance'], x['o']['name']))
    # v68: possible duplicates. Spellings cannot find these twins (the
    # same shop written in two scripts reads as two unrelated names),
    # so the ONLY signal used here is place: two pinned shops within
    # 40 m of each other. A person confirms — merge the pair, or mark
    # it as two genuinely different shops (the dismissal is stored
    # with who/when and the pair stops listing).
    _pinned = d.execute("SELECT * FROM outlets WHERE lat IS NOT NULL AND lng IS NOT NULL ORDER BY id").fetchall()
    _dismissed = {(r['outlet_a'], r['outlet_b']) for r in
                  d.execute("SELECT outlet_a, outlet_b FROM outlet_dup_dismissals").fetchall()}
    dup_pairs = []
    for _i in range(len(_pinned)):
        for _j in range(_i + 1, len(_pinned)):
            _a, _b = _pinned[_i], _pinned[_j]
            if (_a['id'], _b['id']) in _dismissed: continue
            _dm = haversine_m(_a['lat'], _a['lng'], _b['lat'], _b['lng'])
            if _dm <= 40:
                dup_pairs.append({'a': _a, 'b': _b, 'la': picker_label(_a), 'lb': picker_label(_b),
                                  'dist': fmt_dist(_dm)})
    dup_pairs.sort(key=lambda p: p['la'])
    return render_template('outlets.html', outs=outs, q=q, ty=ty, office=office_point(d), fmt_money=fmt_money,
                           dup_pairs=dup_pairs, **shop_type_ctx())


@app.route('/outlets/<int:olid>')
@require_perm('outlets.view')
def outlet_detail(olid):
    d = db()
    o = d.execute("SELECT * FROM outlets WHERE id=?", (olid,)).fetchone()
    if not o: abort(404)
    TOT = "(SELECT COALESCE(SUM(qty*price),0) FROM order_items WHERE order_id=o.id)-o.discount"
    _inv_rows = d.execute("SELECT o.*, u.name repname FROM orders o LEFT JOIN users u ON u.id=o.sales_user WHERE o.outlet_id=? ORDER BY o.odate DESC, o.id DESC LIMIT 100", (olid,)).fetchall()
    _tot = order_totals_map(d, [r['id'] for r in _inv_rows])
    _recm = order_received_map(d, [r['id'] for r in _inv_rows])
    invs = []; total_all = 0.0; recv_all = 0.0; bal_all = 0.0; oldest = None
    for r in _inv_rows:
        t = _tot.get(r['id'], (0, 0))[0]; rec = _recm.get(r['id'], 0)
        bal = (t - rec) if r['status'] == 'delivered' else 0.0
        if r['status'] != 'cancelled':
            total_all += t; recv_all += rec
            if r['status'] == 'delivered' and bal > 0.5:
                bal_all += bal
                if oldest is None or r['odate'] < oldest: oldest = r['odate']
        invs.append({'o': r, 'total': t, 'recv': rec, 'bal': bal})
    days = None
    if oldest:
        y, mo, dd = map(int, oldest.split('-')); y2, m2, d2 = map(int, today().split('-'))
        days = (datetime.date(y2, m2, d2) - datetime.date(y, mo, dd)).days
    per = request.args.get('p') or 'month'
    yr = request.args.get('y') or today()[:4]
    if per == 'year':
        vf, vt, per_label = yr + '-01-01', yr + '-12-31', tr('{y} နှစ်', y=yr)
    else:
        months = {'month': 1, '3m': 3, '6m': 6, '9m': 9, '12m': 12}.get(per, 1)
        y0, m0 = int(today()[:4]), int(today()[5:7])
        sm, sy = m0 - (months - 1), y0
        while sm <= 0: sm += 12; sy -= 1
        vf, vt = f'{sy:04d}-{sm:02d}-01', today()
        per_label = tr({1: 'ဒီလ', 3: '၃ လ အတွင်း', 6: '၆ လ အတွင်း', 9: '၉ လ အတွင်း', 12: '၁ နှစ် အတွင်း'}[months])
    vst = d.execute("SELECT COUNT(*) tot, COALESCE(SUM(kind='order'),0) eff, COALESCE(SUM(kind='probable'),0) prob FROM visits WHERE outlet_id=? AND vdate BETWEEN ? AND ?", (olid, vf, vt)).fetchone()
    samt = d.execute("SELECT COALESCE(SUM(" + TOT + "),0) v, COUNT(*) c FROM orders o WHERE o.outlet_id=? AND o.status!='cancelled' AND o.odate BETWEEN ? AND ?", (olid, vf, vt)).fetchone()
    _amt_map = {r['uid']: r['v'] for r in d.execute("SELECT o.sales_user uid, COALESCE(SUM(" + TOT + "),0) v FROM orders o WHERE o.outlet_id=? AND o.status!='cancelled' AND o.odate BETWEEN ? AND ? GROUP BY o.sales_user", (olid, vf, vt)).fetchall()}
    perrep = []
    for v in d.execute("SELECT v.user_id uid, u.name nm, COUNT(*) tot, COALESCE(SUM(v.kind='order'),0) eff FROM visits v JOIN users u ON u.id=v.user_id WHERE v.outlet_id=? AND v.vdate BETWEEN ? AND ? GROUP BY v.user_id ORDER BY tot DESC, v.user_id", (olid, vf, vt)).fetchall():
        perrep.append({'name': v['nm'], 'tot': v['tot'], 'eff': v['eff'], 'amt': _amt_map.get(v['uid'], 0)})
    creator = d.execute("SELECT name FROM users WHERE id=?", (o['created_by'],)).fetchone() if o['created_by'] else None
    others = [{'id': r['id'], 'label': picker_label(r)} for r in
              d.execute("SELECT * FROM outlets WHERE id!=? ORDER BY name, area, id", (olid,)).fetchall()]
    years = [r['y'] for r in d.execute("SELECT DISTINCT substr(first_seen,1,4) y FROM outlets WHERE first_seen IS NOT NULL AND first_seen!='' ORDER BY y DESC").fetchall()]
    if today()[:4] not in years: years = [today()[:4]] + years
    exchanges = d.execute("SELECT x.*, p.sku, p.name pname FROM outlet_exchanges x JOIN products p ON p.id=x.product_id WHERE x.outlet_id=? ORDER BY x.id", (olid,)).fetchall()
    aliases = d.execute("SELECT * FROM outlet_aliases WHERE outlet_id=? ORDER BY id", (olid,)).fetchall()
    return render_template('outlet_detail.html', o=o, olabel=outlet_label(d, o), aliases=aliases, invs=invs, total_all=total_all, recv_all=recv_all,
                           bal_all=bal_all, oldest=oldest, days=days, vst=vst, samt=samt, per=per, yr=yr,
                           per_label=per_label, perrep=perrep, creator=(creator['name'] if creator else ''),
                           others=others, years=years, exchanges=exchanges, fmt_money=fmt_money, **shop_type_ctx())


@app.route('/outlets/<int:olid>/edit', methods=['POST'])
@require_perm('outlets.manage')
def outlet_edit(olid):
    d = db()
    o = d.execute("SELECT * FROM outlets WHERE id=?", (olid,)).fetchone()
    if not o: abort(404)
    nm = (request.form.get('name') or '').strip()
    ar = (request.form.get('area') or '').strip()
    # v67: identity is (name, area) — the same name in a DIFFERENT ward
    # is a different shop and is allowed; only an exact (name, area)
    # collision with another outlet is refused. The check runs against
    # the resulting pair whenever name or area actually changes, so
    # editing the note/type of an already-duplicated name never blocks.
    if nm and (nm != o['name'] or ar != (o['area'] or '')):
        clash = d.execute("SELECT id FROM outlets WHERE name=? AND COALESCE(area,'')=? AND id!=?", (nm, ar, olid)).fetchone()
        if clash:
            flash(tr('အဲ့ဒီ နာမည် နဲ့ ဆိုင် ရှိနှင့်ပြီးသား — ပေါင်းချင်ရင် Merge ကို သုံးပါ'))
            return redirect(url_for('outlet_detail', olid=olid))
    if nm and nm != o['name']:
        d.execute("UPDATE outlets SET name=? WHERE id=?", (nm, olid))
        d.execute("UPDATE orders SET outlet=? WHERE outlet_id=?", (nm, olid))
    d.execute("UPDATE outlets SET area=? WHERE id=?", (ar, olid))
    if 'note' in request.form:
        d.execute("UPDATE outlets SET note=? WHERE id=?",
                  ((request.form.get('note') or '').strip() or None, olid))
    if 'shop_type' in request.form:
        # v65: '' clears back to not-set; v69: any of the 13 keys sticks.
        stv = (request.form.get('shop_type') or '').strip()
        d.execute("UPDATE outlets SET shop_type=? WHERE id=?",
                  (stv if stv in SHOP_TYPE_KEYS else None, olid))
    if 'credit_limit' in request.form:
        cl = (request.form.get('credit_limit') or '').strip().replace(',', '')
        try:
            clv = float(cl) if cl else None
        except ValueError:
            clv = None
        d.execute("UPDATE outlets SET credit_limit=? WHERE id=?", (clv, olid))
    d.commit(); flash(tr('ပြင်ပြီးပြီ'))
    return redirect(url_for('outlet_detail', olid=olid))


@app.route('/outlets/<int:olid>/alias-add', methods=['POST'])
@require_perm('outlets.manage')
def outlet_alias_add(olid):
    # v68: an alias spelling is how a shop is ALSO written ('Sut' for
    # 'ဆွတ်'). One alias text belongs to ONE outlet only — adding a
    # text that is already a shop's official name or another shop's
    # alias is refused plainly, because that text must keep finding
    # exactly one shop.
    d = db()
    o = d.execute("SELECT * FROM outlets WHERE id=?", (olid,)).fetchone()
    if not o: abort(404)
    a = (request.form.get('alias') or '').strip()
    if not a:
        flash(tr('အရေး ထည့်ပါ'))
        return redirect(url_for('outlet_detail', olid=olid))
    n = _norm_shop_text(a)
    if n == _norm_shop_text(o['name']):
        flash(tr('ဆိုင် နာမည် နဲ့ တူနေတယ် — ထည့်ဖို့ မလိုဘူး'))
        return redirect(url_for('outlet_detail', olid=olid))
    ex = d.execute("SELECT outlet_id FROM outlet_aliases WHERE alias_norm=?", (n,)).fetchone()
    if ex:
        flash(tr('အဲ့ဒီ အရေး ဒီ ဆိုင် မှာ မှတ်ပြီး သား') if ex['outlet_id'] == olid
              else tr('အဲ့ဒီ အရေး က အခြား ဆိုင် မှာ မှတ်ပြီး သား — ထည့်လို့ မရဘူး'))
        return redirect(url_for('outlet_detail', olid=olid))
    for r in d.execute("SELECT name FROM outlets WHERE id!=?", (olid,)).fetchall():
        if _norm_shop_text(r['name']) == n:
            flash(tr('အဲ့ဒီ အရေး က အခြား ဆိုင် မှာ မှတ်ပြီး သား — ထည့်လို့ မရဘူး'))
            return redirect(url_for('outlet_detail', olid=olid))
    d.execute("INSERT INTO outlet_aliases(outlet_id, alias, alias_norm, created_by, created_at) VALUES(?,?,?,?,?)",
              (olid, a, n, g.user['id'], now()))
    d.commit(); flash(tr('ထည့်ပြီးပြီ'))
    return redirect(url_for('outlet_detail', olid=olid))


@app.route('/outlets/<int:olid>/alias-del', methods=['POST'])
@require_perm('outlets.manage')
def outlet_alias_del(olid):
    d = db()
    o = d.execute("SELECT id FROM outlets WHERE id=?", (olid,)).fetchone()
    if not o: abort(404)
    aid = request.form.get('alias_id', type=int)
    d.execute("DELETE FROM outlet_aliases WHERE id=? AND outlet_id=?", (aid, olid))
    d.commit(); flash(tr('ဖြုတ်ပြီးပြီ'))
    return redirect(url_for('outlet_detail', olid=olid))


@app.route('/outlets/dup-dismiss', methods=['POST'])
@require_perm('outlets.manage')
def outlet_dup_dismiss():
    # v68: a person looked at a near-pin pair and confirmed the two
    # records really are two different shops. Stored with who/when;
    # the pair stops listing in 'ထပ် နေ နိုင် တဲ့ ဆိုင်'.
    d = db()
    a = request.form.get('a', type=int); b = request.form.get('b', type=int)
    if a and b and a != b:
        lo, hi = min(a, b), max(a, b)
        d.execute("INSERT OR IGNORE INTO outlet_dup_dismissals(outlet_a, outlet_b, by_user, created_at) VALUES(?,?,?,?)",
                  (lo, hi, g.user['id'], now()))
        d.commit(); flash(tr('ဆိုင် ကွဲ အဖြစ် မှတ်ထားပြီ'))
    return redirect(url_for('outlets_list'))


@app.route('/outlets/<int:olid>/pin-reset', methods=['POST'])
@require_perm('outlets.manage')
def outlet_pin_reset(olid):
    # v47: the shop moved — forget the learned place pin so the next order's
    # GPS learns the new place from scratch (the learn code handles no-pin).
    d = db()
    o = d.execute("SELECT id FROM outlets WHERE id=?", (olid,)).fetchone()
    if not o: abort(404)
    d.execute("UPDATE outlets SET lat=NULL, lng=NULL, gps_n=0 WHERE id=?", (olid,))
    d.commit(); flash(tr('နေရာ အမှတ် ပြန် စပြီးပြီ — နောက် order ရောက်ရင် အမှတ် အသစ် မှတ်မယ်'))
    return redirect(url_for('outlet_detail', olid=olid))


@app.route('/outlets/<int:olid>/merge', methods=['POST'])
@require_perm('outlets.manage')
def outlet_merge(olid):
    d = db()
    src = d.execute("SELECT * FROM outlets WHERE id=?", (olid,)).fetchone()
    tgt_id = request.form.get('target', type=int)
    tgt = d.execute("SELECT * FROM outlets WHERE id=?", (tgt_id,)).fetchone() if tgt_id else None
    if not src or not tgt or tgt['id'] == olid: abort(400)
    d.execute("UPDATE orders SET outlet_id=?, outlet=? WHERE outlet_id=?", (tgt['id'], tgt['name'], olid))
    for v in d.execute("SELECT * FROM visits WHERE outlet_id=?", (olid,)).fetchall():
        ex = d.execute("SELECT id, kind FROM visits WHERE user_id=? AND outlet_id=? AND vdate=?", (v['user_id'], tgt['id'], v['vdate'])).fetchone()
        if ex:
            if v['kind'] == 'order' and ex['kind'] != 'order':
                d.execute("UPDATE visits SET kind='order', order_id=? WHERE id=?", (v['order_id'], ex['id']))
            d.execute("DELETE FROM visits WHERE id=?", (v['id'],))
        else:
            d.execute("UPDATE visits SET outlet_id=? WHERE id=?", (tgt['id'], v['id']))
    if tgt['lat'] is None and src['lat'] is not None:
        d.execute("UPDATE outlets SET lat=?, lng=?, gps_n=? WHERE id=?", (src['lat'], src['lng'], src['gps_n'], tgt['id']))
    if src['first_seen'] and (not tgt['first_seen'] or src['first_seen'] < tgt['first_seen']):
        d.execute("UPDATE outlets SET first_seen=? WHERE id=?", (src['first_seen'], tgt['id']))
    # v68: the losing shop's SPELLINGS move to the winner. Its aliases
    # simply change owner (an alias text belongs to one outlet only,
    # so the move can never collide), and its official name becomes an
    # alias of the winner — from now on, typing the losing spelling
    # finds the surviving shop and can never spawn a twin again.
    # Skipped (and said so) only when that name already belongs to a
    # THIRD shop — as its official name or as its alias.
    d.execute("UPDATE outlet_aliases SET outlet_id=? WHERE outlet_id=?", (tgt['id'], olid))
    skipped = None
    if _norm_shop_text(src['name']) != _norm_shop_text(tgt['name']):
        _sn = _norm_shop_text(src['name'])
        _clash = d.execute("SELECT outlet_id FROM outlet_aliases WHERE alias_norm=? AND outlet_id NOT IN (?,?)",
                           (_sn, olid, tgt['id'])).fetchone()
        if not _clash:
            for r in d.execute("SELECT id, name FROM outlets WHERE id NOT IN (?,?)", (olid, tgt['id'])).fetchall():
                if _norm_shop_text(r['name']) == _sn:
                    _clash = r; break
        if _clash:
            skipped = src['name']
        else:
            try:
                d.execute("INSERT INTO outlet_aliases(outlet_id, alias, alias_norm, created_by, created_at) VALUES(?,?,?,?,?)",
                          (tgt['id'], src['name'], _sn, g.user['id'], now()))
            except sqlite3.IntegrityError:
                pass   # the winner already carries this spelling as its own alias — it still finds the winner
    d.execute("DELETE FROM outlets WHERE id=?", (olid,))
    d.commit(); flash(tr('{a} ကို {b} ထဲ ပေါင်းလိုက်ပြီ', a=src['name'], b=tgt['name']))
    if skipped:
        flash(tr('"{name}" ဆိုတဲ့ အရေး က အခြား ဆိုင် မှာ မှတ်ပြီး သား မို့ အခြား အရေး အဖြစ် မထည့်လိုက်ဘူး', name=skipped))
    return redirect(url_for('outlet_detail', olid=tgt['id']))


# v91: Product Data Analysis (Ko, 2026-10-29) — six ranked views. Pure
# functions (a DB connection + a 'YYYY-MM' month only, no Flask globals) so
# the test can run them verbatim against a synthetic DB.
# ANALYSIS-START
def _analysis_bands(rows):
    """Split a most-first ranked list into top/middle/bottom thirds. Empty
    bands stay empty (tiny lists); zero-count items never enter here — they
    get their own never/unsold list."""
    n = len(rows)
    if n == 0:
        return {'top': [], 'mid': [], 'bot': []}
    k1 = max(1, n // 3)
    k2 = max(k1, (2 * n) // 3)
    return {'top': rows[:k1], 'mid': rows[k1:k2], 'bot': rows[k2:]}


def _analysis_rank(rows):
    for i, r in enumerate(rows):
        r['rank'] = i + 1
    return rows


def analysis_sections(d, m):
    """All six analyses for month m ('YYYY-MM'), except prod_all which is
    all-time. Scopes (stated on the page too): orders status != 'cancelled'
    only (new + delivered; a delivered line's qty is already the delivered
    qty, so SUM(qty) is goods actually ordered/delivered); money net of
    per-line discounts with order-level discounts allocated pro-rata by line
    gross; cost = cost_act snapshot, falling back to the product's current
    actual_base (the same fallback /profit uses), with the fallback count
    reported. Purchases link to an outlet by outlet_id, or by name+area
    when the order has no outlet_id."""
    out = {}
    # --- visits in month (S2) + the visited set for S1 ---
    vrows = d.execute(
        "SELECT ol.id id, ol.name name, ol.area area, ol.shop_type st, COUNT(*) vc,"
        " COALESCE(SUM(v.kind='order'),0) oc, COALESCE(SUM(v.kind='probable'),0) pc"
        " FROM visits v JOIN outlets ol ON ol.id=v.outlet_id"
        " WHERE substr(v.vdate,1,7)=? GROUP BY ol.id ORDER BY vc DESC, ol.name",
        (m,)).fetchall()
    visits = [{'id': r['id'], 'name': r['name'], 'area': r['area'], 'st': r['st'],
               'vc': r['vc'], 'oc': r['oc'], 'pc': r['pc']} for r in vrows]
    _analysis_rank(visits)
    out['visits'] = {'rows': visits, 'bands': _analysis_bands(visits),
                     'total': sum(v['vc'] for v in visits)}
    # --- purchases in month per outlet (S1) ---
    buys = {}
    for r in d.execute(
            "SELECT outlet_id oid, COUNT(*) c FROM orders WHERE status!='cancelled'"
            " AND substr(odate,1,7)=? AND outlet_id IS NOT NULL GROUP BY outlet_id",
            (m,)).fetchall():
        buys[r['oid']] = r['c']
    na = {(r['name'], r['area']): r['id'] for r in
          d.execute("SELECT id, name, area FROM outlets").fetchall()}
    for r in d.execute(
            "SELECT outlet nm, area ar, COUNT(*) c FROM orders WHERE status!='cancelled'"
            " AND substr(odate,1,7)=? AND outlet_id IS NULL GROUP BY outlet, area",
            (m,)).fetchall():
        oid = na.get((r['nm'], r['ar']))
        if oid is not None:
            buys[oid] = buys.get(oid, 0) + r['c']
    by_type = {}
    for v in visits:
        b = buys.get(v['id'], 0)
        by_type.setdefault(v['st'], []).append(
            {'id': v['id'], 'name': v['name'], 'area': v['area'], 'buys': b})
    shops = {}
    for st, lst in by_type.items():
        lst.sort(key=lambda x: (-x['buys'], x['name']))
        ranked = _analysis_rank([x for x in lst if x['buys'] > 0])
        shops[st] = {'ranked': ranked, 'bands': _analysis_bands(ranked),
                     'never': [x for x in lst if x['buys'] == 0]}
    out['shops'] = shops
    out['shops_total_buys'] = sum(buys.get(v['id'], 0) for v in visits)
    # --- products: all-time (S3) and month (S4) ---
    def prod_qty(month):
        # Per-product sums are computed in a subquery over non-cancelled
        # orders FIRST, then LEFT JOINed to products: putting the status
        # filter in a LEFT JOIN's ON clause would keep cancelled lines
        # (the join just nulls the order side) and inflate the totals.
        if month:
            inner = ("SELECT oi.product_id pid, SUM(oi.qty) q FROM order_items oi"
                     " JOIN orders o ON o.id=oi.order_id"
                     " WHERE o.status!='cancelled' AND substr(o.odate,1,7)=?"
                     " GROUP BY oi.product_id")
            args = (month,)
        else:
            inner = ("SELECT oi.product_id pid, SUM(oi.qty) q FROM order_items oi"
                     " JOIN orders o ON o.id=oi.order_id"
                     " WHERE o.status!='cancelled' GROUP BY oi.product_id")
            args = ()
        q = ("SELECT p.id id, p.sku sku, p.name name, p.unit unit, p.sellable sell,"
             " COALESCE(s.q,0) q FROM products p LEFT JOIN (" + inner + ") s"
             " ON s.pid=p.id WHERE p.active=1 ORDER BY q DESC, p.name")
        rows = []
        for r in d.execute(q, args).fetchall():
            sell = r['sell'] if r['sell'] is not None else 1
            qq = r['q']
            if isinstance(qq, float) and qq.is_integer():
                qq = int(qq)
            rows.append({'id': r['id'], 'sku': r['sku'], 'name': r['name'],
                         'unit': r['unit'], 'sell': sell, 'q': qq})
        ranked = _analysis_rank([x for x in rows if x['q'] > 0])
        return {'ranked': ranked, 'bands': _analysis_bands(ranked),
                'unsold': [x for x in rows if x['q'] == 0 and x['sell'] == 1],
                'total_q': sum(x['q'] for x in rows)}
    out['prod_all'] = prod_qty(None)
    out['prod_month'] = prod_qty(m)
    # --- profit by product in month (S5) ---
    lines = d.execute(
        "SELECT oi.product_id pid, p.sku sku, p.name name, oi.qty qty, oi.price price,"
        " oi.discount ldisc, COALESCE(oi.cost_act, p.actual_base, 0) cost,"
        " CASE WHEN oi.cost_act IS NULL THEN 1 ELSE 0 END nosnap,"
        " o.discount odisc, o.id oid FROM order_items oi"
        " JOIN orders o ON o.id=oi.order_id JOIN products p ON p.id=oi.product_id"
        " WHERE o.status!='cancelled' AND substr(o.odate,1,7)=?", (m,)).fetchall()
    gross = {}
    for ln in lines:
        gross[ln['oid']] = gross.get(ln['oid'], 0.0) + ln['qty'] * ln['price']
    prof = {}
    nosnap = 0
    for ln in lines:
        g = gross[ln['oid']]
        alloc = ln['odisc'] * (ln['qty'] * ln['price'] / g) if g else 0.0
        lp = (ln['qty'] * ln['price'] - (ln['ldisc'] or 0) - alloc) - ln['cost'] * ln['qty']
        nosnap += ln['nosnap']
        e = prof.setdefault(ln['pid'], {'sku': ln['sku'], 'name': ln['name'], 'profit': 0.0})
        e['profit'] += lp
    plist = sorted(prof.values(), key=lambda x: (-x['profit'], x['name']))
    _analysis_rank(plist)
    out['profit'] = {'ranked': plist, 'bands': _analysis_bands(plist),
                     'total': sum(x['profit'] for x in plist), 'nosnap': nosnap}
    # --- expenses by category in month (S6) ---
    erows = [{'cat': r['cat'], 'tot': r['tot'], 'c': r['c']} for r in d.execute(
        "SELECT category cat, COALESCE(SUM(amount),0) tot, COUNT(*) c FROM expenses"
        " WHERE substr(edate,1,7)=? GROUP BY category ORDER BY tot DESC", (m,)).fetchall()]
    _analysis_rank(erows)
    out['expenses'] = {'rows': erows, 'bands': _analysis_bands(erows),
                       'total': sum(x['tot'] for x in erows)}
    return out
# ANALYSIS-END


# v93: one entry per analysis (Ko, 2026-10-29) — the six views are NOT
# sections of one page under စစ်ဆေး; each is its own dashboard-group entry
# under a Sales Analysis heading, opened individually.
ANALYSIS_SECTIONS = [
    ('shops', 'ဆိုင်များ — အမျိုးအစား အလိုက် ဝယ်ယူမှု'),
    ('visits', 'Outlet များ — ဝင်ခဲ့တဲ့ အကြိမ်ရေ'),
    ('products', 'ကုန်ပစ္စည်း — ရောင်းအား (စုစုပေါင်း)'),
    ('month', 'ကုန်ပစ္စည်း — အချိန်ကာလ အလိုက် (ဒီလ)'),
    ('profit', 'အမြတ် — ပစ္စည်း အလိုက် (ဒီလ)'),
    ('expenses', 'အသုံးစရိတ် — အမျိုးအစား အလိုက် (ဒီလ)'),
]


@app.route('/analysis')
@require_perm('analysis.view')
def analysis_page():
    """Sales analysis (Ko, 2026-10-29): six ranked views — shops by type x
    purchase frequency, outlets by visit frequency, products by sales
    (all-time), products by month (fast/regular/slow), profit by product,
    expenses by category. ?s= picks ONE view (default shops); the dashboard
    links each view individually under its own Sales Analysis heading.
    Month picker (YYYY-MM); Owner + Finance only because the profit view
    reveals cost."""
    d = db()
    m = (request.args.get('m') or today()[:7]).strip()
    if not re.match(r'^\d{4}-\d{2}$', m): m = today()[:7]
    s = request.args.get('s') or 'shops'
    if s not in [k for k, _ in ANALYSIS_SECTIONS]: s = 'shops'
    data = analysis_sections(d, m)
    shops = sorted(data['shops'].items(),
                   key=lambda kv: (SHOP_TYPE_KEYS.index(kv[0]) if kv[0] in SHOP_TYPE_KEYS else 99))
    return render_template('analysis.html', m=m, s=s, stitle=dict(ANALYSIS_SECTIONS)[s],
                           sections=ANALYSIS_SECTIONS, data=data, shops=shops,
                           **shop_type_ctx())


@app.route('/profit')
@require_perm('profit.view')
def profit_page():
    d = db()
    m = (request.args.get('m') or today()[:7]).strip()
    if not re.match(r'^\d{4}-\d{2}$', m): m = today()[:7]
    TOT = "(SELECT COALESCE(SUM(qty*price),0) FROM order_items WHERE order_id=o.id)-o.discount"
    sales = d.execute("SELECT COALESCE(SUM(" + TOT + "),0) v, COUNT(*) c FROM orders o WHERE o.status='delivered' AND substr(o.odate,1,7)=?", (m,)).fetchone()
    cogs = d.execute("SELECT COALESCE(SUM(oi.qty * COALESCE(oi.cost_act, p.actual_base, 0)),0) v, "
                     "COALESCE(SUM(CASE WHEN oi.cost_act IS NULL THEN 1 ELSE 0 END),0) nosnap, COUNT(*) lines "
                     "FROM order_items oi JOIN orders o ON o.id=oi.order_id JOIN products p ON p.id=oi.product_id "
                     "WHERE o.status='delivered' AND substr(o.odate,1,7)=?", (m,)).fetchone()
    cogs_base = d.execute("SELECT COALESCE(SUM(oi.qty * COALESCE(oi.cost_base, p.base_price, 0)),0) v, "
                     "COALESCE(SUM(CASE WHEN oi.cost_base IS NULL THEN 1 ELSE 0 END),0) nosnap, COUNT(*) lines "
                     "FROM order_items oi JOIN orders o ON o.id=oi.order_id JOIN products p ON p.id=oi.product_id "
                     "WHERE o.status='delivered' AND substr(o.odate,1,7)=?", (m,)).fetchone()
    cashin = d.execute("SELECT COALESCE(SUM(amount),0) v, COUNT(*) c FROM order_payments WHERE COALESCE(verified,1)=1 AND substr(rdate,1,7)=?", (m,)).fetchone()
    cashin_groups = []
    _gmap = {r['method'] or 'cash': r for r in d.execute(
        "SELECT COALESCE(method,'cash') method, COALESCE(SUM(amount),0) v, COUNT(*) c FROM order_payments "
        "WHERE COALESCE(verified,1)=1 AND substr(rdate,1,7)=? GROUP BY COALESCE(method,'cash')", (m,)).fetchall()}
    for _k, _lbl in PAY_METHODS:
        if _k in _gmap:
            cashin_groups.append({'key': _k, 'label': _lbl, 'v': _gmap[_k]['v'], 'c': _gmap[_k]['c']})
    credit_now = 0.0; credit_c = 0
    for co in d.execute("SELECT id FROM orders WHERE status='delivered' AND paid=0").fetchall():
        ct, _ = order_total(co['id']); b = ct - order_received(co['id'], d)
        if b > 0.5: credit_now += b; credit_c += 1
    rows = []
    for u in d.execute("SELECT o.sales_user uid, COALESCE(u.name,'') nm, u.role role, COALESCE(SUM(" + TOT + "),0) v, COUNT(*) c "
                       "FROM orders o LEFT JOIN users u ON u.id=o.sales_user WHERE o.status='delivered' AND substr(o.odate,1,7)=? "
                       "GROUP BY o.sales_user ORDER BY v DESC", (m,)).fetchall():
        cg = d.execute("SELECT COALESCE(SUM(oi.qty * COALESCE(oi.cost_act, p.actual_base, 0)),0) v FROM order_items oi JOIN orders o ON o.id=oi.order_id "
                       "JOIN products p ON p.id=oi.product_id WHERE o.status='delivered' AND substr(o.odate,1,7)=? AND o.sales_user=?", (m, u['uid'])).fetchone()['v']
        cgb = d.execute("SELECT COALESCE(SUM(oi.qty * COALESCE(oi.cost_base, p.base_price, 0)),0) v FROM order_items oi JOIN orders o ON o.id=oi.order_id "
                       "JOIN products p ON p.id=oi.product_id WHERE o.status='delivered' AND substr(o.odate,1,7)=? AND o.sales_user=?", (m, u['uid'])).fetchone()['v']
        rows.append({'name': u['nm'] or tr('(မသိ)'), 'office': u['role'] in ('owner', 'warehouse'), 'v': u['v'], 'c': u['c'], 'cogs': cg, 'profit': u['v'] - cg,
                     'cogs_base': cgb, 'profit_base': u['v'] - cgb})
    profit = sales['v'] - cogs['v']
    exp_cats = d.execute("SELECT category, COALESCE(SUM(amount),0) v, COUNT(*) c FROM expenses WHERE substr(edate,1,7)=? GROUP BY category ORDER BY v DESC", (m,)).fetchall()
    exp_total = sum(r['v'] for r in exp_cats)
    # v51: buying extras (ကားခ etc.) of purchases received this month — a
    # profit figure only; deliberately NOT part of the money book, whose
    # lines are payment records.
    buy_extra = d.execute("SELECT COALESCE(SUM(extra_cost),0) v, COUNT(*) c FROM purchases "
                          "WHERE status!='cancelled' AND COALESCE(extra_cost,0)>0 "
                          "AND substr(COALESCE(pdate, substr(created_at,1,10)),1,7)=?", (m,)).fetchone()
    buy_extra_v = buy_extra['v'] or 0
    net = profit - buy_extra_v - exp_total
    profit_base = sales['v'] - cogs_base['v']
    net_base = profit_base - buy_extra_v - exp_total
    settle_out = d.execute("SELECT COALESCE(SUM(final_amount),0) v, COUNT(*) c FROM settlements WHERE status='paid' AND substr(paid_at,1,7)=?", (m,)).fetchone()
    cash_out = (settle_out['v'] or 0) + exp_total
    return render_template('profit.html', m=m, sales=sales, cogs=cogs, profit=profit,
                           margin=(profit / sales['v'] * 100 if sales['v'] else 0),
                           cogs_base=cogs_base, profit_base=profit_base,
                           margin_base=(profit_base / sales['v'] * 100 if sales['v'] else 0),
                           net_base=net_base,
                           net_margin_base=(net_base / sales['v'] * 100 if sales['v'] else 0),
                           cashin=cashin, cashin_groups=cashin_groups, credit_now=credit_now, credit_c=credit_c, rows=rows,
                           exp_cats=exp_cats, exp_total=exp_total, net=net, buy_extra=buy_extra,
                           net_margin=(net / sales['v'] * 100 if sales['v'] else 0),
                           settle_out=settle_out, cash_out=cash_out,
                           cash_move=(cashin['v'] or 0) - cash_out, fmt_money=fmt_money)


def leader_prices_on(d):
    """Owner's on/off switch for the leader's /leader-prices access (Ko, 2026-10-18).
    Default ON (setting unset) so the current leader's access is unchanged."""
    return get_setting(d, 'leader_prices_on') != '0'

@app.route('/settings/leader-prices', methods=['POST'])
@require_perm('leader_prices.toggle')
def settings_leader_prices():
    d = db()
    set_setting(d, 'leader_prices_on', '1' if request.form.get('on') else '0')
    d.commit()
    flash(tr('Leader ဈေးနှုန်း ကြည့်ခွင့် ပြောင်းပြီးပြီ'))
    return redirect(url_for('leader_prices'))

@app.route('/leader-prices')
@login_required()
def leader_prices():
    # Trial feature (Ko, 2026-10-17): Sales Team Leader sees BASE price + Base-based
    # margins only, so he understands the margin built into Ko's prices. NEVER Actual,
    # NEVER B2C, NEVER the Base-Actual Difference (would leak Actual exactly).
    u = g.user
    if not has_perm(u, 'leader_prices.view'):
        abort(403)
    lp_on = leader_prices_on(db())
    if not has_perm(u, 'leader_prices.toggle') and not lp_on:
        abort(403)
    d = db()
    q = (request.args.get('q') or '').strip()
    cat = (request.args.get('cat') or '').strip()
    sql = "SELECT sku, name, category, unit, upc, base_price, retail, wholesale, sellable FROM products WHERE active=1"
    args = []
    if q:
        sql += " AND (name LIKE ? OR sku LIKE ?)"; args += ['%' + q + '%', '%' + q + '%']
    if cat:
        sql += " AND category=?"; args.append(cat)
    sql += " ORDER BY category, name"
    rows = []
    for r in d.execute(sql, args).fetchall():
        rows.append({'r': r,
                     'm_r': margin_pct(r['retail'], r['base_price']),
                     'm_w': margin_pct(r['wholesale'], r['base_price'])})
    cats = [r['category'] for r in d.execute("SELECT DISTINCT category FROM products WHERE active=1 AND category IS NOT NULL AND category<>'' ORDER BY category").fetchall()]
    return render_template('leader_prices.html', rows=rows, q=q, cat=cat, cats=cats, fmt_money=fmt_money, lp_on=lp_on)

@app.route('/settings/office', methods=['POST'])
@require_perm('settings.office')
def settings_office():
    d = db(); f = request.form
    if f.get('clear'):
        d.execute("DELETE FROM app_settings WHERE k IN ('office_lat','office_lng')")
        d.commit(); flash(tr('ရုံး တည်နေရာ ဖျက်ပြီးပြီ'))
    else:
        la, ln = _flt(f.get('lat')), _flt(f.get('lng'))
        if la is None or ln is None:
            flash(tr('GPS မရဘူး — ထပ်စမ်းပါ'))
        else:
            d.execute("INSERT OR REPLACE INTO app_settings(k, v) VALUES('office_lat', ?)", (repr(la),))
            d.execute("INSERT OR REPLACE INTO app_settings(k, v) VALUES('office_lng', ?)", (repr(ln),))
            d.commit(); flash(tr('ရုံး တည်နေရာ မှတ်ပြီးပြီ 🏢'))
    return redirect('/outlets')

@app.route('/settings/gps-mismatch', methods=['POST'])
@require_perm('settings.gps_threshold')
def settings_gps_mismatch():
    """Owner sets how far apart the sold and delivered places must be
    before the place warning is raised (metres, default 200)."""
    d = db()
    try:
        v = int(float((request.form.get('m') or '200').replace(',', '')))
    except (TypeError, ValueError):
        v = 200
    set_setting(d, 'gps_mismatch_m', str(max(20, min(v, 10000))))
    d.commit(); flash(tr('သိမ်းပြီးပြီ'))
    return redirect(url_for('delivery_board'))

@app.route('/settings/business', methods=['GET', 'POST'])
@require_perm('settings.business')
def settings_business():
    """Business profile (Ko, 2026-10-28): Owner edits the letterhead shown on
    invoices, settlement prints and order-issue slips — like a username setting:
    type the new value, save, done. Stored in app_settings; print headers read
    it via biz_info() with the old hardcoded values as defaults."""
    d = db()
    if request.method == 'POST':
        f = request.form
        set_setting(d, 'biz_name', (f.get('name') or '').strip())
        set_setting(d, 'biz_address', (f.get('address') or '').strip())
        set_setting(d, 'biz_phones', (f.get('phones') or '').strip())
        d.commit(); flash(tr('လုပ်ငန်း အချက်အလက် ပြင်ပြီးပြီ'))
        return redirect(url_for('settings_business'))
    return render_template('settings_business.html', biz=biz_info(d))

@app.route('/expenses')
@require_perm('expenses.view')
def expenses_page():
    d = db()
    m = (request.args.get('m') or today()[:7]).strip()
    if not re.match(r'^\d{4}-\d{2}$', m): m = today()[:7]
    rows = d.execute("""SELECT e.*, u.name uname FROM expenses e LEFT JOIN users u ON u.id=e.by_user
                        WHERE substr(e.edate,1,7)=? ORDER BY e.edate DESC, e.id DESC""", (m,)).fetchall()
    cats = d.execute("SELECT category, SUM(amount) v, COUNT(*) c FROM expenses WHERE substr(edate,1,7)=? GROUP BY category ORDER BY v DESC", (m,)).fetchall()
    total = sum(r['amount'] or 0 for r in rows)
    allcats = [r['category'] for r in d.execute("SELECT DISTINCT category FROM expenses WHERE category<>'' ORDER BY category").fetchall()]
    return render_template('expenses.html', rows=rows, cats=cats, total=total, m=m,
                           allcats=allcats, today_s=today(), fmt_money=fmt_money)

@app.route('/expenses/add', methods=['POST'])
@require_perm('expenses.manage')
def expense_add():
    d = db(); f = request.form
    edate = (f.get('edate') or '').strip()
    if not re.match(r'^\d{4}-\d{2}-\d{2}$', edate or ''): edate = today()
    cat = (f.get('category') or '').strip()
    det = (f.get('detail') or '').strip()
    method = (f.get('method') or 'cash').strip()
    if method not in PAY_METHOD_KEYS: method = 'cash'
    try: amt = float((f.get('amount') or '0').replace(',', ''))
    except Exception: amt = 0
    if not cat or amt <= 0:
        flash(tr('Category နဲ့ ပမာဏ (0 ထက် ကြီး) လိုပါတယ်'))
    else:
        # v60: optional note breakdown, Cash only (record only).
        denom_json, _dt = parse_denom(f, 'ea_') if method == 'cash' else (None, 0)
        d.execute("INSERT INTO expenses(edate, category, detail, amount, by_user, created_at, method, denom) VALUES(?,?,?,?,?,?,?,?)",
                  (edate, cat, det, amt, g.user['id'], now(), method, denom_json))
        d.commit(); flash(tr('အသုံးစရိတ် မှတ်ပြီ 💸'))
    return redirect('/expenses?m=' + edate[:7])

@app.route('/expenses/<int:eid>/edit', methods=['GET', 'POST'])
@require_perm('expenses.manage')
def expense_edit(eid):
    d = db()
    e = d.execute("SELECT * FROM expenses WHERE id=?", (eid,)).fetchone()
    if not e: abort(404)
    if request.method == 'POST':
        f = request.form
        edate = (f.get('edate') or '').strip()
        if not re.match(r'^\d{4}-\d{2}-\d{2}$', edate or ''): edate = e['edate']
        cat = (f.get('category') or '').strip()
        det = (f.get('detail') or '').strip()
        method = (f.get('method') or 'cash').strip()
        if method not in PAY_METHOD_KEYS: method = 'cash'
        try: amt = float((f.get('amount') or '0').replace(',', ''))
        except Exception: amt = e['amount']
        if cat and amt > 0:
            # v60: the counter is prefilled with the stored breakdown, so
            # an untouched save keeps it; Cash only — a non-cash method
            # clears it (notes only ever describe cash).
            denom_json, _dt = parse_denom(f, 'ee_') if method == 'cash' else (None, 0)
            d.execute("UPDATE expenses SET edate=?, category=?, detail=?, amount=?, method=?, denom=? WHERE id=?",
                      (edate, cat, det, amt, method, denom_json, eid))
            d.commit(); flash(tr('ပြင်ပြီ ✓'))
            return redirect('/expenses?m=' + edate[:7])
        flash(tr('Category နဲ့ ပမာဏ (0 ထက် ကြီး) လိုပါတယ်'))
    allcats = [r['category'] for r in d.execute("SELECT DISTINCT category FROM expenses WHERE category<>'' ORDER BY category").fetchall()]
    return render_template('expense_edit.html', e=e, allcats=allcats, fmt_money=fmt_money,
                           denom_vals=denom_counts(e['denom']))

@app.route('/expenses/<int:eid>/delete', methods=['POST'])
@require_perm('expenses.manage')
def expense_delete(eid):
    d = db()
    e = d.execute("SELECT * FROM expenses WHERE id=?", (eid,)).fetchone()
    if e:
        d.execute("DELETE FROM expenses WHERE id=?", (eid,))
        audit_log(d, g.user, 'expdel',
                  'အသုံးစရိတ် ဖျက် — %s: %s Ks (%s)' % (e['category'] or '', fmt_money(e['amount']), e['edate'] or ''),
                  'expense', eid, 0)
        d.commit()
        flash(tr('ဖျက်လိုက်ပြီ'))
        return redirect('/expenses?m=' + (e['edate'] or today())[:7])
    return redirect('/expenses')

def _risk_line(r):
    """One order line's risk entry, or None (shared arithmetic for the
    single-order and batched paths — identical numbers either way)."""
    cur = r['cur_act']
    snap = r['cost_act'] if r['cost_act'] is not None else cur
    if cur is None or snap is None or cur <= snap:
        return None
    qty = r['qty'] or 0
    if qty <= 0:
        return None
    net_unit = ((qty * (r['price'] or 0)) - (r['discount'] or 0)) / qty
    if net_unit < cur:
        tier, amt = 'red', qty * (cur - net_unit)
    else:
        tier, amt = 'yellow', qty * (cur - snap)
    return {'pid': r['pid'], 'pname': r['pname'], 'sku': r['sku'], 'qty': qty,
            'net_unit': net_unit, 'snap': snap, 'cur': cur, 'tier': tier, 'amt': amt}

def _risk_lines(d, oid):
    """Affected lines of one order: current Actual above the sale-time snapshot."""
    out = []
    rows = d.execute("""SELECT oi.product_id pid, oi.qty, oi.price, oi.discount, oi.cost_act,
                          p.name pname, p.sku, p.actual_base cur_act
                        FROM order_items oi JOIN products p ON p.id=oi.product_id
                        WHERE oi.order_id=? ORDER BY oi.id""", (oid,)).fetchall()
    for r in rows:
        e = _risk_line(r)
        if e:
            out.append(e)
    return out

def _price_review(d):
    """v52 (Ko, 2026-10-21): product-level price review — 'ဈေး ပြန် စစ် ရန်'.
    A product belongs when its Actual Base changed (per price_history) at a
    time AFTER its Retail/Wholesale were last set: the cost moved but nobody
    revisited the selling price, so the margin may have quietly shrunk.
    Display only — nothing here ever changes a price (Ko's standing rule).
    A '✓ စစ် ပြီး' mark hides the product until a NEWER Actual change
    arrives (same idea as risk_marks on invoices). Returns
    (open_rows, handled_rows); open rows sorted by today's Wholesale margin,
    thinnest first."""
    marks = {r['product_id']: r for r in
             d.execute("SELECT * FROM price_review_marks").fetchall()}
    names = {r['id']: r['name'] for r in d.execute("SELECT id, name FROM users").fetchall()}
    # v62: the whole price history in one pass, grouped per product in
    # memory (was one query per product — ~355 on the dashboard).
    hist_by_pid = {}
    for h in d.execute("SELECT product_id, field, old_price, new_price, created_at "
                       "FROM price_history ORDER BY product_id, id").fetchall():
        hist_by_pid.setdefault(h['product_id'], []).append(h)
    open_rows, handled_rows = [], []
    for p in d.execute("SELECT * FROM products WHERE active=1").fetchall():
        hist = hist_by_pid.get(p['id'], [])
        acts = [h for h in hist if h['field'] == 'actual_base']
        if not acts:
            continue  # cost never changed on record — nothing to review
        last_act = acts[-1]
        sells = [h for h in hist if h['field'] in ('retail', 'wholesale')]
        if sells and (sells[-1]['created_at'] or '') >= (last_act['created_at'] or ''):
            continue  # selling price set at/after the latest cost change
        if sells:
            st = sells[-1]['created_at'] or ''
            before = [h for h in acts if (h['created_at'] or '') <= st]
            old_act = before[-1]['new_price'] if before else acts[0]['old_price']
        else:
            old_act = acts[0]['old_price']  # value at creation, before any change
        cur = p['actual_base']
        if cur is None or old_act is None:
            continue
        chg = cur - old_act
        e = {'p': p, 'old_act': old_act, 'cur': cur, 'chg': chg,
             'pct': (chg / old_act * 100.0) if old_act else None,
             'chg_date': (last_act['created_at'] or '')[:10],
             'rm': margin_pct(p['retail'], cur), 'wm': margin_pct(p['wholesale'], cur)}
        m = marks.get(p['id'])
        if m and (m['marked_at'] or '') >= (last_act['created_at'] or ''):
            e['mark'] = m
            e['marker'] = names.get(m['marked_by'], '')
            handled_rows.append(e)
        else:
            open_rows.append(e)
    open_rows.sort(key=lambda e: (e['wm'] is None, e['wm'] if e['wm'] is not None else 0))
    handled_rows.sort(key=lambda e: e['mark']['marked_at'] or '', reverse=True)
    return open_rows, handled_rows

@app.route('/at-risk')
@require_perm('at_risk.view')
def at_risk():
    # 'E' helper (Ko, 2026-10-17): invoices still actionable (status 'new') whose
    # product cost (Actual Base) has risen since the sale-time snapshot.
    # Red: net line price below current Actual. Yellow: margin shrank.
    # Handled marks (Ko, 2026-10-17): a marked line hides while Actual stays at or
    # below the marked level; a further rise makes it reappear.
    d = db()
    td = today()
    day = (request.args.get('day') or td).strip()
    show_all = request.args.get('all') == '1'
    if not re.match(r'^\d{4}-\d{2}-\d{2}$', day or ''): day = td
    marks = {(r['order_id'], r['product_id']): r['cur_act'] for r in
             d.execute("SELECT order_id, product_id, cur_act FROM risk_marks").fetchall()}
    q = """SELECT o.id oid, o.code, o.outlet, o.odate, o.printed_at, u.name repname
           FROM orders o LEFT JOIN users u ON u.id=o.sales_user WHERE o.status='new'"""
    args = []
    if not show_all:
        q += " AND o.odate=?"
        args.append(day)
    q += " ORDER BY o.odate, o.id"
    orders_out = []
    tot_red = tot_yellow = 0.0
    _orows = d.execute(q, args).fetchall()
    # v62: every listed order's lines in one query (was one per order).
    _lines_by_oid = {}
    if _orows:
        _ph = ','.join('?' * len(_orows))
        for r in d.execute("""SELECT oi.order_id oid, oi.product_id pid, oi.qty, oi.price, oi.discount, oi.cost_act,
                                     p.name pname, p.sku, p.actual_base cur_act
                              FROM order_items oi JOIN products p ON p.id=oi.product_id
                              WHERE oi.order_id IN (%s) ORDER BY oi.order_id, oi.id""" % _ph,
                           [o['oid'] for o in _orows]).fetchall():
            e = _risk_line(r)
            if e:
                _lines_by_oid.setdefault(r['oid'], []).append(e)
    for o in _orows:
        lines = []
        for l in _lines_by_oid.get(o['oid'], []):
            m = marks.get((o['oid'], l['pid']))
            if m is not None and l['cur'] <= m:
                continue  # handled at this cost level
            lines.append(l)
        if not lines:
            continue
        red = sum(l['amt'] for l in lines if l['tier'] == 'red')
        yel = sum(l['amt'] for l in lines if l['tier'] == 'yellow')
        tot_red += red; tot_yellow += yel
        orders_out.append({'o': o, 'lines': lines, 'red': red, 'yel': yel, 'expo': red + yel})
    orders_out.sort(key=lambda x: -x['expo'])
    hq = """SELECT rm.order_id oid, o.code, o.outlet, o.odate, o.status,
              p.name pname, rm.cur_act, rm.marked_at, u.name marker
            FROM risk_marks rm JOIN orders o ON o.id=rm.order_id
            JOIN products p ON p.id=rm.product_id
            LEFT JOIN users u ON u.id=rm.marked_by"""
    hargs = []
    if not show_all:
        hq += " WHERE o.odate=?"
        hargs.append(day)
    hq += " ORDER BY rm.marked_at DESC, o.id"
    handled = {}
    for r in d.execute(hq, hargs).fetchall():
        h = handled.setdefault(r['oid'], {'o': r, 'lines': [], 'marked_at': r['marked_at'], 'marker': r['marker']})
        h['lines'].append(r)
    review_rows, review_handled = _price_review(d)
    return render_template('at_risk.html', orders=orders_out, handled=list(handled.values()),
                           day=day, show_all=show_all, tot_red=tot_red,
                           tot_yellow=tot_yellow, fmt_money=fmt_money,
                           review=review_rows, review_handled=review_handled)

@app.route('/at-risk/mark', methods=['POST'])
@require_perm('at_risk.mark')
def at_risk_mark():
    d = db()
    try:
        oid = int(request.form.get('order_id') or 0)
    except Exception:
        oid = 0
    o = d.execute("SELECT id FROM orders WHERE id=?", (oid,)).fetchone()
    if o:
        for l in _risk_lines(d, oid):
            d.execute("INSERT OR REPLACE INTO risk_marks(order_id, product_id, cur_act, marked_by, marked_at) VALUES(?,?,?,?,?)",
                      (oid, l['pid'], l['cur'], g.user['id'], now()))
        d.commit()
        flash(tr('ကိုင်တွယ်ပြီး မှတ်လိုက်ပြီ ✓'))
    day = (request.form.get('day') or '').strip()
    if request.form.get('all') == '1':
        return redirect('/at-risk?all=1')
    return redirect('/at-risk' + (('?day=' + day) if re.match(r'^\d{4}-\d{2}-\d{2}$', day or '') else ''))

@app.route('/at-risk/unmark', methods=['POST'])
@require_perm('at_risk.mark')
def at_risk_unmark():
    d = db()
    try:
        oid = int(request.form.get('order_id') or 0)
    except Exception:
        oid = 0
    d.execute("DELETE FROM risk_marks WHERE order_id=?", (oid,))
    d.commit()
    flash(tr('မှတ်တာ ပြန်ဖျက်လိုက်ပြီ'))
    day = (request.form.get('day') or '').strip()
    if request.form.get('all') == '1':
        return redirect('/at-risk?all=1')
    return redirect('/at-risk' + (('?day=' + day) if re.match(r'^\d{4}-\d{2}-\d{2}$', day or '') else ''))

def _review_redirect():
    day = (request.form.get('day') or '').strip()
    if request.form.get('all') == '1':
        return redirect('/at-risk?all=1#price-review')
    if re.match(r'^\d{4}-\d{2}-\d{2}$', day or ''):
        return redirect('/at-risk?day=' + day + '#price-review')
    return redirect('/at-risk#price-review')

@app.route('/at-risk/review-mark', methods=['POST'])
@require_perm('at_risk.mark')
def at_risk_review_mark():
    # v52: '✓ စစ် ပြီး' for one product on the price-review list. The mark is
    # stored against the current Actual; the product stays hidden until a
    # newer Actual change arrives (see _price_review).
    d = db()
    try:
        pid = int(request.form.get('product_id') or 0)
    except Exception:
        pid = 0
    p = d.execute("SELECT id, actual_base FROM products WHERE id=?", (pid,)).fetchone()
    if p:
        d.execute("INSERT OR REPLACE INTO price_review_marks(product_id, marked_act, marked_by, marked_at) VALUES(?,?,?,?)",
                  (pid, p['actual_base'], g.user['id'], now()))
        d.commit()
        flash(tr('ကိုင်တွယ်ပြီး မှတ်လိုက်ပြီ ✓'))
    return _review_redirect()

@app.route('/at-risk/review-unmark', methods=['POST'])
@require_perm('at_risk.mark')
def at_risk_review_unmark():
    d = db()
    try:
        pid = int(request.form.get('product_id') or 0)
    except Exception:
        pid = 0
    d.execute("DELETE FROM price_review_marks WHERE product_id=?", (pid,))
    d.commit()
    flash(tr('မှတ်တာ ပြန်ဖျက်လိုက်ပြီ'))
    return _review_redirect()

@app.route('/visits')
@require_perm('visits.view')
def visits_page():
    d = db()
    td = today()
    f = (request.args.get('f') or td).strip()
    t = (request.args.get('t') or td).strip()
    if not re.match(r'^\d{4}-\d{2}-\d{2}$', f or ''): f = td
    if not re.match(r'^\d{4}-\d{2}-\d{2}$', t or ''): t = td
    if f > t: f, t = t, f
    rep = (request.args.get('rep') or '').strip()
    TOT = "(SELECT COALESCE(SUM(qty*price),0) FROM order_items WHERE order_id=o.id)-o.discount"
    q = """SELECT v.*, ot.name AS oname, ot.area AS oarea, u.name AS uname, u.team AS uteam,
      (SELECT COALESCE(SUM({TOT}),0) FROM orders o WHERE o.outlet_id=v.outlet_id AND o.sales_user=v.user_id AND o.odate=v.vdate AND o.status<>'cancelled') AS samt,
      (SELECT COUNT(*) FROM orders o WHERE o.outlet_id=v.outlet_id AND o.sales_user=v.user_id AND o.odate=v.vdate AND o.status<>'cancelled') AS scnt
      FROM visits v JOIN outlets ot ON ot.id=v.outlet_id JOIN users u ON u.id=v.user_id
      WHERE v.vdate>=? AND v.vdate<=?""".format(TOT=TOT)
    args = [f, t]
    if rep:
        q += " AND v.user_id=?"; args.append(int(rep))
    q += " ORDER BY v.vdate DESC, v.created_at DESC LIMIT 500"
    rows = d.execute(q, args).fetchall()
    sums = d.execute("""SELECT u.id, u.name, u.team, COUNT(*) tot,
        SUM(CASE WHEN v.kind='order' THEN 1 ELSE 0 END) eff,
        SUM(CASE WHEN v.kind='probable' THEN 1 ELSE 0 END) prob,
        COUNT(DISTINCT v.outlet_id) shops
        FROM visits v JOIN users u ON u.id=v.user_id
        WHERE v.vdate>=? AND v.vdate<=? GROUP BY u.id ORDER BY tot DESC""", (f, t)).fetchall()
    reps = d.execute("SELECT id, name, team FROM users WHERE role='sales' AND active=1 ORDER BY name").fetchall()
    return render_template('visits.html', rows=rows, sums=sums, reps=reps, f=f, tend=t,
                           rep=rep, month_start=td[:7] + '-01', fmt_money=fmt_money)

@app.route('/sales-report')
@require_perm('sales_report.view')
def sales_report():
    d = db()
    f = (request.args.get('f') or '').strip()
    t = (request.args.get('t') or '').strip()
    if not re.match(r'^\d{4}-\d{2}-\d{2}$', f or ''): f = today()[:8] + '01'
    if not re.match(r'^\d{4}-\d{2}-\d{2}$', t or ''): t = today()
    if f > t: f, t = t, f
    TOT = "(SELECT COALESCE(SUM(qty*price),0) FROM order_items WHERE order_id=o.id)-o.discount"
    def status_map(where, args):
        m = {}
        for r in d.execute("SELECT o.status st, COALESCE(SUM(" + TOT + "),0) v, COUNT(*) c FROM orders o WHERE " + where + " GROUP BY o.status", args).fetchall():
            m[r['st']] = {'v': r['v'], 'c': r['c']}
        z = {'v': 0, 'c': 0}
        allv = sum(x['v'] for x in m.values()); allc = sum(x['c'] for x in m.values())
        return {'all': {'v': allv, 'c': allc}, 'delivered': m.get('delivered', z), 'new': m.get('new', z), 'cancelled': m.get('cancelled', z)}
    # v62: every still-open delivered invoice in the period, batched once
    # (was two queries per invoice, repeated per rep and again for office).
    _open_credit = d.execute("SELECT id, sales_user FROM orders WHERE status='delivered' AND paid=0 AND odate BETWEEN ? AND ?", (f, t)).fetchall()
    _ctot = order_totals_map(d, [co['id'] for co in _open_credit])
    _crec = order_received_map(d, [co['id'] for co in _open_credit])
    _credit_by_user = {}
    for co in _open_credit:
        e = _credit_by_user.setdefault(co['sales_user'], [0.0, 0])
        e[0] += _ctot.get(co['id'], (0, 0))[0] - _crec.get(co['id'], 0); e[1] += 1
    _office_ids = {r['id'] for r in d.execute("SELECT id FROM users WHERE role IN ('owner','warehouse')").fetchall()}
    reps = []
    for u in d.execute("SELECT id, name, team, active FROM users WHERE role='sales' ORDER BY active DESC, name").fetchall():
        uid = u['id']
        sm = status_map("o.sales_user=? AND o.odate BETWEEN ? AND ?", (uid, f, t))
        rp = d.execute("SELECT COUNT(*) c FROM orders o WHERE o.sales_user=? AND o.status!='cancelled' AND o.paid=1 AND o.odate BETWEEN ? AND ?", (uid, f, t)).fetchone()
        recv = d.execute("SELECT COALESCE(SUM(p.amount),0) v FROM order_payments p JOIN orders o ON o.id=p.order_id WHERE o.sales_user=? AND o.status!='cancelled' AND COALESCE(p.verified,1)=1 AND o.odate BETWEEN ? AND ?", (uid, f, t)).fetchone()
        credit_v, credit_c = _credit_by_user.get(uid, (0.0, 0))
        sk = d.execute("SELECT COUNT(DISTINCT oi.product_id) c FROM order_items oi JOIN orders o ON o.id=oi.order_id WHERE o.sales_user=? AND o.status!='cancelled' AND o.odate BETWEEN ? AND ?", (uid, f, t)).fetchone()['c']
        ol = d.execute("SELECT COUNT(DISTINCT outlet) c FROM orders WHERE sales_user=? AND status!='cancelled' AND odate BETWEEN ? AND ?", (uid, f, t)).fetchone()['c']
        nc_v = sm['all']['v'] - sm['cancelled']['v']; nc_c = sm['all']['c'] - sm['cancelled']['c']
        newol = d.execute("SELECT COUNT(*) c FROM outlets WHERE created_by=? AND first_seen BETWEEN ? AND ?", (uid, f, t)).fetchone()['c']
        reps.append({'id': uid, 'name': u['name'], 'team': u['team'] or '-', 'active': u['active'], 'sm': sm,
                     'avg': (nc_v / nc_c) if nc_c else 0, 'recv_v': recv['v'], 'paid_c': rp['c'],
                     'credit_v': credit_v, 'credit_c': credit_c, 'skus': sk, 'outlets': ol, 'new_outlets': newol})
    team_names = [r['tm'] for r in d.execute("SELECT DISTINCT COALESCE(NULLIF(o.team,''),'-') tm FROM orders o WHERE o.sales_user IN (SELECT id FROM users WHERE role='sales') AND o.odate BETWEEN ? AND ?", (f, t)).fetchall()]
    teams = []
    for tm in team_names:
        sm = status_map("o.sales_user IN (SELECT id FROM users WHERE role='sales') AND COALESCE(NULLIF(o.team,''),'-')=? AND o.odate BETWEEN ? AND ?", (tm, f, t))
        teams.append({'tm': tm, 'sm': sm})
    teams.sort(key=lambda x: -x['sm']['all']['v'])
    OFFICE_SCOPE = "o.sales_user IN (SELECT id FROM users WHERE role IN ('owner','warehouse'))"
    office_sm = status_map(OFFICE_SCOPE + " AND o.odate BETWEEN ? AND ?", (f, t))
    orp = d.execute("SELECT COUNT(*) c FROM orders o WHERE " + OFFICE_SCOPE + " AND o.status!='cancelled' AND o.paid=1 AND o.odate BETWEEN ? AND ?", (f, t)).fetchone()
    orecv = d.execute("SELECT COALESCE(SUM(p.amount),0) v FROM order_payments p JOIN orders o ON o.id=p.order_id WHERE " + OFFICE_SCOPE.replace('o.sales_user', 'o.sales_user') + " AND o.status!='cancelled' AND COALESCE(p.verified,1)=1 AND o.odate BETWEEN ? AND ?", (f, t)).fetchone()
    ocredit_v = 0; ocredit_c = 0
    for _uid2, (_v2, _c2) in _credit_by_user.items():
        if _uid2 in _office_ids:
            ocredit_v += _v2; ocredit_c += _c2
    office = {'sm': office_sm, 'recv_v': orecv['v'], 'paid_c': orp['c'], 'credit_v': ocredit_v, 'credit_c': ocredit_c}
    rep_sel = request.args.get('rep') or ''
    SALES_SCOPE = "o.sales_user IN (SELECT id FROM users WHERE role='sales')"
    sku_where = SALES_SCOPE + " AND o.odate BETWEEN ? AND ?"
    sku_args = [f, t]
    sel_name = tr('Rep အားလုံး')
    if rep_sel == 'office':
        sku_where = OFFICE_SCOPE + " AND o.odate BETWEEN ? AND ?"
        sel_name = tr('🏢 ရုံး အရောင်း')
    elif rep_sel == 'all':
        sku_where = "o.odate BETWEEN ? AND ?"
        sel_name = tr('အကုန်လုံး (Rep + ရုံး)')
    elif rep_sel.isdigit():
        sku_where = SALES_SCOPE + " AND o.odate BETWEEN ? AND ? AND o.sales_user=?"
        sku_args.append(int(rep_sel))
        uu = d.execute("SELECT name FROM users WHERE id=?", (int(rep_sel),)).fetchone()
        if uu: sel_name = uu['name']
    skus = d.execute("SELECT p.sku sku, p.name name, p.unit unit, SUM(oi.qty) q_all, "
                     "SUM(CASE WHEN o.status='delivered' THEN oi.qty ELSE 0 END) q_del, "
                     "SUM(CASE WHEN o.status='new' THEN oi.qty ELSE 0 END) q_new, "
                     "SUM(CASE WHEN o.status='cancelled' THEN oi.qty ELSE 0 END) q_can, "
                     "SUM(CASE WHEN o.status!='cancelled' THEN oi.qty*oi.price - COALESCE(oi.discount,0) ELSE 0 END) amt, "
                     "COUNT(DISTINCT CASE WHEN o.status!='cancelled' THEN o.id END) ords "
                     "FROM order_items oi JOIN orders o ON o.id=oi.order_id JOIN products p ON p.id=oi.product_id WHERE " + sku_where +
                     " GROUP BY oi.product_id ORDER BY amt DESC LIMIT 80", sku_args).fetchall()
    # v65 (Ko, 2026-10-23): sales by SHOP TYPE — which outlet types each
    # rep actually sells to. Invoice counts + amounts over the same
    # period, cancelled excluded (the basis of the received figures
    # above). Orders join their outlet record via outlet_id; an order
    # with no matched outlet, or whose outlet was never typed, lands
    # in 'unset'. v69: 13 types, so each block renders as TYPE ROWS
    # (only types with sales, plus the unset row always) instead of
    # columns — 13 columns would not fit a phone screen.
    _tbuckets = SHOP_TYPE_KEYS + ['unset']
    _tph = ','.join('?' * len(SHOP_TYPE_KEYS))
    _tmap = {}
    for r in d.execute(
            "SELECT o.sales_user uid, CASE WHEN ou.shop_type IN (" + _tph + ") THEN ou.shop_type ELSE 'unset' END bucket, "
            "COUNT(*) c, COALESCE(SUM(" + TOT + "),0) v FROM orders o "
            "LEFT JOIN outlets ou ON ou.id=o.outlet_id "
            "WHERE o.status!='cancelled' AND o.odate BETWEEN ? AND ? GROUP BY o.sales_user, bucket",
            tuple(SHOP_TYPE_KEYS) + (f, t)).fetchall():
        _tmap.setdefault(r['uid'], {})[r['bucket']] = {'c': r['c'], 'v': r['v']}
    def _trow(name, uids):
        row = {'name': name, 'cells': {b: {'c': 0, 'v': 0.0} for b in _tbuckets}}
        for _u in uids:
            for _b in _tbuckets:
                _e = _tmap.get(_u, {}).get(_b)
                if _e: row['cells'][_b]['c'] += _e['c']; row['cells'][_b]['v'] += _e['v']
        return row
    type_rows = [_trow(r['name'], [r['id']]) for r in reps]
    type_rows.append(_trow(tr('🏢 ရုံး အရောင်း'), sorted(_office_ids)))
    type_tot = _trow(tr('စုစုပေါင်း'), list(_tmap.keys()))
    return render_template('sales_report.html', reps=reps, teams=teams, skus=skus, office=office, f=f, tend=t, rep_sel=rep_sel,
                           sel_name=sel_name, today_s=today(), month_start=today()[:8] + '01',
                           type_rows=type_rows, type_tot=type_tot, **shop_type_ctx())


# ---------------- Users (owner only) ----------------
@app.route('/users', methods=['GET', 'POST'])
@require_perm('users.manage')
def users():
    d = db()
    if request.method == 'POST':
        un = request.form.get('username', '').strip(); nm = request.form.get('name', '').strip()
        pw = request.form.get('password', '')
        team = request.form.get('team', '').strip()
        # v75: new form posts role_id; legacy clients may post a role name.
        rrow = None
        rid = request.form.get('role_id')
        if rid:
            try: rrow = d.execute("SELECT * FROM roles WHERE id=?", (int(rid),)).fetchone()
            except Exception: rrow = None
        if rrow is None and request.form.get('role'):
            rrow = d.execute("SELECT * FROM roles WHERE name=?", (request.form.get('role'),)).fetchone()
        if not un or not nm or rrow is None or len(pw) < 6:
            flash(tr('အချက်အလက် မပြည့်စုံဘူး (password အနည်းဆုံး ၆ လုံး)'))
        elif d.execute("SELECT 1 FROM users WHERE username=?", (un,)).fetchone():
            flash(tr('Username ရှိပြီးသား ဖြစ်နေတယ်'))
        else:
            d.execute("INSERT INTO users(username,pw_hash,name,role,role_id,team,created_at) VALUES(?,?,?,?,?,?,?)",
                      (un, generate_password_hash(pw), nm, rrow['name'], rrow['id'], team, now()))
            d.commit(); flash(tr('{nm} ({role}) account ဆောက်ပြီးပြီ', nm=nm, role=ROLES.get(rrow['name'], rrow['name'])))
        return redirect(url_for('users'))
    rows = []
    for r in d.execute("SELECT u.*, rl.name role_name, dp.name dept_name FROM users u LEFT JOIN roles rl ON rl.id=u.role_id LEFT JOIN departments dp ON dp.id=u.department_id ORDER BY u.role, u.name").fetchall():
        rr = dict(r); rr['role_label'] = ROLES.get(rr['role_name'] or rr['role'], rr['role_name'] or rr['role'])
        rows.append(rr)
    teams = [r[0] for r in d.execute("SELECT DISTINCT team FROM users WHERE team IS NOT NULL AND team!='' ORDER BY team").fetchall()]
    roles = [dict(r) for r in d.execute("SELECT r.*, (SELECT COUNT(*) FROM users WHERE role_id=r.id) uc FROM roles r ORDER BY r.is_system DESC, r.name").fetchall()]
    for rl in roles: rl['label'] = ROLES.get(rl['name'], rl['name'])
    depts = d.execute("SELECT * FROM departments ORDER BY name").fetchall()
    return render_template('users.html', rows=rows, teams=teams, roles=roles, departments=depts)

# ---------------- Areas (owner only) ----------------
@app.route('/areas', methods=['GET', 'POST'])
@require_perm('areas.manage')
def areas():
    d = db()
    if request.method == 'POST':
        nm = request.form.get('name', '').strip()
        if nm:
            d.execute("INSERT OR IGNORE INTO areas(name) VALUES(?)", (nm,)); d.commit()
            flash(tr('{nm} ထည့်ပြီးပြီ', nm=nm))
        return redirect(url_for('areas'))
    rows = d.execute("SELECT * FROM areas ORDER BY name").fetchall()
    return render_template('areas.html', rows=rows)

@app.route('/areas/<int:aid>/delete', methods=['POST'])
@require_perm('areas.manage')
def area_delete(aid):
    d = db()
    d.execute("DELETE FROM areas WHERE id=?", (aid,)); d.commit()
    flash(tr('ရပ်ကွက် ဖျက်ပြီးပြီ'))
    return redirect(url_for('areas'))


@app.route('/users/<int:uid>/edit', methods=['POST'])
@require_perm('users.manage')
def user_edit(uid):
    d = db()
    if 'name' in request.form:
        nm = request.form.get('name', '').strip()
        if nm:
            d.execute("UPDATE users SET name=? WHERE id=?", (nm, uid)); d.commit()
            flash(tr('နာမည် ပြင်ပြီးပြီ'))
    if 'username' in request.form:
        un = request.form.get('username', '').strip()
        if un:
            clash = d.execute("SELECT id FROM users WHERE username=? AND id!=?", (un, uid)).fetchone()
            if clash:
                flash(tr('အဲဒီ Username ကို တခြားသူ သုံးနေတယ် — မပြောင်းဘူး'))
            else:
                d.execute("UPDATE users SET username=? WHERE id=?", (un, uid)); d.commit()
                flash(tr('Username ပြင်ပြီးပြီ'))
    return redirect(url_for('users'))

@app.route('/users/<int:uid>/team', methods=['POST'])
@require_perm('users.manage')
def user_team(uid):
    team = request.form.get('team', '').strip()
    d = db()
    d.execute("UPDATE users SET team=? WHERE id=?", (team, uid)); d.commit()
    flash(tr('Team တပ် ပြောင်းပြီးပြီ (အတိတ် ဘောက်ချာတွေ မပြောင်း)'))
    return redirect(url_for('users'))

@app.route('/users/<int:uid>/password', methods=['POST'])
@require_perm('users.manage')
def user_password(uid):
    pw = request.form.get('password', '')
    if len(pw) < 6:
        flash(tr('Password အနည်းဆုံး ၆ လုံး ရှိရမယ်'))
    else:
        d = db()
        d.execute("UPDATE users SET pw_hash=? WHERE id=?", (generate_password_hash(pw), uid)); d.commit()
        flash(tr('Password အသစ် ပြောင်းပြီးပြီ'))
    return redirect(url_for('users'))

@app.route('/users/<int:uid>/leader')
@require_perm('users.leader_toggle')
def user_leader(uid):
    d = db()
    d.execute("UPDATE users SET is_leader=1-COALESCE(is_leader,0) WHERE id=? AND role='sales'", (uid,)); d.commit()
    return redirect(url_for('users'))

@app.route('/users/<int:uid>/toggle')
@require_perm('users.manage')
def user_toggle(uid):
    d = db()
    if uid != g.user['id']:
        d.execute("UPDATE users SET active=1-active WHERE id=?", (uid,)); d.commit()
    return redirect(url_for('users'))



# ---------------- v75: Roles (data bundles) + Departments (labels only) ----------------
# Owner-only. Department assignment touches ONLY users.department_id — by
# construction it cannot change any permission (no permission logic reads
# department_id anywhere in this file).
@app.route('/users/<int:uid>/role', methods=['POST'])
@require_perm('users.manage')
def user_assign_role(uid):
    d = db()
    if uid == g.user['id']:
        flash(tr('ကိုယ့် အခန်း ကိုယ် မပြောင်းပါနဲ့ — လော့ခ် ဖြစ်သွားနိုင်တယ်')); return redirect(url_for('users'))
    rrow = None
    rid = request.form.get('role_id')
    if rid:
        try: rrow = d.execute("SELECT * FROM roles WHERE id=?", (int(rid),)).fetchone()
        except Exception: rrow = None
    if rrow is None and request.form.get('role'):
        rrow = d.execute("SELECT * FROM roles WHERE name=?", (request.form.get('role'),)).fetchone()
    if rrow is None:
        flash(tr('အခန်း မတွေ့ဘူး')); return redirect(url_for('users'))
    d.execute("UPDATE users SET role=?, role_id=?, is_leader=CASE WHEN ?='sales' THEN COALESCE(is_leader,0) ELSE 0 END WHERE id=?",
              (rrow['name'], rrow['id'], rrow['name'], uid)); d.commit()
    # Clear the per-request permission memo so the next check re-reads.
    try: g._perms_memo = None
    except Exception: pass
    flash(tr('အခန်း ပြောင်းပြီးပြီ')); return redirect(url_for('users'))

@app.route('/users/<int:uid>/department', methods=['POST'])
@require_perm('users.manage')
def user_assign_department(uid):
    d = db()
    did = (request.form.get('department_id') or '').strip()
    if did:
        try: didv = int(did)
        except Exception: didv = None
        if didv is not None and not d.execute("SELECT 1 FROM departments WHERE id=?", (didv,)).fetchone():
            didv = None
    else:
        didv = None
    d.execute("UPDATE users SET department_id=? WHERE id=?", (didv, uid)); d.commit()
    flash(tr('ဌာန ပြောင်းပြီးပြီ — ခွင့်ပြုချက် မပြောင်းဘူး')); return redirect(url_for('users'))

@app.route('/roles', methods=['GET', 'POST'])
@require_perm('roles.manage')
def roles_list():
    d = db()
    if request.method == 'POST':
        nm = (request.form.get('name') or '').strip()
        if not nm:
            flash(tr('အခန်း နာမည် လိုပါတယ်'))
        elif d.execute("SELECT 1 FROM roles WHERE name=?", (nm,)).fetchone():
            flash(tr('အဲဒီ အခန်း ရှိပြီးသား ဖြစ်နေတယ်'))
        else:
            d.execute("INSERT INTO roles(name, is_system) VALUES(?,0)", (nm,)); d.commit()
            flash(tr('အခန်း အသစ် ဆောက်ပြီးပြီ'))
        return redirect(url_for('roles_list'))
    rows = []
    for r in d.execute("SELECT r.*, (SELECT COUNT(*) FROM users WHERE role_id=r.id) uc, (SELECT COUNT(*) FROM role_permissions WHERE role_id=r.id) pc FROM roles r ORDER BY r.is_system DESC, r.name").fetchall():
        rr = dict(r); rr['label'] = ROLES.get(rr['name'], rr['name']); rows.append(rr)
    return render_template('roles.html', rows=rows)

@app.route('/roles/<int:rid>/edit', methods=['GET', 'POST'])
@require_perm('roles.manage')
def role_edit(rid):
    d = db()
    r = d.execute("SELECT * FROM roles WHERE id=?", (rid,)).fetchone()
    if not r: abort(404)
    if request.method == 'POST':
        if r['name'] == 'owner':
            # Owner role's permissions can never be stripped.
            flash(tr('Owner အခန်းရဲ့ ခွင့်ပြုချက်တွေကို ဖြုတ်လို့ မရဘူး'))
            return redirect(url_for('roles_list'))
        nm = (request.form.get('name') or '').strip()
        if nm and nm != r['name']:
            if r['is_system']:
                flash(tr('System အခန်းရဲ့ နာမည်ကို ပြောင်းလို့ မရဘူး'))
            elif d.execute("SELECT 1 FROM roles WHERE name=? AND id!=?", (nm, rid)).fetchone():
                flash(tr('အဲဒီ အခန်း ရှိပြီးသား ဖြစ်နေတယ်'))
            else:
                d.execute("UPDATE roles SET name=? WHERE id=?", (nm, rid))
                d.execute("UPDATE users SET role=? WHERE role_id=?", (nm, rid)); d.commit()
                r = d.execute("SELECT * FROM roles WHERE id=?", (rid,)).fetchone()
        _allk = list(PERMISSIONS)
        picked = set()
        for _v in request.form.getlist('perm'):
            try: _i = int(_v)
            except Exception: continue
            if 0 <= _i < len(_allk): picked.add(_allk[_i])
        d.execute("DELETE FROM role_permissions WHERE role_id=?", (rid,))
        for k in picked:
            d.execute("INSERT INTO role_permissions(role_id, perm_key) VALUES(?,?)", (rid, k))
        d.commit(); flash(tr('အခန်း ပြင်ပြီးပြီ')); return redirect(url_for('roles_list'))
    cur = {x[0] for x in d.execute("SELECT perm_key FROM role_permissions WHERE role_id=?", (rid,)).fetchall()}
    uc = d.execute("SELECT COUNT(*) c FROM users WHERE role_id=?", (rid,)).fetchone()['c']
    groups = []
    for gk in PERM_GROUP_ORDER:
        ks = [k for k, v in PERMISSIONS.items() if v[0] == gk]
        groups.append({'key': gk, 'label': PERM_GROUP_LABELS[gk][0], 'perms': ks})
    return render_template('role_edit.html', role=r, label=ROLES.get(r['name'], r['name']), cur=cur, uc=uc, groups=groups, perms_meta=PERMISSIONS, key_idx={k: i for i, k in enumerate(PERMISSIONS)})

@app.route('/roles/<int:rid>/delete', methods=['POST'])
@require_perm('roles.manage')
def role_delete(rid):
    d = db()
    r = d.execute("SELECT * FROM roles WHERE id=?", (rid,)).fetchone()
    if not r: abort(404)
    if r['is_system']:
        flash(tr('System အခန်း — ဖျက်လို့ မရဘူး')); return redirect(url_for('roles_list'))
    if d.execute("SELECT COUNT(*) c FROM users WHERE role_id=?", (rid,)).fetchone()['c'] > 0:
        flash(tr('ဒီ အခန်းကို အသုံးပြုသူ ရှိသေးလို့ ဖျက်လို့ မရဘူး')); return redirect(url_for('roles_list'))
    d.execute("DELETE FROM role_permissions WHERE role_id=?", (rid,))
    d.execute("DELETE FROM roles WHERE id=?", (rid,)); d.commit()
    flash(tr('အခန်း ဖျက်ပြီးပြီ')); return redirect(url_for('roles_list'))

@app.route('/departments', methods=['GET', 'POST'])
@require_perm('departments.manage')
def departments_list():
    d = db()
    if request.method == 'POST':
        nm = (request.form.get('name') or '').strip()
        if not nm:
            flash(tr('ဌာန နာမည် လိုပါတယ်'))
        elif d.execute("SELECT 1 FROM departments WHERE name=?", (nm,)).fetchone():
            flash(tr('အဲဒီ ဌာန ရှိပြီးသား ဖြစ်နေတယ်'))
        else:
            d.execute("INSERT INTO departments(name) VALUES(?)", (nm,)); d.commit()
            flash(tr('ဌာန အသစ် ဆောက်ပြီးပြီ'))
        return redirect(url_for('departments_list'))
    rows = [dict(r) for r in d.execute("SELECT dp.*, (SELECT COUNT(*) FROM users WHERE department_id=dp.id) uc FROM departments dp ORDER BY dp.name").fetchall()]
    return render_template('departments.html', rows=rows)

@app.route('/departments/<int:did>/rename', methods=['POST'])
@require_perm('departments.manage')
def department_rename(did):
    d = db()
    nm = (request.form.get('name') or '').strip()
    if nm:
        if d.execute("SELECT 1 FROM departments WHERE name=? AND id!=?", (nm, did)).fetchone():
            flash(tr('အဲဒီ ဌာန ရှိပြီးသား ဖြစ်နေတယ်'))
        else:
            d.execute("UPDATE departments SET name=? WHERE id=?", (nm, did)); d.commit()
            flash(tr('ဌာန နာမည် ပြင်ပြီးပြီ'))
    return redirect(url_for('departments_list'))

@app.route('/departments/<int:did>/delete', methods=['POST'])
@require_perm('departments.manage')
def department_delete(did):
    d = db()
    # Deleting a department only unassigns its members to none — it never
    # touches any permission (permissions live on roles, not departments).
    d.execute("UPDATE users SET department_id=NULL WHERE department_id=?", (did,))
    d.execute("DELETE FROM departments WHERE id=?", (did,)); d.commit()
    flash(tr('ဌာန ဖျက်ပြီးပြီ — အဖွဲ့ဝင်တွေကို ဌာန မရှိအဖြစ် ပြန်ထားတယ်'))
    return redirect(url_for('departments_list'))


# ---------------- Products ----------------
def fmt_money(v):
    if v is None: return '-'
    return f"{v:,.0f}"

# v60: denomination counter (Ko, 2026-10-22) — ONE reusable cash-notes
# counter (Ks notes below) placed at the four cash points: the deliver
# form's collection entry, the cash-count close, the expense form and the
# settlement pay form. The breakdown is a stored RECORD (JSON) of how the
# cash was counted — nothing more: no total, balance, verification or
# report ever reads it. Filling it in is always optional and never blocks
# a save; the forms only ever appear where the money method is Cash.
# (The DENOMS constant itself sits with PAY_METHODS near the top.)

def parse_denom(form, prefix):
    """Read the counter fields '<prefix>denom_<value>' from a submitted
    form. Returns (json_text_or_None, total). A count that is not a clean
    non-negative integer is IGNORED (treated as 0) — junk typed into one
    box must never block a money record. The breakdown is returned only
    when at least one count is positive; an untouched counter gives
    (None, 0), which every caller stores as NULL (no record)."""
    counts = {}
    total = 0
    for v in DENOMS:
        raw = (form.get(f'{prefix}denom_{v}') or '').strip().replace(',', '')
        try:
            n = int(raw) if raw else 0
        except ValueError:
            n = 0
        if n < 0:
            n = 0
        if n > 0:
            counts[str(v)] = n
            total += v * n
    if not counts:
        return None, 0
    return json.dumps(counts, sort_keys=True), total

def denom_counts(json_text):
    """Stored breakdown -> {note_value_str: count} for form prefills."""
    if not json_text:
        return {}
    try:
        c = json.loads(json_text)
    except Exception:
        return {}
    if not isinstance(c, dict):
        return {}
    return {str(k): v for k, v in c.items()}

def denom_total(json_text):
    """Notes total of a stored breakdown, or None when there is no record."""
    counts = denom_counts(json_text)
    if not counts:
        return None
    try:
        return sum(int(k) * int(v) for k, v in counts.items())
    except Exception:
        return None

def fmt_denom(json_text):
    """Render a stored breakdown compactly, e.g.
    '10,000×12 · 5,000×3 = 135,000 Ks' (largest notes first, zero counts
    omitted). '' when there is no record. Digits only — same in every
    language, so the displayed line needs no translation."""
    counts = denom_counts(json_text)
    if not counts:
        return ''
    parts = []
    total = 0
    for v in DENOMS:
        n = counts.get(str(v)) or 0
        try:
            n = int(n)
        except Exception:
            n = 0
        if n > 0:
            parts.append(f'{v:,}×{n}')
            total += v * n
    if not parts:
        return ''
    return ' · '.join(parts) + f' = {total:,.0f} Ks'

app.jinja_env.filters['denom'] = fmt_denom

def margin_pct(price, cost):
    """True profit margin: (price - cost) / price * 100. None if not computable."""
    if not price or cost is None:
        return None
    return (price - cost) / price * 100.0

def prod_margins(r):
    diff = None
    if r['base_price'] and r['actual_base'] is not None:
        diff = (r['base_price'] - r['actual_base']) / r['base_price'] * 100.0
    return {
        'rb': margin_pct(r['retail'], r['base_price']),
        'wb': margin_pct(r['wholesale'], r['base_price']),
        'ra': margin_pct(r['retail'], r['actual_base']),
        'wa': margin_pct(r['wholesale'], r['actual_base']),
        'diff': diff,
    }


@app.route('/products')
@login_required()
def products():
    if not has_perm(g.user, 'products.view'): abort(403)
    q = request.args.get('q', '').strip(); cat = request.args.get('cat', '')
    sup = request.args.get('sup', ''); sell = request.args.get('sell', '')
    d = db()
    sql = ("SELECT p.*, COALESCE(s.qty,0) qty FROM products p LEFT JOIN stock s ON s.product_id=p.id WHERE p.active=1")
    args = []
    if q:
        sql += " AND (p.name LIKE ? OR p.sku LIKE ?)"; args += [f'%{q}%', f'%{q}%']
    if cat:
        sql += " AND p.category=?"; args.append(cat)
    if sup:
        sql += " AND p.supplier=?"; args.append(sup)
    if sell == 'on':
        sql += " AND p.sellable=1"
    elif sell == 'off':
        sql += " AND COALESCE(p.sellable,0)=0"
    sql += " ORDER BY p.sku"
    rows = d.execute(sql, args).fetchall()
    cats = [r['category'] for r in d.execute("SELECT DISTINCT category FROM products WHERE active=1 AND category!='' ORDER BY category").fetchall()]
    sups = [r['supplier'] for r in d.execute("SELECT DISTINCT supplier FROM products WHERE active=1 AND supplier!='' ORDER BY supplier").fetchall()]
    margins = {r['id']: prod_margins(r) for r in rows}
    closed = closed_supplier_names(d)
    _sup_counts = {r['supplier']: r['c'] for r in d.execute("SELECT supplier, COUNT(*) c FROM products WHERE active=1 GROUP BY supplier").fetchall()}
    sup_states = [{'name': sn, 'count': _sup_counts.get(sn, 0), 'closed': sn in closed} for sn in sups]
    # v46: show each SKU's own view of its family pool (full packs for a big
    # SKU, the whole pool for the base) + a small family mark.
    stockmap = {r['product_id']: r['qty'] for r in d.execute("SELECT product_id, qty FROM stock").fetchall()}
    sku_of = {r['id']: r['sku'] for r in d.execute("SELECT id, sku FROM products").fetchall()}
    members_of = {}
    for r in d.execute("SELECT id, sku, pool_id FROM products WHERE pool_id IS NOT NULL AND pool_id != id").fetchall():
        members_of.setdefault(r['pool_id'], []).append(r['sku'])
    rows = [dict(r) for r in rows]
    for r in rows:
        if r['pool_id'] and r['pool_id'] != r['id']:
            base, f = r['pool_id'], r['pack_factor'] or 1
        else:
            base, f = r['id'], 1
        pool = stockmap.get(base, 0)
        r['qty'] = int(pool / f) if f != 1 else pool
        r['fam'] = None
        if base != r['id']:
            r['fam'] = {'kind': 'big', 'partner': sku_of.get(base, ''), 'factor': f}
        elif r['id'] in members_of:
            r['fam'] = {'kind': 'base', 'partners': ', '.join(members_of[r['id']])}
    sug = pack_suggestions(d) if has_perm(g.user, 'products.pack_link') else []
    return render_template('products.html', rows=rows, q=q, cat=cat, sup=sup, sell=sell, cats=cats, sups=sups, fmt_money=fmt_money, margins=margins, sup_states=sup_states, closed_sups=closed, sug=sug)

@app.route('/products/new', methods=['GET', 'POST'])
@require_perm('products.create')
def product_new():
    return product_form(None)

@app.route('/products/<int:pid>/edit', methods=['GET', 'POST'])
@require_perm('products.edit')
def product_edit(pid):
    return product_form(pid)

PRICE_FIELDS = ('base_price', 'retail', 'wholesale', 'b2c', 'actual_base')

def log_price_changes(d, old, newvals, user_id):
    for f in PRICE_FIELDS:
        if f in newvals and old is not None and (old[f] or 0) != (newvals[f] or 0):
            d.execute("INSERT INTO price_history(product_id,field,old_price,new_price,by_user,created_at) VALUES(?,?,?,?,?,?)",
                      (old['id'], f, old[f], newvals[f], user_id, now()))

_AUDIT_PRICE_FIELDS = ('base_price', 'retail', 'wholesale', 'b2c', 'actual_base', 'risk_pct')

def _audit_price_save(d, p, vals, user):
    """v51 Mini Audit: one event per price-form save that changed a price
    field (or Risk %). It doubles as an Owner-only bell notice when the
    Actual changed, or Retail/Wholesale was cut by AUDIT_CUT_PCT+ at once."""
    parts = []
    notify = 0
    for f in _AUDIT_PRICE_FIELDS:
        if f not in vals: continue
        old, newv = (p[f] or 0), (vals[f] or 0)
        if old == newv: continue
        if f == 'risk_pct':
            parts.append('Risk %% %g → %g' % (old, newv))
        else:
            parts.append('%s %s → %s' % (_PRICE_FIELD_NAMES[f], fmt_money(old), fmt_money(newv)))
        if f == 'actual_base':
            notify = 1
        if f in ('retail', 'wholesale') and old > 0 and newv < old and (old - newv) / old * 100 >= AUDIT_CUT_PCT:
            notify = 1
    if parts:
        audit_log(d, user, 'price',
                  'ဈေး ပြောင်း — %s (%s): %s' % (p['name'], p['sku'], ', '.join(parts)),
                  'product', p['id'], notify)

def product_form(pid):
    d = db()
    p = d.execute("SELECT * FROM products WHERE id=?", (pid,)).fetchone() if pid else None
    role = user_role_name(g.user)  # v75: data role name (synced users.role)
    if request.method == 'POST':
        f = request.form
        def num(k):
            v = (f.get(k) or '').replace(',', '').strip()
            return float(v) if v else 0
        def numn(k):
            v = (f.get(k) or '').replace(',', '').strip()
            return float(v) if v else None
        data_vals = dict(category=f.get('category', '').strip(), name=f.get('name', '').strip(),
                         unit=f.get('unit', '').strip(), upc=numn('upc'), supplier=canon_supplier(d, f.get('supplier', '')))
        price_vals = dict(base_price=num('base_price'), retail=num('retail'), wholesale=num('wholesale'), b2c=num('b2c'))
        if has_perm(g.user, 'actual.edit'):
            price_vals['actual_base'] = numn('actual_base')
            price_vals['risk_pct'] = num('risk_pct')   # v51: costing input, price-form only
        # Field-level permissions (Ko, 2026-10-14): Finance edits PRICES only (incl. Actual);
        # Warehouse edits product DATA only (name/category/unit/pack/supplier) - never prices;
        # Owner edits everything.
        if has_perm(g.user, 'prices.edit') and not has_perm(g.user, 'products.data_edit'):
            vals = dict(price_vals)
        elif has_perm(g.user, 'products.data_edit') and not has_perm(g.user, 'prices.edit'):
            vals = dict(data_vals)
        else:
            vals = dict(data_vals); vals.update(price_vals)
        img = request.files.get('image')
        if img and img.filename and has_perm(g.user, 'products.data_edit'):
            fn = f"{(p['sku'] if p else 'img')}-{uuid.uuid4().hex[:6]}.webp"
            img.save(os.path.join(IMG_DIR, fn)); vals['image'] = fn
        if 'name' in vals and not vals['name']:
            flash(tr('Product name လိုပါတယ်'))
        elif p:
            log_price_changes(d, p, vals, g.user['id'])
            _audit_price_save(d, p, vals, g.user)
            sets = ', '.join(f"{k}=?" for k in vals)
            d.execute(f"UPDATE products SET {sets} WHERE id=?", (*vals.values(), pid)); d.commit()
            flash(tr('ပြင်ပြီးပြီ')); return redirect(_prod_return(pid))
        else:
            sku = next_code('GTM', 'product_seq')
            full = dict(data_vals); full.update(price_vals)
            d.execute("INSERT INTO products(sku, category, name, unit, upc, supplier, base_price, actual_base, retail, wholesale, b2c, risk_pct, image) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)",
                      (sku, full['category'], full['name'], full['unit'], full['upc'], full['supplier'], full['base_price'], full.get('actual_base'), full['retail'], full['wholesale'], full['b2c'], full.get('risk_pct') or 0, vals.get('image')))
            nid = d.execute("SELECT last_insert_rowid() i").fetchone()['i']
            d.execute("INSERT OR IGNORE INTO stock(product_id,qty) VALUES(?,0)", (nid,)); d.commit()
            flash(tr('{sku} ဆောက်ပြီးပြီ', sku=sku)); return redirect(_prod_return(nid))
    cats = [r['category'] for r in d.execute("SELECT DISTINCT category FROM products WHERE category!='' ORDER BY category").fetchall()]
    sups = [r['supplier'] for r in d.execute("SELECT DISTINCT supplier FROM products WHERE supplier!='' ORDER BY supplier").fetchall()]
    # v46: pack link section (product DATA — Owner + Warehouse only, never Finance)
    pack_ctx = None
    if p and has_perm(g.user, 'products.data_edit'):
        base_id, pf = pool_info(d, pid)
        ctx = {'locked': family_has_moves(d, pid), 'view': sku_stock(d, pid)}
        if base_id != pid:
            ctx['mode'] = 'big'
            ctx['base'] = d.execute("SELECT id, sku, name FROM products WHERE id=?", (base_id,)).fetchone()
            ctx['factor'] = pf
        else:
            mem = d.execute("SELECT id, sku, name, pack_factor FROM products WHERE pool_id=? AND id!=? ORDER BY id", (pid, pid)).fetchall()
            if mem:
                ctx['mode'] = 'base'
                ctx['members'] = [dict(m) for m in mem]
            else:
                # v99: candidates are same-name relatives only (Ko, 2026-10-31).
                # v100: ONE form (Ko: user-friendly) — combined list + a
                # which-is-big choice, instead of two direction forms.
                ctx['mode'] = 'free'
                _me = d.execute("SELECT id, sku, name, category, unit FROM products WHERE id=?", (pid,)).fetchone()
                _stand = [dict(r) for r in d.execute(
                    "SELECT id, sku, name, category, unit FROM products WHERE active=1 AND id!=? AND (pool_id IS NULL OR pool_id=id) "
                    "AND id NOT IN (SELECT pool_id FROM products WHERE pool_id IS NOT NULL) ORDER BY sku", (pid,)).fetchall()]
                _rel = pack_related(([dict(_me)] if _me else []) + _stand, pid)
                _others = sorted(_rel['smalls'] + _rel['bigs'], key=lambda m: m['sku'])
                ctx['others'] = [{'id': m['id'], 'sku': m['sku'], 'name': m['name'],
                                  'big': 1 if _pack_is_big(m) else 0} for m in _others]
                ctx['me_big'] = 1 if (_me and _pack_is_big(dict(_me))) else 0
        pack_ctx = ctx
    return render_template('product_form.html', p=p, cats=cats, sups=sups, fmt_money=fmt_money, pack_ctx=pack_ctx)

def _sup_open(alias=''):
    c = (alias + '.') if alias else ''
    return (" AND (" + c + "supplier IS NULL OR " + c + "supplier='' OR " + c +
            "supplier NOT IN (SELECT supplier FROM supplier_state WHERE closed=1))")

def closed_supplier_names(d):
    return {r['supplier'] for r in d.execute("SELECT supplier FROM supplier_state WHERE closed=1").fetchall()}

def _norm_sup_name(s):
    return ' '.join((s or '').split())

def supplier_known_names(d):
    """Every supplier spelling already in use anywhere in the system."""
    names = []
    for tbl in ('products', 'purchases', 'order_requests', 'settlements'):
        try:
            for r in d.execute(f"SELECT DISTINCT supplier FROM {tbl} WHERE supplier IS NOT NULL AND TRIM(supplier)!=''").fetchall():
                names.append(r['supplier'])
        except Exception:
            pass
    for r in d.execute("SELECT supplier FROM supplier_state").fetchall():
        names.append(r['supplier'])
    return names

def canon_supplier(d, raw):
    """v47 (Ko): supplier fields are plain type-in boxes, so the SERVER keeps
    spellings tidy — a typed name that matches a known supplier (ignoring
    letter case and extra spaces) is saved under the existing spelling;
    only a genuinely new name is saved as typed (cleaned of extra spaces)."""
    nm = _norm_sup_name(raw)
    if not nm:
        return nm
    key = nm.casefold()
    for ex in supplier_known_names(d):
        if _norm_sup_name(ex).casefold() == key:
            return ex
    return nm

def _prod_return(pid=None):
    qs = {}
    for k in ('q', 'cat', 'sup', 'sell'):
        v = (request.form.get('r' + k) or '').strip()
        if v: qs[k] = v
    url = url_for('products')
    if qs: url += '?' + _urlencode(qs)
    if pid: url += '#p' + str(pid)
    return url

@app.route('/products/<int:pid>/sellable', methods=['POST'])
@require_perm('products.sellable_toggle')
def product_sellable(pid):
    d = db()
    p = d.execute("SELECT sellable, name FROM products WHERE id=?", (pid,)).fetchone()
    if p:
        newv = 0 if (p['sellable'] or 0) else 1
        d.execute("UPDATE products SET sellable=? WHERE id=?", (newv, pid)); d.commit()


        flash(tr('ရောင်းဖွင့် 🟢' if newv else 'ရောင်းပိတ် 🔴') + ' ' + (p['name'] or ''))
    return redirect(_prod_return(pid))

@app.route('/suppliers/toggle', methods=['POST'])
@require_perm('suppliers.toggle')
def supplier_toggle():
    name = (request.form.get('supplier') or '').strip()
    if name:
        d = db()
        row = d.execute("SELECT closed FROM supplier_state WHERE supplier=?", (name,)).fetchone()
        newv = 0 if (row and row['closed']) else 1
        d.execute("INSERT INTO supplier_state(supplier, closed, updated_by, updated_at) VALUES(?,?,?,?) "
                  "ON CONFLICT(supplier) DO UPDATE SET closed=excluded.closed, updated_by=excluded.updated_by, updated_at=excluded.updated_at",
                  (name, newv, g.user['id'], now()))
        d.commit()
        cnt = d.execute("SELECT COUNT(*) c FROM products WHERE active=1 AND supplier=?", (name,)).fetchone()['c']
        if newv:
            flash(tr('{name} အရောင်း ပိတ်လိုက်ပြီ — ပစ္စည်း {cnt} မျိုး Catalog ကနေ ပျောက်သွားမယ် (ပစ္စည်းချင်း setting မပြောင်းပါ)', name=name, cnt=cnt))
        else:
            flash(tr('{name} အရောင်း ပြန်ဖွင့်လိုက်ပြီ — ပစ္စည်း {cnt} မျိုး မူလ အတိုင်း ပြန်ပေါ်မယ်', name=name, cnt=cnt))
    return redirect(_prod_return(None))

@app.route('/products/<int:pid>/adjust', methods=['POST'])
@require_perm('stock.adjust')
def product_adjust(pid):
    d = db()
    try:
        delta = float(request.form.get('delta', '0'))
    except ValueError:
        delta = 0
    note = request.form.get('note', '').strip()
    if delta:
        move_stock(pid, delta, 'adjust', '', note, g.user['id'])
        p = d.execute("SELECT name, sku, actual_base FROM products WHERE id=?", (pid,)).fetchone()
        if p:
            _val = abs(delta) * (p['actual_base'] or 0)
            audit_log(d, g.user, 'stock',
                      'Stock ညှိ — %s (%s): %+g%s' % (p['name'], p['sku'], delta,
                                                      (' (အကြောင်း: ' + note + ')') if note else ''),
                      'product', pid, 1 if _val >= AUDIT_STOCK_KS else 0)
        d.commit()
        flash(tr('Stock ပြင်ပြီးပြီ'))
    return redirect(_prod_return(pid))


# ---------------- v46: pack link routes ----------------
@app.route('/products/<int:pid>/pack-link', methods=['POST'])
@require_perm('products.pack_link')
def product_pack_link(pid):
    d = db()
    f = request.form
    factor = f.get('factor')
    # v100: ONE form (Ko: user-friendly) — counterpart + which-is-big.
    # Names are edited in the main edit form above, not here (v47 rename
    # fields removed from this section).
    try:
        other_id = int(f['other_id']) if f.get('other_id') else None
    except (TypeError, ValueError):
        other_id = None
    if other_id is None:
        ok, err = False, 'ရွေးထားတဲ့ ပစ္စည်း နဲ့ ချိတ်လို့ မရဘူး'
    else:
        _dir = pack_link_direction(pid, other_id, f.get('big_side'))
        if _dir is None:
            ok, err = False, 'အကြီး ဘယ်ဟာလဲ ရွေးပါ'
        else:
            ok, err = do_link(d, _dir[0], _dir[1], factor, g.user['id'])
    if ok:
        d.commit(); flash(tr('ချိတ်ပြီးပြီ — stock တစ်စု တည်း ဖြစ်သွားပြီ'))
    else:
        flash(tr(err))
    return redirect(url_for('product_edit', pid=pid))

@app.route('/products/<int:pid>/pack-unlink', methods=['POST'])
@require_perm('products.pack_unlink')
def product_pack_unlink(pid):
    d = db()
    ok, err = do_unlink(d, pid, request.form.get('packs'), g.user['id'])
    if ok:
        d.commit(); flash(tr('ချိတ်ဖြုတ်ပြီးပြီ'))
    else:
        flash(tr(err))
    base, _f = pool_info(d, pid)
    return redirect(url_for('product_edit', pid=base if base != pid else pid))

@app.route('/products/pack-confirm', methods=['POST'])
@require_perm('products.pack_link')
def pack_confirm():
    d = db()
    f = request.form
    # v47: both names are editable in the confirm row and save with the link
    sn = (f.get('small_name') or '').strip(); bn = (f.get('big_name') or '').strip()
    if ('small_name' in f or 'big_name' in f) and (not sn or not bn):
        flash(tr('နာမည် ထည့်ပါ'))
        return redirect(url_for('products'))
    if sn:
        d.execute("UPDATE products SET name=? WHERE id=?", (sn, int(f['small_id'])))
    if bn:
        d.execute("UPDATE products SET name=? WHERE id=?", (bn, int(f['big_id'])))
    ok, err = do_link(d, int(f['small_id']), int(f['big_id']),
                      f.get('factor'), g.user['id'])
    if ok:
        d.commit(); flash(tr('ချိတ်ပြီးပြီ — stock တစ်စု တည်း ဖြစ်သွားပြီ'))
    else:
        d.rollback(); flash(tr(err))
    return redirect(url_for('products'))

@app.route('/products/pack-dismiss', methods=['POST'])
@require_perm('products.pack_link')
def pack_dismiss():
    d = db()
    d.execute("INSERT OR IGNORE INTO pack_dismissed(small_id,big_id,dismissed_by,created_at) VALUES(?,?,?,?)",
              (int(request.form['small_id']), int(request.form['big_id']), g.user['id'], now()))
    d.commit()
    return redirect(url_for('products'))


# ---------------- Purchases (2-step) ----------------
def _extra_cost_of(f):
    """v51 batch costing: the optional per-purchase extra cost (ကားခ etc.)
    typed on the purchase / receive form. Never negative, never required."""
    try:
        v = float((f.get('extra_cost') or '0').replace(',', ''))
    except (TypeError, ValueError):
        v = 0
    return max(0.0, v)

# v56 currency memo (Ko, 2026-10-22): when goods are paid for in foreign
# money, the purchase may carry the foreign currency, the amount paid and
# that day's rate — as a NOTE only. The books always stay in Ks; nothing
# anywhere reads these values for a total.
FX_CURRENCIES = ('USD', 'THB', 'RMB', 'INR')

def _fx_of(f):
    """Read the currency-memo fields off a form. Returns (currency, amount,
    rate) — all None when no currency is chosen (the memo stays empty), or
    ('ERR', None, None) when a currency is chosen without a usable rate
    (the rate is required then; the amount is optional)."""
    cur = (f.get('fx_currency') or '').strip().upper()
    if cur not in FX_CURRENCIES:
        return (None, None, None)
    try:
        rate = float((f.get('fx_rate') or '').replace(',', ''))
    except (TypeError, ValueError):
        rate = 0
    if rate <= 0:
        return ('ERR', None, None)
    try:
        amt = float((f.get('fx_amount') or '0').replace(',', ''))
    except (TypeError, ValueError):
        amt = 0
    return (cur, (amt if amt > 0 else None), rate)

def _fmt_rate(v):
    """A rate like 305 or 305.5 — no forced decimals, commas kept."""
    s = '{:,.4f}'.format(v or 0).rstrip('0').rstrip('.')
    return s or '0'

def fx_memo(pu):
    """The one-line currency memo for a purchase row (display only), or
    None when the purchase carries no memo."""
    try:
        cur = pu['fx_currency']; amt = pu['fx_amount']; rate = pu['fx_rate']
    except Exception:
        return None
    if not cur or not rate:
        return None
    if amt:
        return '%s %s × %s Ks ≈ %s Ks' % (cur, fmt_money(amt), _fmt_rate(rate), fmt_money(amt * rate))
    return '%s · %s Ks' % (cur, _fmt_rate(rate))

@app.route('/purchases')
@require_perm('purchases.view')
def purchases():
    stf = request.args.get('st', '')
    q = ("SELECT pu.*, u.name byname, (SELECT COALESCE(SUM(qty*cost),0) FROM purchase_items WHERE purchase_id=pu.id) total "
         "FROM purchases pu LEFT JOIN users u ON u.id=pu.created_by ")
    if stf == 'pending_check':
        q += "WHERE pu.status='pending_check' "
    q += "ORDER BY pu.id DESC"
    rows = db().execute(q).fetchall()
    return render_template('purchases.html', rows=rows, fmt_money=fmt_money, stf=stf)

@app.route('/purchases/new', methods=['GET', 'POST'])
@require_perm('purchases.create_direct')
def purchase_new():
    d = db()
    if request.method == 'POST':
        f = request.form
        fx_cur, fx_amt, fx_rate = _fx_of(f)
        if fx_cur == 'ERR':
            flash(tr('ငွေကြေး ရွေး ထား ရင် လဲ နှုန်း ထည့် ပါ'))
            return redirect(url_for('purchase_new'))
        code = next_code('PUR', 'purchase_seq')
        d.execute("INSERT INTO purchases(code,supplier,invoice_no,pdate,created_by,note,created_at,extra_cost,extra_note,fx_currency,fx_amount,fx_rate) VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",
                  (code, canon_supplier(d, f.get('supplier', '')), f.get('invoice_no', '').strip(),
                   f.get('pdate') or today(), g.user['id'], f.get('note', '').strip(), now(),
                   _extra_cost_of(f), (f.get('extra_note') or '').strip(), fx_cur, fx_amt, fx_rate))
        puid = d.execute("SELECT last_insert_rowid() i").fetchone()['i']
        added = 0
        _exps = f.getlist('exp')
        for _ix, (pid_s, qty_s, cost_s) in enumerate(zip(f.getlist('pid'), f.getlist('qty'), f.getlist('cost'))):
            try:
                qty = float(qty_s or 0); cost = float((cost_s or '0').replace(',', ''))
            except ValueError:
                continue
            if pid_s and qty > 0:
                exp = (_exps[_ix] if _ix < len(_exps) else '') or ''
                if not re.match(r'^\d{4}-\d{2}-\d{2}$', exp): exp = None
                d.execute("INSERT INTO purchase_items(purchase_id,product_id,qty,cost,expiry_date) VALUES(?,?,?,?,?)", (puid, int(pid_s), qty, cost, exp))
                move_stock(int(pid_s), qty, 'purchase', code, tr('စစ်ရန်ကျန်'), g.user['id'])
                added += 1
        if not added:
            d.execute("DELETE FROM purchases WHERE id=?", (puid,)); d.commit()
            flash(tr('ပစ္စည်း အနည်းဆုံး ၁ မျိုး ထည့်ပါ'))
            return redirect(url_for('purchase_new'))
        d.commit(); flash(tr('{code} သွင်းပြီးပြီ — Finance စစ်ရန် ကျန်နေတယ်', code=code))
        return redirect(url_for('purchases'))
    prods = d.execute("SELECT id, sku, name, unit FROM products WHERE active=1 ORDER BY sku").fetchall()
    sups = [r['supplier'] for r in d.execute("SELECT DISTINCT supplier FROM products WHERE supplier!='' ORDER BY supplier").fetchall()]
    return render_template('purchase_form.html', prods=prods, sups=sups)

@app.route('/purchases/<int:puid>')
@require_perm('purchases.view')
def purchase_detail(puid):
    d = db()
    pu = d.execute("SELECT pu.*, u.name byname, c.name checkedname, cb.name cancelledname FROM purchases pu LEFT JOIN users u ON u.id=pu.created_by LEFT JOIN users c ON c.id=pu.checked_by LEFT JOIN users cb ON cb.id=pu.cancelled_by WHERE pu.id=?", (puid,)).fetchone()
    if not pu: abort(404)
    items = d.execute("SELECT pi.*, p.sku, p.name, p.unit, p.risk_pct FROM purchase_items pi JOIN products p ON p.id=pi.product_id WHERE pi.purchase_id=?", (puid,)).fetchall()
    stl = find_settlement(d, pu['or_id'], pu['id'] if not pu['or_id'] else None)
    # v51 batch costing card — Owner+Finance only (cost-derived figures):
    # the batch's extra cost is shared over the lines in proportion to
    # goods value; each line adds its own product's Risk % of goods value.
    # A decision view only — settlements and payables never read it.
    costing = None
    if has_perm(g.user, 'actual.view'):
        extra = pu['extra_cost'] or 0
        goods_total = sum((it['qty'] or 0) * (it['cost'] or 0) for it in items)
        lines = []
        risk_total = 0.0
        for it in items:
            goods = (it['qty'] or 0) * (it['cost'] or 0)
            alloc = extra * goods / goods_total if goods_total > 0 else 0.0
            risk = goods * (it['risk_pct'] or 0) / 100.0
            risk_total += risk
            dec = goods + alloc + risk
            lines.append({'it': it, 'goods': goods, 'alloc': alloc, 'risk': risk,
                          'dec': dec, 'unit': (dec / it['qty'] if it['qty'] else 0)})
        costing = {'lines': lines, 'goods': goods_total, 'extra': extra,
                   'risk': risk_total, 'decision': goods_total + extra + risk_total}
    can_extra = pu['status'] != 'cancelled' and (pu['status'] == 'pending_check' or has_perm(g.user, 'audit.view'))  # v75: audit.view is an owner-only key
    return render_template('purchase_detail.html', pu=pu, items=items, fmt_money=fmt_money, stl=stl, STL_STATUS=STL_STATUS,
                           costing=costing, can_extra=can_extra, fx_memo=fx_memo(pu), FX_CURRENCIES=FX_CURRENCIES)

@app.route('/purchases/<int:puid>/extra', methods=['POST'])
@require_perm('purchases.extra_edit')
def purchase_extra(puid):
    """Set/correct the batch's extra cost: any of the three office roles
    while the purchase is still pending_check; once checked it is frozen
    for everyone except the Owner (owner-uncapped pattern)."""
    d = db()
    pu = d.execute("SELECT * FROM purchases WHERE id=?", (puid,)).fetchone()
    if not pu or pu['status'] == 'cancelled':
        return redirect(url_for('purchase_detail', puid=puid))
    if pu['status'] != 'pending_check' and not has_perm(g.user, 'audit.view'):
        flash(tr('စစ်ပြီး သား အဝယ် — အပို ကုန်ကျ ကို Owner ပဲ ပြင် နိုင်တယ်'))
        return redirect(url_for('purchase_detail', puid=puid))
    d.execute("UPDATE purchases SET extra_cost=?, extra_note=? WHERE id=?",
              (_extra_cost_of(request.form), (request.form.get('extra_note') or '').strip(), puid))
    d.commit(); flash(tr('အပို ကုန်ကျ မှတ်ပြီးပြီ'))
    return redirect(url_for('purchase_detail', puid=puid))

@app.route('/purchases/<int:puid>/fx', methods=['POST'])
@require_perm('purchases.fx_edit')
def purchase_fx(puid):
    """Set/clear the purchase's currency memo — exactly the extra-cost
    edit's permissions: any office role while pending_check, Owner only
    once checked, never on a cancelled purchase. Choosing no currency
    clears the memo (all three columns back to NULL)."""
    d = db()
    pu = d.execute("SELECT * FROM purchases WHERE id=?", (puid,)).fetchone()
    if not pu or pu['status'] == 'cancelled':
        return redirect(url_for('purchase_detail', puid=puid))
    if pu['status'] != 'pending_check' and not has_perm(g.user, 'audit.view'):
        flash(tr('စစ်ပြီး သား အဝယ် — အပို ကုန်ကျ ကို Owner ပဲ ပြင် နိုင်တယ်'))
        return redirect(url_for('purchase_detail', puid=puid))
    fx_cur, fx_amt, fx_rate = _fx_of(request.form)
    if fx_cur == 'ERR':
        flash(tr('ငွေကြေး ရွေး ထား ရင် လဲ နှုန်း ထည့် ပါ'))
        return redirect(url_for('purchase_detail', puid=puid))
    d.execute("UPDATE purchases SET fx_currency=?, fx_amount=?, fx_rate=? WHERE id=?",
              (fx_cur, fx_amt, fx_rate, puid))
    d.commit(); flash(tr('ငွေကြေး မှတ်ချက် သိမ်းပြီးပြီ'))
    return redirect(url_for('purchase_detail', puid=puid))

@app.route('/purchases/<int:puid>/check', methods=['POST'])
@require_perm('purchases.check')
def purchase_check(puid):
    d = db()
    pu = d.execute("SELECT * FROM purchases WHERE id=?", (puid,)).fetchone()
    if pu and pu['status'] == 'pending_check':
        d.execute("UPDATE purchases SET status='checked', checked_by=? WHERE id=?", (g.user['id'], puid))
        d.commit(); flash(tr('စစ်ပြီး အတည်ပြုပြီးပြီ'))
    if request.form.get('back') == 'check':
        return redirect('/purchases?st=pending_check')
    return redirect(url_for('purchase_detail', puid=puid))


# ---------------- Orders / Sales ----------------
def order_total(oid):
    r = db().execute("SELECT COALESCE(SUM(qty*price),0) s, (SELECT discount FROM orders WHERE id=?) dc FROM order_items WHERE order_id=?", (oid, oid)).fetchone()
    return (r['s'] or 0) - (r['dc'] or 0), r['s'] or 0

def order_received(oid, d=None):
    """Money the office has actually got. v47: only VERIFIED payments count —
    a delivery person's field collections stay pending until Finance confirms
    the handover, so they never make an invoice look paid early."""
    d = d or db()
    r = d.execute("SELECT COALESCE(SUM(amount),0) s FROM order_payments WHERE order_id=? AND COALESCE(verified,1)=1", (oid,)).fetchone()
    return r['s'] or 0

def order_totals_map(d, oids):
    """v62: {oid: (total, gross)} for a page of orders in two grouped
    queries — exactly order_total()'s numbers per order (total = the
    lines' qty*price sum minus the order's discount; gross = the sum)."""
    ids = list(dict.fromkeys(oids))
    out = {}
    if not ids:
        return out
    ph = ','.join('?' * len(ids))
    gross = {r['oid']: (r['s'] or 0) for r in d.execute(
        "SELECT order_id oid, COALESCE(SUM(qty*price),0) s FROM order_items "
        "WHERE order_id IN (%s) GROUP BY order_id" % ph, ids).fetchall()}
    disc = {r['id']: (r['discount'] or 0) for r in d.execute(
        "SELECT id, discount FROM orders WHERE id IN (%s)" % ph, ids).fetchall()}
    for oid in ids:
        gs = gross.get(oid, 0)
        out[oid] = (gs - disc.get(oid, 0), gs)
    return out

def order_received_map(d, oids):
    """v62: {oid: verified received} for a page of orders in one grouped
    query — exactly order_received()'s numbers per order."""
    ids = list(dict.fromkeys(oids))
    if not ids:
        return {}
    ph = ','.join('?' * len(ids))
    return {r['oid']: (r['s'] or 0) for r in d.execute(
        "SELECT order_id oid, COALESCE(SUM(amount),0) s FROM order_payments "
        "WHERE COALESCE(verified,1)=1 AND order_id IN (%s) GROUP BY order_id" % ph, ids).fetchall()}

def order_pending_collect(oid, d=None):
    """Field collections recorded by a delivery person, not yet checked."""
    d = d or db()
    r = d.execute("SELECT COALESCE(SUM(amount),0) s FROM order_payments WHERE order_id=? AND COALESCE(verified,1)=0", (oid,)).fetchone()
    return r['s'] or 0

def sync_paid(oid, d=None):
    d = d or db()
    total, _ = order_total(oid)
    recv = order_received(oid, d)
    paid = 1 if (recv >= total - 0.5 and (total > 0 or recv > 0)) else 0
    d.execute("UPDATE orders SET paid=? WHERE id=?", (paid, oid))
    return paid

def order_flags(o, items):
    """Finance flags: line sold below wholesale, or below true cost (actual)."""
    flags = []
    for it in items:
        _net = it['price'] - ((it['discount'] or 0) / it['qty'] if it['qty'] else 0)
        if it['wholesale'] and _net < it['wholesale']:
            flags.append(tr('{sku}: Wholesale ({amt}) အောက် ရောင်းထားတယ်', sku=it['sku'], amt=fmt_money(it['wholesale'])))
        if it['actual_base'] is not None and _net < it['actual_base']:
            flags.append(tr('{sku}: အရင်းအမှန် ({amt}) အောက် ရောင်းထားတယ်', sku=it['sku'], amt=fmt_money(it['actual_base'])))
    if o['overstock'] and o['offline']:
        # v48 display scope: this flag is for orders that came in OFFLINE
        # (rep ordered without seeing stock). In a demand-driven shop most
        # office-entered invoices exceed on-hand stock until purchased, so
        # flagging them too just trains everyone to ignore the banner.
        flags.append(tr('Offline က တင်လာတာ — stock ထက် ပိုနေတယ်'))
    return flags

@app.route('/orders')
@login_required()
def orders():
    d = db()
    sql = "SELECT o.*, u.name rep FROM orders o LEFT JOIN users u ON u.id=o.sales_user"
    args = []
    if has_perm(g.user, 'dashboard.sales_own') and not is_leader(g.user):
        sql += " WHERE o.sales_user=?"; args.append(g.user['id'])
    elif has_perm(g.user, 'dashboard.delivery_own'):
        sql += " WHERE o.assigned_to=?"; args.append(g.user['id'])
    sql += " ORDER BY o.id DESC LIMIT 200"
    olist = d.execute(sql, args).fetchall()
    totals = order_totals_map(d, [o['id'] for o in olist])
    received = order_received_map(d, [o['id'] for o in olist])
    rows = [(o, totals.get(o['id'], (0, 0))[0], received.get(o['id'], 0)) for o in olist]
    return render_template('orders.html', rows=rows, fmt_money=fmt_money)

@app.route('/orders/new', methods=['GET'])
@require_perm('orders.create')
def order_new_page():
    d = db()
    areas = [r['name'] for r in d.execute("SELECT name FROM areas ORDER BY name").fetchall()]
    orows = d.execute("SELECT * FROM outlets ORDER BY name, area, id").fetchall()
    onames = [r['name'] for r in orows]
    _amap = {}
    for r in d.execute("SELECT outlet_id, alias FROM outlet_aliases ORDER BY id").fetchall():
        _amap.setdefault(r['outlet_id'], []).append(r['alias'])
    oj = [{'id': r['id'], 'name': r['name'], 'area': r['area'] or '', 'note': r['note'] or '',
           'aliases': _amap.get(r['id'], []), 'label': picker_label(r)} for r in orows]
    return render_template('order_form.html', areas=areas, onames=onames, orows=oj,
                           outlets_js=json.dumps(oj, ensure_ascii=False),
                           ambig=session.get('ambig'), **shop_type_ctx())

@app.route('/api/products')
@login_required()
def api_products():
    if not has_perm(g.user, 'catalog.api_products'): abort(403)
    d = db()
    rows = d.execute(
        "SELECT p.id, p.sku, p.name, p.category, p.unit, p.upc, p.retail, p.wholesale, p.image, COALESCE(s.qty,0) qty, "
        "p.pool_id, p.pack_factor, "
        "ph.field pc_field, ph.old_price pc_old, ph.new_price pc_new, ph.created_at pc_at "
        "FROM products p LEFT JOIN stock s ON s.product_id=p.id "
        "LEFT JOIN price_history ph ON ph.id=(SELECT MAX(id) FROM price_history WHERE product_id=p.id "
        "AND field IN ('retail','wholesale') AND created_at>=datetime('now','-30 days')) "
        "WHERE p.active=1 AND COALESCE(p.sellable,1)=1" + _sup_open('p') + " ORDER BY p.sku").fetchall()
    # v46: qty = this SKU's view of the family pool; pool fields stay server-side
    smap = {r['product_id']: r['qty'] for r in d.execute("SELECT product_id, qty FROM stock").fetchall()}
    out = []
    for r in rows:
        r = dict(r)
        if r['pool_id'] and r['pool_id'] != r['id']:
            f = r['pack_factor'] or 1
            pool = smap.get(r['pool_id'], 0)
            r['qty'] = int(pool / f) if f != 1 else pool
        del r['pool_id']; del r['pack_factor']
        out.append(r)
    return jsonify(out)

def haversine_m(lat1, lng1, lat2, lng2):
    R = 6371000.0
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dp = math.radians(lat2 - lat1); dl = math.radians(lng2 - lng1)
    a = math.sin(dp / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2
    return 2 * R * math.asin(math.sqrt(a))

def _flt(v):
    try: return float(v)
    except (TypeError, ValueError): return None

def _norm_shop_text(s):
    """Shop text as it is MATCHED: case-insensitive, whitespace
    collapsed. 'Sut', ' sut ' and 'SUT' are the same typed text. The
    stored name/alias keep their original spelling; only matching
    normalises."""
    return ' '.join((s or '').split()).lower()

def outlet_aliases_of(d, olid):
    return [r['alias'] for r in d.execute(
        "SELECT alias FROM outlet_aliases WHERE outlet_id=? ORDER BY id", (olid,)).fetchall()]

def _alias_owner(d, norm):
    """The outlet owning the alias whose normalised form is `norm`."""
    if not norm: return None
    return d.execute("SELECT o.* FROM outlet_aliases a JOIN outlets o ON o.id=a.outlet_id "
                     "WHERE a.alias_norm=?", (norm,)).fetchone()

def _outlet_text_hits(d, typed):
    """Every outlet a typed shop text may mean (v68): official-name
    hits PLUS the owners of an equal alias spelling, deduped by id in
    id order. Zero = a genuinely new shop; one = that shop; two or
    more = a collision the system must ask about, never guess."""
    n = _norm_shop_text(typed)
    if not n: return []
    hits = {}
    for r in d.execute("SELECT * FROM outlets ORDER BY id").fetchall():
        if _norm_shop_text(r['name']) == n:
            hits[r['id']] = r
    ao = _alias_owner(d, n)
    if ao is not None:
        hits[ao['id']] = ao
    return [hits[k] for k in sorted(hits)]

def resolve_outlet(d, name, area, user_id, odate):
    """Name -> outlet id for the NON-order flow that still resolves by a
    typed shop name (the returns exchange mark). v67: a name matching 2+
    outlets returns None — the caller must ask, never guess; zero
    matches creates the outlet, exactly one matches as before.
    v68: the typed text is matched against official names AND alias
    spellings (normalised) — a shop typed by its other spelling lands
    on the same record; a text that is one shop's name and another's
    alias is a collision and returns None like any 2+ match."""
    name = (name or '').strip()
    if not name: return None
    hits = _outlet_text_hits(d, name)
    if len(hits) > 1: return None
    if hits:
        o = hits[0]
        if area and not (o['area'] or ''):
            d.execute("UPDATE outlets SET area=? WHERE id=?", (area, o['id']))
        return o['id']
    d.execute("INSERT INTO outlets(name, area, created_by, created_at, first_seen) VALUES(?,?,?,?,?)",
              (name, area or '', user_id, now(), odate))
    return d.execute("SELECT last_insert_rowid() i").fetchone()['i']

def picker_label(o):
    """How a shop is named in a PICKER: always name + area (+ note when
    present), so same-name shops can be told apart at a glance."""
    lab = o['name']
    if o['area']: lab += ' · ' + o['area']
    try: nt = o['note']
    except Exception: nt = None
    if nt: lab += ' · (' + nt + ')'
    return lab

def outlet_label(d, o):
    """How a shop is named wherever a person reads it: the plain name,
    unless another outlet shares it — then 'Name · Area · (note)'."""
    n = d.execute("SELECT COUNT(*) c FROM outlets WHERE name=?", (o['name'],)).fetchone()['c']
    return picker_label(o) if n > 1 else o['name']

def outlet_candidates(d, name, lat=None, lng=None):
    """The shops behind an ambiguous typed text (official-name hits
    plus an alias owner, v68): id, name, area, note, the picker label,
    and the distance when the submit carries a GPS fix and the shop
    has a pin — nearest first, a hint only."""
    return _candidates_for(d, _outlet_text_hits(d, name), lat, lng)

def _candidates_for(d, rows, lat=None, lng=None):
    out = []
    for r in rows:
        c = {'id': r['id'], 'name': r['name'], 'area': r['area'] or '', 'note': r['note'] or '',
             'label': picker_label(r), 'dist': '', '_dm': None}
        if lat is not None and lng is not None and r['lat'] is not None and r['lng'] is not None:
            c['_dm'] = haversine_m(lat, lng, r['lat'], r['lng'])
            c['dist'] = fmt_dist(c['_dm'])
        out.append(c)
    out.sort(key=lambda c: (c['_dm'] is None, c['_dm'] if c['_dm'] is not None else 0, c['id']))
    for c in out: del c['_dm']
    return out

def _resolve_order_outlet(d, payload, name, area, user, offline):
    """Which outlet an order belongs to. Returns (outlet_id, candidates,
    unattributed, canon) — exactly one outcome:
    - a submitted id VERIFIED against the submitted text (stale-cache
      guard), or the ONE match -> (id, None, False, official name)
    - zero matches -> the outlet is created from the order, as always
      -> (id, None, False, the typed name)
    - 2+ matches, interactive submit -> (None, candidates, False, None):
      ASK, create nothing
    - 2+ matches, offline sync -> (None, None, True, None): the order is
      created unattributed; only the person who stood at the shop can
      say which one it was, so the question goes back to them.
    v68: the typed text is matched against official names AND alias
    spellings (case-insensitive, whitespace collapsed). A text that is
    one shop's alias and another shop's official name is a collision —
    2 hits, asked about like any other. canon is the shop's OFFICIAL
    name: an order placed by typing a shop's other spelling carries
    the official name, exactly as if it had been typed."""
    sub = payload.get('outlet_id')
    try: sub = int(sub) if sub not in (None, '') else None
    except (TypeError, ValueError): sub = None
    if sub:
        row = d.execute("SELECT * FROM outlets WHERE id=?", (sub,)).fetchone()
        _ao = _alias_owner(d, _norm_shop_text(name)) if row else None
        if row and (_norm_shop_text(row['name']) == _norm_shop_text(name)
                    or (_ao is not None and _ao['id'] == sub)):
            if area and not (row['area'] or ''):
                d.execute("UPDATE outlets SET area=? WHERE id=?", (area, sub))
            return sub, None, False, row['name']
    hits = _outlet_text_hits(d, name)
    if len(hits) == 1:
        o = hits[0]
        if area and not (o['area'] or ''):
            d.execute("UPDATE outlets SET area=? WHERE id=?", (area, o['id']))
        return o['id'], None, False, o['name']
    if len(hits) >= 2:
        if offline:
            return None, None, True, None
        return None, _candidates_for(d, hits, _flt(payload.get('lat')), _flt(payload.get('lng'))), False, None
    d.execute("INSERT INTO outlets(name, area, created_by, created_at, first_seen) VALUES(?,?,?,?,?)",
              (name, area or '', user['id'], now(), payload.get('odate') or today()))
    return d.execute("SELECT last_insert_rowid() i").fetchone()['i'], None, False, name

OFFICE_RADIUS_M = 20   # Ko, 2026-10-17: submissions within 20 m of the office learn no pin / log no visit

def get_setting(d, k):
    # v62: settings are read many times per request (office point, GPS
    # threshold, money openings, backup stamp...). Memoise per request;
    # set_setting() keeps the memo in step, so a write is never hidden.
    try:
        memo = g.get('_settings_memo')
        if memo is None:
            memo = g._settings_memo = {}
    except RuntimeError:
        memo = None
    if memo is not None and k in memo:
        return memo[k]
    r = d.execute("SELECT v FROM app_settings WHERE k=?", (k,)).fetchone()
    v = r['v'] if r else None
    if memo is not None:
        memo[k] = v
    return v

def set_setting(d, k, v):
    d.execute("INSERT OR REPLACE INTO app_settings(k, v) VALUES(?, ?)", (k, v))
    try:
        memo = g.get('_settings_memo')
    except RuntimeError:
        memo = None
    if memo is not None:
        memo[k] = v

def office_point(d):
    la, ln = get_setting(d, 'office_lat'), get_setting(d, 'office_lng')
    try:
        return (float(la), float(ln)) if la and ln else None
    except Exception:
        return None

BIZ_DEFAULTS = {'name': 'GTM Distribution', 'address': 'No-10/144, Jan Mai Kaung Ward, Myitkyina', 'phones': ''}
def biz_info(d):
    """Business letterhead (Ko, 2026-10-28): Owner-editable via /settings/business.
    Defaults reproduce the old hardcoded print headers, so nothing changes
    until Ko edits them; a blank name/address falls back to the default so
    the letterhead can never go blank."""
    return {'name': get_setting(d, 'biz_name') or BIZ_DEFAULTS['name'],
            'address': get_setting(d, 'biz_address') or BIZ_DEFAULTS['address'],
            'phones': get_setting(d, 'biz_phones') or ''}

def near_office(d, lat, lng):
    if lat is None or lng is None:
        return False
    op = office_point(d)
    return bool(op) and haversine_m(lat, lng, op[0], op[1]) <= OFFICE_RADIUS_M

def outlet_learn_gps(d, outlet_id, lat, lng, acc):
    if lat is None or lng is None or (acc is not None and acc > 60): return
    o = d.execute("SELECT lat, lng, gps_n FROM outlets WHERE id=?", (outlet_id,)).fetchone()
    if not o: return
    if o['lat'] is None:
        d.execute("UPDATE outlets SET lat=?, lng=?, gps_n=1 WHERE id=?", (lat, lng, outlet_id)); return
    n = min(o['gps_n'] or 1, 20)
    d.execute("UPDATE outlets SET lat=?, lng=?, gps_n=? WHERE id=?",
              ((o['lat'] * n + lat) / (n + 1), (o['lng'] * n + lng) / (n + 1), n + 1, outlet_id))

def record_visit(d, outlet_id, user_id, vdate, kind, order_id=None, lat=None, lng=None, acc=None, cu=None):
    ex = d.execute("SELECT id, kind FROM visits WHERE user_id=? AND outlet_id=? AND vdate=?",
                   (user_id, outlet_id, vdate)).fetchone()
    if ex:
        if kind == 'order' and ex['kind'] != 'order':
            d.execute("UPDATE visits SET kind='order', order_id=? WHERE id=?", (order_id, ex['id']))
        return
    d.execute("INSERT OR IGNORE INTO visits(outlet_id, user_id, vdate, kind, order_id, lat, lng, acc, client_uuid, created_at) VALUES(?,?,?,?,?,?,?,?,?,?)",
              (outlet_id, user_id, vdate, kind, order_id, lat, lng, acc, cu, now()))

def shop_outstanding(d, outlet_id):
    """A shop's unpaid delivered invoices: (count, balance still to collect).
    'Paid' follows the verified-payments rule via order_received()."""
    cnt = 0; bal = 0.0
    if not outlet_id:
        return cnt, bal
    for r in d.execute("SELECT id FROM orders WHERE outlet_id=? AND status='delivered' AND paid=0", (outlet_id,)).fetchall():
        t, _ = order_total(r['id'])
        b = t - order_received(r['id'], d)
        if b > 0.5:
            cnt += 1; bal += b
    return cnt, bal

def create_order(payload, user, offline=0):
    """payload: {client_uuid, outlet, area, discount, note, items:[{product_id, qty, price, tier}]}
    Returns (order_id, created, credit) — credit carries the v49 credit-guard
    outcome: stage 0 = nothing owed, 1 = this invoice becomes the shop's 2nd
    unpaid one (allowed, flagged + warned), 2 = 3rd or more (blocked for
    everyone except the Owner). GTM is a no-credit shop (Ko); this ladder is
    a safeguard for the day a shop genuinely needs room."""
    d = db()
    cu = payload.get('client_uuid')
    if cu:
        ex = d.execute("SELECT id FROM orders WHERE client_uuid=?", (cu,)).fetchone()
        if ex:
            return ex['id'], False, {'stage': 0}
    items = [i for i in payload.get('items', []) if float(i.get('qty', 0)) > 0]
    if not items or not (payload.get('outlet') or '').strip():
        return None, False, {'stage': 0}
    od = (payload.get('odate') or '').strip()
    if not re.match(r'^\d{4}-\d{2}-\d{2}$', od) or od > today():
        od = today()
    payload['odate'] = od
    _has_ld = any('discount' in i for i in items)
    _ld_total = 0.0
    for i in items:
        try: _dc = float(i.get('discount') or 0)
        except (TypeError, ValueError): _dc = 0
        i['_disc'] = max(0.0, min(_dc, float(i['qty']) * float(i.get('price') or 0)))
        _ld_total += i['_disc']
    inv_discount = _ld_total if _has_ld else float(payload.get('discount') or 0)
    over = 0
    area = (payload.get('area') or '').strip()
    if area:
        d.execute("INSERT OR IGNORE INTO areas(name) VALUES(?)", (area,))
    team = ''
    try:
        team = user['team'] or ''
    except Exception:
        team = ''
    # v67: outlet identity is (name, area) — a typed name may be 2+ shops.
    # Interactive submits with an ambiguous name create NOTHING and get
    # the candidates back (ask, never guess); an ambiguous name arriving
    # through the offline sync (stale phone cache) creates the order
    # unattributed and raises the question to its creator.
    # v68: matching also consults alias spellings; when the typed text
    # was a shop's OTHER spelling, canon (its official name) is what
    # the order carries.
    outlet_id, ambig, unattributed, canon = _resolve_order_outlet(
        d, payload, payload['outlet'].strip(), area, user, offline)
    if ambig is not None:
        return None, False, {'stage': 0, 'ambiguous': ambig}
    shop_text = canon or payload['outlet'].strip()
    # v80 (Ko, 2026-10-25): new sales invoices take GTM-V-#### from a FRESH
    # counter ('order_seq_gtmv'), starting at GTM-V-0001. The old 'order_seq'
    # counter and every existing PV-#### code are left exactly as they are —
    # history is never rewritten; only new vouchers take the new prefix.
    code = next_code('GTM-V', 'order_seq_gtmv')
    # v65 (Ko, 2026-10-23): a shop type named at order time fills the
    # outlet's record ONLY where it is still unset — a newly created
    # outlet (the resolver inserts it typeless) or one never typed.
    # A type the office already set is never overwritten from the field.
    _stype = (payload.get('shop_type') or '').strip()
    if outlet_id and _stype in SHOP_TYPE_KEYS:
        d.execute("UPDATE outlets SET shop_type=? WHERE id=? AND (shop_type IS NULL OR shop_type='')",
                  (_stype, outlet_id))
    # v49 credit guard (Ko's ladder): count the shop's unpaid delivered
    # invoices. A set credit limit at/past its balance counts as the warn
    # stage on its own.
    credit = {'stage': 0, 'count': 0, 'balance': 0.0}
    if outlet_id:
        ocnt, obal = shop_outstanding(d, outlet_id)
        orow = d.execute("SELECT credit_limit FROM outlets WHERE id=?", (outlet_id,)).fetchone()
        limit = orow['credit_limit'] if orow else None
        stage = 2 if ocnt >= 2 else (1 if ocnt == 1 else 0)
        if limit is not None and ocnt >= 1 and obal >= limit:
            stage = max(stage, 1)
        credit = {'stage': stage, 'count': ocnt, 'balance': obal, 'limit': limit}
        if stage >= 2 and not has_perm(user, 'orders.create.credit_override'):
            credit['blocked'] = True
            return None, False, credit
    d.execute("INSERT INTO orders(code,client_uuid,outlet,area,sales_user,team,odate,discount,note,offline,created_at,outlet_id) VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",
              (code, cu, shop_text, area, user['id'], team,
               payload.get('odate') or today(), inv_discount,
               payload.get('note', ''), offline, now(), outlet_id))
    oid = d.execute("SELECT last_insert_rowid() i").fetchone()['i']
    if unattributed:
        # v67: domain 'outlets' — the feed shows it while the invoice is
        # still unattributed and it clears itself once someone attaches
        # the shop (resolution the system can see). The invoice's own
        # creator is the person who can answer it (they stood there);
        # the office roles see it too and may attach if they know.
        _notify_edit(d, oid, user,
                     tr('ဆိုင် မသဲကွဲ — {code}: {name}', code=code, name=payload['outlet'].strip()),
                     domain='outlets')
    if credit['stage'] >= 1:
        d.execute("UPDATE orders SET credit_warn=1 WHERE id=?", (oid,))
        if offline:
            _notify_edit(d, oid, user, tr('သတိ — ဒီ ဆိုင်မှာ ငွေ မရသေးတဲ့ ဘောက်ချာ {n} စောင် (ကျန်ငွေ {bal} Ks) ရှိနေတယ်။ ဒီ ဘောက်ချာ က နောက် တစ်စောင် ထပ်တိုးတယ်', n=credit['count'], bal=fmt_money(credit['balance'])))
    for i in items:
        pid = int(i['product_id']); qty = float(i['qty'])
        _pr = d.execute("SELECT actual_base, base_price FROM products WHERE id=?", (pid,)).fetchone()
        d.execute("INSERT INTO order_items(order_id,product_id,qty,price,tier,discount,cost_act,cost_base) VALUES(?,?,?,?,?,?,?,?)",
                  (oid, pid, qty, float(i.get('price') or 0), i.get('tier', ''), i.get('_disc', 0),
                   (_pr['actual_base'] if _pr else None), (_pr['base_price'] if _pr else None)))
        if line_overstock(d, pid, qty):
            over = 1
    if over:
        d.execute("UPDATE orders SET overstock=1 WHERE id=?", (oid,))
    d.commit()
    if has_perm(user, 'dashboard.sales_own') and outlet_id:
        try:
            _lat, _lng, _acc = _flt(payload.get('lat')), _flt(payload.get('lng')), _flt(payload.get('acc'))
            if not near_office(d, _lat, _lng):
                record_visit(d, outlet_id, user['id'], payload.get('odate') or today(), 'order', oid, _lat, _lng, _acc, cu)
                outlet_learn_gps(d, outlet_id, _lat, _lng, _acc)
            d.commit()
        except Exception:
            pass
    return oid, True, credit

def _parse_order_form(f):
    items = []
    _pids, _qtys, _prices, _tiers, _discs = f.getlist('pid'), f.getlist('qty'), f.getlist('price'), f.getlist('tier'), f.getlist('disc')
    for _ix in range(len(_pids)):
        pid_s = _pids[_ix]; qty_s = _qtys[_ix] if _ix < len(_qtys) else ''
        if pid_s and qty_s and float(qty_s) > 0:
            items.append({'product_id': int(pid_s), 'qty': float(qty_s),
                          'price': float(((_prices[_ix] if _ix < len(_prices) else '0') or '0').replace(',', '')),
                          'tier': _tiers[_ix] if _ix < len(_tiers) else '',
                          'discount': float(((_discs[_ix] if _ix < len(_discs) else '0') or '0').replace(',', ''))})
    return {'outlet': f.get('outlet', ''), 'area': f.get('area', ''), 'discount': f.get('discount') or 0,
            'note': f.get('note', ''), 'shop_type': f.get('shop_type', ''),
            'outlet_id': f.get('outlet_id', ''), 'items': items}

def _finish_web_order(oid, credit):
    if not oid:
        if credit.get('blocked'):
            flash(tr('ဒီ ဆိုင်မှာ ငွေ မရသေးတဲ့ ဘောက်ချာ {n} စောင် ရှိနေပြီ — နောက်ထပ် ဘောက်ချာ အသစ် ဖွင့်လို့ မရတော့ဘူး။ ကျန်ငွေ အရင် ရှင်းပါ', n=credit.get('count', 0)))
        else:
            flash(tr('Outlet နဲ့ ပစ္စည်း အနည်းဆုံး ၁ မျိုး လိုပါတယ်'))
        return redirect(url_for('order_new_page'))
    flash(tr('Order တင်ပြီးပြီ'))
    if credit.get('stage', 0) >= 1:
        flash(tr('သတိ — ဒီ ဆိုင်မှာ ငွေ မရသေးတဲ့ ဘောက်ချာ {n} စောင် (ကျန်ငွေ {bal} Ks) ရှိနေတယ်။ ဒီ ဘောက်ချာ က နောက် တစ်စောင် ထပ်တိုးတယ်', n=credit.get('count', 0), bal=fmt_money(credit.get('balance', 0))))
    return redirect(url_for('order_detail', oid=oid))

@app.route('/orders/create', methods=['POST'])
@require_perm('orders.create')
def order_create_web():
    oid, _, credit = create_order(_parse_order_form(request.form), g.user)
    if not oid and credit.get('ambiguous') is not None:
        # v67: the name is 2+ shops and no shop was picked — nothing was
        # created. Stash the whole form and let the person pick on the
        # order page (the chooser there replays every field verbatim).
        session['ambig'] = {
            'form': {k: request.form.get(k, '') for k in ('outlet', 'area', 'discount', 'note', 'shop_type')},
            'lists': {k: request.form.getlist(k) for k in ('pid', 'qty', 'price', 'tier', 'disc')},
            'candidates': credit['ambiguous']}
        flash(tr('ဆိုင် နာမည် တူတာ နှစ်ဆိုင် ရှိတယ် — ဘယ် ဆိုင် လဲ ရွေးပါ'))
        return redirect(url_for('order_new_page'))
    return _finish_web_order(oid, credit)

@app.route('/orders/pick', methods=['POST'])
@require_perm('orders.create')
def order_pick():
    # The office chooser's answer: the same form, now carrying the picked
    # shop's id (create_order verifies it against the name as always).
    oid, _, credit = create_order(_parse_order_form(request.form), g.user)
    if oid: session.pop('ambig', None)
    return _finish_web_order(oid, credit)

@app.route('/orders/pick-cancel', methods=['POST'])
@require_perm('orders.create')
def order_pick_cancel():
    session.pop('ambig', None)
    return redirect(url_for('order_new_page'))

@app.route('/api/orders', methods=['POST'])
@require_perm('orders.create')
def api_orders():
    payload = request.get_json(force=True)
    oid, created, credit = create_order(payload, g.user, offline=1 if payload.get('offline') else 0)
    if not oid:
        if credit.get('ambiguous') is not None:
            # v67 fallback ask: the phone's cache was stale/absent, so the
            # ambiguous name only surfaces here. Nothing was created; the
            # Catalog shows the candidates and resubmits with the pick.
            return jsonify({'ok': False, 'error': 'ambiguous_outlet', 'candidates': credit['ambiguous']})
        if credit.get('blocked'):
            return jsonify({'ok': False, 'error': 'credit_block',
                            'message': tr('ဒီ ဆိုင်မှာ ငွေ မရသေးတဲ့ ဘောက်ချာ {n} စောင် ရှိနေပြီ — နောက်ထပ် ဘောက်ချာ အသစ် ဖွင့်လို့ မရတော့ဘူး။ ကျန်ငွေ အရင် ရှင်းပါ', n=credit.get('count', 0))}), 403
        return jsonify({'ok': False, 'error': 'invalid'}), 400
    o = db().execute("SELECT code FROM orders WHERE id=?", (oid,)).fetchone()
    return jsonify({'ok': True, 'code': o['code'], 'created': created, 'credit_warn': credit.get('stage', 0)})

@app.route('/api/my-orders')
@require_perm('sales.api_my_orders')
def api_my_orders():
    """The caller's OWN orders for today + yesterday, for the PWA's offline
    ဘောက်ချာ view (cached on the phone at every successful load/sync)."""
    d = db()
    uid = g.user['id']
    yday = (datetime.datetime.now(MMT) - datetime.timedelta(days=1)).date().isoformat()
    rows = []
    for o in d.execute("SELECT * FROM orders WHERE sales_user=? AND odate IN (?, ?) ORDER BY odate DESC, id DESC",
                       (uid, today(), yday)).fetchall():
        items = []
        tot = 0.0
        for it in d.execute("""SELECT oi.product_id, oi.qty, oi.price, oi.tier, oi.discount,
                                      p.sku, p.name FROM order_items oi JOIN products p ON p.id=oi.product_id
                               WHERE oi.order_id=? ORDER BY oi.id""", (o['id'],)).fetchall():
            lt = it['qty'] * it['price'] - (it['discount'] or 0)
            tot += lt
            items.append({'product_id': it['product_id'], 'sku': it['sku'], 'name': it['name'],
                          'qty': it['qty'], 'price': it['price'], 'tier': it['tier'] or '',
                          'discount': it['discount'] or 0, 'line_total': lt})
        gross = sum(i['qty'] * i['price'] for i in items)
        total = gross - (o['discount'] or 0)   # same basis as order_total()
        counts = edit_counts(d, o['id'])
        editable = bool(o['status'] == 'new' and not o['paid'] and not o['printed_at']
                        and o['odate'] == today() and counts.get('sales', 0) < EDIT_CAP)
        # v67: flag the creator's own invoices whose shop is still
        # ambiguous (synced from a stale cache) — the question is theirs
        # to answer, and the flag clears itself once the shop attaches.
        needs_outlet = bool(not o['outlet_id'] and (o['outlet'] or '').strip() and
                            len(_outlet_text_hits(d, o['outlet'])) >= 2)
        rows.append({'id': o['id'], 'code': o['code'], 'outlet': o['outlet'], 'area': o['area'] or '',
                     'odate': o['odate'], 'status': o['status'], 'paid': bool(o['paid']),
                     'printed': bool(o['printed_at']), 'discount': o['discount'] or 0,
                     'total': total, 'items': items, 'needs_outlet': needs_outlet,
                     'sales_edits_used': counts.get('sales', 0), 'editable': editable})
    return jsonify({'ok': True, 'today': today(), 'orders': rows})

EDIT_REJECT_PRINTED = 'ပုံနှိပ်ပြီးသား'
EDIT_REJECT_CAP = 'ပြင်ခွင့် ၂ ကြိမ် ကုန်ပြီ'
EDIT_REJECT_DAY = 'နေ့ကျော်သွားပြီ'
EDIT_REJECT_PAID = 'ငွေပေးချေပြီးသား'
EDIT_REJECT_STATE = 'အခြေအနေ ပြောင်းသွားပြီ'
EDIT_REJECT_HOURS = 'ရုံးချိန် (မနက် ၈:၃၀ – ညနေ ၅:၃၀) ကျော်သွားပြီ'
EDIT_REJECT_EMPTY = 'ပစ္စည်း မကျန်တော့ပါ — အနည်းဆုံး ၁ မျိုး ကျန်ရမယ်'

def _apply_offline_edit(d, user, edit):
    """Validate + apply ONE queued offline edit. Returns (ok, reason).
    All standing sales-edit rules are re-checked at apply time."""
    try:
        oid = int(edit.get('order_id') or 0)
    except Exception:
        oid = 0
    o = d.execute("SELECT * FROM orders WHERE id=?", (oid,)).fetchone() if oid else None
    if not o or o['sales_user'] != user['id']:
        return False, EDIT_REJECT_STATE
    if o['status'] != 'new':
        return False, EDIT_REJECT_STATE
    sync_paid(oid, d)
    o = d.execute("SELECT * FROM orders WHERE id=?", (oid,)).fetchone()
    if o['paid']:
        return False, EDIT_REJECT_PAID
    if o['printed_at']:
        return False, EDIT_REJECT_PRINTED
    if o['odate'] != today():
        return False, EDIT_REJECT_DAY
    if not office_open():
        return False, EDIT_REJECT_HOURS
    if edit_counts(d, oid).get('sales', 0) >= EDIT_CAP:
        return False, EDIT_REJECT_CAP
    payload_items = []
    for i in (edit.get('items') or []):
        try:
            pid = int(i.get('product_id')); qty = float(i.get('qty') or 0)
        except Exception:
            continue
        if qty > 0:
            payload_items.append({'product_id': pid, 'qty': qty, 'tier': i.get('tier') or '',
                                  'price': _flt(i.get('price')), 'discount': i.get('discount'),
                                  'has_disc': 'discount' in i})
    if not payload_items:
        return False, EDIT_REJECT_EMPTY
    existing = d.execute("SELECT oi.*, p.sku FROM order_items oi JOIN products p ON p.id=oi.product_id WHERE oi.order_id=? ORDER BY oi.id", (oid,)).fetchall()
    by_key = {}
    for it in existing:
        by_key.setdefault((it['product_id'], it['tier'] or ''), it)
        by_key.setdefault((it['product_id'], None), it)
    changes = []
    kept_ids = set()
    inserted = 0
    any_disc = any(i['has_disc'] for i in payload_items)
    for pi in payload_items:
        ex = by_key.get((pi['product_id'], pi['tier'])) or by_key.get((pi['product_id'], None))
        if ex is not None and ex['id'] not in kept_ids:
            kept_ids.add(ex['id'])
            price = ex['price']   # keep the original price snapshot
            dc = ex['discount'] or 0
            if pi['has_disc']:
                try: dc = float(pi['discount'] or 0)
                except (TypeError, ValueError): dc = ex['discount'] or 0
            dc = max(0.0, min(dc, pi['qty'] * price))
            if pi['qty'] != ex['qty'] or dc != (ex['discount'] or 0):
                d.execute("UPDATE order_items SET qty=?, discount=? WHERE id=?", (pi['qty'], dc, ex['id']))
            if pi['qty'] != ex['qty']:
                changes.append(f"{ex['sku']}: {('%g' % ex['qty'])} → {('%g' % pi['qty'])}")
            if dc != (ex['discount'] or 0):
                changes.append(tr('{sku} လျှော့ဈေး: {a} → {b}', sku=ex['sku'], a=('%g' % (ex['discount'] or 0)), b=('%g' % dc)))
        else:
            pr = d.execute("SELECT sku, actual_base, base_price FROM products WHERE id=?", (pi['product_id'],)).fetchone()
            if not pr:
                continue
            price = pi['price'] if pi['price'] is not None else 0
            dc = 0.0
            if pi['has_disc']:
                try: dc = float(pi['discount'] or 0)
                except (TypeError, ValueError): dc = 0.0
            dc = max(0.0, min(dc, pi['qty'] * price))
            d.execute("INSERT INTO order_items(order_id,product_id,qty,price,tier,discount,cost_act,cost_base) VALUES(?,?,?,?,?,?,?,?)",
                      (oid, pi['product_id'], pi['qty'], price, pi['tier'], dc, pr['actual_base'], pr['base_price']))
            inserted += 1
            changes.append(tr('{sku}: အသစ်ထည့် × {q}', sku=pr['sku'], q=('%g' % pi['qty'])))
    if not kept_ids and inserted == 0:
        return False, EDIT_REJECT_EMPTY   # nothing valid would remain — nothing was modified yet
    for it in existing:
        if it['id'] not in kept_ids:
            d.execute("DELETE FROM order_items WHERE id=?", (it['id'],))
            changes.append(tr('{sku}: ဖြုတ်', sku=it['sku']))
    if any_disc:
        new_disc = d.execute("SELECT COALESCE(SUM(discount),0) s FROM order_items WHERE order_id=?", (oid,)).fetchone()['s']
        if new_disc != (o['discount'] or 0):
            d.execute("UPDATE orders SET discount=? WHERE id=?", (new_disc, oid))
    over = 0
    for it in d.execute("SELECT * FROM order_items WHERE order_id=?", (oid,)).fetchall():
        if line_overstock(d, it['product_id'], it['qty']):
            over = 1
    d.execute("UPDATE orders SET overstock=? WHERE id=?", (over, oid))
    d.execute("INSERT INTO order_edits(order_id,by_user,by_role,summary,created_at) VALUES(?,?,?,?,?)",
              (oid, user['id'], 'sales', ('; '.join(changes) if changes else tr('Offline ပြင် (ပြောင်းလဲမှု မရှိ)')) + tr(' · [Offline ဖုန်းတန်းစီမှ]'), now()))
    return True, ''

@app.route('/api/order-edits', methods=['POST'])
@require_perm('sales.api_order_edits')
def api_order_edits():
    """Apply queued offline edits of SYNCED orders, in order. Each edit is
    validated independently against the standing rules at apply time; results
    are returned per edit so the phone can flag rejections (never silent)."""
    d = db()
    payload = request.get_json(force=True, silent=True) or {}
    edits = payload.get('edits') if isinstance(payload, dict) else payload
    if not isinstance(edits, list):
        edits = []
    results = []
    for edit in edits:
        if not isinstance(edit, dict):
            continue
        eu = (edit.get('client_edit_uuid') or '').strip()
        prev = d.execute("SELECT result, reason FROM offline_edit_log WHERE client_edit_uuid=?", (eu,)).fetchone() if eu else None
        if prev:
            results.append({'client_edit_uuid': eu, 'status': prev['result'], 'reason': tr(prev['reason']) if prev['reason'] else ''})
            continue
        ok, reason = _apply_offline_edit(d, g.user, edit)
        try:
            oid = int(edit.get('order_id') or 0)
        except Exception:
            oid = 0
        if eu:
            d.execute("INSERT OR IGNORE INTO offline_edit_log(client_edit_uuid, order_id, result, reason, by_user, created_at) VALUES(?,?,?,?,?,?)",
                      (eu, oid, 'ok' if ok else 'rejected', reason, g.user['id'], now()))
        d.commit()
        results.append({'client_edit_uuid': eu, 'status': 'ok' if ok else 'rejected', 'reason': tr(reason) if reason else ''})
    return jsonify({'ok': True, 'results': results})

@app.route('/api/outlets')
@login_required()
def api_outlets():
    # v67: the phone cache also carries the note and the pin, so the
    # entry-time chooser can label same-name shops and rank them by the
    # phone's own GPS distance — fully offline.
    # v68: the cache also carries each shop's ALIAS spellings, so a
    # name typed in another script finds the same shop on the phone.
    d = db()
    rows = d.execute("SELECT id, name, area, note, lat, lng FROM outlets ORDER BY name, area, id").fetchall()
    amap = {}
    for r in d.execute("SELECT outlet_id, alias FROM outlet_aliases ORDER BY id").fetchall():
        amap.setdefault(r['outlet_id'], []).append(r['alias'])
    out = []
    for r in rows:
        dd = dict(r); dd['aliases'] = amap.get(r['id'], []); out.append(dd)
    return jsonify(out)

@app.route('/api/visit-probe', methods=['POST'])
@require_perm('sales.api_visit_probe')
def api_visit_probe():
    p = request.get_json(force=True, silent=True) or {}
    lat, lng, acc = _flt(p.get('lat')), _flt(p.get('lng')), _flt(p.get('acc'))
    if lat is None or lng is None or acc is None or acc > 80:
        return jsonify({'ok': True, 'matched': None})
    vd = (p.get('pdate') or '').strip()
    if not re.match(r'^\d{4}-\d{2}-\d{2}$', vd) or vd > today():
        vd = today()
    d = db()
    if near_office(d, lat, lng):
        return jsonify({'ok': True, 'matched': None})
    best, best_dist = None, 1e18
    for o in d.execute("SELECT id, name, lat, lng FROM outlets WHERE lat IS NOT NULL AND lng IS NOT NULL").fetchall():
        dist = haversine_m(lat, lng, o['lat'], o['lng'])
        if dist < best_dist:
            best, best_dist = o, dist
    if best is not None and best_dist <= 30:
        record_visit(d, best['id'], g.user['id'], vd, 'probable', None, lat, lng, acc, p.get('client_uuid'))
        d.commit()
        return jsonify({'ok': True, 'matched': best['name']})
    return jsonify({'ok': True, 'matched': None})

@app.route('/orders/<int:oid>')
@login_required()
def order_detail(oid):
    d = db()
    o = d.execute("SELECT o.*, u.name rep FROM orders o LEFT JOIN users u ON u.id=o.sales_user WHERE o.id=?", (oid,)).fetchone()
    if not o: abort(404)
    if has_perm(g.user, 'dashboard.sales_own') and o['sales_user'] != g.user['id'] and not is_leader(g.user): abort(403)
    if has_perm(g.user, 'dashboard.delivery_own') and o['assigned_to'] != g.user['id']: abort(403)
    items = d.execute("SELECT oi.*, p.sku, p.name, p.unit, p.wholesale, p.actual_base FROM order_items oi JOIN products p ON p.id=oi.product_id WHERE oi.order_id=?", (oid,)).fetchall()
    total, subtotal = order_total(oid)
    flags = order_flags(o, items) if has_perm(g.user, 'actual.view') else ([tr('Offline က တင်လာတာ — stock ထက် ပိုနေတယ်')] if (o['overstock'] and o['offline']) else [])
    edits = [dict(e) for e in d.execute("SELECT e.*, u.name byname FROM order_edits e LEFT JOIN users u ON u.id=e.by_user WHERE e.order_id=? ORDER BY e.id", (oid,)).fetchall()]
    _seq = {}
    for e in edits:
        r = e['by_role'] or 'owner'
        _seq[r] = _seq.get(r, 0) + 1
        e['seq'] = _seq[r]
    counts = edit_counts(d, oid)
    can_edit = (o['status'] == 'new') and (not o['paid'] or has_perm(g.user, 'audit.view')) and (has_perm(g.user, 'products.create') or (has_perm(g.user, 'orders.edit') and o['sales_user'] == g.user['id']))
    if can_edit and not has_perm(g.user, 'products.create') and (o['printed_at'] or o['odate'] != today() or not office_open()):
        can_edit = False
    sales_lock = None
    if has_perm(g.user, 'dashboard.sales_own') and o['status'] == 'new':
        if o['paid']: sales_lock = 'paid'
        elif o['printed_at']: sales_lock = 'printed'
        elif o['odate'] != today(): sales_lock = 'day'
        elif not office_open(): sales_lock = 'hours'
    payments = d.execute("SELECT p.*, u.name by_name FROM order_payments p LEFT JOIN users u ON u.id=p.by_user WHERE p.order_id=? ORDER BY p.id", (oid,)).fetchall()
    received = order_received(oid, d)
    assignee = d.execute("SELECT name FROM users WHERE id=?", (o['assigned_to'],)).fetchone() if o['assigned_to'] else None
    # v50: recorded places (sold at / delivered at) for delivered invoices —
    # two independent fixes; the gap between them is the evidence.
    loc = None
    if o['status'] == 'delivered':
        sp = sale_point(d, o)
        att = latest_delivered_attempt(d, oid)
        dp = (att['lat'], att['lng']) if (att and att['lat'] is not None and att['lng'] is not None) else None
        dist = haversine_m(sp[0], sp[1], dp[0], dp[1]) if (sp and dp) else None
        checker = None
        if att and att['checked_by']:
            cu = d.execute("SELECT name FROM users WHERE id=?", (att['checked_by'],)).fetchone()
            checker = cu['name'] if cu else ''
        loc = {'sale': sp, 'del': dp, 'dist': fmt_dist(dist) if dist is not None else '',
               'att': att, 'checker': checker,
               'flag_dist': fmt_dist(att['gps_dist']) if (att and att['gps_flag']) else ''}
    # v67: the attached shop's disambiguated label (shown when the name
    # is shared), and — for an invoice still unattributed — the picker.
    # The picker belongs first to the invoice's CREATOR (the person who
    # stood at the shop); the office roles may also attach if they know.
    olabel = ''
    if o['outlet_id']:
        _ou = d.execute("SELECT * FROM outlets WHERE id=?", (o['outlet_id'],)).fetchone()
        if _ou: olabel = outlet_label(d, _ou)
    ambig_cands = []
    can_attach = False
    if not o['outlet_id'] and (o['outlet'] or '').strip():
        ambig_cands = outlet_candidates(d, o['outlet'])
        can_attach = bool(ambig_cands) and (o['sales_user'] == g.user['id'] or (has_perm(g.user, 'products.view') and has_perm(g.user, 'orders.attach_outlet')))
    return render_template('order_detail.html', o=o, items=items, total=total, subtotal=subtotal, flags=flags, fmt_money=fmt_money, edits=edits, can_edit=can_edit, counts=counts, EDIT_CAP=EDIT_CAP, sales_lock=sales_lock, payments=payments, received=received, balance=total - received, today_s=today(),
                           pay_ftok=uuid.uuid4().hex,
                           pending_collect=order_pending_collect(oid, d),
                           deliverers=delivery_users(d) if has_perm(g.user, 'delivery.assign') else [],
                           assignee_name=(assignee['name'] if assignee else ''), loc=loc,
                           olabel=olabel, ambig_cands=ambig_cands, can_attach=can_attach)

@app.route('/orders/<int:oid>/attach-outlet', methods=['POST'])
@login_required()
def order_attach_outlet(oid):
    """Attach an unattributed invoice to the shop it belongs to. The
    invoice's creator is the primary resolver (they stood at the shop);
    the office roles may attach too. The picked outlet must carry the
    invoice's shop name — attaching a differently-named shop is refused.
    Attaching clears the 'ဆိုင် မသဲကွဲ' notice by itself (the feed and
    the bell stop showing it once outlet_id is set)."""
    d = db()
    o = d.execute("SELECT * FROM orders WHERE id=?", (oid,)).fetchone()
    if not o: abort(404)
    if o['sales_user'] != g.user['id'] and not (has_perm(g.user, 'products.view') and has_perm(g.user, 'orders.attach_outlet')):
        abort(403)
    olid = request.form.get('outlet_id', type=int)
    ou = d.execute("SELECT * FROM outlets WHERE id=?", (olid,)).fetchone() if olid else None
    # v68: the picked shop may own the invoice's typed text as an ALIAS
    # spelling (the invoice was typed 'Sut', the shop is 'ဆွတ်') — that
    # attach is the right one, not a wrong-name attach.
    _ao = _alias_owner(d, _norm_shop_text(o['outlet'])) if ou else None
    if not ou or o['outlet_id'] or not (_norm_shop_text(ou['name']) == _norm_shop_text(o['outlet'])
                                        or (_ao is not None and _ao['id'] == ou['id'])):
        abort(400)
    d.execute("UPDATE orders SET outlet_id=? WHERE id=?", (ou['id'], oid))
    d.commit(); flash(tr('ဆိုင် တွဲပြီးပြီ'))
    return redirect(url_for('order_detail', oid=oid))

@app.route('/orders/<int:oid>/status', methods=['POST'])
@login_required()
def order_status(oid):
    d = db()
    o = d.execute("SELECT * FROM orders WHERE id=?", (oid,)).fetchone()
    if not o: abort(404)
    st = request.form.get('status')
    # Sales may create/edit their own orders only - delivering, payment marking,
    # printing and cancelling are warehouse/office duties (Ko, 2026-10-14)
    ACTION_PERMS = {'delivered': 'orders.status.deliver', 'cancelled': 'orders.status.cancel',
                    'paid': 'orders.status.paid_toggle', 'unpaid': 'orders.status.paid_toggle'}
    if st in ACTION_PERMS and not has_perm(g.user, ACTION_PERMS[st]):
        abort(403)
    if st == 'delivered' and o['status'] == 'new':
        # v61 (Ko-flagged, PV-0007): the office button used to deduct the
        # full ordered quantities blind — stock could silently go
        # negative, and the invoice kept its creation-time 'stock
        # မလောက်' flag after delivery. Now the plan is capped by what
        # each family pool actually holds (simulated line by line, in
        # order) and the SHARED delivery applies it: deliver what
        # exists, cancel the rest on the line (Ko's no-backorder rule),
        # never below zero. No GPS/money here — the office path stays
        # what it was, only the stock outcome is the same as the
        # delivery page's.
        items = d.execute("SELECT * FROM order_items WHERE order_id=?", (oid,)).fetchall()
        sim = {}
        plan = []
        for it in items:
            base, fctr = pool_info(d, it['product_id'])
            if base not in sim:
                sim[base] = raw_stock(d, base)
            avail = max(0.0, sim[base]) / fctr if fctr else 0.0
            dq = min(it['qty'], avail)
            if it['qty'] - dq < 1e-9:
                dq = it['qty']
            sim[base] -= dq * fctr
            plan.append((it, dq))
        if sum(dq for _it, dq in plan) <= 0:
            flash(tr('stock လုံးဝ မရှိဘူး — ပို့လို့ မရဘူး'))
            return redirect(url_for('order_detail', oid=oid))
        _osub, _nsub, new_disc = _delivery_figures(items, plan, o['discount'])
        partial = _apply_delivery(d, o, plan, new_disc)
        if partial:
            _notify_edit(d, oid, g.user, tr('ရုံးက ပို့ပြီး မှတ်တယ် (တစ်ပိုင်း — ကုန်မရ ပယ် ပါတယ်)'))
            flash(tr('ပို့ပြီး မှတ်ပြီးပြီ (တစ်ပိုင်း — stock ရှိသလောက်ပဲ ပို့ပြီး ကျန်တာ ပယ်ပါတယ်)'))
    elif st == 'cancelled' and o['status'] == 'delivered':
        # v49 (Ko, 2026-10-20): a delivered invoice is closed — cancelling is
        # refused here in the handler too, not just hidden in the page. Goods
        # that come back are recorded under Returns instead.
        flash(tr('ပို့ပြီး ဘောက်ချာ ကို ပယ်ဖျက်လို့ မရတော့ဘူး — ကုန် ပြန်လာရင် Returns မှာ မှတ်ပါ'))
        return redirect(url_for('order_detail', oid=oid))
    elif st == 'cancelled' and o['status'] == 'new':
        d.execute("UPDATE orders SET status='cancelled' WHERE id=?", (oid,))
        audit_log(d, g.user, 'cancel',
                  'ဘောက်ချာ ပယ်ဖျက် — %s (%s)' % (o['code'], o['outlet']),
                  'order', oid, 1)
    elif st == 'paid':
        if o['status'] != 'delivered':
            # Money is only received after delivery (COD flow) - Ko, 2026-10-15
            flash(tr('ငွေ ရပြီး မှတ်ဖို့ အရင် ပစ္စည်း ပို့ပြီး ဖြစ်ရပါမယ်'))
            return redirect(url_for('order_detail', oid=oid))
        total, _ = order_total(oid)
        # v65 (Ko, 2026-10-23 — PV-0007): the balance used to be worked
        # out from VERIFIED money only, blind to a delivery person's
        # still-unchecked collection. Marking 'paid' then recorded the
        # full amount a SECOND time, and verifying the handover later
        # made the invoice show 2x the money that ever came in. The
        # hand-recordable balance is total - verified - pending.
        pending = order_pending_collect(oid, d)
        bal = total - order_received(oid, d) - pending
        if bal > 0.5:
            d.execute("INSERT INTO order_payments(order_id, amount, rdate, note, by_user, created_at) VALUES(?,?,?,?,?,?)",
                      (oid, bal, today(), tr('အပြည့် ရရှိ'), g.user['id'], now()))
            sync_paid(oid, d)
        elif pending > 0.5:
            flash(tr('Delivery ကောက် ထားတဲ့ ငွေ {x} Ks အတည် ပြုရန် ကျန် နေ တယ် — ထပ် မမှတ် ပါနဲ့၊ အရင် Handover မှာ အတည် ပြုပါ', x=fmt_money(pending)))
            return redirect(url_for('order_detail', oid=oid))
    elif st == 'unpaid':
        flash(tr('ငွေ မှတ်တမ်းကို အောက်က "ငွေ ရရှိ မှတ်တမ်း" မှာ ပြင်/ဖျက် ပါ'))
        return redirect(url_for('order_detail', oid=oid))
    d.commit()
    return redirect(url_for('order_detail', oid=oid))

@app.route('/orders/<int:oid>/payment', methods=['POST'])
@require_perm('payments.record')
def order_payment_add(oid):
    d = db()
    o = d.execute("SELECT * FROM orders WHERE id=?", (oid,)).fetchone()
    if not o: abort(404)
    # v65: one-time form token (the v63 pattern) — a lag double-tap or a
    # refresh-resubmit of THIS form records nothing a second time.
    ftok = (request.form.get('ftok') or '').strip()
    used = _used_pay_token(d, ftok)
    if used is not None:
        _dup_pay_flash(used)
        return redirect(url_for('order_detail', oid=oid))
    if o['status'] != 'delivered':
        flash(tr('ပစ္စည်း ပို့ပြီးမှ ငွေ မှတ်လို့ရပါမယ်'))
        return redirect(url_for('order_detail', oid=oid))
    try:
        amt = float((request.form.get('amount') or '0').replace(',', ''))
    except ValueError:
        amt = 0
    total, _ = order_total(oid)
    # v65 (Ko, 2026-10-23 — PV-0007): the recordable balance used to be
    # total - VERIFIED only, blind to a delivery person's still-unchecked
    # collection — so the same money could be recorded by hand in full,
    # and the handover verify later doubled the invoice's received
    # figure. Pending collections reserve their part of the balance.
    pending = order_pending_collect(oid, d)
    bal = total - order_received(oid, d) - pending
    if amt <= 0:
        flash(tr('ငွေ ပမာဏ ထည့်ပါ'))
    elif amt > bal + 0.5 and pending > 0.5 and amt <= bal + pending + 0.5:
        # the amount only overflows because the pending collection is
        # holding part of the balance — say that, plainly
        flash(tr('Delivery ကောက် ထားတဲ့ ငွေ {x} Ks အတည် ပြုရန် ကျန် နေ တယ် — ထပ် မမှတ် ပါနဲ့၊ အရင် Handover မှာ အတည် ပြုပါ', x=fmt_money(pending)))
    elif amt > bal + 0.5:
        flash(tr('ကျန်ငွေ ထက် များနေတယ် (ကျန်: {bal} Ks)', bal='{:,.0f}'.format(max(bal, 0))))
    else:
        rdate = (request.form.get('rdate') or '').strip()
        if not re.match(r'^\d{4}-\d{2}-\d{2}$', rdate or ''): rdate = today()
        method = (request.form.get('method') or 'cash').strip()
        if method not in PAY_METHOD_KEYS: method = 'cash'
        # v71: the hand-recorded payment's own note breakdown (Cash only,
        # optional, warn-only — same v60 counter as the other cash points).
        # The automatic 'paid'-button insert below has no form, so it stays
        # unsplit and the Cashbox shows it as 'ရွက်ရေ မမှတ်'.
        pay_denom_json, _dt = parse_denom(request.form, 'op_') if method == 'cash' else (None, 0)
        d.execute("INSERT INTO order_payments(order_id, amount, rdate, note, by_user, created_at, method, verified, denom) VALUES(?,?,?,?,?,?,?,?,?)",
                  (oid, amt, rdate, (request.form.get('note') or '').strip(), g.user['id'], now(), method, 1, pay_denom_json))
        if not _spend_pay_token(d, ftok, oid):
            d.rollback(); _dup_pay_flash(_used_pay_token(d, ftok))
            return redirect(url_for('order_detail', oid=oid))
        sync_paid(oid, d)
        d.commit()
        flash(tr('ငွေ {amt} Ks မှတ်ပြီးပြီ', amt='{:,.0f}'.format(amt)))
    return redirect(url_for('order_detail', oid=oid))

@app.route('/orders/<int:oid>/payment/<int:pid>/delete', methods=['POST'])
@require_perm('payments.delete')
def order_payment_delete(oid, pid):
    d = db()
    pay = d.execute("SELECT * FROM order_payments WHERE id=? AND order_id=?", (pid, oid)).fetchone()
    o = d.execute("SELECT code, outlet FROM orders WHERE id=?", (oid,)).fetchone()
    d.execute("DELETE FROM order_payments WHERE id=? AND order_id=?", (pid, oid))
    sync_paid(oid, d)
    if pay and o:
        audit_log(d, g.user, 'paydel',
                  'ငွေ မှတ်တမ်း ဖျက် — %s (%s): %s Ks' % (o['code'], o['outlet'], fmt_money(pay['amount'])),
                  'order', oid, 1)
    d.commit()
    flash(tr('ငွေ မှတ်တမ်း ဖျက်ပြီးပြီ'))
    return redirect(url_for('order_detail', oid=oid))

@app.route('/orders/<int:oid>/invoice')
@require_perm('invoice.print')
def invoice(oid):
    d = db()
    o = d.execute("SELECT o.*, u.name rep FROM orders o LEFT JOIN users u ON u.id=o.sales_user WHERE o.id=?", (oid,)).fetchone()
    if not o: abort(404)
    items = d.execute("SELECT oi.*, p.sku, p.name, p.unit FROM order_items oi JOIN products p ON p.id=oi.product_id WHERE oi.order_id=?", (oid,)).fetchall()
    total, subtotal = order_total(oid)
    if not o['printed_at']:
        d.execute("UPDATE orders SET printed_at=? WHERE id=?", (now(), oid)); d.commit()
    return render_template('invoice.html', o=o, items=items, total=total, subtotal=subtotal, fmt_money=fmt_money, biz=biz_info(d))


# ---------------- v47: Delivery module ----------------
# Delivery staff get their own accounts and see ONLY the invoices given to
# them (Ko, 2026-10-19). Two owner rules shape everything here:
#  * collections are recorded by the delivery person but count as received
#    only after Finance checks the handover (recorder != verifier);
#  * there is NO carry-over: what cannot be delivered is cancelled — a
#    short delivery closes the invoice at what really went out, and a
#    failed delivery (shop closed...) cancels the whole invoice.
FAIL_REASONS = ('ဆိုင်ပိတ်', 'လူမရှိ', 'နောက်မှ လာပို့ရန်', 'တခြား')

def delivery_users(d):
    return d.execute("SELECT id, name FROM users WHERE role='delivery' AND active=1 ORDER BY name").fetchall()

def _notify_edit(d, oid, user, summary, attempt_id=None, domain='orders'):
    """Surface an event in /notifications (which reads order_edits).
    v50: every stored notice carries its domain ('orders'; money/backup
    alerts are derived live, never stored). v67 adds the stored 'outlets'
    domain: an invoice whose shop could not be told apart at sync time.
    It stays visible only while the invoice is unattributed — the feed
    and the bell both filter it out once the shop is attached."""
    d.execute("INSERT INTO order_edits(order_id,by_user,by_role,summary,created_at,domain,attempt_id) VALUES(?,?,?,?,?,?,?)",
              (oid, user['id'], user['role'], summary, now(), domain, attempt_id))

# ---------------- v51: Mini Audit System (Ko, 2026-10-21) ----------------
# One Owner-only record of important ACTIONS (changes, not views): price
# saves, stock adjustments, invoice cancels, payment deletions, purchase
# cancels, expense deletions, settlement payments. A few of them are also
# sensitive enough to raise an Owner-only bell notice (notify=1) that stays
# until the Owner marks it handled (handled_by/handled_at — the same
# stored-notice pattern as the v50 GPS check). Thresholds are plain
# defaults Ko can ask to change: a Retail/Wholesale cut of 10%+ in one
# save, and a manual stock adjustment worth 100,000 Ks+ at Actual.
AUDIT_CUT_PCT = 10.0
AUDIT_STOCK_KS = 100000.0

def audit_log(d, user, kind, summary, ref_type=None, ref_id=None, notify=0):
    """Write one audit_events row. Summaries are stored plain Burmese text
    (a stored record, like other stored notes — shown as stored)."""
    try:
        uid = user['id']
    except Exception:
        uid = None
    d.execute("INSERT INTO audit_events(created_at,user_id,kind,summary,ref_type,ref_id,notify) "
              "VALUES(?,?,?,?,?,?,?)",
              (now(), uid, kind, summary, ref_type, ref_id, 1 if notify else 0))
    return d.execute("SELECT last_insert_rowid() i").fetchone()['i']

def audit_open_count(d):
    """Sensitive audit notices still waiting for the Owner's handled mark."""
    try:
        return d.execute("SELECT COUNT(*) c FROM audit_events WHERE COALESCE(notify,0)=1 AND handled_by IS NULL").fetchone()['c']
    except Exception:
        return 0

def audit_open_rows(d, limit=50):
    return d.execute("""SELECT a.*, u.name byname, h.name handledname FROM audit_events a
                        LEFT JOIN users u ON u.id=a.user_id LEFT JOIN users h ON h.id=a.handled_by
                        WHERE COALESCE(a.notify,0)=1 ORDER BY a.id DESC LIMIT ?""", (limit,)).fetchall()

# ---------------- v50: GPS place check + notification unification ---------
# Ko's rules (2026-10-20): (1) operational alerts live as PERSISTENT
# notifications until they resolve — never one-off flashes; (2) a user only
# ever sees notices from sections their role may open (audience by domain:
# 'backup' -> owner, 'money' -> owner+finance, 'orders' -> the three office
# roles, exactly as before); (3) a notice clears itself when the system can
# see the condition is fixed — only judgment calls (the GPS place check)
# wait for a person's "✓ စစ်ပြီး" tap, which records who and when.
# The GPS check itself: the place an order was sold from (the order's own
# submit fix on its visits row, else the shop's learned pin) and the place
# the delivery was marked from are two independent records. GPS here can be
# jammed, so the check is accuracy-aware (both fixes' accuracy radii are
# subtracted first), the wording stays neutral, and nothing is ever blocked.

def haversine_m(lat1, lng1, lat2, lng2):
    r = 6371000.0
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dp = math.radians(lat2 - lat1); dl = math.radians(lng2 - lng1)
    a = math.sin(dp / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2
    return 2 * r * math.asin(min(1.0, math.sqrt(a)))

def fmt_dist(m):
    """Plain distance text: '850 m' under a kilometre, else '1.2 km'."""
    if m is None: return ''
    if m < 1000: return '%d m' % int(round(m))
    return '%.1f km' % (m / 1000.0)

def gps_threshold(d):
    try:
        v = int(float(get_setting(d, 'gps_mismatch_m') or 200))
    except (TypeError, ValueError):
        v = 200
    return max(20, min(v, 10000))

def sale_point(d, o):
    """(lat, lng, acc) the sale was recorded at, or None: the order's own
    submit fix first (visits row for this order), then the shop's pin."""
    v = d.execute("SELECT lat, lng, acc FROM visits WHERE order_id=? AND kind='order' AND lat IS NOT NULL AND lng IS NOT NULL ORDER BY id DESC LIMIT 1",
                  (o['id'],)).fetchone()
    if v: return (v['lat'], v['lng'], v['acc'])
    if o['outlet_id']:
        ou = d.execute("SELECT lat, lng FROM outlets WHERE id=?", (o['outlet_id'],)).fetchone()
        if ou and ou['lat'] is not None and ou['lng'] is not None:
            return (ou['lat'], ou['lng'], None)
    return None

def latest_delivered_attempt(d, oid):
    return d.execute("SELECT * FROM delivery_attempts WHERE order_id=? AND result='delivered' ORDER BY id DESC LIMIT 1",
                     (oid,)).fetchone()

def _gps_check(d, o, att_id, lat, lng, acc, user):
    """After a delivered attempt is stored: compare its place with the sale
    place. Beyond the (accuracy-adjusted) threshold, flag the attempt and
    leave a persistent notice for the office roles. Never blocks anything."""
    if lat is None or lng is None or not att_id: return
    sp = sale_point(d, o)
    if not sp: return
    dist = haversine_m(sp[0], sp[1], lat, lng)
    gap = max(0.0, dist - (sp[2] or 0) - (acc or 0))
    if gap > gps_threshold(d):
        d.execute("UPDATE delivery_attempts SET gps_flag=1, gps_dist=? WHERE id=?", (dist, att_id))
        _notify_edit(d, o['id'], user,
                     tr('{code} — ရောင်း တဲ့ နေရာ နဲ့ ပို့ တဲ့ နေရာ ~{dist} ခြား နေ တယ် — စစ် ကြည့် ပါ',
                        code=o['code'], dist=fmt_dist(dist)),
                     attempt_id=att_id)

def cash_count_diff(d):
    """The open cash-count difference, derived from the LATEST count each
    time (never a stored flag): {'cdate','counted','diff'} or None. v70:
    the Cashbox close is the live flow, so its latest counted/closed
    day's stored difference wins; legacy cash_counts rows are only the
    fallback for pre-v70 counts."""
    try:
        row = d.execute("SELECT * FROM cashbox_days WHERE status IN ('counted','closed') AND diff IS NOT NULL ORDER BY cdate DESC LIMIT 1").fetchone()
        if row and abs(row['diff'] or 0) >= 0.5:
            return {'cdate': row['cdate'], 'counted': row['counted'], 'diff': row['diff'], 'href': '/cashbox/' + row['cdate']}
    except Exception:
        pass
    row = d.execute("SELECT * FROM cash_counts ORDER BY cdate DESC LIMIT 1").fetchone()
    if not row: return None
    diff = (row['amount'] or 0) - money_balances(d, upto=row['cdate'])['cash']
    if abs(diff) < 0.5: return None
    return {'cdate': row['cdate'], 'counted': row['amount'], 'diff': diff, 'href': '/money'}

def backup_is_due(d):
    """True while no backup download is on record, or the last one is
    15+ days old (the same rule as the v49 dashboard notice)."""
    last_dl = get_setting(d, 'last_backup_download')
    if not last_dl: return True
    try:
        y, mo, dd = map(int, last_dl[:10].split('-'))
        return (datetime.datetime.now(MMT).date() - datetime.date(y, mo, dd)).days >= 15
    except Exception:
        return True

def open_alerts(d, u):
    """Derived (never stored) open alerts for this user, by domain
    audience: cash-count difference -> owner+finance; backup due -> owner.
    GPS place flags are stored notices, counted separately off the
    attempts themselves."""
    out = []
    if not u: return out
    if has_perm(u, 'money.view'):
        cd = cash_count_diff(d)
        if cd:
            out.append({'domain': 'money', 'href': cd.get('href') or '/money',
                        'text': tr('ငွေ ရေတွက် စာရင်း ကွာ နေ တယ် — {grp} {amt} Ks',
                                   grp=tr('ငွေသား'), amt=fmt_money(abs(cd['diff'])))})
    if has_perm(u, 'backups.view') and backup_is_due(d):
        out.append({'domain': 'backup', 'href': '/backups',
                    'text': tr('Backup မ ဆွဲ ရ သေး တာ ၁၅ ရက် ကျော် နေ ပြီ')})
    return out

def gps_open_count(d):
    """Delivered attempts still waiting for a person's ✓ စစ်ပြီး."""
    return d.execute("SELECT COUNT(*) c FROM delivery_attempts WHERE COALESCE(gps_flag,0)=1 AND checked_by IS NULL").fetchone()['c']

@app.route('/delivery-board')
@require_perm('delivery.view_board')
def delivery_board():
    d = db()
    day = (request.args.get('day') or '').strip()
    if not re.match(r'^\d{4}-\d{2}-\d{2}$', day or ''): day = today()
    deliverers = delivery_users(d)
    dnames = {u['id']: u['name'] for u in d.execute("SELECT id, name FROM users").fetchall()}
    _new_orders = d.execute("SELECT * FROM orders WHERE status='new' ORDER BY area, odate, id").fetchall()
    _done_ids = [r['order_id'] for r in d.execute("SELECT DISTINCT order_id FROM delivery_attempts WHERE result='delivered' AND substr(created_at,1,10)=?", (day,)).fetchall()]
    _tot = order_totals_map(d, [o['id'] for o in _new_orders] + _done_ids)
    groups = {}
    for o in _new_orders:
        groups.setdefault(o['area'] or '', []).append(
            {'o': o, 'total': _tot.get(o['id'], (0, 0))[0], 'assignee': dnames.get(o['assigned_to'], '')})
    board = []
    for u in d.execute("SELECT id, name FROM users WHERE role='delivery' ORDER BY active DESC, name").fetchall():
        uid = u['id']
        att = d.execute("SELECT result, COUNT(*) c FROM delivery_attempts WHERE by_user=? AND substr(created_at,1,10)=? GROUP BY result", (uid, day)).fetchall()
        attm = {r['result']: r['c'] for r in att}
        expected = 0.0
        for r in d.execute("SELECT DISTINCT order_id FROM delivery_attempts WHERE by_user=? AND result='delivered' AND substr(created_at,1,10)=?", (uid, day)).fetchall():
            expected += _tot.get(r['order_id'], (0, 0))[0]
        coll = d.execute("SELECT COALESCE(SUM(amount),0) v FROM order_payments WHERE by_user=? AND rdate=?", (uid, day)).fetchone()['v']
        verf = d.execute("SELECT COALESCE(SUM(amount),0) v FROM order_payments WHERE by_user=? AND rdate=? AND COALESCE(verified,1)=1", (uid, day)).fetchone()['v']
        assigned = d.execute("SELECT COUNT(*) c FROM orders WHERE assigned_to=? AND status='new'", (uid,)).fetchone()['c']
        board.append({'u': u, 'assigned': assigned, 'delivered': attm.get('delivered', 0),
                      'failed': attm.get('failed', 0), 'expected': expected,
                      'collected': coll, 'verified': verf})
    fails = d.execute("SELECT a.*, o.code, o.outlet, u.name byname FROM delivery_attempts a JOIN orders o ON o.id=a.order_id "
                      "LEFT JOIN users u ON u.id=a.by_user WHERE a.result='failed' AND substr(a.created_at,1,10)=? ORDER BY a.id DESC", (day,)).fetchall()
    return render_template('delivery_board.html', groups=groups, deliverers=deliverers, board=board,
                           fails=fails, day=day, fmt_money=fmt_money, gps_m=gps_threshold(d),
                           gps_open=gps_open_count(d))

@app.route('/delivery/assign', methods=['POST'])
@require_perm('delivery.assign')
def delivery_assign():
    d = db(); f = request.form
    uid = f.get('user_id', type=int)
    tgt = d.execute("SELECT id FROM users WHERE id=? AND role='delivery' AND active=1", (uid,)).fetchone() if uid else None
    if not tgt:
        flash(tr('ပို့သူ ရွေးပါ'))
        return redirect(url_for('delivery_board', day=f.get('day') or today()))
    if f.get('area') is not None and f.get('bulk'):
        d.execute("UPDATE orders SET assigned_to=?, assigned_at=? WHERE status='new' AND COALESCE(area,'')=?",
                  (uid, now(), f.get('area') or ''))
    elif f.get('order_id'):
        d.execute("UPDATE orders SET assigned_to=?, assigned_at=? WHERE id=? AND status='new'",
                  (uid, now(), int(f['order_id'])))
    d.commit(); flash(tr('တာဝန် ပေးပြီးပြီ'))
    return redirect(url_for('delivery_board', day=f.get('day') or today()))

@app.route('/orders/<int:oid>/assign', methods=['POST'])
@require_perm('delivery.assign')
def order_assign(oid):
    d = db()
    uid = request.form.get('user_id', type=int)
    tgt = d.execute("SELECT id FROM users WHERE id=? AND role='delivery' AND active=1", (uid,)).fetchone() if uid else None
    if tgt:
        d.execute("UPDATE orders SET assigned_to=?, assigned_at=? WHERE id=? AND status='new'", (uid, now(), oid))
        d.commit(); flash(tr('တာဝန် ပေးပြီးပြီ'))
    else:
        flash(tr('ပို့သူ ရွေးပါ'))
    return redirect(url_for('order_detail', oid=oid))

def _delivery_figures(items, plan, order_discount):
    """Money maths of a delivery, shared by both delivery paths (v61):
    what actually goes out, priced from the plan. Line discounts scale
    with each line's delivered share; a legacy order-level discount
    scales with the delivered subtotal instead."""
    old_sub = sum(it['qty'] * it['price'] for it in items)
    old_ld = sum((it['discount'] or 0) for it in items)
    new_sub = sum(dq * it['price'] for it, dq in plan)
    if old_ld > 0:
        new_disc = sum((it['discount'] or 0) * (dq / it['qty'] if it['qty'] else 0) for it, dq in plan)
    elif order_discount:
        new_disc = order_discount * (new_sub / old_sub if old_sub else 0)
    else:
        new_disc = 0.0
    return old_sub, new_sub, new_disc


def _apply_delivery(d, o, plan, new_disc):
    """The delivery itself — ONE implementation for the delivery-role
    page and the office status button (v61, Ko-flagged: the button used
    to deduct the full ordered quantities with no stock check at all,
    and left the creation-time overstock flag sitting on a delivered
    invoice forever). plan = [(order_item, delivered_qty)] in each
    line's own units. A short line is rewritten (the original qty is
    kept in orig_qty, the undelivered rest recorded as cancelled_qty,
    its line discount scaled); stock is deducted for what goes out; the
    order closes as delivered with its partial flag set and its
    overstock flag recomputed truthfully from the outcome — what was
    delivered never exceeds what the pool held for it, so the stale
    flag clears (a shortfall shows as တစ်ပိုင်း instead); the flag
    survives only when a family's pool was driven below zero.
    Returns 1 when any line fell short, else 0."""
    partial = 0
    drawn = {}
    for it, dq in plan:
        if dq < it['qty'] - 1e-9:
            partial = 1
            nd = (it['discount'] or 0) * (dq / it['qty'] if it['qty'] else 0)
            d.execute("UPDATE order_items SET orig_qty=COALESCE(orig_qty, qty), cancelled_qty=?, qty=?, discount=? WHERE id=?",
                      (it['qty'] - dq, dq, nd, it['id']))
        if dq > 0:
            move_stock(it['product_id'], -dq, 'sale', o['code'], '', g.user['id'])
            base, f = pool_info(d, it['product_id'])
            drawn[base] = drawn.get(base, 0) + dq * f
    over = 0
    for base in drawn:
        if raw_stock(d, base) < -1e-9:
            over = 1
    d.execute("UPDATE orders SET status='delivered', discount=?, partial=?, overstock=? WHERE id=?",
              (new_disc, partial, over, o['id']))
    return partial


def _deliver_gate(oid):
    d = db()
    o = d.execute("SELECT * FROM orders WHERE id=?", (oid,)).fetchone()
    if not o: abort(404)
    if has_perm(g.user, 'dashboard.delivery_own'):
        if o['assigned_to'] != g.user['id']: abort(403)
    elif not has_perm(g.user, 'delivery.deliver'):
        abort(403)
    return d, o

@app.route('/deliver/<int:oid>', methods=['GET', 'POST'])
@login_required()
def deliver_page(oid):
    d, o = _deliver_gate(oid)
    back = url_for('dashboard') if has_perm(g.user, 'dashboard.delivery_own') else url_for('order_detail', oid=oid)
    if o['status'] != 'new':
        flash(tr('ဒီ ဘောက်ချာ က ပို့လို့ မရတော့ဘူး'))
        return redirect(back)
    items = [dict(r) for r in d.execute("SELECT oi.*, p.sku, p.name, p.unit FROM order_items oi JOIN products p ON p.id=oi.product_id WHERE oi.order_id=?", (oid,)).fetchall()]
    for _it in items: _it['dqv'] = None
    total, old_sub = order_total(oid)
    old_ld = sum((it['discount'] or 0) for it in items)
    def _render(vals=None):
        # v48: a bounced submit re-renders THIS form with the typed values kept
        # (vals.preserve), instead of redirecting to a fresh GET that silently
        # reset every quantity back to full.
        return render_template('deliver_confirm.html', o=o, items=items, total=total,
                               old_sub=old_sub, old_ld=old_ld,
                               reasons=FAIL_REASONS, fmt_money=fmt_money,
                               vals=vals or {'collected': None, 'method': None, 'money_note': '', 'preserve': False, 'denom': {}})
    if request.method == 'GET':
        return _render()
    f = request.form
    lat, lng, acc = _flt(f.get('lat')), _flt(f.get('lng')), _flt(f.get('acc'))
    if f.get('action') == 'fail':
        reason = (f.get('reason') or '').strip()
        if reason not in FAIL_REASONS: reason = 'တခြား'
        note = (f.get('note') or '').strip()
        d.execute("INSERT INTO delivery_attempts(order_id,by_user,result,reason,note,lat,lng,acc,created_at) VALUES(?,?,?,?,?,?,?,?,?)",
                  (oid, g.user['id'], 'failed', reason, note, lat, lng, acc, now()))
        full_reason = reason + ((' — ' + note) if note else '')
        d.execute("UPDATE orders SET status='cancelled', cancel_reason=? WHERE id=?", (full_reason, oid))
        _notify_edit(d, oid, g.user, tr('ပို့သူ က မပို့ဖြစ် မှတ်တယ် — ဘောက်ချာ ပယ်ဖျက်ပြီး') + ' · ' + full_reason)
        d.commit(); flash(tr('မပို့ဖြစ် — ဘောက်ချာ ပယ်ဖျက်ပြီးပြီ'))
        return redirect(back)
    # ---- deliver (full or short) ----
    plan = []
    for it in items:
        raw = f.get(f'dq_{it["id"]}')
        try:
            dq = float(raw) if raw not in (None, '') else it['qty']
        except ValueError:
            dq = it['qty']
        dq = max(0.0, min(dq, it['qty']))
        plan.append((it, dq))
    if sum(dq for _it, dq in plan) <= 0:
        flash(tr('အနည်းဆုံး ၁ မျိုး ပို့တဲ့ အရေအတွက် ထည့်ပါ'))
        for it, dq in plan: it['dqv'] = dq
        return _render({'collected': f.get('collected', ''), 'method': f.get('method') or 'cash',
                        'money_note': f.get('money_note', ''), 'preserve': True,
                        'denom': denom_counts(parse_denom(f, 'dl_')[0])})
    _osub, new_sub, new_disc = _delivery_figures(items, plan, o['discount'])
    payable = new_sub - new_disc
    craw = (f.get('collected') or '').replace(',', '').strip()
    try:
        collected = float(craw) if craw else payable
    except ValueError:
        collected = payable
    if collected < 0: collected = 0
    if collected > payable + 0.5 and abs(collected - total) <= 0.5:
        # v48 safety net: the form pre-fills collected with the ORIGINAL total;
        # quantities were then reduced, so an untouched default now sits above
        # the reduced payable. That is the form's own number, not an overage
        # the person typed — follow the reduced payable instead of bouncing.
        collected = payable
    if collected > payable + 0.5:
        flash(tr('ကောက်ငွေ က ရှင်းရမယ့် ငွေထက် များနေတယ်'))
        for it, dq in plan: it['dqv'] = dq
        return _render({'collected': f.get('collected', ''), 'method': f.get('method') or 'cash',
                        'money_note': f.get('money_note', ''), 'preserve': True,
                        'denom': denom_counts(parse_denom(f, 'dl_')[0])})
    partial = _apply_delivery(d, o, plan, new_disc)
    method = (f.get('method') or 'cash').strip()
    if method not in PAY_METHOD_KEYS: method = 'cash'
    # v60: the collector's own note breakdown of the cash they took
    # (optional, Cash only) — saved on the attempt so Finance counts the
    # physical notes against it at the handover. Never affects the money.
    denom_json, _dt = parse_denom(f, 'dl_') if method == 'cash' else (None, 0)
    d.execute("INSERT INTO delivery_attempts(order_id,by_user,result,reason,note,lat,lng,acc,created_at,denom) VALUES(?,?,?,?,?,?,?,?,?,?)",
              (oid, g.user['id'], 'delivered', '', (f.get('note') or '').strip(), lat, lng, acc, now(), denom_json))
    _att_id = d.execute("SELECT last_insert_rowid() i").fetchone()['i']
    _gps_check(d, o, _att_id, lat, lng, acc, g.user)   # v50: sold-vs-delivered place check
    if collected > 0:
        verified = 1 if has_perm(g.user, 'payments.record') else 0
        d.execute("INSERT INTO order_payments(order_id, amount, rdate, note, by_user, created_at, method, verified) VALUES(?,?,?,?,?,?,?,?)",
                  (oid, collected, today(), (f.get('money_note') or '').strip(), g.user['id'], now(), method, verified))
    sync_paid(oid, d)
    if partial:
        _notify_edit(d, oid, g.user, tr('ပို့သူ က ပို့ပြီး မှတ်တယ် (တစ်ပိုင်း — ကုန်မရ ပယ် ပါတယ်)'))
    d.commit(); flash(tr('ပို့ပြီး မှတ်ပြီးပြီ'))
    return redirect(back)

# ---------------- v78: Offline Delivery (Ko, 2026-10-25) ----------------
# Delivery role ONLY, phone-first. The delivery form on the phone is fully
# editable UNTIL the confirm tap; the phone queues the attempt and pushes
# it when back online. BEFORE marked delivered, corrections happen in one
# pass on the phone; AFTER confirm, the attempt is sealed — there is NO
# post-delivery edit door here (only 'new' invoices can be delivered;
# mistakes after delivery go through the office). The Catalog offline
# flow and every online delivery path are untouched.
# Money/secrecy: the phone sees the payable total only, never per-line
# prices and never any cost data (no price/cost field is in either API
# response). Field collections land as order_payments rows with
# verified=0 — pending until Finance/Owner handover verification,
# exactly like an online delivery person's collections.
OFFDEL_REJECT_NO_UUID = 'မှတ်တမ်း ကုဒ် မပါဘူး'
OFFDEL_REJECT_STATE = 'ဒီ ဘောက်ချာ က ပို့လို့ မရတော့ဘူး'
OFFDEL_REJECT_LINE = 'ဘောက်ချာ ထဲမှာ မပါတဲ့ ပစ္စည်း ပါဝင်နေတယ်'
OFFDEL_REJECT_QTY = 'အရေအတွက် မှားနေတယ်'
OFFDEL_REJECT_FAIL_REASON = 'မပို့ဖြစ်တဲ့ အကြောင်း ထည့်ပါ'
OFFDEL_REJECT_MONEY = 'ကောက်ငွေ မှားနေတယ်'
OFFDEL_REJECT_RETRY = 'တင်လို့ မရဘူး — နောက်မှ ပြန်စမ်းပါ'
OFFDEL_OVER_COLLECT = 'ကောက်ငွေ က ရှင်းရမယ့် ငွေထက် များနေတယ်'
OFFDEL_ALL_ZERO = 'အနည်းဆုံး ၁ မျိုး ပို့တဲ့ အရေအတွက် ထည့်ပါ'

def _offdel_int(v):
    """Lenient int for phone-sent ids (accepts 3 and '3', not '3.5')."""
    try:
        return int(v)
    except (TypeError, ValueError):
        try:
            f = float(v)
        except (TypeError, ValueError):
            return None
        return int(f) if f == int(f) else None

def _apply_offline_delivery_attempt(d, user, att):
    """Validate + apply ONE queued offline delivery attempt. Returns
    ('ok', '') or ('rejected', plain-Burmese-reason). Writes NOTHING on
    a rejection (every check runs before any write), so the caller can
    commit per attempt and a batch can succeed partially. Applies
    through the EXISTING _apply_delivery machinery — shortfall rewrite
    (orig_qty kept, cancelled_qty recorded), payable = delivered lines
    only, NO backorder ever — and the existing _gps_check place check
    for delivered attempts. A failed attempt (shop closed etc.) cancels
    the whole invoice with its reason, as online. Lines omitted from a
    delivered attempt count as 0 delivered (cancelled with the rest of
    the shortfall); the phone must therefore send every line, with 0s,
    not only the lines it carried out."""
    eu = att.get('client_attempt_uuid')
    eu = eu.strip() if isinstance(eu, str) else ''
    if not eu:
        return 'rejected', OFFDEL_REJECT_NO_UUID
    # Idempotency: an already-APPLIED attempt with this uuid returns ok
    # without re-applying (a re-push after a network blip must not
    # deliver twice, re-cancel, or double-record the collection).
    prev = d.execute("SELECT result FROM delivery_attempts WHERE client_uuid=?", (eu,)).fetchone()
    if prev:
        return 'ok', ''
    try:
        oid = int(att.get('order_id') or 0)
    except (TypeError, ValueError):
        oid = 0
    o = d.execute("SELECT * FROM orders WHERE id=?", (oid,)).fetchone() if oid else None
    if not o or o['assigned_to'] != user['id'] or o['status'] != 'new':
        return 'rejected', OFFDEL_REJECT_STATE
    failed = bool(att.get('failed'))
    fail_reason = ''
    if failed:
        fail_reason = att.get('fail_reason')
        fail_reason = fail_reason.strip() if isinstance(fail_reason, str) else ''
        if not fail_reason:
            return 'rejected', OFFDEL_REJECT_FAIL_REASON
    items = [dict(r) for r in d.execute(
        "SELECT oi.*, p.sku, p.name FROM order_items oi JOIN products p ON p.id=oi.product_id "
        "WHERE oi.order_id=? ORDER BY oi.id", (oid,)).fetchall()]
    by_id = {it['id']: it for it in items}
    raw_lines = att.get('lines') or []
    if not isinstance(raw_lines, list):
        raw_lines = []
    agg = {}
    for ln in raw_lines:
        if not isinstance(ln, dict):
            return 'rejected', OFFDEL_REJECT_LINE
        iid = _offdel_int(ln.get('item_id'))
        if iid is None or iid not in by_id:
            return 'rejected', OFFDEL_REJECT_LINE
        try:
            q = float(ln.get('qty'))
        except (TypeError, ValueError):
            return 'rejected', OFFDEL_REJECT_QTY
        agg[iid] = agg.get(iid, 0.0) + q
    for iid, q in agg.items():
        if q < 0 or q > by_id[iid]['qty'] + 1e-9:
            return 'rejected', OFFDEL_REJECT_QTY
    lat, lng, acc = _flt(att.get('lat')), _flt(att.get('lng')), _flt(att.get('acc'))
    if failed:
        # Failed / shop-closed: no lines, no collection, no denom — the
        # whole invoice is cancelled with the reason, as online.
        d.execute("INSERT INTO delivery_attempts(order_id,by_user,result,reason,note,lat,lng,acc,created_at,client_uuid) VALUES(?,?,?,?,?,?,?,?,?,?)",
                  (oid, user['id'], 'failed', fail_reason, '', lat, lng, acc, now(), eu))
        d.execute("UPDATE orders SET status='cancelled', cancel_reason=? WHERE id=?", (fail_reason, oid))
        _notify_edit(d, oid, user, tr('ပို့သူ က မပို့ဖြစ် မှတ်တယ် — ဘောက်ချာ ပယ်ဖျက်ပြီး') + ' · ' + fail_reason)
        return 'ok', ''
    plan = [(it, agg.get(it['id'], 0.0)) for it in items]
    if sum(dq for _it, dq in plan) <= 0:
        return 'rejected', OFFDEL_ALL_ZERO
    _osub, new_sub, new_disc = _delivery_figures(items, plan, o['discount'])
    payable = new_sub - new_disc
    craw = att.get('collected')
    if craw is None or craw == '':
        collected = payable
    else:
        try:
            collected = float(craw)
        except (TypeError, ValueError):
            return 'rejected', OFFDEL_REJECT_MONEY
    if collected < 0:
        return 'rejected', OFFDEL_REJECT_MONEY
    if collected > payable + 0.5:
        return 'rejected', OFFDEL_OVER_COLLECT
    method = att.get('method')
    method = method.strip() if isinstance(method, str) else 'cash'
    if method not in PAY_METHOD_KEYS:
        method = 'cash'
    # Denomination split: evidence only, Cash only, as online (v60).
    denom_json = None
    if method == 'cash' and isinstance(att.get('denom'), dict):
        denom_json = json.dumps(att.get('denom'), ensure_ascii=False)
    partial = _apply_delivery(d, o, plan, new_disc)
    cur = d.execute("INSERT INTO delivery_attempts(order_id,by_user,result,reason,note,lat,lng,acc,created_at,denom,client_uuid) VALUES(?,?,?,?,?,?,?,?,?,?,?)",
                    (oid, user['id'], 'delivered', '', '', lat, lng, acc, now(), denom_json, eu))
    _gps_check(d, o, cur.lastrowid, lat, lng, acc, user)
    if collected > 0:
        # verified=0 always here: an offline field collection is pending
        # until Finance/Owner handover verification, exactly like an
        # online delivery person's collection.
        d.execute("INSERT INTO order_payments(order_id, amount, rdate, note, by_user, created_at, method, verified) VALUES(?,?,?,?,?,?,?,?)",
                  (oid, collected, today(), '', user['id'], now(), method, 0))
    sync_paid(oid, d)
    if partial:
        _notify_edit(d, oid, user, tr('ပို့သူ က ပို့ပြီး မှတ်တယ် (တစ်ပိုင်း — ကုန်မရ ပယ် ပါတယ်)'))
    return 'ok', ''

@app.route('/api/my-deliveries')
@require_perm('delivery.offline')
def api_my_deliveries():
    """The caller's OWN assigned invoices with status 'new' only, for
    the Delivery phone's offline list (cached on the phone at every
    successful load). Returns a bare JSON ARRAY (not wrapped), newest
    first; the phone groups by area. NO prices or costs per line — the
    delivery person sees the payable total only, never cost data."""
    d = db()
    uid = g.user['id']
    rows = d.execute("SELECT * FROM orders WHERE assigned_to=? AND status='new' ORDER BY id DESC",
                     (uid,)).fetchall()
    totals = order_totals_map(d, [o['id'] for o in rows])
    out = []
    for o in rows:
        items = [{'item_id': it['id'], 'sku': it['sku'], 'name': it['name'], 'qty': it['qty']}
                 for it in d.execute("SELECT oi.id, oi.qty, p.sku, p.name FROM order_items oi "
                                      "JOIN products p ON p.id=oi.product_id WHERE oi.order_id=? ORDER BY oi.id",
                                      (o['id'],)).fetchall()]
        out.append({'id': o['id'], 'code': o['code'], 'outlet': o['outlet'], 'area': o['area'] or '',
                    'odate': o['odate'], 'total': totals.get(o['id'], (0, 0))[0], 'items': items})
    return jsonify(out)

@app.route('/api/delivery-attempts', methods=['POST'])
@require_perm('delivery.offline')
def api_delivery_attempts():
    """Apply queued offline delivery attempts, in order. Each attempt is
    validated independently against the standing rules at apply time
    and committed per attempt, so a batch can succeed partially; results
    are returned per attempt so the phone can flag rejections (never
    silent). A re-push of an applied client_attempt_uuid returns ok
    without re-applying."""
    d = db()
    payload = request.get_json(force=True, silent=True) or {}
    attempts = payload.get('attempts') if isinstance(payload, dict) else None
    if not isinstance(attempts, list):
        return jsonify({'ok': False, 'error': tr('ပို့မှတ်တမ်း စာရင်း မပါဘူး')}), 400
    results = []
    for att in attempts:
        if not isinstance(att, dict):
            results.append({'client_attempt_uuid': '', 'status': 'rejected',
                            'reason': tr(OFFDEL_REJECT_LINE)})
            continue
        eu = att.get('client_attempt_uuid')
        eu = eu.strip() if isinstance(eu, str) else ''
        try:
            status, reason = _apply_offline_delivery_attempt(d, g.user, att)
        except sqlite3.IntegrityError:
            # Concurrent double-push: the UNIQUE index kept one copy.
            # Re-read — if the uuid landed, the push succeeded.
            d.rollback()
            landed = d.execute("SELECT 1 FROM delivery_attempts WHERE client_uuid=?", (eu,)).fetchone() if eu else None
            status, reason = ('ok', '') if landed else ('rejected', OFFDEL_REJECT_RETRY)
        except Exception:
            d.rollback()
            status, reason = 'rejected', OFFDEL_REJECT_RETRY
        if status == 'ok':
            d.commit()
        else:
            d.rollback()
        results.append({'client_attempt_uuid': eu, 'status': status,
                        'reason': tr(reason) if reason else ''})
    return jsonify({'ok': True, 'results': results})

@app.route('/delivery-offline')
@require_perm('delivery.offline')
def delivery_offline_page():
    """The offline delivery page itself (templates/delivery_offline.html),
    delivery role only via the delivery.offline permission."""
    return render_template('delivery_offline.html')

@app.route('/attempts/<int:aid>/gps-check', methods=['POST'])
@require_perm('gps.check_mark')
def gps_check_mark(aid):
    """The one judgment-type notice: a person confirms they looked at the
    sold-vs-delivered place gap. Records who and when; the notice clears."""
    d = db()
    a = d.execute("SELECT * FROM delivery_attempts WHERE id=?", (aid,)).fetchone()
    if not a: abort(404)
    d.execute("UPDATE delivery_attempts SET checked_by=?, checked_at=? WHERE id=?",
              (g.user['id'], now(), aid))
    d.commit(); flash(tr('မှတ်ပြီးပြီ'))
    # v53: the same check-off from the gathered /gps-check list returns to
    # the list; everywhere else it returns to the invoice, as before. The
    # stamping above is the one and only check-off path.
    if (request.form.get('back') or '') == 'gps-check':
        return redirect('/gps-check')
    return redirect(url_for('order_detail', oid=a['order_id']))

def _gps_row(d, a):
    """One flagged attempt, display-ready: the two recorded places (the
    sale fix for the order, this attempt's delivery fix) + plain distance."""
    o = d.execute("SELECT * FROM orders WHERE id=?", (a['order_id'],)).fetchone()
    sp = sale_point(d, o) if o else None
    dp = (a['lat'], a['lng']) if (a['lat'] is not None and a['lng'] is not None) else None
    return {'a': a, 'sale': sp, 'del': dp, 'dist': fmt_dist(a['gps_dist'])}

@app.route('/gps-check')
@require_perm('gps_check.view')
def gps_check_page():
    """v53 (Ko, 2026-10-21): every flagged delivery in one list — until
    now a v50 place flag was visible only on its own invoice and in the
    bell feed, so a busy day could bury one. Open flags first; below, the
    record of who checked which flag and when."""
    d = db()
    df = (request.args.get('from') or '').strip()
    dt = (request.args.get('to') or '').strip()
    if not re.match(r'^\d{4}-\d{2}-\d{2}$', df or ''): df = ''
    if not re.match(r'^\d{4}-\d{2}-\d{2}$', dt or ''): dt = ''
    try:
        du = int(request.args.get('deliverer') or 0)
    except ValueError:
        du = 0
    deliverers = delivery_users(d)
    if du and du not in {u['id'] for u in deliverers}: du = 0
    sql = """SELECT a.*, o.code, o.outlet, u.name byname FROM delivery_attempts a
             JOIN orders o ON o.id=a.order_id LEFT JOIN users u ON u.id=a.by_user
             WHERE COALESCE(a.gps_flag,0)=1 AND a.checked_by IS NULL"""
    args = []
    if du: sql += " AND a.by_user=?"; args.append(du)
    if df: sql += " AND substr(a.created_at,1,10)>=?"; args.append(df)
    if dt: sql += " AND substr(a.created_at,1,10)<=?"; args.append(dt)
    sql += " ORDER BY a.id DESC"
    open_rows = [_gps_row(d, a) for a in d.execute(sql, args).fetchall()]
    done = d.execute("""SELECT a.*, o.code, o.outlet, u.name byname, cu.name checker
                        FROM delivery_attempts a JOIN orders o ON o.id=a.order_id
                        LEFT JOIN users u ON u.id=a.by_user LEFT JOIN users cu ON cu.id=a.checked_by
                        WHERE COALESCE(a.gps_flag,0)=1 AND a.checked_by IS NOT NULL
                        ORDER BY a.checked_at DESC, a.id DESC LIMIT 51""").fetchall()
    done_trunc = len(done) > 50
    done_rows = [_gps_row(d, a) for a in done[:50]]
    return render_template('gps_check.html', open_rows=open_rows, done_rows=done_rows,
                           done_trunc=done_trunc, deliverers=deliverers, du=du, df=df, dt=dt)

@app.route('/handover')
@require_perm('handover.view')
def handover_page():
    d = db()
    day = (request.args.get('day') or '').strip()
    if not re.match(r'^\d{4}-\d{2}-\d{2}$', day or ''): day = today()
    cand = {r['id']: r for r in d.execute("SELECT id, name, role, active FROM users WHERE role='delivery'").fetchall()}
    for r in d.execute("SELECT DISTINCT by_user FROM order_payments WHERE by_user IS NOT NULL AND COALESCE(verified,1)=0").fetchall():
        u = d.execute("SELECT id, name, role, active FROM users WHERE id=?", (r['by_user'],)).fetchone()
        if u: cand[u['id']] = u
    rows = []
    for uid, u in sorted(cand.items(), key=lambda kv: kv[1]['name']):
        expected = 0.0
        for r in d.execute("SELECT DISTINCT order_id FROM delivery_attempts WHERE by_user=? AND result='delivered' AND substr(created_at,1,10)=?", (uid, day)).fetchall():
            t, _ = order_total(r['order_id']); expected += t
        recorded = d.execute("SELECT COALESCE(SUM(amount),0) v FROM order_payments WHERE by_user=? AND rdate=?", (uid, day)).fetchone()['v']
        verf = d.execute("SELECT COALESCE(SUM(amount),0) v FROM order_payments WHERE by_user=? AND rdate=? AND COALESCE(verified,1)=1", (uid, day)).fetchone()['v']
        pend = [dict(p) for p in d.execute("SELECT p.*, o.code FROM order_payments p JOIN orders o ON o.id=p.order_id WHERE p.by_user=? AND COALESCE(p.verified,1)=0 ORDER BY p.id", (uid,)).fetchall()]
        for p in pend:
            # v60: the collector's note breakdown for this collection lives
            # on the delivered attempt — Finance counts the physical notes
            # against it right here. Display only: the difference (if any)
            # is a plain note; verification below works exactly as before.
            att = d.execute("SELECT denom FROM delivery_attempts WHERE order_id=? AND by_user=? AND result='delivered' ORDER BY id DESC LIMIT 1",
                            (p['order_id'], uid)).fetchone()
            p['denom_fmt'] = fmt_denom(att['denom']) if att else ''
            _dt = denom_total(att['denom']) if att else None
            p['denom_diff'] = (_dt - p['amount']) if _dt is not None else None
        pend_v = sum(p['amount'] for p in pend)
        if expected or recorded or pend_v:
            rows.append({'u': u, 'expected': expected, 'recorded': recorded, 'verified': verf,
                         'pend': pend, 'pend_v': pend_v})
    return render_template('handover.html', rows=rows, day=day, fmt_money=fmt_money)

@app.route('/handover/verify', methods=['POST'])
@require_perm('handover.verify')
def handover_verify():
    d = db(); f = request.form
    uid = f.get('deliverer_id', type=int)
    day = (f.get('day') or '').strip()
    if not re.match(r'^\d{4}-\d{2}-\d{2}$', day or ''): day = today()
    try:
        handed = float((f.get('handed') or '0').replace(',', ''))
    except ValueError:
        handed = 0
    if not uid or handed <= 0:
        flash(tr('လွှဲအပ်ငွေ ထည့်ပါ'))
        return redirect(url_for('handover_page', day=day))
    remaining = handed; touched = set()
    for p in d.execute("SELECT * FROM order_payments WHERE by_user=? AND COALESCE(verified,1)=0 ORDER BY id", (uid,)).fetchall():
        if p['amount'] <= remaining + 0.5:
            d.execute("UPDATE order_payments SET verified=1 WHERE id=?", (p['id'],))
            remaining -= p['amount']; touched.add(p['order_id'])
        else:
            break
    for oid in touched:
        sync_paid(oid, d)
    d.execute("INSERT INTO delivery_handovers(deliverer_id, hdate, handed, verified_by, note, created_at) VALUES(?,?,?,?,?,?)",
              (uid, day, handed, g.user['id'], (f.get('note') or '').strip(), now()))
    d.commit(); flash(tr('ငွေ လွှဲအပ် အတည်ပြုပြီးပြီ'))
    return redirect(url_for('handover_page', day=day))


# ---------------- Returns / Damage ----------------
@app.route('/returns', methods=['GET', 'POST'])
@login_required()
def returns():
    if not has_perm(g.user, 'returns.view'): abort(403)  # v75: sales POST stays unreachable — GET blocks sales before the POST branch, exactly as before
    d = db()
    if request.method == 'POST':
        if not has_perm(g.user, 'returns.record'):
            abort(403)
        f = request.form
        pid = int(f.get('product_id') or 0); qty = float(f.get('qty') or 0); kind = f.get('kind')
        if not pid or qty <= 0 or kind not in ('good', 'damaged'):
            flash(tr('အချက်အလက် မပြည့်စုံဘူး'))
            return redirect(url_for('returns'))
        oid = int(f['order_id']) if f.get('order_id') else None
        # v49 (Ko, 2026-10-20): a good return can be settled as an EXCHANGE —
        # the shop hands the goods back and takes the SAME product out again,
        # once per shop. The exchange needs to know which shop it was.
        settle = (f.get('settle') or '').strip()
        ex_outlet_id, ex_outlet_name = None, ''
        if kind == 'good' and settle == 'exchange':
            if oid:
                orow = d.execute("SELECT outlet, outlet_id FROM orders WHERE id=?", (oid,)).fetchone()
                if orow:
                    ex_outlet_name = orow['outlet'] or ''; ex_outlet_id = orow['outlet_id']
            if not ex_outlet_name:
                ex_outlet_name = (f.get('shop') or '').strip()
            if not ex_outlet_name:
                flash(tr('ကုန် လဲ ဖို့ ဆိုင် နာမည် လိုပါတယ် — Order ရွေးပါ (သို့) ဆိုင် နာမည် ရိုက်ပါ'))
                return redirect(url_for('returns'))
            if not ex_outlet_id:
                # v67: a typed shop name that is 2+ shops cannot be
                # guessed — the once-only exchange mark would land on
                # the wrong shop. Ask for the order (which carries the
                # shop's id) instead; nothing is recorded.
                # v68: the count is over name AND alias hits — a text
                # that is one shop's name and another's alias is the
                # same collision. One alias hit alone resolves fine.
                if len(_outlet_text_hits(d, ex_outlet_name)) >= 2:
                    flash(tr('ဆိုင် နာမည် တူတာ နှစ်ဆိုင် ရှိတယ် — ဘယ် ဆိုင် လဲ မသဲကွဲဘူး။ Order ကို ရွေးပြီး ပြန် လုပ်ပါ'))
                    return redirect(url_for('returns'))
                ex_outlet_id = resolve_outlet(d, ex_outlet_name, '', g.user['id'], today())
        d.execute("INSERT INTO returns(order_id,product_id,qty,kind,reason,amount,by_user,created_at) VALUES(?,?,?,?,?,?,?,?)",
                  (oid, pid, qty, kind, f.get('reason', '').strip(), float((f.get('amount') or '0').replace(',', '')), g.user['id'], now()))
        rid = d.execute("SELECT last_insert_rowid() i").fetchone()['i']
        if kind == 'good':
            move_stock(pid, qty, 'return_good', f'RET-{rid}', '', g.user['id'])
        else:
            move_stock(pid, 0, 'damage_note', f'RET-{rid}', f.get('reason', ''), g.user['id'])  # damaged goods are already out; loss tracked by value
        if kind == 'good' and settle == 'exchange':
            # Same product, same quantity straight back out to the shop —
            # the two stock moves cancel out; the lasting record is the
            # once-only exchange mark on the shop.
            move_stock(pid, -qty, 'exchange_out', f'RET-{rid}', tr('ကုန် လဲ — ဆိုင် ကို အသစ် ပြန် ပေး'), g.user['id'])
            d.execute("INSERT INTO outlet_exchanges(outlet_id,outlet_name,product_id,qty,edate,return_id,by_user,created_at) VALUES(?,?,?,?,?,?,?,?)",
                      (ex_outlet_id, ex_outlet_name, pid, qty, today(), rid, g.user['id'], now()))
        d.commit(); flash(tr('မှတ်ပြီးပြီ — Finance စစ်ရန် ကျန်တယ်'))
        return redirect(url_for('returns'))
    rows = d.execute(
        "SELECT r.*, p.sku, p.name, p.unit, u.name byname, o.code ocode FROM returns r JOIN products p ON p.id=r.product_id "
        "LEFT JOIN users u ON u.id=r.by_user LEFT JOIN orders o ON o.id=r.order_id ORDER BY r.id DESC LIMIT 200").fetchall()
    prods = d.execute("SELECT id, sku, name, unit FROM products WHERE active=1 ORDER BY sku").fetchall()
    ords = []
    if has_perm(g.user, 'products.view'):
        ords = d.execute("SELECT id, code, outlet FROM orders ORDER BY id DESC LIMIT 100").fetchall()
    elif has_perm(g.user, 'dashboard.sales_own'):  # dead for sales (GET blocks them above); kept, not widened
        ords = d.execute("SELECT id, code, outlet FROM orders WHERE sales_user=? ORDER BY id DESC LIMIT 100", (g.user['id'],)).fetchall()
    ex_return_ids = {r['return_id'] for r in d.execute("SELECT return_id FROM outlet_exchanges WHERE return_id IS NOT NULL").fetchall()}
    used_shops = d.execute("SELECT x.*, p.sku, p.name pname FROM outlet_exchanges x JOIN products p ON p.id=x.product_id ORDER BY x.id").fetchall()
    return render_template('returns.html', rows=rows, prods=prods, ords=ords, fmt_money=fmt_money,
                           ex_return_ids=ex_return_ids, used_shops=used_shops)

@app.route('/returns/<int:rid>/check', methods=['POST'])
@require_perm('returns.check')
def return_check(rid):
    d = db()
    d.execute("UPDATE returns SET status='checked', checked_by=? WHERE id=? AND status='recorded'", (g.user['id'], rid))
    d.commit()
    return redirect(url_for('returns'))


# ---------------- Export: Stock + prices sheet (offline office copy) ----------------
def xlsx_bytes(headers, rows):
    def colname(i):
        out = ''; i += 1
        while i:
            i, r = divmod(i - 1, 26); out = chr(65 + r) + out
        return out
    def cell(ref, val):
        if isinstance(val, (int, float)) and not isinstance(val, bool):
            return f'<c r="{ref}"><v>{val}</v></c>'
        txt = xml_escape('' if val is None else str(val))
        return f'<c r="{ref}" t="inlineStr"><is><t>{txt}</t></is></c>'
    body = []
    for ri, row in enumerate([headers] + rows, 1):
        body.append(f'<row r="{ri}">' + ''.join(cell(f"{colname(ci)}{ri}", v) for ci, v in enumerate(row)) + '</row>')
    sheet = ('<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
             '<worksheet xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main">'
             '<sheetData>' + ''.join(body) + '</sheetData></worksheet>')
    ct = ('<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
          '<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">'
          '<Default Extension="rels" ContentType="application/vnd.openxmlformats-package.relationships+xml"/>'
          '<Default Extension="xml" ContentType="application/xml"/>'
          '<Override PartName="/xl/workbook.xml" ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet.main+xml"/>'
          '<Override PartName="/xl/worksheets/sheet1.xml" ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.worksheet+xml"/></Types>')
    rels = ('<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
            '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">'
            '<Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/officeDocument" Target="xl/workbook.xml"/></Relationships>')
    wb = ('<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
          '<workbook xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main" '
          'xmlns:r="http://schemas.openxmlformats.org/officeDocument/2006/relationships">'
          '<sheets><sheet name="Stock" sheetId="1" r:id="rId1"/></sheets></workbook>')
    wbrels = ('<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
              '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">'
              '<Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/worksheet" Target="worksheets/sheet1.xml"/></Relationships>')
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, 'w', zipfile.ZIP_DEFLATED) as z:
        z.writestr('[Content_Types].xml', ct)
        z.writestr('_rels/.rels', rels)
        z.writestr('xl/workbook.xml', wb)
        z.writestr('xl/_rels/workbook.xml.rels', wbrels)
        z.writestr('xl/worksheets/sheet1.xml', sheet)
    return buf.getvalue()

@app.route('/export/stock.xlsx')
@require_perm('export.stock_xlsx')
def export_stock():
    d = db()
    rows = d.execute(
        "SELECT p.id, p.sku, p.name, p.category, p.unit, p.supplier, COALESCE(s.qty,0) qty, p.retail, p.wholesale, p.pool_id, p.pack_factor "
        "FROM products p LEFT JOIN stock s ON s.product_id=p.id WHERE p.active=1 ORDER BY p.category, p.name").fetchall()
    smap = {r['product_id']: r['qty'] for r in d.execute("SELECT product_id, qty FROM stock").fetchall()}
    def _view(r):
        if r['pool_id'] and r['pool_id'] != r['id']:
            f = r['pack_factor'] or 1
            pool = smap.get(r['pool_id'], 0)
            return int(pool / f) if f != 1 else pool
        return r['qty']
    headers = ['SKU', tr('ပစ္စည်း'), tr('အမျိုးအစား'), tr('ယူနစ်'), 'Supplier', 'Stock', tr('လက်လီ (Retail)'), tr('လက်ကား (Wholesale)')]
    data = [[r['sku'], r['name'], r['category'] or '', r['unit'] or '', r['supplier'] or '',
             _view(r), r['retail'], r['wholesale']] for r in rows]
    fn = 'GTM_stock_prices_' + datetime.date.today().isoformat() + '.xlsx'
    return Response(xlsx_bytes(headers, data),
                    mimetype='application/vnd.openxmlformats-officedocument.spreadsheetml.sheet',
                    headers={'Content-Disposition': f'attachment; filename="{fn}"'})


# ---------------- v79: Daily snapshot file ----------------
# Page: GET /daily-file (date picker, default = today Myanmar time, any
# past date allowed). Download: GET /daily-file?download=1&date=YYYY-MM-DD
# returns GTM-YYYY-MM-DD.xlsx. Bad / future dates never traceback: the
# page re-renders with a plain error. The builder + writer land
# separately (build_daily_snapshot / xlsx_multi_bytes); this route only
# calls them, exactly per the v79 contract. Section page, so the back
# pill is the base.html backbar's fixed '← ပင်မ' to '/' — no hand-written
# back link in the template (v73 lesson).
@app.route('/daily-file')
@require_perm('snapshot.daily')
def daily_file():
    raw = (request.args.get('date') or '').strip()
    date_str = raw or today()
    error = None
    dval = None
    if not re.fullmatch(r'\d{4}-\d{2}-\d{2}', date_str):
        error = tr('ရက် ပုံစံ မှားနေတယ် — YYYY-MM-DD နဲ့ ရေးပါ')
    else:
        try:
            dval = datetime.datetime.strptime(date_str, '%Y-%m-%d').date()
            if dval.strftime('%Y-%m-%d') != date_str:
                raise ValueError('format')
        except ValueError:
            error = tr('ရက် ပုံစံ မှားနေတယ် — YYYY-MM-DD နဲ့ ရေးပါ')
    if error is None and dval > datetime.datetime.now(MMT).date():
        error = tr('အနာဂတ် ရက် ကို ရွေး လို့ မရဘူး')
    if request.args.get('download') == '1' and error is None:
        sheets, meta = build_daily_snapshot(db(), date_str, g.user['username'], user_role_name(g.user))
        fn = 'GTM-' + date_str + '.xlsx'
        return Response(xlsx_multi_bytes(sheets),
                        mimetype='application/vnd.openxmlformats-officedocument.spreadsheetml.sheet',
                        headers={'Content-Disposition': f'attachment; filename="{fn}"'})
    return render_template('daily_file.html', date_str=date_str, error=error)


# ---------------- v79: Daily snapshot file — writer + builder ----------------
# Read-only: every query below is a SELECT (the cashbox helpers it reuses
# are read-time computations too). Invoice lines show the invoiced
# selling price only, receiving shows quantities only, and no internal
# pricing figure of any kind is selected or written anywhere here —
# every SELECT names its columns explicitly so nothing extra can leak
# through a star-select. Headers are plain Burmese literals (never
# passed through tr()), so the file's sheet names and columns stay
# Burmese whatever UI language the downloader uses.
def xlsx_multi_bytes(sheets):
    """Multi-sheet XLSX bytes from [(sheet_name, headers, rows), ...].
    Same vendored stdlib writer as xlsx_bytes (zip + xml, inline
    strings, no new dependency): headers are written as the first row
    of each sheet, then rows. The snapshot builder uses that contract
    for its current-state sheets by passing the generation-time label
    as the headers row and the column names as the first data row, so
    row 1 of those sheets in the file really is the label row."""
    def colname(i):
        out = ''; i += 1
        while i:
            i, r = divmod(i - 1, 26); out = chr(65 + r) + out
        return out
    def cell(ref, val):
        if isinstance(val, (int, float)) and not isinstance(val, bool):
            return f'<c r="{ref}"><v>{val}</v></c>'
        txt = xml_escape('' if val is None else str(val))
        return f'<c r="{ref}" t="inlineStr"><is><t>{txt}</t></is></c>'
    def sheet_xml(headers, rows):
        body = []
        for ri, row in enumerate([headers] + rows, 1):
            body.append(f'<row r="{ri}">' + ''.join(cell(f"{colname(ci)}{ri}", v) for ci, v in enumerate(row)) + '</row>')
        return ('<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
                '<worksheet xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main">'
                '<sheetData>' + ''.join(body) + '</sheetData></worksheet>')
    if not sheets:
        sheets = [('Sheet1', [], [])]
    parts, ct_over, wb_sheets, wb_rels = [], [], [], []
    for idx, (nm, headers, rows) in enumerate(sheets, 1):
        parts.append((f'xl/worksheets/sheet{idx}.xml', sheet_xml(headers, rows)))
        ct_over.append(f'<Override PartName="/xl/worksheets/sheet{idx}.xml" ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.worksheet+xml"/>')
        esc = xml_escape(str(nm), {'"': '&quot;'})
        wb_sheets.append(f'<sheet name="{esc}" sheetId="{idx}" r:id="rId{idx}"/>')
        wb_rels.append(f'<Relationship Id="rId{idx}" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/worksheet" Target="worksheets/sheet{idx}.xml"/>')
    ct = ('<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
          '<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">'
          '<Default Extension="rels" ContentType="application/vnd.openxmlformats-package.relationships+xml"/>'
          '<Default Extension="xml" ContentType="application/xml"/>'
          '<Override PartName="/xl/workbook.xml" ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet.main+xml"/>'
          + ''.join(ct_over) + '</Types>')
    rels = ('<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
            '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">'
            '<Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/officeDocument" Target="xl/workbook.xml"/></Relationships>')
    wb = ('<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
          '<workbook xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main" '
          'xmlns:r="http://schemas.openxmlformats.org/officeDocument/2006/relationships">'
          '<sheets>' + ''.join(wb_sheets) + '</sheets></workbook>')
    wbrels = ('<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
              '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">'
              + ''.join(wb_rels) + '</Relationships>')
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, 'w', zipfile.ZIP_DEFLATED) as z:
        z.writestr('[Content_Types].xml', ct)
        z.writestr('_rels/.rels', rels)
        z.writestr('xl/workbook.xml', wb)
        z.writestr('xl/_rels/workbook.xml.rels', wbrels)
        for path, xml in parts:
            z.writestr(path, xml)
    return buf.getvalue()


def build_daily_snapshot(d, date_str, username, role):
    """Build the daily snapshot file's sheets for one Myanmar date.

    Returns (sheets, meta) where sheets is an ordered list of
    (burmese_sheet_name, headers, rows) with plain values, and meta is
    {'date': date_str, 'generated_at': <Myanmar-time now()>,
    'generated_by': username}. Activity sheets scope strictly by the
    selected date; current-state sheets carry a generation-time label
    as their first row (headers slot) with column names as the first
    data row, and are never presented as as-of-date figures.
    Role filtering lives here: owner + finance get cover + all 9
    content sheets; warehouse gets cover + sheets 1, 2, 4, 5, 6, 9
    only; any other role gets the cover alone (the route 403s anyway).
    Sheet order: cover, ဘောက်ချာ, ပို့ပြီး, ငွေ, လက်ခံ, စတော့,
    ဝယ်ရန် ORQ, ကုန်ကျစရိတ်, Cashbox, လက်ကျန် — with the money,
    expense and Cashbox sheets dropped for warehouse."""
    gen = now()
    meta = {'date': date_str, 'generated_at': gen, 'generated_by': username}
    label = 'လက်ရှိ အခြေအနေ — ' + gen
    names = {r['id']: r['name'] for r in d.execute("SELECT id, name FROM users").fetchall()}

    def uname(uid):
        return names.get(uid, '') if uid else ''

    def money(v):
        return round(v or 0, 2)

    # ---- 1. ဘောက်ချာ: one row per order line, all statuses, odate = date.
    # Invoice total mirrors order_total(): lines' qty*price minus the
    # order discount (current qty, i.e. delivered qty after a
    # shortfall rewrite). Balance = total minus VERIFIED received, via
    # order_received_map() — pending handover collections do not shrink
    # the balance, exactly as the app itself counts received money.
    ords = d.execute("SELECT o.id, o.code, o.outlet, o.area, o.sales_user, o.team, "
                     "o.odate, o.status, o.discount FROM orders o "
                     "WHERE o.odate=? ORDER BY o.id", (date_str,)).fetchall()
    oids = [o['id'] for o in ords]
    rec = order_received_map(d, oids) if oids else {}
    lines_by_order = {}
    if oids:
        ph = ','.join('?' * len(oids))
        for it in d.execute("SELECT oi.order_id, oi.qty, oi.price, oi.discount, "
                            "p.sku, p.name FROM order_items oi "
                            "JOIN products p ON p.id=oi.product_id "
                            f"WHERE oi.order_id IN ({ph}) ORDER BY oi.id", oids).fetchall():
            lines_by_order.setdefault(it['order_id'], []).append(it)
    rows_orders = []
    hdr_orders = ['ဘောက်ချာ', 'ရက်စွဲ', 'ဆိုင်', 'နယ်မြေ', 'ရောင်းသူ', 'အဖွဲ့',
                  'ပစ္စည်း', 'SKU', 'အရေအတွက်', 'ဈေးနှုန်း', 'လျှော့ဈေး',
                  'စုစုပေါင်း', 'ဘောက်ချာ စုစုပေါင်း', 'အခြေအနေ', 'ပေးပြီး', 'လက်ကျန်']
    for o in ords:
        its = lines_by_order.get(o['id'], [])
        total = money(sum((it['qty'] or 0) * (it['price'] or 0) for it in its) - (o['discount'] or 0))
        received = money(rec.get(o['id'], 0))
        bal = money(total - received)
        base = [o['code'], o['odate'], o['outlet'], o['area'] or '',
                uname(o['sales_user']), o['team'] or '']
        if not its:
            rows_orders.append(base + ['', '', '', '', '', '', total, o['status'], received, bal])
        for it in its:
            lt = money((it['qty'] or 0) * (it['price'] or 0) - (it['discount'] or 0))
            rows_orders.append(base + [it['name'], it['sku'], it['qty'], it['price'],
                                       it['discount'] or 0, lt, total, o['status'],
                                       received, bal])

    # ---- 2. ပို့ပြီး: delivery attempts created on the date.
    # Delivered attempts expand per order line: ordered = orig_qty
    # (kept by _apply_delivery on a shortfall rewrite) else current
    # qty; delivered = current qty; shortfall = cancelled_qty (falling
    # back to ordered − delivered for legacy rows). Failed attempts
    # are one row with the reason. GPS is presence only ('ရှိ'/'မရှိ')
    # — raw coordinates are never read into a cell.
    atts = d.execute("SELECT a.order_id, a.by_user, a.result, a.reason, a.note, "
                     "a.lat, a.lng, o.code ocode, o.outlet FROM delivery_attempts a "
                     "LEFT JOIN orders o ON o.id=a.order_id "
                     "WHERE substr(a.created_at,1,10)=? ORDER BY a.id", (date_str,)).fetchall()
    att_oids = sorted({a['order_id'] for a in atts if a['order_id']})
    att_lines = {}
    if att_oids:
        ph = ','.join('?' * len(att_oids))
        for it in d.execute("SELECT oi.order_id, oi.qty, oi.orig_qty, oi.cancelled_qty, "
                            "p.sku, p.name FROM order_items oi "
                            "JOIN products p ON p.id=oi.product_id "
                            f"WHERE oi.order_id IN ({ph}) ORDER BY oi.id", att_oids).fetchall():
            att_lines.setdefault(it['order_id'], []).append(it)
    rows_deliv = []
    hdr_deliv = ['ဘောက်ချာ', 'ဆိုင်', 'ပစ္စည်း', 'SKU', 'မှာထားသော အရေအတွက်',
                 'ပို့သော အရေအတွက်', 'ပို့မရ/ပယ်ဖျက် အရေအတွက်', 'ရလဒ်',
                 'ပျက်ကွက်ရသည့် အကြောင်း', 'ပို့သူ', 'GPS']
    for a in atts:
        gps = 'ရှိ' if (a['lat'] is not None or a['lng'] is not None) else 'မရှိ'
        reason = (a['reason'] or '') + ((' — ' + a['note']) if a['note'] else '')
        if a['result'] == 'delivered':
            its = att_lines.get(a['order_id'], [])
            if not its:
                rows_deliv.append([a['ocode'] or '', a['outlet'] or '', '', '', '', '',
                                   '', 'ပို့ပြီး', '', uname(a['by_user']), gps])
            for it in its:
                ordered = it['orig_qty'] if it['orig_qty'] is not None else it['qty']
                short = it['cancelled_qty'] or 0
                if not short and (ordered or 0) > (it['qty'] or 0):
                    short = (ordered or 0) - (it['qty'] or 0)
                rows_deliv.append([a['ocode'] or '', a['outlet'] or '', it['name'], it['sku'],
                                   ordered, it['qty'], short, 'ပို့ပြီး', '',
                                   uname(a['by_user']), gps])
        else:
            rows_deliv.append([a['ocode'] or '', a['outlet'] or '', '', '', '', '',
                               '', 'မပို့ဖြစ်' if a['result'] == 'failed' else (a['result'] or ''),
                               reason, uname(a['by_user']), gps])

    # ---- 3. ငွေ: payments dated the selected date.
    pays = d.execute("SELECT p.order_id, p.amount, p.method, p.verified, p.by_user, "
                     "o.code ocode, o.outlet FROM order_payments p "
                     "LEFT JOIN orders o ON o.id=p.order_id "
                     "WHERE p.rdate=? ORDER BY p.id", (date_str,)).fetchall()
    rows_money = [[p['ocode'] or '', p['outlet'] or '', p['amount'],
                   p['method'] or 'cash',
                   'အတည်ပြု' if (p['verified'] if p['verified'] is not None else 1) else 'handover စောင့်',
                   uname(p['by_user'])] for p in pays]
    hdr_money = ['ဘောက်ချာ', 'ဆိုင်', 'ငွေ', 'နည်းလမ်း', 'အခြေအနေ', 'မှတ်သူ']

    # ---- 4. လက်ခံ: purchases whose receive date is the selected date.
    # Receive date = purchases.pdate: both the receive flow and the
    # direct-purchase flow store the form's pdate (default today()) on
    # the PUR row at receive time, and the app's own purchase lists
    # filter on pdate — so pdate, not created_at, is the date the app
    # itself treats as the receive date. Quantities only, no amounts.
    purs = d.execute("SELECT pu.code, pu.supplier, pu.invoice_no, pu.status, "
                     "pi.qty, p.sku, p.name FROM purchases pu "
                     "LEFT JOIN purchase_items pi ON pi.purchase_id=pu.id "
                     "LEFT JOIN products p ON p.id=pi.product_id "
                     "WHERE pu.pdate=? ORDER BY pu.id, pi.id", (date_str,)).fetchall()
    rows_recv = [[pu['code'], pu['supplier'] or '', pu['name'] or '', pu['sku'] or '',
                  pu['qty'] if pu['qty'] is not None else '', pu['invoice_no'] or '',
                  pu['status'] or ''] for pu in purs]
    hdr_recv = ['အဝယ် (PUR)', 'Supplier', 'ပစ္စည်း', 'SKU', 'လက်ခံအရေအတွက်',
                'Supplier ဘောက်ချာနံပါတ်', 'အခြေအနေ']

    # ---- 5. စတော့: CURRENT pool stock at generation time (label row
    # first). View logic mirrors /export/stock.xlsx: a linked big SKU
    # shows FULL packs only (floor of pool / factor); the pool balance
    # lives on the base product's stock row. Sellable flag is
    # products.sellable (migration-added, default 1; NULL counts as
    # sellable, as the app's COALESCE(sellable,1) checks do).
    smap = {r['product_id']: r['qty'] for r in d.execute("SELECT product_id, qty FROM stock").fetchall()}
    prows = d.execute("SELECT p.id, p.sku, p.name, p.unit, p.upc, p.pool_id, p.pack_factor, "
                      "COALESCE(p.sellable,1) sell FROM products p "
                      "WHERE p.active=1 ORDER BY p.category, p.name").fetchall()

    def _view(r):
        if r['pool_id'] and r['pool_id'] != r['id']:
            f = r['pack_factor'] or 1
            pool = smap.get(r['pool_id'], 0)
            return int(pool / f) if f != 1 else pool
        return smap.get(r['id'], 0)

    hdr_stock = ['ပစ္စည်း', 'SKU', 'ယူနစ်', 'ထုပ် (အရေအတွက်)', 'စတော့', 'ရောင်းနိုင်/မရောင်း']
    data_stock = [[r['name'], r['sku'], r['unit'] or '', r['upc'] if r['upc'] is not None else '',
                   _view(r), 'ရောင်း' if r['sell'] else 'မရောင်း'] for r in prows]

    # ---- 6. ဝယ်ရန်/ORQ: open/partial requests AT GENERATION TIME.
    # Outstanding per line = qty − received_qty; only outstanding
    # lines appear (an open/partial request with no outstanding line
    # still gets one blank-item row so the request itself is visible).
    # NOTE: the sheet name uses a space, not '/', because '/' is
    # illegal in an Excel sheet name (Excel would force a repair).
    ors = d.execute("SELECT r.id, r.code, r.supplier, r.kind FROM order_requests r "
                    "WHERE r.status IN ('open','partial') ORDER BY r.id").fetchall()
    or_lines = {}
    if ors:
        ph = ','.join('?' * len(ors))
        for it in d.execute("SELECT ri.or_id, ri.qty, ri.received_qty, p.sku, p.name "
                            "FROM order_request_items ri "
                            "JOIN products p ON p.id=ri.product_id "
                            f"WHERE ri.or_id IN ({ph}) ORDER BY ri.id",
                            [r['id'] for r in ors]).fetchall():
            or_lines.setdefault(it['or_id'], []).append(it)
    rows_orq = []
    hdr_orq = ['ORQ', 'Supplier', 'အမျိုး', 'ပစ္စည်း', 'SKU', 'ကျန်အရေအတွက်']
    for r in ors:
        outstanding = [(it, (it['qty'] or 0) - (it['received_qty'] or 0))
                       for it in or_lines.get(r['id'], [])]
        outstanding = [(it, q) for it, q in outstanding if q > 1e-9]
        if not outstanding:
            rows_orq.append([r['code'], r['supplier'] or '', r['kind'] or '', '', '', 0])
        for it, q in outstanding:
            rows_orq.append([r['code'], r['supplier'] or '', r['kind'] or '',
                             it['name'], it['sku'], q])

    # ---- 7. ကုန်ကျစရိတ်: expenses dated the selected date. The
    # expenses table's method column is migration-added (it exists on
    # any database that ran init_db); the SELECT is built from the
    # live PRAGMA so the sheet simply omits the method column on a
    # database where it is genuinely absent, instead of inventing it.
    ecols = [r[1] for r in d.execute("PRAGMA table_info(expenses)").fetchall()]
    has_method = 'method' in ecols
    esel = "SELECT e.category, e.amount, e.detail, e.by_user" + (", e.method" if has_method else "") + \
           " FROM expenses e WHERE e.edate=? ORDER BY e.id"
    erows = d.execute(esel, (date_str,)).fetchall()
    hdr_exp = ['အမျိုးအစား', 'ငွေ'] + (['နည်းလမ်း'] if has_method else []) + ['မှတ်သူ', 'မှတ်ချက်']
    rows_exp = [[e['category'] or '', e['amount']] +
                ([e['method'] or ''] if has_method else []) +
                [uname(e['by_user']), e['detail'] or ''] for e in erows]

    # ---- 8. Cashbox: the selected date's day via the app's own
    # cashbox_day_figures() — the chain (opening = latest counted
    # closing, closed days frozen) is reused, never recomputed here,
    # and no day row is created: a missing row is reported honestly
    # as still-open with whatever figures exist. In/out split reuses
    # cashbox_auto() + cashbox_manual(), the same feeds the figures
    # helper sums internally. Denom split summary reuses
    # cashbox_day_denoms().
    fg = cashbox_day_figures(d, date_str)
    crow = fg['row']
    ain, aout = cashbox_auto(d, date_str)
    _mrows, minn, mout = cashbox_manual(d, date_str)
    if crow is None:
        cb_status = 'မပိတ် ရသေး (မှတ်တမ်း မရှိ)'
    elif crow['status'] in ('counted', 'closed'):
        cb_status = 'ပိတ်ပြီး' + (' (frozen)' if fg['frozen'] else '')
    else:
        cb_status = 'မပိတ် ရသေး'
    rows_cb = [['ရက်စွဲ', date_str],
               ['အခြေအနေ', cb_status],
               ['အဖွင့်', fg['opening'] if fg['opening'] is not None else ''],
               ['ဝင်', money(ain + minn)],
               ['ထွက်', money(aout + mout)],
               ['စာရင်းပိတ်', fg['book'] if fg['book'] is not None else ''],
               ['ရေတွက်ပိတ်', fg['counted'] if fg['counted'] is not None else ''],
               ['ကွာခြားချက်', fg['diff'] if fg['diff'] is not None else ''],
               ['ကွာခြားချက် အကြောင်း', crow['diff_reason'] if crow and crow['diff_reason'] else '']]
    if fg['opening'] is None:
        rows_cb.append(['မှတ်ချက်', 'အဖွင့် မသတ်မှတ်ရ သေး — seed လိုအပ်နေ'])
    if crow and crow['status'] == 'closed' and fg['changed_after_close']:
        rows_cb.append(['မှတ်ချက်', 'ပိတ်ပြီးနောက် ပြောင်းလဲမှု ရှိ (flag)'])
    dd = cashbox_day_denoms(d, date_str)
    for dr in dd['rows']:
        if dr['book'] is None and dr['counted'] is None:
            txt = ''
        else:
            txt = 'စာရင်း: %s ရွက် · ရေတွက်: %s ရွက်' % (
                dr['book'] if dr['book'] is not None else '—',
                dr['counted'] if dr['counted'] is not None else '—')
        rows_cb.append(['ရွက်ရေ — %s' % dr['value'], txt])
    if dd['partial']:
        rows_cb.append(['မှတ်ချက်', 'ရွက်ရေ အချက်အလက် မပြည့်စုံ (partial) — မခွဲထားသော ငွေ ရှိ'])
    hdr_cb = ['အရာ', 'အသေးစိတ်']

    # ---- 9. လက်ကျန်: outstanding AT GENERATION TIME (label row
    # first). Undelivered = status 'new'; unpaid/partial = any
    # non-cancelled invoice whose total minus verified received is
    # above zero; pending handover = payments with verified = 0, any
    # date (current state, not the selected date's activity).
    allords = d.execute("SELECT o.id, o.code, o.outlet, o.odate, o.status, o.discount "
                        "FROM orders o ORDER BY o.odate, o.id").fetchall()
    allids = [o['id'] for o in allords]
    allrec = order_received_map(d, allids) if allids else {}
    alltot = {}
    if allids:
        ph = ','.join('?' * len(allids))
        gross = {r['oid']: (r['s'] or 0) for r in d.execute(
            "SELECT order_id oid, COALESCE(SUM(qty*price),0) s FROM order_items "
            f"WHERE order_id IN ({ph}) GROUP BY order_id", allids).fetchall()}
        for o in allords:
            alltot[o['id']] = money(gross.get(o['id'], 0) - (o['discount'] or 0))
    rows_out = []
    hdr_out = ['အမျိုး', 'ဘောက်ချာ', 'ဆိုင်', 'ရက်စွဲ', 'စုစုပေါင်း', 'ပေးပြီး', 'လက်ကျန်', 'အသေးစိတ်']
    for o in allords:
        total, received = alltot[o['id']], money(allrec.get(o['id'], 0))
        bal = money(total - received)
        if o['status'] == 'new':
            rows_out.append(['မပို့ရ သေး', o['code'], o['outlet'], o['odate'], total, received, bal, ''])
    for o in allords:
        if o['status'] == 'cancelled':
            continue
        total, received = alltot[o['id']], money(allrec.get(o['id'], 0))
        bal = money(total - received)
        if bal > 0:
            rows_out.append(['ပေးရန် ကျန်', o['code'], o['outlet'], o['odate'], total, received, bal, ''])
    pend = d.execute("SELECT p.amount, p.rdate, p.method, p.by_user, o.code ocode, o.outlet "
                     "FROM order_payments p LEFT JOIN orders o ON o.id=p.order_id "
                     "WHERE COALESCE(p.verified,1)=0 ORDER BY p.id").fetchall()
    for p in pend:
        rows_out.append(['handover စောင့်', p['ocode'] or '', p['outlet'] or '', p['rdate'] or '',
                         '', '', p['amount'],
                         'မှတ်သူ: %s · နည်းလမ်း: %s' % (uname(p['by_user']), p['method'] or 'cash')])

    content = [
        ('ဘောက်ချာ', hdr_orders, rows_orders),
        ('ပို့ပြီး', hdr_deliv, rows_deliv),
        ('ငွေ', hdr_money, rows_money),
        ('လက်ခံ', hdr_recv, rows_recv),
        ('စတော့', [label], [hdr_stock] + data_stock),
        ('ဝယ်ရန် ORQ', [label], [hdr_orq] + rows_orq),
        ('ကုန်ကျစရိတ်', hdr_exp, rows_exp),
        ('Cashbox', hdr_cb, rows_cb),
        ('လက်ကျန်', [label], [hdr_out] + rows_out),
    ]
    if role == 'warehouse':
        keep = {'ဘောက်ချာ', 'ပို့ပြီး', 'လက်ခံ', 'စတော့', 'ဝယ်ရန် ORQ', 'လက်ကျန်'}
        content = [s for s in content if s[0] in keep]
    shown = content if role in ('owner', 'finance', 'warehouse') else []
    cover_rows = [['ရက်စွဲ', date_str],
                  ['ထုတ်လုပ်ချိန်', gen],
                  ['ထုတ်လုပ်သူ', username],
                  ['မှတ်ချက်', 'လက်ရှိ ဟု သတ်မှတ်ထားသော စာရွက်များသည် ထုတ်လုပ်ချိန်က လက်ရှိ အခြေအနေ ဖြစ်ပြီး၊ ရွေးထားသော ရက်စွဲ၏ အခြေအနေ မဟုတ်ပါ။'],
                  ['ပါဝင်သော စာရွက်များ', ', '.join(s[0] for s in shown)]]
    cover = ('အကြောင်း', ['အရာ', 'အသေးစိတ်'], cover_rows)
    return ([cover] + shown, meta)


# ---------------- Stock ledger ----------------
@app.route('/stock')
@require_perm('stock.view')
def stock_moves():
    pid = request.args.get('pid')
    d = db()
    sql = ("SELECT m.*, p.sku, p.name, u.name uname FROM stock_moves m JOIN products p ON p.id=m.product_id "
           "LEFT JOIN users u ON u.id=m.by_user")
    args = []
    sel = None
    if pid:
        pid = int(pid)
        fam = family_ids(d, pid)
        ph = ','.join('?' * len(fam))
        sql += f" WHERE m.product_id IN ({ph})"; args += fam
        prow = d.execute("SELECT * FROM products WHERE id=?", (pid,)).fetchone()
        if prow:
            base_id, pf = pool_info(d, pid)
            partner = None
            if base_id != pid:
                partner = d.execute("SELECT sku, unit FROM products WHERE id=?", (base_id,)).fetchone()
            else:
                mem = d.execute("SELECT sku, unit FROM products WHERE pool_id=? AND id!=? ORDER BY id LIMIT 1", (pid, pid)).fetchone()
                partner = mem
            sel = {'prod': prow, 'view': sku_stock(d, pid), 'pool': pool_stock(d, pid),
                   'partner': partner, 'base_unit': (d.execute("SELECT unit FROM products WHERE id=?", (base_id,)).fetchone() or {})['unit'] if base_id else ''}
    sql += " ORDER BY m.id DESC LIMIT 300"
    rows = d.execute(sql, args).fetchall()
    prods = d.execute("SELECT id, sku, name FROM products WHERE active=1 ORDER BY sku").fetchall()
    return render_template('stock.html', rows=rows, prods=prods, pid=pid, sel=sel)


# ---------------- Catalog (Sales PWA) ----------------
@app.route('/catalog')
@login_required()
def catalog():
    if not has_perm(g.user, 'catalog.view'):
        # v49 icon fix: the installed app used to open on /catalog and a
        # delivery user met a dead "Forbidden" page (proven from the server
        # logs). Send them to their own home page instead.
        return redirect(url_for('dashboard'))
    return render_template('catalog.html', **shop_type_ctx())

@app.route('/manifest.webmanifest')
def manifest():
    return jsonify({
        'name': 'GTM Distribution ERP', 'short_name': 'GTM ERP',
        'start_url': '/', 'display': 'standalone',
        'background_color': '#193059', 'theme_color': '#193059',
        'icons': [{'src': '/static/icon-192.png', 'sizes': '192x192', 'type': 'image/png'},
                  {'src': '/static/icon-512.png', 'sizes': '512x512', 'type': 'image/png'}]})

@app.route('/sw.js')
def sw():
    resp = app.make_response(render_template('sw.js'))
    resp.headers['Content-Type'] = 'application/javascript'
    resp.headers['Service-Worker-Allowed'] = '/'
    return resp



# ---------------- Buy list + Order Requests (supplier orders) ----------------
OR_STATUS = {'open': 'မှာထား 📝', 'partial': 'တစ်ချို့ ရောက်ပြီး 🚚', 'done': 'ပြီးဆုံး ✅', 'cancelled': 'ပယ်ဖျက် ❌'}

def or_recalc_status(d, orid):
    """Recompute an order request's status from its lines (unless cancelled)."""
    r = d.execute("SELECT status FROM order_requests WHERE id=?", (orid,)).fetchone()
    if not r or r['status'] == 'cancelled':
        return
    agg = d.execute("SELECT COALESCE(SUM(qty),0) q, COALESCE(SUM(received_qty),0) rc FROM order_request_items WHERE or_id=?", (orid,)).fetchone()
    if agg['rc'] <= 0:
        st = 'open'
    elif agg['rc'] >= agg['q']:
        st = 'done'
    else:
        st = 'partial'
    d.execute("UPDATE order_requests SET status=? WHERE id=?", (st, orid))

@app.route('/buy-group', methods=['POST'])
@require_perm('buy_group.set')
def buy_group_set():
    # v58: the buy strip's tab toggle posts the tapped group here so the
    # choice persists — shared-tail pages (and later visits) open on the
    # group the user last picked. Fire-and-forget from the strip script;
    # a failed post just means the next full page render re-derives the
    # group from the page itself, as in v57.
    grp = (request.form.get('group') or '').strip()
    if grp not in ('demand', 'direct'):
        data = request.get_json(silent=True) or {}
        grp = (data.get('group') or '').strip()
    if grp not in ('demand', 'direct'):
        return jsonify(ok=False), 400
    session['buy_group'] = grp
    return jsonify(ok=True)

@app.route('/buy-list')
@require_perm('buy_list.view')
def buy_list():
    d = db()
    day = (request.args.get('day') or '').strip()
    show_all = (day == 'all')
    if not show_all and not re.match(r'^\d{4}-\d{2}-\d{2}$', day or ''):
        day = today()
    # v64: remember the view being worked in, so the why page's back
    # control returns to THIS view (see _trail_back).
    session['buylist_day'] = 'all' if show_all else day
    if show_all:
        dm_sql = "SELECT oi.product_id, SUM(oi.qty) demand FROM order_items oi JOIN orders od ON od.id=oi.order_id WHERE od.status='new' GROUP BY oi.product_id"
        dm_args = ()
    else:
        dm_sql = "SELECT oi.product_id, SUM(oi.qty) demand FROM order_items oi JOIN orders od ON od.id=oi.order_id WHERE od.status='new' AND od.odate=? GROUP BY oi.product_id"
        dm_args = (day,)
    older = {}
    older_orders = 0
    older_short = 0
    if not show_all:
        for r in d.execute("SELECT oi.product_id pid, SUM(oi.qty) q FROM order_items oi JOIN orders od ON od.id=oi.order_id WHERE od.status='new' AND od.odate<? GROUP BY oi.product_id", (day,)).fetchall():
            older[r['pid']] = r['q']
        older_orders = d.execute("SELECT COUNT(*) c FROM orders WHERE status='new' AND odate<?", (day,)).fetchone()['c']
    rows = d.execute("""
      SELECT p.id, p.sku, p.name, p.unit, p.supplier, COALESCE(s.qty,0) stock,
             COALESCE(dm.demand,0) demand, COALESCE(oo.onorder,0) onorder, p.pool_id, p.pack_factor
      FROM products p
      LEFT JOIN stock s ON s.product_id=p.id
      LEFT JOIN (""" + dm_sql + """) dm ON dm.product_id=p.id
      LEFT JOIN (SELECT ri.product_id, SUM(ri.qty-ri.received_qty) onorder FROM order_request_items ri
                 JOIN order_requests r ON r.id=ri.or_id WHERE r.status IN ('open','partial') GROUP BY ri.product_id) oo ON oo.product_id=p.id
      WHERE COALESCE(dm.demand,0)>0 OR COALESCE(oo.onorder,0)>0
      ORDER BY p.sku""", dm_args).fetchall()
    sups = [r['supplier'] for r in d.execute("SELECT DISTINCT supplier FROM products WHERE supplier!='' ORDER BY supplier").fetchall()]
    # v46: need is computed per FAMILY in base units (demand and on-order
    # converted by each SKU's factor, stock = the one pool), then shared back
    # to the demanded rows so a case on order visibly covers card demand.
    import math as _math
    def _fam(r):
        if r['pool_id'] and r['pool_id'] != r['id']:
            return (r['pool_id'], r['pack_factor'] or 1)
        return (r['id'], 1)
    onorder_map = {r['product_id']: r['onorder'] for r in d.execute(
        "SELECT ri.product_id, SUM(ri.qty-ri.received_qty) onorder FROM order_request_items ri "
        "JOIN order_requests r ON r.id=ri.or_id WHERE r.status IN ('open','partial') GROUP BY ri.product_id").fetchall()}
    prod_fam = {}
    for pr in d.execute("SELECT id, pool_id, pack_factor FROM products").fetchall():
        if pr['pool_id'] and pr['pool_id'] != pr['id']:
            prod_fam[pr['id']] = (pr['pool_id'], pr['pack_factor'] or 1)
        else:
            prod_fam[pr['id']] = (pr['id'], 1)
    # v62: every pool's level from one pass (was raw_stock() per family —
    # one query per family on this page).
    stockmap = {r['product_id']: r['qty'] for r in d.execute("SELECT product_id, qty FROM stock").fetchall()}
    fam = {}
    def _fagg(base):
        return fam.setdefault(base, {'demand': 0.0, 'older': 0.0, 'onorder': 0.0})
    for r in rows:
        b, f = _fam(r)
        a = _fagg(b)
        a['demand'] += (r['demand'] or 0) * f
        a['onorder'] += (r['onorder'] or 0) * f
    for pid, q in (older.items() if not show_all else []):
        b, f = prod_fam.get(pid, (pid, 1))
        _fagg(b)['older'] += q * f
    for pid, q in onorder_map.items():
        b, f = prod_fam.get(pid, (pid, 1))
        if not any(r['id'] == pid for r in rows):
            _fagg(b)['onorder'] += q * f
    for b, a in fam.items():
        a['pool'] = stockmap.get(b, 0)
        a['need_base'] = a['demand'] - (a['pool'] + a['onorder'] - a['older'])
    if not show_all:
        older_short = sum(1 for a in fam.values() if a['older'] > 0 and a['older'] > a['pool'] + a['onorder'])
    disp = [dict(r) for r in rows if show_all or (r['demand'] and r['demand'] > 0)]
    disp.sort(key=lambda r: (_fam(r)[1], r['sku']))
    remaining = {b: a['need_base'] for b, a in fam.items()}
    need_of = {}
    for r in disp:
        b, f = _fam(r)
        dem_base = (r['demand'] or 0) * f
        alloc = max(0.0, min(remaining.get(b, 0.0), dem_base))
        remaining[b] = remaining.get(b, 0.0) - alloc
        need_of[r['id']] = int(_math.ceil(alloc / f - 1e-9)) if alloc > 0 else 0
    groups = {}
    for r in rows:
        if not show_all and not (r['demand'] and r['demand'] > 0):
            continue
        rr = dict(r)
        b, f = _fam(r)
        a = fam.get(b, {'demand': 0.0, 'older': 0.0, 'onorder': 0.0, 'pool': stockmap.get(b, 0)})
        rr['stock'] = int(a['pool'] / f) if f != 1 else a['pool']
        rr['onorder'] = int(a['onorder'] / f) if f != 1 else a['onorder']
        rr['older'] = (int(a['older'] / f) if f != 1 else a['older']) if not show_all else 0
        rr['need'] = need_of.get(r['id'], 0)
        sup = (r['supplier'] or '').strip() or 'Unknown'
        groups.setdefault(sup, []).append(rr)
    # v63 (display only): per-group flags so the template can tell a group
    # with something still to order from one that is fully covered — by an
    # open request (any_ordered) or by stock on hand. No math changes.
    glist = [{'supplier': k, 'rows': v,
              'any_need': any((r['need'] or 0) > 0 for r in v),
              'any_ordered': any((r['need'] or 0) <= 0 and (r['onorder'] or 0) > 0 for r in v),
              # v63: a fresh one-time token per group form per page load —
              # resubmitting THIS form (lag double-tap) creates nothing.
              'ftok': uuid.uuid4().hex}
             for k, v in sorted(groups.items(), key=lambda kv: (kv[0] == 'Unknown', kv[0].lower()))]
    open_reqs = d.execute("""SELECT r.*, u.name byname,
        (SELECT COALESCE(SUM(qty),0) FROM order_request_items WHERE or_id=r.id) tq,
        (SELECT COALESCE(SUM(received_qty),0) FROM order_request_items WHERE or_id=r.id) rq
        FROM order_requests r LEFT JOIN users u ON u.id=r.created_by
        WHERE r.status IN ('open','partial') ORDER BY r.id""").fetchall()
    return render_template('buy_list.html', groups=glist, sups=sups, day=day, show_all=show_all,
                           older_orders=older_orders, older_short=older_short, today_s=today(),
                           open_reqs=open_reqs, OR_STATUS=OR_STATUS)

def _why_family(d, pid, day, show_all):
    """v64: buy_list()'s v46 family computation for ONE product, mirrored
    step for step — same demand/older/on-order aggregates in base units,
    same pool, same allocation walk over the family's displayed rows —
    so the drilldown's need figure is the row's figure by construction.
    Returns None when the product does not exist. Display figures are
    converted back with the row's own convention (pool / factor,
    truncated, exactly like the buy list's row cells); the strip's total
    is the sum of the two displayed parts, so the strip always adds up
    on the page itself."""
    import math as _math
    p = d.execute("SELECT * FROM products WHERE id=?", (pid,)).fetchone()
    if not p:
        return None
    def _fam_of(r):
        if r['pool_id'] and r['pool_id'] != r['id']:
            return (r['pool_id'], r['pack_factor'] or 1)
        return (r['id'], 1)
    base, f_pid = _fam_of(p)
    famrows = [r for r in d.execute("SELECT id, sku, pool_id, pack_factor FROM products").fetchall()
               if _fam_of(r)[0] == base]
    fmap = {r['id']: _fam_of(r)[1] for r in famrows}
    ids = [r['id'] for r in famrows]
    ph = ','.join('?' * len(ids))
    if show_all:
        dm = {r['product_id']: r['demand'] for r in d.execute(
            "SELECT oi.product_id, SUM(oi.qty) demand FROM order_items oi JOIN orders od ON od.id=oi.order_id "
            "WHERE od.status='new' AND oi.product_id IN (%s) GROUP BY oi.product_id" % ph, ids).fetchall()}
        older = {}
    else:
        dm = {r['product_id']: r['demand'] for r in d.execute(
            "SELECT oi.product_id, SUM(oi.qty) demand FROM order_items oi JOIN orders od ON od.id=oi.order_id "
            "WHERE od.status='new' AND od.odate=? AND oi.product_id IN (%s) GROUP BY oi.product_id" % ph, (day,) + tuple(ids)).fetchall()}
        older = {r['pid']: r['q'] for r in d.execute(
            "SELECT oi.product_id pid, SUM(oi.qty) q FROM order_items oi JOIN orders od ON od.id=oi.order_id "
            "WHERE od.status='new' AND od.odate<? AND oi.product_id IN (%s) GROUP BY oi.product_id" % ph, (day,) + tuple(ids)).fetchall()}
    oo = {r['product_id']: r['onorder'] for r in d.execute(
        "SELECT ri.product_id, SUM(ri.qty-ri.received_qty) onorder FROM order_request_items ri "
        "JOIN order_requests r ON r.id=ri.or_id WHERE r.status IN ('open','partial') AND ri.product_id IN (%s) "
        "GROUP BY ri.product_id" % ph, ids).fetchall()}
    srow = d.execute("SELECT qty FROM stock WHERE product_id=?", (base,)).fetchone()
    pool = srow['qty'] if srow else 0
    demand_b = sum((dm.get(i) or 0) * fmap[i] for i in ids)
    older_b = sum((older.get(i) or 0) * fmap[i] for i in ids)
    onorder_b = sum((oo.get(i) or 0) * fmap[i] for i in ids)
    need_base = demand_b - (pool + onorder_b - older_b)
    # The allocation walk, over exactly the rows the buy list displays
    # for this family, in its (factor, sku) order.
    disp = [r for r in famrows if show_all or (dm.get(r['id']) or 0) > 0]
    disp.sort(key=lambda r: (fmap[r['id']], r['sku']))
    remaining = need_base
    need_of = {}
    for r in disp:
        dem_b = (dm.get(r['id']) or 0) * fmap[r['id']]
        alloc = max(0.0, min(remaining, dem_b))
        remaining -= alloc
        need_of[r['id']] = int(_math.ceil(alloc / fmap[r['id']] - 1e-9)) if alloc > 0 else 0
    def _units(base_qty):
        return int(base_qty / f_pid) if f_pid != 1 else base_qty
    return {'prod': p, 'base': base, 'factor': f_pid, 'ids': ids, 'family_size': len(ids),
            'today_disp': _units(demand_b), 'older_disp': _units(older_b),
            'total_disp': _units(demand_b + older_b) if show_all else _units(demand_b) + _units(older_b),
            'stock_disp': _units(pool), 'onorder_disp': _units(onorder_b),
            'need': need_of.get(pid, 0)}

@app.route('/buy-list/why')
@require_perm('buy_list.why')
def buy_list_why():
    """v64 (Ko, 2026-10-23): the demand drilldown — WHICH undelivered
    invoices make this product's buy-list need. His words: new and old
    demand 'အကုန်ရောနေတယ်' (all mixed up). Read-only; the totals strip
    reconciles with the buy-list row the user came from (same math,
    see _why_family)."""
    d = db()
    try:
        pid = int(request.args.get('pid') or 0)
    except (TypeError, ValueError):
        pid = 0
    day = (request.args.get('day') or '').strip()
    show_all = (day == 'all')
    if not show_all and not re.match(r'^\d{4}-\d{2}-\d{2}$', day or ''):
        day = today()
    session['buylist_day'] = 'all' if show_all else day
    w = _why_family(d, pid, day, show_all)
    if w is None:
        abort(404)
    fam_ids = w['ids']
    ph = ','.join('?' * len(fam_ids))
    lines = d.execute("""
      SELECT o.id oid, o.code, o.outlet, o.odate, oi.qty, p.sku, p.unit
      FROM order_items oi JOIN orders o ON o.id=oi.order_id JOIN products p ON p.id=oi.product_id
      WHERE o.status='new' AND oi.product_id IN (%s)
      ORDER BY o.odate DESC, o.id DESC, oi.id""" % ph, fam_ids).fetchall()
    today_lines, older_lines, all_lines = [], [], []
    for l in lines:
        if show_all:
            all_lines.append(l)
        elif l['odate'] == day:
            today_lines.append(l)
        elif l['odate'] < day:
            older_lines.append(l)
        else:
            # A future-dated line (a phone clock ahead of today): the
            # day view's math counts it in NEITHER group; it is listed
            # with today's lines so it is at least visible, real date
            # shown — the strip stays the buy list's exact figures.
            today_lines.append(l)
    return render_template('buy_why.html', w=w, day=day, show_all=show_all,
                           today_lines=today_lines, older_lines=older_lines,
                           all_lines=all_lines)

def _used_form_token(d, ftok):
    """v63 (Ko, 2026-10-23): one-time form tokens for request creation —
    the client_uuid pattern from offline orders, DB-backed so a token
    stays consumed across devices/sessions. Returns the token's row
    (with the created request's code) when the token was already spent,
    else None. A missing token returns None: an old cached page without
    the field still orders normally (fail-open)."""
    if not ftok:
        return None
    return d.execute("SELECT t.or_id AS or_id, r.code AS code FROM buy_form_tokens t "
                     "LEFT JOIN order_requests r ON r.id = t.or_id WHERE t.token=?",
                     (ftok,)).fetchone()

def _dup_token_flash(used):
    if used is not None and used['code']:
        flash(tr('ဒီ အမှာ ကို ဖွင့်ပြီးသား — ထပ်မဖွင့်ဘူး ({code})', code=used['code']))
    else:
        flash(tr('ဒီ အမှာ ကို ဖွင့်ပြီးသား — ထပ်မဖွင့်ဘူး'))

def _spend_form_token(d, ftok, orid):
    """Record ftok as spent by request orid (same transaction as the
    create). Returns False when a racing submit spent it first — the
    caller then rolls its own create back and answers as a duplicate."""
    if not ftok:
        return True
    try:
        d.execute("INSERT INTO buy_form_tokens(token,user_id,or_id,created_at) VALUES(?,?,?,?)",
                  (ftok, g.user['id'], orid, now()))
        return True
    except sqlite3.IntegrityError:
        return False

def _merged_token_flash(used):
    """v64: the merge twin of _dup_token_flash — a resubmitted merge
    action answers 'already added in', naming the slip it went into."""
    if used is not None and used['code']:
        flash(tr('ဒီ အမှာ ကို ပေါင်းပြီးသား — ထပ်မပေါင်းဘူး ({code})', code=used['code']))
    else:
        flash(tr('ဒီ အမှာ ကို ပေါင်းပြီးသား — ထပ်မပေါင်းဘူး'))

def _used_pay_token(d, ftok):
    """v65: the payment form's one-time token — the v63 pattern on its
    own table (pay_form_tokens), DB-backed so a token stays spent across
    devices/sessions. Returns the token's row when already spent, else
    None. A missing token returns None: an old cached page without the
    field still records normally (fail-open, as v63)."""
    if not ftok:
        return None
    return d.execute("SELECT t.order_id AS order_id, o.code AS code FROM pay_form_tokens t "
                     "LEFT JOIN orders o ON o.id = t.order_id WHERE t.token=?",
                     (ftok,)).fetchone()

def _spend_pay_token(d, ftok, oid):
    """Record ftok as spent by the payment on order oid (same transaction
    as the insert). Returns False when a racing submit spent it first —
    the caller then rolls its own insert back and answers as a dup."""
    if not ftok:
        return True
    try:
        d.execute("INSERT INTO pay_form_tokens(token,user_id,order_id,created_at) VALUES(?,?,?,?)",
                  (ftok, g.user['id'], oid, now()))
        return True
    except sqlite3.IntegrityError:
        return False

def _dup_pay_flash(used):
    if used is not None and used['code']:
        flash(tr('ဒီ ငွေ ကို မှတ်ပြီးသား — ထပ်မမှတ်ဘူး ({code})', code=used['code']))
    else:
        flash(tr('ဒီ ငွေ ကို မှတ်ပြီးသား — ထပ်မမှတ်ဘူး'))

def _create_request(d, supplier, kind, note, lines):
    """Insert a request + its lines and return (orid, code). No commit —
    the caller owns the transaction, so the v63 form token can be spent
    inside the same one and a lost race can roll the create back."""
    code = next_code('ORQ', 'or_seq')
    d.execute("INSERT INTO order_requests(code,supplier,note,created_by,created_at,kind) VALUES(?,?,?,?,?,?)",
              (code, supplier, note, g.user['id'], now(), kind))
    orid = d.execute("SELECT last_insert_rowid() i").fetchone()['i']
    for pid, q in lines:
        d.execute("INSERT INTO order_request_items(or_id,product_id,qty) VALUES(?,?,?)", (orid, pid, q))
    return orid, code

def _open_slips_same_kind(d, supplier, kind):
    """v64: slips that could take more lines for this supplier — OPEN or
    PARTIAL, and of the SAME kind only (Ko's never-mixed rule: a Direct
    slip is never offered as a merge target for a Demand submit)."""
    return d.execute("""
      SELECT r.*, u.name byname,
        (SELECT COUNT(*) FROM order_request_items WHERE or_id=r.id) nlines,
        (SELECT COALESCE(SUM(qty-received_qty),0) FROM order_request_items WHERE or_id=r.id) rem
      FROM order_requests r LEFT JOIN users u ON u.id=r.created_by
      WHERE r.supplier=? AND r.kind=? AND r.status IN ('open','partial')
      ORDER BY r.id""", (supplier, kind)).fetchall()

def _render_merge_choice(d, source, supplier, kind, note, day, lines, ftok, cands):
    """v64: the stop-and-ask page. It CREATES NOTHING — it only carries
    the submission (lines + the original form's v63 one-time token) to
    the /requests/merge-choice action, where merge-or-new happens."""
    disp = []
    for pid, q in lines:
        p = d.execute("SELECT sku, name, unit FROM products WHERE id=?", (pid,)).fetchone()
        disp.append({'pid': pid, 'qty': q,
                     'sku': p['sku'] if p else '', 'name': p['name'] if p else '',
                     'unit': (p['unit'] or '') if p else ''})
    return render_template('request_merge_choice.html', source=source, supplier=supplier,
                           kind=kind, note=note, day=day, lines=disp, ftok=ftok,
                           cands=cands, OR_STATUS=OR_STATUS,
                           strip_active='buy' if source == 'buylist' else 'req')

@app.route('/requests/merge-choice', methods=['POST'])
@require_perm('requests.merge_choice')
def request_merge_choice():
    """v64: the merge prompt's action endpoint. The original create
    submit (buy list or manual form) found a same-supplier, same-kind
    slip already open and rendered the choice page instead of creating;
    this endpoint carries the SAME one-time token and either merges the
    lines into the chosen slip or creates the new slip after all —
    whichever action lands first spends the token with its effect, and
    a resubmission of either action changes nothing."""
    d = db(); f = request.form
    source = f.get('source') if f.get('source') in ('buylist', 'manual') else 'buylist'
    supplier = canon_supplier(d, f.get('supplier', ''))
    kind = (f.get('kind') or 'demand').strip()
    if kind not in ('demand', 'direct'):
        kind = 'demand'
    day = (f.get('day') or '').strip()
    raw_note = f.get('note', '').strip()
    lines = []
    for pid_s, qty_s in zip(f.getlist('pid'), f.getlist('qty')):
        try:
            qty = float(qty_s or 0); pid = int(pid_s)
        except (ValueError, TypeError):
            continue
        if qty > 0:
            lines.append((pid, qty))
    choice = (f.get('choice') or '').strip()
    merging = choice.startswith('merge:')

    def _back(used):
        # Land where the originating form's own answers land.
        if source == 'manual':
            if used is not None and used['or_id']:
                return redirect(url_for('request_detail', orid=used['or_id']))
            return redirect(url_for('request_new'))
        return redirect(url_for('buy_list') + (('?day=' + day) if day else ''))

    if not supplier or not lines:
        flash(tr('ပစ္စည်း အနည်းဆုံး ၁ မျိုး ထည့်ပါ') if source == 'manual'
              else tr('ဝယ်ရန် ပစ္စည်း အနည်းဆုံး ၁ မျိုး ရွေးပါ'))
        return _back(None)
    ftok = (f.get('ftok') or '').strip()
    used = _used_form_token(d, ftok)
    if used is not None:
        if merging:
            _merged_token_flash(used)
            if used['or_id']:
                return redirect(url_for('request_detail', orid=used['or_id']))
        else:
            _dup_token_flash(used)
        return _back(used)
    if choice == 'new':
        note = raw_note
        if source == 'buylist' and day and day != 'all':
            note = (day + ' batch') + ((' · ' + note) if note else '')
        orid, code = _create_request(d, supplier, kind, note, lines)
        if not _spend_form_token(d, ftok, orid):
            d.rollback(); w = _used_form_token(d, ftok); _dup_token_flash(w)
            return _back(w)
        d.commit()
        if source == 'buylist':
            flash(tr('{code} အမှာ တင်ပြီးပြီ — အောက် စလစ် စာရင်း ထဲမှာ ရှိတယ် (ကုန် ရောက်မှ stock ဝင်မယ်)', code=code))
            return redirect(url_for('buy_list') + (('?day=' + day) if day else ''))
        flash(tr('{code} အမှာ တင်ပြီးပြီ', code=code))
        return redirect(url_for('request_detail', orid=orid))
    if merging:
        try:
            target_id = int(choice.split(':', 1)[1])
        except (ValueError, IndexError):
            abort(400)
        t = d.execute("SELECT * FROM order_requests WHERE id=?", (target_id,)).fetchone()
        if not t or t['status'] not in ('open', 'partial') or t['supplier'] != supplier or t['kind'] != kind:
            flash(tr('အဲ့ဒီ အမှာ က ပေါင်းလို့ မရတော့ဘူး — ပြန် စမ်းပါ'))
            return _back(None)
        # Same merge the detail page's add-line does: a product already
        # on the slip grows on its ONE line; a new product is appended.
        for pid, q in lines:
            ex = d.execute("SELECT * FROM order_request_items WHERE or_id=? AND product_id=?", (target_id, pid)).fetchone()
            if ex:
                d.execute("UPDATE order_request_items SET qty=qty+? WHERE id=?", (q, ex['id']))
            else:
                d.execute("INSERT INTO order_request_items(or_id,product_id,qty) VALUES(?,?,?)", (target_id, pid, q))
        # The merge leaves a dated line in the slip's note — requests
        # keep no edit log of their own, and the note is the slip's
        # visible trail (same append shape as request_close). A note
        # typed on the original form rides along after the trail line.
        src_key = 'ဝယ်ရန် စာရင်းက ပေါင်းထည့် ({date})' if source == 'buylist' else 'အမှာ အသစ်က ပေါင်းထည့် ({date})'
        extra = ' · ' + tr(src_key, date=today()) + ((' — ' + raw_note) if raw_note else '')
        d.execute("UPDATE order_requests SET note=COALESCE(note,'') || ? WHERE id=?", (extra, target_id))
        or_recalc_status(d, target_id)
        if not _spend_form_token(d, ftok, target_id):
            d.rollback(); w = _used_form_token(d, ftok); _merged_token_flash(w)
            return _back(w)
        d.commit(); flash(tr('{code} ထဲ ပေါင်းထည့်ပြီးပြီ', code=t['code']))
        return redirect(url_for('request_detail', orid=target_id))
    abort(400)

@app.route('/requests/from-buylist', methods=['POST'])
@require_perm('requests.from_buylist')
def request_from_buylist():
    d = db(); f = request.form
    supplier = canon_supplier(d, f.get('supplier', ''))
    if not supplier:
        flash(tr('Supplier နာမည် ထည့်ပါ')); return redirect(url_for('buy_list'))
    sel = f.getlist('sel')
    lines = []
    for pid_s in sel:
        try:
            q = float(f.get(f'qty_{pid_s}') or 0)
        except ValueError:
            q = 0
        if q > 0:
            lines.append((int(pid_s), q))
    if not lines:
        flash(tr('ဝယ်ရန် ပစ္စည်း အနည်းဆုံး ၁ မျိုး ရွေးပါ')); return redirect(url_for('buy_list'))
    # v63: one-time form token — a lag double-tap resubmits THIS form;
    # the second submit creates nothing and names the request it made.
    ftok = (f.get('ftok') or '').strip()
    used = _used_form_token(d, ftok)
    if used is not None:
        _dup_token_flash(used)
        _bd = (f.get('day') or '').strip()
        return redirect(url_for('buy_list') + (('?day=' + _bd) if _bd else ''))
    day = (f.get('day') or '').strip()
    # v64 (Ko, 2026-10-23): a Demand slip for this supplier is already
    # open — stop and ask (merge into it, or a separate new slip)
    # instead of silently spawning slip #2 (his ORQ-0013 + ORQ-0014:
    # correct totals, but two slips → two PURs → two settlements).
    cands = _open_slips_same_kind(d, supplier, 'demand')
    if cands:
        return _render_merge_choice(d, 'buylist', supplier, 'demand',
                                     f.get('note', '').strip(), day, lines, ftok, cands)
    note = f.get('note', '').strip()
    if day and day != 'all':
        note = (day + ' batch') + ((' · ' + note) if note else '')
    # v59: a request born on the buy list is a Demand Driven request.
    orid, code = _create_request(d, supplier, 'demand', note, lines)
    if not _spend_form_token(d, ftok, orid):
        d.rollback(); _dup_token_flash(_used_form_token(d, ftok))
        _bd = (f.get('day') or '').strip()
        return redirect(url_for('buy_list') + (('?day=' + _bd) if _bd else ''))
    d.commit(); flash(tr('{code} အမှာ တင်ပြီးပြီ — အောက် စလစ် စာရင်း ထဲမှာ ရှိတယ် (ကုန် ရောက်မှ stock ဝင်မယ်)', code=code))
    _bday = request.form.get('day') or ''
    return redirect(url_for('buy_list') + (('?day=' + _bday) if _bday else ''))

@app.route('/requests')
@require_perm('requests.view')
def requests_list():
    d = db()
    # v57 (Ko-flagged live, 2026-10-22): ?open=1 is the RECEIVE landing
    # view — the strip's '🚚 ကုန်ရောက် သွင်း' stop now lands on its action:
    # only requests that can still be received (open/partial), each row
    # with its own receive button. Plain /requests stays the all-status
    # list, unchanged. The open view is a buy-list page: Demand expanded,
    # back fixed to '← ပင်မ' by the section rule (path is /requests).
    # v59: ?kind=demand|direct|all picks a KIND view (the two ways a
    # purchase starts, never shown mixed without a label): the filtered
    # views list only their kind; plain /requests (no ?kind=) is the
    # 'all' view and keeps showing everything, each row kind-tagged.
    # The open view stays the shared receive landing across kinds —
    # its rows are kind-tagged too.
    open_only = request.args.get('open') == '1'
    kind = (request.args.get('kind') or '').strip()
    view_kind = kind if kind in ('demand', 'direct') else 'all'
    conds = []
    if open_only:
        conds.append("r.status IN ('open','partial')")
    if kind in ('demand', 'direct'):
        conds.append("r.kind='%s'" % kind)
    rows = d.execute("""
      SELECT r.*, u.name byname,
        (SELECT COALESCE(SUM(qty),0) FROM order_request_items WHERE or_id=r.id) tq,
        (SELECT COALESCE(SUM(received_qty),0) FROM order_request_items WHERE or_id=r.id) trc
      FROM order_requests r LEFT JOIN users u ON u.id=r.created_by
      """ + (("WHERE " + " AND ".join(conds)) if conds else "") + """
      ORDER BY r.id DESC""").fetchall()
    return render_template('requests.html', rows=rows, OR_STATUS=OR_STATUS,
                           open_only=open_only, view_kind=view_kind,
                           strip_active='recv' if open_only else 'req')

@app.route('/requests/new', methods=['GET', 'POST'])
@require_perm('requests.create')
def request_new():
    d = db()
    if request.method == 'POST':
        f = request.form
        supplier = canon_supplier(d, f.get('supplier', ''))
        if not supplier:
            flash(tr('Supplier နာမည် ထည့်ပါ')); return redirect(url_for('request_new'))
        # v59: the kind is chosen on the form (preselected from context,
        # always editable before saving) and never changes afterwards.
        kind = (f.get('kind') or 'demand').strip()
        if kind not in ('demand', 'direct'):
            kind = 'demand'
        # v63: gather the valid lines BEFORE creating — the old shape
        # created then deleted an empty request (and burnt its ORQ
        # number); an empty submit now just gets the same message.
        new_lines = []
        for pid_s, qty_s in zip(f.getlist('pid'), f.getlist('qty')):
            try:
                qty = float(qty_s or 0)
            except ValueError:
                continue
            if pid_s and qty > 0:
                new_lines.append((int(pid_s), qty))
        if not new_lines:
            flash(tr('ပစ္စည်း အနည်းဆုံး ၁ မျိုး ထည့်ပါ')); return redirect(url_for('request_new'))
        # v63: one-time form token, same guard as the buy-list form.
        ftok = (f.get('ftok') or '').strip()
        used = _used_form_token(d, ftok)
        if used is not None:
            _dup_token_flash(used)
            if used['or_id']:
                return redirect(url_for('request_detail', orid=used['or_id']))
            return redirect(url_for('request_new'))
        # v64 (Ko, 2026-10-23): same stop-and-ask as the buy list — a
        # slip of THIS kind for this supplier is already open. (The
        # other kind is never a candidate: the two ways never mix.)
        cands = _open_slips_same_kind(d, supplier, kind)
        if cands:
            return _render_merge_choice(d, 'manual', supplier, kind,
                                        f.get('note', '').strip(), '', new_lines, ftok, cands)
        orid, code = _create_request(d, supplier, kind, f.get('note', '').strip(), new_lines)
        if not _spend_form_token(d, ftok, orid):
            d.rollback()
            w = _used_form_token(d, ftok); _dup_token_flash(w)
            if w is not None and w['or_id']:
                return redirect(url_for('request_detail', orid=w['or_id']))
            return redirect(url_for('request_new'))
        d.commit(); flash(tr('{code} အမှာ တင်ပြီးပြီ', code=code))
        return redirect(url_for('request_detail', orid=orid))
    prods = d.execute("SELECT id, sku, name, unit FROM products WHERE active=1 ORDER BY sku").fetchall()
    sups = [r['supplier'] for r in d.execute("SELECT DISTINCT supplier FROM products WHERE supplier!='' ORDER BY supplier").fetchall()]
    # v59: preselect the kind from context — an explicit ?kind= (the kind
    # views' create buttons) beats the session's working view, which
    # beats the Demand default. The user can still switch it on the form.
    k = (request.args.get('kind') or '').strip()
    if k not in ('demand', 'direct'):
        k = session.get('req_kind')
    kind_sel = k if k in ('demand', 'direct') else 'demand'
    # v63: fresh one-time form token per page load (double-submit guard).
    return render_template('request_form.html', prods=prods, sups=sups, kind_sel=kind_sel,
                           ftok=uuid.uuid4().hex)

def _pin_req_kind(r):
    """v59: while a request is being worked on (detail / receive), the
    session's working kind follows THAT request's kind — so the strip
    tab, and the back-to-list target (see _trail_back), stay in the
    request's own kind view whichever list or page it was opened from."""
    k = 'demand'
    try:
        if r is not None and r['kind'] in ('demand', 'direct'):
            k = r['kind']
    except Exception:
        pass
    session['req_kind'] = k
    session['buy_group'] = k

def _load_or(orid):
    d = db()
    r = d.execute("SELECT r.*, u.name byname, cb.name cancelledname FROM order_requests r LEFT JOIN users u ON u.id=r.created_by LEFT JOIN users cb ON cb.id=r.cancelled_by WHERE r.id=?", (orid,)).fetchone()
    if not r: abort(404)
    items = d.execute("SELECT ri.*, p.sku, p.name, p.unit, p.base_price FROM order_request_items ri JOIN products p ON p.id=ri.product_id WHERE ri.or_id=?", (orid,)).fetchall()
    return r, items

@app.route('/requests/<int:orid>')
@require_perm('requests.view')
def request_detail(orid):
    r, items = _load_or(orid)
    _pin_req_kind(r)
    prods = db().execute("SELECT id, sku, name, unit FROM products WHERE active=1 ORDER BY sku").fetchall()
    editable = has_perm(g.user, 'requests.update') and r['status'] in ('open', 'partial')
    stl = find_settlement(db(), orid)
    return render_template('request_detail.html', r=r, items=items, prods=prods, OR_STATUS=OR_STATUS, editable=editable, stl=stl, STL_STATUS=STL_STATUS)

@app.route('/requests/<int:orid>/print')
@require_perm('requests.view')
def request_print(orid):
    r, items = _load_or(orid)
    total_qty = sum(i['qty'] for i in items)
    return render_template('request_print.html', r=r, items=items, total_qty=total_qty, fmt_money=fmt_money, biz=biz_info(db()))

@app.route('/requests/<int:orid>/update', methods=['POST'])
@require_perm('requests.update')
def request_update(orid):
    d = db(); f = request.form
    r = d.execute("SELECT * FROM order_requests WHERE id=?", (orid,)).fetchone()
    if not r or r['status'] not in ('open', 'partial'): abort(404)
    sup = canon_supplier(d, f.get('supplier', ''))
    if sup:
        d.execute("UPDATE order_requests SET supplier=? WHERE id=?", (sup, orid))
    d.execute("UPDATE order_requests SET note=? WHERE id=?", (f.get('note', '').strip(), orid))
    for it in d.execute("SELECT * FROM order_request_items WHERE or_id=?", (orid,)).fetchall():
        raw = f.get(f'qty_{it["id"]}')
        if raw is None: continue
        try:
            nq = float(raw)
        except ValueError:
            continue
        if nq < it['received_qty']:
            nq = it['received_qty']  # cannot go below what already arrived
        if nq != it['qty']:
            d.execute("UPDATE order_request_items SET qty=? WHERE id=?", (nq, it['id']))
    or_recalc_status(d, orid); d.commit(); flash(tr('အမှာ ပြင်ပြီးပြီ'))
    return redirect(url_for('request_detail', orid=orid))

@app.route('/requests/<int:orid>/add-line', methods=['POST'])
@require_perm('requests.add_line')
def request_add_line(orid):
    d = db(); f = request.form
    r = d.execute("SELECT * FROM order_requests WHERE id=?", (orid,)).fetchone()
    if not r or r['status'] not in ('open', 'partial'): abort(404)
    try:
        pid = int(f.get('pid') or 0); qty = float(f.get('qty') or 0)
    except ValueError:
        pid, qty = 0, 0
    if pid and qty > 0:
        ex = d.execute("SELECT * FROM order_request_items WHERE or_id=? AND product_id=?", (orid, pid)).fetchone()
        if ex:
            d.execute("UPDATE order_request_items SET qty=qty+? WHERE id=?", (qty, ex['id']))
        else:
            d.execute("INSERT INTO order_request_items(or_id,product_id,qty) VALUES(?,?,?)", (orid, pid, qty))
        or_recalc_status(d, orid); d.commit(); flash(tr('ပစ္စည်း ထပ်ထည့်ပြီးပြီ'))
    return redirect(url_for('request_detail', orid=orid))

@app.route('/requests/<int:orid>/remove-line/<int:iid>', methods=['POST'])
@require_perm('requests.remove_line')
def request_remove_line(orid, iid):
    d = db()
    it = d.execute("SELECT * FROM order_request_items WHERE id=? AND or_id=?", (iid, orid)).fetchone()
    if it and it['received_qty'] <= 0:
        d.execute("DELETE FROM order_request_items WHERE id=?", (iid,))
        or_recalc_status(d, orid); d.commit(); flash(tr('Line ဖြုတ်ပြီးပြီ'))
    else:
        flash(tr('ရောက်ပြီးသား line ကို ဖြုတ်လို့ မရဘူး'))
    return redirect(url_for('request_detail', orid=orid))

@app.route('/requests/<int:orid>/cancel', methods=['POST'])
@require_perm('requests.cancel')
def request_cancel(orid):
    d = db(); f = request.form
    r = d.execute("SELECT * FROM order_requests WHERE id=?", (orid,)).fetchone()
    if not r or r['status'] not in ('open', 'partial'): abort(404)
    reason = f.get('reason', '').strip()
    if not reason:
        flash(tr('ပယ်ဖျက်ရတဲ့ အကြောင်းပြချက် ရေးပါ')); return redirect(url_for('request_detail', orid=orid))
    d.execute("UPDATE order_requests SET status='cancelled', cancel_reason=?, cancelled_by=? WHERE id=?", (reason, g.user['id'], orid))
    d.commit(); flash(tr('အမှာ ပယ်ဖျက်ပြီးပြီ — ရောက်ပြီးသား အပိုင်း PUR အဖြစ် ကျန်နေမယ်'))
    return redirect(url_for('request_detail', orid=orid))

@app.route('/requests/<int:orid>/close', methods=['POST'])
@require_perm('requests.close')
def request_close(orid):
    d = db(); f = request.form
    r = d.execute("SELECT * FROM order_requests WHERE id=?", (orid,)).fetchone()
    if not r or r['status'] not in ('open', 'partial'): abort(404)
    note = f.get('note', '').strip()
    d.execute("UPDATE order_request_items SET qty=received_qty WHERE or_id=?", (orid,))
    extra = (' · ' + tr('ကျန်တာ ပိတ်ပြီး') + (f': {note}' if note else ''))
    d.execute("UPDATE order_requests SET status='done', note=COALESCE(note,'') || ? WHERE id=?", (extra, orid))
    d.commit(); flash(tr('အမှာ ပိတ်ပြီးပြီ — ကျန်တဲ့ အရေအတွက် ထပ် မစောင့်တော့ဘူး'))
    return redirect(url_for('request_detail', orid=orid))

@app.route('/requests/<int:orid>/receive', methods=['GET', 'POST'])
@require_perm('requests.receive')
def request_receive(orid):
    d = db()
    r, items = _load_or(orid)
    _pin_req_kind(r)
    if r['status'] not in ('open', 'partial'):
        flash(tr('ဒီ အမှာ က ကုန် သွင်းလို့ မရတော့ဘူး')); return redirect(url_for('request_detail', orid=orid))
    if request.method == 'POST':
        f = request.form
        # v61 (Ko-flagged): the receive form no longer pre-fills the
        # remaining quantity, so a blank line means "not this time" and
        # an all-blank submit is a plain mistake — it must NOT become a
        # purchase (before, an untouched submit recorded goods that
        # never arrived as if they had).
        _tot = 0.0
        for _qs in f.getlist('qty'):
            try:
                _q = float(_qs or 0)
            except ValueError:
                _q = 0
            if _q > 0:
                _tot += _q
        if _tot <= 0:
            flash(tr('ရောက်တဲ့ အရေအတွက် ထည့်ပါ — ဘာမှ မရောက်ရင် အမှာ ကို ပိတ်ပါ'))
            return redirect(url_for('request_receive', orid=orid))
        fx_cur, fx_amt, fx_rate = _fx_of(f)
        if fx_cur == 'ERR':
            flash(tr('ငွေကြေး ရွေး ထား ရင် လဲ နှုန်း ထည့် ပါ'))
            return redirect(url_for('request_receive', orid=orid))
        code = next_code('PUR', 'purchase_seq')
        d.execute("INSERT INTO purchases(code,supplier,invoice_no,pdate,created_by,note,created_at,or_id,extra_cost,extra_note,fx_currency,fx_amount,fx_rate) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)",
                  (code, r['supplier'], f.get('invoice_no', '').strip(), f.get('pdate') or today(),
                   g.user['id'], tr('{code} က ကုန်ရောက် သွင်းတာ', code=r['code']), now(), orid,
                   _extra_cost_of(f), (f.get('extra_note') or '').strip(), fx_cur, fx_amt, fx_rate))
        puid = d.execute("SELECT last_insert_rowid() i").fetchone()['i']
        added = 0
        _exps = f.getlist('exp')
        for _ix, (iid_s, qty_s, cost_s) in enumerate(zip(f.getlist('iid'), f.getlist('qty'), f.getlist('cost'))):
            it = d.execute("SELECT * FROM order_request_items WHERE id=? AND or_id=?", (int(iid_s), orid)).fetchone()
            if not it: continue
            try:
                qty = float(qty_s or 0); cost = float((cost_s or '0').replace(',', ''))
            except ValueError:
                continue
            remain = it['qty'] - it['received_qty']
            if qty > remain: qty = remain
            if qty > 0:
                exp = (_exps[_ix] if _ix < len(_exps) else '') or ''
                if not re.match(r'^\d{4}-\d{2}-\d{2}$', exp): exp = None
                d.execute("INSERT INTO purchase_items(purchase_id,product_id,qty,cost,expiry_date) VALUES(?,?,?,?,?)", (puid, it['product_id'], qty, cost, exp))
                move_stock(it['product_id'], qty, 'purchase', code, tr('စစ်ရန်ကျန်'), g.user['id'])
                d.execute("UPDATE order_request_items SET received_qty=received_qty+? WHERE id=?", (qty, it['id']))
                added += 1
        if not added:
            d.execute("DELETE FROM purchases WHERE id=?", (puid,)); d.commit()
            flash(tr('ရောက်တဲ့ အရေအတွက် ထည့်ပါ — ဘာမှ မရောက်ရင် အမှာ ကို ပိတ်ပါ')); return redirect(url_for('request_receive', orid=orid))
        or_recalc_status(d, orid); d.commit()
        flash(tr('{code} သွင်းပြီးပြီ — Finance စစ်ရန် ကျန်နေတယ်', code=code))
        return redirect(url_for('purchase_detail', puid=puid))
    lines = [i for i in items if i['qty'] - i['received_qty'] > 0]
    return render_template('request_receive.html', r=r, lines=lines)

@app.route('/purchases/<int:puid>/cancel', methods=['POST'])
@require_perm('purchases.cancel')
def purchase_cancel(puid):
    d = db(); f = request.form
    pu = d.execute("SELECT * FROM purchases WHERE id=?", (puid,)).fetchone()
    if not pu or pu['status'] != 'pending_check':
        flash(tr('စစ်ရန်ကျန် အဝယ်ပဲ ပယ်ဖျက်လို့ ရတယ်')); return redirect(url_for('purchase_detail', puid=puid))
    reason = f.get('reason', '').strip()
    if not reason:
        flash(tr('ပယ်ဖျက်ရတဲ့ အကြောင်းပြချက် ရေးပါ')); return redirect(url_for('purchase_detail', puid=puid))
    for it in d.execute("SELECT * FROM purchase_items WHERE purchase_id=?", (puid,)).fetchall():
        move_stock(it['product_id'], -it['qty'], 'purchase_cancel', pu['code'], reason, g.user['id'])
        if pu['or_id']:
            ri = d.execute("SELECT * FROM order_request_items WHERE or_id=? AND product_id=?", (pu['or_id'], it['product_id'])).fetchone()
            if ri:
                d.execute("UPDATE order_request_items SET received_qty=MAX(0, received_qty-?) WHERE id=?", (it['qty'], ri['id']))
    if pu['or_id']:
        or_recalc_status(d, pu['or_id'])
    d.execute("UPDATE purchases SET status='cancelled', cancel_reason=?, cancelled_by=? WHERE id=?", (reason, g.user['id'], puid))
    audit_log(d, g.user, 'pucancel',
              'အဝယ် ပယ်ဖျက် — %s (%s): အကြောင်း %s' % (pu['code'], pu['supplier'] or '', reason),
              'purchase', puid, 0)
    d.commit(); flash(tr('အဝယ် ပယ်ဖျက်ပြီးပြီ — stock ပြန် နှုတ်ပြီးပြီ'))
    return redirect(url_for('purchase_detail', puid=puid))

@app.route('/notifications')
@require_perm('notifications.view')
def notifications():
    d = db()
    rows = d.execute("""SELECT e.*, o.code, o.outlet, u.name byname,
                               a.gps_flag gps_flag, a.checked_at gps_checked_at, cu.name gps_checked_name
                        FROM order_edits e
                        JOIN orders o ON o.id=e.order_id LEFT JOIN users u ON u.id=e.by_user
                        LEFT JOIN delivery_attempts a ON a.id=e.attempt_id
                        LEFT JOIN users cu ON cu.id=a.checked_by
                        WHERE NOT (COALESCE(e.domain,'orders')='outlets' AND o.outlet_id IS NOT NULL)
                        ORDER BY e.id DESC LIMIT 100""").fetchall()
    mx = d.execute("SELECT COALESCE(MAX(id),0) m FROM order_edits").fetchone()['m']
    d.execute("UPDATE users SET last_edit_seen=? WHERE id=?", (mx, g.user['id'])); d.commit()
    g.pop('_cu_memo', None)   # v62: the bell count on this very page must see the row just updated
    # v51: sensitive audit notices ride in the same feed — Owner only.
    arows = audit_open_rows(d) if has_perm(g.user, 'audit.view') else []
    return render_template('notifications.html', rows=rows, alerts=open_alerts(d, g.user), audit_rows=arows)

AUDIT_KIND_LABELS = {'price': 'ဈေး ပြောင်းလဲမှု', 'stock': 'Stock ညှိ/ရေတွက်',
                     'cancel': 'ဘောက်ချာ ကိစ္စ', 'paydel': 'ဘောက်ချာ ကိစ္စ',
                     'edit': 'ဘောက်ချာ ကိစ္စ', 'pucancel': 'အဝယ် ကိစ္စ',
                     'expdel': 'အသုံးစရိတ် ကိစ္စ', 'settlepay': 'ရှင်းရန် ကိစ္စ'}
AUDIT_TYPE_KINDS = {'price': ('price',), 'stock': ('stock',),
                    'order': ('cancel', 'paydel', 'edit'),
                    'purchase': ('pucancel',), 'expense': ('expdel',),
                    'settlement': ('settlepay',)}
# v96: audit summary type words that follow the UI language at DISPLAY time
# (Ko, 2026-10-29). These prefixes are fixed system wording — NOT item names
# and NOT staff-typed text — so translating the prefix is safe; the rest of
# the summary (names, numbers, reasons, dates) stays verbatim as recorded,
# and stored summaries never change. Cancel/paydel reuse the app's existing
# English words for those concepts ('Cancel orders', 'Delete payments').
# AUDIT-SUM-START
AUDIT_SUMMARY_PREFIXES = ('ဈေး ပြောင်း', 'Stock ညှိ', 'Stock ရေတွက်',
                          'ဘောက်ချာ ပယ်ဖျက်', 'ငွေ မှတ်တမ်း ဖျက်')


def audit_summary_lang(summary, lang):
    """Translate a leading known type prefix; leave the rest verbatim.
    The ' — ' guard matters: 'ဈေး ပြောင်းလဲမှု' (a kind label) starts with
    'ဈေး ပြောင်း' but is NOT a summary prefix, so it must pass through."""
    if not summary:
        return summary
    for pre in AUDIT_SUMMARY_PREFIXES:
        if summary == pre or summary.startswith(pre + ' — '):
            return tr(pre, lang) + summary[len(pre):]
    return summary
# AUDIT-SUM-END
_PRICE_FIELD_NAMES = {'base_price': 'Base', 'retail': 'Retail', 'wholesale': 'Wholesale',
                      'b2c': 'B2C', 'actual_base': 'Actual'}

@app.route('/audit')
@require_perm('audit.view')
def audit_page():
    d = db()
    df = (request.args.get('from') or '').strip()
    dt = (request.args.get('to') or '').strip()
    if not re.match(r'^\d{4}-\d{2}-\d{2}$', df or ''): df = ''
    if not re.match(r'^\d{4}-\d{2}-\d{2}$', dt or ''): dt = ''
    try:
        pu = int(request.args.get('person') or 0)
    except ValueError:
        pu = 0
    tp = (request.args.get('type') or '').strip()
    if tp not in AUDIT_TYPE_KINDS: tp = ''
    kinds = AUDIT_TYPE_KINDS.get(tp) if tp else None
    def _keep(at, uid):
        day = (at or '')[:10]
        if df and day < df: return False
        if dt and day > dt: return False
        if pu and uid != pu: return False
        return True
    rows = []
    for a in d.execute("""SELECT a.*, u.name byname FROM audit_events a
                          LEFT JOIN users u ON u.id=a.user_id ORDER BY a.id DESC LIMIT 400""").fetchall():
        if kinds and a['kind'] not in kinds: continue
        if not _keep(a['created_at'], a['user_id']): continue
        rows.append({'at': a['created_at'], 'who': a['byname'] or '', 'kind': a['kind'],
                     'summary': a['summary'], 'src': 'audit'})
    if not tp or tp == 'price':
        for h in d.execute("""SELECT h.*, p.name pname, p.sku, u.name byname FROM price_history h
                              JOIN products p ON p.id=h.product_id LEFT JOIN users u ON u.id=h.by_user
                              ORDER BY h.id DESC LIMIT 400""").fetchall():
            if not _keep(h['created_at'], h['by_user']): continue
            rows.append({'at': h['created_at'], 'who': h['byname'] or '', 'kind': 'price', 'src': 'history',
                         'summary': 'ဈေး ပြောင်း — %s (%s): %s %s → %s' % (
                             h['pname'], h['sku'], _PRICE_FIELD_NAMES.get(h['field'], h['field']),
                             fmt_money(h['old_price']), fmt_money(h['new_price']))})
    if not tp or tp == 'order':
        for e in d.execute("""SELECT e.*, o.code, u.name byname FROM order_edits e
                              JOIN orders o ON o.id=e.order_id LEFT JOIN users u ON u.id=e.by_user
                              ORDER BY e.id DESC LIMIT 400""").fetchall():
            if not _keep(e['created_at'], e['by_user']): continue
            rows.append({'at': e['created_at'], 'who': e['byname'] or '', 'kind': 'edit', 'src': 'edits',
                         'summary': '%s — %s' % (e['code'], e['summary'] or '')})
    if not tp or tp == 'stock':
        for mv in d.execute("""SELECT mv.*, p.name pname, p.sku, u.name byname FROM stock_moves mv
                               JOIN products p ON p.id=mv.product_id LEFT JOIN users u ON u.id=mv.by_user
                               WHERE mv.kind IN ('adjust','count') ORDER BY mv.id DESC LIMIT 400""").fetchall():
            if not _keep(mv['created_at'], mv['by_user']): continue
            rows.append({'at': mv['created_at'], 'who': mv['byname'] or '', 'kind': 'stock', 'src': 'moves',
                         'summary': 'Stock %s — %s (%s): %+g%s' % (
                             'ညှိ' if mv['kind'] == 'adjust' else 'ရေတွက်', mv['pname'], mv['sku'],
                             mv['delta'] or 0, (' (' + mv['note'] + ')') if mv['note'] else '')})
    rows.sort(key=lambda r: (r['at'] or '', ), reverse=True)
    rows = rows[:300]
    people = d.execute("SELECT id, name FROM users ORDER BY name").fetchall()
    open_notis = [a for a in audit_open_rows(d) if not a['handled_by']]
    return render_template('audit.html', rows=rows, people=people, df=df, dt=dt, pu=pu, tp=tp,
                           labels=AUDIT_KIND_LABELS, open_notis=open_notis)

@app.route('/audit/<int:aid>/handled', methods=['POST'])
@require_perm('audit.handled')
def audit_handled(aid):
    """Judgment-type audit notice: the Owner marks it handled (who/when
    recorded); the bell count and the feed entry clear."""
    d = db()
    d.execute("UPDATE audit_events SET handled_by=?, handled_at=? WHERE id=? AND handled_by IS NULL",
              (g.user['id'], now(), aid))
    d.commit(); flash(tr('မှတ်ပြီးပြီ'))
    if (request.form.get('to') or '') == 'notifications':
        return redirect(url_for('notifications'))
    return redirect(url_for('audit_page'))

EDIT_CAP = 2   # per side (sales / warehouse) per invoice - Ko, 2026-10-14

def edit_counts(d, oid):
    rows = d.execute("SELECT by_role, COUNT(*) c FROM order_edits WHERE order_id=? GROUP BY by_role", (oid,)).fetchall()
    return {r['by_role']: r['c'] for r in rows}

# ---------------- Invoice edit (before delivery) ----------------
@app.route('/orders/<int:oid>/edit', methods=['GET', 'POST'])
@login_required()
def order_edit(oid):
    d = db()
    o = d.execute("SELECT o.*, u.name rep FROM orders o LEFT JOIN users u ON u.id=o.sales_user WHERE o.id=?", (oid,)).fetchone()
    if not o: abort(404)
    allowed = has_perm(g.user, 'products.create') or (has_perm(g.user, 'orders.edit') and o['sales_user'] == g.user['id'])
    if not allowed: abort(403)
    if o['status'] != 'new':
        flash(tr('မပို့ရသေးတဲ့ ဘောက်ချာပဲ ပြင်လို့ ရတယ်')); return redirect(url_for('order_detail', oid=oid))
    if o['paid'] and not has_perm(g.user, 'audit.view'):
        flash(tr('ငွေကောက်ခံပြီးသား ဘောက်ချာ — ပြင်လို့ မရတော့ပါ (Owner ကို ဆက်သွယ်ပါ)')); return redirect(url_for('order_detail', oid=oid))
    if not has_perm(g.user, 'products.create') and o['printed_at']:
        flash(tr('Print ထုတ်ပြီးသား ဘောက်ချာ — အရောင်း ပြင်ခွင့် မရှိတော့ပါ (Warehouse ကို ဆက်သွယ်ပါ)')); return redirect(url_for('order_detail', oid=oid))
    if not has_perm(g.user, 'products.create') and o['odate'] != today():
        flash(tr('ကောက်ခဲ့တဲ့ နေ့ ကျော်သွားပြီ — Sales ပြင်ခွင့် ပိတ်ပြီ (Warehouse ကို ဆက်သွယ်ပါ)')); return redirect(url_for('order_detail', oid=oid))
    if not has_perm(g.user, 'products.create') and not office_open():
        flash(tr('ရုံးချိန် (မနက် ၈:၃၀ – ညနေ ၅:၃၀) မဟုတ်တော့ပါ — Sales ပြင်ခွင့် ပိတ်ပြီ (Warehouse ကို ဆက်သွယ်ပါ)')); return redirect(url_for('order_detail', oid=oid))
    items = d.execute("SELECT oi.*, p.sku, p.name, p.unit, COALESCE(s.qty,0) stock FROM order_items oi JOIN products p ON p.id=oi.product_id LEFT JOIN stock s ON s.product_id=p.id WHERE oi.order_id=?", (oid,)).fetchall()
    items = [dict(it) for it in items]
    for it in items:   # v46: the "current stock" column shows the SKU's pool view
        it['stock'] = sku_stock(d, it['product_id'])
    side = user_role_name(g.user) if user_role_name(g.user) in ('sales', 'warehouse') else 'owner'  # v75: edit-cap side is a classification, not a gate
    counts = edit_counts(d, oid)
    if side in ('sales', 'warehouse') and counts.get(side, 0) >= EDIT_CAP:
        flash(tr('{who} ပြင်ခွင့် {cap} ကြိမ် ကုန်ပြီ — {contact}', who=('Sales' if side=='sales' else 'Warehouse'), cap=EDIT_CAP, contact=tr('Warehouse ကို ဆက်သွယ်ပါ' if side=='sales' else 'Owner ကို ဆက်သွယ်ပါ')))
        return redirect(url_for('order_detail', oid=oid))
    if request.method == 'POST':
        f = request.form
        plan = []   # (item, newqty)
        for it in items:
            raw = f.get(f'qty_{it["id"]}')
            if raw is None: continue
            try:
                nq = float(raw)
            except ValueError:
                nq = it['qty']
            plan.append((it, nq))
        keep = [1 for it, nq in plan if nq > 0]
        if not keep:
            flash(tr('ပစ္စည်း အနည်းဆုံး ၁ မျိုး ကျန်ရမယ် — တစ်ခုလုံး ဖျက်ချင်ရင် ပယ်ဖျက် သုံးပါ'))
            return redirect(url_for('order_edit', oid=oid))
        changes = []
        old_line_disc = 0.0
        for it, nq in plan:
            old_line_disc += (it['discount'] or 0)
            ndl = it['discount'] or 0
            raw_dc = f.get(f'disc_{it["id"]}')
            if raw_dc is not None:
                try: ndl = float(raw_dc)
                except ValueError: ndl = it['discount'] or 0
            ndl = max(0.0, min(ndl, nq * it['price']))
            if nq <= 0:
                d.execute("DELETE FROM order_items WHERE id=?", (it['id'],))
                changes.append(tr('{sku}: {q} → ဖြုတ်', sku=it['sku'], q=('%g' % it['qty'])))
            else:
                if nq != it['qty'] or ndl != (it['discount'] or 0):
                    d.execute("UPDATE order_items SET qty=?, discount=? WHERE id=?", (nq, ndl, it['id']))
                if nq != it['qty']:
                    changes.append(f"{it['sku']}: {('%g' % it['qty'])} → {('%g' % nq)}")
                if ndl != (it['discount'] or 0):
                    changes.append(tr('{sku} လျှော့ဈေး: {a} → {b}', sku=it['sku'], a=('%g' % (it['discount'] or 0)), b=('%g' % ndl)))
        new_line_disc = d.execute("SELECT COALESCE(SUM(discount),0) s FROM order_items WHERE order_id=?", (oid,)).fetchone()['s']
        if new_line_disc > 0 or abs(old_line_disc - (o['discount'] or 0)) < 0.01:
            target_disc = new_line_disc
        else:
            try: target_disc = float(f.get('discount') or 0)
            except ValueError: target_disc = o['discount']
        if target_disc != o['discount']:
            d.execute("UPDATE orders SET discount=? WHERE id=?", (target_disc, oid))
            changes.append(tr('လျှော့ဈေး စုစုပေါင်း: {a} → {b}', a=('%g' % o['discount']), b=('%g' % target_disc)))
        if changes:
            d.execute("INSERT INTO order_edits(order_id,by_user,by_role,summary,created_at) VALUES(?,?,?,?,?)",
                      (oid, g.user['id'], side, '; '.join(changes), now()))
            over = 0
            for it in d.execute("SELECT * FROM order_items WHERE order_id=?", (oid,)).fetchall():
                if line_overstock(d, it['product_id'], it['qty']): over = 1
            d.execute("UPDATE orders SET overstock=? WHERE id=?", (over, oid))
            d.commit(); flash(tr('ဘောက်ချာ ပြင်ပြီးပြီ — မှတ်တမ်း တင်ထားတယ်'))
        return redirect(url_for('order_detail', oid=oid))
    _ld = sum((i['discount'] or 0) for i in items)
    legacy_disc = (o['discount'] or 0) if abs(_ld - (o['discount'] or 0)) > 0.01 else 0
    return render_template('order_edit.html', o=o, items=items, fmt_money=fmt_money, counts=counts, side=side, EDIT_CAP=EDIT_CAP, legacy_disc=legacy_disc)



# ---------------- Supplier settlements (per batch: one Order Request, or one standalone PUR) ----------------
STL_STATUS = {'draft': 'ပြင်ဆင်နေ 📝', 'sent': 'Supplier ဆီ ပို့ပြီး 📤', 'confirmed': 'Supplier အတည်ပြု ✅', 'paid': 'ငွေရှင်းပြီး 💰'}

def batch_purchases(d, or_id=None, pu_id=None):
    if or_id:
        return d.execute("SELECT * FROM purchases WHERE or_id=? AND status!='cancelled' ORDER BY id", (or_id,)).fetchall()
    return d.execute("SELECT * FROM purchases WHERE id=? AND status!='cancelled'", (pu_id,)).fetchall()

def pur_total(d, puid):
    return d.execute("SELECT COALESCE(SUM(qty*cost),0) t FROM purchase_items WHERE purchase_id=?", (puid,)).fetchone()['t']

def batch_figures(d, or_id=None, pu_id=None):
    """Settlement figures on the ACTUAL-price basis (Ko, 2026-10-14).
    gross = sum over CHECKED purchases of qty x product Actual Base; bill = the same
    lines at recorded PUR (invoice) prices; pending purchases are listed separately;
    lines are aggregated per product; products without an Actual Base are 'missing'."""
    gross, bill, pending, lines_map = 0, 0, [], {}
    for pu in batch_purchases(d, or_id, pu_id):
        t = pur_total(d, pu['id'])
        if pu['status'] == 'pending_check':
            pending.append((pu, t)); continue
        if pu['status'] != 'checked':
            continue
        bill += t
        for it in d.execute("""SELECT pi.product_id, pi.qty, p.sku, p.name, p.unit, p.actual_base
                               FROM purchase_items pi JOIN products p ON p.id=pi.product_id
                               WHERE pi.purchase_id=?""", (pu['id'],)).fetchall():
            L = lines_map.setdefault(it['product_id'],
                {'product_id': it['product_id'], 'sku': it['sku'], 'name': it['name'],
                 'unit': it['unit'], 'qty': 0, 'actual': it['actual_base'], 'amount': None})
            L['qty'] += it['qty']
    lines, missing = [], []
    for L in lines_map.values():
        if L['actual']:
            L['amount'] = L['qty'] * L['actual']; gross += L['amount']
        else:
            missing.append(L['name'])
        lines.append(L)
    if or_id:
        rv = d.execute("SELECT COALESCE(SUM(qty*cost),0) v FROM supplier_returns WHERE or_id=?", (or_id,)).fetchone()['v']
    else:
        rv = d.execute("SELECT COALESCE(SUM(qty*cost),0) v FROM supplier_returns WHERE pu_id=?", (pu_id,)).fetchone()['v']
    return {'gross': gross, 'bill': bill, 'pending': pending, 'rv': rv, 'lines': lines, 'missing': missing}

def find_settlement(d, or_id=None, pu_id=None):
    if or_id:
        return d.execute("SELECT * FROM settlements WHERE or_id=?", (or_id,)).fetchone()
    return d.execute("SELECT * FROM settlements WHERE pu_id=?", (pu_id,)).fetchone()

@app.route('/settlements')
@require_perm('settlements.view')
def settlements_list():
    d = db()
    stls = d.execute("SELECT s.*, u.name byname FROM settlements s LEFT JOIN users u ON u.id=s.prepared_by ORDER BY s.id DESC").fetchall()
    settled_or = {s['or_id'] for s in stls if s['or_id'] and s['status'] != 'draft'}
    settled_pu = {s['pu_id'] for s in stls if s['pu_id'] and s['status'] != 'draft'}
    draft_or = {s['or_id']: s['id'] for s in stls if s['or_id'] and s['status'] == 'draft'}
    draft_pu = {s['pu_id']: s['id'] for s in stls if s['pu_id'] and s['status'] == 'draft'}
    # v82 (Ko, 2026-10-25): never-mixed tagging — every row carries its
    # buying kind. OR batches take order_requests.kind; standalone PURs
    # are Direct-path by construction (every Demand PUR carries or_id).
    _or_kind = {r['id']: (r['kind'] or 'demand') for r in d.execute("SELECT id, kind FROM order_requests").fetchall()}
    stls = [dict(s, buy_kind=(_or_kind.get(s['or_id'], 'demand') if s['or_id'] else 'direct')) for s in stls]
    batches = []
    for r in d.execute("""SELECT or_id, MAX(id) mx FROM purchases WHERE or_id IS NOT NULL AND status!='cancelled' GROUP BY or_id""").fetchall():
        orr = d.execute("SELECT * FROM order_requests WHERE id=?", (r['or_id'],)).fetchone()
        if orr['id'] in settled_or: continue
        fig = batch_figures(d, or_id=orr['id'])
        batches.append({'kind': 'or', 'id': orr['id'], 'code': orr['code'], 'supplier': orr['supplier'],
                        'gross': fig['gross'], 'pending': fig['pending'], 'returns': fig['rv'], 'missing': fig['missing'], 'draft_id': draft_or.get(orr['id']),
                        'buy_kind': (orr['kind'] or 'demand')})
    for pu in d.execute("SELECT * FROM purchases WHERE or_id IS NULL AND status='checked' ORDER BY id DESC").fetchall():
        if pu['id'] in settled_pu: continue
        batches.append({'kind': 'pu', 'id': pu['id'], 'code': pu['code'], 'supplier': pu['supplier'],
                        'gross': pur_total(d, pu['id']), 'pending': [], 'returns': 0, 'draft_id': draft_pu.get(pu['id']),
                        'buy_kind': 'direct'})
    # v83 (Ko, 2026-10-25): ORQ-NUMBER order, newest ORQ first. This
    # replaces the v64 freshest-purchase sort for this table only: the
    # old rule put ORQ-0015 above ORQ-0016 (0015's last receive was
    # entered later), which reads as unordered on screen because the
    # receive sequence is invisible there. OR batches sort by ORQ
    # number descending; standalone PUR batches have no ORQ number,
    # so they sit AFTER all OR batches, by PUR number descending.
    # History (STL id DESC) is untouched.
    def _code_num(code):
        try:
            return int(code.rsplit('-', 1)[1])
        except (ValueError, IndexError):
            return 0
    batches.sort(key=lambda b: (0 if b['kind'] == 'or' else 1, -_code_num(b['code'])))
    return render_template('settlements.html', stls=stls, batches=batches, STL_STATUS=STL_STATUS, fmt_money=fmt_money)

@app.route('/settlements/create', methods=['POST'])
@require_perm('settlements.create')
def settlement_create():
    d = db(); f = request.form
    or_id = int(f.get('or_id') or 0) or None
    pu_id = int(f.get('pu_id') or 0) or None
    if not or_id and not pu_id: abort(400)
    ex = find_settlement(d, or_id, pu_id)
    if ex:
        return redirect(url_for('settlement_detail', sid=ex['id']))
    if or_id:
        anchor = d.execute("SELECT * FROM order_requests WHERE id=?", (or_id,)).fetchone(); supplier = anchor['supplier']
    else:
        anchor = d.execute("SELECT * FROM purchases WHERE id=?", (pu_id,)).fetchone(); supplier = anchor['supplier']
    code = next_code('STL', 'stl_seq')
    d.execute("INSERT INTO settlements(code,or_id,pu_id,supplier,prepared_by,created_at) VALUES(?,?,?,?,?,?)",
              (code, or_id, pu_id, supplier, g.user['id'], now()))
    sid = d.execute("SELECT last_insert_rowid() i").fetchone()['i']
    d.commit(); flash(tr('{code} ရှင်းရန် စပြင်ပြီ', code=code))
    return redirect(url_for('settlement_detail', sid=sid))

def _load_settlement(sid):
    d = db()
    st = d.execute("SELECT s.*, u.name byname, pb.name paidname FROM settlements s LEFT JOIN users u ON u.id=s.prepared_by LEFT JOIN users pb ON pb.id=s.paid_by WHERE s.id=?", (sid,)).fetchone()
    if not st: abort(404)
    return st

@app.route('/settlements/<int:sid>')
@require_perm('settlements.view')
def settlement_detail(sid):
    d = db()
    st = _load_settlement(sid)
    purs = batch_purchases(d, st['or_id'], st['pu_id'])
    pur_rows = [(pu, pur_total(d, pu['id'])) for pu in purs]
    fig = None
    if st['figures_json'] and st['status'] != 'draft':
        try: fig = json.loads(st['figures_json'])
        except Exception: fig = None
    if fig is None:
        fig = batch_figures(d, st['or_id'], st['pu_id'])
    gross, pending, rv = fig['gross'], fig['pending'], fig['rv']
    if st['or_id']:
        rets = d.execute("SELECT sr.*, p.sku, p.name, p.unit FROM supplier_returns sr JOIN products p ON p.id=sr.product_id WHERE sr.or_id=? ORDER BY sr.id", (st['or_id'],)).fetchall()
        batch_items = d.execute("""SELECT DISTINCT pi.product_id, p.sku, p.name, pi.cost FROM purchase_items pi
                                   JOIN purchases pu ON pu.id=pi.purchase_id JOIN products p ON p.id=pi.product_id
                                   WHERE pu.or_id=? AND pu.status!='cancelled'""", (st['or_id'],)).fetchall()
        batch_code = d.execute("SELECT code FROM order_requests WHERE id=?", (st['or_id'],)).fetchone()['code']
    else:
        rets = d.execute("SELECT sr.*, p.sku, p.name, p.unit FROM supplier_returns sr JOIN products p ON p.id=sr.product_id WHERE sr.pu_id=? ORDER BY sr.id", (st['pu_id'],)).fetchall()
        batch_items = d.execute("""SELECT DISTINCT pi.product_id, p.sku, p.name, pi.cost FROM purchase_items pi
                                   JOIN products p ON p.id=pi.product_id WHERE pi.purchase_id=?""", (st['pu_id'],)).fetchall()
        batch_code = d.execute("SELECT code FROM purchases WHERE id=?", (st['pu_id'],)).fetchone()['code']
    payable = gross - rv + (st['adjust'] or 0)
    editable = has_perm(g.user, 'settlements.edit_draft') and st['status'] == 'draft'
    return render_template('settlement_detail.html', st=st, pur_rows=pur_rows, gross=gross, pending=pending,
                           rets=rets, rv=rv, payable=payable, batch_items=batch_items, batch_code=batch_code,
                           lines=fig['lines'], missing=fig.get('missing', []), bill=fig.get('bill', 0),
                           show_actual=show_actual(g.user),
                           STL_STATUS=STL_STATUS, fmt_money=fmt_money, editable=editable)

@app.route('/settlements/<int:sid>/returns/add', methods=['POST'])
@require_perm('settlements.edit_draft')
def supplier_return_add(sid):
    d = db(); f = request.form
    st = _load_settlement(sid)
    if st['status'] != 'draft':
        flash(tr('ရှင်းရန် ပို့ပြီးရင် ပြန်ပို့ ထပ်ထည့်လို့ မရဘူး')); return redirect(url_for('settlement_detail', sid=sid))
    try:
        pid = int(f.get('pid') or 0); qty = float(f.get('qty') or 0)
        cost_in = float((f.get('cost') or '0').replace(',', '')) if has_perm(g.user, 'actual.view') else 0
    except ValueError:
        pid, qty, cost_in = 0, 0, 0
    if not pid or qty <= 0:
        flash(tr('ပစ္စည်းနဲ့ အရေအတွက် မှန်အောင် ထည့်ပါ')); return redirect(url_for('settlement_detail', sid=sid))
    prod = d.execute("SELECT * FROM products WHERE id=?", (pid,)).fetchone()
    cost = cost_in or (prod['actual_base'] if prod else 0)
    if not cost:
        flash(tr('ဒီ ပစ္စည်းမှာ Actual ဈေး မဖြည့်ရသေးဘူး — Finance/Owner ဖြည့်ပြီးမှ ပြန်ပို့ မှတ်လို့ရမယ်')); return redirect(url_for('settlement_detail', sid=sid))
    code = next_code('SRT', 'srt_seq')
    d.execute("INSERT INTO supplier_returns(code,or_id,pu_id,supplier,product_id,qty,cost,reason,by_user,created_at) VALUES(?,?,?,?,?,?,?,?,?,?)",
              (code, st['or_id'], st['pu_id'], st['supplier'], pid, qty, cost, f.get('reason', '').strip(), g.user['id'], now()))
    move_stock(pid, -qty, 'supplier_return', code, f.get('reason', '').strip(), g.user['id'])
    d.commit(); flash(tr('{code} ပြန်ပို့ မှတ်ပြီ — stock နုတ်ပြီး အကြွေး လျော့မယ်', code=code))
    return redirect(url_for('settlement_detail', sid=sid))

@app.route('/settlements/<int:sid>/returns/<int:rid>/delete', methods=['POST'])
@require_perm('settlements.edit_draft')
def supplier_return_delete(sid, rid):
    d = db()
    st = _load_settlement(sid)
    if st['status'] != 'draft':
        flash(tr('ပို့ပြီးသား ရှင်းရန်မှာ ပြန်ပို့ ဖျက်လို့ မရဘူး')); return redirect(url_for('settlement_detail', sid=sid))
    r = d.execute("SELECT * FROM supplier_returns WHERE id=?", (rid,)).fetchone()
    if r:
        move_stock(r['product_id'], r['qty'], 'supplier_return_cancel', r['code'], tr('မှား မှတ်မိလို့ ပြန် ဖျက်'), g.user['id'])
        d.execute("DELETE FROM supplier_returns WHERE id=?", (rid,)); d.commit(); flash(tr('ပြန်ပို့ မှတ်တမ်း ဖျက်ပြီ — stock ပြန် ပေါင်းပြီ'))
    return redirect(url_for('settlement_detail', sid=sid))

@app.route('/settlements/<int:sid>/adjust', methods=['POST'])
@require_perm('settlements.edit_draft')
def settlement_adjust(sid):
    d = db(); f = request.form
    st = _load_settlement(sid)
    if st['status'] != 'draft':
        flash(tr('ပို့ပြီးသား ရှင်းရန်ကို ပြင်လို့ မရဘူး')); return redirect(url_for('settlement_detail', sid=sid))
    try:
        adj = float((f.get('adjust') or '0').replace(',', ''))
    except ValueError:
        adj = 0
    d.execute("UPDATE settlements SET adjust=?, adjust_note=? WHERE id=?", (adj, f.get('adjust_note', '').strip(), sid))
    d.commit(); flash(tr('ပြင်ဆင် ပမာဏ သိမ်းပြီ'))
    return redirect(url_for('settlement_detail', sid=sid))

@app.route('/settlements/<int:sid>/send', methods=['POST'])
@require_perm('settlements.send')
def settlement_send(sid):
    d = db()
    st = _load_settlement(sid)
    if st['status'] != 'draft': abort(400)
    fig = batch_figures(d, st['or_id'], st['pu_id'])
    if fig['pending']:
        flash(tr('စစ်ရန်ကျန် PUR ကျန်နေသေးတယ် — အရင် စစ် ပြီးမှ ပို့ပါ')); return redirect(url_for('settlement_detail', sid=sid))
    if fig['missing']:
        flash(tr('Actual ဈေး မဖြည့်ရသေးတဲ့ ပစ္စည်း ရှိတယ်: {names} — Finance/Owner ကို အကြောင်းကြားပါ', names=', '.join(fig['missing']))); return redirect(url_for('settlement_detail', sid=sid))
    final = fig['gross'] - fig['rv'] + (st['adjust'] or 0)
    snap = json.dumps({'lines': fig['lines'], 'gross': fig['gross'], 'rv': fig['rv'], 'bill': fig['bill'], 'pending': [], 'missing': []})
    d.execute("UPDATE settlements SET status='sent', final_amount=?, figures_json=?, sent_at=? WHERE id=?", (final, snap, now(), sid))
    d.commit(); flash(tr('ကျသင့် ပမာဏ {amt} Ks အဖြစ် အတည်ပြုပြီး Supplier ဆီ ပို့ပြီး အဖြစ် မှတ်ပြီ', amt=fmt_money(final)))
    return redirect(url_for('settlement_detail', sid=sid))

@app.route('/settlements/<int:sid>/confirm', methods=['POST'])
@require_perm('settlements.confirm')
def settlement_confirm(sid):
    d = db()
    st = _load_settlement(sid)
    if st['status'] != 'sent': abort(400)
    d.execute("UPDATE settlements SET status='confirmed', confirmed_at=? WHERE id=?", (now(), sid))
    d.commit(); flash(tr('Supplier အတည်ပြုပြီး အဖြစ် မှတ်ပြီ'))
    return redirect(url_for('settlement_detail', sid=sid))

@app.route('/settlements/<int:sid>/print')
@require_perm('settlements.view')
def settlement_print(sid):
    d = db()
    st = _load_settlement(sid)
    if st['or_id']:
        rets = d.execute("SELECT sr.*, p.name, p.unit FROM supplier_returns sr JOIN products p ON p.id=sr.product_id WHERE sr.or_id=? ORDER BY sr.id", (st['or_id'],)).fetchall()
        batch_code = d.execute("SELECT code FROM order_requests WHERE id=?", (st['or_id'],)).fetchone()['code']
    else:
        rets = d.execute("SELECT sr.*, p.name, p.unit FROM supplier_returns sr JOIN products p ON p.id=sr.product_id WHERE sr.pu_id=? ORDER BY sr.id", (st['pu_id'],)).fetchall()
        batch_code = d.execute("SELECT code FROM purchases WHERE id=?", (st['pu_id'],)).fetchone()['code']
    fig = None
    if st['figures_json'] and st['status'] != 'draft':
        try: fig = json.loads(st['figures_json'])
        except Exception: fig = None
    if fig is None:
        fig = batch_figures(d, st['or_id'], st['pu_id'])
    gross, rv = fig['gross'], fig['rv']
    final = st['final_amount'] if st['final_amount'] is not None else gross - rv + (st['adjust'] or 0)
    return render_template('settlement_print.html', st=st, lines=fig['lines'], rets=rets, gross=gross, rv=rv,
                           final=final, batch_code=batch_code, fmt_money=fmt_money, show_actual=show_actual(g.user), biz=biz_info(d))

@app.route('/settlements/<int:sid>/pay', methods=['POST'])
@require_perm('settlements.pay')
def settlement_pay(sid):
    d = db(); f = request.form
    st = _load_settlement(sid)
    if st['status'] != 'confirmed': abort(400)
    # v60: optional note breakdown of the cash handed over, Cash only —
    # a record on the settlement; the paid figures never read it.
    _pm = f.get('pay_method', '').strip()
    _pm_eff = _pm if _pm in PAY_METHOD_KEYS else 'cash'
    pay_denom, _dt = parse_denom(f, 'sp_') if _pm_eff == 'cash' else (None, 0)
    d.execute("UPDATE settlements SET status='paid', paid_at=?, pay_method=?, pay_ref=?, paid_by=?, pay_denom=? WHERE id=?",
              (f.get('paid_at') or today(), _pm, f.get('pay_ref', '').strip(), g.user['id'], pay_denom, sid))
    _amt = st['final_amount']
    if _amt is None:
        _fig = batch_figures(d, st['or_id'], st['pu_id'])
        _amt = _fig['gross'] - _fig['rv'] + (st['adjust'] or 0)
    audit_log(d, g.user, 'settlepay',
              'Supplier ရှင်း — %s (%s): %s Ks' % (st['code'], st['supplier'] or '', fmt_money(_amt)),
              'settlement', sid, 0)
    d.commit(); flash(tr('ငွေရှင်းပြီး အဖြစ် မှတ်ပြီ 💰'))
    return redirect(url_for('settlement_detail', sid=sid))


# ================= v49 combined release (Ko, 2026-10-20) =================
# ---------------- B1: money book ----------------
def money_group_totals(d, date_from=None, date_to=None):
    """Per money group {'in','out'} totals from every money source.
    IN  = checked (verified) invoice payments, on the day they were collected.
    OUT = supplier settlements once paid (final amount, on the paid date),
          expenses (on their own date), and manual money-book lines."""
    out = {k: {'in': 0.0, 'out': 0.0} for k in PAY_METHOD_KEYS}
    def rng(col):
        s = ''
        if date_from: s += f" AND {col}>=?"
        if date_to: s += f" AND {col}<=?"
        return s
    def args(*base):
        a = list(base)
        if date_from: a.append(date_from)
        if date_to: a.append(date_to)
        return a
    for r in d.execute("SELECT COALESCE(method,'cash') g, COALESCE(SUM(amount),0) v FROM order_payments WHERE COALESCE(verified,1)=1" + rng('rdate') + " GROUP BY COALESCE(method,'cash')", args()).fetchall():
        if r['g'] in out: out[r['g']]['in'] += r['v'] or 0
    for r in d.execute("SELECT COALESCE(pay_method,'cash') g, COALESCE(SUM(COALESCE(final_amount,0)),0) v FROM settlements WHERE status='paid'" + rng('substr(paid_at,1,10)') + " GROUP BY COALESCE(pay_method,'cash')", args()).fetchall():
        if r['g'] in out: out[r['g']]['out'] += r['v'] or 0
    for r in d.execute("SELECT COALESCE(method,'cash') g, COALESCE(SUM(amount),0) v FROM expenses WHERE 1=1" + rng('edate') + " GROUP BY COALESCE(method,'cash')", args()).fetchall():
        if r['g'] in out: out[r['g']]['out'] += r['v'] or 0
    for r in d.execute("SELECT grp g, direction dir, COALESCE(SUM(amount),0) v FROM money_entries WHERE 1=1" + rng('edate') + " GROUP BY grp, direction", args()).fetchall():
        if r['g'] in out: out[r['g']]['in' if r['dir'] == 'in' else 'out'] += r['v'] or 0
    return out

def money_opening(d, grp):
    try:
        return float(get_setting(d, 'money_open_' + grp) or 0)
    except (TypeError, ValueError):
        return 0.0

def money_balances(d, upto=None):
    """Balance per group at end of `upto` (default today): opening + in - out."""
    upto = upto or today()
    # v62: the bell's cash alert, the dashboard position card and the
    # money page can all ask for the same balances inside one page load.
    # GET requests never change money data, so reuse the first answer.
    memo = None
    try:
        if request.method == 'GET':
            memo = g.get('_money_bal_memo')
            if memo is None:
                memo = g._money_bal_memo = {}
    except RuntimeError:
        memo = None
    if memo is not None and upto in memo:
        return memo[upto]
    t = money_group_totals(d, None, upto)
    bal = {k: money_opening(d, k) + t[k]['in'] - t[k]['out'] for k in PAY_METHOD_KEYS}
    if memo is not None:
        memo[upto] = bal
    return bal

def money_position(d):
    """The owner's one-glance position: money in hand + stock value (at true
    cost) + what shops still owe - what suppliers are still owed."""
    bal = money_balances(d)
    stock_value = 0.0
    # v62: stock levels ride along in the same query (was one extra query
    # per product — ~355 on every dashboard load for owner/finance).
    for p in d.execute("SELECT p.id, p.pool_id, p.actual_base, COALESCE(s.qty,0) qty "
                       "FROM products p LEFT JOIN stock s ON s.product_id=p.id "
                       "WHERE p.active=1").fetchall():
        if p['pool_id'] and p['pool_id'] != p['id']:
            continue   # pack member — its pool is counted once, at the base
        stock_value += p['qty'] * (p['actual_base'] or 0)
    recv = 0.0
    # v62: what shops still owe, batched (was two queries per open invoice).
    unpaid_ids = [co['id'] for co in d.execute("SELECT id FROM orders WHERE status='delivered' AND paid=0").fetchall()]
    _tot = order_totals_map(d, unpaid_ids)
    _rec = order_received_map(d, unpaid_ids)
    for oid in unpaid_ids:
        b = _tot.get(oid, (0, 0))[0] - _rec.get(oid, 0)
        if b > 0.5: recv += b
    payables = 0.0
    for s in d.execute("SELECT * FROM settlements WHERE status IN ('sent','confirmed')").fetchall():
        if s['final_amount'] is not None:
            payables += s['final_amount']
        else:
            fig = batch_figures(d, s['or_id'], s['pu_id'])
            payables += fig['gross'] - fig['rv'] + (s['adjust'] or 0)
    return {'balances': bal, 'money_total': sum(bal.values()), 'stock_value': stock_value,
            'receivables': recv, 'payables': payables,
            'total': sum(bal.values()) + stock_value + recv - payables}

def money_day_lines(d, day):
    lines = []
    for r in d.execute("""SELECT p.amount, COALESCE(p.method,'cash') g, o.code, u.name byname
                          FROM order_payments p JOIN orders o ON o.id=p.order_id
                          LEFT JOIN users u ON u.id=p.by_user
                          WHERE COALESCE(p.verified,1)=1 AND p.rdate=? ORDER BY p.id""", (day,)).fetchall():
        lines.append({'grp': r['g'], 'dir': 'in', 'amount': r['amount'],
                      'label': tr('ဘောက်ချာ ငွေ') + ' ' + (r['code'] or ''), 'by': r['byname'] or '', 'kind': 'pay'})
    for r in d.execute("""SELECT s.final_amount amount, COALESCE(s.pay_method,'cash') g, s.code, u.name byname
                          FROM settlements s LEFT JOIN users u ON u.id=s.paid_by
                          WHERE s.status='paid' AND substr(s.paid_at,1,10)=? ORDER BY s.id""", (day,)).fetchall():
        lines.append({'grp': r['g'], 'dir': 'out', 'amount': r['amount'] or 0,
                      'label': tr('Supplier ရှင်း') + ' ' + (r['code'] or ''), 'by': r['byname'] or '', 'kind': 'stl'})
    for r in d.execute("""SELECT e.amount, COALESCE(e.method,'cash') g, e.category, e.detail, u.name byname
                          FROM expenses e LEFT JOIN users u ON u.id=e.by_user
                          WHERE e.edate=? ORDER BY e.id""", (day,)).fetchall():
        lines.append({'grp': r['g'], 'dir': 'out', 'amount': r['amount'],
                      'label': tr('အသုံးစရိတ်') + ' — ' + (r['category'] or '') + ((' · ' + r['detail']) if r['detail'] else ''), 'by': r['byname'] or '', 'kind': 'exp'})
    for r in d.execute("""SELECT m.*, u.name byname FROM money_entries m LEFT JOIN users u ON u.id=m.by_user
                          WHERE m.edate=? ORDER BY m.id""", (day,)).fetchall():
        lines.append({'grp': r['grp'], 'dir': r['direction'], 'amount': r['amount'],
                      'label': r['note'] or tr('လက်ရေးထည့် မှတ်တမ်း'), 'by': r['byname'] or '', 'kind': 'manual', 'id': r['id']})
    return lines

@app.route('/money')
@require_perm('money.view')
def money_page():
    d = db()
    day = (request.args.get('d') or today()).strip()
    if not re.match(r'^\d{4}-\d{2}-\d{2}$', day): day = today()
    totals_all = money_group_totals(d, None, day)
    totals_day = money_group_totals(d, day, day)
    groups = []
    for k, lbl in PAY_METHODS:
        groups.append({'key': k, 'label': lbl, 'opening': money_opening(d, k),
                       'day_in': totals_day[k]['in'], 'day_out': totals_day[k]['out'],
                       'balance': money_opening(d, k) + totals_all[k]['in'] - totals_all[k]['out']})
    counted = d.execute("SELECT * FROM cash_counts WHERE cdate=?", (day,)).fetchone()
    cash_bal = groups[0]['balance']
    return render_template('money.html', day=day, groups=groups, lines=money_day_lines(d, day),
                           transfers=money_transfer_lines(d, day),
                           position=money_position(d), counted=counted, cash_bal=cash_bal,
                           cash_diff=((counted['amount'] - cash_bal) if counted else None),
                           today_s=today(), fmt_money=fmt_money)

@app.route('/money/add', methods=['POST'])
@require_perm('money.manage')
def money_add():
    d = db(); f = request.form
    edate = (f.get('edate') or '').strip()
    if not re.match(r'^\d{4}-\d{2}-\d{2}$', edate or ''): edate = today()
    grp = (f.get('grp') or 'cash').strip()
    if grp not in PAY_METHOD_KEYS: grp = 'cash'
    direction = 'in' if f.get('direction') == 'in' else 'out'
    try: amt = float((f.get('amount') or '0').replace(',', ''))
    except Exception: amt = 0
    if amt <= 0:
        flash(tr('ပမာဏ (0 ထက် ကြီး) လိုပါတယ်'))
    else:
        d.execute("INSERT INTO money_entries(edate, grp, direction, amount, note, by_user, created_at) VALUES(?,?,?,?,?,?,?)",
                  (edate, grp, direction, amt, (f.get('note') or '').strip(), g.user['id'], now()))
        d.commit(); flash(tr('မှတ်ပြီးပြီ'))
    return redirect('/money?d=' + edate)

@app.route('/money/entry/<int:eid>/delete', methods=['POST'])
@require_perm('money.manage')
def money_entry_delete(eid):
    d = db()
    e = d.execute("SELECT edate FROM money_entries WHERE id=?", (eid,)).fetchone()
    if e:
        d.execute("DELETE FROM money_entries WHERE id=?", (eid,)); d.commit()
        flash(tr('ဖျက်လိုက်ပြီ'))
        return redirect('/money?d=' + (e['edate'] or today()))
    return redirect('/money')

@app.route('/money/opening', methods=['POST'])
@require_perm('money.opening_set')
def money_opening_set():
    d = db()
    for k in PAY_METHOD_KEYS:
        v = (request.form.get('open_' + k) or '').strip().replace(',', '')
        try:
            fv = float(v) if v else 0.0
        except ValueError:
            fv = 0.0
        set_setting(d, 'money_open_' + k, str(fv))
    d.commit(); flash(tr('သိမ်းပြီးပြီ'))
    return redirect('/money')

@app.route('/money/count', methods=['POST'])
@require_perm('money.manage')
def money_count():
    # v70: the /money cash-count close is SUPERSEDED by the Cashbox
    # close — one close flow only. Old cash_counts rows stay readable
    # on /money; no new ones are created here any more. Any old form
    # post lands on the Cashbox day page instead of silently writing.
    d = db(); f = request.form
    cdate = (f.get('cdate') or '').strip()
    if not re.match(r'^\d{4}-\d{2}-\d{2}$', cdate or ''): cdate = today()
    flash(tr('ငွေသား ပိတ် ကို Cashbox မှာ လုပ် ပါ'))
    return redirect('/cashbox/' + cdate)

# ---------------- B4: cashbox (Ko, 2026-10-25) ----------------
# ONE box, ONE receipt list, ONE issue list. The daily chain (agreed
# with Ko after his 'ခန' hold): Opening(day) = COUNTED closing of the
# latest counted/closed day before it (counted wins over book); the
# very first day's opening is a manual counted seed; month/year
# boundaries change nothing — only the view groups by month. A closed
# day is a frozen snapshot; corrections never rewrite it, they enter
# as adjustment entries on the current open day. Auto-feed is computed
# from the ledgers at read time, so a late ledger correction
# self-corrects on open days and only FLAGS on closed days.
def cashbox_auto(d, day):
    """(in, out) auto-fed cash for one day from the ledgers. Verified
    Cash order payments only — a pending delivery collection is NOT in
    the box until handover-verified (v65 rule). Digital/Banking never
    enter the box."""
    inn = d.execute("SELECT COALESCE(SUM(amount),0) v FROM order_payments WHERE COALESCE(verified,1)=1 AND COALESCE(method,'cash')='cash' AND rdate=?", (day,)).fetchone()['v'] or 0
    out = 0
    out += d.execute("SELECT COALESCE(SUM(amount),0) v FROM expenses WHERE COALESCE(method,'cash')='cash' AND edate=?", (day,)).fetchone()['v'] or 0
    out += d.execute("SELECT COALESCE(SUM(COALESCE(final_amount,0)),0) v FROM settlements WHERE status='paid' AND COALESCE(pay_method,'cash')='cash' AND substr(paid_at,1,10)=?", (day,)).fetchone()['v'] or 0
    return inn, out

def cashbox_manual(d, day):
    rows = d.execute("SELECT e.*, u.name byname FROM cashbox_entries e LEFT JOIN users u ON u.id=e.by_user WHERE e.edate=? ORDER BY e.id", (day,)).fetchall()
    inn = sum(r['amount'] or 0 for r in rows if r['direction'] == 'in')
    out = sum(r['amount'] or 0 for r in rows if r['direction'] == 'out')
    return rows, inn, out

def cashbox_auto_lines(d, day):
    """Itemised auto-feed lines with links to their source records.
    v71: each line also carries its denomination split ('denom' JSON,
    'denom_fmt' rendered) when one was stored. A payment's own split wins;
    a delivery collection's payment row has none, so it falls back to the
    collector's own delivery-attempt breakdown for the same order — the
    notes the collector actually counted at the doorstep. No split
    anywhere -> denom None ('ရွက်ရေ မမှတ်' in the view)."""
    lines = []
    for r in d.execute("""SELECT p.id, p.amount, p.denom, p.by_user, o.code, o.id oid, u.name byname FROM order_payments p
                          JOIN orders o ON o.id=p.order_id LEFT JOIN users u ON u.id=p.by_user
                          WHERE COALESCE(p.verified,1)=1 AND COALESCE(p.method,'cash')='cash' AND p.rdate=? ORDER BY p.id""", (day,)).fetchall():
        dj = r['denom']
        if not dj:
            att = d.execute("""SELECT denom FROM delivery_attempts WHERE order_id=? AND by_user=? AND result='delivered'
                               AND denom IS NOT NULL ORDER BY id DESC LIMIT 1""", (r['oid'], r['by_user'])).fetchone()
            dj = att['denom'] if att else None
        lines.append({'dir': 'in', 'amount': r['amount'], 'label': tr('ဘောက်ချာ ငွေ') + ' ' + (r['code'] or ''), 'by': r['byname'] or '', 'link': '/orders/%s' % r['oid'], 'src': 'pay', 'sid': r['id'],
                      'denom': dj, 'denom_fmt': fmt_denom(dj)})
    for r in d.execute("""SELECT e.id, e.amount, e.category, e.detail, e.denom, u.name byname FROM expenses e
                          LEFT JOIN users u ON u.id=e.by_user
                          WHERE COALESCE(e.method,'cash')='cash' AND e.edate=? ORDER BY e.id""", (day,)).fetchall():
        lines.append({'dir': 'out', 'amount': r['amount'], 'label': tr('အသုံးစရိတ်') + ' — ' + (r['category'] or '') + ((' · ' + r['detail']) if r['detail'] else ''), 'by': r['byname'] or '', 'link': '/expenses', 'src': 'exp', 'sid': r['id'],
                      'denom': r['denom'], 'denom_fmt': fmt_denom(r['denom'])})
    for r in d.execute("""SELECT s.id, s.final_amount amount, s.code, s.pay_denom, u.name byname FROM settlements s
                          LEFT JOIN users u ON u.id=s.paid_by
                          WHERE s.status='paid' AND COALESCE(s.pay_method,'cash')='cash' AND substr(s.paid_at,1,10)=? ORDER BY s.id""", (day,)).fetchall():
        lines.append({'dir': 'out', 'amount': r['amount'] or 0, 'label': tr('Supplier ရှင်း') + ' ' + (r['code'] or ''), 'by': r['byname'] or '', 'link': '/settlements/%s' % r['id'], 'src': 'stl', 'sid': r['id'],
                      'denom': r['pay_denom'], 'denom_fmt': fmt_denom(r['pay_denom'])})
    return lines

def cashbox_day_denoms(d, day):
    """v71: the day's denomination table (Ko's RLCA/ILCA shape). Per note
    value: opening leaves (prior counted day's counted split, else this
    day's seed split, else unknown), + receipt leaves and - issue leaves
    summed across the day's lines, = book leaves, counted leaves from the
    day's close count, difference per value. Lines without a stored split
    count in the MONEY totals but NOT in the leaf sums; their amounts are
    reported separately as unsplit, and the leaf figures are flagged
    partial whenever any unsplit amount exists or the opening split is
    unknown. Partial leaf figures are never presented as complete."""
    row = d.execute("SELECT * FROM cashbox_days WHERE cdate=?", (day,)).fetchone()
    prev = cashbox_last_counted(d, day)
    opening_counts = None
    if prev and prev['counted_denom']:
        opening_counts = denom_counts(prev['counted_denom'])
    elif row and row['opening_denom']:
        opening_counts = denom_counts(row['opening_denom'])
    opening_known = bool(opening_counts)
    in_counts, out_counts = {}, {}
    unsplit_in = unsplit_out = 0.0
    for l in cashbox_auto_lines(d, day):
        if l.get('denom'):
            tgt = in_counts if l['dir'] == 'in' else out_counts
            for k, v in denom_counts(l['denom']).items():
                tgt[k] = tgt.get(k, 0) + (int(v) or 0)
        elif l['dir'] == 'in':
            unsplit_in += l['amount'] or 0
        else:
            unsplit_out += l['amount'] or 0
    mrows, minn, mout = cashbox_manual(d, day)
    for e in mrows:
        if e['denom']:
            tgt = in_counts if e['direction'] == 'in' else out_counts
            for k, v in denom_counts(e['denom']).items():
                tgt[k] = tgt.get(k, 0) + (int(v) or 0)
        elif e['direction'] == 'in':
            unsplit_in += e['amount'] or 0
        else:
            unsplit_out += e['amount'] or 0
    counted_counts = denom_counts(row['counted_denom']) if row and row['counted_denom'] else None
    rows = []
    for v in DENOMS:
        op = (opening_counts.get(str(v), 0) if opening_known else None)
        i = in_counts.get(str(v), 0)
        o = out_counts.get(str(v), 0)
        book = (op + i - o) if op is not None else None
        cnt = (counted_counts.get(str(v), 0) if counted_counts is not None else None)
        diff = (cnt - book) if (cnt is not None and book is not None) else None
        rows.append({'value': v, 'opening': op, 'inn': i, 'out': o, 'book': book, 'counted': cnt, 'diff': diff})
    ain, aout = cashbox_auto(d, day)
    money_in, money_out = ain + minn, aout + mout
    denom_in_val = sum(v * in_counts.get(str(v), 0) for v in DENOMS)
    denom_out_val = sum(v * out_counts.get(str(v), 0) for v in DENOMS)
    return {'rows': rows, 'opening_known': opening_known,
            'unsplit_in': unsplit_in, 'unsplit_out': unsplit_out,
            'partial': (not opening_known) or unsplit_in > 0.5 or unsplit_out > 0.5,
            'money_in': money_in, 'money_out': money_out,
            'denom_in_val': denom_in_val, 'denom_out_val': denom_out_val,
            'bal_in_ok': abs((denom_in_val + unsplit_in) - money_in) < 0.5,
            'bal_out_ok': abs((denom_out_val + unsplit_out) - money_out) < 0.5}

def cashbox_last_counted(d, before):
    """Latest day before `before` that has a count (counted or closed)."""
    return d.execute("SELECT * FROM cashbox_days WHERE cdate<? AND status IN ('counted','closed') AND counted IS NOT NULL ORDER BY cdate DESC LIMIT 1", (before,)).fetchone()

def cashbox_day_figures(d, day, row=None):
    """Figures for a day. Closed days return the frozen snapshot; open
    days compute live (opening = latest counted closing before the day,
    else the stored seed, else None = seed still needed)."""
    row = row if row is not None else d.execute("SELECT * FROM cashbox_days WHERE cdate=?", (day,)).fetchone()
    if row and row['status'] == 'closed':
        ain, aout = cashbox_auto(d, day)
        _m, minn, mout = cashbox_manual(d, day)
        live_book = (row['opening'] or 0) + ain + minn - aout - mout
        return {'row': row, 'opening': row['opening'], 'book': row['book_closing'],
                'counted': row['counted'], 'diff': row['diff'], 'frozen': True,
                'changed_after_close': abs(live_book - (row['book_closing'] or 0)) > 0.5}
    prev = cashbox_last_counted(d, day)
    if prev:
        opening = prev['counted']
    elif row and row['opening'] is not None:
        opening = row['opening']
    else:
        opening = None
    ain, aout = cashbox_auto(d, day)
    _m, minn, mout = cashbox_manual(d, day)
    book = None if opening is None else opening + ain + minn - aout - mout
    counted = row['counted'] if row else None
    diff = (counted - book) if (counted is not None and book is not None) else None
    return {'row': row, 'opening': opening, 'book': book, 'counted': counted,
            'diff': diff, 'frozen': False, 'changed_after_close': False}

def cashbox_bundle_split(counts):
    """Template logic: leaves -> (full 100-leaf bundles, loose leaves)
    per denomination, plus the balancing total (bundles*100 + loose)."""
    out = []
    for v in DENOMS:
        leaves = int((counts or {}).get(str(v), 0) or 0)
        out.append({'value': v, 'leaves': leaves, 'bundles': leaves // 100, 'loose': leaves % 100})
    return out

def cashbox_get_or_create(d, day):
    row = d.execute("SELECT * FROM cashbox_days WHERE cdate=?", (day,)).fetchone()
    if not row:
        d.execute("INSERT INTO cashbox_days(cdate, status, created_at) VALUES(?,?,?)", (day, 'open', now()))
        d.commit()
        row = d.execute("SELECT * FROM cashbox_days WHERE cdate=?", (day,)).fetchone()
    return row

def cashbox_month(d, ym):
    """Month view: day rows in the month, in/out totals, month-end
    balance = counted (else book) closing of the latest counted/closed
    day in the month, and cash-active days that have no box day yet."""
    rows = d.execute("SELECT * FROM cashbox_days WHERE substr(cdate,1,7)=? ORDER BY cdate", (ym,)).fetchall()
    tin = tout = 0.0
    for r in rows:
        ain, aout = cashbox_auto(d, r['cdate'])
        _m, minn, mout = cashbox_manual(d, r['cdate'])
        tin += ain + minn; tout += aout + mout
    end_bal = None
    for r in reversed(rows):
        if r['status'] in ('counted', 'closed'):
            fg = cashbox_day_figures(d, r['cdate'], r)
            end_bal = fg['counted'] if fg['counted'] is not None else fg['book']
            break
    active = set()
    for (dt,) in d.execute("SELECT DISTINCT rdate FROM order_payments WHERE COALESCE(verified,1)=1 AND COALESCE(method,'cash')='cash' AND substr(rdate,1,7)=?", (ym,)).fetchall():
        if dt: active.add(dt)
    for (dt,) in d.execute("SELECT DISTINCT edate FROM expenses WHERE COALESCE(method,'cash')='cash' AND substr(edate,1,7)=?", (ym,)).fetchall():
        if dt: active.add(dt)
    for (dt,) in d.execute("SELECT DISTINCT substr(paid_at,1,10) FROM settlements WHERE status='paid' AND COALESCE(pay_method,'cash')='cash' AND substr(paid_at,1,7)=?", (ym,)).fetchall():
        if dt: active.add(dt)
    have = {r['cdate'] for r in rows}
    missing = sorted(dt for dt in active if dt not in have)
    return {'rows': rows, 'total_in': tin, 'total_out': tout, 'end_bal': end_bal, 'missing': missing}

def money_transfer_lines(d, day):
    """Cashbox top-ups for the day, as transfer-in lines for the
    Digital/Banking books. Display only — deliberately NOT part of
    money_group_totals (not business income, and /profit is untouched)."""
    lines = []
    for r in d.execute("""SELECT e.*, u.name byname FROM cashbox_entries e LEFT JOIN users u ON u.id=e.by_user
                          WHERE e.kind='topup' AND e.edate=? ORDER BY e.id""", (day,)).fetchall():
        lines.append({'grp': r['dest'] or 'digital', 'dir': 'in', 'amount': r['amount'],
                      'label': tr('လွှဲပြောင်း ဝင် (Cashbox ဖြည့်)') + ((' — ' + (r['note'] or '')) if r['note'] else ''),
                      'by': r['byname'] or '', 'kind': 'topup'})
    return lines

@app.route('/cashbox')
@require_perm('cashbox.view')
def cashbox_list():
    d = db()
    m = (request.args.get('m') or today()[:7]).strip()
    if not re.match(r'^\d{4}-\d{2}$', m): m = today()[:7]
    summ = cashbox_month(d, m)
    # gaps, plainly: between consecutive rows (newest first), a skipped
    # stretch where the box was not opened at all.
    disp = []
    prev = None
    for r in reversed(summ['rows']):
        gap = None
        if prev is not None:
            try:
                d0 = datetime.date.fromisoformat(r['cdate']); d1 = datetime.date.fromisoformat(prev)
                if (d1 - d0).days > 1: gap = (d1 - d0).days - 1
            except ValueError:
                gap = None
        disp.append({'row': r, 'gap': gap})
        prev = r['cdate']
    return render_template('cashbox.html', m=m, summ=summ, disp=disp, today_s=today(), fmt_money=fmt_money)

@app.route('/cashbox/<day>')
@require_perm('cashbox.view')
def cashbox_day(day):
    d = db()
    if not re.match(r'^\d{4}-\d{2}-\d{2}$', day): day = today()
    row = d.execute("SELECT * FROM cashbox_days WHERE cdate=?", (day,)).fetchone()
    fg = cashbox_day_figures(d, day, row)
    byname = {}
    for u in d.execute("SELECT id, name FROM users").fetchall():
        byname[u['id']] = u['name']
    mrows = [dict(e, denom_fmt=fmt_denom(e['denom']) if e['denom'] else '') for e in cashbox_manual(d, day)[0]]
    return render_template('cashbox_day.html', day=day, fg=fg, row=row,
                           auto_lines=cashbox_auto_lines(d, day),
                           manual_rows=mrows,
                           denoms=cashbox_day_denoms(d, day),
                           counted_counts=denom_counts(row['counted_denom']) if row and row['counted_denom'] else {},
                           bundle=cashbox_bundle_split(denom_counts(row['counted_denom']) if row and row['counted_denom'] else {}),
                           byname=byname, today_s=today(), fmt_money=fmt_money)

@app.route('/cashbox/<day>/seed', methods=['POST'])
@require_perm('cashbox.manage')
def cashbox_seed(day):
    d = db(); f = request.form
    if not re.match(r'^\d{4}-\d{2}-\d{2}$', day): day = today()
    row = cashbox_get_or_create(d, day)
    if row['status'] == 'closed':
        flash(tr('ပိတ်ပြီးသား နေ့ ကို ပြင် လို့ မရဘူး')); return redirect('/cashbox/' + day)
    if cashbox_last_counted(d, day):
        flash(tr('အရင် နေ့ အပိတ် ကနေ အလို ဆက်')); return redirect('/cashbox/' + day)
    denom_json, denom_tot = parse_denom(f, 'cb_')
    if denom_json is not None:
        amt = denom_tot
    else:
        try: amt = float((f.get('amount') or '0').replace(',', ''))
        except Exception: amt = 0
    if amt < 0:
        flash(tr('ပမာဏ (0 ထက် ကြီး) လိုပါတယ်')); return redirect('/cashbox/' + day)
    # v71: the seed counter's split was computed but discarded — store it
    # as the day's opening split so the denomination table can open from it.
    d.execute("UPDATE cashbox_days SET opening=?, opening_denom=? WHERE cdate=?", (amt, denom_json, day))
    d.commit(); flash(tr('မှတ်ပြီးပြီ'))
    return redirect('/cashbox/' + day)

@app.route('/cashbox/<day>/entry', methods=['POST'])
@require_perm('cashbox.manage')
def cashbox_entry(day):
    d = db(); f = request.form
    if not re.match(r'^\d{4}-\d{2}-\d{2}$', day): day = today()
    row = cashbox_get_or_create(d, day)
    if row['status'] == 'closed':
        flash(tr('ပိတ်ပြီးသား နေ့ ကို ပြင် လို့ မရဘူး') + ' — ' + tr('ပြင်ဆင် မှတ်တမ်း ကို ဖွင့် ထား တဲ့ နေ့ မှာ ထည့်ပါ'))
        return redirect('/cashbox/' + day)
    kind = (f.get('kind') or 'manual').strip()
    if kind not in ('manual', 'topup', 'adjust'): kind = 'manual'
    direction = 'in' if f.get('direction') == 'in' else 'out'
    dest = None
    if kind == 'topup':
        direction = 'out'
        dest = (f.get('dest') or 'digital').strip()
        if dest not in ('digital', 'bank'): dest = 'digital'
    try: amt = float((f.get('amount') or '0').replace(',', ''))
    except Exception: amt = 0
    if amt <= 0:
        flash(tr('ပမာဏ (0 ထက် ကြီး) လိုပါတယ်')); return redirect('/cashbox/' + day)
    note = (f.get('note') or '').strip()
    if kind == 'manual' and direction == 'out' and not note:
        pass  # plain note optional for ordinary manual lines; topups/adjusts carry their kind label
    # v71: optional note breakdown on a manual entry (the box is cash, so
    # the counter is always offered; a skipped counter stores NULL).
    entry_denom_json, _dt = parse_denom(f, 'cb_e_')
    d.execute("INSERT INTO cashbox_entries(edate, kind, direction, dest, amount, note, by_user, created_at, denom) VALUES(?,?,?,?,?,?,?,?,?)",
              (day, kind, direction, dest, amt, note, g.user['id'], now(), entry_denom_json))
    audit_log(d, g.user, 'cashbox', 'Cashbox မှတ်တမ်း — %s %s: %s Ks' % (day, kind, fmt_money(amt)), 'cashbox', None, 0)
    d.commit(); flash(tr('မှတ်ပြီးပြီ'))
    return redirect('/cashbox/' + day)

@app.route('/cashbox/entry/<int:eid>/delete', methods=['POST'])
@require_perm('cashbox.manage')
def cashbox_entry_delete(eid):
    d = db()
    e = d.execute("SELECT * FROM cashbox_entries WHERE id=?", (eid,)).fetchone()
    if not e:
        return redirect('/cashbox')
    day = e['edate']
    row = d.execute("SELECT status FROM cashbox_days WHERE cdate=?", (day,)).fetchone()
    if row and row['status'] == 'closed':
        flash(tr('ပိတ်ပြီးသား နေ့ ကို ပြင် လို့ မရဘူး')); return redirect('/cashbox/' + day)
    d.execute("DELETE FROM cashbox_entries WHERE id=?", (eid,))
    audit_log(d, g.user, 'cashbox', 'Cashbox မှတ်တမ်း ဖျက် — %s: %s Ks' % (day, fmt_money(e['amount'] or 0)), 'cashbox', None, 0)
    d.commit(); flash(tr('ဖျက်လိုက်ပြီ'))
    return redirect('/cashbox/' + day)

@app.route('/cashbox/<day>/count', methods=['POST'])
@require_perm('cashbox.manage')
def cashbox_count(day):
    d = db(); f = request.form
    if not re.match(r'^\d{4}-\d{2}-\d{2}$', day): day = today()
    row = cashbox_get_or_create(d, day)
    if row['status'] == 'closed':
        flash(tr('ပိတ်ပြီးသား နေ့ ကို ပြင် လို့ မရဘူး')); return redirect('/cashbox/' + day)
    fg = cashbox_day_figures(d, day, row)
    if fg['opening'] is None:
        flash(tr('အဖွင့် အရင် ထည့်ပါ')); return redirect('/cashbox/' + day)
    denom_json, denom_tot = parse_denom(f, 'cb_')
    if denom_json is not None:
        amt = denom_tot
    else:
        try: amt = float((f.get('amount') or '0').replace(',', ''))
        except Exception: amt = 0
    reason = (f.get('diff_reason') or '').strip()
    diff = amt - (fg['book'] or 0)
    if abs(diff) > 0.5 and not reason:
        flash(tr('ကွာခြားချက် ရှိ ရင် အကြောင်း ရေး မှ ပိတ် လို့ ရမယ်')); return redirect('/cashbox/' + day)
    d.execute("UPDATE cashbox_days SET counted=?, counted_denom=?, counted_by=?, diff=?, diff_reason=?, status='counted' WHERE cdate=?",
              (amt, denom_json, g.user['id'], diff, reason, day))
    d.commit(); flash(tr('ရေတွက် ပြီးပြီ — အတည်ပြု စောင့် နေ တယ်'))
    return redirect('/cashbox/' + day)

@app.route('/cashbox/<day>/verify', methods=['POST'])
@require_perm('cashbox.verify')
def cashbox_verify(day):
    d = db()
    if not re.match(r'^\d{4}-\d{2}-\d{2}$', day): day = today()
    row = d.execute("SELECT * FROM cashbox_days WHERE cdate=?", (day,)).fetchone()
    if not row or row['status'] != 'counted':
        flash(tr('ရေတွက် ပြီးမှ အတည်ပြု လို့ ရမယ်')); return redirect('/cashbox/' + day)
    if row['counted_by'] == g.user['id']:
        flash(tr('ရေတွက် သူ နဲ့ အတည်ပြု သူ မတူ ရမယ်')); return redirect('/cashbox/' + day)
    fg = cashbox_day_figures(d, day, row)
    d.execute("UPDATE cashbox_days SET status='closed', verified_by=?, verified_at=?, opening=?, book_closing=?, diff=? WHERE cdate=?",
              (g.user['id'], now(), fg['opening'], fg['book'], fg['diff'], day))
    audit_log(d, g.user, 'cashbox', 'Cashbox ပိတ် — %s: ရေတွက် %s Ks' % (day, fmt_money(row['counted'] or 0)), 'cashbox', None, 0)
    d.commit(); flash(tr('ပိတ်ပြီးပြီ ✅'))
    return redirect('/cashbox/' + day)

# ---------------- v72: retroactive denomination backfill ----------------
# Ko, 2026-10-25 ('ရုံးက ပြန်ထည့်ပေးလိုက်လို့ရနိုင်လား?'): when a Cash line
# was recorded without a note breakdown, the OFFICE fills it in afterwards
# from the Cashbox day view — or corrects a wrong one the same way. The
# split total must equal the recorded amount EXACTLY, or nothing is saved:
# a backfill happens in the office with no time pressure, unlike v60's
# warn-only doorstep counter, so exactness is required here. Money figures
# are never touched — a split is evidence only — so this works on CLOSED
# days too: the closed day's money snapshot (opening/book/counted/diff)
# does not move, only the day's leaf-breakdown display recomputes from
# the splits. Every backfill is logged (who/when, old -> new).
# For a delivery-collection payment the split lives on the PAYMENT row
# (the office-side money record); when the collector's delivered attempt
# for that order has NO split, the same split fills that gap too — a
# recorded doorstep count is never overwritten.
def _cashbox_denom_source(d, src, sid):
    """Resolve a Cashbox line to its source record. Returns
    (day, amount, old_json, label, table, column) or None when the
    source is not a Cashbox Cash line."""
    if src == 'pay':
        r = d.execute("""SELECT p.*, o.code FROM order_payments p JOIN orders o ON o.id=p.order_id
                         WHERE p.id=? AND COALESCE(p.verified,1)=1 AND COALESCE(p.method,'cash')='cash'""", (sid,)).fetchone()
        if not r: return None
        return (r['rdate'], r['amount'] or 0, r['denom'], tr('ဘောက်ချာ ငွေ') + ' ' + (r['code'] or ''), 'order_payments', 'denom')
    if src == 'exp':
        r = d.execute("SELECT * FROM expenses WHERE id=? AND COALESCE(method,'cash')='cash'", (sid,)).fetchone()
        if not r: return None
        return (r['edate'], r['amount'] or 0, r['denom'], tr('အသုံးစရိတ်') + ' — ' + (r['category'] or ''), 'expenses', 'denom')
    if src == 'stl':
        r = d.execute("SELECT * FROM settlements WHERE id=? AND status='paid' AND COALESCE(pay_method,'cash')='cash'", (sid,)).fetchone()
        if not r: return None
        return ((r['paid_at'] or '')[:10], r['final_amount'] or 0, r['pay_denom'], tr('Supplier ရှင်း') + ' ' + (r['code'] or ''), 'settlements', 'pay_denom')
    if src == 'entry':
        r = d.execute("SELECT * FROM cashbox_entries WHERE id=?", (sid,)).fetchone()
        if not r: return None
        return (r['edate'], r['amount'] or 0, r['denom'], tr('လက် နဲ့ မှတ်တမ်း') + ((' — ' + r['note']) if r['note'] else ''), 'cashbox_entries', 'denom')
    return None

@app.route('/cashbox/denom/<src>/<int:sid>', methods=['GET', 'POST'])
@require_perm('cashbox.manage')
def cashbox_denom_fill(src, sid):
    d = db()
    got = _cashbox_denom_source(d, src, sid)
    if not got: abort(404)
    day, amount, old_json, label, table, col = got
    if request.method == 'POST':
        f = request.form
        denom_json, denom_tot = parse_denom(f, 'df_')
        if denom_json is None or abs(denom_tot - amount) > 0.5:
            flash(tr('အမျိုးအစား ပေါင်းလဒ် ပမာဏ နဲ့ ကိုက်မှ သိမ်း လို့ ရမယ်'))
            return redirect('/cashbox/denom/%s/%d' % (src, sid))
        d.execute("UPDATE %s SET %s=? WHERE id=?" % (table, col), (denom_json, sid))
        att_note = ''
        if src == 'pay':
            prow = d.execute("SELECT order_id, by_user FROM order_payments WHERE id=?", (sid,)).fetchone()
            if prow:
                att = d.execute("""SELECT id FROM delivery_attempts WHERE order_id=? AND by_user=? AND result='delivered'
                                   AND denom IS NULL ORDER BY id DESC LIMIT 1""", (prow['order_id'], prow['by_user'])).fetchone()
                if att:
                    d.execute("UPDATE delivery_attempts SET denom=? WHERE id=?", (denom_json, att['id']))
                    att_note = ' (Delivery မှတ်တမ်း ပါ ဖြည့်)'
        audit_log(d, g.user, 'cashbox', 'ရွက်ရေ ဖြည့်/ပြင် — %s %s: %s → %s%s' % (day, label, fmt_denom(old_json) or tr('ရွက်ရေ မမှတ်'), fmt_denom(denom_json), att_note), 'cashbox', None, 0)
        d.commit(); flash(tr('မှတ်ပြီးပြီ'))
        return redirect('/cashbox/' + day)
    return render_template('cashbox_denom.html', src=src, sid=sid, day=day, label=label,
                           amount=amount, old_fmt=fmt_denom(old_json),
                           old_counts=denom_counts(old_json), fmt_money=fmt_money)

# ---------------- B3: physical stock count ----------------
def _pool_bases(d):
    """Products that hold a pool balance (pack members are counted through
    their base product, in the smallest unit)."""
    return [p for p in d.execute("SELECT * FROM products WHERE active=1 ORDER BY sku").fetchall()
            if not (p['pool_id'] and p['pool_id'] != p['id'])]

@app.route('/stock-count')
@require_perm('stock_count.view')
def stock_count_list():
    d = db()
    rows = d.execute("""SELECT c.*, u.name byname, a.name aname,
                        (SELECT COUNT(*) FROM stock_count_lines l WHERE l.count_id=c.id AND l.counted IS NOT NULL) done_n,
                        (SELECT COUNT(*) FROM stock_count_lines l WHERE l.count_id=c.id) tot_n
                        FROM stock_counts c LEFT JOIN users u ON u.id=c.created_by
                        LEFT JOIN users a ON a.id=c.approved_by ORDER BY c.id DESC""").fetchall()
    return render_template('stock_count.html', rows=rows, today_s=today(), fmt_money=fmt_money)

@app.route('/stock-count/create', methods=['POST'])
@require_perm('stock_count.create')
def stock_count_create():
    d = db()
    d.execute("INSERT INTO stock_counts(cdate, created_by, created_at) VALUES(?,?,?)", (today(), g.user['id'], now()))
    cid = d.execute("SELECT last_insert_rowid() i").fetchone()['i']
    for p in _pool_bases(d):
        d.execute("INSERT INTO stock_count_lines(count_id, product_id) VALUES(?,?)", (cid, p['id']))
    d.commit()
    return redirect(url_for('stock_count_detail', cid=cid))

@app.route('/stock-count/<int:cid>')
@require_perm('stock_count.view')
def stock_count_detail(cid):
    d = db()
    c = d.execute("SELECT c.*, u.name byname, a.name aname FROM stock_counts c LEFT JOIN users u ON u.id=c.created_by LEFT JOIN users a ON a.id=c.approved_by WHERE c.id=?", (cid,)).fetchone()
    if not c: abort(404)
    lines = []
    for l in d.execute("""SELECT l.*, p.sku, p.name, p.unit FROM stock_count_lines l
                          JOIN products p ON p.id=l.product_id WHERE l.count_id=? ORDER BY p.sku""", (cid,)).fetchall():
        book = pool_stock(d, l['product_id'])
        members = d.execute("SELECT sku, name, unit FROM products WHERE pool_id=? AND id!=? ORDER BY sku", (l['product_id'], l['product_id'])).fetchall()
        lines.append({'l': l, 'book': book, 'members': members,
                      'var': (l['counted'] - book) if l['counted'] is not None else None})
    can_edit = c['status'] == 'draft' and has_perm(g.user, 'stock_count.save')
    return render_template('stock_count_detail.html', c=c, lines=lines, can_edit=can_edit, fmt_money=fmt_money)

@app.route('/stock-count/<int:cid>/save', methods=['POST'])
@require_perm('stock_count.save')
def stock_count_save(cid):
    d = db()
    c = d.execute("SELECT * FROM stock_counts WHERE id=?", (cid,)).fetchone()
    if not c or c['status'] != 'draft': abort(400)
    for l in d.execute("SELECT id FROM stock_count_lines WHERE count_id=?", (cid,)).fetchall():
        v = (request.form.get('cnt_%d' % l['id']) or '').strip()
        if v == '':
            d.execute("UPDATE stock_count_lines SET counted=NULL WHERE id=?", (l['id'],))
        else:
            try:
                d.execute("UPDATE stock_count_lines SET counted=? WHERE id=?", (float(v), l['id']))
            except ValueError:
                pass
    d.commit(); flash(tr('သိမ်းပြီးပြီ'))
    return redirect(url_for('stock_count_detail', cid=cid))

@app.route('/stock-count/<int:cid>/approve', methods=['POST'])
@require_perm('stock_count.approve')
def stock_count_approve(cid):
    d = db()
    c = d.execute("SELECT * FROM stock_counts WHERE id=?", (cid,)).fetchone()
    if not c or c['status'] != 'draft': abort(400)
    moved = 0
    for l in d.execute("SELECT * FROM stock_count_lines WHERE count_id=? AND counted IS NOT NULL", (cid,)).fetchall():
        diff = l['counted'] - pool_stock(d, l['product_id'])
        if abs(diff) > 1e-9:
            move_stock(l['product_id'], diff, 'count', 'COUNT-%d' % cid, tr('လက်တွေ့ ရေတွက် စစ်ဆေး'), g.user['id'])
            moved += 1
    d.execute("UPDATE stock_counts SET status='approved', approved_by=?, approved_at=? WHERE id=?", (g.user['id'], now(), cid))
    d.commit(); flash(tr('အတည်ပြုပြီးပြီ — ကွာတဲ့ {n} မျိုးကို စာရင်း ညှိပြီးပြီ', n=moved))
    return redirect(url_for('stock_count_detail', cid=cid))

@app.route('/stock-count/<int:cid>/delete', methods=['POST'])
@require_perm('stock_count.delete')
def stock_count_delete(cid):
    d = db()
    c = d.execute("SELECT * FROM stock_counts WHERE id=?", (cid,)).fetchone()
    if c and c['status'] == 'draft' and (has_perm(g.user, 'audit.view') or c['created_by'] == g.user['id']):
        d.execute("DELETE FROM stock_count_lines WHERE count_id=?", (cid,))
        d.execute("DELETE FROM stock_counts WHERE id=?", (cid,))
        d.commit(); flash(tr('ဖျက်လိုက်ပြီ'))
    return redirect(url_for('stock_count_list'))

# ---------------- B4: expiry watch (60 days) ----------------
EXPIRY_WINDOW_DAYS = 60

def expiry_batches(d):
    """Purchase lines whose recorded expiry date is past or within the
    warning window, oldest first. Batch qty is the received quantity on
    that purchase record."""
    lim = (datetime.datetime.now(MMT) + datetime.timedelta(days=EXPIRY_WINDOW_DAYS)).date().isoformat()
    return d.execute("""SELECT pi.*, pu.code pcode, pu.pdate, p.sku, p.name, p.unit
                        FROM purchase_items pi JOIN purchases pu ON pu.id=pi.purchase_id
                        JOIN products p ON p.id=pi.product_id
                        WHERE pi.expiry_date IS NOT NULL AND pi.expiry_date!=''
                          AND pi.expiry_date<=? AND pu.status!='cancelled'
                        ORDER BY pi.expiry_date, p.sku""", (lim,)).fetchall()

def _days_left(datestr):
    try:
        y, m, dd = map(int, datestr.split('-'))
        return (datetime.date(y, m, dd) - datetime.datetime.now(MMT).date()).days
    except Exception:
        return None

@app.route('/expiry')
@require_perm('expiry.view')
def expiry_page():
    d = db()
    prods = {}
    # v62: pool levels for the listed products from two bulk queries
    # (was pool_info + raw_stock — two queries — per batch line).
    _smap = {r['product_id']: r['qty'] for r in d.execute("SELECT product_id, qty FROM stock").fetchall()}
    _pmap = {r['id']: (r['pool_id'] if (r['pool_id'] and r['pool_id'] != r['id']) else r['id'])
             for r in d.execute("SELECT id, pool_id FROM products").fetchall()}
    for b in expiry_batches(d):
        e = prods.setdefault(b['product_id'], {'sku': b['sku'], 'name': b['name'], 'unit': b['unit'],
                                                'pool': _smap.get(_pmap.get(b['product_id'], b['product_id']), 0), 'batches': []})
        e['batches'].append({'exp': b['expiry_date'], 'qty': b['qty'], 'pcode': b['pcode'],
                             'pdate': b['pdate'], 'days': _days_left(b['expiry_date'])})
    rows = sorted(prods.values(), key=lambda e: e['batches'][0]['exp'])
    return render_template('expiry.html', rows=rows, window=EXPIRY_WINDOW_DAYS, fmt_money=fmt_money)

# ---------------- B6: tax report (report only) ----------------
@app.route('/tax')
@require_perm('tax.view')
def tax_page():
    d = db()
    m = (request.args.get('m') or today()[:7]).strip()
    if not re.match(r'^\d{4}-\d{2}$', m): m = today()[:7]
    try:
        rate = float(get_setting(d, 'tax_rate') or 5)
    except (TypeError, ValueError):
        rate = 5.0
    days = {}
    total = 0.0
    # v62: per-day sums in one grouped pass (was order_total per invoice).
    _TOT = "(SELECT COALESCE(SUM(qty*price),0) FROM order_items WHERE order_id=o.id)-o.discount"
    for o in d.execute("SELECT o.odate od, COALESCE(SUM(" + _TOT + "),0) v FROM orders o WHERE o.status='delivered' AND substr(o.odate,1,7)=? GROUP BY o.odate", (m,)).fetchall():
        days[o['od']] = o['v']
        total += o['v']
    rows = [{'day': k, 'sales': v, 'tax': v * rate / 100.0} for k, v in sorted(days.items())]
    return render_template('tax.html', m=m, rate=rate, rows=rows, total=total,
                           tax_total=total * rate / 100.0, fmt_money=fmt_money)

@app.route('/tax/rate', methods=['POST'])
@require_perm('tax.rate_set')
def tax_rate_set():
    d = db()
    try:
        rate = float((request.form.get('rate') or '5').replace(',', ''))
    except ValueError:
        rate = 5.0
    if rate < 0: rate = 0.0
    if rate > 100: rate = 100.0
    set_setting(d, 'tax_rate', str(rate))
    d.commit(); flash(tr('သိမ်းပြီးပြီ'))
    return redirect('/tax?m=' + (request.form.get('m') or today()[:7]))

# ---------------- B7: in-app backup copies ----------------
BACKUP_DIR = os.path.join(os.path.dirname(DB_PATH), 'backups')
BACKUP_KEEP = 15

def make_backup_copy():
    """Copy the whole database file into data/backups with today's date,
    keeping only the newest copies. These copies sit on the same server:
    they guard against mistakes and file damage, and the owner still
    downloads one off the server regularly (see the /backups page)."""
    os.makedirs(BACKUP_DIR, exist_ok=True)
    fname = 'gtm-%s.db' % today()
    dest = os.path.join(BACKUP_DIR, fname)
    src = sqlite3.connect(DB_PATH); dst = sqlite3.connect(dest)
    try:
        src.backup(dst)
    finally:
        dst.close(); src.close()
    names = sorted(n for n in os.listdir(BACKUP_DIR) if re.match(r'^gtm-\d{4}-\d{2}-\d{2}\.db$', n))
    for old in names[:-BACKUP_KEEP] if len(names) > BACKUP_KEEP else []:
        try: os.remove(os.path.join(BACKUP_DIR, old))
        except OSError: pass
    return fname

@app.before_request
def _daily_backup_hook():
    try:
        u = current_user()
        if not u or not has_perm(u, 'profit.view'):
            return
        d = db()
        if get_setting(d, 'last_auto_backup') == today():
            return
        make_backup_copy()
        set_setting(d, 'last_auto_backup', today())
        d.commit()
    except Exception:
        pass

@app.route('/backups')
@require_perm('backups.view')
def backups_page():
    d = db()
    return render_template('backups.html', files=_backup_file_list(),
                           last_dl=get_setting(d, 'last_backup_download'),
                           last_restore=get_setting(d, 'last_restore'),
                           tok_copy=uuid.uuid4().hex, tok_up=uuid.uuid4().hex,
                           fmt_money=fmt_money)

@app.route('/backups/make', methods=['POST'])
@require_perm('backups.make')
def backups_make():
    make_backup_copy()
    d = db()
    set_setting(d, 'last_auto_backup', today())
    d.commit(); flash(tr('မိတ္တူ ယူပြီးပြီ'))
    return redirect(url_for('backups_page'))

@app.route('/backups/download/<fname>')
@require_perm('backups.download')
def backups_download(fname):
    if not BACKUP_NAME_RE.match(fname or ''):
        abort(404)
    if not os.path.exists(os.path.join(BACKUP_DIR, fname)):
        abort(404)
    d = db()
    set_setting(d, 'last_backup_download', now())
    d.commit()
    return send_from_directory(BACKUP_DIR, fname, as_attachment=True)

# ---------------- v85: restore ----------------
# A restore replaces the WHOLE live DB with a backup file — everything
# after the backup date is lost. So every restore (either path) is
# guarded the same way: Owner-only permission, a typed confirmation
# (never a bare button), a v63-style one-time token, validation of the
# candidate BEFORE the live DB is touched, and a pre-restore safety
# copy that is itself restorable (the undo).
BACKUP_NAME_RE = re.compile(r'^gtm-\d{4}-\d{2}-\d{2}(-pre-restore-\d{6}(-\d+)?)?\.db$')
RESTORE_UPLOAD_MAX = 100 * 1024 * 1024  # 100 MB

def _backup_file_list():
    """Daily copies + pre-restore safety copies, newest first. Safety
    copies never match the daily rotation regex, so rotation never
    prunes them — and they stay restorable, which is the restore undo."""
    out = []
    if os.path.isdir(BACKUP_DIR):
        for n in sorted((n for n in os.listdir(BACKUP_DIR) if BACKUP_NAME_RE.match(n)), reverse=True):
            p = os.path.join(BACKUP_DIR, n)
            out.append({'name': n, 'size': os.path.getsize(p),
                        'safety': '-pre-restore-' in n})
    return out

def _validate_backup_file(path):
    """Read-only check: valid SQLite + GTM schema markers + clean
    integrity_check. Returns None when OK, else a plain Burmese reason.
    Never touches the live DB."""
    try:
        con = sqlite3.connect('file:%s?mode=ro' % _urlquote(path), uri=True, timeout=10)
    except Exception:
        return tr('ဒီ ဖိုင် ကို ဖွင့် လို့ မရ ဘူး')
    try:
        tables = {r[0] for r in con.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall()}
        if 'products' not in tables or 'users' not in tables:
            return tr('ဒီ ဖိုင် က GTM စာရင်း ဖိုင် မဟုတ် ဘူး')
        row = con.execute("PRAGMA integrity_check").fetchone()
        if not row or (row[0] or '').lower() != 'ok':
            return tr('ဒီ ဖိုင် ပျက် နေ တယ် — စစ် လို့ မရ ဘူး')
    except sqlite3.Error:
        return tr('ဒီ ဖိုင် ကို ဖွင့် လို့ မရ ဘူး')
    finally:
        con.close()
    return None

def _make_safety_copy():
    """Copy the CURRENT live DB aside before a restore. The name never
    matches the daily rotation regex, so it is never pruned."""
    os.makedirs(BACKUP_DIR, exist_ok=True)
    base = 'gtm-%s-pre-restore-%s' % (today(), now()[11:19].replace(':', ''))
    fname = base + '.db'
    dest = os.path.join(BACKUP_DIR, fname)
    i = 2  # two restores in the same second must not overwrite each other's undo
    while os.path.exists(dest):
        fname = '%s-%d.db' % (base, i); dest = os.path.join(BACKUP_DIR, fname); i += 1
    src = sqlite3.connect(DB_PATH); dst = sqlite3.connect(dest)
    try:
        src.backup(dst)
    finally:
        dst.close(); src.close()
    return fname

def _restore_live(src_path):
    """Replace the live DB content from src_path via the sqlite3 backup
    API (same file, no file swap, no restart needed). Raises on failure;
    the caller names the safety copy in the error message, since the
    safety copy is the recovery from a half-done restore."""
    src = sqlite3.connect('file:%s?mode=ro' % _urlquote(src_path), uri=True, timeout=30)
    dst = sqlite3.connect(DB_PATH, timeout=30)
    try:
        src.backup(dst)
    finally:
        dst.close(); src.close()

def _restore_token_used(d, tok):
    """True when this one-time restore token was already spent. A missing
    token (old cached page) fails open like v63, but the typed
    confirmation below is still mandatory on every restore."""
    if not tok:
        return False
    try:
        row = d.execute("SELECT token FROM restore_form_tokens WHERE token=?", (tok,)).fetchone()
    except sqlite3.Error:
        return False  # table missing (pre-v85 DB) — the token cannot have been spent
    return row is not None

def _after_restore(token, src_name, safety_name, user_id):
    """Bookkeeping AFTER a restore, on a fresh connection (the request's
    own connection predates the restore). Spends the token so a lag
    double-submit is refused, writes the audit_events row, and records
    the last-restore info shown on the /backups page. The token spend
    and audit row live in the RESTORED db; a later restore of an older
    copy rolls them back too — so the token guards double-submits, not
    deliberate replays after rolling back to an older DB."""
    con = sqlite3.connect(DB_PATH, timeout=30)
    try:
        con.execute("CREATE TABLE IF NOT EXISTS restore_form_tokens(token TEXT PRIMARY KEY, user_id INTEGER, created_at TEXT)")
        if token:
            con.execute("INSERT OR IGNORE INTO restore_form_tokens(token,user_id,created_at) VALUES(?,?,?)",
                        (token, user_id, now()))
        try:
            con.execute("INSERT INTO audit_events(created_at,user_id,kind,summary,ref_type,ref_id,notify) VALUES(?,?,?,?,?,?,0)",
                        (now(), user_id, 'restore',
                         'Restore လုပ် — %s ကနေ (safety: %s)' % (src_name, safety_name), 'backup', None))
        except sqlite3.Error:
            pass  # very old backups may lack audit_events; the setting below still records it
        con.execute("INSERT OR REPLACE INTO app_settings(k, v) VALUES(?, ?)",
                    ('last_restore', '%s | %s | safety: %s' % (now(), src_name, safety_name)))
        con.commit()
    finally:
        con.close()

def _do_restore_or_flash(src_path, src_name, token, user_id):
    """Shared restore flow for both paths. Returns (ok, flash_text).
    The live DB is touched only after validation + safety copy."""
    err = _validate_backup_file(src_path)
    if err:
        return False, err
    safety = _make_safety_copy()
    try:
        _restore_live(src_path)
    except Exception:
        return False, tr('Restore လုပ် ရင်း ပျက် သွား — safety မိတ္တူ %s ကနေ ပြန် restore လုပ် ပါ' % safety)
    _after_restore(token, src_name, safety, user_id)
    return True, tr('Restore ပြီးပြီ — %s ကနေ ပြန် ဖြစ် သွား ပြီ (safety: %s)' % (src_name, safety))

@app.route('/backups/restore', methods=['POST'])
@require_perm('backups.restore')
def backups_restore():
    d = db()
    fname = (request.form.get('fname') or '').strip()
    confirm = (request.form.get('confirm') or '').strip()
    tok = request.form.get('token') or ''
    if _restore_token_used(d, tok):
        flash(tr('ဒီ restore ကို လုပ်ပြီးသား — ထပ်မလုပ်ဘူး'))
        return redirect(url_for('backups_page'))
    if not BACKUP_NAME_RE.match(fname or ''):
        flash(tr('မိတ္တူ ဖိုင် နာမည် မှား နေ တယ်'))
        return redirect(url_for('backups_page'))
    path = os.path.join(BACKUP_DIR, fname)
    if not os.path.exists(path):
        flash(tr('မိတ္တူ ဖိုင် မရှိ တော့ ဘူး'))
        return redirect(url_for('backups_page'))
    if confirm != fname:
        flash(tr('ဖိုင် နာမည် အတိုင်း ရိုက် ရ မယ် — ဘာမှ မပြောင်း ဘူး'))
        return redirect(url_for('backups_page'))
    ok, msg = _do_restore_or_flash(path, fname, tok, g.user['id'])
    flash(msg)
    return redirect(url_for('backups_page'))

@app.route('/backups/restore-upload', methods=['POST'])
@require_perm('backups.restore')
def backups_restore_upload():
    d = db()
    confirm = (request.form.get('confirm') or '').strip()
    tok = request.form.get('token') or ''
    if _restore_token_used(d, tok):
        flash(tr('ဒီ restore ကို လုပ်ပြီးသား — ထပ်မလုပ်ဘူး'))
        return redirect(url_for('backups_page'))
    f = request.files.get('dbfile')
    if f is None or not (f.filename or '').lower().endswith('.db'):
        flash(tr('.db ဖိုင် ပဲ တင် လို့ ရ တယ်'))
        return redirect(url_for('backups_page'))
    if confirm != 'RESTORE':
        flash(tr('RESTORE လို့ ရိုက် ရ မယ် — ဘာမှ မပြောင်း ဘူး'))
        return redirect(url_for('backups_page'))
    data = f.read(RESTORE_UPLOAD_MAX + 1)
    if len(data) > RESTORE_UPLOAD_MAX:
        flash(tr('ဖိုင် ကြီး လွန်း တယ် (100 MB အထိ ပဲ)'))
        return redirect(url_for('backups_page'))
    tmp = tempfile.NamedTemporaryFile(prefix='gtm-restore-', suffix='.db', delete=False)
    try:
        tmp.write(data); tmp.close()
        ok, msg = _do_restore_or_flash(tmp.name, 'upload: ' + (f.filename or ''), tok, g.user['id'])
        flash(msg)
    finally:
        try: os.remove(tmp.name)
        except OSError: pass
    return redirect(url_for('backups_page'))

# ---------------- A9: "Ai မေးကြည့်" Ask layer (renamed in v50) ----------------
def _ask_latest_market(d, pid, kind):
    r = d.execute("""SELECT * FROM ask_market_prices WHERE product_id=? AND kind=?
                     ORDER BY pdate DESC, id DESC LIMIT 1""", (pid, kind)).fetchone()
    if not r:
        return None
    age = None
    try:
        y, m, dd = map(int, (r['pdate'] or '').split('-'))
        age = (datetime.datetime.now(MMT).date() - datetime.date(y, m, dd)).days
    except Exception:
        age = None
    return {'row': r, 'age': age}

def _ask_stale_note(age):
    if age is None: return ''
    if age > 30: return tr('ဈေး အဟောင်း — သတိ နဲ့ သုံး (ရက် {n} ကြာပြီ)', n=age)
    if age > 10: return tr('ရက် {n} ကြာ ဈေး', n=age)
    return ''

def _ask_missing(pid):
    return {'title': tr('မသိ'), 'status': None,
            'lines': [tr('ဒီ ပစ္စည်း အတွက် ဈေးကွက် ဈေး မထည့်ရသေးဘူး — အရင် ထည့်ပြီးမှ ဖြေ လို့ ရမယ်')],
            'links': [('/ask/prices', tr('ဈေးကွက် ဈေး ထည့်ရန်'))]}

@app.route('/ask')
@require_perm('ask.view')
def ask_page():
    d = db()
    q = (request.args.get('q') or '').strip()
    pid = request.args.get('pid', type=int)
    prod = d.execute("SELECT * FROM products WHERE id=?", (pid,)).fetchone() if pid else None
    ans = ask_answer(d, q, prod) if q else None
    prods = d.execute("SELECT id, sku, name, unit FROM products WHERE active=1 ORDER BY sku").fetchall()
    return render_template('ask.html', q=q, prod=prod, ans=ans, prods=prods, fmt_money=fmt_money)

def ask_answer(d, q, prod):
    TD = today(); MO = TD[:7]
    TOT = "(SELECT COALESCE(SUM(qty*price),0) FROM order_items WHERE order_id=o.id)-o.discount"
    if q == 'today_sales':
        r = d.execute("SELECT COALESCE(SUM(" + TOT + "),0) v, COUNT(*) c FROM orders o WHERE o.odate=? AND o.status!='cancelled'", (TD,)).fetchone()
        dv = d.execute("SELECT COUNT(*) c FROM orders WHERE odate=? AND status='delivered'", (TD,)).fetchone()['c']
        return {'title': tr('ဒီနေ့ အရောင်း'), 'status': 'green' if r['v'] > 0 else None,
                'lines': [tr('ဒီနေ့ ရောင်းရငွေ: {v} Ks (ဘောက်ချာ {c} စောင်)', v=fmt_money(r['v']), c=r['c']),
                          tr('ပို့ပြီး ဘောက်ချာ: {c} စောင်', c=dv)],
                'links': [('/sales-report', tr('📊 Sales စစ်ဆေး'))]}
    if q == 'month_profit':
        sales = d.execute("SELECT COALESCE(SUM(" + TOT + "),0) v FROM orders o WHERE o.status='delivered' AND substr(o.odate,1,7)=?", (MO,)).fetchone()['v']
        cogs = d.execute("""SELECT COALESCE(SUM(oi.qty * COALESCE(oi.cost_act, p.actual_base, 0)),0) v
                            FROM order_items oi JOIN orders o ON o.id=oi.order_id JOIN products p ON p.id=oi.product_id
                            WHERE o.status='delivered' AND substr(o.odate,1,7)=?""", (MO,)).fetchone()['v']
        gp = sales - cogs
        return {'title': tr('ဒီလ အမြတ်'), 'status': 'green' if gp >= 0 else 'red',
                'lines': [tr('ဒီလ ရောင်းရငွေ (ပို့ပြီး): {v} Ks', v=fmt_money(sales)),
                          tr('ကုန် အရင်း: {v} Ks', v=fmt_money(cogs)),
                          tr('အမြတ် ကြမ်း: {v} Ks', v=fmt_money(gp))],
                'links': [('/profit', tr('📈 လချုပ် အမြတ်'))]}
    if q == 'credit_out':
        pos = money_position(d)
        n = d.execute("SELECT COUNT(*) c FROM orders WHERE status='delivered' AND paid=0").fetchone()['c']
        return {'title': tr('အကြွေး ကျန်'), 'status': 'yellow' if pos['receivables'] > 0 else 'green',
                'lines': [tr('ဆိုင်တွေ က ပေးရန် ကျန်: {v} Ks (ဘောက်ချာ {c} စောင်)', v=fmt_money(pos['receivables']), c=n)],
                'links': [('/outlets', tr('🏪 ဆိုင်များ (Credit)'))]}
    if q == 'pending_checks':
        pc = d.execute("SELECT COUNT(*) c FROM purchases WHERE status='pending_check'").fetchone()['c']
        rc = d.execute("SELECT COUNT(*) c FROM returns WHERE status='recorded'").fetchone()['c']
        uc = d.execute("SELECT COALESCE(SUM(amount),0) v FROM order_payments WHERE COALESCE(verified,1)=0").fetchone()['v']
        return {'title': tr('စစ်ရန် ကျန်'), 'status': 'yellow' if (pc or rc or uc) else 'green',
                'lines': [tr('အဝယ် စစ်ရန်: {c} ခု', c=pc), tr('ပြန်အပ် စစ်ရန်: {c} ခု', c=rc),
                          tr('ပို့သူ ကောက်ငွေ စစ်ရန်: {v} Ks', v=fmt_money(uc))],
                'links': [('/purchases?st=pending_check', tr('အဝယ် မှတ်တမ်း')), ('/handover', tr('💵 ငွေ လွှဲအပ် စစ်ရန်'))]}
    if q == 'month_expenses':
        tot = d.execute("SELECT COALESCE(SUM(amount),0) v FROM expenses WHERE substr(edate,1,7)=?", (MO,)).fetchone()['v']
        top = d.execute("SELECT category, COALESCE(SUM(amount),0) v FROM expenses WHERE substr(edate,1,7)=? GROUP BY category ORDER BY v DESC LIMIT 1", (MO,)).fetchone()
        lines = [tr('ဒီလ အသုံးစရိတ် စုစုပေါင်း: {v} Ks', v=fmt_money(tot))]
        if top: lines.append(tr('အများဆုံး သုံးတာ: {c} — {v} Ks', c=top['category'] or '', v=fmt_money(top['v'])))
        return {'title': tr('ဒီလ အသုံးစရိတ်'), 'status': None, 'lines': lines,
                'links': [('/expenses', tr('💸 အသုံးစရိတ်'))]}
    if q == 'money_flow':
        t = money_group_totals(d, TD, TD)
        lines = []
        for k, lbl in PAY_METHODS:
            lines.append(tr('{g} — အဝင် {i} Ks · အထွက် {o} Ks', g=tr(lbl), i=fmt_money(t[k]['in']), o=fmt_money(t[k]['out'])))
        return {'title': tr('ဒီနေ့ ငွေ အဝင်/အထွက်'), 'status': None, 'lines': lines,
                'links': [('/money', tr('💰 ငွေ စာရင်း'))]}
    if q in ('prod_status', 'prod_endure', 'prod_price', 'cheap_supplier'):
        if not prod:
            return {'title': tr('မသိ'), 'status': None, 'lines': [tr('အရင် ပစ္စည်း ရွေးပါ')], 'links': []}
        return _ask_product(d, q, prod)
    if q == 'risk_list':
        rows = d.execute("""SELECT p.sku, p.name, oi.qty, oi.cost_act, p.actual_base cur, o.code
                            FROM order_items oi JOIN orders o ON o.id=oi.order_id JOIN products p ON p.id=oi.product_id
                            WHERE o.status='new' AND oi.cost_act IS NOT NULL AND p.actual_base IS NOT NULL
                              AND p.actual_base > oi.cost_act ORDER BY (p.actual_base - oi.cost_act) * oi.qty DESC LIMIT 10""").fetchall()
        if not rows:
            return {'title': tr('အန္တရာယ် ပစ္စည်းများ'), 'status': 'green',
                    'lines': [tr('ခု လောလောဆယ် အရင်း တက်ပြီး ထိနေတဲ့ ဘောက်ချာ မရှိဘူး')], 'links': [('/at-risk', tr('⚠️ အန္တရာယ် စာရင်း'))]}
        lines = [tr('{sku} {name} — ရောင်းချိန် အရင်း {a} Ks, ခု အရင်း {b} Ks ({code})',
                    sku=r['sku'], name=r['name'], a=fmt_money(r['cost_act']), b=fmt_money(r['cur']), code=r['code']) for r in rows]
        return {'title': tr('အန္တရာယ် ပစ္စည်းများ'), 'status': 'red', 'lines': lines,
                'links': [('/at-risk', tr('⚠️ အန္တရာယ် စာရင်း'))]}
    if q == 'oldest_debt':
        best = None
        for o in d.execute("SELECT * FROM orders WHERE status='delivered' AND paid=0 ORDER BY odate, id").fetchall():
            t, _ = order_total(o['id']); b = t - order_received(o['id'], d)
            if b > 0.5:
                best = {'outlet': o['outlet'], 'odate': o['odate'], 'bal': b, 'code': o['code']}
                break
        if not best:
            return {'title': tr('အကြွေး အကြာဆုံး ဆိုင်'), 'status': 'green', 'lines': [tr('ကျန်ငွေ ရှိတဲ့ ဆိုင် မရှိဘူး')], 'links': [('/outlets', tr('🏪 ဆိုင်များ (Credit)'))]}
        return {'title': tr('အကြွေး အကြာဆုံး ဆိုင်'), 'status': 'yellow',
                'lines': [tr('{shop} — {d} ကတည်းက ကျန် {v} Ks ({code})', shop=best['outlet'], d=best['odate'], v=fmt_money(best['bal']), code=best['code'])],
                'links': [('/outlets', tr('🏪 ဆိုင်များ (Credit)'))]}
    if q == 'quiet_shops':
        rows = []
        for o in d.execute("SELECT * FROM outlets ORDER BY name").fetchall():
            last = d.execute("SELECT MAX(odate) m FROM orders WHERE outlet_id=? AND status!='cancelled'", (o['id'],)).fetchone()['m']
            if not last: continue
            try:
                y, m, dd = map(int, last.split('-'))
                days = (datetime.datetime.now(MMT).date() - datetime.date(y, m, dd)).days
            except Exception:
                continue
            if days >= 30: rows.append((days, o['name'], last))
        rows.sort(reverse=True)
        if not rows:
            return {'title': tr('မဝယ် တာ ကြာ ပြီ ဆိုင်'), 'status': 'green', 'lines': [tr('ရက် ၃၀ ကျော် မဝယ် တဲ့ ဆိုင် မရှိဘူး')], 'links': [('/outlets', tr('🏪 ဆိုင်များ (Credit)'))]}
        return {'title': tr('မဝယ် တာ ကြာ ပြီ ဆိုင်'), 'status': 'yellow',
                'lines': [tr('{shop} — နောက်ဆုံး ဝယ်တာ {d} ({n} ရက် ကြာပြီ)', shop=n, d=l, n=dy) for dy, n, l in rows[:8]],
                'links': [('/outlets', tr('🏪 ဆိုင်များ (Credit)'))]}
    if q == 'to_buy':
        rows = d.execute("""SELECT p.id, p.sku, p.name, COALESCE(SUM(oi.qty),0) dem
                            FROM order_items oi JOIN orders o ON o.id=oi.order_id JOIN products p ON p.id=oi.product_id
                            WHERE o.status='new' GROUP BY p.id ORDER BY dem DESC LIMIT 30""").fetchall()
        out = []
        for r in rows:
            if pool_stock(d, r['id']) <= 0:
                mb = _ask_latest_market(d, r['id'], 'buy')
                extra = ''
                if mb: extra = ' · ' + tr('ပေါက်ဈေး ဝယ် {p} Ks ({src})', p=fmt_money(mb['row']['price']), src=mb['row']['source'] or '')
                out.append(tr('{sku} {name} — မှာ ထား {q} ခု, stock မရှိတော့ဘူး', sku=r['sku'], name=r['name'], q='%g' % r['dem']) + extra)
            if len(out) >= 10: break
        if not out:
            return {'title': tr('ဝယ် ရမယ့် ပစ္စည်း'), 'status': 'green', 'lines': [tr('မှာ ထား ပြီး stock ကုန် နေ တဲ့ ပစ္စည်း မရှိဘူး')], 'links': [('/buy-list', tr('ဝယ်ရန် စာရင်း (ဝယ်ရမယ့် စာရင်း အမှန်)'))]}
        return {'title': tr('ဝယ် ရမယ့် ပစ္စည်း'), 'status': 'yellow', 'lines': out,
                'links': [('/buy-list', tr('ဝယ်ရန် စာရင်း (ဝယ်ရမယ့် စာရင်း အမှန်)'))]}
    return {'title': tr('မသိ'), 'status': None, 'lines': [tr('ဒီ မေးခွန်း မပါ သေးဘူး — အောက်က ခလုတ်တွေ ထဲ က ရွေးပါ')], 'links': []}

def _ask_product(d, q, prod):
    pid = prod['id']
    act = prod['actual_base']; base = prod['base_price'] or 0
    retail = prod['retail'] or 0; whole = prod['wholesale'] or 0
    mbuy = _ask_latest_market(d, pid, 'buy')
    msell = _ask_latest_market(d, pid, 'sell')
    head = tr('{sku} {name}', sku=prod['sku'], name=prod['name'])
    plink = [('/products/%d/edit' % pid, tr('ပစ္စည်း စာမျက်နှာ'))]
    if q == 'cheap_supplier':
        rows = d.execute("""SELECT pu.supplier, MIN(pi.cost) c FROM purchase_items pi
                            JOIN purchases pu ON pu.id=pi.purchase_id
                            WHERE pi.product_id=? AND pu.status!='cancelled' GROUP BY pu.supplier ORDER BY c""", (pid,)).fetchall()
        lines = [head]
        if rows:
            lines += [tr('{src} — ဝယ်ဖူး တဲ့ အနှစ် ဆုံး ဈေး {p} Ks', src=(r['supplier'] or ''), p=fmt_money(r['c'])) for r in rows]
        else:
            lines.append(tr('ဝယ် မှတ်တမ်း မရှိသေးဘူး'))
        if mbuy:
            lines.append(tr('ပေါက်ဈေး ဝယ်: {p} Ks ({src} · {d})', p=fmt_money(mbuy['row']['price']), src=mbuy['row']['source'] or '', d=mbuy['row']['pdate'] or '') + ((' — ' + _ask_stale_note(mbuy['age'])) if _ask_stale_note(mbuy['age']) else ''))
        return {'title': tr('ဘယ် သူ အ သက်သာ ဆုံး လဲ'), 'status': None, 'lines': lines, 'links': plink}
    if q == 'prod_status':
        if not msell and not mbuy:
            return _ask_missing(pid)
        lines = [head, tr('ကိုယ့် ရောင်းဈေး: လက်လီ {r} Ks · လက်ကား {w} Ks', r=fmt_money(retail), w=fmt_money(whole))]
        status = None
        if msell:
            diff = retail - (msell['row']['price'] or 0)
            lines.append(tr('ဈေးကွက် ရောင်းဈေး: {p} Ks ({src} · {d})', p=fmt_money(msell['row']['price']), src=msell['row']['source'] or '', d=msell['row']['pdate'] or '') + ((' — ' + _ask_stale_note(msell['age'])) if _ask_stale_note(msell['age']) else ''))
            if diff > 0:
                lines.append(tr('ကိုယ့် လက်လီ က ဈေးကွက် ထက် {v} Ks များ နေတယ်', v=fmt_money(diff)))
                status = 'yellow' if diff <= retail * 0.05 else 'red'
            else:
                lines.append(tr('ကိုယ့် လက်လီ က ဈေးကွက် ထက် {v} Ks နည်း (ယှဉ်နိုင်) နေတယ်', v=fmt_money(-diff)))
                status = 'green'
        if mbuy:
            lines.append(tr('ပေါက်ဈေး ဝယ်: {p} Ks ({src} · {d})', p=fmt_money(mbuy['row']['price']), src=mbuy['row']['source'] or '', d=mbuy['row']['pdate'] or '') + ((' — ' + _ask_stale_note(mbuy['age'])) if _ask_stale_note(mbuy['age']) else ''))
            if act is not None and (mbuy['row']['price'] or 0) > act:
                lines.append(tr('ပေါက်ဈေး က ခု အရင်း ထက် များ နေတယ် — ပြန် ဝယ် ရင် အရင်း တက် မယ်'))
                status = 'red' if status != 'red' else status
        return {'title': tr('ဈေးကွက် နဲ့ ယှဉ် ရင်'), 'status': status, 'lines': lines, 'links': plink}
    if q == 'prod_endure':
        if act is None:
            return {'title': tr('တောင့်ခံ နိုင် မှု'), 'status': None,
                    'lines': [head, tr('အရင်း အမှန် မထည့်ရသေးဘူး — ဖြေ လို့ မရဘူး')], 'links': plink}
        buf = (base - act)
        bufpct = ((base - act) / base * 100) if base else 0
        room = retail - act
        status = 'red' if act >= base else ('yellow' if bufpct < 5 else 'green')
        lines = [head,
                 tr('ခု အရင်း အမှန်: {v} Ks', v=fmt_money(act)),
                 tr('အရင်း (Base): {v} Ks — ကြား ကာကွယ် ကျန် {b} Ks ({p}%)', v=fmt_money(base), b=fmt_money(buf), p='%.1f' % bufpct),
                 tr('လက်လီ ဈေး မပြောင်း ပဲ အရင်း {v} Ks ထိ တက် ခံ နိုင် တယ် (အဲ့ အထက် ဆို အမြတ် ကုန်)', v=fmt_money(room))]
        if mbuy:
            mb = mbuy['row']['price'] or 0
            lines.append(tr('ပေါက်ဈေး ဝယ်: {p} Ks ({src} · {d})', p=fmt_money(mb), src=mbuy['row']['source'] or '', d=mbuy['row']['pdate'] or '') + ((' — ' + _ask_stale_note(mbuy['age'])) if _ask_stale_note(mbuy['age']) else ''))
            if mb > base:
                lines.append(tr('ပေါက်ဈေး က Base ကို ကျော် သွား ပြီ — ကာကွယ် မကျန် တော့ဘူး'))
                status = 'red'
            if retail > act and mb > 0:
                mg = (retail - act) / retail
                cand = mb / (1 - mg) if mg < 1 else 0
                lines.append(tr('ခု အမြတ် အတိုင်း ထိန်း မယ် ဆို စဉ်းစား လို့ ရတဲ့ ဈေး: {v} Ks က စပြီး (ဈေး ပြင် တာ Ko ကိုယ် တိုင် ပဲ)', v=fmt_money(round(cand))))
        return {'title': tr('တောင့်ခံ နိုင် မှု'), 'status': status, 'lines': lines, 'links': plink}
    if q == 'prod_price':
        if act is None:
            return {'title': tr('ဈေး အကြံ'), 'status': None,
                    'lines': [head, tr('အရင်း အမှန် မထည့်ရသေးဘူး — ဖြေ လို့ မရဘူး')], 'links': plink}
        lines = [head]
        def _mg(price, cost):
            return ((price - cost) / price * 100) if price else 0
        lines.append(tr('ထိန်း — ခု လက်လီ {p} Ks (အမြတ် {m}%)', p=fmt_money(retail), m='%.1f' % _mg(retail, act)))
        if mbuy:
            mb = mbuy['row']['price'] or 0
            mg = (retail - act) / retail if retail else 0
            if mb > 0 and mg < 1:
                cand = mb / (1 - mg)
                lines.append(tr('အမြတ် ထိန်း (ပေါက်ဈေး အရင်း နဲ့) — {p} Ks', p=fmt_money(round(cand))))
        if msell:
            sp = msell['row']['price'] or 0
            lines.append(tr('ဈေးကွက် လိုက် — {p} Ks (အမြတ် {m}%)', p=fmt_money(sp), m='%.1f' % _mg(sp, act)))
        lines.append(tr('ဒါ တွေ က အကြံ ပြု သက်သက် — ဈေး ပြင် တာ Ko ကိုယ် တိုင် ပဲ လုပ် ရမယ်'))
        return {'title': tr('ဈေး အကြံ'), 'status': None, 'lines': lines, 'links': plink}
    return _ask_missing(pid)

@app.route('/ask/prices', methods=['GET', 'POST'])
@require_perm('ask.prices_manage')
def ask_prices():
    d = db()
    if request.method == 'POST':
        f = request.form
        pid = int(f.get('product_id') or 0)
        try: price = float((f.get('price') or '0').replace(',', ''))
        except ValueError: price = 0
        kind = 'buy' if f.get('kind') == 'buy' else 'sell'
        pdate = (f.get('pdate') or '').strip()
        if not re.match(r'^\d{4}-\d{2}-\d{2}$', pdate or ''): pdate = today()
        if not pid or price <= 0:
            flash(tr('ပစ္စည်း နဲ့ ဈေး လိုပါတယ်'))
        else:
            d.execute("INSERT INTO ask_market_prices(product_id, town, kind, price, pdate, source, note, by_user, created_at) VALUES(?,?,?,?,?,?,?,?,?)",
                      (pid, (f.get('town') or '').strip(), kind, price, pdate,
                       (f.get('source') or '').strip(), (f.get('note') or '').strip(), g.user['id'], now()))
            d.commit(); flash(tr('မှတ်ပြီးပြီ'))
        return redirect(url_for('ask_prices'))
    rows = d.execute("""SELECT a.*, p.sku, p.name, u.name byname FROM ask_market_prices a
                        JOIN products p ON p.id=a.product_id LEFT JOIN users u ON u.id=a.by_user
                        ORDER BY a.id DESC LIMIT 100""").fetchall()
    prods = d.execute("SELECT id, sku, name, unit FROM products WHERE active=1 ORDER BY sku").fetchall()
    return render_template('ask_prices.html', rows=rows, prods=prods, today_s=today(), fmt_money=fmt_money)

@app.route('/ask/prices/<int:mid>/delete', methods=['POST'])
@require_perm('ask.prices_manage')
def ask_price_delete(mid):
    d = db()
    d.execute("DELETE FROM ask_market_prices WHERE id=?", (mid,))
    d.commit(); flash(tr('ဖျက်လိုက်ပြီ'))
    return redirect(url_for('ask_prices'))

init_db()

if __name__ == '__main__':
    app.run(host='127.0.0.1', port=int(os.environ.get('PORT', '5000')), debug=False)
