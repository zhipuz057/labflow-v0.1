# LabFlow

### 猛男zzp的README

让管理实验室更高效，让我们更爽。让浪费实验室钱的狗们无处遁形。

P.S. 都他*什么年代了，一个管理系统还卖上万，要脸不要！

---

## 当前版本

**LabFlow V0.1**

目前支持：

- 粘贴供应商信息，自动识别产品名称、品牌、货号、规格、价格、数量和供应商
- 采购信息人工确认后提交
- 公共实验试剂 / 个人订购试剂分类
- 每个用户拥有独立访问钥匙
- 普通采购成员查看自己的订单
- 负责人查看全部订单
- 实验室成员共同确认到货
- 自动记录收货人和收货时间
- 订单可以取消，但不物理删除
- 按时间、申请人和订单类型查看采购金额
- 导出 CSV 账目
- 保留关键操作审计记录

目前设计四种角色：

- `owner`：系统所有者（没错就是我）
- `manager`：实验室管理人员（没错还是我）
- `purchaser`：普通采购成员
- `account_viewer`：只读查看和导出账目的老师

---

## 为什么做 LabFlow

因为我要管理实验室20多个人的吃喝拉撒啊！md我自己快30了都活不明白，还管别人。

供应商已经发来：

`CST 4970S Phospho-Akt (Ser473) Antibody 100 μL，报价1680元`

以前可能还需要重新填写：

品牌、货号、名称、规格、价格。

LabFlow 直接变成：

**复制 → 粘贴 → 自动识别 → 检查 → 提交**

不为了炫技，就是想解决实验室里这些每天都会碰到的小麻烦，让大家少做一点重复劳动。

---

## 现在用什么，接下来做什么

当前技术栈是 **Streamlit + Python + SQLite**。快速识别在本地完成，不需要付费 API。

先把下单、查单、收货和看账这几件事做好，不急着把它做成 ERP。

下一步准备迁移到 **Supabase / PostgreSQL**，为实验室多人正式使用做好数据存储和权限配置。现在还在用 SQLite，迁移时会保留已有订单、收货信息和审计记录。

---

## 本地运行

需要 Python 3.10 或更新版本。在包含 `app.py` 的项目目录中打开终端。

### macOS / Linux

```bash
python3 -m venv .venv
source .venv/bin/activate
python -m pip install -r requirements.txt
streamlit run app.py
```

### Windows PowerShell

```powershell
py -m venv .venv
.\.venv\Scripts\Activate.ps1
python -m pip install -r requirements.txt
streamlit run app.py
```

启动后打开 [http://localhost:8501](http://localhost:8501)，使用分配给自己的访问钥匙登录。按 `Ctrl+C` 停止服务。

已有数据库请保留，升级不需要删库重建。访问钥匙和其他凭据只在本地妥善保管，不要写进 README 或提交到 GitHub。

---

# LabFlow — English Version

### README by Hunk zzp

Make lab management more efficient, make PhDs live longer, and make it much harder for anyone wasting lab money to hide.

P.S. It’s 2026. While Elon is trying to send us to Mars and Jensen is trying to sell us an RTX 9090, why the hell does a basic lab management system still cost thousands of RMB? Cheaters, frauds?

---

## Current Version

**LabFlow V0.1**

Currently supports:

- Paste supplier messages and automatically extract product name, brand, catalog number, specification, price, quantity, and supplier
- Manual review before submitting a purchase request
- Classification of shared lab reagents and personal purchases
- Individual access keys for each user
- Purchasers can view their own orders
- Managers can view all orders
- Lab members can confirm received deliveries
- Automatic recording of who received an item and when
- Orders can be cancelled without being physically deleted
- Spending summaries by date range, purchaser, and order type
- CSV export of purchasing records
- Audit logs for key operations

Four user roles are currently supported:

- `owner`: system owner — yes, that's me
- `manager`: lab manager — yes, also me, lmao
- `purchaser`: regular lab member who submits purchase requests
- `account_viewer`: read-only access for supervisors who need to review and export purchasing records

---

## Why I Built LabFlow

Because somehow I ended up helping manage the daily chaos of a lab with more than 20 people.

Honestly, I'm almost 30 and still figuring out how to manage my own life, and now I'm supposed to manage everyone else's stuff too.

A supplier might send something like:

`CST 4970S Phospho-Akt (Ser473) Antibody 100 μL, quoted at RMB 1,680`

Previously, someone would still have to manually re-enter:

brand, catalog number, product name, specification, and price.

LabFlow turns that into:

**Copy → Paste → Auto-detect → Review → Submit**

That's the whole point.

If a tool doesn't save people time, there is no reason for them to use it. I'm building this to deal with real lab problems, not to show off a tech stack.

---

## What It Runs On, and What's Next

The current stack is **Streamlit + Python + SQLite**. Supplier text is parsed locally, with no paid API required.

For now, the focus is on placing orders, finding records, confirming deliveries, and checking spending. It doesn't need to become an ERP.

The next step is a migration to **Supabase / PostgreSQL**, with storage and permissions ready for regular use by the whole lab. That migration hasn't happened yet. Existing orders, delivery records, and audit history will be preserved.

---

## Local Setup

Requires Python 3.10 or later. Open a terminal in the project directory containing `app.py`.

### macOS / Linux

```bash
python3 -m venv .venv
source .venv/bin/activate
python -m pip install -r requirements.txt
streamlit run app.py
```

### Windows PowerShell

```powershell
py -m venv .venv
.\.venv\Scripts\Activate.ps1
python -m pip install -r requirements.txt
streamlit run app.py
```

Open [http://localhost:8501](http://localhost:8501) and sign in with your assigned access key. Press `Ctrl+C` to stop the server.

Keep your existing database; upgrades do not require deleting it. Store access keys and other credentials privately, and never put them in this README or commit them to GitHub.
