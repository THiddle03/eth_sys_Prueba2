import streamlit as st
import biosteam as bst
import thermosteam as tmo
import pandas as pd
import google.generativeai as genai
import os
import uuid

# 1. CONFIGURACIÓN DE PÁGINA
st.set_page_config(page_title="Simulador Bioetanol Pro v5", layout="wide")

# 2. FUNCIÓN DE SIMULACIÓN
def correr_simulacion(flow_water, flow_eth, temp_mosto, T_flash, P_flash, 
                      precio_elec, precio_vapor, precio_agua, precio_mp, precio_etanol):
    
    bst.main_flowsheet.clear()
    chemicals = tmo.Chemicals(["Water", "Ethanol"])
    bst.settings.set_thermo(chemicals)

    # Configuración de precios dinámicos
    bst.PowerUtility.price = precio_elec
    vapor = bst.HeatUtility.get_agent("low_pressure_steam")
    vapor.heat_transfer_price = precio_vapor
    agua = bst.HeatUtility.get_agent("cooling_water")
    agua.heat_transfer_price = precio_agua

    # --- CORRIENTES ---
    mosto = bst.Stream("1_MOSTO", Water=flow_water, Ethanol=flow_eth, units="kg/hr",
                       T=temp_mosto + 273.15, P=101325)
    mosto.price = precio_mp
    vinazas_retorno = bst.Stream("Vinazas_Retorno", T=95+273.15, P=3*101325)

    # --- EQUIPOS ---
    P100 = bst.Pump("P100", ins=mosto, P=4*101325)
    W210 = bst.HXprocess("W210", ins=(P100-0, vinazas_retorno), outs=("3_Mosto_Pre", "Drenaje"), phase0="l", phase1="l")
    W210.outs[0].T = 85 + 273.15
    W220 = bst.HXutility("W220", ins=W210-0, outs="Mezcla", T=T_flash+273.15)
    V100 = bst.IsenthalpicValve("V100", ins=W220-0, outs="Mezcla_Bifasica", P=P_flash*101325)
    V1 = bst.Flash("V1", ins=V100-0, outs=("Vapor_caliente", "Vinazas"), P=P_flash*101325, Q=0)
    W310 = bst.HXutility("W310", ins=V1-0, outs="Producto_Final", T=25+273.15)
    
    producto = W310.outs[0]
    producto.price = precio_etanol
    P200 = bst.Pump("P200", ins=V1-1, outs=vinazas_retorno, P=3*101325)

    # --- SISTEMA ---
    eth_sys = bst.System("planta_etanol", path=(P100, W210, W220, V100, V1, W310, P200))
    
    try:
        eth_sys.simulate()
    except Exception as e:
        return None, None, None, None, f"Error: {e}"

    # --- REPORTE MATERIA ---
    datos_mat = []
    for s in eth_sys.streams:
        if s.F_mass > 0.01:
            datos_mat.append({
                "Corriente": s.ID,
                "Temp (°C)": round(s.T - 273.15, 2),
                "Presión (bar)": round(s.P / 100000, 3),
                "Flujo (kg/h)": round(s.F_mass, 2),
                "% Etanol": f"{(s.imass['Ethanol']/s.F_mass if s.F_mass > 0 else 0):.1%}",
                "% Agua": f"{(s.imass['Water']/s.F_mass if s.F_mass > 0 else 0):.1%}"
            })
    
    # --- REPORTE ENERGÍA ---
    datos_en = []
    for u in eth_sys.units:
        calor = sum([hu.duty for hu in u.heat_utilities])/3600 if hasattr(u, "heat_utilities") else 0
        potencia = u.power_utility.rate if u.power_utility else 0
        if abs(calor) > 0.1 or potencia > 0.1:
            datos_en.append({"Equipo": u.ID, "Calor (kW)": round(calor, 2), "Potencia (kW)": round(potencia, 2)})

# --- TEA ROBUSTO (ACTUALIZADO) ---
    class TEA_Robusto(bst.TEA):
        def _DPI(self, installed_equipment_cost): return self.purchase_cost
        def _TDC(self, DPI): return DPI
        def _FCI(self, TDC): return self.purchase_cost * self.lang_factor
        def _TCI(self, FCI): return FCI + self.WC
        def _FOC(self, FCI): return 0.0
        @property
        def VOC(self): return self.system.material_cost + self.system.utility_cost

    tea = TEA_Robusto(
        system=eth_sys, IRR=0.15, duration=(2025, 2045), depreciation='MACRS7',
        income_tax=0.3, operating_days=330, lang_factor=4.0, construction_schedule=(0.4, 0.6),
        WC_over_FCI=0.05, startup_months=6, startup_FOCfrac=0.5, startup_VOCfrac=0.5,
        startup_salesfrac=0.5, finance_interest=0, finance_years=0, finance_fraction=0
    )
    
    tea.IRR = 0.0
    costo_p = tea.solve_price(producto)
    
    # Diccionario con los nuevos parámetros solicitados
    ind_econ = {
        "Costo Producción ($/kg)": round(costo_p, 3),
        "Precio Venta ($/kg)": round(precio_etanol, 3), # Parámetro agregado
        "NPV (MUSD)": round(tea.NPV/1e6, 2),
        "ROI (%)": round(tea.ROI*100, 1),
        "PBP (Años)": round(tea.PBP, 2) # Parámetro agregado
    }
                        
# 3. INTERFAZ DE USUARIO
st.title("🧪 Simulador Bioetanol: Control Termodinámico y Económico")

# BARRA LATERAL
st.sidebar.header("🌡️ Parámetros Proceso")
f_w = st.sidebar.slider("Agua (kg/h)", 100, 2000, 900)
f_e = st.sidebar.slider("Etanol (kg/h)", 50, 300, 100)
t_mosto = st.sidebar.slider("Temp. Mosto (°C)", 10, 50, 25)
t_flash = st.sidebar.slider("Temp. W220 (°C)", 70, 120, 92)
p_flash = st.sidebar.slider("Presión (atm)", 0.1, 3.0, 1.0, step=0.1)

st.sidebar.divider()
st.sidebar.header("💰 Parámetros Económicos")
# Nuevos Sliders Solicitados
p_elec = st.sidebar.slider("Precio Electricidad ($/kWh)", 0.0, 0.25, 0.085, step=0.005)
p_agua_c = st.sidebar.slider("Precio Agua Enfr. ($/MJ)", 0.0001, 0.01, 0.0005, step=0.0001, format="%.4f")
# Sliders mantenidos
p_vapor = st.sidebar.slider("Precio Vapor ($/MJ)", 0.01, 0.10, 0.025, step=0.005)
p_mp = st.sidebar.slider("Precio Materia Prima ($/kg)", 0.001, 0.50, 0.05, step=0.005)
p_etanol = st.sidebar.slider("Precio Venta Etanol ($/kg)", 0.5, 25.0, 1.2, step=1.0)

# Lógica de Simulación
if st.sidebar.button("Simular Proceso", type="primary"):
    dm, de, ec, pf, err = correr_simulacion(f_w, f_e, t_mosto, t_flash, p_flash, 
                                            p_elec, p_vapor, p_agua_c, p_mp, p_etanol)
    if err:
        st.error(err)
    else:
        st.session_state['resultados'] = (dm, de, ec, pf)

# MOSTRAR RESULTADOS
if 'resultados' in st.session_state:
    dm, de, ec, pf = st.session_state['resultados']
    
    if pf and os.path.exists(pf):
        st.image(pf, caption="PFD del Sistema")
    
    col1, col2 = st.columns(2)
    with col1:
        st.subheader("📊 Balances de Materia")
        st.dataframe(dm, use_container_width=True)
        st.subheader("📈 Economía")
        st.table(pd.DataFrame(list(ec.items()), columns=["Indicador", "Valor"]))
        
    with col2:
        st.subheader("⚡ Energía")
        st.dataframe(de, use_container_width=True)
        
        st.divider()
        st.subheader("🤖 Tutor IA Interactivo")
        
        api_key = st.secrets.get("GEMINI_API_KEY")
        if api_key:
            user_question = st.text_input("Hazle una pregunta al tutor sobre los resultados:")
            
            if st.button("Enviar al Tutor"):
                if user_question:
                    with st.spinner('Analizando...'):
                        genai.configure(api_key=api_key)
                        model = genai.GenerativeModel('gemini-pro')
                        contexto = f"""
                        Eres un experto en ingeniería química.
                        Resultados: {dm.to_string()}
                        Economía: {ec}
                        Precios: Elec={p_elec}$, Agua={p_agua_c}$, Vapor={p_vapor}$, MP={p_mp}$.
                        Condiciones: Temp={t_flash}C, Pres={p_flash}atm.
                        Responde en <250 palabras de forma didáctica.
                        """
                        full_prompt = f"{contexto}\n\nPregunta: {user_question}"
                        try:
                            response = model.generate_content(full_prompt)
                            st.info(response.text)
                        except Exception as e:
                            st.error(f"Error: {e}")
                else:
                    st.warning("Escribe una pregunta.")
        else:
            st.warning("Falta GEMINI_API_KEY.")
