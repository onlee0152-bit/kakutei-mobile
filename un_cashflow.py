import hashlib
import io
import json
from datetime import datetime

import pandas as pd
import streamlit as st
from google import genai
from google.genai import types
from PIL import Image, ImageOps

st.set_page_config(
    page_title="UN｜金持ちのキャッシュフロー",
    page_icon="🌊",
    layout="centered",
    initial_sidebar_state="collapsed",
)

st.markdown(
    """
    <style>
    [data-testid="stHeader"],
    [data-testid="stToolbar"],
    #MainMenu,
    footer {
        display: none !important;
    }
    .block-container {
        padding-top: 1.2rem !important;
        padding-bottom: 2.0rem !important;
    }
    .un-header {
        background: #1e293b;
        color: #f8fafc;
        padding: 18px 20px;
        border-radius: 16px;
        margin-bottom: 16px;
    }
    .un-brand {
        font-size: 0.85rem;
        letter-spacing: 0.14em;
        color: #cbd5e1;
        margin-bottom: 4px;
    }
    .un-title {
        font-size: 1.35rem;
        font-weight: 700;
        margin: 0;
    }
    .un-sub {
        font-size: 0.88rem;
        color: #94a3b8;
        margin-top: 4px;
    }
    .un-badge-row {
        display: flex;
        justify-content: space-between;
        align-items: center;
        background: #f5f3ef;
        border: 1px solid #e2ddd5;
        border-radius: 12px;
        padding: 12px 16px;
        margin-bottom: 16px;
    }
    .un-stage {
        font-weight: 700;
        color: #1f2937;
        font-size: 0.98rem;
    }
    .un-wallet {
        font-weight: 700;
        color: #0f172a;
        background: #ffffff;
        border: 1px solid #d6d3cd;
        padding: 4px 12px;
        border-radius: 999px;
        font-size: 0.95rem;
    }
    </style>
    """,
    unsafe_allow_html=True,
)

PRIMARY_MODEL = "gemini-3.1-flash-lite"

INVEST_CATEGORIES = {"広告宣伝費", "外注工賃", "新聞図書費"}
WATCH_CATEGORIES = {"接待交際費", "会議費", "雑費"}

ACCOUNT_CATEGORIES = [
    "消耗品費", "旅費交通費", "接待交際費", "会議費", "通信費",
    "水道光熱費", "地代家賃", "新聞図書費", "広告宣伝費", "車両費",
    "支払手数料", "外注工賃", "修繕費", "租税公課", "雑費",
]


def get_secret(key: str) -> str:
    try:
        return str(st.secrets.get(key, "")).strip()
    except Exception:
        return ""


def map_cf_attribute(category: str, flow_type: str) -> str:
    if flow_type == "入金":
        return "入金・売上"
    if category in INVEST_CATEGORIES:
        return "未来への投資"
    if category in WATCH_CATEGORIES:
        return "見直し余地"
    return "事業・生活維持"


def make_row_hash(date_str: str, flow_type: str, store: str, amount: int) -> str:
    raw = f"{str(date_str).strip()}|{flow_type}|{str(store).strip().lower()}|{int(amount)}"
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def init_session():
    defaults = {
        "cf_records": [],
        "row_hashes": set(),
        "opening_balance": 500000,
        "tax_reserve_rate": 20,
        "un_balance": 0,
        "un_logs": [],
        "awarded_milestones": set(),
        "last_msg": None,
    }
    for k, v in defaults.items():
        if k not in st.session_state:
            st.session_state[k] = v


def award_un(amount: int, reason: str, milestone_key: str = ""):
    if milestone_key:
        if milestone_key in st.session_state["awarded_milestones"]:
            return
        st.session_state["awarded_milestones"].add(milestone_key)
    st.session_state["un_balance"] += int(amount)
    st.session_state["un_logs"].insert(
        0,
        {
            "日時": datetime.now().strftime("%Y-%m-%d %H:%M"),
            "内容": reason,
            "獲得UN": f"+{int(amount):,} UN",
        },
    )


def add_record(date_str: str, flow_type: str, partner: str, category: str, amount: int, memo: str = "") -> bool:
    amt = int(amount or 0)
    if amt <= 0:
        return False
    h = make_row_hash(date_str, flow_type, partner, amt)
    if h in st.session_state["row_hashes"]:
        return False

    st.session_state["row_hashes"].add(h)
    st.session_state["cf_records"].append(
        {
            "日付": str(date_str or datetime.now().strftime("%Y-%m-%d"))[:10],
            "区分": flow_type,
            "取引先・内容": str(partner or ""),
            "勘定科目": str(category or ("売上高" if flow_type == "入金" else "雑費")),
            "CF属性": map_cf_attribute(category, flow_type),
            "金額": amt,
            "メモ": str(memo or ""),
        }
    )
    return True


def optimize_image(file_bytes: bytes, max_px: int = 1400, quality: int = 80) -> bytes:
    try:
        img = Image.open(io.BytesIO(file_bytes))
        img = ImageOps.exif_transpose(img)
        if img.mode in ("RGBA", "P", "LA"):
            img = img.convert("RGB")
        w, h = img.size
        if max(w, h) > max_px:
            scale = max_px / float(max(w, h))
            img = img.resize((max(1, int(w * scale)), max(1, int(h * scale))), Image.Resampling.LANCZOS)
        buf = io.BytesIO()
        img.save(buf, format="JPEG", quality=quality, optimize=True)
        return buf.getvalue()
    except Exception:
        return file_bytes


def parse_smartpay_screenshot(img_bytes: bytes) -> list[dict]:
    api_key = get_secret("GEMINI_API_KEY")
    if not api_key:
        raise RuntimeError("GEMINI_API_KEY が Secrets に設定されていません。")

    client = genai.Client(api_key=api_key)
    today_year = datetime.now().year
    prompt = (
        "これはPayPay、クレジットカード、モバイルPASMO、銀行アプリなどの利用履歴スクリーンショットです。\n"
        "画面に表示されている決済・入出金の履歴をすべて抽出し、JSON配列（items）で返してください。\n"
        f"- date: YYYY-MM-DD形式（年の記載がなければ {today_year} 年として補完）\n"
        "- flow_type: 出金（支払い・購入）なら '出金'、入金（チャージ返金・振込受取等）なら '入金'\n"
        "- partner: 店舗名・サービス名・振込元\n"
        f"- category: 出金の場合は次から1つ選択: {', '.join(ACCOUNT_CATEGORIES)}。入金の場合は '売上・入金'\n"
        "- amount: 金額（正の整数）\n"
        "- memo: 決済手段や補足があれば簡潔に"
    )

    schema = {
        "type": "OBJECT",
        "properties": {
            "items": {
                "type": "ARRAY",
                "items": {
                    "type": "OBJECT",
                    "properties": {
                        "date": {"type": "STRING"},
                        "flow_type": {"type": "STRING"},
                        "partner": {"type": "STRING"},
                        "category": {"type": "STRING"},
                        "amount": {"type": "INTEGER"},
                        "memo": {"type": "STRING"},
                    },
                    "required": ["date", "flow_type", "partner", "category", "amount", "memo"],
                },
            }
        },
        "required": ["items"],
    }

    res = client.models.generate_content(
        model=PRIMARY_MODEL,
        contents=[types.Part.from_bytes(data=img_bytes, mime_type="image/jpeg"), prompt],
        config=types.GenerateContentConfig(
            response_mime_type="application/json",
            response_schema=schema,
            temperature=0.1,
        ),
    )
    raw = (res.text or "").strip()
    if raw.startswith("```"):
        raw = "\n".join(ln for ln in raw.splitlines() if not ln.strip().startswith("```")).strip()
    parsed = json.loads(raw)
    return parsed.get("items", [])


def import_kakutei_excels(uploaded_files) -> tuple[int, int]:
    added = 0
    skipped = 0
    for f in uploaded_files:
        try:
            xls = pd.ExcelFile(f)
            sheet = "経費明細一覧" if "経費明細一覧" in xls.sheet_names else xls.sheet_names[0]
            df = pd.read_excel(xls, sheet_name=sheet)
            for _, row in df.iterrows():
                date_val = str(row.get("日付", "")).strip()
                partner = str(row.get("取引先・店名", row.get("取引先・内容", ""))).strip()
                cat = str(row.get("勘定科目", "雑費")).strip()
                amt_raw = row.get("税込金額", row.get("金額", 0))
                amt = int(pd.to_numeric(amt_raw, errors="coerce") or 0)
                memo = str(row.get("摘要・品目", row.get("メモ", ""))).strip()
                flow_type = str(row.get("区分", "出金")).strip()
                if flow_type not in ("入金", "出金"):
                    flow_type = "出金"
                if amt > 0:
                    if add_record(date_val, flow_type, partner, cat, amt, memo):
                        added += 1
                    else:
                        skipped += 1
        except Exception as exc:
            st.error(f"{f.name} の読み込みに失敗しました: {exc}")
    return added, skipped


def build_cf_excel(df_all: pd.DataFrame, monthly_df: pd.DataFrame) -> bytes:
    out = io.BytesIO()
    with pd.ExcelWriter(out, engine="openpyxl") as writer:
        df_all.to_excel(writer, index=False, sheet_name="入出金統合明細")
        monthly_df.to_excel(writer, index=False, sheet_name="月次キャッシュフロー")
    return out.getvalue()


init_session()

# --- 計算ロジック ---
df_all = pd.DataFrame(st.session_state["cf_records"])
if not df_all.empty:
    df_all["金額"] = pd.to_numeric(df_all["金額"], errors="coerce").fillna(0).astype(int)
    df_all["年月"] = df_all["日付"].astype(str).str.slice(0, 7)
    total_in = int(df_all.loc[df_all["区分"] == "入金", "金額"].sum())
    total_out = int(df_all.loc[df_all["区分"] == "出金", "金額"].sum())
    invest_out = int(df_all.loc[(df_all["区分"] == "出金") & (df_all["CF属性"] == "未来への投資"), "金額"].sum())
    watch_out = int(df_all.loc[(df_all["区分"] == "出金") & (df_all["CF属性"] == "見直し余地"), "金額"].sum())
    months_count = max(1, df_all["年月"].nunique())
else:
    total_in = 0
    total_out = 0
    invest_out = 0
    watch_out = 0
    months_count = 1

net_cf = total_in - total_out
current_cash = int(st.session_state["opening_balance"]) + net_cf
avg_monthly_out = (total_out / months_count) if total_out > 0 else 0
runway_months = (current_cash / avg_monthly_out) if avg_monthly_out > 0 else 0.0
retention_rate = ((net_cf + invest_out) / total_in * 100.0) if total_in > 0 else 0.0
watch_ratio = (watch_out / total_out * 100.0) if total_out > 0 else 0.0

# 準備度（Readiness）判定
cond_a = (total_in > 0) and (total_out > 0)
cond_b = (retention_rate >= 10.0) and (watch_ratio < 20.0)
cond_c = runway_months >= 3.0
score = sum([cond_a, cond_b, cond_c])

if score == 3:
    stage_label = "Stage 3：循環（好機待機）"
elif score == 2:
    stage_label = "Stage 2：流れの安定"
else:
    stage_label = "Stage 1：整頓開始"

# マイルストーンUN付与
if retention_rate >= 10.0 and total_in > 0:
    award_un(500, "バビロン基準（残存率10%以上）達成", "milestone_babylon_10")
if runway_months >= 3.0 and total_out > 0:
    award_un(1000, "現金余白（Runway 3ヶ月以上）突破", "milestone_runway_3m")

# --- ヘッダー ---
st.markdown(
    """
    <div class="un-header">
      <div class="un-brand">UN — PREPARE THE VESSEL. LET THE FLOW BEGIN.</div>
      <div class="un-title">金持ちのキャッシュフロー</div>
      <div class="un-sub">事実を統合し、残るお金と余白を整える</div>
    </div>
    """,
    unsafe_allow_html=True,
)

st.markdown(
    f"""
    <div class="un-badge-row">
      <div class="un-stage">{stage_label}</div>
      <div class="un-wallet">UN紙幣：{st.session_state['un_balance']:,} UN</div>
    </div>
    """,
    unsafe_allow_html=True,
)

if st.session_state["last_msg"]:
    lvl, txt = st.session_state["last_msg"]
    if lvl == "success":
        st.success(txt)
    elif lvl == "warning":
        st.warning(txt)
    else:
        st.info(txt)
    st.session_state["last_msg"] = None

tab_dash, tab_import, tab_vessels, tab_data = st.tabs(
    ["流れ", "取り込み・記録", "3つの器", f"明細・出力 ({len(st.session_state['cf_records'])})"]
)

# ==========================================
# タブ1：流れ（月別ビューアー・ダッシュボード）
# ==========================================
with tab_dash:
    c1, c2, c3 = st.columns(3)
    c1.metric("純キャッシュフロー", f"¥{net_cf:+,}")
    c2.metric("現金余白 (Runway)", f"{runway_months:.1f} ヶ月" if avg_monthly_out > 0 else "—")
    c3.metric("残存・投資率", f"{retention_rate:.1f}%" if total_in > 0 else "—")

    st.caption(f"現在手元残高目安：¥{current_cash:,}（期首残高 ¥{st.session_state['opening_balance']:,} ＋ 純CF ¥{net_cf:+,}）")

    if df_all.empty:
        st.info("まずは「取り込み・記録」タブから、第1弾のExcelやスマート決済のスクショを取り込んでください。")
    else:
        # 月次集計テーブル
        monthly_in = df_all[df_all["区分"] == "入金"].groupby("年月")["金額"].sum()
        monthly_out = df_all[df_all["区分"] == "出金"].groupby("年月")["金額"].sum()
        months = sorted(df_all["年月"].unique())
        m_rows = []
        running_bal = int(st.session_state["opening_balance"])
        for m in months:
            inc = int(monthly_in.get(m, 0))
            out = int(monthly_out.get(m, 0))
            diff = inc - out
            running_bal += diff
            m_rows.append({"年月": m, "入金": inc, "出金": out, "純CF": diff, "月末残高": running_bal})
        monthly_df = pd.DataFrame(m_rows)

        st.markdown("#### 月次キャッシュフロー推移")
        st.bar_chart(monthly_df.set_index("年月")[["入金", "出金"]])
        st.dataframe(monthly_df, use_container_width=True, hide_index=True)

        st.markdown("#### 支出の質（お金の行き先）")
        out_df = df_all[df_all["区分"] == "出金"]
        if not out_df.empty:
            attr_df = out_df.groupby("CF属性", as_index=False)["金額"].sum()
            st.bar_chart(attr_df.set_index("CF属性")["金額"])
            st.dataframe(attr_df, use_container_width=True, hide_index=True)

# ==========================================
# タブ2：取り込み・記録
# ==========================================
with tab_import:
    st.markdown("#### ① 第1弾（確定申告 Pro）Excelの一括合体")
    st.caption("フォルダに溜まった複数のExcelをまとめて選択できます。重複データは自動でカットします。")
    excel_files = st.file_uploader(
        "Excelファイル (.xlsx) を複数選択",
        type=["xlsx"],
        accept_multiple_files=True,
        key="multi_excel_up",
    )
    if excel_files and st.button("選択したExcelを一括合体する", use_container_width=True):
        added, skipped = import_kakutei_excels(excel_files)
        if added > 0:
            award_un(200, f"Excel一括合体（{added}件追加）")
            st.session_state["last_msg"] = ("success", f"{added} 件を取り込みました（重複スキップ：{skipped} 件）。+200 UN 獲得！")
        else:
            st.session_state["last_msg"] = ("warning", f"新規追加はありませんでした（重複スキップ：{skipped} 件）。")
        st.rerun()

    st.divider()

    st.markdown("#### ② スマート決済（PayPay・クレカ・PASMO）スクショ読取")
    st.caption("利用履歴画面のスクショをアップすると、写っている複数件をまとめてデータ化します。")
    shot_file = st.file_uploader(
        "利用履歴のスクリーンショットを選択",
        type=["jpg", "jpeg", "png", "webp"],
        accept_multiple_files=False,
        key="smartpay_shot_up",
    )
    if shot_file is not None and st.button("スクショから履歴を一括抽出する", use_container_width=True):
        with st.spinner("履歴を解析中..."):
            try:
                opt_bytes = optimize_image(shot_file.getvalue())
                items = parse_smartpay_screenshot(opt_bytes)
                added = 0
                skipped = 0
                for it in items:
                    ok = add_record(
                        it.get("date", ""),
                        it.get("flow_type", "出金"),
                        it.get("partner", ""),
                        it.get("category", "雑費"),
                        it.get("amount", 0),
                        it.get("memo", "スクショ読取"),
                    )
                    if ok:
                        added += 1
                    else:
                        skipped += 1
                if added > 0:
                    award_un(150, f"スマート決済スクショ読取（{added}件）")
                    st.session_state["last_msg"] = ("success", f"スクショから {added} 件を登録しました（重複：{skipped} 件）。+150 UN 獲得！")
                else:
                    st.session_state["last_msg"] = ("warning", "新規で登録できる履歴が見つかりませんでした。")
                st.rerun()
            except Exception as exc:
                st.error(f"解析エラー: {exc}")

    st.divider()

    st.markdown("#### ③ 入金（売上・報酬）のクイック登録")
    with st.form("income_form", clear_on_submit=True):
        col_d, col_a = st.columns(2)
        in_date = col_d.date_input("入金日", value=datetime.now().date())
        in_amt = col_a.number_input("入金額（円）", min_value=0, step=10000, value=0)
        in_partner = st.text_input("入金元・案件名", placeholder="例：〇〇株式会社 売上振込")
        in_cat = st.selectbox("区分", ["事業売上", "配当・資産収入", "その他入金"])
        in_memo = st.text_input("メモ（任意）")
        if st.form_submit_button("入金を記録する", use_container_width=True):
            if in_amt > 0:
                if add_record(in_date.strftime("%Y-%m-%d"), "入金", in_partner or in_cat, in_cat, int(in_amt), in_memo):
                    award_un(100, f"入金記録（¥{int(in_amt):,}）")
                    st.session_state["last_msg"] = ("success", f"入金 ¥{int(in_amt):,} を記録しました。+100 UN 獲得！")
                    st.rerun()
                else:
                    st.warning("同じ内容の入金がすでに登録されています。")
            else:
                st.warning("入金額を入力してください。")

    st.divider()
    st.markdown("#### ④ 期首残高（スタート時の手元現金）")
    new_bal = st.number_input(
        "現在の口座＋現金の合計（円）",
        min_value=0,
        step=50000,
        value=int(st.session_state["opening_balance"]),
    )
    if new_bal != st.session_state["opening_balance"]:
        st.session_state["opening_balance"] = int(new_bal)
        st.rerun()

# ==========================================
# タブ3：3つの器（ユダヤ式・バビロン式配分）
# ==========================================
with tab_vessels:
    st.markdown("#### 混ぜないための自動配分（3つの器）")
    base_profit = max(0, net_cf)
    tax_rate = st.slider("納税リザーブ率（%）", min_value=0, max_value=40, value=int(st.session_state["tax_reserve_rate"]))
    st.session_state["tax_reserve_rate"] = tax_rate

    tax_reserve = int(base_profit * (tax_rate / 100.0))
    free_cash = max(0, base_profit - tax_reserve)

    st.metric("最優先：納税リザーブ（手をつけないお金）", f"¥{tax_reserve:,}")
    st.caption(f"残りの自由配分キャッシュ：¥{free_cash:,}")

    col_v1, col_v2, col_v3 = st.columns(3)
    w_guard = col_v1.number_input("守り（手元現金）%", min_value=0, max_value=100, value=34)
    w_grow = col_v2.number_input("攻め（事業・自己投資）%", min_value=0, max_value=100, value=33)
    w_asset = col_v3.number_input("育てる（長期資産）%", min_value=0, max_value=100, value=33)

    w_sum = max(1, w_guard + w_grow + w_asset)
    v_guard = int(free_cash * (w_guard / w_sum))
    v_grow = int(free_cash * (w_grow / w_sum))
    v_asset = free_cash - v_guard - v_grow

    v_df = pd.DataFrame(
        [
            {"器": "器A：守り（手元現金・好機待機）", "配分比率": f"{w_guard/w_sum*100:.1f}%", "配分目安額": f"¥{v_guard:,}"},
            {"器": "器B：攻め（事業・自己投資）", "配分比率": f"{w_grow/w_sum*100:.1f}%", "配分目安額": f"¥{v_grow:,}"},
            {"器": "器C：育てる（長期資産・積立）", "配分比率": f"{w_asset/w_sum*100:.1f}%", "配分目安額": f"¥{v_asset:,}"},
        ]
    )
    st.dataframe(v_df, use_container_width=True, hide_index=True)

# ==========================================
# タブ4：明細・出力・UN紙幣履歴
# ==========================================
with tab_data:
    if df_all.empty:
        st.info("まだ明細データがありません。")
    else:
        st.markdown("#### 統合明細の編集")
        edited_df = st.data_editor(
            df_all.drop(columns=["年月"]),
            num_rows="dynamic",
            use_container_width=True,
            key="cf_editor",
        )
        if not edited_df.equals(df_all.drop(columns=["年月"])):
            st.session_state["cf_records"] = edited_df.to_dict(orient="records")
            st.rerun()

        # 月次集計を再構築してExcel出力
        monthly_in = df_all[df_all["区分"] == "入金"].groupby("年月")["金額"].sum()
        monthly_out = df_all[df_all["区分"] == "出金"].groupby("年月")["金額"].sum()
        months = sorted(df_all["年月"].unique())
        m_rows = []
        running_bal = int(st.session_state["opening_balance"])
        for m in months:
            inc = int(monthly_in.get(m, 0))
            out = int(monthly_out.get(m, 0))
            diff = inc - out
            running_bal += diff
            m_rows.append({"年月": m, "入金": inc, "出金": out, "純CF": diff, "月末残高": running_bal})
        monthly_export_df = pd.DataFrame(m_rows)

        excel_bytes = build_cf_excel(df_all.drop(columns=["年月"]), monthly_export_df)
        st.download_button(
            label="統合キャッシュフロー Excel ダウンロード (.xlsx)",
            data=excel_bytes,
            file_name=f"un_cashflow_{datetime.now().strftime('%Y%m%d_%H%M')}.xlsx",
            mime="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
            use_container_width=True,
        )

        if st.button("データをすべてクリア", use_container_width=True):
            st.session_state["cf_records"] = []
            st.session_state["row_hashes"] = set()
            st.rerun()

    st.divider()
    st.markdown("#### デジタル紙幣「UN」獲得履歴")
    if st.session_state["un_logs"]:
        st.dataframe(pd.DataFrame(st.session_state["un_logs"]), use_container_width=True, hide_index=True)
    else:
        st.caption("データを取り込むとUN紙幣が貯まります。")
