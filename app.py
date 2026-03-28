iimport streamlit as st
import biosteam as bst
import thermosteam as tmo
import pandas as pd
import google.generativeai as genai
from PIL import Image
import os
import uuid

# =================================================================
# 1. CONFIGURACIÓN DE LA PÁGINA
# =================================================================
st.set_page_config(page_title="BioSTEAM Web Simulator", layout="wide")

# =================================================================
# 2. FUNCIÓN MAESTRA DE SIMULACIÓN
# =================================================================
def correr_simulacion(flow_water, flow_eth, temp_mosto, pres_mosto, T_flash, P_flash, 
                      precio_elec, precio_vapor, precio_agua, precio_mp):
    
    # IMPORTANTE: Limpiar el flowsheet para que Streamlit no duplique IDs al recargar
    bst.main_flowsheet.clear()
    
    # Configuración de compuestos y termodinámica
    chemicals = tmo.Chemicals(["Water", "Ethanol"])
    bst.settings.set_thermo(chemicals)

    # Configuración global de precios (Utilities)
    bst.PowerUtility.price = precio_elec # $/kWh
    
    vapor = bst.HeatUtility.get_agent("low_pressure_steam")
    vapor.heat_transfer_price = precio_vapor # $/MJ
    
    agua = bst.HeatUtility.get_agent("cooling_water")
    agua.heat_transfer_price = precio_agua # $/MJ

    # --- DEFINICIÓN DE CORRIENTES ---
    mosto = bst.Stream("1_MOSTO",
                       Water=flow_water, Ethanol=flow_eth, units="kg/hr",
                       T=temp_mosto + 273.15, 
                       P=pres_mosto * 101325)
    mosto.price = precio_mp

    vinazas_retorno = bst.Stream("Vinazas_Retorno", T=95+273.15, P=3*101325)

    # --- DEFINICIÓN DE EQUIPOS ---
    P100 = bst.Pump("P100", ins=mosto, P=4*101325)
    
    W210 = bst.HXprocess("W210", ins=(P100-0, vinazas_retorno), 
                        outs=("3_Mosto_Pre", "Drenaje"),
                        phase0="l", phase1="l")
    W210.outs[0].T = 85 + 273.15

    W220 = bst.HXutility("W220", ins=W210-0, outs="Mezcla", T=T_flash+273.15)
    
    V100 = bst.IsenthalpicValve("V100", ins=W220-0, outs="Mezcla_Bifasica", P=P_flash*101325)

    V1 = bst.Flash("V1", ins=V100-0, outs=("Vapor_caliente", "Vinazas"), P=P_flash*101325, Q=0)

    W310 = bst.HXutility("W310", ins=V1-0, outs="Producto_Final", T=25+273.15)
    producto = W310.outs[0]
    producto.price = 1.2 # Precio de venta fijo

    P200 = bst.Pump("P200", ins=V1-1, outs=vinazas_retorno, P=3*101325)

    # --- SIMULACIÓN DEL SISTEMA ---
    eth_sys = bst.System("planta_etanol", path=(P100, W210, W220, V100, V1, W310, P200))
    
    try:
        eth_sys.simulate()
    except Exception as e:
        return None, None, None, None, f"Error en simulación: {e}"

    # --- GENERACIÓN DE REPORTE DE MATERIA ---
    datos_mat = []
    for s in eth_sys.streams:
        if s.F_mass > 0.001:
            datos_mat.append({
                "Corriente": s.ID,
                "Temp (°C)": round(s.T - 273.15, 2),
                "Flujo (kg/h)": round(s.F_mass, 2),
                "% Etanol": f"{(s.imass['Ethanol']/s.F_mass if s.F_mass >0 else 0):.1%}",
                "% Agua": f"{(s.imass['Water']/s.F_mass if s.F_mass > 0 else 0):.1%}"
            })
    df_mat = pd.DataFrame(datos_mat)

    # --- GENERACIÓN DE REPORTE DE ENERGÍA (CORREGIDO) ---
    datos_en = []
    for u in eth_sys.units:
        calor_kw = 0.0
        tipo = "-"
        
        if isinstance(u, bst.HXprocess):
            calor_kw = (u.outs[0].H - u.ins[0].H) / 3600
            tipo = "Recuperación Interna"
        elif hasattr(u, "heat_utilities") and u.heat_utilities:
            # Sumatoria de duties de todos los agentes de servicio
            calor_kw = sum([hu.duty for hu in u.heat_utilities]) / 3600
            if calor_kw > 0.1: tipo = "Calentamiento (Vapor)"
            elif calor_kw < -0.1: tipo = "Enfriamiento (Agua)"

        potencia = u.power_utility.rate if u.power_utility else 0.0

        if abs(calor_kw) > 0.01:
            datos_en.append({"Equipo": u.ID, "Función": tipo, "Calor (kW)": round(calor_kw, 2)})
        if potencia > 0.01:
            datos_en.append({"Equipo": u.ID, "Función": "Motor Eléctrico", "Potencia (kW)": round(potencia, 2)})
            
    df_en = pd.DataFrame(datos_en)

    # --- ANÁLISIS ECONÓMICO (TEA) ---
    class TEA_Didactico(bst.TEA):
        def _DPI(self, installed_equipment_cost): return self.purchase_cost
        def _TDC(self, DPI): return DPI
        def _FCI(self, TDC): return self.purchase_cost * self.lang_factor
        def _TCI(self, FCI): return FCI + self.WC
        def _FOC(self, FCI): return 0.0
        @property
        def VOC(self):
            return getattr(self.system, 'material_cost', 0) + getattr(self.system, 'utility_cost', 0)

    tea = TEA_Didactico(system=eth_sys, IRR=0.15, duration=(2025, 2045), income_tax=0.3,
                        depreciation="MACRS7", construction_schedule=(0.4, 0.6), operating_days=330,
                        lang_factor=4.0, WC_over_FCI=0.05)
    
    tea.IRR = 0.0
    costo_prod = tea.solve_price(producto)
    tea.IRR = 0.15
    precio_venta = tea.solve_price(producto)
    
    ind_econ = {
        "Costo Producción ($/kg)": round(costo_prod, 3),
        "Precio Venta Meta ($/kg)": round(precio_venta, 3),
        "NPV (MUSD)": round(tea.NPV / 1e6, 2),
        "PBP (Años)": round(tea.PBP, 1),
        "ROI (%)": round(tea.ROI * 100, 1)
    }

    # --- DIAGRAMA (PFD) ---
    pfd_filename = f"pfd_{uuid.uuid4().hex[:8]}"
    try:
        eth_sys.diagram(file=pfd_filename, format="png", display=False)
        pfd_path = pfd_filename + ".png"
    except:
        pfd_path = None

    return df_mat, df_en, ind_econ, pfd_path, None

# =================================================================
# 3. INTERFAZ STREAMLIT
# =================================================================
st.title("🧪 Simulador BioSTEAM + IA Gemini")

# Sidebar
st.sidebar.header("Configuración")
f_w = st.sidebar.slider("Agua (kg/h)", 500, 2000, 900)
f_e = st.sidebar.slider("Etanol (kg/h)", 50, 300, 100)
t_f = st.sidebar.slider("Temp. Flash (°C)", 80, 110, 92)
p_f = st.sidebar.slider("Presión Flash (atm)", 0.5, 2.0, 1.0)

st.sidebar.subheader("Precios")
p_v = st.sidebar.number_input("Vapor ($/MJ)", value=0.025, format="%.4f")
p_mp = st.sidebar.number_input("Materia Prima ($/kg)", value=0.05, format="%.3f")

if st.sidebar.button("Simular Proceso", type="primary"):
    df_m, df_e_en, econ, pfd, err = correr_simulacion(f_w, f_e, 25, 1, t_f, p_f, 0.085, p_v, 0.0005, p_mp)
    
    if err:
        st.error(err)
    else:
        # Mostrar PFD
        if pfd and os.path.exists(pfd):
            st.image(pfd, caption="Diagrama de Flujo")
            os.remove(pfd)

        col1, col2 = st.columns(2)
        with col1:
            st.subheader("Balances")
            st.dataframe(df_m)
            st.subheader("Economía")
            st.table(pd.DataFrame(list(econ.items()), columns=["Métrica", "Valor"]))
        
        with col2:
            st.subheader("Energía")
            st.dataframe(df_e_en)
            
            # IA Gemini
            st.divider()
            st.subheader("🤖 Tutor IA")
            api_key = st.secrets.get("GEMINI_API_KEY")
            if api_key:
                genai.configure(api_key=api_key)
                model = genai.GenerativeModel('gemini-pro')
                prompt = f"Analiza estos resultados de simulación de etanol: {econ}. Explica si es rentable y por qué."
                if st.button("Consultar IA"):
                    res = model.generate_content(prompt)
                    st.write(res.text)
            else:
                st.info("Configura GEMINI_API_KEY en Secrets para usar la IA.")
