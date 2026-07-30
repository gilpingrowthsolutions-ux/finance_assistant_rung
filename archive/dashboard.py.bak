import streamlit as st
import requests
import pandas as pd
import plotly.express as px

API_URL = "http://127.0.0.1:5000"

# -----------------------------------------------------------------------------
# 1. PAGE CONFIGURATION
# -----------------------------------------------------------------------------
st.set_page_config(
    page_title="Finance Hub",
    layout="wide",
    initial_sidebar_state="expanded",
)

# -----------------------------------------------------------------------------
# 2. SPOTIFY-INSPIRED DARK THEMING & STRICT TAB CSS OVERRIDES
# -----------------------------------------------------------------------------
st.markdown(
    """
    <style>
    /* App Background & Base Fonts */
    .stApp {
        background-color: #121212 !important;
        color: #FFFFFF !important;
        font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, sans-serif;
    }
    [data-testid="stSidebar"] {
        background-color: #000000 !important;
        border-right: 1px solid #282828 !important;
    }
    [data-testid="stHeader"] {
        background-color: #121212 !important;
    }
    .block-container {
        padding-top: 1rem;
        padding-bottom: 2rem;
        max-width: 1400px;
    }

    /* Modern Card Container Style */
    .spotify-card {
        background-color: #181818;
        border: 1px solid #282828;
        border-radius: 8px;
        padding: 20px;
        margin-bottom: 16px;
        transition: border-color 0.2s ease;
    }
    .spotify-card:hover {
        border-color: #3e3e3e;
    }
    .card-label {
        font-size: 0.75rem;
        text-transform: uppercase;
        letter-spacing: 0.08em;
        color: #b3b3b3;
        font-weight: 700;
        margin-bottom: 8px;
    }
    .card-value-large {
        font-size: 2.25rem;
        font-weight: 800;
        color: #ffffff;
        letter-spacing: -0.02em;
    }

    /* Form Controls Dark Override */
    .stTextInput input, .stNumberInput input, .stSelectbox select, .stDateInput input {
        background-color: #242424 !important;
        border: 1px solid #333333 !important;
        border-radius: 6px !important;
        color: #ffffff !important;
    }
    
    /* Buttons - Clean Dark Green Theme */
    .stButton button {
        background-color: #15803d !important;
        color: #ffffff !important;
        font-weight: 700 !important;
        border-radius: 50px !important;
        border: none !important;
        padding: 0.5rem 1.2rem !important;
        transition: transform 0.1s ease, background-color 0.2s ease !important;
        width: 100%;
    }
    .stButton button:hover {
        background-color: #166534 !important;
        transform: scale(1.02);
    }

    /* ------------------------------------------------------------------------- */
    /* STRICT TAB OVERRIDES - ALL RED REMOVED / OVERRIDDEN TO GREEN             */
    /* ------------------------------------------------------------------------- */
    
    /* Base Tab List Container */
    .stTabs [data-baseweb="tab-list"] {
        gap: 24px;
        background-color: transparent !important;
        border-bottom: 1px solid #282828 !important;
        padding-bottom: 0px;
        margin-bottom: 24px;
    }

    /* Target Streamlit's dynamic sliding underline highlight component and make it green */
    .stTabs [data-baseweb="tab-highlight"],
    .stTabs [data-baseweb="tab-border"] {
        background-color: #15803d !important;
    }

    /* Inactive Tab Base State */
    .stTabs [data-baseweb="tab"] {
        background-color: transparent !important;
        border: none !important;
        border-radius: 0px !important;
        color: #b3b3b3 !important; /* Normal text color */
        font-weight: 600 !important;
        font-size: 0.95rem;
        padding: 10px 4px !important;
        margin-bottom: -1px;
        border-bottom: 2px solid transparent !important;
        transition: border-color 0.2s ease, color 0.2s ease;
    }

    /* Force all tab internal paragraph/label tags to inherit color and strip red */
    .stTabs [data-baseweb="tab"] * {
        color: inherit !important;
    }

    /* 1. HOVER STATE (Before Click): Letters stay normal (#b3b3b3), Green underline appears */
    .stTabs [data-baseweb="tab"]:hover {
        color: #b3b3b3 !important;
        border-bottom: 2px solid #15803d !important;
        background-color: transparent !important;
    }

    /* 2. CLICKED/ACTIVE STATE: Letters turn Dark Green (#15803d), NO underline */
    .stTabs [aria-selected="true"] {
        background-color: transparent !important;
        color: #15803d !important;
        font-weight: 700 !important;
        border-bottom: 2px solid transparent !important;
    }

    /* Ensure clicked state stays green without underline even if hovered over */
    .stTabs [aria-selected="true"]:hover {
        color: #15803d !important;
        border-bottom: 2px solid transparent !important;
    }

    /* Remove outline/focus highlight rings */
    .stTabs button:focus, .stTabs button:active {
        box-shadow: none !important;
        outline: none !important;
    }
    /* ------------------------------------------------------------------------- */
    </style>
""",
    unsafe_allow_html=True,
)

# -----------------------------------------------------------------------------
# 3. API DATA FETCHING
# -----------------------------------------------------------------------------
try:
    health_check = requests.get(f"{API_URL}/", timeout=2)
    if health_check.status_code != 200:
        st.error("API Connection Error. Check Flask terminal logs.")
except requests.exceptions.RequestException:
    st.error("Backend server unreachable. Make sure `python3 app.py` is running on port 5000.")
    st.stop()

# Summary & Core Data
summary_res = requests.get(f"{API_URL}/summary")
summary_data = summary_res.json() if summary_res.status_code == 200 else {}
current_bal = summary_data.get('current_balance', 0.0)
unpaid_bills = summary_data.get('total_unpaid_bills', 0.0)
disposable = summary_data.get('disposable_cash', 0.0)
bills_count = summary_data.get('recurring_bills_count', 0)

loc_info = summary_data.get('location', {})
user_zip = loc_info.get('zip_code', '65084')
user_city = loc_info.get('city_state', 'Versailles, MO')
user_tax_pct = loc_info.get('sales_tax_pct', 8.225)

bills_res = requests.get(f"{API_URL}/bills")
bills_list = bills_res.json() if bills_res.status_code == 200 else []

trans_res = requests.get(f"{API_URL}/transactions")
trans_list = trans_res.json() if trans_res.status_code == 200 else []

recipes_res = requests.get(f"{API_URL}/recipes")
recipes_list = recipes_res.json() if recipes_res.status_code == 200 else []

grocery_res = requests.get(f"{API_URL}/grocery-list")
grocery_list = grocery_res.json() if grocery_res.status_code == 200 else []

# -----------------------------------------------------------------------------
# 4. SIDEBAR CONFIGURATION
# -----------------------------------------------------------------------------
with st.sidebar:
    st.markdown("### Settings")
    st.caption("Account & Local Store Configurations")

    with st.expander(":material/location_on: Location & Tax Settings", expanded=True):
        input_city = st.text_input("City, State", value=user_city)
        input_zip = st.text_input("ZIP Code", value=user_zip)
        st.caption(f"Calculated Tax Rate: **{user_tax_pct}%**")
        if st.button("Update Location & Tax"):
            res = requests.post(f"{API_URL}/set-location", json={
                "city_state": input_city,
                "zip_code": input_zip
            })
            if res.status_code == 200:
                st.success("Location updated")
                st.rerun()

    with st.expander(":material/account_balance_wallet: Checking Balance", expanded=False):
        new_bal = st.number_input("Set Balance ($)", min_value=0.0, step=50.0, value=1000.0)
        if st.button("Save Balance"):
            res = requests.post(f"{API_URL}/set-balance", json={"amount": new_bal})
            if res.status_code == 200:
                st.success("Balance updated")
                st.rerun()

    with st.expander(":material/pie_chart: Budget Allocation", expanded=False):
        paycheck = st.number_input("Paycheck Amount ($)", min_value=0.0, step=100.0, value=2000.0)
        sav = st.slider("Savings %", 0, 100, 20)
        ess = st.slider("Essentials %", 0, 100, 50)
        disc = st.slider("Discretionary %", 0, 100, 30)
        if st.button("Save Ratios"):
            res = requests.post(f"{API_URL}/set-ratios", json={
                "expected_paycheck": paycheck, "savings_ratio": sav,
                "essentials_ratio": ess, "discretionary_ratio": disc
            })
            if res.status_code == 200:
                st.success("Ratios saved")
                st.rerun()

# -----------------------------------------------------------------------------
# 5. HEADER & METRICS
# -----------------------------------------------------------------------------
st.markdown("# Financial Assistant")
st.caption(f"Location Target: **{user_city} ({user_zip})** | Sales Tax: **{user_tax_pct}%**")
st.markdown("---")

k1, k2, k3, k4 = st.columns(4)
k1.markdown(f'<div class="spotify-card"><div class="card-label">Bank Balance</div><div class="card-value-large">${current_bal:,.2f}</div></div>', unsafe_allow_html=True)
k2.markdown(f'<div class="spotify-card"><div class="card-label">Unpaid Obligations</div><div class="card-value-large" style="color: #f43f5e;">${unpaid_bills:,.2f}</div></div>', unsafe_allow_html=True)
k3.markdown(f'<div class="spotify-card"><div class="card-label">Safe Disposable</div><div class="card-value-large" style="color: #15803d;">${disposable:,.2f}</div></div>', unsafe_allow_html=True)
k4.markdown(f'<div class="spotify-card"><div class="card-label">Active Unpaid Bills</div><div class="card-value-large">{bills_count}</div></div>', unsafe_allow_html=True)

# -----------------------------------------------------------------------------
# 6. MAIN TABS NAVIGATION
# -----------------------------------------------------------------------------
tab_overview, tab_bills, tab_recipes, tab_simulator, tab_ledger = st.tabs([
    "Overview", "Bill Management", "Recipe & Grocery Hub", "Purchase Simulator", "Expense Ledger"
])

# --- OVERVIEW ---
with tab_overview:
    col_c1, col_c2 = st.columns(2)
    with col_c1:
        st.markdown('<div class="spotify-card">', unsafe_allow_html=True)
        st.markdown("#### Liquidity Allocation")
        alloc_df = pd.DataFrame({
            "Category": ["Unpaid Bills", "Safe Cash"],
            "Amount": [unpaid_bills, max(0.0, disposable)]
        })
        fig_donut = px.pie(
            alloc_df, names="Category", values="Amount", hole=0.6,
            color_discrete_sequence=["#F43F5E", "#15803d"]
        )
        fig_donut.update_layout(
            paper_bgcolor="rgba(0,0,0,0)", plot_bgcolor="rgba(0,0,0,0)",
            font_color="#b3b3b3", margin=dict(l=10, r=10, t=10, b=10),
            legend=dict(orientation="h", yanchor="bottom", y=-0.2, xanchor="center", x=0.5)
        )
        st.plotly_chart(fig_donut, use_container_width=True, config={"displayModeBar": False})
        st.markdown('</div>', unsafe_allow_html=True)

    with col_c2:
        st.markdown('<div class="spotify-card">', unsafe_allow_html=True)
        st.markdown("#### System Summary")
        st.write(f"• **Liquid Checking Balance:** ${current_bal:,.2f}")
        st.write(f"• **Reserved Obligations:** ${unpaid_bills:,.2f}")
        st.write(f"• **Net Uncommitted Cash:** ${disposable:,.2f}")
        st.write(f"• **Active Shopping Region:** {user_city} ({user_zip})")
        st.markdown("<br>", unsafe_allow_html=True)
        if disposable < 0:
            st.error("Alert: Reserved obligations exceed current balance.")
        else:
            st.success("Nominal: Balance fully covers all obligations.")
        st.markdown('</div>', unsafe_allow_html=True)

# --- BILL MANAGEMENT ---
with tab_bills:
    col_b_list, col_b_add = st.columns([2, 1])
    with col_b_list:
        st.markdown('<div class="spotify-card">', unsafe_allow_html=True)
        st.markdown("#### Recurring Obligations & Bills")
        if not bills_list:
            st.info("No recurring bills recorded yet.")
        else:
            for b in bills_list:
                bc1, bc2, bc3, bc4, bc5 = st.columns([3, 2, 2, 2, 2])
                with bc1: st.write(f"**{b['name']}**")
                with bc2: st.write(f"${b['amount']:,.2f}")
                with bc3: st.write(f"Due: {b['due_date']}")
                with bc4:
                    status_label = "Paid" if b['is_paid'] else "Unpaid"
                    if st.button(status_label, key=f"pay_{b['id']}"):
                        requests.post(f"{API_URL}/bills/{b['id']}/pay")
                        st.rerun()
                with bc5:
                    if st.button("Delete", key=f"del_bill_{b['id']}"):
                        requests.delete(f"{API_URL}/bills/{b['id']}")
                        st.rerun()
                st.markdown("<hr style='margin: 4px 0; border-color: #282828;'>", unsafe_allow_html=True)
        st.markdown('</div>', unsafe_allow_html=True)

    with col_b_add:
        st.markdown('<div class="spotify-card">', unsafe_allow_html=True)
        st.markdown("#### Add Recurring Bill")
        with st.form("add_bill_form"):
            b_name = st.text_input("Bill Name", placeholder="e.g. Electric Bill")
            b_amt = st.number_input("Amount ($)", min_value=0.01, step=10.0, value=75.0)
            b_date = st.date_input("Due Date")
            if st.form_submit_button("Add Bill"):
                if b_name.strip():
                    requests.post(f"{API_URL}/bills", json={"name": b_name, "amount": b_amt, "due_date": b_date.strftime("%Y-%m-%d")})
                    st.success(f"Added {b_name}")
                    st.rerun()
                else:
                    st.error("Please provide a bill name.")
        st.markdown('</div>', unsafe_allow_html=True)

# --- RECIPE & GROCERY HUB ---
with tab_recipes:
    col_r_recipes, col_r_grocery = st.columns([1, 1])
    
    with col_r_recipes:
        st.markdown('<div class="spotify-card">', unsafe_allow_html=True)
        st.markdown("#### Meal & Recipe Preferences")
        
        selected_recipe_ids = []
        for r in recipes_list:
            st.markdown(f"**{r['title']}** ({r['servings']} servings)")
            include_meal = st.checkbox(f"Plan meal for current week", key=f"rec_check_{r['id']}")
            if include_meal:
                selected_recipe_ids.append(r['id'])
            
            with st.expander("Ingredients & Swaps", expanded=False):
                for ing in r['ingredients']:
                    st.write(f"• {ing['quantity']} {ing['unit']} - **{ing['product_name']}**")
                    if ing['swap_options']:
                        st.caption(f"Swaps: {', '.join(ing['swap_options'])}")
            st.markdown("<hr style='margin: 8px 0; border-color: #282828;'>", unsafe_allow_html=True)
            
        st.markdown('</div>', unsafe_allow_html=True)

    with col_r_grocery:
        st.markdown('<div class="spotify-card">', unsafe_allow_html=True)
        st.markdown("#### Local Grocery Search & Tax Engine")
        
        target_store = st.selectbox("Preferred Store Chain", ["Walmart", "Target", "Aldi", "Kroger", "Local Market"])
        st.caption(f"Searching location: **{user_city} ({user_zip})** | Tax Rate: **{user_tax_pct}%**")
        
        if st.button("Generate Local Grocery List"):
            if not selected_recipe_ids:
                st.warning("Please check at least one meal to generate a grocery list.")
            else:
                res = requests.post(f"{API_URL}/grocery-list/generate", json={
                    "recipe_ids": selected_recipe_ids,
                    "store_name": target_store
                })
                if res.status_code == 200:
                    st.success("Grocery list updated")
                    st.rerun()

        st.markdown("---")
        st.markdown("##### Smart Grocery List (Tax Included)")
        
        if not grocery_list:
            st.info("No active grocery list generated.")
        else:
            total_cost = sum(g['estimated_price'] for g in grocery_list)
            st.markdown(f"**Estimated Local Total:** `${total_cost:,.2f}`")
            
            for g in grocery_list:
                gc1, gc2, gc3 = st.columns([3, 2, 1])
                with gc1: st.write(f"• {g['quantity']}x **{g['item_name']}**")
                with gc2: st.write(f"${g['estimated_price']:,.2f}")
                with gc3:
                    if st.button("Delete", key=f"del_g_{g['id']}"):
                        requests.delete(f"{API_URL}/grocery-list/{g['id']}")
                        st.rerun()
                        
        st.markdown('</div>', unsafe_allow_html=True)

# --- PURCHASE SIMULATOR ---
with tab_simulator:
    st.markdown('<div class="spotify-card">', unsafe_allow_html=True)
    st.markdown("#### Purchase Impact Advisor")
    st.caption("Simulate purchases against active obligations and safe disposable balance.")
    
    with st.form("desk_check_form"):
        d_item = st.text_input("Item Description", placeholder="e.g. Impact Driver")
        d_price = st.number_input("Item Price ($)", min_value=0.01, step=10.0, value=120.0)
        d_submit = st.form_submit_button("Analyze Purchase Impact")
        
        if d_submit:
            if not d_item.strip():
                st.error("Please enter a description.")
            else:
                res = requests.post(f"{API_URL}/can-i-buy", json={"item": d_item, "price": d_price})
                if res.status_code == 200:
                    r = res.json()
                    st.info(r.get("advisor_note"))
    st.markdown('</div>', unsafe_allow_html=True)

# --- EXPENSE LEDGER ---
with tab_ledger:
    col_l_form, col_l_list = st.columns([1, 2])
    with col_l_form:
        st.markdown('<div class="spotify-card">', unsafe_allow_html=True)
        st.markdown("#### Log New Expense")
        with st.form("desk_log_form"):
            l_item = st.text_input("Transaction Description", placeholder="e.g. Hardware Store")
            l_amt = st.number_input("Amount ($)", min_value=0.01, step=1.0, value=45.0)
            l_cat = st.selectbox("Category", ["discretionary", "essentials", "savings"])
            l_submit = st.form_submit_button("Post Transaction")
            
            if l_submit:
                if not l_item.strip():
                    st.error("Please enter a description.")
                else:
                    res = requests.post(f"{API_URL}/log-expense", json={"item": l_item, "amount": l_amt, "category": l_cat})
                    if res.status_code == 200:
                        st.success(f"Posted ${l_amt:.2f} for '{l_item}'.")
                        st.rerun()
        st.markdown('</div>', unsafe_allow_html=True)

    with col_l_list:
        st.markdown('<div class="spotify-card">', unsafe_allow_html=True)
        st.markdown("#### Transaction History")
        if not trans_list:
            st.info("No logged transactions found.")
        else:
            for t in trans_list:
                tc1, tc2, tc3, tc4, tc5 = st.columns([3, 2, 2, 3, 2])
                with tc1: st.write(f"**{t['description']}**")
                with tc2: st.write(f"${t['amount']:,.2f}")
                with tc3: st.caption(t['category'])
                with tc4: st.caption(t['date'])
                with tc5:
                    if st.button("Delete", key=f"del_trans_{t['id']}"):
                        requests.delete(f"{API_URL}/transactions/{t['id']}")
                        st.rerun()
                st.markdown("<hr style='margin: 4px 0; border-color: #282828;'>", unsafe_allow_html=True)
        st.markdown('</div>', unsafe_allow_html=True)