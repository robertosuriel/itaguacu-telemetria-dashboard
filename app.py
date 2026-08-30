# -*- coding: utf-8 -*-
# Dashboard local (Streamlit) lendo direto do Supabase da Estação Solarimétrica.
# Rodar: python -m streamlit run app.py

import os
from datetime import date, datetime, timedelta, timezone
from zoneinfo import ZoneInfo

import pandas as pd
import plotly.graph_objects as go
import streamlit as st
from dotenv import load_dotenv
from streamlit_autorefresh import st_autorefresh
from supabase import create_client

load_dotenv()

# ts no banco e sempre UTC. A usina fica no Brasil (sem horario de verao desde 2019),
# entao converter pra local aqui equivale a UTC-3 — mas usar o nome IANA em vez do
# offset fixo deixa isso a prova de qualquer mudanca futura de fuso.
LOCAL_TZ = ZoneInfo("America/Sao_Paulo")


def local_day_bounds_utc(day):
    # "day" e uma data sem fuso (do st.date_input). Sem isso, comparar direto contra
    # a coluna ts (timestamptz) faz o Postgres tratar "day.isoformat()" como meia-noite
    # UTC, nao meia-noite local — um dia selecionado como "25/08" acabava trazendo
    # 21h de 24/08 a 21h de 25/08 no horario real da usina, e a mesma confusao inverte
    # o cursor "date.today()" perto da virada do dia. Ancorar em LOCAL_TZ corrige os
    # dois: o range da consulta e (por consequencia) os graficos que dependem dele.
    start_local = datetime.combine(day, datetime.min.time(), tzinfo=LOCAL_TZ)
    end_local = start_local + timedelta(days=1)
    return start_local.astimezone(timezone.utc).isoformat(), end_local.astimezone(timezone.utc).isoformat()

# Local: le do .env (python-dotenv). Streamlit Community Cloud: le do cofre de
# secrets configurado no painel deles (nunca do .env, que nao vai pro repositorio).
# st.secrets lanca StreamlitSecretNotFoundError se nao existir NENHUM secrets.toml
# (caso do dev local, que so tem .env) — por isso o try/except em vez de .get().
try:
    SUPABASE_URL = st.secrets["SUPABASE_URL"]
    SUPABASE_KEY = st.secrets["SUPABASE_SERVICE_KEY"]
except (FileNotFoundError, KeyError, st.errors.StreamlitSecretNotFoundError):
    SUPABASE_URL = os.environ.get("SUPABASE_URL")
    SUPABASE_KEY = os.environ.get("SUPABASE_SERVICE_KEY")

st.set_page_config(page_title="Usina Itaguaçu — Telemetria", layout="wide", page_icon="☀️")

if not SUPABASE_URL or not SUPABASE_KEY:
    st.error("Faltam SUPABASE_URL / SUPABASE_SERVICE_KEY no .env (copie de .env.example e preencha).")
    st.stop()

supabase = create_client(SUPABASE_URL, SUPABASE_KEY)

REFRESH_MS = 30_000
st_autorefresh(interval=REFRESH_MS, key="auto_refresh")

COLOR_REAL = "#1baf7a"
COLOR_TEORICA = "#2a78d6"


@st.cache_data(ttl=30)
def load_usinas():
    res = supabase.table("usinas").select("*").order("slug").execute()
    return pd.DataFrame(res.data)


@st.cache_data(ttl=15)
def load_latest_reading(device_id):
    if not device_id:
        return None
    # Ordena por created_at (confiavel, gerado pelo Postgres), nao por ts — uma
    # linha com ts contaminado/rotulado no futuro (residuo do bug de relogio do
    # PC industrial) apareceria como "a mais recente" se ordenasse por ts,
    # mesmo nao sendo de verdade a ultima leitura recebida.
    res = (
        supabase.table("readings")
        .select("ts, metrics, created_at")
        .eq("device_id", device_id)
        .order("created_at", desc=True)
        .limit(1)
        .execute()
    )
    return res.data[0] if res.data else None


@st.cache_data(ttl=60)
def load_daily_performance(usina_id, start_date, end_date):
    res = (
        supabase.table("v_performance_diaria")
        .select("*")
        .eq("usina_id", usina_id)
        .gte("dia", start_date.isoformat())
        .lte("dia", end_date.isoformat())
        .order("dia")
        .execute()
    )
    return pd.DataFrame(res.data)


@st.cache_data(ttl=30)
def load_teorica_dia(usina_id, day):
    start, end = local_day_bounds_utc(day)
    res = (
        supabase.table("v_geracao_teorica")
        .select("ts, potencia_teorica_kw")
        .eq("usina_id", usina_id)
        .gte("ts", start)
        .lt("ts", end)
        .order("ts")
        .execute()
    )
    df = pd.DataFrame(res.data)
    if not df.empty:
        df["ts"] = pd.to_datetime(df["ts"], utc=True).dt.tz_convert(LOCAL_TZ)
    return df


@st.cache_data(ttl=30)
def load_medidor_dia(device_id, day):
    if not device_id:
        return pd.DataFrame()
    start, end = local_day_bounds_utc(day)
    res = (
        supabase.table("readings")
        .select("ts, metrics")
        .eq("device_id", device_id)
        .gte("ts", start)
        .lt("ts", end)
        .order("ts")
        .execute()
    )
    rows = []
    for r in res.data:
        m = r.get("metrics") or {}
        p3f = m.get("P3f", {}).get("avg")
        direta = m.get("Direta", {}).get("avg")
        rows.append({"ts": r["ts"], "potencia_real_kw": (p3f / 1000) if p3f is not None else None, "direta_kwh": direta})
    df = pd.DataFrame(rows)
    if not df.empty:
        df["ts"] = pd.to_datetime(df["ts"], utc=True).dt.tz_convert(LOCAL_TZ)
    return df


def freshness_badge(ts_str):
    if not ts_str:
        return "⚪ sem dado"
    ts = datetime.fromisoformat(ts_str.replace("Z", "+00:00"))
    age_min = (datetime.now(timezone.utc) - ts).total_seconds() / 60
    if age_min < 10:
        return f"🟢 atualizado há {age_min:.0f} min"
    elif age_min < 60:
        return f"🟡 atualizado há {age_min:.0f} min"
    else:
        return f"🔴 sem dado novo há {age_min/60:.1f} h"


# ---------------- Sidebar ----------------
usinas_df = load_usinas()
if usinas_df.empty:
    st.error("Não achei nenhuma usina na tabela `usinas`.")
    st.stop()

usinas_df["label"] = usinas_df["name"]
default_idx = int(usinas_df.index[usinas_df["slug"] == "itaguacu-7"][0])
usina_label = st.sidebar.selectbox("Usina", usinas_df["label"], index=default_idx)
usina = usinas_df[usinas_df["label"] == usina_label].iloc[0]

st.sidebar.markdown("---")
st.sidebar.caption(f"kWp instalado: **{usina['kwp_instalado']}**")
st.sidebar.caption(f"Corte de injeção: **{usina['corte_kw']} kW**")
st.sidebar.caption(f"PR esperado: **{float(usina['pr_esperado'])*100:.0f}%**")
st.sidebar.markdown("---")
st.sidebar.caption(f"Atualiza a cada {REFRESH_MS // 1000}s")

tem_medidor_proprio = bool(usina.get("own_meter_device_id"))

# ---------------- Header / KPIs ----------------
st.title(f"☀️ {usina['name']}")

estacao_reading = load_latest_reading(usina.get("reference_station_device_id"))
medidor_reading = load_latest_reading(usina.get("own_meter_device_id")) if tem_medidor_proprio else None

col1, col2, col3, col4 = st.columns(4)

irr = None
if estacao_reading:
    irr = (estacao_reading.get("metrics") or {}).get("irradiancia_w_m2", {}).get("avg")
col1.metric("Irradiância", f"{irr:.0f} W/m²" if irr is not None else "—")
col1.caption(freshness_badge(estacao_reading["ts"] if estacao_reading else None))

if tem_medidor_proprio and medidor_reading:
    p3f = (medidor_reading.get("metrics") or {}).get("P3f", {}).get("avg")
    direta = (medidor_reading.get("metrics") or {}).get("Direta", {}).get("avg")
    col2.metric("Potência real (medidor)", f"{p3f/1000:.1f} kW" if p3f is not None else "—")
    col2.caption(freshness_badge(medidor_reading["ts"]))
    col3.metric("Direta acumulada", f"{direta:,.1f} kWh".replace(",", ".") if direta is not None else "—")
else:
    col2.metric("Potência real (medidor)", "sem medidor próprio")
    col3.metric("Direta acumulada", "—")

if irr is not None:
    p_teorica = min((irr / 1000) * float(usina["kwp_instalado"]) * float(usina["pr_esperado"]), float(usina["corte_kw"]))
    col4.metric("Potência teórica (agora)", f"{p_teorica:.1f} kW")
else:
    col4.metric("Potência teórica (agora)", "—")

st.markdown("---")

# ================= Gráfico 1 — Energia diária (kWh/dia), filtro de período =================
st.subheader("1. Energia diária")

default_end = date.today()
default_start = default_end - timedelta(days=30)
periodo = st.date_input("Período", value=(default_start, default_end), key="periodo_diario")
if isinstance(periodo, tuple) and len(periodo) == 2:
    start_date, end_date = periodo
else:
    start_date, end_date = default_start, default_end

daily = load_daily_performance(usina["id"], start_date, end_date)

# ---- Resumo acumulado do período selecionado (mesmos dados do gráfico abaixo, só
# somados em vez de dia a dia — resposta direta pra "quanto gerou nesse intervalo") ----
if not daily.empty:
    tem_real = "energia_real_kwh" in daily and daily["energia_real_kwh"].notna().any()
    tem_teorica = "energia_teorica_kwh" in daily and daily["energia_teorica_kwh"].notna().any()
    total_real = daily["energia_real_kwh"].sum() if tem_real else None
    total_teorica = daily["energia_teorica_kwh"].sum() if tem_teorica else None
    # PR do período = soma real / soma teórica (não a média dos PRs diários — isso
    # pesaria dias de baixa geração igual a dias de alta, distorcendo o resultado).
    pr_periodo = (total_real / total_teorica) if total_real is not None and total_teorica else None

    st.markdown(f"**Acumulado no período** ({start_date.strftime('%d/%m/%Y')} a {end_date.strftime('%d/%m/%Y')})")
    rc1, rc2, rc3 = st.columns(3)
    rc1.metric("Real (Direta)", f"{total_real:,.0f} kWh".replace(",", ".") if total_real is not None else "—")
    rc2.metric("Teórica (irradiância)", f"{total_teorica:,.0f} kWh".replace(",", ".") if total_teorica is not None else "—")
    rc3.metric("PR do período", f"{pr_periodo * 100:.1f}%" if pr_periodo is not None else "—")

fig1 = go.Figure()
if not daily.empty:
    daily["dia"] = pd.to_datetime(daily["dia"])
    if "energia_real_kwh" in daily and daily["energia_real_kwh"].notna().any():
        fig1.add_trace(go.Bar(x=daily["dia"], y=daily["energia_real_kwh"], name="Real (Direta)", marker_color=COLOR_REAL))
    if "energia_teorica_kwh" in daily and daily["energia_teorica_kwh"].notna().any():
        fig1.add_trace(go.Scatter(x=daily["dia"], y=daily["energia_teorica_kwh"], name="Teórica (irradiância)", line=dict(color=COLOR_TEORICA, width=2)))
    fig1.update_layout(height=380, margin=dict(l=10, r=10, t=10, b=10), yaxis_title="kWh", legend=dict(orientation="h", y=1.1))
    st.plotly_chart(fig1, use_container_width=True)
    st.download_button(
        "⬇️ Exportar CSV (energia diária do período)",
        data=daily.to_csv(index=False).encode("utf-8"),
        file_name=f"energia_diaria_{usina['slug']}_{start_date}_a_{end_date}.csv",
        mime="text/csv",
    )
else:
    st.info("Sem dados de energia diária nesse período.")

st.caption("⚠️ Energia teórica pode estar subcontada em dias com falha de comunicação com a estação (janelas de irradiância faltando). O PR do período usa soma real / soma teórica, não a média dos PRs diários.")

st.markdown("---")

# ---------------- Filtro de dia único (gráficos 2 e 3) ----------------
dia_unico = st.date_input("Dia (gráficos 2 e 3)", value=date.today(), key="dia_unico")

teorica_dia = load_teorica_dia(usina["id"], dia_unico)
medidor_dia = load_medidor_dia(usina.get("own_meter_device_id"), dia_unico) if tem_medidor_proprio else pd.DataFrame()

# ================= Gráfico 2 — Potência a cada 5min (kW) =================
st.subheader("2. Potência ao longo do dia (kW)")

fig2 = go.Figure()
if not teorica_dia.empty:
    fig2.add_trace(go.Scatter(x=teorica_dia["ts"], y=teorica_dia["potencia_teorica_kw"], name="Estação (teórica)", line=dict(color=COLOR_TEORICA, width=2)))
if not medidor_dia.empty and medidor_dia["potencia_real_kw"].notna().any():
    fig2.add_trace(go.Scatter(x=medidor_dia["ts"], y=medidor_dia["potencia_real_kw"], name="Medidor (P3f)", line=dict(color=COLOR_REAL, width=2)))
if teorica_dia.empty and medidor_dia.empty:
    st.info("Sem dados de potência para esse dia.")
else:
    fig2.update_layout(height=360, margin=dict(l=10, r=10, t=10, b=10), yaxis_title="kW", legend=dict(orientation="h", y=1.1))
    st.plotly_chart(fig2, use_container_width=True)

# ================= Gráfico 3 — Energia acumulada no dia (kWh) =================
st.subheader("3. Energia acumulada ao longo do dia (kWh)")

fig3 = go.Figure()
has_data3 = False

if not teorica_dia.empty:
    teorica_dia_sorted = teorica_dia.sort_values("ts").copy()
    teorica_dia_sorted["energia_acumulada_kwh"] = (teorica_dia_sorted["potencia_teorica_kw"] * (300 / 3600)).cumsum()
    fig3.add_trace(go.Scatter(x=teorica_dia_sorted["ts"], y=teorica_dia_sorted["energia_acumulada_kwh"], name="Estação (teórica, acumulada)", line=dict(color=COLOR_TEORICA, width=2)))
    has_data3 = True

if not medidor_dia.empty and medidor_dia["direta_kwh"].notna().any():
    medidor_dia_sorted = medidor_dia.dropna(subset=["direta_kwh"]).sort_values("ts").copy()
    # Direta e um acumulador de vida inteira do medidor (nao zera no dia) — pra comparar
    # com a teorica (que comeca em 0 as 00:00), subtrai a primeira leitura do dia.
    baseline = medidor_dia_sorted["direta_kwh"].iloc[0]
    medidor_dia_sorted["energia_acumulada_kwh"] = medidor_dia_sorted["direta_kwh"] - baseline
    fig3.add_trace(go.Scatter(x=medidor_dia_sorted["ts"], y=medidor_dia_sorted["energia_acumulada_kwh"], name="Medidor (Direta, acumulado no dia)", line=dict(color=COLOR_REAL, width=2)))
    has_data3 = True

if has_data3:
    fig3.update_layout(height=360, margin=dict(l=10, r=10, t=10, b=10), yaxis_title="kWh acumulado", legend=dict(orientation="h", y=1.1))
    st.plotly_chart(fig3, use_container_width=True)
else:
    st.info("Sem dados de energia acumulada para esse dia.")

st.caption("O acumulado do medidor é reindexado pra começar em 0 na primeira leitura do dia (o Direta bruto é um acumulador desde a instalação do medidor, não zera todo dia).")

# ---- Exporta CSV com tudo que alimenta os gráficos 2 e 3 do dia selecionado ----
if not teorica_dia.empty or not medidor_dia.empty:
    export_dia = pd.DataFrame({"ts": []})
    if not teorica_dia.empty:
        export_dia = teorica_dia_sorted[["ts", "potencia_teorica_kw", "energia_acumulada_kwh"]].rename(
            columns={"energia_acumulada_kwh": "teorica_acumulada_kwh"}
        )
    if not medidor_dia.empty and medidor_dia["direta_kwh"].notna().any():
        medidor_export = medidor_dia_sorted[["ts", "potencia_real_kw", "direta_kwh", "energia_acumulada_kwh"]].rename(
            columns={"energia_acumulada_kwh": "medidor_acumulada_kwh"}
        )
        export_dia = pd.merge(export_dia, medidor_export, on="ts", how="outer") if not export_dia.empty else medidor_export
    export_dia = export_dia.sort_values("ts")
    st.download_button(
        "⬇️ Exportar CSV (potência e acumulado do dia)",
        data=export_dia.to_csv(index=False).encode("utf-8"),
        file_name=f"potencia_dia_{usina['slug']}_{dia_unico}.csv",
        mime="text/csv",
    )
